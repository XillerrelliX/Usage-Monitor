# Usage Monitor

A local Codex token-usage dashboard and optional Codex skill. The prebuilt
Windows x64 package includes its own Python runtime, timezone data and tested
native scanner. It needs no Codex Usage Desktop installation, system Python,
Rust, compiler, API key, account connection or runtime downloads.

## Windows

Extract the complete prebuilt package and double-click **Start-Usage-Monitor.cmd**.
The first run scans local Codex files and opens the dashboard in your browser.
Run the launcher again to scan new usage and update the counts. It starts a
local helper bound to `127.0.0.1`; it stops after an hour without activity.
Restart the launcher if the helper has stopped. The launcher and filtering
make no model calls and send no data to an external service.
**Export CSV** is below the Chats list.

Data and settings are stored in the current user's local application-data
directory under `usage-monitor`, separate from this distributable folder.
The live dashboard uses a private loopback URL. Saved HTML snapshots remain
readable without the helper. Run the Windows launcher to collect new data.
Use `--config PATH`, `--codex-home PATH`, `--timezone ZONE` or `--out PATH` when
you need a different local setup. `--no-open` suppresses opening a browser.

## Codex skill and GitHub

The **complete Windows package folder** is also the GitHub-ready skill source:
it contains `SKILL.md`, source, licenses and the verified prebuilt runtime.
Keep the runtime files, manifests and `.gitattributes` when committing it.
The latter preserves exact file bytes on Windows Git checkouts.

Install that complete folder as `usage-monitor` in your Codex skills directory,
or ask Codex to install it from your eventual GitHub repository. Invoke
`$usage-monitor`. The skill opens the same local application; invoking Codex
still uses conversation tokens. Standalone use does not need Codex running.

For each report requested through Codex, the skill asks both questions together:
scope presets are **This chat**, **This project**, **All usage**; time presets
are **Today (so far)**, **Last 3 days**, **Last 7 days**. Both fields also accept
your own free-text answer. It runs only after both answers are explicit and
resolved, retaining valid confirmed choices during the same ongoing request.
The current parser supports calendar days in the saved app timezone, with
today included in the 3- and 7-day periods; hours require an explicitly accepted
specific day-based alternative. This project uses the exact active project
assignment; unclear or projectless assignments need clarification. All usage
covers the selected local data source, not account quota.
The dashboard opens with the confirmed dates, projects and chats already selected.
Select several projects or chats to compare them in separate colored series.
Selections within each field combine; project and chat fields intersect.
The overall total counts each usage slice once. Single-day comparisons also
have a chart. Reload preserves the applied selection.
An ordinary completed Codex report returns only
`[Lokales Dashboard öffnen](<dashboardUrl>)`, using that execution's exact URL.
Diagnostics and request measurements remain in the local result. An explicit
request for explanation, diagnostics or query measurements changes this format.
Local refresh uses no model calls; the surrounding Codex conversation uses tokens.

After the two scope/period answers, Codex uses one command:
`Start-Usage-Monitor.cmd --codex-report --scope all --period today --today YYYY-MM-DD --timezone IANA_ZONE`
(substitute the confirmed selection and current client date/timezone). It runs
one verified local refresh, returns exact filtered totals and dashboard URL,
and includes the optional request meter. No separate environment/API lookups,
manual projections or model-driven meter/poll loops are needed. See the
[batch contract](references/codex-report.md).

The request meter subtracts the captured execution turn's verified starting
counter from its ending counter. The batch runs it automatically. Individual
`--query-meter` commands remain available for explicit meter-only work and
diagnosis. The background observer waits
for the captured turn to finish, including its final response, then updates
**Query usage** in the dashboard and the separate local `query-usage.html` page.
A sample taken while Codex is answering remains provisional. Earlier
clarification turns are not automatically included. Delegated work remains
partial in this version: registering agent turns supplies a captured-turn
subtotal, without proving their exact causal link to this request. Missing
telemetry or changed source history also prevents a complete total. See
[query measurement](references/query-meter.md).

The separate source-only export is for development and other-platform source
builds. It does not include the Windows runtime. Use the prebuilt Windows ZIP
for compiler-free installation. No GitHub repository has to be configured for
the Windows package to run offline.

## Accounting and privacy

Usage Monitor reuses the pinned MIT-licensed parser from Codex Usage Desktop
4.2.0, commit `6bd0eed7b108497aa50051580387345340e3167f`. Its counting code is
preserved, with local-only and privacy persistence patches. It does not launch
the desktop app or request account limits, pricing, updates or telemetry.
Original attribution and reconciliation checks remain mandatory.

Cached input is included in input; reasoning is included in output. Recorded
totals are preserved rather than repaired. Chat and agent ownership, project
and model maps, archived/compressed deduplication and all five token counters
are checked. Failed refreshes preserve the previous valid cache and report.

The scanner reads session files locally. Conversation text and prompt-derived
titles are not retained in reports; chat labels use actual local sidebar names.
Authentication files and API keys are not needed. Stored or synced history
does not prove device-only execution, the current account, or which human used
a shared account. These counts are not account quota or credit balances.

## Development

See [accounting checks](references/accounting.md), the
[native source contract](native/README.md) and
[Windows packaging](references/windows-package.md). Keep every existing
accounting gate when changing code. Native source and binary provenance is
recorded in manifests; the Windows launcher verifies its shipped files before
execution. A checksum is an integrity check, not a publisher signature.

Usage Monitor is an independent tool, not an official OpenAI product. Its MIT
license covers the wrapper; third-party components retain their original
notices, including the upstream parser, Python, Rust/GNU runtime, timezone data
and fonts. See `licenses/` in the prebuilt package.

