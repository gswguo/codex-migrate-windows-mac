import json
import sqlite3
import tempfile
import unittest
import zipfile
import stat
import os
from pathlib import Path
from fixtures import make_home
from codex_migrate.compatibility import validate_profile
from codex_migrate.discovery import inspect_home
from codex_migrate.exporter import export_bundle
from codex_migrate.bundle import extract_bundle
from codex_migrate.errors import MigrationError

class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.base=Path(self.temp.name); self.fx=make_home(self.base)
    def tearDown(self): self.temp.cleanup()
    def test_exact_profile_and_unknown_index_rejected(self):
        self.assertTrue(validate_profile(self.fx['home']/'state_5.sqlite',self.fx['home']/'thread_history_1.sqlite')['profile_id'])
        with sqlite3.connect(self.fx['home']/'state_5.sqlite') as db: db.execute('CREATE INDEX unknown_index ON threads(title)')
        with self.assertRaises(MigrationError): inspect_home(self.fx['home'])
    def test_selective_clean_export_and_round_trip(self):
        result=export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        manifest=extract_bundle(self.base/'out.zip',self.base/'extracted')
        self.assertEqual([t['id'] for t in manifest['threads']],[self.fx['thread_id']])
        self.assertEqual(result['profile_id'],manifest['profile_id'])
        files=manifest['files']; self.assertFalse(any('.env' in p or 'node_modules' in p for p in files))
        self.assertTrue(any(p.endswith('/hello.txt') for p in files))
        rows=[json.loads(x) for x in (self.base/'extracted/data/state/threads.jsonl').read_text().splitlines()]
        self.assertEqual([r['id'] for r in rows],[self.fx['thread_id']])
        self.assertFalse(any(p.endswith('.sqlite') or p.endswith('auth.json') for p in files))
    def test_selection_must_be_explicit(self):
        with self.assertRaises(MigrationError): export_bundle(self.fx['home'],self.base/'out.zip',check_idle=False)

    def test_manifest_identifiers_and_references_are_validated_before_extraction(self):
        import copy
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        with zipfile.ZipFile(self.base/'out.zip') as source:
            members={name:source.read(name) for name in source.namelist() if name!='manifest.json'}
        mutations=[lambda m:m.update(bundle_id='/outside'),lambda m:m.update(bundle_id='../outside'),
                   lambda m:m['threads'][0].update(id='../outside'),
                   lambda m:m['roots'][0].update(id='C:outside'),
                   lambda m:m['roots'][0].update(archive_path='data/state'),
                   lambda m:m['projects'][0].update(main_thread_ids=['unknown']),
                   lambda m:m['threads'].append(dict(m['threads'][0]))]
        for change in mutations:
            candidate=copy.deepcopy(manifest);change(candidate)
            with zipfile.ZipFile(self.base/'bad.zip','w') as archive:
                archive.writestr('manifest.json',json.dumps(candidate))
                for name,data in members.items():archive.writestr(name,data)
            with self.assertRaises(MigrationError):extract_bundle(self.base/'bad.zip',self.base/'bad')
            self.assertFalse((self.base/'bad').exists())
    def test_input_changes_remove_incomplete_bundle(self):
        from unittest.mock import patch
        import codex_migrate.exporter as exporter
        original=exporter._stream_file
        def changing(path,*args,**kwargs):
            data=original(path,*args,**kwargs)
            if Path(path).name=='hello.txt': Path(path).write_text('changed')
            return data
        with patch.object(exporter,'_stream_file',side_effect=changing):
            with self.assertRaises(MigrationError): export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        self.assertFalse((self.base/'out.zip').exists())
    def test_worktree_rejected(self):
        (self.fx['root']/'.git').write_text('gitdir: ../elsewhere')
        with self.assertRaises(MigrationError): export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
    def test_bundle_rejects_hostile_names_and_undeclared_member(self):
        from codex_migrate.bundle import safe_member_name
        for name in ('projects/CONIN$','projects/COM¹.txt','projects/CONOUT$.txt'):
            with self.assertRaises(MigrationError): safe_member_name(name)
        for name in ['../escape','C:/evil','projects/CON.txt','projects/CONIN$','projects/COM¹.txt','projects/a.','projects/a\\b','undeclared.txt']:
            archive=self.base/'bad.zip'
            with zipfile.ZipFile(archive,'w') as z:
                z.writestr('manifest.json',json.dumps({'format':'codex-migrate/v1','files':{}}));z.writestr(name,b'bad')
            with self.assertRaises(MigrationError): extract_bundle(archive,self.base/'bad')
            self.assertFalse((self.base/'bad').exists())
    def test_bundle_rejects_hash_tamper_duplicate_and_link(self):
        export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        with zipfile.ZipFile(self.base/'out.zip') as source: members={n:source.read(n) for n in source.namelist()}
        file=next(n for n in members if n!='manifest.json');members[file]=b'tampered'
        with zipfile.ZipFile(self.base/'tampered.zip','w') as z:
            for n,v in members.items(): z.writestr(n,v)
        with self.assertRaises(MigrationError): extract_bundle(self.base/'tampered.zip',self.base/'bad')

    def test_source_thread_creator_identity_is_not_transferred(self):
        with sqlite3.connect(self.fx['home']/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET creator_user_id=?, creator_account_id=?',('synthetic-private-user','synthetic-private-account'))
        export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        with zipfile.ZipFile(self.base/'out.zip') as z:
            row=json.loads(z.read('data/state/threads.jsonl'))
        self.assertIsNone(row['creator_user_id']);self.assertIsNone(row['creator_account_id'])

    def test_dependency_closure_does_not_include_unselected_sibling(self):
        # A and B share P: choosing A should export A/P, but not B.
        from fixtures import insert
        with sqlite3.connect(self.fx['home']/'state_5.sqlite') as db:
            db.row_factory=sqlite3.Row
            original=dict(db.execute('SELECT * FROM threads WHERE id=?',(self.fx['thread_id'],)).fetchone())
            for tid in ('synthetic-parent','synthetic-sibling'):
                row=dict(original);row['id']=tid;row['project_id']=None
                path=self.fx['home']/'sessions'/(tid+'.jsonl');path.write_text(json.dumps({'ordinal':0,'type':'session_meta','payload':{'id':tid}})+'\n');row['rollout_path']=str(path)
                insert(db,'threads',row)
            for child in (self.fx['thread_id'],'synthetic-sibling'):
                insert(db,'thread_spawn_edges',{'parent_thread_id':'synthetic-parent','child_thread_id':child,'status':'completed'})
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        self.assertEqual({t['id'] for t in manifest['threads']},{self.fx['thread_id'],'synthetic-parent'})

    def test_shared_project_roots_are_stored_once(self):
        from fixtures import insert
        with sqlite3.connect(self.fx['home']/'state_5.sqlite') as db:
            insert(db,'projects',{'id':'synthetic-second-project','name':'Other','position':1,'created_at_ms':1,'updated_at_ms':1})
            insert(db,'project_roots',{'project_id':'synthetic-second-project','position':0,'path':str(self.fx['root'])})
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id'],'synthetic-second-project'],check_idle=False)
        self.assertEqual(len(manifest['roots']),1)
        self.assertEqual(manifest['projects'][0]['root_ids'],manifest['projects'][1]['root_ids'])

    def test_explicit_generated_image_is_only_external_asset_included(self):
        image=self.fx['home']/'generated_images'/'included.png';image.parent.mkdir();image.write_bytes(b'synthetic image')
        (image.parent/'unselected.png').write_bytes(b'private other image')
        with self.fx['rollout'].open('a') as f: f.write(json.dumps({'ordinal':5,'type':'response_item','payload':{'type':'image','savedPath':str(image)}})+'\n')
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        self.assertEqual([a['source_path'] for a in manifest['assets']],[str(image)])

    def test_bundle_collision_link_and_limits(self):
        import warnings
        from unittest.mock import patch
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        with zipfile.ZipFile(self.base/'out.zip') as z: members={n:z.read(n) for n in z.namelist()}
        hostile=[('projects/a',None),('projects/A',None)]
        for entries in [hostile,[('projects/é.txt',None),('projects/e\u0301.txt',None)],[('projects/link',stat.S_IFLNK|0o777)],[('projects/device',stat.S_IFCHR|0o600)],[('projects/dupe',None),('projects/dupe',None)]]:
            with warnings.catch_warnings():
                warnings.simplefilter('ignore',UserWarning)
                with zipfile.ZipFile(self.base/'bad.zip','w') as z:
                    for name,data in members.items(): z.writestr(name,data)
                    for name,mode in entries:
                        info=zipfile.ZipInfo(name)
                        if mode: info.external_attr=mode<<16
                        z.writestr(info,b'bad')
            with self.assertRaises(MigrationError): extract_bundle(self.base/'bad.zip',self.base/'bad')
        with patch('codex_migrate.bundle.MAX_MEMBERS',1):
            with self.assertRaises(MigrationError): extract_bundle(self.base/'out.zip',self.base/'bad')

    def test_existing_output_and_extraction_destination_preserved(self):
        out=self.base/'out.zip';out.write_bytes(b'keep')
        with self.assertRaises(MigrationError): export_bundle(self.fx['home'],out,thread_ids=[self.fx['thread_id']],check_idle=False)
        self.assertEqual(out.read_bytes(),b'keep')
        target=self.base/'extract';target.mkdir();(target/'keep').write_text('keep')
        with self.assertRaises(MigrationError): extract_bundle(out,target)
        self.assertEqual((target/'keep').read_text(),'keep')

    def test_missing_dependency_and_symlink_fail_closed(self):
        with self.fx['rollout'].open('a') as f: f.write(json.dumps({'ordinal':5,'type':'history_base','payload':{'history_base':{'thread_id':'synthetic-missing','end_ordinal_exclusive':1}}})+'\n')
        with self.assertRaises(MigrationError): export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        self.fx['rollout'].write_text(json.dumps({'ordinal':0,'type':'session_meta','payload':{}})+'\n')
        (self.fx['root']/'link').symlink_to(self.base/'elsewhere')
        with self.assertRaises(MigrationError): export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)

    def test_sidebar_only_membership_is_exported_without_global_state(self):
        legacy='synthetic-legacy-project'
        with sqlite3.connect(self.fx['home']/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET project_id=NULL WHERE id=?',(self.fx['thread_id'],))
        global_state={'local-projects':{legacy:{'id':legacy,'name':'Synthetic sidebar','rootPaths':[str(self.fx['root'])],'createdAt':1,'updatedAt':2}},'app-server-project-id-by-legacy-project-id-by-host':{'local:'+str(self.fx['home']):{legacy:self.fx['project_id']}},'thread-project-assignments':{self.fx['thread_id']:{'projectId':legacy,'projectKind':'local'}},'thread-project-membership-host-ids':{self.fx['thread_id']:'local'},'project-order':[legacy],'private-unrelated-token':'must never export'}
        (self.fx['home']/'.codex-global-state.json').write_text(json.dumps(global_state))
        inv=inspect_home(self.fx['home'])
        p=next(p for p in inv['projects'] if p['id']==self.fx['project_id'])
        self.assertEqual(p['legacy_id'],legacy);self.assertEqual(p['main_thread_ids'],[self.fx['thread_id']])
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        self.assertEqual([t['id'] for t in manifest['threads']],[self.fx['thread_id']])
        with zipfile.ZipFile(self.base/'out.zip') as z:
            self.assertNotIn('must never export',str({n:z.read(n) for n in z.namelist()}))
            self.assertEqual(json.loads(z.read('data/state/threads.jsonl'))['project_id'],self.fx['project_id'])

    def test_export_does_not_delete_concurrently_created_output(self):
        from unittest.mock import patch
        original=Path.open
        out=self.base/'out.zip'
        def concurrent_output(path,mode='r',*args,**kwargs):
            if path==out and mode=='xb':
                with original(out,'wb') as stream: stream.write(b'another process owns this file')
            return original(path,mode,*args,**kwargs)
        with patch.object(Path,'open',new=concurrent_output):
            with self.assertRaises(MigrationError): export_bundle(self.fx['home'],out,thread_ids=[self.fx['thread_id']],check_idle=False)
        self.assertEqual(out.read_bytes(),b'another process owns this file')

    def test_archive_parent_directory_case_collision_is_rejected(self):
        import hashlib
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        with zipfile.ZipFile(self.base/'out.zip') as z: members={n:z.read(n) for n in z.namelist() if n!='manifest.json'}
        for name in ('projects/X/a.txt','projects/x/b.txt'):
            members[name]=b'X';manifest['files'][name]={'sha256':hashlib.sha256(b'X').hexdigest(),'size':1}
        with zipfile.ZipFile(self.base/'bad.zip','w') as z:
            z.writestr('manifest.json',json.dumps(manifest))
            for name,data in members.items(): z.writestr(name,data)
        with self.assertRaises(MigrationError): extract_bundle(self.base/'bad.zip',self.base/'extracted')

    def test_repetitive_project_file_produces_extractable_bundle(self):
        (self.fx['root']/'repeated.txt').write_bytes(b'x'*(4*1024*1024))
        export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        extract_bundle(self.base/'out.zip',self.base/'extract')

    def test_project_assets_and_events_are_streamed_in_bounded_chunks(self):
        from unittest.mock import patch
        import codex_migrate.exporter as exporter
        payload=self.fx['root']/'streamed.bin';payload.write_bytes(b'0123456789'*10000)
        image=self.fx['home']/'generated_images'/'streamed.png';image.parent.mkdir();image.write_bytes(b'synthetic-image'*1000)
        with self.fx['rollout'].open('a') as f: f.write(json.dumps({'ordinal':5,'type':'response_item','payload':{'type':'image','savedPath':str(image)}})+'\n')
        original=Path.read_bytes
        def no_whole_file(path):
            if path in (payload,image,self.fx['rollout']): raise AssertionError('Selected source must stream, never Path.read_bytes')
            return original(path)
        writes=[]
        real_write=zipfile._ZipWriteFile.write
        def observe_write(writer,data):
            if writer._zinfo.filename!='manifest.json': writes.append(len(data))
            return real_write(writer,data)
        with patch.object(Path,'read_bytes',new=no_whole_file),patch.object(exporter,'STREAM_CHUNK_SIZE',97),patch.object(zipfile._ZipWriteFile,'write',new=observe_write):
            manifest=export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        self.assertGreater(len(writes),1000);self.assertLessEqual(max(writes),97)
        extract_bundle(self.base/'out.zip',self.base/'extract')
        self.assertEqual(manifest['files']['projects/'+self.fx['root_id']+'/streamed.bin']['size'],100000)

    def test_project_root_cannot_copy_native_home_or_external_sqlite_home(self):
        # Even an explicitly selected project must not package full private DB pages.
        private=self.fx['home']
        (private/'auth.json').write_text('synthetic auth never packaged')
        with sqlite3.connect(private/'state_5.sqlite') as db:
            db.execute('UPDATE project_roots SET path=?',(str(private),))
        global_path=private/'.codex-global-state.json'
        state=json.loads(global_path.read_text())
        state['local-projects'][self.fx['legacy_id']]['rootPaths']=[str(private)]
        global_path.write_text(json.dumps(state))
        with self.assertRaises(MigrationError): export_bundle(private,self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        self.assertFalse((self.base/'out.zip').exists())

    def test_windows_directory_reparse_points_are_rejected(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        junction=self.fx['root']/'synthetic-junction';junction.mkdir();(junction/'outside.txt').write_text('must not traverse')
        original=Path.lstat
        def lstat(path,*args,**kwargs):
            if path==junction: return SimpleNamespace(st_mode=stat.S_IFDIR|0o755,st_file_attributes=0x400)
            return original(path,*args,**kwargs)
        with patch.object(Path,'lstat',new=lstat),self.assertRaises(MigrationError):
            export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)

    def test_unassigned_thread_cwd_is_an_independent_root_and_can_be_imported(self):
        from codex_migrate.operations import plan_import,apply_plan
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['unassigned_thread_id']],check_idle=False)
        self.assertEqual(manifest['projects'],[]);self.assertEqual(len(manifest['roots']),1)
        target=make_home(self.base/'target',prefix='synthetic-unassigned-target')
        plan=plan_import(self.base/'out.zip',target['home'],self.base/'Imported',self.base/'work',check_idle=False);apply_plan(plan,check_idle=False)
        with sqlite3.connect(target['home']/'state_5.sqlite') as db:
            cwd,pid=db.execute('SELECT cwd,project_id FROM threads WHERE id=?',(self.fx['unassigned_thread_id'],)).fetchone()
        self.assertIsNone(pid);self.assertTrue((Path(cwd)/'hello.txt').is_file())

    def test_unassigned_foreign_or_missing_cwd_fails_closed(self):
        with sqlite3.connect(self.fx['home']/'state_5.sqlite') as db:
            db.execute('UPDATE threads SET cwd=? WHERE id=?',(str(self.base/'missing-local-root'),self.fx['unassigned_thread_id']))
        with self.assertRaises(MigrationError): export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['unassigned_thread_id']],check_idle=False)

    @unittest.skipUnless(os.name!='nt','Owner executable permissions are a POSIX filesystem property')
    def test_owner_executable_bit_survives_extraction_without_unsafe_mode_bits(self):
        script=self.fx['root']/'run.sh';script.write_text('#!/bin/sh\nexit 0\n');script.chmod(0o6751)
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        archive_path='projects/'+self.fx['root_id']+'/run.sh'
        self.assertIs(manifest['files'][archive_path]['executable'],True)
        extract_bundle(self.base/'out.zip',self.base/'extracted')
        self.assertEqual(stat.S_IMODE((self.base/'extracted'/archive_path).stat().st_mode),0o700)
        self.assertEqual(stat.S_IMODE((self.base/'extracted/projects'/self.fx['root_id']/'hello.txt').stat().st_mode),0o600)

    def test_empty_project_root_and_empty_descendants_are_preserved(self):
        (self.fx['root']/'hello.txt').unlink();(self.fx['root']/'empty/nested').mkdir(parents=True)
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        root='projects/'+self.fx['root_id']
        self.assertEqual(set(manifest['directories']),{root,root+'/empty',root+'/empty/nested'})
        extract_bundle(self.base/'out.zip',self.base/'extracted')
        self.assertTrue((self.base/'extracted'/root/'empty/nested').is_dir());self.assertFalse((self.base/'extracted'/root/'node_modules').exists())

    def test_manifest_directory_and_executable_metadata_is_validated(self):
        import copy
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        with zipfile.ZipFile(self.base/'out.zip') as z: members={n:z.read(n) for n in z.namelist() if n!='manifest.json'}
        root='projects/'+self.fx['root_id'];file=root+'/hello.txt'
        changes=[lambda m:m.update(directories=['../escape']),lambda m:m.update(directories=[root+'/Case',root+'/case']),lambda m:m.update(directories=[file]),lambda m:m.update(directories=['events/empty']),lambda m:m['files'][file].update(executable='yes')]
        for change in changes:
            candidate=copy.deepcopy(manifest);change(candidate)
            with zipfile.ZipFile(self.base/'bad.zip','w') as z:
                z.writestr('manifest.json',json.dumps(candidate))
                for name,data in members.items(): z.writestr(name,data)
            with self.assertRaises(MigrationError): extract_bundle(self.base/'bad.zip',self.base/'extracted')
            self.assertFalse((self.base/'extracted').exists())

    def test_optional_directory_and_executable_fields_allow_older_bundles(self):
        manifest=export_bundle(self.fx['home'],self.base/'out.zip',thread_ids=[self.fx['thread_id']],check_idle=False)
        manifest.pop('directories',None)
        for meta in manifest['files'].values(): meta.pop('executable',None)
        with zipfile.ZipFile(self.base/'out.zip') as z: members={n:z.read(n) for n in z.namelist() if n!='manifest.json'}
        with zipfile.ZipFile(self.base/'older.zip','w') as z:
            z.writestr('manifest.json',json.dumps(manifest))
            for name,data in members.items(): z.writestr(name,data)
        extract_bundle(self.base/'older.zip',self.base/'extracted')

    def test_unreadable_project_tree_fails_closed(self):
        from unittest.mock import patch
        def inaccessible_walk(root,*,followlinks=False,onerror=None):
            if onerror: onerror(PermissionError('synthetic denied directory'))
            return iter(())
        with patch('codex_migrate.exporter.os.walk',new=inaccessible_walk),self.assertRaises(MigrationError):
            export_bundle(self.fx['home'],self.base/'out.zip',project_ids=[self.fx['project_id']],check_idle=False)
        self.assertFalse((self.base/'out.zip').exists())

if __name__=='__main__': unittest.main()
