# Plan 025 — KRI-240: iPhone editor caption text editing redesign

**Linear:** [KRI-240](https://linear.app/kria/issue/KRI-240) · **Status:** In Progress (design review stage) · **Owner:** Yasin Berk · **Label:** iOS
**Related:** KRI-216 (phone caption edits save), KRI-110 (guided-story captions via `text_elements`), KRI-230 (TR word-by-word timing), KRI-241 (silent preview after caption edit, separate), KRI-148 (connected editor panels contract, `docs/runbooks/ios-development.md`).
**Base:** origin/main `572d02d1d` (2026-10-01). Code facts below are from that commit.

> This file is the plan under review by `/plan-design-review`. §6 holds the approved decisions (D2–D16); §7 keeps the findings as evidence; the review report is the last section.

## 1. Problem (verbatim from KRI-240)

In the iPhone editor, go to **Captions → Edit captions** and try to fix a line (screenshot: a Turkish Talking edit, fixing line 4 "Issizim").

1. **It takes two taps to start typing.** The first tap on a line only turns the text into a text field. No keyboard appears and there's no cursor. You have to tap the line again to get the keyboard.
2. **The line you're editing hides behind the keyboard.** When the keyboard opens, the panel squeezes into the space above it, and the active row is cut off at the bottom edge. You only see one line, and only part of it.
3. **You can't see your change on the video.** The preview shrinks to a thumbnail about 130 pt wide. The caption on it is a small pill you can't read.
4. **Rows look like they open a new screen, but they don't.** Each row has a `›` chevron, which suggests a detail screen, but tapping edits the row in place.
5. **There's no quick way to fix the next line.** Fixing a transcript is: dismiss the keyboard, find the next row, tap twice. Return just adds a newline (the field is `axis: .vertical`).
6. **The keyboard language doesn't follow the caption language.** Turkish captions, English keyboard: English suggestions, İ/ş/ğ need long-presses, autocorrect can silently rewrite names and Turkish words.

Screenshot of today's state: Linear attachment "Caption editing with keyboard up (iPhone)".

## 2. Code facts (origin/main 572d02d1d)

_As written in the issue; being verified against the code during this review — see §2a._

* `NativeEditorLanePanel.swift`, `NativeCaptionPanel.transcript`: tapping a row sets `editingCueID`, which swaps `Text` for a SwiftUI `TextField` but never focuses it. A comment says this is deliberate: `@FocusState` set from the row tap in the same turn as `session.select(...)` never committed, and focusing from `.onAppear` hung the UI tests. **Cause #1.**
* `NativeTextCreationPanel` uses `NativeExplicitLineTextEditor` (a `UIViewRepresentable` in `NativeEditorTextPanel.swift` that calls `becomeFirstResponder()` once in a window); focus works there.
* `NativeEditorView.swift` / `NativeEditorIsland.swift`: with the keyboard up, `panelDefaultHeight` uses the whole remaining height, the transport and tool rail hide, the preview shrinks. Nothing scrolls the active row into view.
* `session.select(...)` already moves the playhead to the line's start time.

### 2a. Verified during review (code read at origin/main 572d02d1d, 2026-10-01)

**Confirmed as written in the issue**
* Row tap = `endTransaction(); select(.captionCue); editingCueID = cue.id` (`NativeEditorLanePanel.swift:162-166`); no `@FocusState` in `NativeCaptionPanel`. The no-focus comment (`:83-93`) is deliberate and names the UI-test hang.
* Caption field is a SwiftUI `TextField(…, axis: .vertical)` with no `.onSubmit`, `.submitLabel`, `.lineLimit`, `.autocorrectionDisabled` or `.textInputAutocapitalization` (`:145-149`). Return inserts a newline.
* With the keyboard up: `panelDefaultHeight` = whole remaining budget (`NativeEditorIsland.swift:125-128`); transport hidden (`NativeEditorView.swift:398-405`); tool rail hidden (`:453-455`); tap-to-fullscreen disabled (`:312`). No `ScrollViewReader`/`scrollTo` in the caption panel.
* `session.select` seeks to the cue start AND pauses playback (`NativeEditorSession.swift:891-896`, `:2182-2205`).
* Chevron on every row, including the row being edited (`:141-157`).

**Refinements the issue did not have**
* The editor only observes `keyboardVisible: Bool` (`NativeEditorView.swift:29, :157-158`). No keyboard frame/height is read anywhere in the app; the layout shrinks because the root `GeometryReader` shrinks. A bar "pinned above the keyboard" needs either the keyboard-driven safe area (what the shell already gets) or `UIKeyboardLayoutGuide`.
* Preview size while typing is per-tool: `shrinksPreviewWhileTyping: panel?.tool == .text` (`NativeEditorView.swift:229`) gives the Text tool the documented 120pt compact preview (KRI-148 runbook). Captions does NOT opt in; it gets the generic branch `min(preferred, max(80, viewport − 320 − topChrome))` ≈ 137pt on a 16 Pro-class viewport. The per-tool hook is the natural place for a Captions-specific "preview grows while typing" branch.
* Panel resize and preview growth are both inert while the keyboard is up (`panelMaxHeight == budget`, drag guard `range > 0`); preview shrink to 80pt and the list's `scrollDismissesKeyboard(.interactively)` still work.
* `NativeEditorLayoutMetricsTests` pins `pro(keyboard:true).defaultPreviewHeight == 258.06` against an unshrunk 759pt viewport, so the test fixture does not reproduce the real keyboard-up value.
* **Guided-story rows probably do not seek on tap.** `startTime(for:)` looks up a timeline item by `EditorSelection`; guided-story captions are `.text` timeline items but the row selects `(.captionCue, <text-element id>)`, so the lookup misses and the seek is skipped (`NativeEditorSession.swift:939-940, :4698-4701`). The issue's "session.select already moves the playhead" holds only for real `caption_cues`.
* **No caption language on the device model.** `EditorCaptionCue` is `{id,startS,endS,text,raw}`; zero `language` matches in `Core/`, `Features/`, `Generated/openapi.yaml`. The "TR captions → Turkish keyboard" criterion has no data source yet. The web `CaptionsDrawer.tsx:253-296` shows an en/tr language chip with a re-transcribe flow, so the API has a value somewhere; it must be plumbed to iOS.
* No currently-playing highlight exists on iOS. Web has one: `aria-current` + lime fill + `scrollIntoView({block:"nearest"})`, suppressed while editing (`CaptionsDrawer.tsx:118-148, :471-482`).
* Web parity on Return: web is a single-line `<Input>`; Enter and Escape both exit edit mode; there is no next-line step (`:453-459`). "Return = Next" is a deliberate divergence.
* `NativeExplicitLineTextEditor` (`NativeEditorTextPanel.swift:500-564`) sets font/Dynamic Type/`keyboardAppearance`/`returnKeyType = .default` only; `autocorrectionType`, `textInputMode`, autocapitalization are UIKit defaults. Focus is a `Binding<Bool>` + `becomeFirstResponder()` in `didMoveToWindow`. The connected editor's Text creation panel also does not auto-focus (`.task { if !connected { focused = true } }`).
* A11y today: rows are not combined elements, the chevron is not hidden from VoiceOver, the only custom action is "Edit caption" (`:167-170`). No `@ScaledMetric` in the caption panel (Text panel has them). Footer helper copy "Tap a line to edit. Changes stay synced to speech." (`:173`) is inline helper text, which DESIGN.md §9 forbids.
* `.disabled(!session.canEditCaptionLines)` on the whole transcript (`:110`) blocks seeking as well as typing.
* Existing UI test `testTappingCaptionRowEntersEditModeAndPersistsTypedText` (`NativeEditorInspectorUITests.swift:1168`) taps the field a second time by hand and never asserts the keyboard appears after the row tap; its comment (`:1192-1195`) notes cursor placement at end-of-text is not guaranteed on a freshly focused multi-line field.

## 3. Proposed design (verbatim from KRI-240)

Model it on how CapCut and Edits fix subtitles: **when you're typing, the screen shows only the line, the video, and the keyboard.**

**Browsing (keyboard down)**
* A list of lines with timestamps, as today. Remove the chevron. Highlight the line that's playing and keep it scrolled into view during playback.

**Editing (keyboard up)**
* **One tap opens the keyboard** with the cursor at the end of the line, using `NativeExplicitLineTextEditor` (or a caption version of it).
* Replace the squeezed list with a **compact edit bar pinned above the keyboard**: line number and time range, the text field (grows to about 3 lines), **‹ previous / next ›** buttons, and a **✓** button.
* **Return = Next.** Saves the line and moves to the next one with the keyboard still up. On the last line, Return finishes (`returnKeyType` .next, then .done).
* **The preview takes the space above the bar.** Paused on the line's start time, shows the real caption style, updates as you type. Optional: a small play button that loops just this line.
* Swiping the keyboard down, tapping the preview, or tapping ✓ saves and goes back to the list. One undo step per line (existing `beginTransaction`/`endTransaction`).

**Keyboard**
* Ask for a keyboard in the caption language when installed (TR captions → Turkish keyboard, `textInputMode` override on the UITextView); fall back to the current keyboard otherwise.
* `autocorrectionType = .no` for transcript text, keep spell-check underlines.

**Edge cases to decide during build**
* Clearing a line completely: hide that line, or block an empty save? (Today an empty string is saved as-is.)
* Guided-story captions (KRI-110) save through `text_elements` and only the text can change; the edit bar must not offer timing controls for them.
* VoiceOver: "Edit caption" action should open the keyboard directly; check the bar at large text sizes.

## 4. Acceptance criteria (verbatim from KRI-240)

- [ ] Tapping a caption line opens the keyboard with the cursor in that line in **one tap**: no second tap, no hang (XCUITest in `NativeEditorInspectorUITests`).
- [ ] The line being edited is never covered by the keyboard or the panel edge on iPhone SE, iPhone 15 and Pro Max sizes.
- [ ] The preview stays readable while typing (well beyond today's ~130 pt thumbnail) and shows the edited text live on the right frame.
- [ ] Return / › moves to the next line with the keyboard still up; ‹ goes back; ✓ / swipe-down saves and closes.
- [ ] No chevron on caption rows; the playing line is highlighted during playback.
- [ ] Turkish captions get the Turkish keyboard when installed; transcript text isn't auto-replaced.
- [ ] Edits still save through `caption_cues` (or `text_elements` for guided-story) and survive Save + reopen (KRI-216 behaviour).
- [ ] Checked on a real iPhone with a Turkish and an English Talking edit, before/after screen recordings attached to KRI-240.

## 5. First step (verbatim from KRI-240)

Before building, make a quick mock of the edit bar and preview layout and check it against CapCut / Edits caption editing. Then build it.

## 6. Decisions recorded by the review (approved)

### D2 (2026-10-02) — Edit state follows wireframe Variant B ("collapsed chrome, bar rides the keyboard")

Chosen over A (KRI-240 as written: app header kept, preview 125×70pt on SE) and C (list retained, preview fixed at the Text tool's 120pt). Board: `~/.gstack/projects/emirerben-nova/designs/caption-edit-bar-20261001/design-board.html`, `approved.json` beside it.

**Edit state (text view is first responder):**
* Hidden: app header (back / title / share / Save), Chat/Editor control, "Rendered on iPhone" label, panel title row + tabs, transport, tool rail; **added by eng review D29 (R9 = A):** timeline resize handle, Kria sparkles button (it overlays the caption area), newer-job prompt; the panel top inset and island bottom pad collapse so the bar sits on the keyboard. Status bar stays. The edit bar REPLACES the panel; the panel's tab and scroll position are restored on exit. Floor: 220pt for 9:16 on SE-class; other aspect ratios fill the available width.
* Top → bottom: **preview** (fills the remaining height, 9:16 centered, paused on the line's frame, caption drawn by the same compositor as export) · **edit bar** on `paper` with a 1px `line` hairline on top: 8pt padding · **field** (`softZinc` fill, 1px `line`, 12pt radius, Inter 17/22, 3 lines on iPhone 15-class, 2 lines on SE-class, scrolls internally beyond that) with a 44×44 **loop-play** button trailing (`lilac` fill, `plum` glyph) · **nav row** 44pt: "‹ Previous" (15 `ink`) · "4 of 20" (13 `mutedInk`, centered) · "Next ›" · trailing **Done** pill (44pt, `butter` fill, `ink` text, the word Done, never a ✓ glyph) · 8pt padding · keyboard.
* Attention hierarchy: 1 the field (caret), 2 the preview (verify), 3 the nav row (move on). Timecode is demoted out of the bar (it lives on the list row).
* Preview budget from the board (1px = 1pt; keyboard 291/216 + 44 predictive row): iPhone 15 = 282×159pt, iPhone SE = 267×150pt. Floor: 220pt tall on SE-class. Variant A would give 125pt on SE, C 120pt everywhere.
* Carried from the issue, now confirmed by the mockup: no chevron on rows; "4 of 20" position count; loop-play present (its behaviour is specified under P4-1).
* The accessibility-size frame on the board (field 2 lines, icon-only ‹ ›, Done word, 207pt free above the bar) is drawn but its rule is NOT yet approved: see P6-1.

**Browse state (keyboard down):** unchanged shell (header, Chat/Editor, label, preview 258pt, panel with title row + tabs, rows, transport, rail). Rows: number (13 `zinc`) · text (16 `ink`) · start time (12 `mutedInk`), 59pt, no chevron.

### D3 (2026-10-02) — Removing a line (Pass 1, issue 1 → option 1A)

* Browse state: ~~each caption row gets a trailing swipe action labelled with the word **Delete**~~ **Amended by eng review D26 (R6 = A):** each caption row has a long-press context menu with one destructive item, the word **Delete** (red, DESIGN.md §14 rule: never an icon); SwiftUI swipe actions need a `List` and the caption list is a `ScrollView` stack. Deleting pushes one undo step.
* Edit state: committing an emptied field on a transcript caption (`caption_cues`) **removes the line** and shows an Undo toast ("Line 4 removed · Undo", 4s, bottom of the preview area, above the bar). The bar then shows the next line (or the previous one if it was the last). **Refined by eng review D32 (R12 = A):** the removal replaces the line's typing transaction, so one Undo restores the original non-empty line; the toast lasts 4s or until the next keystroke or line change, and offers Undo only while the removal is the newest undo step.
* ~~Guided-story captions (`text_elements`, server-owned timing): an empty commit is **blocked**; the field reverts…~~ **Superseded by eng review D22 (R2 = A):** guided-story rows behave like cue rows. Swipe "Delete" and an emptied-field commit remove the line with the Undo toast, through `deleteSelection(EditorSelection(kind: .captionCue, id:))`, which #1319 already turns into a server-validated `caption_cue` deletion. Their timing stays server-owned (no timing edits).
* ~~Requires a `removeCaptionCue(id:)` session mutation~~ **Eng-review correction (F3):** reuse `session.deleteSelection(EditorSelection(kind: .captionCue, id:))` from #1319 (`NativeEditorSession.swift:3620-3628` @ 7672ded66): one undo step, removes the cue and any caption text element with that id, records a server-validated `caption_cue` deletion. The cue's `words` go with the row.
* Pass 1 re-rated 9/10 (remaining: list context is absent while typing by design; the "4 of 20" counter and Previous/Next carry orientation).

### D4 (2026-10-02) — Interaction-state table (Pass 2, issue 2 → option 2A)

| State | What the creator sees |
|---|---|
| LOADING (captions still transcribing) | List shows 4 skeleton rows (`softZinc`), row taps disabled, panel tabs stay. The edit bar never appears. |
| EMPTY (zero captions) | One quiet `zinc` line "No captions on this edit yet" + the single existing action (`butter` "Add captions") when the item supports caption generation; otherwise the line alone. No bar. _Build check: confirm what the Captions panel renders today for an item with no cues._ |
| UNSAVED / PARTIAL | 6pt `sky` dot before the number on every row whose line changed since the last Save. Header Save stays enabled only while dirty (existing grey-checkmark/Save rule). |
| ERROR (Save fails) | Local draft persists, dots stay. ~~Non-modal banner at the top of the panel: "Couldn't save captions · Retry".~~ **Amended by eng review D23 (R3 = A):** the existing top-chrome `NativeEditorSaveBanner` ("Couldn’t save this edit" + detail) shows it, with a new 44pt "Try saving again" button. Never an alert over the keyboard. |
| SUCCESS (Save) | Dots clear; Save returns to the grey checkmark. |
| INTERRUPTED (call / background with keyboard up) | Commit the current line on `resignActive`. On return: browse state, that row scrolled into view and selected, keyboard down, no auto-refocus. |
| PLAYBACK WHILE EDITING | First keystroke pauses playback; loop-play restarts the line loop. |
| LIVE PREVIEW UNAVAILABLE (added by eng review D30) | `.failed` / `.originalsUnavailable`: chrome collapses and the bar works as usual; the preview frame gets a 50% `paper` scrim and one `mutedInk` 13pt line "Preview shows your last render. Your fix appears after Save."; loop-play still plays the line's audio. `.preparing`: existing preparing indicator. |
| ROTATION | Edit state is portrait-only. **Eng-review correction (F4):** already true on iPhone (`Kria/Support/Info.plist:26-27` lists only `UIInterfaceOrientationPortrait`); no work. |

### D5 (2026-10-02) — Commit vs Save, exits (Pass 2, issue 3 → option 3A; also resolves P4-2)

* **Vocabulary.** Leaving a line **commits** it to the local editor draft. Commit triggers: Return, Next, Previous, Done, Esc, tool change, `resignActive`, Delete (keyboard swipe-down removed by D28). The header **Save** is the only action that persists and re-renders. UI copy never uses "save" for a line commit.
* **No line-level cancel.** Undo is the editor's existing one step per changed line (`beginTransaction`/`endTransaction` around the line). Next/Previous on an untouched line adds no undo entry and no unsaved dot.
* **Preview tap toggles the line loop** (play/pause), never exits the line. ~~Only Done and keyboard swipe-down return to browse~~ **Amended by eng review D28 (R8 = A):** Done, Return on the last line and Esc return to browse (tool change and background also end the line); they land on that row, scrolled into view and selected. No keyboard swipe-down exit.
* **Return on the last line = Done** (`returnKeyType` is `.next` on lines 1…N-1 and `.done` on line N).
* Divergence from web (Enter exits edit mode in `CaptionsDrawer.tsx`) is intentional; document it in `docs/runbooks/ios-development.md` next to the KRI-148 contract.
* Each line gets a fresh undo manager (clear the UITextView's `undoManager` on line change) so shake-to-undo after Next cannot edit the previous line.

### D6 (2026-10-02) — Word-by-word styles mid-edit (Pass 2, issue 4 → option 4A)

* **Park frame.** ~~Entering a line seeks to its fully-revealed frame: the start time of the cue's last word… For sentence/pop-in-by-line styles that equals `startS`.~~ **Amended by eng review D25 (R5 = A):** park at `startS + 0.15s` (word styles: the last word's start + 0.15s), clamped to `endS − 1 frame`, because `.captionPop` fades in over 0.12s and settles scale by 0.14s (`TextTransformTiming.swift:38-40`); at `startS` the caption is invisible. Guided-story captions use the same rule.
* **Text edit never moves `startS`/`endS`.**
* **Word-timing rule (deterministic, shared by `KriaMediaEngine` and the server reburn):** if the edited line has the same word count, each word keeps its timing by position; if the count differs, words are spread across `[startS, endS)` proportional to character count (character weight), preserving order. Pasted newlines become spaces.
* **Build check:** confirm the server's current word-timing fix-up on `caption_cues` text edits matches this rule, or change it to match; add one fixture that both renderers must agree on.
* Loop-play plays `[startS, endS)` of the current line with audio, stops on the first keystroke.

### D7 (2026-10-02) — Guided-story rows seek too (Pass 2, issue 5 → option 5A)

* The caption panel seeks **explicitly** by the tapped unit's own `startS`/`endS` (park frame per D6), for `caption_cues` and guided-story `text_elements` alike, instead of relying on `session.select(seekToStart:)` finding a `.captionCue` timeline item. `select` still runs for selection/routing; the seek is a separate `session.seek(to:)` call made by the panel.
* Loop-play reuses the same `[startS, endS)`.
* Pass 2 re-rated **10/10**.

### Pass 3 — Journey storyboard (required artifact; restates D2–D7, approves nothing new)

Scenario: creator fixes a 20-line Turkish Talking transcript on an iPhone.

| STEP | USER DOES | USER FEELS | PLAN SPECIFIES? |
|---|---|---|---|
| 1 | Opens Captions → Edit captions after the render; 20 rows, row 2 highlighted while the video plays | Oriented: "this is my transcript" | Yes: browse state + playing-row highlight (D2); auto-follow rule P6-4 pending |
| 2 | Spots "Issizim" on row 4, taps it once | Expectant | Yes: one tap → first responder; chrome collapses; preview parks on the fully-revealed frame (D2, D6, D7) |
| 3 | Keyboard rises with the bar; the big preview shows the line; caret at the end of the text | Confident: "I can see what I'm fixing" | Yes: Variant B; caret at end (issue). **Named limitation:** a typo at the start of the line needs a tap inside the field or a double-tap on the word; accepted, since most fixes are one word |
| 4 | Types "İşsizim" on a Turkish keyboard, no autocorrect rewrite | In flow | Pending P5-2 (language source) |
| 5 | Taps ▶ to hear the line | Reassured | Yes: loop-play (D2, D6) |
| 6 | Presses Return → line 5, counter reads "5 of 20" | Fast: "a loop, not a chore" | Yes: D5 commit + D2 counter |
| 7 | Lines 5–13 are fine; Returns through them | Safe: nothing changes | Yes: untouched line = no undo entry, no dot (D5) |
| 8 | Line 14 is junk on silence; clears it, Return | Relieved | Yes: remove + Undo toast (D3) |
| 9 | Phone rings on line 15 | Not punished | Yes: commit on resign-active, land in browse on row 15 (D4) |
| 10 | Comes back, taps row 15, finishes, taps Done | Closure; dots on 4 and 15 | Yes: exits land on the row (D5); dots (D4) |
| 11 | Taps header Save | Trust: the export changes, names did not | Yes: success/error states (D4); existing Save contract |

Time horizons: **5 s** — the chrome fold + large preview reads as "focus mode" (motion spec: P4-1). **5 min** — the Return loop makes a 20-line pass feel like one gesture repeated. **5 years** — autocorrect off and the Undo toast build the trust that the app never rewrites your words silently.

Pass 3: 4/10 → **9/10** (remaining: the fold/rise motion is unspecified → folded into P4-1).

### D8 (2026-10-02) — Exact values for the edit bar (Pass 4, issue 6 → option 6A)

| Phrase in the issue | Pinned value |
|---|---|
| "compact edit bar" | ≤142pt at default type on 15-class (8 + field 82 + 8 + nav 44); ≤120pt on SE-class (2-line field) |
| "about 3 lines" | Field: Inter 17/22, 12pt horizontal padding, `softZinc`, 1px `line`, 12pt radius; grows 1→3 lines (2 on SE) with height **snapping** (no tween); scrolls internally beyond |
| "small play button" | Loop-play 44×44, `lilac` fill, `plum` `play.fill` 17pt, 8pt from the field; label "Play line"; plays `[startS, endS)` with audio, loops until a keystroke or tap |
| ‹ › ✓ | Nav row 44pt: "Previous" / "Next" Inter 15 `ink` text buttons, `zinc` when disabled (Previous on line 1, Next on line N); counter "4 of 20" Inter 13 `mutedInk`, tabular numerals; **Done** = `butter` pill, Inter 15 semibold `ink`, 16pt side padding |
| "readable" | Rendered caption text ≥14pt on screen at 15-class (159pt-wide preview = 1080px canvas × 0.147; a 96px caption font → 14pt) and ≥12pt on SE; drawn by the live compositor at preview scale with locale-aware uppercasing |
| "paused on the line's start time" | Park frame per D6 as amended by D25 (`startS + 0.15s`, clamped), tolerance ±1 frame |
| "swipe the keyboard down" | ~~Interactive dismiss on the field~~ Removed by eng review D28: no swipe-down exit; Done / Return on last line / Esc |
| motion (unspecified) | Bar rises with the keyboard's own animation curve; chrome folds in 200ms opacity + height; **Reduce Motion = cut**; field growth never animates |
| "decide during build" items | Empty line → D3; guided-story → D3/D7; VoiceOver → P6-2; large text → P6-1 |

Pass 4 re-rated **10/10** (no hard rejections against Variant B; vague vocabulary replaced).

### D9 (2026-10-02) — Helper copy removed (Pass 5, issue 7 → option 7A)

* Delete the footer sentence "Tap a line to edit. Changes stay synced to speech." (`NativeEditorLanePanel.swift:173`). The row is the affordance; the unsaved dots and Save state carry the reassurance.
* If product wants "timing follows the speech" to survive, it lives behind an InfoDot on the Captions title row (DESIGN.md §2), never inline.

### D10 (2026-10-02) — KRI-148 contract and the per-tool typing hook (Pass 5, issue 8 → option 8A)

* `shrinksPreviewWhileTyping: Bool` (`NativeEditorView.swift:229`) becomes a per-tool enum, e.g. `TypingLayout { case keepsSplit; case compactPreview(height: 120); case fillsAboveEditBar(minPreviewHeight: 220) }`. Text → `.compactPreview(120)` (unchanged); Captions → `.fillsAboveEditBar(220)`; Visuals/Sounds → `.keepsSplit`.
* Rewrite the KRI-148 line in `docs/runbooks/ios-development.md`: Text keeps its compact 120pt preview because its panel lists presets and on-screen blocks under the field; Captions fills the space above the edit bar because the live caption on the video is the thing being checked. Document the Return=Next divergence from web beside it (D5).
* `NativeEditorLayoutMetricsTests`: add a keyboard-up case with a realistic shrunk viewport (15-class ≈ 457pt area; SE ≈ 387pt) asserting the 220pt preview floor for Captions and the existing 120pt for Text. The current `pro(keyboard:true) == 258.06` fixture does not exercise the real keyboard-up viewport.
* Follow-up TODO (deferred, not in this PR): should the Text tool also adopt fill-above-bar? Its 120pt preview has the same readability problem for titles.

### D11 (2026-10-02) — Caption language and keyboard rules (Pass 5, issue 9 → option 9A)

1. **Language source.** ~~Server field (additive) … Deploy API before the app.~~ **Eng-review correction (F2, structure D20):** the field already exists. Every subtitled variant (cloud and phone) and phone narrated variant carries `caption_language` in `Job.assembly_plan["variants"][i]`, returned untouched by `GET /generative-jobs/{id}/status`, which the iOS editor already loads (`EditorDocument.decode` keeps the variant as `rawRoot`). iOS reads `rawRoot["caption_language"]`. No API, Pydantic or openapi change, no deploy order. Values are whisper codes (usually `en`/`tr`, may be others). Absent field ⇒ rules 2–3 are skipped and behaviour is byte-identical to today.
2. **Keyboard request:** the caption text view overrides `textInputMode` to the first installed `UITextInputMode` whose `primaryLanguage` matches the caption language; otherwise the current keyboard. Verify on device; this is a documented-but-soft UIKit behaviour.
3. **Input flags:** `autocorrectionType = .no` always for transcript text. `spellCheckingType = .yes` only when the active keyboard's language equals the caption language, else `.no` (avoids a squiggle storm on fallback). `autocapitalizationType = .sentences`. `smartQuotesType`/`smartDashesType` = `.no`.
4. ~~**Locale-aware uppercasing**~~ **Deferred by eng review D19 (2026-10-02).** No caption style uppercases anything on phone or server; the only uppercase path is `text_case: upper` on text blocks, implemented locale-blind in Python (`app/agents/_schemas/text_element.py:175-190`), web (`overlay-layout.ts:447-453`) and iOS (`NativeEditorRenderCompiler.swift:394-398`). Tracked as TODOS.md `T-CAP025-2`.
5. Pass 5 re-rated **10/10**.

### D12 (2026-10-02) — Accessibility-size layout (Pass 6, issue 10 → option 10A)

At `dynamicTypeSize.isAccessibilitySize` (≥ `.accessibility1`):
* Field caps at **2 lines** and scrolls internally; `@ScaledMetric` for paddings as in the Text panel.
* Nav row becomes icon-only **‹ ›** (44×44, accessibility labels "Previous line" / "Next line") + the **Done** word pill (never shrinks below 44pt).
* "4 of 20" moves **above** the field, 20pt scaled.
* Preview: shown at whatever height remains when ≥120pt; hidden below 120pt (loop-play with audio remains the verification path). The board's "B at AX3 on SE" frame (207pt free) therefore SHOWS the preview at 207pt.
* Coverage: `AppleTextAccessibilityUITests` at 320pt width + AX sizes, asserting the field and all four controls are on screen with the keyboard up.

### D13 (2026-10-02) — VoiceOver contract (Pass 6, issue 11 → option 11A)

* **Rows:** `.accessibilityElement(children: .combine)`; label "Line 4, Issizim ama mutluyum, 0:07 to 0:09"; no chevron; default action = Edit (opens the bar and moves VoiceOver focus into the field); custom actions: Edit, Delete (D3). Values: "Playing" on the row under the playhead, "Edited" on rows with an unsaved change.
* **Bar:** buttons labelled "Previous line", "Next line", "Play line", "Done"; the counter is read as part of the field's accessibility hint ("Line 4 of 20"). Next/Previous/Return post `UIAccessibility.post(notification: .announcement, argument: "Line 5 of 20")`. The Undo toast is announced.
* **Auto-scroll to the playing row is off while VoiceOver is running** (`UIAccessibility.isVoiceOverRunning`).
* Coverage: XCUITest reads the combined row label, performs the Edit action, asserts the field has keyboard focus, taps Next and asserts the announcement.

### D14 (2026-10-02) — Hardware keyboard (Pass 6, issue 12 → option 12A)

* ~~Edit state = the caption text view is first responder (not `keyboardVisible`).~~ **Amended by eng review D27 (R7 = A):** edit state = `editingCueID != nil`; keyboard focus is a requested effect, never the truth; `editingCueID` resets on panel disappear, tool change, background (after commit), document replacement and Done. The bar is constrained to `view.keyboardLayoutGuide` (or the keyboard-driven safe-area inset the shell already receives); with no software keyboard it sits on the bottom safe area and the preview fills above it.
* Key map (`UIKeyCommand` on the text view): **Return** = Next (commit); **Shift-Return** = insert newline (the only newline path); **Tab / Shift-Tab** = Next / Previous line; **Esc** = Done; **Cmd-Z** = undo within the current line.
* Unit test: key commands dispatch the same session calls as the on-screen buttons.

### D15 (2026-10-02) — Playing-row follow (Pass 6, issue 13 → option 13A)

* While playing in browse state: `ScrollViewReader.scrollTo(cue.id, anchor: .center)` when the playing row changes.
* A user drag on the list **suspends** following; it **resumes** on the next play/pause tap, or when playback reaches a row already on screen.
* Never follow while editing (bar is up) or while VoiceOver is running (D13).
* Overlapping cues (post-KRI-202 clamping can leave touching cues): highlight the cue with the **latest start ≤ playhead**.
* Highlight = `selectionSoft` fill + 3pt `sky` leading bar + semibold text (non-color cue; `selectionSoft` on `paper` is ≈1.1:1).
* Pass 6 re-rated **10/10**.

### D3 clarification (same decision, applied to the same verb)
~~Guided-story rows (`text_elements`) expose **no** Delete swipe action…~~ **Superseded by eng review D22 (R2 = A):** guided-story rows expose the same Delete swipe and VoiceOver Delete action as cue rows, matching the timeline/inspector deletion #1319 already allows.

### D16 (2026-10-02) — Read-only captions (Pass 7, issue 14 → option 14A)

* When `canEditCaptionLines` is false: rows still **seek + highlight** (D7, D15); edit entry points are disabled instead of the list container: no bar on tap, no Delete swipe, no caret, VoiceOver default action becomes Seek.
* One visible disabled-reason line at the top of the list (`mutedInk`, 13pt), using the shell's existing capability wording for that item. Never a silent dead list.
* Move the `.disabled(!session.canEditCaptionLines)` from the transcript container (`NativeEditorLanePanel.swift:110`) to the edit affordances.

### Pass 7 register

| DECISION NEEDED | STATUS |
|---|---|
| Edit-state layout / chrome | Resolved D2 |
| Removing a line / empty commit | Resolved D3 (+ clarification) |
| Interaction states | Resolved D4 |
| Commit vs Save, exits, preview tap | Resolved D5 |
| Word-by-word park frame + timing rule | Resolved D6 (build check: server parity) |
| Guided-story seek | Resolved D7 |
| Exact sizes, tokens, motion, "readable" | Resolved D8 |
| Helper copy | Resolved D9 |
| KRI-148 contract / typing-layout enum | Resolved D10 (Text follow-up deferred → TODO) |
| Caption language + keyboard rules | Resolved D11 (API dependency) |
| AX-size layout | Resolved D12 |
| VoiceOver contract | Resolved D13 |
| Hardware keyboard | Resolved D14 |
| Playing-row follow | Resolved D15 |
| Read-only captions | Resolved D16 |
| Chat/Editor switch unreachable while typing | Accepted consequence of D2; recorded under NOT in scope |

## 7. Review findings — RESOLVED (evidence)

_All items in 7.2 were decided individually in §6 (D3–D16). The litmus scorecard in 7.1 stands as evidence; the "stacked bands" hard-rejection applied to today's screen and is cleared by D2._

_Found during `/plan-design-review` 2026-10-01/02. Each numbered item was decided individually in §6. Source: primary reviewer (Fable) + independent Sonnet design subagent (outside voice; Codex skipped by user instruction "orchestrate sonnet agents")._

### 7.1 Outside-voice litmus scorecard

| Check | Sonnet subagent | Codex | Consensus |
|---|---|---|---|
| 1. Product unmistakable in first screen? | YES, once "Add Captions" chrome is gone in edit state | skipped | single-model |
| 2. One strong visual anchor? | NO on SE: keyboard dominates, preview has no stated size | skipped | single-model |
| 3. Understandable by scanning headings? | NO: three stacked labels (Add Captions / Captions / Edit captions), edit state has none | skipped | single-model |
| 4. Each section has one job? | YES if preview-tap-exits is cut (list finds, bar edits, preview verifies) | skipped | single-model |
| 5. Cards necessary? | NO: hairlines + softZinc bar | skipped | single-model |
| 6. Motion improves hierarchy? | NO as specified (nothing specified) | skipped | single-model |
| 7. Premium without decorative shadows? | YES: all layout and hairlines | skipped | single-model |
| Hard rejections | "App UI of stacked bands": today's screen hits (five bands before content); plan avoids it only if chrome collapses | skipped | single-model |

### 7.2 Issues as found (by pass; all resolved in §6)

**Pass 1 — Information architecture**
- P1-1 Edit-state chrome + hierarchy: the plan's thesis ("only the line, the video, the keyboard") is never made a requirement. App header, segmented control and "Rendered on iPhone" label stay; preview math then leaves ~80pt width on SE. Order of elements in the bar unspecified; no position count ("4 of 20").
- P1-2 Browse-state headings: three stacked labels and two exits (panel Done, header Save) before the first row.
- P1-3 No way to delete a line (ASR hallucinations on silence are routine); today an empty string is saved as a ghost row.

**Pass 2 — Interaction states**
- P2-1 No state table: loading/transcribing, zero captions, save failure, unsaved edits across lines, interruption (call / background with keyboard up), rotation, playback while editing, success.
- P2-2 "Saves" means three things (line commit vs header Save vs render); exits collide (Return, ‹ ›, ✓, swipe-down, preview-tap). Next on an untouched line must have no side effects (no undo entry, no dirty mark).
- P2-3 Word-by-word caption styles: which frame to park on, and how per-word timings redistribute when the word count changes.
- P2-4 Guided-story captions do not seek on tap today (code fact §2a); "right frame" needs a target time.

**Pass 3 — Journey**
- P3-1 No storyboard for a 20-line transcript: after the first Next the list is gone and nothing says where you are; cursor-at-end means start-of-line typos still need a second tap; finishing must land on the same line in the list; after line 20 Return = Done.

**Pass 4 — Specificity / slop**
- P4-1 Vague phrases: "compact edit bar", "about 3 lines", "small play button", "real caption style", "paused on the line's start time", "swipe the keyboard down", "scrolled into view", "one undo step per line", "when installed", and "decide during build" items that are design decisions.
- P4-2 Preview tap = exit breaks the editor-wide convention (tap video = play/pause).
- P4-3 Edit state keyed on `keyboardVisible` fails with a hardware keyboard (no software keyboard ⇒ bar never appears); the bar needs the keyboard safe-area or `UIKeyboardLayoutGuide`, and interactive swipe-dismiss needs a scroll view to exist in edit state.
- P4-4 Reusing one UITextView across lines leaks the system undo stack (shake-undo after Next edits the wrong line).

**Pass 5 — Design system**
- P5-1 No tokens/components named: bar surface, field, loop-play, Done, playing-row highlight (selectionSoft on paper ≈ 1.1:1, needs a non-color cue), edited-row marker; inline helper copy "Tap a line to edit…" violates §9; KRI-148 runbook contract (`shrinksPreviewWhileTyping` boolean) needs an explicit Captions branch and a doc update; `NativeEditorLayoutMetricsTests` keyboard fixture doesn't reproduce the real viewport.
- P5-2 Language: no caption language on the iOS model (§2a). Needs a server-authoritative field, `textInputMode` fallback, `spellCheckingType = .no` when keyboard ≠ caption language, locale-aware uppercasing for uppercase styles (`"iyi".uppercased()` → "IYI"), explicit autocapitalization.

**Pass 6 — Responsive & accessibility**
- P6-1 AX sizes on SE: body at AX5 ≈ 53pt ⇒ 3 lines ≈ 190pt + nav row + keyboard ⇒ preview gets nothing. Needs a fallback layout.
- P6-2 VoiceOver: rows are not combined elements, chevron audible, no position announcement on Next, button labels unspecified, auto-scroll under VoiceOver.
- P6-3 Reduce Motion and hardware keyboard behaviour unspecified (Tab/Shift-Tab, Return, Esc, Shift-Return).
- P6-4 Playing-line auto-follow: suspend once the user scrolls, resume on next play; overlapping-cue rule.

**Pass 7 — Unresolved decisions:** see the register in §6 (D16).


## 8. NOT in scope

- **Text tool fill-above-bar** — deferred to TODOS.md `T-CAP025-1` (D10/D17); Text keeps its 120pt compact preview.
- **Timing edits (start/end) from the phone** — the panel only passes `text:`; the web's KRI-18 lock is mirrored. Unchanged.
- **Find / Replace-all** — exists on web `CaptionsDrawer`; not in KRI-240. Separate issue if wanted on iOS.
- **Re-transcribe / language chip on iOS** — D11 only reads the language; changing it stays on web.
- **Chat/Editor switch while typing** — hidden with the rest of the chrome in edit state (D2). Accepted consequence: switch tabs after Done.
- **KRI-241 (silent preview after a caption edit)** — separate issue, separate fix; D6's loop-play must not regress it.
- **Landscape** — edit state is portrait-only (D4).
- **Codex outside voice** — skipped by user instruction ("orchestrate sonnet agents"); outside coverage is recorded as skipped, not as clean.

## 9. What already exists (reuse, don't reinvent)

- `NativeExplicitLineTextEditor` (`NativeEditorTextPanel.swift:500-564`): UIKit first-responder focus that works; base for the caption editor (add `autocorrectionType`, `spellCheckingType`, `textInputMode`, `returnKeyType`, key commands).
- `shrinksPreviewWhileTyping` per-tool hook (`NativeEditorView.swift:229`) → becomes the `TypingLayout` enum (D10).
- `session.select` / `seek(to:)` (pauses + seeks), `beginTransaction` / `endTransaction` (one undo step per line), `updateCaptionCue(id:text:)` with the guided-story text-only guard.
- `captionUnits` / `isCaption` / `isCaptionCueMirror` (`NativeEditorDocument.swift`) for the cue-vs-guided distinction.
- Panel header "Done" (word), `scrollDismissesKeyboard(.interactively)`, KRI-235 header drag, KRI-170 fullscreen gate, Save checkmark/dirty rule (2026-09-15 decision).
- Text panel `@ScaledMetric` usage; `AppleTextAccessibilityUITests` 320pt + AX harness; `accessibilityAction` patterns on text rows.
- Web `CaptionsDrawer.tsx`: `aria-current` playing row + lime fill, `scrollIntoView` suppressed while editing, en/tr language chip (source of the D11 field).
- Tokens: `selectionSoft`, `sky`, `lilac` + `plum` (text-editing tools), `butter` (primary action), `softZinc`, `line`, `mutedInk`, `zinc`; `KriaFont.body` scales with Dynamic Type.

## 10. Engineering review (`/plan-eng-review`, 2026-10-02)

**Target:** this plan (`plans/025-kri-240-caption-text-editing.md`). **Code base:** origin/main `7672ded66` (42 commits after the design review's `572d02d1d`). Primary reviewer: Opus 5.5; code maps by two Sonnet explorers (iOS save/render path; server language + word timing).

### 10.1 Scope record (Step 0 complexity gate)

feature answers: D19 = A (defer D11 item 4, locale-aware uppercasing → TODOS.md `T-CAP025-2`); structure: D20 = Smaller arrangement; accepted scope: every approved behaviour in D2–D16 except D11 item 4, built as: extend `NativeExplicitLineTextEditor` with a configuration struct (return key, autocorrect, spell-check, input mode, key commands) shared by Text and Captions; reuse `deleteSelection(.captionCue)`; read `rawRoot["caption_language"]`; loop-play via session seek + boundary observer in `NativeEditorSession`; edit bar in `NativeEditorLanePanel.swift`, undo toast in `NativeEditorComponents.swift`, `TypingLayout` in `NativeEditorIsland.swift`; no DesignSystem, KriaMediaEngine, `Services.swift` or openapi edits (≈15 files, 3 new types: `TypingLayout`, `CaptionEditBar`, `UndoToast`); pending remedies: R1 (word-timing rule location), R2 (guided-story Delete), R3 (save-failure banner).

### 10.2 Factual corrections (no behaviour change; no question needed)

- **F1 Base moved.** origin/main is `7672ded66`; line numbers in §2a/§9 are from `572d02d1d` and have shifted (e.g. `updateCaptionCue` is now `NativeEditorSession.swift:3810`).
- **F2 `caption_language` already in the payload** (server explorer): set at `tasks/generative_build.py:24475` (cloud subtitled), `:6119` (phone subtitled), `:6624` (phone narrated); survives finalize (`:28666`); returned by `get_generative_job_status` (`routes/generative_jobs.py:11369`) as a pass-through dict; iOS keeps the variant as `rawRoot` (`NativeEditorDocument.swift:297-300`). Not in `Services.swift:1693` `directKeys`, which only matters for draft rebase. Applied to D11 item 1.
- **F3 Line deletion exists** (#1319): `deleteSelection` case `.captionCue` (`NativeEditorSession.swift:3620-3628`). Applied to D3.
- **F4 Portrait already locked on iPhone** (`Kria/Support/Info.plist:26-27`). Applied to D4.
- **F5 A save-failure banner exists**: `NativeEditorSaveBanner` (`NativeEditorComponents.swift:67`, shown at `NativeEditorView.swift:249`) renders "Couldn’t save this edit" + detail in the top chrome; 422 maps to `EditorSaveError` ("This save was rejected. Your edits are still here."). Feeds R3.
- **F6 Chrome ownership.** `NativeEditorView.editor(viewport:)` owns `NativeEditorProjectHeader` (`WorkspaceTopRow` + `WorkspaceModeSwitch`, `:242-247`) and the "Rendered on iPhone" button (`:256-261`); `NativeCaptionPanel` is a child built at `:499-500`. Hiding chrome (D2) is a `NativeEditorView` state driven by the caption editor's focus. `NativeEditorLayoutMetrics.headerHeight = 94` (`NativeEditorIsland.swift:49`) is a fixed constant and must drop to 0 in edit state or the preview budget is 94pt short. The KRI-197 runbook marks the header height as load-bearing for `NativeEditorInspectorUITests` / `NativeCaptionVisualUITests` geometry assertions.
- **F7 Keyboard geometry.** The editor reads only `keyboardVisible` (`NativeEditorView.swift:161-162`); the root `GeometryReader` already shrinks with the keyboard safe area, so a bar at the bottom of the panel area sits on the keyboard, and with a hardware keyboard on the bottom safe area. No `keyboardLayoutGuide` / `inputAccessoryView` / `UIKeyCommand` exists anywhere. D14's "bar on the keyboard layout guide" is satisfied by the existing safe-area behaviour; edit state must key on the text view's focus binding, not `keyboardVisible`.
- **F8 KRI-241 dependency.** Loop-play with audio (D6/D8) needs the preview voice to survive a caption edit. KRI-241 (fast-path `updateText` replaces `audioMix` and the voice goes silent) has a fix in review: PR [emirerben/nova#1322](https://github.com/emirerben/nova/pull/1322). Build loop-play on top of it.
- **F9 CI mapping** (`scripts/ios/ui-test-groups.json`, `ui_tests.py:150-186`): `NativeEditorLanePanel`, `NativeEditorTextPanel`, `NativeEditorView`, `NativeEditorSession`, `NativeEditorComponents`, `NativeEditorRenderCompiler`, `NativeEditorDocument`, `NativeEditorUITestFixtures`, `NativeEditorInspectorUITests` map to the `editor` group. `DesignSystem/*`, `KriaMediaEngine/*`, `Info.plist`, `Services.swift` are unmapped and drop a PR to smoke-only UI tests. New UI test methods must be registered in `ui-test-groups.json` (and durations).
- **F10 Test locator.** `testTappingCaptionRowEntersEditModeAndPersistsTypedText` (`NativeEditorInspectorUITests.swift:1168-1198`) finds the field via `app.textFields`; a `UITextView` is an `app.textViews` element, so the test must change.
- **F11 Caption render compiler lives in the app target** (`Kria/Core/NativeEditorRenderCompiler.swift:423-515`), not `KriaMediaEngine`. Swift tests for it are `KriaTests` (xcodebuild), not `swift test`.
- **F12 Word timings today.** Cues carry `raw["words"]` (`[{text,start_s,end_s}]`). iOS compiler (`:466-482`) and cloud reburn (`captions.py:748` `_words_match_text`, `:761`) use stored words only on an exact token match, else an even split with a 0.05s floor (`word_timing.py:36-117`). The phone caption compiler (`phone_captions.py:364-377`) has no match check. `encodeCaption` (`NativeEditorDocument.swift:625-628`) and editor-commit (`routes/generative_jobs.py:9734-9736`) keep stale `words`. Existing test `testWordCaptionsUseStoredTimingsUntilTheirTextIsEdited` (`NativeEditorRenderCompilerTests.swift:1003`) pins the iOS even-split fallback.

### 10.3 Architecture spec (implements approved D2, D5, D6/R1, D14; no new decisions)

```
 NativeEditorView (@State panel, keyboardVisible, editingCueID mirror)   [D27: editingCueID is the truth]
  │  editingCueID != nil  ⇒  edit state
  │    • hide NativeEditorProjectHeader (WorkspaceTopRow + WorkspaceModeSwitch),
  │      "Rendered on iPhone" button, transport, tool rail, fullscreen entry
  │    • NativeEditorLayoutMetrics(headerHeight: 0, typingLayout: .fillsAboveEditBar(minPreviewHeight: 220))
  │  editingCueID == nil  ⇒ browse state (today's chrome, headerHeight 94, TypingLayout per tool)
  ▼
 NativeCaptionPanel(session, editingCueID: $editingCueID)   (focus requested on set / Next / Previous)
  ├─ browse: ScrollViewReader list → rows (combined a11y, dot, playing highlight, swipe Delete)
  └─ edit:   CaptionEditBar
       ├─ NativeExplicitLineTextEditor(config: .captionLine(language:, isLast:))   ← one tap = focus on attach
       │     Return → onNext / onDone · Tab/Shift-Tab/Esc/Cmd-Z key commands · paste "\n" → " "
       │     textDidChange → session.updateCaptionCue(id:text:)  ─┐
       ├─ loop-play button → session.loopLine(id)                  │
       └─ Previous · "4 of 20" · Next · Done                       │
                                                                   ▼
 NativeEditorSession.updateCaptionCue (cue path)
   text + raw["words"] rewritten by CaptionWordRewrite (D6 rule)  ── same rule ──►  word_timing.rewrite_cue_words (Python)
   one transaction per line (begin on focus, end on leave; untouched ⇒ no undo)          ▲ editor-commit / persist_variant_captions
   preview recompiles (fast path; voice fix = PR #1322)                                  ▲ phone_captions._prepare_cues (legacy guard)
                                                                   shared fixture: tests/fixtures/caption_word_rewrite.json
```

- **Editor configuration (D20).** `NativeExplicitLineTextEditor` gains `LineEditorConfiguration` with defaults equal to today's Text behaviour (`returnKeyType = .default`, autocorrection default, `widthTracksTextView = false`, no key commands, no auto-focus). `.captionLine` sets: wraps lines (`widthTracksTextView = true`, height via `sizeThatFits`, cap 3 lines / 2 on SE-class and AX sizes, internal scroll beyond), `returnKeyType .next`/`.done`, `autocorrectionType .no`, `spellCheckingType` per D11, `autocapitalizationType .sentences`, `smartQuotes/Dashes .no`, `textInputMode` override (match `primaryLanguage` by language subtag, e.g. `tr` ↔ `tr-TR`; nil ⇒ system keyboard), key commands per D14, focus on attach, `undoManager?.removeAllActions()` on line change. `updateUIView` must not reassign identical text (caret stability).
- **Loop-play.** `NativeEditorSession` seeks to the park frame and adds an `AVPlayer` boundary/periodic observer for `[startS, endS)`; any keystroke or Done removes it. Depends on PR #1322 (F8).
- **Failure modes covered here:** focus request before window attach (handled in `didMoveToWindow`, the Text panel's working path); keyboard frame not observed (layout keyed on focus, not keyboard notifications, F7); stale `words` (R1).

### 10.4 Test review

Framework: CLAUDE.md quality checks — `pytest` (src/apps/api), `make ios-verify` / xcodebuild `KriaTests` + `KriaUITests`; PR UI selection via `scripts/ios/ui-test-groups.json` (`editor` group).

```
CODE PATHS (proposed)                                         USER FLOWS
[+] NativeEditorTextPanel.swift (LineEditorConfiguration)      [+] Fix a 20-line TR transcript
  ├── [GAP] focus on attach → keyboard after one row tap         ├── [GAP] [→E2E] tap row → type → Return ×N → Done → Save → reopen persists
  ├── [GAP] wrap + grow 1→3 (2 on SE/AX); Text stays no-wrap     ├── [GAP] [→E2E] clear line + Return → removed; Undo restores
  ├── [GAP] Return → next/done; paste "\n" → space               ├── [GAP] background mid-edit → committed, lands in browse on that row
  ├── [GAP] flags: autocorrect off, spell-check iff lang match   └── [GAP] device: TR keyboard + recordings (T14)
  ├── [GAP] textInputMode subtag match / nil fallback          [+] Error states
  ├── [GAP] key commands Return/Shift-Return/Tab/Esc/Cmd-Z       ├── [GAP] Save fails → banner + "Try saving again"
  └── [GAP] undoManager cleared per line                         └── [GAP] read-only captions → reason line; tap still seeks
[+] NativeEditorSession.swift                                  [+] Accessibility
  ├── [GAP] updateCaptionCue rewrites words (same/diff count,    ├── [GAP] AX5 @ 320pt: field + 4 controls on screen, keyboard up
  │         no words, already matching, empty) + fixture         └── [GAP] VoiceOver: combined row label; Edit focuses field;
  ├── [★★ TESTED] guided text-only update (SessionTests)                   announcement via announcer seam (XCUITest cannot read
  ├── [GAP] seek to park frame — cue AND guided (guided = bug)             UIAccessibility announcements)
  ├── [GAP] loop observer starts/stops; stops on keystroke
  ├── [GAP] untouched line → no undo entry, no dot
  └── [GAP] first keystroke pauses playback
[+] NativeEditorLanePanel.swift (panel + CaptionEditBar)
  ├── [GAP] rows: no chevron, combined label, dot, playing highlight
  ├── [GAP] bar: counter; Previous disabled on 1; Next disabled on N
  ├── [GAP] swipe Delete cue + guided → deleteSelection; Undo toast
  ├── [GAP] follow: center while playing; suspend on drag; resume on play; off in edit/VO
  ├── [GAP] read-only: seek only + reason line
  └── [GAP] loading skeleton; empty state
[+] NativeEditorView.swift / NativeEditorIsland.swift
  ├── [GAP] TypingLayout: Text 120 / Captions ≥220 / others split at SE 667, 15 852, Pro Max 932 viewports (keyboard up)
  ├── [GAP] edit state hides chrome with hardware keyboard (no keyboardVisible)
  └── [★★★ TESTED] browse-state header/panel geometry (Inspector + CaptionVisual UI tests) — regression
[+] NativeEditorComponents.swift
  ├── [GAP] SaveBanner .failed → retry button calls save()
  └── [GAP] UndoToast 4s; Undo; announced
[+] NativeEditorRenderCompiler.swift — unchanged
  └── [★★★ TESTED] testWordCaptionsUseStoredTimingsUntilTheirTextIsEdited stays (render fallback)
[+] SERVER
  ├── [GAP] word_timing.rewrite_cue_words (+ shared fixture)
  ├── [GAP] editor-commit / persist_variant_captions normalise stale words; matching cues byte-identical (test_editor_commit.py)
  └── [GAP] phone_captions._prepare_cues legacy guard; matching rows compile byte-identical (test_phone_captions.py, test_phone_subtitled_plan.py)

LLM integration: none (no prompt or agent changes).
COVERAGE: 3/38 paths tested (8%) | Code paths: 3/30 | User flows: 0/8
QUALITY: ★★★:2 ★★:1 ★:0 | GAPS: 35 (2 E2E, 0 eval)
Legend: ★★★ behavior + edge + error | ★★ happy path | ★ smoke | [→E2E] UI/integration test
```

Notes: `testTappingCaptionRowEntersEditModeAndPersistsTypedText` must change (one tap, `app.textViews` locator, F10). New UI test methods must be added to `scripts/ios/ui-test-groups.json` (`editor`) and durations (F9). Swift fixture tests live in `KriaTests` (app target, F11) and resolve the fixture via `#filePath` like `DissolveTimingTests`.

### 10.5 Performance review

No issues found. Keystrokes inside a typing transaction already coalesce preview rebuilds at 80 ms (`NativeEditorSession.swift:2062-2079`, `previewCoalesceMilliseconds = 80`), written for "~170 caption layouts per sample" on guided stories; the plan keeps one transaction open per line, so coalescing applies. The D6 word rewrite is O(words) per keystroke; follow-highlight lookup is O(cues) per tick against a 500-cue cap.

### 10.6 Outside voice (Sonnet subagent, read-only; Codex skipped per the user's "orchestrate sonnet agents" instruction)

Verified by the primary reviewer before raising: #1 (`TextTransformTiming.swift:38-40` `alpha: min(1, max(0, localTime / 0.12))`; compiler `:512` uses `.captionPop` for `subtitled`), #2 (`NativeEditorSession.swift:1107-1116`: document = `snapshot.editorDraft(…authoritativeVariant:)`; `Services.swift:1692-1709` copies only `directKeys`, no `caption_language`), #3 (no `swipeActions`/`onDelete` in `Kria/`; list is `VStack` in `ScrollView`, `NativeEditorLanePanel.swift:47`), #9 (#1322 merged `dc10f6eca`; #1321 `bedfa2cc7` changed `panelMaxHeight`), #10 (`testflight.yml:6-23` runs on every successful `main` iOS workflow; `:220` "Upload and distribute to external TestFlight"). #4–#8 are reasoning findings (confidence 6–8) checked against the cited code paths.

Summary of the subagent's findings (full text retained in the session transcript): (1) D6 park frame at `startS` is a blank frame for `captionPop` captions; (2) F2 mechanism wrong, language never reaches the document; (3) swipe Delete has no mechanism; (4) edit state keyed on focus has two sources of truth and can strand the user with the header hidden; keyboard swipe-down exit has no surface; spike focus on device; (5) real container chrome (44pt timeline handle, 10pt preview padding, island grabber band + paddings, sparkles FAB, newer-job prompt) puts SE at ~189pt < 220 floor; non-9:16 aspects are width-bound; no state for a failed/unavailable live preview; (6) per-keystroke word rewrite is lossy (space then delete re-spreads; phantom undo/dot); ignores `timing_quality`, `smart_word_ids`/`smart_keep_together`; server "words don't spell text" rule would flatten non-whitespace languages; reproduce the stale-words burn in pytest first; (7) R1 option C wording; the server fix can ship alone; (8) Undo toast vs linear undo stack; (9) plan stale (F8, §2a, Implementation Tasks not reconciled); (10) TestFlight ships partial slices; new UI tests must be registered; the row test also uses the hidden panel Done; (11) one PR is too big — proposes PR0 spike … PR6. Recommendation: revise before building.

### 10.7 Factual corrections from the outside voice (no behaviour change)

- **F2 (revised).** `rawRoot` is the chat **draft** snapshot, not the variant; `caption_language` never reaches the document. Mechanism: capture `caption_language` from `authoritativeVariant` in every variant-load path in `NativeEditorSession` (beside `configureCapabilities(from:)` at `:1116`; reload paths at `:1233-1244`, `:1499-1515`, `:1747`, `:4202`, `:4273`). Stays inside `NativeEditorSession` (approved D20 structure; no `Services.swift` change). Verify on a real load (unit test with a variant fixture carrying `caption_language`).
- **F8 (revised).** PR #1322 (KRI-241) is merged (`dc10f6eca`); loop-play's dependency is satisfied.
- **F13.** #1321 (KRI-275, `bedfa2cc7`) lets a keyboard-up panel grow over the shrunk preview (`panelMaxHeight` no longer capped at `panelBudget`). §2a's "panel resize is inert with the keyboard up" is stale; `TypingLayout.compactPreview(120)` must keep #1321's Text behaviour.
- **F14.** TestFlight uploads every green `main` iOS run to the external tester group (`testflight.yml:6-23`, `:220`); any merged partial slice reaches testers.
- **F15.** XCUITest cannot observe `UIAccessibility.post(.announcement)`; D13's "Line 5 of 20" proof becomes a unit test through an injectable announcer.
- **F16.** The rewritten caption-row UI test cannot use the panel's `native-editor-captions-done` in edit state (hidden); it uses the bar's Done.
- **F17.** origin/main is now `dcde00488`.

## Decision ledger

### R1: Where the D6 word-timing rule is applied
Finding: #1, P1, confidence 8/10, `src/apps/api/app/pipeline/phone_captions.py:366-369` + `src/apps/api/app/pipeline/portable_text_layout.py:663-671` + `src/apps/ios/Kria/Core/NativeEditorDocument.swift:625-628`; reviewer: primary (Opus 5.5) from server explorer (Sonnet) evidence, verified by direct read.
Motivating code:
```python
# phone_captions.py:366-369
use_karaoke = "words" in cue and (
    style == "word" or resolved_look.highlight_spoken_word is True
)
word_timings = _relative_word_timings(cue, cue["words"]) if use_karaoke else []
# portable_text_layout.py:663-671 — burned words come from word_timings, not the cue text
for entry in overlay.get("word_timings") or []:
    text = str(entry.get("text", "")).strip()
    ...
    words.append(text)
```
Reached from the phone Talking Save recompile (`services/phone_editor.py:122-123` → `phone_subtitled_plan.py:342`) and the authored phone timeline (`phone_authored_timeline.py:173`). iOS `encodeCaption` merges `item.raw` and overwrites only `start_s`/`end_s`/`text`/`id`, so a text edit commits the old `words`.
Plan baseline: D6 (approved 2026-10-02): same word count ⇒ keep each word's timing by position; different count ⇒ spread across `[startS,endS)` by character weight; "shared by `KriaMediaEngine` and the server reburn"; the location of the rule was not specified.
Runtime evidence: static read only (not reproduced on device). Effect: on a word-style phone Talking edit, fixing "Issizim" → "İşsizim" previews correctly (iOS compiler falls back to even split on token mismatch) but the Saved phone render burns "Issizim". Cloud reburn falls back to an even split (correct text, D6 timing not applied).
Comparison grid:

| Choice | Current | A | B | C |
|---|---|---|---|---|
| R1 where the D6 rule runs | Nowhere; readers disagree (iOS/cloud even split on mismatch, phone compiler burns stale words) | Write-time on both sides: iOS rewrites `raw["words"]` in `updateCaptionCue`; server rewrites at editor-commit/persist; phone compiler guard for legacy rows | Write-time on iOS only | Render-time in all three readers |
| Stored `words` after an iOS edit | Stale (old text) | Spell the new text | Spell the new text | Stale |
| Stored `words` after a web edit | Stale | Spell the new text (server rewrite) | Stale | Stale |
| Phone render of an edited word-style cue | Old words | New words, D6 timing | New words for iOS edits; old words for web edits and legacy rows | New words, D6 timing |
| Cloud reburn of an edited cue | Even split, new text | D6 timing via stored words | D6 for iOS edits; even split otherwise | D6 timing |
| Parity proof | None | Shared fixture `tests/fixtures/caption_word_rewrite.json`, asserted by Swift `KriaTests` and pytest | Swift unit tests only | Fixture across three readers (Swift, cloud, phone compiler) |
| Server deploy | — | Yes, independent of the app (no ordering) | No | Yes |
| D6 behaviour (approved) | fixed | unchanged | unchanged | unchanged |

Question D21:
D21 — Where should the word-timing rule run, given stale words burn the old text in phone renders?
Project/branch/task: nova main @ 7672ded66, plan 025 (KRI-240), eng review Section 1 (Architecture), finding R1.
ELI10: Each caption line stores the timing of every word, plus the word's text. When you fix a typo, the app changes the line's text but keeps the old word list. The phone preview notices the mismatch and spaces the words evenly, but the server's phone-render compiler (phone_captions.py:366-369, portable_text_layout.py:663-671) draws the words from the old list. So on word-by-word captions the saved video still shows the typo. The approved timing rule (D6) has to live somewhere that every renderer sees.
Stakes if we pick wrong: the creator fixes 'Issizim' to 'İşsizim', the preview shows it, and the exported phone video still says 'Issizim'.
Recommendation: A because rewriting the stored word list when the text changes fixes every renderer at once, covers web edits and old rows, and needs no renderer to remember a rule.
Completeness: A=10/10, B=6/10, C=8/10
Pros / cons:
A) Write-time, both sides (recommended) (human: ~1.5 days / CC: ~1h)
  ✅ Stored words always spell the text, so iOS preview, cloud reburn and phone compile agree with no renderer change
  ✅ Server rewrite at editor-commit also fixes web edits; a guard in the phone compiler fixes rows already saved stale
  ❌ Two implementations of one rule (Swift and Python) that must stay in step via a shared fixture
B) Write-time, iOS only (human: ~4h / CC: ~20 min)
  ✅ Smallest change; no server deploy; fixes every edit made on the iPhone
  ✅ Renderers stay untouched, so no regression risk on the cloud or phone compile paths
  ❌ Web edits and already-saved stale rows still burn the old words in phone renders
C) Render-time, all readers (human: ~1.5 days / CC: ~1h)
  ✅ Fixes legacy rows and every client without touching what is stored
  ✅ No change to the save path or the editor-commit contract
  ❌ Three readers (Swift compiler, cloud reburn, phone compiler) must each apply the rule; stored data stays wrong for any future reader
Net: A fixes the data once and guards old rows; B is the cheap iPhone-only fix; C fixes the readers and leaves the data stale.
Header: R1 word rule
Options (as asked):
A) Write-time, both sides (recommended)
iOS `updateCaptionCue` rewrites the cue's `raw["words"]` per D6 on every text change (preview and commit agree); server applies the same rule when editor-commit / `persist_variant_captions` stores a cue whose words no longer spell its text; `phone_captions._prepare_cues` applies it to legacy stale rows; shared fixture `tests/fixtures/caption_word_rewrite.json` asserted by Swift `KriaTests` and pytest. Human ~1.5 days / CC ~1h. Risk: two implementations of one rule; maintenance: fixture keeps them in step.
B) Write-time, iOS only
iOS `updateCaptionCue` rewrites `raw["words"]` per D6; no server change. Human ~4h / CC ~20 min. Risk: web edits and already-saved stale rows still burn old words in phone renders; maintenance: none beyond Swift tests.
C) Render-time, all readers
Leave stored `words` as-is; apply D6 inside the iOS compiler (`NativeEditorRenderCompiler.swift:466-482`), the cloud reburn (`captions.py:761`) and the phone compiler (`phone_captions.py:366-369`) whenever words don't spell the text; fixture across all three. Human ~1.5 days / CC ~1h. Risk: every future reader must remember the rule; maintenance: three call sites.

State: approved
Actual answer: A) Write-time, both sides (recommended) — user answer to D21, 2026-10-02
Accepted scope: (1) iOS: `updateCaptionCue` (cue path, `NativeEditorSession.swift:3823-3828`) rewrites the cue's `raw["words"]` per D6 on every text change, so preview and commit agree; guided-story text elements carry no `words` and are unaffected. (2) Server: one Python helper (in `app/pipeline/word_timing.py`, beside `rebuild_word_timings_for_text`) applies the same rule; editor-commit (`routes/generative_jobs.py:9734-9736`) and `persist_variant_captions` (`:3478`) call it for any cue whose `words` no longer spell its `text` (idempotent when they already do). (3) `phone_captions._prepare_cues` (`:464-466`) applies it to legacy stale rows before compile. (4) Shared fixture `src/apps/api/tests/fixtures/caption_word_rewrite.json` asserted by a Swift `KriaTests` case and a pytest case (pattern: `phone_dissolve_timing.json`). Server change deploys independently of the app. Renderer read paths (iOS compiler, cloud reburn) stay unchanged. D6 behaviour unchanged.
History: Accepted scope refined by R11 (D31 = A, 2026-10-02): iOS rewrite is a pure function of the line-entry words and current text with exact restore; server rewrites only cues whose text changed vs the stored cue by id; phone guard compares whitespace-free concatenations; `timing_quality` handled; cue-level `smart_*` untouched; failing reproducer pytest first.

### R2: Delete for guided-story captions in the Captions panel
Finding: #2, P2, confidence 9/10, `src/apps/ios/Kria/Features/NativeEditorSession.swift:3558-3561` + `:3607-3619` (origin/main 7672ded66, from #1319); reviewer: primary (Opus 5.5), verified by direct read.
Motivating code:
```swift
// NativeEditorSession.swift:3558-3561 — captions are no longer blocked
func textDeletion(id: String) -> TextDeletion {
    guard document.textElements.contains(where: { $0.id == id }) else { return .blocked("This text no longer exists.") }
    return .allowed
}
// :3609-3610 — a guided caption text element becomes a server-validated caption_cue deletion
let lyricID = lyricDeletionID(for: element)
let deletion = EditorDeletion(kind: element.isCaption ? "caption_cue" : (lyricID == nil ? "text" : "lyric_line"), id: lyricID ?? element.id)
```
Server accepts the kind (`routes/generative_jobs.py:1292` lists `"caption_cue"`; editor-commit checks `"caption_cue" in payload._deleted_kinds` at `:9726`). #1319 removed the old block `"Captions are managed in the Captions panel."`.
Plan baseline: D3 (approved 2026-10-02) + D3 clarification: guided-story rows expose no Delete swipe and block an empty commit (field reverts), on the premise that guided captions are server-owned and cannot be removed from the phone.
Runtime evidence: static read of origin/main. A guided caption can now be deleted from the timeline/inspector paths; the Captions panel under D3 would be the only surface that refuses.
Comparison grid:

| Choice | Current (approved D3) | A | B | C |
|---|---|---|---|---|
| R2 guided-story Delete in the Captions panel | No swipe Delete; empty commit reverts | Swipe Delete + empty commit removes the line with the Undo toast, via `deleteSelection(.captionCue)` | No swipe Delete; empty commit reverts (D3 as approved) | No swipe Delete; empty commit reverts |
| Guided caption delete from timeline/inspector (#1319) | Allowed | Allowed | Allowed | Blocked again (restore the pre-#1319 guard in `textDeletion`) |
| D3 behaviour for `caption_cues` rows | approved | unchanged | unchanged | unchanged |
| D13 VoiceOver Delete action on guided rows | absent | present | absent | absent |

Question D22:
D22 — Should guided-story captions get Delete in the Captions panel, now that #1319 lets the timeline delete them?
Project/branch/task: nova main @ 7672ded66, plan 025 (KRI-240), eng review Section 1 (Architecture), finding R2.
ELI10: The design review decided guided-story captions (the sentence captions on guided edits) can't be deleted from the Captions list, because at the time the app couldn't remove them. Since then Emir's #1319 made every editor block deletable, including guided captions: the timeline and inspector can delete one, and the server accepts it with undo. So the Captions list would be the only place that says no.
Stakes if we pick wrong: creators see a Delete on the timeline but not in the list for the same caption, or we undo part of a teammate's merged change.
Recommendation: A because the server already validates the deletion and the Undo toast makes it safe; one rule everywhere beats a list that is stricter than the timeline.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Allow Delete for guided captions (recommended) (human: ~1h / CC: ~5 min)
  ✅ Same answer on the timeline, the inspector and the Captions list for one caption
  ✅ Reuses deleteSelection(.captionCue) and the D3 Undo toast; no new code path
  ❌ Reopens the D3 guided-story clause the design review approved earlier today
B) Keep D3 as approved (no Delete, empty reverts)
  ✅ Honours the design decision exactly; no change to the plan
  ✅ Guided sentence captions can't be removed by an accidental clear-and-Return
  ❌ The list refuses what the timeline allows for the same caption, which reads as a bug
C) Keep D3 and re-block guided deletion everywhere (human: ~2h / CC: ~15 min)
  ✅ One consistent rule, matching D3's original premise
  ✅ No guided caption can disappear from any surface
  ❌ Reverses part of a teammate's merged #1319 inside an unrelated PR
