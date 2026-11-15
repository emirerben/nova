"""Behavior of the failed-job replay driver on production-shaped job and item debug payloads.

Shapes come from real production debug reads (KRI-1375): a pool image added after the job ran,
`asset-<uuid>` visual ids in the brief binding, and a Main Creator question that ends planning.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

API = Path(__file__).parents[2] / "scripts"
for name in ("replay_creator", "replay_job"):
    spec = importlib.util.spec_from_file_location(name, API / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
replay_job = sys.modules["replay_job"]
replay_creator = sys.modules["replay_creator"]

USER = "25a596e6-0ada-4572-b669-f4f9d5c5aced"
ITEM = "fac3dbd4-f61b-4c2d-b5c0-a387d18cfa2a"
CLIP = "analysis-proxy-ios-407A6373.mp4"
LATE_POOL = "2f22b5a3-14ae-4599-8756-0bee53fa7e2e"
USED_POOL = "71843110-fb7d-4124-90a2-99861d248a2a"


def _job(snapshot: dict | None) -> dict:
    binding = {"media_snapshot": snapshot} if snapshot is not None else {}
    return {
        "job": {
            "id": "89fefe4d-137a-409c-b846-760161dc5063",
            "user_id": USER,
            "content_plan_item_id": ITEM,
            "all_candidates": {"creator_request": "Caption the clips."},
            "assembly_plan": {"creator_brief_binding": binding} if binding else {},
        }
    }


def _item() -> dict:
    return {
        "item": {"item_status": "done", "voiceover_gcs_path": f"users/{USER}/vo/voice.m4a"},
        "clip_assignments": [
            {
                "media_id": CLIP,
                "kind": "video",
                "gcs_path": f"users/{USER}/creation-threads/t/{CLIP}",
            }
        ],
        "pool_assets": [
            {
                "id": pool,
                "kind": "image",
                "status": "ready",
                "gcs_path": f"users/{USER}/plan/{ITEM}/pool/{pool}.jpg",
            }
            for pool in (LATE_POOL, USED_POOL)
        ],
    }


@pytest.fixture
def stubbed(monkeypatch, tmp_path):
    copied: list[str] = []

    def fake_copy(bucket, object_path, destination, generation=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")
        copied.append(object_path)

    def run(job_debug, item_debug):
        monkeypatch.setattr(
            replay_job,
            "prod_get",
            lambda path: job_debug if "/jobs/" in path else item_debug,
        )
        monkeypatch.setattr(replay_job, "storage_copy", fake_copy)
        return replay_job.collect_inputs("89fefe4d-137a-409c-b846-760161dc5063", tmp_path, "b")

    run.copied = copied
    run.tmp = tmp_path
    return run


def test_replay_takes_only_the_media_the_job_was_approved_against(stubbed) -> None:
    snapshot = {
        "clip_assignments": [{"media_id": CLIP}],
        "visuals": [{"media_id": f"asset-{USED_POOL}", "kind": "image"}],
    }
    info = stubbed(_job(snapshot), _item())
    assert info["source_count"] == 2
    assert not any(LATE_POOL in path for path in stubbed.copied)
    assert not any("voice.m4a" in path for path in stubbed.copied)
    assert info["source_coverage"]["bound_to_recorded_media"] is True


def test_replay_refuses_when_a_recorded_visual_is_no_longer_recoverable(stubbed) -> None:
    snapshot = {
        "clip_assignments": [{"media_id": CLIP}],
        "visuals": [{"media_id": "asset-deleted-asset", "kind": "image"}],
    }
    with pytest.raises(replay_job.ReplayJobError, match="not recoverable"):
        stubbed(_job(snapshot), _item())


def _unnamed_snapshot(**overrides) -> dict:
    """A binding as the admin debug serves it for a job whose speech cleanup is still
    required (4620fd7c): no media ids, only kind, duration, receipt."""

    snapshot = {
        "clip_assignments": [
            {
                "kind": "video",
                "duration_s": 48.8,
                "upload_contract": {"purpose": "analysis_proxy"},
            }
        ],
        "visuals": [{"kind": "image", "capture": {}}, {"kind": "image", "capture": {}}],
    }
    snapshot.update(overrides)
    return snapshot


def _timed_item() -> dict:
    item = _item()
    item["clip_assignments"][0]["duration_s"] = 48.8
    return item


def test_binding_rows_without_ids_are_matched_by_kind_and_duration(stubbed) -> None:
    job_debug = _job(_unnamed_snapshot())
    info = stubbed(job_debug, _timed_item())
    assert info["source_count"] == 3
    assert info["source_coverage"]["bound_to_recorded_media"] is True
    assert info["source_coverage"]["matched_without_media_id"] == {"clips": 1, "pool_visuals": 2}
    receipts = replay_creator._extract_recorded_assignments(job_debug)
    assert receipts[CLIP]["upload_contract"] == {"purpose": "analysis_proxy"}


def test_unnamed_clip_with_no_item_clip_of_that_length_is_refused(stubbed) -> None:
    item = _timed_item()
    item["clip_assignments"][0]["duration_s"] = 12.0
    with pytest.raises(replay_job.ReplayJobError, match="no unclaimed clip"):
        stubbed(_job(_unnamed_snapshot()), item)


def test_two_equally_close_item_clips_are_refused_as_ambiguous(stubbed) -> None:
    item = _timed_item()
    item["clip_assignments"].append({**item["clip_assignments"][0], "media_id": "twin.mp4"})
    with pytest.raises(replay_job.ReplayJobError, match="equally"):
        stubbed(_job(_unnamed_snapshot()), item)


def test_unnamed_pool_visuals_must_match_the_item_pool_count(stubbed) -> None:
    snapshot = _unnamed_snapshot(visuals=[{"kind": "image", "capture": {}}])
    with pytest.raises(replay_job.ReplayJobError, match="which ones the job used is unknown"):
        stubbed(_job(snapshot), _timed_item())


def test_job_without_a_binding_replays_everything_the_item_holds(stubbed) -> None:
    info = stubbed(_job(None), _item())
    assert info["source_count"] == 4
    assert info["source_coverage"]["bound_to_recorded_media"] is False


JOB_RAN_AT = "2026-10-21T13:05:04.458952Z"
SCOPED_PHOTO = "6ae61e1a-96ff-4776-8f95-54ace669db23"


def _job_without_visuals_key() -> dict:
    """50f67a06: the binding names its clip and has no `visuals` key, so it says nothing
    about the pool, while the job's own plan scopes a requirement to a pool photo."""

    job = _job({"clip_assignments": [{"media_id": CLIP}]})
    job["job"]["created_at"] = JOB_RAN_AT
    job["job"]["assembly_plan"]["creator_render_requirements"] = {
        "exact_texts": [{"media_id": f"asset-{USED_POOL}", "text": "Oblea"}]
    }
    return job


