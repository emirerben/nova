# Plan 027 — KRI-508: edit text and titles right on the video (iPhone editor)

**Linear:** [KRI-508](https://linear.app/kria/issue/KRI-508) · **Status:** Phase 1 built on `ybyesilyurt/kri-508-enable-direct-text-and-title-editing-on-the-video-screen` (see §5); D13–D15 not built · **Owner:** Yasin Berk · **Label:** iOS
**Planned at:** `46f08e505` (origin/main, 2026-10-07) · **Mockup:** https://claude.ai/artifact/6Y5YtkeMj5NpmosLxjMgw5 (private until shared)

## 1. Problem (verbatim from KRI-508)

> Titles are available in the timeline and through text editing, but users cannot directly click text on the video screen to edit its content or size. Make text and titles easy to edit directly on the video screen and improve the text and title editing experience.

Source: Slack, 2026-10-07: "you cannot directly click and edit the text and size on the video screen. The title and text should be easy to edit, and the user experience for text and title editing should be great."

## 2. Code facts (origin/main `46f08e505`)

Paths are under `src/apps/ios/Kria/`. The surprise: **direct manipulation already exists, it is just undiscoverable, and words can't be edited from the preview at all.**

* **Preview hit-testing exists.** Text is hit-testable only while the playhead is inside its window (`Features/NativeEditorMediaViews.swift:681-683`, half-open `isVisible` in `Core/NativeEditorInteraction.swift:119`). Hit box = renderer-measured bounds padded to 44pt (`Core/NativeEditorInteraction.swift:192-205`). Tap picks the topmost text and repeated taps cycle overlaps (`selectPreviewObject`, `NativeEditorMediaViews.swift:291-318`).
* **Two taps to reach the panel, and it opens on the wrong tab.** First tap selects and shows the island strip "Edit text / Delete / Deselect" (`Features/NativeEditorComponents.swift:407-458`, sage accent). Second tap opens the Text panel (`routeSelection`, `Features/NativeEditorView.swift:719-725`) on **Style** whenever it came from the preview, timeline or strip (`initialTab: textEditOrigin == .list ? .edit : .style`, `NativeEditorView.swift:576`). No keyboard.
* **Words are typed only in a panel text box** (`NativeExplicitLineTextEditor`, `Features/NativeEditorTextPanel.swift:500-564`); with the keyboard up the preview shrinks to `typingPreviewHeight = 120` (`Features/NativeEditorIsland.swift:56`).
* **Drag / corner / pinch already work** on editable text: drag moves (≥4pt), the 22pt sky corner dot scales + rotates with 90° snap, pinch scales; drag pauses playback (`NativeEditorMediaViews.swift:342-553`; `NativeTextTransformLayer.swift:42-182`; `beginDirectManipulation`, `NativeEditorSession.swift:1199`). Live 60fps feedback uses the split frame (below / text / above layers, `NativeEditorSession.swift:200-230`). No guide lines (haptics only), no two-finger twist (slide posts have it), handle not inset into the canvas.
* **The preview is small.** Default browse preview ≈258pt tall on iPhone 15 (≈145pt wide at 9:16). A title is ~15pt tall on screen. `maxPreviewScreenFraction = 0.6` (`NativeEditorIsland.swift:60`).
* **Empty-preview tap opens fullscreen** (auto-plays) unless a clip is selected (`NativeEditorMediaViews.swift:385-392`).
* **Size model:** `size_px` (fallback `size_class`), new text 72; scaling multiplies size and `max_width_frac` together, min 8, **no max** (`NativeEditorSession.swift:3237-3249, 3289-3294`). Position `x_frac`/`y_frac` (named `position` until first drag), `rotation_deg`, `alignment` (`Core/NativeEditorDocument.swift:216-232`).
* **Locked text is invisible to taps.** `source_params.read_only` rows are excluded from hit-test, timeline, list and mutations (`NativeEditorSession.swift:1263-1270`, `:3322-3324`); Delete says "This title can't be edited here yet." (`:3974`). Server sets `read_only` only for phone Narrated titles while `phone_narrated_title_edits_supported()` is off (`src/apps/api/app/routes/generative_jobs.py:6790-6831`).
* **Captions are separate:** `caption_cue`-tagged elements route to the Captions panel (`NativeEditorView.swift:712-717`); KRI-240 (plan 026) owns the one-tap caption edit bar.
* **Slide posts already do "tap = edit with keyboard"** (`SlidePostInteractions.swift:27-36, 97-110`; `SlidePostWorkspaceView.swift:515-525`), hold 0.5s = select, pinch + twist rotate, handle inset 13. Both surfaces share the `NativeTextEditing` protocol (`Features/NativeTextEditing.swift:16`).
* **Web parity reference:** the web pocket editor already selects on canvas, corner-scales, and edits selected text in place (`src/apps/web/src/app/plan/items/[id]/_editor/EditorCanvas.tsx:1-17, 931-949`).
* **No face avoidance in the iOS editor** (no references); product rule: text never covers faces.
* Undo: 100 steps, one per gesture/typing transaction (`beginTransaction`/`endTransaction`, `NativeEditorSession.swift:1165-1174`). Save is manual (header Save).

## 3. Design (auto-decided under Yasin's "don't ask, make the best decisions", 2026-10-07)

Every decision below was made by the design review without per-issue questions, at Yasin's explicit instruction. They are proposals for cofounder review via the mockup; reopen any of them there.

### Screen structure

```
 BROWSE (today's layout, unchanged)     TEXT FOCUS (after one tap on text)     TYPING (after a second tap)
 ┌───────────────────────────┐          ┌───────────────────────────┐         ┌───────────────────────────┐
 │ ‹  Lisbon weekend  ⤴ Save │          │ ‹  Lisbon weekend  ⤴ Save │         │      (status bar only)    │
 │ [ Chat |  Editor ]        │          │ [ Chat |  Editor ]        │         │  A  ┌─────────────┐       │
 │      ┌──────┐             │          │   [Edit text|Style|Delete]│         │  │  │ Lisbon in 48│       │
 │      │ video│ 258pt       │          │    ┌───────────────┐      │         │  ●  │  hours|     │ 333pt │
 │      │ ┆T┆  │ (dashed     │          │    │ ┏━━━━━━━━━━┓  │ 444pt│         │  │  │ (dimmed)    │       │
 │      └──────┘  when paused)│          │    │ ┃ Lisbon in┃  │      │         │  a  └─────────────┘       │
 │ ── grabber ──             │          │    │ ┗━━━━━━━━━━◉  │      │         │ 104                       │
 │ ▶ 0:01.2/0:15   + ↶ ↷     │          │    └───────────────┘      │         ├───────────────────────────┤
 │ TEXT  [Lisbon…] [Alfama]  │          │ ▶ 0:01.2/0:15     ↶ ↷     │         │ [Playfair][Bebas][Mont…] Done│
 │ VIDEO ▤▤▤▤ ▤▤▤            │          │ TEXT  [Lisbon… sky]       │         │ ○ ● ○ ○ ○ │ Simple │ ≡  │
 │ MUSIC ~~~~~~~~            │          │ VIDEO ▤▤▤▤ ▤▤▤            │         ├───────────────────────────┤
 │ (Text Captions Visuals …) │          │ (Text Captions Visuals …) │         │         keyboard          │
 └───────────────────────────┘          └───────────────────────────┘         └───────────────────────────┘
```

Attention order in Text Focus: 1 the text and its box (what you're changing), 2 the action pill (what you can do), 3 the highlighted TEXT bar (when it shows). In Typing: 1 the caret on the video, 2 font/colour rows, 3 Done.

### Decisions

* **D1 — Gesture model: select first, then type.** One tap on visible editable text selects it. Tap the selected text again, double-tap, or press **Edit text** to type on the video. Drag moves at any time (today's behaviour); corner handle or pinch resizes the selected text. *Why:* the editor has a timeline, Delete and Style that need a selected object (CapCut convention, web pocket parity). Rejected: Instagram-style "tap = keyboard" (hides resizing, throws a keyboard over the video when you meant to move it); keeping the bottom panel (the complaint).
* **D2 — Text Focus.** When a tap selects text, the preview grows to the 0.6 screen cap (444pt on iPhone 15; 9:16 ⇒ 250pt wide, ~1.7×) after the touch ends, the timeline collapses to the controls row + TEXT + VIDEO lanes, the resize grabber hides. Deselect restores the creator's previous split. 220ms ease, instant under Reduce Motion. Growth never happens mid-gesture (a drag that starts on unselected text grows on release).
* **D3 — Floating action pill next to the text** replaces the island text strip: **Edit text** (lilac `#E7DDF5` fill, plum `#332847` text, text icon), **Style** (opens today's Text panel on Style), **Delete** (the word, `failureText`). 52pt glass capsule, 44pt buttons, 14pt above the box; flips 18pt below when the text sits in the top 22%; clamped 8pt inside the screen. Hidden during gestures. "Deselect" is dropped: tapping the video deselects.
* **D4 — Selection chrome.** 2pt Sky `#9BCAFF` box, 8pt radius, 1px ink 35% halo for contrast on bright footage; corner handle 28pt visual / 44pt hit, white 2pt ring, resize glyph, kept inside the preview (slide-post inset rule). Keep scale+rotate on the handle with 90° detents; add two-finger twist (slide-post parity, starts past 8°).
* **D5 — Typing on the video (KRI-240 Variant B grammar).** Chrome collapses (header, Chat/Editor, timeline, island, Kria button hidden; status bar stays). Preview fills the space above an edit bar that rides the keyboard (≥200pt tall on SE-class). The text is edited in place at its real position, font and wrap width on the paused frame; the rest of the frame dims 24% black. Edit bar on `paper` with a 1px `line` hairline: row 1 = font chips rendered in their own face (all `NativeFontCatalog.pickerFonts`, current first, horizontal scroll) + **Done** (butter pill, the word Done); row 2 = five swatches (white, ink, butter, sky, lilac; 28pt dots in 36×44 targets) · divider · background preset button cycling **Simple / Bold / Highlight** · alignment button cycling centre/left/right. A vertical **size slider** sits in the gutter left of the preview (200pt track, 24–320, live number under it). Return inserts a line break (titles are multi-line); Done or a tap on the dimmed video commits. One undo step per typing session.
* **D6 — Empty commit removes the text** with a toast "Text removed · Undo" (4s), same pattern as KRI-240 D3.
* **D7 — Paused affordance.** While paused with nothing selected, every editable visible text shows a faint 1.5pt dashed white (80%) outline, 6pt outset. None during playback, never in renders.
* **D8 — Empty-preview tap with a selection only deselects** (no fullscreen). With nothing selected it stays fullscreen.
* **D9 — Tapping text during playback pauses and selects** (playback would otherwise carry the text out of its window). Deviation from the web's "selecting never pauses", recorded here.
* **D10 — Move guides.** While moving: Sky 1.5pt centre lines snap at ±2.5% horizontally / ±2% vertically (existing haptic tick stays); the area platform buttons cover (right 17%, bottom 23%, top 8%) is shaded ink 28% with "App buttons cover this". Advisory only, never blocks.
* **D11 — Size readout + clamp.** During corner-drag/pinch a "Size N" chip (ink 90%, white Inter 13 semibold) sits centred above the text; N is `size_px`, the same number the Style tab shows. Gestures clamp 24–320; stored out-of-range values stay until touched.
* **D12 — Locked text explains itself.** `read_only` text becomes hit-testable: tap shows a dashed white box + lock badge and a notice "This title can't be edited here yet." with **Ask Kria** (opens chat with the composer focused). No handles, no pill, no mutation.
* **D13 — Covers-a-face warning (phase 2).** On gesture end, on-device Vision face rectangles on the paused frame; if the text box overlaps a face: box turns amber, chip "Covers a face" with **Move it** (nudge to the nearest clear spot inside the safe area, up first; one undo step) and **Keep here**. Warn, never block. Needs a new amber token.
* **D14 — Captions on the video:** tapping a caption opens KRI-240's caption edit bar on that line with the keyboard up (one tap). Individual caption lines stay non-draggable (caption style is global).
* **D15 — New text goes straight to typing.** "+ → Text" adds text at the centre at the playhead and enters D5 with placeholder "Type something". The Text tool's "On screen" rows seek, select on the video (D2) and enter D5.
* **D16 — The Text panel stays the "everything" home** (outline, shadow, numeric position, rotation, animation, timing); only its entry points change.
* **D17 — Ship whole.** Prior learning applied: `testflight-ships-every-green-main-ios` (confidence 10/10, 2026-10-02): every green main iOS build reaches external testers, so phase 1 (D1–D12, D14–D16) lands as one PR; D13 is a separate additive PR.

### Interaction states

```
  FEATURE            | LOADING                               | EMPTY                              | ERROR                                   | SUCCESS                       | PARTIAL
  -------------------|---------------------------------------|------------------------------------|-----------------------------------------|-------------------------------|------------------------------
  Tap on text        | Source preview preparing: text taps   | No text at playhead: tap = today's | Locked text: D12 notice + Ask Kria      | Box + handle + pill (D2-D4)   | Overlapping texts: 2nd tap
                     | show "Preview is updating, try again  | fullscreen; Text tool lists all    |                                         |                               | types into the selected one;
                     | in a moment" toast; no gestures       | blocks                             |                                         |                               | others via TEXT lane / list
  Move / resize      | n/a (local)                           | n/a                                | Hit max/min: light haptic, number stops | Guides, readout, 1 undo step  | Text partly off-frame allowed
                     |                                       |                                    |                                         |                               | (clamped anchor 0.06-0.94)
  Typing on video    | Font file loading: chip shows the     | Empty on Done: removed + Undo      | Save later fails: existing save banner  | Text updates live, Done       | Emoji/RTL: field accepts, 
                     | name in Inter until ready             | toast (D6)                         | (unchanged)                             | returns to Browse             | renderer shapes (HarfBuzz)
  Face warning (P2)  | Detection runs after release, ≤150ms; | No face found: nothing shown       | Vision error: no warning (fail-open)    | Amber chip, Move it / Keep    | Several faces: nudge clears all
                     | chip appears only if overlapping      |                                    |                                         | here                          |
```

### Journey storyboard

```
  STEP | USER DOES                          | USER FEELS                      | PLAN SPECIFIES
  -----|------------------------------------|---------------------------------|---------------------------------------------
  1    | Opens the editor, video paused     | "Where do I change the title?"  | D7 dashed outline says "this is tappable"
  2    | Taps the title                     | Relief: it reacted, it's big    | D2 focus growth, D3 pill with the word "Edit text"
  3    | Drags it lower, pulls the corner   | In control                      | D10 snap lines, D11 live size number
  4    | Taps it again                      | "Oh, I just type here"          | D5 caret on the video, keyboard, fonts above it
  5    | Picks a font and colour, hits Done | Proud, it looks like my video   | D5 live preview, one undo step, back to Browse
  6    | (Drops it on a face — phase 2)     | Caught before posting           | D13 amber nudge, never blocks
```
5-second read: the title is obviously tappable and grows when touched. 5-minute use: move, size, type without opening a panel. Long term: same grammar as slide posts' canvas and the web editor.

## 4. Acceptance criteria

- [ ] One tap on visible editable text selects it, grows the preview (D2) and shows the pill; the matching TEXT bar is highlighted (XCUITest).
- [ ] Second tap on the selected text, double-tap, or **Edit text** shows the keyboard with the caret at the end in one step; typed characters appear on the video at the text's position in its font (XCUITest + source-pixel check).
- [ ] Corner handle hit area ≥44pt and inside the preview; pinch and twist work; "Size N" shows during resize; each gesture is one undo step.
- [ ] Done on empty text removes it with "Text removed · Undo"; Undo restores the words.
- [ ] Tap on `read_only` text shows the D12 notice; no tap on visible text is ever silently ignored.
- [ ] Empty-preview tap with a selection deselects without fullscreen.
- [ ] iPhone SE (375×667): typing preview ≥200pt tall; edit bar never covered by the keyboard.
- [ ] VoiceOver: each text exposes actions Edit text, Bigger, Smaller, Move up, Move down, Delete; the pill and edit bar controls are labelled; Reduce Motion removes the growth animation.
- [ ] Caption tap opens the KRI-240 bar on that line (one tap).

## 5. Implementation notes (2026-10-07)

Built: D1–D12 and D16 (T1–T10). Not built: D13 face warning, D14 caption tap → caption bar, D15 "+ → Text" straight to typing (the Text tool's list rows also keep opening the panel's Edit tab, KRI-185).

Deviations from §3, decided while building:
* **D1:** no double-tap. A double-tap recogniser would delay every first tap on the canvas; the second single tap already types.
* **D1:** the *timeline's* second tap on a text bar still opens the Text panel (existing contract and tests). Only preview taps and Edit text type on the video.
* **D3:** the island strip's Edit text now types on the video as well; the strip remains for previews too narrow for the pill (the unfocused 9:16 preview is ~160pt wide) and at accessibility text sizes.
* **D5:** the typing layout reuses the caption edit-bar path in `NativeEditorLayoutMetrics` (`captionEditBarHeight`) with the header hidden; the bar is `NativeTextInlineBar` (118pt at default text size).
* **D12:** the notice offers "Ask Kria" only when the editor has a conversation.

Files: `Features/NativeTextOnVideo.swift` (new), `NativeEditorMediaViews.swift`, `NativeEditorView.swift`, `NativeEditorConnectedPanel.swift`, `Core/NativeEditorInteraction.swift` (`activeGuides`). Tests: `Tests/KriaTests/NativeTextOnVideoTests.swift`, two new UI tests in `NativeEditorInspectorUITests`, one updated in `EditorUITests`.

## What already exists (reuse)

* `NativeTextTransformLayer` gesture math (move, corner, pinch, 90° snap) and the split-frame live preview — keep; add twist and the 24–320 gesture clamp.
* `SlidePostTextCanvas` / `SlidePostInteractions`: handle inset, pinch + twist, VoiceOver "Select" / "Edit text" actions.
* KRI-240 caption edit bar (plan 026, D2 Variant B): chrome collapse, bar riding the keyboard, Done pill, removal toast.
* `NativeFontCatalog.previewFont` (renderer-identical faces for chips and the inline field).
* `NativeEditorIslandMetrics` preview sizing (`maxPreviewScreenFraction`, `captionEditBarHeight` path).
* Kria tokens: `sky` selection, `selectionSoft` lane fill, `lilac`/`plum` text tools, `butter` primary, `failureText` Delete, glass `kriaFloatingSurface`.

## NOT in scope

* Free-dragging individual caption lines (caption style is global; KRI-240 owns caption text).
* Changing the slide-post canvas to select-first (it has no timeline; tracked as a follow-up below).
* New fonts, animations or text presets (only surfaced, not added).
* Making phone Narrated titles editable server-side (`phone_narrated_title_edits_supported`); D12 only explains the lock.
* Web editor changes (already has on-canvas editing).
* Text-behind-subject (`behind_subject`) authoring on iPhone.

## Follow-ups (kept here, not added to TODOS.md)

* **Slide-post tap model alignment.** What: decide whether slide posts move to select-first or video moves to tap-to-type after usage data. Why: two grammars in one app. Depends on: phase 1 shipping.
* **Face warning (D13) as its own PR** with the amber token in `DesignTokens.swift` + DESIGN.md.

## Implementation Tasks

Synthesized from this review's findings. Effort ratio assumption: features ~30× (human ÷ CC), tests ~50×.

- [ ] **T1 (P1, human: ~1d / CC: ~30min)** — NativeEditorView / MediaViews — Route second tap, double-tap and Edit text to inline typing; empty tap deselects first; pause on select
  - Surfaced by: Code facts — `routeSelection` opens Style; empty tap → fullscreen
  - Files: `Features/NativeEditorView.swift` (`:576`, `:719-725`), `Features/NativeEditorMediaViews.swift` (`:291-318`, `:385-392`)
  - Verify: XCUITest in `Tests/KriaUITests/NativeEditorInspectorUITests.swift`
- [ ] **T2 (P1, human: ~1d / CC: ~30min)** — Action pill — Replace the text context strip with the floating pill (D3)
  - Surfaced by: Pass 1 — actions live in the island, far from the text
  - Files: `Features/NativeEditorComponents.swift:407-458`, `Features/NativeEditorMediaViews.swift`
  - Verify: XCUITest asserts pill frame sits within 60pt of the selection box and inside the screen
- [ ] **T3 (P1, human: ~3d / CC: ~2h)** — Inline typing — `NativeTextInlineEditor` (UITextView styled from the renderer layout over the split-frame "below" layer) + edit bar + size slider + chrome collapse (D5, D6)
  - Surfaced by: Pass 2/3 — words only editable in a panel box at 120pt preview
  - Files: new `Features/NativeTextInlineEditor.swift`, `Features/NativeEditorIsland.swift`, `Features/NativeEditorView.swift`, `Features/NativeEditorSession.swift` (transactions)
  - Verify: XCUITest types into the field; source-pixel check that rendered text matches after Done; SE + Pro Max layout tests in `NativeEditorLayoutMetricsTests`
- [ ] **T4 (P1, human: ~0.5d / CC: ~20min)** — Text Focus — preview growth on select, restore on deselect (D2)
  - Files: `Features/NativeEditorIsland.swift` (metrics), `Features/NativeEditorView.swift`
  - Verify: `NativeEditorLayoutMetricsTests` pins focus height per device class
- [ ] **T5 (P2, human: ~1d / CC: ~30min)** — Selection chrome + twist + clamp + guides + readout (D4, D10, D11)
  - Files: `Features/NativeTextTransformLayer.swift`, `Features/NativeEditorMediaViews.swift:1011-1047`, `Features/NativeEditorSession.swift:3237-3249`
  - Verify: unit tests on clamp/snap math in `NativeEditorInteractionTests`
- [ ] **T6 (P2, human: ~0.5d / CC: ~15min)** — Paused dashed affordance (D7); follow `playbackClock`, not `session.currentTime` (prior learning `native-editor-currenttime-never-publishes`)
  - Files: `Features/NativeEditorMediaViews.swift`
- [ ] **T7 (P2, human: ~0.5d / CC: ~20min)** — Locked text notice + Ask Kria (D12)
  - Files: `Features/NativeEditorSession.swift:1263-1270`, `Features/NativeEditorMediaViews.swift`
- [ ] **T8 (P2, human: ~0.5d / CC: ~15min)** — Caption tap → KRI-240 bar (D14); "+ → Text" and On-screen rows → inline typing (D15)
  - Files: `Features/NativeEditorView.swift:680-717`, `Features/NativeTextCreationPanel.swift`
- [ ] **T9 (P1, human: ~1d / CC: ~30min)** — VoiceOver actions, Dynamic Type at AX sizes, Reduce Motion (Pass 6)
  - Verify: `AppleTextAccessibilityUITests` at 320pt width + accessibility sizes
- [ ] **T10 (P2, human: ~1h / CC: ~10min)** — DESIGN.md "Native iOS alignment": on-video text editing grammar (same PR, per DESIGN.md rule)
- [ ] **T11 (P3, human: ~2d / CC: ~1h)** — Face warning (D13) in a separate PR + amber token

## Approved Mockups

Built as an HTML canvas (the image mockup tool has no API key on this machine). Auto-selected under Yasin's instruction; awaiting cofounder review.

| Screen/Section | Mockup | Direction | Notes |
|----------------|--------|-----------|-------|
| Today vs new (interactive) | https://claude.ai/artifact/6Y5YtkeMj5NpmosLxjMgw5 — `Today`, `Main` (clickable) | Select-first, Text Focus, type on video | Prototype logic checked in a stub harness; not click-tested in the canvas runtime |
| Steps 1-3 | same canvas — `Step1-Select`, `Step2-Move`, `Step3-Edit` | D2-D5, D10, D11 | Keyboard drawn as a grey placeholder |
| Edge cases | same canvas — `Edge-Locked`, `Edge-Face` | D12, D13 | Face case is phase 2 |

## Design review: completion summary

```
  +====================================================================+
  |         DESIGN PLAN REVIEW — COMPLETION SUMMARY                    |
  +====================================================================+
  | System Audit         | DESIGN.md present (Native iOS alignment);   |
  |                      | UI scope: iOS native editor preview + text  |
  | Step 0               | 2/10 (two-sentence issue); all 7 passes     |
  | Pass 1  (Info Arch)  | 2/10 → 9/10 (hierarchy + 3 screen diagrams) |
  | Pass 2  (States)     | 1/10 → 9/10 (state table)                   |
  | Pass 3  (Journey)    | 2/10 → 8/10 (storyboard; device test open)  |
  | Pass 4  (AI Slop)    | 4/10 → 8/10 (OPERATE; real app chrome)      |
  | Pass 5  (Design Sys) | 3/10 → 9/10 (tokens mapped; amber is new)   |
  | Pass 6  (Responsive) | 1/10 → 8/10 (SE floor, VO, Reduce Motion)   |
  | Pass 7  (Decisions)  | 17 resolved (auto), 0 deferred              |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (6 items)                           |
  | What already exists  | written                                     |
  | TODOS.md updates     | 0 (2 follow-ups kept in this plan)          |
  | Approved Mockups     | 7 boards built, auto-selected               |
  | Decisions made       | 17 added to plan                            |
  | Decisions deferred   | 0                                           |
  | Overall design score | 1/10 → 8/10                                 |
  +====================================================================+
```

Pass notes:
* **Pass 4 (OPERATE / app UI):** no hard rejections. Litmus: brand unmistakable (Kria chrome, Sky/Butter) YES; one visual anchor (the video) YES; scannable YES; each region one job YES; cards necessary — none used YES; motion improves hierarchy (focus growth) YES; premium without decorative shadows YES (shadows only on floating controls over video, offset + blur). Inter body + Fraunces size glyphs follow DESIGN.md §5.
* **Pass 5:** Edit text uses Lilac/Plum (DESIGN.md §9 text tools) instead of today's sage strip; amber warning has no iOS token yet (T11).
* **Pass 6:** iPhone only (iPad not shipped). SE floor 200pt typing preview; landscape projects (Video shape) keep D2 growth by height cap; 44pt targets everywhere; custom VoiceOver actions replace gestures.
* **Outside voices:** skipped (no questions asked; time to mockup prioritised).

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Review | codex (design outside voices) | Independent 2nd opinion | 0 | skipped | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 0 | — | — |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | clean | score: 1/10 → 8/10, 17 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex design voice, phase design, skipped by auto-decision (user asked for no questions); no outside findings.
- **VERDICT:** DESIGN CLEARED (decisions auto-made at Yasin's instruction; cofounder review of the mockup pending) — eng review required.

NO UNRESOLVED DECISIONS
