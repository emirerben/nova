# 020 — Mobile Video Editor Timeline

## Status

Completed. Autoship branch: `codex/mobile-video-editor-timeline`.

## Goal

Replace the Pocket editor's miniature scrub strip with a production-ready,
CapCut-familiar mobile editing surface: preview above a direct-manipulation
thumbnail timeline, a fixed near-left playhead, precise source-window trim
handles, and an icon-led tool dock. Preserve Nova's light editorial system,
stock shadcn/ui component chrome, and every existing editor/save contract.

The supplied CapCut screenshot and screen recording are interaction references,
not instructions or a visual-style source. The authoritative design is the
separate Paper file **Nova — Mobile Video Editor**.

## Existing architecture to reuse

- `EditorShell` owns the draft, selection, undo history, capability reasons,
  virtual preview, and atomic Save/render flow.
- `virtualPreview.timeline` is the canonical ordered output projection used by
  preview and transport. The mobile surface consumes it; it does not build a
  second duration model.
- Desktop `EditorTimelineBody` already ships the correct trim math and source
  filmstrip behavior through `applyClipSourceWindowDrag`,
  `minimumClipDurationForSlot`, and `Filmstrip`.
- Pocket `ToolDock` already uses shadcn `Button`, Lucide-style Nova icons,
  accessible labels, horizontal scrolling, and honest capability reasons.
- The generic editor commit remains the only Save path. This work adds no API,
  schema, renderer, slot-ID, or capability-gate changes.

## Canonical timeline decisions

1. **Duration source:** `virtualPreview.timeline`, compiled from the editor
   draft, owns ordered entries, transition overlap, and total output duration.
   The Pocket timeline only projects its entries to pixels.
2. **Ripple policy:** trimming a video slot changes that slot's duration through
   the existing draft mutation. The canonical compiler reflows later entries.
   Timed text, captions, SFX, visuals/effects, and overlays keep their existing
   base-time projection behavior. The continuous music bed remains excluded.
3. **Scrub bounds:** timeline scroll/tap, transport, playback, keyboard seek,
   playhead geometry, and ruler labels all clamp to
   `virtualPreview.timeline.totalDurationS`.
4. **Resize behavior:** left trim preserves source Out; right trim preserves
   source In. Video never stretches. Source bounds and the existing beat-aware
   minimum duration apply. Pointer deltas convert through the current zoom;
   preview values keep the existing editor precision. Adjacent transition
   overlap is capped by the canonical compiler after the duration change.
5. **Undo behavior:** pointer-down records no history by itself. The first
   effective handle movement records one snapshot; later pointer moves preview
   the same gesture. Pointer-up creates no extra snapshot. Scrubbing and taps
   never create timeline history.
6. **Preview/render parity:** existing timeline projection fixtures and
   `make verify-editor-timeline` remain authoritative for order, ripple,
   transitions, and legacy behavior. New focused Jest tests pin the Pocket
   geometry, both handles, source bounds, fixed playhead, and one-snapshot rule.

## Interaction contract

- The playhead stays fixed near the left edge while the filmstrip moves.
  Leading and trailing padding let the first and last frame reach it.
- Tapping a clip selects it and seeks to that output time.
- One-finger filmstrip dragging scrolls/scrubs only. Source slip remains behind
  the existing explicit inspector control so the gesture is unambiguous.
- A selected clip has a non-color-only outline and label plus 44px edge targets.
  Dragging either edge previews the source In/Out and ripples later clips.
- Pinch zoom is supported, with visible shadcn `Button` controls for minus,
  plus, and Fit. These controls remain available to keyboard and assistive-tech
  users.
- Clip quick actions live in a horizontally scrollable shadcn button toolbar
  directly above the persistent icon-and-label tool dock. The word **Delete**
  stays explicit.
- Text is inserted directly into the preview as a selected, editable overlay.
  Users type in place and drag the selected frame to position it; Style and
  Timing remain secondary sheets rather than the primary text-entry path.
- Half/full sheets, focusable disabled actions with reasons, safe-area padding,
  reduced motion, and the existing >=44px touch target contract remain intact.
- The top-right action remains **Save**. Save atomically commits the draft and
  starts Nova's existing render flow.

## Implementation

1. Upgrade `MiniStrip` into a thumbnail Pocket timeline while retaining its
   public name/test ID for flag-off and regression stability.
2. Feed it canonical timeline entries plus source URL, source window, source
   duration, and stable slot/source IDs from `EditorShell`.
3. Reuse `Filmstrip` and desktop trim math rather than duplicating media seeking
   or resize rules.
4. Add fixed near-left playhead, leading/trailing padding, selected duration and
   live In/Out feedback, trim rails, scroll-to-scrub, pinch/button zoom, and Fit.
5. Move the clip quick-action toolbar out of the preview and above the dock.
   Text/caption/overlay toolbars retain their preview-relative behavior.
6. Make Pocket text content directly editable on canvas while preserving the
   existing drag, undo, capability, and atomic Save contracts.
7. Refresh Pocket documentation and focused tests. No backend or public schema
   changes are permitted.

