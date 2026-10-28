"""Selective native-row export. Source files are read, never modified."""
import base64
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from .bundle import safe_member_name,validate_archive_layout,MAX_FILE_SIZE,MAX_TOTAL_SIZE,MAX_MEMBERS,MAX_MANIFEST_SIZE
from .compatibility import STATE_TABLES,HISTORY_TABLES,connect_readonly
from .discovery import inspect_home
from .errors import MigrationError

MAX_JSON_LINE=16*1024*1024
STREAM_CHUNK_SIZE=1024*1024
EXCLUDED_DIRS={'.git','node_modules','.venv','venv','__pycache__','.next','dist','build','.cache','.aws','.ssh','.codex'}
EXCLUDED_FILES={'.env','.env.local','.env.production','auth.json','credentials','credentials.json','.npmrc','.pypirc','id_rsa','id_ed25519'}

def _digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk:=f.read(STREAM_CHUNK_SIZE): h.update(chunk)
    return h.hexdigest()

def _reject_link(path):
    metadata=Path(path).lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata,'st_file_attributes',0)&0x400:
        raise MigrationError('Symbolic links and Windows reparse points are unsupported in migration inputs',code='unsupported_layout')

def _no_links(path):
    path=Path(path)
    for parent in (path,*path.parents): _reject_link(parent)

def _fingerprint(path):
    path=Path(path);_no_links(path)
    s=path.stat()
    if not stat.S_ISREG(s.st_mode): raise MigrationError('Input is not a regular file',code='unsupported_layout')
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,_digest(path),bool(s.st_mode&stat.S_IXUSR))

def _register_file(path,checks):
    path=Path(path)
    if path.stat().st_size>MAX_FILE_SIZE: raise MigrationError('Input exceeds single-file size limit',code='bundle_limit')
    before=_fingerprint(path)
    if path in checks and checks[path]!=before: raise MigrationError('Source changed during export',code='source_changed')
    checks[path]=before
    return path

def _stream_file(path,destination,checks):
    """Copy one bounded chunk at a time and authenticate the bytes actually sent."""
    path=Path(path);before=checks[path];digest=hashlib.sha256();size=0
    _no_links(path)
    with path.open('rb') as incoming:
        current=os.fstat(incoming.fileno())
        if (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)!=before[:4]:
            raise MigrationError('Source changed before archive copy',code='source_changed')
        while chunk:=incoming.read(STREAM_CHUNK_SIZE):
            size+=len(chunk)
            if size>before[2] or size>MAX_FILE_SIZE: raise MigrationError('Source grew during archive copy',code='source_changed')
            digest.update(chunk);destination.write(chunk)
        current=os.fstat(incoming.fileno())
    if (current.st_dev,current.st_ino,current.st_size,current.st_mtime_ns)!=before[:4] or size!=before[2] or digest.hexdigest()!=before[4] or bool(current.st_mode&stat.S_IXUSR)!=before[5]:
        raise MigrationError('Source changed during archive copy',code='source_changed')
    return {'sha256':digest.hexdigest(),'size':size}

def _bytes_json(value):
    if isinstance(value,bytes): return {'$bytes':base64.b64encode(value).decode('ascii')}
    raise TypeError('Unsupported database value')

def _encoded(value): return json.dumps(value,ensure_ascii=False,separators=(',',':'),default=_bytes_json).encode('utf-8')

def _write_row(stream,row):
    encoded=_encoded(row)
    if len(encoded)>MAX_JSON_LINE: raise MigrationError('Native database record exceeds JSON record limit',code='bundle_limit')
    stream.write(encoded+b'\n')

def _scan_events(path,checks):
    """Inspect one bounded record at a time; raw events remain on source disk."""
    path=_register_file(path,checks);dependencies=set();assets=set()
    with path.open('rb') as stream:
        while line:=stream.readline(MAX_JSON_LINE+2):
            if len(line.rstrip(b'\r\n'))>MAX_JSON_LINE: raise MigrationError('Rollout JSON record exceeds limit',code='bundle_limit')
            try: row=json.loads(line)
            except (ValueError,UnicodeError) as exc: raise MigrationError('Invalid rollout JSON',code='invalid_history') from exc
            if not isinstance(row,dict): raise MigrationError('Invalid rollout event',code='invalid_history')
            dependencies.update(_dependencies(row));assets.update(_asset_paths(row))
    if checks[path]!=_fingerprint(path): raise MigrationError('Source changed during history scan',code='source_changed')
    return dependencies,assets

