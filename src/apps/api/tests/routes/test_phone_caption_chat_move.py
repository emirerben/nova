"""KRI-1008 Finding 4 (prod job c7430eed): a chat "move the captions down" never moved them.

A phone Talking (``subtitled``, device-rendered, horizontal 4K source letterboxed in a
1080x1920 canvas) was asked to "move the captions down to the bottom, below his chin and
above the Kria logo". Kria staged ``set_caption_meta {"y_frac": 0.85}``, replied "Done:
Changed the caption y frac." and, after Save, the device render still had the captions at
~66% height. The Save route maps ``y_frac`` to ``caption_margin_v`` only, while the phone
compiler (``caption_look_from_variant``) prefers ``caption_editor_style.position_y_frac``
over the margin, so the stale style y won.

Failure modes, written down before the tests:

- F1  chat y_frac Save, stored geometry, no appearance: style y stale, anchor_y unchanged
      (the incident).
- F2  chat y_frac Save, the client echoes the stale ``appearance.position_y_frac``: the
      stale echo must not beat the explicit ``y_frac``.
- F3  manual slider (y_frac + mirrored appearance y) must still land at the new y.
- F4  appearance-only move with an echoed old y_frac: the appearance must not be reverted.
- F5  legacy variant without style geometry: the margin path stays unchanged and no style
      map is invented.
- F6  y 0.85 left block near the Kria logo: the ink must stay clear of the watermark rect.
- F7  letterboxed landscape: the block must sit below the video band (off the face).
- F8  turn-2 model snapshot must show the staged y, not the old margin.
- F9  the reply names the move in plain words, EN + TR, never the raw key.
- F10 every other caption key also reads as plain words (no underscores / raw keys).
- F11 alignment / x / width in the style map survive a y move.
"""

import uuid
from types import SimpleNamespace

import pytest

from app.kria.device_render import make_device_request
from app.kria.editor_receipts import reply_from_diff
from app.kria.reply_language import reply_language_for
from app.pipeline import text_overlay_skia as cloud
from app.pipeline.captions import y_frac_to_margin_v
from app.pipeline.phone_captions import (
    caption_ink_box,
    caption_look_from_variant,
    watermark_keepout_rect,
)
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    project_editor_draft,
)
from app.services.kria_editor_ops_diff import describe_diff
from app.services.phone_sources import PHONE_SOURCES_FIELD
from tests.pipeline.test_phone_captions import _ink_pixels_under_the_mark
from tests.pipeline.test_phone_subtitled_plan import _binding
from tests.routes.test_phone_subtitled_editor_commit import _enable_subtitled_editor, save

_CANVAS_H = 1920
_FONT = "Inter"

# The real prod caption state of job c7430eed BEFORE the move (job debug JSON).
_PROD_Y_FRAC = 0.6625
_PROD_X_FRAC = 0.10693299424913194
_PROD_WIDTH_FRAC = 0.68
_PROD_LOOK = {
    "caption_size_px": 56,
    "caption_stroke_width": 0,
    "caption_text_color": "#FFFFFF",
    "caption_highlight_color": "#84CC16",
    "caption_shadow_enabled": False,
    "voiceover_caption_font": _FONT,
    "voiceover_caption_style": "word",
    "captions_enabled": True,
}
_PROD_GEOMETRY = {
    "caption_margin_v": 648,
    "caption_editor_style": {
        "alignment": "left",
        "max_width_frac": _PROD_WIDTH_FRAC,
        "position_x_frac": _PROD_X_FRAC,
        "position_y_frac": _PROD_Y_FRAC,
    },
}

