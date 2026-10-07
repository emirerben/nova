"""KRI-187: phone-rendering accounts on Kria runtime v2.

Offline coverage of the seams a phone thread crosses on v2: the capabilities
gate, the strategy approval -> device-job dispatch, the editor approval ->
`prepare_phone_editor_commit` (device) path, and the observer treating
`awaiting_device` as pending until the phone publishes or the record fails.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from app.config import Settings, settings
from app.kria.device_render import make_device_request
from app.kria.recipes import EditRecipeV1
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    PlanItem,
)
from app.services.device_render import (
    device_record,
    mark_device_failed,
    pin_device_request,
    save_device_record,
)
from app.tasks import kria_runtime
from app.tasks.kria_runtime import (
    _claim_approval_dispatch,
    _device_render_state,
    _observe_dispatched_execution,
    execute_kria_approval,
)

_RECIPE = EditRecipeV1.model_validate_json(
    (Path(__file__).resolve().parents[1] / "fixtures/kria_edit_recipe_v1.json").read_text()
)
VARIANT = "guided_story"


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def _device_job(*, revision: int = 1, status: str = "awaiting_device") -> SimpleNamespace:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status=status,
        content_plan_ownership_epoch=1,
        failure_reason=None,
        error_detail=None,
        assembly_plan={
            "variants": [
                {
                    "variant_id": VARIANT,
                    "render_status": "awaiting_device",
                    "render_destination": "device",
                    "render_generation_id": "edit-gen-2",
                }
            ]
        },
    )
    pin_device_request(
        job,
        make_device_request(job_id=job.id, variant_id=VARIANT, revision=revision, recipe=_RECIPE),
        base_generation="edit-gen-2",
    )
    return job


def _publish(job: SimpleNamespace, *, attempt: str = "upload-attempt-1") -> None:
    """Mirror `complete_device_export` (routes/device_render.py)."""
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "published"
    record.update(published_attempt=attempt, base_generation=attempt)
    save_device_record(job, VARIANT, record)
    job.assembly_plan["variants"] = [
        {
            **job.assembly_plan["variants"][0],
            "ok": True,
            "render_status": "ready",
            "render_generation_id": attempt,
            "output_url": "https://example.test/device.mp4",
        }
    ]
    job.status = "variants_ready"


def _execution(*, variant: str | None, minimum: int | None = None) -> SimpleNamespace:
    prep = {"device_recipe_revision": minimum} if minimum is not None else None
    return SimpleNamespace(
        target_variant_id=variant,
        result={"editor_prep": prep} if prep else {},
    )


# ---------------------------------------------------------------- settings / gate


def test_v2_phone_flag_defaults_off_and_allowlist_narrows_it() -> None:
    member, other = uuid.uuid4(), uuid.uuid4()
    assert Settings.model_fields["kria_runtime_v2_phone_enabled"].default is False
    off = Settings(storage_bucket="b", kria_runtime_v2_phone_user_ids=[member])
    assert off.kria_runtime_v2_phone_for(member) is False
    on_everyone = Settings(storage_bucket="b", kria_runtime_v2_phone_enabled=True)
    assert on_everyone.kria_runtime_v2_phone_for(other) is True
    on_listed = Settings(
        storage_bucket="b",
        kria_runtime_v2_phone_enabled=True,
        kria_runtime_v2_phone_user_ids=[member],
    )
    assert on_listed.kria_runtime_v2_phone_for(member) is True
    assert on_listed.kria_runtime_v2_phone_for(other) is False


# ---------------------------------------------------------------- device state


def test_a_cloud_job_is_not_a_device_job() -> None:
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={"variants": []})
    assert _device_render_state(job, _execution(variant=None)) is None


def test_awaiting_device_is_pending_not_failed() -> None:
    job = _device_job()
    assert _device_render_state(job, _execution(variant=VARIANT, minimum=1)) == "pending"
    assert _device_render_state(job, _execution(variant=None)) == "pending"


def test_published_record_is_ready_and_needs_attention_is_failed() -> None:
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="export_failed", detail="")
    assert _device_render_state(job, _execution(variant=VARIANT, minimum=1)) == "failed"
    published = _device_job()
    _publish(published)
    assert _device_render_state(published, _execution(variant=VARIANT, minimum=1)) == "ready"


def test_a_record_older_than_the_pinned_edit_revision_stays_pending() -> None:
    job = _device_job(revision=1)
    _publish(job)  # revision 1 published; the approved edit pinned revision 2
    assert _device_render_state(job, _execution(variant=VARIANT, minimum=2)) == "pending"


# ---------------------------------------------------------------- observer


class _Db:
    """`sync_session` double: `get` by model, `execute` results in call order."""

    def __init__(self, gets: dict, executes: list) -> None:
        self._gets = gets
        self._executes = list(executes)
        self.commit = MagicMock()

    def get(self, model, _identity):  # noqa: ANN001, ANN201
        return self._gets[model]

    def execute(self, _statement):  # noqa: ANN001, ANN201
        row = self._executes.pop(0)
        return SimpleNamespace(scalar_one_or_none=lambda: row)


def _observe(job: SimpleNamespace, execution_fields: dict) -> tuple[str, SimpleNamespace, list]:
    user_id = job.user_id
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=user_id, ownership_epoch=1)
    item = SimpleNamespace(id=uuid.uuid4(), content_plan_id=plan.id, current_job_id=job.id)
    job.content_plan_item_id = item.id
    session = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=user_id,
        plan_item_id=item.id,
        target_job_id=job.id,
        ownership_epoch=1,
        status="rendering",
        target_variant_id=None,
        target_generation_id=None,
        last_good=None,
        last_error=None,
    )
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        runtime_version=2,
        creator_id=user_id,
        active_creator_agent_session_id=session.id,
        active_plan_item_id=item.id,
        active_job_id=None,
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(),
        status="observing",
        completed_at=None,
        error=None,
        observed_event_id=None,
    )
    fields = dict(
        id=uuid.uuid4(),
        status="dispatched",
        target_job_id=job.id,
        turn_id=turn.id,
        session_id=session.id,
        target_thread_id=thread.id,
        target_draft_id=None,
        target_variant_id=None,
        target_generation_id=None,
        result={},
        error=None,
        completed_at=None,
        observed_at=None,
        observed_event_id=None,
    )
    fields.update(execution_fields)
    execution = SimpleNamespace(**fields)
    db = _Db(
        {CreatorAgentExecution: execution, CreatorAgentSession: session, PlanItem: item},
        [plan, item, job, session, turn, execution, thread],
    )
    events: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
        patch.object(kria_runtime, "_promote_queued_successor_sync", return_value=None),
    ):
        outcome, _ = _observe_dispatched_execution(execution.id)
    return outcome, execution, events


def test_observer_keeps_an_awaiting_device_execution_pending() -> None:
    job = _device_job()
    outcome, execution, events = _observe(
        job,
        {"target_variant_id": VARIANT, "result": {"editor_prep": {"device_recipe_revision": 1}}},
    )
    assert outcome == "pending"
    assert execution.status == "dispatched"
    assert events == []


def test_observer_completes_when_the_phone_publishes_under_its_own_attempt_id() -> None:
    job = _device_job()
    _publish(job, attempt="upload-attempt-9")  # != the pinned edit generation
    outcome, execution, events = _observe(
        job,
        {
            "target_variant_id": VARIANT,
            "target_generation_id": "edit-gen-2",
            "result": {"editor_prep": {"device_recipe_revision": 1}},
        },
    )
    assert outcome == "completed"
    assert execution.status == "completed"
    assert execution.result["render_generation_id"] == "upload-attempt-9"
    assert [event["event_type"] for event in events] == ["generation_ready", "assistant_review"]


def test_observer_completes_a_first_render_with_no_pinned_variant() -> None:
    job = _device_job()
    _publish(job)
    outcome, execution, _ = _observe(job, {})
    assert outcome == "completed"
    assert execution.result["variant_id"] == VARIANT


_LEGACY_REVIEW = (
    "The guided story cut is ready. "
    "The approved render finished; review the opening, pacing, and text, "
    "then tell me what you want changed."
)


def _unified_brief(version: int = 3):  # noqa: ANN202
    from app.kria.brief import BriefRequirement, CreativeBrief

    return CreativeBrief(
        version=version,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="landmarks"),
            BriefRequirement(id="r2", kind="timing", scope="global", description="fast"),
        ],
    )


def _observe_unified(job: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, brief) -> list:  # noqa: ANN001
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(kria_runtime, "load_latest_brief_sync", lambda _db, _thread_id: brief)
    _publish(job)
    _outcome, _execution_row, events = _observe(job, {})
    return events


def test_observer_review_carries_the_unified_montage_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _device_job()
    job.assembly_plan["unified_montage"] = {
        "brief_version": 3,
        "requirement_receipts": [
            {
                "requirement_id": "r1",
                "status": "partial",
                "reason": "Text landed on 10 of 14 clips.",
                "inferred": ["Dolmabahce"],
            },
            {
                "requirement_id": "r2",
                "status": "partial",
                "reason": "I can't verify this timing automatically.",
                "inferred": [],
            },
        ],
    }
    events = _observe_unified(job, monkeypatch, _unified_brief())
    review = next(e for e in events if e["event_type"] == "assistant_review")
    assert review["content"].startswith("Not everything you asked for made it in:")
    assert "10 of 14 clips" in review["content"]
    assert "I guessed these, tell me if any is wrong: Dolmabahce" in review["content"]
    # r2's stored "can't verify" judged nothing: no line, and not carried forward.
    assert "fast" not in review["content"]
    assert [r["requirement_id"] for r in review["payload"]["requirement_receipts"]] == ["r1"]


def test_observer_review_ignores_receipts_from_an_older_brief_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _device_job()
    job.assembly_plan["unified_montage"] = {
        "brief_version": 2,
        "requirement_receipts": [
            {"requirement_id": "r1", "status": "partial", "reason": "x", "inferred": []}
        ],
    }
    events = _observe_unified(job, monkeypatch, _unified_brief(version=3))
    review = next(e for e in events if e["event_type"] == "assistant_review")
    assert review["content"] == _LEGACY_REVIEW
    assert "requirement_receipts" not in review["payload"]


def test_observer_review_is_unchanged_for_a_job_with_no_unified_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _device_job()
    events = _observe_unified(job, monkeypatch, _unified_brief())
    review = next(e for e in events if e["event_type"] == "assistant_review")
    assert review["content"] == _LEGACY_REVIEW
    assert "requirement_receipts" not in review["payload"]


def test_observer_fails_a_device_render_the_phone_gave_up_on() -> None:
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="thermal", detail="")
    assert job.status == "awaiting_device"  # Job.status never moves on a device failure
    outcome, execution, events = _observe(
        job,
        {"target_variant_id": VARIANT, "result": {"editor_prep": {"device_recipe_revision": 1}}},
    )
    assert outcome == "failed"
    assert execution.status == "failed"
    assert execution.error["code"] == "device_render_failed"
    assert events[-1]["event_type"] == "assistant_render_failed"


def test_observer_keeps_bound_recovery_message_without_extracted_requirements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pinned raw request can require a recovery before it has ledger rows."""
    job = _device_job(status="processing_failed")
    job.failure_reason = "phone_plan_unsupported"
    job.assembly_plan.pop("_device_render_v1", None)
    job.assembly_plan.update(
        {
            "creator_generation_id": "edit-gen-2",
            "creator_brief_binding": {"saved": "binding"},
            "request_recovery": {
                "message": "I couldn't match the narration. Try a simpler clip sequence?",
                "requirement_receipts": [],
                "brief_version": None,
                "generation_id": "edit-gen-2",
                "binding_digest": "pinned-digest",
            },
        }
    )

    binding = SimpleNamespace(digest="pinned-digest", resolve=lambda _thread_id: None)
    monkeypatch.setattr("app.kria.brief_binding.BriefBinding.model_validate", lambda _raw: binding)
    outcome, _execution, events = _observe(job, {})

    assert outcome == "failed"
    failed = events[-1]
    assert failed["event_type"] == "assistant_render_failed"
    assert failed["content"] == "I couldn't match the narration. Try a simpler clip sequence?"
    assert "requirement_receipts" not in failed["payload"]


