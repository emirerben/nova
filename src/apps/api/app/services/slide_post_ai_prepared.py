"""Versioned, provider-free admission and execution of immutable AI inputs."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
from functools import lru_cache
from pathlib import Path


class PreparedInput(BaseException):
    # Deliberately bypass legacy exception/fallback/thought-failure handlers.
    def __init__(self, snapshot):
        self.snapshot = snapshot


class PrepareAIExecution:
    def __init__(self, request_id):
        self.request_id = request_id

    async def run(self, snapshot, operation):
        raise PreparedInput(snapshot)


def prepared_context(ctx):
    if ctx is None:
        return {}
    return {
        name: getattr(ctx, name)
        for name in (
            "creator_id",
            "request_id",
            "request_id_authoritative",
            "usage_purpose",
            "test_run_id",
            "release_canary_id",
            "timeout_override_s",
        )
    }


@lru_cache(maxsize=1)
def executor_code_digest():
    """Conservative compatibility pin; changed parser/compiler/schema code rejects pending work.

    Files are immutable within a deployed process. No private state or credentials enter the pin.
    """
    root = Path(__file__).resolve().parents[1]
    paths = set()
    for directory in ("agents", "kria", "schemas"):
        paths.update((root / directory).rglob("*.py"))
    paths.update((root / "services").glob("slide_post_*.py"))
    paths.add(root / "routes" / "_copilot.py")
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def executor_manifest(kind, snapshot):
    from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
    from app.agents.slide_post_composer import SlidePostComposerAgent, SlidePostComposerInput
    from app.config import settings

    agent_type, input_type = (
        (EditCopilotAgent, EditCopilotInput)
        if kind == "chat_edit"
        else (SlidePostComposerAgent, SlidePostComposerInput)
    )
    agent = agent_type(None)
    value = input_type.model_validate(snapshot["agent_input"])
    # Rendered bytes include the actual prompt/skill fragments, not merely their version labels.
    return {
        "executor_version": 1,
        "code_digest": executor_code_digest(),
        "prompt_digest": hashlib.sha256(agent.render_prompt(value).encode()).hexdigest(),
        "spec": json.loads(json.dumps(asdict(agent.spec), default=str)),
        "flags": {
            "custom_effects_enabled": settings.custom_effects_enabled,
            "copilot_direction_follows_request_enabled": (
                settings.copilot_direction_follows_request_enabled
            ),
        },
    }


def envelope(kind, snapshot=None, result=None):
    if result is not None:
        return {"version": 1, "result": result, "code_digest": executor_code_digest()}
    return {"version": 1, "snapshot": snapshot, "manifest": executor_manifest(kind, snapshot)}


def supported(kind, value):
    try:
        if value.get("version") != 1:
            return False
        if "result" in value:
            return value["code_digest"] == executor_code_digest()
        return value["manifest"] == executor_manifest(kind, value["snapshot"])
    except (KeyError, TypeError, ValueError):
        return False


async def execute_prepared(kind, value):
    from app.agents._model_client import default_client
    from app.agents._runtime import RunContext
    from app.schemas.slide_post import SlidePostDraft

    if "result" in value:
        return value["result"]
    snapshot = value["snapshot"]
    ctx = RunContext(**snapshot["context"])
    if kind == "chat_edit":
        from app.agents.edit_copilot import EditCopilotInput
        from app.kria.brief_binding import BriefBinding
        from app.services.slide_post_chat_edit import EditCopilotAgent, finish_prepared_chat

        output = await asyncio.to_thread(
            EditCopilotAgent(default_client()).run,
            EditCopilotInput.model_validate(snapshot["agent_input"]),
            ctx=ctx,
        )
        binding = (
            BriefBinding.model_validate(snapshot["binding"]) if snapshot.get("binding") else None
        )
        return finish_prepared_chat(
            SlidePostDraft.model_validate(snapshot["baseline"]),
            binding,
            snapshot["server_version"],
            output,
        ).model_dump(mode="json")
    from app.agents.slide_post_composer import SlidePostComposerInput
    from app.services.slide_post_compose import SlidePostComposerAgent, finish_prepared_composition

    output = await asyncio.to_thread(
        SlidePostComposerAgent(default_client()).run,
        SlidePostComposerInput.model_validate(snapshot["agent_input"]),
        ctx=ctx,
    )
    result = finish_prepared_composition(
        SlidePostDraft.model_validate(snapshot["fallback"]), output
    )
    return {
        "draft": result.draft.model_dump(mode="json"),
        "base_version": snapshot["previous_version"],
        "fallback_used": result.fallback_used,
        "summary": "Kria could not complete a composition, so this keeps the selected asset order."
        if result.fallback_used
        else "Kria proposed an order, cover, and caption. Review before saving.",
    }