# Two short cues plus one that wraps, so the moved block is a real multi-line block.
_CUES = [
    {
        "text": "Hello everyone",
        "start_s": 0.0,
        "end_s": 1.5,
        "words": [
            {"text": "Hello", "start_s": 0.0, "end_s": 0.7},
            {"text": "everyone", "start_s": 0.7, "end_s": 1.5},
        ],
    },
    {
        "text": "welcome back",
        "start_s": 1.5,
        "end_s": 3.0,
        "words": [
            {"text": "welcome", "start_s": 1.5, "end_s": 2.2},
            {"text": "back", "start_s": 2.2, "end_s": 3.0},
        ],
    },
    {
        "text": "this is the oldest square in the city and everything meets here",
        "start_s": 3.0,
        "end_s": 6.0,
        "words": [
            {"text": word, "start_s": 3.0 + 0.25 * index, "end_s": 3.25 + 0.25 * index}
            for index, word in enumerate(
                "this is the oldest square in the city and everything meets here".split()
            )
        ],
    },
]

_CHAT_MOVE = [{"op": "set_caption_meta", "patch": {"y_frac": 0.85}}]


def caption_job(monkeypatch, *, style_geometry: bool = True):
    """The prod-shaped phone Talking variant, pinned with its caption look compiled in.

    Unlike `test_phone_subtitled_editor_commit.phone_job`, the variant carries the real
    prod caption keys BEFORE the first pin, so revision 1 genuinely has the stale
    geometry. ``style_geometry=False`` strips ``caption_margin_v`` and
    ``caption_editor_style`` to give the legacy variant (F5).
    """
    _enable_subtitled_editor(monkeypatch)
    monkeypatch.setattr(gj.settings, "phone_sfx_speech_duck_enabled", False)
    bindings = (_binding(duration_s=10.0, width=3840, height=2160),)
    variant = {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "render_status": "awaiting_device",
        "render_generation_id": "first",
        "caption_cues": _CUES,
        **_PROD_LOOK,
        **(_PROD_GEOMETRY if style_geometry else {}),
    }
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=_CUES,
        caption_style="word",
        caption_look=caption_look_from_variant(variant),
        landscape_fit="fit",
    )
    variant["duration_s"] = recipe.duration
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        assembly_plan={
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [variant],
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id="subtitled", revision=1, recipe=recipe),
        base_generation="first",
    )
    return job


def _variant(job) -> dict:
    return job.assembly_plan["variants"][0]


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


def _caption_layers(job) -> list:
    layers = [layer for layer in _recipe(job).text_layers if layer.id.startswith("caption-")]
    assert layers, "expected caption layers to compile"
    return layers


def _anchor_ys(job) -> list[float]:
    return [layer.anchor_y for layer in _caption_layers(job)]


def _video_band(job) -> tuple[float, float]:
    """Top and bottom (canvas px) of the letterboxed landscape video."""
    track = next(t for t in _recipe(job).tracks if t.id == "subtitled")
    height = _CANVAS_H * track.clips[0].transform.scale
    return (_CANVAS_H - height) / 2, (_CANVAS_H + height) / 2


def _ink_top(layers) -> float:
    typeface = cloud._resolve_typeface_for_overlay({"font_family": _FONT}).typeface
    boxes = [caption_ink_box(run, typeface) for layer in layers for run in layer.runs]
    return min(box[1] for box in boxes if box is not None)


def _assert_anchored_at(job, y_frac: float) -> None:
    """Every caption block sits at ``y_frac`` -- or a watermark lift ABOVE it, never below."""
    expected = y_frac * _CANVAS_H
    for y in _anchor_ys(job):
        assert expected - 0.05 * _CANVAS_H <= y <= expected + 1, (
            f"caption anchor_y {y} is not at y_frac {y_frac} ({expected}px)"
        )


def _commit_chat_ops(job, ops):
    """The chat path: compile the staged ops, then Save the request they build."""
    compiled = compile_editor_ops(job, _variant(job), ops)
    assert isinstance(compiled.payload, gj.EditorCommitRequest)
    gj.prepare_editor_commit(
        job, "subtitled", compiled.payload, user_id="owner", plan_item_id="item"
    )
    return compiled


