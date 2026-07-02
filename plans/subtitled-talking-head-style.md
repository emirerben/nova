# Subtitled talking-head edit style — design plan

Third user-facing edit style, after **Montage** and **Narrated walkthrough**.
The user uploads one talk-to-camera clip (face optional). Nova transcribes the
**in-video speech** and burns **word-by-word highlighted captions** that are
editable like every other caption. Must be excellent in **Turkish and English**.

Status: design review in progress (`/plan-design-review`). Decisions marked
**PENDING** are resolved through the review's AskUserQuestion gates.

---

## Scope (agreed in Step 0)

- **Shape:** single captioned clip (not speaker + B-roll). Face optional.
- **Caption look:** word-by-word highlight (active-word emphasis).
- **Language:** auto-detect + editable override.
- **Review depth:** all 7 design dimensions.

---

## What already exists (reuse map — do NOT rebuild)

| Capability | Where | Reuse |
|---|---|---|
| Transcription (Whisper: openai-api word-level, or faster_whisper local; gemini backend default) | `src/apps/api/app/pipeline/transcribe.py` | Source of caption timing. **Gap: English-only + no language param.** |
| Cue builders — sentence blocks + word-per-cue | `captions.py` `build_plain_cues`, `build_word_cues` | Sentence cues = editable source of truth. |
| Per-word karaoke `\k` lines **from sentence groups** | `captions.py` `_build_dialogue_lines` (272) | Render word-by-word highlight from sentence cues — decouples edit from render. |
| ASS burn + reburn (libass) | `narrated_assembler.py` `burn_captions_on_video`, `_generate_caption_ass` | Burn path for the new style. |
| Caption-free base for fast reburn | `variant["base_video_path"]` | Edit → Apply without full re-render. |
| On-video caption editor (paused edit, tap-to-fix, cue list, font row, Apply) | `src/apps/web/src/app/plan/_components/CaptionEditor.tsx` | The editing surface. Currently narrated-only. |
| Caption edit endpoints | `routes/plan_items.py` `PATCH …/captions`, `…/caption-font`, `POST …/captions/apply` | Persist + reburn. |
| Sentence/word style toggle + font | `voiceover_caption_style`, `voiceover_caption_font` on `PlanItem` | Model fields. |
| Archetype dispatch | `generative_build.py` `_resolve_archetype`, `_specs_for_archetype` | Add the new style's branch. |
| `talking_head` edit format (Speaker + B-roll, **no captions**) | `edit_format.py`, `talking_head_assembler.py` | Distinct from this style — see D7. |
| Glyph-coverage assertion (agentic overlays only) | `text_overlay_skia.py` `assert_glyphs_present` | Extend to caption fonts for Turkish. |
| Font-registry extended-Latin gap notes | `assets/fonts/font-registry.json` (`note`) | Flag caption fonts lacking Turkish glyphs. |
| Design system (light editorial: cream/lime/Fraunces) | `DESIGN.md` | All product-UI decisions calibrate here. |

**Design surfaces:** (1) product UI — style picker + CaptionEditor (follows
DESIGN.md); (2) burned on-video captions — a separate medium governed by the
video-overlay rules (DESIGN.md §10 exempts burned fonts from web font rules).

---

## The linchpin: decouple edit granularity from render granularity

`build_word_cues` emits **one cue per word** → a 45s clip = 120-180 rows in
CaptionEditor's 224px scroller. Unusable. But `_build_dialogue_lines` already
renders per-word `\k` highlight **from sentence cues**. So: **edit sentence
cues (~15 rows), render word-by-word at Apply.** This is the recommended
architecture (D1) and it's also where the active-word emphasis (D2) is encoded.

---

## Seven-pass findings (verified + adversarially challenged)

Ratings are the request's design completeness on each dimension (before fixes).

1. **Information architecture — 3/10.** Word-mode overflows the editor; preview
   geometry (78px bottom-third) contradicts the word-mode burn (120px mid-frame);
   no entry point in the hardcoded picker.
