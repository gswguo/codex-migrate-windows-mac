# Security and private data

This alpha processes private local projects and conversation histories. Migration bundles, staging directories, plans, journals, backups, and receipts may contain sensitive content and local paths. Keep them outside this public repository and share them only through your own private transfer channel. Secret-file exclusions cannot guarantee that project code or conversation text contains no secrets.

Do not attach real Codex databases, chat exports, credentials, screenshots of private conversations, or migration packages to public issues, pull requests, or CI logs. Report ordinary bugs using a minimal synthetic fixture, operating system, Python version, tool version, and redacted error code.

For a suspected vulnerability, use the repository's private vulnerability reporting feature if available. If it is unavailable, open an issue requesting a private contact channel without including exploit details or private data. There is no promised response time or support SLA for this alpha.

Keep Codex and relevant writers closed from the first target write through verification. After a crash or reboot, run recovery before opening Codex. The tool cannot block the desktop application from starting while it is not running. If objects have changed unexpectedly, recovery must stop rather than overwrite those changes.

Unknown native schemas and unsafe archive members are rejected. Do not bypass compatibility, idle, or fingerprint checks to force an import. The public CLI exposes no idle-check bypass.