def test_chat_caption_move_reaches_the_phone_recipe(monkeypatch):
    """F1 + F6 + F7 + F11: the incident, end to end through the chat path and Save."""
    job = caption_job(monkeypatch)

    # Ground the repro: revision 1 really has the prod geometry, over a letterboxed band.
    assert _anchor_ys(job) == [pytest.approx(_PROD_Y_FRAC * _CANVAS_H)] * 3
    band_top, band_bottom = _video_band(job)
    assert (band_top, band_bottom) == (pytest.approx(656.25), pytest.approx(1263.75))

    _commit_chat_ops(job, _CHAT_MOVE)

    # Collect every violation so one run shows the whole picture, not just the first.
    problems: list[str] = []
    layers = _caption_layers(job)
    variant = _variant(job)
    style = variant.get("caption_editor_style") or {}

    # F11 + the persisted half of F1: margin moved, style y mirrored, rest of the map intact.
    if variant.get("caption_margin_v") != 288:  # y_frac_to_margin_v(0.85)
        problems.append(f"caption_margin_v={variant.get('caption_margin_v')}, wanted 288")
    if style.get("position_y_frac") != pytest.approx(0.85):
        problems.append(f"caption_editor_style.position_y_frac={style.get('position_y_frac')}")
    if style.get("alignment") != "left":
        problems.append(f"alignment={style.get('alignment')}, wanted left")
    if style.get("position_x_frac") != pytest.approx(_PROD_X_FRAC):
        problems.append(f"position_x_frac={style.get('position_x_frac')}")
    if style.get("max_width_frac") != pytest.approx(_PROD_WIDTH_FRAC):
        problems.append(f"max_width_frac={style.get('max_width_frac')}")

    # F1: the compiled recipe moved (a watermark lift may nudge it up, never below).
    for layer in layers:
        if layer.anchor_y < 0.80 * _CANVAS_H:
            problems.append(f"{layer.id} anchor_y={layer.anchor_y}, wanted >= {0.80 * _CANVAS_H}")

    # F7: the ink starts below the letterboxed video, i.e. off the speaker's face.
    ink_top = _ink_top(layers)
    if not ink_top > band_bottom:
        problems.append(
            f"caption ink top {ink_top:.0f} is not below the video band ({band_bottom})"
        )

    # F6: no ink under the Kria logo's keep-out rect.
    ink_under_logo = _ink_pixels_under_the_mark(layers, font_family=_FONT)
    if ink_under_logo:
        problems.append(
            f"{ink_under_logo} caption pixels inside {watermark_keepout_rect(1080, _CANVAS_H)}"
        )

    assert not problems, "\n".join(problems)


@pytest.mark.parametrize(
    ("legacy", "y_frac", "appearance_y", "expected_y_frac"),
    [
        pytest.param(False, 0.85, _PROD_Y_FRAC, 0.85, id="F2-chat-y-with-stale-appearance-echo"),
        pytest.param(False, 0.5, 0.5, 0.5, id="F3-slider-y-with-mirrored-appearance"),
        pytest.param(False, _PROD_Y_FRAC, 0.5, 0.5, id="F4-appearance-move-with-echoed-y"),
        pytest.param(True, 0.5, None, 0.5, id="F5-legacy-variant-margin-path"),
    ],
)
def test_caption_y_save_matrix(monkeypatch, legacy, y_frac, appearance_y, expected_y_frac):
    """F2-F5: whichever of ``y_frac`` / ``appearance.position_y_frac`` the creator CHANGED wins."""
    job = caption_job(monkeypatch, style_geometry=not legacy)
    baseline_y_frac = 0.8 if legacy else _PROD_Y_FRAC  # legacy default margin 384 -> 0.8
    assert _anchor_ys(job) == [pytest.approx(baseline_y_frac * _CANVAS_H)] * 3

    appearance = (
        None
        if appearance_y is None
        else gj.EditorCaptionAppearance(
            alignment="left",
            position_x_frac=_PROD_X_FRAC,
            max_width_frac=_PROD_WIDTH_FRAC,
            position_y_frac=appearance_y,
        )
    )
    save(job, caption_meta=gj.EditorCommitCaptionMeta(y_frac=y_frac, appearance=appearance))

    _assert_anchored_at(job, expected_y_frac)
    if legacy:
        # The margin path is untouched and no style map is invented for a legacy variant.
        assert _variant(job)["caption_margin_v"] == y_frac_to_margin_v(expected_y_frac)
        assert "caption_editor_style" not in _variant(job)


