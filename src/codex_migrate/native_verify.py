"""Read native paginated history in a marked disposable, credential-free home.

The allowlisted protocol never creates a model turn. Optional resume can mutate
only the explicitly marked disposable copy and is disabled by default.
"""
from __future__ import annotations
import json
import os
import queue
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path
from .compatibility import connect_readonly
from .discovery import inspect_home,resolve_layout
from .errors import MigrationError

REQUEST_TIMEOUT=30
MAX_RPC_MESSAGE=16*1024*1024
MAX_PAGES=100000
_ALLOWED={'initialize','initialized','thread/list','thread/read','thread/turns/list','thread/items/list','thread/resume'}
MARKER='.codex-migrate-disposable'
MARKER_FORMAT='codex-migrate-disposable/v1'


def _isolated_home(home):
    requested=Path(home).expanduser()
    if requested.is_symlink(): raise MigrationError('Native verification requires a regular disposable directory','unsafe_native_home')
    home=requested.resolve()
    effective_home,effective_sqlite=resolve_layout()
    protected={Path.home().joinpath('.codex').resolve(),effective_home,effective_sqlite}
    if any(home.is_relative_to(p) or p.is_relative_to(home) for p in protected):
        raise MigrationError('Native verification refuses the default or effective production Codex home','unsafe_native_home')
    marker=home/MARKER
    try:
        if not home.is_dir() or marker.is_symlink() or marker.stat().st_size>1024 or json.loads(marker.read_text(encoding='utf-8'))!={'format':MARKER_FORMAT}:
            raise ValueError('invalid marker')
    except (OSError,ValueError) as exc:
        raise MigrationError('Native verification requires an explicit disposable-home marker','unsafe_native_home') from exc
    for name in ('auth.json','config.toml','config.json','session_tokens.json','plugins','automations','skills'):
        if (home/name).exists() or (home/name).is_symlink():
            raise MigrationError('Disposable native verification home must contain no credentials, application configuration, plugins or automations','unsafe_native_home')
    def walk_error(error): raise error
    try:
        for current,directories,files in os.walk(home,followlinks=False,onerror=walk_error):
            for name in directories+files:
                path=Path(current)/name;metadata=path.lstat()
                if stat.S_ISLNK(metadata.st_mode) or getattr(metadata,'st_file_attributes',0)&0x400 or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)) or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink!=1):
                    raise MigrationError('Disposable native copy must contain no symbolic links, shared hardlinks or device files','unsafe_native_home')
    except OSError as exc: raise MigrationError('Cannot validate the disposable native copy safely','unsafe_native_home') from exc
    return home


def discover_cli(cli=None):
    if cli is not None:
        candidate=Path(cli).expanduser().resolve()
        if not candidate.is_file(): raise MigrationError('Supplied Codex CLI does not exist','missing_cli')
        return candidate
    found=shutil.which('codex')
    if found: return Path(found).resolve()
    candidates=[]
    if os.name=='nt':
        local=os.environ.get('LOCALAPPDATA')
        if local:
            candidates.extend([Path(local)/'Programs/Codex/resources/codex.exe',Path(local)/'Programs/Codex/codex.exe'])
    else:
        for applications in (Path('/Applications'),Path.home()/'Applications'):
            candidates.extend([applications/'Codex.app/Contents/Resources/codex',applications/'Codex.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex',applications/'ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'])
    for candidate in candidates:
        if candidate.is_file(): return candidate.resolve()
    raise MigrationError('Codex CLI was not found; supply its executable with --cli','missing_cli')


def _environment(home):
    environment={key:os.environ[key] for key in ('PATH','SYSTEMROOT','SystemRoot','WINDIR','TEMP','TMP','TMPDIR','LANG','LC_ALL') if key in os.environ}
    environment.update({'CODEX_HOME':str(home),'CODEX_SQLITE_HOME':str(home),'HOME':str(home),'USERPROFILE':str(home)})
    return environment


