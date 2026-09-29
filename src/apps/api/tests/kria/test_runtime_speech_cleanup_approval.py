"""KRI-205: `decide_approval`'s strategy-draft speech-cleanup gating.

`app.kria.runtime._apply_strategy_approval_media` is the unit that applies a
strategy draft's media targets early (at approval time) and, in enforce mode
for a source in the cohort, either validates an aware client's submitted
analysis id/choice (`evaluate_enforce_mode_decision`) or silently picks a
legacy default for a client with no cleanup UI at all. It is exercised
directly here -- the full `decide_approval` lock chain around it is covered
by the (pre-existing, adjusted) mocked tests in `test_runtime_v2.py`, and the
real end-to-end DB wiring by `test_runtime_postgres_integration.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.runtime import (
    RuntimeFailure,
    _apply_strategy_approval_media,
    _restore_strategy_approval_media,
    _restore_strategy_media_snapshot_if_present,
    approval_fingerprint,
)
from app.models import SpeechCleanupAnalysis


def _body(**overrides: object) -> ApprovalDecisionBody:
    defaults: dict[str, object] = dict(
        expected_thread_revision=1,
        expected_draft_revision=1,
        expected_approval_fingerprint="0" * 64,
        speech_cleanup_aware=False,
        speech_cleanup_analysis_id=None,
        speech_cleanup_choice=None,
    )
    defaults.update(overrides)
    return ApprovalDecisionBody(**defaults)


def _item(**overrides: object) -> SimpleNamespace:
    defaults: dict[str, object] = dict(
        id=uuid.uuid4(),
        # Real columns are NOT NULL with server defaults ("montage" / "kria");
        # a snapshot must have a real pre-mutation value to restore to.
        edit_format="montage",
        audio_mode="kria",
        voiceover_gcs_path=None,
        voiceover_caption_style=None,
        user_edited=False,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _thread() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4(), revision=7)


def _row(*, status: str = "ready", candidate_count: int = 2) -> SpeechCleanupAnalysis:
    return SpeechCleanupAnalysis(
        id=uuid.uuid4(),
        plan_item_id=uuid.uuid4(),
        source_kind="voiceover",
        source_media_identity="media-1",
        source_storage_path="users/private/take.wav",
        source_generation="generation-1",
        window_start_s=0.0,
        window_end_s=10.0,
        source_policy_fingerprint="fingerprint-1",
        engine_version="preflight-v1-2026-09-05",
        detector_version="mixed-gap-v1",
        analysis_payload_version="1",
        status=status,
        candidate_count=candidate_count,
    )


def _resolution(fingerprint: str = "fingerprint-1") -> SimpleNamespace:
    return SimpleNamespace(
        source=SimpleNamespace(source_policy_fingerprint=fingerprint),
        reason=None,
        video_present=True,
    )


@pytest.fixture(autouse=True)
def _media_mutation_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the pure media-mutation seam so these tests exercise only the gate."""

    monkeypatch.setattr(
        "app.services.plan_item_media.current_detector_policy", lambda: "policy-token"
    )
    monkeypatch.setattr("app.services.plan_item_media.mutate_plan_item_media", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.mutation_current_analysis_async",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.schedule_item_preflight_async",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.plan_item_media.publish_preflight_after_commit",
        lambda *a, **k: None,
    )


