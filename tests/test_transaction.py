import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import stat
import unittest
from unittest.mock import patch

from codex_migrate.errors import MigrationError
from codex_migrate.transaction import prepare_transaction, apply_transaction, recover_transaction, verify_transaction
from codex_migrate import platforms


def digest(value):
    return hashlib.sha256(value).hexdigest()


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.target = self.root / 'target'
        self.stage = self.root / 'stage'
        self.target.mkdir()
        self.stage.mkdir()
        self.guard = lambda: None

    def change(self, name, before=b'old', after=b'new'):
        target = self.target / name
        source = self.stage / name
        if before is not None:
            target.write_bytes(before)
        if after is not None:
            source.write_bytes(after)
        return {'target': str(target), 'source': str(source) if after is not None else None,
                'before_sha256': digest(before) if before is not None else None}

    def prepare(self, changes):
        return prepare_transaction(changes, self.root / 'journals', guard=self.guard)

    def test_publish_copies_stage_and_completed_replay_preserves_user_edits(self):
        change = self.change('state.db')
        journal = self.prepare([change])
        Path(change['source']).write_bytes(b'changed stage')
        receipt = apply_transaction(journal, guard=self.guard)
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(Path(change['target']).read_bytes(), b'new')
        self.assertTrue(verify_transaction(journal)['ok'])
        Path(change['target']).write_bytes(b'later user edit')
        self.assertEqual(apply_transaction(journal, guard=self.guard), receipt)
        self.assertEqual(recover_transaction(journal, guard=self.guard), receipt)
        self.assertEqual(Path(change['target']).read_bytes(), b'later user edit')
        self.assertFalse(verify_transaction(journal)['ok'])

    def test_conflicting_before_hash_and_new_file_collision(self):
        change = self.change('state.db')
        change['before_sha256'] = digest(b'wrong')
        with self.assertRaises(MigrationError):
            self.prepare([change])
        change['before_sha256'] = None
        with self.assertRaises(MigrationError):
            self.prepare([change])

    def test_all_targets_preflight_before_apply_and_recovery(self):
        changes = [self.change('one'), self.change('two')]
        journal = self.prepare(changes)
        Path(changes[1]['target']).write_bytes(b'user edit')
        with self.assertRaises(MigrationError):
            apply_transaction(journal, guard=self.guard)
        self.assertEqual(Path(changes[0]['target']).read_bytes(), b'old')
        with self.assertRaises(MigrationError):
            recover_transaction(journal, guard=self.guard)
        self.assertEqual(Path(changes[1]['target']).read_bytes(), b'user edit')

    def test_crash_after_replace_recovers_without_status_update(self):
        changes = [self.change('one'), self.change('two', None)]
        journal = self.prepare(changes)
        code = "from pathlib import Path; import os; from codex_migrate.transaction import apply_transaction; apply_transaction(Path(%r), guard=lambda:None, fault=lambda phase: os._exit(73) if phase == 'after_publish:0' else None)" % str(journal)
        result = subprocess.run([sys.executable, '-c', code], env=os.environ.copy())
        self.assertEqual(result.returncode, 73)
        self.assertEqual(Path(changes[0]['target']).read_bytes(), b'new')
        receipt = recover_transaction(journal, guard=self.guard)
        self.assertEqual(receipt['status'], 'rolled_back')
        self.assertEqual(Path(changes[0]['target']).read_bytes(), b'old')
        self.assertFalse(Path(changes[1]['target']).exists())
        self.assertEqual(recover_transaction(journal, guard=self.guard), receipt)

    def test_crash_new_file_then_unknown_edit_prevents_any_rollback(self):
        changes = [self.change('one'), self.change('two', None)]
        journal = self.prepare(changes)
        def crash(phase):
            if phase == 'after_publish:1':
                raise RuntimeError('fault')
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard, fault=crash)
        Path(changes[1]['target']).write_bytes(b'user change')
        with self.assertRaises(MigrationError):
            recover_transaction(journal, guard=self.guard)
        self.assertEqual(Path(changes[0]['target']).read_bytes(), b'new')
        self.assertEqual(Path(changes[1]['target']).read_bytes(), b'user change')

    def test_new_file_no_clobber_and_transactional_delete(self):
        changes = [self.change('new', None), self.change('state.db-wal', b'', None)]
        journal = self.prepare(changes)
        receipt = apply_transaction(journal, guard=self.guard)
        self.assertEqual(receipt['status'], 'completed')
        self.assertEqual(Path(changes[0]['target']).read_bytes(), b'new')
        self.assertFalse(Path(changes[1]['target']).exists())
        self.assertTrue(verify_transaction(journal)['ok'])

    def test_delete_recovers_and_guard_stops_before_writes(self):
        change = self.change('state.db-shm', b'stale', None)
        journal = self.prepare([change])
        def stop():
            raise MigrationError('occupied')
        with self.assertRaises(MigrationError):
            apply_transaction(journal, guard=stop)
        self.assertEqual(Path(change['target']).read_bytes(), b'stale')
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard,
                              fault=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase == 'after_publish:0' else None)
        recover_transaction(journal, guard=self.guard)
        self.assertEqual(Path(change['target']).read_bytes(), b'stale')

    def test_symlinks_duplicate_and_source_target_overlap_rejected(self):
        change = self.change('one')
        link = self.target / 'link'
        try:
            link.symlink_to(Path(change['target']))
        except OSError:
            self.skipTest('symlink unavailable')
        with self.assertRaises(MigrationError):
            self.prepare([{**change, 'target': str(link)}])
        with self.assertRaises(MigrationError):
            self.prepare([change, change])
        with self.assertRaises(MigrationError):
            self.prepare([{**change, 'source': change['target']}])

    def test_no_clobber_if_target_appears_at_publish_boundary(self):
        change = self.change('new', None)
        journal = self.prepare([change])
        def conflict(phase):
            if phase == 'before_publish:0':
                Path(change['target']).write_bytes(b'new user file')
        with self.assertRaises(MigrationError):
            apply_transaction(journal, guard=self.guard, fault=conflict)
        self.assertEqual(Path(change['target']).read_bytes(), b'new user file')

    def test_corrupt_backup_prevents_recovery_before_any_mutation(self):
        changes = [self.change('one'), self.change('two')]
        journal = self.prepare(changes)
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard,
                              fault=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase == 'after_publish:1' else None)
        (journal.parent / 'backup' / '1').write_bytes(b'corrupt')
        with self.assertRaises(MigrationError):
            recover_transaction(journal, guard=self.guard)
        self.assertEqual(Path(changes[0]['target']).read_bytes(), b'new')

    def test_fault_at_each_persistent_boundary_can_recover(self):
        phases = ['after_begin', 'before_publish:0', 'after_publish:0',
                  'after_record:0', 'before_publish:1', 'after_publish:1',
                  'after_record:1', 'before_complete', 'after_complete']
        for number, selected in enumerate(phases):
            with self.subTest(phase=selected):
                changes = [self.change('old-' + str(number)), self.change('new-' + str(number), None)]
                journal = self.prepare(changes)
                def fail(phase):
                    if phase == selected:
                        raise RuntimeError('injected crash')
                with self.assertRaises(RuntimeError):
                    apply_transaction(journal, guard=self.guard, fault=fail)
                receipt = recover_transaction(journal, guard=self.guard)
                completed = selected == 'after_complete'
                self.assertEqual(receipt['status'], 'completed' if completed else 'rolled_back')
                self.assertEqual(Path(changes[0]['target']).read_bytes(), b'new' if completed else b'old')
                self.assertEqual(Path(changes[1]['target']).exists(), completed)

    def test_nested_parent_creation_and_recovery_preserves_unrelated_file(self):
        source = self.stage / 'nested'
        source.write_bytes(b'nested')
        target = self.target / 'newdir' / 'child' / 'new'
        journal = self.prepare([{'source': str(source), 'target': str(target), 'before_sha256': None}])
        def fail(phase):
            if phase == 'after_publish:0':
                (target.parent / 'user-file').write_bytes(b'keep')
                raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard, fault=fail)
        recover_transaction(journal, guard=self.guard)
        self.assertFalse(target.exists())
        self.assertEqual((target.parent / 'user-file').read_bytes(), b'keep')

    @unittest.skipIf(os.name == 'nt', 'POSIX mode semantics')
    def test_replacement_does_not_widen_private_file_permissions(self):
        change = self.change('private')
        Path(change['target']).chmod(0o600)
        journal = self.prepare([change])
        apply_transaction(journal, guard=self.guard)
        self.assertEqual(Path(change['target']).stat().st_mode & 0o777, 0o600)

    def test_hardlinked_source_target_and_duplicate_targets_rejected(self):
        change = self.change('one')
        alias = self.stage / 'alias'
        try:
            os.link(change['target'], alias)
        except OSError:
            self.skipTest('hard links unavailable')
        with self.assertRaises(MigrationError):
            self.prepare([{**change, 'source': str(alias)}])
        target_alias = self.target / 'alias'
        os.link(change['target'], target_alias)
        with self.assertRaises(MigrationError):
            self.prepare([change, {**change, 'target': str(target_alias)}])

    def test_real_handle_probe_detects_held_file_without_process_name(self):
        path = self.target / 'held.db'
        path.write_bytes(b'synthetic')
        code = "import sys; f=open(sys.argv[1], 'rb'); print('ready', flush=True); sys.stdin.read()"
        process = subprocess.Popen([sys.executable, '-c', code, str(path)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(), 'ready')
            with patch.object(platforms, '_processes', return_value=[]):
                with self.assertRaises(MigrationError):
                    platforms.ensure_idle(self.target)
        finally:
            process.communicate('done', timeout=10)

    @unittest.skipUnless(sys.platform == 'darwin', 'macOS filesystem API')
    def test_network_filesystem_check_fails_closed(self):
        with patch.object(platforms, '_darwin_filesystem', return_value=('smbfs', 0)):
            with self.assertRaises(MigrationError):
                platforms.ensure_local_storage(self.target)

    def test_migration_lock_coordinates_different_journals(self):
        first = self.prepare([self.change('first')])
        second = self.prepare([self.change('second')])
        self.assertNotEqual(first, second)
        with platforms.migration_lock(self.target):
            with self.assertRaises(MigrationError):
                with platforms.migration_lock(self.target):
                    self.fail('second migration acquired the lock')
            code = "from pathlib import Path; from codex_migrate.platforms import migration_lock; from codex_migrate.errors import MigrationError; import sys; \ntry:\n with migration_lock(Path(%r)): sys.exit(9)\nexcept MigrationError: sys.exit(0)" % str(self.target)
            result = subprocess.run([sys.executable, '-c', code], env=os.environ.copy())
            self.assertEqual(result.returncode, 0)
        with platforms.migration_lock(self.target):
            pass

    def test_lock_rejects_symlink_without_touching_destination(self):
        destination = self.root / 'unrelated'
        destination.write_bytes(b'preserve')
        try:
            (self.target / '.codex-migrate.lock').symlink_to(destination)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(MigrationError):
            with platforms.migration_lock(self.target):
                self.fail('symlink accepted')
        self.assertEqual(destination.read_bytes(), b'preserve')

    def test_completed_replay_returns_receipt_when_target_becomes_symlink(self):
        change = self.change('one')
        journal = self.prepare([change])
        receipt = apply_transaction(journal, guard=self.guard)
        target = Path(change['target'])
        target.unlink()
        try:
            target.symlink_to(Path(change['source']))
        except OSError:
            self.skipTest('symlinks unavailable')
        self.assertEqual(recover_transaction(journal, guard=self.guard), receipt)
        self.assertEqual(apply_transaction(journal, guard=self.guard), receipt)
        self.assertTrue(target.is_symlink())
        self.assertFalse(verify_transaction(journal)['ok'])

    def test_recovery_does_not_remove_directory_created_by_someone_else(self):
        source = self.stage / 'file'
        source.write_bytes(b'new')
        directory = self.target / 'newdir'
        target = directory / 'file'
        journal = self.prepare([{'target': str(target), 'source': str(source), 'before_sha256': None}])
        original = Path.mkdir
        def collision(path, *args, **kwargs):
            if path == directory:
                original(path)
                raise FileExistsError('another actor created the directory')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', collision):
            with self.assertRaises(FileExistsError):
                apply_transaction(journal, guard=self.guard)
        recover_transaction(journal, guard=self.guard)
        self.assertTrue(directory.is_dir())

    def test_own_migration_lock_does_not_make_idle_probe_busy(self):
        with platforms.migration_lock(self.target), patch.object(platforms, '_processes', return_value=[]):
            platforms.ensure_idle(self.target)

    def test_private_journal_directory_creation(self):
        directory = self.root / 'private'
        platforms.make_private_directory(directory)
        self.assertTrue(directory.is_dir())
        if os.name != 'nt':
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        with self.assertRaises(OSError):
            platforms.make_private_directory(directory)

    def test_windows_reparse_directory_is_rejected_on_python_311(self):
        fake_os = types.SimpleNamespace(name='nt', walk=lambda *args, **kwargs: [(str(self.target), ['linked'], [])])
        info = types.SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
        with patch.object(platforms, 'os', fake_os), patch.object(Path, 'lstat', return_value=info), patch.object(Path, 'is_junction', return_value=False, create=True):
            with self.assertRaises(OSError):
                platforms._check_handles([self.target])

    def test_storage_probe_accepts_new_nested_target_and_rejects_symlink(self):
        platforms.ensure_local_storage(self.target / 'missing' / 'nested' / 'file')
        link = self.root / 'link'
        try:
            link.symlink_to(self.target, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(MigrationError):
            platforms.ensure_local_storage(link / 'file')

    def test_explicit_empty_directories_are_published_and_verified(self):
        directory = self.target / 'empty-project' / 'empty-child'
        journal = prepare_transaction([], self.root / 'journals', directories=[directory], guard=self.guard)
        self.assertFalse(directory.exists())
        receipt = apply_transaction(journal, guard=self.guard)
        self.assertEqual(receipt['status'], 'completed')
        self.assertTrue(directory.is_dir())
        self.assertEqual(list(directory.iterdir()), [])
        self.assertTrue(verify_transaction(journal)['ok'])
        directory.rmdir()
        self.assertFalse(verify_transaction(journal)['ok'])
        self.assertEqual(recover_transaction(journal, guard=self.guard), receipt)
        self.assertFalse(directory.exists())

    def test_explicit_empty_directory_crash_recovers_owned_ancestors(self):
        directory = self.target / 'empty-project' / 'empty-child'
        journal = prepare_transaction([], self.root / 'journals', directories=[directory], guard=self.guard)
        code = "from pathlib import Path; import os; from codex_migrate.transaction import apply_transaction; apply_transaction(Path(%r), guard=lambda:None, fault=lambda phase: os._exit(73) if phase == 'after_directory:0' else None)" % str(journal)
        result = subprocess.run([sys.executable, '-c', code], env=os.environ.copy())
        self.assertEqual(result.returncode, 73)
        self.assertTrue(directory.is_dir())
        receipt = recover_transaction(journal, guard=self.guard)
        self.assertEqual(receipt['status'], 'rolled_back')
        self.assertFalse((self.target / 'empty-project').exists())
        self.assertTrue(verify_transaction(journal)['ok'])

    def test_existing_explicit_directory_survives_rollback(self):
        directory = self.target / 'existing'
        directory.mkdir()
        (directory / 'keep').write_bytes(b'user data')
        journal = prepare_transaction([], self.root / 'journals', directories=[directory], guard=self.guard)
        def fail(phase):
            if phase == 'before_complete':
                raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard, fault=fail)
        recover_transaction(journal, guard=self.guard)
        self.assertEqual((directory / 'keep').read_bytes(), b'user data')

    def test_explicit_directory_symlink_and_file_collisions_rejected(self):
        file = self.target / 'file'
        file.write_bytes(b'preserve')
        with self.assertRaises(MigrationError):
            prepare_transaction([], self.root / 'journals', directories=[file], guard=self.guard)
        with self.assertRaises(MigrationError):
            prepare_transaction([], self.root / 'journals', directories=[file / 'child'], guard=self.guard)
        link = self.root / 'link'
        try:
            link.symlink_to(self.target, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(MigrationError):
            prepare_transaction([], self.root / 'journals', directories=[link / 'empty'], guard=self.guard)
        self.assertEqual(file.read_bytes(), b'preserve')

    def test_explicit_directory_concurrent_creation_refuses_file_publication(self):
        directory = self.target / 'empty'
        change = self.change('file')
        journal = prepare_transaction([change], self.root / 'journals', directories=[directory], guard=self.guard)
        directory.mkdir()
        (directory / 'user-file').write_bytes(b'keep')
        with self.assertRaises(MigrationError):
            apply_transaction(journal, guard=self.guard)
        with self.assertRaises(MigrationError):
            recover_transaction(journal, guard=self.guard)
        self.assertEqual(Path(change['target']).read_bytes(), b'old')
        self.assertEqual((directory / 'user-file').read_bytes(), b'keep')

    @unittest.skipIf(os.name == 'nt', 'POSIX execute-bit semantics')
    def test_staged_execute_bit_drift_refuses_apply(self):
        change = self.change('file')
        journal = self.prepare([change])
        staged = journal.parent / 'stage' / '0'
        staged.chmod(staged.stat().st_mode ^ 0o100)
        with self.assertRaises(MigrationError):
            apply_transaction(journal, guard=self.guard)
        self.assertEqual(Path(change['target']).read_bytes(), b'old')

    @unittest.skipIf(os.name == 'nt', 'POSIX execute-bit semantics')
    def test_target_execute_bit_drift_refuses_apply(self):
        change = self.change('file')
        journal = self.prepare([change])
        target = Path(change['target'])
        target.chmod(target.stat().st_mode ^ 0o100)
        with self.assertRaises(MigrationError):
            apply_transaction(journal, guard=self.guard)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertTrue(target.stat().st_mode & 0o100)

    @unittest.skipIf(os.name == 'nt', 'POSIX execute-bit semantics')
    def test_completed_execute_bit_drift_is_reported_and_preserved(self):
        change = self.change('file')
        journal = self.prepare([change])
        receipt = apply_transaction(journal, guard=self.guard)
        target = Path(change['target'])
        target.chmod(target.stat().st_mode ^ 0o100)
        report = verify_transaction(journal)
        self.assertFalse(report['ok'])
        self.assertFalse(report['drift'][0]['expected_executable'])
        self.assertTrue(report['drift'][0]['actual_executable'])
        self.assertEqual(recover_transaction(journal, guard=self.guard), receipt)
        self.assertTrue(target.stat().st_mode & 0o100)

    @unittest.skipIf(os.name == 'nt', 'POSIX execute-bit semantics')
    def test_later_execute_bit_change_prevents_any_destructive_recovery(self):
        changes = [self.change('one'), self.change('two')]
        journal = self.prepare(changes)
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard,
                              fault=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase == 'after_publish:0' else None)
        target = Path(changes[0]['target'])
        target.chmod(target.stat().st_mode ^ 0o100)
        with self.assertRaises(MigrationError):
            recover_transaction(journal, guard=self.guard)
        self.assertEqual(target.read_bytes(), b'new')
        self.assertTrue(target.stat().st_mode & 0o100)
        self.assertEqual(Path(changes[1]['target']).read_bytes(), b'old')

    @unittest.skipIf(os.name == 'nt', 'POSIX execute-bit semantics')
    def test_backup_execute_bit_drift_refuses_recovery(self):
        change = self.change('file')
        journal = self.prepare([change])
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard,
                              fault=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase == 'after_publish:0' else None)
        backup = journal.parent / 'backup' / '0'
        backup.chmod(backup.stat().st_mode ^ 0o100)
        with self.assertRaises(MigrationError):
            recover_transaction(journal, guard=self.guard)
        self.assertEqual(Path(change['target']).read_bytes(), b'new')

    @unittest.skipIf(os.name == 'nt', 'POSIX execute-bit semantics')
    def test_execute_modes_are_preserved_through_publish_and_rollback(self):
        change = self.change('script')
        Path(change['target']).chmod(0o700)
        Path(change['source']).chmod(0o600)
        journal = self.prepare([change])
        with self.assertRaises(RuntimeError):
            apply_transaction(journal, guard=self.guard,
                              fault=lambda phase: (_ for _ in ()).throw(RuntimeError()) if phase == 'after_publish:0' else None)
        self.assertEqual(Path(change['target']).stat().st_mode & 0o777, 0o600)
        recover_transaction(journal, guard=self.guard)
        self.assertEqual(Path(change['target']).read_bytes(), b'old')
        self.assertEqual(Path(change['target']).stat().st_mode & 0o777, 0o700)
        self.assertTrue(verify_transaction(journal)['ok'])

    @unittest.skipIf(os.name == 'nt', 'POSIX execute-bit semantics')
    def test_publish_boundary_rechecks_execute_bits_of_target_and_artifact(self):
        for index, mutate in enumerate(('target', 'stage')):
            with self.subTest(mutate=mutate):
                change = self.change('file-' + str(index))
                journal = self.prepare([change])
                path = Path(change['target']) if mutate == 'target' else journal.parent / 'stage' / '0'
                def chmod(phase):
                    if phase == 'before_publish:0':
                        path.chmod(path.stat().st_mode ^ 0o100)
                with self.assertRaises(MigrationError):
                    apply_transaction(journal, guard=self.guard, fault=chmod)
                self.assertEqual(Path(change['target']).read_bytes(), b'old')

    def test_windows_missing_child_below_regular_file_fails_before_journal_creation(self):
        ancestor = self.target / 'regular-file'
        ancestor.write_bytes(b'preserve')
        destination = ancestor / 'child'
        original_lstat = Path.lstat
        def windows_missing_child(path, *args, **kwargs):
            if ancestor in path.parents:
                raise FileNotFoundError('Windows reports a missing child under a regular file')
            return original_lstat(path, *args, **kwargs)
        journals = self.root / 'new-journals'
        with patch.object(Path, 'lstat', windows_missing_child):
            with self.assertRaises(MigrationError):
                prepare_transaction([], journals, directories=[destination], guard=self.guard)
        self.assertFalse(journals.exists())
        self.assertEqual(ancestor.read_bytes(), b'preserve')

    def test_platform_probe_failure_and_live_app_fail_closed(self):
        with patch.object(platforms, '_processes', side_effect=OSError('denied')):
            with self.assertRaises(MigrationError):
                platforms.ensure_idle(self.target)
        with patch.object(platforms, '_processes', return_value=[(123, 'Codex.exe')]):
            with self.assertRaises(MigrationError):
                platforms.ensure_idle(self.target)
        with patch.object(platforms, '_processes', return_value=[]), patch.object(platforms, '_check_handles', side_effect=OSError('unknown')):
            with self.assertRaises(MigrationError):
                platforms.ensure_idle(self.target)


if __name__ == '__main__':
    unittest.main()
