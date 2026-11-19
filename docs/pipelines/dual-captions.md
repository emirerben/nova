# Two-language captions (KRI-1501)

A phone edit can show two caption lines for the same stretch of speech: the **spoken line**
(today's caption, in the language the person speaks, with its word-by-word highlight) and a
smaller **translated line** in a second language. The translated line is its own editor
track: the creator moves, sizes, colours and edits it apart from the spoken line, but its
timing always follows the spoken line it translates.

Languages: `en`, `de`, `tr`, `pt` (`phone_rollout.DUAL_CAPTION_LANGUAGES`), so 12
spoken→translation pairs. Decision record: Emir's picks 1A (server translates), 2B
(separate track), 3A (chat ask and editor switch), 4A (native reviewers per language).

## Gate

`phone_rollout.dual_captions_planned_for(client_protocol)` is the only gate:
`DUAL_CAPTIONS_ENABLED` (default `false`) AND `dualCaptions` in
`PHONE_RENDER_VERIFIED_FEATURES` AND the app build's protocol ≥
`dual_captions_min_client_protocol` (= `client_protocol.DUAL_CAPTIONS_PROTOCOL`). Gate off:
no translated line is planned, compiled, offered or accepted, and captions are byte-identical
to before. A chat ask with the gate off gets an honest "not drawn yet" receipt.

## Data contract

### Caption cue (`variant["caption_cues"][i]`, iOS `EditorCaptionCue.raw`)

| key | type | meaning |
| --- | --- | --- |
| `translation` | `str`, optional | The translated line for this cue. Shown for exactly the cue's `[start_s, end_s]`. No per-word timing and no highlight. Absent or empty = no translated line for this cue. |

Every other cue key is unchanged. A cue's `translation` translates that cue's final `text`
(after correction, re-splitting and creator spellings), never a sentence chunk.

### Variant `caption_translation` (persisted) / editor `caption_meta.translation`

```json
{
  "enabled": true,
  "language": "en",
  "source_language": "tr",
  "size_px": null,
  "color": "#F5D33C",
  "font": null,
  "stroke_width": null,
  "position_x_frac": null,
  "position_y_frac": null,
  "line_overrides": {"<cue id>": {"position_x_frac": 0.5, "position_y_frac": 0.86, "size_px": 46}},
  "outcome": "translated",
  "dropped_cue_ids": []
}
```

- `enabled` false keeps the texts but draws nothing (the editor switch).
- `language` ∈ `DUAL_CAPTION_LANGUAGES` and ≠ `source_language`.
- `size_px` null ⇒ `round(0.6 × spoken caption size)`, clamped 28–120. Range when set: 24–120.
- `color` default `#F5D33C` (the reference look). `font` null ⇒ the spoken caption's font.
  `stroke_width` null ⇒ `max(2, round(0.6 × spoken stroke))`.
- `position_*_frac` null ⇒ **attached**: centred under the spoken line's lowest ink with a gap of
  `0.25 × small size`; when that would end below the frame budget or on a face, it goes above
  the spoken line instead. Set ⇒ **detached**: a block centre on the canvas, same clamps as
  the spoken captions (x 0.05–0.95, y 0.10–0.90).
- `line_overrides[cue_id]` wins over the track-wide values for that one line (same shape as
  the spoken captions' `appearance.line_overrides`).
- `outcome` / `dropped_cue_ids` are written by the server only (`translated` | `partial` |
  `unmet`); the editor sends them back unchanged or omits them.

### Layout rules (server compile and iOS preview must match)

- One row, never wraps. Width budget 0.9 × canvas width; when the text is wider it shrinks to
  fit, never below 24 px. Still too wide at 24 px ⇒ the line is dropped (never cut off) and the
  receipt says so.
- Plain fill + stroke, same entrance/exit as the spoken cue's non-karaoke pop-in; no karaoke.
- Layer ids `f"{id_prefix}-tr-{index}"`, one per ORIGINAL cue. When the spoken cue is split in
  time to clear the watermark, the translated line still spans the original cue's
  `[start_s, end_s]`.
- Text never covers a face (attached placement flips above; a detached position is the
  creator's own choice in the editor, same as moved spoken captions).

### Translation step (`caption_translate.translate_caption_lines`)

- Input: the final cue texts, their durations, `source_language`, `target_language`, glossary.
- One structured model call per video (`caption_translation_model` → `caption_correction_model`
  → `gpt-4o`), fixed line count, temperature 0, never raises.
- Each line carries a character budget `floor(cps × duration_s)` with cps 20 for `en`, 17 for
  `de`/`tr`/`pt` (Netflix Timed Text Style Guides; 42 chars/line). The prompt asks for a
  condensed subtitle, not a word-for-word translation.
- Every returned line is checked with `caption_language_verdict`; a `contradicted` line is
  dropped (its cue gets no `translation`), never shown in the wrong language.
- Anything fails ⇒ no translated lines, `outcome: "unmet"`, the spoken captions unchanged.

## How a creator turns it on

- **Chat:** `caption_language.caption_language_decision(text).translation_languages` — an
  ordered tuple of the languages named in a two-language ask ("Turkish captions with English
  under it", "add an English translation under the captions", "altyazının altına İngilizce
  çeviri", "zweisprachige Untertitel, Englisch darunter"). The first language that is not the
  spoken one becomes the translated line. The dispatcher persists it as
  `caption_translation_request` (list of codes); the worker resolves it once the spoken
  language is known. A two-language ask never translates the spoken line itself.
- **Editor:** `editor_capabilities.caption_translation = {"languages": [...]}` (only when the gate
  passes for the request's protocol and the variant has caption cues). The switch calls
  `POST /plan-items/{item_id}/variants/{variant_id}/caption-translation` with
  `{"language", "cues": [{"id", "text", "start_s", "end_s"}]}`; the answer
  `{"language", "source_language", "outcome", "translations": [{"id", "text" | null}],
  "dropped_cue_ids"}` is applied in the editor and persisted by the normal editor-commit
  Save (`caption_cues[*].translation` + `caption_meta.translation`). The route persists nothing.

## Checks

- Offline replay with real fixture transcripts (tr `kri632_t1_talking_words.json`, en
  `kri720_cigdem_take_a_words.json`, de `de1_null_zu_null.json`) and an authored stub translator;
  no live model calls in tests.
- iOS UI test renders all 12 pairs and writes PNGs to `$KRIA_SHOT_DIR` for a contact sheet.
- Owed before turning the flag on: real-iPhone render of the 12 pairs, a real Portuguese
  talking clip, native-speaker rating (9/10 natural per language).
