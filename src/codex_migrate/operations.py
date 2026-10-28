"""Public migration workflows: inspectable plans, isolated snapshots and offline apply."""
from __future__ import annotations

import copy
import json
import ntpath
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .bundle import extract_bundle
from .common import atomic_json, read_json, sha256_file
from .compatibility import HISTORY_TABLES, STATE_TABLES, validate_profile
from .discovery import inspect_home, resolve_layout
from .errors import MigrationError
from .history import rewrite_histories
from .paths import PathMapper
from .platforms import ensure_idle, ensure_local_storage, migration_lock
from .staging import (DATABASES, capture_native, check_fingerprints, fingerprint,
                      index_bytes, merge_databases, merge_sidebar, read_history_rows,
                      read_rows, replace_history_rows, snapshot_native, validate_import_scope)
from .transaction import (apply_transaction, prepare_transaction, recover_transaction,
                          verify_transaction)


def _new_work(work: Path, protected: list[Path]) -> Path:
    work = Path(work).expanduser().resolve()
    for other in protected:
        other = Path(other).resolve()
        if work == other or work.is_relative_to(other) or other.is_relative_to(work):
            raise MigrationError('Working directory must be separate from Codex and project destinations', 'unsafe_layout')
    work.mkdir(parents=True, exist_ok=False, mode=0o700)
    return work


def _guard(home: Path, sqlite_home: Path, paths: list[Path], check_idle: bool):
    def check():
        if check_idle:
            ensure_idle(home, sqlite_home)
            for path in paths:
                ensure_local_storage(path)
    return check


def _change(target: Path, source: Path | None) -> dict:
    return {'target': str(target), 'source': str(source) if source else None,
            'before_sha256': fingerprint(target),
            'stage_sha256': sha256_file(source) if source else None,
            'stage_executable': bool(source.stat().st_mode & 0o100) if source and os.name != 'nt' else None}


def _native_changes(home: Path, sqlite_home: Path, stage: Path, names) -> list[dict]:
    changes = []
    for name in names:
        # Delete old SQLite sidecars under the same recoverable stopped-app transaction.
        # The staged backup contains all committed WAL pages and uses DELETE journaling.
        for suffix in ('-wal', '-shm', '-journal'):
            path = sqlite_home / (name + suffix)
            if path.exists():
                changes.append(_change(path, None))
        changes.append(_change(sqlite_home / name, stage / name))
    return changes


def _seal(work: Path, plan: dict) -> Path:
    # The human-readable plan is local private state, not a portable trusted command file.
    targets = [c['target'] for c in plan['changes']]
    if len(targets) != len(set(targets)):
        raise MigrationError('Duplicate planned destination', 'unsafe_layout')
    check_fingerprints(plan['preconditions'])
    plan['format'] = 'codex-migrate-plan/v1'
    plan['plan_id'] = str(uuid.uuid4())
    plan['journal_pointer'] = str(work / 'transaction-pointer.json')
    path = work / 'plan.json'
    atomic_json(path, plan)
    return path


def _files_for_root(extracted: Path, manifest: dict, archive_root: str, destination: Path):
    prefix = archive_root.rstrip('/') + '/'
    for member, info in manifest['files'].items():
        if member.startswith(prefix):
            relative = Path(member[len(prefix):])
            final = destination / relative
            if final.exists():
                if fingerprint(final) != info['sha256']:
                    raise MigrationError('Existing project file differs: ' + str(final), 'file_conflict')
                if os.name != 'nt' and bool(final.stat().st_mode & 0o100) != info.get('executable', False):
                    raise MigrationError('Existing project executable permission differs: ' + str(final), 'file_conflict')
                yield final, extracted / member, True
            else:
                yield final, extracted / member, False


