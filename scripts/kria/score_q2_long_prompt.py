#!/usr/bin/env python3
"""Scorecard for item 23: a long creation prompt (17 separate asks) planned in one go.

Runs the REAL ``planner.plan_live_turn`` (first chat turn, no rendered job yet) with the REAL
``MainCreatorAgent``; only the model client is replaced by a cassette that answers every call
with the authored answer in ``tests/fixtures/kria_replay/q2-long-prompt/fixture.json`` (no model
is called, a socket guard fails any network attempt). The turn is run twice:

* flag off (``BRIEF_LONG_REQUEST_ENABLED`` unset): must equal ``flag_off_golden.json``, which
  was recorded on the code before the flag existed (today: the batch of 17 is refused, the
  agent retries twice, the planner returns ``creator_planning_failed``);
* flag on: all 17 asks must reach the brief, in order, from exactly one model call, with no
  recovery.

    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_q2_long_prompt.py
    cd src/apps/api && .venv/bin/python ../../../scripts/kria/score_q2_long_prompt.py --write-golden
    make kria-replay FIXTURE=q2-long-prompt

Exit 0 only when the flag-off run equals the golden AND the flag-on run kept every ask.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import socket
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

REPO = Path(__file__).resolve().parents[2]
API = REPO / "src" / "apps" / "api"
FIXTURE_DIR = API / "tests" / "fixtures" / "kria_replay" / "q2-long-prompt"
FIXTURE = FIXTURE_DIR / "fixture.json"
GOLDEN = FIXTURE_DIR / "flag_off_golden.json"
FLAG = "brief_long_request_enabled"

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


class OfflineViolation(RuntimeError):
    """The harness tried to reach the network."""


class FlagNotAvailable(RuntimeError):
    """The code under test predates ``BRIEF_LONG_REQUEST_ENABLED``."""


def bootstrap() -> None:
    """Script entry only: fill in safe settings env and make ``app`` importable."""
    for key, value in _SAFE_ENV.items():
        os.environ[key] = value
    if str(API) not in sys.path:
        sys.path.insert(0, str(API))


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _manifest(fx: dict[str, Any], item_id: str):  # noqa: ANN202
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        CreatorMediaRef,
        ResolvedCreatorManifest,
    )

    on = CapabilityAvailability(available=True)
    return ResolvedCreatorManifest(
        item_id=item_id,
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id=c["id"], kind=c["kind"], duration_s=c["duration_s"])
            for c in fx["clips"]
        ],
        capabilities={
            "dispatch_render": on,
            "draft_guided_proposal": on,
            "edit_format:montage": on,
        },
        context_hash="a" * 64,
        manifest_hash="b" * 64,
    )


def agent_input(fx: dict[str, Any]):  # noqa: ANN202
    """The Main Creator input the fixture's turn sees (also used to render its prompt)."""
    from app.agents.main_creator import MainCreatorInput

    return MainCreatorInput(
        user_message=fx["user_message"],
        media_context=[
            {"media_id": c["id"], "kind": c["kind"], "duration_s": c["duration_s"]}
            for c in fx["clips"]
        ],
        conversation=[{"role": "user", "content": fx["user_message"]}],
        capability_manifest=_manifest(fx, "q2-item"),
        brief_enabled=True,
    )


def _planner_item(fx: dict[str, Any]):  # noqa: ANN202
    """An in-memory first-turn item (no rendered job) and a session that serves it."""
    from app.models import ContentPlan, CreationThread, Job, Persona, PlanItem

    rows = [
        {
            "media_id": c["id"],
            "gcs_path": f"users/redacted/creation-threads/redacted/{c['id']}",
            "kind": c["kind"],
            **({"duration_s": c["duration_s"]} if c["duration_s"] else {}),
            "generation": "1",
            "has_analysis": True,
        }
        for c in fx["clips"]
    ]
    item = SimpleNamespace(
        id=uuid.uuid4(),
        content_plan_id=uuid.uuid4(),
        current_job_id=None,
        edit_format="montage",
        clip_gcs_paths=[r["gcs_path"] for r in rows],
        clip_assignments=rows,
    )
    creator_id = uuid.uuid4()
    plan = SimpleNamespace(id=item.content_plan_id, user_id=creator_id, persona_id=uuid.uuid4())
    persona = SimpleNamespace(user_id=creator_id)
    thread = SimpleNamespace(active_creator_agent_session_id=None)

    async def get(model, _identifier, **_kwargs):  # noqa: ANN001, ANN202
        return {
            PlanItem: item,
            ContentPlan: plan,
            Persona: persona,
            CreationThread: thread,
            Job: SimpleNamespace(status="done"),
        }[model]

    class _Rows(list):
        def all(self):  # noqa: ANN202
            return list(self)

    db = SimpleNamespace(
        get=AsyncMock(side_effect=get),
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: [], scalars=lambda: _Rows())),
        rollback=AsyncMock(),
        commit=AsyncMock(),
    )
    return db, item, creator_id


