"""Best-effort Jev shadow verification for committed Kria drafts (KRI-228).

The task is deliberately outside the creator's request path. Its result is
calibration data only: deterministic Creative Brief receipts remain the sole
authority for replies, drafts, approvals, and renders.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import structlog

from app.agents._persistence import persist_agent_run
from app.config import settings
from app.services.jev_brief_shadow import (
    JevPayload,
    build_jev_questions,
    map_jev_evaluation,
)
from app.services.jev_client import JEV_PRICE_VERSION, JevClient, JevError
from app.worker import celery_app

log = structlog.get_logger()

JEV_SHADOW_AGENT_NAME = "nova.brief.jev_shadow"
JEV_SHADOW_PROMPT_VERSION = "2026-09-30.1"


def _input_metadata(payload: JevPayload) -> dict[str, Any]:
    """Return content-free metadata suitable for AgentRun persistence."""
    encoded = payload.model_dump_json(exclude_none=True)
    return {
        "payload_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "requirement_count": len(payload.brief.requirements),
        "claim_count": len(build_jev_questions(payload)) - len(payload.brief.requirements),
        "media_description_count": len(payload.media),
        "shadow_only": True,
    }


def _baseline_projection(receipts: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Keep only stable IDs and statuses; never carry receipt reasons or creator text."""
    projected: list[dict[str, str]] = []
    for raw in receipts or []:
        if not isinstance(raw, dict):
            continue
        requirement_id = str(raw.get("requirement_id") or "")[:24]
        status = str(raw.get("status") or "")
        if requirement_id and status in {"met", "partial", "not_possible"}:
            projected.append({"requirement_id": requirement_id, "status": status})
    return projected


def run_jev_brief_shadow(
    *,
    plan_item_id: str,
    creator_agent_session_id: str,
    payload: dict[str, Any],
    baseline_receipts: list[dict[str, Any]] | None = None,
    client_factory: Callable[..., JevClient] = JevClient,
    persist_run: Callable[..., None] = persist_agent_run,
) -> bool:
    """Run and record one shadow decision, swallowing every provider failure.

    Returns ``True`` when the feature was enabled and a run was attempted.
    ``False`` means the flag/key/payload guard skipped the provider entirely.
    """
    if not settings.jev_brief_shadow_enabled or not settings.typesafe_api_key:
        return False

    try:
        parsed = JevPayload.model_validate(payload)
        questions = build_jev_questions(parsed)
    except Exception as exc:  # noqa: BLE001 - internal shadow data must fail open
        log.warning("jev_brief_shadow_invalid_payload", error_type=type(exc).__name__)
        return False
    if not questions:
        return False

    metadata = _input_metadata(parsed)
    metadata["baseline_receipts"] = _baseline_projection(baseline_receipts)
    try:
        result = client_factory(
            settings.typesafe_api_key,
            endpoint=settings.jev_api_url,
            model=settings.jev_model,
            timeout_s=settings.jev_timeout_seconds,
            max_attempts=settings.jev_max_attempts,
        ).evaluate(parsed.model_dump(mode="json", exclude_none=True), questions)
        judgment = map_jev_evaluation(parsed, result)
    except JevError as exc:
        persist_run(
            job_id=None,
            plan_item_id=plan_item_id,
            creator_agent_session_id=creator_agent_session_id,
            segment_idx=None,
            agent_name=JEV_SHADOW_AGENT_NAME,
            prompt_version=JEV_SHADOW_PROMPT_VERSION,
            model=settings.jev_model,
            outcome="failed",
            attempts=exc.attempts,
            tokens_in=0,
            tokens_out=0,
            token_details={"provider": "typesafe", "shadow_only": True},
            environment=settings.ai_usage_environment,
            usage_purpose="optional_background",
            price_version=JEV_PRICE_VERSION,
            cost_usd=0.0,
            latency_ms=0,
            input_dict=metadata,
            output_dict=None,
            raw_text=None,
            error=exc.code,
        )
        log.warning(
            "jev_brief_shadow_failed",
            code=exc.code,
            status_code=exc.status_code,
            attempts=exc.attempts,
        )
        return True
    except Exception as exc:  # noqa: BLE001 - shadow failures never affect Kria
        persist_run(
            job_id=None,
            plan_item_id=plan_item_id,
            creator_agent_session_id=creator_agent_session_id,
            segment_idx=None,
            agent_name=JEV_SHADOW_AGENT_NAME,
            prompt_version=JEV_SHADOW_PROMPT_VERSION,
            model=settings.jev_model,
            outcome="failed",
            attempts=1,
            tokens_in=0,
            tokens_out=0,
            token_details={"provider": "typesafe", "shadow_only": True},
            environment=settings.ai_usage_environment,
            usage_purpose="optional_background",
            price_version=JEV_PRICE_VERSION,
            cost_usd=0.0,
            latency_ms=0,
            input_dict=metadata,
            output_dict=None,
            raw_text=None,
            error=type(exc).__name__,
        )
        log.warning("jev_brief_shadow_failed", code="internal_error")
        return True

    output = judgment.model_dump(mode="json", exclude_none=True)
    persist_run(
        job_id=None,
        plan_item_id=plan_item_id,
        creator_agent_session_id=creator_agent_session_id,
        segment_idx=None,
        agent_name=JEV_SHADOW_AGENT_NAME,
        prompt_version=JEV_SHADOW_PROMPT_VERSION,
        model=result.model,
        outcome="ok",
        attempts=result.attempts,
        tokens_in=result.input_tokens,
        tokens_out=result.output_tokens,
        token_details={"provider": "typesafe", "shadow_only": True},
        environment=settings.ai_usage_environment,
        usage_purpose="optional_background",
        provider_request_id=result.provider_request_id,
        price_version=JEV_PRICE_VERSION,
        settled_cost_usd=result.cost_usd,
        cost_usd=result.cost_usd,
        latency_ms=round(result.latency_ms),
        input_dict=metadata,
        output_dict=output,
        raw_text=None,
        error=None,
    )
    log.info(
        "jev_brief_shadow_done",
        attempts=result.attempts,
        input_tokens=result.input_tokens,
        latency_ms=round(result.latency_ms),
        requirement_count=metadata["requirement_count"],
        claim_count=metadata["claim_count"],
    )
    return True


@celery_app.task(
    name="tasks.jev_brief_shadow",
    max_retries=0,
    soft_time_limit=15,
    time_limit=20,
    acks_late=True,
)
def evaluate_jev_brief_shadow(
    *,
    plan_item_id: str,
    creator_agent_session_id: str,
    payload: dict[str, Any],
    baseline_receipts: list[dict[str, Any]] | None = None,
) -> None:
    run_jev_brief_shadow(
        plan_item_id=plan_item_id,
        creator_agent_session_id=creator_agent_session_id,
        payload=payload,
        baseline_receipts=baseline_receipts,
    )
