# Background focus (Qendresa item 18): failure modes

Written 2026-11-05, before the code. Item 18 of `~/Downloads/kria-qendresa-test/BUILD_LIST.md`
(row B4 of the kit's Q2 report). Related: KRI-574 (screen-recording backgrounds), KRI-1169
(the tilt zoom slider this copies), KRI-1162 (timed backgrounds), item 16 (full-screen opening).

## What is wrong today

A Talk-to-camera background (a Visuals photo or a muted looping screen recording behind the
cut-out speaker) is always drawn cover-fit at focal 0.5/0.5, zoom 1
(`SpeakerBackground.swift` `backgroundMediaLayer` builds a bare `VisualMediaPlacement`). A
16:9 recording on the 9:16 canvas shows only its middle 31.6 % of the width. A README or docs
column almost always starts left of 0.342 of the screen width, so **every line is cut at its
start**: the frame is full of words (the kit's `screen` rule already passes) but no sentence can
be read. Her own Q2 videos (V1, V5, V6) zoom so the text column fills the width, lines read from
their first word, with black above and below.

Measured on synthetic recordings (scratch `readme/`, Apple Vision OCR as in the kit):

One still frame each, placed with the engine's geometry (prototype focus from the measure below):

| layout | text column (of width) | centre crop shows | words OCR'd centre / aimed | words readable from their line start, centre / aimed |
|---|---|---|---|---|
| GitHub 1x 1920x1080 | 0.18-0.60 | 0.342-0.658 | 249 / 349 | 0 / 301 |
| GitHub retina 3024x1964 | 0.09-0.67 | 0.318-0.683 | 182 / 313 | 0 / 266 |
| docs, text left | 0.16-0.53 | 0.342-0.658 | 159 / 339 | 2 / 298 |
| README left, terminal right | 0.04-0.47 | 0.342-0.658 | 263 / 354 | 0 / 311 |

So the done check counts **words readable from the start of their line** (an OCR line whose
first words are the first words of a README line, counted for as long as it keeps matching);
the plain word count is recorded too.

## The design (decided before the code)

- **Recipe**: optional `focus: {x, y, zoom}` on each `speaker_background.windows[i]` and on a
  photo/video fill (never a colour fill), omitted while unused. Same meaning as
  `VisualMediaPlacement`: base = cover, drawn scale = `max(cover * zoom, contain)` (never smaller
  than the whole picture), `x`/`y` align the picture (0 = left/top edge, 0.5 = centre, 1 =
  right/bottom). `zoom` 0.2-4 on the wire; below cover the picture no longer fills the frame and
  the uncovered part is black, her look. Capability `backgroundFocus`, client protocol 42, flag
  `BACKGROUND_FOCUS_ENABLED` (default off).
- **Automatic choice, free**: the worker already downloads every Visual before a phone render
  (`phone_visuals.bind_phone_visual_assets`, to hash and probe it). While the file is on disk it
  measures where the text sits: a few frames, grey, horizontal steps that glyph strokes make,
  summed per column; the densest contiguous span is the text column; the spacing of text-row
  starts inside it is the line pitch. Stored on `PhoneVisualBinding.text_column`. The focus is
  then: zoom so the column fills the width, but never so small that a text line is under ~40 px
  of the 1920 px canvas (her V5 measures 39-44 px), never above 2.5x; if the column is then wider
  than what shows, the view starts at the column's left edge (lines read from their start);
  otherwise it is centred on the column. No measurable text = no focus = today's centre crop.
- **Chat**: `set_background_focus {position: left|center|right, zoom?, asset_id?}` (Edit
  Copilot), and `background_media.entries[i].focus` / `zoom` on the plan (Main Creator) for a
  creator who names a side or a zoom. A creator's choice beats the measured one.
- **Editor**: the BACKGROUND lane window gets Left / Centre / Right and a zoom slider (the
  KRI-1169 pattern); Save validates and keeps it.

## Ways it could fail

- **F1 Old build draws a recipe it can't read.** A build below 42 or without the verified
  `backgroundFocus` token gets a `focus` key and rejects the whole recipe (strict decoders).
  Guard: the compile never writes focus unless the gate is open; old builds get today's centre
  crop and a fallback trace; the Save answers 426 below 42 only for a document that carries focus.
- **F2 Switch off is not byte-identical.** Measuring at bind time adds a key to stored job
  state, or the compile writes focus, or a prompt mentions it. Guard: nothing measured, stored,
  taught or written with the flag off; frozen recipe/prompt guard tests.
- **F3 Wrong column.** A sidebar, a terminal, a file tree or a photo out-scores the README; a
  dark-on-light page or light-on-dark page measures differently; syntax-highlighted code; a
  page with two equal columns. Guard: tests on light and dark pages, README + terminal, README
  + sidebar, a photo (no text => no focus), a blank recording.
- **F4 Text too small to read.** Fitting a wide column (or a whole 1x screen) makes the text
  tiny. Guard: the line-pitch floor (~40 px of 1920); pitch not measurable => fit no wider
  than half the screen width.
- **F5 Over-zoom.** A narrow column or a mis-measured pitch zooms 4x into blur. Guard: auto
  zoom capped at 2.5; pitch outliers ignored (median of run starts).
- **F6 Scrolling recording.** The column moves vertically every frame; one frame at a menu or a
  blank page decides wrongly. Guard: several frames spread over the first minute, profiles summed.
- **F7 Slow or failing measure.** A 500 MB recording, a corrupt file, ffmpeg missing, a time-out:
  the render must not fail or stall. Guard: bounded frames, a timeout, every error => None.
- **F8 Portrait or square visuals.** A phone screenshot or a portrait recording already fits;
  aiming it adds bands or zooms oddly. Guard: measured only when the picture is wider than the
  canvas by a margin; otherwise no focus.
- **F9 Rotated video.** A recording with a 90/270 rotation tag: measured on decoded (upright)
  frames but mapped with the wrong width/height. Guard: use the binding's display size; test a
  rotated file.
- **F10 Bands under the speaker.** Below cover the uncovered strip must be black, not the fill
  (another recording) and not transparent. The compositor drops the fill while a window is
  active and draws on an opaque canvas. Guard: simulator frame check (band pixels black).
- **F11 The fill.** A single recording behind the whole video is the fill, not a window: it must
  get the same focus path; a colour fill must reject focus.
- **F12 Lost on the way.** A per-entry field is dropped by the strategy repair (colour and media
  branches), `_strategy_background_request` (explicit keys), `_resolve_strategy_background`,
  `remap_lanes_for_cut` (a speech cut rebuilds windows), the editor document round trip,
  `_keep_full_screen`-style op rebuilds, or a sequence re-layout. Guard: an end-to-end test from
  the strategy and from a chat op to the compiled recipe, through a speech cut.
- **F13 Chat names a side the creator didn't mean.** "Zoom in" with no side; "left" meaning her
  corner (the speaker frame), not the recording. Guard: the op is about the recording only;
  `set_speaker_frame` keeps owning the corner; zoom without a side keeps the current side.
- **F14 Creator's choice overwritten.** A later re-render re-measures and replaces a focus she
  set in the editor or chat. Guard: an explicit lane focus always wins over the measure.
- **F15 Editor shows one thing, render another.** The preview path and the export path place the
  layer differently, or the slider range differs from the server's bounds. Guard: one placement
  function used by both; the slider uses the wire bounds; a Swift test on both paths.
- **F16 Receipts lie.** "Zoom into the README" answered "met" when nothing was aimed, or "can't
  verify" when it was. Guard: receipts unchanged unless the focus is in the recipe (not in scope
  to add a new brief fact; the receipt keeps saying it can't verify).
- **F17 Captions and cards read the old geometry.** Text placement that avoids "screen text" or
  faces reads the full canvas, not the aimed recording. Not changed here (placement doesn't read
  the background picture today).
- **F18 Caption colour judged against the un-aimed picture.** Since #2099 (Green 5) the automatic
  caption colour is chosen against the background picture, sampled as the plain cover fit; an
  aimed recording shows another part of itself (a zoomed white page where the cover fit shows a
  dark player), so white text could land on white. Guard: the compile hands its own focus
  resolver to `register_generation_background` (one gate question for both), each picture is
  sampled the way the phone places it (`sample_background(focus=...)`: cover x zoom, never below
  contain, black bands), and a closed gate records exactly #2099's spec.
  `tests/pipeline/test_background_focus_caption_contrast.py` (failure modes listed before the
  fix).
  **Limit (predates F18, not universal):** `register_generation_background` returns at once for a
  colour fill (`kind="color"`, #2099's scope), so on a colour fill nothing is sampled, aimed
  windows included: those captions keep the take-based colour. Aimed-background caption contrast
  covers photo/video fills and their windows only; say so before rollout.
