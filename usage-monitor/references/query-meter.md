# Measurement of this query

Query metering is separate from the calendar-day dashboard. It subtracts the
native adapter's five canonical counters for explicitly selected turns. It does
not add an hourly report, estimate conversation tokens, read an account API, or
rescan the shared historical cache. It makes zero model calls and uses no network.

The Codex report's scope/time questions still apply before beginning a report.
The standalone local CLI does not implement a conversation consent gate. Editing
this feature and running synthetic tests do not access the user's usage data.

## Normal Codex reports

Use the single `Start-Usage-Monitor.cmd --codex-report` workflow after both required answers. It calls begin/watch, the existing report pipeline and one final provisional sample locally, returning only a compact envelope. Do not surround it with separate model-driven begin/sample commands. The advanced commands below remain available for explicit query-only work and diagnosis. A nonfinal individual meter exit status alone is not a report failure; inspect its structured result. This is separate from the batched report's 0/2/1 result contract.

## Packaged commands

Run the shipped launcher, which verifies the complete bundle and prebuilt engine
before handing off to the controller:

```text
Start-Usage-Monitor.cmd --query-meter begin --watch --thread-id CURRENT_THREAD_UUID
Start-Usage-Monitor.cmd --query-meter status
Start-Usage-Monitor.cmd --query-meter sample
```

`begin` defaults to `--from-turn-start`. The native `beforeTurn` checkpoint
reconstructs the selected turn's beginning, even when metering starts after early
tool calls. `--turn-id EXACT_TURN_ID` fixes an explicit turn; otherwise the native
adapter selects the last unambiguous explicit turn context/task start. The only
implicit thread ID is `CODEX_THREAD_ID`. No chat title, prompt, directory name, or
timestamp is used as turn identity. The home comes from an explicit `--codex-home`,
the saved offline settings, `CODEX_HOME`, or the standard local `~/.codex` home.
`--config PATH` and `--out DASHBOARD_FOLDER` override local settings and output
without changing them. The exact UUID filename is located only within that home;
ambiguous copies fail closed. `--source EXACT_JSONL_PATH` can resolve an explicit
copy within the selected home. The native adapter validates its session metadata
against the filename. Compressed sources are explicitly unsupported in this mode.
An implicit turn selection must still be active. If the latest recorded turn is
already completed/interrupted/failed, begin returns
`query_current_turn_unavailable` without activating a query. Retrospective
measurement of a completed turn requires its exact explicit `--turn-id`.

`begin --checkpoint-only` deliberately saves `throughTurn` instead. Its result is
the interval after that checkpoint and is always labelled `checkpoint-only`;
it must not be described as consumption of the complete request. Clarification
turns or other earlier root turns are outside this single-root measurement.

`--watch` is an alias for `--detach`, starting a hidden, independent local process
after a successful begin. `watch --detach` starts it for an existing query;
`watch` alone remains in the current local process. The default timeout is 3,600
seconds and poll interval is two seconds. Supported bounds are 1–3,600 seconds and
0.1–30 seconds. The watcher uses the bundled Python's isolated runtime through a
small `runpy` path bridge, without modifying its `_pth`, PATH, Codex configuration,
or any global hook. Unchanged source/registration stat signatures avoid repeated
hashing and native parsing. Stat is only a recheck trigger, never evidence of
usage or completion. Every actual sample revalidates the fixed source prefixes.

Transient `query_meter_busy` contention is retried during initial status lookup,
signature checks, status polling, and sampling. Retries share the original
monotonic deadline, wait no longer than the configured poll interval, and never
reset a checkpoint or infer completion. Windows validates the lock byte only
after acquiring its byte lock, so a competing writer produces the static busy
code instead of a blocked read. Other errors still fail closed. If contention
persists through the deadline, the watcher reports `query_watch_timeout`; it
cannot publish a timeout while another process owns the lock, so the last local
result may remain provisional until a later successful sample.