2. **Interaction states — 2/10.** `low_confidence` is dead in prod (openai-api
   hardcodes confidence 1.0); no wrong-language state; transcription-in-progress
   is undifferentiated; no review-first nudge.
3. **User journey — 3/10.** "Reading your speech" and "review your captions" are
   distinct emotional beats with no design; users may Apply without ever opening
   the cue list.
4. **AI-slop risk — 3/10.** The word-by-word look is generic (flat white or
   yellow-karaoke cliché); no Nova signature; casing undecided (raw transcript).
5. **Design-system alignment — 6/10.** CaptionEditor + picker already follow the
   light editorial system; the new style card fits. Burn-side emphasis token
   undocumented.
6. **Responsive & a11y — 3/10.** Cue rows + font chips < 44px; no reduced-motion
   rule for word highlight; no SR model for the karaoke effect; Turkish glyph
   coverage + preview contrast unverified.
7. **Unresolved decisions — see below.**

---

## Decisions (resolved)

- **D1 — Edit sentences, render word-by-word.** CaptionEditor edits sentence
  cues (`build_plain_cues`, ~15 rows). The burn derives per-word highlight from
  those cues via `_build_dialogue_lines` `\k` tags at Apply. Do NOT expose
  word-cues as the edit surface. Remove the hard `max-h-56` for a taller
  sticky-header scroller; add a text-filter over cue text.
- **D2 — Lime active-word fill.** The currently-spoken word gets a bright lime
  fill box + near-black text (DESIGN.md §9 "one accent per surface"). Encode
  once in `_ass_caption_header`; hard-gate the legacy yellow-karaoke header so it
  can't ship as the default. Document the caption emphasis token in DESIGN.md §10
  (burned-font exemption applies).
- **D3 — Sentence case.** Captions render clean sentence-case (the intentional
  counter to the CapCut all-caps cliché, matches editorial voice). No naive
  uppercase. If ALL-CAPS is ever offered, use Turkish-locale casing (İ/ı) with a
  pinned Turkish test.
