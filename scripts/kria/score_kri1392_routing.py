#!/usr/bin/env python3
"""Routing replay for KRI-1392: in-place edit or full re-plan, per message and per path.

Drives the REAL ``plan_live_turn`` over every recorded request-following turn plus an authored
edge corpus. The database is an in-memory stand-in and the three models are stubs: the copilot
answers with the turn's RECORDED output (parsed by the real ``EditCopilotAgent``), the brief
extractor either raises or returns a scope, and Main Creator only records that it was reached.
The script reads no planner internals, so the same command runs on the base and on the branch:

    cd src/apps/api
    .venv/bin/python ../../../scripts/kria/score_kri1392_routing.py --out /tmp/after.json
    (cd <base checkout>/src/apps/api && .venv/bin/python <this file> --out /tmp/before.json)
    .venv/bin/python ../../../scripts/kria/score_kri1392_routing.py \
        --compare /tmp/before.json /tmp/after.json

Paths (each is one ``plan_live_turn`` call per message):

* ``missing``: brief flag off, a render whose editor target cannot be opened
  (``no_ready_variant``).
* ``degrade``: brief flag on, the requirement extraction fails, the copilot answers (recorded
  turns).
* ``ready``: brief flag off, the editor target opens, the copilot answers (recorded turns).
* ``offer``: brief flag off, the editor target opens, and the previous reply offered a fresh
  version (the unsupported refusal's offer); the copilot would refuse again.

A decision is ``main_creator`` (the full re-plan agent ran), ``edit`` (editor ops staged), or the
reply's ``turn_value`` (``question``, ``recovery``), with ``+offer`` when the reply offers the
fresh version. ``degrade``'s ``question`` is the honest "couldn't read every change" recovery.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

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

# (id, message, what an attentive editor would do). The label is the independent expectation the
# report compares against: "edit" = change this video in place, "rebuild" = a new version.
AUTHORED: list[tuple[str, str, str]] = [
    ("font-different", "make the font different", "edit"),
    ("title-different-color", "give the title a different colour", "edit"),
    ("tr-font-different", "yazı tipini farklı yap", "edit"),
    ("de-font-different", "Nimm eine andere Schriftart", "edit"),
    ("text-faster", "Make the text faster", "edit"),
    ("video-shorter", "Make the video shorter", "edit"),
    ("shorter-each-text", "Make the video shorter, showing each text shorter", "edit"),
    ("revert", "Revert to the old version", "edit"),
    ("undo", "undo that", "edit"),
    ("speech-cut-quote", "Cut the part where I say 'um let me start over'.", "edit"),
    ("title-again", "make the title bigger again", "edit"),
    ("tr-title-again", "başlığı tekrar yap", "edit"),
    ("long-edit", "Make the title bigger. " * 15, "edit"),
    ("mixed", "Make the title red and then redo the whole video from scratch", "rebuild"),
    ("start-over", "start over", "rebuild"),
    ("redo-whole", "redo the whole thing", "rebuild"),
    ("new-version", "make me a new version", "rebuild"),
    ("again-prompt", "Create the video again based on my prompt", "rebuild"),
    ("vibe", "give me a completely different vibe", "rebuild"),
    ("tr-start-over", "baştan yap", "rebuild"),
    ("tr-new-version", "yeni bir versiyon istiyorum", "rebuild"),
    ("de-start-over", "Mach das Video komplett neu", "rebuild"),
    ("es-start-over", "Hazlo de nuevo desde cero", "rebuild"),
    ("accept-redo", "redo", "rebuild"),
    ("accept-redo-tr", "yeniden yap", "rebuild"),
    ("accept-redo-de", "nochmal", "rebuild"),
]
_OFFER_EN = (
    ' If you want a fresh version built from your whole request, reply "redo" and I\'ll '
    "render it again."
)


def _bootstrap(api: Path) -> None:
    for key, value in _SAFE_ENV.items():
        os.environ[key] = value
    sys.path.insert(0, str(api))
    os.chdir(api)


def _recorded_turns(api: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted((api / "tests/fixtures/request_following/threads").glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for turn in raw.get("turns") or []:
            copilot = turn.get("copilot") or {}
            if turn.get("engine") == "recorded" or not turn.get("user_message"):
                continue
            rows.append(
                {
                    "id": f"{path.stem}/{turn['turn_id']}",
                    "message": turn["user_message"],
                    "copilot": copilot if "raw_text" in copilot else None,
                }
            )
    return rows


def _copilot_response(copilot: dict[str, Any] | None):
    """The recorded copilot output through the real parser and the real honest-outcome gate."""
    from app.agents.edit_copilot import EditCopilotAgent
    from app.routes._copilot import CopilotTurnResponse, _honest_outcome
    from tests.evals.runners.eval_runner import CassetteModelClient, build_eval_run_context

    if copilot is None:
        return None
    output = EditCopilotAgent(CassetteModelClient(str(copilot["raw_text"]))).run(
        copilot["agent_input"], ctx=build_eval_run_context("kri1392", is_live=False)
    )
    ops = [] if (output.needs_clarification or output.intent != "edit") else output.ops
    outcome, reply = _honest_outcome(output, ops, supports_proposed=True)
    return CopilotTurnResponse(
        intent=output.intent,
        ops=ops,
        confidence=output.confidence,
        reply=reply,
        outcome=outcome,
        needs_clarification=outcome == "clarification",
        rejection_reasons=output.rejection_reasons,
        unmet_requests=output.unmet_requests,
        reply_notes=output.reply_notes,
    )


class _ReachedMainCreator(Exception):
    pass


def _wire(mp, *, brief_on: bool, target: bool, copilot, extraction_fails: bool, last_reply: str):
    from app.agents._runtime import TerminalSchemaError
    from app.config import settings
    from app.kria import planner
    from tests.kria.test_creative_brief import _MANIFEST, _ExpiringItem, _planner_db

    item = _ExpiringItem(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        current_job_id=uuid.uuid4(),
        edit_format="montage",
    )
    db, creator_id = _planner_db(item)

    def _result(sql: str):
        # The latest-reply read selects the event text and nothing else.
        reply_query = sql.split("FROM")[0].split() == ["SELECT", "creation_thread_events.content"]
        rows = [last_reply] if reply_query and last_reply else []
        return SimpleNamespace(
            all=lambda: [],
            scalars=lambda: SimpleNamespace(all=lambda: rows),
            scalar_one_or_none=lambda: None,
        )

    async def execute(statement, *_a, **_k):  # noqa: ANN001, ANN202
        return _result(str(statement))

    db.execute = AsyncMock(side_effect=execute)
    mp.setattr(settings, "kria_creative_brief_enabled", brief_on)
    mp.setattr(settings, "kria_brief_binding_enabled", False)
    mp.setattr(settings, "clip_intents_enabled", False)
    mp.setattr(settings, "kria_clip_understanding_enabled", False)
    manifest = _MANIFEST.model_copy(update={"item_id": str(item.id)})
    mp.setattr(planner, "resolve_item_creator_context", AsyncMock(return_value=(manifest, [])))
    mp.setattr(planner, "creator_context", lambda *_a: ("creator", "item"))
    mp.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    mp.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    mp.setattr(planner, "_copilot_clip_context", AsyncMock(return_value=None))
    mp.setattr(planner, "_attach_chat_undo", AsyncMock(return_value=None))
    editor_target = planner._EditorTarget(
        job_id=uuid.uuid4(),
        snapshot={"allowed_op_families": ["text", "clip"], "text_bars": []},
        conversation=[],
    )

    async def load_target(*_a, **_k):  # noqa: ANN202
        if target:
            return editor_target
        planner._editor_target_miss.set("no_ready_variant")
        return None

    mp.setattr(planner, "_load_editor_target", AsyncMock(side_effect=load_target))
    mp.setattr(planner, "run_copilot_turn", AsyncMock(return_value=copilot))

    class MainCreator:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            raise _ReachedMainCreator

    class Extractor:
        def __init__(self, _client) -> None:  # noqa: ANN001
            pass

        def run(self, *_a, **_k):  # noqa: ANN002, ANN003, ANN201
            if extraction_fails:
                raise TerminalSchemaError("offline: extraction failed")
            return SimpleNamespace(brief_updates=[], request_scope="edit")

    mp.setattr(planner, "MainCreatorAgent", MainCreator)
    mp.setattr(planner, "BriefExtractorAgent", Extractor)
    mp.setattr(planner, "default_client", lambda: object())
    return db, item, creator_id


def _decision(planned) -> str:  # noqa: ANN001
    plan = planned.plan
    if plan.mode == "act" and any(
        getattr(i, "tool_name", None) == "draft.apply_editor_ops" for i in plan.intents or []
    ):
        return "edit"
    response = plan.response or ""
    offers = '"redo"' in response or '"yeniden yap"' in response
    return f"{plan.turn_value}+offer" if offers else str(plan.turn_value)


async def _run(message: str, **wiring) -> str:
    import pytest

    from app.kria import planner

    with pytest.MonkeyPatch.context() as mp:
        db, item, creator_id = _wire(mp, **wiring)
        try:
            planned = await planner.plan_live_turn(
                db,
                thread_id=uuid.uuid4(),
                item_id=item._fields["id"],
                creator_id=creator_id,
                user_message=message,
            )
        except _ReachedMainCreator:
            return "main_creator"
        except Exception as exc:  # noqa: BLE001 - a crash is a decision worth reporting
            return f"error:{type(exc).__name__}"
    return _decision(planned)


def _unsupported():
    from app.routes._copilot import CopilotTurnResponse

    return CopilotTurnResponse(
        intent="reject",
        confidence=1.0,
        reply="I can't rebuild the whole video from here.",
        outcome="unsupported",
    )


async def score(api: Path, *, recorded: bool = True) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    corpus = [
        {"id": f"authored/{key}", "message": message, "expected": label, "copilot": None}
        for key, message, label in AUTHORED
    ] + (_recorded_turns(api) if recorded else [])
    for case in corpus:
        message = case["message"]
        response = _copilot_response(case["copilot"]) if case["copilot"] else None
        paths = {
            "missing": await _run(
                message,
                brief_on=False,
                target=False,
                copilot=None,
                extraction_fails=False,
                last_reply="",
            ),
            "offer": await _run(
                message,
                brief_on=False,
                target=True,
                copilot=_unsupported(),
                extraction_fails=False,
                last_reply="That kind of edit isn't available for this draft yet." + _OFFER_EN,
            ),
        }
        if response is not None:
            paths["ready"] = await _run(
                message,
                brief_on=False,
                target=True,
                copilot=response,
                extraction_fails=False,
                last_reply="",
            )
            paths["degrade"] = await _run(
                message,
                brief_on=True,
                target=True,
                copilot=response,
                extraction_fails=True,
                last_reply="",
            )
        rows[case["id"]] = {
            "message": message[:120],
            **({"expected": case["expected"]} if "expected" in case else {}),
            "paths": paths,
        }
    return rows


def compare(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    changed = []
    for key, row in after.items():
        old = (before.get(key) or {}).get("paths") or {}
        for path, decision in row["paths"].items():
            if old.get(path) != decision:
                changed.append(
                    {
                        "id": key,
                        "path": path,
                        "message": row["message"],
                        "expected": row.get("expected"),
                        "before": old.get(path),
                        "after": decision,
                    }
                )
    return changed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = parser.parse_args(argv)
    if args.compare:
        before, after = (json.loads(p.read_text(encoding="utf-8")) for p in args.compare)
        changed = compare(before, after)
        print(json.dumps(changed, indent=2, ensure_ascii=False))
        print(f"{len(changed)} routing decisions changed over {len(after)} messages")
        return 0
    api = Path.cwd()
    _bootstrap(api)
    import logging

    from structlog.testing import capture_logs

    logging.disable(logging.CRITICAL)
    with capture_logs():
        rows = asyncio.run(score(api))
    text = json.dumps(rows, indent=2, ensure_ascii=False) + "\n"
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    totals: dict[str, dict[str, int]] = {}
    for row in rows.values():
        for path, decision in row["paths"].items():
            totals.setdefault(path, {}).setdefault(decision, 0)
            totals[path][decision] += 1
    print(json.dumps(totals, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
