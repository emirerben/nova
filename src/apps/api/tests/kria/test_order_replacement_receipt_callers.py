"""Real actor-owned PostgreSQL sources, compiled artifacts and approval/worker receipts."""

import copy

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.approval_readiness import assess_readiness
from app.kria.brief import CreativeBrief
from app.kria.brief_checks import build_receipts
from app.kria.order_replacement_receipts import with_verified_order_execution
from app.kria.order_replacement_sources import load_order_replacement_source_context_sync
from app.kria.strategy_policy import CheckedStrategy, check_strategy_for_runtime_v2
from app.services.creator_capabilities import resolve_creator_manifest
from tests.kria.test_order_replacement_receipts import (
    confirmed_execution,
    facts_with_omission,
    requirement,
)
from tests.kria.test_order_replacement_sources import stored as stored


@pytest.fixture
def ready_context(stored, prod_profile):
    db, owner, item = stored
    proof, execution, sources = confirmed_execution()
    kinds = {m.source_slot: m.kind for m in proof.proposal.moments}
    item.clip_gcs_paths = [s.path for s in sources]
    item.clip_assignments = [
        {
            "media_id": s.media_id,
            "gcs_path": s.path,
            "storage_generation": s.generation,
            "duration_s": s.duration_s,
            "kind": kinds[s.slot],
        }
        for s in sources
    ]
    db.flush()
    ctx = load_order_replacement_source_context_sync(
        db, item_id=item.id, creator_id=owner.id, proposal=proof
    )
    manifest = resolve_creator_manifest(
        item_id=str(item.id),
        edit_format="montage",
        media=[
            {"media_id": s.media_id, "kind": kinds[s.slot], "duration_s": s.duration_s}
            for s in sources
        ],
    )
    strategy = CreativeStrategy(
        direction="guided_story",
        render_program="guided",
        audio_strategy="original_audio",
        media_scope="all",
        confirmed_order_replacement=proof,
    )
    checked = check_strategy_for_runtime_v2(manifest, strategy)
    assert isinstance(checked, CheckedStrategy), checked
    return {
        "brief": CreativeBrief(version=4, requirements=[requirement(proof)]),
        "strategy": checked.strategy.model_dump(mode="json"),
        "media": {
            "clip_paths": list(item.clip_gcs_paths),
            "clip_assignments": list(item.clip_assignments),
        },
        "manifest": manifest,
        "creator_id": owner.id,
        "order_execution": execution,
        "order_source_context": ctx,
    }


def test_actual_preapproval_reader_verifies_actor_owned_execution(ready_context):
    report = assess_readiness(**ready_context)
    assert report["policy_problem"] is None, report["policy_problem"]
    assert report["ready"], report["requirements"]
    assert report["requirements"][0]["disposition"] == "executable"


@pytest.mark.parametrize(
    "failure",
    [
        "missing_sources",
        "wrong_actor",
        "missing_execution",
        "wrong_execution",
        "missing_strategy_proof",
        "mismatched_proof",
    ],
)
def test_actual_preapproval_reader_missing_or_wrong_context_is_not_ready(ready_context, failure):
    args = dict(ready_context)
    if failure == "missing_sources":
        args["order_source_context"] = None
    elif failure == "wrong_actor":
        args["creator_id"] = "different-actor"
    elif failure == "missing_execution":
        args["order_execution"] = None
    elif failure == "wrong_execution":
        args["order_execution"] = copy.deepcopy(args["order_execution"])
        args["order_execution"]["story_timeline"].reverse()
    else:
        args["strategy"] = copy.deepcopy(args["strategy"])
        if failure == "missing_strategy_proof":
            args["strategy"].pop("confirmed_order_replacement")
        else:
            args["strategy"]["confirmed_order_replacement"]["question_id"] = "different-question"
    report = assess_readiness(**args)
    assert not report["ready"]
    assert report["requirements"][0]["disposition"] != "executable"


def test_worker_receipt_helper_uses_same_owned_artifact(ready_context):
    args = ready_context
    facts = with_verified_order_execution(
        facts_with_omission(),
        brief=args["brief"],
        strategy=args["strategy"],
        execution=args["order_execution"],
        source_context=args["order_source_context"],
        creator_id=args["creator_id"],
    )
    assert build_receipts(args["brief"].live(), facts, include_unchecked=True)[0].status == "met"
    cleared = with_verified_order_execution(
        facts,
        brief=args["brief"],
        strategy=args["strategy"],
        execution=args["order_execution"],
        source_context=None,
        creator_id=args["creator_id"],
    )
    assert cleared.verified_order_execution is None
    assert (
        build_receipts(args["brief"].live(), cleared, include_unchecked=True)[0].status == "partial"
    )