def _dependencies(value,context=''):
    found=set()
    if isinstance(value,list):
        for child in value: found.update(_dependencies(child,context))
    elif isinstance(value,dict):
        for key,child in value.items():
            if key in ('parent_thread_id','parentThreadId','forked_from_id','forkedFromId') and isinstance(child,str): found.add(child)
            if context=='history_base' and key in ('thread_id','threadId','parent_id') and isinstance(child,str): found.add(child)
            found.update(_dependencies(child,key if key=='history_base' else context))
    return found

def _asset_paths(value):
    found=set()
    if isinstance(value,list):
        for child in value: found.update(_asset_paths(child))
    elif isinstance(value,dict):
        for key,child in value.items():
            if key in ('savedPath','saved_path') and isinstance(child,str): found.add(child)
            found.update(_asset_paths(child))
    return found

def _walk_root(root,warnings):
    root=Path(root);_no_links(root)
    if not root.is_dir(): raise MigrationError('Selected project root is unavailable locally',code='missing_root')
    if (root/'.git').is_file(): raise MigrationError('Git worktrees and external gitdir repositories are unsupported',code='unsupported_layout')
    files=[];directories=[]
    def walk_error(error): raise error
    for current,dirs,names in os.walk(root,followlinks=False,onerror=walk_error):
        current=Path(current)
        directories.append(current)
        for name in list(dirs):
            p=current/name
            _reject_link(p)
            if name in EXCLUDED_DIRS:
                dirs.remove(name);warnings.add('Excluded project directory: '+str(p.relative_to(root)))
        for name in names:
            p=current/name
            _reject_link(p)
            if name in EXCLUDED_FILES or name.startswith('.env.') or p.suffix.lower() in ('.pem','.key','.p12','.pfx'):
                warnings.add('Excluded potentially sensitive file: '+str(p.relative_to(root)));continue
            files.append(p)
    return sorted(files),sorted(directories)