@pytest.mark.asyncio
async def test_voiceover_required_skips_the_gate_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the voiceover_required failure and its copy to the claim, unchanged."""

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    item = _item(voiceover_gcs_path=None)
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})

    result = await _apply_strategy_approval_media(
        db,
        thread=_thread(),
        item=item,
        strategy_payload={"audio_strategy": "voiceover"},
        body=_body(),
        execution=execution,
    )

    assert result.speech_cleanup_stash is None
    assert result.preflight_analysis_id is None
    assert item.user_edited is False
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_mode_off_applies_media_but_never_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "off")
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})

    result = await _apply_strategy_approval_media(
        db,
        thread=_thread(),
        item=item,
        strategy_payload={"audio_strategy": "licensed_music"},
        body=_body(),
        execution=execution,
    )

    assert result.speech_cleanup_stash is None
    assert item.user_edited is True
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_enforced_for_this_source_applies_media_but_never_gates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: SimpleNamespace(source=None, reason=None, video_present=True),
    )
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})

    result = await _apply_strategy_approval_media(
        db,
        thread=_thread(),
        item=item,
        strategy_payload={"audio_strategy": "licensed_music"},
        body=_body(),
        execution=execution,
    )

    assert result.speech_cleanup_stash is None
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_aware_with_no_choice_conflicts_commits_and_raises_ask_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: aware+no choice -> 409 ask_user; the scheduled check is committed."""

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="ready", candidate_count=3)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})
    thread = _thread()

    with pytest.raises(RuntimeFailure) as failure:
        await _apply_strategy_approval_media(
            db,
            thread=thread,
            item=item,
            strategy_payload={"audio_strategy": "licensed_music"},
            body=_body(speech_cleanup_aware=True, speech_cleanup_analysis_id=row.id),
            execution=execution,
        )

    assert failure.value.code == "speech_cleanup_choice_required"
    assert failure.value.status_code == 409
    assert failure.value.phase == "approval"
    assert failure.value.recovery == "ask_user"
    assert failure.value.current_revision == thread.revision
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_aware_with_a_valid_choice_stashes_it_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: aware+valid choice -> approved path continues, stash is returned."""

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="ready", candidate_count=3)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})

    result = await _apply_strategy_approval_media(
        db,
        thread=_thread(),
        item=item,
        strategy_payload={"audio_strategy": "licensed_music"},
        body=_body(
            speech_cleanup_aware=True,
            speech_cleanup_analysis_id=row.id,
            speech_cleanup_choice="keep_original",
        ),
        execution=execution,
    )

    assert result.speech_cleanup_stash == {"analysis_id": str(row.id), "choice": "keep_original"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_aware_ready_with_findings_defaults_to_keep_original(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance: not aware -> legacy default, never blocks an old client."""

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="ready", candidate_count=3)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})

    result = await _apply_strategy_approval_media(
        db,
        thread=_thread(),
        item=item,
        strategy_payload={"audio_strategy": "licensed_music"},
        body=_body(speech_cleanup_aware=False),
        execution=execution,
    )

    assert result.speech_cleanup_stash == {"analysis_id": str(row.id), "choice": "keep_original"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_not_aware_in_flight_analysis_defaults_to_unchecked_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="queued", candidate_count=0)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})

    result = await _apply_strategy_approval_media(
        db,
        thread=_thread(),
        item=item,
        strategy_payload={"audio_strategy": "licensed_music"},
        body=_body(speech_cleanup_aware=False),
        execution=execution,
    )

    assert result.speech_cleanup_stash == {
        "analysis_id": str(row.id),
        "choice": "create_without_cleanup",
    }


@pytest.mark.asyncio
async def test_create_without_cleanup_bypass_accepted_while_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="running", candidate_count=0)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})

    result = await _apply_strategy_approval_media(
        db,
        thread=_thread(),
        item=item,
        strategy_payload={"audio_strategy": "licensed_music"},
        body=_body(
            speech_cleanup_aware=True,
            speech_cleanup_analysis_id=row.id,
            speech_cleanup_choice="create_without_cleanup",
        ),
        execution=execution,
    )

    assert result.speech_cleanup_stash == {
        "analysis_id": str(row.id),
        "choice": "create_without_cleanup",
    }
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_without_cleanup_conflicts_once_the_check_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checked analysis (status "ready") no longer accepts the unchecked bypass."""

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="ready", candidate_count=2)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item()
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})
    thread = _thread()

    with pytest.raises(RuntimeFailure) as failure:
        await _apply_strategy_approval_media(
            db,
            thread=thread,
            item=item,
            strategy_payload={"audio_strategy": "licensed_music"},
            body=_body(
                speech_cleanup_aware=True,
                speech_cleanup_analysis_id=row.id,
                speech_cleanup_choice="create_without_cleanup",
            ),
            execution=execution,
        )

    assert failure.value.code == "speech_cleanup_analysis_changed"
    assert failure.value.recovery == "ask_user"
    db.commit.assert_awaited_once()


def test_fingerprint_is_independent_of_the_new_body_fields() -> None:
    """The three new `ApprovalDecisionBody` fields must never feed the fence hash."""

    approval = SimpleNamespace(
        id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        draft_id=uuid.uuid4(),
        draft_revision=3,
        target_job_id=None,
        target_variant_id=None,
        target_generation_id=None,
        target_manifest_hash=None,
        target_ownership_epoch=1,
        expires_at=datetime.now(UTC),
    )
    unaware = approval_fingerprint(approval)
    # Two request bodies differing ONLY in the new fields must not be able to
    # change the fingerprint -- it is computed from `approval` alone.
    _body(speech_cleanup_aware=False)
    _body(speech_cleanup_aware=True, speech_cleanup_choice="clean")
    assert approval_fingerprint(approval) == unaware


# ---------------------------------------------------------------------------
# The deny-must-not-strand-the-item bug: a strategy approval that commits its
# media mutation, then 409s on the speech-cleanup gate, must not leave the
# PlanItem on a direction the creator can still reject via deny.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conflict_snapshots_pre_mutation_media_onto_the_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 still commits -- the snapshot of the PRE-mutation item must ride along."""

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="ready", candidate_count=3)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item(edit_format="montage", audio_mode="kria", user_edited=False)
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})
    thread = _thread()

    with pytest.raises(RuntimeFailure):
        await _apply_strategy_approval_media(
            db,
            thread=thread,
            item=item,
            strategy_payload={"audio_strategy": "licensed_music"},
            body=_body(speech_cleanup_aware=True, speech_cleanup_analysis_id=row.id),
            execution=execution,
        )

    # The mutation DID happen (audio_strategy="licensed_music" -> "kria" is a
    # no-op here, but edit_format/user_edited change) ...
    assert item.user_edited is True
    # ... and the snapshot captured the item's state from BEFORE that.
    assert execution.result["strategy_media_before"] == {
        "edit_format": "montage",
        "audio_mode": "kria",
        "voiceover_caption_style": None,
        "user_edited": False,
    }


