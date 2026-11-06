"""Qendresa Q3 case 4: "Mute my sound" is promised on the draft, then refused after render.

Ported from the kit (`kria-qendresa-test/tests/bugs/test_q3_mute_refused_after_promise.py`). The
brief records her whole-video mute as `audio/global {"clip_sound": "off"}`; the Main Creator picks
`audio_strategy: "original_audio"` when the catalog has no song, and the render contract pinned
`original_audio="require"` from that strategy alone. The phone writer's sound lane built the muted
recipe, the contract refused it ("This edit needs audible camera audio") and the lane stepped
back to the unmuted plan, while the draft had already said "Clip sound off".

The end-to-end tests run the real phone writer (`_run_generative_job`) on the repo's KRI-942
fixture, mocking only DB and storage, exactly like `test_kri942_clip_sound_toggle.py`.
Failure modes: docs/reviews/qendresa-says-done-b-failure-modes.md (case 4).
"""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import PlanFacts, check_requirement
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    build_render_contract,
    read_render_contract,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_status
from app.services.length_policy_state import read_length_policy, stamp_length_policy
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import generative_build as gb
from tests.tasks import test_kri942_clip_sound_toggle as kri942

TEXT = "The prompt I run at the end of every Claude session"
MUTE = BriefRequirement(
    id="r5",
    kind="audio",
    scope="global",
    description="mute my sound",
    facts={"clip_sound": "off"},
)
STRATEGY = {
    "direction": "fast_montage",
    "edit_format": "montage",
    "audio_strategy": "original_audio",
    "story_structure": ["she sits down", "she starts working"],
    "caption_style": "none",
    "pacing": "balanced",
    "target_duration_s": 11,
    "render_program": "guided",
    "selected_media_ids": [],
    "optional_treatments": [],
    "rationale": "one continuous shot",
    "pinned_texts": [{"text": TEXT, "corner": "top_center"}],
}


def test_a_promised_mute_is_not_contradicted_by_the_render_contract() -> None:
    brief = CreativeBrief(version=1, requirements=[MUTE])
    contract = build_render_contract(STRATEGY, generation_id="g1", brief=brief)
    snapshot = {
        "clip_assignments": [
            {"media_id": "clip-1", "kind": "video", "duration_s": 11.0, "has_audio": True}
        ]
    }
    draft = check_requirement(
        MUTE, PlanFacts(phone_montage=True, media_snapshot=snapshot, clip_ids=("clip-1",))
    )
    promised = draft.status == "met"
    assert not (promised and contract.original_audio == "require"), (
        f"draft says {draft.status}: {draft.reason!r}, but the approved contract pins "
        f"original_audio={contract.original_audio!r}, so the mute is refused after render"
    )


def test_partial_mute_keeps_the_pin() -> None:
    """Some camera audio still plays ("off except the last line"): the pin is not dropped."""
    partial = MUTE.model_copy(update={"facts": dict(kri942.OFF_EXCEPT_LINE)})
    brief = CreativeBrief(version=1, requirements=[partial])
    contract = build_render_contract(STRATEGY, generation_id="g1", brief=brief)
    assert contract.original_audio == "require"


def test_switch_off_keeps_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "creator_mute_wins_enabled", False)
    brief = CreativeBrief(version=1, requirements=[MUTE])
    contract = build_render_contract(STRATEGY, generation_id="g1", brief=brief)
    assert contract.original_audio == "require"


# -- end to end through the real phone writer (repo KRI-942 harness) --------------------

_JOB_STRATEGY = {"audio_strategy": "original_audio"}