# ---------------------------------------------------------------- dispatch


def test_editor_approval_on_a_device_variant_enqueues_no_cloud_render() -> None:
    approval_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    prep = {
        "generation": "edit-gen-2",
        "has_render_section": True,
        "render_destination": "device",
        "device_recipe_revision": 2,
    }
    claim = SimpleNamespace(
        draft_kind="editor",
        target_job_id=job_id,
        target_variant_id=VARIANT,
        target_generation_id="edit-gen-2",
        editor_prep=prep,
    )
    with (
        patch("app.tasks.kria_runtime._claim_approval_dispatch", return_value=claim),
        patch("app.tasks.kria_runtime.enqueue_editor_commit_render") as enqueue,
        patch(
            "app.tasks.kria_runtime._finish_approval_dispatch", return_value=("dispatched", None)
        ) as finish,
    ):
        result = execute_kria_approval.run(approval_id)
    enqueue.assert_not_called()
    assert result == {"approval_id": approval_id, "status": "dispatched", "job_id": str(job_id)}
    finish.assert_called_once_with(claim, outcome="dispatched", job_id=str(job_id))


# ---------------------------------------------------------------- claim


def _claim_fixture(job: SimpleNamespace) -> tuple[_Db, SimpleNamespace, SimpleNamespace, list]:
    user_id = job.user_id
    # Approve the current variant: phone publication replaces the edit token
    # with its upload-attempt ID, which is also the next editor draft's baseline.
    variant = next(row for row in job.assembly_plan["variants"] if row["variant_id"] == VARIANT)
    generation = kria_runtime.variant_render_baseline(variant)
    approval_id, execution_id = uuid.uuid4(), uuid.uuid4()
    plan = SimpleNamespace(id=uuid.uuid4(), user_id=user_id, ownership_epoch=1)
    item = SimpleNamespace(id=uuid.uuid4(), content_plan_id=plan.id, current_job_id=job.id)
    session = SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item.id,
        ownership_epoch=1,
        target_job_id=job.id,
        target_variant_id=VARIANT,
        target_generation_id=generation,
        manifest_hash="m",
        status="awaiting_approval",
    )
    turn = SimpleNamespace(
        id=uuid.uuid4(), status="awaiting_approval", completed_at=None, error=None
    )
    draft = SimpleNamespace(
        id=uuid.uuid4(), draft_revision=3, is_head=True, snapshot_json={"kind": "editor"}
    )
    approval = SimpleNamespace(
        id=approval_id,
        creator_id=user_id,
        execution_ids=[str(execution_id)],
        turn_id=turn.id,
        session_id=session.id,
        draft_id=draft.id,
        draft_revision=3,
        thread_id=uuid.uuid4(),
        status="approved",
        consumed_at=None,
        target_ownership_epoch=1,
        target_job_id=job.id,
        target_variant_id=VARIANT,
        target_generation_id=generation,
        target_manifest_hash="m",
    )
    thread = SimpleNamespace(
        id=approval.thread_id,
        runtime_version=2,
        creator_id=user_id,
        active_creator_agent_session_id=session.id,
    )
    execution = SimpleNamespace(
        id=execution_id,
        status="awaiting_approval",
        target_draft_id=draft.id,
        target_draft_revision=3,
        target_variant_id=VARIANT,
        target_generation_id=generation,
        result={},
        error=None,
        completed_at=None,
        accepted_at=None,
        external_task_id=None,
    )
    db = _Db(
        {
            CreatorAgentApproval: approval,
            CreatorAgentTurn: turn,
            CreatorAgentSession: session,
            CreatorEditDraft: draft,
            CreationThread: thread,
            PlanItem: item,
        },
        [plan, item, job, session, turn, draft, approval, execution, thread],
    )
    return db, approval, execution, [turn, session, thread]