- **D4 — Safe-zone band + preview mirrors burn.** Pin captions in a platform-safe
  band (~18-22% from bottom, clear of face + TikTok/Reels UI). Thread the resolved
  caption style into `CaptionEditor` so the DOM preview matches the real burn
  size + position (keep the single `_ass_caption_header` source-of-truth so
  preview and libass can't drift).
- **D5 — Locale-defaulted language chip + confirm on change.** Auto-detect fills
  a chip ("Captions in Türkçe · change"), defaulted from the user's plan locale
  (zero-click common case). A language change shows a confirm dialog
  ("re-transcribing replaces your edits"), reusing the swap-song confirm pattern.
  Persist selected/detected language on the plan item; thread a `language` param
  into `transcribe()` → `whisper-1`.
- **D6 — Review-first banner + transcription loading beat.** After transcription
  the Captions tab opens with a persistent "Check your captions before applying"
  notice (`border-zinc-200 bg-white text-[#3f3f46]`, DESIGN.md §3 — NOT amber on
  light) until the user interacts with the cue list. A distinct serif loading line
  ("Reading what you said…") during transcription, driven by a real backend phase
  event, honoring §6 reduced-motion. Reserve "may have misheard" copy for the
  local backend where confidence is real; file the prod confidence gap.
- **D7 — New `subtitled` format token + third picker card.** Add a "Subtitled"
  card (desc: "One talking-to-camera clip, auto-captioned word by word") using the
  existing card grammar (`border-lime-400 bg-lime-50` active state). New
  `EDIT_FORMATS` token, distinct from `talking_head`; route through
  `coerce_edit_format`. Uploader copy sets the "one clip" constraint before
  generation.
- **D8 — Specify all three a11y fixes.** (1) Cue rows + font chips to
  `min-h-[44px]` (DESIGN.md §11); (2) word-highlight preview uses `motion-safe:`,
  collapses to static under `prefers-reduced-motion`, animation `aria-hidden`, the
  full line is the SR unit; (3) extend `assert_glyphs_present` to caption fonts for
  the Turkish set (ı İ ş ğ ç ü ö), flag gaps via the font-registry `note`
  convention, and tighten the preview `OUTLINE` so it never under-represents the
  burn.

---

## Interaction states (Pass 2 fix)

| State | What the user SEES |
|---|---|
| Transcribing (loading) | Serif line "Reading what you said…" in the §7 loading system; driven by a real backend phase event, not a timer. |
| Ready → review | Captions tab auto-opens with a persistent cream/zinc "Check your captions before applying" banner until the cue list is touched (D6). |
| Empty (no speech detected) | Quiet zinc invitation, not an error: "No speech found in this clip. Try Montage, or upload a clip where you're talking." + switch-style CTA. (Existing code already skips captions gracefully — this makes the outcome legible.) |
| Wrong language | Detected-language chip is editable ("Captions in Türkçe · change"); changing it confirms then re-transcribes (D5). |
| Low confidence | Only surfaced on the local backend (real confidence); prod relies on the mandatory review banner instead (D6). |
| Editing | Sentence rows (≥44px), lime active row, tabular timecode, text-filter; "Saved" ↔ "Unsaved edits" status. |
| Applying | "Applying…" overlay on the preview; fast reburn from `base_video_path`. |
| Partial / failure | Per DESIGN.md §7 D10: dashed zinc tile, plain-language reason; partial success celebrated. Never a red wall or raw FFmpeg output. |

---

## User journey storyboard (Pass 3 fix)

| Step | User does | Feels | Plan supports it |
|---|---|---|---|
| 1 | Picks "Subtitled" | "This is the one I want" | Third card + New badge, single-clip copy (D7) |
| 2 | Uploads one talking clip | Hopeful, slightly unsure | Upload copy sets the one-clip constraint |
| 3 | Waits | "Is it even hearing me?" | "Reading what you said…" beat (D6) |
| 4 | Sees auto-captions | Curious → scanning for errors | Review-first banner + cue list open (D6) |
| 5 | Fixes 2-4 words | "Easy to correct" | Sentence rows, filter, tap-to-fix, live preview (D1, D8) |
| 6 | Applies | Confident, proud | Fast reburn; safe-zone lime captions that look like Nova (D2, D4) |

---

## NOT in scope (deferred, with rationale)

- Speaker + B-roll captioning (the existing `talking_head` archetype) — separate style.
- Multi-clip subtitle stitching — single-clip only for v1.
- Auto-translation / bilingual dual captions — transcribe in the spoken language only.
- Emoji / animated sticker captions — out of the editorial brand.

---

## Approved mockups

| Screen | Reference | Direction |
|---|---|---|
| Word-by-word caption editor | rendered inline (no image export; OpenAI key absent) | Cream/lime/Fraunces; lower-third caption, lime active-word box; detected-language chip; sentence-row cue list; Apply pill |
| Three-style picker | rendered inline | "Subtitled" card + New badge alongside Montage / Narrated walkthrough |

---

## Implementation Tasks
Synthesized from this review's findings. Each derives from a specific decision above.

- [ ] **T1 (P1, human: ~1d / CC: ~30min)** — transcribe — Turkish language support
  - Surfaced by: i18n-turkish — English-only models + no `language` param
  - Files: `src/apps/api/app/pipeline/transcribe.py`, `config.py`, `models.py` (+ migration), `generative_build.py`
  - Verify: a Turkish talking-head clip transcribes correctly; language persists on the item
- [ ] **T2 (P1, human: ~1d / CC: ~30min)** — captions — sentence-cue → word `\k` render + lime active-word emphasis
  - Surfaced by: D1 + D2 — render word-by-word from sentence cues; encode lime fill once
  - Files: `captions.py` (`_build_dialogue_lines`, `_ass_caption_header`), `narrated_assembler.py`
  - Verify: burn shows lime active word derived from sentence cues; legacy yellow-karaoke gated off
- [ ] **T3 (P1, human: ~1.5d / CC: ~45min)** — generative_build — `subtitled` archetype sourcing in-video audio
  - Surfaced by: D7 — new format token + render path + kill switch
  - Files: `edit_format.py`, `generative_build.py` (`_resolve_archetype`, `_specs_for_archetype`, new render), `config.py`
  - Verify: a `subtitled` job renders a captioned single clip with editable cues
- [ ] **T4 (P1, human: ~2h / CC: ~15min)** — page.tsx — third picker card + single-clip uploader copy
  - Surfaced by: D7 / journey — no entry point
  - Files: `src/apps/web/src/app/plan/items/[id]/page.tsx`
  - Verify: "Subtitled" card selectable; persists `edit_format`
- [ ] **T5 (P1, human: ~4h / CC: ~20min)** — CaptionEditor — route subtitled to editor + preview mirrors word-mode geometry + safe zone
  - Surfaced by: D4 + info-arch — preview lies in word mode
  - Files: `eligibility.ts`, `CaptionEditor.tsx`, `page.tsx`
  - Verify: DOM preview size/position matches the burned output
- [ ] **T6 (P1, human: ~3h / CC: ~20min)** — CaptionEditor — edit sentence rows, remove `max-h-56`, add text-filter
  - Surfaced by: D1 / caption-scale — 150-row overflow
  - Files: `CaptionEditor.tsx`
  - Verify: a 60s clip shows ~15 sentence rows, filterable
- [ ] **T7 (P1, human: ~4h / CC: ~20min)** — states — review-first banner + "Reading what you said…" loading beat
  - Surfaced by: D6 / journey — no review nudge, no transcription beat
  - Files: `CaptionEditor.tsx` / item page, `components/progress/`
  - Verify: banner persists until the cue list is touched; loading line shows during transcription
- [ ] **T8 (P2, human: ~4h / CC: ~20min)** — language — editable chip + confirm-on-change re-transcribe
  - Surfaced by: D5 — language override placement + destructive re-transcribe
  - Files: `CaptionEditor.tsx`, `plan-api.ts`, `routes/plan_items.py`
  - Verify: changing language confirms, then re-transcribes; edits not silently lost
- [ ] **T9 (P1, human: ~3h / CC: ~20min)** — fonts — Turkish glyph coverage for caption fonts + sentence-case
  - Surfaced by: D3 + responsive-a11y — unverified glyph coverage, casing
  - Files: `text_overlay_skia.py` (`assert_glyphs_present`)/`captions.py`, `assets/fonts/font-registry.json`
  - Verify: ı İ ş ğ ç ü ö render (no tofu); registry flags any gap; captions render sentence-case
- [ ] **T10 (P1, human: ~3h / CC: ~20min)** — a11y — 44px targets, reduced-motion + aria on highlight, preview outline
  - Surfaced by: D8 / responsive-a11y
  - Files: `CaptionEditor.tsx`
  - Verify: targets ≥44px; reduced-motion collapses the highlight; SR reads the line not the flashing word
- [ ] **T11 (P2, human: ~2h / CC: ~15min)** — states — no-speech empty state copy + switch-style CTA
  - Surfaced by: states — no-speech outcome illegible
  - Files: item page
  - Verify: a music-only/no-speech clip shows a quiet invitation, not a blank render
- [ ] **T12 (P3, human: ~30min / CC: ~10min)** — backend — file prod ASR confidence gap (openai-api hardcodes 1.0)
  - Surfaced by: states — `low_confidence` dead in prod
  - Files: `transcribe.py` (tracking note)
  - Verify: n/a (tracking)

---

## Engineering review (plan-eng-review)

Verified against the code. Scope is right-sized: one new `subtitled` format token +
one lean render path; ~13 files but each touch is small. Reuse the caption
sub-components; do NOT reuse `narrated_assembler` (voiceover-spine + clip reflow,
none of which a single captioned clip needs).

**Correction to the design-review Turkish framing:** prod `whisper_backend="openai-api"`
calls **`whisper-1`, which is multilingual**. The `base.en`/`small.en` names only bind
the *local* faster-whisper backend. So the prod Turkish fix is "pass a `language` param
to whisper-1," not "swap models." (Swap the local dev model `small.en`→`small` so dev
isn't English-only.)

### Eng decisions

- **E1 — libass active-word color-pop (NOT Skia).** The lime *filled box* in the mockup
  isn't cleanly expressible in libass `\k` (that's a progressive sweep). Render per-word-
  window Dialogue events: full line visible, active word recolored lime (optional
  underline). Pure libass → keeps the fast `base_video_path` reburn. A literal filled box
  is a later Skia upgrade (deferred, see NOT in scope). `_ass_caption_header` gets the
  lime PrimaryColour for the active-word span; hard-gate the legacy yellow `_ASS_HEADER`.
