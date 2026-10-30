# Usage Monitor for macOS

The Mac release contains separate native packages for Apple Silicon (arm64) and
Intel (x86_64). Each includes its own Python, timezone data, offline scanner and
license notices. It needs no system Python, Rust, compiler or administrator
access. Keep the complete extracted folder together.

Open `Start-Usage-Monitor.command`, or run it from Terminal:

```bash
/bin/bash -p "/path/to/Usage-Monitor/Start-Usage-Monitor.command"
```

The same command accepts the Windows application's report, date, project, chat,
query-meter and export options. For a Codex report, obtain both required scope
and period answers, then run one operation:

```bash
/bin/bash -p "<skill>/Start-Usage-Monitor.command" --codex-report --scope all --period today --today YYYY-MM-DD --timezone Europe/Berlin
```

Pass the current client date and agreed saved timezone. Keep the exact returned
dashboard URL and filters. Ordinary Codex answers remain a single dashboard
link. A usable warning result is exit 2; failure is exit 1. Preserve the native
exit code when wrapping the command.

When installed from the repository's `usage-monitor` folder, the Mac launcher
downloads only its architecture's fixed release ZIP over HTTPS, checks its
pinned SHA256, and keeps the complete verified application under
`~/Library/Application Support/usage-monitor/packages/`. Subsequent launches
reuse that package without network access. A release ZIP has a direct offline
launcher instead of this repository bootstrap.

Settings, the owned database and dashboard normally live under
`~/Library/Application Support/usage-monitor/`, independently of the current
project. `CODEX_HOME` selects the local Codex source on first use; an explicitly
chosen `--config` remains a separate setup. No usage data, account credentials,
logs or dashboard files are downloaded or uploaded. Filters and exports run
locally. Run the launcher again to scan newly recorded usage.

Both package architectures are validated on native GitHub-hosted Mac runners
using synthetic data. The release evidence records native Rust assertions,
Python/JavaScript suites, packaged launch/report/query checks and browser tests.
These checks do not establish compatibility with every older macOS version or
replace a manual Finder first-launch test. The packages are not Apple-notarized.
Do not disable Gatekeeper or remove quarantine attributes globally.

Public exports contain only declared code, runtime and licenses. Settings,
history, caches, reports and interpreter hints remain excluded. The launcher
checks the public payload before executing its interpreter and verifies the
native source/runtime identity before scanning. Keep these integrity checks;
checksums detect corruption and are not publisher signatures.
