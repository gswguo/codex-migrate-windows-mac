"""Build native snapshots in an isolated directory; never execute bundle SQL."""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

from .common import atomic_json, canonical_json, connect_readonly, decode_row, sha256_file, snapshot_database
from .compatibility import HISTORY_TABLES, STATE_TABLES, validate_profile
from .errors import MigrationError
from .history import transform_runtime

DATABASES = ('state_5.sqlite', 'thread_history_1.sqlite')
MAX_STRUCTURED_BYTES = 256 * 1024 * 1024


def fingerprint(path: Path) -> str | None:
    if path.is_symlink():
        raise MigrationError('Symbolic-link targets are unsupported', 'unsafe_path')
    if not path.exists():
        return None
    if not path.is_file():
        raise MigrationError('Expected a regular file', 'unsafe_path')
    return sha256_file(path)


def capture_native(home: Path, sqlite_home: Path) -> dict[str, str | None]:
    paths = [home / '.codex-global-state.json', home / 'session_index.jsonl']
    for name in DATABASES:
        for suffix in ('', '-wal', '-shm', '-journal'):
            paths.append(sqlite_home / (name + suffix))
    return {str(path): fingerprint(path) for path in paths}


def check_fingerprints(expected: dict) -> None:
    for name, digest in expected.items():
        if fingerprint(Path(name)) != digest:
            raise MigrationError('Target changed since planning; create a new plan: ' + name, 'target_changed')


def snapshot_native(home: Path, sqlite_home: Path, stage: Path) -> None:
    stage.mkdir(parents=True, exist_ok=False)
    for name in DATABASES:
        snapshot_database(sqlite_home / name, stage / name)


def read_rows(directory: Path, kind: str, tables) -> dict:
    result = {}
    size = 0
    for table in tables:
        path = directory / 'data' / kind / (table + '.jsonl')
        if not path.is_file():
            raise MigrationError('Required native table export is missing: ' + table, 'invalid_bundle')
        size += path.stat().st_size
        if size > MAX_STRUCTURED_BYTES:
            raise MigrationError('Structured history exceeds alpha 256 MiB per-database planning limit', 'planning_limit')
        rows = []
        with path.open('rb') as stream:
            for line in stream:
                if len(line) > 16 * 1024 * 1024:
                    raise MigrationError('Native table record exceeds limit', 'planning_limit')
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise MigrationError('Native table rows must be objects', 'invalid_bundle')
                rows.append(decode_row(row))
        result[table] = rows
    return result


def validate_import_scope(state_rows: dict, history_rows: dict, manifest: dict) -> None:
    """Reject rows outside manifest ownership before touching a staged database.

    Native history may reference inherited ordinal ranges. This checks identity
    ownership only; ordinal and byte-boundary semantics belong to history.py.
    """
    def fail():
        raise MigrationError('Native rows extend outside the declared migration scope', 'invalid_bundle')

    def identities(rows, key='id'):
        if not isinstance(rows, list): fail()
        found=set()
        for row in rows:
            if not isinstance(row, dict): fail()
            identity=row.get(key)
            if not isinstance(identity, str) or not identity or identity in found: fail()
            found.add(identity)
        return found

    if not isinstance(state_rows, dict) or not isinstance(history_rows, dict) or not isinstance(manifest, dict): fail()
    if set(state_rows)!=set(STATE_TABLES) or set(history_rows)!=set(HISTORY_TABLES): fail()
    for group in (state_rows, history_rows):
        for rows in group.values():
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows): fail()
    projects=identities(manifest.get('projects'))
    threads=identities(manifest.get('threads'))
    if identities(state_rows['projects'])!=projects or identities(state_rows['threads'])!=threads: fail()
    referenced_sections=set()
    for row in state_rows['threads']:
        project=row.get('project_id')
        if project is not None and (not isinstance(project, str) or project not in projects): fail()
        section=row.get('thread_section_id')
        if section is not None:
            if not isinstance(section, str) or not section: fail()
            referenced_sections.add(section)
    if identities(state_rows['thread_sections'])!=referenced_sections: fail()
    for row in state_rows['project_roots']:
        if not isinstance(row.get('project_id'), str) or row['project_id'] not in projects: fail()
    for table, rows in state_rows.items():
        for row in rows:
            for key, value in row.items():
                if key=='thread_id' or key.endswith('_thread_id'):
                    if not isinstance(value, str) or value not in threads: fail()
            if table=='thread_spawn_edges' and not all(key in row for key in ('parent_thread_id','child_thread_id')): fail()
            if table in ('thread_dynamic_tools','thread_attachments','thread_read_receipts','guardian_review_feedback') and 'thread_id' not in row: fail()
    for rows in history_rows.values():
        for row in rows:
            if not isinstance(row.get('thread_id'), str) or row['thread_id'] not in threads: fail()


