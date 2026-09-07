"""Credential-free whole-turn replay for the runtime-v2 golden slice."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.kria.contracts import (
    KriaObservedTurnResponse,
    KriaProblem,
    KriaToolReceipt,
    KriaTurnPlan,
)
from app.kria.language import is_paraphrase_only
from app.kria.registry import KRIA_TOOLS, KriaToolRegistry


class KriaReplayFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    scenario: Literal["execute", "stale", "approval", "ambiguous_dispatch"] = "execute"
    user_message: str = Field(min_length=1)
    snapshot: dict[str, Any]
    plan: KriaTurnPlan
    expected_thread_revision: int = Field(default=0, ge=0)
    current_thread_revision: int = Field(default=0, ge=0)
    expected_message: str | None = None


class KriaReplayTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: str
    trace_id: str
    events: list[dict[str, Any]]
    receipts: list[KriaToolReceipt]
    response: KriaObservedTurnResponse


def load_fixture(path: Path) -> KriaReplayFixture:
    return KriaReplayFixture.model_validate_json(path.read_text(encoding="utf-8"))


def replay_fixture(
    fixture: KriaReplayFixture,
    *,
    registry: KriaToolRegistry = KRIA_TOOLS,
) -> KriaReplayTrace:
    """Run a deterministic turn without DB, model, broker, storage, or renderer."""

    trace_id = hashlib.sha256(fixture.fixture_id.encode()).hexdigest()[:16]
    events: list[dict[str, Any]] = [
        {"phase": "accept", "type": "user_message", "value": fixture.user_message},
        {"phase": "plan", "type": "turn_plan", "value": fixture.plan.model_dump(mode="json")},
    ]
    receipts: list[KriaToolReceipt] = []
    results: list[dict[str, Any]] = []

    if fixture.scenario == "stale":
        if fixture.expected_thread_revision == fixture.current_thread_revision:
            raise ValueError("stale replay requires different expected and current revisions")
        problem = KriaProblem(
            code="thread_revision_stale",
            phase="policy",
            message="The project changed before this turn could act.",
            recovery="refresh_replan",
            trace_id=trace_id,
            current_revision=fixture.current_thread_revision,
        )
        events.append(
            {"phase": "policy", "type": "problem", "value": problem.model_dump(mode="json")}
        )
        response = KriaObservedTurnResponse(
            turn_value="recovery",
            message="The project changed, so I left it untouched. I’ll replan from the latest cut.",
            next_actions=["refresh_replan"],
        )
        return _finish_trace(fixture, trace_id, events, receipts, response)

    for intent in fixture.plan.intents:
        try:
            registered = registry.get(intent.tool_name, intent.tool_version)
            if registered.definition.risk == "approval_required":
                if fixture.scenario not in {"approval", "ambiguous_dispatch"}:
                    raise ValueError("approval-required tool needs an approval replay scenario")
                registered.arguments_model.model_validate(intent.arguments)
                draft_id = str(fixture.snapshot.get("draft_id") or "current-draft")
                draft_revision = int(fixture.snapshot.get("draft_revision") or 0)
                if fixture.scenario == "approval":
                    receipt = KriaToolReceipt(
                        intent_id=intent.intent_id,
                        tool_name=intent.tool_name,
                        tool_version=intent.tool_version,
                        status="awaiting_approval",
                        result={
                            "approval_id": f"approval-{trace_id}",
                            "consequence": (
                                f"Render draft {draft_id} revision {draft_revision} once."
                            ),
                        },
                    )
                else:
                    receipt = KriaToolReceipt(
                        intent_id=intent.intent_id,
                        tool_name=intent.tool_name,
                        tool_version=intent.tool_version,
                        status="outcome_unknown",
                        error=KriaProblem(
                            code="dispatch_outcome_unknown",
                            phase="dispatch",
                            message="Broker acknowledgement was not conclusive.",
                            retryable=False,
                            recovery="manual",
                            trace_id=trace_id,
                            target={"draft_id": draft_id},
                        ),
                    )
                receipts.append(receipt)
                events.append(
                    {
                        "phase": "approval" if fixture.scenario == "approval" else "dispatch",
                        "type": "receipt",
                        "value": receipt.model_dump(mode="json"),
                    }
                )
                continue
            if registered.definition.risk != "read":
                raise ValueError("golden replay permits read or approval-gated tools only")
            args = registered.arguments_model.model_validate(intent.arguments)
            result_model = registered.result_model.model_validate(
                registered.handler(args, fixture.snapshot)
            )
            result = result_model.model_dump(mode="json")
            receipt = KriaToolReceipt(
                intent_id=intent.intent_id,
                tool_name=intent.tool_name,
                tool_version=intent.tool_version,
                status="completed",
                result=result,
            )
            results.append(result)
        except (KeyError, ValueError) as exc:
            problem = KriaProblem(
                code="tool_contract_rejected",
                phase="tool",
                message=str(exc),
                retryable=False,
                recovery="manual",
                trace_id=trace_id,
            )
            receipt = KriaToolReceipt(
                intent_id=intent.intent_id,
                tool_name=intent.tool_name,
                tool_version=intent.tool_version,
                status="failed",
                error=problem,
            )
        receipts.append(receipt)
        events.append(
            {"phase": "tool", "type": "receipt", "value": receipt.model_dump(mode="json")}
        )

    completed = [receipt for receipt in receipts if receipt.status == "completed"]
    if any(receipt.status == "outcome_unknown" for receipt in receipts):
        response = KriaObservedTurnResponse(
            turn_value="progress",
            message=(
                "I’m checking whether the render started. I won’t start another one until that "
                "is confirmed."
            ),
            receipt_ids=[receipt.intent_id for receipt in receipts],
            next_actions=["reconcile_dispatch"],
        )
    elif any(receipt.status == "awaiting_approval" for receipt in receipts):
        response = KriaObservedTurnResponse(
            turn_value="action",
            message="The tighter cut is ready to render. Approve it to start one render.",
            receipt_ids=[receipt.intent_id for receipt in receipts],
            next_actions=["approve_render"],
        )
    elif completed and results:
        message = str(results[-1]["editorial_decision"])
        next_action = str(results[-1]["next_action"])
        response = KriaObservedTurnResponse(
            turn_value="question" if next_action == "attach_media" else "decision",
            message=message,
            receipt_ids=[receipt.intent_id for receipt in completed],
            next_actions=[next_action],
        )
    else:
        response = KriaObservedTurnResponse(
            turn_value="recovery",
            message="I couldn't inspect this project safely. The project is unchanged.",
            receipt_ids=[receipt.intent_id for receipt in receipts],
            next_actions=["inspect_trace"],
        )

    return _finish_trace(fixture, trace_id, events, receipts, response)


def _finish_trace(
    fixture: KriaReplayFixture,
    trace_id: str,
    events: list[dict[str, Any]],
    receipts: list[KriaToolReceipt],
    response: KriaObservedTurnResponse,
) -> KriaReplayTrace:
    if fixture.expected_message is not None and response.message != fixture.expected_message:
        raise ValueError("observed response did not match the frozen fixture")
    if is_paraphrase_only(user_message=fixture.user_message, assistant_message=response.message):
        raise ValueError("observed response only acknowledges or repeats the user message")
    events.append(
        {"phase": "observe", "type": "assistant_response", "value": response.model_dump()}
    )
    return KriaReplayTrace(
        fixture_id=fixture.fixture_id,
        trace_id=trace_id,
        events=events,
        receipts=receipts,
        response=response,
    )


def render_readable(trace: KriaReplayTrace) -> str:
    lines = [f"Kria replay: {trace.fixture_id}", f"trace: {trace.trace_id}"]
    for event in trace.events:
        lines.append(f"{event['phase']:>7}  {event['type']}")
    lines.extend(("", f"Kria: {trace.response.message}"))
    return "\n".join(lines)


def trace_as_json(trace: KriaReplayTrace) -> str:
    return json.dumps(trace.model_dump(mode="json"), indent=2, sort_keys=True)