- **E2 — lean render path.** Subtitled = keep source audio (like `original_text`) +
  transcribe + burn captions. Reuse `transcribe`, `captions.py`, `CaptionEditor`, the
  reburn endpoints. `narrated_assembler` verified NOT reusable (needs a voiceover file).
- **E3 — wire `synthesize_word_timings` on reburn.** Editing sentence text drops per-word
  timestamps; `word_timing.py:36` `synthesize_word_timings(words, start_s, end_s)` already
  distributes durations across a window but is unused in the reburn path. In the subtitled
  reburn, split each edited sentence cue into per-word windows, then emit the color-pop
  events. (Fresh burn keeps real whisper word timing.)
- **E4 — whisper-1 + `language` param.** Thread the D5 language (chip) into
  `transcribe()`→`whisper-1`'s `language` arg. Swap local dev model `small.en`→`small`.
  Gemini (multilingual) is the fallback only if whisper-1 Turkish quality proves weak.
- **E5 — persist a caption-free `base_video_path` for subtitled.** `talking_head` sets it
  to `None` (generative_build.py:4281) → every edit re-renders. Single-clip base is cheap;
  write it so Apply is a seconds-long reburn (matches the narrated contract the editor
  expects).
- **E-SEC — namespace the transcript cache by `item_id`.** Prior IDOR
  (`redis-store-idor-bind-to-owner`): a transcript cache keyed on a bare id let users read
  each other's footage. Key on `(item_id, …)` so the owner-checked route scopes the read.

