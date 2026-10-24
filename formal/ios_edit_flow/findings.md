# Phone-render findings (the "killer" screens)

Screens in scope:
- S1 "Kria couldn't prepare this edit on your iPhone"
- S2 "Couldn't refresh this edit"
- S3 "This edit needs your attention" / "Needs attention"
- S4 the "couldn't render" family (`phone_plan_unsupported`, "Kria couldn't render this approved edit")

Models:
- `PhoneRender.tla`: the code on `codex/killer-integration` (the fixes). Synced commit in `coverage.json`.
- `PhoneRenderBefore.tla`: the code at `origin/main@ff27b0a65`, before the fixes. `verify.py` proves it broke each rule the fixes made pass.

Run with `make verify-phone-render` (about 40 s). `--full` adds the two slow liveness pairs (about 3.5 more minutes; both hold at the synced commit).

## Prod evidence (2026-09-24 to 2026-10-24, 133 jobs, read-only admin GETs)

| # | What the user saw | Jobs | Share |
|---|---|---|---|
| 1 | `phone_plan_unsupported`: the planner refused before any render (S4) | 30 | 22.6% |
| 2 | needs_attention on the phone (S3/S1); none recovered | 4 | 3.0% |
| 3 | `creator_render_contract_unsupported`: cloud refusal before render | 3 | 2.3% |
| 4 | `phone_plan_failed` (Gemini timeout) | 1 | 0.8% |
| 5 | `creation_text_failed` | 1 | 0.8% |

Breakdown of bucket 1:
- About 21 jobs were refused by our own verification/contract checks:
  - "missing confirmed on-screen text" ×6
  - "can't verify this ordering rule" ×3
  - "That exact text isn't in this draft" ×3
  - "couldn't keep the confirmed length" ×3
  - clip-order or grouping misses ×4
  - title refusal ×1
- 2 were planner-quality failures (narration match).
- 3 were genuinely the user's to fix: no song in the clips, no speech.
- 2 were phone engine gaps: portrait-only subtitled, letterboxed landscape.
- 3 were internal bugs. Two of them showed a raw Pydantic error to the user.

Once a phone recipe exists, export succeeds 84 of 88 times (95.5%). The 4 needs_attention jobs:
- `missingOriginal` (proxy) ×1
- `missingAsset("1")` ×1
- 24h reaper ×1
- server export-picture check ×1

Small sample: 5 active users, one of whom owns 75 jobs.

## Code paths to the screens

The tracers found 69 paths and sorted each one by cause:

| Class | Paths |
|---|---|
| Bug: the screen should never appear | 30 |
| Temporary glitch that should recover on its own (offline, 5xx, lock contention, hot phone) | 15 |
| Misleading copy (an update is needed, or the edit was superseded, but we say "failed") | 6 |
| Genuine, user can fix it (storage, missing original, too long, signed out) | 10 |
| Genuine, user can't fix it (rollout paused, contract changed) | 8 |

So 51 of 69 paths (74%) put up a failure screen when nothing the user did went wrong.

## Fixed on this branch

All five finding rules now hold on `PhoneRender.tla`, and each still breaks on
`PhoneRenderBefore.tla`. One condition is left: a recipe this iPhone cannot render (see
"Still broken" below). Each passing rule has a mutation that puts the old behavior back at
one guard, and TLC catches it ("teeth").

| Rule | Before the fixes (trace) | What changed in the code | Teeth (old behavior put back) |
|---|---|---|---|
| NoSpuriousAttention | breaks, 2 states | A failed status read waits quietly and re-reads (`DeviceRenderSessions.swift` `readFailed` / `wait` / `scheduleRetry`). `URLError(.cancelled)` counts as cancellation. A transient error inside `perform` parks the render instead of failing it (`DeviceRenderCoordinator.swift` `park`). "Couldn't refresh this edit" is gone: refresh failures only write `refreshSync` (`NativeEditorSession.swift` `handleRefreshFailure`). A Stop shows as "rendering paused", not a failure. | A failed read shows "couldn't prepare" again: caught |
| CloudDecisionReported | breaks, 4 states | A hot phone waits and resumes when it cools (`gate` -> `.wait(.cooling)`, `resumeWaiting`). A failed capability read keeps the last enabled set instead of `.disabled` (sessions, chat and editor). A recipe the phone will never render is reported with `sendDecline`, retried until the server accepts it. | The decline is not sent: caught |
| AttentionHasWayOut | breaks, 16-state lasso (Stop, then runtime-v2 Try again goes to chat approval, which refuses) | `/device-render/retry` no longer asks for approval, and GET always sends `retry_requires_approval = false` (`routes/device_render.py`). Relinking originals resumes on its own (`DeviceRenderPanel.swift` `resume`). The reaper's `timed_out` is re-pinned when the phone opens the project (`_auto_resume_reaped`). | `/retry` refuses runtime-v2 jobs again: caught |
| ClientServerAgree | breaks, ~15-state lasso (Stop, report dropped offline) | Stop is a resumable pause on the server (`mark_device_paused`; the phase stays `awaiting_device`/`syncing`; the reaper skips it). The report is saved on the receipt and re-sent until the server takes it, including after a relaunch (`reportFailure`, `startReporting`, `recover`). | A failed delivery drops the report again: caught |
| PreviewConverges | breaks, 10- to 16-state lasso | Transport errors do not use up the preview budget. A skipped reconcile is retried from the poll (`deviceReconcilePending`). A ready variant of a newer generation, seen twice, is followed instead of waited out. The retry banner follows the live render and heals itself (`deviceRenderPresentationChanged`). | The newer-generation follow is removed: caught |