def _cassette(raw_texts: list[str], prompts: list[str]):  # noqa: ANN202
    """A model client that answers call N with ``raw_texts[min(N, len - 1)]``, recording prompts."""
    from app.agents._runtime import ModelClient, ModelInvocation

    class _Authored(ModelClient):
        def invoke(self, *, prompt: str, **_kwargs: Any) -> ModelInvocation:
            raw_text = raw_texts[min(len(prompts), len(raw_texts) - 1)]
            prompts.append(prompt)
            return ModelInvocation(raw_text=raw_text)

    return _Authored()


def _brief_shape(updates: Any) -> list[dict[str, Any]]:
    return [
        {
            "kind": u.kind,
            "scope": u.scope,
            "literal": u.literal,
            "description": u.description,
            "facts": u.facts,
        }
        for u in updates
    ]


def run_turn(*, long_request: bool, raw_texts: list[str] | None = None) -> dict[str, Any]:
    """One first chat turn through the real planner and Main Creator, model replaced by a cassette.

    ``long_request`` sets ``settings.brief_long_request_enabled``. The flag-off run leaves an
    absent attribute alone (it is the default); the flag-on run raises ``FlagNotAvailable`` on
    code that has no such setting. ``raw_texts`` scripts the model: call N gets
    ``raw_texts[min(N, len - 1)]`` (default: the fixture's single answer for every call); when
    given, the result also carries every ``prompts`` the model saw.
    """
    from app.config import settings
    from app.kria import planner
    from app.kria.brief import apply_updates, render_brief_request

    fx = load_fixture()
    has_flag = hasattr(settings, FLAG)
    if long_request and not has_flag:
        raise FlagNotAvailable("code predates BRIEF_LONG_REQUEST_ENABLED")
    prompts: list[str] = []
    db, item, creator_id = _planner_item(fx)
    manifest = _manifest(fx, str(item.id))
    media_context = [
        {"media_id": c["id"], "kind": c["kind"], "duration_s": c["duration_s"]} for c in fx["clips"]
    ]

    def _blocked(*args: Any, **kwargs: Any):  # noqa: ANN202
        raise OfflineViolation("network access is blocked in the q2-long-prompt harness")

    # The real RunContext, with only the two persistence/tracing side channels switched off.
    real_run_context = planner.RunContext

    def run_context(**kwargs: Any):  # noqa: ANN202
        extra = {**(kwargs.pop("extra", None) or {})}
        extra.update(skip_langfuse_trace=True, skip_agent_run_persist=True)
        return real_run_context(extra=extra, **kwargs)

    with contextlib.ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(socket.socket, "connect", _blocked))
        enter(patch.object(socket, "create_connection", _blocked))
        enter(patch.object(settings, "kria_creative_brief_enabled", True))
        enter(patch.object(settings, "kria_creative_brief_user_ids", []))
        enter(patch.object(settings, "kria_brief_binding_enabled", False))
        enter(patch.object(settings, "kria_brief_binding_user_ids", []))
        enter(patch.object(settings, "clip_intents_enabled", False))
        if has_flag:
            enter(patch.object(settings, FLAG, long_request))
        enter(patch.object(planner, "_load_thread_events", AsyncMock(return_value=[])))
        enter(patch.object(planner, "_change_request_turn", AsyncMock(return_value=None)))
        enter(patch.object(planner, "load_latest_brief", AsyncMock(return_value=None)))
        enter(
            patch.object(
                planner,
                "resolve_item_creator_context",
                AsyncMock(return_value=(manifest, media_context)),
            )
        )
        enter(patch.object(planner, "creator_context", lambda *_a: ("creator", "item")))
        enter(patch.object(planner, "_plan_editor_revision", AsyncMock(return_value=None)))
        enter(
            patch.object(
                planner,
                "default_client",
                lambda: _cassette(raw_texts or [fx["main_creator_raw_text"]], prompts),
            )
        )
        enter(patch.object(planner, "RunContext", run_context))
        enter(patch.object(planner, "paid_call_request_id", lambda _thread_id: "q2-offline"))

        async def go():  # noqa: ANN202
            return await planner.plan_live_turn(
                db,
                thread_id=uuid.uuid4(),
                item_id=item.id,
                creator_id=creator_id,
                user_message=fx["user_message"],
            )

        planned = asyncio.run(go())

    coverage = planned.brief_coverage or {}
    updates = _brief_shape(planned.brief_updates)
    applied = apply_updates(None, planned.brief_updates, source_turn_id=None)
    effective_request = render_brief_request(applied, latest_message=fx["user_message"])
    first_cap = re.search(r"at most (\d+) objects", prompts[0]) if prompts else None
    result = {
        "long_request": long_request,
        "model_calls": len(prompts),
        "first_prompt_cap": int(first_cap.group(1)) if first_cap else None,
        "last_prompt_has_exceeds_16": bool(prompts)
        and "brief_updates exceeds 16 entries" in prompts[-1],
        "outcome": {
            "mode": planned.plan.mode,
            "turn_value": planned.plan.turn_value,
            "recovery_reason": coverage.get("reason"),
            "response": planned.plan.response,
            "brief_route": planned.brief_route,
        },
        "brief_update_count": len(updates),
        "brief_updates": updates,
        "effective_request_chars": len(effective_request),
    }
    if raw_texts is not None:
        result["prompts"] = prompts
    return result


