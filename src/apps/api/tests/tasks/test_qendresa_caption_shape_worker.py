"""Qendresa item 12: her caption SHAPE through the real phone Talking writer.

Qendresa (@qbuilder) shows 1-2 caption words at a time (kit `_src/measure/ocr`, answer_key
`caption_max_words: 3` on >= 85 % of caption frames):

- Q1 (talking head + cards, V4/V7): white, the spoken word bright and the other one dimmed,
  no lime, at 61-62 % of the height (band 0.50-0.75).
- Q2 (screen walkthrough, her cut-out bottom right, V1/V5/V6): black bold text in a light-grey
  rounded box at 76-87 % of the height (band 0.65-0.92).

On origin/main 37f470c7f the phone Talking writer builds 8-word / 40-char cues
(`captions.PHONE_CUE_BUDGET`), sweeps them in lime #84CC16, draws no caption box (no field for
one), caps a bottom caption at y 0.70 (`phone_text_styling.generation_caption_look`,
`phone_text_style._candidates`), and the receipt answers "met" without judging words or box.

The brief records her shape as typed facts (`caption_words`, `caption_box`; Main Creator
v120), exactly the rows below; no model runs here. The real worker (`_run_generative_job`)
renders the Cafe T2 harness take: full frame for Q1, the corner speaker (`small_bottom_right`)
for Q2. No provider, network or DB (harness `_offline`); fonts are not model-selected.

Ways this can go wrong, and the test that catches each (F-numbers: the item-12 failure list):
- F6/F7 cues still > 2 words -> `_words_per_layer` on every caption layer.
- F10 lime still drawn -> every fill / highlight compared with the asked white.
- F11 dim word invisible or not dimmed -> run fill must be a mid grey, highlight white,
  `active_only` (only the spoken word lights up).
- F12 box missing / not rounded / per-word karaoke over a box (the device paints the box once
  per word, hiding earlier words) -> background on every layer, radius, no karaoke layer.
- F13/F14 box text not black, or outlined -> run fill black, stroke 0.
- F15/F16/F17 position: Q1 in 0.55-0.70, Q2 box centre in 0.75-0.87 and clear of her corner face.
- F19 flag off -> byte-identical text layers and caption fields to the same job without the
  new facts. F20 old build / token off / no protocol stamp -> today's captions.
- F22 an editor Save recompiles from the variant -> same shape.
- F24/F25 receipt honest both ways.
- B1-B5 (review on 2080f66) the box text always reads on the asked box: see the last section.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import build_receipts, plan_facts_from_phone_variant
from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS
from app.pipeline.phone_captions import caption_look_from_variant, compile_caption_layers
from app.services import phone_text_styling as styling
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests.tasks.test_green3_caption_look_worker import R1, R4, SEQUENCE
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)
from tests.tasks.test_speaker_background_strategy_worker import _job, _windows_job

CANVAS_W, CANVAS_H = 1080.0, 1920.0

Q1_PROMPT = (
    "Cut out all my pauses and mistakes so it's tight. Captions word by word in the middle of the "
    'screen, white, two words at a time. Start with a big title "lets build an agent that reads '
    'an APP before I trust it!!" for the first 3 seconds. When I talk about something, show the '
    "matching screenshot or screen recording in the top or bottom part of the screen, not over "
    'my face. When I list the four things, show each one as big text, like "1. which company it '
    'sends data to?". End with "comment READ" and an arrow for the last 3 seconds. Only my '
    "voice, no music."
)
Q2_PROMPT = (
    "Start with me full screen and the title \"5 PROJECTS / the AI builder community / can't stop "
    'talking about!!!!" with the GitHub logo above it. After that, put my screen recordings full '
    "screen and me cut out (no background) small in the bottom right corner while I talk. Zoom "
    "into the recordings so the text is readable, and show the right one when I talk about each "
    "repo. "
    'Add a label when each one starts: "1. Spec-kit", "2. Impeccable", "3. TradingAgents", '
    '"4. Agents", "5. Claude code game studio". Captions word by word in a light grey box near the '
    'bottom. Cut all my pauses. End with "comment REPO" and an arrow. Only my voice.'
)

# The rows the brief writer records for her caption sentences (Main Creator v120 teaching).
Q1_CAPTIONS = BriefRequirement(
    id="q1cap",
    kind="style",
    scope="global",
    description="Captions word by word in the middle of the screen, white, two words at a time",
    facts={
        "captions": "word_by_word",
        "caption_position": "middle",
        "caption_color": "#FFFFFF",
        "caption_words": 2,
    },
)
Q2_CAPTIONS = BriefRequirement(
    id="q2cap",
    kind="style",
    scope="global",
    description="Captions word by word in a light grey box near the bottom",
    facts={"captions": "word_by_word", "caption_box": "#E5E5E5", "caption_position": "bottom"},
)
LIGHT_GREY = 0xE5 / 255
LIME = {(0x84, 0xCC, 0x16), (0xC5, 0xF8, 0x2A)}

# Where her face is DRAWN with the corner preset: a typical upright face box (0.33-0.67 x
# 0.22-0.40 of the take) mapped through `small_bottom_right` (kit test_q2_caption_over_corner_face).
_F = SPEAKER_FRAME_PRESETS["small_bottom_right"]
CORNER_FACE = (
    _F["x_fraction"] + (0.33 - 0.5) * _F["width_fraction"],
    _F["y_fraction"] + (0.22 - 0.5) * _F["width_fraction"],
    _F["x_fraction"] + (0.67 - 0.5) * _F["width_fraction"],
    _F["y_fraction"] + (0.40 - 0.5) * _F["width_fraction"],
)


def _shape_job(
    monkeypatch,
    requirements,
    *,
    request: str,
    corner: bool,
    flag: bool = True,
    shape_verified: bool = True,
    protocol: int | None = 36,
):
    """Her job on the real writer: brief pinned, text style stamped as dispatch stamps it."""
    monkeypatch.setattr(settings, "phone_caption_shape_enabled", flag)
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)

    def environment(canvas):  # no proxy on disk: no faces / contrast frames sampled
        styling._POLICY.get()["_face_boxes"] = []

    monkeypatch.setattr(styling, "_ensure_generation_environment", environment)
    if corner:
        job = _windows_job(monkeypatch, SEQUENCE, cadence_s=4.0, speaker_frame="small_bottom_right")
    else:
        job = _job(monkeypatch, background=None)
    features = [f for f in settings.phone_render_verified_features if f != "captionShape"]
    assert "authoredText" in features
    if shape_verified:
        features.append("captionShape")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    job.all_candidates["creator_request"] = request
    # "word by word" approved as caption_style karaoke -> "word" (`kria/runtime.py:1916-1923`).
    job.all_candidates["voiceover_caption_style"] = "word"
    if protocol is not None:
        job.all_candidates["creator_client_protocol"] = protocol
    brief = CreativeBrief(version=1, requirements=list(requirements))
    job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
        uuid.uuid4(), brief
    ).model_dump(mode="json")
    stamp_phone_text_style(job)
    job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
    return job


def _caption_layers(job):
    return [layer for layer in _recipe(job).text_layers if layer.id.startswith("caption-")]


def _words_per_layer(layer) -> int:
    if layer.karaoke is not None:
        return len(layer.runs)  # one run per word
    return len(" ".join(run.text for run in layer.runs).split())


def _rgb255(ink) -> tuple[int, int, int]:
    return (round(ink.red * 255), round(ink.green * 255), round(ink.blue * 255))


def _overlaps(a, b) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _run(job):
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.status
    return job.assembly_plan["variants"][0]


def _receipt(req, variant):
    (receipt,) = build_receipts(
        [req], plan_facts_from_phone_variant(variant), include_unchecked=True
    )
    return receipt


# ------------------------------------------------------------------------------------ Q1


def test_q1_two_white_words_the_spoken_one_bright_the_other_dimmed(monkeypatch):
    job = _shape_job(monkeypatch, [Q1_CAPTIONS, R4], request=Q1_PROMPT, corner=False)
    variant = _run(job)
    layers = _caption_layers(job)
    assert len(layers) > 10

    # F6/F7: never more than the two words she asked for.
    assert max(_words_per_layer(layer) for layer in layers) <= 2
    # Her talk is not one-word mush: most captions hold two words.
    assert sum(_words_per_layer(layer) == 2 for layer in layers) >= len(layers) // 2
    for layer in layers:
        assert layer.background is None  # Q1: no box
        assert layer.effect == "karaoke-line", layer.effect
        karaoke = layer.karaoke
        # F11: only the spoken word lights up, in her white; the other word is a mid grey.
        assert karaoke.active_only is True
        assert _rgb255(karaoke.highlight) == (255, 255, 255)
        for run in layer.runs:
            r, g, b = _rgb255(run.fill)
            assert r == g == b and 110 <= r <= 200, (run.text, (r, g, b))
            assert (r, g, b) not in LIME  # F10
        # F17: her spot, under the face (V4 0.62 / V7 0.61); never above the middle.
        assert 0.55 <= layer.anchor_y / CANVAS_H <= 0.70, layer.anchor_y / CANVAS_H

    assert variant["caption_shape"] == {"version": 1, "words": 2, "dim": True}
    assert all(len(cue["text"].split()) <= 2 for cue in variant["caption_cues"])
    # Met. A met look combined with the met word-by-word part carries no reason (KRI-558
    # `combine_receipts` keeps reasons of unmet parts only); the gate tests below pin the text.
    assert _receipt(Q1_CAPTIONS, variant).status == "met"


# ------------------------------------------------------------------------------------ Q2


def test_q2_black_words_in_a_light_grey_rounded_box_low_and_off_her_face(monkeypatch):
    job = _shape_job(monkeypatch, [R1, Q2_CAPTIONS, R4], request=Q2_PROMPT, corner=True)
    variant = _run(job)
    layers = _caption_layers(job)
    assert len(layers) > 10

    assert max(_words_per_layer(layer) for layer in layers) <= 2
    for layer in layers:
        # F12: her box on every caption, whole words in it (no karaoke sweep under a box).
        assert layer.effect != "karaoke-line"
        box = layer.background
        assert box is not None, layer.id
        assert (box.color.red, box.color.green, box.color.blue) == pytest.approx(
            (LIGHT_GREY,) * 3, abs=0.01
        )
        assert box.color.alpha == 1
        assert 16 <= box.radius <= box.height / 2, (box.radius, box.height)
        # The box hugs its words with air around them.
        for run in layer.runs:
            assert _rgb255(run.fill) == (0, 0, 0)  # F13: black text
            assert run.stroke_width == 0  # F14: no outline on the box
            assert box.left < run.x
        # F15/F16: low like hers (V5 0.76, V6 0.77, V1 0.87), off her drawn corner face.
        centre_y = (box.top + box.height / 2) / CANVAS_H
        assert 0.75 <= centre_y <= 0.87, centre_y
        rect = (
            box.left / CANVAS_W,
            box.top / CANVAS_H,
            (box.left + box.width) / CANVAS_W,
            (box.top + box.height) / CANVAS_H,
        )
        assert not _overlaps(rect, CORNER_FACE), (layer.id, rect, CORNER_FACE)

    assert variant["caption_shape"] == {"version": 1, "words": 2, "box": "#E5E5E5"}
    assert variant["caption_text_color"] == "#000000"
    assert _receipt(Q2_CAPTIONS, variant).status == "met"


def test_an_editor_save_recompiles_the_same_shape(monkeypatch):
    """F22: Save / chat edits recompile from the variant (`caption_look_from_variant`)."""
    job = _shape_job(monkeypatch, [R1, Q2_CAPTIONS, R4], request=Q2_PROMPT, corner=True)
    variant = _run(job)
    drawn = _caption_layers(job)
    look = caption_look_from_variant(variant)
    saved = compile_caption_layers(
        variant["caption_cues"],
        canvas_width=int(CANVAS_W),
        canvas_height=int(CANVAS_H),
        style=variant["voiceover_caption_style"],
        look=look,
    )
    assert len(saved) == len(drawn)
    for before, after in zip(drawn, saved, strict=True):
        assert after.background is not None
        assert after.background.color == before.background.color
        assert after.background.radius == pytest.approx(before.background.radius)
        assert [_rgb255(run.fill) for run in after.runs] == [_rgb255(r.fill) for r in before.runs]
        assert after.anchor_y == pytest.approx(before.anchor_y, abs=1.0)


# ------------------------------------------------------------ gates: today's captions


def _today(layers) -> None:
    """Today's phone Talking captions: some cue holds more than 2 words, no box anywhere."""
    assert layers
    assert all(layer.background is None for layer in layers)
    assert max(_words_per_layer(layer) for layer in layers) > 2


