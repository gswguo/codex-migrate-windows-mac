"""Real subprocess protocol tests; the service reads synthetic fixture rows only."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fixtures import make_home,insert,managed_connection
from codex_migrate.errors import MigrationError
from codex_migrate.native_verify import verify_native

FAKE_SERVER=r'''
import json,os,sqlite3,sys,time
from pathlib import Path
home=Path(os.environ['CODEX_HOME']);mode=json.loads((home/'fake-mode.json').read_text())
s=sqlite3.connect(home/'state_5.sqlite');s.row_factory=sqlite3.Row
h=sqlite3.connect(home/'thread_history_1.sqlite');h.row_factory=sqlite3.Row
with (home/'fake-env.json').open('w') as f: json.dump(dict(os.environ),f)
for line in sys.stdin:
 r=json.loads(line);method=r['method'];p=r.get('params',{})
 with (home/'fake-requests.jsonl').open('a') as f: f.write(json.dumps(r)+'\n')
 if 'id' not in r: continue
 if mode=='timeout' and method=='thread/read': time.sleep(30)
 if mode=='rpc-error' and method=='thread/items/list':
  print(json.dumps({'id':r['id'],'error':{'code':-1,'message':'synthetic private error'}}),flush=True);continue
 if method=='initialize': result={'userAgent':'synthetic native service'}
 elif method=='thread/list':
  rows=[dict(x) for x in s.execute('SELECT id FROM threads ORDER BY id')];start=int(p.get('cursor','0'));result={'data':rows[start:start+1],'nextCursor':str(start+1) if start+1<len(rows) else None}
 elif method in ('thread/read','thread/resume'):
  t=dict(s.execute('SELECT id,cwd FROM threads WHERE id=?',(p['threadId'],)).fetchone());result={'thread':t}
  if mode=='cwd-mismatch': t['cwd']=str(home/'missing')
 elif method=='thread/turns/list':
  rows=[{'id':x['turn_id']} for x in h.execute('SELECT turn_id FROM thread_turns WHERE thread_id=? ORDER BY turn_id',(p['threadId'],))]
  if mode=='duplicate-turn': rows=rows+rows
  start=int(p.get('cursor','0'));result={'data':rows[start:start+1],'nextCursor':str(start+1) if start+1<len(rows) else None}
  if mode=='cursor-loop': result['nextCursor']='0'
 elif method=='thread/items/list':
  rows=[{'item':json.loads(x['item_json'])} for x in h.execute('SELECT item_json FROM thread_items WHERE thread_id=? AND turn_id=? ORDER BY item_id',(p['threadId'],p['turnId']))]
  if mode=='missing-item': rows=[]
  if mode=='duplicate-item': rows=rows+rows
  if mode=='unknown-item': rows=[{'item':{'id':'synthetic-unknown','type':'userMessage'}}]
  start=int(p.get('cursor','0'));result={'data':rows[start:start+1],'nextCursor':str(start+1) if start+1<len(rows) else None}
 else: raise RuntimeError('Forbidden native method: '+method)
 print(json.dumps({'id':r['id'],'result':result}),flush=True)
s.close();h.close()
'''

class NativeVerifyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name).resolve();self.fx=make_home(self.base)
        self.home=self.fx['home'];self.cli=self.base/'fake-codex.py';self.cli.write_text(FAKE_SERVER)
        (self.home/'.codex-migrate-disposable').write_text(json.dumps({'format':'codex-migrate-disposable/v1'}))
        (self.home/'fake-mode.json').write_text(json.dumps('normal'))
        real_popen=subprocess.Popen
        def python_service(command,**kwargs):
            self.assertEqual(command[0],str(self.cli));self.assertEqual(command[1:3],['app-server','--stdio'])
            return real_popen([sys.executable,str(self.cli)],**kwargs)
        self.popen=patch('codex_migrate.native_verify.subprocess.Popen',side_effect=python_service);self.popen.start()
    def tearDown(self): self.popen.stop();self.temp.cleanup()
    def verify(self,**kwargs): return verify_native(self.home,cli=self.cli,**kwargs)
    def test_reads_all_pages_and_optional_resume_without_model_turns(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'never-inherit','SYNTHETIC_TOKEN':'never-inherit'}):
            report=self.verify(resume=True)
        self.assertTrue(report['passed']);self.assertEqual(report['threads_read'],2);self.assertEqual(report['threads_resumed'],2)
        self.assertEqual(report['logical_turns'],2);self.assertEqual(report['logical_items'],2)
        requests=[json.loads(x) for x in (self.home/'fake-requests.jsonl').read_text().splitlines()]
        self.assertTrue(any(r.get('params',{}).get('cursor') for r in requests))
        self.assertLessEqual({r['method'] for r in requests},{'initialize','initialized','thread/list','thread/read','thread/resume','thread/turns/list','thread/items/list'})
        env=json.loads((self.home/'fake-env.json').read_text());self.assertNotIn('OPENAI_API_KEY',env);self.assertNotIn('SYNTHETIC_TOKEN',env)
        self.assertEqual(env['CODEX_HOME'],str(self.home));self.assertEqual(env['CODEX_SQLITE_HOME'],str(self.home))
    def test_explicit_selection_does_not_resume_by_default(self):
        report=self.verify(thread_ids=[self.fx['thread_id']]);self.assertEqual(report['threads_read'],1);self.assertEqual(report['threads_resumed'],0)
    def test_duplicate_ids_cursor_loops_unknown_items_and_rpc_errors_fail(self):
        for mode in ('duplicate-turn','duplicate-item','cursor-loop','unknown-item','missing-item','rpc-error','cwd-mismatch'):
            with self.subTest(mode=mode):
                (self.home/'fake-mode.json').write_text(json.dumps(mode))
                with self.assertRaises(MigrationError): self.verify(thread_ids=[self.fx['thread_id']])
    def test_timeout_is_bounded_and_service_is_closed(self):
        (self.home/'fake-mode.json').write_text(json.dumps('timeout'))
        with patch('codex_migrate.native_verify.REQUEST_TIMEOUT',0.05),self.assertRaises(MigrationError) as error: self.verify()
        self.assertEqual(error.exception.code,'native_rpc_timeout')
    def test_marker_credentials_real_default_and_effective_home_are_refused(self):
        marker=self.home/'.codex-migrate-disposable';marker.unlink()
        with self.assertRaises(MigrationError): self.verify()
        marker.write_text(json.dumps({'format':'codex-migrate-disposable/v1'}))
        for filename in ('auth.json','config.toml'):
            p=self.home/filename;p.write_text('synthetic do not read')
            with self.assertRaises(MigrationError): self.verify()
            p.unlink()
        with patch.dict(os.environ,{'CODEX_HOME':str(self.home)}):
            with self.assertRaises(MigrationError): self.verify()
        with patch('codex_migrate.native_verify.Path.home',return_value=self.home.parent):
            native=self.home.parent/'.codex';native.mkdir();(native/'.codex-migrate-disposable').write_text(marker.read_text())
            with self.assertRaises(MigrationError): verify_native(native,cli=self.cli)
    def test_unknown_thread_id_and_symlinked_database_are_refused(self):
        with self.assertRaises(MigrationError): self.verify(thread_ids=['synthetic-missing'])
        db=self.home/'state_5.sqlite';moved=self.base/'external.sqlite';db.rename(moved);db.symlink_to(moved)
        with self.assertRaises(MigrationError): self.verify()

    def test_rollout_paths_must_be_regular_files_inside_disposable_copy(self):
        external=self.base/'external.jsonl';external.write_text(self.fx['rollout'].read_text())
        with managed_connection(self.home/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?',(str(external),self.fx['thread_id']))
        with self.assertRaises(MigrationError): self.verify()

    def test_disposable_copy_cannot_share_hardlinked_mutable_files(self):
        os.link(self.home/'state_5.sqlite',self.base/'shared-state.sqlite')
        with self.assertRaises(MigrationError): self.verify()

    def test_synthetic_fixture_has_native_paginated_discovery_metadata(self):
        events=[json.loads(line) for line in self.fx['rollout'].read_text().splitlines()]
        self.assertEqual(events[0]['payload']['history_mode'],'paginated')
        self.assertTrue(self.fx['rollout'].name.startswith('rollout-2026-01-01T00-00-00-'))
        self.assertTrue(any(event['type']=='event_msg' and event['payload'].get('type')=='user_message' for event in events))

    def test_disposable_home_cannot_contain_windows_reparse_directories(self):
        from types import SimpleNamespace
        import stat
        junction=self.home/'synthetic-junction';junction.mkdir()
        original=Path.lstat
        def lstat(path,*args,**kwargs):
            if path==junction: return SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_file_attributes=0x400,st_nlink=1)
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',new=lstat),self.assertRaises(MigrationError): self.verify()


@unittest.skipUnless(os.environ.get('CODEX_MIGRATE_NATIVE_TEST_CLI'),'Actual native CLI test requires explicit opt-in executable')
class NativeActualCliTests(unittest.TestCase):
    def test_synthetic_paginated_fixture_reads_and_resumes_without_turns(self):
        with tempfile.TemporaryDirectory(prefix='codex-migrate-native-synthetic-') as directory:
            fixture=make_home(Path(directory).resolve(),prefix='synthetic-native-rpc')
            (fixture['home']/'.codex-migrate-disposable').write_text(json.dumps({'format':'codex-migrate-disposable/v1'}))
            report=verify_native(fixture['home'],cli=Path(os.environ['CODEX_MIGRATE_NATIVE_TEST_CLI']),resume=True)
            self.assertTrue(report['passed']);self.assertEqual(report['threads_read'],2);self.assertEqual(report['threads_resumed'],2)
            self.assertEqual(report['logical_turns'],2);self.assertEqual(report['logical_items'],2)

    def test_selective_import_reads_and_resumes_with_existing_target_chats(self):
        from codex_migrate.exporter import export_bundle
        from codex_migrate.operations import plan_import,apply_plan
        for scope in ('project','unassigned'):
            with self.subTest(scope=scope),tempfile.TemporaryDirectory(prefix='codex-migrate-native-import-') as directory:
                root=Path(directory).resolve();source=make_home(root/'source',prefix='synthetic-native-source');target=make_home(root/'target',prefix='synthetic-native-target')
                selection={'project_ids':[source['project_id']]} if scope=='project' else {'thread_ids':[source['unassigned_thread_id']]}
                bundle=root/'package.zip';export_bundle(source['home'],bundle,check_idle=False,**selection)
                plan=plan_import(bundle,target['home'],root/'Projects',root/'work',check_idle=False);apply_plan(plan,check_idle=False)
                (target['home']/'.codex-migrate-disposable').write_text(json.dumps({'format':'codex-migrate-disposable/v1'}))
                report=verify_native(target['home'],cli=Path(os.environ['CODEX_MIGRATE_NATIVE_TEST_CLI']),resume=True)
                self.assertTrue(report['passed']);self.assertEqual(report['threads_read'],3);self.assertEqual(report['threads_resumed'],3)
                self.assertEqual(report['logical_turns'],3);self.assertEqual(report['logical_items'],3)

if __name__=='__main__': unittest.main()
