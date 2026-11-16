"""KRI-1501: the editor's translated caption track (capability, switch route, Save).

Contract: docs/pipelines/dual-captions.md ("How a creator turns it on", "Data contract").

Failure modes, written before the code (each is a test below):

 1. The capability is offered to a build that cannot keep the line (older protocol, switch
    off, device unverified) or to a variant with no captions.                  [capability]
 2. A Save loses the translated line or its look: ``caption_cues[*].translation`` and
    ``caption_meta.translation`` must persist, the recipe must draw the line where the track
    says, and the server-owned ``outcome`` / ``dropped_cue_ids`` survive a Save that echoes
    something else.                                                            [round_trip]
 3. An older build adds a translated line, or re-sends the cues without the stored lines
    (erasing them): 426 ``client_update_required`` (feature ``caption_translation``).
    With the switch off: 422 ``caption_translation_disabled``. Switching the line off or
    removing the track is never refused.                                       [refusals]
 4. The switch route stores something, translates the spoken line into itself, or fails the
    request on a provider error.                                               [route]
 5. A malformed track (unknown language, same as the source, size out of range, extra keys)
    is accepted.                                                               [validation]

Review round (written before the fixes):

 6. The route trusts the stored ``caption_language`` label (which can echo a whisper hint)
    over the lines' own text: Turkish lines under an "en" label get translated "from
    English", and a Turkish line under Turkish speech is not refused.          [route]
 7. The route is an unbounded paid call on client text: no rate limit, no size cap
    (422 ``caption_translation_too_long`` past 20,000 characters; 429 past the limit). [route]
 8. A Save that brings the switch route's fresh lines keeps the stale stored outcome
    ("translated" over the route's "partial"), so the receipt overstates.     [round_trip]
 9. A chat op rewrites a caption's words and the yellow line keeps the old sentence: the
    cue must lose its ``translation``, its id joins ``dropped_cue_ids``, the outcome becomes
    "partial", and the op's change line says so.                               [chat]
10. With the switch off, an older build's Save that leaves out the stored lines is refused
    with 426 although nothing would draw them.                                 [refusals]
"""

from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routes import generative_jobs as gj
from app.services.client_protocol import DUAL_CAPTIONS_PROTOCOL, set_client_protocol
from app.services.device_render import device_status
from tests.routes.test_phone_subtitled_editor_commit import phone_job

_TRACK = {
    "enabled": True,
    "language": "de",
    "source_language": "en",
    "size_px": None,
    "color": "#F5D33C",
    "font": None,
    "stroke_width": None,
    "position_x_frac": None,
    "position_y_frac": None,
    "line_overrides": {},
    "outcome": "translated",
    "dropped_cue_ids": [],
}
_CUES = [
    {"id": "native-caption-0", "text": "Hello everyone", "start_s": 0.0, "end_s": 1.5},
    {"id": "native-caption-1", "text": "welcome back", "start_s": 1.5, "end_s": 3.0},
]
_GERMAN = ["Hallo zusammen", "Willkommen zurück"]


def _gate(monkeypatch, *, on: bool = True, verified: bool = True) -> None:
    monkeypatch.setattr(gj.settings, "dual_captions_enabled", on)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [
            *[f for f in gj.settings.phone_render_verified_features if f != "dualCaptions"],
            *(["dualCaptions"] if verified else []),
        ],
    )


def _job(
    monkeypatch,
    *,
    translated: bool = False,
    on: bool = True,
    protocol: int | None = DUAL_CAPTIONS_PROTOCOL,
):
    job = phone_job(monkeypatch)
    _gate(monkeypatch, on=on)
    variant = job.assembly_plan["variants"][0]
    variant["caption_cues"] = copy.deepcopy(_CUES)
    variant["caption_language"] = "en"
    if translated:
        for cue, line in zip(variant["caption_cues"], _GERMAN, strict=True):
            cue["translation"] = line
        variant["caption_translation"] = copy.deepcopy(_TRACK)
    set_client_protocol(protocol)
    return job