def _claim(job: SimpleNamespace, prepare) -> tuple[object, SimpleNamespace, SimpleNamespace, list]:  # noqa: ANN001
    db, approval, execution, _ = _claim_fixture(job)
    events: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    document = SimpleNamespace(
        brief_binding=None,
        kind="editor",
        editor_payload={"base_generation": approval.target_generation_id},
        strategy=None,
        intent="x",
    )
    payload = SimpleNamespace(music_track_id=None)
    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
        patch.object(kria_runtime.KriaDraftDocument, "model_validate", return_value=document),
        patch.object(kria_runtime.EditorCommitRequest, "model_validate", return_value=payload),
        patch.object(kria_runtime, "prepare_editor_commit", prepare),
    ):
        claim = _claim_approval_dispatch(approval.id)
    return claim, approval, execution, events


def test_claim_turns_a_refused_phone_edit_into_a_terminal_reply_not_a_crash_loop() -> None:
    job = _device_job(status="variants_ready")
    _publish(job)

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise HTTPException(422, detail={"code": "unsupported_phone_edit", "reason": "x"})

    claim, approval, execution, events = _claim(job, refuse)
    assert claim is None
    assert approval.status == "cancelled"
    assert execution.status == "failed"
    assert execution.error["code"] == "unsupported_phone_edit"
    assert events[0]["event_type"] == "assistant_error"
    assert "iPhone" in events[0]["content"]