@pytest.mark.parametrize(
    "gate",
    [
        {"flag": False},
        {"shape_verified": False},
        {"protocol": 35},
        {"protocol": None},
    ],
    ids=["flag_off", "token_off", "old_build", "no_protocol_stamp"],
)
def test_without_every_gate_the_captions_are_todays_and_the_review_says_so(monkeypatch, gate):
    job = _shape_job(monkeypatch, [R1, Q2_CAPTIONS, R4], request=Q2_PROMPT, corner=True, **gate)
    variant = _run(job)
    _today(_caption_layers(job))
    assert "caption_shape" not in variant
    ask = job.assembly_plan[PHONE_TEXT_STYLE_FIELD].get("caption_ask") or {}
    assert "words" not in ask and "box" not in ask
    # F24: the shape she asked for was not drawn, and the review says so.
    receipt = _receipt(Q2_CAPTIONS, variant)
    assert receipt.status != "met", receipt
    assert "box" in receipt.reason.lower(), receipt.reason


def _caption_state(job) -> dict:
    variant = job.assembly_plan["variants"][0]
    keys = [k for k in variant if k.startswith("caption") or k == "voiceover_caption_font"]
    return {
        "layers": [layer.model_dump(mode="json") for layer in _caption_layers(job)],
        "variant": {k: variant[k] for k in keys if k != "caption_look_receipt"},
        "receipt": variant.get("caption_look_receipt"),
        "stamp": job.assembly_plan[PHONE_TEXT_STYLE_FIELD],
    }


