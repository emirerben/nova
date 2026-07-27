# 013 — Mobile UX baseline: make the phone a first-class surface

**Status:** IMPLEMENTED (F1–F5 landed on `claude/mobile-ux-plan-e7892b`)
**Owner:** web / light editorial system
**Flag:** none — CSS/layout only, no render-path or API change

## Outcome

All five fixes landed, plus one addition found during visual verification:
`WhatYouMakeStep`'s footage cards were `grid-cols-2` with no breakpoint, which
wrapped "Talking to camera" onto three lines at 390px — now
`grid-cols-1 sm:grid-cols-2` per §8.

**Overlap with PR #738.** While this was in progress, #738 shipped the same
class of fix for onboarding only (rail `hidden md:flex`, `px-5 py-6 md:px-12`,
`min-h-[100dvh]`, plus a static "Step n of 4" line). Reconciled by taking
origin/main as the merge base — every #738 change is preserved verbatim — and
re-applying the shared `<StepRail>` on top, dropping only #738's inline counter
as redundant with the strip's label + counter. The shared rail supersedes the
onboarding-only fix because it also covers the record flow (untouched by #738,
and still carrying both the `w-56` overflow and the `100vh` record-button bug)
and keeps tap-back to a done step on mobile.

**Measured at 390×844** (real Tailwind build, real class strings): horizontal
overflow 0px (was the bug), strip 45px tall vs 224px of stolen width, dot target
44×44, `text-sm` input lifted 14px→16px while a `text-lg` input correctly stays
18px. At 768px the rail returns at exactly 224px with `main` at 544px (= 768,
zero overflow) and dense inputs return to 14px — desktop is behaviourally
unchanged. Boundaries checked at 360/390/639/640/767/768/1280.

**Known gap:** 17 Jest suites (the `_editor/` tree) cannot load on this machine
— `@nova/motion-runtime` and `canvaskit-wasm` are declared in
`src/apps/web/package.json` but absent from the shared `node_modules`, which
predates them. Pre-existing and unrelated to this change; CI installs them, so
it will exercise those suites. F3's skeleton branch is therefore covered here
only by the source-level assertion in the guard test, not a render test.

## Problem

On a phone, `/plan` onboarding is unusable: a 224px step rail eats 57% of the
viewport and the question pane overflows off-screen (user screenshot, iPhone
Safari at 390pt). The rail is not the only instance — the same shell pattern is
copy-pasted into the record flow.

This is **not** a "design mobile from scratch" job. `DESIGN.md` §8 already
ratifies the mobile contract:

> - **Touch targets** ≥44px on mobile.
> - **Touch inputs:** inputs on touch viewports are ≥44px tall with ≥16px font
>   to prevent iOS Safari zoom-on-focus.
> - **Mobile-first:** single column default, `sm:`/`md:` enhance.
>
> | base | <640px | Phone-first single column; 44px touch targets; 16px focused inputs. |

Several surfaces never implemented that contract, and nothing enforces it, so
each new surface re-introduces the gap. The work is to close the specific
violations and add a guard.

## What already works — do NOT redo

Mobile quality here is *inconsistent*, not absent. These surfaces had a real
mobile pass; treat them as the reference patterns to copy:

| Surface | Pattern to reuse |
|---|---|
| `_editor/useEditorLayoutMode.ts` | `light` (<1024) / `overlay` / `full` matchMedia switch, SSR-safe via `useSyncExternalStore`. The heavy timeline never mounts on a phone. |
| `workspace/IdeasHome.tsx` | `min-[380px]:` column shift, `min-h-[44px]` targets, `[@media(hover:hover)]:` guards so hover-only affordances stay visible on touch. |
| `OverlayLane.tsx:627`, `OverlayCardPopover.tsx:188` | `(pointer: coarse)` branch for drag affordances. |
| `_editor/EditorShell.tsx:4662`, `CopilotDrawer.tsx:141` | `dvh` units, not `vh`. |
| `library/page.tsx:126` | `grid-cols-2 sm:grid-cols-3 lg:grid-cols-4`. |
| `layout.tsx` | `viewport` export is correct; zoom is not disabled (§8 compliant). |