def _run_with_contract(
    monkeypatch: pytest.MonkeyPatch, contract: CreatorRenderContract | None, facts: dict
):
    """`kri942._run` with a "clip sound" ask and, optionally, the approval's pinned contract
    (stamped with a length policy, as dispatch does)."""
    bindings = kri942._bindings()
    brief = kri942._brief(facts, description="Mute my sound.")
    snapshot = {
        PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
        "creator_generation_id": "generation",
        "guided_edit": {"approved": True},
        "creator_brief_binding": kri942._binding(brief),
    }
    if contract is not None:
        snapshot[CONTRACT_FIELD] = contract.model_dump(mode="json")
        snapshot = stamp_length_policy(snapshot, _JOB_STRATEGY, contract, mode="log_only")
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        assembly_plan=copy.deepcopy(snapshot),
        status="queued",
        all_candidates={"creator_strategy": dict(_JOB_STRATEGY)},
        error_detail=None,
        failure_reason=None,
    )
    session = Mock()

    @contextmanager
    def sessions():
        yield session

    monkeypatch.setattr(gb, "_sync_session", sessions)
    monkeypatch.setattr(gb, "_lock_owned_entry_job", lambda *args: (job, 3))
    monkeypatch.setattr(
        gb,
        "_guided_execution_plan",
        Mock(return_value=(copy.deepcopy(kri942.DATA["execution_plan"]), None)),
    )
    monkeypatch.setattr(gb.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(gb.settings, "phone_clip_sound_windows_enabled", False)
    monkeypatch.setattr(
        gb.settings, "phone_render_verified_features", [*kri942._FEATURES, "visualBlocks"]
    )
    monkeypatch.setattr(
        gb, "_run_guided_story_job", Mock(side_effect=AssertionError("entered the cloud renderer"))
    )
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", (job.error_detail, job.failure_reason)
    return job


def test_control_whole_video_mute_is_silent_without_a_contract(monkeypatch) -> None:
    job = _run_with_contract(monkeypatch, None, {"clip_sound": "off"})
    assert set(kri942._volumes(job).values()) == {0.0}
    assert CONTRACT_REVISIONS_FIELD not in job.assembly_plan


def test_whole_video_mute_survives_an_original_audio_strategy(monkeypatch) -> None:
    contract = CreatorRenderContract(generation_id="generation", original_audio="require").rebind()
    job = _run_with_contract(monkeypatch, contract, {"clip_sound": "off"})
    volumes = kri942._volumes(job)
    rows = kri942._rows(job)
    assert set(volumes.values()) == {0.0}, (
        f"recipe clip volumes {sorted(set(volumes.values()))}; toggle row "
        f"{ {k: (v.get('status'), v.get('reason_code')) for k, v in rows.items()} }"
    )
    assert rows["r7"]["status"] == "met", rows["r7"]
    # The pin verified the recipe against the relaxed revision, which keeps the length check.
    revision = read_render_contract(
        {CONTRACT_FIELD: job.assembly_plan[CONTRACT_REVISIONS_FIELD]["guided_story"]}
    )
    assert revision is not None and revision.original_audio is None
    record = device_status(job, "guided_story")
    assert record is not None
    assert read_length_policy(job.assembly_plan, revision) is not None


def test_partial_mute_against_a_pinned_contract_is_unchanged(monkeypatch) -> None:
    """ "Off except that last line" keeps camera audio for that line: the pin stays and nothing
    is relaxed (the KRI-942 incident's own result)."""
    contract = CreatorRenderContract(generation_id="generation", original_audio="require").rebind()
    job = _run_with_contract(monkeypatch, contract, dict(kri942.OFF_EXCEPT_LINE))
    assert CONTRACT_REVISIONS_FIELD not in job.assembly_plan
    assert kri942._volumes(job)[kri942.LAST] == 1.0


def test_a_lineage_stamped_contract_is_not_relaxed() -> None:
    """The initial render review requires the pinned contract to be the one the prepared
    execution stamped: such a job keeps the step-down instead of a changed contract digest."""
    from app.kria.clip_sound_toggle_ask import brief_sound_toggle_asks
    from app.kria.prepared_execution import STAMP_FIELD
    from app.services.phone_clip_sound_toggle import ToggleLane, mute_relaxed_contract

    contract = CreatorRenderContract(generation_id="generation", original_audio="require").rebind()
    lane = ToggleLane(
        asks=brief_sound_toggle_asks(CreativeBrief(version=1, requirements=[MUTE])),
        intent=None,  # type: ignore[arg-type]
        candidates=(),
    )
    relaxed = mute_relaxed_contract(contract, lane, {}, variant_id="guided_story")
    assert relaxed is not None and relaxed.original_audio is None
    stamped = {STAMP_FIELD: {"id": "prepared-1", "contract_digest": contract.digest}}
    assert mute_relaxed_contract(contract, lane, stamped, variant_id="guided_story") is None