def read_history_rows(database: Path, selected: set[str]) -> dict:
    result = {table: [] for table in HISTORY_TABLES}
    size = 0
    db = connect_readonly(database)
    try:
        for table in HISTORY_TABLES:
            for row in db.execute('SELECT * FROM "' + table + '"'):
                if row['thread_id'] in selected:
                    value = dict(row)
                    size += sum(len(v) if isinstance(v, (str, bytes)) else 16 for v in value.values())
                    if size > MAX_STRUCTURED_BYTES:
                        raise MigrationError('Selected history exceeds alpha planning limit', 'planning_limit')
                    result[table].append(value)
    finally:
        db.close()
    return result


def _insert_rows(db: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    columns = [r[1] for r in db.execute('PRAGMA table_info("' + table + '")')]
    for row in rows:
        if set(row) != set(columns):
            raise MigrationError('Native row columns do not match the pinned profile: ' + table, 'invalid_bundle')
        sql = 'INSERT INTO "' + table + '" (' + ','.join('"' + c + '"' for c in columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
        db.execute(sql, [row[c] for c in columns])


def check_database(path: Path) -> None:
    db = connect_readonly(path)
    try:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise MigrationError('Staged native database is not intact', 'database_corrupt')
        if db.execute('PRAGMA foreign_key_check').fetchone():
            raise MigrationError('Staged native foreign-key check failed', 'invalid_history')
    finally:
        db.close()


def _thread_policy(row: dict, mapper) -> dict:
    row = dict(row)
    raw = json.loads(row['sandbox_policy'])
    if isinstance(raw, str) and raw in ('read-only', 'workspace-write', 'danger-full-access'):
        raw = {'type': raw}
    transformed, _ = transform_runtime({'cwd': row['cwd'], 'sandbox_policy': raw,
                                        'approval_mode': row['approval_mode']}, mapper)
    row['cwd'] = transformed['cwd']
    row['sandbox_policy'] = canonical_json(transformed['sandbox_policy'])
    row['approval_mode'] = transformed['approval_mode']
    return row


def _attachment(row: dict, mapper) -> dict:
    """Map declared local attachment fields, preserving opaque identity values."""
    row = dict(row)
    if row['attachment_type'] not in ('image', 'file', 'generated_image'):
        raise MigrationError('Unsupported attachment type', 'unsupported_attachment')
    value = json.loads(row['payload'])
    paths = {'savedPath', 'saved_path', 'localPath', 'local_path', 'path'}
    def visit(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key in paths and isinstance(child, str):
                    node[key] = mapper.map_path(child, required=True)
                else:
                    visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
    visit(value)
    row['payload'] = canonical_json(value)
    mapped = mapper.map_path(row['identity_key'])
    if mapped is not None:
        row['identity_key'] = mapped
    return row


def merge_databases(stage: Path, state_rows: dict, history_rows: dict, mapper,
                    final_paths: dict[str, str], projects: list[dict]) -> None:
    state_rows = copy.deepcopy(state_rows)
    db = sqlite3.connect(stage / DATABASES[0])
    try:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN')
        db.execute('PRAGMA defer_foreign_keys=ON')
        current_ids = {r[0] for r in db.execute('SELECT id FROM threads')}
        incoming = {r['id'] for r in state_rows['threads']}
        if current_ids & incoming:
            raise MigrationError('Thread ID already exists. This alpha does not merge two devices’ chat changes.', 'thread_conflict')
        next_position = db.execute('SELECT COALESCE(MAX(position), -1)+1 FROM projects').fetchone()[0]
        existing_names = {r[0] for r in db.execute('SELECT name FROM projects')}
        by_id = {p['id']: p for p in projects}
        for index, row in enumerate(state_rows['projects']):
            row['position'] = next_position + index
            name = row['name']
            if name in existing_names:
                name += ' (imported)'
            while name in existing_names:
                name += ' (imported)'
            row['name'] = name
            existing_names.add(name)
            by_id[row['id']]['name'] = name
        for row in state_rows['project_roots']:
            row['path'] = mapper.map_path(row['path'], required=True)
        for index, row in enumerate(state_rows['threads']):
            row = _thread_policy(row, mapper)
            row['rollout_path'] = final_paths[row['id']]
            state_rows['threads'][index] = row
        state_rows['thread_attachments'] = [_attachment(row, mapper) for row in state_rows['thread_attachments']]
        for table in STATE_TABLES:
            _insert_rows(db, table, state_rows[table])
        for project in projects:
            key = project['legacy_id'] or project['id']
            db.execute('INSERT INTO project_idempotency_keys(key,project_id,created_at_ms) VALUES (?,?,?)',
                       (key, project['id'], project['created_at_ms']))
        db.commit()
    except sqlite3.IntegrityError as error:
        db.rollback()
        raise MigrationError('A native ID or relation conflicts with existing data', 'native_conflict') from error
    finally:
        db.close()
    db = sqlite3.connect(stage / DATABASES[1])
    try:
        db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN'); db.execute('PRAGMA defer_foreign_keys=ON')
        for table in HISTORY_TABLES:
            _insert_rows(db, table, history_rows[table])
        db.commit()
    finally:
        db.close()
    for name in DATABASES:
        check_database(stage / name)
    validate_profile(stage / DATABASES[0], stage / DATABASES[1])


def replace_history_rows(stage: Path, history_rows: dict, selected: set[str]) -> None:
    db = sqlite3.connect(stage / DATABASES[1])
    try:
        db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN'); db.execute('PRAGMA defer_foreign_keys=ON')
        for table in HISTORY_TABLES:
            for tid in selected:
                db.execute('DELETE FROM "' + table + '" WHERE thread_id=?', (tid,))
        for table in HISTORY_TABLES:
            _insert_rows(db, table, history_rows[table])
        db.commit()
    finally:
        db.close()
    check_database(stage / DATABASES[1])


def merge_sidebar(before: dict, projects: list[dict], roots: dict[str, str], home: Path) -> dict:
    after = copy.deepcopy(before)
    local = after.setdefault('local-projects', {})
    order = after.setdefault('project-order', [])
    maps = after.setdefault('app-server-project-id-by-legacy-project-id-by-host', {}).setdefault('local:' + str(home), {})
    assignments = after.setdefault('thread-project-assignments', {})
    hosts = after.setdefault('thread-project-membership-host-ids', {})
    if not isinstance(order, list) or any(not isinstance(v, dict) for v in (local, maps, assignments, hosts)):
        raise MigrationError('Unknown target sidebar format', 'unsupported_profile')
    for project in projects:
        legacy = project['legacy_id'] or project['id']
        if legacy in local or legacy in maps:
            raise MigrationError('Project identifier already exists in sidebar', 'project_conflict')
        local[legacy] = {'id': legacy, 'name': project['name'],
                         'rootPaths': [roots[r] for r in project['root_ids']],
                         'createdAt': project['created_at_ms'], 'updatedAt': project['updated_at_ms']}
        order.append(legacy); maps[legacy] = project['id']
        for tid in project['main_thread_ids']:
            if tid in assignments or tid in hosts:
                raise MigrationError('Thread membership already exists', 'thread_conflict')
            assignments[tid] = {'projectId': legacy, 'projectKind': 'local'}
            hosts[tid] = 'local'
    return after


def index_bytes(home: Path, incoming: list[dict], main_ids: set[str]) -> bytes:
    path = home / 'session_index.jsonl'
    original = path.read_bytes() if path.exists() else b''
    if original and not original.endswith(b'\n'):
        original += b'\n'
    from datetime import datetime, timezone
    for thread in incoming:
        if thread['id'] in main_ids:
            value = {'id': thread['id'], 'thread_name': thread['title'],
                     'updated_at': datetime.fromtimestamp(thread['updated_at'], timezone.utc).isoformat().replace('+00:00', 'Z')}
            original += (canonical_json(value) + '\n').encode()
    return original
