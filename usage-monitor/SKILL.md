---
name: usage-monitor
description: Open Usage Monitor, a standalone local Codex token dashboard with a prebuilt Windows runtime, date/project/chat filters and verified accounting. Stored files do not prove device-only execution or account allowance.
---

# Usage Monitor

Count local files with the pinned upstream parser. No desktop app or account connection is required. All parsing, accounting, saved history and browser filtering remain local; only missing-dependency setup downloads software.

## Required scope and time questions

For every new usage report or refresh requested through Codex, ask BOTH questions below together and wait for explicit answers before running the launcher, scanning logs, reading usage data, calculating totals or generating a report. This applies in every chat and project; never reuse a previous request's scope or period implicitly. If the request already contains values, present them for confirmation in these two questions. A preselected option, silence or elapsed time is not an answer. Within the same ongoing request, retain already valid, explicitly confirmed answers and clarify only an unresolved or changed field; do not ask the same valid selection again. This gate does not apply to explaining or editing the skill itself, or to standalone use without Codex.

Use the user's language for the two questions, with these exact preset labels:

1. **What would you like to evaluate?** (German: **Was möchtest du auswerten?**)
   - **1** — This chat
   - **2** — This project
   - **3** — All usage
2. **Which period would you like to evaluate?** (German: **Welchen Zeitraum möchtest du auswerten?**)
   - **1** — Today (so far)
   - **2** — Last 3 days
   - **3** — Last 7 days

Both fields must also accept the user's own free-text answer alongside the presets, for example named chats/projects or a specific date range. When `request_user_input_async` is available, submit both questions together with these three choices each; its built-in free-text input supplies custom answers, so do not add a separate “Other” option. Otherwise ask both questions together in chat with the numbered presets and explicitly invite a custom answer for either field. Accept concise answers such as `Umfang 1, Zeitraum 3`, as well as free text.

Resolve missing counts, ambiguous names/paths, a specific other chat, or unclear start/end/timezone with follow-up questions; keep the unresolved question pending and do not launch. “This chat” means its exact current chat ID, including its verified agents. “This project” means the exact active project's verified assignment, not a guess based on the current folder; if the chat is projectless or the assignment is unclear, ask which exact project is intended. “All usage” means the selected local Codex data source, not an account-wide or remote total. Freeze the confirmed dates in the saved app timezone. “Today (so far)” means local 00:00 through the latest available scan; “Last 3 days” means today and the preceding two calendar dates, and “Last 7 days” means today and the preceding six. Custom N-calendar-day periods likewise include today and the preceding N−1 dates.

The current tested report pipeline has **calendar-day precision only**. For hours or intraday boundaries, explain that exact results are unavailable and obtain explicit acceptance of a specific calendar-day alternative before running anything. Never silently round or relabel a daily total as an hourly total. The dashboard supports multiple projects and chats: selections within either field are combined, and the two fields intersect. The total counts each underlying usage slice once. Separate colored comparison series show the selected chats within the project filter, or the selected projects when no chats are selected. Do not add overlapping reports to calculate this total. If exact selection or project-folder attribution cannot be resolved, ask rather than broadening it.

## Fixed report execution

After both answers are explicit and supported, use the batched public command. A normal check has one local execution and one compact result. Do not recreate its steps in model-driven tool calls.

```text
<skill>/Start-Usage-Monitor.cmd --codex-report --scope all --period today --today YYYY-MM-DD --timezone Europe/Berlin
```

Replace the date and timezone with the current client context; these are parameters, not fixed defaults. The saved app timezone must agree. A mismatch needs clarification before scanning; do not silently change the setup or shift dates.

- Scope: `all` for All usage; `chat` for This chat (resolved from `CODEX_THREAD_ID` inside the batch); `project --project-id <verified-id>` for This project. `selection` accepts repeated exact `--project-id` and/or `--chat-id` flags for a confirmed custom selection. Never infer a project from cwd. Resolve ambiguous names/assignments before execution; never pass a guessed ID.
- Presets: `--period today|last3|last7 --today YYYY-MM-DD`. The batch computes inclusive dates, including today. For an explicitly confirmed custom range, use `--from-date YYYY-MM-DD --to-date YYYY-MM-DD` instead of both preset flags.
- A deliberately selected separate setup uses `--config PATH` on this same command. Do not discover or switch sources opportunistically.

The batch validates scope, dates, timezone and verified runtime, begins the optional request meter/watch, performs exactly one existing refresh, verifies the published report, obtains exact filtered totals, samples the same request once and returns its links. The scanner still updates its shared all-history cache; scope limits the result. All existing accounting, ownership, privacy and atomic-publication checks remain in force.

