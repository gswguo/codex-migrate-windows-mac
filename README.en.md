# Codex Migrate Windows–Mac

[中文](README.md) · [User guide](docs/usage.en.md) · [MIT license](LICENSE)

Selectively export, stage, and migrate local Codex projects and conversations, with repair of historical local-file links. Includes a Python CLI, a bilingual wizard, and source launchers for Windows and macOS.

**Status: `0.1.0a1` alpha.** The executable core and synthetic tests are implemented. Native data must exactly match a bundled compatibility profile. Automated tests, filesystem/hash verification, native history pagination, and actual file-link clicks are separate acceptance checks. The complete Windows desktop workflow still requires real-device acceptance. This is not a stable release or a promise of compatibility with every Codex version.

On 2026-10-28, macOS native CLI `0.164.0-alpha.11.1` passed synthetic fresh-home, selective project-import, and unassigned-thread import read/pagination/resume checks. See the [validation record](docs/validation.md). This does not establish Windows desktop or actual file-click acceptance.

## Download and run

On this repository's page, choose **Code → Download ZIP**, and extract into a normal directory. No Git knowledge is needed. Keep the program outside `.codex` and outside private migration packages. A source ZIP and a ZIP containing your personal migration data are different files.

On a new destination computer, first open the official Codex app, sign in and initialize your own local layout, then exit. The utility needs existing destination databases matching its profile. Source and destination keep their own credentials; the utility does not transfer or replace them.

Open the launcher in the system terminal before quitting Codex for export, planning, or apply. Only `inspect` supports read-only use while the app remains open.

Requires **Python 3.11+**. The core has no third-party runtime dependencies. From the downloaded repository:

macOS:

```sh
./launchers/Codex-Migrate.command --help
./launchers/Codex-Migrate.command
```

Windows PowerShell:

```powershell
.\launchers\Codex-Migrate.cmd --help
.\launchers\Codex-Migrate.cmd
```

Run without arguments, or double-click the launcher, for the bilingual wizard. Launchers do not install Python. Set `CODEX_MIGRATE_PYTHON` to a full interpreter path when needed. These source launchers are not standalone installers that bundle Python.

## Workflow

1. `inspect`: read inventory, compatibility, and project/thread IDs.
2. `export`: explicitly select projects/threads and create a private ZIP, including necessary ancestors.
3. `plan`: build isolated staged files and a reviewable `plan.json` on the destination computer.
4. `apply`: publish a reviewed frozen plan with recovery journals and backups.
5. `verify`: check published fingerprints, then complete native pagination, continuation, and file-click acceptance.

For existing migrated conversations, `repair-links` creates a separate repair plan; review it before running `apply`. Every file-writing command requires `--yes`; the wizard asks you to type `YES` for each write. Confirmation never bypasses idle, compatibility, conflict, or fingerprint checks.

**Offline window:** close Codex and relevant writers before export and destination planning/writing. Keep them closed from the first target write through verification. After interruption or reboot, run `recover` and obtain a clear recovery result before reopening Codex. The tool cannot prevent the app from starting while it is not running. Unexpected later edits stop automatic recovery rather than being overwritten.

## Scope and privacy

- Windows/macOS paths use source-platform semantics, explicit mappings, longest segment-prefix matching, and destination confinement. Recognized Markdown destinations change; prose and code remain. Missing, ambiguous, and unsupported references are reported.
- Native events and item projections are transformed together. Turn, projection, and `history_base` boundaries are rebuilt from final UTF-8 bytes. Repair transforms explicit selections and affected descendants; ancestors remain byte-preserved context and unrelated siblings stay unchanged.
- Empty roots/subdirectories are preserved transactionally. Unassigned chats carry an independent cwd root without inventing a project. Files preserve owner executability only; POSIX output uses private `0600`/`0700`, not the full old permission set.
- Fingerprint and conflict checks protect existing destination data. Same-ID histories are not guessed or automatically merged. Concurrent device synchronization and conflict-aware round-trip handoff are outside this alpha.
- Credentials, keychains, full global settings, plugins, and automations are not migrated. Project files and chats can still contain secrets; a bundle is not guaranteed to be sanitized.
- Bundles, chats, projects, plans, journals, and backups belong in private directories. Public code and CI use synthetic fixtures only. The utility does not require uploading conversations.

`verify-native` runs app-server checks against a disposable synthetic copy without credentials. It may write that copy and requires separate confirmation. Real homes and credential-bearing directories are rejected. This still does not replace native-window click acceptance.

This alpha pins `native-state5-history1-61-8-alpha`, comparing exact schema objects and migration records. Matching database filenames alone is insufficient. Import creates complete staged copies of the existing destination databases, merges allowlisted rows there, then checks before-hashes and publishes files while Codex is closed. This is offline snapshot publication, with no single atomic transaction across all files.

Limits: 256 MiB of structured planning data per database; 16 MiB per JSON record/manifest; 8 GiB per ZIP member; 64 GiB total expanded data; 100,000 archive members; maximum compression ratio 1000. Large projects may hit the structured-data limit first. Allow additional disk space for extraction, complete database snapshots, and recovery backups; there is no precise free-space estimator.

See the [user guide](docs/usage.en.md), [design](docs/superpowers/specs/2026-10-27-cross-platform-migration-design.md), and [historical-link design](docs/history-link-repair.md). Development and reporting guidance is in [CONTRIBUTING](CONTRIBUTING.md) and [SECURITY](SECURITY.md).

## Development checks

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The GitHub Actions matrix is configured for Windows/macOS and Python 3.11/3.12 synthetic tests. CI results and native desktop acceptance must be recorded separately; a workflow configuration is not proof that every platform has passed.
