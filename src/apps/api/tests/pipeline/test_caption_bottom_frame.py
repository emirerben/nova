"""An asked caption position never leaves its final glyphs outside the frame (KRI-1266).

Driven through the real writer (`generation_caption_look`) and the real compiler
(`compile_caption_layers`), because the failure only appeared in their composition: the writer
anchors an asked-bottom block at 0.84, then the watermark pass re-breaks and grows it.
"""

from copy import deepcopy

import pytest

from app.pipeline import text_overlay_skia as cloud
from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_captions import caption_ink_box, compile_caption_layers
from app.services import phone_text_styling as styling
from tests.pipeline.test_phone_text_fit_never_fails import FIXTURE

LONG_EN = (
    "This caption is a longer sentence that should remain visible and unobscured by the "
    "watermark on every exported video"
)
TURKISH_12 = "Kadıköy'ü istediler ve stat teklifi geldi sonra herkes çok şaşırdı bugün"
FRAME_BUDGET = 0.88  # phone_captions.ASKED_CAPTION_FRAME_BUDGET_FRAC


def _compile(position: str, text: str, size: str | None):
    policy = deepcopy(FIXTURE["policy"])
    policy.update(
        selected_fonts={"caption": "Inter", "title": "Inter", "label": "Inter"},
        selection_roles=["caption", "title", "label", "pinned"],
        _environment_sampled=True,
        _background_frames=[],
        _face_boxes=[],
        _caption_boxes=[],
        caption_ask={"position": position, **({"size": size} if size else {})},
    )
    cues = [{"text": text, "start_s": 0.0, "end_s": 4.0}]
    with styling.phone_text_policy_scope(policy):
        look = styling.generation_caption_look(cues, canvas=PORTRAIT)
        layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=look)
    typeface = cloud._resolve_typeface_for_overlay({"font_family": look.font_family}).typeface
    inks = [caption_ink_box(run, typeface) for layer in layers for run in layer.runs]
    inks = [ink for ink in inks if ink]
    return look, layers, inks


@pytest.mark.parametrize("position", ["bottom_left", "bottom", "bottom_right"])
@pytest.mark.parametrize(
    ("text", "size"),
    [(LONG_EN, "large"), (TURKISH_12, None), (TURKISH_12, "large"), ("Short caption", "large")],
)
def test_asked_bottom_glyphs_stay_inside_the_frame(position, text, size):
    look, layers, inks = _compile(position, text, size)
    assert layers and inks
    assert max(ink[3] for ink in inks) <= 1920 * FRAME_BUDGET + 1
    assert min(ink[1] for ink in inks) >= 0
    assert max(ink[2] for ink in inks) <= 1080
    # The asked size is kept: the block is lifted, never shrunk.
    if size == "large":
        assert {run.font_size for layer in layers for run in layer.runs} == {96}


@pytest.mark.parametrize("position", ["bottom_left", "bottom", "bottom_right"])
def test_a_short_asked_bottom_caption_sits_at_the_bottom(position):
    _look, _layers, inks = _compile(position, "Short caption", "large")
    bottom = max(ink[3] for ink in inks) / 1920
    # Not "toward the middle": the ink ends between the watermark band and the frame budget.
    assert 0.80 <= bottom <= FRAME_BUDGET


@pytest.mark.parametrize("position", ["bottom_left", "bottom", "bottom_right"])
def test_the_frame_budget_survives_persist_and_reload(position):
    """Writer -> persisted variant fields -> `caption_look_from_variant` -> compiler (a Save or
    recipe rebuild) still holds the cue inside the frame."""
    from app.pipeline.phone_captions import caption_look_from_variant

    policy = deepcopy(FIXTURE["policy"])
    policy.update(
        selected_fonts={"caption": "Inter", "title": "Inter", "label": "Inter"},
        selection_roles=["caption", "title", "label", "pinned"],
        _environment_sampled=True,
        _background_frames=[],
        _face_boxes=[],
        _caption_boxes=[],
        caption_ask={"position": position, "size": "large"},
    )
    cues = [{"text": LONG_EN, "start_s": 0.0, "end_s": 4.0}]
    with styling.phone_text_policy_scope(policy):
        styling.generation_caption_look(cues, canvas=PORTRAIT)
        fields = styling.generation_style_fields()
    reloaded = caption_look_from_variant(fields)
    assert reloaded.frame_budget_frac == FRAME_BUDGET
    layers = compile_caption_layers(cues, canvas_width=1080, canvas_height=1920, look=reloaded)
    typeface = cloud._resolve_typeface_for_overlay({"font_family": reloaded.font_family}).typeface
    inks = [caption_ink_box(run, typeface) for layer in layers for run in layer.runs]
    assert max(ink[3] for ink in inks if ink) <= 1920 * FRAME_BUDGET + 1


def test_an_unasked_caption_persists_no_budget():
    from app.pipeline.phone_captions import caption_look_from_variant

    policy = deepcopy(FIXTURE["policy"])
    policy.update(
        selected_fonts={"caption": "Inter", "title": "Inter", "label": "Inter"},
        selection_roles=["caption", "title", "label", "pinned"],
        _environment_sampled=True,
        _background_frames=[],
        _face_boxes=[],
        _caption_boxes=[],
    )
    cues = [{"text": "Short caption", "start_s": 0.0, "end_s": 4.0}]
    with styling.phone_text_policy_scope(policy):
        styling.generation_caption_look(cues, canvas=PORTRAIT)
        fields = styling.generation_style_fields()
    assert "caption_frame_budget_frac" not in fields
    assert caption_look_from_variant(fields).frame_budget_frac is None
