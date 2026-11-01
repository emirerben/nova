"""KRI-1162: background windows + the speaker frame over the HTTP editor surface.

Rows S32 and S35 of `docs/reviews/speaker-background-windows-failure-modes.md`: poll and Save
answer a build below protocol 31 with 426, the editor capability is advertised only where the
phone can draw it, and Save sets or clears the windows and the frame.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.pipeline.phone_subtitled_lanes import (
    SubtitledBackgroundWindow,
    SubtitledSpeakerFrame,
)
from app.routes import generative_jobs as gj
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.services.device_render import device_status
from app.services.phone_speaker_background import background_pin_request, background_pin_requests
from app.services.phone_subtitled_editor import (
    PHONE_SUBTITLED_EDITOR_LANES_FIELD,
    project_phone_subtitled_editor_sections,
    speaker_background_document,
    variant_carries_speaker_background_windows,
)
from tests.pipeline.test_phone_subtitled_plan import _photo_visual, _pool_video_visual
from tests.routes.test_phone_subtitled_editor_commit import _resave, save
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
VIDEO = _pool_video_visual()
POOL = (PHOTO, SECOND, VIDEO)
CORNER = {"width_fraction": 0.42, "x_fraction": 0.76, "y_fraction": 0.80}


@pytest.fixture(autouse=True)
def _request_context():
    clear_request_context()
    yield
    clear_request_context()


def _open_windows(monkeypatch, *, token: bool = True) -> None:
    features = [
        f for f in settings.phone_render_verified_features if f != "speakerBackgroundWindows"
    ]
    if token:
        features.append("speakerBackgroundWindows")
    monkeypatch.setattr(settings, "phone_render_verified_features", features)


def _entry(visual=SECOND, start_s=0.0, end_s=3.0, **changes) -> dict:
    return {
        "media_id": visual.media_id,
        "kind": visual.kind,
        "source_start_s": 0.0,
        "start_s": start_s,
        "end_s": end_s,
    } | changes


def _windowed(**section) -> dict:
    return (
        _document(PHOTO)
        | {
            "windows": [_entry(SECOND, 0.0, 3.0), _entry(VIDEO, 3.0, 6.0, source_start_s=1.0)],
            "speaker_frame": CORNER,
        }
        | section
    )


def windows_job(monkeypatch, *, pinned=POOL):
    """A phone Talking variant whose pinned recipe already carries windows and a frame."""
    job = background_job(monkeypatch, pinned=pinned)
    _open_windows(monkeypatch)
    set_client_protocol(31)
    save(job, speaker_background=_windowed())
    clear_request_context()
    return job


# --- S32: an old build is told to update, never silently stripped -----------------------------


def test_poll_tells_an_old_build_to_update(monkeypatch) -> None:
    from app.routes import device_render

    job = windows_job(monkeypatch)
    assert _recipe(job).speaker_background.uses_windows
    fill_only = background_job(monkeypatch, background=_background())
    record: dict = {}

    def detail(target, protocol):
        set_client_protocol(protocol)
        status = SimpleNamespace(request=SimpleNamespace(recipe=_recipe(target)))
        return device_render._client_update_detail(target, record, status)

    refusal = detail(job, 30)
    assert refusal == {
        "code": "client_update_required",
        "feature": "speaker_background_windows",
        "minimum_client_protocol": 31,
    }
    assert detail(job, 31) is None
    # A first-cut recipe (fill only) is still fine on protocol 27..30, and still 426 below 27.
    assert detail(fill_only, 28) is None
    assert detail(fill_only, 26)["feature"] == "speaker_background"
    # Below 27 the first cut's own refusal wins for a windowed recipe too.
    assert detail(job, 26)["feature"] == "speaker_background"


def test_save_from_old_build_is_426(monkeypatch) -> None:
    job = windows_job(monkeypatch)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    set_client_protocol(30)
    with pytest.raises(HTTPException) as refused:
        _resave(job, sound_effects=[])
    assert refused.value.status_code == 426
    assert refused.value.detail == {
        "code": "client_update_required",
        "feature": "speaker_background_windows",
        "minimum_client_protocol": 31,
    }
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _recipe(job).speaker_background.windows is not None


def test_an_old_build_saving_a_fill_only_edit_is_not_asked_to_update(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background())
    set_client_protocol(28)
    save(job, sound_effects=[])
    assert _recipe(job).speaker_background is not None
    assert not variant_carries_speaker_background_windows(job.assembly_plan, _variant(job))


def test_setting_windows_needs_a_new_enough_build_and_the_token(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_windows(monkeypatch)
    set_client_protocol(30)
    with pytest.raises(HTTPException) as old_build:
        save(job, speaker_background=_windowed())
    assert old_build.value.status_code == 426
    assert old_build.value.detail["feature"] == "speaker_background_windows"
    assert old_build.value.detail["minimum_client_protocol"] == 31

    set_client_protocol(31)
    _open_windows(monkeypatch, token=False)
    with pytest.raises(HTTPException) as dark:
        save(job, speaker_background=_windowed())
    assert dark.value.status_code == 422
    assert dark.value.detail == {
        "code": "speaker_background_windows_disabled",
        "reason": "windows_token_not_verified",
    }
    # The same Save without the new keys is the first cut and still goes through.
    save(job, speaker_background=_document(SECOND))
    assert _recipe(job).speaker_background.windows is None
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)


# --- the Save: set, replace, clear -------------------------------------------------------------


def test_save_sets_windows_and_frame_and_the_document_carries_them(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_windows(monkeypatch)
    set_client_protocol(31)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    prep = save(job, speaker_background=_windowed())

    assert prep["sections"]["speaker_background"] is True
    assert device_status(job, "subtitled").request.identity.recipe_revision == before + 1
    layer = _recipe(job).speaker_background
    assert [(w.media_kind, w.start, w.end, w.source_start) for w in layer.windows] == [
        ("image", 0.0, 3.0, 0.0),
        ("video", 3.0, 6.0, 1.0),
    ]
    assert layer.speaker_frame.model_dump() == CORNER
    assert "speakerBackgroundWindows" in _recipe(job).required_capabilities
    assert _variant(job)["speaker_background"] == _windowed()
    lanes = _variant(job)[PHONE_SUBTITLED_EDITOR_LANES_FIELD]["lanes"]["background"]
    assert [w["generation"] for w in lanes["windows"]] == [SECOND.generation, VIDEO.generation]
    assert lanes["speaker_frame"] == CORNER
    # The projection a reopened editor reads says the same.
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"] == _windowed()
    assert variant_carries_speaker_background_windows(job.assembly_plan, _variant(job))


def test_a_later_save_of_another_lane_keeps_windows_and_frame(monkeypatch) -> None:
    job = windows_job(monkeypatch)
    set_client_protocol(31)
    _resave(job, sound_effects=[])
    assert len(_recipe(job).speaker_background.windows) == 2
    assert _recipe(job).speaker_background.speaker_frame is not None
    assert _variant(job)["speaker_background"] == _windowed()


def test_the_recipe_alone_projects_windows_and_frame_for_the_editor(monkeypatch) -> None:
    job = windows_job(monkeypatch)
    _variant(job).pop("speaker_background", None)
    _variant(job).pop(PHONE_SUBTITLED_EDITOR_LANES_FIELD, None)
    projected = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))
    assert projected["speaker_background"] == _windowed()


def test_a_save_without_the_new_keys_replaces_windows_and_frame(monkeypatch) -> None:
    job = windows_job(monkeypatch)
    set_client_protocol(31)
    save_result = _resave(job, speaker_background=_document(PHOTO))
    assert save_result["sections"]["speaker_background"] is True
    assert _recipe(job).speaker_background.windows is None
    assert _recipe(job).speaker_background.speaker_frame is None
    assert "speakerBackgroundWindows" not in _recipe(job).required_capabilities
    assert _variant(job)["speaker_background"] == _document(PHOTO)


def test_a_frame_alone_without_windows_is_a_valid_save(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_windows(monkeypatch)
    set_client_protocol(31)
    save(job, speaker_background=_document(PHOTO) | {"speaker_frame": CORNER})
    assert _recipe(job).speaker_background.windows is None
    assert _recipe(job).speaker_background.speaker_frame.model_dump() == CORNER
    assert _variant(job)["speaker_background"]["speaker_frame"] == CORNER


def test_clearing_is_still_allowed_with_windows_in_place(monkeypatch) -> None:
    job = windows_job(monkeypatch)
    set_client_protocol(31)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    _open_windows(monkeypatch, token=False)
    _resave(job, speaker_background=None)
    assert _recipe(job).speaker_background is None
    assert "speaker_background" not in _variant(job)
    assert "speakerBackgroundWindows" not in _recipe(job).required_capabilities


def test_an_echo_of_the_stored_windows_commits_nothing(monkeypatch) -> None:
    job = windows_job(monkeypatch)
    set_client_protocol(31)
    prep = _resave(job, speaker_background=_windowed())
    assert prep["has_render_section"] is False


# --- S35: only this item's pool; and the window rules ------------------------------------------


def test_a_window_from_outside_the_pool_is_refused(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=(PHOTO, SECOND))
    _open_windows(monkeypatch)
    set_client_protocol(31)
    before = device_status(job, "subtitled").request.identity.recipe_revision
    for media_id in (VIDEO.media_id, str(uuid.uuid4()), "../../other-users-photo"):
        outsider = _entry(SECOND, 0.0, 3.0, media_id=media_id)
        with pytest.raises(HTTPException) as refused:
            save(job, speaker_background=_document(PHOTO) | {"windows": [outsider]})
        assert refused.value.status_code == 422
        assert refused.value.detail == {"code": "speaker_background_media_not_in_pool"}
    assert device_status(job, "subtitled").request.identity.recipe_revision == before
    assert _recipe(job).speaker_background is None


def test_overlapping_windows_are_refused(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_windows(monkeypatch)
    set_client_protocol(31)
    cases = {
        "overlap": [_entry(SECOND, 0.0, 3.5), _entry(PHOTO, 3.0, 6.0)],
        "unsorted": [_entry(PHOTO, 3.0, 6.0), _entry(SECOND, 0.0, 3.0)],
        "too_short": [_entry(SECOND, 1.0, 1.4)],
        "end_before_start": [_entry(SECOND, 2.0, 1.0)],
    }
    for windows in cases.values():
        with pytest.raises(HTTPException) as refused:
            save(job, speaker_background=_document(PHOTO) | {"windows": windows})
        assert (refused.value.status_code, refused.value.detail) == (
            422,
            {"code": "speaker_background_window_invalid"},
        )
    assert _recipe(job).speaker_background is None
    # Touching windows are fine.
    save(
        job,
        speaker_background=_document(PHOTO)
        | {"windows": [_entry(SECOND, 0.0, 3.0), _entry(PHOTO, 3.0, 6.0)]},
    )
    assert len(_recipe(job).speaker_background.windows) == 2


@pytest.mark.parametrize(
    ("entry", "code"),
    [
        (_entry(SECOND, kind="video"), "speaker_background_kind_mismatch"),
        (_entry(SECOND, source_start_s=1.0), "speaker_background_start_out_of_range"),
        (
            _entry(VIDEO, source_start_s=VIDEO.duration_s),
            "speaker_background_start_out_of_range",
        ),
    ],
)
def test_a_window_that_cannot_play_is_refused_with_its_reason(monkeypatch, entry, code) -> None:
    job = background_job(monkeypatch, pinned=POOL)
    _open_windows(monkeypatch)
    set_client_protocol(31)
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=_document(PHOTO) | {"windows": [entry]})
    assert (refused.value.status_code, refused.value.detail["code"]) == (422, code)


@pytest.mark.parametrize(
    "bad",
    [
        {"windows": []},
        {"windows": [_entry(SECOND, 0.0, 3.0, surprise=1)]},
        {"windows": [_entry(SECOND, 0.0, 1801.0)]},
        {"windows": [_entry(SECOND, i * 0.6, i * 0.6 + 0.5) for i in range(41)]},
        {"speaker_frame": {"width_fraction": 0.1, "x_fraction": 0.5, "y_fraction": 0.5}},
        {"speaker_frame": {"width_fraction": 0.5, "x_fraction": 1.5, "y_fraction": 0.5}},
        {"speaker_frame": {**CORNER, "rotation": 1}},
    ],
)
def test_the_request_shape_rejects_malformed_windows_and_frames(bad) -> None:
    with pytest.raises(ValueError):
        gj.EditorCommitSpeakerBackground.model_validate(_document(PHOTO) | bad)


def test_an_old_shape_request_dumps_exactly_as_it_did() -> None:
    section = gj.EditorCommitSpeakerBackground.model_validate(_document(PHOTO))
    assert section.model_dump(mode="json") == _document(PHOTO)
    assert not section.uses_windows
    full = gj.EditorCommitSpeakerBackground.model_validate(_windowed())
    assert full.model_dump(mode="json") == _windowed()


def test_the_document_omits_the_new_keys_while_unused() -> None:
    assert speaker_background_document(_background()) == _document(PHOTO)
    windowed = _background(
        windows=(
            SubtitledBackgroundWindow(
                media_id=SECOND.media_id,
                gcs_path=SECOND.gcs_path,
                generation=SECOND.generation,
                start_s=0.0,
                end_s=3.0,
            ),
        ),
        speaker_frame=SubtitledSpeakerFrame(**CORNER),
    )
    assert speaker_background_document(windowed) == _document(PHOTO) | {
        "windows": [_entry(SECOND, 0.0, 3.0)],
        "speaker_frame": CORNER,
    }


# --- the pin step the Save and the chat apply paths share (S35) ----------------------------------


def test_the_pin_request_names_the_fill_and_every_window_once(monkeypatch) -> None:
    job = background_job(monkeypatch, pinned=(PHOTO,))
    _open_windows(monkeypatch)
    set_client_protocol(31)
    payload = gj.EditorCommitRequest(
        base_generation="first",
        speaker_background=_document(PHOTO)
        | {
            "windows": [_entry(SECOND, 0.0, 2.0), _entry(PHOTO, 2.0, 4.0), _entry(SECOND, 4.0, 6.0)]
        },
    )
    assert background_pin_requests(job, "subtitled", payload) == [
        (PHOTO.media_id, "image"),
        (SECOND.media_id, "image"),
    ]
    assert background_pin_request(job, "subtitled", payload) == (PHOTO.media_id, "image")
    # A closed windows gate pins the fill only: the validator refuses the rest, nothing to download.
    _open_windows(monkeypatch, token=False)
    assert background_pin_requests(job, "subtitled", payload) == [(PHOTO.media_id, "image")]
    # Clears and untouched Saves pin nothing.
    assert (
        background_pin_requests(job, "subtitled", gj.EditorCommitRequest(base_generation="x")) == []
    )
    cleared = gj.EditorCommitRequest(base_generation="x", speaker_background=None)
    assert background_pin_requests(job, "subtitled", cleared) == []


# --- capability: present only where the phone can draw it --------------------------------------


@pytest.mark.parametrize(
    ("protocol", "token", "expected"),
    [(31, True, True), (30, True, False), (None, True, False), (31, False, False)],
)
def test_the_windows_capability_only_while_the_gate_is_open(
    monkeypatch, protocol, token, expected
) -> None:
    job = background_job(monkeypatch)
    _open_windows(monkeypatch, token=token)
    set_client_protocol(protocol)
    capabilities = gj._editor_capabilities(job, _variant(job))
    assert capabilities.get("speaker_background_windows") is (True if expected else None)
    # The first cut's capability is independent of the new one.
    if protocol is not None and protocol >= 27:
        assert capabilities["speaker_background"] is True


def test_the_windows_capability_needs_the_base_capability(monkeypatch) -> None:
    job = background_job(monkeypatch)
    _open_windows(monkeypatch)
    set_client_protocol(31)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    capabilities = gj._editor_capabilities(job, _variant(job))
    assert "speaker_background_windows" not in capabilities
    assert "speaker_background" not in capabilities
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    for extra in ({"render_destination": "cloud"}, {"editor_timeline_mode": "authored"}):
        assert "speaker_background_windows" not in gj._editor_capabilities(
            job, {**_variant(job), **extra}
        )
    assert gj._speaker_background_windows_capability(job, _capability_variant("device")) is True
    assert gj._speaker_background_windows_capability(job, _capability_variant("cloud")) is False
