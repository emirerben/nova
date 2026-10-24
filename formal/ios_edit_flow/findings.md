# Phone-render findings (the "killer" screens)

Screens in scope:
- S1 "Kria couldn't prepare this edit on your iPhone"
- S2 "Couldn't refresh this edit"
- S3 "This edit needs your attention" / "Needs attention"
- S4 the "couldn't render" family (`phone_plan_unsupported`, "Kria couldn't render this approved edit")

Model: `PhoneRender.tla`. Run with `make verify-phone-render`. Code was read at `origin/main@ff27b0a65`.

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

## TLC counterexamples (rules the code breaks today)

1. **NoSpuriousAttention**, a 2-step trace.
   - The editor is open and a chat-refresh read fails once (offline or a 5xx).
   - "Couldn't refresh this edit" appears and stays: it has no button and no retry on error (`NativeEditorSession.swift:1856-1871`).
   - Same rule, phone side: any failed status GET puts up S1 (`DeviceRenderSessions.swift:170-187`).
   - This includes a `URLError(.cancelled)`, which the `CancellationError` check doesn't catch, and 409 superseded / 426 update-required answers.
2. **CloudDecisionReported**, a 4-step trace.
   - The phone is hot, low on storage, or the recipe needs a capability it lacks.
   - `reconcile` returns before `coordinator.start`, so the server stays `awaiting_device` and is never told.
   - The user sees needs-attention; the server waits for 24h until the reaper runs.
3. **AttentionHasWayOut**, a 17-step lasso.
   - The user taps Stop, then the capabilities fetch fails. `try? … ?? .disabled` turns that into "not available" (`NativeEditorSession.swift:810-814`).
   - From there, every Try again loops back to needs-attention, and no retry on screen ever publishes.
   - The same loop happens for runtime-v2 renders: `retry_requires_approval` sends Try again to chat approval, which 409s.
4. **ClientServerAgree**, a 13-step lasso.
   - The user taps Stop and the failure report is dropped (fire-and-forget, offline).
   - The phone says "stopped" while the server keeps waiting for this phone, forever until the reaper runs.
5. **PreviewConverges**, a 16-step lasso.
   - After a save, the preview poll waits on a fence that only a device reconcile clears.
   - If that reconcile fails or is skipped, the editor waits out 300 polls and then shows conflict copy.

## Rules the code keeps (with teeth)

Each rule fails when its guard is deleted:
- NoStalePublish
- PublishedStaysPublished
- SyncedMeansPublished
- NoStaleRequestReinstalled

## Fix order (ranked by user impact)

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