def test_claim_pins_the_device_recipe_revision_the_edit_created() -> None:
    job = _device_job(status="variants_ready")
    _publish(job)

    def prepare(current_job, variant_id, *_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        # `prepare_phone_editor_commit` pins revision N+1 and flips awaiting_device.
        pin_device_request(
            current_job,
            make_device_request(
                job_id=current_job.id, variant_id=variant_id, revision=2, recipe=_RECIPE
            ),
            base_generation="edit-gen-2",
        )
        return {
            "generation": "edit-gen-2",
            "has_render_section": True,
            "render_destination": "device",
            "sections": {},
        }

    claim, _, execution, _ = _claim(job, prepare)
    assert claim is not None
    assert claim.editor_prep["device_recipe_revision"] == 2
    assert execution.result["editor_prep"]["device_recipe_revision"] == 2


def test_claim_passes_phone_catalog_sfx_paths_to_the_editor_commit(monkeypatch) -> None:
    """A chat edit that leaves a phone Talking edit's sound lane alone still
    persists its effects' real catalog paths (same read as the iOS Save)."""
    job = _device_job(status="variants_ready")
    _publish(job)
    reads: list = []

    def catalog(db, current_job, variant):  # noqa: ANN001, ANN202
        reads.append((current_job, variant))
        return {"pop": "sound-effects/pop/audio.m4a"}

    monkeypatch.setattr(kria_runtime, "phone_subtitled_sfx_paths_sync", catalog)
    seen: dict = {}

    def prepare(current_job, variant_id, *_args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.update(kwargs)
        pin_device_request(
            current_job,
            make_device_request(
                job_id=current_job.id, variant_id=variant_id, revision=2, recipe=_RECIPE
            ),
            base_generation="edit-gen-2",
        )
        return {
            "generation": "edit-gen-2",
            "has_render_section": True,
            "render_destination": "device",
            "sections": {},
        }

    claim, _, _, _ = _claim(job, prepare)
    assert claim is not None
    assert seen["phone_sfx_catalog_paths"] == {"pop": "sound-effects/pop/audio.m4a"}
    [(read_job, read_variant)] = reads
    assert read_job is job
    assert read_variant["variant_id"] == VARIANT


def test_claim_refuses_when_the_pinned_recipe_cannot_derive_sfx_paths(monkeypatch) -> None:
    """Deriving the catalog paths re-validates the pinned device recipe; a
    recipe that fails there is the same terminal refusal, not a crash loop."""
    job = _device_job(status="variants_ready")
    _publish(job)

    def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise ValueError("pinned recipe no longer validates")

    monkeypatch.setattr(kria_runtime, "phone_subtitled_sfx_paths_sync", broken)

    def prepare(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("the commit must not run after the lane read failed")

    claim, approval, execution, events = _claim(job, prepare)
    assert claim is None
    assert approval.status == "cancelled"
    assert execution.status == "failed"
    assert events[0]["event_type"] == "assistant_error"


def test_claim_still_raises_for_a_cloud_variant_validation_error() -> None:
    job = _device_job()
    job.assembly_plan["variants"][0]["render_destination"] = "cloud"

    def refuse(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise HTTPException(422, detail={"code": "x"})

    with pytest.raises(HTTPException):
        _claim(job, refuse)


# ---------------------------------------------------------------- review round


def test_editor_edit_with_no_render_section_ignores_a_stale_device_record() -> None:
    """A no-render edit pinned no revision; an older failed record is not its failure."""
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="export_failed", detail="")
    execution = SimpleNamespace(
        target_variant_id=VARIANT, result={"editor_prep": {"generation": "g", "sections": {}}}
    )
    assert _device_render_state(job, execution) is None


def test_device_render_failure_points_the_creator_at_the_phone_not_chat_retry() -> None:
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="thermal", detail="")
    _, execution, events = _observe(
        job,
        {"target_variant_id": VARIANT, "result": {"editor_prep": {"device_recipe_revision": 1}}},
    )
    assert execution.error["recovery"] == "manual"
    assert execution.error["retryable"] is False
    failed = events[-1]
    assert failed["payload"]["recovery"] == "manual"
    assert "iPhone" in failed["content"]
    assert "retry without rebuilding" not in failed["content"]


def test_a_cloud_render_failure_keeps_the_chat_retry_recovery() -> None:
    job = _device_job(status="processing_failed")
    job.assembly_plan = {"variants": [{"variant_id": VARIANT}]}  # no device records
    job.failure_reason = "render_failed"
    _, execution, events = _observe(job, {})
    assert execution.error["recovery"] == "retry"
    assert events[-1]["payload"]["recovery"] == "retry"
    assert execution.error["retryable"] is True


def test_a_deterministic_phone_plan_reject_asks_the_user_instead_of_retrying() -> None:
    from app.tasks.content_plan_build import humanize_job_failure_reason

    job = _device_job(status="processing_failed")
    job.assembly_plan = {"variants": [{"variant_id": VARIANT}]}
    job.failure_reason = "phone_plan_unsupported"
    _, execution, events = _observe(job, {})
    assert execution.error["code"] == "phone_plan_unsupported"
    assert execution.error["recovery"] == "ask_user"
    assert execution.error["retryable"] is False
    assert events[-1]["payload"]["recovery"] == "ask_user"
    assert events[-1]["content"] == humanize_job_failure_reason("phone_plan_unsupported")


def test_a_song_plan_decline_shows_its_own_detail_and_asks_the_user() -> None:
    job = _device_job(status="processing_failed")
    job.assembly_plan = {"variants": [{"variant_id": VARIANT}]}
    job.failure_reason = "user_song_plan_declined"
    job.error_detail = "Your song was replaced after this edit was approved. Ask for it again."
    _, execution, events = _observe(job, {})
    assert execution.error["code"] == "user_song_plan_declined"
    assert execution.error["recovery"] == "ask_user"
    assert execution.error["retryable"] is False
    assert events[-1]["content"] == job.error_detail
    assert "iPhone" not in events[-1]["content"]


def test_a_song_plan_decline_without_detail_uses_the_song_copy() -> None:
    from app.tasks.content_plan_build import JOB_FAILURE_MESSAGES

    job = _device_job(status="processing_failed")
    job.assembly_plan = {"variants": [{"variant_id": VARIANT}]}
    job.failure_reason = "user_song_plan_declined"
    job.error_detail = None
    _, _execution, events = _observe(job, {})
    assert events[-1]["content"] == JOB_FAILURE_MESSAGES["user_song_plan_declined"]
    assert "song" in events[-1]["content"]


def test_observer_review_notes_a_background_fallback_and_kept_broll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _device_job()
    job.assembly_plan["unified_montage"] = {
        "user_song": {
            "mode": "background",
            "requested_mode": "lipsync",
            "fallback_reason": "no_synced_takes",
            "kept_broll_ids": ["a", "b"],
            "low_confidence_ids": ["c"],
            "placed_outside_ids": ["d", "e"],
            "placed": [{"media_id": "c", "method": "lyrics"}],
        }
    }
    events = _observe_unified(job, monkeypatch, _unified_brief())
    review = next(e for e in events if e["event_type"] == "assistant_review")
    assert review["content"].startswith(_LEGACY_REVIEW)
    assert "used it as background music cut to the beat" in review["content"]
    assert "2 takes have no usable singing or words" in review["content"]
    assert "1 take is placed by my best guess and may be slightly off" in review["content"]
    assert "2 takes sit later in the song than a 2-minute video can hold" in review["content"]
    assert "I matched 1 take by your singing." in review["content"]


def test_a_phone_capability_reject_stays_retryable() -> None:
    """KRI-286: a capability the device has not verified yet is a rollout decision,
    not a plan defect -- the same edit works after the flag flips, so retry stays open."""
    from app.tasks.content_plan_build import humanize_job_failure_reason
    from app.tasks.kria_runtime import _DETERMINISTIC_JOB_FAILURE_CODES

    assert "phone_capability_unavailable" not in _DETERMINISTIC_JOB_FAILURE_CODES
    job = _device_job(status="processing_failed")
    job.assembly_plan = {"variants": [{"variant_id": VARIANT}]}
    job.failure_reason = "phone_capability_unavailable"
    _, execution, events = _observe(job, {})
    assert execution.error["code"] == "phone_capability_unavailable"
    assert execution.error["recovery"] == "retry"
    assert execution.error["retryable"] is True
    assert events[-1]["payload"]["recovery"] == "retry"
    # The chat copy says WHY (code-specific), not the generic "didn't finish".
    assert events[-1]["content"] == humanize_job_failure_reason("phone_capability_unavailable")
    assert events[-1]["content"] != humanize_job_failure_reason("phone_plan_unsupported")


def test_claim_reports_a_baseline_conflict_as_a_stale_video_not_an_unsupported_edit() -> None:
    job = _device_job(status="variants_ready")
    _publish(job)

    def conflict(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise HTTPException(409, detail={"code": "baseline_conflict"})

    claim, _, execution, events = _claim(job, conflict)
    assert claim is None
    assert execution.error["code"] == "baseline_conflict"
    assert "changed" in events[0]["content"]
    assert "can't be rendered" not in events[0]["content"]


def test_a_refused_phone_dispatch_is_not_a_retry_loop() -> None:
    job = _device_job()
    db, _approval, execution, _rows = _claim_fixture(job)
    execution.status = "accepted"
    session = db._gets[CreatorAgentSession]
    turn = db._gets[CreatorAgentTurn]
    approval = db._gets[CreatorAgentApproval]
    thread = db._gets[CreationThread]
    session.render_attempts = 0
    session.last_error = None
    db._executes = [session, turn, approval, execution, thread]
    claim = SimpleNamespace(
        session_id=session.id,
        turn_id=turn.id,
        approval_id=approval.id,
        execution_id=execution.id,
        thread_id=thread.id,
        target_variant_id=None,
        target_generation_id=None,
    )
    events: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
        patch.object(kria_runtime, "_promote_queued_successor_sync", return_value=None),
    ):
        status, _ = kria_runtime._finish_approval_dispatch(
            claim, outcome="invalid_clips", job_id=None, reason="unsupported_format"
        )
    assert status == "failed"
    assert execution.error["reason"] == "unsupported_format"
    assert execution.error["retryable"] is False
    assert execution.error["recovery"] == "ask_user"
    assert events[-1]["payload"]["recovery"] == "ask_user"
    assert "retry without" not in events[-1]["content"]


def _finish_refused_dispatch(outcome: str) -> tuple[SimpleNamespace, list]:
    job = _device_job()
    db, _approval, execution, _rows = _claim_fixture(job)
    execution.status = "accepted"
    session = db._gets[CreatorAgentSession]
    turn = db._gets[CreatorAgentTurn]
    approval = db._gets[CreatorAgentApproval]
    thread = db._gets[CreationThread]
    session.render_attempts = 0
    session.last_error = None
    db._executes = [session, turn, approval, execution, thread]
    claim = SimpleNamespace(
        session_id=session.id,
        turn_id=turn.id,
        approval_id=approval.id,
        execution_id=execution.id,
        thread_id=thread.id,
        target_variant_id=None,
        target_generation_id=None,
    )
    events: list = []

    def append(_db, _thread, **kwargs):  # noqa: ANN001, ANN202
        events.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    @contextmanager
    def sessions():  # noqa: ANN202
        yield db

    with (
        patch.object(kria_runtime, "sync_session", sessions),
        patch.object(kria_runtime, "_append_sync_event", append),
        patch.object(kria_runtime, "_promote_queued_successor_sync", return_value=None),
    ):
        status, _ = kria_runtime._finish_approval_dispatch(claim, outcome=outcome, job_id=None)
    assert status == "failed"
    return execution, events


@pytest.mark.parametrize(
    ("outcome", "says"),
    [
        # KRI-217: a montage lane that cannot place Visuals (cloud runtime v2,
        # or a Visual kind the phone cannot draw) refuses the same way on every
        # retry, so the reply says how to get unstuck.
        ("guided_edit_bypass_unsafe", "Remove them from Visuals"),
        ("visuals_processing", "still being prepared"),
    ],
)
def test_a_visuals_refusal_says_what_to_do_instead_of_retry(outcome: str, says: str) -> None:
    execution, events = _finish_refused_dispatch(outcome)

    assert execution.error["outcome"] == outcome
    assert execution.error["retryable"] is False
    assert execution.error["recovery"] == "ask_user"
    assert events[-1]["payload"]["recovery"] == "ask_user"
    assert events[-1]["payload"]["dispatch_outcome"] == outcome
    assert says in events[-1]["content"]
    assert "retry without" not in events[-1]["content"]


def test_an_unknown_dispatch_failure_keeps_the_generic_retry_copy() -> None:
    execution, events = _finish_refused_dispatch("publish_failed")

    assert execution.error["recovery"] == "retry"
    assert "retry without repeating the edit" in events[-1]["content"]


# --- KRI-470 PR-A: typed creator-contract declines reach the creator thread ----------
#
# Failure modes: a typed reason is persisted but the observer still applies the
# failure-code default (dead retry loop for a capability refusal, or an ask where a
# repair/retry is right); the refusal hides the limit or the alternative; a typed
# reason on a stale/unrelated variant leaks into another failure; untyped failures
# change behaviour.


def _declined_job(
    failure_reason: str, *, decline: dict | None = None, detail: str | None = None
) -> SimpleNamespace:
    job = _device_job(status="processing_failed")
    job.assembly_plan = {"variants": [{"variant_id": VARIANT}]}
    if decline is not None:
        job.assembly_plan["creator_decline"] = decline
    job.failure_reason = failure_reason
    job.error_detail = detail
    return job


def test_a_deterministic_phone_decline_stays_an_ask_and_shows_the_alternative() -> None:
    """A phone `phone_plan_unsupported` fails identically on retry: never offer one."""
    job = _declined_job(
        "phone_plan_unsupported",
        decline={
            "decline_reason": "evidence_missing",
            "field_path": "target_duration_s",
            "alternative": "I can rebuild the edit so it shows this, or relax the requirement.",
            "failure_reason": "phone_plan_unsupported",
        },
        detail="This edit couldn't keep the confirmed length.",
    )
    _, execution, events = _observe(job, {})
    assert execution.error == {
        "code": "phone_plan_unsupported",
        "retryable": False,
        "recovery": "ask_user",
        "decline_reason": "evidence_missing",
        "field_path": "target_duration_s",
    }
    content = events[-1]["content"]
    assert "can rebuild the edit" in content
    assert "retry without rebuilding" not in content
    assert events[-1]["payload"]["decline_reason"] == "evidence_missing"


def test_a_cloud_publication_evidence_gap_is_a_repair_retry() -> None:
    job = _declined_job("creator_render_contract_unverified")
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": VARIANT,
                "render_status": "failed",
                "error": "This edit couldn't keep the confirmed length.",
                "error_class": "creator_render_contract_unverified",
                "decline_reason": "evidence_missing",
                "field_path": "target_duration_s",
            }
        ]
    }
    _, execution, events = _observe(job, {"target_variant_id": VARIANT})
    assert execution.error["recovery"] == "retry"
    assert execution.error["retryable"] is True
    assert execution.error["decline_reason"] == "evidence_missing"
    assert "retry without rebuilding" in events[-1]["content"]