When invoking the launcher from PowerShell, finish that shell command with `exit $LASTEXITCODE` so the native warning exit code 2 is preserved.

Keep process waiting inside the same tool execution. With `functions.exec`, await `tools.exec_command`, then await `tools.write_stdin` while that returned session remains active; expose only terminal compact stdout and material errors. If the orchestration itself yields, resume that same cell/session, using bounded waits of at most 60 seconds. Never launch the command again merely because it is still running. Send a concise progress update if needed without opening another lookup or scan.

For ordinary checks, do **not** separately look up the chat environment variable, inspect API signatures, read settings/history/source code, run manual projections, invoke individual meter commands, create link files, open a browser for verification, delegate agents, browse documentation or run developer tests. Those steps caused unnecessary model round trips. The tested batch owns them; read [batch command contract](references/codex-report.md) only for a concrete error or advanced custom setup.

For an ordinary completed report with exit 0 or 2, the final response must contain exactly one Markdown link: `[Lokales Dashboard öffnen](<dashboardUrl>)`, substituting the exact `dashboardUrl` returned by this execution. Preserve its protected path and exact filters; never hard-code a previous URL. Add no introduction, totals, dates, warning commentary, request-consumption text, second link or closing sentence. Apply this format also to usable reports with warnings, including a partial current day or an unavailable optional meter. Keep diagnostics and request measurements in the local result. Only an explicit request for an explanation, diagnostics or query measurements changes this response format.

Stop after a terminal result. Exit 0 is success, 2 is a usable result with warnings, and 1 is failure. On failure, report the static error and required clarification/recovery; do not retry the scan, broaden scope, invent flags or return an old report as fresh. Optional meter failure does not invalidate an otherwise verified report. Diagnose only the indicated failing stage; preserve already valid answers and the last good data.

## Request measurement

Ordinary report responses contain only the dashboard link; do not append a request-consumption summary or a per-query link. The batch's optional `queryUsage` measurement and bounded local watcher remain enabled. Do not wait for your own completion or run another sample before answering.

If the user explicitly asks for request measurements, use only this execution turn's `queryUsage`. Its pre-answer sample is provisional and excludes the complete closing response and earlier clarification turns. State its cutoff and use its exact protected `viewUrl`. Preserve partial/unavailable labels; never describe a captured-turn subtotal as the full request cost. Missing telemetry is not zero usage. Without a suitable metric, say the request is not separately measurable; the local analysis itself makes no model calls.

Cached input is a subset of input; reasoning is a subset of output. Do not add either subset again or total overlapping reports. The total includes repeated cached context across model steps. It is not a measure of unique text or an account allowance. Never infer credits, money, quota or the human/account/device responsible from stored files.

For an explicit query-meter-only request or meter diagnosis, read [query measurement](references/query-meter.md); do not add a calendar report. Individual begin/sample/register/watch commands are advanced operations, not the normal report workflow.

## Local operation and maintenance

The complete Windows x64 package includes verified Python, timezone data and the pinned scanner. It needs no system Python/compiler, account connection or runtime download. Double-click `Start-Usage-Monitor.cmd` for standalone use; run it again for new usage. Browser filters and the local scanner make zero model calls. The Codex conversation still uses tokens. macOS/Linux source builds are separate and unverified here.

The live dashboard is served only on protected `127.0.0.1`; the helper exits after an hour without activity. `dashboard`/`fileDashboardUrl` are saved snapshots. Preserve initial selections when copying a view. Unknown IDs/invalid dates must fail instead of broadening. Export CSV stays below Chats; preserve the user's current simplified navy/mint UI.

Never upload logs, caches or reports, publish the dashboard, read auth.json/cookies/account identities, or load raw logs, messages, complete ledgers, HTML or chat lists into model context. The native parser necessarily reads selected local rollout files in its own process and retains only approved numeric/lineage metadata. Named-project attribution and guardian/chat/agent ownership follow verified metadata. Stored or synced files do not prove execution on this device or the selected account.

For code changes/diagnosis read [accounting and validation](references/accounting.md), the complete [native contract](native/README.md), and only the relevant implementation. Preserve mandatory tests and independent final review; ordinary checks never repeat these developer gates. For setup, explicit legacy cache reading, source selection or public distribution read [Windows package contract](references/windows-package.md). Public exports must contain only declared code/runtime/licenses, never private settings/history/reports. Do not bypass manifest verification or publish/upload without explicit authorization.