def _dated_item() -> dict:
    item = _item()
    late, used = item["pool_assets"]
    late["created_at"] = "2026-10-21T13:20:00Z"
    used["created_at"] = "2026-10-21T12:52:12.939395Z"
    return item


def test_a_binding_without_a_visuals_key_keeps_the_pool_the_item_held_when_the_job_ran(
    stubbed,
) -> None:
    info = stubbed(_job_without_visuals_key(), _dated_item())
    assert any(USED_POOL in path for path in stubbed.copied)
    assert not any(LATE_POOL in path for path in stubbed.copied)
    coverage = info["source_coverage"]
    assert coverage["pool_visuals_recorded"] is False
    assert (coverage["pool_visuals_inferred"], coverage["pool_visuals_added_after_job"]) == (1, 1)
    assert info["unseeded_state"]["pool_visuals_not_recorded"] == 1


def test_pool_media_the_job_names_but_the_item_no_longer_holds_is_refused(stubbed) -> None:
    job = _job_without_visuals_key()
    job["job"]["assembly_plan"]["creator_render_requirements"]["exact_texts"].append(
        {"media_id": f"asset-{SCOPED_PHOTO}", "text": "Merope"}
    )
    with pytest.raises(replay_job.ReplayJobError, match=SCOPED_PHOTO):
        stubbed(job, _dated_item())