def test_a_preflight_evidence_gap_on_the_job_is_a_repair_retry() -> None:
    job = _declined_job(
        "creator_render_contract_unsupported",
        decline={
            "decline_reason": "evidence_missing",
            "failure_reason": "creator_render_contract_unsupported",
        },
    )
    _, execution, _events = _observe(job, {})
    assert execution.error["recovery"] == "retry"


def test_capability_unavailable_decline_names_the_limit_and_a_supported_alternative() -> None:
    job = _declined_job(
        "creator_render_contract_unsupported",
        decline={
            "decline_reason": "capability_unavailable",
            "field_path": "opening_title",
            "alternative": "Ask for it to be rendered on your iPhone, where I can check it.",
            "failure_reason": "creator_render_contract_unsupported",
        },
        detail="This cloud renderer can't verify confirmed on-screen text yet.",
    )
    _, execution, events = _observe(job, {})
    assert execution.error["recovery"] == "ask_user"
    assert execution.error["retryable"] is False
    assert execution.error["decline_reason"] == "capability_unavailable"
    content = events[-1]["content"]
    assert "can't verify confirmed on-screen text" in content
    assert "rendered on your iPhone" in content
    assert "retry without rebuilding" not in content


@pytest.mark.parametrize("reason", ["requirement_conflict", "needs_choice"])
def test_conflict_and_choice_declines_keep_todays_ask_behaviour(reason: str) -> None:
    from app.tasks.content_plan_build import humanize_job_failure_reason

    job = _declined_job(
        "phone_plan_unsupported",
        decline={"decline_reason": reason, "failure_reason": "phone_plan_unsupported"},
    )
    _, execution, events = _observe(job, {})
    assert execution.error["recovery"] == "ask_user"
    assert execution.error["retryable"] is False
    assert events[-1]["content"] == humanize_job_failure_reason("phone_plan_unsupported")


