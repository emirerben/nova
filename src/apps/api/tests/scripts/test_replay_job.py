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


JOB_RAN_AT = "2026-10-21T13:05:04.458952Z"


def _job(snapshot: dict | None) -> dict:
    binding = {"media_snapshot": snapshot} if snapshot is not None else {}
    return {
        "job": {
            "id": "89fefe4d-137a-409c-b846-760161dc5063",
            "created_at": JOB_RAN_AT,
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
                "created_at": created_at,
            }
            for pool, created_at in (
                (LATE_POOL, "2026-10-21T13:20:00Z"),
                (USED_POOL, "2026-10-21T12:52:12.939395Z"),
            )
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


SCOPED_PHOTO = "6ae61e1a-96ff-4776-8f95-54ace669db23"


def test_job_without_a_binding_replays_only_the_pool_the_item_held_when_it_ran(stubbed) -> None:
    info = stubbed(_job(None), _item())
    assert any(USED_POOL in path for path in stubbed.copied)
    assert not any(LATE_POOL in path for path in stubbed.copied)
    coverage = info["source_coverage"]
    assert coverage["bound_to_recorded_media"] is False
    assert (coverage["pool_visuals_inferred"], coverage["pool_visuals_added_after_job"]) == (1, 1)
    # Clip rows carry no creation time, so the run says how many it could not date.
    assert coverage["clips_not_dated"] == 1


def _job_without_visuals_key() -> dict:
    """50f67a06: the binding names its clip and has no `visuals` key, so it says nothing
    about the pool, while the job's own plan scopes a requirement to a pool photo."""

    job = _job({"clip_assignments": [{"media_id": CLIP}]})
    job["job"]["assembly_plan"]["creator_render_requirements"] = {
        "exact_texts": [{"media_id": f"asset-{USED_POOL}", "text": "Oblea"}]
    }
    return job


def test_a_binding_without_a_visuals_key_keeps_the_pool_the_item_held_when_the_job_ran(
    stubbed,
) -> None:
    info = stubbed(_job_without_visuals_key(), _item())
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
        stubbed(job, _item())


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


def _runs(*pairs: tuple[str, str], model: str = GEMINI, comparable: bool = True) -> dict:
    return {
        "success": SUCCESS,
        "runs": [
            {"agent_name": n, "outcome": o, "model": model, "comparable": comparable}
            for n, o in pairs
        ],
    }


def test_a_session_scoped_agent_failure_is_not_called_a_route_divergence() -> None:
    """Verifier, 19f61092: the clip-intent agents run on the Creator session alone
    (`creator_agent.py`, RunContext without a plan item), so production's item trace never
    lists them while the replay counted every run in its database.

    Ways this goes wrong: the failure reads as `diverged_from_production` "inside
    nova.plan.clip_request_resolver, which production never ran"; that agent is named as
    replay-only; the spend total drops the runs left out of the comparison; its failure, which
    production's trace cannot show, still reads as production's (verifier P5).
    """

    recorded = _runs(("nova.creator.main", "ok"))
    recorded["runs"] += _runs(
        ("nova.plan.clip_request_resolver", "terminal_transient"), comparable=False
    )["runs"]
    recorded["runs"][-1]["cost_usd"] = 0.004
    route = replay_job.compare_routes(_production(), recorded)
    assert route["replay_only_agents"] == [] and route["diverged_failures"] == []
    assert route["replay_uncompared_agents"] == ["nova.plan.clip_request_resolver"]
    assert route["replay_spend_usd"] == 0.004
    result = _failed_replay("phone_plan_unsupported", ORDERING_0701FCC9, ORDERING_REFUSAL)
    replay_job.classify_failure(
        result, route, _production_render("phone_plan_unsupported", detail=ORDERING_0701FCC9)
    )
    assert result["outcome"] == "failed"
    assert result["reproduction"] == "reproduction_unproven"
    assert "nova.plan.clip_request_resolver" in " ".join(result["reproduction_gaps"])


@pytest.mark.parametrize(
    ("began", "diverged"),
    [
        ("2026-10-19T16:05:00Z", []),
        ("2026-11-14T06:53:16Z", ["nova.plan.clip_request_resolver"]),
    ],
)
def test_an_agent_production_did_not_record_yet_proves_no_divergence(
    monkeypatch, began: str, diverged: list[str]
) -> None:
    """19f61092 (2026-10-19): before #1973 deployed (2026-10-31 10:21 UTC) the chat runtime ran
    the clip-intent planner, resolver and clip question with no plan item, so production
    stored no run of them; today's replay stores them on the item. Ways this goes wrong: an
    older job's replay names them as agents production never ran, or a job after the deploy
    loses that comparison."""

    trace = {
        "agent_runs": [
            {"agent_name": "nova.creator.main", "model": GEMINI, "created_at": began},
        ],
        "next_cursor": None,
    }
    monkeypatch.setattr(replay_job, "prod_get", lambda path: trace)
    production = replay_job.production_agents(ITEM, [], until=None, began=began)
    recorded = _runs(
        ("nova.creator.main", "ok"), ("nova.plan.clip_request_resolver", "terminal_transient")
    )
    route = replay_job.compare_routes(production, recorded)
    assert route["diverged_failures"] == diverged
    assert ("nova.plan.clip_request_resolver" in route["replay_uncompared_agents"]) is not diverged


def test_the_replay_is_interrupted_once_its_settled_spend_passes_the_cap(monkeypatch) -> None:
    """Gemini is not metered by the ledger while AI_COST_CONTROL_ENABLED stays off as in
    production, so MAX_COST_USD capped nothing for it (verifier: $0.1487 on Gemini 3.1 Pro
    under a 0.25 cap). Ways this goes wrong: the run continues past the cap; it is stopped but
    reported as a route result; a failed spend read crashes the driver mid-run."""

    import time

    from sqlalchemy.exc import OperationalError

    reads = iter([OperationalError("select", {}, Exception("busy")), 0.01, 0.42])

    def spend(url: str) -> float:
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(replay_job, "settled_spend_usd", spend)
    started = time.monotonic()
    code, spent = replay_job.run_under_spend_cap(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        env={"PATH": "/usr/bin:/bin"},
        scratch_url="postgresql://x@localhost/y",
        cap_usd=0.35,
        poll_s=0.05,
    )
    assert spent == 0.42 and code != 0
    assert time.monotonic() - started < 30
    result = {"outcome": "failed", "failure_reason": "replay interrupted", "error_detail": None}
    replay_job.mark_spend_cap(result, spent, 0.35)
    assert result["outcome"] == "spend_cap_reached"
    assert "0.42" in result["failure_reason"]
    finished = replay_job.run_under_spend_cap(
        [sys.executable, "-c", "pass"],
        env={"PATH": "/usr/bin:/bin"},
        scratch_url="postgresql://x@localhost/y",
        cap_usd=0.35,
        poll_s=5,
    )
    assert finished == (0, None)


def test_a_stopped_driver_takes_its_replay_down_with_it(monkeypatch, tmp_path) -> None:
    """A SIGTERM to the driver (make interrupted) left replay_creator running after the worker
    stopped and the scratch database was dropped: it sent message 1 and the Main Creator call
    was billed ($0.047, 2026-11-15 17:24). Ways this goes wrong: the child outlives the driver;
    the interrupt is swallowed and the driver reports a result."""

    import os
    import time

    pid_file = tmp_path / "child.pid"

    def interrupted(url: str) -> float:
        while not pid_file.exists():
            time.sleep(0.01)
        raise KeyboardInterrupt

    monkeypatch.setattr(replay_job, "settled_spend_usd", interrupted)
    with pytest.raises(KeyboardInterrupt):
        replay_job.run_under_spend_cap(
            [
                sys.executable,
                "-c",
                f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(60)",
            ],
            env={"PATH": "/usr/bin:/bin"},
            scratch_url="postgresql://x@localhost/y",
            cap_usd=0.35,
            poll_s=0.05,
        )
    child = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


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
    replay_job.classify_failure(result, route, {"failure_reason": "phone_plan_unsupported"})
    assert result["outcome"] == "diverged_from_production"
    assert result["replay_failure"]["failure_reason"] == "creation_text_failed"


def test_a_reproduced_failure_stands_even_on_another_model() -> None:
    route = replay_job.compare_routes(
        _production(main_model=SOL), _runs(("nova.creator.main", "ok"), model=GEMINI)
    )
    result = _failed_replay("phone_plan_unsupported", FIT_51F7124F["error_tail"], FIT_51F7124F)
    replay_job.classify_failure(
        result,
        route,
        _production_render(
            "phone_plan_unsupported", FIT_51F7124F, detail=FIT_51F7124F["error_tail"]
        ),
    )
    assert result["outcome"] == "failed" and result["production_failure_reproduced"] is True


RESEARCH = "nova.research.public_facts"
TEXT_LAYERS = "nova.research.text_layers"
PRODUCTION_PLAN_FAILURE = {
    "error_class": "ResearchUnavailable",
    "error_tail": "This explanation needs a longer clip or shorter wording",
}
REPLAY_PLAN_FAILURE = {
    "error_class": "TerminalError",
    "error_tail": f"{RESEARCH}: schema — Search produced no usable cited evidence",
}


def _plan_failed_trace(failure: dict | None) -> list[dict]:
    if failure is None:
        return []
    return [
        {
            "stage": "assembly",
            "event": "phone_plan_failed",
            "data": {"failure_reason": "phone_plan_failed", "kind": "bug", **failure},
        }
    ]


@pytest.mark.parametrize(
    ("case", "production_research", "replay_runs", "complete", "failures", "expected", "named"),
    [
        (
            "1be34fc8 as replayed on 2026-11-15",
            "ok",
            [(RESEARCH, "ok"), (TEXT_LAYERS, "ok"), (RESEARCH, "terminal_schema")],
            True,
            (PRODUCTION_PLAN_FAILURE, REPLAY_PLAN_FAILURE),
            "failure_reason_matched_route_differs",
            [TEXT_LAYERS, RESEARCH, "ResearchUnavailable", "TerminalError"],
        ),
        (
            "same route, the replay's research failed where production's succeeded",
            "ok",
            [(RESEARCH, "terminal_schema")],
            True,
            (None, None),
            "reproduction_unproven",
            [RESEARCH],
        ),
        (
            "same route and agents, another planning error",
            "ok",
            [(RESEARCH, "ok")],
            True,
            (PRODUCTION_PLAN_FAILURE, REPLAY_PLAN_FAILURE),
            "reproduction_unproven",
            ["ResearchUnavailable", "TerminalError"],
        ),
        (
            "production's trace was cut off",
            "ok",
            [(RESEARCH, "ok")],
            False,
            (None, None),
            "reproduction_unproven",
            ["cut off"],
        ),
        (
            "same route, same planning error",
            "ok",
            [(RESEARCH, "ok")],
            True,
            (PRODUCTION_PLAN_FAILURE, PRODUCTION_PLAN_FAILURE),
            "reproduced",
            [],
        ),
        (
            "research failed in both",
            "terminal_schema",
            [(RESEARCH, "terminal_schema")],
            True,
            (REPLAY_PLAN_FAILURE, REPLAY_PLAN_FAILURE),
            "reproduced",
            [],
        ),
        (
            "production recorded no planning error and no error detail",
            "ok",
            [(RESEARCH, "ok")],
            True,
            (None, REPLAY_PLAN_FAILURE),
            "reproduction_unproven",
            ["no planning error"],
        ),
    ],
)
def test_a_matching_failure_reason_is_production_failure_only_on_production_route(
    monkeypatch,
    tmp_path,
    case: str,
    production_research: str,
    replay_runs: list[tuple[str, str]],
    complete: bool,
    failures: tuple[dict | None, dict | None],
    expected: str,
    named: list[str],
) -> None:
    """Verifier, 1be34fc8 ("10 November" bilingual text, phone_plan_failed): every one of
    production's eight agent runs succeeded and its plan failed with ResearchUnavailable after
    the last caption binding. Today's code took the text-layers route, and the replay failed on
    its second public-facts search (terminal_schema), yet printed
    production_failure_reproduced: true because phone_plan_failed is a catch-all reason.

    Ways this goes wrong: a reason match alone reads as reproduced; a replay-only agent, a
    failure in an agent production ran cleanly, a cut-off trace or another planning error class
    still read as reproduced; the verdict names no agent or error class; a real reproduction
    (same route, the same agent failing, the same error) stops reading as one; a production job
    that recorded neither a planning error nor an error detail reads as reproduced on its reason
    alone (verifier P3; jobs with an error detail are in the next test).
    """

    import json

    production_runs = [
        {"agent_name": name, "outcome": outcome, "model": GEMINI, "created_at": at}
        for name, outcome, at in (
            ("nova.edit.copilot", "ok", "2026-11-10T07:21:12Z"),
            (RESEARCH, "ok", "2026-11-10T07:21:21Z"),
            ("nova.research.caption_binding", "ok", "2026-11-10T07:21:58Z"),
            ("nova.research.caption_binding", "ok", "2026-11-10T07:22:25Z"),
            ("nova.edit.copilot", "ok", "2026-11-10T07:22:36Z"),
            (RESEARCH, production_research, "2026-11-10T07:22:46Z"),
            ("nova.research.caption_binding", "ok", "2026-11-10T07:23:17Z"),
            ("nova.research.caption_binding", "ok", "2026-11-10T07:23:41Z"),
        )
    ]
    trace = {
        "agent_runs": [
            {"agent_name": "nova.creator.main", "outcome": "ok", "model": GEMINI},
            {"agent_name": "nova.plan.clip_intent_planner", "outcome": "ok", "model": GEMINI},
        ],
        "next_cursor": "older" if not complete else None,
    }
    monkeypatch.setattr(replay_job, "prod_get", lambda path: trace)
    production = replay_job.production_agents(
        ITEM, production_runs, until="2026-11-10T07:23:47Z", began="2026-11-10T07:21:03Z"
    )
    recorded = _runs(
        ("nova.creator.main", "ok"),
        ("nova.plan.clip_intent_planner", "ok"),
        ("nova.edit.copilot", "ok"),
        *replay_runs,
    )
    production_failure, replay_failure = failures
    receipt = tmp_path / "replay-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "job": {
                    "id": "6a3335a9-9286-4381-9544-0eb40e251847",
                    "status": "processing_failed",
                    "failure_reason": "phone_plan_failed",
                    "pipeline_trace": _plan_failed_trace(replay_failure),
                }
            }
        )
    )
    result = replay_job.summarize(receipt, tmp_path)
    route = replay_job.compare_routes(production, recorded)
    production_job = {"pipeline_trace": _plan_failed_trace(production_failure)}
    replay_job.classify_failure(
        result,
        route,
        {
            "failure_reason": "phone_plan_failed",
            "planning_failure": replay_job.planning_failure(production_job),
        },
    )

    assert result["outcome"] == "failed", case
    assert result["failure_reason"] == "phone_plan_failed"
    assert result["reproduction"] == expected, case
    assert result["production_failure_reproduced"] is (expected == "reproduced")
    gaps = " ".join(result["reproduction_gaps"])
    assert all(name in gaps for name in named), (case, gaps)
    assert bool(gaps) is (expected != "reproduced")