### Data flow (subtitled render + reburn)

```
UPLOAD one clip
   │
   ▼
extract audio ──► transcribe(whisper-1, language=<chip>) ──► words[] (word ts)
   │                                                            │
   ▼                                                            ▼
render: source audio kept, caption-free base ───────►  build_plain_cues() → sentence cues
   │  (persist base_video_path)                                 │  (editable source of truth)
   ▼                                                            ▼
first burn: _build_dialogue_lines(words) → per-word color-pop  persist cues + language + base
   │
   ▼
EDIT (CaptionEditor): user fixes sentence text  ──► PATCH cues (no re-render)
   │
   ▼
APPLY reburn (fast, from base):
   for each edited cue: synthesize_word_timings(cue.text.split, cue.start, cue.end)
   → per-word-window color-pop events → burn_captions_on_video(base + ASS)
```

### Test coverage (gaps → add with the feature)

```
CODE PATHS                                          USER FLOWS
[+] captions.py                                     [+] Subtitled edit
  ├── synthesize_word_timings on edited cues          ├── [GAP][→E2E] upload→transcribe→review→edit→apply
  │   ├── [GAP] added words (edit lengthens cue)      ├── [GAP] fix a mis-heard Turkish word (İ/ı) → reburn
  │   ├── [GAP] removed words (edit shortens)         └── [GAP] change language chip → confirm → re-transcribe
  │   └── [GAP] empty/1-word cue window
  ├── per-word color-pop ASS emit                     [+] States
  │   ├── [GAP] identical line layout across N events   ├── [GAP] no-speech clip → empty state
  │   └── [GAP] long Turkish word line-wrap parity      ├── [GAP] wrong-language detect → chip override
  └── _ass_header lime PrimaryColour + gate yellow     └── [GAP] transcription failed → quiet failure tile
[+] transcribe.py language param                     [+] i18n [→EVAL]
  ├── [GAP] language="tr" passed to whisper-1           └── [GAP] Turkish transcription quality eval (TR clip)
  └── [GAP] local backend multilingual model
[+] generative_build subtitled render
  ├── [GAP] persists base_video_path (not None)
  └── [GAP][REGRESSION] narrated path unchanged

COVERAGE: 0/17 (new feature)  |  GAPS: 17 (1 E2E, 1 eval)
```

