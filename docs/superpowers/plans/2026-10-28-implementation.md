# Codex Migrate Windows–Mac Implementation Plan

> **For agentic workers:** Use subagent-driven-development for bounded modules and a fresh whole-repository review. The user has approved the design and explicitly requested implementation and GitHub publication; no additional design approval is required.

**Goal:** Deliver a runnable, privacy-separated migration CLI/wizard, native historical-link repair, synthetic tests, bilingual documentation, and a public GitHub repository.

**Architecture:** A version-bound source adapter exports clean allowlisted records and project files to a verified ZIP bundle. Planning builds an immutable isolated target, then an offline journaled publisher applies changes after compatibility, hash, conflict and idle checks. Link transformation precedes native byte-index reconstruction.

**Tech Stack:** Python 3.11+, standard-library core, unittest, SQLite, ZIP64, OS-specific idle/publish helpers; GitHub Actions on Windows/macOS.

**Spec:** `../specs/2026-10-27-cross-platform-migration-design.md`, supplemented by `../../history-link-repair.md`.

## Global Constraints

- Never write the developer's real Codex home while implementing/testing; native validation uses disposable isolated homes.
- No private chats, IDs, paths, credentials, original migration packages or database contents in Git/CI; schemas are structural metadata and fixtures synthetic.
- Unknown profiles, unsafe archives, schema mismatch, missing dependency objects and same-ID content conflicts stop before target mutation.
- First version is an alpha. Windows native desktop validation remains separately documented until real-device evidence exists. No automatic two-device merge.
- Public destination authorized by user: `gswguo/codex-migrate-windows-mac`.
- No source credential/config/plugin/automation transfer. Restore must not overwrite later user edits.

## Review Focus

1. Archive path traversal, symlink/device entries, case/Unicode collisions and size limits: bundle tests.
2. Markdown links versus code/prose and inherited byte offsets: transform tests.
3. Crashes between file/DB/global publication and changed targets: transaction tests.
4. Native projected history and sidebar membership semantic compatibility: integration/native read tests.
5. Existing IDs, shared roots, dependency closures, attachment privacy: export/planning tests.

## Shared interfaces

`docs/implementation-contract.md` is the integration contract. All modules raise `MigrationError` from `errors.py`; paths at public Python boundaries are `pathlib.Path`, JSON state is ordinary dictionaries. Agents own disjoint files and run RED then GREEN tests before reporting. The coordinator integrates and commits after independent review.

### Task 1: Structural profile, discovery, secure package and selective export

**Files:** `compatibility.py`, `discovery.py`, `bundle.py`, `exporter.py`, `profiles/*.json`, `tests/test_export_bundle.py`, `tests/fixtures.py`.

- [ ] Write failing tests for unknown schema, clean selective rows, shared roots/dependencies, file exclusion, hostile archive entries and changed export input.
- [ ] Implement exact structural profile inspection, readonly inventory and deterministic clean ZIP manifest.
- [ ] Run targeted tests; report covered source layout and unsupported cases.

### Task 2: Path and historical-link transformation, native offsets

**Files:** `paths.py`, `links.py`, `history.py`, `tests/test_paths_links.py`, `tests/test_history.py`.

- [ ] Write failing tests for Windows/POSIX paths, matched segment roots, nested Markdown destinations, code fences, reference links and ambiguous/missing targets.
- [ ] Implement explicit root mapping and lossless allowlisted link-target patches.
- [ ] Write failing multi-generation history tests including nonzero ordinal, inclusive end and exclusive inherited bounds.
- [ ] Implement event/projection transformation, dependency ordering and final byte-offset rebuild; run tests.

### Task 3: Cross-platform offline publishing, recovery and backups

**Files:** `platforms.py`, `transaction.py`, `tests/test_transaction.py`.

- [ ] Write failing tests for no-overwrite publishing, crash phases, partial replace, later user edits and idempotent completion.
- [ ] Implement process/file occupancy checks, target fingerprint validation, backup manifest and persistent transaction journal.
- [ ] Implement recover-to-before behavior only when current objects match recorded before/after states; preserve unexpected states and stop.
- [ ] Run fixture tests without weakening production idle checks.

### Task 4: Planning/staging, native merge and link-repair orchestration

**Files:** `common.py`, `errors.py`, `staging.py`, `operations.py`, `native_verify.py`, `tests/test_workflows.py`.

- [ ] Write synthetic end-to-end failing tests for export→import, original-data preservation, link repair and dependent offset updates.
- [ ] Build stage by merging allowlisted rows into consistent target DB snapshots, never executing package DDL.
- [ ] Bind plans to source/stage/target hashes; create final-path rollouts without changing original source data.
- [ ] Run synthetic full workflows and native RPC on disposable compatible snapshots, without model turns.

### Task 5: Runnable CLI/wizard, documentation, CI and release packaging

**Files:** `cli.py`, `__main__.py`, `wizard.py`, `launchers/*`, bilingual README/user guides, `.github/workflows/*`, LICENSE, SECURITY, CONTRIBUTING.

- [ ] Write CLI tests for help, exit codes, dry planning, explicit apply and diagnostic errors.
- [ ] Implement inspect/export/plan/apply/repair-links/recover/verify and a guided menu.
- [ ] Build source distribution and wheel; run installed entry point in a clean temporary environment.
- [ ] Complete Chinese/English user docs with actual commands and current support boundaries.

### Task 6: Independent review and GitHub publication

- [ ] Fresh review of all code, security boundaries, native mutation/recovery and claims.
- [ ] Fix findings with regression tests and rerun required checks.
- [ ] Inspect tracked files and history for private data; commit clean repository.
- [ ] Authenticate with user's GitHub account, create authorized public repository and push tested code; confirm remote revision.
- [ ] Check CI results if available. Never label unexecuted Windows desktop tests as passed.