def asks_kept(result: dict[str, Any], fx: dict[str, Any]) -> tuple[int, list[str]]:
    """How many authored asks reached the brief in order, and which labels are missing."""
    want = json.loads(fx["main_creator_raw_text"])["brief_updates"]
    got = result["brief_updates"]
    key = ("kind", "scope", "literal", "description")
    got_keys = [tuple(g[k] for k in key) for g in got]
    kept, missing, pos = 0, [], 0
    for label, w in zip(fx["expected_asks"], want, strict=True):
        wanted = (w["kind"], w["scope"], w["literal"], w["description"])
        try:
            pos = got_keys.index(wanted, pos) + 1
            kept += 1
        except ValueError:
            missing.append(label)
    return kept, missing


def _line(label: str, result: dict[str, Any], fx: dict[str, Any]) -> str:
    kept, missing = asks_kept(result, fx)
    out = result["outcome"]
    what = (
        f"recovery ({out['recovery_reason']})"
        if out["recovery_reason"]
        else f"{out['mode']}/{out['turn_value']}"
    )
    text = (
        f"{label}: {result['model_calls']} model call(s), {what}, "
        f"asks kept {kept}/{len(fx['expected_asks'])}, prompt cap {result['first_prompt_cap']}"
    )
    if missing:
        text += f"\n    missing: {', '.join(missing)}"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--write-golden",
        action="store_true",
        help="re-record flag_off_golden.json (flag off)",
    )
    args = parser.parse_args(argv)
    bootstrap()
    fx = load_fixture()
    off = run_turn(long_request=False)
    if args.write_golden:
        GOLDEN.write_text(json.dumps(off, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {GOLDEN}")
        print(_line("flag off", off, fx))
        return 0
    print(f"Q2 long prompt: {len(fx['expected_asks'])} separate asks in one first message")
    print(_line("flag off", off, fx))
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    off_ok = off == golden
    print(f"  flag off equals golden: {'yes' if off_ok else 'NO'}")
    try:
        on = run_turn(long_request=True)
    except FlagNotAvailable:
        print("flag on: NOT AVAILABLE (code predates BRIEF_LONG_REQUEST_ENABLED)")
        on_ok = False
    else:
        print(_line("flag on", on, fx))
        kept, _missing = asks_kept(on, fx)
        on_ok = (
            kept == len(fx["expected_asks"])
            and on["brief_update_count"] == len(fx["expected_asks"])
            and on["model_calls"] == 1
            and on["outcome"]["mode"] == "act"
            and on["outcome"]["recovery_reason"] is None
        )
    print("RESULT:", "PASS" if off_ok and on_ok else "FAIL")
    return 0 if off_ok and on_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
