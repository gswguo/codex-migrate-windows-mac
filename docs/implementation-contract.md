# Internal implementation contract (v0.1 alpha)

All identifiers in committed test data are synthetic. JSON objects below describe a format, not real migration data.

## Core types

`MigrationError(message, code="migration_error")` is the shared user-facing exception.

`Layout` is represented as `{home: absolute str, sqlite_home: absolute str}`. Read-only inspection may run while the application is open; export/apply/repair require a frozen source or idle target. Source/target CLI paths are discovered or explicitly passed, not hardcoded to a user account.

Inventory dictionaries: `{home, sqlite_home, profile_id, schema, projects:[{id,legacy_id,name,roots:[str],main_thread_ids:[str],created_at_ms,updated_at_ms}], threads:[{id,cwd,rollout_path,archived,project_id,...}], warnings:[str]}`. Full app global state is never exported.

## Bundle

`export_bundle(home: Path, output: Path, *, project_ids: list[str]|None=None, thread_ids: list[str]|None=None, sqlite_home: Path|None=None, check_idle: bool=True) -> dict` creates a NEW ZIP, with before/after source consistency checks. `check_idle=False` is an internal testing hook, never exposed in CLI.

ZIP entries: `manifest.json`, `data/state/<table>.jsonl`, `data/history/<table>.jsonl`, `events/<thread-id>.jsonl`, `projects/<root-id>/<relative-path>`, `assets/<asset-id>/<basename>`.

Manifest format: `{format:"codex-migrate/v1", bundle_id: UUID, profile_id: str, source:{platform:"windows"|"macos", home:str, sqlite_home:str}, schema:dict, projects:[{id,legacy_id,name,root_ids:[str],main_thread_ids:[str],created_at_ms:int,updated_at_ms:int}], roots:[{id,source_path,archive_path}], threads:[{id,archive_path}], assets:[{source_path,archive_path}], directories:[relative_path], files:{relative_path:{sha256,size,executable?:bool}}, warnings:[str]}`. `files` includes every non-manifest member; no undeclared member allowed. BLOBs encoded as `{"$bytes":base64}`. Table allowlists come from `compatibility.py`, not the package.

`extract_bundle(bundle: Path, destination: Path) -> dict` validates limits, members, hashes and manifest and writes into a NEW directory. Failure removes only its own incomplete directory.

`inspect_home(home: Path, sqlite_home: Path|None=None) -> dict`; `validate_profile(state_db: Path, history_db: Path) -> dict` returns `{profile_id,schema}` or raises. Known schema metadata includes table definitions, indices/constraints and migrations; data rows are not part of profile.

## Transformation

`PathMapper(mappings: list[tuple[str,str]], *, expected_hashes: dict[str,str]|None=None, validation_mappings: list[tuple[str,str]]|None=None)` maps old absolute paths using source flavor and longest segment prefix. `map_path(value:str, required:bool=False) -> str|None`; `resolve_link(value:str) -> dict` returns `{status,old,new,path,...}`; target existence/hash check applies to links, not all structural runtime paths.

`repair_markdown(text:str, mapper:PathMapper) -> tuple[str,list[dict]]` preserves prose/code/link labels and changes recognized link destinations only. Missing/ambiguous/unsupported references produce records with no patch. Native structured item paths and known user/agent textual fields handled by `transform_item(item:dict, mapper:PathMapper) -> tuple[dict,list[dict]]`.

`rewrite_histories(rollouts:dict[str,Path], destination:Path, mapper:PathMapper, history_rows:dict[str,list[dict]], *, final_rollout_paths:dict[str,str]|None=None, transform_thread_ids:set[str]|None=None) -> dict` creates NEW event files in destination, applies structural mappings and item/link patches, rebuilds offsets, returns `{rollouts:{id:str}, history_rows:dict, changes:[dict], counts:dict}`. Files preserve original event ordinals and IDs; parse all history_base references. Explicit `transform_thread_ids` keeps other dependency-context rollouts byte-for-byte intact and omits their projection rows from transformed output. Native state threads are rewritten by orchestration using mapper. History tables unchanged except approved item fields and byte offsets. No package DDL executed.