def save(job, **sections):
    """One editor Save from the variant's current baseline."""
    variant = job.assembly_plan["variants"][0]
    return gj.prepare_editor_commit(
        job,
        "subtitled",
        gj.EditorCommitRequest(base_generation=gj.variant_render_baseline(variant), **sections),
        user_id="owner",
        plan_item_id="item",
    )


def _translated_cues() -> list[dict]:
    return [{**cue, "translation": line} for cue, line in zip(_CUES, _GERMAN, strict=True)]


def _layers(job) -> dict:
    recipe = device_status(job, "subtitled").request.recipe
    return {layer.id: layer for layer in recipe.text_layers if layer.id.startswith("caption")}


# ----------------------------------------------------------------------------- [capability]


def test_capability_is_offered_only_through_the_gate(monkeypatch):
    job = _job(monkeypatch)
    (variant,) = gj._variants_for_response(job)
    assert variant["editor_capabilities"]["caption_translation"] == {
        "languages": ["de", "en", "pt", "tr"]
    }

    for protocol in (DUAL_CAPTIONS_PROTOCOL - 1, None):
        set_client_protocol(protocol)
        (variant,) = gj._variants_for_response(job)
        assert "caption_translation" not in variant["editor_capabilities"]
    set_client_protocol(DUAL_CAPTIONS_PROTOCOL)
    _gate(monkeypatch, on=False)
    (variant,) = gj._variants_for_response(job)
    assert "caption_translation" not in variant["editor_capabilities"]
    _gate(monkeypatch, verified=False)
    (variant,) = gj._variants_for_response(job)
    assert "caption_translation" not in variant["editor_capabilities"]


def test_no_capability_without_captions(monkeypatch):
    job = _job(monkeypatch)
    job.assembly_plan["variants"][0]["caption_cues"] = []
    (variant,) = gj._variants_for_response(job)
    assert "caption_translation" not in variant["editor_capabilities"]


# ----------------------------------------------------------------------------- [round_trip]


def test_save_persists_the_lines_and_the_track_and_draws_them(monkeypatch):
    job = _job(monkeypatch)
    track = {
        **_TRACK,
        "size_px": 50,
        "color": "#ffffff",
        "line_overrides": {"native-caption-1": {"position_y_frac": 0.3, "size_px": 40}},
    }
    save(
        job,
        caption_cues=_translated_cues(),
        caption_meta=gj.EditorCommitCaptionMeta(translation=track),
    )
    variant = job.assembly_plan["variants"][0]
    assert [cue["translation"] for cue in variant["caption_cues"]] == _GERMAN
    stored = variant["caption_translation"]
    assert stored["language"] == "de" and stored["source_language"] == "en"
    assert stored["size_px"] == 50 and stored["color"] == "#FFFFFF"
    assert stored["line_overrides"] == {"native-caption-1": {"position_y_frac": 0.3, "size_px": 40}}
    assert stored["outcome"] == "translated" and stored["dropped_cue_ids"] == []
    layers = _layers(job)
    assert " ".join(run.text for run in layers["caption-tr-0"].runs) == _GERMAN[0]
    assert layers["caption-tr-0"].runs[0].font_size == 50
    assert layers["caption-tr-1"].runs[0].font_size == 40
    assert layers["caption-tr-1"].anchor_y < layers["caption-1"].anchor_y  # its own spot

    # Read back for the editor exactly as saved.
    (read,) = gj._variants_for_response(job)
    assert [cue.get("translation") for cue in read["caption_cues"]] == _GERMAN
    assert read["caption_translation"]["line_overrides"] == stored["line_overrides"]


def test_server_owned_outcome_survives_a_save_that_echoes_another(monkeypatch):
    job = _job(monkeypatch, translated=True)
    variant = job.assembly_plan["variants"][0]
    variant["caption_translation"].update(outcome="partial", dropped_cue_ids=["native-caption-9"])
    save(
        job,
        caption_meta=gj.EditorCommitCaptionMeta(
            translation={**_TRACK, "color": "#00FF00", "outcome": "translated"}
        ),
    )
    stored = job.assembly_plan["variants"][0]["caption_translation"]
    assert stored["color"] == "#00FF00"
    assert stored["outcome"] == "partial" and stored["dropped_cue_ids"] == ["native-caption-9"]