# Planning errors and error details production recorded (job debug reads, 2026-10/11).
FIT_51F7124F = {
    "error_class": "CreatorRenderContractError",
    "error_tail": "This text cannot fit clearly in the requested area. Choose another placement "
    "or shorter text.",
}
FONTS_40684A25 = {
    "error_class": "CreatorRenderContractError",
    "error_tail": "The eligible fonts cannot draw all text. Choose another font.",
}
INCOMPLETE_3C312AF4 = {
    "error_class": "CreationTextCompositionError",
    "error_tail": "Text composition is incomplete: Since the English translation had to be added "
    "as new text in this step, it cannot be simultaneously targeted for a word-by-word sequence "
    "split.",
}
INCOMPLETE_4E8C925D = {
    "error_class": "CreationTextCompositionError",
    "error_tail": "Text composition is incomplete: I can only split texts that already exist in "
    "the timeline. I have added the full English translation at the bottom; please ask again in "
    "the next turn to make it show word by word.",
}
COMPILE_E7BF7099 = {
    "error_class": "CreationTextCompositionError",
    "error_tail": "Text composition could not compile: Text changed before this edit could be "
    "drafted",
}
ORDERING_0701FCC9 = "I can't verify this ordering rule from the approved media."
ORDERING_REFUSAL = {"error_class": "CreatorRenderContractError", "error_tail": ORDERING_0701FCC9}
SILENT_9734ED83 = (
    "I couldn't hear anyone talking in your clips, so I can't play what you say. Add a clip "
    "where you talk to the camera, or tell me to make it without speech."
)
GENERIC = object()