def test_untyped_contract_failure_keeps_its_existing_recovery() -> None:
    job = _declined_job("creator_render_contract_unsupported")
    _, execution, events = _observe(job, {})
    assert execution.error == {
        "code": "creator_render_contract_unsupported",
        "retryable": True,
        "recovery": "retry",
    }
    assert "decline_reason" not in events[-1]["payload"]


def test_a_stale_decline_from_an_earlier_failure_is_not_applied_to_a_new_one() -> None:
    """Job failed once with a capability refusal, was re-run, then failed transiently."""
    job = _declined_job(
        "phone_plan_failed",
        decline={
            "decline_reason": "capability_unavailable",
            "alternative": "Stale alternative.",
            "failure_reason": "creator_render_contract_unsupported",
        },
        detail="ffmpeg exploded: OOM",
    )
    _, execution, events = _observe(job, {})
    assert execution.error["recovery"] == "retry"
    assert execution.error["retryable"] is True
    assert "decline_reason" not in execution.error
    assert "Stale alternative." not in events[-1]["content"]


def test_an_unstamped_job_decline_is_never_honoured() -> None:
    job = _declined_job(
        "creator_render_contract_unsupported",
        decline={"decline_reason": "capability_unavailable"},
    )
    _, execution, _events = _observe(job, {})
    assert "decline_reason" not in execution.error