def test_voiceover_outside_the_owner_prefix_is_refused(stubbed) -> None:
    item = _item()
    item["item"]["voiceover_gcs_path"] = "users/someone-else/vo/voice.m4a"
    with pytest.raises(replay_job.ReplayJobError, match="outside the job owner"):
        stubbed(_job(None), item)


def _plan(events: list[dict], preparation: dict | None) -> dict:
    return {
        "status": "briefing",
        "pending_plan": None,
        "events": events,
        "preparation": preparation,
    }


QUESTION = {
    "event_type": "assistant_question",
    "payload": {
        "message": "Which clips match the pink house?",
        "reason_code": "clip_intent_unresolved",
    },
}


def test_a_trailing_question_ends_planning_with_its_text() -> None:
    open_question = replay_creator._open_question(_plan([QUESTION], {"status": "ready"}))
    assert open_question == {
        "message": "Which clips match the pink house?",
        "reason_code": "clip_intent_unresolved",
    }


def test_a_question_during_clip_preparation_is_not_final() -> None:
    assert replay_creator._open_question(_plan([QUESTION], {"status": "analyzing"})) is None


def test_a_later_event_supersedes_the_question() -> None:
    later = {"event_type": "assistant_message", "payload": {"message": "Working on it"}}
    assert replay_creator._open_question(_plan([QUESTION, later], None)) is None


def test_summary_reports_the_question_not_a_timeout(tmp_path) -> None:
    import json

    receipt = tmp_path / "replay-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "error": {
                    "type": "MainCreatorQuestion",
                    "message": "Main Creator asked a question",
                    "question": QUESTION["payload"],
                }
            }
        )
    )
    result = replay_job.summarize(receipt, tmp_path)
    assert result["outcome"] == "main_creator_question"
    assert result["main_creator_question"]["reason_code"] == "clip_intent_unresolved"


@pytest.mark.parametrize("name", ["nova", "nova_creator_fidelity_test", "x; DROP"])
def test_only_per_run_scratch_databases_can_be_dropped(name: str) -> None:
    with pytest.raises(replay_job.ReplayJobError):
        replay_job._scratch_name(f"postgresql://u@localhost/{name}")


SUCCESS = ["ok", "ok_fallback"]
GEMINI = "gemini-3.1-pro-preview"
SOL = "gpt-6.1-sol"


def _production(complete: bool = True, main_model: str = GEMINI):
    return replay_job.ProductionAgents(
        models={
            "nova.creator.main": frozenset({main_model}),
            "nova.plan.clip_intent_planner": frozenset({"gemini-2.5-flash"}),
        },
        prompt_versions={},
        trace_complete=complete,
    )


def _runs(*pairs: tuple[str, str], model: str = GEMINI) -> dict:
    return {
        "success": SUCCESS,
        "runs": [{"agent_name": n, "outcome": o, "model": model} for n, o in pairs],
    }


def test_a_failure_inside_an_agent_production_never_ran_is_a_divergence() -> None:
    recorded = _runs(
        ("nova.creator.main", "ok"),
        ("nova.plan.semantic_edit_proposal", "terminal_schema"),
    )
    route = replay_job.compare_routes(_production(), recorded)
    assert route["diverged_failures"] == ["nova.plan.semantic_edit_proposal"]
    assert route["replay_only_agents"] == ["nova.plan.semantic_edit_proposal"]


