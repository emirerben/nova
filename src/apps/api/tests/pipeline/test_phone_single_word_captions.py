"""Display: Word as ONE WORD AT A TIME on phone Talking recipes (`karaoke.single_word`).

Everything goes through the real compilers: the editor Save path
(`prepare_editor_commit` -> `phone_editor` -> `compile_phone_subtitled_plan` ->
`compile_caption_layers`) on a ~190 s talk with per-word timings. The off-state is pinned to
digests generated from origin/main (`tests/pipeline/single_word_off_state.json`):
until `singleWordText` is verified AND the build declares protocol 11, nothing moves.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings
from app.kria.portable_text import KaraokeContent
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.phone_captions import (
    MAX_CAPTION_LAYERS,
    PhoneCaptionLook,
    caption_look_from_variant,
    compile_caption_layers,
)
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.phone_rollout import (
    SINGLE_WORD_CAPTIONS_STAMP,
    PhoneCapabilityUnavailable,
    validate_phone_pilot_recipe,
)
from tests.pipeline.single_word_fixture import (
    OFF_STATE_CASES,
    TALK_SECONDS,
    off_state_digests,
    recipe_digest,
    save_digests,
    save_word_style,
    saved_recipe,
    talk_cues,
    talk_job,
    talk_recipe,
)

GOLDEN = json.loads((Path(__file__).with_name("single_word_off_state.json")).read_text())
SAVE_CASES = {"save-word-unset": None, "save-word-true": True, "save-word-false": False}
VERIFIED = ["singleWordText", "authoredText"]
LIME = (0xC5 / 255, 0xF8 / 255, 0x2A / 255)


@pytest.fixture(autouse=True)
def _no_request_context():
    clear_request_context()
    yield
    clear_request_context()


def _request_from(protocol: int | None) -> None:
    set_client_protocol(protocol)


def _baseline(job) -> str:
    from app.routes.generative_jobs import variant_render_baseline

    return variant_render_baseline(job.assembly_plan["variants"][0])


def _word_cues():
    return [cue for cue in talk_cues() if cue.get("words")]


# --- the off-state is origin/main, byte for byte ---------------------------------------


def test_compile_with_the_default_look_matches_origin_main():
    assert off_state_digests() == {name: GOLDEN[name] for name in OFF_STATE_CASES}


@pytest.mark.parametrize("name,highlight", SAVE_CASES.items())
def test_save_with_the_feature_unverified_matches_origin_main(monkeypatch, name, highlight):
    job = talk_job(monkeypatch)
    _request_from(11)  # a new build, but singleWordText is not verified (production today)
    save_word_style(job, highlight=highlight)
    assert recipe_digest(saved_recipe(job)) == GOLDEN[name]
    assert SINGLE_WORD_CAPTIONS_STAMP not in job.assembly_plan["variants"][0]


@pytest.mark.parametrize("name,highlight", SAVE_CASES.items())
def test_save_from_a_build_below_protocol_11_matches_origin_main(monkeypatch, name, highlight):
    job = talk_job(monkeypatch, verified=VERIFIED)
    _request_from(10)
    save_word_style(job, highlight=highlight)
    assert recipe_digest(saved_recipe(job)) == GOLDEN[name]
    assert SINGLE_WORD_CAPTIONS_STAMP not in job.assembly_plan["variants"][0]


@pytest.mark.parametrize("name,highlight", SAVE_CASES.items())
def test_save_with_no_declared_protocol_matches_origin_main(monkeypatch, name, highlight):
    job = talk_job(monkeypatch, verified=VERIFIED)
    _request_from(None)  # an old build sends no header
    save_word_style(job, highlight=highlight)
    assert recipe_digest(saved_recipe(job)) == GOLDEN[name]


def test_save_digests_are_still_the_golden_ones():
    assert save_digests() == {name: GOLDEN[name] for name in SAVE_CASES}


def test_unset_highlight_and_sentence_style_keep_the_full_line_sweep_even_when_enabled():
    # `single_word=True` is what a qualifying build gets; these cases are not in the mode.
    for name in ("word-unset", "sentence-unset", "sentence-highlight-true"):
        style, look = OFF_STATE_CASES[name]
        recipe = talk_recipe(caption_style=style, look=PhoneCaptionLook(**look, single_word=True))
        assert recipe_digest(recipe) == GOLDEN[name], name
        assert "singleWordText" not in recipe.required_capabilities


# --- the new shape -----------------------------------------------------------------------


def _saved_single_word_recipe(monkeypatch, *, highlight: bool):
    job = talk_job(monkeypatch, verified=VERIFIED)
    _request_from(11)
    save_word_style(job, highlight=highlight)
    return job, saved_recipe(job)


@pytest.mark.parametrize("highlight", [True, False])
def test_a_qualifying_save_emits_one_single_word_layer_per_cue(monkeypatch, highlight):
    job, recipe = _saved_single_word_recipe(monkeypatch, highlight=highlight)
    cues = _word_cues()

    assert len(recipe.text_layers) == len(cues)  # one layer per cue, far under the 500 cap
    assert len(recipe.text_layers) < MAX_CAPTION_LAYERS
    for layer, cue in zip(recipe.text_layers, cues, strict=True):
        assert layer.effect == "karaoke-line"
        assert layer.karaoke.single_word is True
        assert layer.karaoke.active_only is True
        assert [run.text for run in layer.runs] == [word["text"] for word in cue["words"]]
        assert len(layer.karaoke.starts) == len(layer.runs)
        # Starts are the words' own timings, local to the layer; the layer opens at word 0.
        assert layer.start == pytest.approx(cue["words"][0]["start_s"], abs=1e-3)
        assert layer.karaoke.starts == pytest.approx(
            [word["start_s"] - cue["words"][0]["start_s"] for word in cue["words"]], abs=2e-3
        )
        assert layer.karaoke.starts == sorted(layer.karaoke.starts)
        assert layer.karaoke.starts[0] == 0.0
        assert layer.end <= cue["end_s"] + 1e-9
        assert {(run.baseline_y) for run in layer.runs} == {layer.runs[0].baseline_y}
        assert layer.anchor_x == pytest.approx(540.0)
    assert job.assembly_plan["variants"][0][SINGLE_WORD_CAPTIONS_STAMP] is True


@pytest.mark.parametrize("highlight", [True, False])
def test_every_word_is_the_one_word_line_layout_of_its_own_caption(monkeypatch, highlight):
    _job, recipe = _saved_single_word_recipe(monkeypatch, highlight=highlight)
    # The reference is the plain full-line path (no one-word mode) for a one-word caption.
    look = replace(
        caption_look_from_variant({"caption_editor_style": {"highlight_spoken_word": highlight}}),
        single_word=False,
    )

    for layer in recipe.text_layers[:6]:
        for run in layer.runs:
            alone = compile_caption_layers(
                [
                    {
                        "text": run.text,
                        "start_s": 0.0,
                        "end_s": 2.0,
                        "words": [{"text": run.text, "start_s": 0.0, "end_s": 1.0}],
                    }
                ],
                canvas_width=1080,
                canvas_height=1920,
                style="word",
                look=look,
            )[0].runs[0]
            # Same absolute pixels, font, tracking, stroke and shadow as the standalone caption.
            assert run.model_dump() == alone.model_dump()
            assert run.font_asset_id == recipe.text_layers[0].runs[0].font_asset_id


def test_the_highlight_colour_is_the_highlight_or_the_plain_caption_colour(monkeypatch):
    _job, on = _saved_single_word_recipe(monkeypatch, highlight=True)
    _job, off = _saved_single_word_recipe(monkeypatch, highlight=False)
    lit = on.text_layers[0].karaoke.highlight
    plain = off.text_layers[0].karaoke.highlight
    assert (lit.red, lit.green, lit.blue) == pytest.approx(LIME, abs=1 / 255)
    assert (plain.red, plain.green, plain.blue) == pytest.approx((1.0, 1.0, 1.0), abs=1 / 255)


def test_the_recipe_requires_the_capability_and_validates(monkeypatch):
    _job, recipe = _saved_single_word_recipe(monkeypatch, highlight=True)
    assert "singleWordText" in recipe.required_capabilities
    assert "authoredText" in recipe.required_capabilities  # active_only, as before
    EditRecipeV2.model_validate_json(recipe.model_dump_json())
    assert '"single_word":true' in recipe.model_dump_json().replace(" ", "")

    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)

    others = [c for c in recipe.required_capabilities if c != "singleWordText"]
    monkeypatch.setattr(settings, "phone_render_verified_features", others)
    with pytest.raises(PhoneCapabilityUnavailable) as refused:
        validate_phone_pilot_recipe(recipe)
    assert refused.value.capability == "singleWordText"


def test_without_the_mode_the_capability_is_not_required(monkeypatch):
    recipe = talk_recipe(caption_style="word", look=PhoneCaptionLook(highlight_spoken_word=True))
    assert "singleWordText" not in recipe.required_capabilities
    assert recipe.text_layers[0].karaoke.single_word is None
    assert "single_word" not in recipe.model_dump_json()


# --- later compiles with no request (worker chat edits) ----------------------------------


def test_a_worker_compile_follows_the_stamp_the_last_qualifying_save_left(monkeypatch):
    job, _recipe = _saved_single_word_recipe(monkeypatch, highlight=True)
    variant = job.assembly_plan["variants"][0]
    clear_request_context()  # the worker: no HTTP request, so no app build to ask

    assert caption_look_from_variant(variant).single_word is True
    unstamped = {k: v for k, v in variant.items() if k != SINGLE_WORD_CAPTIONS_STAMP}
    assert caption_look_from_variant(unstamped).single_word is False

    monkeypatch.setattr(settings, "phone_render_verified_features", [])  # rollback = instant
    assert caption_look_from_variant(variant).single_word is False


def test_a_save_from_an_older_build_clears_the_stamp(monkeypatch):
    job, _recipe = _saved_single_word_recipe(monkeypatch, highlight=True)
    assert job.assembly_plan["variants"][0][SINGLE_WORD_CAPTIONS_STAMP] is True
    _request_from(10)
    save_word_style(job, highlight=True, base_generation=_baseline(job))
    assert SINGLE_WORD_CAPTIONS_STAMP not in job.assembly_plan["variants"][0]
    assert not any(layer.karaoke.single_word for layer in saved_recipe(job).text_layers)


# --- edge cases --------------------------------------------------------------------------


def _compile(cues, **look):
    return compile_caption_layers(
        cues,
        canvas_width=1080,
        canvas_height=1920,
        style="word",
        timeline_duration_s=TALK_SECONDS + 10,
        look=PhoneCaptionLook(highlight_spoken_word=True, single_word=True, **look),
    )


def _cue(words: list[str], *, start: float = 0.0, step: float = 0.1):
    return {
        "text": " ".join(words),
        "start_s": start,
        "end_s": start + step * len(words) + 0.5,
        "words": [
            {"text": w, "start_s": start + i * step, "end_s": start + (i + 1) * step}
            for i, w in enumerate(words)
        ],
    }


def test_a_word_that_would_wrap_sends_that_cue_as_full_line_karaoke():
    fine = _cue(["alpha", "beta", "gamma"])
    too_wide = _cue(["W" * 40, "tail"], start=5.0)
    layers = _compile([fine, too_wide, _cue(["delta", "epsilon"], start=9.0)])

    assert [layer.karaoke.single_word for layer in layers] == [True, None, True]
    assert layers[1].karaoke.active_only is None  # today's cumulative sweep, untouched
    assert len(layers[1].runs) == 2


def test_a_cue_over_100_words_becomes_consecutive_layers():
    words = [f"w{i}" for i in range(250)]
    layers = _compile([_cue(words, step=0.05)])

    assert [layer.id for layer in layers] == ["caption-0", "caption-0-part-1", "caption-0-part-2"]
    assert [len(layer.runs) for layer in layers] == [100, 100, 50]
    assert all(layer.karaoke.single_word for layer in layers)
    assert [run.text for layer in layers for run in layer.runs] == words
    for before, after in zip(layers, layers[1:], strict=False):
        assert before.end == pytest.approx(after.start)  # no gap, no overlap
    for layer in layers:
        assert layer.karaoke.starts[0] == 0.0
        assert layer.karaoke.starts == sorted(layer.karaoke.starts)


def test_overlapping_word_timings_never_overlap_on_screen():
    cue = _cue(["one", "two", "three"], step=0.1)
    cue["words"][1]["start_s"] = cue["words"][0]["start_s"]  # two starts with one
    (layer,) = _compile([cue])
    assert layer.karaoke.starts[0] < layer.karaoke.starts[1] < layer.karaoke.starts[2]


def test_a_left_anchored_caption_keeps_every_word_on_the_left_edge():
    (layer,) = _compile(
        [_cue(["short", "a-much-longer-word"])], text_anchor="left", position_x_frac=80 / 1080
    )
    assert layer.runs[0].x == pytest.approx(layer.runs[1].x)


# --- the model contract ------------------------------------------------------------------


def test_single_word_requires_active_only_and_whitespace_free_words():
    _job_layer = _compile([_cue(["alpha", "beta"])])[0]
    ink = _job_layer.karaoke.highlight
    with pytest.raises(ValueError, match="active-only"):
        KaraokeContent(starts=[0.0, 0.1], highlight=ink, single_word=True)
    spaced = _job_layer.runs[0].model_copy(update={"text": "two words"})
    with pytest.raises(ValueError, match="whitespace-free"):
        _job_layer.model_validate(
            {
                **_job_layer.model_dump(),
                "runs": [spaced.model_dump(), _job_layer.runs[1].model_dump()],
            }
        )


# --- the poll tells an older build to update ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [None, 9, 10])
async def test_device_render_poll_tells_an_old_build_to_update(monkeypatch, protocol):
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from app.routes import device_render

    job, recipe = _saved_single_word_recipe(monkeypatch, highlight=True)
    request = SimpleNamespace(recipe=recipe, identity=SimpleNamespace(), recovery=None)
    monkeypatch.setattr(device_render, "_owned_job", AsyncMock(return_value=job))
    monkeypatch.setattr(
        device_render, "device_status", lambda *_a: SimpleNamespace(request=request)
    )
    monkeypatch.setattr(
        device_render, "_record", lambda *_a: ({}, SimpleNamespace(request=request, phase="x"))
    )
    _request_from(protocol)
    with pytest.raises(HTTPException) as refused:
        await device_render.get_device_render(
            job.id, SimpleNamespace(id=uuid.uuid4()), variant_id="subtitled", db=AsyncMock()
        )
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "single_word_text",
    }