def plan_import(bundle: Path, home: Path, destination: Path, work: Path, *,
                sqlite_home: Path | None = None, check_idle: bool = True) -> Path:
    home, sqlite_home = resolve_layout(home, sqlite_home)
    bundle = Path(bundle).expanduser().resolve()
    destination = Path(destination).expanduser().resolve()
    if destination == home or destination.is_relative_to(home) or home.is_relative_to(destination):
        raise MigrationError('Project destination must not overlap Codex home', 'unsafe_layout')
    guard = _guard(home, sqlite_home, [destination, Path(work)], check_idle)
    guard()
    target_inventory = inspect_home(home, sqlite_home)
    baseline = capture_native(home, sqlite_home)
    bundle_hash = sha256_file(bundle)
    work = _new_work(work, [home, sqlite_home, destination])
    try:
        extracted = work / 'bundle'
        manifest = extract_bundle(bundle, extracted)
        if manifest['profile_id'] != target_inventory['profile_id'] or manifest['schema'] != target_inventory['schema']:
            raise MigrationError('Bundle and target native profiles do not match', 'unsupported_profile')
        state_rows = read_rows(extracted, 'state', STATE_TABLES)
        history_rows = read_rows(extracted, 'history', HISTORY_TABLES)
        validate_import_scope(state_rows, history_rows, manifest)
        incoming = {r['id'] for r in state_rows['threads']}
        if incoming & {r['id'] for r in target_inventory['threads']}:
            raise MigrationError('A selected thread already exists; automatic synchronization is unsupported', 'thread_conflict')
        if incoming != {r['id'] for r in manifest['threads']}:
            raise MigrationError('Thread records and event manifest disagree', 'invalid_bundle')
        existing_projects = {p['id'] for p in target_inventory['projects']}
        if existing_projects & {p['id'] for p in manifest['projects']}:
            raise MigrationError('Project ID already exists; choose a distinct migration scope', 'project_conflict')
        mappings = []; validation = []; expected = {}; root_targets = {}; changes = []; retained = {}; directories = []
        for root in manifest['roots']:
            # Root IDs are validated package path components, not user-controlled absolute paths.
            root_id = root['id']
            if not isinstance(root_id, str) or Path(root_id).name != root_id or '/' in root_id or '\\' in root_id or root_id in ('.', '..'):
                raise MigrationError('Invalid root identifier', 'invalid_bundle')
            final = destination / root_id
            staged = extracted / root['archive_path']
            root_targets[root_id] = str(final)
            directories.append(str(final))
            for directory in manifest.get('directories', []):
                prefix = root['archive_path'] + '/'
                if directory.startswith(prefix):
                    directories.append(str(final / Path(directory[len(prefix):])))
            mappings.append((root['source_path'], str(final)))
            validation.append((str(final), str(staged)))
            for target, source, unchanged in _files_for_root(extracted, manifest, root['archive_path'], final):
                expected[str(target)] = sha256_file(source)
                if not unchanged:
                    changes.append(_change(target, source))
                else:
                    baseline[str(target)] = expected[str(target)]
                    retained[str(target)] = {'sha256': expected[str(target)], 'executable': bool(target.stat().st_mode & 0o100) if os.name != 'nt' else None}
        for asset in manifest.get('assets', []):
            relative = Path(asset['archive_path'])
            final = home / 'migration_assets' / manifest['bundle_id'] / relative.relative_to('assets')
            staged = extracted / relative
            mappings.append((asset['source_path'], str(final)))
            validation.append((str(final), str(staged)))
            expected[str(final)] = sha256_file(staged)
            changes.append(_change(final, staged))
        mapper = PathMapper(mappings, expected_hashes=expected, validation_mappings=validation)
        final_paths = {}
        state_by_id = {row['id']: row for row in state_rows['threads']}
        for tid in incoming:
            base = 'archived_sessions' if state_by_id[tid]['archived'] else 'sessions'
            # Native paginated reads parse identity from this canonical basename.
            stamp = datetime.fromtimestamp(state_by_id[tid]['created_at'], timezone.utc)
            basename = 'rollout-' + stamp.strftime('%Y-%m-%dT%H-%M-%S') + '-' + tid + '.jsonl'
            final_paths[tid] = str(home / base / stamp.strftime('%Y/%m/%d') / basename)
        rollouts = {r['id']: extracted / r['archive_path'] for r in manifest['threads']}
        rewritten = rewrite_histories(rollouts, work / 'events', mapper, history_rows, final_rollout_paths=final_paths)
        for tid, source in rewritten['rollouts'].items():
            target = Path(final_paths[tid])
            if target.exists():
                raise MigrationError('Target event path already exists', 'file_conflict')
            changes.append(_change(target, Path(source)))
        stage = work / 'native'
        snapshot_native(home, sqlite_home, stage)
        projects = copy.deepcopy(manifest['projects'])
        merge_databases(stage, state_rows, rewritten['history_rows'], mapper, final_paths, projects)
        global_path = home / '.codex-global-state.json'
        global_state = read_json(global_path) if global_path.exists() else {}
        global_after = merge_sidebar(global_state, projects, root_targets, home)
        atomic_json(stage / '.codex-global-state.json', global_after)
        main_ids = {tid for p in projects for tid in p['main_thread_ids']}
        main_ids.update(incoming - {r['child_thread_id'] for r in state_rows['thread_spawn_edges']})
        (stage / 'session_index.jsonl').write_bytes(index_bytes(home, state_rows['threads'], main_ids))
        changes += _native_changes(home, sqlite_home, stage, DATABASES)
        changes += [_change(global_path, stage / '.codex-global-state.json'),
                    _change(home / 'session_index.jsonl', stage / 'session_index.jsonl')]
        guard()
        if sha256_file(bundle) != bundle_hash:
            raise MigrationError('Bundle changed during planning', 'source_changed')
        return _seal(work, {'mode': 'import', 'home': str(home), 'sqlite_home': str(sqlite_home),
                            'destination': str(destination), 'profile_id': manifest['profile_id'],
                            'bundle': str(bundle), 'bundle_sha256': bundle_hash,
                            'preconditions': baseline, 'changes': changes,
                            'directories': sorted(set(directories)), 'retained_files': retained,
                            'thread_ids': sorted(incoming), 'main_thread_ids': sorted(main_ids),
                            'summary': {'projects': len(projects), 'threads': len(incoming), 'files': len(changes)},
                            'link_changes': rewritten['changes'], 'warnings': manifest.get('warnings', [])})
    except BaseException:
        shutil.rmtree(work)
        raise


