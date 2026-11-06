"""Qendresa item 18, review fixes (findings 1, 2 server half): the Save baseline for the focus
gate is the document the editor shows (stored, else the one projected from the pinned recipe),
and a recording pinned for the first time carries the focus measured while its file was on disk."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.pipeline.phone_subtitled_lanes import SubtitledBackgroundFocus
from app.services.client_protocol import clear_request_context, set_client_protocol
from tests.routes.test_background_focus_editor import (
    CORNER,
    LEFT,
    PHOTO,
    POOL,
    RIGHT,
    SECOND,
    _aimed,
    _layer_focus,
    _open,
    _window,
    focus_job,
)
from tests.routes.test_phone_subtitled_editor_commit import _resave, save
from tests.routes.test_speaker_background_editor import (
    _background,
    _document,
    _open_gate,
    _recipe,
    _variant,
    background_job,
)


@pytest.fixture(autouse=True)
def _ctx():
    clear_request_context()
    yield
    clear_request_context()


def _never_saved_aimed_job(monkeypatch):
    from app.services import phone_rollout

    set_client_protocol(42)
    with monkeypatch.context() as early:
        early.setattr(
            phone_rollout, "background_focus_unavailable_reason", lambda **_k: None, raising=False
        )
        job = background_job(
            monkeypatch,
            background=_background(PHOTO, focus=SubtitledBackgroundFocus(**LEFT)),
            pinned=POOL,
        )
    return job


# --- finding 1: baseline = what the editor shows ----------------------------------------------


def test_rollback_echo_on_never_saved_variant_is_not_a_set(monkeypatch) -> None:
    from app.services.phone_subtitled_editor import project_phone_subtitled_editor_sections

    job = _never_saved_aimed_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(42)
    shown = project_phone_subtitled_editor_sections(job.assembly_plan, _variant(job))[
        "speaker_background"
    ]
    assert shown["focus"] == LEFT
    edited = dict(shown) | {"media_id": SECOND.media_id}
    save(job, speaker_background=edited)  # 200, no refusal
    # The gate is closed, so the compile leaves the focus out.
    assert _layer_focus(job)[0] is None
    assert _recipe(job).speaker_background.asset_id.endswith(SECOND.media_id)


def test_a_new_window_of_the_same_media_inheriting_the_aim_is_not_a_set(monkeypatch) -> None:
    job = focus_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(42)
    # Windows are shifted by index: the first stored window is dropped, a new one of the same
    # media carrying the same aim is added in front.
    _resave(
        job,
        speaker_background=_document(PHOTO)
        | {
            "focus": _aimed()["focus"],
            "windows": [
                _window(PHOTO, 0.0, 1.5, focus=LEFT),
                _window(PHOTO, 1.5, 3.0, focus=LEFT),
            ],
            "speaker_frame": CORNER,
        },
    )


def test_deleting_a_window_is_not_a_set(monkeypatch) -> None:
    job = focus_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(42)
    # The remaining window moves to index 0: compared by media, not by index.
    _resave(
        job,
        speaker_background=_aimed()
        | {"windows": [_window(SECOND, 3.0, 6.0, focus={"x": 1.0, "y": 0.5, "zoom": 0.8})]},
    )


def test_a_genuinely_new_aim_with_the_gate_closed_is_422(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch, flag=False)
    set_client_protocol(42)
    with pytest.raises(HTTPException) as dark:
        save(job, speaker_background=_document(PHOTO) | {"focus": LEFT})
    assert dark.value.status_code == 422
    assert dark.value.detail["code"] == "background_focus_disabled"


def test_a_different_aim_on_a_stored_media_with_the_gate_closed_is_422(monkeypatch) -> None:
    job = focus_job(monkeypatch)
    _open(monkeypatch, flag=False)
    set_client_protocol(42)
    with pytest.raises(HTTPException) as dark:
        _resave(job, speaker_background=_aimed(focus={"x": 0.5, "y": 0.5, "zoom": 1.1}))
    assert dark.value.status_code == 422


def test_build_38_setting_one_is_426(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(41)
    with pytest.raises(HTTPException) as refused:
        save(job, speaker_background=_document(PHOTO) | {"focus": LEFT})
    assert refused.value.status_code == 426


# --- finding 2: a recording pinned for the first time carries the focus measured on pinning ---


def _measured() -> SubtitledBackgroundFocus:
    from app.services.background_text_focus import focus_for_text_column

    focus = focus_for_text_column(_column(), source_w=1920, source_h=1080)
    return SubtitledBackgroundFocus(x=focus.x, y=focus.y, zoom=focus.zoom)


def _column():
    from app.services.background_text_focus import TextColumn

    return TextColumn(
        left=0.18, right=0.60, line_pitch=26 / 1080, source_width=1920, source_height=1080
    )


def _two_window_save(**second):
    return _document(PHOTO) | {
        "windows": [_window(PHOTO, 0.0, 3.0), _window(SECOND, 3.0, 6.0, **second)],
        "speaker_frame": CORNER,
    }


def test_a_recording_pinned_in_this_request_is_stored_and_compiled_with_its_measured_focus(
    monkeypatch,
) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(42)
    job._kria_new_background_columns = {SECOND.media_id: _column()}
    save(job, speaker_background=_two_window_save())
    focus = _layer_focus(job)
    assert focus[0] is None  # the fill (an already pinned recording) stays centred
    assert focus[1][0] is None
    assert focus[1][1] == _measured().model_dump(mode="json")
    assert "backgroundFocus" in _recipe(job).required_capabilities


def test_a_creators_own_focus_beats_the_measured_one(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(42)
    job._kria_new_background_columns = {SECOND.media_id: _column()}
    save(job, speaker_background=_two_window_save(focus=RIGHT))
    assert _layer_focus(job)[1][1] == RIGHT


def test_an_already_pinned_recording_saved_without_a_focus_stays_centred(monkeypatch) -> None:
    job = background_job(monkeypatch, background=_background(PHOTO), pinned=POOL)
    _open(monkeypatch)
    set_client_protocol(42)
    save(job, speaker_background=_two_window_save())  # nothing was pinned in this request
    assert _layer_focus(job) == (None, [None, None])


@pytest.mark.asyncio
async def test_pinning_records_the_measured_focus_only_for_what_it_newly_pinned(
    monkeypatch,
) -> None:
    import uuid
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.services import phone_visuals
    from app.services.phone_speaker_background import (
        new_background_focus,
        pin_pool_visual_for_background,
    )

    item = uuid.uuid4()
    asset = uuid.uuid4()
    binding = SECOND.model_copy(update={"media_id": str(asset), "text_column": _column()})
    seen = {}

    def bind(_open, *, job_id, pins, measure_text=False):
        seen["measure_text"] = measure_text
        return (binding,)

    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", bind)
    _open_gate(monkeypatch)
    _open(monkeypatch)
    set_client_protocol(42)
    row = SimpleNamespace(
        id=asset,
        status="ready",
        kind="image",
        gcs_path=f"users/u/plan/{item}/pool/a.jpg",
        gcs_generation=7,
    )
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={})
    await pin_pool_visual_for_background(
        db, job, plan_item_id=item, media_id=str(asset), kind="image"
    )
    assert seen["measure_text"] is True
    got = new_background_focus(job, 1080, 1920)[str(asset)]
    assert got == _measured()
    # Already pinned: nothing re-measured, nothing re-aimed.
    job2 = SimpleNamespace(id=job.id, assembly_plan=job.assembly_plan)
    await pin_pool_visual_for_background(
        db, job2, plan_item_id=item, media_id=str(asset), kind="image"
    )
    assert not new_background_focus(job2, 1080, 1920)
