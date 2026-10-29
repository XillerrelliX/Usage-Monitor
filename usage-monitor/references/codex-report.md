# Batched Codex report contract

Read this for a concrete batching error or deliberately separate setup. Ordinary reports use the fixed SKILL.md command without inspecting implementation.

## Inputs

```text
Start-Usage-Monitor.cmd --codex-report --scope all --period today --today YYYY-MM-DD --timezone IANA_ZONE
Start-Usage-Monitor.cmd --codex-report --scope chat --period last3 --today YYYY-MM-DD --timezone IANA_ZONE
Start-Usage-Monitor.cmd --codex-report --scope project --project-id EXACT_ID --period last7 --today YYYY-MM-DD --timezone IANA_ZONE
Start-Usage-Monitor.cmd --codex-report --scope selection --project-id EXACT_ID --chat-id EXACT_ID --from-date YYYY-MM-DD --to-date YYYY-MM-DD
```

The two conversational questions and explicit answers remain mandatory before any command. A preset requires the client-provided `--today` and explicit `--timezone` assertion. It computes today plus the previous N−1 dates internally, never from the latest recorded usage date or an untrusted host date. The assertion must match saved app-zone grouping. On a mismatch, preserve the scope answer and clarify the calendar dates in the saved app timezone; do not silently change source/settings or relabel a day. Custom exact dates use the saved app zone and accept an explicit matching timezone assertion.

`all` takes no project/chat IDs. `chat` resolves the actual `CODEX_THREAD_ID` locally and takes no target-ID override. `project` needs one exact verified project ID. `selection` needs explicit project/chat IDs; repeated IDs have the existing union-within/intersection-across semantics. The helper cannot establish a named project from a generated cwd. If This project has no verified assignment, ask which project before invoking the command. A brand-new executing chat can be absent from the older snapshot; its exact environment identity is still checked by the existing source pipeline.

Optional `--config PATH` deliberately selects an existing separate local setup for every batch stage. This mode does not accept source-home overrides, export, cache-only, arbitrary selection-file, helper-only or query-only flags. First-use all/chat follows the normal offline setup, with an explicit timezone. Project/custom IDs need a verified existing catalog. Invalid or ambiguous arguments fail before source work; no default to All usage.

## One local operation

In PowerShell, append `exit $LASTEXITCODE` to the shell command invoking the launcher so usable native warning exit code 2 is preserved by the surrounding shell.

1. Validate arguments, scope, timezone/config and the shipped bundle/runtime through the public entry.
2. Begin the optional request meter for the actual executing chat and start its bounded hidden watcher. An already active/invalid meter is reported separately and never overwritten.
3. Run the existing accounting/refresh pipeline exactly once. Its conservation, ownership, source revision, rollback and atomic publication gates are unchanged.
4. Verify the newly published data/proof and exact initial selection. Compute the requested projection through `project_metadata.project_report`, bound to the same snapshot/cutoff.
5. Sample this same query once, then emit one compact JSON object. The watcher can update the result after the answer completes; do not wait for that completion while preparing the answer.

No stage makes model calls, uploads data or contacts an account service. Existing internal verification checks are preserved; batching removes repeated public invocations and model round trips, not integrity checks. A report failure stops, preserving the last good report. There is no automatic rescan, wider source, stale-result substitution or model-driven recovery loop.

Keep process polling inside one orchestration execution. If it yields, resume its exact cell/session rather than restarting. A required filesystem grant may need a framework permission flow; retain the confirmed scope/period and never switch sources to avoid it. Model/tool runtime limits can require resumes, so one local command does not promise a fixed number of model inferences or tokens.

## Compact result

For ordinary completed reports with exit 0 or 2, the entire user-facing final response is exactly `[Lokales Dashboard öffnen](<dashboardUrl>)`, using the returned protected URL and filters. Keep totals, dates, diagnostics and request measurements in the local result; append no footer or second link. Explicit explanation, diagnostic or query-measurement requests are exceptions. The structured fields below remain available to the application and those explicit requests.

The result supplies `scope`, `selection`, `timeZone`, `cutoff`, exact five-field `totals`, `diagnostics`, filtered `dashboardUrl`, and separate `queryUsage`, plus local-only execution indicators. Do not load complete data/ledger files or extract a second projection in the model. Use structured warning/failure state and the process result: 0 success, 2 usable report with warnings, 1 report failure. A nonfinal individual meter exit status alone is not a failed batched report.

`queryUsage` describes the captured executing turn, not the report's target scope or all earlier clarification turns. Its sample is provisional and excludes the eventual complete closing answer. Preserve partial/unavailable attribution and static errors. Only its same-origin protected per-query `viewUrl` is the stable measurement link; no copied page or custom redirect is needed. Cached input and reasoning remain subsets. The count is neither quota nor money.

If the local helper cannot start but report publication succeeds, retain the pipeline's saved-file fallback and warning. Do not fabricate a live URL or expose arbitrary filesystem routes. Standalone double-click, advanced meter-only commands, public export and legacy developer setup remain separate documented operations.