def test_legacy_absence_leaves_readiness_report_byte_identical(ready_context):
    args = dict(ready_context)
    args["brief"] = CreativeBrief(requirements=[requirement()])
    args["strategy"] = dict(args["strategy"])
    args["strategy"].pop("confirmed_order_replacement")
    with_context = assess_readiness(**args)
    args.pop("order_execution")
    args.pop("order_source_context")
    assert assess_readiness(**args) == with_context


def test_preapproval_reloads_changed_generation_from_owned_database_rows(ready_context, stored):
    db, owner, item = stored
    assignments = copy.deepcopy(item.clip_assignments)
    assignments[0]["storage_generation"] = "new-generation"
    item.clip_assignments = assignments
    db.flush()
    proof = ready_context["brief"].live()[0].order_replacement
    current = load_order_replacement_source_context_sync(
        db,
        item_id=item.id,
        creator_id=owner.id,
        proposal=proof,
    )
    report = assess_readiness(**{**ready_context, "order_source_context": current})
    assert not report["ready"]
    assert report["requirements"][0]["disposition"] == "evidence_missing"


@pytest.mark.parametrize(
    "change", [None, "wrong_window", "new_generation", "cached_met_wrong_window"]
)
def test_actual_finished_review_rechecks_compiled_artifact_and_current_sources(
    ready_context, stored, change
):
    import uuid

    from app.kria.brief_binding import BriefBinding
    from app.models import CreationThread, CreatorAgentExecution, CreatorAgentSession, Job
    from app.tasks.kria_runtime import _approved_generation_requirements_review

    db, owner, item = stored
    brief = ready_context["brief"]
    thread = CreationThread(
        id=uuid.uuid4(),
        creator_id=owner.id,
        content_plan_id=item.content_plan_id,
        active_plan_item_id=item.id,
    )
    db.add(thread)
    db.flush()
    binding = BriefBinding.create(thread.id, brief, media_snapshot=ready_context["media"])
    variant = copy.deepcopy(ready_context["order_execution"])
    variant["render_generation_id"] = "current-render"
    record = {
        "generation_id": "current-render",
        "brief_version": brief.version,
        "ordering_basis": "creator_order",
        "intent_outcomes": [
            {
                "intent_id": "interior",
                "op": "order",
                "name": "interior",
                "status": "unresolved",
                "code": "skipped",
            }
        ],
    }
    if change == "cached_met_wrong_window":
        record["requirement_receipts"] = [
            {
                "requirement_id": brief.live()[0].id,
                "brief_version": brief.version,
                "generation_id": "current-render",
                "status": "met",
                "verification": "checked",
                "stage": "checked",
            }
        ]
    if change in {"wrong_window", "cached_met_wrong_window"}:
        video = next(row for row in variant["story_timeline"] if row["kind"] == "video")
        video["source_start_s"] += 0.25
        video["source_end_s"] += 0.25
    elif change == "new_generation":
        assignments = copy.deepcopy(item.clip_assignments)
        assignments[0]["storage_generation"] = "new-generation"
        item.clip_assignments = assignments
    job = Job(
        id=uuid.uuid4(),
        user_id=owner.id,
        content_plan_item_id=item.id,
        raw_storage_path="offline/readers",
        status="done",
        assembly_plan={"unified_montage": record, "variants": [variant]},
        all_candidates={"creator_strategy": ready_context["strategy"]},
    )
    session = CreatorAgentSession(id=uuid.uuid4(), creator_id=owner.id, plan_item_id=item.id)
    db.add_all([job, session])
    db.flush()
    execution = CreatorAgentExecution(
        id=uuid.uuid4(),
        session_id=session.id,
        idempotency_key=str(uuid.uuid4()),
        request_digest="offline-order-review",
        expected_revision=0,
        status="succeeded",
        result={"brief_binding": binding.model_dump(mode="json")},
    )
    db.add(execution)
    db.flush()
    summary = "The confirmed remaining sequence is ready."
    text, receipts = _approved_generation_requirements_review(
        db, thread, job, variant, execution, summary
    )
    assert len(receipts) == 1
    assert receipts[0]["status"] == ("met" if change is None else "partial")
    assert receipts[0]["generation_id"] == "current-render"
    assert (summary in text) == (change is None)