Net: A aligns the list with what main already allows; B keeps the design decision at the cost of inconsistency; C buys consistency by undoing #1319.
Header: R2 guided Delete
Options (as asked):
A) Allow Delete for guided captions (recommended)
Guided-story rows get the same swipe "Delete" and empty-commit removal with the Undo toast as cue rows, through `deleteSelection(EditorSelection(kind: .captionCue, id:))`; VoiceOver Delete action included. Human ~1h / CC ~5 min. Risk: low, server already accepts the deletion; maintenance: none.
B) Keep D3 as approved
Guided-story rows keep no Delete and the empty commit reverts; the timeline/inspector still allow deletion (#1319 unchanged). Human 0 / CC 0. Risk: inconsistent surfaces for one caption; maintenance: none.
C) Keep D3 and re-block guided deletion everywhere
Guided-story rows keep no Delete; restore a guard in `textDeletion`/`deleteSelection` so guided captions can't be deleted from the timeline or inspector either. Human ~2h / CC ~15 min. Risk: reverses merged #1319 behaviour owned by another author; maintenance: a guard to keep in sync.

State: approved
Actual answer: A) Allow Delete for guided captions (recommended) — user answer to D22, 2026-10-02
Accepted scope: Guided-story caption rows (`text_elements` with `isCaption`) get the same swipe "Delete", empty-commit removal with the Undo toast, and VoiceOver Delete action as `caption_cues` rows, all through `session.deleteSelection(EditorSelection(kind: .captionCue, id:))`. No timing controls for guided rows (unchanged). #1319 behaviour unchanged. D3 guided clause and D3 clarification amended in §6.
History: D3 (design review, 2026-10-02) approved "no Delete, empty commit reverts" for guided-story rows; reopened because #1319 (on main after the design review's code map at 572d02d1d) changed the premise that guided captions cannot be removed from the phone.

