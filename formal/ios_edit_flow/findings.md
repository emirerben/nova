# Phone-render findings (the "killer" screens)

Screens in scope:
- S1 "Kria couldn't prepare this edit on your iPhone"
- S2 "Couldn't refresh this edit"
- S3 "This edit needs your attention" / "Needs attention"
- S4 the "couldn't render" family (`phone_plan_unsupported`, "Kria couldn't render this approved edit")

Models:
- `PhoneRender.tla`: the code on `codex/killer-integration` (the fixes). Synced commit in `coverage.json`.
- `PhoneRenderBefore.tla`: the code at `origin/main@ff27b0a65`, before the fixes. `verify.py` proves it broke each rule the fixes made pass.

Run with `make verify-phone-render` (about 2.5 minutes). `--full` adds the heavier event combinations (`FULL_RUNS` in `verify.py`).

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

All finding rules hold on `PhoneRender.tla` with no condition scoped out, and each of the
five original ones still breaks on `PhoneRenderBefore.tla`. `RESIDUAL` in `verify.py` is
empty. Each passing rule has at least one mutation that puts a piece of the old behavior
back, and TLC catches it ("teeth", `TEETH` in `verify.py`).

### Round 1 (the killer-screens fixes)

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

### Round 2 (an adversarial check of round 1 found five more)

