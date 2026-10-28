"""Strict ZIP reader with bounded expansion and content authentication."""
import hashlib
import json
import re
import shutil
import stat
import unicodedata
import uuid
import zipfile
from pathlib import Path
from .errors import MigrationError

MAX_MEMBERS=100000
MAX_FILE_SIZE=8*1024*1024*1024
MAX_TOTAL_SIZE=64*1024*1024*1024
MAX_MANIFEST_SIZE=16*1024*1024
MAX_COMPRESSION_RATIO=1000
MAX_JSON_RECORD=16*1024*1024
RESERVED=re.compile(r'^(CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)',re.I)

def safe_member_name(name):
    if not isinstance(name,str) or not name or '\\' in name or '\x00' in name or name.startswith('/') or len(name)>4096:
        raise MigrationError('Unsafe archive path',code='unsafe_bundle')
    parts=name.split('/')
    for part in parts:
        if part in ('','.','..') or any(ord(c)<32 or c in '<>:"|?*' for c in part) or part.endswith((' ','.')) or RESERVED.match(part):
            raise MigrationError('Unsafe or unsupported archive filename',code='unsafe_bundle')
    return unicodedata.normalize('NFC',name).casefold()

def validate_member_names(names):
    seen=set();prefixes={}
    for name in names:
        key=safe_member_name(name)
        if key in seen: raise MigrationError('Archive has duplicate or colliding filenames',code='unsafe_bundle')
        seen.add(key)
        parts=name.split('/')
        for count in range(1,len(parts)+1):
            prefix='/'.join(parts[:count]);normalized=unicodedata.normalize('NFC',prefix).casefold()
            if normalized in prefixes and prefixes[normalized]!=prefix: raise MigrationError('Archive contains colliding directory names',code='unsafe_bundle')
            prefixes[normalized]=prefix
    for key in seen:
        if any('/'.join(key.split('/')[:i]) in seen for i in range(1,len(key.split('/')))):
            raise MigrationError('Archive file/directory collision',code='unsafe_bundle')

def validate_archive_layout(files,directories):
    """Directory declarations may be ancestors of files, but never aliases."""
    validate_member_names(files)
    if not isinstance(directories,list) or len(files)+len(directories)>MAX_MEMBERS:
        raise MigrationError('Invalid or excessive directory declarations',code='bundle_limit')
    file_keys={safe_member_name(name) for name in files};directory_keys=set();prefixes={}
    for name in directories:
        key=safe_member_name(name)
        if key in directory_keys or key in file_keys: raise MigrationError('Archive directory collision',code='unsafe_bundle')
        directory_keys.add(key)
    for name in list(files)+directories:
        parts=name.split('/')
        for count in range(1,len(parts)+1):
            prefix='/'.join(parts[:count]);key=safe_member_name(prefix)
            if key in prefixes and prefixes[key]!=prefix: raise MigrationError('Archive contains colliding directory names',code='unsafe_bundle')
            prefixes[key]=prefix
            if count<len(parts) and key in file_keys: raise MigrationError('Archive file/directory collision',code='unsafe_bundle')

def _manifest_shape(value):
    if not isinstance(value,dict) or value.get('format')!='codex-migrate/v1' or not isinstance(value.get('files'),dict):
        raise MigrationError('Invalid migration manifest',code='invalid_bundle')
    required={'bundle_id','profile_id','source','schema','projects','roots','threads','assets','warnings'}
    if not required.issubset(value): raise MigrationError('Incomplete migration manifest',code='invalid_bundle')
    for key in ('projects','roots','threads','assets','warnings'):
        if not isinstance(value[key],list): raise MigrationError('Invalid manifest list',code='invalid_bundle')
    try:
        if str(uuid.UUID(value['bundle_id'])) != value['bundle_id']:
            raise ValueError('noncanonical bundle ID')
    except (ValueError, TypeError, AttributeError):
        raise MigrationError('Bundle ID must be a canonical UUID',code='invalid_bundle')
    identities={}
    for group in ('projects','roots','threads'):
        seen=set()
        for row in value[group]:
            if not isinstance(row,dict) or not isinstance(row.get('id'),str) or not re.fullmatch(r'[A-Za-z0-9_-]+',row['id']):
                raise MigrationError('Invalid manifest identifier',code='invalid_bundle')
            safe_member_name(row['id'])
            if row['id'] in seen:raise MigrationError('Duplicate manifest identifier',code='invalid_bundle')
            seen.add(row['id'])
        identities[group]=seen
    for name,meta in value['files'].items():
        safe_member_name(name)
        if name=='manifest.json' or not isinstance(meta,dict) or not isinstance(meta.get('size'),int) or not 0<=meta['size']<=MAX_FILE_SIZE or not re.fullmatch('[0-9a-f]{64}',str(meta.get('sha256',''))):
            raise MigrationError('Invalid file declaration',code='invalid_bundle')
        if not isinstance(meta.get('executable',False),bool) or (meta.get('executable') and not name.startswith('projects/')):
            raise MigrationError('Invalid owner executable declaration',code='invalid_bundle')
    declared=value['files']
    for key in ('threads','roots','assets'):
        for row in value[key]:
            if not isinstance(row,dict) or not isinstance(row.get('archive_path'),str): raise MigrationError('Invalid manifest reference',code='invalid_bundle')
            safe_member_name(row['archive_path'])
            if key!='roots' and row['archive_path'] not in declared: raise MigrationError('Undeclared manifest reference',code='invalid_bundle')
            if key=='roots' and row['archive_path']!='projects/'+row['id']:
                raise MigrationError('Project archive reference does not match its identifier',code='invalid_bundle')
            if key=='threads' and row['archive_path']!='events/'+row['id']+'.jsonl':
                raise MigrationError('Event archive reference does not match its identifier',code='invalid_bundle')
            if key=='assets' and not row['archive_path'].startswith('assets/'):
                raise MigrationError('Invalid asset archive reference',code='invalid_bundle')
            if key in ('roots','assets') and not isinstance(row.get('source_path'),str):
                raise MigrationError('Invalid source path declaration',code='invalid_bundle')
    asset_names=[row['archive_path'] for row in value['assets']]
    if len(asset_names)!=len(set(asset_names)):
        raise MigrationError('Duplicate asset reference',code='invalid_bundle')
    for project in value['projects']:
        for key,group in (('root_ids','roots'),('main_thread_ids','threads')):
            refs=project.get(key)
            if not isinstance(refs,list) or any(not isinstance(x,str) or x not in identities[group] for x in refs) or len(set(refs))!=len(refs):
                raise MigrationError('Invalid project membership reference',code='invalid_bundle')
    directories=value.get('directories',[row['archive_path'] for row in value['roots']])
    validate_archive_layout(list(value['files'])+['manifest.json'],directories)
    root_paths=[row['archive_path'] for row in value['roots']]
    for directory in directories:
        if not any(directory==root or directory.startswith(root+'/') for root in root_paths):
            raise MigrationError('Directory is outside declared project roots',code='invalid_bundle')
    return value