**CRITICAL (regression):** a guard that the existing `narrated` caption path is byte-
unchanged after the `_ass_caption_header` lime edit (renderer-parity per CLAUDE.md).
**Eval:** a Turkish transcription-quality case (prompt/ASR change → `tests/evals/`).

### Failure modes

| Codepath | Failure | Test? | Handled? | User sees |
|---|---|---|---|---|
| transcribe(language) | whisper-1 429/500 | add | needed | quiet failure tile (§7 D10) — **critical if silent** |
| synthesize_word_timings | edit adds many words, cue window tiny | add | clamp per-word floor (reuse `_MIN_WORD_CUE_S`) | captions flash — cap min duration |
| no-speech clip | empty transcript | add | exists (skips captions) | needs the D6 empty state (T11) |
| base_video_path missing | reburn can't find base | add | fall back to full render | slower Apply, not a failure |

### Parallelization

| Lane | Modules | Depends on |
|---|---|---|
| A (backend render) | `pipeline/`, `tasks/generative_build.py`, `agents/_schemas` | — |
| B (frontend) | `web/src/app/plan`, `web/src/lib` | A's `edit_format` token + variant shape |

Lane A (E1-E5, T1-T3, T9) and Lane B (T4-T8, T10-T11) split cleanly. Launch A first;
B needs the `subtitled` token + variant fields from A. Within A, T2 (render) depends on
T1 (language) only for the fresh-burn path. Conflict flag: both eventually touch
`CaptionEditor.tsx` (B) vs `captions.py` (A) — no overlap.

### Engineering implementation tasks

- [ ] **ET1 (P1, human: ~3h / CC: ~20min)** — captions — lime active-word color-pop + gate yellow header
  - Surfaced by: E1 — `\k` can't do a per-word box; recolor active word per window
  - Files: `src/apps/api/app/pipeline/captions.py`
  - Verify: burn shows white line, active word lime; legacy yellow path unreachable
- [ ] **ET2 (P1, human: ~4h / CC: ~25min)** — captions — wire synthesize_word_timings into subtitled reburn
  - Surfaced by: E3 — edited cues have no per-word timing
  - Files: `src/apps/api/app/pipeline/captions.py`, `src/apps/api/app/tasks/generative_build.py`
  - Verify: edit a sentence, Apply, per-word highlight still tracks; test added/removed words
- [ ] **ET3 (P1, human: ~1d / CC: ~40min)** — generative_build — lean `subtitled` render path + base cache
  - Surfaced by: E2 + E5 — keep source audio, persist caption-free base_video_path
  - Files: `src/apps/api/app/tasks/generative_build.py`, `src/apps/api/app/agents/_schemas/edit_format.py`
  - Verify: subtitled job renders captioned clip; Apply reburns from base in seconds
- [ ] **ET4 (P1, human: ~3h / CC: ~20min)** — transcribe — language param → whisper-1 + local multilingual model
  - Surfaced by: E4 — Turkish needs a language hint
  - Files: `src/apps/api/app/pipeline/transcribe.py`, `src/apps/api/app/config.py`
  - Verify: language="tr" reaches whisper-1; Turkish clip transcribes; dev model multilingual
- [ ] **ET5 (P2, human: ~1h / CC: ~10min)** — cache — namespace transcript cache by item_id
  - Surfaced by: E-SEC — prior IDOR learning
  - Files: transcript cache/store + `routes/plan_items.py`
  - Verify: cross-user read of another item's transcript returns 404/empty