def test_a_published_cloud_decline_on_the_failed_variant_reaches_the_recovery() -> None:
    job = _declined_job("creator_render_contract_unverified")
    job.assembly_plan = {
        "variants": [
            {"variant_id": "other", "render_status": "ready"},
            {
                "variant_id": VARIANT,
                "render_status": "failed",
                "error": "This edit couldn't verify confirmed on-screen text.",
                "error_class": "creator_render_contract_unverified",
                "decline_reason": "capability_unavailable",
                "field_path": "closing_title",
                "alternative": "Tell me to drop that requirement.",
            },
        ]
    }
    _, execution, events = _observe(job, {"target_variant_id": VARIANT})
    assert execution.error["decline_reason"] == "capability_unavailable"
    assert execution.error["field_path"] == "closing_title"
    assert execution.error["recovery"] == "ask_user"
    assert "Tell me to drop that requirement." in events[-1]["content"]


def test_the_target_variant_never_inherits_another_variants_decline() -> None:
    job = _declined_job("variant_render_failed")
    job.status = "variants_ready_partial"
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": "other",
                "render_status": "failed",
                "error_class": "creator_render_contract_unverified",
                "decline_reason": "capability_unavailable",
                "alternative": "Other variant's alternative.",
            },
            {
                "variant_id": VARIANT,
                "render_status": "failed",
                "error_class": "variant_render_failed",
            },
        ]
    }
    _, execution, events = _observe(job, {"target_variant_id": VARIANT})
    assert "decline_reason" not in execution.error
    assert execution.error["recovery"] == "retry"
    assert "Other variant's alternative." not in events[-1]["content"]