### R3: Save-failure surface for caption edits
Finding: #3, P2, confidence 9/10, `src/apps/ios/Kria/Features/NativeEditorComponents.swift:89-90` (origin/main 7672ded66); reviewer: primary (Opus 5.5) from iOS explorer (Sonnet) evidence, verified by direct read.
Motivating code:
```swift
// NativeEditorComponents.swift:89-90 — the editor-wide save banner, shown in the top chrome (NativeEditorView.swift:249)
case .failed(let message):
    banner(title: "Couldn’t save this edit", detail: message, systemImage: "exclamationmark.triangle", tint: .red)
// :91-99 — sibling cases already pair a banner with a 44pt retry Button, e.g.
Button("Retry render") { Task { await session.retryRender() } }
```
Plan baseline: D4 ERROR row (approved 2026-10-02): "Local draft persists, dots stay. Non-modal banner at the top of the panel: 'Couldn't save captions · Retry'. Never an alert over the keyboard."
Runtime evidence: static read. Every editor Save failure already shows the top-chrome banner above; it has no Retry action. Building D4 literally adds a second banner for the same failure.
Comparison grid:

| Choice | Current | A | B | C |
|---|---|---|---|---|
| R3 where a caption Save failure shows | Top-chrome `NativeEditorSaveBanner` "Couldn’t save this edit" (no Retry); D4 panel banner approved but not built | One banner: the existing top-chrome banner, plus a new 44pt "Try saving again" button on its `.failed` case (all editor saves) | Two banners: existing top-chrome banner + D4 panel banner "Couldn't save captions · Retry" | Existing top-chrome banner only, no Retry (creator taps Save again) |
| Retry affordance after a failed Save | None | Yes, for every editor Save | Yes, inside the Captions panel only | None |
| D4 ERROR row text | as approved | Amended to point at the existing banner + new button | unchanged | Amended to point at the existing banner |
| Other D4 rows (dots persist, no alert over keyboard) | approved | unchanged | unchanged | unchanged |