## Publishing

Publisher operates on already-validated complete staged files. `prepare_transaction(changes:list[dict], journal_dir:Path, *, directories:list[str]|None=None, guard:Callable[[],None]|None=None) -> Path`; changes each `{target:str,source:str,before_sha256:str|None}`. Existing targets must be regular files; `None` means must not exist. Source content is copied/fsynced into private journal storage. Preparation verifies target fingerprints and takes durable backups before mutation. Reject any source/target overlap or symlink escape.

`apply_transaction(journal:Path, *, guard:Callable|None=None, fault:Callable[[str],None]|None=None) -> dict` publishes staged files with persistent per-file states, verifies targets at each boundary, and records completion. All involved apps must stay closed; arbitrary multi-file changes cannot be atomic. On failure leave the journal, do not overwrite unexpected user changes.

`recover_transaction(journal:Path, *, guard:Callable|None=None) -> dict` rolls back an incomplete transaction only if EVERY current target matches a recorded before/after state; completed transactions return their receipt without overwriting later edits. No unconditional old-backup restore. `verify_transaction(journal:Path) -> dict` checks completed after hashes and reports drift without mutation.

`ensure_idle(home:Path, sqlite_home:Path|None=None) -> None` fails closed on unknown occupancy or live relevant app; OS checks never kill processes. Core tests inject guard functions; production uses this function.

## Ownership

Agent A: `paths.py`, `links.py`, `history.py` and their unit tests.

Agent B: `platforms.py`, `transaction.py`, transaction unit tests.

Agent C: `compatibility.py`, `discovery.py`, `bundle.py`, `exporter.py`, profile JSON, `tests/fixtures.py`, export/bundle tests.

Coordinator: shared errors/common, CLI, native orchestration, integration, docs, CI, commits, publication. Do not change another owner's files without coordinating. Tests run via `PYTHONPATH=src python -m unittest discover -s tests -v`; Python 3.11+ required.

## Current alpha orchestration

Pinned structural profile: `native-state5-history1-61-8-alpha`, exact schema objects and migration records/checksums. Filenames alone do not prove compatibility. Destination databases must already exist and match.

`plan_import(bundle, home, destination, work, *, sqlite_home=None, check_idle=True) -> Path`; `destination` is a parent for stable root-id directories. `plan_link_repair(home, mappings, work, *, thread_ids=None, sqlite_home=None, check_idle=True) -> Path` requires explicit nonempty thread IDs; it transforms explicit selections and affected descendants; required ancestors are byte-preserved context, without transforming unrelated siblings. `apply_plan`, `recover_plan`, and `verify_plan` accept the saved plan path. Public CLI commands never expose `check_idle=False`.

Planning snapshots the complete existing destination databases and merges allowlisted rows only inside those copies. Apply publishes prepared whole files using expected before-hashes (compare-and-swap), per-file backups and a durable transaction journal; it does not execute imported DDL or perform a live attached-database migration. There is no all-files atomic commit. Recovery refuses unexpected current content; keep Codex closed until verification or a clear recovery result.

`validation_mappings` maps final roots to extracted stage roots only for link existence/hash validation; emitted paths remain final. Hash lookup accepts normalized source or final paths.

Limits: 256 MiB structured planning data per database; 16 MiB per JSON record and manifest; 8 GiB per archive member; 64 GiB total expanded ZIP data; 100,000 members including the manifest; compression ratio at most 1000. Structured row planning is bounded by the cap rather than unlimited streaming. Full database snapshots and recovery copies need additional disk space; no precise disk-budget estimator exists.

`verify_native(home, *, cli=None, thread_ids=None, resume=False)` is restricted to explicitly marked, credential-free disposable synthetic homes; it may mutate only that copy and creates no model turn. This developer check does not establish native link clicks or Windows desktop acceptance.

Review hardening: manifests preserve empty root/subdirectories and project owner-executable flags. POSIX files remain private 0600/0700; full old permission sets are not copied. Unassigned threads retain cwd as independent roots without synthetic project records. Native synthetic evidence is recorded separately in [validation.md](validation.md).
