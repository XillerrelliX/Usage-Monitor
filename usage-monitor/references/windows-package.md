# Usage Monitor for Windows x64

Extract the complete `Usage-Monitor` folder from the ZIP into a local folder. Double-click `Start-Usage-Monitor.cmd`. Keep the folder together. The package includes Python, timezone data and the tested offline scanner; Python, Rust and C compiler installation is unnecessary. The same complete folder can be distributed as the `usage-monitor` Codex skill, including its public runtime.

Usage Monitor reads local Codex session files. On first use it selects `CODEX_HOME`, when set, or this Windows profile's `.codex` folder. A local home selected with `--codex-home` is saved for later refreshes. It does not sign in, call account APIs, upload data or contact an update service. The scanner and existing independent accounting checks run before reports are published. Filtering the generated dashboard happens in the browser on this computer.

Settings, the owned scanner database, dashboard and ledger are stored outside the product folder. On Windows the default is `%LOCALAPPDATA%\usage-monitor`, using this profile's `AppData\Local` folder if that environment variable is unset. The dashboard is in its `dashboard` subfolder. An explicit `--config` selects another local settings folder and, on first use, its sibling `dashboard` folder. These files contain personal local usage and are not part of an exported product ZIP. Double-click the same launcher to refresh and open the dashboard. A warning can produce exit code 2 while still generating a dashboard; exit code 1 means the operation failed.

For a Codex report with explicitly confirmed scope/period, use one batched invocation:

```powershell
.\Start-Usage-Monitor.cmd --codex-report --scope all --period today --today 2026-10-29 --timezone Europe/Berlin
```

The date/timezone above are examples; pass the current client values. `--scope chat` resolves the executing chat locally. A project requires an exact `--project-id`; custom `selection` accepts repeated project/chat IDs. Presets `today`, `last3`, `last7` include today; custom dates use `--from-date`/`--to-date` instead. The saved app timezone must match the explicit assertion. The batch performs one existing verified refresh, exact selected projection and optional before/after meter in local code, then emits one compact result. It does not perform model calls or retry a failed scan. See [batch command contract](codex-report.md).

Other standalone arguments can be passed from a terminal:

```powershell
.\Start-Usage-Monitor.cmd --codex-home "D:\Local Codex" --timezone Europe/Berlin
.\Start-Usage-Monitor.cmd --config "D:\Monitor data\settings.json" --out "D:\Monitor dashboard" --no-open
.\Start-Usage-Monitor.cmd --from-date 2026-10-26 --to-date 2026-10-28 --chat-id "<chat-id>" --no-open
.\Start-Usage-Monitor.cmd --from-date 2026-10-22 --to-date 2026-10-28 --project-id "<project-a-id>" --project-id "<project-b-id>"
.\Start-Usage-Monitor.cmd --export "D:\Exports\Usage-Monitor-Windows-x64.zip"
```

The standalone launcher always uses the shipped offline scanner. It accepts `--refresh`, `--cache-file`, `--names-database` and `--project-state` for local settings. The separate source CLI retains its explicit desktop-cache read mode.

The Windows launcher runs the existing verified local scanner and opens a live dashboard served only on `127.0.0.1`. Run the launcher again to collect new usage. A failed refresh keeps the previous valid report. Export CSV is below Chats. There are no model calls or external network requests; browser/helper communication uses local loopback HTTP. The hidden helper exits after an hour without activity. Opening the launcher restarts it.

`dashboardUrl` is the live private URL; `dashboard` and `fileDashboardUrl` identify the saved HTML snapshot. Snapshots stay readable; run the Windows launcher to collect new usage and open the live view. `--serve-only --no-open` starts a helper for an existing verified report without scanning sources, for restoring a live view. Query-meter and export commands do not start a helper. If helper startup fails after successful report generation, the launcher returns the valid saved file with a warning rather than discarding the report.

The helper accepts only bounded, authenticated local requests for this one saved setup. Its private capability URL and lifecycle state are kept outside the public package. It does not expose the filesystem, raw logs, credentials or arbitrary commands. Concurrent helper refresh requests share one active refresh; changed or invalid source settings require reopening the launcher. A browser page never selects a different source home, settings file or output directory.

`--from-date` and `--to-date` select inclusive app-zone calendar dates. Repeat `--project-id` and `--chat-id` to select several exact IDs. Project selections combine, chat selections combine, and the two dimensions intersect. Unknown IDs and invalid dates fail instead of opening a broader view. The returned `dashboard` file embeds this initial selection and `dashboardUrl` also encodes it as `#from=...&to=...&project=...&chat=...`, repeating project/chat keys as needed. Browser changes update the fragment so reloading keeps the selection. The saved dashboard's own refresh launcher retains its initial selection in private local state; this state is excluded from exported packages. The public launcher without selection arguments defaults to today and all usage.

`Start-Usage-Monitor.cmd --query-meter begin --watch --thread-id <current-chat-id>` starts an optional before/after measurement without refreshing the shared history cache. `--query-meter sample --query-id <id>` reads its provisional progress. The bounded hidden observer records the final result only after the selected turns have completed. Its JSON, script sidecar and `query-usage.html` stay beside the personal dashboard, outside the public package. The normal dashboard shows the latest measurement independently of its date/project/chat filters. See [query measurement](query-meter.md) for coverage, registration and failure behavior. Codex must still obtain the two required report answers before starting this measurement for a usage report.

The launcher checks the SHA256 of every declared public file before starting the bundled interpreter. A second check binds the runtime to the unchanged native source and rejects missing, altered or additional runtime files. If a payload file is damaged or missing, extract the original ZIP into a clean folder. The launcher never downloads dependencies or falls back to installing a compiler. The manifests detect accidental corruption; they are not digital signatures, so obtain the ZIP from a trusted source.

`--export` exports only the declared public code, bundled runtime and license notices. It excludes settings, reports, history, caches, personal databases and interpreter hints, even after this copy has generated a dashboard. The release contains the native source, pinned upstream originals, test fixtures and third-party notices under `licenses`.

For a new computer, transfer a clean ZIP and extract it there. The executable and runtime paths are resolved from that extracted folder, while settings belong to the current Windows profile. If you transfer personal settings separately, select that computer's local home and output folders explicitly.