Question D23:
D23 — Reuse the editor's existing save-failure banner (with a new Retry button) instead of adding a second banner in the Captions panel?
Project/branch/task: nova main @ 7672ded66, plan 025 (KRI-240), eng review Section 2 (Code quality), finding R3.
ELI10: The design review asked for a banner at the top of the Captions panel saying 'Couldn't save captions · Retry'. The editor already shows a banner for every failed Save, 'Couldn’t save this edit', in the area under the header (NativeEditorComponents.swift:89-90). It just has no Retry button, unlike its neighbours for render and preview failures. Building the design literally would show two banners for one failure.
Stakes if we pick wrong: two stacked error banners for one failed Save, or a failure with no one-tap way to try again.
Recommendation: A because one banner per failure is clearer, and adding Retry to the shared banner fixes every save failure, not only captions.
Completeness: A=10/10, B=7/10, C=5/10
Pros / cons:
A) Existing banner + Retry (recommended) (human: ~1h / CC: ~5 min)
  ✅ One banner per failure, and every editor Save failure gains a one-tap Retry
  ✅ Reuses the banner + 44pt button pattern already used for render and preview retries
  ❌ Copy says 'this edit', not 'captions', and it sits under the header instead of inside the panel
B) Build the D4 panel banner too (human: ~3h / CC: ~15 min)
  ✅ Matches the approved design word for word, inside the panel the creator is looking at
  ✅ Caption-specific copy
  ❌ Two banners for one failure; the panel banner duplicates state the session already renders