@pytest.mark.parametrize("corner", [False, True], ids=["q1_full_frame", "q2_corner"])
def test_flag_off_new_facts_change_nothing(monkeypatch, corner):
    """F19: with the switch off, a brief carrying the new facts renders byte-identically to the
    same brief without them."""
    asked = Q2_CAPTIONS if corner else Q1_CAPTIONS
    plain = asked.model_copy(
        update={
            "facts": {
                k: v for k, v in asked.facts.items() if k not in ("caption_words", "caption_box")
            }
        }
    )
    prompt = Q2_PROMPT if corner else Q1_PROMPT
    base = [R1] if corner else []
    with_facts = _shape_job(
        monkeypatch, [*base, asked, R4], request=prompt, corner=corner, flag=False
    )
    _run(with_facts)
    without = _shape_job(monkeypatch, [*base, plain, R4], request=prompt, corner=corner, flag=False)
    _run(without)
    assert _caption_state(with_facts) == _caption_state(without)


# ------------------------------------------------------------------------- draft review


def _draft_facts() -> bc.PlanFacts:
    import dataclasses

    strategy = {
        "edit_format": "subtitled",
        "caption_style": "karaoke",
        "audio_strategy": "original_audio",
    }
    return dataclasses.replace(
        bc.plan_facts_from_strategy(strategy, clip_ids=["take"], speech_cleanup_offered=True),
        phone_text=True,
        phone_talking=True,
        caption_words_at_render=True,
    )