def _generic(reason: str) -> str:
    from app.tasks.content_plan_build import humanize_job_failure_reason

    return humanize_job_failure_reason(reason)


def _failed_replay(reason: str, detail: object, planning: dict | None) -> dict:
    """What ``summarize`` makes of a receipt whose replayed job failed this way."""

    import json
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        receipt = Path(tmp) / "replay-receipt.json"
        receipt.write_text(
            json.dumps(
                {
                    "job": {
                        "id": "6a3335a9-9286-4381-9544-0eb40e251847",
                        "status": "processing_failed",
                        "failure_reason": reason,
                        "error_detail": _generic(reason) if detail is GENERIC else detail,
                        "generic_error_detail": _generic(reason),
                        "pipeline_trace": _plan_failed_trace(planning),
                    }
                }
            )
        )
        return replay_job.summarize(receipt, Path(tmp))


def _production_render(reason: str, planning: dict | None = None, *, detail: object = None) -> dict:
    job = {
        "failure_reason": reason,
        "error_detail": _generic(reason) if detail is GENERIC else detail,
        "pipeline_trace": _plan_failed_trace(planning),
    }
    return {
        "failure_reason": reason,
        "planning_failure": replay_job.planning_failure(job),
        "error_detail": job["error_detail"],
    }


@pytest.mark.parametrize(
    ("case", "reason", "production", "replay", "expected", "named"),
    [
        (
            "51f7124f's text-fit gate, 40684a25's font gate: one class",
            "phone_plan_unsupported",
            (FIT_51F7124F, FIT_51F7124F["error_tail"]),
            (FONTS_40684A25, FONTS_40684A25["error_tail"]),
            "reproduction_unproven",
            [FIT_51F7124F["error_tail"], FONTS_40684A25["error_tail"]],
        ),
        (
            "07efa44c and 51f7124f: the same text-fit gate",
            "phone_plan_unsupported",
            (FIT_51F7124F, FIT_51F7124F["error_tail"]),
            (FIT_51F7124F, FIT_51F7124F["error_tail"]),
            "reproduced",
            [],
        ),
        (
            "3c312af4 incomplete, e7bf7099 could not compile: one class",
            "creation_text_failed",
            (INCOMPLETE_3C312AF4, GENERIC),
            (COMPILE_E7BF7099, GENERIC),
            "reproduction_unproven",
            ["Since the English translation", "could not compile"],
        ),
        (
            "3c312af4 and 4e8c925d: both incomplete, the model gave other reasons",
            "creation_text_failed",
            (INCOMPLETE_3C312AF4, GENERIC),
            (INCOMPLETE_4E8C925D, GENERIC),
            "reproduction_unproven",
            ["Since the English translation", "I can only split texts"],
        ),
        (
            "production recorded a planning error, the replay none",
            "phone_plan_unsupported",
            (FIT_51F7124F, FIT_51F7124F["error_tail"]),
            (None, ORDERING_0701FCC9),
            "reproduction_unproven",
            [FIT_51F7124F["error_tail"], "no planning error"],
        ),
        (
            "older job: 0701fcc9's refusal, the replay 9734ed83's",
            "phone_plan_unsupported",
            (None, ORDERING_0701FCC9),
            (None, SILENT_9734ED83),
            "reproduction_unproven",
            [ORDERING_0701FCC9, SILENT_9734ED83],
        ),
        (
            "older job: 0701fcc9's refusal again (as f88555bc's)",
            "phone_plan_unsupported",
            (None, ORDERING_0701FCC9),
            (ORDERING_REFUSAL, ORDERING_0701FCC9),
            "reproduced",
            [],
        ),
        (
            "older job whose error detail is the generic sentence for its reason",
            "phone_plan_failed",
            (None, GENERIC),
            (None, GENERIC),
            "reproduction_unproven",
            ["no planning error"],
        ),
        (
            "older job with no error detail",
            "phone_plan_failed",
            (None, None),
            (None, "gemini provider outcome unknown after 90.0s"),
            "reproduction_unproven",
            ["no planning error"],
        ),
    ],
)
def test_only_the_cause_production_recorded_makes_a_replay_its_failure(
    case: str,
    reason: str,
    production: tuple[dict | None, object],
    replay: tuple[dict | None, object],
    expected: str,
    named: list[str],
) -> None:
    """Verifier, pass 6 round 2: the error class is as much a catch-all as the reason.
    CreatorRenderContractError covers the text-fit, font and missing-text gates;
    CreationTextCompositionError covers every creation_text_failed job; and a production job
    recorded before planning errors were (0701fcc9, 9734ed83, 1e79ff4f) was compared on its
    reason alone though it stores a specific error detail.

    Ways this goes wrong: one class with another message reads as reproduced (P1, P2); a
    different error detail reads as reproduced (P3); a planning error on one side only reads as
    reproduced (P6); the app's generic sentence for the reason, the same for every failure with
    it, counts as a matching cause; production recording no cause at all reads as reproduced;
    the gap hides either side's message, so a model-worded difference cannot be judged; the
    same gate failing again (07efa44c and 51f7124f), or an older job's same refusal, stops
    reading as reproduced.
    """

    route = replay_job.compare_routes(
        _production(), _runs(("nova.creator.main", "ok"), ("nova.plan.clip_intent_planner", "ok"))
    )
    result = _failed_replay(reason, replay[1], replay[0])
    replay_job.classify_failure(
        result, route, _production_render(reason, production[0], detail=production[1])
    )

    assert result["outcome"] == "failed", case
    assert result["reproduction"] == expected, case
    assert result["production_failure_reproduced"] is (expected == "reproduced")
    gaps = " ".join(result["reproduction_gaps"])
    assert all(name in gaps for name in named), (case, gaps)
    assert bool(gaps) is (expected != "reproduced"), case