C) Existing banner only, no Retry
  ✅ Zero new code
  ✅ No second banner
  ❌ No one-tap retry; the creator must find and tap Save again
Net: A trades caption-specific copy for one banner and a Retry everywhere; B keeps the design's copy at the cost of a duplicate banner.
Header: R3 save banner
Options (as asked):
A) Existing banner + Retry (recommended)
No Captions-panel banner. The `.failed` case of `NativeEditorSaveBanner` gains a 44pt "Try saving again" button calling `session.save()` (identifier `native-editor-retry-save`), visible in browse state; dots persist; never an alert. D4 ERROR row amended. Human ~1h / CC ~5 min. Risk: low; maintenance: one shared banner.
B) Build the D4 panel banner too
Keep the top-chrome banner unchanged and add the approved panel-top banner "Couldn't save captions · Retry" in `NativeCaptionPanel`. Human ~3h / CC ~15 min. Risk: duplicate banners for one failure; maintenance: two surfaces for one state.
C) Existing banner only, no Retry
No new banner and no Retry; D4 ERROR row amended to point at the existing banner. Human 0 / CC 0. Risk: no one-tap retry; maintenance: none.

State: approved
Actual answer: A) Existing banner + Retry (recommended) — user answer to D23, 2026-10-02
Accepted scope: No Captions-panel banner. `NativeEditorSaveBanner`'s `.failed` case (`NativeEditorComponents.swift:89-90`) gains a 44pt "Try saving again" button calling `session.save()`, identifier `native-editor-retry-save`, same style as the sibling retry buttons; it applies to every editor Save failure and shows in browse state (top chrome is hidden while a line is in edit). Unsaved dots persist; never an alert over the keyboard. D4 ERROR row amended in §6.
History: none

### R4: Regression contract for behaviour this plan puts at risk
Finding: #4, P1 (CRITICAL regression rule), confidence 9/10; reviewer: primary (Opus 5.5).
At-risk existing behaviour (motivating code):
- Text tool editor shared via D20: `NativeExplicitLineTextEditor` (`NativeEditorTextPanel.swift:500-541`) sets `returnKeyType = .default` and `widthTracksTextView = false` (explicit lines, no wrap); guarded by `NativeEditorInspectorUITests.swift:21` ("Opening a tab must not take keyboard focus"), `:164` `testKeyboardKeepsSourcePreviewVisibleAboveConnectedPanel`, `EditorUITests.swift:185`/`:336`, `AppleTextAccessibilityUITests.swift:5`.
- Browse-state chrome geometry: `NativeEditorLayoutMetrics.headerHeight = 94` (`NativeEditorIsland.swift:49`), load-bearing for Inspector/CaptionVisual geometry assertions (KRI-197 runbook).
- Server caption save: editor-commit stores `CaptionCue.model_validate(c).model_dump(exclude_none=True)` (`routes/generative_jobs.py:9734-9736`); web round-trips `words` untouched (`plan-api.ts:2041-2057`); covered by `tests/routes/test_editor_commit.py`.
- Phone compile: `phone_captions.compile_caption_layers` (`:364-377`) recipes for cues whose `words` already spell the text; covered by `tests/pipeline/test_phone_captions.py`, `test_phone_subtitled_plan.py`.
- Caption-row edit persistence (KRI-216): `testTappingCaptionRowEntersEditModeAndPersistsTypedText` (`NativeEditorInspectorUITests.swift:1168-1198`).
- Save banner: identifier `native-editor-save-state` and existing copy (`NativeEditorComponents.swift:139-141`).
Plan baseline: no regression contract recorded; D20 notes "Text-panel tests must stay green" as a con, not a contract.
Runtime evidence: tests listed above exist on origin/main 7672ded66 (not run in this review).
Comparison grid:

| Choice | Current | A | B |
|---|---|---|---|
| R4 how at-risk behaviour is proven | No contract | Explicit contract: existing suites stay green unmodified (except the caption-row UI test, rewritten for the intentional one-tap change) **plus** 4 new preserve-assertions: (1) unit test that `LineEditorConfiguration()` default equals today's Text editor settings; (2) `test_editor_commit.py` case: cues whose words spell the text, and cues without words, store byte-identical; (3) `test_phone_captions.py` case: matching-word cues compile byte-identical; (4) metrics unit test: browse state keeps `headerHeight` 94 and today's panel split | Existing suites stay green unmodified (except the caption-row UI test); no new preserve-assertions |
| Intentional behaviour changes named | none | one-tap focus + `textViews` locator; stale words rewritten (server + iOS); stale legacy rows compile with new text; `.failed` banner gains a retry button | same |
| New-behaviour tests (35 gaps above) | none | required (approved behaviour) | required (approved behaviour) |

Question D24:
D24 — How should we prove this plan doesn't break the Text tool, the browse layout and the server caption paths?
Project/branch/task: nova main @ 7672ded66, plan 025 (KRI-240), eng review Section 3 (Tests), finding R4 (regression rule).
ELI10: This plan changes four things other features already rely on: the text box the Text tool uses (shared under D20), the editor's header height in browse mode, how the server stores caption words, and how phone renders compile captions. Existing tests cover most of that today. The question is whether we also add small tests that pin 'nothing changed' for the cases that are supposed to stay identical, or rely on the existing suites alone.
Stakes if we pick wrong: a shared-editor default quietly changes and the Text tool starts wrapping or grabbing focus, or the server rewrites captions it should have left byte-identical, and no test says so.
Recommendation: A because each preserve-assertion is a few lines and catches exactly the silent drift the existing suites don't check directly.
Completeness: A=10/10, B=7/10
Pros / cons:
A) Explicit contract + 4 preserve-assertions (recommended) (human: ~3h / CC: ~15 min)
  ✅ Pins the Text tool's editor defaults, browse geometry, and byte-identical server/phone output for unchanged cues
  ✅ Names every intentional change, so reviewers can tell a regression from a planned difference
  ❌ Four more tests to maintain across Swift and Python
B) Existing suites only
  ✅ No extra tests; relies on suites that already run in CI
  ✅ Still rewrites the caption-row UI test for the intentional one-tap change
  ❌ No direct check that the server leaves matching cues byte-identical or that the Text editor's defaults didn't move
Net: A spends four small tests to make 'unchanged' provable; B trusts existing suites to notice drift indirectly.
Header: R4 regressions
Options (as asked):
A) Explicit contract + 4 preserve-assertions (recommended)
Existing suites stay green unmodified except `testTappingCaptionRowEntersEditModeAndPersistsTypedText` (rewritten: one tap, `app.textViews`, persistence through Done + reopen kept). Add: default `LineEditorConfiguration` equals today's Text editor settings; `test_editor_commit.py` byte-identical storage for matching/no-words cues; `test_phone_captions.py` byte-identical compile for matching cues; metrics test for browse `headerHeight` 94 + today's split. Human ~3h / CC ~15 min. Risk: low; maintenance: 4 small tests.
B) Existing suites only
Existing suites stay green unmodified except the rewritten caption-row UI test; no new preserve-assertions. Human 0 extra / CC 0 extra. Risk: silent drift in defaults or byte-identity goes unnoticed; maintenance: none.

State: approved
Actual answer: A) Explicit contract + 4 preserve-assertions (recommended) — user answer to D24, 2026-10-02
Accepted scope: Regression contract. Preserve (unmodified, must stay green): Text-tool editor tests (`NativeEditorInspectorUITests.swift:21`, `:164`; `EditorUITests.swift:185`, `:336`; `AppleTextAccessibilityUITests.swift:5`), browse-state geometry assertions (Inspector + CaptionVisual UI tests), `test_editor_commit.py`, `test_phone_captions.py`, `test_phone_subtitled_plan.py`, `NativeEditorRenderCompilerTests.testWordCaptionsUseStoredTimingsUntilTheirTextIsEdited`, save-banner identifier/copy. Intentional changes: one-tap focus and `app.textViews` locator in the rewritten `testTappingCaptionRowEntersEditModeAndPersistsTypedText` (persistence through Done + reopen kept); stale `words` rewritten on iOS and server; stale legacy rows compile with the new text; `.failed` banner gains `native-editor-retry-save`. New preserve-assertions: (1) `LineEditorConfiguration()` default equals today's Text editor settings (KriaTests); (2) `test_editor_commit.py`: cues whose words spell the text, and cues without words, store byte-identical; (3) `test_phone_captions.py`: matching-word cues compile byte-identical; (4) `NativeEditorLayoutMetricsTests`: browse state keeps `headerHeight` 94 and today's split.
History: none

### R5: Park frame for pop-in caption styles
Finding: #5, P1, confidence 9/10, `src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/TextTransformTiming.swift:38-40` + `src/apps/ios/Kria/Core/NativeEditorRenderCompiler.swift:512`; reviewer: outside voice (Sonnet), verified by primary.
Motivating code: `if effect == .captionPop { return TextTransformSample(alpha: min(1, max(0, localTime / 0.12)), …) }`; compiler: `effect: document.editFormat == "subtitled" ? .captionPop : .none`.
Plan baseline: D6 (approved): park on the fully-revealed frame = last word's start; "for sentence/pop-in-by-line styles that equals `startS`"; ±1 frame (D8).
Runtime evidence: static read; at `startS` the caption's alpha is 0 for every `subtitled` edit (the KRI-240 screenshot case), so the parked preview shows no caption.
Comparison grid:

| Choice | Current (D6) | A | B | C | D |
|---|---|---|---|---|---|
| R5 park time for sentence/pop-in captions | `startS` (alpha 0 under captionPop) | `startS + 0.15s`, clamped to `endS − 1 frame` (alpha 1, scale settled at 0.14s); word styles unchanged (last word's start, also +0.15s clamp) | `startS` as approved | Measure on device before choosing | Leave D6 as is for now; revisit after build |
| Proof | none | unit test: sampled alpha ≥ 0.9 at the park frame for `.captionPop`, `.none` and word styles | none | spike | none |

Question D25:
D25 — Park the preview 0.15s after a caption starts, so the pop-in caption is visible?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R5.
ELI10: When you tap a line, the preview jumps to that line's start so you can watch your fix. But Talking captions fade in over their first 0.12 seconds (TextTransformTiming.swift:38-40), so at the exact start the caption is invisible. The approved rule parks exactly at the start, which would show the video with no caption while you type.
Stakes if we pick wrong: the big readable preview, the whole point of the redesign, shows a blank frame on every Talking edit.
Recommendation: A because a 0.15s offset lands after the fade (0.12s) and the scale settle (0.14s), and a clamp keeps very short lines inside their window.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~1h / CC: ~5 min)
  ✅ The parked frame always shows the full caption at final opacity and size
  ✅ One small rule, provable with a unit test that samples alpha at the park frame
  ❌ The parked frame is 0.15s into the line, so the very first syllable's mouth shape is not shown
B) Keep D6 as approved
  ✅ Parks exactly where the line starts, matching the timestamp in the list
  ✅ No change to the approved rule
  ❌ Talking captions are invisible on the parked frame
C) Investigate first (human: ~1h / CC: n/a)
  ✅ Confirms on a device how the pop-in looks at several offsets
  ✅ Could tune the offset per style
  ❌ Delays a fix whose cause is already read from the code
D) Defer this change
  ✅ Nothing changes in the plan now
  ✅ Can be revisited after the first device build
  ❌ The first build ships the blank-frame preview
Net: A shifts the park point past a fade the code already defines; B and D ship a blank preview.
Header: R5 park frame
Options (as asked):
A) Apply this change (recommended)
Park at `startS + 0.15s`, clamped to `endS − 1 frame`, for every caption style (word styles: last word's start + 0.15s, same clamp); unit test asserts alpha ≥ 0.9 at the park frame for `.captionPop`, `.none` and word styles; D6/D8 amended. Human ~1h / CC ~5 min.
B) Keep D6 as approved
Park at `startS` (word styles: last word's start). Human 0 / CC 0. Risk: blank caption on the parked frame for Talking edits.
C) Investigate first
Device spike comparing offsets per style before choosing; D6 stays pending. Human ~1h. Risk: delay.
D) Defer this change
D6 unchanged in this plan; revisit after the first device build. Risk: first build ships the blank frame.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D25, 2026-10-02
Accepted scope: Park frame = `startS + 0.15s` (word styles: last word's start + 0.15s), clamped to `endS − 1 frame`, for every caption style; unit test asserts sampled alpha ≥ 0.9 at the park frame for `.captionPop`, `.none` and word styles. D6 park-frame bullet and D8 'paused on the line's start time' row amended.
History: none

### R6: Row-level Delete mechanism (swipe has no implementation path)
Finding: #6, P2, confidence 9/10, `src/apps/ios/Kria/Features/NativeEditorLanePanel.swift:47` (`ScrollView { content()… }`) and the caption rows' `VStack`/`ForEach`; no `swipeActions` or `onDelete` anywhere in `src/apps/ios/Kria`; reviewer: outside voice (Sonnet), verified by primary.
Plan baseline: D3 + D22 (approved): browse rows get a trailing swipe action labelled "Delete" (cue and guided-story rows); empty-commit removal with Undo toast; D13 VoiceOver Delete action.
Runtime evidence: SwiftUI `.swipeActions` works only inside `List`/`Form`; the caption list is a `VStack` inside the shared lane-panel `ScrollView`, whose rows also carry `.onTapGesture`. A swipe needs either a custom horizontal `DragGesture` (competes with vertical scroll and tap) or converting the list to `List` (restyles the shared panel).
Comparison grid:

| Choice | Current (D3/D22) | A | B | C | D |
|---|---|---|---|---|---|
| R6 row-level Delete affordance | Trailing swipe "Delete" (no mechanism) | Long-press context menu on each row with one destructive item "Delete" (red, the word), plus the D13 VoiceOver "Delete" action; swipe dropped | Custom horizontal `DragGesture` swipe revealing a red "Delete" button inside the existing `ScrollView` | Spike custom swipe vs `List` conversion before choosing | No row-level Delete in v1; removal only via emptied-field commit and the VoiceOver action |
| Empty-commit removal + Undo toast (D3) | approved | unchanged | unchanged | unchanged | unchanged |
| Guided-story rows (D22) | same as cues | same as cues | same as cues | same as cues | same as cues |

Question D26:
D26 — Replace the swipe-to-Delete on caption rows with a long-press "Delete" menu?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R6.
ELI10: The design approved swiping a caption row left to reveal Delete. iOS only gives that gesture for free in a system List, and the caption list is a plain scrolling stack (NativeEditorLanePanel.swift:47) with tap-to-edit on every row. Building a hand-made swipe means fighting the scroll and the tap. A long-press menu with one red "Delete" item is the standard iOS fallback and needs no custom gesture.
Stakes if we pick wrong: a home-made swipe that sometimes scrolls when you meant to delete, or opens the editor when you meant to swipe.
Recommendation: A because a context menu is a system control that coexists with scroll and tap, and Delete stays one long-press away with VoiceOver covered by its own action.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~2h / CC: ~10 min)
  ✅ System context menu: no gesture conflict with scrolling or tap-to-edit
  ✅ Destructive item reads as the word Delete, matching the design system rule
  ❌ Long-press is less discoverable than a swipe; first-time users may only find the clear-the-line path
B) Keep the swipe (custom gesture) (human: ~1.5 days / CC: ~1h)
  ✅ Matches the approved design and the familiar Mail-style swipe
  ✅ Discoverable for users who try swiping
  ❌ Hand-made gesture inside a scroll view with tap rows; needs careful thresholds and UI tests to avoid misfires
C) Investigate first (human: ~3h / CC: ~20 min)
  ✅ A spike shows whether a List conversion keeps the panel's look, giving native swipe
  ✅ Avoids committing to a gesture before seeing it on device
  ❌ Adds a step before the row work can start
D) Defer this change
  ✅ Smallest v1: Delete via clearing the line or VoiceOver only
  ✅ No new row gesture to test
  ❌ Sighted users have no explicit Delete; clearing text to delete is hidden
Net: A trades swipe discoverability for a conflict-free system control; B keeps the swipe at the cost of a custom gesture.
Header: R6 row Delete
Options (as asked):
A) Apply this change (recommended)
Each caption row (cue and guided) gets `.contextMenu` with one destructive `Button("Delete", role: .destructive)` calling `session.deleteSelection(EditorSelection(kind: .captionCue, id:))` and showing the Undo toast; D13 VoiceOver "Delete" custom action kept; swipe dropped from D3/D22. Hidden when captions are read-only (D16). Human ~2h / CC ~10 min.
B) Keep the swipe (custom gesture)
Custom horizontal `DragGesture` on rows inside the existing `ScrollView` reveals a red "Delete" button; thresholds tuned so vertical scroll and tap-to-edit win; UI tests for swipe vs scroll vs tap. Human ~1.5 days / CC ~1h.
C) Investigate first
Spike: convert the caption list to `List` with `.swipeActions` and compare against a custom gesture on device; D3 swipe stays pending. Human ~3h / CC ~20 min.
D) Defer this change
No row-level Delete in v1; removal via emptied-field commit (D3) and VoiceOver action (D13) only. Human 0 / CC 0.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D26, 2026-10-02
Accepted scope: Swipe dropped. Each caption row (cue and guided-story) gets `.contextMenu` with one destructive `Button("Delete", role: .destructive)` calling `session.deleteSelection(EditorSelection(kind: .captionCue, id:))` and showing the Undo toast; D13 VoiceOver "Delete" custom action kept; hidden when captions are read-only (D16). D3 browse bullet, D22 (R2) wording and D13 amended to 'long-press Delete'.
History: none

### R7: Source of truth for "a line is in edit"
Finding: #7, P1, confidence 7/10 (reasoning from cited code paths), `src/apps/ios/Kria/Features/NativeEditorLanePanel.swift:94` (`@State private var editingCueID`) + `:119-130` (transaction/cleanup on change, tab change, lifecycle) + `NativeEditorTextPanel.swift` `ExplicitLineTextView.didMoveToWindow` focus + `NativeEditorView.swift` transport/rail/fullscreen guards keyed on `keyboardVisible`; reviewer: outside voice (Sonnet), checked by primary.
Plan baseline: D14 (approved): "Edit state = the caption text view is first responder (not `keyboardVisible`)"; §10.3 spec routes a `captionLineFocused` binding from the text view up to `NativeEditorView`, which hides the header (Back, Save), Chat/Editor switch, transport and rail while it is true.
Runtime evidence: static read. Focus is an effect that can fail (`becomeFirstResponder` refused under a sheet / non-key window) or outlive its view (panel switch, `loadEditor` on a new job, conflict rebase replacing the document), leaving `focused == true` with no field on screen and the header (the only Back and Save) hidden. A row tap must mount the bar before focus can fire, so two states (`editingCueID`, focus) can disagree.
Comparison grid:

| Choice | Current (D14 + §10.3) | A | B | C | D |
|---|---|---|---|---|---|
| R7 what defines edit state | Caption text view focus binding | `editingCueID` (panel/session state) is the truth; focus is a requested effect of it | Focus binding (as approved) | Device spike before choosing | Leave as approved for now |
| Reset rules | unspecified | `editingCueID = nil` on panel `onDisappear`, `changePanel`, `scenePhase` → background (after the D4 commit), document replacement (load / rebase / job switch), and Done | unspecified | — | unspecified |
| Focus failure | chrome stays hidden with no field | bar stays visible with the field unfocused; tapping the field focuses; Done/Back always reachable via the bar's Done | stuck | — | stuck |
| Text view identity across Next/Previous | unspecified | one stable text view; only its text/config change (keyboard stays up, no hide/show flicker) | unspecified | — | unspecified |
| Other `keyboardVisible` readers | unaudited | transport, tool rail, fullscreen guard, `panelDefaultHeight` re-keyed to edit state where they serve the caption edit bar | unaudited | — | unaudited |

Question D27:
D27 — Make the panel's 'which line is being edited' state the source of truth, with keyboard focus as a side effect?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R7.
ELI10: The approved design hides the header (with Back and Save) while a line is being edited, and decides 'being edited' by whether the text box has keyboard focus. Focus is fragile: it can fail to arrive, or linger after the box is torn down (switching tools, a new job loading, a conflict reload). Then the header stays hidden with no text box to finish, and the user has no Back or Save. Keying everything on 'which line is open' (editingCueID, NativeEditorLanePanel.swift:94), resetting it on every way out, and treating focus as a request avoids that.
Stakes if we pick wrong: a creator gets stuck on a screen with no Back, no Save and no keyboard.
Recommendation: A because a plain state value with explicit resets can't be left dangling the way a focus flag can, and the bar's Done is always there as an exit.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~4h / CC: ~20 min)
  ✅ Edit state survives a failed focus request; the bar and its Done stay usable
  ✅ Explicit reset on every exit path removes the stuck-header failure mode
  ❌ Hardware-keyboard and focus-loss cases need their own UI tests
B) Keep D14 as approved (focus is truth)
  ✅ Matches the approved wording exactly
  ✅ No second state to keep in sync
  ❌ A lost or failed focus can leave the header hidden with no exit
C) Investigate first (human: ~3h)
  ✅ A device spike shows how often focus fails in the connected editor
  ✅ Could keep focus-as-truth if it proves reliable
  ❌ The stuck-header risk is structural, not frequency-dependent
D) Defer this change
  ✅ No plan change now
  ✅ Revisit after the first build
  ❌ The first build can strand users without Back or Save
Net: A swaps a fragile signal for a state with explicit resets; B keeps the approved wording and its stuck-header risk.
Header: R7 edit state
Options (as asked):
A) Apply this change (recommended)
Edit state = `editingCueID != nil` (owned by `NativeCaptionPanel`, mirrored up to `NativeEditorView`); focus is requested when it becomes non-nil and on each Next/Previous; reset to nil on panel `onDisappear`, `changePanel`, background (after the D4 commit), document replacement and Done; one stable text view across lines; transport/rail/fullscreen/`panelDefaultHeight` re-keyed where they serve the bar. D14 bullet 1 amended. Human ~4h / CC ~20 min.
B) Keep D14 as approved
Edit state = caption text view is first responder, routed up as `captionLineFocused`. Human 0 / CC 0. Risk: stuck hidden header on focus loss.
C) Investigate first
Device spike on focus reliability in the connected editor before choosing; D14 stays as approved meanwhile. Human ~3h.
D) Defer this change
Leave D14 as approved; revisit after the first build. Risk: first build can strand users.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D27, 2026-10-02
Accepted scope: Edit state = `editingCueID != nil` (owned by `NativeCaptionPanel`, mirrored up to `NativeEditorView`); focus is requested when it becomes non-nil and on each Next/Previous; reset to nil on panel `onDisappear`, `changePanel`, background (after the D4 commit), document replacement (load / rebase / job switch) and Done; one stable text view across lines; transport, tool rail, fullscreen guard and `panelDefaultHeight` re-keyed to edit state where they serve the bar. A failed focus leaves the bar visible with an unfocused field. D14 bullet 1 and §10.3 diagram amended.
History: none

### R8: Keyboard swipe-down as an exit from a line
Finding: #8, P2, confidence 8/10, `src/apps/ios/Kria/Features/NativeEditorLanePanel.swift:47` (`.scrollDismissesKeyboard(.interactively)` on the panel's `ScrollView`, which the edit bar replaces) ; reviewer: outside voice (Sonnet), checked by primary.
Plan baseline: D5 (approved): "Only Done and keyboard swipe-down return to browse"; D8: "interactive dismiss on the bar's own scroll container (the field) → commit + return to browse".
Runtime evidence: static read. Interactive keyboard dismissal comes from a scroll view's drag. In edit state the list's `ScrollView` is gone; the field is a 44–82pt text view that scrolls only past 3 lines, so a downward swipe usually has nothing to drag. With D27, losing focus no longer ends edit state, so a dismiss alone would leave the bar up with an unfocused field.
Comparison grid:

| Choice | Current (D5/D8) | A | B | C | D |
|---|---|---|---|---|---|
| R8 swipe-down exit | Listed as an exit (no reliable surface) | Dropped. Exits: Done, Return on the last line, Esc (D14), tool change, background. If the keyboard is dismissed by any other means, the bar stays with an unfocused field; tapping it refocuses | Kept: a downward drag on the edit bar (≥ 40pt) commits the line and returns to browse | Device spike first | Leave the wording; revisit |
| D5 commit vocabulary | approved | unchanged | unchanged | unchanged | unchanged |

Question D28:
D28 — Drop 'swipe the keyboard down' as a way to finish a line, and rely on Done?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R8.
ELI10: The approved design lets you finish a line by swiping the keyboard down. iOS only supports that when you drag a scrolling list, and in edit mode the list is replaced by the small edit bar (the list's ScrollView at NativeEditorLanePanel.swift:47 goes away). There is usually nothing to drag. Done, Return on the last line and Esc already finish a line.
Stakes if we pick wrong: a promised gesture that silently does nothing, or a custom drag on the bar that competes with the text box.
Recommendation: A because Done is always visible in thumb reach, and an unreliable gesture is worse than none.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~15 min / CC: ~2 min)
  ✅ No gesture that only sometimes works; Done, Return and Esc are reliable exits
  ✅ Fits D27: a dismissed keyboard leaves the bar usable instead of exiting by surprise
  ❌ Users who habitually swipe the keyboard down must tap Done instead
B) Keep it as a drag on the edit bar (human: ~3h / CC: ~15 min)
  ✅ Keeps a familiar 'pull down to finish' motion
  ✅ The bar does not scroll, so a vertical drag there has no scroll conflict
  ❌ A custom gesture next to the text field; needs thresholds and UI tests
C) Investigate first (human: ~2h)
  ✅ Shows on device whether users expect the swipe here
  ✅ Could reveal a cheap system option
  ❌ Delays a choice the code already constrains
D) Defer this change
  ✅ No plan change now
  ✅ Revisit after the first build
  ❌ The plan keeps promising a gesture the build won't deliver
Net: A removes an exit that has nothing to attach to; B rebuilds it as a custom drag.
Header: R8 swipe exit
Options (as asked):
A) Apply this change (recommended)
Drop keyboard swipe-down from D5/D8. Exits from a line: Done, Return on the last line, Esc, tool change, background. A keyboard dismissed by other means leaves the bar with an unfocused field; tapping it refocuses. Human ~15 min / CC ~2 min.
B) Keep it as a drag on the edit bar
A downward drag ≥ 40pt on the edit bar (outside the field) commits the line and returns to browse; UI test for drag vs tap. Human ~3h / CC ~15 min.
C) Investigate first
Device check before choosing; D5/D8 wording stays meanwhile. Human ~2h.
D) Defer this change
Keep the D5/D8 wording; revisit after the first build.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D28, 2026-10-02
Accepted scope: Keyboard swipe-down removed from D5/D8. Exits from a line: Done, Return on the last line, Esc, tool change, background. A keyboard dismissed by other means leaves the bar with an unfocused field; tapping the field refocuses. D5 exit bullet and D8 'swipe the keyboard down' row amended.
History: none

### R9: Real editor chrome vs the 220pt preview floor
Finding: #9, P1, confidence 7/10 (elements verified; budget arithmetic estimated), `src/apps/ios/Kria/Features/NativeEditorView.swift:282-299` (preview `.padding(.vertical, 5)`, 52pt sparkles FAB overlay bottom-trailing, `timelineResizeHandle` 44pt) + `:446` (panel `.padding(.top, 18)`) + `NativeEditorIsland.swift:20` (`bottomPadding = 6`); reviewer: outside voice (Sonnet), elements verified by primary.
Plan baseline: D2 (approved): edit state hides header, Chat/Editor, "Rendered on iPhone", panel title + tabs, transport, tool rail; preview fills the rest with a 220pt floor on SE-class (board: SE 267pt, iPhone 15 282pt). D8 pins the bar at ≤142pt / ≤120pt.
Runtime evidence: static read. The board drew no timeline resize handle, preview padding, panel top inset or island bottom pad. With them kept, SE-class lands near 189pt (estimate), below the floor; the sparkles FAB covers the bottom-right of the preview where captions sit. Non-9:16 edits (`session.previewAspectRatio`) are width-bound and cannot reach 220pt tall.
Comparison grid:

| Choice | Current (D2/D8) | A | B | C | D |
|---|---|---|---|---|---|
| R9 extra chrome in edit state | Kept (not listed) | Also hidden in edit state: timeline resize handle, sparkles FAB, newer-job prompt; panel top inset and island bottom pad collapse so the bar sits flush on the keyboard; 5pt preview padding kept | Kept; floor lowered to what remains (~185pt SE-class) | Measure in the simulator first | Leave as is |
| Floor definition | 220pt on SE-class | 220pt for 9:16 on SE-class; other aspect ratios fill the available width | ~185pt | — | 220pt (likely missed) |
| Proof | metrics tests (D10) | `NativeEditorLayoutMetricsTests` keyboard-up cases at SE/15/Pro Max assert ≥220pt (9:16) with real constants | metrics test at the lowered floor | — | — |

Question D29:
D29 — In edit state, also hide the timeline handle and the Kria sparkles button so the preview keeps its 220pt floor on small phones?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R9.
ELI10: The wireframe that set the 220pt preview floor did not draw some real pieces of the editor: a 44pt drag handle under the video, a little padding, the panel's top inset, and the round Kria sparkles button that floats over the bottom-right of the video (NativeEditorView.swift:282-299). Kept as they are, a small iPhone ends up with roughly 189pt of preview, below the floor, and the sparkles button sits on top of the captions you are trying to read.
Stakes if we pick wrong: on an iPhone SE the preview is smaller than designed and partly covered right where the caption is.
Recommendation: A because those pieces have no job while typing a caption, and hiding them is what makes the approved floor reachable.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~3h / CC: ~15 min)
  ✅ Preview reaches the approved floor on SE-class phones and nothing covers the caption area
  ✅ Metrics tests pin the budget with the real constants, not the wireframe's
  ❌ Kria chat and the timeline resize are unreachable until the line is finished (Done)
B) Keep chrome, lower the floor (human: ~1h / CC: ~5 min)
  ✅ Fewer elements change between browse and edit
  ✅ Kria sparkles stays reachable while typing
  ❌ Smaller preview on SE, and the sparkles button still covers the bottom-right of the captions
C) Investigate first (human: ~2h)
  ✅ Simulator measurement replaces the estimate with exact numbers
  ✅ Could show the floor is met on most devices
  ❌ The overlap of the sparkles button on captions holds regardless of the numbers
D) Defer this change
  ✅ No plan change now
  ✅ Revisit on the first device build
  ❌ The first build likely misses the floor on small phones
Net: A hides three idle pieces to make the approved floor real; B keeps them and shrinks the promise.
Header: R9 preview room
Options (as asked):
A) Apply this change (recommended)
While `editingCueID != nil` also hide the timeline resize handle, the sparkles FAB and the newer-job prompt, and collapse the panel top inset and island bottom pad so the bar sits on the keyboard; keep the 5pt preview padding. Floor = 220pt for 9:16 on SE-class, other aspect ratios fill the width. `NativeEditorLayoutMetricsTests` keyboard-up cases at SE/15/Pro Max with real constants. D2/D8 amended. Human ~3h / CC ~15 min.
B) Keep chrome, lower the floor
Keep handle, FAB, prompt and insets in edit state; floor becomes what remains (~185pt SE-class, measured in tests). Human ~1h / CC ~5 min.
C) Investigate first
Simulator measurement of the real budget on SE/15/Pro Max before choosing; D2/D8 unchanged meanwhile. Human ~2h.
D) Defer this change
D2/D8 unchanged; revisit on the first device build.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D29, 2026-10-02
Accepted scope: While `editingCueID != nil`, also hidden: timeline resize handle, sparkles FAB, newer-job prompt; panel top inset (18pt) and island bottom pad (6pt) collapse so the bar sits on the keyboard; 5pt preview padding kept. Floor = 220pt for 9:16 on SE-class; other aspect ratios fill the available width. `NativeEditorLayoutMetricsTests` keyboard-up cases at SE (667) / 15 (852) / Pro Max (932) with real constants assert the floor. D2 hidden-chrome list amended.
History: none

### R10: Edit state when the live preview is unavailable
Finding: #10, P2, confidence 8/10, `src/apps/ios/Kria/Features/NativeEditorSession.swift:8-16` (`NativeSourcePreviewState`: `idle, preparing, ready, failed(String), originalsUnavailable`; "Both settle on the finished render") + `NativeEditorMediaViews.swift:648-660`; reviewer: outside voice (Sonnet), verified by primary.
Plan baseline: D2/D4 (approved) assume a live preview that "shows the real caption style, updating as you type"; D4's state table has no row for an unavailable live preview.
Runtime evidence: static read. In `.failed` and `.originalsUnavailable` the preview plays the finished render, whose burned-in caption still has the typo; typing changes nothing on screen while edit state enlarges that stale frame.
Comparison grid:

| Choice | Current (D2/D4) | A | B | C | D |
|---|---|---|---|---|---|
| R10 edit state with no live preview | Unspecified (big stale render) | Edit bar and chrome collapse as usual; the preview frame is dimmed (50% `paper` scrim) with one `mutedInk` 13pt line: "Preview shows your last render. Your fix appears after Save."; loop-play still plays the line's audio; `.preparing` keeps the existing preparing indicator | Unspecified (stale render, enlarged) | Device check first | Leave unspecified |
| D4 table | no row | new row "LIVE PREVIEW UNAVAILABLE" | no row | — | no row |

Question D30:
D30 — When the live preview can't show edits, dim it and say so while a line is being edited?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R10.
ELI10: Sometimes the editor can't build a live preview, for example when the original clips aren't on this iPhone. Then it plays the finished video instead (NativeEditorSession.swift:8-16). That video has the old caption burned in, typo included. In the new edit mode that stale video becomes the big picture above your text box, so you would be fixing a typo while staring at it unchanged.
Stakes if we pick wrong: a creator thinks their fix didn't take and retypes it, or loses trust in the editor.
Recommendation: A because one honest line costs nothing and keeps the edit flow identical; the fix really does show after Save.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~2h / CC: ~10 min)
  ✅ The creator knows the preview is stale and why their fix isn't visible yet
  ✅ Same edit bar and exits as normal, so nothing else changes
  ❌ Adds one visible line of copy in a rare state
B) Keep as is (no special handling)
  ✅ No new copy or state
  ✅ The edit bar still works
  ❌ A large stale caption contradicts what the creator is typing
C) Investigate first (human: ~2h)
  ✅ Confirms how often each state occurs on real devices
  ✅ Could show a better fallback
  ❌ The stale-frame mismatch is certain whenever the state occurs
D) Defer this change
  ✅ No plan change now
  ✅ Revisit after the first build
  ❌ The first build shows the stale frame without explanation
Net: A spends one line of copy to keep the preview honest; B leaves a confusing stale frame.
Header: R10 no preview
Options (as asked):
A) Apply this change (recommended)
In `.failed` / `.originalsUnavailable`, edit state still collapses chrome and shows the bar; the preview frame gets a 50% `paper` scrim and one `mutedInk` 13pt line "Preview shows your last render. Your fix appears after Save."; loop-play still plays the line's audio; `.preparing` keeps the existing indicator. New D4 row. Human ~2h / CC ~10 min.
B) Keep as is
No special handling; the finished render plays under the edit bar. Human 0 / CC 0.
C) Investigate first
Device check of how often each state occurs before choosing. Human ~2h.
D) Defer this change
No D4 row now; revisit after the first build.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D30, 2026-10-02
Accepted scope: In `.failed` / `.originalsUnavailable`, edit state still collapses chrome and shows the bar; the preview frame gets a 50% `paper` scrim and one `mutedInk` 13pt line "Preview shows your last render. Your fix appears after Save."; loop-play still plays the line's audio; `.preparing` keeps the existing preparing indicator. New D4 row added.
History: none

### R11: Refine R1's word rewrite (reopened)
Finding: #11, P1, confidence 8/10, `src/apps/api/app/pipeline/captions.py:756-758` (whitespace token match) + `src/apps/api/app/routes/generative_jobs.py:3124` (`CaptionWord.timing_quality`) + `:3166-3180` (`smart_word_ids`, `smart_keep_together` "must survive a caption text edit") ; reviewer: outside voice (Sonnet), verified by primary.
Plan baseline: R1 (approved, D21 = A): iOS rewrites `raw["words"]` per D6 on every text change; server rewrites "any cue whose `words` no longer spell its `text`" at editor-commit / `persist_variant_captions`; `phone_captions._prepare_cues` applies the rule to legacy stale rows; shared fixture.
Runtime evidence: static read. (1) Re-spreading on every keystroke is lossy: typing a space then deleting it changes 3→4→3 words and re-spreads by character weight, so Whisper timings are lost although the text is back where it started, and `document != baseline` leaves a phantom undo step and dot. (2) "Words don't spell text" uses whitespace tokens, so languages written without spaces (whisper can return e.g. `ja`, `zh`) would have every untouched cue rewritten and their real timings flattened. (3) The rule must not touch cue-level `smart_*` fields, which #699 made survive edits. (4) The stale-words burn is a static read, never reproduced.
Comparison grid:

| Choice | Current (R1 approved) | A | B | C | D |
|---|---|---|---|---|---|
| iOS rewrite input | Previous keystroke's words | Pure function of (words captured when the line was entered, current text); text equal to the entry text ⇒ entry words restored exactly (no phantom undo/dot) | as R1 | Spike | as R1 |
| Server rewrite trigger | Any cue whose words don't spell its text | Only cues whose `text` differs from the stored cue with the same `id` | as R1 | — | as R1 |
| Phone compiler legacy guard | Token-list mismatch | Mismatch = concatenated word texts ≠ cue text after removing all whitespace (works without spaces) | as R1 | — | as R1 |
| Word metadata | unspecified | Positional keep keeps each word's `timing_quality`; re-spread words get `timing_quality: "segment_estimate"`; cue-level `smart_role` / `smart_word_ids` / `smart_emphasis` / `smart_keep_together` never touched | unspecified | — | unspecified |
| Reproducer | none | A failing pytest reproduces the stale-words burn in `phone_captions` before the fix lands | none | — | none |
| D6 behaviour | approved | unchanged | unchanged | unchanged | unchanged |