def test_a_text_edit_keeps_every_translated_line(monkeypatch):
    job = _job(monkeypatch, translated=True)
    edited = _translated_cues()
    edited[0] = {**edited[0], "text": "Hello all"}
    save(job, caption_cues=edited)
    variant = job.assembly_plan["variants"][0]
    assert [cue["translation"] for cue in variant["caption_cues"]] == _GERMAN
    assert variant["caption_translation"] == _TRACK
    assert "caption-tr-0" in _layers(job) and "caption-tr-1" in _layers(job)


# ------------------------------------------------------------------------------- [refusals]


@pytest.mark.parametrize("protocol", [DUAL_CAPTIONS_PROTOCOL - 1, None])
def test_an_older_build_cannot_add_a_translated_line(monkeypatch, protocol):
    job = _job(monkeypatch, protocol=protocol)
    for sections in (
        {"caption_cues": _translated_cues()},
        {"caption_meta": gj.EditorCommitCaptionMeta(translation=_TRACK)},
    ):
        with pytest.raises(HTTPException) as error:
            save(job, **sections)
        assert error.value.status_code == 426
        assert error.value.detail == {
            "code": "client_update_required",
            "feature": "caption_translation",
            "minimum_client_protocol": DUAL_CAPTIONS_PROTOCOL,
        }


def test_an_older_build_cannot_erase_stored_lines(monkeypatch):
    job = _job(monkeypatch, translated=True, protocol=DUAL_CAPTIONS_PROTOCOL - 1)
    with pytest.raises(HTTPException) as error:
        save(job, caption_cues=copy.deepcopy(_CUES))
    assert error.value.status_code == 426
    assert job.assembly_plan["variants"][0]["caption_cues"][0]["translation"] == _GERMAN[0]


def test_switch_off_refuses_a_new_line_but_never_taking_it_off(monkeypatch):
    job = _job(monkeypatch, translated=True, on=False)
    with pytest.raises(HTTPException) as error:
        save(job, caption_meta=gj.EditorCommitCaptionMeta(translation={**_TRACK, "language": "tr"}))
    assert error.value.status_code == 422
    assert error.value.detail == {"code": "caption_translation_disabled"}
    # An unchanged echo, switching it off, and removing it all go through.
    save(job, caption_meta=gj.EditorCommitCaptionMeta(translation=_TRACK))
    save(job, caption_meta=gj.EditorCommitCaptionMeta(translation={**_TRACK, "enabled": False}))
    assert job.assembly_plan["variants"][0]["caption_translation"]["enabled"] is False
    save(job, caption_meta=gj.EditorCommitCaptionMeta(translation=None))
    assert "caption_translation" not in job.assembly_plan["variants"][0]


def test_switch_off_draws_no_translated_layer(monkeypatch):
    job = _job(monkeypatch, translated=True, on=False)
    save(job, caption_cues=_translated_cues())
    assert not [key for key in _layers(job) if "-tr-" in key]


# ----------------------------------------------------------------------------- [validation]


@pytest.mark.parametrize(
    "track",
    [
        {**_TRACK, "language": "es"},
        {**_TRACK, "language": "en", "source_language": "en"},
        {**_TRACK, "size_px": 20},
        {**_TRACK, "position_y_frac": 0.95},
        {**_TRACK, "color": "yellow"},
        {**_TRACK, "line_overrides": {"a": {"size_px": 200}}},
        {**_TRACK, "surprise": 1},
    ],
    ids=["language", "same_as_source", "small", "y_range", "color", "line_size", "extra"],
)
def test_a_malformed_track_is_refused(track):
    with pytest.raises(ValidationError):
        gj.EditorCommitCaptionMeta(translation=track)


# ---------------------------------------------------------------------------------- [route]


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    from app.limiter import limiter

    limiter.reset()
    yield
    limiter.reset()


def _request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/plan-items/item/variants/subtitled/caption-translation",
            "headers": [],
            "client": ("203.0.113.7", 1234),
            "query_string": b"",
        }
    )