class _Rpc:
    def __init__(self,cli,home):
        try:
            self.process=subprocess.Popen([str(cli),'app-server','--stdio','-c','analytics.enabled=false'],env=_environment(home),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
        except OSError as exc: raise MigrationError('Cannot launch the supplied native Codex CLI','native_rpc_launch') from exc
        self.messages=queue.Queue(maxsize=8);self.counter=0;self.stopped=threading.Event()
        self.reader=threading.Thread(target=self._read,daemon=True);self.reader.start()

    def _put(self,value):
        while not self.stopped.is_set():
            try: self.messages.put(value,timeout=0.1);return
            except queue.Full: continue

    def _read(self):
        try:
            while not self.stopped.is_set():
                line=self.process.stdout.readline(MAX_RPC_MESSAGE+1)
                if not line: self._put(('error','Native RPC closed before the requested response'));return
                if len(line)>MAX_RPC_MESSAGE: self._put(('error','Native RPC response exceeds the message limit'));return
                try: message=json.loads(line)
                except (ValueError,UnicodeError): self._put(('error','Native RPC returned invalid JSON'));return
                if not isinstance(message,dict): self._put(('error','Native RPC returned an invalid message'));return
                self._put(('message',message))
        except (OSError,ValueError): self._put(('error','Native RPC output could not be read'))

    def send(self,method,params=None,request_id=None):
        if method not in _ALLOWED: raise MigrationError('Native verifier refused a non-read protocol method','unsafe_native_method')
        message={'method':method}
        if params is not None: message['params']=params
        if request_id is not None: message['id']=request_id
        try:
            self.process.stdin.write((json.dumps(message,ensure_ascii=False)+'\n').encode('utf-8'));self.process.stdin.flush()
        except (OSError,ValueError) as exc: raise MigrationError('Native RPC input closed unexpectedly','native_rpc_error') from exc

    def call(self,method,params):
        self.counter+=1;request_id=self.counter;self.send(method,params,request_id)
        deadline=time.monotonic()+REQUEST_TIMEOUT
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0: raise MigrationError('Native RPC request timed out: '+method,'native_rpc_timeout')
            try: kind,message=self.messages.get(timeout=remaining)
            except queue.Empty as exc: raise MigrationError('Native RPC request timed out: '+method,'native_rpc_timeout') from exc
            if kind=='error': raise MigrationError(message,'native_rpc_error')
            if 'method' in message and 'id' in message: raise MigrationError('Native RPC requested interactive approval during verification','native_rpc_error')
            if message.get('id')!=request_id: continue
            if 'error' in message: raise MigrationError('Native RPC returned an error for '+method,'native_rpc_error')
            result=message.get('result')
            if not isinstance(result,dict): raise MigrationError('Native RPC response has no valid result','native_rpc_error')
            return result

    def close(self):
        self.stopped.set()
        try:
            if self.process.stdin: self.process.stdin.close()
        except OSError: pass
        try: self.process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try: self.process.wait(timeout=2)
            except subprocess.TimeoutExpired: self.process.kill();self.process.wait(timeout=2)
        if self.process.stdout: self.process.stdout.close()
        self.reader.join(timeout=0.5)


def _pages(rpc,method,params):
    cursor=None;seen=set()
    for _ in range(MAX_PAGES):
        request=dict(params)
        if cursor is not None: request['cursor']=cursor
        page=rpc.call(method,request);data=page.get('data')
        if not isinstance(data,list) or any(not isinstance(row,dict) for row in data): raise MigrationError('Native pagination returned invalid records','native_rpc_error')
        yield from data
        cursor=page.get('nextCursor')
        if cursor is None: return
        if not isinstance(cursor,str) or not cursor or cursor in seen: raise MigrationError('Native pagination returned a repeated or invalid cursor','native_rpc_error')
        seen.add(cursor)
    raise MigrationError('Native pagination exceeded its safety limit','native_rpc_error')


def _identity(value,expected=None):
    if not isinstance(value,str) or not value or (expected is not None and value!=expected): raise MigrationError('Native RPC returned an invalid object identity','native_rpc_error')
    return value


def _check_thread(result,tid,cwd):
    thread=result.get('thread')
    if not isinstance(thread,dict): raise MigrationError('Native RPC omitted the requested thread','native_rpc_error')
    _identity(thread.get('id'),tid)
    native_cwd=thread.get('cwd')
    if not isinstance(native_cwd,str) or native_cwd!=cwd or not Path(native_cwd).is_dir(): raise MigrationError('Native thread working directory is missing or disagrees with the isolated database','native_cwd_mismatch')


def verify_native(home: Path,*,cli: Path|None=None,thread_ids: list[str]|None=None,resume: bool=False) -> dict:
    home=_isolated_home(home);inventory=inspect_home(home,home)
    threads={row['id']:row for row in inventory['threads']}
    # Initialize may inspect any stored rollout, including unselected threads.
    for row in threads.values():
        rollout=Path(row['rollout_path'])
        if not rollout.is_absolute() or not rollout.is_file() or not rollout.resolve().is_relative_to(home) or any(p.is_symlink() for p in (rollout,*rollout.parents)):
            raise MigrationError('Every native rollout must be a regular file inside the disposable home','unsafe_native_home')
    selected=sorted(threads) if thread_ids is None else list(dict.fromkeys(thread_ids))
    if not selected or not set(selected)<=threads.keys(): raise MigrationError('Native verification requires existing explicitly selected threads','invalid_selection')
    known_items=set();expected_items={tid:set() for tid in selected}
    history=connect_readonly(home/'thread_history_1.sqlite')
    try:
        for table in ('thread_items','thread_realtime_items'):
            for row in history.execute('SELECT thread_id,item_id FROM '+table):
                known_items.add(row[1])
                if table=='thread_items' and row[0] in expected_items: expected_items[row[0]].add(row[1])
    finally: history.close()
    report={'passed':False,'home':str(home),'profile_id':inventory['profile_id'],'threads_read':0,'threads_resumed':0,'cwd_matches':0,'logical_turns':0,'logical_items':0,'image_generation_items':0,'duplicate_turns':0,'duplicate_items':0,'unknown_item_ids':0,'rpc_errors':[],'per_thread':[]}
    rpc=_Rpc(discover_cli(cli),home)
    try:
        rpc.call('initialize',{'clientInfo':{'name':'codex_migrate_native_verifier','version':'0.1.0'},'capabilities':{'experimentalApi':True,'explicitGatewayOauth':True}})
        rpc.send('initialized')
        listed=set()
        for archived in (False,True):
            seen=set()
            for row in _pages(rpc,'thread/list',{'limit':100,'archived':archived}):
                tid=_identity(row.get('id'))
                if tid in seen: raise MigrationError('Native thread listing returned duplicate identities','native_rpc_error')
                seen.add(tid);listed.add(tid)
        if not set(selected)<=listed: raise MigrationError('Native thread listing omitted selected chats','native_rpc_error')
        for tid in selected:
            cwd=threads[tid]['cwd'];_check_thread(rpc.call('thread/read',{'threadId':tid,'includeTurns':False}),tid,cwd)
            report['threads_read']+=1;report['cwd_matches']+=1
            if resume:
                _check_thread(rpc.call('thread/resume',{'threadId':tid,'excludeTurns':True}),tid,cwd);report['threads_resumed']+=1
            per={'thread_id':tid,'turns':0,'items':0,'image_items':0};seen_turns=set();thread_items=set()
            for turn in _pages(rpc,'thread/turns/list',{'threadId':tid,'limit':100,'sortDirection':'asc'}):
                turn_id=_identity(turn.get('id'))
                if turn_id in seen_turns: raise MigrationError('Native history returned duplicate turn identities','native_rpc_error')
                seen_turns.add(turn_id);per['turns']+=1;seen_items=set()
                for entry in _pages(rpc,'thread/items/list',{'threadId':tid,'turnId':turn_id,'limit':100,'sortDirection':'asc'}):
                    item=entry.get('item',entry)
                    if not isinstance(item,dict): raise MigrationError('Native history returned an invalid item','native_rpc_error')
                    item_id=_identity(item.get('id'))
                    if item_id in seen_items: raise MigrationError('Native history returned duplicate item identities','native_rpc_error')
                    if item_id not in known_items: raise MigrationError('Native history returned an unknown projected item identity','native_rpc_error')
                    seen_items.add(item_id);thread_items.add(item_id);per['items']+=1
                    if item.get('type')=='imageGeneration':
                        per['image_items']+=1;saved=item.get('savedPath')
                        if saved is not None and (not isinstance(saved,str) or not Path(saved).is_file()): raise MigrationError('Native generated image is unavailable in the isolated copy','native_asset_missing')
            if not expected_items[tid]<=thread_items: raise MigrationError('Native pagination omitted stored projected items','native_rpc_error')
            report['logical_turns']+=per['turns'];report['logical_items']+=per['items'];report['image_generation_items']+=per['image_items'];report['per_thread'].append(per)
        report['passed']=True;return report
    finally: rpc.close()
