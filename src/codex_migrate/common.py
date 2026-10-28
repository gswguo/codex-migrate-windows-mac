"""Small shared file and SQLite primitives; no application-specific data access."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .errors import MigrationError


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    with Path(path).open(encoding='utf-8-sig') as stream:
        return json.load(stream)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def fsync_directory(path: Path) -> None:
    if os.name == 'nt':
        return  # File flushing/publishing uses the Windows platform implementation.
    handle = os.open(path, os.O_RDONLY)
    try:
        os.fsync(handle)
    finally:
        os.close(handle)


def atomic_json(path: Path, value: Any) -> None:
    path = Path(path)
    if path.is_symlink():
        raise MigrationError('Refusing to replace a symbolic-link JSON file', 'unsafe_path')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(canonical_json(value))
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def connect_readonly(path: Path) -> sqlite3.Connection:
    path = Path(path).resolve()
    if not path.is_file():
        raise MigrationError(f'Database does not exist: {path}', 'missing_database')
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        connection.execute('PRAGMA schema_version').fetchone()
    except sqlite3.OperationalError:
        connection.close()
        wal = path.with_name(path.name + '-wal')
        if wal.exists() and wal.stat().st_size:
            raise
        connection = sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def snapshot_database(source: Path, destination: Path) -> None:
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb'):
        pass
    incoming = None
    outgoing = None
    try:
        incoming = connect_readonly(source)
        outgoing = sqlite3.connect(destination)
        incoming.backup(outgoing)
        if outgoing.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise MigrationError('SQLite snapshot failed integrity verification', 'database_corrupt')
        outgoing.execute('PRAGMA journal_mode=DELETE')
    except BaseException:
        if outgoing is not None:
            outgoing.close()
            outgoing = None
        destination.unlink(missing_ok=True)
        raise
    finally:
        if outgoing is not None:
            outgoing.close()
        if incoming is not None:
            incoming.close()


def encode_row(row: dict) -> dict:
    return {key: {'$bytes': base64.b64encode(value).decode('ascii')} if isinstance(value, bytes) else value
            for key, value in dict(row).items()}


def decode_row(row: dict) -> dict:
    decoded = {}
    for key, value in row.items():
        if isinstance(value, dict) and set(value) == {'$bytes'}:
            try:
                value = base64.b64decode(value['$bytes'], validate=True)
            except (TypeError, ValueError) as error:
                raise MigrationError('Invalid binary field encoding in package', 'invalid_bundle') from error
        decoded[key] = value
    return decoded


# Short aliases retained for modules which use familiar primitive names.
readonly = connect_readonly
digest = sha256_file