The developer CLI is `python -B -X utf8 scripts/query_meter.py COMMAND --engine
EXPLICIT_EXE ...`; this explicitly supplied development engine is not a substitute
for verification by the packaged launcher. Without `--engine`, the controller
requires the verified shipped prebuilt runtime and never builds/downloads one.

## Explicit agent registration

An explicitly captured child turn can be registered with a claimed dispatch:

```text
Start-Usage-Monitor.cmd --query-meter register --thread-id CHILD_SESSION_UUID --turn-id CHILD_TURN_ID --parent-session-id PARENT_SESSION_UUID --parent-turn-id PARENT_TURN_ID --dispatch-call-id EXACT_RECORDED_CALL_ID
```

The parent turn must already be registered, and the native adapter must have
observed that dispatch call in that exact parent turn. The child's own session
metadata must identify that parent through `source.subagent.thread_spawn`; fork
ancestry does not qualify. Every ancestor is registered back to the query root.
Duplicate child turns and duplicate dispatch assignments are rejected. Follow-up
turns in the same child session are separate captured turns. These checks verify
the parent session relationship and that a dispatch exists; they **do not prove
which child turn that dispatch caused**. A previous or unrelated child turn can
share the same parent lineage. The current native interface supplies no concrete
dispatch-to-child-turn causal evidence. Consequently every manual registration
has `ownershipVerified:false` and `ownershipStatus:"claimed"`, and its dispatch
remains unaccounted for purposes of complete request coverage. No CLI switch can
promote that claim into verified ownership.

Any such registration keeps the entire request result `partial`, with
`counters:null` and `final:false`, even after all captured turns complete. The
numeric `subtotal` is explicitly a **captured-turn subtotal**, not a verified
request total. `unverifiedRegistrations` and
`warnings:["query_agent_ownership_unverified"]` expose this limitation. Temporal
proximity, names, paths, or an operator assertion cannot fill the missing causal
evidence. A root-only turn without observed delegation can still complete.

## Proof, storage, and completion

The Python controller never parses rollout JSON. It hashes raw bytes in its own
local process, fixes a byte limit, runs `--query-snapshot PATH --byte-limit N`, and
hashes the same prefix again. A source can append while sampled; mutation or
truncation of an already observed prefix invalidates that turn permanently for
this measurement. The native adapter counts only complete LF-terminated records.
Each later sample validates the previous prefix and unchanged turn baseline,
lineage, engine identity, and monotonic counters. Source text is neither copied to
state/evidence nor printed. Native arbitrary errors are replaced by static codes.

All state is outside the installed package and source home:

```text
<dashboard>/query-usage/active.json
<dashboard>/query-usage/<query-uuid>/state.json
<dashboard>/query-usage/<query-uuid>/evidence/<event-uuid>.json
<dashboard>/query-usage/<query-uuid>/result.json
<dashboard>/query-usage/<query-uuid>/result.js
<dashboard>/query-usage/<query-uuid>/index.html
<dashboard>/query-usage/latest.json
<dashboard>/query-usage/latest.js
<dashboard>/query-usage.html
```

Evidence events are append-only, hash chained, and bind the current safe numeric
state; updates to state/results are atomic. The existing dashboard output folder,
its ancestors, and existing dashboard files retain their permissions.

On Windows, new query folders, files, and atomic replacements inherit the
permissions of the deliberately selected local output folder or its existing
ancestor. This is the same access boundary as the full dashboard and ledger
stored beside them. The meter never writes Windows DACLs, derives user/owner
identities, or saves SID/anchor registries. It adds no Everyone/Users grants and
does not change permissions on existing objects. A later host or sandbox writer
therefore inherits the configured output access rather than recalculating grants
from whichever account happens to execute that write. The same rule applies if
the output folder was initially missing.

New POSIX owned directories use mode 0700 and new query files mode 0600; existing
shared parents are never chmodded. A read-only `status` neither changes
permissions or file bytes nor creates a missing scope/lock. Previously repaired
legacy query trees retain their existing inheritable permissions; the application
performs no automatic permission repair or migration. A process lock serializes
updates.