@pytest.mark.parametrize("asked", [Q1_CAPTIONS, Q2_CAPTIONS], ids=["two_words", "box"])
def test_the_draft_review_never_promises_a_shape_that_is_switched_off(monkeypatch, asked):
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "phone_caption_shape_enabled", False)
    receipt = bc.check_requirement(asked, _draft_facts())
    assert receipt.status != "met", receipt


@pytest.mark.parametrize("asked", [Q1_CAPTIONS, Q2_CAPTIONS], ids=["two_words", "box"])
def test_the_draft_review_says_done_when_the_shape_is_on(monkeypatch, asked):
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "phone_caption_shape_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "authoredText", "captionShape"],
    )
    receipt = bc.check_requirement(asked, _draft_facts())
    assert receipt.status == "met", receipt


# ------------------------------------------------------------------- faces (team rule)

# A low full-frame face, where the box's default spot (0.79) would land on it.
LOW_FACE = {"start_s": 0.0, "end_s": 60.0, "box": (0.30, 0.70, 0.70, 0.88), "kind": "face"}
HIGH_FACE = {"start_s": 0.0, "end_s": 60.0, "box": (0.35, 0.30, 0.65, 0.50), "kind": "face"}
BOX_ONLY = BriefRequirement(
    id="boxonly",
    kind="style",
    scope="global",
    description="Captions in a light grey box",
    facts={"captions": "on", "caption_box": "#E5E5E5"},
)
BOX_BOTTOM = BriefRequirement(
    id="boxbottom",
    kind="style",
    scope="global",
    description="Captions in a light grey box at the bottom",
    facts={"captions": "on", "caption_box": "#E5E5E5", "caption_position": "bottom"},
)


