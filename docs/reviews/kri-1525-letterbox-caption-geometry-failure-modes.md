# Talking captions on a letterboxed take — failure modes (written before the fix)

KRI-1525 (captions black on the black bar), and cause 1 of the caption-position investigation on
KRI-1407 (captions 34–38 px in a top strip). Cusco DJI kit runs T1 "Good Take" (job `d988bb99`) and
T2 "list 1" (job `887f89d6`): sideways 1920×1080 takes on phone Talking, `landscape_fit: "fit"`.

## The bug

Phone Talking draws a sideways take letterboxed: the engine cover-fills it, then scales it by
`fit_transform(...).scale` (0.3164 for 1920×1080 into 1080×1920) about the canvas centre over black
(`phone_recipe_shared.fit_transform`). Caption styling samples faces and background frames once per
generation (`services/phone_text_styling._ensure_generation_environment`) and maps them through the
main-track windows registered by `register_generation_timeline`.

On the Talking path the caption look is first chosen **before** the plan is compiled: the KRI-632 face
band (`generative_build._phone_face_band_caption_look`) and the KRI-827 protected plan both call
`phone_protected_text.choose_caption_look`, which calls `generation_caption_look`, which samples the
environment. No window is registered yet, so every take is sampled with the default window (scale 1:
cover-fill). `register_generation_timeline` in the later compile returns early (`_environment_sampled`),
and the look chosen early is the one passed to the compile and drawn.

Measured on both jobs:

- Face boxes recorded in `text_fit_relaxed.obstacles` span the full width and 17–87 % of the height
  (T2 `[0.0, 0.165, 1.0, 0.695]`), while the picture only fills y 0.342–0.658. The fitter finds no clear
  spot, drops to the relaxed rung and leaves a 34 px (T1) / 38 px (T2) caption block in the top strip.
- `styling_environment_sample.frames` is 5 (T2) and 10 (T1): background frames were sampled
  cover-filled (a scaled window is never sampled), so T1's bright church wall chose black text, drawn on
  the black bar (`caption_text_color: "#000000"`).
- A dry replay of the live placement rule with the faces mapped where the take is drawn puts T2 at
  y 0.661, 54 px and T1 at 76 px.

## Acceptance (what the creator sees)

- A sideways take shown with black bars: captions at a readable size beside the picture, not squeezed
  into the top strip, and never on the face.
- Captions on the black bar are light (white with its outline), never black.
- Everything else is drawn exactly as today: a vertical take, a sideways take that fills the screen,
  a landscape canvas, a face-filled take.
- Still wrong after this fix (named, out of scope): captions are left-anchored by default
  (`PLACEMENT_CASCADE` starts at `bottom_left`), and a face-filled take (`speaker_position_x`) is still
  sampled centre-cropped before compile. Both stay on KRI-1407.

## Ways the fix could fail

1. **Order of calls.** The geometry must be known before the first `_ensure_generation_environment`,
   i.e. before `_phone_protected_plan` and `_phone_face_band_caption_look`. Registering it in the
   compile is too late (the bug).
2. **Vertical take.** `fit_transform` is the identity for a portrait source: must stay byte-identical.
3. **Sideways take that fills** (`landscape_fit: "fill"`, the creator asked for no bars): identity, unchanged.
4. **Landscape canvas** (creator picked a landscape video): the job forces `"fill"`; unchanged.
5. **Face-filled take** (`speaker_position_x` set): `framed_fit` is `"fill"` there, so the default stays
   scale 1. Its sideways shift is not modelled here (named gap); it must not regress.
6. **Several takes** (T1 has two): each source gets its own scale from its own display size, as the
   compiler does for extra speakers (`extra_transform`).
7. **Rotated source** (`orientation_degrees` 90/270): decide landscape from the displayed size, as the
   environment already does for the face mapping.
8. **Windows registered first** (no early caller, e.g. no face found by the face band): the compile's own
   windows are used, as today. Their faces were already right; only the background changes (item 9).
9. **Background of a scaled take.** Today a window with scale ≠ 1 is not sampled at all. A centred,
   unrotated take scaled below 1 is drawn over black, so sample it fitted with black pad
   (`sample_background(focus={"x": .5, "y": .5, "zoom": scale})`). A take with a backdrop (blurred or
   picture fill behind it), a rotation or a shift is not over black: keep skipping it.
10. **Kill switch.** The fix moves captions on an existing kind of edit. Area convention: a switch,
    default true (`TALKING_FIT_CAPTION_GEOMETRY_ENABLED`); false + worker restart = today, byte-identical.
11. **Registration after sampling** (another caller sampled first): no effect, no crash.
12. **Asked spot / asked colour** (KRI-1177): unchanged; the fix only changes what the default placement
    and the automatic colour see.
13. **A face near the side of a letterboxed take** (found while writing the tests, before the code). The
    environment maps a face with the cover crop (`face_box_to_canvas(crop=None)`) and only then applies
    the window scale. A letterboxed take shows its whole width, so a face outside the cover crop's middle
    third is trimmed to it or dropped, and captions could sit on it. A window scaled below 1 must map
    its faces from the whole cover-filled layer, then scale. Scale 1 stays on the crop (byte-identical).