def _route(job, body: dict, monkeypatch):
    from app.routes import plan_items

    monkeypatch.setattr(plan_items, "_owned_item_render_job", AsyncMock(return_value=job))
    db = SimpleNamespace(commit=AsyncMock(), flush=AsyncMock(), add=Mock())
    response = asyncio.run(
        plan_items.caption_translation_item(
            request=_request(),
            item_id="item",
            variant_id="subtitled",
            body=plan_items.CaptionTranslationRequest.model_validate(body),
            user=SimpleNamespace(id="owner"),
            db=db,
        )
    )
    db.commit.assert_not_called()
    db.flush.assert_not_called()
    db.add.assert_not_called()
    return response


def test_route_translates_the_given_cues_and_stores_nothing(monkeypatch):
    job = _job(monkeypatch)
    before = json.dumps(job.assembly_plan, sort_keys=True, default=str)
    translator = Mock(side_effect=lambda lines, **_kw: _GERMAN[: len(lines)])
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", translator)

    response = _route(job, {"language": "de", "cues": _CUES}, monkeypatch)

    assert response.model_dump() == {
        "language": "de",
        "source_language": "en",
        "outcome": "translated",
        "translations": [
            {"id": "native-caption-0", "text": _GERMAN[0]},
            {"id": "native-caption-1", "text": _GERMAN[1]},
        ],
        "dropped_cue_ids": [],
    }
    assert translator.call_args.kwargs["budgets"] == [25, 25]  # floor(17 x 1.5 s)
    assert json.dumps(job.assembly_plan, sort_keys=True, default=str) == before


def test_route_reports_a_provider_failure_as_unmet(monkeypatch):
    job = _job(monkeypatch)
    # conftest's guard makes the provider call raise.
    response = _route(job, {"language": "de", "cues": _CUES}, monkeypatch)
    assert response.outcome == "unmet"
    assert [line.text for line in response.translations] == [None, None]


def test_route_refuses_the_spoken_language_and_a_closed_gate(monkeypatch):
    job = _job(monkeypatch)
    with pytest.raises(HTTPException) as error:
        _route(job, {"language": "en", "cues": _CUES}, monkeypatch)
    assert error.value.detail == {"code": "caption_translation_same_language"}

    set_client_protocol(DUAL_CAPTIONS_PROTOCOL - 1)
    with pytest.raises(HTTPException) as error:
        _route(job, {"language": "de", "cues": _CUES}, monkeypatch)
    assert error.value.status_code == 426
    assert error.value.detail["feature"] == "caption_translation"

    set_client_protocol(DUAL_CAPTIONS_PROTOCOL)
    _gate(monkeypatch, on=False)
    with pytest.raises(HTTPException) as error:
        _route(job, {"language": "de", "cues": _CUES}, monkeypatch)
    assert (error.value.status_code, error.value.detail) == (
        422,
        {"code": "caption_translation_disabled"},
    )


def test_route_reads_the_spoken_language_from_the_lines_not_the_label(monkeypatch):
    job = _job(monkeypatch)
    job.assembly_plan["variants"][0]["caption_language"] = "en"  # an echoed whisper hint
    turkish = [
        {**_CUES[0], "text": "Abonelerimize bir şey demek ister misin?"},
        {**_CUES[1], "text": "Biz bir komünite yaratma derdindeyiz."},
    ]
    translator = Mock(side_effect=lambda lines, **_kw: ["Hallo zusammen", "Willkommen"])
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", translator)

    response = _route(job, {"language": "de", "cues": turkish}, monkeypatch)
    assert response.source_language == "tr"
    assert translator.call_args.kwargs["source_language"] == "Turkish"
    with pytest.raises(HTTPException) as error:
        _route(job, {"language": "tr", "cues": turkish}, monkeypatch)
    assert error.value.detail == {"code": "caption_translation_same_language"}