Tap-target compliance is broadly fine — 41 non-admin files already use
`min-h-11`/`min-h-[44px]`. Tap targets are **not** a phase of this plan.

## Fixes

### F1 — Split-rail shells never collapse (the screenshot bug)

Two files, one defect:

- `src/apps/web/src/app/plan/_components/OnboardingShell.tsx:421` — outer
  `flex min-h-screen`; rail `aside` `flex w-56 shrink-0` (line 137); pane
  `main` `flex flex-1 items-start justify-center px-12 py-16` (line 424).
- `src/apps/web/src/app/plan/items/[id]/transcript/page.tsx:294` — identical
  shell; rail `aside` `flex w-56 shrink-0` (line 60); pane `main`
  `flex flex-1 flex-col px-8 py-10 sm:px-12` (line 296).

Why it overflows: `w-56` (224px) + `px-12` (96px) = 320px of fixed chrome on a
390px viewport, leaving 70px. `flex-1` is `flex: 1 1 0%` but flex items default
to `min-width: auto`, so `<main>` cannot shrink below its min-content width —
the row overflows the viewport instead, producing horizontal scroll and the
clipped heading in the screenshot. 224/390 = 57%, matching the rail's measured
share exactly.

Fix — one shared component, both callers:

1. Extract the duplicated `StepRail` into
   `src/apps/web/src/app/plan/_components/ui/StepRail.tsx`, generic over its
   step list (labels, done/active/skipped state, `onGoBack`). This is the
   altitude fix: the defect exists twice because the component does.
2. Below `md`: render steps as a horizontal strip beneath the header —
   `flex-row` dots with the active label visible, upcoming labels
   `sr-only`/truncated — so progress stays legible without stealing width.
   At `md+`: today's docked `w-56` rail, unchanged.
3. Outer container `flex-col md:flex-row`.
4. Add `min-w-0` to both `<main>` elements (kills the residual overflow even if
   a future child has a wide min-content).
5. Pane padding `px-5 py-10 md:px-12 md:py-16`.

Keep `max-w-lg` on the inner column — with `w-full` it already degrades
correctly once the parent can shrink.

### F2 — iOS Safari zoom-on-focus (24 form controls)

Focusing any input with a computed font-size below 16px makes iOS Safari zoom
the viewport and leave it zoomed — the single most-felt "this site is broken on
my phone" symptom. 24 non-admin `<input>`/`<textarea>`/`<select>` elements are
below the floor (11 × `text-sm`, 4 × `text-[13px]`, 3 × `text-xs`,
3 × `text-[12px]`, 3 × `text-[11px]`). Direct violation of `DESIGN.md` §8:203.

Fix at the mechanism, not 24 call sites — `src/apps/web/src/app/globals.css`:

```css
/* DESIGN.md §8: base tier form controls never trip iOS zoom-on-focus. */
@media (max-width: 639px) {
  input:not([type="checkbox"]):not([type="radio"]),
  textarea,
  select {
    font-size: 16px;
  }
}
```

`globals.css` currently has **zero** width-based media queries — this
establishes the base tier the §8 table already documents. Visual review needed
on the dense editor inputs (`EditorShell.tsx:4473` is a `w-[240px]` title
field) since 12px→16px changes their intrinsic height; expect to widen or
re-wrap two or three of them.

### F3 — Editor loading skeleton ignores its own layout mode

`src/apps/web/src/app/plan/items/[id]/_editor/EditorShell.tsx:4185`

```
<div className="grid min-h-0 flex-1 grid-cols-[92px_1fr_320px_72px]">
```

92 + 320 + 72 = 484px of fixed columns on a 390px viewport → the `1fr` canvas
column resolves to zero and the skeleton overflows. The *loaded* editor is
fine (it branches on `layoutMode`), but per the hook's own docstring the real
editor only renders after the async variant load — so **the broken skeleton is
what every phone user sees first**, and the 260px timeline block below it
(line 4193) is dead weight in light mode.

`layoutMode` is already in scope: it is read at line 789 inside the same
`EditorShell` component (declared line 547) that returns this branch. Fix is a
branch, not new plumbing:

```tsx
if (loading) {
  return layoutMode === "light" ? <LightLoadingSkeleton /> : <FullLoadingSkeleton />;
}
```

The light skeleton is canvas placeholder + transport bar only — mirror the
light-mode grid at line 4423 (`56px minmax(0, 1fr) clamp(220px, 30dvh, 260px)`).

### F4 — `100vh` cuts the record controls off-screen

`src/apps/web/src/app/plan/items/[id]/transcript/page.tsx:265`

```
<div className="h-[calc(100vh-8rem)]">   /* wraps TeleprompterRecorder */
```

On mobile Safari/Chrome `100vh` is the viewport *ignoring* browser chrome, so
the recorder box extends beneath the toolbar and the 44px record targets
(`DESIGN.md` §13) can land off-screen. Recording is the one flow that is
*inherently* phone-only — this is the highest-severity functional bug after F1.

Fix: `h-[calc(100dvh-8rem)]`. The editor already standardized on `dvh`
(`EditorShell.tsx:4662`, `CopilotDrawer.tsx:141`) — this is following an
existing decision, not introducing one.

Audit the other `min-h-screen` users in the same pass; they are non-scrolling
page wrappers where `min-h-screen` is harmless, so leave them unless the
`dvh` sweep is free.

### F5 — Nothing enforces §8, so the gap keeps coming back

`OnboardingShell.tsx` (460 loc), `transcript/page.tsx` (687 loc) and the whole
`_editor/` tree (~12k loc) contain **zero** `sm:`/`md:`/`lg:`/`xl:` prefixes.
The editor gets away with it via the matchMedia hook; the other two do not.
No test asserts any responsive behavior.

Fix — a Jest guard, `src/apps/web/src/__tests__/plan/mobile-shell.test.tsx`.
Existing tests already mock `matchMedia` (`transitions-motion.test.tsx`,
`plan-instant-editor.test.tsx`), so the harness is idiomatic:

1. Render `OnboardingShell` and the transcript shell; assert the rail element
   carries a `md:`-gated width class and is **not** unconditionally `w-56`.
2. Assert each `<main>` has `min-w-0`.
3. `resolveLayoutMode(false, false) === "light"` (pins the existing hook).
4. A source-level assertion that no non-admin `<input>`/`<textarea>` declares a
   sub-16px font class without the global override in place — or, cheaper and
   less brittle, assert the `globals.css` base-tier block exists.

Then add one line to `DESIGN.md` §8 pointing at the guard test, so the contract
names its own enforcement.

## Verification

1. `cd src/apps/web && npx tsc --noEmit && npm run lint && npm test`
2. `bash scripts/preship-check.sh`
3. Manual pass at 390 × 844 (iPhone 14) and 360 × 800 (small Android) on:
   `/plan` onboarding steps 1–4 · `/plan` workspace · `/plan/items/[id]` ·
   `/plan/items/[id]/transcript` steps 1–4 · `/library` · `/generative` · `/`
   For each: **zero horizontal scroll** (`document.scrollingElement.scrollWidth
   <= innerWidth`), and no viewport zoom after focusing every text field.
4. Real-device check on the record flow — the record button must be reachable
   with the browser toolbar visible (F4 regresses only on real chrome, not in
   devtools emulation).

## Out of scope

- `/admin/*` and `/dev-qa/*` — desktop-only by design, excluded from every grep
  in this plan.
- `/architecture` (`h-screen w-screen` canvas viz) — desktop tool.
- `/template-jobs` dark surface — 687 loc, 0 breakpoints, but it is a
  centred single-column status page; it degrades acceptably. Separate ticket if
  the mobile pass shows otherwise.
- Rebuilding the editor timeline for touch. The `light` mode decision (§8 table,
  D12) deliberately withholds it below 1024px. Revisit only with a product call.
- Tap-target sweep — already broadly compliant (see "What already works").

## Sequencing

F1 and F4 are the user-visible breakage and are independent — ship them first,
together. F2 is one CSS block plus a visual re-check of dense inputs. F3 is
isolated to one component. F5 lands last, guarding what the first four fixed.