@pytest.mark.asyncio
async def test_double_conflict_keeps_the_original_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second 409 retry must not overwrite the snapshot with the already
    -mutated (rejected-strategy) values -- only the FIRST attempt's pre
    -mutation state is the creator's real baseline.
    """

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="ready", candidate_count=3)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item(edit_format="montage", audio_mode="kria", user_edited=False)
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})
    thread = _thread()

    for _attempt in range(2):
        with pytest.raises(RuntimeFailure):
            await _apply_strategy_approval_media(
                db,
                thread=thread,
                item=item,
                strategy_payload={"audio_strategy": "licensed_music"},
                body=_body(speech_cleanup_aware=True, speech_cleanup_analysis_id=row.id),
                execution=execution,
            )

    assert execution.result["strategy_media_before"] == {
        "edit_format": "montage",
        "audio_mode": "kria",
        "voiceover_caption_style": None,
        "user_edited": False,
    }


@pytest.mark.asyncio
async def test_successful_approve_clears_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Approve→409→approve-with-choice: the strategy values are kept, snapshot dropped."""

    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    row = _row(status="ready", candidate_count=3)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *a, **k: _resolution(),
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async",
        AsyncMock(return_value=row),
    )
    item = _item(edit_format="montage", audio_mode="kria", user_edited=False)
    db = SimpleNamespace(commit=AsyncMock())
    execution = SimpleNamespace(result={})
    thread = _thread()

    with pytest.raises(RuntimeFailure):
        await _apply_strategy_approval_media(
            db,
            thread=thread,
            item=item,
            strategy_payload={"audio_strategy": "licensed_music"},
            body=_body(speech_cleanup_aware=True, speech_cleanup_analysis_id=row.id),
            execution=execution,
        )
    assert "strategy_media_before" in execution.result

    result = await _apply_strategy_approval_media(
        db,
        thread=thread,
        item=item,
        strategy_payload={"audio_strategy": "licensed_music"},
        body=_body(
            speech_cleanup_aware=True,
            speech_cleanup_analysis_id=row.id,
            speech_cleanup_choice="keep_original",
        ),
        execution=execution,
    )

    assert result.speech_cleanup_stash == {"analysis_id": str(row.id), "choice": "keep_original"}
    # The strategy's values are kept on the item ...
    assert item.user_edited is True
    # ... and the reversal snapshot is gone: there is nothing left to restore.
    assert "strategy_media_before" not in execution.result


@pytest.mark.asyncio
async def test_restore_reverts_fields_and_reschedules_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deny must put the item back exactly where it was before the rejected strategy."""

    monkeypatch.setattr(
        "app.services.plan_item_media.current_detector_policy", lambda: "policy-token"
    )
    applied: dict[str, object] = {}

    def _fake_mutate(item, *, detector_policy, edit_format, audio_mode, current_analysis):  # noqa: ANN001
        applied["edit_format"] = edit_format
        applied["audio_mode"] = audio_mode
        item.edit_format = edit_format
        item.audio_mode = audio_mode

    monkeypatch.setattr("app.services.plan_item_media.mutate_plan_item_media", _fake_mutate)
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.mutation_current_analysis_async",
        AsyncMock(return_value=None),
    )
    scheduled_id = uuid.uuid4()
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.schedule_item_preflight_async",
        AsyncMock(return_value=scheduled_id),
    )
    item = _item(edit_format="narrated", audio_mode="voiceover", user_edited=True)
    db = SimpleNamespace()

    result = await _restore_strategy_approval_media(
        db,
        item=item,
        snapshot={
            "edit_format": "montage",
            "audio_mode": "kria",
            "voiceover_caption_style": None,
            "user_edited": False,
        },
    )

    assert applied == {"edit_format": "montage", "audio_mode": "kria"}
    assert item.edit_format == "montage"
    assert item.audio_mode == "kria"
    assert item.voiceover_caption_style is None
    assert item.user_edited is False
    assert result == scheduled_id


@pytest.mark.asyncio
async def test_restore_snapshot_if_present_is_a_noop_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    db = SimpleNamespace()

    assert await _restore_strategy_media_snapshot_if_present(db, item=item, execution=None) is None
    assert (
        await _restore_strategy_media_snapshot_if_present(
            db, item=item, execution=SimpleNamespace(result={})
        )
        is None
    )
    assert (
        await _restore_strategy_media_snapshot_if_present(
            db, item=None, execution=SimpleNamespace(result={"strategy_media_before": {}})
        )
        is None
    )
