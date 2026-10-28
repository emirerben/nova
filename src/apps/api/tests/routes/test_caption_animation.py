"""Caption animation (KRI-1007): entrance / exit / loop / speed on captions, per line or for all.

Every case drives the real editor Save (`prepare_editor_commit`) and reads back what the iPhone
loads and what the device recipe draws. Failure modes written first:

  (a) phases saved on one line leak onto other lines, or are dropped on persist;
  (b) the all-captions default overrides a line's own phases, or misses lines without one;
  (c) a Save that sets no phases moves any output (recipe, stored cues, caption meta);
  (d) hidden lyric rows (KRI-1000) draw because phases were set, or lose phases when shown;
  (e) an app build below the caption-animation protocol saves phases, or wipes stored ones;
  (f) an invalid phase name or an out-of-range speed is stored;
  (g) a guided revision round trip drops a caption row's phases.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status
from app.services.phone_editor import PHONE_EDITOR_SAVED_PLAN_FIELD
from tests.routes.caption_animation_fixture import (
    GOLDEN_FILE,
    all_cases,
    song_caption_job,
    song_caption_save,
)
from tests.tasks.test_user_song_montage_job import composer, harness  # noqa: F401

GOLDEN = json.loads(Path(__file__).with_name(GOLDEN_FILE).read_text())


def test_saves_without_caption_animation_match_origin_main(harness, composer):  # noqa: F811
    assert all_cases(harness) == GOLDEN


# --- shared -------------------------------------------------------------------------------

FADE = {"entrance": "fade", "exit": "fade", "loop": "none", "speed": 1.0}
SLIDE_PULSE = {"entrance": "slide", "exit": "none", "loop": "pulse", "speed": 1.5}
STILL = {"entrance": "none", "exit": "none", "loop": "none", "speed": 1.0}


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _from_app(protocol: int | None = 23) -> None:
    set_client_protocol(protocol)


def _narrated(monkeypatch, **kwargs):
    from tests.routes.test_phone_narrated_editor_commit import _VERIFIED_FEATURES, phone_job

    job = phone_job(monkeypatch, **kwargs)
    job.all_candidates = {}
    monkeypatch.setattr(
        gj.settings, "phone_render_verified_features", [*_VERIFIED_FEATURES, "authoredText"]
    )
    return job


def save(job, **sections):
    return gj.prepare_editor_commit(
        job,
        "narrated",
        gj.EditorCommitRequest(
            base_generation=gj.variant_render_baseline(job.assembly_plan["variants"][0]),
            **sections,
        ),
        user_id="owner",
        plan_item_id="item",
    )


def _captions(job, variant_id="narrated"):
    recipe = device_status(job, variant_id).request.recipe
    return {layer.id: layer for layer in recipe.text_layers if layer.id.startswith("caption-")}


def _phases(layer):
    return None if layer.animation_phases is None else layer.animation_phases.model_dump()


# --- cue-native captions (narrated / subtitled) ----------------------------------------------


def test_phases_on_one_line_persist_draw_and_read_back_on_that_line_only(monkeypatch):
    from tests.routes.test_phone_narrated_editor_commit import _CUES

    job = _narrated(monkeypatch)
    _from_app()
    save(job, caption_cues=[_CUES[0], {**_CUES[1], "animation_phases": SLIDE_PULSE}])

    stored = job.assembly_plan["variants"][0]["caption_cues"]
    assert "animation_phases" not in stored[0]
    assert stored[1]["animation_phases"] == SLIDE_PULSE
    layers = _captions(job)
    assert _phases(layers["caption-1"]) == SLIDE_PULSE
    assert layers["caption-1"].effect == "none"
    assert _phases(layers["caption-0"]) is None
    assert layers["caption-0"].effect == "pop-in"

    (variant,) = gj._variants_for_response(job)
    assert variant["caption_cues"][1]["animation_phases"] == SLIDE_PULSE
    assert "animation_phases" not in variant["caption_cues"][0]
    assert "caption_animation_phases" not in variant
    assert variant["editor_capabilities"]["caption_animation"] is True


def test_the_default_animates_lines_without_their_own_and_a_line_override_wins(monkeypatch):
    from tests.routes.test_phone_narrated_editor_commit import _CUES

    job = _narrated(monkeypatch)
    _from_app()
    save(
        job,
        caption_cues=[{**_CUES[0], "animation_phases": STILL}, _CUES[1]],
        caption_meta=gj.EditorCommitCaptionMeta(animation_phases=FADE),
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["caption_animation_phases"] == FADE
    layers = _captions(job)
    # "None" on a line is a choice: no pop, no default fade.
    assert _phases(layers["caption-0"]) == STILL
    assert layers["caption-0"].effect == "none"
    assert _phases(layers["caption-1"]) == FADE
    (read,) = gj._variants_for_response(job)
    assert read["caption_animation_phases"] == FADE

    # Apply to all: the default changes and the line override is removed by the client.
    save(
        job,
        caption_cues=list(_CUES),
        caption_meta=gj.EditorCommitCaptionMeta(animation_phases=SLIDE_PULSE),
    )
    layers = _captions(job)
    assert [_phases(layers[key]) for key in ("caption-0", "caption-1")] == [SLIDE_PULSE] * 2

    # Clearing the default (explicit null) returns every line to today's pop-in.
    save(job, caption_meta=gj.EditorCommitCaptionMeta(animation_phases=None))
    assert "caption_animation_phases" not in job.assembly_plan["variants"][0]
    layers = _captions(job)
    assert [(layers[k].effect, _phases(layers[k])) for k in ("caption-0", "caption-1")] == [
        ("pop-in", None)
    ] * 2


def test_word_highlight_lines_keep_their_karaoke_sweep_under_the_phases(monkeypatch):
    from tests.routes.test_phone_narrated_editor_commit import _WORD_CUES

    job = _narrated(monkeypatch, cues=_WORD_CUES, style="word")
    _from_app()
    save(job, caption_meta=gj.EditorCommitCaptionMeta(animation_phases=FADE))
    (layer,) = _captions(job).values()
    assert layer.effect == "karaoke-line"
    assert layer.karaoke is not None and len(layer.karaoke.starts) == 3
    assert _phases(layer) == FADE


def test_a_talking_edit_applies_the_default_to_every_caption(monkeypatch):
    from tests.pipeline.single_word_fixture import saved_recipe, talk_job

    job = talk_job(monkeypatch, verified=["authoredText"])
    _from_app()
    gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(
            base_generation="first",
            caption_meta=gj.EditorCommitCaptionMeta(animation_phases=SLIDE_PULSE),
        ),
        user_id="owner",
        plan_item_id="item",
    )
    captions = [layer for layer in saved_recipe(job).text_layers if layer.id.startswith("caption")]
    assert len(captions) > 50
    assert all(_phases(layer) == SLIDE_PULSE for layer in captions)
    assert "authoredText" in saved_recipe(job).required_capabilities


def test_the_kill_switch_compiles_stored_phases_as_before(monkeypatch):
    from tests.routes.test_phone_narrated_editor_commit import _CUES

    job = _narrated(monkeypatch)
    monkeypatch.setattr(gj.settings, "caption_animation_enabled", False)
    _from_app()
    save(job, caption_cues=[_CUES[0], {**_CUES[1], "animation_phases": FADE}])
    assert job.assembly_plan["variants"][0]["caption_cues"][1]["animation_phases"] == FADE
    assert all(
        layer.effect == "pop-in" and layer.animation_phases is None
        for layer in _captions(job).values()
    )
    (variant,) = gj._variants_for_response(job)
    assert "caption_animation" not in variant["editor_capabilities"]


@pytest.mark.parametrize("protocol", [22, None])
def test_an_older_build_cannot_save_or_wipe_caption_phases(monkeypatch, protocol):
    from tests.routes.test_phone_narrated_editor_commit import _CUES

    job = _narrated(monkeypatch)
    _from_app(protocol)
    (variant,) = gj._variants_for_response(job)
    assert "caption_animation" not in variant["editor_capabilities"]
    for sections in (
        {"caption_cues": [_CUES[0], {**_CUES[1], "animation_phases": FADE}]},
        {"caption_meta": gj.EditorCommitCaptionMeta(animation_phases=FADE)},
    ):
        with pytest.raises(HTTPException) as refused:
            save(job, **sections)
        assert refused.value.status_code == 426
        assert refused.value.detail["feature"] == "caption_animation"

    _from_app(23)
    save(job, caption_cues=[_CUES[0], {**_CUES[1], "animation_phases": FADE}])
    before = copy.deepcopy(job.assembly_plan)
    _from_app(protocol)
    # The old build re-sends the cues without the key it does not know.
    with pytest.raises(HTTPException) as refused:
        save(job, caption_cues=[dict(_CUES[0], text="First we pack it"), _CUES[1]])
    assert refused.value.status_code == 426
    assert job.assembly_plan == before
    # A caption setting it does know merges, so the stored phases survive it.
    save(job, caption_meta=gj.EditorCommitCaptionMeta(size_px=96))
    assert job.assembly_plan["variants"][0]["caption_cues"][1]["animation_phases"] == FADE


@pytest.mark.parametrize(
    "phases",
    [
        {"entrance": "Fade"},
        {"entrance": "spin"},
        {"loop": "fade"},
        {"speed": 0.1},
        {"speed": 3.5},
        {"entrance": "fade", "duration": 1},
    ],
)
def test_an_invalid_phase_or_speed_is_refused_and_nothing_is_stored(monkeypatch, phases):
    from tests.routes.test_phone_narrated_editor_commit import _CUES

    job = _narrated(monkeypatch)
    _from_app()
    before = copy.deepcopy(job.assembly_plan)
    with pytest.raises(HTTPException) as refused:
        save(job, caption_cues=[_CUES[0], {**_CUES[1], "animation_phases": phases}])
    assert refused.value.status_code == 422
    assert job.assembly_plan == before
    with pytest.raises(ValidationError):
        gj.EditorCommitRequest.model_validate(
            {"base_generation": "first", "caption_meta": {"animation_phases": phases}}
        )


# --- guided song captions (caption_cue text rows + revision caption_meta) ---------------------

POP_BOUNCE = {"entrance": "pop", "exit": "none", "loop": "bounce", "speed": 2.0}


def _lyric_lines(job):
    """Lyric layers grouped by the line they display, each group in time order."""
    recipe = device_status(job, "guided_story").request.recipe
    lines: dict[str, list] = {}
    for layer in recipe.text_layers:
        text = " ".join(" ".join(run.text for run in layer.runs).split())
        if re.search(r"\bla\d+\b", text):
            lines.setdefault(text, []).append(layer)
    return {text: sorted(layers, key=lambda layer: layer.start) for text, layers in lines.items()}


def _line_phases(layers):
    return [_phases(layer) for layer in layers]


def _fade_line(count):
    if count == 1:
        return [FADE]
    middle = [None] * (count - 2)
    return [{**FADE, "exit": "none"}, *middle, {**FADE, "entrance": "none"}]


def test_song_captions_animate_hidden_then_shown_with_a_row_override_that_survives(
    harness,  # noqa: F811
    composer,  # noqa: F811
    monkeypatch,
):
    job = song_caption_job(harness, monkeypatch)
    _from_app()
    hidden = gj.EditorCommitCaptionMeta(enabled=False, style="sentence", animation_phases=FADE)
    song_caption_save(job, caption_meta=hidden)
    assert _lyric_lines(job) == {}
    variant = job.assembly_plan["variants"][0]
    assert gj._guided_v2_revision(job, variant)["caption_meta"]["animation_phases"] == FADE

    shown = gj.EditorCommitCaptionMeta(enabled=True, style="sentence", animation_phases=FADE)
    song_caption_save(job, caption_meta=shown)
    lines = _lyric_lines(job)
    assert len(lines) >= 2
    for layers in lines.values():
        assert _line_phases(layers) == _fade_line(len(layers))

    # One row gets its own animation; its whole displayed line follows it.
    variant = job.assembly_plan["variants"][0]
    rows = copy.deepcopy(gj._guided_v2_revision(job, variant)["text_elements"])
    lyric_rows = [row for row in rows if row["id"].startswith("song-caption-")]
    target = lyric_rows[-1]
    target["animation_phases"] = POP_BOUNCE
    song_caption_save(job, text_elements=rows)
    variant = job.assembly_plan["variants"][0]
    stored = {row["id"]: row for row in gj._guided_v2_revision(job, variant)["text_elements"]}
    assert stored[target["id"]]["animation_phases"] == POP_BOUNCE
    assert not any(
        row.get("animation_phases") for key, row in stored.items() if key != target["id"]
    )
    (own_line,) = [text for text in _lyric_lines(job) if re.search(rf"\b{target['text']}\b", text)]
    for text, layers in _lyric_lines(job).items():
        if text == own_line:
            assert [p and p["loop"] for p in _line_phases(layers)] == ["bounce"] * len(layers)
            assert _line_phases(layers)[0]["entrance"] == "pop"
        else:
            assert _line_phases(layers) == _fade_line(len(layers))

    # A later Save that never touches captions (song moved) keeps both.
    start = variant[PHONE_EDITOR_SAVED_PLAN_FIELD]["user_song"]["window_start_s"]
    song_caption_save(job, user_song=gj.EditorCommitUserSong(window_start_s=start + 0.5))
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    assert {row["id"]: row for row in revision["text_elements"]}[target["id"]][
        "animation_phases"
    ] == POP_BOUNCE
    assert revision["caption_meta"]["animation_phases"] == FADE

    (read,) = gj._variants_for_response(job)
    assert read["caption_meta"]["animation_phases"] == FADE
    assert {row["id"]: row for row in read["text_elements"]}[target["id"]][
        "animation_phases"
    ] == POP_BOUNCE
    assert read["editor_capabilities"]["caption_animation"] is True


def test_an_older_build_cannot_replace_guided_caption_meta_that_holds_phases(
    harness,  # noqa: F811
    composer,  # noqa: F811
    monkeypatch,
):
    job = song_caption_job(harness, monkeypatch)
    _from_app()
    song_caption_save(
        job, caption_meta=gj.EditorCommitCaptionMeta(enabled=True, animation_phases=FADE)
    )
    before = copy.deepcopy(job.assembly_plan)
    _from_app(22)
    with pytest.raises(HTTPException) as refused:
        song_caption_save(job, caption_meta=gj.EditorCommitCaptionMeta(enabled=True, size_px=90))
    assert refused.value.status_code == 426
    assert job.assembly_plan == before
