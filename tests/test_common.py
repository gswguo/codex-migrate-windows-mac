import base64
import hashlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from contextlib import closing


class CommonTests(unittest.TestCase):
    def test_native_fixture_closes_every_connection_before_returning(self):
        from fixtures import make_home
        opened=[];connect=sqlite3.connect
        class TrackedConnection(sqlite3.Connection):
            closed_explicitly=False
            def close(self):
                self.closed_explicitly=True
                super().close()
        def track(*args,**kwargs):
            connection=connect(*args,factory=TrackedConnection,**kwargs);opened.append(connection);return connection
        with tempfile.TemporaryDirectory() as directory:
            try:
                with patch('fixtures.sqlite3.connect',side_effect=track): make_home(Path(directory))
                self.assertTrue(opened);self.assertTrue(all(connection.closed_explicitly for connection in opened))
            finally:
                for connection in opened: connection.close()

    def test_snapshot_includes_committed_wal_without_mutating_source_rows(self):
        from codex_migrate.common import snapshot_database, connect_readonly
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'source.sqlite'
            dest = Path(d) / 'snapshot.sqlite'
            writer = sqlite3.connect(source)
            try:
                writer.execute('PRAGMA journal_mode=WAL')
                writer.execute('CREATE TABLE entries (id INTEGER PRIMARY KEY, payload BLOB)')
                writer.execute('INSERT INTO entries VALUES (?, ?)', (1, b'\x00\xffprivate fixture'))
                writer.commit()
                snapshot_database(source, dest)
                with closing(connect_readonly(dest)) as copy:
                    self.assertEqual(copy.execute('SELECT payload FROM entries').fetchone()[0], b'\x00\xffprivate fixture')
                self.assertEqual(writer.execute('SELECT count(*) FROM entries').fetchone()[0], 1)
                with self.assertRaises(FileExistsError):
                    snapshot_database(source, dest)
            finally:
                writer.close()

    def test_binary_rows_roundtrip_and_invalid_encoding_refused(self):
        from codex_migrate.common import encode_row, decode_row
        from codex_migrate.errors import MigrationError
        original = {'id': 'synthetic', 'blob': b'\x00\xff', 'text': '{"literal": true}'}
        self.assertEqual(decode_row(encode_row(original)), original)
        with self.assertRaises(MigrationError):
            decode_row({'blob': {'$bytes': 'not base64!'}})

    def test_atomic_json_and_hash_have_no_temporary_files(self):
        from codex_migrate.common import atomic_json, read_json, sha256_file
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / 'report.json'
            atomic_json(target, {'message': '中文'})
            self.assertEqual(read_json(target), {'message': '中文'})
            self.assertEqual(sha256_file(target), hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual([p.name for p in Path(d).iterdir()], ['report.json'])


if __name__ == '__main__':
    unittest.main()