| # | What was wrong (trace) | What changed in the code | Model | Teeth |
|---|---|---|---|---|
| 1 | A recipe this iPhone declines (`unsupported_recipe`) read "This edit needs your attention" with no button; the editor's "Retry device render" re-declined it forever. Was RESIDUAL for NoSpuriousAttention, AttentionHasWayOut, PreviewConverges. | `unsupported_recipe` is a change reason from the moment the decline is recorded (`DeviceRenderPanel.swift` `changeReasons`): "This edit needs a change" + "Review the updated edit". The editor banner follows `nextStep`: "Review in chat" / "Update Kria" / "Resume rendering" / "Retry device render" (`NativeEditorComponents.swift`, `NativeEditorSession.deviceRenderNextStep`). A quiet network wait keeps that button (`needsChange` no longer needs `waiting == nil`; the model found a failed read on the declined card hid it). | `Screen(p)` (the card's title by phase + reason), editor states `review` / `update`, `ChatChange` (the creator asks for a change; Kria re-plans with phone-verified features) and `UpdateApp`, both under user fairness. AttentionHasWayOut now also covers the your-turn screens (stronger). | `decline-has-no-change-screen`, `decline-dead-end`, `outdated-banner-offers-retry` |
| 2 | Losing a capability after the export finished kept the file, said "can't sync from this iPhone yet", sent no decline; the server waited forever (polls keep `last_polled_at` fresh, so the reaper never fires). | `DeviceRenderSessions.apply`: `.decline where resumesSync` uploads the video it already has (the server checks the bytes, never the phone's capabilities). Any other decline is reported, also when an export of the request is kept. | `LoseCaps` (event `caps`: `unsupported` turns TRUE once `coord.file`). Contradict now also covers `localReady` with an error. | `lost-capability-kept-silently` (CloudDecisionReported), `lost-capability-contradicts` (ClientServerAgree) |
| 3 | `renderer_outdated` loop: `/retry` re-pinned for the build that declined it, the phone declined the re-pin, the editor retry (no filter) looped. | `routes/device_render.py`: the failure report stores `declined_client_protocol`; `/retry` from a build that is not newer answers 409 `requires_update`; a newer build is re-pinned on `GET` (`_auto_resume_updated`). The phone maps `requires_update` to "Update Kria"; `retryDeviceRender` refuses change and update reasons. | Decline reasons split into `unsupported` and `outdated`; `RetryOK` mirrors the route; `RecFetch` auto-resumes an outdated record for a non-outdated phone; new rule NoFutileRepin (the server never re-pins the same recipe for a phone that declines it again). | `outdated-retry-loop`, `unsupported-retry-loop` (NoFutileRepin) |
| 4 | Verified real. The chat poll only reads when `changed || !deviceSettled || status == .rendering` (W:2240-2242); `changed` and the project status are server projections the phone cannot rely on, and an editor Save of a phone render does not have to move the thread. Two traces: (a) the chat GET for request 1 is in flight, the editor saves (request 2), its refresh returns at the ticket guard, request 1 installs and turns `superseded` (settled), nothing reads request 2, "Check preview again" loops; (b) found by the model once the chat poll's fairness matched the guard: after a publish, the editor's refresh read fails, `wait()` skipped a synced card without scheduling a re-read. (c) also found: a report that lands after a relink makes the coordinator `superseded` against a needs_attention record, and nothing reads it again. | `reconcile` remembers a read asked for while the ticket is held (`rereads`) and runs it when the holder finishes. `wait()` keeps a synced card as it is but still schedules the re-read. `isSettled` no longer counts `superseded`. | `ChatPoll` is always allowed but only fair while not `Settled` (synced); `again` + `Reread`; `ReadFailedPres` always schedules. | `lost-reread`, `failed-read-not-re-read` (PreviewConverges) |
| 5 | Model fidelity. | (code unchanged) | `/retry` refusal sets the presentation reason (`requiresChange`, or `outdated` for `requires_update`); the paused resume's `try? retryFailure` may fail (`ResumeServer`); `sendDecline` skips a second decline for an identity it already sent (`decl` keeps the identity and whether it is still sending). | `failed-decline-dropped` (CloudDecisionReported) |

Teeth added for things that had none: `reaped-not-auto-resumed` (AttentionHasWayOut with nobody
tapping, kill + reaper: the reaper's timed_out must be re-pinned on reopen) replaces the old
AttentionHasWayOut tooth that only flipped the `approval` variable nothing reads.
`failed-read-not-re-read` runs with `Chat = {FALSE}` (the editor opened from the gallery,
`EditorViews.swift:265`, with no chat poll underneath), so the scheduled re-read is the only
thing that reads again.

Environment assumptions added in round 2:
- **Review in chat leads to a renderable recipe.** `ChatChange` sets `unsupported` FALSE: Kria's
  phone planner only uses features verified for phones (`phone_render_verified_features`). The
  model does not model the planner.
- **An app update is a newer protocol.** `UpdateApp` clears `outdated`; the server compares
  `X-Kria-Client-Protocol` with the declining one. A release that renders a newer recipe without
  bumping the protocol would get `requires_update` again (open risk, not modeled).
- `UpdateApp` and `ChatChange` are bounded like the other pinning events (`sId < MaxId`).

New runs: `one-save-declines`, `lost-capability`, `live-save-unsupported`, `live-faults` (kill,
reaper, background, so the reaper's timed_out appears), `live-reaped-no-user`,
`live-gallery-editor`, `live-lost-capability`, `agree-lost-capability`; `--full` adds Stop with
declines and lost capabilities, two saves with every condition, `live-save-outdated`,
`live-save-other`, `live-save-faults` (with draft sync) and `live-save-stop`.

## Scope and bounds

- Bounds: MaxFaults 1, Stop at most once, MaxId 3 or 4, one or two Saves, and 2 answered preview polls (the real budget is 300).
- The default runs split the events (see `MODEL_RUNS`): every condition for safety across
  four runs; liveness for Save (cool or hot, and an unsupported recipe), Stop and
  kill/reaper/background with every condition, the reaper with nobody tapping, the gallery
  editor, and ClientServerAgree with Stop, kills and a lost capability.
- `--full` adds the heavier combinations (`FULL_RUNS`). Not run for liveness (past TLC's
  15-minute limit with the round-2 state): Save with a missing original (round 1 ran it), and two
  saves (this editor and another device). `live-save-stop` checks PreviewConverges no more
  (one answered poll keeps it under 7 minutes). The safety runs cover both.
- A chat poll that finds the ticket held also asks for a re-read; that read is the same as a
  later chat poll, which the model always allows, so only the editor's, the scheduled and the
  decline's skipped reads set `again`.
- Not modeled:
  - the rollout hold (`phone_rendering_paused`)
  - the GET's 409 superseded / cancelled / `editor_empty` and 426 answers
  - contract refusals (`contract_changed`)
  - output verification
  - the retry caps: 12 re-reads, 4 transient waits, 12 decline sends. MaxFaults 1 never reaches them.
  - the editor's "reconnecting" hint, shown after 3 failed reads
  - the planner (S4)
- The resume inside `apply` (`retryFailure` on a paused record) is one step in the model and may fail (`ResumeServer`); the code swallows the error, and the reservation clears the pause anyway.
- The phone's own clear of a stale `renderer_outdated` (S `apply`, needs_attention branch) serves records declined before `declined_client_protocol` existed; every model record carries it, so the server's auto-resume answers first.
- The editor starts from a failed capability read. With a good read, a later failed read does the same as the no-fault branch.
- The statecharts `statechart/editor-device.mmd` and `creation-rendering.mmd` show the fixed flow.

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

None of them excludes a condition any more. New in round 2, with teeth: NoFutileRepin.

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
