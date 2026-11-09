# Colour moments — failure modes (KRI-1350)

Written before the code. Each row names the test that pins it.

Answer key: `tests/fixtures/colour-moments/cigdem-reel-answer-key.json` (Çiğdem's 10 Kasım reel,
measured per frame). Checker: `scripts/verify-colour-moments.py`.

| # | How it could fail | What the creator would see | Pinned by |
|---|---|---|---|
| 1 | Edge timing drifts: the flash peak, the dip's darkest frame or the tone switch lands a frame or more off the requested time (rounding, PTS offset, frame-duration vs midpoint sampling) | The flash happens beside the word, not on it | Engine pixel test at exact frame times + `verify-colour-moments.py check` on an exported MP4 (±1 frame) |
| 2 | The sound locked to an edge drifts from the flash (SFX placed at the window start instead of the edge, or the sound's own lead-in) | Whoosh and flash feel separate | Recipe compiler test (sound `timelineStart` == edge time) + checker sound onset ±1 frame of the flash peak |
| 3 | A recipe without colour moments changes bytes or pixels (new key encoded when unset, post-process pass always on) | Every existing phone video changes | Swift encode test (no `colorMoments` key) + Python digest test + pixel identity for an untouched recipe |
| 4 | An older iPhone build gets a recipe with colour moments it cannot decode | Whole render fails ("couldn't prepare") | 426 `client_update_required` for protocol < 47; unknown-capability decode test |
| 5 | Flag off still lets the chat op write colour moments | Effect appears though the switch is off | Op gate test: flag off ⇒ honest decline, recipe unchanged |
| 6 | Kria adds colour moments nobody asked for (look words in the plan, "make it cinematic") | Unwanted black-and-white | Replay fixtures: a vague ask produces no op; only explicit tone/flash asks do |
| 7 | Too many moments or overlapping windows (two tones on one frame) | Flicker, double flashes | Validator: ≤ 6 moments, sorted, non-overlapping, inside the video, min length |
| 8 | The tone is applied per clip / under the speaker cut-out or the background, so the green-screen speaker stays in colour while the photo goes grey (or the reverse) | Half-grey frame | Post-process runs on the final composited frame; pixel test with a two-layer frame |
| 9 | Captions turn grey / flash with the frame | Unreadable or off-brand captions during a flash | Decision: captions stay as authored (post-process runs before text); pixel test on a caption pixel |
| 10 | Live preview differs from the export (effect only in export, or preview in another order) | "It looked different in the editor" | Preview and export share one `ColorMomentRenderer`; parity test renders both paths |
| 11 | Light-leak looks nothing like the reference (wrong colour, too long, no white core) | Cheap-looking flash | Shape test against the answer-key curve: core 4–8 frames, warm saturation burst, near-white peak |
| 12 | End fade cuts the audio abruptly or leaves picture at full brightness on the last frame | Video ends with a pop | Last frame luma ≤ 24 and audio ramps to silence over the same span |
| 13 | Swift/Python wire drift (camelCase keys, enum spellings, defaults) | Server accepts what the phone rejects | Shared parity fixture `tests/fixtures/colour-moments/parity-v1.json` decoded on both sides |
| 14 | Edges at the very start/end of the video or longer than the window clip outside the timeline | Crash or half flash | Validator clamps; engine treats out-of-range samples as no-op |
