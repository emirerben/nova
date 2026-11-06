"""Qendresa item 16: the speaker full screen in their own take, over the HTTP editor surface.

Rows S76, S77, S79, S80 (server half) of `docs/reviews/speaker-background-windows-failure-modes.md`:
the editor document shows ``full_screen_windows`` in cut time, a Save from a build below protocol
41 never silently drops it (426, nothing committed), a closed gate refuses a SET but never a
clear, a malformed list has its own code, the copilot background ops keep it, the device poll
tells an old build to update, and with the flag off nothing about a variant without the field
changes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status
from app.services.kria_editor_ops import compile_editor_ops
from app.services.phone_speaker_background import BACKGROUND_REFUSALS
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    lanes_from_recipe,
    project_phone_subtitled_editor_sections,
    variant_carries_speaker_full_opening,
)
from tests.pipeline.test_phone_subtitled_plan import _photo_visual
from tests.routes.test_phone_subtitled_editor_commit import _resave, save
from tests.routes.test_speaker_background_chat_ops import (
    _base,
    _commit,
    _parse,
    _snapshot,
)
from tests.routes.test_speaker_background_editor import (
    _background,
    _capability_variant,
    _document,
    _recipe,
    _variant,
    background_job,
)

PHOTO = _photo_visual()
SECOND = _photo_visual(
    media_id="photo-2", gcs_path="users/owner/plan/item/pool/photo-2.jpg", generation="78"
)
POOL = (PHOTO, SECOND)
OPENING = [{"start_s": 0.0, "end_s": 4.0}]
CORNER = {"width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.80}


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open(monkeypatch, *, flag=True, token=True, windows=True) -> None:
    ours = {"speakerBackgroundWindows", "speakerFrameWindows"}
    features = [f for f in settings.phone_render_verified_features if f not in ours]
    if windows:
        features.append("speakerBackgroundWindows")
    if token:
        features.append("speakerFrameWindows")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", flag)


def opening_job(monkeypatch):
    """A phone Talking variant whose pinned recipe starts full screen, then the corner."""
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(41)
    save(
        job,
        speaker_background=_document(PHOTO)
        | {"speaker_frame": CORNER, "full_screen_windows": OPENING},
    )
    clear_request_context()
    return job


def _spans(layer) -> list[tuple[float, float]]:
    return [(w.start, w.end) for w in layer.full_screen_windows or ()]


# --- the document shows it, in cut time ---------------------------------------------------------


def test_the_recipe_and_the_editor_document_carry_the_opening(monkeypatch) -> None:
    job = opening_job(monkeypatch)
    assert _spans(_recipe(job).speaker_background) == [(0.0, 4.0)]
    assert "speakerFrameWindows" in _recipe(job).required_capabilities
    assert _variant(job)["speaker_background"]["full_screen_windows"] == OPENING
    assert variant_carries_speaker_full_opening(job.assembly_plan, _variant(job))
    # Re-projected from the pinned recipe alone (a never-saved variant), same document.
    _variant(job).pop("speaker_background")
    for key in (PHONE_SUBTITLED_EDITOR_LANES_FIELD, "sound_effects", "media_overlays"):
        _variant(job).pop(key, None)
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"]["full_screen_windows"] == OPENING
    rebuilt = lanes_from_recipe(_recipe(job), visuals=POOL)
    assert [(s.start_s, s.end_s) for s in rebuilt.background.full_screen_windows] == [(0.0, 4.0)]


def test_a_variant_without_the_field_shows_no_key_anywhere(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(41)
    save(job, speaker_background=_document(PHOTO))
    assert "full_screen_windows" not in json.dumps(_variant(job))
    assert "full_screen_windows" not in json.dumps(
        _recipe(job).model_dump(mode="json")["speaker_background"]
    )
    assert not variant_carries_speaker_full_opening(job.assembly_plan, _variant(job))


# --- a Save that does not touch the opening keeps it --------------------------------------------


def test_changing_the_fill_keeps_the_opening(monkeypatch) -> None:
    job = opening_job(monkeypatch)
    set_client_protocol(41)
    _resave(
        job,
        speaker_background=_document(SECOND)
        | {"speaker_frame": CORNER, "full_screen_windows": OPENING},
    )
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)
    assert _spans(_recipe(job).speaker_background) == [(0.0, 4.0)]
    assert _variant(job)["speaker_background"]["full_screen_windows"] == OPENING


def test_a_save_that_leaves_the_background_alone_keeps_it(monkeypatch) -> None:
    job = opening_job(monkeypatch)
    set_client_protocol(41)
    _resave(job, sound_effects=[])
    assert _spans(_recipe(job).speaker_background) == [(0.0, 4.0)]
    assert _variant(job)["speaker_background"]["full_screen_windows"] == OPENING


def test_the_save_can_move_and_clear_the_opening(monkeypatch) -> None:
    job = opening_job(monkeypatch)
    set_client_protocol(41)
    moved = [{"start_s": 0.0, "end_s": 2.5}, {"start_s": 6.0, "end_s": 7.0}]
    _resave(
        job,
        speaker_background=_document(PHOTO)
        | {"speaker_frame": CORNER, "full_screen_windows": moved},
    )
    assert _spans(_recipe(job).speaker_background) == [(0.0, 2.5), (6.0, 7.0)]
    _resave(job, speaker_background=_document(PHOTO) | {"speaker_frame": CORNER})
    assert _recipe(job).speaker_background.full_screen_windows is None
    assert "speakerFrameWindows" not in _recipe(job).required_capabilities
    assert "full_screen_windows" not in _variant(job)["speaker_background"]


def test_remove_background_clears_it(monkeypatch) -> None:
    job = opening_job(monkeypatch)
    set_client_protocol(41)
    _resave(job, speaker_background=None)
    assert _recipe(job).speaker_background is None
    assert "speaker_background" not in _variant(job)
    assert not variant_carries_speaker_full_opening(job.assembly_plan, _variant(job))


# --- S76/S77: an old build is told to update, never silently stripped ---------------------------


def test_save_from_build_37_on_a_variant_carrying_it_is_426_and_commits_nothing(
    monkeypatch,
) -> None:
    job = opening_job(monkeypatch)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    snapshot_before = json.dumps(_variant(job), sort_keys=True, default=str)
    set_client_protocol(40)
    with pytest.raises(HTTPException) as refused:
        _resave(job, sound_effects=[])
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "speaker_full_opening",
        "minimum_client_protocol": 41,
    }
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert json.dumps(_variant(job), sort_keys=True, default=str) == snapshot_before
    assert _spans(_recipe(job).speaker_background) == [(0.0, 4.0)]


def test_a_build_below_38_setting_it_is_426(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(40)
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=_document(PHOTO) | {"full_screen_windows": OPENING})
    assert refused.value.status_code == 426
    assert refused.value.detail["feature"] == "speaker_full_opening"
    assert "full_screen_windows" not in json.dumps(_recipe(job).model_dump(mode="json"))


def test_an_old_build_saving_a_variant_without_it_is_not_asked_to_update(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(40)
    save(job, sound_effects=[])
    assert _recipe(job).speaker_background is not None


def test_device_poll_from_37_on_a_pinned_recipe_with_it_is_426_and_from_38_is_200(
    monkeypatch,
) -> None:
    from app.routes import device_render

    job = opening_job(monkeypatch)
    plain = background_job(monkeypatch, background=_background(PHOTO))
    record: dict = {}

    def detail(target, protocol):
        set_client_protocol(protocol)
        status = SimpleNamespace(request=SimpleNamespace(recipe=_recipe(target)))
        return device_render._client_update_detail(target, record, status)

    assert detail(job, 40) == {
        "code": "client_update_required",
        "feature": "speaker_full_opening",
        "minimum_client_protocol": 41,
    }
    assert detail(job, 41) is None
    # A recipe without the opening is not asked to update on 40 (windows protocol is 32).
    assert detail(plain, 40) is None


# --- S77: a closed gate refuses a SET, never a clear --------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"flag": False}, "full_opening_disabled"),
        ({"token": False}, "full_opening_token_not_verified"),
    ],
)
def test_setting_it_with_a_closed_gate_is_422(monkeypatch, kwargs, reason) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch, **kwargs)
    set_client_protocol(41)
    with pytest.raises(HTTPException) as dark:
        save(job, speaker_background=_document(PHOTO) | {"full_screen_windows": OPENING})
    assert dark.value.status_code == 422
    assert dark.value.detail == {"code": "speaker_full_opening_disabled", "reason": reason}
    assert _recipe(job).speaker_background.full_screen_windows is None
    # A Save of the same background without the opening is unaffected.
    save(job, speaker_background=_document(SECOND))
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)


def test_clearing_works_with_the_gate_closed(monkeypatch) -> None:
    job = opening_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(41)
    _resave(job, speaker_background=_document(PHOTO) | {"speaker_frame": CORNER})
    assert _recipe(job).speaker_background.full_screen_windows is None
    assert "full_screen_windows" not in _variant(job)["speaker_background"]


@pytest.mark.parametrize(
    "spans",
    [
        [{"start_s": 3.0, "end_s": 5.0}, {"start_s": 1.0, "end_s": 2.0}],  # unsorted
        [{"start_s": 0.0, "end_s": 4.0}, {"start_s": 3.0, "end_s": 6.0}],  # overlap
        [{"start_s": 0.0, "end_s": 0.3}],  # under half a second
        [{"start_s": 8.0, "end_s": 40.0}],  # past the edit end
        [{"start_s": float(i), "end_s": i + 0.6} for i in range(5)],  # more than four
        [{"start_s": 4.0, "end_s": 3.0}],  # backwards
    ],
    ids=["unsorted", "overlap", "short", "past_end", "too_many", "backwards"],
)
def test_a_malformed_list_is_422_invalid(monkeypatch, spans) -> None:
    job = opening_job(monkeypatch)
    set_client_protocol(41)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    with pytest.raises(HTTPException) as bad:
        _resave(job, speaker_background=_document(PHOTO) | {"full_screen_windows": spans})
    assert bad.value.status_code == 422
    assert bad.value.detail == {"code": "speaker_full_opening_invalid"}
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _spans(_recipe(job).speaker_background) == [(0.0, 4.0)]


def test_the_refusal_codes_have_creator_words() -> None:
    for code in ("speaker_full_opening_disabled", "speaker_full_opening_invalid"):
        english, turkish = BACKGROUND_REFUSALS[code]
        assert english.startswith("Couldn't") and turkish


# --- capability: present only where the phone can draw it ---------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "protocol", "expected"),
    [
        ({}, 41, True),
        ({}, 40, False),
        ({}, None, False),
        ({"flag": False}, 41, False),
        ({"token": False}, 41, False),
        ({"windows": False}, 41, False),
    ],
)
def test_the_capability_key_follows_the_gate(monkeypatch, kwargs, protocol, expected) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open(monkeypatch)
    variant = _variant(job)
    _open(monkeypatch, **kwargs)
    set_client_protocol(protocol)
    capabilities = gj._editor_capabilities(job, variant)
    assert capabilities.get("speaker_full_opening", False) is expected
    assert gj._speaker_full_opening_capability(job, variant) is expected


def test_a_cloud_variant_never_advertises_it(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(41)
    assert not gj._speaker_full_opening_capability(job, _capability_variant("cloud"))


def test_flag_off_leaves_the_capability_map_and_the_variant_dump_byte_identical(
    monkeypatch,
) -> None:
    flag_off_job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    set_client_protocol(41)
    monkeypatch.setattr(settings, "speaker_full_opening_enabled", False)
    off = json.dumps(gj._editor_capabilities(flag_off_job, _variant(flag_off_job)), sort_keys=True)
    assert "speaker_full_opening" not in off
    save(flag_off_job, speaker_background=_document(PHOTO))
    assert "full_screen_windows" not in json.dumps(_variant(flag_off_job), default=str)
    assert "speakerFrameWindows" not in _recipe(flag_off_job).required_capabilities
    # The save-section model dumps exactly as it did without the field.
    section = gj.EditorCommitSpeakerBackground(**_document(PHOTO))
    assert "full_screen_windows" not in section.model_dump()
    assert section.model_dump() == _document(PHOTO)


def test_the_save_section_rejects_unknown_span_keys() -> None:
    with pytest.raises(ValueError):
        gj.EditorCommitSpeakerBackground(
            **_document(PHOTO), full_screen_windows=[{"start_s": 0, "end_s": 2, "x": 1}]
        )


# --- the copilot ops keep it --------------------------------------------------------------------


def _chat_job(monkeypatch):
    job = opening_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "speakerBackgroundColor"],
    )
    job.assembly_plan["variants"][0]["output_url"] = "https://storage.example/out.mp4"
    job.all_candidates = {}
    return job


def _pool_rows():
    return [
        {"id": PHOTO.media_id, "kind": "image", "label": "a.jpg"},
        {"id": SECOND.media_id, "kind": "image", "label": "b.jpg"},
    ]


def test_the_snapshot_shows_the_opening_only_while_there_is_one(monkeypatch) -> None:
    job = _chat_job(monkeypatch)
    set_client_protocol(41)
    snapshot = _snapshot(job, _base(job), pool=_pool_rows())
    assert snapshot["background"]["full_screen_windows"] == OPENING
    plain = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    plain.assembly_plan["variants"][0]["output_url"] = "https://storage.example/out.mp4"
    plain.all_candidates = {}
    assert (
        "full_screen_windows" not in _snapshot(plain, _base(plain), pool=_pool_rows())["background"]
    )


@pytest.mark.parametrize(
    "raw",
    [
        {"op": "set_background", "asset_id": SECOND.media_id},
        {"op": "set_background_sequence", "asset_ids": [PHOTO.media_id, SECOND.media_id]},
        {"op": "set_speaker_frame", "preset": "small_top_left"},
        {"op": "clear_speaker_frame"},
        {"op": "set_background_color", "color": "#00FF00"},
    ],
    ids=lambda raw: raw["op"],
)
def test_the_background_ops_keep_the_opening(monkeypatch, raw) -> None:
    job = _chat_job(monkeypatch)
    set_client_protocol(41)
    base = _base(job)
    snapshot = _snapshot(job, base, pool=_pool_rows())
    (op,), _state = _parse(snapshot, raw)
    assert op is not None
    compiled = compile_editor_ops(job, base, [op])
    document = compiled.payload.speaker_background.model_dump(exclude_none=True)
    assert document["full_screen_windows"] == OPENING
    _commit(job, compiled)
    assert _spans(_recipe(job).speaker_background) == [(0.0, 4.0)]


def test_remove_background_clears_it_in_chat(monkeypatch) -> None:
    job = _chat_job(monkeypatch)
    set_client_protocol(41)
    base = _base(job)
    (op,), _ = _parse(_snapshot(job, base, pool=_pool_rows()), {"op": "remove_background"})
    compiled = compile_editor_ops(job, base, [op])
    assert compiled.payload.speaker_background is None
    _commit(job, compiled)
    assert _recipe(job).speaker_background is None


def test_a_never_saved_variant_keeps_its_opening_when_the_gate_closed_after_the_render(
    monkeypatch,
) -> None:
    job = opening_job(monkeypatch)
    # A worker-rendered variant: the opening lives only in the pinned recipe, no stored document.
    _variant(job).pop("speaker_background")
    for key in (PHONE_SUBTITLED_EDITOR_LANES_FIELD, "sound_effects", "media_overlays"):
        _variant(job).pop(key, None)
    _open(monkeypatch, flag=False)
    set_client_protocol(41)
    # A fill change that echoes the spans it was shown is not a change to the opening.
    _resave(
        job,
        speaker_background=_document(SECOND)
        | {"speaker_frame": CORNER, "full_screen_windows": OPENING},
    )
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)
    # The document keeps the spans; with the gate closed the compile still falls back to the
    # corner from 0 s on the device recipe (S77), exactly as for a Saved variant.
    assert _variant(job)["speaker_background"]["full_screen_windows"] == OPENING
    # Changing the spans still needs the gate.
    with pytest.raises(HTTPException) as dark:
        _resave(
            job,
            speaker_background=_document(SECOND)
            | {"speaker_frame": CORNER, "full_screen_windows": [{"start_s": 0.0, "end_s": 3.0}]},
        )
    assert dark.value.status_code == 422