def _dependencies(path: Path) -> set[str]:
    from .history import _bases
    parents = set()
    with path.open('rb') as stream:
        for line in iter(lambda: stream.readline(16 * 1024 * 1024 + 1), b''):
            if len(line) > 16 * 1024 * 1024:
                raise MigrationError('History record too large', 'planning_limit')
            value = json.loads(line)
            for base in _bases(value):
                parents.add(base['thread_id'])
    return parents


def plan_link_repair(home: Path, mappings: list[tuple[str, str]], work: Path, *,
                     thread_ids: list[str] | None = None, sqlite_home: Path | None = None,
                     check_idle: bool = True) -> Path:
    home, sqlite_home = resolve_layout(home, sqlite_home)
    if not mappings or not thread_ids:
        raise MigrationError('Link repair requires explicit mappings and thread selection', 'selection_required')
    guard = _guard(home, sqlite_home, [Path(work)], check_idle); guard()
    inventory = inspect_home(home, sqlite_home)
    baseline = capture_native(home, sqlite_home)
    threads = {t['id']: t for t in inventory['threads']}
    selected = set(thread_ids)
    if not selected <= threads.keys():
        raise MigrationError('Unknown selected thread', 'invalid_selection')
    deps = {}
    for tid, thread in threads.items():
        path = Path(thread['rollout_path']).resolve()
        if not path.is_file() or not path.is_relative_to(home):
            raise MigrationError('Native rollout outside selected Codex home or missing', 'unsupported_layout')
        deps[tid] = _dependencies(path) - {tid}
    # Only reverse dependencies are affected. Ancestors supply exact boundary
    # context and must never promote unrelated siblings into the repair scope.
    changed = True
    while changed:
        before = set(selected)
        for tid, parents in deps.items():
            if parents & selected:
                selected.add(tid)
        changed = before != selected
    context = set(selected)
    while True:
        before = set(context)
        for tid in list(context):
            context.update(deps[tid])
        if not context <= threads.keys():
            raise MigrationError('A required history ancestor is missing', 'invalid_history')
        if context == before:
            break
    full_mappings = list(mappings)
    for thread in threads.values():
        cwd = thread['cwd']
        if Path(cwd).is_dir():
            full_mappings.append((cwd, cwd))
    mapper = PathMapper(full_mappings)
    work = _new_work(work, [home, sqlite_home, *[Path(new).resolve() for _, new in mappings]])
    try:
        rollouts = {tid: Path(threads[tid]['rollout_path']).resolve() for tid in context}
        originals = {str(p): fingerprint(p) for p in rollouts.values()}
        baseline.update(originals)
        rows = read_history_rows(sqlite_home / DATABASES[1], selected)
        rewritten = rewrite_histories(rollouts, work / 'events', mapper, rows, transform_thread_ids=selected)
        stage = work / 'native'; snapshot_native(home, sqlite_home, stage)
        replace_history_rows(stage, rewritten['history_rows'], selected)
        changes = []
        for tid, source in rewritten['rollouts'].items():
            target = rollouts[tid]
            if sha256_file(Path(source)) != originals[str(target)]:
                changes.append(_change(target, Path(source)))
        changes += _native_changes(home, sqlite_home, stage, [DATABASES[1]])
        guard()
        return _seal(work, {'mode': 'repair-links', 'home': str(home), 'sqlite_home': str(sqlite_home),
                            'profile_id': inventory['profile_id'], 'preconditions': baseline,
                            'changes': changes, 'thread_ids': sorted(selected),
                            'context_thread_ids': sorted(context - selected),
                            'requested_thread_ids': sorted(thread_ids), 'link_changes': rewritten['changes'],
                            'summary': {'threads': len(selected), 'files': len(changes)},
                            'warnings': ['Explicit directory mappings prove file presence; without a source hash they do not prove original content identity.']})
    except BaseException:
        shutil.rmtree(work)
        raise


