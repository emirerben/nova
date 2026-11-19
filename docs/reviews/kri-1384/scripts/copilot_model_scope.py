"""What every caller of the copilot's model sends to its provider, written as JSON to argv[1].

Drives the real chat turn (`run_copilot_turn`), the slide-post chat edit, the prepared
slide-post executor and the proposal receipt, and dumps the specs of the JSON-mode agents that
share the copilot's model. A recording client answers each provider call by raising, so no
request leaves the machine. Run it from an API tree; `compare_copilot_model_scope.sh` runs it
in this tree and in origin/main, with and without the Sonnet tools flip, and diffs the output.

  cd src/apps/api
  .venv/bin/python ../../../docs/reviews/kri-1384/scripts/copilot_model_scope.py out.json
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from dataclasses import asdict
from types import SimpleNamespace

for key, value in {
    "STORAGE_BUCKET": "nova-test",
    "STORAGE_PROVIDER": "gcs",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_test",
    "REDIS_URL": "redis://localhost:6379",
    "GEMINI_API_KEY": "test",
    "ANTHROPIC_API_KEY": "test",
}.items():
    os.environ.setdefault(key, value)
sys.path.insert(0, os.getcwd())

from app.agents import _persistence  # noqa: E402
from app.agents._runtime import TerminalError  # noqa: E402
from app.services import ai_cost_control  # noqa: E402

# A paid non-Google call reserves budget in Postgres first; with no reservation the runtime
# goes straight to the client, which is the recorder. Agent runs are not written either.
ai_cost_control.reserve_paid_call = lambda _request: None
_persistence.persist_agent_run = lambda **_kw: None


class Recorder:
    """A model client that records each request and fails it, so nothing is sent."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke(self, **kw):
        request = kw.get("tool_request")
        self.calls.append(
            {
                "model": kw["model"],
                "thinking_level": kw.get("thinking_level"),
                "response_json": kw.get("response_json"),
                "max_output_tokens": kw.get("max_output_tokens"),
                "tools": sorted(tool.name for tool in request.tools) if request else None,
                "prompt_sha256": hashlib.sha256(kw["prompt"].encode()).hexdigest()[:16],
            }
        )
        raise TerminalError("recorded, not sent")


def spec(agent_or_class) -> dict:
    return json.loads(json.dumps(asdict(agent_or_class.spec), default=str))


def chat_turn() -> dict:
    from app.routes import _copilot

    recorder = Recorder()
    built: list = []
    configure = _copilot.configure_creator_agent

    def capture(agent, name):
        built.append(configure(agent, name))
        return built[-1]

    _copilot.default_client = lambda: recorder
    _copilot.configure_creator_agent = capture
    body = _copilot.CopilotTurnBody(
        message="make the title bigger",
        snapshot={"editor_ops_version": 2, "allowed_op_families": ["all", "text"]},
        client_request_id="kri1384-scope",
    )
    try:
        asyncio.run(_copilot.run_copilot_turn(body, job_id=uuid.UUID(int=1384)))
    except Exception as exc:  # noqa: BLE001
        outcome = type(exc).__name__
    else:
        outcome = "returned"
    agent = built[0]
    return {
        "spec": spec(agent),
        "first_call": recorder.calls[0],
        "calls": [
            {"model": call["model"], "with_tools": call["tools"] is not None}
            for call in recorder.calls
        ],
        "outcome": outcome,
    }


def _slide_draft():
    from app.schemas.slide_post import SlidePostDraft, SlideRef

    asset = SimpleNamespace(id=uuid.UUID(int=7), kind="image", analysis=None, capture=None)
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="s0", asset_id=asset.id, kind="image")],
        cover_index=0,
        version=3,
        caption="",
    )
    return draft, {asset.id: asset}


def slide_post_chat_edit() -> dict:
    from app.agents import _model_client
    from app.services.slide_post_chat_edit import run_slide_post_chat_edit

    recorder = Recorder()
    _model_client.default_client = lambda: recorder
    draft, assets = _slide_draft()
    result = asyncio.run(
        run_slide_post_chat_edit(
            draft=draft,
            assets_by_id=assets,
            message="make the caption shorter",
            turns=[],
            user_id=uuid.UUID(int=9),
            server_version=3,
        )
    )
    return {"first_call": recorder.calls[0], "outcome": result.outcome}


def slide_post_prepared() -> dict:
    from app.agents import _model_client
    from app.services.slide_post_ai_prepared import execute_prepared, executor_manifest

    recorder = Recorder()
    _model_client.default_client = lambda: recorder
    snapshot = {
        "context": {},
        "agent_input": {"utterance": "make the caption shorter", "variant_snapshot": {}},
    }
    try:
        asyncio.run(execute_prepared("chat_edit", {"snapshot": snapshot}))
    except Exception as exc:  # noqa: BLE001
        outcome = type(exc).__name__
    return {
        "first_call": recorder.calls[0],
        "manifest_spec": executor_manifest("chat_edit", snapshot)["spec"],
        "outcome": outcome,
    }


def proposal_receipt() -> dict:
    from app.routes._copilot import CopilotTurnBody, CopilotTurnResponse
    from app.services import edit_interaction_receipts as receipts

    rows: list = []

    async def add(_db, row):
        rows.append(row)

    async def eligible(_db, _creator_id):
        return SimpleNamespace(
            eligible=True, basis="internal", consent_event_id=None, internal_grant_id=None
        )

    class Db:
        async def commit(self):
            return None

    receipts._add = add
    receipts.evaluate_training_eligibility_async = eligible
    response = CopilotTurnResponse(
        intent="describe", reply="ok", ops=[], confidence=0.9, outcome="no_effect"
    )
    asyncio.run(
        receipts.persist_copilot_proposal(
            Db(),
            creator_id=uuid.UUID(int=1),
            plan_item_id=uuid.UUID(int=2),
            job_id=uuid.UUID(int=3),
            variant_id="v1",
            body=CopilotTurnBody(message="hi", snapshot={}),
            response=response,
        )
    )
    return {"model": rows[0].model, "prompt_version": rows[0].prompt_version}


def json_mode_specs() -> dict:
    from app.agents.edit_copilot import EditCopilotAgent
    from app.services.creation_text_composition import (
        CreationTextComposer,
        CreationTextComposerFast,
    )
    from app.services.researched_text import (
        ResearchCopyAgent,
        ResearchLayersAgent,
        ResearchReviewAgent,
    )

    return {
        cls.__name__: spec(cls)
        for cls in (
            EditCopilotAgent,
            CreationTextComposer,
            CreationTextComposerFast,
            ResearchCopyAgent,
            ResearchLayersAgent,
            ResearchReviewAgent,
        )
    }


if __name__ == "__main__":
    with open(sys.argv[1], "w") as out:
        out.write(
            json.dumps(
                {
                    "chat_turn": chat_turn(),
                    "slide_post_chat_edit": slide_post_chat_edit(),
                    "slide_post_prepared": slide_post_prepared(),
                    "proposal_receipt": proposal_receipt(),
                    "json_mode_specs": json_mode_specs(),
                },
                indent=2,
                sort_keys=True,
            )
        )
