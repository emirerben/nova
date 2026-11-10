#!/usr/bin/env python3
"""Scorecard for KRI-1365 (prod thread 147eb928, item fac3dbd4, 2026-11-10).

After the creator approved the wording and a 38.1 s video was ready, "Make the text faster",
"Make the video shorter, showing each text shorter" and "Make the video shorter" were each
replanned from scratch with a new approval. The in-place edit ops already exist. This replays
those follow-ups through the REAL ``plan_live_turn`` and the REAL ``_load_editor_target``; only
the database rows (an in-memory stand-in built from ``fixture.json``) and the two model calls are
replaced. Offline: a socket guard fails any network attempt.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_kri1365_edit_route.py
    make kria-replay FIXTURE=kri1365_edit_route

Model outputs are assumed, never recorded (prod's copilot never ran on this thread): the brief
extractor reads each follow-up as an ``edit`` with a timing requirement, and the copilot's ops
are parsed by the real ``EditCopilotAgent.parse`` against the snapshot the planner built.

Checks (VERIFIED / NOT VERIFIED / INCONCLUSIVE):

1-3. Each follow-up on the ready video reaches ``draft.apply_editor_ops`` with no Main Creator
     call and no ``render.request``.
4.   "Revert to the old version. ..." after the 18:53 replan's render failed with no variant
     edits the last ready video (the copilot is called for the ready job, not the failed one).
5.   The compiled drafts are shorter (total and per-word windows), keep every word, and pass the
     copy gate the draft commit runs.
6.   Control: while a wording question is still open the follow-up still goes to Main Creator.
7.   Wrong-content control: an op that rewrites the approved words is refused by that gate.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import inspect
import json
import os
import socket
import sys
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"
FIXTURE = (
    API / "tests" / "fixtures" / "kria_replay" / "kri1365_edit_route" / "fixture.json"
)

_SAFE_ENV = {
    "STORAGE_BUCKET": "offline-scorecard",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://localhost:5432/offline",
    "REDIS_URL": "redis://localhost:6379",
    "OPENAI_API_KEY": "offline",
    "GEMINI_API_KEY": "offline-scorecard",
    "TOKEN_ENCRYPTION_KEY": "offline",
    "INTERNAL_API_KEY": "offline",
    "ALLOWED_ORIGINS": '["http://localhost:3000"]',
}
SHORTER_SHARE = 0.7


class OfflineViolation(RuntimeError):
    pass


class ReachedMainCreator(BaseException):
    """The turn reached the full re-plan agent, whose output becomes ``draft.apply_strategy``."""


def _bootstrap() -> None:
    for key, value in _SAFE_ENV.items():
        os.environ[key] = value
    if str(API) not in sys.path:
        sys.path.insert(0, str(API))

    def _blocked(*_args: Any, **_kwargs: Any):  # noqa: ANN202
        raise OfflineViolation("network access is blocked in the offline scorecard")

    socket.socket.connect = _blocked  # type: ignore[method-assign]
    socket.create_connection = _blocked  # type: ignore[assignment]


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _flags() -> contextlib.ExitStack:
    from app.config import settings

    stack = contextlib.ExitStack()
    for name, value in {
        "kria_runtime_v2_enabled": True,
        "phone_rendering_enabled": True,
        "kria_creative_brief_enabled": True,
        "kria_creative_brief_user_ids": [],
        "kria_brief_binding_enabled": True,
        "kria_brief_binding_user_ids": [],
        "kria_choice_questions_enabled": True,
        "kria_plan_authority_enabled": True,
        "kria_clip_understanding_enabled": False,
        "clip_intents_enabled": True,
    }.items():
        stack.enter_context(patch.object(settings, name, value))
    return stack


def _sequence_ids(fx: dict, prefix: str) -> list[str]:
    return [
        row["id"]
        for row in fx["variant"]["text_elements"]
        if row["id"].startswith(prefix)
    ]


def copilot_raw(fx: dict, message: str, total_s: float) -> dict:
    """The copilot answer the prompt teaches for each follow-up (assumed model output)."""
    target = round(total_s * SHORTER_SHARE, 1)
    shorter = {
        "op": "set_total_duration",
        "target_s": target,
        "strategy": "proportional",
    }
    retime = [
        {
            "op": "retime_text_sequence",
            "selector": {"ids": _sequence_ids(fx, prefix)},
            "start_s": 0,
            "end_s": target,
            "gap_policy": "scale",
        }
        for prefix in ("title-tr", "translation-en")
    ]
    lowered = message.casefold()
    if "text" in lowered and "video" not in lowered:
        ops = retime
    elif "text" in lowered:
        ops = [shorter, *retime]
    else:
        ops = [shorter]
    return {
        "intent": "edit",
        "ops": ops,
        "confidence": 0.9,
        "reply": f"Made it about {target:g} seconds.",
        "suggestions": [],
        "needs_clarification": False,
    }


def wording_events(fx: dict, media: dict, *, approved: bool) -> list:
    from app.services.creative_copy_decisions import media_digest, wording_question

    question = wording_question(
        target="opening_title",
        candidate=fx["approved_copy"]["text"],
        dependency_digest=media_digest(media),
    )
    events: list = [("assistant", {"choice_question": question})]
    if approved:
        events.append(
            (
                "user",
                {
                    "choice_selection": {
                        "question_id": question["question_id"],
                        "option_key": "approve",
                    }
                },
            )
        )
    return events


def _world(fx: dict, *, current_failed: bool):  # noqa: ANN202
    """In-memory rows for the item, its thread, its editor session and both jobs."""
    from app.models import (
        ContentPlan,
        CreationThread,
        CreatorAgentSession,
        Job,
        Persona,
        PlanItem,
    )

    ids = {key: uuid.UUID(value) for key, value in fx["ids"].items()}
    ready = Job(
        id=ids["ready_job_id"],
        user_id=ids["creator_id"],
        content_plan_item_id=ids["item_id"],
        status="variants_ready",
        assembly_plan={"variants": [copy.deepcopy(fx["variant"])]},
        all_candidates={"clip_paths": [clip["gcs_path"] for clip in fx["clips"]]},
    )
    failed = Job(
        id=ids["failed_job_id"],
        user_id=ids["creator_id"],
        content_plan_item_id=ids["item_id"],
        status="processing_failed",
        failure_reason="creation_text_failed",
        assembly_plan={},
        all_candidates={"clip_paths": [clip["gcs_path"] for clip in fx["clips"]]},
    )
    current = failed if current_failed else ready
    item = SimpleNamespace(
        id=ids["item_id"],
        content_plan_id=uuid.uuid4(),
        current_job_id=current.id,
        edit_format="montage",
        clip_gcs_paths=[clip["gcs_path"] for clip in fx["clips"]],
        clip_assignments=[
            {
                "media_id": clip["media_id"],
                "gcs_path": clip["gcs_path"],
                "generation": "1",
            }
            for clip in fx["clips"]
        ],
    )
    plan = SimpleNamespace(
        id=item.content_plan_id, user_id=ids["creator_id"], persona_id=uuid.uuid4()
    )
    persona = SimpleNamespace(user_id=ids["creator_id"])
    variant_id = fx["variant"]["variant_id"]
    session = SimpleNamespace(
        id=ids["session_id"],
        creator_id=ids["creator_id"],
        plan_item_id=item.id,
        target_job_id=current.id,
        target_variant_id=variant_id,
        target_generation_id=None
        if current_failed
        else fx["variant"]["render_generation_id"],
        last_good={
            "job_id": str(ready.id),
            "variant_id": variant_id,
            "render_generation_id": fx["variant"]["render_generation_id"],
            "draft_id": None,
        },
    )
    thread = SimpleNamespace(
        id=ids["thread_id"],
        creator_id=ids["creator_id"],
        active_creator_agent_session_id=session.id,
        active_plan_item_id=item.id,
        state=None,
    )
    rows = {
        (PlanItem, item.id): item,
        (ContentPlan, plan.id): plan,
        (Persona, plan.persona_id): persona,
        (CreationThread, thread.id): thread,
        (CreatorAgentSession, session.id): session,
        (Job, ready.id): ready,
        (Job, failed.id): failed,
    }

    async def get(model, identifier, **_kwargs):  # noqa: ANN001, ANN202
        return rows.get((model, identifier))

    class _Rows(list):
        def all(self):  # noqa: ANN202
            return list(self)

    result = SimpleNamespace(
        all=lambda: [],
        scalars=lambda: _Rows(),
        scalar_one_or_none=lambda: None,
        scalar=lambda: None,
        first=lambda: None,
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(return_value=result),
        scalar=AsyncMock(return_value=None),
        rollback=AsyncMock(),
        commit=AsyncMock(),
        begin_nested=lambda: contextlib.nullcontext(),
    )
    return db, item, thread, ready


async def replay_turn(
    fx: dict, message: str, *, current_failed: bool = False, open_question: bool = False
) -> dict:
    from app.agents._model_client import ModelClient
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        ResolvedCreatorManifest,
    )
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.kria import planner
    from app.kria.brief import BriefUpdate
    from app.kria.brief_binding import snapshot_media

    db, item, thread, ready = _world(fx, current_failed=current_failed)
    calls: list[str] = []
    copilot_jobs: list[str] = []
    events = wording_events(fx, snapshot_media(item), approved=not open_question)
    manifest = ResolvedCreatorManifest(
        item_id=str(item.id),
        edit_format="montage",
        render_program="guided",
        capabilities={"dispatch_render": CapabilityAvailability(available=True)},
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )

    async def extractor(*_args: Any, **_kwargs: Any):  # noqa: ANN202
        calls.append("nova.brief.extractor")
        update = BriefUpdate(
            kind="timing",
            scope="global",
            description=message,
            facts={"editor_timing_only": True},
        )
        return SimpleNamespace(
            brief_updates=[update], request_scope="edit", clarification=None
        )

    async def main_creator(*_args: Any, **_kwargs: Any):  # noqa: ANN202
        calls.append("nova.creator.main")
        raise ReachedMainCreator

    async def copilot(body, *, job_id, **_kwargs):  # noqa: ANN001, ANN202
        calls.append("nova.edit.copilot")
        copilot_jobs.append(str(job_id))
        snapshot = body.snapshot
        raw = copilot_raw(fx, message, float(snapshot.get("total_duration_s") or 0))
        parsed = EditCopilotAgent(ModelClient()).parse(
            json.dumps(raw),
            EditCopilotInput(
                utterance=message, prior_turns=[], variant_snapshot=snapshot
            ),
        )
        return SimpleNamespace(
            ops=parsed.ops,
            outcome="proposed",
            reply=raw["reply"],
            intent="edit",
            rejection_reasons=[],
            unmet_requests=[],
            reply_notes=None,
        )

    patches = [
        patch.object(planner, "_load_thread_events", AsyncMock(return_value=events)),
        patch.object(planner, "load_latest_brief", AsyncMock(return_value=None)),
        patch.object(
            planner,
            "resolve_item_creator_context",
            AsyncMock(return_value=(manifest, [])),
        ),
        patch.object(planner, "creator_context", lambda *_a: ("creator", "item")),
        patch.object(planner, "_copilot_clip_context", AsyncMock(return_value=None)),
        patch.object(planner, "_call_brief_extractor", extractor),
        patch.object(planner, "_call_main_creator", main_creator),
        patch.object(planner, "run_copilot_turn", copilot),
        patch.object(planner, "default_client", lambda: object()),
    ]
    outcome: dict[str, Any] = {
        "message": message,
        "tools": [],
        "ops": [],
        "error": None,
    }
    with _flags(), contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        try:
            planned = await planner.plan_live_turn(
                db,
                thread_id=thread.id,
                item_id=item.id,
                creator_id=thread.creator_id,
                user_message=message,
            )
            outcome["route"] = planned.brief_route
            outcome["mode"] = planned.plan.mode
            outcome["reply"] = planned.plan.response
            for intent in planned.plan.intents:
                outcome["tools"].append(intent.tool_name)
                ops = (intent.arguments or {}).get("operations") or []
                outcome["ops"] += [op.get("op") for op in ops]
                if intent.tool_name == "draft.apply_editor_ops":
                    outcome["operations"] = ops
        except ReachedMainCreator:
            outcome["route"] = "replan"
            outcome["tools"].append("draft.apply_strategy (Main Creator re-plan)")
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"
    outcome["model_calls"] = calls
    outcome["copilot_job"] = (
        "ready"
        if copilot_jobs and copilot_jobs[-1] == str(ready.id)
        else ("failed" if copilot_jobs else None)
    )
    return outcome


def _edits_in_place(outcome: dict) -> bool:
    return (
        outcome["tools"] == ["draft.apply_editor_ops"]
        and "nova.creator.main" not in outcome["model_calls"]
        and "render.request" not in outcome["tools"]
    )


def _lineages(rows: list[dict]) -> dict[str, tuple[float, float, str]]:
    from app.services.kria_editor_ops_text import sequence_source

    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if not row.get("removed"):
            grouped.setdefault(sequence_source(row) or row["id"], []).append(row)
    return {
        key: (
            min(float(r["start_s"]) for r in group),
            max(float(r["end_s"]) for r in group),
            " ".join(
                r["text"] for r in sorted(group, key=lambda r: float(r["start_s"]))
            ),
        )
        for key, group in grouped.items()
    }


def copy_gate(fx: dict, before: list[dict], after: list[dict]) -> str | None:
    """The copy check the draft commit runs on this editor draft (``None`` = admitted)."""
    from app.kria.brief_binding import snapshot_media
    from app.services.creative_copy_gate import creative_copy_problem

    _db, item, _thread, _ready = _world(fx, current_failed=False)
    media = snapshot_media(item)
    events = wording_events(fx, media, approved=True)
    if "editor_text" in inspect.signature(creative_copy_problem).parameters:
        return creative_copy_problem(events, media, editor_text=(before, after))
    return creative_copy_problem(events, media)


def compile_draft(fx: dict, operations: list[dict]) -> dict:
    from app.models import Job
    from app.services.kria_editor_ops import compile_editor_ops

    variant = copy.deepcopy(fx["variant"])
    job = Job(
        id=uuid.UUID(fx["ids"]["ready_job_id"]),
        status="variants_ready",
        assembly_plan={"variants": [variant]},
        all_candidates={"clip_paths": [clip["gcs_path"] for clip in fx["clips"]]},
    )
    compiled = compile_editor_ops(job, variant, operations)
    payload = compiled.payload.model_dump(mode="json", exclude_none=True)
    after_rows = payload.get("text_elements") or variant["text_elements"]
    slots = payload.get("timeline_slots")
    total = (
        round(sum(s["duration_s"] for s in slots if not s.get("removed")), 3)
        if slots
        else float(variant["duration_s"])
    )
    before = _lineages(variant["text_elements"])
    after = _lineages(after_rows)
    return {
        "total_before_s": float(variant["duration_s"]),
        "total_after_s": total,
        "text_windows": {
            key: {"before": before[key][:2], "after": after[key][:2]} for key in before
        },
        "words_kept": {
            key: before[key][2] == after.get(key, (0, 0, None))[2] for key in before
        },
        "copy_gate": copy_gate(fx, variant["text_elements"], after_rows),
    }


def _result(
    check_id: int, name: str, passed: bool, why: list[str], evidence: dict
) -> dict:
    return {
        "id": check_id,
        "name": name,
        "verdict": "VERIFIED" if passed else "NOT VERIFIED",
        "why": why,
        "evidence": evidence,
    }


def _guarded(check_id: int, name: str, fn) -> dict:  # noqa: ANN001
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {
            "id": check_id,
            "name": name,
            "verdict": "INCONCLUSIVE",
            "why": [f"{type(exc).__name__}: {exc}"],
            "evidence": {"traceback": traceback.format_exc()[-2000:]},
        }


def check_in_place(
    fx: dict, check_id: int, key: str, *, current_failed: bool = False
) -> dict:
    message = fx["messages"][key]
    outcome = asyncio.run(replay_turn(fx, message, current_failed=current_failed))
    why = []
    if not _edits_in_place(outcome):
        why.append(
            f"route={outcome.get('route')} tools={outcome['tools']} "
            f"model_calls={outcome['model_calls']} error={outcome['error']}"
        )
    if current_failed and outcome["copilot_job"] != "ready":
        why.append(
            f"the edit did not target the last ready video (copilot_job={outcome['copilot_job']})"
        )
    name = f'"{message}" edits the {"last ready" if current_failed else "ready"} video in place'
    evidence = {k: v for k, v in outcome.items() if k != "operations"}
    return _result(check_id, name, not why, why, evidence)


def check_compiled(fx: dict) -> dict:
    why: list[str] = []
    drafts = {}
    for key in ("t1853", "shorter_each_text", "t1937"):
        message = fx["messages"][key]
        raw = copilot_raw(fx, message, float(fx["variant"]["duration_s"]))
        from app.agents._model_client import ModelClient
        from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
        from app.models import Job
        from app.services.kria_editor_ops import build_editor_snapshot

        variant = copy.deepcopy(fx["variant"])
        job = Job(
            id=uuid.UUID(fx["ids"]["ready_job_id"]),
            status="variants_ready",
            assembly_plan={"variants": [variant]},
            all_candidates={"clip_paths": [clip["gcs_path"] for clip in fx["clips"]]},
        )
        snapshot = build_editor_snapshot(job, variant)
        ops = (
            EditCopilotAgent(ModelClient())
            .parse(
                json.dumps(raw),
                EditCopilotInput(
                    utterance=message, prior_turns=[], variant_snapshot=snapshot
                ),
            )
            .ops
        )
        draft = compile_draft(fx, ops)
        drafts[message] = draft
        if draft["copy_gate"] is not None:
            why.append(
                f'"{message}": the copy gate refuses the draft: {draft["copy_gate"]}'
            )
        if not all(draft["words_kept"].values()):
            why.append(f'"{message}": words changed: {draft["words_kept"]}')
        if (
            "video" in message.casefold()
            and not draft["total_after_s"] < draft["total_before_s"]
        ):
            why.append(f'"{message}": the video is not shorter')
        if "text" in message.casefold():
            for lineage, window in draft["text_windows"].items():
                if (
                    not window["after"][1] - window["after"][0]
                    < window["before"][1] - window["before"][0]
                ):
                    why.append(f'"{message}": {lineage} is not shown shorter')
    return _result(
        5,
        "drafts are shorter, keep every word, and pass the copy gate",
        not why,
        why,
        drafts,
    )


def check_open_question(fx: dict) -> dict:
    outcome = asyncio.run(replay_turn(fx, fx["messages"]["t1937"], open_question=True))
    passed = (
        "nova.creator.main" in outcome["model_calls"]
        and "nova.edit.copilot" not in outcome["model_calls"]
    )
    why = (
        []
        if passed
        else [f"an open wording question no longer reaches Main Creator: {outcome}"]
    )
    return _result(
        6,
        "control: an open wording question still goes to Main Creator",
        passed,
        why,
        outcome,
    )


def check_wrong_content(fx: dict) -> dict:
    before = fx["variant"]["text_elements"]
    after = copy.deepcopy(before)
    for row in after:
        if row["id"] == "title-tr::sequence-3":
            row["text"] = "dünyada"
    refusal = copy_gate(fx, before, after)
    passed = refusal is not None
    why = (
        []
        if passed
        else ["a draft that rewrites an approved word passes the copy gate"]
    )
    return _result(
        7,
        "wrong-content control: rewriting an approved word is refused",
        passed,
        why,
        {"rewritten": "cihanda -> dünyada", "refusal": refusal},
    )


def run_offline() -> dict:
    fx = load_fixture()
    checks = [
        _guarded(1, "text faster", lambda: check_in_place(fx, 1, "t1853")),
        _guarded(2, "video shorter", lambda: check_in_place(fx, 2, "t1937")),
        _guarded(
            3,
            "shorter + each text shorter",
            lambda: check_in_place(fx, 3, "shorter_each_text"),
        ),
        _guarded(
            4,
            "revert after failed render",
            lambda: check_in_place(fx, 4, "t1905", current_failed=True),
        ),
        _guarded(5, "compiled drafts", lambda: check_compiled(fx)),
        _guarded(6, "open question control", lambda: check_open_question(fx)),
        _guarded(7, "wrong-content control", lambda: check_wrong_content(fx)),
    ]
    return {
        "fixture": fx["fixture_id"],
        "incident": fx["incident"],
        "evidence_level": "offline replay (real planner, editor target loader, copilot parser, "
        "op compiler and copy gate; assumed model outputs)",
        "checks": checks,
        "summary": {
            verdict: sum(1 for c in checks if c["verdict"] == verdict)
            for verdict in ("VERIFIED", "NOT VERIFIED", "INCONCLUSIVE")
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    _bootstrap()
    report = run_offline()
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    for check in report["checks"]:
        print(f"{check['id']}. {check['verdict']:<13} {check['name']}")
        for line in check["why"]:
            print(f"     - {line}")
    print(json.dumps(report["summary"]))
    return 0 if report["summary"]["VERIFIED"] == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