def test_route_refuses_too_much_text_without_a_model_call(monkeypatch):
    job = _job(monkeypatch)
    translator = Mock(side_effect=AssertionError("no paid call on an oversized request"))
    monkeypatch.setattr("app.pipeline.caption_translate._llm_translate_lines", translator)
    cues = [
        {"id": f"c{i}", "text": "word " * 119, "start_s": float(i), "end_s": i + 1.0}
        for i in range(40)
    ]
    with pytest.raises(HTTPException) as error:
        _route(job, {"language": "de", "cues": cues}, monkeypatch)
    assert (error.value.status_code, error.value.detail) == (
        422,
        {"code": "caption_translation_too_long"},
    )
    translator.assert_not_called()


def test_route_is_rate_limited(monkeypatch):
    from slowapi.errors import RateLimitExceeded

    job = _job(monkeypatch)
    monkeypatch.setattr(
        "app.pipeline.caption_translate._llm_translate_lines",
        Mock(side_effect=lambda lines, **_kw: _GERMAN[: len(lines)]),
    )
    for _ in range(10):
        _route(job, {"language": "de", "cues": _CUES}, monkeypatch)
    with pytest.raises(RateLimitExceeded):
        _route(job, {"language": "de", "cues": _CUES}, monkeypatch)


def test_a_save_with_the_routes_fresh_lines_takes_its_outcome(monkeypatch):
    job = _job(monkeypatch, translated=True)
    fresh = _translated_cues()
    fresh[1] = {**fresh[1], "translation": "Schön, dass ihr wieder da seid"}
    fresh[0] = {key: value for key, value in fresh[0].items() if key != "translation"}
    save(
        job,
        caption_cues=fresh,
        caption_meta=gj.EditorCommitCaptionMeta(
            translation={**_TRACK, "outcome": "partial", "dropped_cue_ids": ["native-caption-0"]}
        ),
    )
    stored = job.assembly_plan["variants"][0]["caption_translation"]
    assert stored["outcome"] == "partial"
    assert stored["dropped_cue_ids"] == ["native-caption-0"]


def test_switch_off_lets_an_older_build_save_without_the_stored_lines(monkeypatch):
    job = _job(monkeypatch, translated=True, on=False, protocol=DUAL_CAPTIONS_PROTOCOL - 1)
    save(job, caption_cues=copy.deepcopy(_CUES))
    assert all("translation" not in cue for cue in job.assembly_plan["variants"][0]["caption_cues"])


# ----------------------------------------------------------------------------------- [chat]


@pytest.mark.parametrize(
    "op",
    [
        {"op": "edit_caption", "cue_index": 0, "text": "Hello friends"},
        {"op": "replace_caption_text", "find": "everyone", "replace": "friends"},
    ],
    ids=["edit_caption", "replace_caption_text"],
)
def test_a_chat_rewrite_drops_the_stale_translated_line(monkeypatch, op):
    from app.services.kria_editor_ops import compile_editor_ops

    job = _job(monkeypatch, translated=True, protocol=None)
    variant = job.assembly_plan["variants"][0]
    compiled = compile_editor_ops(job, variant, [op])

    payload = compiled.payload
    assert "translation" not in payload.caption_cues[0]
    assert payload.caption_cues[1]["translation"] == _GERMAN[1]
    assert any("translated line" in change for change in compiled.changes)
    assert compiled.before["caption_meta"]["translation"] == _TRACK  # Undo puts it back

    save(job, **payload.model_dump(exclude_unset=True, exclude={"base_generation"}))
    variant = job.assembly_plan["variants"][0]
    assert variant["caption_cues"][0]["text"] == "Hello friends"
    assert "translation" not in variant["caption_cues"][0]
    assert variant["caption_translation"]["outcome"] == "partial"
    assert variant["caption_translation"]["dropped_cue_ids"] == ["native-caption-0"]
    layers = _layers(job)
    assert "caption-tr-0" not in layers and "caption-tr-1" in layers


def test_a_chat_timing_change_keeps_the_translated_line(monkeypatch):
    from app.services.kria_editor_ops import compile_editor_ops

    job = _job(monkeypatch, translated=True, protocol=None)
    variant = job.assembly_plan["variants"][0]
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_caption_timing", "cue_index": 0, "start_s": 0.1}]
    )
    assert compiled.payload.caption_cues[0]["translation"] == _GERMAN[0]
    assert compiled.payload.caption_meta is None
