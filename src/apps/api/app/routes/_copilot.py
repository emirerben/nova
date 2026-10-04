"""Shared edit-copilot turn helper.

The plan-items route mounts v1. A future generative-jobs mirror can reuse this
module after it supplies its own ownership/variant guard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid

import structlog
from fastapi import HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.agents._model_client import default_client
from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    RunContext,
    TerminalError,
)
from app.agents.edit_copilot import (
    CopilotOutcome,
    EditCopilotAgent,
    EditCopilotInput,
    EditCopilotOutput,
)
from app.services.copilot_limits import COPILOT_SNAPSHOT_MAX_BYTES

log = structlog.get_logger()

_MAX_SNAPSHOT_BYTES = COPILOT_SNAPSHOT_MAX_BYTES


class CopilotTurnBody(BaseModel):
    # Minted once per user intent and retained for transport retries. Optional
    # only for split-deploy compatibility with already-open browser bundles.
    client_request_id: str | None = Field(default=None, min_length=1, max_length=128)
    message: str = Field(default="", max_length=2000)
    turns: list[dict] = Field(default_factory=list, max_length=12)
    # KRI-186: the thread's first creator brief (chat-thread callers only).
    original_request: str | None = Field(default=None, max_length=2000)
    snapshot: dict = Field(default_factory=dict)
    # Contract v2 distinguishes a server proposal from a locally staged edit.
    # Default to v1 so an already-open pre-v2 browser remains compatible while
    # frontend and API deploy independently.
    client_contract_version: int = Field(default=1, ge=1, le=2)

    @field_validator("message", mode="before")
    @classmethod
    def _coerce_message(cls, value: object) -> str:
        return str(value or "")


class CopilotTurnResponse(BaseModel):
    # Minted by the owning HTTP route after the final, server-validated
    # operation bundle is known.  The shared helper deliberately leaves this
    # unset because it has no database session or plan-item identity.
    receipt_id: str | None = None
    intent: str
    ops: list[dict] = []
    confidence: float
    reply: str
    suggestions: list[str] = []
    needs_clarification: bool = False
    outcome: CopilotOutcome = "no_effect"
    rejection_reasons: list[dict[str, str]] = []
    clarification_context: dict | None = None
    pending_actions: list[dict] = []
    # KRI-186: parts of the message that did not become an op (see the agent).
    unmet_requests: list[dict[str, str]] = []


# Edit verbs a reply uses to claim the draft changed. Stem-based so past and
# progressive forms (shortened/shortening, trimmed/trimming...) are all caught.
_SUCCESS_WORDS = re.compile(
    r"\b(?:"
    r"done|stored|saved|made|sped|set|cut|split|put|"
    r"(?:delet|eras|clear|chang|updat|appl|stag|edit|trimm?|remov|swapp?|shorten|"
    r"reduc|mov|reorder|replac|add|insert|extend|lengthen|tighten|speed|slow|"
    r"rearrang|adjust|increas|decreas|lower|rais|mut|unmut|merg|flipp?|rotat|"
    r"crop|resiz|scal|shift|align|fix|correct|rewrit|renam|restyl|recolor|"
    r"enabl|disabl|turn|switch|swap|convert|appl)"
    r"(?:ed|ied|ing|ying|ting|ming|ping)"
    r")\b",
    re.IGNORECASE,
)
_NEGATED_SUCCESS = re.compile(
    r"\b(already|unchanged|cannot|can't|couldn't|unable|not|no change|nothing|"
    r"didn't|did not|wasn't|weren't|haven't|hasn't|won't|never)\b",
    re.IGNORECASE,
)


def _claims_success(reply: str) -> bool:
    return bool(_SUCCESS_WORDS.search(reply) and not _NEGATED_SUCCESS.search(reply))


# KRI-297: "use all overlays as full screen" is a display-mode change the editor
# ops cannot express; it needs a fresh edit (the planner re-plans it).
_FULLSCREEN_WORD = re.compile(r"\b(full[\s-]?(screen|frame)|cutaways?)\b", re.IGNORECASE)
_OVERLAY_WORD = re.compile(
    r"\b(overlays?|visuals?|photos?|images?|pictures?|cards?|cutaways?|videos?|clips?)\b",
    re.IGNORECASE,
)
OVERLAY_DISPLAY_LIMIT_REPLY = (
    "Full-screen overlays on an already-rendered iPhone edit need a fresh edit "
    "\u2014 they can't be switched on in place."
)


def is_overlay_display_ask(message: str) -> bool:
    """True when the creator asks to show their overlays/Visuals full screen."""
    text = message or ""
    return bool(_FULLSCREEN_WORD.search(text) and _OVERLAY_WORD.search(text))


def _honest_outcome(
    output: EditCopilotOutput,
    ops: list[dict],
    *,
    supports_proposed: bool = True,
    message: str = "",
) -> tuple[CopilotOutcome, str]:
    """Derive a stable outcome and prevent success prose for empty edits."""
    reasons = output.rejection_reasons
    if ops:
        # This endpoint only proposes operations. The browser reports
        # ``staged`` after its atomic validator/applier succeeds, and Save
        # later links that receipt to the committed revision as ``applied``.
        outcome = "proposed" if supports_proposed else "applied"
    elif any(item.get("reason") == "stale_target" for item in reasons):
        outcome = "stale"
    elif output.intent == "reject" or any(
        item.get("reason") in {"capability_unavailable", "unknown_operation"} for item in reasons
    ):
        outcome = "unsupported"
    elif any(item.get("reason") in {"missing_required", "invalid_value"} for item in reasons):
        outcome = "failed"
    elif output.needs_clarification or output.intent == "clarify":
        outcome = "clarification"
    else:
        outcome = "no_effect"

    reply = output.reply.strip()
    if outcome == "proposed":
        if reply and not _claims_success(reply):
            return outcome, reply
        canned = "I prepared this edit for the editor to validate and stage."
        return outcome, f"{canned} {output.reply_notes}".strip()
    if outcome == "applied":
        # Compatibility response for pre-v2 browser bundles during a split
        # deploy. Those clients own the historical local-apply wording.
        return outcome, reply
    if outcome == "clarification":
        if reply and not _claims_success(reply):
            return outcome, reply
        return outcome, "I need one detail before changing the draft."
    if outcome == "stale":
        return outcome, "That edit is based on an older draft. Refresh the editor and try again."
    detail = next((item.get("detail") for item in reasons if item.get("detail")), None)
    if outcome == "unsupported":
        if detail:
            return outcome, detail
        # With no supported operation, a negation elsewhere in the sentence
        # must not excuse a separate claim that something was changed.
        if reply and not _SUCCESS_WORDS.search(reply):
            return outcome, reply
        if is_overlay_display_ask(message):
            return outcome, OVERLAY_DISPLAY_LIMIT_REPLY
        return outcome, "That kind of edit isn't available for this draft yet."
    if outcome == "failed":
        if output.reply_notes:
            # A specific reason beats the generic line (e.g. which value was not accepted).
            return outcome, f"I couldn't apply that: {output.reply_notes}"
        return outcome, "I couldn't build a valid draft change for that request. Try again."
    if reasons and _SUCCESS_WORDS.search(reply):
        # A structured rejection means nothing changed: never surface prose that
        # claims it did (negation elsewhere in the sentence must not excuse it).
        return outcome, detail or "I couldn't make that change on this edit."
    if reply and not _claims_success(reply):
        return outcome, reply
    # A request that matched nothing must say so, not claim the draft already
    # reflects it: the agent attaches the real reason as an unmet request.
    unmet = next((u.get("reason") for u in output.unmet_requests if u.get("reason")), None)
    if unmet:
        return outcome, unmet
    return outcome, "That change is already reflected in the draft."


def _snapshot_size_bytes(snapshot: dict) -> int:
    try:
        return len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="snapshot must be JSON-serializable",
        ) from exc


def _paid_request_id(body: CopilotTurnBody, *, job_id: uuid.UUID) -> str:
    if body.client_request_id:
        client_digest = hashlib.sha256(body.client_request_id.encode("utf-8")).hexdigest()
        return f"edit-copilot:{job_id}:client:{client_digest}"
    payload = json.dumps(
        {"job_id": str(job_id), "body": body.model_dump(mode="json")},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"edit-copilot:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


async def run_copilot_turn(
    body: CopilotTurnBody,
    *,
    job_id: uuid.UUID,
) -> CopilotTurnResponse:
    """Run one stateless edit-copilot turn.

    Zero writes to variant/job/item rows. The client snapshot is untrusted and is
    never written back to the variant, though it is included in agent_run.input_json
    like every Agent.run input. Returned ops are also untrusted; the editor's
    local applier and the existing Save/editor-commit path enforce again.
    """
    if _snapshot_size_bytes(body.snapshot) > _MAX_SNAPSHOT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The editor context is too large to inspect in one request.",
        )

    agent_input = EditCopilotInput(
        utterance=body.message,
        prior_turns=body.turns[:12],
        variant_snapshot=body.snapshot,
        original_request=body.original_request,
    )

    try:
        output: EditCopilotOutput = await asyncio.to_thread(
            EditCopilotAgent(default_client()).run,
            agent_input,
            ctx=RunContext(
                job_id=str(job_id),
                request_id=_paid_request_id(body, job_id=job_id),
                request_id_authoritative=bool(body.client_request_id),
            ),
        )
    except AiBudgetExceededError:
        raise
    except ProviderOutcomeUnknownError:
        raise
    except TerminalError as exc:
        log.warning("edit_copilot.agent_failed", job_id=str(job_id), error=str(exc)[:300])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="edit_copilot_failed",
        ) from exc

    # Ops ride only genuine edit turns: a disobedient model returning
    # intent="reject"/"describe"/"clarify" WITH ops must not have them applied
    # while the reply text says nothing was done (adversarial review F5).
    ops = [] if (output.needs_clarification or output.intent != "edit") else output.ops
    outcome, reply = _honest_outcome(
        output,
        ops,
        supports_proposed=body.client_contract_version >= 2,
        message=body.message,
    )
    return CopilotTurnResponse(
        intent=output.intent,
        ops=ops,
        confidence=output.confidence,
        reply=reply,
        suggestions=output.suggestions,
        needs_clarification=outcome == "clarification",
        outcome=outcome,
        rejection_reasons=output.rejection_reasons,
        clarification_context=(
            output.clarification_context if outcome == "clarification" else None
        ),
        pending_actions=(output.pending_actions if outcome == "clarification" else []),
        unmet_requests=output.unmet_requests,
    )