- [ ] **ET6 (P1, human: ~2h / CC: ~15min)** — tests — narrated renderer-parity regression + Turkish eval
  - Surfaced by: Test review — CRITICAL regression + i18n eval
  - Files: `tests/pipeline/`, `tests/evals/`
  - Verify: narrated ASS byte-unchanged; Turkish transcription eval passes

---

## v1 scope (FINAL — after outside-voice challenge)

The outside voice showed word-by-word concentrates nearly all the feature's risk
(whisper-1 word-timestamp drift on fast/code-switched Turkish, libass wrap jitter,
preview-geometry mismatch), while **sentence-block captions are already wired end to
end** and reburn + preview cleanly. Re-scoped accordingly.

### v1 ships (sentence-block captions)

- New `subtitled` edit-format token + picker card, **kill-switch gated** so an
  unimplemented token never silently falls back to montage (Fix 4).
- Lean render path: single clip, keep source audio, transcribe (whisper-1 +
  `language` from the D5 chip), `build_plain_cues` → burn via
  `generate_ass_from_cues(style="plain")` at the **safe-zone MarginV** (D4), persist a
  caption-free `base_video_path` (E5).
- Reuse `CaptionEditor` **as-is** — it already edits sentence cues and previews
  sentence geometry, so the word-mode overflow (D1) and preview-lies (D4 word geometry)
  problems **do not arise in v1**.
- Extend the reburn archetype guard `narrated`→`{narrated, subtitled}` and the
  eligibility gate + `/captions` routes (Fix 1).
- Render the clip **1:1** (no trim/speed) so cue times need no clip→assembled rebasing;
  lock with a test (Fix 2).
- Turkish: `language` param → whisper-1; swap local dev model `small.en`→`small`;
  **validate the caption font's Turkish glyph coverage on the libass path** (offline +
  registry pin), NOT via the Skia assert (Fix 3).
- Namespace the transcript cache by `item_id` (E-SEC).
- `subtitled` added to the `EditFormat` Literal + tuple; audit
  `NARRATED_EDIT_FORMATS`/voiceover branches in lockstep (Fix 5).
- Sentence-case captions (D3); review-first banner + "Reading what you said…" loading
  (D6); 44px targets + reduced-motion + libass glyph check (D8).

### v2 fast-follow — BUILT (word-by-word) + status

- **Lime active-word word-by-word pop** (D2 / E1) — BUILT. `captions.generate_word_pop_ass`
  emits one dialogue event per spoken word, full line visible, active word popped in the
  Nova lime accent via inline `\c` (no baseline jitter; NOT the one-big-word look nor the
  yellow karaoke sweep). Wired through the subtitled render + reburn; toggled by the
  item's caption style ("Word-by-word" vs "Sentence"). Guards:
  `tests/pipeline/test_captions.py::test_word_pop_*`.
- **Preserve-real-timings / synthesize-only-edited-cues** (E3 / Q2) — BUILT.
  `build_plain_cues(attach_words=True)` persists real per-word timings on each cue;
  `CaptionCue.words` round-trips them; `_word_windows_for_cue` reuses real times for an
  untouched cue and synthesizes (even split) only when the edited text no longer spells
  the stored words. Guard: `test_word_pop_synthesizes_for_an_edited_cue`.
- **Single-clip uploader cap** — BUILT (`PoolUploadCard maxClips={1}` for subtitled).
- **Subtitled cues in the Text-lane** — already works (the timeline conversion is
  `caption_cues`-driven, not archetype-gated).

### Post-land TODOs (from the /review pre-landing pass, user-approved deferrals)

- **P2 — Surface silent re-transcribe failures.** On whisper/gpt-4o failure or an
  empty new-language transcript, the task keeps existing captions and resets to
  `ready` with NO user-facing signal (spinner → unchanged captions). Persist a
  transient `last_edit_outcome` on the variant, return it in
  `_variants_for_response`, show a one-line toast; clear on next success.