def test_a_failure_inside_an_agent_production_also_ran_is_comparable() -> None:
    recorded = _runs(("nova.creator.main", "ok"), ("nova.plan.clip_intent_planner", "timeout"))
    route = replay_job.compare_routes(_production(), recorded)
    assert route["diverged_failures"] == []


def test_an_agent_that_failed_then_recovered_is_not_a_failure() -> None:
    recorded = _runs(
        ("nova.plan.semantic_edit_proposal", "schema_invalid"),
        ("nova.plan.semantic_edit_proposal", "ok"),
    )
    route = replay_job.compare_routes(_production(), recorded)
    assert route["replay_failed_agents"] == []
    assert route["replay_only_agents"] == ["nova.plan.semantic_edit_proposal"]


def test_a_cut_off_production_trace_never_claims_a_divergence() -> None:
    recorded = _runs(("nova.plan.semantic_edit_proposal", "terminal_schema"))
    route = replay_job.compare_routes(_production(complete=False), recorded)
    assert route["diverged_failures"] == []
    assert route["production_trace_complete"] is False


def test_a_main_creator_on_another_model_is_named_and_its_new_failure_is_not_productions() -> None:
    route = replay_job.compare_routes(
        _production(main_model=SOL), _runs(("nova.creator.main", "ok"), model=GEMINI)
    )
    assert route["model_differences"] == [
        {"agent": "nova.creator.main", "production": [SOL], "replay": [GEMINI]}
    ]
    result = {"outcome": "failed", "failure_reason": "creation_text_failed", "error_detail": None}
    replay_job.classify_failure(result, route, "phone_plan_unsupported")
    assert result["outcome"] == "diverged_from_production"
    assert result["replay_failure"]["failure_reason"] == "creation_text_failed"


def test_a_reproduced_failure_stands_even_on_another_model() -> None:
    route = replay_job.compare_routes(
        _production(main_model=SOL), _runs(("nova.creator.main", "ok"), model=GEMINI)
    )
    result = {"outcome": "failed", "failure_reason": "phone_plan_unsupported", "error_detail": None}
    replay_job.classify_failure(result, route, "phone_plan_unsupported")
    assert result["outcome"] == "failed" and result["production_failure_reproduced"] is True


def test_a_stalled_turn_is_not_reported_as_a_route_divergence(tmp_path) -> None:
    import json

    # Verifier run 1 on 1cb33402: the cold worker lost the lease, the turn sat `planning`, and
    # the timeout read as `diverged_from_production` because two planner agents were missing.
    receipt = tmp_path / "replay-receipt.json"
    stalled = {"status": "planning", "lease_lapsed": True, "reason": "Timed out"}
    receipt.write_text(
        json.dumps({"error": {"type": "ReplayStalled", "message": "x", "stalled": stalled}})
    )
    result = replay_job.summarize(receipt, tmp_path)
    route = replay_job.compare_routes(_production(), _runs(("nova.creator.main", "ok")))
    route["replay_only_agents"] = ["nova.edit.copilot"]
    replay_job.classify_failure(result, route, "phone_plan_unsupported")
    assert result["outcome"] == "stalled"
    assert result["stalled"]["lease_lapsed"] is True


def test_the_first_message_that_got_another_reply_is_named() -> None:
    turns = [
        {"production_turn_id": "a", "matches_production": True},
        {
            "production_turn_id": "b",
            "matches_production": False,
            "production_reply": "approval",
            "replay_reply": "reply",
        },
        {"production_turn_id": "c", "matches_production": False},
    ]
    first = replay_job.first_divergent_turn(turns)
    assert first["message_index"] == 2 and first["production_turn_id"] == "b"
    assert (first["production_reply"], first["replay_reply"]) == ("approval", "reply")


