"""Synthetic native databases; never copies developer records."""
import json
import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

PROFILE = Path(__file__).parents[1] / 'src/codex_migrate/profiles/native-v1.json'

@contextmanager
def managed_connection(*args,**kwargs):
    """A transaction context which also releases native Windows file handles."""
    connection=sqlite3.connect(*args,**kwargs)
    try:
        with connection:
            yield connection
    finally:
        connection.close()

def create_schema(path: Path, kind: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    metadata = json.loads(PROFILE.read_text())['databases'][kind]
    with managed_connection(path) as db:
        for obj in sorted(metadata['objects'], key=lambda x: x['type'] != 'table'):
            db.execute(obj['sql'])
        for m in metadata['migrations']:
            db.execute('INSERT INTO _sqlx_migrations(version,description,success,checksum,execution_time) VALUES(?,?,?,?,0)', (m['version'], m['description'], m['success'], bytes.fromhex(m['checksum'])))

def insert(db, table, row):
    names = ','.join('"'+x+'"' for x in row)
    db.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))

def make_home(root: Path, *, platform='macos', prefix='source'):
    root = Path(root).resolve()
    home = root / (prefix + '-home')
    project = root / (prefix + '-project')
    project.mkdir(parents=True)
    home.mkdir(parents=True)
    (project / 'hello.txt').write_text('Synthetic project\n')
    (project / '.env').write_text('SYNTHETIC_SECRET=excluded\n')
    (project / 'node_modules').mkdir()
    (project / 'node_modules' / 'excluded.js').write_text('build dependency')
    project_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-migrate-synthetic:'+prefix+':project'))
    thread_id = str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-migrate-synthetic:'+prefix+':thread'))
    unassigned = str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-migrate-synthetic:'+prefix+':unassigned'))
    create_schema(home / 'state_5.sqlite', 'state')
    create_schema(home / 'thread_history_1.sqlite', 'history')
    rollouts = {}
    with managed_connection(home / 'state_5.sqlite') as db:
        insert(db, 'projects', {'id':project_id,'name':'Synthetic project','position':0,'created_at_ms':1000,'updated_at_ms':2000})
        insert(db, 'project_roots', {'project_id':project_id,'position':0,'path':str(project)})
        for tid, pid in [(thread_id,project_id),(unassigned,None)]:
            rollout = home / 'sessions/2026/01/01' / ('rollout-2026-01-01T00-00-00-' + tid + '.jsonl')
            rollout.parent.mkdir(parents=True,exist_ok=True)
            turn_id=str(uuid.uuid5(uuid.UUID(tid),'synthetic-turn'))
            item_id=str(uuid.uuid5(uuid.UUID(tid),'synthetic-item'))
            message='Read [file]('+str(project / 'hello.txt')+')'
            item={'id':item_id,'type':'userMessage','content':[{'type':'text','text':message}]}
            response={'id':item_id,'type':'message','role':'user','content':[{'type':'input_text','text':message}]}
            meta={'id':tid,'cwd':str(project),'timestamp':'2026-01-01T00:00:00Z','originator':'synthetic-fixture','cli_version':'0.164.0-alpha.11.1','source':'cli','model_provider':'openai','history_mode':'paginated'}
            user_event={'type':'user_message','message':message,'images':[],'local_images':[],'text_elements':[]}
            events=[{'timestamp':'2026-01-01T00:00:00Z','type':'session_meta','payload':meta,'ordinal':0},{'timestamp':'2026-01-01T00:00:01Z','type':'event_msg','payload':{'type':'task_started','turn_id':turn_id},'ordinal':1},{'timestamp':'2026-01-01T00:00:02Z','type':'event_msg','payload':user_event,'ordinal':2},{'timestamp':'2026-01-01T00:00:02Z','type':'response_item','payload':response,'ordinal':3},{'timestamp':'2026-01-01T00:00:03Z','type':'event_msg','payload':{'type':'task_complete','turn_id':turn_id},'ordinal':4}]
            lines=[(json.dumps(x,ensure_ascii=False)+'\n').encode() for x in events]
            rollout.write_bytes(b''.join(lines));rollouts[tid]=rollout
            insert(db,'threads',{'id':tid,'rollout_path':str(rollout),'created_at':1767225600,'updated_at':1767225603,'source':'cli','model_provider':'openai','cwd':str(project),'title':'Synthetic conversation','sandbox_policy':'"read-only"','approval_mode':'on-request','history_mode':'paginated','project_id':pid,'cli_version':'0.164.0-alpha.11.1','has_user_event':1,'first_user_message':message})
            with managed_connection(home / 'thread_history_1.sqlite') as history:
                insert(history,'thread_items',{'thread_id':tid,'turn_id':turn_id,'item_id':item['id'],'rollout_ordinal':3,'created_at_ms':2000,'item_json':json.dumps(item),'item_type':'userMessage','updated_at_ordinal':3})
                insert(history,'thread_turns',{'thread_id':tid,'turn_id':turn_id,'rollout_ordinal':1,'status':'completed','rollout_byte_offset':len(lines[0]),'rollout_end_ordinal':4,'rollout_end_byte_offset':sum(map(len,lines))})
                insert(history,'thread_history_projection_state',{'thread_id':tid,'next_rollout_byte_offset':sum(map(len,lines)),'next_rollout_ordinal':5})
    legacy_id=str(uuid.uuid5(uuid.NAMESPACE_URL,'codex-migrate-synthetic:'+prefix+':legacy-project'))
    global_state={'local-projects':{legacy_id:{'id':legacy_id,'name':'Synthetic project','rootPaths':[str(project)],'createdAt':1000,'updatedAt':2000}},'app-server-project-id-by-legacy-project-id-by-host':{'local:'+str(home):{legacy_id:project_id}},'thread-project-assignments':{thread_id:{'projectId':legacy_id,'projectKind':'local'}},'thread-project-membership-host-ids':{thread_id:'local'},'project-order':[legacy_id],'synthetic-private-preference':'do not transfer'}
    (home/'.codex-global-state.json').write_text(json.dumps(global_state))
    return {'home':home,'sqlite_home':home,'root':project,'project_id':project_id,'legacy_id':legacy_id,'thread_id':thread_id,'unassigned_thread_id':unassigned,'rollout':rollouts[thread_id],'rollouts':rollouts,'root_id':'root-'+hashlib.sha256(str(project).encode()).hexdigest()[:24],'platform':platform}
