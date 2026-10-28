"""Read-only native inventory with narrowly scoped location resolution."""
import json
import os
import sqlite3
import tomllib
from pathlib import Path
from .compatibility import connect_readonly,validate_profile
from .errors import MigrationError

def resolve_layout(home=None,sqlite_home=None):
    home=Path(home if home is not None else os.environ.get('CODEX_HOME',Path.home()/'.codex')).expanduser().resolve()
    explicit=sqlite_home
    if explicit is None:
        config=home/'config.toml'
        if config.is_file():
            try: explicit=tomllib.loads(config.read_text(encoding='utf-8')).get('sqlite_home')
            except (ValueError,OSError) as exc: raise MigrationError('Cannot parse Codex location configuration',code='unsupported_layout') from exc
    if explicit is None: explicit=os.environ.get('CODEX_SQLITE_HOME')
    sqlite_home=Path(explicit).expanduser() if explicit is not None else home
    if not sqlite_home.is_absolute(): sqlite_home=home/sqlite_home
    return home,sqlite_home.resolve()

def inspect_home(home: Path,sqlite_home: Path|None=None) -> dict:
    home,sqlite_home=resolve_layout(home,sqlite_home)
    profile=validate_profile(sqlite_home/'state_5.sqlite',sqlite_home/'thread_history_1.sqlite')
    db=connect_readonly(sqlite_home/'state_5.sqlite');db.row_factory=sqlite3.Row
    try:
        threads=[dict(r) for r in db.execute('SELECT * FROM threads ORDER BY id')]
        projects=[]
        for row in db.execute('SELECT id,name,created_at_ms,updated_at_ms FROM projects ORDER BY position,id'):
            p=dict(row);p['legacy_id']=None
            p['roots']=[r[0] for r in db.execute('SELECT path FROM project_roots WHERE project_id=? ORDER BY position',(p['id'],))]
            p['main_thread_ids']=[t['id'] for t in threads if t['project_id']==p['id']]
            projects.append(p)
        warnings=['Alpha adapter: native desktop acceptance is required on each platform.']
        global_path=home/'.codex-global-state.json'
        if global_path.exists():
            if global_path.is_symlink(): raise MigrationError('Global membership file cannot be a symbolic link',code='unsupported_layout')
            try:
                if global_path.stat().st_size>32*1024*1024: raise ValueError('oversize')
                global_state=json.loads(global_path.read_text(encoding='utf-8'))
                if not isinstance(global_state,dict): raise ValueError('not object')
                local=global_state.get('local-projects',{})
                host_maps=global_state.get('app-server-project-id-by-legacy-project-id-by-host',{})
                assignments=global_state.get('thread-project-assignments',{})
                hosts=global_state.get('thread-project-membership-host-ids',{})
                order=global_state.get('project-order',[])
                if not all(isinstance(x,dict) for x in (local,host_maps,assignments,hosts)) or not isinstance(order,list): raise ValueError('shape')
                server_map=host_maps.get('local:'+str(home),{})
                if not isinstance(server_map,dict): raise ValueError('host map')
                project_by_id={p['id']:p for p in projects};legacy_map={}
                for legacy,row in local.items():
                    if not isinstance(row,dict) or row.get('id')!=legacy or not isinstance(row.get('name'),str) or not isinstance(row.get('rootPaths'),list) or not all(isinstance(x,str) for x in row['rootPaths']): raise ValueError('local project')
                    mapped=server_map.get(legacy)
                    pid=mapped if mapped in project_by_id else legacy
                    if pid in project_by_id and project_by_id[pid].get('legacy_id') not in (None,legacy): raise ValueError('ambiguous project mapping')
                    p=project_by_id.get(pid)
                    if p is None:
                        p={'id':pid,'legacy_id':legacy,'name':row['name'],'roots':row['rootPaths'],'main_thread_ids':[],'created_at_ms':int(row.get('createdAt',0)),'updated_at_ms':int(row.get('updatedAt',0))}
                        projects.append(p);project_by_id[pid]=p
                    else:
                        p['legacy_id']=legacy
                        if set(p['roots'])!=set(row['rootPaths']):
                            warnings.append('Sidebar/native root mismatch; sidebar roots selected for project '+pid)
                        p['roots']=row['rootPaths'];p['name']=row['name']
                    legacy_map[legacy]=pid
                for thread in threads:
                    assignment=assignments.get(thread['id'])
                    if assignment is None: continue
                    if not isinstance(assignment,dict): raise ValueError('assignment')
                    host=hosts.get(thread['id'],'local')
                    if host!='local' or assignment.get('projectKind')!='local':
                        warnings.append('Excluded non-local sidebar membership for thread '+thread['id']);continue
                    legacy=assignment.get('projectId')
                    if legacy not in legacy_map: raise ValueError('missing local project')
                    pid=legacy_map[legacy]
                    if thread.get('project_id') not in (None,pid): raise ValueError('ambiguous thread membership')
                    thread['project_id']=pid
                for p in projects: p['main_thread_ids']=[t['id'] for t in threads if t.get('project_id')==p['id']]
                rank={legacy:i for i,legacy in enumerate(order)}
                projects.sort(key=lambda p:(rank.get(p['legacy_id'],len(rank)),p['id']))
            except (ValueError,TypeError,KeyError,OSError) as exc: raise MigrationError('Unsupported local sidebar project membership',code='unsupported_membership') from exc
        return {'home':str(home),'sqlite_home':str(sqlite_home),**profile,'projects':projects,'threads':threads,'warnings':warnings}
    except sqlite3.Error as exc: raise MigrationError('Cannot inventory native database',code='invalid_database') from exc
    finally: db.close()
