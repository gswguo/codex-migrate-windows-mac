# User guide (alpha)

[中文](usage.md) · [Home](../README.en.md)

Requires Python 3.11+. The utility accepts only layouts matching a bundled adapter. Unknown schemas, membership formats, permissions, or byte-boundary semantics stop processing. Windows/macOS synthetic tests do not establish native desktop acceptance for a particular build.

## Download and prepare

Download **Code → Download ZIP** from [gswguo/codex-migrate-windows-mac](https://github.com/gswguo/codex-migrate-windows-mac) and extract it. Git is not required. Keep program files separate from private bundles and staging directories. Double-click the appropriate file under `launchers`, or run it in the system terminal. Source downloads do not bundle Python.

The destination needs a layout initialized by the official Codex app. On a new computer, open the app, sign in and initialize normally, then fully exit. Stop on missing/incompatible databases instead of creating tables manually. Each computer keeps its own credentials; the utility does not transfer or overwrite them.

Open an independent system terminal/launcher before quitting Codex, so execution does not depend on the chat window being closed. Use read-only `inspect` to find IDs first. Exit Codex and relevant writers before export, destination planning, repair, or apply. Keep the destination closed from planning through clear apply/file-verification completion, then reopen for manual acceptance.

## Entry points and locations

Use `./launchers/Codex-Migrate.command` on macOS or `.\launchers\Codex-Migrate.cmd` on Windows. No arguments opens the wizard; append `--help` or command arguments for CLI use. Launchers explain missing Python without installing it. Set `CODEX_MIGRATE_PYTHON` to an existing interpreter when necessary.

Developers may run `PYTHONPATH=src python3 -m codex_migrate --help` on macOS. In PowerShell, set `$env:PYTHONPATH = 'src'`, then run `python -m codex_migrate --help`. Optionally install the command with `python -m pip install .`. Examples below use the installed `codex-migrate` command; replace it with your source launcher if not installed.

Layout commands accept `--home` and `--sqlite-home`. Explicit home wins, otherwise `CODEX_HOME` or the current user's `.codex` is used. The SQLite directory is resolved from an explicit argument, Codex configuration, and environment settings. Inspect the reported locations first; the utility does not need to search the entire disk. WSL and native Windows layouts are not interchangeable merely because their directory names resemble each other.

All example paths and IDs are fictional. Substitute your own and keep private outputs outside the repository.

## Inspect, then select an export

`inspect` is read-only and needs no `--yes`:

```sh
codex-migrate inspect --home "/Users/example/.codex"
```

The result includes project/thread IDs, roots, and compatibility information. Exit source Codex and relevant writers, then explicitly select projects or threads; selection flags may be repeated:

```sh
codex-migrate export --home "C:/Users/example/.codex" --project-id "synthetic-project-id" --output "D:/PrivateTransfer/project.zip" --yes
```

Use `--thread-id` for individual conversations. Required parent/history ancestors are included; unrelated siblings are not automatically exported. The ZIP contains allowlisted rows and selected files, not copies of whole source databases or global state. The output must be a new file.

Common secret files, build outputs, and selected directories are excluded and reported. Project content and chats may still contain sensitive information. Never upload private packages, journals, or databases to public issues, Git, or CI.

## Stage an import on the destination computer

Transfer the ZIP through your own private channel. Exit destination Codex and choose a parent directory for imported project roots plus a new private staging directory:

```sh
codex-migrate plan --bundle "/Volumes/Transfer/project.zip" --home "/Users/example/.codex" --destination "/Users/example/Documents/ImportedProjects" --work "/Users/example/PrivateMigration/import-01" --yes
```

`--destination` is a parent directory: each root is placed below a stable `root-…` subdirectory, and shared roots are stored once. Destination databases must already match the pinned profile; planning does not create schemas for unknown versions.

Here `--yes` confirms writing isolated staging files. The command returns a `plan.json` path; publication requires a separate confirmation below. Review mapped roots, selected and dependent conversations, conflicts, files to publish, permission changes, and unresolved references. Existing same-ID content is not automatically merged by modification time.

Plans are bound to content fingerprints. If a conversation continues or files move/change after preview, create a new plan instead of manually editing a frozen plan to force it through.

## Apply and verify

Keep Codex closed:

```sh
codex-migrate apply "/Users/example/PrivateMigration/import-01/plan.json" --yes
codex-migrate verify "/Users/example/PrivateMigration/import-01/plan.json"
```

Planning makes complete private snapshots of destination `state_5.sqlite` and `thread_history_1.sqlite`, merges selected rows into the copies, and checks integrity. Apply compares current files with their expected before-hashes (CAS), then publishes the prepared whole database files and events/JSON, handling old WAL/SHM sidecars under the same journal. Per-file states and backups support recovery. These files do not share one atomic transaction; do not start Codex mid-publication. `verify` checks planned/published state; it does not establish acceptance inside the native chat window.

After clear completion and verification results, reopen Codex. Confirm project membership is visible, paginate each selected conversation completely, continue the original thread, and actually open its local-file links. Record target existence, matching hashes, and native clicks separately. Missing attachments and excluded external files may remain unresolved; do not describe that outcome as every link repaired.

## Repair links in existing conversations

Declare old-to-new roots explicitly. Quote an entire mapping when it contains spaces. Repeat `--map` for multiple roots:

```sh
codex-migrate repair-links --home "/Users/example/.codex" --map "E:/Research/ProjectA=/Users/example/Documents/ProjectA" --thread-id "synthetic-thread-id" --work "/Users/example/PrivateMigration/repair-01" --yes
```

This command **creates a plan only**. Explicitly selected chats and descendants referring to their history boundaries form the transformation scope. The plan shows any expansion. Required ancestors are byte-preserved context; sharing an ancestor does not pull unrelated siblings into repair. Missing required objects stop the operation. `--thread-id` is mandatory and may be repeated; omitting it does not select everything. Review, then run `apply` and `verify` as above.

Recognized Markdown destinations, attachment/image fields, and known native text fields change together. Code, command examples, prose paths, and link labels remain. Missing, ambiguous, and unsupported references are not guessed. A matching digest establishes content identity; without an old digest the status can establish user mapping/existence only.

## Interruption and later edits

Keep Codex closed from the first destination write through verification. After a crash or reboot, **recover before reopening Codex**:

```sh
codex-migrate recover "/Users/example/PrivateMigration/import-01/plan.json" --yes
```

The utility cannot stop Codex from launching while the utility itself is not running. If Codex was opened, synchronization changed objects, or new content appeared after interruption, close relevant writers, preserve current objects, and inspect differences. Automatic recovery refuses to overwrite changes that cannot be attributed to the recorded plan. Do not restore an old backup over newer conversations. Recovering an already completed plan does not undo later legitimate edits.

Stop if idle, compatibility, occupancy, or fingerprint checks cannot be established. There is no public idle-check bypass. `--yes` is confirmation only.

## Compatibility and capacity limits

Only the bundled `profiles/native-v1.json` profile `native-state5-history1-61-8-alpha` is accepted. It compares exact database objects, SQL definitions, indices, and migration records/checksums. Both source and destination must match; matching filenames alone is insufficient. No real Windows desktop acceptance result is claimed.

| Limit | Alpha maximum |
| --- | --- |
| Structured planning data | 256 MiB per database; exported JSONL total for import, estimated selected history fields for repair |
| JSON record / manifest | 16 MiB each |
| ZIP member / total expanded size | 8 GiB / 64 GiB |
| Archive member count / compression ratio | 100,000 including manifest / 1000 |

Empty project roots and subdirectories are included in journaled directory operations. Export unassigned chats with `--thread-id`; their cwd is retained as an independent root, without inventing a project. Files preserve only the owner execute bit; POSIX output uses private `0600` or `0700` rather than group/other execute bits or the entire original permission set.

Project files and events stream, but structured rows retain a planning-memory cap. A 64 GiB archive limit does not imply that any equally sized conversation dataset can be imported. Allow room for extraction, complete snapshots of both existing databases, publication staging, and recovery backups. No precise free-space estimator is provided; if space fails after publication starts, follow recovery instructions.

Source `.git`, common build directories, and secret files are excluded by default. This is a project file snapshot; **Git history is not migrated**. Git worktrees, symlinks, and unsupported external layouts are rejected. Concurrent synchronization, common-baseline round trips, and divergent-history merging are not implemented.

## Native verification on a disposable synthetic copy

`verify-native` uses the local Codex app-server to check history reading/pagination and optionally resume a thread on the copy. It never sends a model conversation request. The app-server may update the copy, so `--yes` is mandatory. Only disposable synthetic homes without credentials are accepted; real Codex homes and credential-bearing directories are rejected. Never pass your actual `.codex` directory.

```sh
codex-migrate verify-native "/Users/example/PrivateMigration/synthetic-check/home" --cli "/path/to/codex" --thread "synthetic-thread-id" --resume --yes
```

Repeat `--thread` for multiple synthetic threads. Omit `--cli` to use local CLI discovery. `--resume` is an optional copy-only capability check and does not create a model task. This is a developer acceptance entry point, not a user import step pointed directly at `plan/native` or a real home. A complete synthetic copy must contain `.codex-migrate-disposable` with exactly `{"format":"codex-migrate-disposable/v1"}`. Every rollout must reside inside the copy; credentials, configuration, plugins, automations, symlinks, and shared hardlinks are refused. Never add the marker to real data to bypass the safeguard. Construct a complete disposable fixture home first. This command does not replace project-membership, actual continuation, and native link-click acceptance after migration.

## Output and exit codes

CLI success goes to stdout as `{"ok":true,"result":...}`. Diagnostics go to stderr as `{"ok":false,"error":{"code":"...","message":"..."}}`. Help/version use plain text, and the wizard includes interactive prompts.

| Exit code | Meaning |
| --- | --- |
| 0 | Command returned successfully; assess its result and complete relevant acceptance checks |
| 1 | Migration, data, or I/O failure |
| 2 | Invalid arguments or missing write confirmation `--yes` |
| 130 | User cancellation |

Review unresolved references, warnings, and native acceptance status in the result. Filesystem existence is not equivalent to a successful native-window click.

See the [validation record](validation.md) for native synthetic acceptance evidence and outstanding checks.
