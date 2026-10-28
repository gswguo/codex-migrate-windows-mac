"""Transform staged native rollouts and rebuild ordinal-based UTF-8 boundaries."""
from __future__ import annotations

from collections import Counter
import copy
import json
import hashlib
import os
from pathlib import Path
import re
import shutil

from .errors import MigrationError
from .links import repair_markdown, transform_item
from .paths import PathMapper

MAX_EVENT_BYTES = 16 * 1024 * 1024


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _bases(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'history_base':
                if not isinstance(child, dict):
                    raise MigrationError('Invalid history_base object', code='history_boundary')
                if not isinstance(child.get('thread_id'), str) or not _integer(child.get('end_ordinal_exclusive')):
                    raise MigrationError('Invalid history_base identity or ordinal', code='history_boundary')
                if any(('offset' in key or 'ordinal' in key) and key not in {'end_ordinal_exclusive', 'end_byte_offset'} for key in child):
                    raise MigrationError('Unknown history_base boundary semantics', code='unknown_history_offset')
                yield child
            else:
                yield from _bases(child)
    elif isinstance(value, list):
        for child in value:
            yield from _bases(child)


def transform_runtime(payload: dict, mapper: PathMapper) -> tuple[dict, list[dict]]:
    """Map allowlisted runtime fields; revoke unmapped grants and full access."""
    result = copy.deepcopy(payload)
    changes = []
    def note(field, old, new):
        if old != new:
            changes.append({'field': field, 'old': old, 'new': new, 'status': 'structural'})
    def path_field(container, key, field, required=False):
        if key not in container:
            return
        if not isinstance(container[key], str):
            raise MigrationError('Invalid structural path: ' + field, code='unknown_native_field')
        old = container[key]
        mapped = mapper.map_path(old, required=required)
        note(field, old, mapped)
        if mapped is None:
            container.pop(key)
        else:
            container[key] = mapped
    def root_list(container, key, field):
        if key not in container:
            return
        roots = container[key]
        if not isinstance(roots, list) or any(not isinstance(p, str) for p in roots):
            raise MigrationError('Invalid structural path list: ' + field, code='unknown_native_field')
        mapped = [new for old in roots if (new := mapper.map_path(old)) is not None]
        note(field, roots, mapped)
        container[key] = mapped
    def entries(container, key, field):
        if key not in container:
            return
        if not isinstance(container[key], list):
            raise MigrationError('Unknown filesystem permission entries', code='unknown_permission')
        new_entries = []
        for entry in container[key]:
            if not isinstance(entry, dict) or not isinstance(entry.get('path'), dict):
                raise MigrationError('Unknown filesystem permission entry', code='unknown_permission')
            spec = entry['path']
            if spec.get('type') == 'path':
                if not isinstance(spec.get('path'), str):
                    raise MigrationError('Invalid permission path', code='unknown_permission')
                new = mapper.map_path(spec['path'])
                if new is None:
                    note(field, entry, None)
                    continue
                note(field + '.path', spec['path'], new)
                spec['path'] = new
            elif spec.get('type') != 'special':
                raise MigrationError('Unknown permission path variant', code='unknown_permission')
            new_entries.append(entry)
        container[key] = new_entries
    def permission(profile, field):
        if not isinstance(profile, dict):
            raise MigrationError('Unknown permission profile', code='unknown_permission')
        fs = profile.get('file_system')
        if fs is not None:
            if not isinstance(fs, dict):
                raise MigrationError('Unknown filesystem permission profile', code='unknown_permission')
            entries(fs, 'entries', field + '.file_system.entries')
        if 'network' in profile:
            note(field + '.network', profile['network'], 'restricted')
            profile['network'] = 'restricted'
    path_field(result, 'cwd', 'cwd', required=True)
    for key in ('runtime_workspace_roots', 'workspace_roots', 'writable_roots'):
        root_list(result, key, key)
    for key in ('approval_policy', 'approval_mode'):
        if result.get(key) == 'never':
            note(key, 'never', 'on-request')
            result[key] = 'on-request'
    for key in ('permission_profile', 'active_permission_profile'):
        if key in result:
            permission(result[key], key)
    if 'sandbox_policy' in result:
        policy = result['sandbox_policy']
        if not isinstance(policy, dict) or policy.get('type') not in {'danger-full-access', 'workspace-write', 'read-only', 'external-sandbox'}:
            raise MigrationError('Unknown sandbox policy', code='unknown_permission')
        if policy['type'] in {'danger-full-access', 'external-sandbox'}:
            note('sandbox_policy.type', policy['type'], 'workspace-write')
            policy['type'] = 'workspace-write'
        root_list(policy, 'writable_roots', 'sandbox_policy.writable_roots')
        if 'network_access' in policy:
            note('sandbox_policy.network_access', policy['network_access'], False)
            policy['network_access'] = False
        if 'network' in policy:
            note('sandbox_policy.network', policy['network'], 'restricted')
            policy['network'] = 'restricted'
        if isinstance(policy.get('file_system'), dict):
            entries(policy['file_system'], 'entries', 'sandbox_policy.file_system.entries')
    if 'file_system_sandbox_policy' in result:
        policy = result['file_system_sandbox_policy']
        if not isinstance(policy, dict):
            raise MigrationError('Unknown filesystem sandbox policy', code='unknown_permission')
        entries(policy, 'entries', 'file_system_sandbox_policy.entries')
    return result, changes


def _transform_event(event, mapper):
    result = copy.deepcopy(event)
    changes = []
    payload = result.get('payload')
    if not isinstance(payload, dict):
        return result, changes
    kind = result.get('type')
    if kind in {'session_meta', 'turn_context'}:
        payload, records = transform_runtime(payload, mapper)
        result['payload'] = payload
        changes.extend(records)
    elif kind == 'event_msg' and payload.get('type') == 'thread_settings_applied':
        settings = payload.get('thread_settings')
        if not isinstance(settings, dict):
            raise MigrationError('Invalid native thread settings', code='unknown_native_field')
        payload['thread_settings'], records = transform_runtime(settings, mapper)
        changes.extend({**r, 'field': 'thread_settings.' + r['field']} for r in records)
    elif kind == 'world_state':
        state = payload.get('state', {})
        envs = state.get('environments', {}) if isinstance(state, dict) else {}
        envs = envs.get('environments', {}) if isinstance(envs, dict) else {}
        if isinstance(envs, dict):
            for env in envs.values():
                if isinstance(env, dict) and isinstance(env.get('cwd'), str):
                    old = env['cwd']
                    new = mapper.map_path(old)
                    if new is None:
                        env.pop('cwd')
                    else:
                        env['cwd'] = new
                    if old != new:
                        changes.append({'field': 'state.environments.cwd', 'old': old, 'new': new, 'status': 'structural'})
    if kind == 'event_msg' and payload.get('type') == 'item_completed':
        if not isinstance(payload.get('item'), dict):
            raise MigrationError('Invalid completed native item', code='unknown_native_field')
        payload['item'], records = transform_item(payload['item'], mapper)
        changes.extend({**r, 'field': 'item.' + r['field']} for r in records)
    elif kind in {'event_msg', 'response_item'}:
        updated, records = transform_item(payload, mapper)
        result['payload'] = updated
        changes.extend(records)
    return result, changes


def _require_consistent_text_elements(records):
    if any(record.get('status') == 'unsupported-text-elements' for record in records):
        raise MigrationError('Native text element ranges overlap a rewritten link or are invalid; '
                             'resolve this item before planning so replay and projected history stay consistent',
                             code='unsupported_text_elements')


def _iter_rollout(source: Path, sid: str, digest, *, include_raw=False):
    """Decode one event at a time and reject changes while its descriptor is read."""
    source = Path(source)
    last, first = -1, True
    try:
        with source.open('rb') as stream:
            before = os.fstat(stream.fileno())
            for raw in iter(lambda: stream.readline(MAX_EVENT_BYTES + 1), b''):
                if len(raw) > MAX_EVENT_BYTES:
                    raise MigrationError('Rollout event exceeds alpha 16 MiB planning limit', code='planning_limit')
                digest.update(raw)
                record = json.loads(raw)
                ordinal = record.get('ordinal') if isinstance(record, dict) else None
                if not _integer(ordinal) or ordinal <= last:
                    raise MigrationError('Invalid, duplicate, or nonmonotonic rollout ordinal', code='history_ordinal')
                if first:
                    if record.get('type') != 'session_meta' or not isinstance(record.get('payload'), dict):
                        raise MigrationError('Rollout must start with session metadata', code='history_identity')
                    if record['payload'].get('id') != sid:
                        raise MigrationError('Rollout metadata identity mismatch', code='history_identity')
                    first = False
                last = ordinal
                yield (record, raw) if include_raw else record
            after = os.fstat(stream.fileno())
            current = source.stat()
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if identity(before) != identity(after) or identity(after) != identity(current):
                raise MigrationError('Staged rollout changed while being read', code='source_changed')
    except (ValueError, UnicodeError) as exc:
        raise MigrationError('Invalid JSONL rollout', code='invalid_json') from exc
    if first:
        raise MigrationError('Rollout must start with session metadata', code='history_identity')


def rewrite_histories(rollouts: dict[str, Path], destination: Path, mapper: PathMapper,
                      history_rows: dict[str, list[dict]], *, final_rollout_paths: dict[str, str] | None = None,
                      transform_thread_ids: set[str] | None = None) -> dict:
    """Stream new JSONL files, preserving event identity and original input objects.

    Pass one collects dependencies and requested ordinal boundaries without retaining
    event bodies. Pass two rewrites files in dependency order, keeping only one event
    and the offsets referenced by history bases/rows. Memory therefore depends on the
    largest individual event plus the returned rows/change report, not rollout bytes.
    Input hashes must agree between passes. Unknown offset columns fail closed.
    Explicit transform_thread_ids leave other context rollouts byte-for-byte intact
    and return rows only for affected threads. All descendants must be included.
    """
    destination = Path(destination)
    if destination.exists():
        raise MigrationError('History destination must be a new directory', code='destination_exists')
    affected = set(rollouts) if transform_thread_ids is None else set(transform_thread_ids)
    if not affected <= rollouts.keys():
        raise MigrationError('Affected history threads are missing from rollout context', code='invalid_selection')
    dependencies = {}
    requested_offsets = {sid: set() for sid in rollouts}
    requested_boundaries = {sid: set() for sid in rollouts}
    input_hashes = {}
    for sid, source in rollouts.items():
        if not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', sid):
            raise MigrationError('Unsafe rollout identifier', code='history_identity')
        digest = hashlib.sha256()
        deps = set()
        for record in _iter_rollout(source, sid, digest):
            for base in _bases(record):
                parent = base['thread_id']
                if parent not in rollouts:
                    raise MigrationError('History ancestor missing from selected scope', code='missing_ancestor')
                boundary = base['end_ordinal_exclusive']
                requested_boundaries[parent].add(boundary)
                if parent == sid:
                    if boundary > record['ordinal']:
                        raise MigrationError('Same-file history base points to unfinished prefix', code='history_cycle')
                else:
                    deps.add(parent)
        del record
        dependencies[sid] = deps
        input_hashes[sid] = digest.hexdigest()
    if any(sid not in affected and parents & affected for sid, parents in dependencies.items()):
        raise MigrationError('History scope omits a descendant of an affected rollout', code='invalid_selection')
    ordered = []
    remaining = set(rollouts)
    while remaining:
        ready = sorted(sid for sid in remaining if not (dependencies[sid] & remaining))
        if not ready:
            raise MigrationError('History byte dependency graph contains a cycle', code='history_cycle')
        ordered.extend(ready)
        remaining.difference_update(ready)
    allowed_offsets = {'thread_turns': {'rollout_byte_offset', 'rollout_end_byte_offset'},
                       'thread_history_projection_state': {'next_rollout_byte_offset'}}
    rows = copy.deepcopy(history_rows)
    for table, table_rows in rows.items():
        retained = []
        for row in table_rows:
            sid = row.get('thread_id')
            unknown = {key for key in row if key.endswith('_offset') or 'byte_offset' in key} - allowed_offsets.get(table, set())
            if unknown:
                raise MigrationError('Unknown native byte-offset semantics: ' + table, code='unknown_history_offset')
            if table in {'thread_turns', 'thread_history_projection_state', 'thread_items', 'thread_realtime_items'} and sid not in rollouts:
                raise MigrationError('History row references unstaged rollout', code='missing_ancestor')
            if transform_thread_ids is not None and sid not in affected:
                continue
            retained.append(row)
            if table == 'thread_turns':
                start, end = row.get('rollout_ordinal'), row.get('rollout_end_ordinal')
                if not _integer(start) or (end is not None and (not _integer(end) or end < start)):
                    raise MigrationError('Turn ordinal missing or invalid', code='history_boundary')
                requested_offsets[sid].add(start)
                if end is not None:
                    requested_offsets[sid].add(end)
            elif table == 'thread_history_projection_state':
                ordinal = row.get('next_rollout_ordinal')
                if not _integer(ordinal):
                    raise MigrationError('Invalid boundary ordinal', code='history_boundary')
                requested_boundaries[sid].add(ordinal)
        rows[table] = retained
    output_paths, output_offsets, output_boundaries = {}, {}, {}
    changes = []
    counts = Counter()
    destination.mkdir(parents=True)
    try:
        for sid in ordered:
            offsets, boundaries, size = {}, {}, 0
            requests = sorted(requested_boundaries[sid])
            boundary_index = 0
            digest = hashlib.sha256()
            output = destination / (sid + '.jsonl')
            with output.open('xb') as stream:
                for event, original_raw in _iter_rollout(rollouts[sid], sid, digest, include_raw=True):
                    ordinal = event['ordinal']
                    while boundary_index < len(requests) and requests[boundary_index] <= ordinal:
                        boundaries[requests[boundary_index]] = size
                        boundary_index += 1
                    updated, records, raw = event, [], original_raw
                    if sid in affected:
                        updated, records = _transform_event(event, mapper)
                        _require_consistent_text_elements(records)
                        changes.extend({**r, 'thread_id': sid, 'ordinal': ordinal} for r in records)
                        for base in _bases(updated):
                            parent, requested = base['thread_id'], base['end_ordinal_exclusive']
                            old = base.get('end_byte_offset')
                            known = boundaries if parent == sid else output_boundaries.get(parent, {})
                            if requested not in known:
                                raise MigrationError('Staged history dependencies changed between passes', code='source_changed')
                            offset = known[requested]
                            base['end_byte_offset'] = offset
                            if final_rollout_paths and 'rollout_path' in base:
                                if parent not in final_rollout_paths:
                                    raise MigrationError('Missing final ancestor rollout path', code='missing_ancestor')
                                base['rollout_path'] = final_rollout_paths[parent]
                            if old != offset:
                                changes.append({'thread_id': sid, 'ordinal': ordinal, 'field': 'history_base.end_byte_offset', 'old': old, 'new': offset, 'status': 'offset'})
                            counts['history_bases'] += 1
                        raw = json.dumps(updated, ensure_ascii=False, separators=(',', ':')).encode('utf-8') + b'\n'
                    stream.write(raw)
                    if ordinal in requested_offsets[sid]:
                        offsets[ordinal] = (size, size + len(raw))
                    size += len(raw)
                    counts['events'] += 1
                    counts['changed_events'] += updated != event
                    del updated, records, event, original_raw
            if digest.hexdigest() != input_hashes[sid]:
                raise MigrationError('Staged rollout changed between rewrite passes', code='source_changed')
            for requested in requests[boundary_index:]:
                boundaries[requested] = size
            if offsets.keys() != requested_offsets[sid]:
                raise MigrationError('Turn ordinal missing or invalid', code='history_boundary')
            output_paths[sid] = str(output)
            output_offsets[sid], output_boundaries[sid] = offsets, boundaries
        for table, table_rows in rows.items():
            for row in table_rows:
                before_row = copy.deepcopy(row)
                sid = row.get('thread_id')
                if table in {'thread_items', 'thread_realtime_items'} and 'item_json' in row:
                    raw_item = row['item_json']
                    try:
                        item = json.loads(raw_item) if isinstance(raw_item, (str, bytes)) else raw_item
                    except (ValueError, UnicodeError) as exc:
                        raise MigrationError('Invalid native item JSON', code='invalid_json') from exc
                    if not isinstance(item, dict):
                        raise MigrationError('Native item must be an object', code='unknown_native_field')
                    updated, records = transform_item(item, mapper)
                    _require_consistent_text_elements(records)
                    changes.extend({**r, 'thread_id': sid, 'table': table, 'row_id': row.get('id')} for r in records)
                    if updated != item:
                        encoded = json.dumps(updated, ensure_ascii=False, separators=(',', ':'))
                        row['item_json'] = encoded.encode() if isinstance(raw_item, bytes) else encoded if isinstance(raw_item, str) else updated
                elif table == 'thread_turns':
                    offsets = output_offsets[sid]
                    start, end = row.get('rollout_ordinal'), row.get('rollout_end_ordinal')
                    row['rollout_byte_offset'] = offsets[start][0]
                    row['rollout_end_byte_offset'] = None if end is None else offsets[end][1]
                elif table == 'thread_history_projection_state':
                    row['next_rollout_byte_offset'] = output_boundaries[sid][row['next_rollout_ordinal']]
                for key in allowed_offsets.get(table, set()):
                    if before_row.get(key) != row.get(key):
                        changes.append({'thread_id': sid, 'table': table, 'row_id': row.get('id'), 'field': key, 'old': before_row.get(key), 'new': row.get(key), 'status': 'offset'})
                counts['history_rows'] += 1
        counts['rollouts'] = len(output_paths)
        counts['context_rollouts'] = len(rollouts) - len(affected)
        counts['transformed_rollouts'] = len(affected)
        for record in changes:
            counts[record['status']] += 1
        return {'rollouts': output_paths, 'history_rows': rows, 'changes': changes, 'counts': dict(counts)}
    except BaseException:
        shutil.rmtree(destination)
        raise