def export_bundle(home: Path,output: Path,*,project_ids=None,thread_ids=None,sqlite_home: Path|None=None,check_idle=True) -> dict:
    output=Path(output)
    if output.exists() or output.is_symlink(): raise MigrationError('Export requires a new output file',code='target_exists')
    if not project_ids and not thread_ids: raise MigrationError('Explicit project or thread selection is required',code='selection_required')
    inventory=inspect_home(home,sqlite_home);home=Path(inventory['home']);sqlite_home=Path(inventory['sqlite_home'])
    if check_idle:
        from .platforms import ensure_idle
        ensure_idle(home,sqlite_home)
    # Check DB, journals and scoped sidebar metadata across all reads.
    checks={};db_paths=[]
    for name in ('state_5.sqlite','thread_history_1.sqlite'):
        for suffix in ('','-wal','-journal','-shm'):
            p=sqlite_home/(name+suffix)
            if p.exists(): checks[p]=_fingerprint(p);db_paths.append(p)
    original_db_names={p.name for p in db_paths}
    global_path=home/'.codex-global-state.json'
    if global_path.exists(): checks[global_path]=_fingerprint(global_path)
    # Inspect again inside consistency window; the first inventory is only the location/profile gate.
    inventory=inspect_home(home,sqlite_home)
    projects={p['id']:p for p in inventory['projects']};threads={t['id']:t for t in inventory['threads']}
    selected_projects=set(project_ids or []);selected=set(thread_ids or [])
    if not selected_projects<=projects.keys() or not selected<=threads.keys(): raise MigrationError('Unknown selected project or thread ID',code='invalid_selection')
    selected.update(t['id'] for t in threads.values() if t.get('project_id') in selected_projects)
    if not selected and not selected_projects: raise MigrationError('Selection is empty',code='invalid_selection')
    state=connect_readonly(sqlite_home/'state_5.sqlite');state.row_factory=sqlite3.Row
    history=connect_readonly(sqlite_home/'thread_history_1.sqlite');history.row_factory=sqlite3.Row
    state.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_JSON_LINE)
    history.setlimit(sqlite3.SQLITE_LIMIT_LENGTH,MAX_JSON_LINE)
    warnings=set(inventory['warnings']);warnings.add('Project snapshots exclude Git history, common secrets and generated build directories; arbitrary file/chat secrets are not detected.')
    members={};root_files={};events={};assets=set();owned_output=False
    scratch=tempfile.TemporaryDirectory(prefix='codex-migrate-export-')
    try:
        edges=[dict(r) for r in state.execute('SELECT * FROM thread_spawn_edges')]
        queue=[(tid,True) for tid in sorted(selected)];processed=set()
        while queue:
            tid,children=queue.pop(0)
            if (tid,children) in processed: continue
            processed.add((tid,children))
            if tid not in threads: raise MigrationError('Selected history has a missing thread dependency',code='missing_dependency')
            selected.add(tid)
            if tid not in events:
                path=Path(threads[tid]['rollout_path']);_no_links(path)
                try: path.resolve().relative_to(home.resolve())
                except ValueError as exc: raise MigrationError('Rollout must be inside the selected Codex home',code='unsupported_layout') from exc
                dependencies,found_assets=_scan_events(path,checks);events[tid]=path
                assets.update(found_assets)
                for dep in dependencies:
                    if dep!=tid: queue.append((dep,False))
            for edge in edges:
                if edge['child_thread_id']==tid: queue.append((edge['parent_thread_id'],False))
                if children and edge['parent_thread_id']==tid: queue.append((edge['child_thread_id'],True))
        warnings.update('Included required thread dependency: '+t for t in selected-set(thread_ids or [])-{t['id'] for t in threads.values() if t.get('project_id') in selected_projects})
        selected_projects.update(threads[t].get('project_id') for t in selected if threads[t].get('project_id'))
        selected_sections={threads[t].get('thread_section_id') for t in selected}-{None}
        roots=[];project_records=[];root_by_path={};directories=set()
        def add_root(source):
            root=Path(source)
            if not root.is_absolute(): raise MigrationError('Selected working directory is not a local absolute path',code='unsupported_layout')
            _no_links(root);resolved_root=root.resolve()
            if any(resolved_root.is_relative_to(private) or private.is_relative_to(resolved_root) for private in (home.resolve(),sqlite_home.resolve())):
                raise MigrationError('Project root overlaps private native application data; select an independent project directory',code='unsupported_layout')
            normalized=str(resolved_root)
            if normalized in root_by_path: return root_by_path[normalized]
            rid='root-'+hashlib.sha256(normalized.encode()).hexdigest()[:24];root_by_path[normalized]=rid
            arc='projects/'+rid;roots.append({'id':rid,'source_path':source,'archive_path':arc})
            files,folders=_walk_root(root,warnings);root_files[root]=(files,folders)
            for path in files: members[arc+'/'+path.relative_to(root).as_posix()]=_register_file(path,checks)
            for path in folders:
                relative=path.relative_to(root).as_posix();directories.add(arc if relative=='.' else arc+'/'+relative)
            return rid
        for pid in sorted(selected_projects):
            p=projects[pid];root_ids=[]
            for source in p['roots']:
                root_ids.append(add_root(source))
            project_records.append({k:p[k] for k in ('id','legacy_id','name','main_thread_ids','created_at_ms','updated_at_ms')}|{'root_ids':root_ids})
            project_records[-1]['main_thread_ids']=[t for t in p['main_thread_ids'] if t in selected]
        for tid in sorted(selected):
            cwd=Path(threads[tid]['cwd'])
            if not cwd.is_absolute() or not cwd.is_dir(): raise MigrationError('Selected thread working directory is unavailable locally',code='missing_root')
            _no_links(cwd);resolved=cwd.resolve()
            if not any(resolved.is_relative_to(Path(existing)) for existing in root_by_path):
                add_root(str(cwd));warnings.add('Included independent working directory required by thread '+tid)
        for kind,db,tables in [('state',state,STATE_TABLES),('history',history,HISTORY_TABLES)]:
            for table in tables:
                spool=Path(scratch.name).resolve()/(kind+'-'+table+'.jsonl')
                with spool.open('xb') as stream:
                    if table=='projects':
                        for i,pid in enumerate(sorted(selected_projects)):
                            _write_row(stream,{'id':pid,'name':projects[pid]['name'],'metadata':'{}','position':i,'created_at_ms':projects[pid]['created_at_ms'],'updated_at_ms':projects[pid]['updated_at_ms']})
                    elif table=='project_roots':
                        for pid in sorted(selected_projects):
                            for i,path in enumerate(projects[pid]['roots']):
                                _write_row(stream,{'project_id':pid,'position':i,'path':path})
                    else:
                        pk=sorted((r[5],r[1]) for r in db.execute('PRAGMA table_info("'+table+'")') if r[5])
                        order=','.join('"'+name+'"' for _,name in pk)
                        for row in db.execute('SELECT * FROM "'+table+'" ORDER BY '+order):
                            r=dict(row)
                            if table=='thread_sections': include=r['id'] in selected_sections
                            elif table=='thread_spawn_edges': include=r['parent_thread_id'] in selected and r['child_thread_id'] in selected
                            elif table=='threads': include=r['id'] in selected
                            else: include=r['thread_id'] in selected
                            if not include: continue
                            if table=='threads':
                                r['project_id']=threads[r['id']].get('project_id')
                                r['creator_user_id']=None;r['creator_account_id']=None
                            if table in ('thread_items','thread_realtime_items'):
                                try: assets.update(_asset_paths(json.loads(r['item_json'])))
                                except ValueError as exc: raise MigrationError('Invalid history item JSON',code='invalid_history') from exc
                            if table=='thread_attachments':
                                if r['attachment_type'] not in ('image','file','generated_image'): raise MigrationError('Selected thread has unsupported attachment type',code='unsupported_attachment')
                                try: assets.update(_asset_paths(json.loads(r['payload'])))
                                except ValueError as exc: raise MigrationError('Invalid attachment metadata',code='unsupported_attachment') from exc
                            _write_row(stream,r)
                members['data/'+kind+'/'+table+'.jsonl']=_register_file(spool,checks)
        thread_records=[]
        for tid in sorted(selected):
            arc='events/'+tid+'.jsonl';safe_member_name(arc);members[arc]=events[tid];thread_records.append({'id':tid,'archive_path':arc})
        asset_records=[]
        for source in sorted(assets):
            path=Path(source);_no_links(path)
            try: path.resolve().relative_to((home/'generated_images').resolve())
            except ValueError:
                # Project-scoped files are already snapshotted; arbitrary text paths are not followed.
                if any(path.resolve().is_relative_to(r.resolve()) for r in root_files): continue
                raise MigrationError('Selected attachment is outside supported generated_images or project roots',code='unsupported_attachment')
            aid='asset-'+hashlib.sha256(str(path).encode()).hexdigest()[:24];arc='assets/'+aid+'/'+path.name
            members[arc]=_register_file(path,checks);asset_records.append({'source_path':source,'archive_path':arc})
        validate_archive_layout(list(members)+['manifest.json'],sorted(directories))
        total=sum(checks[path][2] for path in members.values())
        if len(members)>MAX_MEMBERS-1 or total>MAX_TOTAL_SIZE: raise MigrationError('Export exceeds bundle limits',code='bundle_limit')
        manifest={'format':'codex-migrate/v1','bundle_id':str(uuid.uuid4()),'profile_id':inventory['profile_id'],'source':{'platform':'windows' if sys.platform=='win32' else 'macos','home':str(home),'sqlite_home':str(sqlite_home)},'schema':inventory['schema'],'projects':project_records,'roots':roots,'threads':thread_records,'assets':asset_records,'directories':sorted(directories),'files':{},'warnings':sorted(warnings)}
        output.parent.mkdir(parents=True,exist_ok=True)
        with output.open('xb') as sink:
            owned_output=True
            if os.name!='nt': os.chmod(output,0o600)
            # Level 1 also bounds the ratio of repetitive legitimate files below
            # the reader's expansion-ratio limit.
            with zipfile.ZipFile(sink,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1,allowZip64=True) as archive:
                for name,path in sorted(members.items()):
                    with archive.open(name,'w',force_zip64=True) as destination:
                        manifest['files'][name]=_stream_file(path,destination,checks)
                        manifest['files'][name]['executable']=name.startswith('projects/') and checks[path][5]
                encoded_manifest=_encoded(manifest)
                if len(encoded_manifest)>MAX_MANIFEST_SIZE or total+len(encoded_manifest)>MAX_TOTAL_SIZE:
                    raise MigrationError('Export manifest or total size exceeds limits',code='bundle_limit')
                archive.writestr('manifest.json',encoded_manifest)
        for path,before in checks.items():
            if not path.exists() or _fingerprint(path)!=before: raise MigrationError('Source changed during export; retry while Codex is closed',code='source_changed')
        if global_path.exists()!=(global_path in checks): raise MigrationError('Source membership changed during export',code='source_changed')
        current_db_names={p.name for p in sqlite_home.iterdir() if p.name in {n+s for n in ('state_5.sqlite','thread_history_1.sqlite') for s in ('','-wal','-journal','-shm')}}
        if current_db_names!=original_db_names: raise MigrationError('Database journal set changed during export',code='source_changed')
        for root,snapshot in root_files.items():
            if _walk_root(root,set())!=snapshot: raise MigrationError('Project file or directory set changed during export',code='source_changed')
        if check_idle:
            from .platforms import ensure_idle
            ensure_idle(home,sqlite_home)
        return manifest
    except BaseException as exc:
        # Unlink only the new output created by this invocation.
        if owned_output and output.exists(): output.unlink()
        if isinstance(exc,(OSError,sqlite3.Error,ValueError)):
            raise MigrationError('Cannot export the selected local files and native rows',code='export_failed') from exc
        raise
    finally:
        state.close();history.close()
        scratch.cleanup()
