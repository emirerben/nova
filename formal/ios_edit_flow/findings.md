# Phone-render findings (the "killer" screens)

Screens in scope:
- S1 "Kria couldn't prepare this edit on your iPhone"
- S2 "Couldn't refresh this edit"
- S3 "This edit needs your attention" / "Needs attention"
- S4 the "couldn't render" family (`phone_plan_unsupported`, "Kria couldn't render this approved edit")

Models:
- `PhoneRender.tla`: the code on `codex/killer-integration` (the fixes). Synced commit in `coverage.json`.
- `PhoneRenderBefore.tla`: the code at `origin/main@ff27b0a65`, before the fixes. `verify.py` proves it broke each rule the fixes made pass.

Run with `make verify-phone-render` (about 3 minutes). `--full` adds the heavier event combinations (`FULL_RUNS` in `verify.py`).

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
- **Review in chat leads to a renderable recipe.** (Dropped in round 3: a chat change may keep
  the feature, see Round 3 #8.)
- **An app update is a newer protocol.** `UpdateApp` clears `outdated`; the server compares
  `X-Kria-Client-Protocol` with the declining one. A release that renders a newer recipe without
  bumping the protocol would get `requires_update` again (open risk, not modeled).
- `UpdateApp` and `ChatChange` are bounded like the other pinning events (`sId < MaxId`).

New runs: `one-save-declines`, `lost-capability`, `live-save-unsupported`, `live-faults` (kill,
reaper, background, so the reaper's timed_out appears), `live-reaped-no-user`,
`live-gallery-editor`, `live-lost-capability`, `agree-lost-capability`; `--full` adds Stop with
declines and lost capabilities, two saves with every condition, `live-save-outdated`,
`live-save-other`, `live-save-faults` (with draft sync) and `live-save-stop`.

### Round 3 (an adversarial check of round 2 found nine more)

Each fix has its own tooth (one mutation, run alone). "Unnecessary" means the mutation is not
caught in any scenario and the reason is given.

| # | What was wrong | What changed in the code | Model | Tooth (rule) |
|---|---|---|---|---|
| 1 | The gallery editor (no conversation under it) has "Review in chat" call `requestBack()`, which only reaches the video detail: no chat, no render card. A TLC trace with `Chat = {FALSE}` + unsupported kept "needs a change" up forever. | `NativeEditorView.reviewRetryInChat`: with no conversation it calls `AppModel.openCreationThread(session.creationThreadID)` (open-in-editor names the creation thread). The model selects that project (fetching it when not listed) and posts `chatRouteRequest`; `ChatWorkspaceView` closes the gallery (and the editor it presented) onto the conversation, or opens the projects drawer when the thread is unknown. | `OpenChat` (user-fair): `chat' = TRUE`, the editor closes. `Reopen` lands on the chat workspace (KriaApp's root). New run `live-gallery-declines` (Chat FALSE, unsupported + outdated, Save). | `gallery-review-goes-back` (AttentionHasWayOut) |
| 2a | A capability lost mid-render: the decline was queued and shown while the coordinator kept exporting; the export published, the server refused the decline. | `apply`: `.decline where resumesSync \|\| inFlight`, so a running export of this request finishes and uploads (the server checks bytes, not capabilities), and no "needs a change" shows for a video on its way. | `LoseCaps` fires mid-render too (`coord.run`). | `decline-while-exporting` (AttentionHasWayOut) |
| 2b | The model then found the rest of the checker's trace: a read answered "awaiting" before the publish is applied after it, declines a published video ("needs a change"), the creator changes it in chat, and a stale "published" read settles the card while the change waits. | `resumesSync` counts a `synced` receipt of the same request. The phone cannot know about a pin it has not read; what it has read is fenced by the ticket (NoStaleRequestReinstalled), and with 2a+2b no card asks for a change while its video publishes, which is what made the stale read harmful. | `resumesSync` includes `synced`. | `stale-read-declines-synced` (AttentionHasWayOut) |
| 3a | `renderer_outdated` was raised for asset-manifest value faults (`RenderAssetError.invalidManifest`, e.g. `byteCount == 0`, RenderAssets.swift:35): "Update Kria" that no update fixes. | KriaMediaEngine: `RenderAssetError.newerManifest` for an unknown key, an unknown asset kind or a newer manifest version. `DeviceRenderRecipeUnreadable`: only `DecodingError`, `newerManifest` and `RecipeError.unsupportedSchema` are outdated; a value fault is `unsupported_recipe` ("This edit needs a change", Review in chat: a re-plan rebuilds the manifest; a retry would re-pin the same bytes). | Below the model's abstraction (it has no manifests). | none: unit test `testOnlyAVersionGapInTheAssetManifestIsOutdated` |
| 3b | `_auto_resume_updated` re-pinned for any newer protocol, before the 426 feature gates ran: a newer but still insufficient build was re-pinned and declined again. | `routes/device_render.py`: every protocol gate is one function, `_client_update_detail` (export_verification 13, text_preferences 10, drawing 6, text_bend 9, freeze_frame 16, single_word_text 11). The GET runs it before any automatic re-pin; a build that fails it gets the 426 and nothing is re-pinned. The failure route records `declined_gap` (`floor` when the decliner failed those gates, else `renderer`); only a floor gap is auto re-pinned. A renderer/schema gap the server cannot see is the phone's call (3c). | Builds as levels (`lvl`, `need`, OLD = 1); `kind` floor/schema; the floor GET answers 426 naming the request; `UpdateApp` may land on a newer but insufficient build once; `RecFetch` checks `Floors` first. | `floors-checked-after-repin` (NoFutileRepin); `auto-resume-updated-removed` (AttentionHasWayOut under `SpecUpdate`: after "Update Kria" nobody taps) |
| 4 | Two devices: an old build's `renderer_outdated` failed a record a newer build was rendering; the newer one re-pinned it, the old one declined again, with no bound. | The GET remembers the newest protocol that read the recipe without a 426 (`max_reader_client_protocol`), kept across re-pins of the same recipe. `/failures` answers 409 `held_by_newer_build` to a `renderer_outdated` from an older build, or while the record is syncing. Bound: each auto re-pin needs a build newer than the decliner, and a decline is accepted only from a build at least as new as the newest reader, so re-pins need strictly newer builds. | `OldDecline` / `OldRetry` (event `old`), `sRead`, `HeldDecline`; new rule NoRepinLoop (`autoRepins <= 1`). | `old-decline-not-held` (NoRepinLoop) |
| 5 | "Update Kria" went to `https://apps.apple.com/` (the store's home page) in the card, the editor banner and the native update screen; Kria ships through TestFlight. | One constant, `KriaUpdateLink` (Core/KriaUpdateLink.swift): `itms-beta://` when `canOpenURL` sees it (`LSApplicationQueriesSchemes` in Info.plist), else TestFlight's App Store page. The update screens also offer "Review in chat". | `ChatChange`/`OpenChat` are enabled on the update screen. | none (a URL): unit test `testUpdateKriaOpensTestFlightOrItsAppStorePage` |
| 6a | The 426 path: `.updateRequired` returned early when the phone held a finished video, without comparing the request, so a 426 about a newer request was never reported. A 426 about a published request read as a failure. | The 426 detail names the request (`identity`, `phase`, `minimum_client_protocol`). `readFailed(.updateRequired(target))`: published stays synced; a finished video of the same request stays; any other request shows "Update Kria" and its decline is sent for that identity. | `RecFetch` answers `upd` (426) with the request; `Apply` mirrors the three branches; ghost `upd426`; new rule UpdateTold; event `feature` (another device's save needs a newer build). | `update-required-ignores-identity` (UpdateTold) |
| 6b | The phone's own clear (`.local` gate on a `renderer_outdated` record) showed "Try again", `/retry` answered `requires_update`, the next read showed "Try again" again. | `DeviceRetryBody.renders_locally`: the phone sends it when its gate renders the pinned recipe (`rendersLocally`), and the server re-pins instead of `requires_update`. The card reads "Ready to render on this iPhone" (`renderable_now`), the editor "Render on this iPhone" (`NextStep.renderNow`). | `Asserts`, `RetryOK`, cause `renderable` (Screen `renderable`, editor `renderNow`), event `rollback`. | `retry-ignores-renders-locally` (AttentionHasWayOut) |
| 7 | `needsChange()` ignored the phase: a kept video showed "Review the updated edit" with "Retry sync" under "Ready on this iPhone"; a kept video with `renderer_outdated` had no "Update Kria". | `DeviceRenderAttentionCopy.yourTurnPhases` (needsAttention, localReady); `needsChange` / new `needsUpdate` use it; `showsRetryButton` hides "Retry sync" for structural reasons on a kept video; the card's detail says what to do with the kept video. | `NeedsChange` / `NeedsUpdate` / `ShowsRetry` / `Screen` over `YourTurnPh`; `Stuck` also covers any card on a needs_attention record (stronger); `newschema` condition + `feature` reaches the kept video with a schema gap. | `kept-video-no-update` (AttentionHasWayOut) |
| 8 | Model fidelity: `ChatChange` set `unsupported' = FALSE` unconditionally. | (code unchanged) | A chat change may keep the feature this iPhone lacks or the newer one it can't decode (`unsupported' \in {FALSE, unsupported}`, `need' \in {need, lvl}`); the phone then declines again and the same way out is there. The rule is a way out, not a fix. | n/a |
| 9 | Round 2's bundled teeth hid that several fixes had none. | (see each) | Split, one mutation each. | `server-requires-update-removed`, `server-requires-change-removed` (NoFutileRepin, with an older device's Try again: older builds in the field are what these server checks stop); `settled-counts-superseded` (new rule CapableRenders: with the chat open, nobody tapping, a capable phone's waiting request gets rendered); `quiet-wait-hides-change` (NoSpuriousAttention, safety run); `auto-resume-updated-removed` (above) |

Unnecessary (not a tooth, not caught in any scenario): the editor's `retryDeviceRender` filter
(`~YourTurn` in `EdRetry`). With it removed, a stale "Retry device render" tap on a change or
update card either calls `/retry`, which the server refuses (`requires_change` /
`requires_update`, both toothed), or re-reads locally and declines the same way. It saves a round
trip and a misleading message; no rule depends on it.

Rules added in round 3: UpdateTold, NoRepinLoop (safety), CapableRenders (liveness, `Spec`). The
`Stuck` helper now also covers any card on a needs_attention record (a kept video included).

Open risks after round 3:
- A schema/renderer gap the server cannot see is never re-pinned automatically; after an update the
  creator taps "Try again" once (`renders_locally`). The model checks that path (`live-save-outdated`).
- The model's `need` stands for the whole recipe's protocol floor; the six real gates are one
  function (`_client_update_detail`) the GET and the failure route share.
- `max_reader_client_protocol` survives re-pins of the same recipe: if the newest device is
  uninstalled, an older device's decline stays held (its card keeps "Update Kria", which is its
  real way forward) and the record waits for the reaper.
- `openCreationThread` falls back to the projects drawer when the edit has no creation thread
  (open-in-editor returns none); phone renders always come from a thread, so the model assumes one.

### Round 4 (an adversarial check of round 3; the last round)

| # | What was wrong | What changed in the code | Model | Tooth (rule) |
|---|---|---|---|---|
| 1 | Common. After a newer request was declined, the needs_attention read showed "Ready on this iPhone" (with "Update Kria to sync it" / "review the edit in chat") because the receipt on disk held an earlier request's export, cancelled when the newer one arrived: nothing to watch for this request, nothing synced. | `DeviceRenderSessions.apply` (needs_attention): `localReady` (and the old-server pause check) only when `saved?.request == request && saved?.outputURL != nil`. KriaTests `testNeedsAttentionForANewerRequestNeverShowsAnEarlierRequestsVideo`. | `ph \|-> IF c1.file /\ c1.id = id`; new rule KeptIsThisRequest (a kept-video card with a change/update reason is about `coord.id = req`). | `kept-video-other-request` (KeptIsThisRequest) |
| 2 | Deploy order: `DeviceRenderRetryBody` always encoded `renders_locally`; the API on main (`DeviceRetryBody`, extra='forbid') answers 422, so every Try again and Resume of a new build failed against it. | Custom `encode(to:)`: the key only when true (the API defaults it to false). Contract test: an ordinary retry sends only `identity`; `rendersLocally: true` sends `renders_locally: true`. Runbook: deploy the API (Fly) before the TestFlight build. | Below the model (wire format). | none: contract test `testFailureAndRetryBodiesUseServerExpectedKeys` |
| 3 | "Update Kria" on a build that is not outdated: `renderable_now` replaced `renderer_outdated` only for a `.local` gate, so a missing capability or a hot phone kept "Update Kria". Also `/retry` answering `requires_update` on a build whose gate has no version gap but lacks a capability (lost since) landed on "Update Kria". | needs_attention branch: whenever the gate reports no version gap, `.local` -> `renderable_now`, `.decline(unsupported_recipe)` -> "This edit needs a change", `.wait(cooling/storage)` -> the wait with a re-check (`.wait(.checking)`, capabilities unknown, keeps the server reason). `retryNeedsAttention`: `requires_update` maps to the gate's non-version decline when there is one (`nonVersionDecline`). KriaTests 27-29. | needs_attention: `reason` by gate (unsupported / renderable), a `wait` branch; the requires_update refusal uses `Gate`. New rule UpdateMeansOutdated (`NeedsUpdate` on the card of the server's request => `Outdated`). | `no-renderable-now` (round-3 M19), `outdated-kept-for-unsupported`, `requires-update-ignores-gate` (UpdateMeansOutdated). The wait branch is unreachable in the model (`hot` excludes a version gap at the start); unit test only. |
| 4 | Server: a GET that renewed a repair grant saved a stale record copy over the one `_note_reader_protocol` had just written, dropping `max_reader_client_protocol`; an older build's decline was then accepted instead of held. | `_device_render_status` re-reads the record after a note. Postgres test `test_a_repair_renewal_keeps_the_newest_reader_it_just_noted` (fails before the fix). | The model has no repair grants. | none: Postgres test |
| 5 | Gallery "Review in chat" with unsaved changes still went back to the video detail; and a conversation whose current job is not this video's never polled this video's record. | `NativeEditorView`: "Review in chat" shows the same "Save your changes before leaving?" prompt; Save and exit / Discard then open the thread (`exitsToChat`, `leave()`). `openCreationThread(_:deviceRender:)` passes the editor's `DeviceRenderKey`; `AppModel.chatDeviceRenderFocus`. The conversation shows and polls that key's own `DeviceRenderPanel` above the composer when it is not the thread's current job, until it settles or the creator sends a message (a change makes a new job, whose card the stage shows). | Matches `OpenChat` (`chat' = TRUE`: the chat polls this key) more closely than before. | n/a (round-3 `gallery-review-goes-back` still holds) |
| 6 | `verify.py --full` could not pass: `builds-stop` ran past 30 minutes; a TLC timeout crashed the script. | `builds-stop` split into `builds-stop-feature-save`, `builds-stop-rollback-save`, `builds-stop-no-save` (old, feature, rollback, Stop) and `builds-old-save` (each 17 s to about 9 minutes under load); an older device with Save and Stop together (past 120M states at 20 minutes) is not run; a timeout is a reported `FAIL model, <run>: TLC TIMED OUT` line (the other runs still run), exit 1. `TLC_TIMEOUT` (default 900 s). Output is line-buffered. | n/a | n/a |

Unnecessary after round 4: round 3's tooth `kept-video-no-update` (`NeedsUpdate` only on
`attention`) is no longer caught in any scenario, and was removed. Its trace was this round's
#1: a kept video of an earlier request on a newer, declined request. With #1 a "Ready on this
iPhone" card is always this request's own finished video, which uploads (`resumesSync`) and is
never declined, so no kept video carries `renderer_outdated`. The invariant
`~(pres.ph = "localReady" /\ pres.cause = "outdated")` holds in `builds`, `builds-caps` and
{none, outdated, schema, newschema} x {save, kill, reaper, feature, old}. The code keeps
`yourTurnPhases` (harmless; a server from before `held_by_newer_build` could still send one).

Known open (rare):
- An older device's own-gate decline (renderer/schema gap) records `declined_gap=renderer`, so nothing re-pins it automatically; the capable phone shows "Ready to render on this iPhone" and needs one tap (the model's `OldDecline` always reports through a 426).
- Another device's `unsupported_recipe` (a capability it lacks) fails a record a capable phone could render; `held_by_newer_build` covers only `renderer_outdated`. That phone reads "This edit needs a change".
- Teeth missing (mutation not caught in any run): M9 (held without the syncing clause) and M11 (no reader carry across re-pins) need a second uploader or three builds; M12 (noting a reader on a 426); M4 (a 426 about a published request) is unreachable (no other device publishes); M6 (the editor's retry filter) is unnecessary (Round 3).
- `DeclDeliver` reconciles after every answer; `sendDecline` reconciles only when the decline is accepted. Checked harmless in live-declines, live-capable-renders, live-lost-capability and live-new-schema (scratch e1.tla).
- After "Update Kria" the relaunched app selects `projects.first`; the auto re-pin runs when something GETs that job, so "no further tap" holds only once the creator opens that project (or its gallery video).
- `openCreationThread` falls back to the projects drawer when open-in-editor returns no creation thread.

## Scope and bounds

- Bounds: MaxFaults 1, Stop at most once, MaxId 3 or 4, one or two Saves, and 2 answered preview polls (the real budget is 300).
- The default runs split the events (see `MODEL_RUNS`): every condition for safety across
  five runs (round 3 adds `builds`: an older device, a newer device's feature, a rollback);
  liveness for Save (cool or hot), the declines with nobody saving, Stop (no decline),
  kill/reaper/background with every condition, the reaper with nobody tapping, the gallery
  editor (and with declines), the update with no further tap, the rollback, a later schema
  gap, CapableRenders, and ClientServerAgree with Stop, kills and a lost capability. Save with
  a decline and Stop with the declines moved to `--full` (the round-3 state made them 60 s and
  100 s); the default run takes about 3 minutes.
- `--full` adds the heavier combinations (`FULL_RUNS`, including the four `builds-*` splits of the old `builds-stop`, `live-save-unsupported`, `live-stop-declines`). A run past `TLC_TIMEOUT` (900 s) prints a FAIL line and the script exits 1. Round 4 adds the default run `builds-caps` (rollback, lost capability, Save, a newer feature with every outdated condition and heat). Not run for liveness (past TLC's
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

None of them excludes a condition any more. New in round 2, with teeth: NoFutileRepin. New in round 3, with teeth: UpdateTold, NoRepinLoop, CapableRenders. New in round 4, with teeth: KeptIsThisRequest, UpdateMeansOutdated.

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

## App-update recovery review, 28 October 2026

PR #1907 restores a moved completed MP4 after a checksum match. Lost or changed
bytes and pre-existing reservation collisions use a fresh upload attempt. These
operations retain the same edit request. In the lifecycle model, `coord.file`
means an intact usable export, and request identity abstracts upload attempts.
The new file-path and byte-identity rules are covered by concrete coordinator
regressions, not by additional claims about this model.

The reviewed drift in `recover`, `retrySync`, `perform`, `performOnce`, and the
publisher preserves the existing prepare, local-ready, sync, and publish phases.
Two inherited changes were reviewed too. `runPromptRevisionSync` follows selected
mutation authority for legacy threads while preserving the model's quiet refresh
failure behavior. `_client_update_detail` adds another protocol-floor case, which
has the same `Floors` and update-required behavior already represented here.
No invariant or transition was weakened. The source hashes were refreshed after
that review. This records the existing abstraction's limits rather than asserting
that it proves relocated file paths or immutable exported bytes.

The TLC checksum pin now matches GitHub's official v1.8.0 release-asset digest,
`fc6878c603f0fdc18b9fe3e5bf51860cb90ab5b1154e3c371660c036d6bdd63e`.
The archive integrity check remains mandatory.