Question D31:
D31 — Tighten the word-timing rewrite so it can't lose real timings or touch untouched captions?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R11 (reopens R1).
ELI10: R1 rewrites a line's word timings whenever its text changes. As specified it has three holes. Typing a space and deleting it re-spreads the words and loses the real timings even though the text is back to where it was. The server side would rewrite any caption whose word list doesn't match space-separated words, which in languages written without spaces means every caption (captions.py:756-758). And the rewrite must leave alone the layout hints the server keeps per caption (generative_jobs.py:3166-3180).
Stakes if we pick wrong: real speech timings silently flattened on untouched captions, phantom 'unsaved' dots, or lost emphasis styling after one edit.
Recommendation: A because each fix is a narrow rule change in code we are already writing, and a failing test first proves the bug we are fixing exists.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~3h / CC: ~20 min)
  ✅ Undoing a typo restores the original timings exactly; no phantom dots or undo steps
  ✅ Untouched captions, including languages without spaces, are never rewritten
  ❌ The server needs the stored cue to compare against at commit time (one lookup by id)
B) Keep R1 as approved
  ✅ Simplest rule text
  ✅ No extra lookup on the server
  ❌ Can flatten real timings on untouched captions and leave phantom dots
C) Investigate first (human: ~2h)
  ✅ Measures how often non-space languages occur in real jobs
  ✅ Could narrow the fix to observed cases
  ❌ The lossy keystroke behaviour exists for every language
D) Defer this change
  ✅ No plan change now
  ✅ Revisit during implementation
  ❌ R1 ships with known data-loss edges
Net: A narrows R1 to exactly the edited text with exact restore; B keeps a broader rule with known edges.
Header: R11 rewrite rule
Options (as asked):
A) Apply this change (recommended)
iOS: words = f(words at line entry, current text); text equal to the entry text ⇒ entry words restored exactly. Server: rewrite only cues whose `text` differs from the stored cue with the same `id`. Phone guard: compare concatenated word texts to cue text with all whitespace removed. Keep `timing_quality` on positional keep, set `"segment_estimate"` on re-spread; never touch cue-level `smart_*`. A failing pytest reproduces the stale-words burn first. R1 accepted scope amended. Human ~3h / CC ~20 min.
B) Keep R1 as approved
Rewrite on every keystroke from the previous words; server rewrites any cue whose words don't spell its text; token-list guard. Human 0 extra.
C) Investigate first
Measure non-space-language frequency in prod jobs before choosing; R1 unchanged meanwhile. Human ~2h.
D) Defer this change
R1 unchanged; revisit during implementation.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D31, 2026-10-02
Accepted scope: R1 refined: (1) iOS words = f(words captured when the line was entered, current text); current text equal to the entry text restores the entry words exactly (no phantom undo step or dot). (2) Server rewrites only cues whose `text` differs from the stored cue with the same `id`. (3) `phone_captions._prepare_cues` guard treats a cue as stale when the concatenated word texts differ from the cue text after removing all whitespace. (4) Positional keep preserves each word's `timing_quality`; re-spread words get `timing_quality: "segment_estimate"`; cue-level `smart_role` / `smart_word_ids` / `smart_emphasis` / `smart_keep_together` are never touched. (5) A failing pytest reproduces the stale-words burn in `phone_captions` before the fix lands. R1 accepted scope amended (History).
History: Reopens R1 (D21 = A, 2026-10-02) because the outside voice found lossy re-spread, a whitespace-only match that misfires on languages without spaces, and unprotected `smart_*` fields.

### R12: Undo toast against the editor's linear undo stack
Finding: #12, P2, confidence 7/10 (reasoning from the transaction model), `src/apps/ios/Kria/Features/NativeEditorSession.swift` `beginTransaction`/`endTransaction` (one undo entry per changed transaction) + `deleteSelection` (`:3577`, its own `transactDocument`); reviewer: outside voice (Sonnet), checked by primary.
Plan baseline: D3 (approved): an emptied-field commit removes the line and shows "Line 4 removed · Undo" for 4s; the bar moves to the next line. D5: one undo step per changed line.
Runtime evidence: static read. Clearing the text happens inside the line's typing transaction; the removal is a second `transactDocument`. As two steps, Undo first restores a blank line (the ghost row D3 exists to kill). And if the creator starts typing the next line within 4s, the toast's Undo would revert that typing, not the removal.
Comparison grid:

| Choice | Current (D3) | A | B | C | D |
|---|---|---|---|---|---|
| R12 empty-commit removal as undo steps | Unspecified (likely two: clear, then delete) | One step: the removal replaces the line's typing transaction, so one Undo restores the original non-empty line | Unspecified | Spike | Unspecified |
| Toast lifetime | 4s | 4s, or until the next keystroke or line change, whichever comes first | 4s | — | 4s |
| Toast Undo target | `session.undo()` | `session.undo()`, offered only while the removal is still the newest undo step | `session.undo()` | — | `session.undo()` |

Question D32:
D32 — Make 'clear a line' one undo step, and hide the Undo toast as soon as you keep typing?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R12.
ELI10: Clearing a line and pressing Return removes it and shows 'Line 4 removed · Undo' for 4 seconds. The editor has one undo history (NativeEditorSession transactions). If clearing and removing are two separate steps, Undo brings back an empty line, the exact ghost row this feature was meant to stop. And if you start typing the next line while the toast is still up, its Undo would undo your typing, not the removal.
Stakes if we pick wrong: Undo restores a blank caption, or erases the wrong edit.
Recommendation: A because one step per removal and a toast that only lives while its Undo is truthful make the button always do what it says.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended) (human: ~2h / CC: ~10 min)
  ✅ One Undo always brings back the original line with its text and timing
  ✅ The toast never offers an Undo that would revert something else
  ❌ The toast can vanish sooner than 4s if the creator keeps typing
B) Keep D3 as approved
  ✅ Toast always stays for the full 4s
  ✅ No change to the approved wording
  ❌ Undo can restore a blank line or revert the wrong edit
C) Investigate first (human: ~1h)
  ✅ Confirms in a unit test how the two transactions stack today
  ✅ Could show the steps already merge
  ❌ The toast-vs-typing conflict exists regardless
D) Defer this change
  ✅ No plan change now
  ✅ Revisit during implementation
  ❌ Leaves a known wrong-undo path in the first build
Net: A makes the toast's Undo always truthful at the cost of an earlier dismiss; B keeps 4s and a misleading button.
Header: R12 undo toast
Options (as asked):
A) Apply this change (recommended)
Emptied-field removal replaces the line's typing transaction with the deletion, so it is one undo step restoring the original non-empty line. Toast shows for 4s or until the next keystroke or line change, whichever first; its Undo calls `session.undo()` and is shown only while the removal is the newest undo step. D3 amended. Unit test: clear + Return then one undo restores text and `words`. Human ~2h / CC ~10 min.
B) Keep D3 as approved
Removal and clear may be separate steps; toast stays 4s regardless of typing. Human 0.
C) Investigate first
Unit-test today's transaction stacking before choosing; D3 unchanged meanwhile. Human ~1h.
D) Defer this change
D3 unchanged; revisit during implementation.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D32, 2026-10-02
Accepted scope: Emptied-field removal replaces the line's typing transaction with the deletion: one undo step that restores the original non-empty line (text and `words`). Toast shows for 4s or until the next keystroke or line change, whichever first; its Undo calls `session.undo()` and is offered only while the removal is the newest undo step. Unit test: clear + Return, then one undo restores text and `words`. D3 edit-state bullet amended.
History: none

### R13: PR sequencing and what reaches TestFlight
Finding: #13, P2, confidence 9/10, `.github/workflows/testflight.yml:6-23` (runs after every successful `main` iOS workflow) + `:220` ("Upload and distribute to external TestFlight"); reviewer: outside voice (Sonnet), verified by primary.
Plan baseline: no PR structure decided (D20 fixed files/classes, not PRs). Implicit current value: one PR containing iOS + server work.
Runtime evidence: static read. Every merged `main` commit that passes iOS CI ships to external testers, so a half-built edit mode (e.g. chrome hidden but no loop-play) would reach real users. Server changes need a Fly deploy, iOS changes need TestFlight; their release clocks differ. The riskiest unknowns (one-tap focus in the connected editor, `textInputMode`, hardware-keyboard behaviour) are device facts not yet checked.
Comparison grid:

| Choice | Current (implicit) | A | B | C | D |
|---|---|---|---|---|---|
| R13 PR structure | One PR (iOS + server) | PR0 device spike, not merged (one-tap focus in the connected editor, `textInputMode` override, hardware keyboard); PR1 server: failing reproducer + R1/R11 rewrite + phone guard + fixture JSON; PR2 tiny iOS: save-banner retry (R3); PR3 iOS: the whole caption edit experience in one PR so TestFlight never ships a partial edit mode | One PR | Decide after the spike | Leave unspecified |
| Ordering | — | PR0 first; PR1 and PR2 independent of everything; PR3 after PR0 findings (Swift side of the fixture test lands in PR3) | — | — | — |
| TestFlight exposure | Whole feature at once | Server fix and banner retry ship early; caption editor ships whole | Whole feature at once | — | Unknown |

Question D33:
D33 — Ship as a device spike, then a server fix, a tiny banner fix, and one complete caption-editor PR?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Outside Voice, finding R13.
ELI10: Every change merged to main that passes iOS checks goes straight to external TestFlight testers (testflight.yml:6-23, :220). So if the caption editor lands in pieces, testers get a half-built edit mode. The server fix for the stale caption words is independent and can ship on its own Fly deploy. And three things are only knowable on a real iPhone (one-tap keyboard in the connected editor, the Turkish keyboard request, hardware keyboards), so a throwaway spike should come first. The outside reviewer suggested seven smaller slices; that only works behind a feature flag, which this plan doesn't have (reply Other if you want that route).
Stakes if we pick wrong: testers see a broken in-between editor, or a server-side bug fix waits on a large iOS review.
Recommendation: A because it ships the independent fixes early, keeps the user-facing editor whole on TestFlight, and retires the device unknowns before the big PR is written.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Apply this change (recommended)
  ✅ Testers never see a half-built caption editor; the server and banner fixes reach users sooner
  ✅ The device spike de-risks focus and keyboard behaviour before the large PR is built
  ❌ PR3 is still large (~14 files) for one review
B) Keep one PR
  ✅ One review, one merge, one place for the whole story
  ✅ No coordination between PRs
  ❌ The independent server fix waits on the iOS review, and device unknowns surface late
C) Investigate first
  ✅ Spike results could change the split
  ✅ Avoids committing to PR boundaries early
  ❌ The TestFlight exposure risk is known now and doesn't depend on the spike
D) Defer this change
  ✅ No plan change now
  ✅ The implementer can choose
  ❌ The default is one big PR with late device findings
Net: A orders the work by risk and release clock while keeping the user-facing piece whole; B keeps everything in one review.
Header: R13 PR split
Options (as asked):
A) Apply this change (recommended)
PR0: device spike, not merged (one-tap focus in the connected editor, `textInputMode`, hardware keyboard), findings written into this plan. PR1: server, failing reproducer pytest then R1/R11 rewrite + phone guard + `caption_word_rewrite.json` (Fly deploy). PR2: iOS save-banner retry only (R3). PR3: iOS caption edit experience whole (everything else), including the Swift fixture test. PR1 and PR2 can merge any time; PR3 after PR0.
B) Keep one PR
iOS and server changes in a single PR, merged together.
C) Investigate first
Run the device spike, then decide PR boundaries.
D) Defer this change
No PR structure in the plan; implementer decides.

State: approved
Actual answer: A) Apply this change (recommended) — user answer to D33, 2026-10-02
Accepted scope: PR0: device spike, not merged (one-tap focus in the connected editor, `textInputMode` override, hardware keyboard), findings written into this plan. PR1: server — failing reproducer pytest, then R1/R11 rewrite + phone guard + `src/apps/api/tests/fixtures/caption_word_rewrite.json` (Fly deploy). PR2: iOS save-banner retry only (R3). PR3: iOS caption edit experience whole (everything else), including the Swift fixture test. PR1 and PR2 merge any time; PR3 after PR0.
History: none

### R14: TODO — run the KriaMediaEngine package tests in CI
Finding: #14, P2, confidence 8/10, `src/apps/ios/project.yml:111-114` (scheme `testTargets`: `KriaTests`, `KriaUITests` only) + no `swift test` in `.github/workflows/*`, `scripts/ios/verify.sh` or the Makefile (only `scripts/ios/phone-audio-parity.py:21` mentions it, as a manual command); reviewer: iOS explorer (Sonnet), verified by primary.
Plan baseline: not in plan 025 (pre-existing gap). Plan 025 puts its own Swift fixture test in `KriaTests`, so it is unaffected.
Runtime evidence: static read. `KriaMediaEngineTests` (e.g. `DissolveTimingTests` for `phone_dissolve_timing.json`, `KaraokePainterTests` for `phone_karaoke_layout.json`) never run in CI, so the Swift half of those cross-platform parity fixtures can drift unnoticed.
Comparison grid:

| Choice | Current | A | B | C |
|---|---|---|---|---|
| R14 disposition | Untracked | Add TODO T-CAP025-3 to TODOS.md | Skip | Build now inside plan 025 (add a CI step) |

Question D34:
D34 — Track 'the media engine's Swift tests never run in CI' as a TODO?
Project/branch/task: nova main @ dcde00488, plan 025 (KRI-240), eng review Final planning decisions (TODOS), finding R14.
ELI10: The iPhone app's rendering package (KriaMediaEngine) has its own test suite, including the Swift half of the shared 'phone and server must agree' fixtures. The app's test scheme only runs the app's tests (project.yml:111-114), and no CI workflow runs the package's tests. So those agreement checks only run when someone remembers to run them by hand. This plan doesn't depend on it (its own fixture test lives in the app's tests), but it's a real gap I found on the way.
Stakes if we pick wrong: a phone/server rendering mismatch the fixtures were built to catch ships unnoticed.
Recommendation: A because it is out of scope for a caption-editor change, but worth a tracked fix.
Note: options differ in kind, not coverage — no completeness score.
Pros / cons:
A) Add to TODOS.md (recommended)
  ✅ The gap is written down with file pointers for a focused CI change
  ✅ Keeps plan 025 scoped to caption editing
  ❌ Parity drift stays possible until the TODO is done
B) Skip — not valuable enough
  ✅ Nothing to maintain
  ✅ No CI time added
  ❌ The Swift parity tests keep silently not running
C) Build it now in this plan (human: ~3h / CC: ~20 min)
  ✅ Parity fixtures start protecting every iOS PR immediately
  ✅ Small change: one CI step running the package tests on macOS
  ❌ Adds CI minutes and a workflow change to an iOS UI PR; may surface existing failures (52 fail without fixture paths in a sparse checkout)
Net: A tracks a real CI gap without widening KRI-240; C fixes it now at the cost of scope and CI time.
Header: R14 engine CI
Options (as asked):
A) Add to TODOS.md (recommended)
Add T-CAP025-3: run `swift test` for `src/apps/ios/Packages/KriaMediaEngine` in `ios.yml` (macOS lane, full checkout so `src/apps/api/tests/fixtures` and `src/packages/motion-runtime` resolve), triggered by package or fixture changes.
B) Skip — not valuable enough
No TODO.
C) Build it now in this plan
Add the CI step to plan 025's scope (separate small PR alongside PR1/PR2).

State: approved
Actual answer: A) Add to TODOS.md (recommended) — user answer to D34, 2026-10-02
Accepted scope: TODOS.md T-CAP025-3 added: run `swift test` for `src/apps/ios/Packages/KriaMediaEngine` in `ios.yml` (macOS lane, full checkout). Not part of plan 025's PRs.
History: none

Approval readiness: PASS — R1 (D21), R2 (D22), R3 (D23), R4 (D24), R5 (D25), R6 (D26), R7 (D27), R8 (D28), R9 (D29), R10 (D30), R11 (D31), R12 (D32), R13 (D33), R14 (D34); scope selectors D19 (defer D11.4) and D20 (Smaller arrangement) recorded in §10.1. Every accepted remedy cites its own user answer; regression contract R4 approved.

## Approved Mockups

| Screen/Section | Mockup Path | Direction | Notes |
|---|---|---|---|
| Caption edit state (keyboard up), iPhone 15 + SE | `/Users/yasinberkyesilyurt/.gstack/projects/emirerben-nova/designs/caption-edit-bar-20261001/design-board.html` (Variant B frames) | Collapsed chrome; preview fills above the bar (282×159 / 267×150pt); field + loop-play; Previous · 4 of 20 · Next · Done | HTML wireframe (designer key unavailable). Values pinned in D8; AX frame approved as D12. Variants A and C archived on the same board as rejected. |
| Browse state | same board, first frame | Unchanged shell; rows without chevron; playing row = selectionSoft + 3pt sky bar + semibold | Follow rule D15; dots D4; Delete swipe D3 |

## Implementation Tasks
Synthesized from this review's findings. Each task derives from a specific
finding above. Run with Claude Code or Codex; checkbox as you ship.

_Reconciled by the eng review (2026-10-02). Supersedes the design review's T1–T15, which named mechanisms the eng review replaced (`removeCaptionCue`, a new server language field, KriaMediaEngine renderer work, swipe Delete, focus-keyed edit state, keyboard swipe-down exit). Ordered by PR per R13 (D33)._

**PR0 — device spike (not merged)**
- [ ] **T0 (P1, human: ~4h / CC: ~30min)** — iOS spike — Prove the four device unknowns before PR3
  - Surfaced by: R13 (D33), R7 (D27), R9 (D29), D11 — one-tap focus in the connected editor, `textInputMode`, hardware keyboard, real preview budget
  - Files: throwaway branch only; findings written into this plan as §10.8
  - Verify: on an iPhone + SE/15/Pro Max simulators: row tap → `editingCueID` → `NativeExplicitLineTextEditor` focus with no UI-test hang; installed Turkish keyboard selected via `textInputMode`, fallback when absent; hardware keyboard shows the bar with key commands; measured preview height with R9 chrome hidden ≥ 220pt on SE (9:16)

**PR1 — server stale-words fix (Fly deploy; independent of the app)**
- [ ] **T1 (P1, human: ~2h / CC: ~10min)** — `phone_captions` — Failing reproducer first
  - Surfaced by: R11 (D31) item 5; R1 (D21)
  - Files: `src/apps/api/tests/pipeline/test_phone_captions.py`
  - Verify: `cd src/apps/api && pytest tests/pipeline/test_phone_captions.py -k stale_words` fails on main (edited word-style cue compiles the old words)
