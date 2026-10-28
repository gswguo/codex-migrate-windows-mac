"""End-to-end workflows over generated native schemas, never the real Codex home."""
import json
import os
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

from fixtures import make_home
from codex_migrate.common import read_json, sha256_file
from codex_migrate.errors import MigrationError
from codex_migrate.exporter import export_bundle


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = make_home(self.root / 'source', prefix='source')
        self.target = make_home(self.root / 'target', prefix='target')
        self.bundle = self.root / 'package.zip'
        export_bundle(self.source['home'], self.bundle,
                      project_ids=[self.source['project_id']], check_idle=False)

    def test_import_preserves_existing_data_and_rewrites_native_links(self):
        from codex_migrate.operations import plan_import, apply_plan, verify_plan
        old_rollout = self.target['rollout'].read_bytes()
        old_source = sha256_file(self.source['rollout'])
        plan = plan_import(self.bundle, self.target['home'], self.root / 'Projects',
                           self.root / 'work', check_idle=False)
        self.assertFalse((self.root / 'Projects').exists())
        preview = read_json(plan)
        self.assertEqual(preview['mode'], 'import')
        result = apply_plan(plan, check_idle=False)
        self.assertEqual(result['status'], 'completed')
        self.assertTrue(verify_plan(plan)['ok'])
        self.assertEqual(self.target['rollout'].read_bytes(), old_rollout)
        self.assertEqual(sha256_file(self.source['rollout']), old_source)
        with sqlite3.connect(self.target['home'] / 'state_5.sqlite') as state:
            self.assertEqual(state.execute('SELECT count(*) FROM threads').fetchone()[0], 3)
            cwd, rollout = state.execute('SELECT cwd,rollout_path FROM threads WHERE id=?', (self.source['thread_id'],)).fetchone()
        self.assertTrue((Path(cwd) / 'hello.txt').is_file())
        self.assertFalse((Path(cwd) / '.env').exists())
        self.assertTrue(Path(rollout).is_file())
        self.assertRegex(Path(rollout).name, r'^rollout-\d{4}-\d\d-\d\dT\d\d-\d\d-\d\d-[0-9a-f-]+\.jsonl$')
        with sqlite3.connect(self.target['home'] / 'thread_history_1.sqlite') as history:
            item = json.loads(history.execute('SELECT item_json FROM thread_items WHERE thread_id=?', (self.source['thread_id'],)).fetchone()[0])
            self.assertIn(cwd, json.dumps(item, ensure_ascii=False))
            self.assertNotIn(str(self.source['root']), json.dumps(item, ensure_ascii=False))
        global_state = read_json(self.target['home'] / '.codex-global-state.json')
        self.assertIn(self.source['thread_id'], global_state['thread-project-assignments'])
        global_before = json.dumps(global_state, sort_keys=True)
        self.assertEqual(apply_plan(plan, check_idle=False)['status'], 'completed')
        self.assertEqual(json.dumps(read_json(self.target['home'] / '.codex-global-state.json'), sort_keys=True), global_before)

    def test_changed_target_or_stage_refuses_apply_without_overwriting(self):
        from codex_migrate.operations import plan_import, apply_plan
        plan = plan_import(self.bundle, self.target['home'], self.root / 'Projects',
                           self.root / 'work', check_idle=False)
        p = self.target['home'] / '.codex-global-state.json'
        original = read_json(p)
        original['user-new-change'] = True
        p.write_text(json.dumps(original))
        changed = p.read_bytes()
        with self.assertRaises(MigrationError):
            apply_plan(plan, check_idle=False)
        self.assertEqual(p.read_bytes(), changed)
        self.assertFalse((self.root / 'Projects').exists())

    def test_existing_thread_collision_is_not_an_implicit_sync(self):
        from codex_migrate.operations import plan_import
        with self.assertRaises(MigrationError):
            plan_import(self.bundle, self.source['home'], self.root / 'Projects',
                        self.root / 'collision-work', check_idle=False)

    def test_unchanged_project_files_remain_guarded_before_and_after_apply(self):
        from codex_migrate.operations import plan_import, apply_plan, verify_plan
        dest=self.root/'Projects'/self.source['root_id'];dest.mkdir(parents=True)
        f=dest/'hello.txt';f.write_bytes((self.source['root']/'hello.txt').read_bytes())
        plan=plan_import(self.bundle,self.target['home'],self.root/'Projects',self.root/'work',check_idle=False)
        f.write_text('later change')
        with self.assertRaises(MigrationError):apply_plan(plan,check_idle=False)
        f.write_bytes((self.source['root']/'hello.txt').read_bytes())
        apply_plan(plan,check_idle=False)
        f.unlink()
        self.assertFalse(verify_plan(plan)['ok'])

    def test_supported_attachment_paths_follow_copied_generated_image(self):
        from fixtures import insert
        from codex_migrate.operations import plan_import,apply_plan
        image=self.source['home']/'generated_images'/'example.png';image.parent.mkdir();image.write_bytes(b'synthetic')
        with sqlite3.connect(self.source['home']/'state_5.sqlite') as db:
            insert(db,'thread_attachments',{'id':str(uuid.uuid4()),'thread_id':self.source['thread_id'],'attachment_type':'generated_image','identity_key':str(image),'payload':json.dumps({'savedPath':str(image)}),'created_at':1})
        other=self.root/'with-image.zip'
        export_bundle(self.source['home'],other,project_ids=[self.source['project_id']],check_idle=False)
        plan=plan_import(other,self.target['home'],self.root/'Projects',self.root/'work',check_idle=False)
        apply_plan(plan,check_idle=False)
        with sqlite3.connect(self.target['home']/'state_5.sqlite') as db:
            key,payload=db.execute('SELECT identity_key,payload FROM thread_attachments WHERE thread_id=?',(self.source['thread_id'],)).fetchone()
        saved=json.loads(payload)['savedPath']
        self.assertNotEqual(saved,str(image));self.assertEqual(key,saved)
        self.assertEqual(Path(saved).read_bytes(),b'synthetic')

    @unittest.skipIf(os.name=='nt','POSIX executable permissions')
    def test_identical_file_with_different_execute_bit_is_not_silently_reused(self):
        from codex_migrate.operations import plan_import
        source=self.source['root']/'run.sh';source.write_text('#!/bin/sh\nexit 0\n');source.chmod(0o755)
        bundle=self.root/'executable.zip';export_bundle(self.source['home'],bundle,project_ids=[self.source['project_id']],check_idle=False)
        dest=self.root/'Projects'/self.source['root_id'];dest.mkdir(parents=True)
        retained=dest/'run.sh';retained.write_bytes(source.read_bytes());retained.chmod(0o600)
        with self.assertRaises(MigrationError):plan_import(bundle,self.target['home'],self.root/'Projects',self.root/'work',check_idle=False)

    def test_empty_project_root_and_child_directory_are_published(self):
        from codex_migrate.operations import plan_import,apply_plan,verify_plan
        (self.source['root']/'hello.txt').unlink();(self.source['root']/'empty'/'nested').mkdir(parents=True)
        bundle=self.root/'empty.zip';export_bundle(self.source['home'],bundle,project_ids=[self.source['project_id']],check_idle=False)
        plan=plan_import(bundle,self.target['home'],self.root/'Projects',self.root/'work',check_idle=False)
        apply_plan(plan,check_idle=False)
        self.assertTrue((self.root/'Projects'/self.source['root_id']/'empty'/'nested').is_dir())
        self.assertTrue(verify_plan(plan)['ok'])

    def test_repair_context_parent_does_not_select_sibling_or_rewrite_parent(self):
        from fixtures import insert
        from codex_migrate.operations import plan_link_repair,apply_plan
        child=self.target['thread_id'];parent=self.target['unassigned_thread_id'];sibling=str(uuid.uuid4())
        parent_path=self.target['rollouts'][parent]
        sibling_path=parent_path.parent/('rollout-2026-01-01T00-00-00-'+sibling+'.jsonl')
        records=[json.loads(x) for x in parent_path.read_text().splitlines()]
        records[0]['payload'].update(id=sibling,history_base={'thread_id':parent,'end_ordinal_exclusive':5,'end_byte_offset':parent_path.stat().st_size})
        sibling_path.write_text(''.join(json.dumps(x)+'\n' for x in records))
        with sqlite3.connect(self.target['home']/'state_5.sqlite') as db:
            db.row_factory=sqlite3.Row
            row=dict(db.execute('SELECT * FROM threads WHERE id=?',(parent,)).fetchone());row.update(id=sibling,rollout_path=str(sibling_path))
            insert(db,'threads',row)
        child_path=self.target['rollout'];records=[json.loads(x) for x in child_path.read_text().splitlines()]
        records[0]['payload']['history_base']={'thread_id':parent,'end_ordinal_exclusive':5,'end_byte_offset':parent_path.stat().st_size}
        child_path.write_text(''.join(json.dumps(x)+'\n' for x in records))
        originals=[parent_path.read_bytes(),sibling_path.read_bytes()]
        plan=plan_link_repair(self.target['home'],[('E:/Research/fixture',str(self.target['root']))],self.root/'repair-work',thread_ids=[child],check_idle=False)
        preview=read_json(plan)
        self.assertEqual(preview['thread_ids'],[child]);self.assertEqual(preview['context_thread_ids'],[parent])
        apply_plan(plan,check_idle=False)
        self.assertEqual([parent_path.read_bytes(),sibling_path.read_bytes()],originals)

    def test_repair_links_changes_both_event_and_projection_preserves_code(self):
        from codex_migrate.operations import plan_link_repair, apply_plan
        old = 'E:/Research/fixture'
        message = 'Open [file](' + old + '/hello.txt). Example `' + old + '/hello.txt`.'
        rollout = self.target['rollout']
        events = [json.loads(line) for line in rollout.read_text().splitlines()]
        events[3]['payload'] = {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': message}]}
        rollout.write_text(''.join(json.dumps(x) + '\n' for x in events))
        with sqlite3.connect(self.target['home'] / 'thread_history_1.sqlite') as db:
            row = db.execute('SELECT item_json FROM thread_items WHERE thread_id=?', (self.target['thread_id'],)).fetchone()
            item = json.loads(row[0]); item['content'][0]['text'] = message
            db.execute('UPDATE thread_items SET item_json=? WHERE thread_id=?', (json.dumps(item), self.target['thread_id']))
        plan = plan_link_repair(self.target['home'], [(old, str(self.target['root']))],
                                self.root / 'repair-work', thread_ids=[self.target['thread_id']], check_idle=False)
        self.assertIn(old, rollout.read_text())  # planning is not a live edit
        apply_plan(plan, check_idle=False)
        with sqlite3.connect(self.target['home'] / 'thread_history_1.sqlite') as db:
            content = json.loads(db.execute('SELECT item_json FROM thread_items WHERE thread_id=?', (self.target['thread_id'],)).fetchone()[0])['content'][0]['text']
            end = db.execute('SELECT rollout_end_byte_offset FROM thread_turns WHERE thread_id=?', (self.target['thread_id'],)).fetchone()[0]
        self.assertIn(str(self.target['root'] / 'hello.txt'), content)
        self.assertIn('`' + old + '/hello.txt`', content)
        self.assertEqual(end, rollout.stat().st_size)
        self.assertIn(str(self.target['root']), rollout.read_text())


if __name__ == '__main__':
    unittest.main()