def _with_faces(monkeypatch, faces):
    def environment(canvas):
        styling._POLICY.get()["_face_boxes"] = [dict(face) for face in faces]

    monkeypatch.setattr(styling, "_ensure_generation_environment", environment)


def _box_rects(job):
    return [
        (
            layer.background.left / CANVAS_W,
            layer.background.top / CANVAS_H,
            (layer.background.left + layer.background.width) / CANVAS_W,
            (layer.background.top + layer.background.height) / CANVAS_H,
        )
        for layer in _caption_layers(job)
    ]


def test_a_box_with_no_spot_never_lands_on_a_face(monkeypatch):
    """No spot asked: the box keeps off the face (team rule) instead of the fixed low spot."""
    job = _shape_job(monkeypatch, [BOX_ONLY], request="Captions in a light grey box.", corner=False)
    _with_faces(monkeypatch, [LOW_FACE])
    variant = _run(job)
    rects = _box_rects(job)
    assert rects
    assert not any(_overlaps(rect, LOW_FACE["box"]) for rect in rects), rects
    assert variant["caption_look_receipt"]["over_face"] is False


@pytest.mark.parametrize(
    ("face", "covered"), [(LOW_FACE, True), (HIGH_FACE, False)], ids=["low_face", "high_face"]
)
def test_an_asked_low_box_says_truthfully_whether_it_covers_a_face(monkeypatch, face, covered):
    """The creator's spot wins (KRI-1177), and `over_face` says what it covers."""
    job = _shape_job(
        monkeypatch,
        [BOX_BOTTOM],
        request="Captions in a light grey box at the bottom.",
        corner=False,
    )
    _with_faces(monkeypatch, [face])
    variant = _run(job)
    rects = _box_rects(job)
    assert all(0.75 <= (r[1] + r[3]) / 2 <= 0.87 for r in rects)
    assert any(_overlaps(rect, face["box"]) for rect in rects) is covered
    assert variant["caption_look_receipt"]["over_face"] is covered