def _load_plan(path: Path) -> dict:
    path = Path(path).expanduser().resolve()
    value = read_json(path)
    if value.get('format') != 'codex-migrate-plan/v1':
        raise MigrationError('Unsupported migration plan', 'invalid_plan')
    if Path(value['journal_pointer']).resolve().parent != path.parent:
        raise MigrationError('Journal pointer escapes plan directory', 'invalid_plan')
    return value


def _validate_sources(plan: dict) -> None:
    for change in plan['changes']:
        source = change['source']
        if source and fingerprint(Path(source)) != change['stage_sha256']:
            raise MigrationError('Staged content changed after validation', 'stage_changed')
        if source and os.name != 'nt' and bool(Path(source).stat().st_mode & 0o100) != change.get('stage_executable', False):
            raise MigrationError('Staged executable permission changed after validation', 'stage_changed')
    if plan.get('bundle') and fingerprint(Path(plan['bundle'])) != plan['bundle_sha256']:
        raise MigrationError('Source bundle changed after planning', 'source_changed')


def _retained_drift(plan: dict) -> list[str]:
    drift = []
    for name, info in plan.get('retained_files', {}).items():
        path = Path(name)
        if fingerprint(path) != info['sha256'] or (os.name != 'nt' and bool(path.stat().st_mode & 0o100) != info['executable']):
            drift.append(name)
    return drift


def apply_plan(plan_path: Path, *, check_idle: bool = True) -> dict:
    plan = _load_plan(plan_path)
    home = Path(plan['home']); sqlite_home = Path(plan['sqlite_home'])
    paths = [Path(c['target']) for c in plan['changes']] + [Path(p) for p in plan.get('directories', [])] + [Path(plan_path).resolve().parent]
    guard = _guard(home, sqlite_home, paths, check_idle)
    pointer = Path(plan['journal_pointer'])
    with migration_lock(home):
        if pointer.exists():
            journal = Path(read_json(pointer)['journal'])
            return apply_transaction(journal, guard=guard)
        guard(); check_fingerprints(plan['preconditions']); _validate_sources(plan)
        if _retained_drift(plan):
            raise MigrationError('An existing project file changed after planning', 'target_changed')
        validate_profile(sqlite_home / DATABASES[0], sqlite_home / DATABASES[1])
        journal = prepare_transaction(plan['changes'], Path(plan_path).resolve().parent / 'recovery', directories=plan.get('directories', []), guard=guard)
        atomic_json(pointer, {'journal': str(journal), 'plan_id': plan['plan_id']})
        return apply_transaction(journal, guard=guard)


def recover_plan(plan_path: Path, *, check_idle: bool = True) -> dict:
    plan = _load_plan(plan_path)
    home = Path(plan['home']); sqlite_home = Path(plan['sqlite_home'])
    pointer = Path(plan['journal_pointer'])
    if not pointer.exists():
        raise MigrationError('This plan has no started transaction to recover', 'not_started')
    with migration_lock(home):
        return recover_transaction(Path(read_json(pointer)['journal']),
                                   guard=_guard(home, sqlite_home, [], check_idle))


def verify_plan(plan_path: Path) -> dict:
    plan = _load_plan(plan_path)
    pointer = Path(plan['journal_pointer'])
    if not pointer.exists():
        _validate_sources(plan); check_fingerprints(plan['preconditions'])
        return {'ok': True, 'status': 'planned', 'summary': plan['summary']}
    result = verify_transaction(Path(read_json(pointer)['journal']))
    retained_drift = _retained_drift(plan)
    if retained_drift:
        result['ok'] = False
        result['retained_file_drift'] = retained_drift
    return {**result, 'summary': plan['summary']}