- **P2 — Stuck-`rendering` sweeper.** A SIGKILL past a caption task's hard
  `time_limit` (660s) skips the except-reset and bricks the variant's editability
  (409 forever). Pre-existing class (reburn); now 3 tasks share it. Add a sweeper or
  startup reconciliation for stale `render_status="rendering"`.
- **P3 — DRY refactors.** Extract the shared transcribe→correct→resplit→burn helper
  (3 call sites), dedupe the caption-style picker into a component (narrated +
  subtitled near-verbatim copies), move the language/archetype sets into a shared
  constants module (lockstep is currently test-enforced only).
- **P3 — Editing while `rendering` is discarded on remount.** Debounced cue saves
  409 during a reburn and the remount re-seeds server cues, silently dropping
  keystrokes typed mid-render. Gate cue editing on `!rendering`.

### Still deferred

- **D5 language OVERRIDE** (change language → re-transcribe): the core Turkish case
  works today (render language flows from the plan/submission `language` en|tr into
  whisper-1). A per-clip language override for code-switching creators needs a
  re-transcribe render flow (new task/endpoint) — genuinely larger, edge-case; separate.
- **Live validation** (CI/render only — no local render env): run a real Turkish
  talking-head clip through the Docker render with `SUBTITLED_ARCHETYPE_ENABLED=true` and
  verify (a) whisper-1 Turkish transcription quality, (b) the burned caption legibility at
  the safe MarginV, (c) the lime word-pop tracks the audio, (d) Turkish glyphs (ı İ ş ğ ç)
  render without tofu. `make local-render MODE=generative` covers montage only — the
  subtitled archetype needs the flag + `edit_format=subtitled`, so validate on a staging
  deploy or extend `scripts/local-render.py` to pass those.

### v1 task list (supersedes the word-by-word tasks above)

- [ ] **V1 (P1)** — `subtitled` token + kill-switch-gated picker card (Fix 4, D7)
  — `edit_format.py`, `page.tsx`, `config.py`
- [ ] **V2 (P1)** — lean subtitled render: source audio + `build_plain_cues` + safe-zone
  burn + persist `base_video_path` (E2, E5, D4) — `generative_build.py`, `captions.py`
- [ ] **V3 (P1)** — `language` param → whisper-1 + local multilingual model (E4)
  — `transcribe.py`, `config.py`
- [ ] **V4 (P1)** — extend reburn guard + eligibility + routes to `subtitled` (Fix 1)
  — `generative_build.py`, `eligibility.ts`, `plan_items.py`
- [ ] **V5 (P1)** — libass Turkish glyph validation + registry pin (Fix 3, D3)
  — `assets/fonts/font-registry.json`, caption burn path
- [ ] **V6 (P2)** — namespace transcript cache by `item_id` (E-SEC)
- [ ] **V7 (P1)** — review-first banner + transcription loading beat (D6) — `CaptionEditor.tsx`/item page
- [ ] **V8 (P1)** — a11y: 44px targets + reduced-motion + preview outline (D8) — `CaptionEditor.tsx`
- [ ] **V9 (P1)** — tests: narrated renderer-parity regression, 1:1-render cue-time test,
  Turkish transcription eval (Fix 2, ET6)
- [ ] **V10 (P2)** — no-speech + wrong-language + failed states (D6, states)

The word-by-word tasks (ET1, ET2, T-word variants) move to the v2 fast-follow.

---

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | — |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | — |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | clean | 12 issues resolved, 0 critical gaps |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | clean | score 2/10 → 9/10, 8 decisions |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | — |

- **CROSS-MODEL:** An independent outside voice (Claude subagent; Codex not installed) challenged the plan and drove a v1 re-scope to sentence-block captions, 5 correctness/safety fixes, and the E3 timing correction. Both reviewers agree sentence blocks are the lower-risk v1; lime word-by-word is a gated v2 fast-follow.
- **VERDICT:** DESIGN + ENG CLEARED — ready to implement v1 (sentence-block subtitled style). 10 v1 tasks (V1-V10) queued; word-by-word deferred to a v2 fast-follow.

NO UNRESOLVED DECISIONS