@pytest.mark.parametrize(
    ("case", "began", "runs", "expected", "named"),
    [
        (
            "P4: clip_intent_planner, not stored by production before 2026-10-31",
            "2026-10-21T13:19:00Z",
            [("nova.plan.clip_intent_planner", "terminal_schema", True)],
            "reproduction_unproven",
            ["nova.plan.clip_intent_planner", "terminal_schema"],
        ),
        (
            "P5: clip_question on the Creator session",
            "2026-11-10T07:00:00Z",
            [("nova.video.clip_question", "terminal_schema", False)],
            "reproduction_unproven",
            ["nova.video.clip_question", "terminal_schema"],
        ),
        (
            "the session-scoped agent failed, then got through",
            "2026-11-10T07:00:00Z",
            [
                ("nova.video.clip_question", "terminal_schema", False),
                ("nova.video.clip_question", "ok", False),
            ],
            "reproduced",
            [],
        ),
    ],
)
def test_a_failure_production_cannot_show_is_not_production_failure(
    monkeypatch, case: str, began: str, runs: list, expected: str, named: list[str]
) -> None:
    """Verifier P4/P5: an agent production's item trace cannot show (session-scoped, or not
    stored yet when the job ran) is left out of the route comparison, so when its last replay
    run failed, nothing said so and the matching cause read as reproduced.

    Ways this goes wrong: such a failure reads as reproduced; it is named as a replay-only
    agent or a divergence; an agent that failed and then got through blocks a reproduction.
    """

    trace = {
        "agent_runs": [{"agent_name": "nova.creator.main", "outcome": "ok", "model": GEMINI}],
        "next_cursor": None,
    }
    monkeypatch.setattr(replay_job, "prod_get", lambda path: trace)
    production = replay_job.production_agents(ITEM, [], until=None, began=began)
    recorded = _runs(("nova.creator.main", "ok"))
    for name, outcome, comparable in runs:
        recorded["runs"] += _runs((name, outcome), comparable=comparable)["runs"]
    route = replay_job.compare_routes(production, recorded)
    result = _failed_replay("phone_plan_unsupported", FIT_51F7124F["error_tail"], FIT_51F7124F)
    replay_job.classify_failure(
        result,
        route,
        _production_render(
            "phone_plan_unsupported", FIT_51F7124F, detail=FIT_51F7124F["error_tail"]
        ),
    )

    assert result["outcome"] == "failed", case
    assert route["replay_only_agents"] == [] and route["diverged_failures"] == [], case
    assert result["reproduction"] == expected, case
    gaps = " ".join(result["reproduction_gaps"])
    assert all(name in gaps for name in named), (case, gaps)