def test_turn_two_snapshot_sees_the_staged_caption_spot(monkeypatch):
    """F8: the snapshot the planner reads on the NEXT chat turn reflects the staged y."""
    job = caption_job(monkeypatch)
    compiled = compile_editor_ops(job, _variant(job), _CHAT_MOVE)

    draft = compiled.payload.model_dump(mode="json", exclude_none=True)
    projected = project_editor_draft(_variant(job), draft, job)
    meta = build_editor_snapshot(job, projected)["captions"]["meta"]

    assert meta["y_frac"] == pytest.approx(0.85)
    # If the snapshot still lists an appearance y, it must not contradict the staged spot.
    assert (meta["appearance"] or {}).get("position_y_frac", 0.85) == pytest.approx(0.85)


def _chat_phrases(job, patch: dict) -> list[str]:
    """The receipt phrases the creator's chat reply is built from for one staged caption patch."""
    compiled = compile_editor_ops(job, _variant(job), [{"op": "set_caption_meta", "patch": patch}])
    return describe_diff(compiled.diff.entries)


@pytest.mark.parametrize(
    ("language", "plain_words"),
    [("en", "Moved the captions to the bottom"), ("tr", "Altyazılar alt konuma taşındı")],
)
def test_caption_move_reply_is_plain_words(monkeypatch, language, plain_words):
    """F9: the reply says what moved, in words -- never "Changed the caption y frac"."""
    job = caption_job(monkeypatch)
    with reply_language_for(language):
        reply = reply_from_diff(_chat_phrases(job, {"y_frac": 0.85}))

    assert plain_words in reply
    assert "y frac" not in reply.lower() and "y_frac" not in reply


# The keys `set_caption_meta` accepts besides y_frac (EditorCommitCaptionMeta / the copilot
# parser's _CAPTION_META_FIELDS), with a representative value each.
_OTHER_CAPTION_KEYS = [
    ("enabled", False),
    ("style", "word"),
    ("font", "Inter"),
    ("size_px", 80),
    ("color", "#FFD60A"),
    ("highlight_color", "#FFD60A"),
    ("stroke_width", 0),
    ("shadow_enabled", True),
]


@pytest.mark.parametrize(
    ("key", "value"), _OTHER_CAPTION_KEYS, ids=[k for k, _ in _OTHER_CAPTION_KEYS]
)
def test_every_caption_key_reads_as_plain_words(monkeypatch, key, value):
    """F10: no caption setting is announced by its code name. Negative checks only: the exact
    wording is the fix's call (the repo spells the colour words British, e.g. "Colour")."""
    job = caption_job(monkeypatch)
    raw = key.replace("_", " ")

    phrases = _chat_phrases(job, {key: value})
    reply = reply_from_diff(phrases)
    assert phrases, "the change must be announced"
    assert "_" not in reply
    assert f"Changed the caption {raw}" not in phrases, "generic raw-key phrasing"
    if "_" in key:
        assert raw not in reply.lower(), f"raw key {key!r} leaked into the reply"

    with reply_language_for("tr"):
        phrases_tr = _chat_phrases(job, {key: value})
    assert "_" not in reply_from_diff(phrases_tr)
    assert "Altyazı ayarı değişti" not in phrases_tr, "generic phrasing says nothing"