def test_a_message_whose_answer_could_not_be_sent_is_the_divergent_one(tmp_path) -> None:
    import json

    # 40684a25: message 1 got a clip question in both, message 2's tapped answer was not sent.
    receipt = tmp_path / "replay-receipt.json"
    diverged = {"production_turn_id": "b", "answer": "clip_selection", "reason": "no category"}
    receipt.write_text(
        json.dumps(
            {
                "turns": [{"production_turn_id": "a", "matches_production": True}],
                "error": {"type": "TurnDiverged", "message": "x", "diverged_turn": diverged},
            }
        )
    )
    result = replay_job.summarize(receipt, tmp_path)
    assert result["outcome"] == "diverged_from_production"
    first = result["first_divergent_turn"]
    assert (first["message_index"], first["production_turn_id"]) == (2, "b")
    assert first["answer_not_sent"] == "clip_selection"


def test_seeded_clips_that_do_not_bind_are_an_input_error_not_a_route_result(tmp_path) -> None:
    import json

    receipt = tmp_path / "replay-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "error": {
                    "type": "ReplayInputError",
                    "message": "seeded clips do not bind as phone sources",
                    "input_error": True,
                }
            }
        )
    )
    result = replay_job.summarize(receipt, tmp_path)
    route = replay_job.compare_routes(_production(), _runs())
    replay_job.classify_failure(result, route, "phone_plan_unsupported")
    assert result["outcome"] == "input_error"
    assert "phone sources" in result["failure_reason"]


def test_clip_copies_are_pinned_to_the_generation_the_binding_recorded(
    stubbed, monkeypatch
) -> None:
    pins: dict[str, str | None] = {}

    def fake_copy(bucket, object_path, destination, generation=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x")
        pins[object_path] = generation

    snapshot = {
        "clip_assignments": [
            {"media_id": CLIP, "kind": "video", "storage_generation": "1792969597555986"}
        ],
        "visuals": [],
    }
    job_debug, item_debug = _job(snapshot), _item()
    monkeypatch.setattr(
        replay_job, "prod_get", lambda path: job_debug if "/jobs/" in path else item_debug
    )
    monkeypatch.setattr(replay_job, "storage_copy", fake_copy)
    replay_job.collect_inputs("89fefe4d-137a-409c-b846-760161dc5063", stubbed.tmp, "b")
    assert pins[f"users/{USER}/creation-threads/t/{CLIP}"] == "1792969597555986"


def test_the_replay_ledger_envelope_is_capped_and_local() -> None:
    env = replay_job.replay_environment(
        {}, "postgresql://x@localhost/y", "replay-abcd1234-z", max_cost_usd=0.35
    )
    assert env["AI_USAGE_ENVIRONMENT"] == "development"
    assert env["AI_MANUAL_QA_TEST_RUN_ID"] == "replay-abcd1234-z"
    assert env["AI_MANUAL_QA_MAX_COST_USD"] == "0.35"
    assert env["AI_OPENAI_PLANNING_BUDGET_USD"] == "0.35"


def test_only_model_provider_keys_reach_the_replay() -> None:
    dotenv = {
        "GEMINI_API_KEY": "g",
        "OPENAI_API_KEY": "o",
        "ANTHROPIC_API_KEY": "a",
        "RESEND_API_KEY": "r",
        "TYPESAFE_API_KEY": "t",
        "ADMIN_PROD_API_KEY": "p",
    }
    env = replay_job.replay_environment(
        dotenv, "postgresql://x@localhost/y", "replay-abcd1234-z", max_cost_usd=1.0
    )
    assert {k for k in env if k.endswith("_API_KEY")} == {
        "GEMINI_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }


@pytest.mark.parametrize("namespace", ["", "replay-*", "nova-dev", "replay-abcd1234-z:x", "dev"])
def test_redis_cleanup_refuses_anything_but_a_replay_namespace(namespace: str) -> None:
    with pytest.raises(replay_job.ReplayJobError):
        replay_job.delete_queue_namespace(namespace)
