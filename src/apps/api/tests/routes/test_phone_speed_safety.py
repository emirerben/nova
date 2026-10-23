"""Route-level safety fences for phone guided playback speed.

These use the normal device guided-story builder and atomic editor Save. They
pin cases where another timeline clock exists: the speed control must be
closed, and a stale client cannot smuggle a rate change through Save.
"""

from __future__ import annotations

import copy

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter

from app.agents._schemas.visual_block import VisualBlock
from app.routes import generative_jobs as gj
from app.schemas.guided_edit_revision import normalize_guided_editor_revision
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.routes._editor_save_matrix import VARIANT, order_required_job


def _enable_speed(monkeypatch) -> None:
    features = list(gj.settings.phone_render_verified_features)
    monkeypatch.setattr(
        gj.settings,
        "phone_render_verified_features",
        [*features, *([] if "variableSpeed" in features else ["variableSpeed"])],
    )


def _speed_payload(job, variant, rate: float):
    """The chat/native payload, built from the real visible phone timeline."""
    return compile_editor_ops(
        job,
        variant,
        [
            {
                "op": "patch_slots",
                "selector": {"slot_indexes": [0]},
                "patch": {"playback_rate": rate},
            }
        ],
    ).payload


def _saved_revision(job, variant) -> dict:
    """Persist the initial canonical revision so lane fixtures are nested there."""
    revision = gj._guided_v2_revision(job, variant)
    assert revision is not None
    variant["guided_edit_revision"] = copy.deepcopy(revision)
    return variant["guided_edit_revision"]


def _caption_lane(_job, variant) -> None:
    variant["caption_cues"] = [{"text": "caption", "start_s": 0, "end_s": 1}]


def _narration(job, _variant) -> None:
    job.assembly_plan["guided_story_execution_plan"]["narration"] = {"duration_s": 30}


def _lipsync(job, _variant) -> None:
    job.assembly_plan["guided_story_execution_plan"]["user_song"] = {
        "mode": "lipsync",
        "plan_item_id": "item",
        "generation": 1,
        "duration_s": 60,
        "window_start_s": 0,
        "window_end_s": 30,
        "takes": {},
    }


def _revision_lane(name: str):
    def apply(job, variant) -> None:
        revision = _saved_revision(job, variant)
        revision[name] = [{"id": f"{name}-1"}]
        # A real Save recomputes the canonical revision hash with every lane
        # write. Keep this fixture equally valid so capability lookup observes
        # the actual persisted lane rather than falling back to approval state.
        revision["state_hash"] = ""
        variant["guided_edit_revision"] = normalize_guided_editor_revision(revision)

    return apply


_CLOCK_BLOCKERS = [
    pytest.param(_caption_lane, id="captions"),
    pytest.param(_narration, id="narration"),
    pytest.param(_lipsync, id="lipsync"),
    pytest.param(_revision_lane("media_overlays"), id="media_overlays"),
    pytest.param(_revision_lane("visual_blocks"), id="visual_blocks"),
    pytest.param(_revision_lane("motion_scenes"), id="motion_scenes"),
    # Camera effects are serialized as the guided revision's custom-effects lane.
    pytest.param(_revision_lane("custom_effects"), id="camera_effects"),
    pytest.param(_revision_lane("sound_effects"), id="sound_effects"),
    pytest.param(_revision_lane("drawings"), id="drawings"),
]


def test_phone_guided_speed_opens_for_a_normal_verified_timeline(monkeypatch):
    job, variant, _ = order_required_job(monkeypatch, order_required=False)
    _enable_speed(monkeypatch)

    snapshot = build_editor_snapshot(job, variant)

    assert snapshot["timeline_patch_capabilities"]["playback_rate"] is True
    gj.prepare_editor_commit(job, VARIANT, _speed_payload(job, variant, 2))


@pytest.mark.parametrize("apply_blocker", _CLOCK_BLOCKERS)
def test_phone_guided_speed_closes_when_a_second_clock_is_present(monkeypatch, apply_blocker):
    job, variant, _ = order_required_job(monkeypatch, order_required=False)
    _enable_speed(monkeypatch)
    apply_blocker(job, variant)

    snapshot = build_editor_snapshot(job, variant)

    assert snapshot["timeline_patch_capabilities"]["playback_rate"] is False


@pytest.mark.parametrize("apply_blocker", _CLOCK_BLOCKERS)
@pytest.mark.parametrize("starting_rate,target_rate", [(1.0, 2.0), (2.0, 1.0)])
def test_phone_guided_speed_save_is_atomic_when_a_clock_is_present(
    monkeypatch, apply_blocker, starting_rate, target_rate
):
    """A stale client cannot bypass a newly closed capability, including reset."""
    job, variant, _ = order_required_job(monkeypatch, order_required=False)
    _enable_speed(monkeypatch)

    if starting_rate != 1:
        gj.prepare_editor_commit(job, VARIANT, _speed_payload(job, variant, starting_rate))
        variant = job.assembly_plan["variants"][0]
    # A client can have compiled this request just before another timed lane
    # became present. The server must still reject it at Save time.
    payload = _speed_payload(job, variant, target_rate)
    apply_blocker(job, variant)
    before = copy.deepcopy(job.assembly_plan)

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, VARIANT, payload)

    assert exc.value.status_code == 422
    assert job.assembly_plan == before


def test_phone_guided_save_rejects_a_new_visual_lane_added_with_speed(monkeypatch):
    """A single atomic request cannot add a timed lane and retime footage together."""
    job, variant, _ = order_required_job(monkeypatch, order_required=False)
    _enable_speed(monkeypatch)
    payload = _speed_payload(job, variant, 2)
    # The route sees this as a simultaneous addition, not just persisted state.
    payload.visual_blocks = [
        TypeAdapter(VisualBlock).validate_python(
            {
                "id": "new-card",
                "kind": "text_card",
                "start_s": 0,
                "end_s": 1,
                "background": {"type": "solid", "color": "#111111"},
            }
        )
    ]
    before = copy.deepcopy(job.assembly_plan)

    with pytest.raises(HTTPException) as exc:
        gj.prepare_editor_commit(job, VARIANT, payload)

    assert exc.value.status_code == 422
    assert job.assembly_plan == before
