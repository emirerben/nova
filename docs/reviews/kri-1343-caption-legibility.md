# KRI-1343 / KRI-1344 — captions readable on any background

Parent: KRI-1342 (yorum3, 2026-11-09, job 7eba9903, thread d9d7ad3d, Fly 4f6ed3014).

## Original request and what went wrong

The creator asked for "captions small, white, lowercase, in the middle". The phone drew 42 px
white captions with `caption_stroke_width: 0` and `caption_shadow_enabled: false` at y 0.62, on
a cream sweater (unreadable, about 1.3-1.5:1). During the full-screen post screenshots
(IMG_7102/7103) the caption sat inside the post's own headline.

## Acceptance (written before code)

- Every caption line reaches WCAG 4.5:1 against what is behind it while it is up: the take, a
  full-screen photo/video card, B-roll. Size and colour stay the creator's; "small, white" is
  not a request to drop legibility. An explicit "no outline" keeps hers.
- While a full-screen picture is up, no caption line intersects the picture's own text: it
  moves to a clear band, or hides there when the picture is text-dense. A moved line never
  covers her face over the take.
- Switch off: byte-identical.
- Still wrong: a calm photo moving lines (wrong-content control), a line moved onto her face,
  a creator colour repainted.

## Root cause

`schemas/phone_text_style.py` defaults captions to no outline / no shadow and relies on
`_auto_caption_colour`; an asked colour skipped it (`"color" in rule`), the automatic pass only
re-picks the fill (outline only over a speaker background), and full-screen cards were never in
the sampled frames (`_background_frames` = take only).

## Change (behind `CAPTION_LEGIBILITY_ENABLED`, default off)

See `docs/pipelines/smart-captions.md` "Caption legibility on any background" and
`src/apps/api/app/pipeline/caption_legibility.py`.

## Evidence (offline, free; no model calls)

- Guard: `src/apps/api/tests/pipeline/test_caption_legibility.py` — real
  `compile_phone_subtitled_plan`, real environment sampler and legibility pass; only downloads
  faked with drawn media; judge reads the compiled text layers. Red controls (scratch, not
  committed): forcing `place_line` to keep, and forcing treatment `none`, both fail it.
- Corpus (`corpus_harness.py.txt`, `summarize.py.txt` in `kri-1343/`): every test-kit run
  folder with a video (56 runs, 9 kits) x the 5 caption asks seen in 51 prod jobs (none,
  yorum3's, words:2, small white, grey box), EN or TR cue text, switch off vs on, real face
  sampler. Judge: fill vs p10/p90 of the pixels under the drawn block on the real frame (or the
  fill vs its own outline), and intersection with Apple Vision OCR boxes of each photo
  (`photo-ocr-boxes.jsonl`, made by `ocr_boxes.swift.txt`, local and free).

| | Before | After |
|---|---|---|
| Caption lines at >= 4.5:1 | 54.3% (1,429 / 2,632) | **100%** (2,655 / 2,655) |
| Videos with every line readable | 86 / 280 | **280 / 280** |
| Lines intersecting photo text | 41 | **5** |
| Videos with no photo-text hit | 263 / 280 | **276 / 280** |

Full numbers: `kri-1343/corpus-report.json` (tested at the commit that adds this file).

Residual (5 lines, 4 videos, 3 photos): no band of the picture is clear (large product
lettering, a restaurant sign, a marathon finish banner); the line takes the least busy band,
outlined. Hiding there would also hide captions on ~13% of busy natural photos without text.
Follow-up candidate: a backing box for those lines (needs `captionShape`, protocol 36).

## Case matrix

- EN and TR cue text (350 TR / 210 EN compiles); sentence and 2-word captions; caption box ask;
  no ask (automatic colour); asked white; explicit "no outline" (guard test).
- Formats: phone Talking (subtitled) recipe with full-screen cards; montage / narrated kit
  footage used as the take. Cloud: no code change — it reads the same persisted
  `caption_stroke_width` / `caption_editor_style` fields and composites full-screen cards above
  captions.
- Excluded: native device export (no device run), production (switch off).

## Known gaps

- Native phone export not inspected (evidence level: offline replay).
- A hidden line is cut only in the compiled recipe; the iPhone editor redraws the persisted cue
  after an edit (needs a per-cue hide field + protocol gate).
- Five older flag-off tests pin "white with no outline" and must be updated when the switch is
  turned on (`test_green45_caption_contrast.py` x3, `test_caption_corner_face.py` x1,
  `test_speaker_full_opening_caption_contrast.py` x1).