def extract_bundle(bundle: Path,destination: Path) -> dict:
    destination=Path(destination)
    if destination.exists() or destination.is_symlink(): raise MigrationError('Extraction requires a new directory',code='target_exists')
    destination=destination.parent.resolve()/destination.name
    owned=False
    try:
        with zipfile.ZipFile(bundle) as archive:
            infos=archive.infolist()
            if len(infos)>MAX_MEMBERS: raise MigrationError('Archive has too many members',code='bundle_limit')
            validate_member_names([info.filename for info in infos])
            seen=set();names=set();total=0
            for info in infos:
                key=safe_member_name(info.filename)
                if key in seen: raise MigrationError('Archive has duplicate or colliding filenames',code='unsafe_bundle')
                seen.add(key);names.add(info.filename)
                mode=info.external_attr>>16
                if info.is_dir() or (stat.S_IFMT(mode) not in (0,stat.S_IFREG)) or info.flag_bits&1:
                    raise MigrationError('Archive links, devices, directories or encryption are unsupported',code='unsafe_bundle')
                limit=MAX_MANIFEST_SIZE if info.filename=='manifest.json' else MAX_FILE_SIZE
                total+=info.file_size
                if info.file_size>limit or total>MAX_TOTAL_SIZE or info.file_size/max(info.compress_size,1)>MAX_COMPRESSION_RATIO:
                    raise MigrationError('Archive expansion limit exceeded',code='bundle_limit')
            if 'manifest.json' not in names: raise MigrationError('Missing migration manifest',code='invalid_bundle')
            manifest=_manifest_shape(json.loads(archive.read('manifest.json')))
            if names!=set(manifest['files'])|{'manifest.json'}: raise MigrationError('Archive contains undeclared or missing members',code='invalid_bundle')
            # A file may not also be a directory prefix.
            for name in names:
                if any('/'.join(name.split('/')[:i]) in names for i in range(1,len(name.split('/')))):
                    raise MigrationError('Archive file/directory collision',code='unsafe_bundle')
            destination.mkdir(mode=0o700);owned=True
            for directory in sorted(manifest.get('directories',[row['archive_path'] for row in manifest['roots']]),key=lambda p:(p.count('/'),p)):
                path=destination.joinpath(*directory.split('/'));path.mkdir(mode=0o700,parents=True,exist_ok=True);path.chmod(0o700)
            for info in infos:
                path=destination.joinpath(*info.filename.split('/'));path.parent.mkdir(parents=True,exist_ok=True)
                h=hashlib.sha256();size=0
                with archive.open(info) as src,path.open('xb') as dst:
                    line_size=0
                    while chunk:=src.read(1024*1024):
                        size+=len(chunk)
                        if size>info.file_size: raise MigrationError('Archive expanded beyond declaration',code='bundle_limit')
                        if info.filename.startswith(('data/','events/')):
                            chunks=chunk.split(b'\n')
                            for i,part in enumerate(chunks):
                                line_size+=len(part)
                                if line_size>MAX_JSON_RECORD: raise MigrationError('Archive JSON record exceeds limit',code='bundle_limit')
                                if i<len(chunks)-1: line_size=0
                        h.update(chunk);dst.write(chunk)
                if info.filename!='manifest.json':
                    expected=manifest['files'][info.filename]
                    if size!=expected['size'] or h.hexdigest()!=expected['sha256']: raise MigrationError('Archive content hash mismatch',code='hash_mismatch')
                path.chmod(0o700 if info.filename!='manifest.json' and manifest['files'][info.filename].get('executable',False) else 0o600)
            return manifest
    except MigrationError:
        if owned: shutil.rmtree(destination)
        raise
    except (OSError,ValueError,TypeError,KeyError,zipfile.BadZipFile,RuntimeError) as exc:
        if owned: shutil.rmtree(destination)
        raise MigrationError('Invalid or unreadable migration bundle',code='invalid_bundle') from exc