def test_the_draft_review_never_promises_a_shape_to_an_old_build(monkeypatch):
    import dataclasses

    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
    monkeypatch.setattr(settings, "phone_caption_shape_enabled", True)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "authoredText", "captionShape"],
    )
    old = dataclasses.replace(_draft_facts(), client_protocol=35)
    assert bc.check_requirement(Q2_CAPTIONS, old).status != "met"
    new = dataclasses.replace(_draft_facts(), client_protocol=36)
    assert bc.check_requirement(Q2_CAPTIONS, new).status == "met"


# --------------------------------------------- the box text always reads (review, 2080f66)

# With no colour asked a box's text was always black, so "captions in a black box" drew black
# on black with no outline. Ways the fix can go wrong, each caught below:
# - B1 no colour asked on a dark box: the text is still black, or any colour under 4.5:1 on it.
# - B2 the readable colour is drawn but not stored on the variant, so an editor Save and the
#   iPhone editor (both rebuild from `caption_text_color` / `caption_editor_style`) paint the
#   invisible text again.
# - B3 an asked colour that vanishes into the asked box ("white captions in a light grey box")
#   stays unreadable; or it is silently repainted (the colour is hers, KRI-1177) while the
#   receipt still calls the look met.
# - B4 an asked colour that already reads on the box is repainted or outlined.
# - B5 the light grey box (Q2) or a caption with no box changes: the Q1 / Q2 / flag-off tests.


def _ink_hex(ink) -> str:
    return "#{:02X}{:02X}{:02X}".format(*_rgb255(ink))


def _wcag_contrast(a: str, b: str) -> float:
    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize(
    ("color", "box", "fill", "outline"),
    [
        (None, "#000000", "#FFFFFF", None),  # B1: the review's case
        (None, "#0000FF", "#FFFFFF", None),  # B1: any dark box
        ("#FFFFFF", "#000000", "#FFFFFF", None),  # B4: her colour reads, so it is left alone
        ("#FFFFFF", "#E5E5E5", "#FFFFFF", "#000000"),  # B3: her colour, outlined to read
        ("#000000", "#000000", "#000000", "#FFFFFF"),  # B3
    ],
    ids=["black_box", "blue_box", "white_on_black", "white_on_light_grey", "black_on_black"],
)
def test_box_text_always_reads_on_the_asked_box(monkeypatch, color, box, fill, outline):
    facts = {"captions": "on", "caption_box": box, "caption_position": "bottom"}
    if color is not None:
        facts["caption_color"] = color
    asked = BriefRequirement(
        id="boxcolor",
        kind="style",
        scope="global",
        description="Captions in a box at the bottom",
        facts=facts,
    )
    job = _shape_job(monkeypatch, [asked], request="Captions in a box at the bottom.", corner=False)
    variant = _run(job)
    drawn = _caption_layers(job)
    # B2: an editor Save rebuilds the captions from the variant alone.
    saved = compile_caption_layers(
        variant["caption_cues"],
        canvas_width=int(CANVAS_W),
        canvas_height=int(CANVAS_H),
        style=variant["voiceover_caption_style"],
        look=caption_look_from_variant(variant),
    )
    assert drawn and len(saved) == len(drawn)
    for layer in [*drawn, *saved]:
        assert layer.background is not None, layer.id
        assert _ink_hex(layer.background.color) == box
        for run in layer.runs:
            assert _ink_hex(run.fill) == fill, (layer.id, run.text)
            if outline is None:
                assert run.stroke_width == 0  # the clean box look: no outline needed
                assert _wcag_contrast(fill, box) >= 4.5
            else:
                assert run.stroke_width > 0, (layer.id, run.text)
                assert _ink_hex(run.stroke) == outline
                assert _wcag_contrast(outline, box) >= 4.5
    # B2: the fields the iPhone editor reads.
    assert variant["caption_text_color"] == fill
    if outline is None:
        assert variant["caption_stroke_width"] == 0
        assert "stroke_color" not in variant["caption_editor_style"]
    else:
        assert variant["caption_stroke_width"] > 0
        assert variant["caption_editor_style"]["stroke_color"] == outline
    # B3: the asked colour is on screen, so the review may call the look met.
    assert _receipt(asked, variant).status == "met"