- [ ] **T2 (P1, human: ~1 day / CC: ~45min)** — server — Word rewrite at write time + legacy guard
  - Surfaced by: R1 (D21) + R11 (D31); R4 (D24) preserve-assertions (2) and (3)
  - Files: `src/apps/api/app/pipeline/word_timing.py` (`rewrite_cue_words`: D6 rule, `timing_quality`, never `smart_*`), `src/apps/api/app/routes/generative_jobs.py` (editor-commit `:9734-9736` and `persist_variant_captions` `:3478`: only cues whose `text` differs from the stored cue by `id`), `src/apps/api/app/pipeline/phone_captions.py` (`_prepare_cues` whitespace-free guard), `src/apps/api/tests/fixtures/caption_word_rewrite.json`, `tests/pipeline/test_word_timing.py`, `tests/routes/test_editor_commit.py`
  - Verify: `cd src/apps/api && pytest tests/pipeline/test_word_timing.py tests/pipeline/test_phone_captions.py tests/pipeline/test_phone_subtitled_plan.py tests/routes/test_editor_commit.py` green; T1 reproducer now passes; matching cues byte-identical

**PR2 — iOS save-banner retry (small)**
- [ ] **T3 (P2, human: ~1h / CC: ~5min)** — `NativeEditorSaveBanner` — "Try saving again" on `.failed`
  - Surfaced by: R3 (D23)
  - Files: `src/apps/ios/Kria/Features/NativeEditorComponents.swift`, a unit or UI test in the `editor` group
  - Verify: failed Save shows the banner with a 44pt `native-editor-retry-save` button that calls `session.save()`; existing `native-editor-save-state` identifier and copy unchanged

**PR3 — iOS caption edit experience (one PR so TestFlight never ships a partial edit mode)**
- [ ] **T4 (P1, human: ~1 day / CC: ~40min)** — `NativeExplicitLineTextEditor` — `LineEditorConfiguration`
  - Surfaced by: D20 structure; D5, D8, D11 (rules 2–3), D14; R4 (D24) preserve-assertion (1)
  - Files: `src/apps/ios/Kria/Features/NativeEditorTextPanel.swift`, `src/apps/ios/Tests/KriaTests/` (config tests)
  - Verify: default configuration equals today's Text editor settings; `.captionLine` wraps and grows 1→3 lines (2 on SE/AX), Return → next/done, paste "\n" → space, autocorrect off, spell-check only on a matching keyboard, `textInputMode` subtag match, key commands, `undoManager` cleared per line, caret stable on identical text
- [ ] **T5 (P1, human: ~2 days / CC: ~1h)** — `NativeEditorSession` — Line editing model
  - Surfaced by: R1/R11 (word rewrite from line-entry words), R5 (park frame), D7 (guided seek), D6/F8 (loop-play), D4 (first keystroke pauses), D5 (one transaction per line), R12 (removal = one step), F2 revised (`caption_language` from every variant-load path)
  - Files: `src/apps/ios/Kria/Features/NativeEditorSession.swift`, `src/apps/ios/Kria/Core/NativeEditorDocument.swift`, `src/apps/ios/Tests/KriaTests/NativeEditorSessionTests.swift`, Swift fixture test reading `caption_word_rewrite.json` via `#filePath`
  - Verify: unit tests for each listed behaviour; fixture parity with T2; alpha ≥ 0.9 at the park frame for `.captionPop`, `.none`, word styles; guided unit seeks; untouched line ⇒ no undo entry / no dot; clear + Return then one undo restores text and `words`
- [ ] **T6 (P1, human: ~2 days / CC: ~1h)** — `NativeCaptionPanel` + `CaptionEditBar` + `UndoToast` — Browse and edit UI
  - Surfaced by: D2/D8 (bar), D3 + R6 (long-press Delete) + R2 (guided Delete), R12 (toast), D4 (loading, empty, dots), D9 (remove helper copy), D15 (follow), D16 (read-only), R10 (preview-unavailable scrim)
  - Files: `src/apps/ios/Kria/Features/NativeEditorLanePanel.swift`, `src/apps/ios/Kria/Features/NativeEditorComponents.swift`
  - Verify: UI tests in the `editor` group for the Return loop, clear + Undo, long-press Delete (cue + guided), read-only seek + reason line; unit tests for the follow state machine and toast lifetime
- [ ] **T7 (P1, human: ~1 day / CC: ~40min)** — `NativeEditorView` / `NativeEditorIsland` — Edit-state shell
  - Surfaced by: D2 + R9 (hidden chrome incl. timeline handle, sparkles FAB, newer-job prompt, collapsed insets), D10 (`TypingLayout`), R7 (edit state = `editingCueID`; re-key `keyboardVisible` readers), F6, F13; R4 (D24) preserve-assertion (4)
  - Files: `src/apps/ios/Kria/Features/NativeEditorView.swift`, `src/apps/ios/Kria/Features/NativeEditorIsland.swift`, `src/apps/ios/Tests/KriaTests/NativeEditorLayoutMetricsTests.swift`
  - Verify: keyboard-up metrics at 667/852/932 viewports with real constants: Captions ≥ 220pt (9:16), Text 120pt with #1321 growth intact; browse `headerHeight` 94 and today's split; header returns on every reset path
- [ ] **T8 (P1, human: ~1.5 days / CC: ~45min)** — Accessibility — AX layout, VoiceOver, Reduce Motion
  - Surfaced by: D12, D13 (announcer seam per F15), D8 motion
  - Files: `src/apps/ios/Kria/Features/NativeEditorLanePanel.swift`, `src/apps/ios/Tests/KriaUITests/AppleTextAccessibilityUITests.swift`, KriaTests announcer test
  - Verify: AX5 at 320pt (`UI_TEST_DYNAMIC_TYPE_SIZE=accessibility5`, `UI_TEST_EDITOR_WIDTH`) shows field + Previous/Next/Done + loop-play with the keyboard up; combined row label; Edit action focuses the field; announcer receives "Line 5 of 20"
- [ ] **T9 (P1, human: ~1 day / CC: ~40min)** — UI test suite — Rewrite, add, register
  - Surfaced by: R4 (D24), F9, F10, F16
  - Files: `src/apps/ios/Tests/KriaUITests/NativeEditorInspectorUITests.swift`, `src/apps/ios/Kria/…/NativeEditorUITestFixtures.swift` (multi-cue fixture with `words` + `caption_language`), `scripts/ios/ui-test-groups.json`, `scripts/ios/ui-test-durations.json`
  - Verify: `testTappingCaptionRowEntersEditModeAndPersistsTypedText` passes with one tap, `app.textViews`, the bar's Done, persistence through reopen; new tests registered in `editor`; Text-tool, geometry and caption-visual suites unchanged and green
- [ ] **T10 (P2, human: ~1h / CC: ~10min)** — Docs — Runbook contract
  - Surfaced by: D10, D5, F13
  - Files: `docs/runbooks/ios-development.md`
  - Verify: KRI-148 section names Text `compactPreview(120)` (with #1321 growth) vs Captions `fillsAboveEditBar(220)`; Return = Next divergence from web documented
- [ ] **T11 (P1, human: ~3h / CC: n/a)** — Real-device acceptance
  - Surfaced by: §4 acceptance criteria, D8 (14pt readability), D11, R1/R11 (word-style phone render)
  - Files: none (device); recordings attached to KRI-240
  - Verify: TR + EN Talking edits on SE / 15 / Pro Max; Turkish keyboard requested when installed; caption ≥ 14pt on screen (≥ 12pt SE); a word-style phone render after Save shows the corrected words
- [ ] **T12 (P2, human: ~20min / CC: ~5min)** — Linear — Sync KRI-240
  - Surfaced by: process (issue text is now stale against D2–D34)
  - Files: Linear KRI-240
  - Verify: issue links `plans/025-kri-240-caption-text-editing.md` and summarises the PR0–PR3 plan

### Failure modes (eng review)

| New path | Realistic production failure | Test / handling | User sees |
|---|---|---|---|
| One-tap focus (T4/T5) | `becomeFirstResponder` refused (sheet, non-key window) | R7: bar stays with an unfocused field; T9 UI test | Bar with a tappable field; Done works |
| Edit-state chrome (T7) | View torn down mid-edit (tool switch, job reload, conflict rebase) | R7 reset paths; T7 verify | Header returns; nothing stuck |
| iOS word rewrite (T5) | Typo then revert re-spreads timings | R11: exact restore from entry words; unit test | Nothing (timings intact, no phantom dot) |
| Server rewrite (T2) | Untouched non-space-language cue flagged stale | R11: only text-changed cues by id; pytest | Nothing |
| Phone compile (T2) | Legacy stale `words` burn the old text | Guard + T1 reproducer | Corrected words in the render |
| Park frame (T5) | Caption invisible at `startS` | R5 offset; alpha test | Visible caption on the parked frame |
| Live preview unavailable (T6) | Originals missing on device | R10 scrim + notice | "Preview shows your last render…" |
| Save (T3) | 5xx / offline / 422 | Banner + retry; existing copy | "Couldn’t save this edit" + "Try saving again" |
| Caption language (T5) | Variant without `caption_language` | Absent ⇒ today's keyboard behaviour; unit test | Normal keyboard |

Critical gaps (no test AND no handling AND silent): none.

### Worktree parallelization strategy

| Step | Modules touched | Depends on |
|---|---|---|
| PR0 spike | iOS editor (throwaway) | — |
| PR1 server | `src/apps/api/app/pipeline`, `src/apps/api/app/routes`, `src/apps/api/tests` | — |
| PR2 banner | iOS `Features/NativeEditorComponents` | — |
| PR3 caption editor | iOS `Features/NativeEditor*`, `Core/NativeEditorDocument`, iOS tests, `scripts/ios`, `docs/runbooks` | PR0 findings; PR1 fixture JSON (for the Swift fixture test) |

Parallel lanes: Lane A: PR1 (server, independent). Lane B: PR0 → PR3 (iOS editor). Lane C: PR2 (tiny, touches `NativeEditorComponents.swift`, which PR3 also edits).
Execution order: launch A + B(PR0) + C together. Merge C before PR3 starts editing `NativeEditorComponents.swift`. Start PR3 after PR0 findings land; rebase PR3 on PR1's fixture JSON before its Swift fixture test.
Conflict flags: `NativeEditorComponents.swift` (PR2 + PR3) → sequence C before PR3.

### NOT in scope (eng review additions)
- **Locale-aware `text_case`** — deferred to TODOS.md `T-CAP025-2` (D19).
- **KriaMediaEngine package tests in CI** — TODOS.md `T-CAP025-3` (D34); plan 025's fixture test lives in `KriaTests`.
- **Feature-flagged incremental iOS slices** — not chosen (D33); PR3 ships the edit experience whole.
- **Server `caption_language` field / openapi regen** — unnecessary; the field already exists (F2, D20).

### What already exists (eng review additions)
- `session.deleteSelection(.captionCue)` (#1319) for every removal path (R2, R6, R12).
- `caption_language` on subtitled / phone narrated variants, read at variant load beside `configureCapabilities(from:)` (F2 revised).
- `NativeEditorSaveBanner` + sibling retry-button pattern (R3).
- Preview rebuild coalescing at 80 ms inside transactions (performance, §10.5).
- `rebuild_word_timings_for_text` / `synthesize_word_timings` in `word_timing.py` as the home for the shared rule (R1).
- Shared-fixture pattern `phone_dissolve_timing.json` (Swift `#filePath` + pytest) for `caption_word_rewrite.json`.
- `UI_TEST_DYNAMIC_TYPE_SIZE` / `UI_TEST_EDITOR_WIDTH` / `UI_TEST_REDUCE_MOTION` launch environment for AX and motion tests.

### Suppressed findings (appendix, confidence ≤ 4)
- **Cloud reburn and languages without spaces** (confidence 4): `captions._words_match_text` (`captions.py:756-758`) compares whitespace tokens, so for a language written without spaces an untouched cue's real word timings may be replaced by an even split at reburn. Not verified against real jobs; Kria's captions are TR/EN today. Pre-existing; plan 025's own rewrite avoids it (R11).

### Eng review unresolved decisions

None. Every Scope Challenge, Section 1–4, Outside Voice and TODO choice has an individual answer (D19–D34).

### Eng review completion summary (2026-10-02)
- Step 0: Scope Challenge — scope accepted with one deferral (D19) and the Smaller arrangement (D20)
- Architecture Review: 2 issues found (R1 stale words, R2 guided Delete)
- Code Quality Review: 1 issue found (R3 duplicate save banner)
- Test Review: diagram produced, 35 gaps identified; regression contract R4 approved
- Performance Review: 0 issues found
- NOT in scope: written
- What already exists: written
- TODOS.md updates: 2 items (T-CAP025-2 via D19, T-CAP025-3 via D34)
- Failure modes: 0 critical gaps flagged
- Unresolved decisions: 0 in this review
- Outside voice: Sonnet subagent (in-host), completed, 11 findings → 8 decisions (R5–R12) + R13 sequencing + 4 factual corrections; Codex skipped (user asked for Sonnet agents)
- Parallelization: 3 lanes, 3 parallel (PR1, PR0→PR3, PR2) / PR3 sequential after PR0 and PR2
- Lake Score: 3/4 = coverage choices answered with the 10/10 option (D21 R1, D23 R3, D24 R4); D19 took the 7/10 Defer; kind choices excluded

### Design review history

### Design review completion summary (2026-10-02)

```
  +====================================================================+
  |         DESIGN PLAN REVIEW — COMPLETION SUMMARY                    |
  +====================================================================+
  | System Audit         | DESIGN.md present; UI scope = iOS caption  |
  |                      | edit (browse + edit state), 2 screens      |
  | Step 0               | 6/10 initial impression; all 7 passes      |
  | Pass 1  (Info Arch)  |  4/10 →  9/10 after fixes                  |
  | Pass 2  (States)     |  3/10 → 10/10 after fixes                  |
  | Pass 3  (Journey)    |  4/10 →  9/10 after fixes                  |
  | Pass 4  (AI Slop)    |  5/10 → 10/10 after fixes                  |
  | Pass 5  (Design Sys) |  3/10 → 10/10 after fixes                  |
  | Pass 6  (Responsive) |  3/10 → 10/10 after fixes                  |
  | Pass 7  (Decisions)  | 15 resolved, 0 deferred                    |
  +--------------------------------------------------------------------+
  | NOT in scope         | written (8 items)                          |
  | What already exists  | written                                    |
  | TODOS.md updates     | 1 item proposed (T-CAP025-1, added)        |
  | Approved Mockups     | 1 board (3 variants + AX), 1 approved (B)  |
  | Decisions made       | 15 added to plan (D2–D16)                  |
  | Decisions deferred   | 0                                          |
  | Overall design score | 3/10 → 9/10                                |
  +====================================================================+
```

Overall = lowest rated pass: before 3 (Passes 2, 5, 6), after 9 (Passes 1 and 3: list context absent while typing is a documented trade-off; the start-of-line caret limitation is named, not removed).

Design-review unresolved decisions: none (every finding in §7.2 has an individual decision in §6).

## 11. Implementation status (2026-10-02)

**Layout switched to Variant A by the user** ("ship design option A", 2026-10-02, after the side-by-side board), superseding D2's Variant B. The app header, Chat/Editor switch and top banners stay while a line is open; the edit bar replaces the caption list; the timeline handle, transport, tool rail and sparkles button hide and the preview fills the space above the bar (`captionEditBarHeight` in `NativeEditorLayoutMetrics`). R7 (edit state = `editingCueID`), R8 (no swipe-down exit) and R10 still apply; R9's header-hiding no longer applies.

**Built on branch `ybyesilyurt/kri-240-iphone-editor-editing-a-caption-line-takes-two-taps-and-the` (PR3 scope, Variant A):**
- One-tap editing via `NativeExplicitLineTextEditor` + `LineEditorConfiguration.captionLine` (wrap, Return = Next / Done on last, autocorrect off, `caption_language` keyboard via `textInputMode`, spell-check off without a matching keyboard, pasted newlines flattened, Tab / Shift-Tab / Esc / Shift-Return, per-line undo reset). Default configuration is unchanged for the Text tool.
- Edit bar: "N of M · time", Previous / Next, Done (butter), wrapping field, loop-play (lilac); preview tap toggles the loop.
- Session: `beginCaptionLineEdit` / `endCaptionLineEdit`, `CaptionWordRewrite` (positional keep, character-weight spread, exact restore), `captionParkTime` (start + 0.15s), `captionTimelineRange`, `isCaptionUnitEdited`, `captionLanguage` (from the loaded variant).
- List: no chevron, playing-line highlight + follow (suspended by a drag, off under VoiceOver / while editing), unsaved dot, combined VoiceOver row with Edit / Delete actions, long-press Delete, read-only rows still seek with a reason line, empty state copy, helper sentence removed.
- Emptied line removed on commit inside the line's transaction (one Undo restores it); "Line N removed · Undo" notice; stale-preview notice when the live preview is unavailable.
- Tests: `CaptionLineEditingTests` (14), rewritten `testTappingCaptionRowEntersEditModeAndPersistsTypedText` (asserts the keyboard after ONE tap). Verified: full `KriaTests` 907 pass; 10 caption / Text-tool UI tests pass; simulator walk-through on iPhone 17 Pro (Turkish keyboard selected, Next, Done, dots).

**Deviations from the reviewed plan (implementation choices):**
- The field is a fixed 3-line box (2 on small phones and accessibility sizes) that scrolls inside, instead of growing 1→3 lines (D8); the bar height is fixed anyway, so growth would only move empty space.
- At accessibility sizes the counter stays in the top row (scales down) rather than moving above the field (D12).
- No loading skeleton (D4): the editor has no "captions still transcribing" signal to drive it.
- The save-failure "Try saving again" button (R3 / PR2) and the server word rewrite + phone compiler guard (R1/R11 / PR1) are not in this branch.
- No device spike (PR0) yet; the real-iPhone check (T11) is still open.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Outside Review | native Sonnet subagent (in-host) in `/plan-design-review` and `/plan-eng-review`; Codex skipped by user instruction | Independent 2nd opinion | 2 | issues_found (native only; outside skipped) | design: 8 findings; plan-review: 11 findings → R5–R13 + 4 factual corrections |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 21 (1 this plan; 20 earlier project runs, all older than 7 days) | ISSUES OPEN (PLAN) | 38 issues, 0 critical gaps (3 architecture/code-quality + 35 test gaps), all mapped to approved tasks T0–T12 |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | CLEAR (FULL) | score: 3/10 → 9/10, 15 decisions — plan changed since review: eng review amended D2, D3, D4, D5, D6, D8, D11, D14 and the D22 guided-Delete clause (D19–D34) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **OUTSIDE COVERAGE:** codex · design phase · skipped (user asked for Sonnet agents); codex · plan-review phase · skipped (same). Native Sonnet subagents completed both phases (design: 8 findings, 3 critical; plan-review: 11 findings, 3 verified defects in approved decisions). No external-model coverage exists for this plan.
- **VERDICT:** DESIGN CLEAR. Eng review ISSUES OPEN (PLAN): 38 issues found, every one resolved into approved tasks T0–T12 with 0 unresolved decisions and 0 critical gaps; the dashboard treats only a zero-issue run as clean — eng review required.

NO UNRESOLVED DECISIONS