def test_a_variant_decline_does_not_leak_into_an_unrelated_failure_code() -> None:
    job = _declined_job("render_failed")
    job.assembly_plan = {
        "variants": [
            {
                "variant_id": VARIANT,
                "render_status": "failed",
                "decline_reason": "capability_unavailable",
            }
        ]
    }
    _, execution, _events = _observe(job, {})
    assert execution.error["recovery"] == "retry"
    assert "decline_reason" not in execution.error


# ---------------------------------------------------------------- KRI-470 PR-G: refusals


def _refused_job(reason: str, *, alternative: str = "", field_path: str | None = "ordering_choice"):
    """A device job whose edit the contract refused at publication (typed, recorded)."""
    from app.services.creator_render_contract import CreatorRenderContractError
    from app.services.device_render import record_contract_decline

    job = _device_job()
    _publish(job)  # a good version is live ...
    job.assembly_plan["variants"][0]["render_status"] = "awaiting_device"  # ... then an edit
    record = device_record(job, VARIANT)
    record["status"]["phase"] = "syncing"
    save_device_record(job, VARIANT, record)
    exc = CreatorRenderContractError(
        "This edit couldn't keep the confirmed clip order.",
        decline_reason=reason,
        field_path=field_path,
        alternative=alternative or None,
    )
    record_contract_decline(job, VARIANT, exc, stage="publication")
    mark_device_failed(job, VARIANT, reason_code="unsupported_recipe", detail=str(exc))
    return job


def _observe_refusal(job):  # noqa: ANN001, ANN202
    return _observe(
        job,
        {"target_variant_id": VARIANT, "result": {"editor_prep": {"device_recipe_revision": 1}}},
    )


def test_a_refused_edit_is_repaired_against_the_approved_instruction_not_re_asked() -> None:
    job = _refused_job("evidence_missing")
    good = dict(job.assembly_plan["variants"][0])

    outcome, execution, events = _observe_refusal(job)

    assert outcome == "failed"
    error = execution.error
    assert (error["recovery"], error["retryable"]) == ("refresh_replan", True)
    assert (error["decline_reason"], error["field_path"]) == ("evidence_missing", "ordering_choice")
    failed = events[-1]
    assert failed["event_type"] == "assistant_render_failed"
    assert failed["payload"]["decline_reason"] == "evidence_missing"
    text = failed["content"]
    assert "confirmed clip order" in text  # what was violated
    assert "last good version" in text  # what stays live
    assert "tap Retry" not in text  # the phone Retry would re-pin the same refused recipe
    for restating in ("tell me again", "re-send", "restate", "which do you want"):
        assert restating not in text.lower()
    assert job.assembly_plan["variants"][0] == good  # nothing about the live version changed


@pytest.mark.parametrize("reason", ["needs_choice", "requirement_conflict"])
def test_a_refusal_that_needs_a_new_decision_asks_the_specific_question(reason) -> None:
    job = _refused_job(
        reason,
        alternative="Use the order you added the clips, or continue without a fixed order.",
        field_path="ordering_choice",
    )

    outcome, execution, events = _observe_refusal(job)

    assert outcome == "failed"
    assert execution.error["recovery"] == "ask_user"
    assert execution.error["retryable"] is False
    assert execution.error["decline_reason"] == reason
    text = events[-1]["content"]
    assert "Use the order you added the clips" in text
    assert "last good version" in text


def test_an_unavailable_capability_refuses_with_the_way_forward() -> None:
    job = _refused_job("capability_unavailable", alternative="Ask for a plain montage instead.")

    _, execution, events = _observe_refusal(job)

    assert execution.error["recovery"] == "ask_user"
    assert "Ask for a plain montage instead." in events[-1]["content"]


def test_a_device_failure_with_no_recorded_refusal_keeps_the_phone_retry_copy() -> None:
    job = _device_job()
    mark_device_failed(job, VARIANT, reason_code="thermal", detail="")

    _, execution, events = _observe_refusal(job)

    assert execution.error["recovery"] == "manual"
    assert "tap Retry" in events[-1]["content"]
    assert "decline_reason" not in execution.error