Starting a second query while one is open is rejected without overwriting
it. A terminal query allows a new begin. Each query retains its own stable result
and page; an older watcher cannot replace a newer query's `latest` sidecars.

`status` reads only owned numeric state and its evidence, never a source rollout
or another process; it does not start a watcher. `sample` explicitly reads the
selected registered sources. `--query-id UUID` addresses an older query without
changing the active pointer. Compact CLI stdout omits the per-turn detail list.

A result is `complete` and `final:true` only after matching successful completion
markers for **every registered turn**, with every observed delegation accounted
for and every proof valid. Under the current native contract, manual agent claims
cannot satisfy that ownership gate, so only an undelegated root-only measurement
can become complete. Any incomplete source tail also blocks completeness:
`partialSources` and `query_trailing_partial` disclose it, and the watcher keeps
waiting for a complete, revalidated prefix or its timeout. A recorded completion
before an unknown partial record does not prove the tail contains no more usage.
Inactivity, mtime, process exit, timeout, or abort never proves completion. An active
verified root result is `provisional`; missing delegation,
timeout, or interrupted/failed turns yield `partial`, while no verifiable turn
yields `unavailable`. `terminal` says the local watcher stopped, independently of
`final`. A pre-answer value remains provisional and excludes the eventual complete
closing response. Link the returned per-query `viewPath` so the same local page
can receive the final measured value after the response completes.

The five fields are `inputTokens`, `cachedInputTokens`, `outputTokens`,
`reasoningOutputTokens`, and `totalTokens`. Native totals and subset discrepancies
are preserved. Cached input remains inside input, reasoning remains inside output;
neither is added again. No quota, credits, money, account ownership, or execution
on this device is inferred from stored source files.

## Browser result contract

The safe sidecar sets `window.USAGE_QUERY_METER` to the compact result plus local
per-turn metadata. `status` is `provisional`, `complete`, `partial`, or
`unavailable`; the browser also supports an initial `pending` display. `counters`
is populated only for a complete result; `subtotal` describes the explicitly
captured turns and `subtotalScope` is `captured-turns`. `coverage`, `cutoff`,
registered/completed turn counts, `unaccountedDispatches`,
`unverifiedRegistrations`, `ownershipVerified`, `partialSources`, static
`warnings`, and static `errors` disclose the boundary. `modelCalls`
is zero and `networkUsed` is false for this local controller.

JSON integer counters remain exact in Python. Browser code uses `counterText` or
`subtotalText`, decimal-string mirrors of the five counters, with `BigInt` and
en-US formatting so sums above JavaScript's safe integer range do not round.
The standalone page polls its local sidecar every two seconds; opening or reloading
HTML does not launch the scanner. The dashboard uses `query-usage/latest.js`;
the per-query page uses its own `result.js`.

## Synthetic validation

```text
python -B -X utf8 -m unittest discover -s tests -p test_query_meter.py -v
```

Set `USAGE_QUERY_TEST_ENGINE` to the just-built production executable to run the
native controller integration fixture too; without it that explicit native gate
is skipped. Tests cover fixed-prefix races, append-only proofs, historical changes,
duplicate/ambiguous scopes, private text exclusion, agent ancestry/claimed
dispatches, foreign older child turns, partial-tail completion, implicit completed
turn rejection, failure/timeout, stable query output, exact counter strings, and
CLI runtime handoff. Native production fixtures use synthetic JSONL only and
verify that later unrelated turns do not enter the selected turn's result, manual
claims never become request ownership, and a hidden watcher updates a root-only
result after the response's matching completion marker.

Windows inheritance regressions inspect isolated synthetic directories only:
existing dashboard/ancestor/source ACLs remain unchanged; a missing output folder
and replacement files inherit the selected parent's grants; independent writers
and watcher completion preserve access; repaired legacy three-principal scope
and lock ACLs remain unchanged. Runtime checks prohibit Windows security SDK and
chmod calls. These controller tests do not claim real token impersonation; the
packaged integration gate separately exercises distinct host and sandbox tokens.
The POSIX mode branch is also checked. No test modifies a profile or an existing
production dashboard.