def test_an_agent_that_failed_only_in_an_earlier_production_job_still_failed_only_in_replay(
    monkeypatch,
) -> None:
    """Verifier P7: public_facts failed in an earlier production job of the item and passed in
    the failed job itself; the replay's last public_facts run failed. Production's outcomes
    were any run's, the replay's its last run's, so the replay's failure read as production's.

    Ways this goes wrong: an earlier production failure covers the replay's; a production
    failure in its last run of the agent stops counting.
    """

    def production_with(*outcomes: tuple[str, str]):
        monkeypatch.setattr(
            replay_job, "prod_get", lambda path: {"agent_runs": [], "next_cursor": None}
        )
        return replay_job.production_agents(
            ITEM,
            [
                {"agent_name": RESEARCH, "outcome": outcome, "model": GEMINI, "created_at": at}
                for outcome, at in outcomes
            ],
            until="2026-11-10T08:00:00Z",
            began="2026-11-09T06:00:00Z",
        )

    recorded = _runs((RESEARCH, "terminal_schema"))
    earlier_failure = production_with(
        ("ok", "2026-11-10T07:30:00Z"), ("terminal_schema", "2026-11-09T07:00:00Z")
    )
    route = replay_job.compare_routes(earlier_failure, recorded)
    assert [f["agent"] for f in route["failed_only_in_replay"]] == [RESEARCH]
    last_failure = production_with(
        ("ok", "2026-11-09T07:00:00Z"), ("terminal_schema", "2026-11-10T07:30:00Z")
    )
    assert replay_job.compare_routes(last_failure, recorded)["failed_only_in_replay"] == []


def test_a_failure_inside_an_agent_production_never_ran_is_not_reproduced() -> None:
    route = replay_job.compare_routes(
        _production(), _runs(("nova.plan.semantic_edit_proposal", "terminal_schema"))
    )
    result = {"outcome": "failed", "failure_reason": "phone_plan_unsupported", "error_detail": None}
    replay_job.classify_failure(result, route, {"failure_reason": "phone_plan_unsupported"})
    assert result["outcome"] == "diverged_from_production"
    assert result["production_failure_reproduced"] is False


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
    replay_job.classify_failure(result, route, {"failure_reason": "phone_plan_unsupported"})
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
    replay_job.classify_failure(result, route, {"failure_reason": "phone_plan_unsupported"})
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