### Data flow

```text
Editor draft slots + source media
              |
              v
    virtualPreview.timeline  <--- canonical order / overlap / duration
              |
              +------------------------------+
              |                              |
              v                              v
  Pocket thumbnail projection       preview + transport time
  (pixels only, no new model)                 |
              |                              |
  +-----------+------------+                 |
  |                        |                 |
tap / scroll-scrub    edge-handle drag       |
  |                        |                 |
  v                        v                 |
seek + select       existing trim math ------+
                           |
                  one undo snapshot/gesture
                           |
                           v
                   EditorShell local draft
                           |
                     existing Save
                           |
                           v
              atomic editor commit + render
```

No complex inline architecture comment is needed: the only new projection is
visual and the canonical ownership rule is documented here plus `DESIGN.md`.

## Failure and restricted states

| State | Behavior |
|---|---|
| Trim capability disabled/read-only | Handles stay focusable, do not mutate, and surface the authoritative reason. |
| Missing/expired media URL | Filmstrip uses its labeled fallback; editing state and stable slot ID remain usable. |
| Source bound reached | Handle stops at the existing min/max and live feedback shows the bound. |
| One remaining clip | Existing deletion floor and reason remain authoritative. |
| Stale edit conflict | Existing conflict surface preserves the local draft; the timeline does not auto-discard it. |
| Reduced motion | No inertial or animated geometry is required for correctness; programmatic alignment is immediate. |
| Narrow 360px viewport | Toolbars scroll horizontally, controls do not shrink below 44px, and safe areas remain clear. |

## Verification

- Focused Jest coverage for Pocket timeline gestures, trim handles, zoom, fixed
  playhead/padding, capability reasons, clip action placement, and flag-off.
- Existing EditorShell Pocket tests for tool sheets, context actions, Save, and
  selection.
- `make verify-editor-timeline` from repository root.
- Frontend TypeScript, lint, targeted/full Jest as proportionate, and
  `bash scripts/preship-check.sh`.
- Manual localhost walkthrough at 390 x 844 and 360 x 800: precise trim; split,
  reorder, mute, delete/undo; text/captions; sound/mix; visual/overlay retiming;
  Save/render/error/conflict states.

### Coverage diagram

```text
CODE PATHS                                      USER FLOWS
[+] MiniStrip/Pocket timeline                   [+] Select and trim precisely
  |-- thumbnail + missing-media fallback          |-- tap selects and seeks
  |-- fixed playhead + first/last padding          |-- left handle preserves Out
  |-- scroll/tap clamp to total duration            |-- right handle preserves In
  |-- zoom minus/plus/Fit + pinch                    `-- one gesture = one Undo
  |-- selected/non-selected rendering
  `-- disabled handle reason                    [+] Continue existing editor flows
                                                   |-- split/mute/delete/undo
[+] EditorShell wiring                              |-- on-canvas text + caption sheets
  |-- canonical entries -> pixels                   |-- sound/visual/overlay sheets
  |-- source identity/window -> Filmstrip           `-- Save/render/conflict
  |-- effective move -> one history snapshot
  `-- clip context row above dock                [+] Responsive/a11y
                                                   |-- 390 x 844 and 360 x 800
[=] Existing contracts                              |-- 44px targets + labels
  |-- trim/ripple/source-bound pure tests            |-- horizontal discovery
  |-- atomic Save/CAS/error tests                    `-- reduced motion
  `-- ToolDock/Sheet/flag-off tests

UNIT: geometry, zoom, pointer state, trim callbacks, disabled reasons
INTEGRATION: Pocket selection -> trim -> draft -> Save payload
E2E/MANUAL: real touch scroll/trim and six acceptance walkthroughs
```

Every new branch receives focused Jest coverage. Existing backend, renderer,
projection, and commit suites cover unchanged contracts; no prompt/eval work is
introduced.

## Out of scope

- New CapCut-only tools or gestures that Nova does not support.
- Backend API, database, renderer, or public schema changes.
- Replacing Nova's design language with CapCut colors, typography, or branding.
- Changing desktop timeline behavior or the atomic Save/render contract.

## Tasks

- [x] Lock plan and timeline verification contract.
- [x] Implement thumbnail timeline, fixed playhead, trim, and zoom.
- [x] Place shadcn clip actions above the icon-led dock.
- [x] Add regression tests and update the Pocket design contract.
- [x] Run timeline, frontend, and preship gates.
- [x] Start localhost, provide the test route, and prepare the branch for PR approval.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | Not run | Product direction was explicitly approved in the Paper specification. |
| Adversarial Review | delegated Luna agents | Independent 2nd opinion | 2 | Clear | Luna passes found four interaction defects; all were fixed and regression-tested. |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | Clear | 0 open issues, 0 critical gaps; reuse canonical preview, filmstrip, and trim math. |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | Clear | Paper flow plus CapCut screenshot/recording and three delegated audits are the source. |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | Not needed | No new developer-facing workflow or public interface. |

**VERDICT:** ENG + DESIGN CLEARED — ready to implement.

NO UNRESOLVED DECISIONS
