"""Journaled, offline publication of complete files with fingerprint checks.

This is a recoverable sequence, not an atomic multi-file commit. Keep all writers
closed through completion/recovery. Unknown target changes always stop recovery.
Journal directories are private local recovery material, not portable input.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from .errors import MigrationError
from .platforms import ensure_idle, flush_directory, journal_lock, make_private_directory, move_file, remove_file


def _error(message):
    return MigrationError(message, code='transaction_conflict')


def _path(value, *, inspect=True):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise _error('Transaction paths must be absolute without parent traversal')
    if not inspect:
        return path
    # lstat every existing component: resolve() alone conceals symlinks/junctions.
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise _error('Symlinks and junctions are not allowed: ' + str(part))
    return path


def _directory_identity(value):
    try:
        path = _path(value)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISDIR(info.st_mode):
            raise _error('Directory destination collides with a non-directory: ' + str(path))
        return [info.st_dev, info.st_ino]
    except OSError as exc:
        raise _error('Cannot inspect directory destination: ' + str(value) + ': ' + str(exc)) from exc


def _preflight_directories(data, *, recognized=False, require_present=False):
    owned = {entry['path']: entry['identity'] for entry in data['created_dirs']}
    for item in data.get('directories', []):
        current = _directory_identity(item['path'])
        if require_present and current is None:
            raise _error('Required directory is missing: ' + item['path'])
        if current == item['before_identity']:
            continue
        if recognized and current is not None and current == owned.get(item['path']):
            continue
        raise _error('Unexpected directory change; preserve current contents: ' + item['path'])


def _owner_executable(info):
    return None if os.name == 'nt' else bool(info.st_mode & stat.S_IXUSR)


def _fingerprint(path):
    path = _path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode):
        raise _error('Expected a regular file: ' + str(path))
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise _error('File changed while opening: ' + str(path))
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    if (opened.st_size, opened.st_mtime_ns, _owner_executable(opened)) != (after.st_size, after.st_mtime_ns, _owner_executable(after)):
        raise _error('File changed while fingerprinting: ' + str(path))
    return digest, _owner_executable(after)


def _hash(path):
    return _fingerprint(path)[0]


_UNCHECKED_MODE = object()


def _expect(path, expected, executable=_UNCHECKED_MODE):
    actual, actual_executable = _fingerprint(path)
    if actual != expected or (executable is not _UNCHECKED_MODE and actual_executable != executable):
        raise _error('Target content or owner execute bit changed: ' + str(path))


def _recorded_state(item, side):
    return item[side + '_sha256'], None if os.name == 'nt' else item[side + '_executable']


def _copy(source, target, expected, executable):
    _expect(source, expected, executable)
    mode = 0o600 | (0o100 if executable else 0)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), mode)
    with os.fdopen(fd, 'wb') as outgoing, open(source, 'rb') as incoming:
        if os.name != 'nt':
            os.fchmod(outgoing.fileno(), mode)
        shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    _expect(target, expected, executable)
    _expect(source, expected, executable)
    flush_directory(target.parent)


def _save(path, document):
    temp = path.parent / ('.journal-' + uuid.uuid4().hex)
    try:
        with temp.open('x', encoding='utf-8', newline='\n') as stream:
            json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        move_file(temp, path, replace=True)
    finally:
        if temp.exists():
            temp.unlink()


def _journal_path(journal):
    path = _path(journal)
    if path.is_dir():
        path = path / 'journal.json'
    if path.name != 'journal.json':
        raise _error('Expected a private transaction journal.json')
    return path


def _load(path):
    _expect_regular = _hash(path)
    if _expect_regular is None:
        raise _error('Journal does not exist')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['format'] != 'codex-migrate-transaction/v1' or data['status'] not in ('prepared', 'applying', 'recovering', 'completed', 'rolled_back'):
            raise ValueError('invalid format/status')
        uuid.UUID(data['transaction_id'])
        intended = data.get('directories', [])
        if not isinstance(intended, list) or not isinstance(data['files'], list) or not (data['files'] or intended):
            raise ValueError('invalid file/directory changes')
        data['directories'] = intended
        targets = []
        for index, item in enumerate(data['files']):
            target = _path(item['target'], inspect=False)
            if target == path.parent or path.parent in target.parents:
                raise ValueError('target overlaps journal')
            if target in targets:
                raise ValueError('duplicate target')
            targets.append(target)
            for key in ('before_sha256', 'after_sha256'):
                value = item[key]
                if value is not None and (not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value)):
                    raise ValueError('invalid hash')
            for side in ('before', 'after'):
                executable = item[side + '_executable']
                if executable is not None and not isinstance(executable, bool):
                    raise ValueError('invalid execute-bit metadata')
                if item[side + '_sha256'] is None and executable is not None:
                    raise ValueError('absent file has execute-bit metadata')
                if os.name != 'nt' and item[side + '_sha256'] is not None and executable is None:
                    raise ValueError('missing execute-bit metadata')
            if item['before_sha256'] is None and item['after_sha256'] is None:
                raise ValueError('empty operation')
            if item['stage'] != ('stage/' + str(index) if item['after_sha256'] is not None else None):
                raise ValueError('invalid stage path')
            if item['backup'] != ('backup/' + str(index) if item['before_sha256'] is not None else None):
                raise ValueError('invalid backup path')
        directory_paths = []
        for directory in intended:
            p = _path(directory['path'], inspect=False)
            identity = directory['before_identity']
            if identity is not None and (not isinstance(identity, list) or len(identity) != 2 or any(not isinstance(i, int) or isinstance(i, bool) for i in identity)):
                raise ValueError('invalid original directory identity')
            if p in directory_paths or p == path.parent or path.parent in p.parents or p in path.parent.parents:
                raise ValueError('duplicate/overlapping directory destination')
            if any(p == target or target in p.parents for target in targets):
                raise ValueError('directory collides with target file')
            directory_paths.append(p)
        if not isinstance(data.get('created_dirs'), list):
            raise ValueError('invalid created directories')
        for directory in data['created_dirs']:
            p = _path(directory['path'], inspect=False)
            identity = directory['identity']
            if identity is not None and (not isinstance(identity, list) or len(identity) != 2 or any(not isinstance(i, int) for i in identity)):
                raise ValueError('invalid directory identity')
            if not any(p in target.parents for target in targets) and not any(p == target or p in target.parents for target in directory_paths):
                raise ValueError('created directory is outside intended destinations')
        return data
    except (ValueError, KeyError, TypeError) as exc:
        raise _error('Invalid migration journal: ' + str(exc)) from exc


def _guard(data, guard):
    if guard is not None:
        guard()
    else:
        roots = set()
        for item in data['files']:
            parent = Path(item['target']).parent
            while not parent.exists():
                parent = parent.parent
            roots.add(parent)
        for item in data.get('directories', []):
            parent = Path(item['path'])
            while not parent.exists():
                parent = parent.parent
            roots.add(parent)
        for root in roots:
            ensure_idle(root)


def _preflight(data, recognized=False):
    _preflight_directories(data, recognized=recognized)
    for item in data['files']:
        current = _fingerprint(item['target'])
        allowed = (_recorded_state(item, 'before'), _recorded_state(item, 'after')) if recognized else (_recorded_state(item, 'before'),)
        if current not in allowed:
            raise _error('Unexpected target changes; preserve current files and seek manual recovery: ' + item['target'])


def _receipt(data, status):
    return {'transaction_id': data['transaction_id'], 'status': status,
            'files': [{'target': item['target'], 'before_sha256': item['before_sha256'], 'after_sha256': item['after_sha256'], 'before_executable': item['before_executable'],
                       'after_executable': item['after_executable']} for item in data['files']],
            'directories': [item['path'] for item in data.get('directories', [])]}


def prepare_transaction(changes, journal_dir, *, directories=None, guard=None):
    """Copy frozen content/backups into a new private child of journal_dir.

    A source of None records a transactional deletion of an existing target.
    Explicit directories publish empty project roots as well as parent folders.
    The returned journal path is the durable identifier for apply/recover/verify.
    """
    directories = list(directories or [])
    if not changes and not directories:
        raise _error('A transaction must contain at least one file or directory change')
    files, targets, sources = [], [], []
    for change in changes:
        target = _path(change['target'])
        source = _path(change['source']) if change.get('source') is not None else None
        before = change.get('before_sha256')
        if source is None and before is None:
            raise _error('Deletion requires an existing target fingerprint')
        if target in targets or any(target.exists() and old.exists() and os.path.samefile(target, old) for old in targets):
            raise _error('Duplicate or aliased target')
        targets.append(target)
        sources.append(source)
        actual_before, before_executable = _fingerprint(target)
        if actual_before != before:
            raise _error('Target fingerprint changed: ' + str(target))
        after, after_executable = _fingerprint(source) if source is not None else (None, None)
        if source is not None and after is None:
            raise _error('Staged source is missing')
        index = len(files)
        files.append({'target': str(target), 'before_sha256': before, 'after_sha256': after,
                      'before_executable': before_executable, 'after_executable': after_executable,
                      'backup': 'backup/' + str(index) if before is not None else None,
                      'stage': 'stage/' + str(index) if source is not None else None, 'state': 'pending'})
    for source in sources:
        if source is not None and any(source == target or source in target.parents or target in source.parents or
                                      (source.exists() and target.exists() and os.path.samefile(source, target)) for target in targets):
            raise _error('Source and target paths overlap')
    intended, directory_paths = [], []
    for value in directories:
        try:
            directory = _path(value)
        except OSError as exc:
            raise _error('Directory destination has an invalid ancestor: ' + str(value)) from exc
        identity = _directory_identity(directory)
        if directory in directory_paths:
            continue
        if any(directory == target or target in directory.parents for target in targets):
            raise _error('Directory destination collides with a target file')
        if any(source is not None and (source == directory or source in directory.parents or directory in source.parents) for source in sources):
            raise _error('Source and directory destination overlap')
        directory_paths.append(directory)
        intended.append({'path': str(directory), 'before_identity': identity})
    parent = _path(journal_dir)
    if any(parent == directory or directory in parent.parents or parent in directory.parents for directory in directory_paths):
        raise _error('Journal storage overlaps a directory destination')
    if any(parent == target or target in parent.parents or parent == source or (source is not None and source in parent.parents) for target in targets for source in sources):
        raise _error('Journal storage overlaps a source or target')
    data = {'format': 'codex-migrate-transaction/v1', 'transaction_id': str(uuid.uuid4()),
            'status': 'prepared', 'files': files, 'directories': intended, 'created_dirs': []}
    _guard(data, guard)
    parent.mkdir(parents=True, exist_ok=True)
    root = parent / ('txn-' + data['transaction_id'])
    make_private_directory(root)
    flush_directory(parent)
    (root / 'stage').mkdir(mode=0o700)
    (root / 'backup').mkdir(mode=0o700)
    journal = root / 'journal.json'
    try:
        for item, source in zip(files, sources):
            if source is not None:
                _copy(source, root / item['stage'], *_recorded_state(item, 'after'))
            if item['backup'] is not None:
                _copy(Path(item['target']), root / item['backup'], *_recorded_state(item, 'before'))
        _guard(data, guard)
        _preflight(data)
        _save(journal, data)
    except BaseException:
        # No target files have been mutated; only our new private directory exists.
        # Retain partial recovery material rather than deleting unknown files.
        raise
    return journal


def _publish(root, item, expected, desired, artifact):
    target = _path(item['target'])
    _expect(target, *expected)
    if desired[0] is None:
        remove_file(target)
        return
    temp = target.parent / ('.codex-migrate-' + uuid.uuid4().hex)
    try:
        _copy(root / artifact, temp, *desired)
        _expect(target, *expected)
        move_file(temp, target, replace=expected[0] is not None)
    except OSError as exc:
        raise _error('Cannot publish file safely: ' + str(target) + ': ' + str(exc)) from exc
    finally:
        if temp.exists():
            temp.unlink()
    _expect(target, *desired)


def _create_parents(path, data, journal, *, include_self=False):
    missing = []
    parent = path if include_self else path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        _path(directory)
        # Record intent before mkdir, so a crash cannot orphan undocumented dirs.
        entry = next((entry for entry in data['created_dirs'] if entry['path'] == str(directory)), None)
        if entry is None:
            entry = {'path': str(directory), 'identity': None}
            data['created_dirs'].append(entry)
            _save(journal, data)
        directory.mkdir()
        info = directory.lstat()
        entry['identity'] = [info.st_dev, info.st_ino]
        flush_directory(directory.parent)
        _save(journal, data)


def apply_transaction(journal, *, guard=None, fault=None):
    journal = _journal_path(journal)
    with journal_lock(journal.parent / '.lock'):
        data = _load(journal)
        if data['status'] == 'completed':
            return data['receipt']
        if data['status'] != 'prepared':
            raise _error('Transaction was interrupted or rolled back; run recovery before continuing')
        _guard(data, guard)
        _preflight(data)
        for item in data['files']:
            if item['stage'] is not None:
                _expect(journal.parent / item['stage'], *_recorded_state(item, 'after'))
            if item['backup'] is not None:
                _expect(journal.parent / item['backup'], *_recorded_state(item, 'before'))
        data['status'] = 'applying'
        _save(journal, data)
        if fault:
            fault('after_begin')
        for index, item in enumerate(data.get('directories', [])):
            _guard(data, guard)
            _preflight(data, recognized=True)
            if fault:
                fault('before_directory:' + str(index))
            _create_parents(Path(item['path']), data, journal, include_self=True)
            _preflight_directories(data, recognized=True)
            if fault:
                fault('after_directory:' + str(index))
        for index, item in enumerate(data['files']):
            _guard(data, guard)
            _preflight(data, recognized=True)
            _create_parents(Path(item['target']), data, journal)
            if fault:
                fault('before_publish:' + str(index))
            _publish(journal.parent, item, _recorded_state(item, 'before'), _recorded_state(item, 'after'), item['stage'])
            if fault:
                fault('after_publish:' + str(index))
            item['state'] = 'published'
            _save(journal, data)
            if fault:
                fault('after_record:' + str(index))
        _guard(data, guard)
        for item in data['files']:
            _expect(item['target'], *_recorded_state(item, 'after'))
        _preflight_directories(data, recognized=True, require_present=True)
        if fault:
            fault('before_complete')
        data['status'] = 'completed'
        data['receipt'] = _receipt(data, 'completed')
        _save(journal, data)
        if fault:
            fault('after_complete')
        return data['receipt']


def recover_transaction(journal, *, guard=None):
    journal = _journal_path(journal)
    with journal_lock(journal.parent / '.lock'):
        data = _load(journal)
        if data['status'] in ('completed', 'rolled_back'):
            return data['receipt']
        _guard(data, guard)
        # Validate EVERY target and backup before the first destructive action.
        _preflight(data, recognized=True)
        for item in data['files']:
            if item['backup'] is not None:
                _expect(journal.parent / item['backup'], *_recorded_state(item, 'before'))
        data['status'] = 'recovering'
        _save(journal, data)
        for item in reversed(data['files']):
            _guard(data, guard)
            _preflight(data, recognized=True)
            current = _fingerprint(item['target'])
            if current != _recorded_state(item, 'before'):
                _publish(journal.parent, item, current, _recorded_state(item, 'before'), item['backup'])
            item['state'] = 'restored'
            _save(journal, data)
        for value in reversed(data['created_dirs']):
            directory = _path(value['path'])
            try:
                info = directory.lstat()
                if value['identity'] is None or value['identity'] != [info.st_dev, info.st_ino]:
                    continue
                directory.rmdir()
                flush_directory(directory.parent)
            except FileNotFoundError:
                pass
            except OSError:
                # Never recursively remove directories or subsequent user files.
                pass
        _guard(data, guard)
        for item in data['files']:
            _expect(item['target'], *_recorded_state(item, 'before'))
        _preflight_directories(data, recognized=True)
        data['status'] = 'rolled_back'
        data['receipt'] = _receipt(data, 'rolled_back')
        _save(journal, data)
        return data['receipt']


def verify_transaction(journal):
    journal = _journal_path(journal)
    with journal_lock(journal.parent / '.lock'):
        data = _load(journal)
        drift = []
        side = 'before' if data['status'] == 'rolled_back' else 'after'
        expected_key = side + '_sha256'
        executable_key = side + '_executable'
        for item in data['files']:
            try:
                actual, actual_executable = _fingerprint(item['target'])
            except (MigrationError, OSError) as exc:
                drift.append({'target': item['target'], 'expected_sha256': item[expected_key], 'actual_sha256': None, 'error': str(exc)})
                continue
            if (actual, actual_executable) != _recorded_state(item, side):
                drift.append({'target': item['target'], 'expected_sha256': item[expected_key], 'actual_sha256': actual,
                              'expected_executable': item[executable_key], 'actual_executable': actual_executable})
        for item in data.get('directories', []):
            try:
                current = _directory_identity(item['path'])
                if data['status'] == 'rolled_back':
                    valid = current == item['before_identity']
                else:
                    valid = current is not None
            except (MigrationError, OSError) as exc:
                drift.append({'target': item['path'], 'type': 'directory', 'error': str(exc)})
                continue
            if not valid:
                drift.append({'target': item['path'], 'type': 'directory', 'error': 'Required directory state differs'})
        return {'transaction_id': data['transaction_id'], 'status': data['status'],
                'ok': data['status'] in ('completed', 'rolled_back') and not drift, 'drift': drift}