## Checks

- Failure-mode tests in `tests/services/test_phone_text_fit_geometry.py` (items 1–13), including the
  switch-off control that reproduces the top-strip position.
- Offline replay of the T1 and T2 caption step on the kit's real takes (2026-11-17, this branch): the
  real phone Talking worker, the real face sampler (OpenCV Haar, `face_sampler_worker`) and the real
  background sampler (ffmpeg) on 568×320 proxies of the takes (the iOS analysis-proxy size), with the
  recorded caption cues as the transcript. Only the proxy download and the transcription were replayed.
  The switch-off rows reproduce production (T2 exactly; T1's size and black ink — the replay is the
  good take alone, prod also played the false start, hence a different position).

| Run | Switch | Caption size | Position (x, y) | Anchor | Ink | Face boxes sampled (first) |
|---|---|---|---|---|---|---|
| T2 "list 1" | off | 38 px | 0.114, 0.147 | left | white | (0.0, 0.158, 1.0, 0.695) |
| T2 "list 1" | **on** | **64 px** | 0.108, **0.654** | left | white | (0.305, 0.392, 0.670, 0.562) |
| T1 "Good Take" | off | 34 px | 0.500, 0.312 | centre | **black** | (0.007, 0.361, 1.0, 0.928) |
| T1 "Good Take" | **on** | **40 px** | 0.108, **0.687** | left | **white** | (0.344, 0.456, 0.727, 0.635) |
| Production T2 (job `887f89d6`) | — | 38 px | 0.113, 0.147 | left | white | (0.0, 0.165, 1.0, 0.695) |
| Production T1 (job `d988bb99`) | — | 34 px | 0.113, 0.131 | left | black | (0.0, 0.165, 1.0, 0.828) |

The picture of a letterboxed 16:9 take spans y 0.342–0.658; with the switch on every sampled face is
inside it, and the captions sit under it on the bar in light ink.

Not covered by this fix (still open on KRI-1407): the left anchor, the face-filled take's shift, and
T4's faces counted on scenery clips (KRI-1517).

## Follow-up: a tilted take (review on #2379, written before the code)

Tadic's review: on a tilted take the environment still protects **the whole canvas** for every face it
samples (`window["rotation"]` → box `(0, 0, 1, 1)`), even a face the tilt leaves outside the picture.
One invisible face then makes the fitter treat the frame as full of face: the captions drop to the
relaxed rung (small, in the top strip) and the title has to dodge them.

How the phone draws a tilted clip (`KriaMediaEngine` `Composition.transform`): cover-fill the whole
take about the canvas centre, scale by `transform.scale` (letterbox and/or tilt zoom), rotate by
`rotation_degrees` **clockwise on screen**, then shift by `position_x/position_y`. The whole layer is
turned, not just the cover crop, so a tilt can bring a sliver past the crop into view.

Today no first-draft writer passes a tilt (the editor's Save keeps tilts but does not re-sample), so
this is a latent path; it is fixed now because every montage/Talking writer already registers its
windows (`register_generation_timeline`) with the rotation, and a tilt asked in a first draft would hit it.

14. **Face outside the picture after the tilt** (e.g. the side of a sideways take that is cover-filled):
    must be dropped, not protected as the whole canvas.
15. **Face inside the picture**: protected where the tilt draws it, i.e. the box around its four corners
    turned about the canvas centre, in pixels (the canvas is not square), then clipped to the canvas.
    Neither the whole canvas nor the untilted box.
16. **Face partly outside**: only the visible part is kept (clipped to the canvas).
17. **Direction**: clockwise on screen. A sign error mirrors the face to the other side; a quarter turn
    of an off-centre face has a known answer that tells the two apart.
18. **Tilt with a letterbox or a zoom, and a shifted tilt**: scale before the turn, shift after, as the
    engine composes them.
19. **Captions and title**: with an off-picture face on a tilted take, the captions and the title are
    drawn exactly as with no face at all: captions at a readable size (≥ 48 px) and clear of the title.
20. **Switch off**: a tilted window keeps today's whole-canvas protection, byte-identical.
21. **Background of a tilted take**: still not sampled (unchanged; item 9).

Found while writing the tests: the off-picture case was **introduced by the first version of this PR**.
On main the cover crop drops an off-crop face before the tilt is looked at; this PR mapped every face
from the whole take first, so the off-crop face reached the whole-canvas branch. The switch-off control
(`test_the_switch_off_keeps_todays_tilted_take_byte_identical`) pins main's behaviour.

Checks (2026-11-18): 8 new tests in `tests/services/test_phone_text_fit_geometry.py` (items 14-19)
fail on the previous head and pass with the fix; the switch-off control passes on both. The
caption/title test runs the real environment, caption placement (`generation_caption_look`) and
title fit (`generation_style_rows`) on a cover-filled sideways take tilted 8°.

Still not covered (named): a face that genuinely fills the frame (the cover-filled T2 close-up spans
the full width, y 0.10–0.77) leaves no clear spot, tilted or not, and the fitter then draws the
captions and the title over it. Same on main for an untilted take; out of scope here.
