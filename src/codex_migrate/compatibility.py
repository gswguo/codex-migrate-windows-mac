"""Pinned structural adapter. Package-provided DDL is never executed."""
import json
import sqlite3
from pathlib import Path
from .errors import MigrationError
from .common import connect_readonly as _common_readonly

STATE_TABLES = ('projects','project_roots','thread_sections','threads','thread_spawn_edges','thread_dynamic_tools','thread_attachments','thread_read_receipts','guardian_review_feedback')
HISTORY_TABLES = ('thread_history_projection_state','thread_items','thread_realtime_items','thread_turns')
PROFILE_PATH = Path(__file__).with_name('profiles') / 'native-v1.json'

def load_profile():
    return json.loads(PROFILE_PATH.read_text(encoding='utf-8'))

def connect_readonly(path):
    path=Path(path)
    if not path.is_file() or path.is_symlink():
        raise MigrationError('Database is missing or is a symbolic link', code='unsupported_layout')
    try:
        db=_common_readonly(path)
        db.execute('PRAGMA query_only=ON')
        return db
    except sqlite3.Error as exc:
        raise MigrationError('Cannot open native database read-only',code='invalid_database') from exc

def schema_metadata(path):
    db=connect_readonly(path)
    try:
        objects=[dict(zip(('type','name','table','sql'),row)) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
        migrations=[{'version':row[0],'description':row[1],'success':row[2],'checksum':bytes(row[3]).hex()} for row in db.execute('SELECT version,description,success,checksum FROM _sqlx_migrations ORDER BY version')]
        return {'objects':objects,'migrations':migrations}
    except (sqlite3.Error,TypeError,ValueError) as exc:
        raise MigrationError('Unknown or invalid native schema',code='unsupported_profile') from exc
    finally: db.close()

def validate_profile(state_db: Path, history_db: Path) -> dict:
    profile=load_profile()
    schema={'state':schema_metadata(state_db),'history':schema_metadata(history_db)}
    if schema != profile['databases']:
        raise MigrationError('Native schema does not match the pinned supported profile',code='unsupported_profile')
    return {'profile_id':profile['profile_id'],'schema':schema}