How the model changed (all in `PhoneRender.tla`):
- New server state: `sPaused` (the record's `paused_by_user`).
- New phone state: `decl` (the decline report being sent), `sched` (a scheduled re-read), the presentation's `wait` (quiet or cooling).
- `rep` is now persisted (`disk`) and re-sent (`live`) instead of fire-and-forget.
- The editor tracks `from` (the generation the save started from), `seen` (newer-generation sightings), `caps` (whether it ever read capabilities) and `recPend` (a reconcile it skipped).
- No path sets `banner` any more. Approval no longer routes anything, so the fixed runs pin `Approvals = {FALSE}`.
- A variant GET with no device refresh is applied in the same step. The code has no await between the GET returning and `applyPreviewVariant`.

Rule wording. The rule texts are the same in both modules. They use three helpers that each module defines:
- `Told`: a report for the pinned request is on its way. In the fixed model this is the coordinator's report or a decline.
- `ServerWaiting`: the server still waits for this phone. A paused record does not count.
- `NoReportInFlight`: nothing is on its way.

`CloudDecisionReported` now names the request: `req = sId`. Without it, the rule broke as soon as another device pinned a new request, before the phone had read it. Nothing had been decided about that new request yet. The old code still breaks the narrowed rule in 4 states.

Two environment assumptions changed in both modules:
- **Stop is bounded.** The creator taps Stop at most once per run, the same way Saves are bounded. Unbounded Stop let the "user" kill every reconcile forever. It also made the liveness state space 50x larger.
- **ClientServerAgree runs without user fairness** (`Spec`, not `SpecUser`), to match its text: "without anyone tapping anything". That is stronger than before.

## Still broken: a recipe this iPhone cannot render

This affects NoSpuriousAttention, AttentionHasWayOut and PreviewConverges. Each one breaks on the fixed code when the recipe needs a capability this phone's verified set lacks (K:19-20). It is a real result, not a modeling gap. `verify.py` checks that these three still break in that scenario (`RESIDUAL`), and that every other condition passes.

`--explore` ran each of the three across 7 event sets × 4 starting conditions (faults only, Save, Stop, background, kill + reaper, Save + another device, draft sync). Every break had the unsupported recipe as its condition:
- NoSpuriousAttention: 7 of 28 scenarios break.
- AttentionHasWayOut: 7 of 28 break.
- PreviewConverges: 2 of 28 break (only the Save scenarios).

Everything else holds, including AttentionHasWayOut with kills, the reaper, backgrounding and a second device.

Trace, 4 states for NoSpuriousAttention and a 10-state lasso for AttentionHasWayOut:
1. The chat polls. The gate says `.decline(unsupported_recipe)`.
2. The card shows needs attention, and `sendDecline` tells the server.
3. The server records `needs_attention` / `unsupported_recipe`. The card now reads "This edit needs your attention" / "Your project is saved."
4. The card has no button. Try again is hidden for a structural reason, the "Review the updated edit" call to action only shows for `contract_changed` / `requires_change`, and "Find original files" is hidden too. Nothing on screen leads to a published video.

In the editor (PreviewConverges, a 14-state lasso), the "Retry device render" banner loops:
- Before the decline lands, retrying reconciles into the same decline.
- After it lands, `/retry` answers `requires_change`. Only then does the chat card switch to "This edit needs a change" with its button.

The only exit is a new edit made in chat, and the model does not assume a new edit will come.

Suggested fix: show "This edit needs a change" (or "Update Kria" when the phone is older than the recipe) as soon as the decline is recorded, rather than after a failed retry. The editor banner should then offer "Review in chat", not a retry.

Not added: RepairNeverSilent (planner repair notes). The planner runs before a recipe is pinned, and this model starts after the pin. Adding it would need a planner model, so it is not cheap here.

## Scope and bounds

- Bounds: MaxFaults 1, Stop at most once, MaxId 3 or 4, one or two Saves, and 2 answered preview polls (the real budget is 300).
- The default liveness runs split the events:
  - Save on a phone with its originals, cool or hot.
  - Stop with every condition except an unsupported recipe.
  - Kills, with every condition (ClientServerAgree).
- `--full` adds Save with a missing original, and Save with Stop. Save + Stop + a missing original all at once is about 4M states and is not run for liveness. The safety runs cover it.
- Not modeled:
  - the rollout hold (`phone_rendering_paused`)
  - the GET's 409 superseded / cancelled / `editor_empty` and 426 answers
  - contract refusals (`contract_changed`)
  - output verification
  - the retry caps: 12 re-reads, 4 transient waits, 12 decline sends. MaxFaults 1 never reaches them.
  - the editor's "reconnecting" hint, shown after 3 failed reads
  - the planner (S4)
- The resume inside `apply` (`retryFailure` on a paused record) is one step in the model. If it fails, the code swallows the error, and the reservation clears the pause anyway.
- The editor starts from a failed capability read. With a good read, a later failed read does the same as the no-fault branch.
- The statecharts in `statechart/` still show the old code.

## Before the fixes (PhoneRenderBefore.tla, origin/main ff27b0a65)

The traces TLC finds today, with Stop bounded to one tap (line refs at ff27b0a65). Lasso lengths vary a little from run to run with TLC's worker order.

1. **NoSpuriousAttention**, a 2-state trace.
   - The editor is open and a chat-refresh read fails once (offline or a 5xx).
   - "Couldn't refresh this edit" appears and stays: it has no button and no retry on error (`NativeEditorSession.swift:1856-1871`).
   - Same rule, phone side: any failed status GET puts up S1 (`DeviceRenderSessions.swift:170-187`).
   - This includes a `URLError(.cancelled)`, which the `CancellationError` check doesn't catch, and 409 superseded / 426 update-required answers.
2. **CloudDecisionReported**, a 4-state trace.
   - The phone is hot, low on storage, or the recipe needs a capability it lacks.
   - `reconcile` returns before `coordinator.start`, so the server stays `awaiting_device` and is never told.
   - The user sees needs-attention; the server waits for 24h until the reaper runs.
3. **AttentionHasWayOut**, a 16-state lasso.
   - The runtime-v2 loop: after a save and a Stop, Try again goes to chat approval, which 409s (`retry_requires_approval`), and nothing on screen ever publishes.
   - Same rule, another path: the user taps Stop, then the capabilities fetch fails. `try? … ?? .disabled` turns that into "not available" (`NativeEditorSession.swift:810-814`).
   - From there, every Try again loops back to needs-attention, and no retry on screen ever publishes.
4. **ClientServerAgree**, a lasso of about 15 states, now checked without user fairness.
   - The user taps Stop and the failure report is dropped (fire-and-forget, offline).
   - The phone says "stopped" while the server keeps waiting for this phone, forever until the reaper runs.
5. **PreviewConverges**, a lasso of 10 to 16 states.
   - After a save, the preview poll waits on a fence that only a device reconcile clears.
   - If that reconcile fails or is skipped, the editor waits out 300 polls and then shows conflict copy.

## Rules the code keeps (with teeth)

Each rule fails when its guard is deleted, on both the old and the fixed code:
- NoStalePublish
- PublishedStaysPublished
- SyncedMeansPublished
- NoStaleRequestReinstalled

Now also passing on the fixed code, each with teeth:
- NoSpuriousAttention
- CloudDecisionReported
- AttentionHasWayOut
- ClientServerAgree
- PreviewConverges

The first three of these exclude an unsupported recipe; see "Still broken" above.

## Fix order (ranked by user impact; written against ff27b0a65)

Items 2 to 6 are on this branch (see "Fixed on this branch"). Item 1, the planner, is outside this model.

1. **S4 planner refusals (22.6% of jobs).**
   - Repair or soften verification refusals at plan time instead of failing after approval.
   - Narrow the bare `ValueError` mapping to `phone_plan_unsupported`.
   - Never show raw Pydantic text.
2. **Transient errors become waiting, not failure (S1/S2/S3).**
   - Classify with `RequestFailureCause`, keep the last good state, and retry with backoff and on reconnect.
   - Treat `URLError(.cancelled)` as cancellation.
   - A failed read never downgrades `.synced` or `.localReady`.
3. **Map 409/426 answers to their real state:** superseded (follow the newer attempt), update app, or edit removed.
4. **Tell the server about local cloud decisions; wait out a hot phone.**
   - Report hot / unsupported / outdated to the server.
   - Auto-start when the phone cools down.
   - Keep the last good capabilities instead of `.disabled`.
5. **Make Stop a resumable pause.**
   - Retry the report until the server accepts it.
   - Re-delivering an already-approved recipe needs no new approval.
6. **The refresh-sync banner gets its own quiet channel,** separate from `saveState`.
