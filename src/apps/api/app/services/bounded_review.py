"""One bounded, gated text review call with shared reservation accounting."""

from __future__ import annotations

import hashlib
import math
import time
from dataclasses import replace

from pydantic import BaseModel, ConfigDict

from app.agents._runtime import (
    ProviderOutcomeUnknownError,
    RunContext,
    TerminalError,
    TransientError,
)
from app.agents._text_providers import TEXT_MODELS
from app.config import settings
from app.services import ai_cost_control as costs

MAX_CONTEXT_BYTES = 48_000
MAX_OUTPUT_TOKENS = 1600


class ReviewProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    prompt_version: str
    context_digest: str
    model: str
    resolved_model: str | None = None
    provider_request_id: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_thoughts: int = 0
    tokens_cached: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0
    finish_status: str | None = None


class BoundedReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    raw_text: str | None = None
    error: str | None = None
    provenance: ReviewProvenance | None = None


def _failed(error, provenance=None):
    return BoundedReviewResponse(error=error, provenance=provenance)


def invoke_bounded_review(
    *,
    snapshot: str,
    job_id: str,
    session_id: str,
    instructions: str,
    response_schema: dict,
    feature: str,
    prompt_version: str,
    run_context: RunContext,
    model: str = "gpt-6.1-sol",
    client=None,
) -> BoundedReviewResponse:
    if not (
        settings.creator_repair_reviewer_enabled
        and settings.credit_quality_review_enabled
        and settings.credit_quality_review_billing_verified
        and settings.ai_cost_control_enabled
    ):
        return _failed("reviewer_disabled")
    if model not in TEXT_MODELS:
        return _failed("unsupported_model")
    if not (
        run_context.reservation_approved is True
        and run_context.test_run_id
        and run_context.request_id
        and run_context.job_id == job_id
        and run_context.creator_agent_session_id == session_id
        and isinstance(run_context.estimated_max_cost_usd, (int, float))
        and math.isfinite(run_context.estimated_max_cost_usd)
        and 0 < run_context.estimated_max_cost_usd <= 2
    ):
        return _failed("unapproved_context")
    if settings.ai_usage_environment == "production":
        if not (
            run_context.test_run_id == settings.credit_quality_review_approved_run_id
            and 0 < run_context.estimated_max_cost_usd <= settings.credit_quality_review_max_run_usd
        ):
            return _failed("unapproved_production_run")
    elif run_context.usage_purpose not in {"live_eval", "provider_smoke"}:
        return _failed("unapproved_usage_purpose")
    if len(snapshot.encode()) > MAX_CONTEXT_BYTES:
        return _failed("context_too_large")
    prompt = instructions + "\nSNAPSHOT_DATA:\n" + snapshot
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    provenance = ReviewProvenance(context_digest=digest, model=model, prompt_version=prompt_version)
    timeout = min(60.0, run_context.timeout_override_s or 60.0)
    if run_context.deadline_monotonic is not None:
        timeout = min(timeout, run_context.deadline_monotonic - time.monotonic())
    if timeout <= 0:
        return _failed("deadline_exhausted", provenance)
    ctx = replace(run_context, extra=dict(run_context.extra))
    estimate = costs.estimate_call_cost_usd(
        model=model, prompt=prompt, max_output_tokens=MAX_OUTPUT_TOKENS, media_mime=None
    )
    if estimate > ctx.estimated_max_cost_usd:
        return _failed("run_budget_exceeded", provenance)
    try:
        receipt = costs.reserve_paid_call(
            costs.PaidCallRequest(
                idempotency_key=costs.logical_call_id(
                    feature=feature, model=model, prompt=prompt, ctx=ctx, attempt=1
                ),
                feature=feature,
                model=model,
                estimated_cost_usd=estimate,
                ctx=ctx,
                provider=TEXT_MODELS[model],
                capability="evaluation",
            )
        )
        if receipt is None:
            return _failed("reservation_unavailable", provenance)
        costs.mark_paid_call_started(receipt)
    except Exception:
        return _failed("budget_denied", provenance)
    started = time.monotonic()
    try:
        if client is None:
            from app.agents._model_client import default_client

            client = default_client()
        invocation = client.invoke(
            model=model,
            prompt=prompt,
            response_json=True,
            response_schema=response_schema,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_level="medium",
            timeout_s=timeout,
        )
    except ProviderOutcomeUnknownError:
        costs.mark_paid_call_unknown(receipt)
        return _failed("provider_outcome_unknown", provenance)
    except (TerminalError, TransientError):
        costs.release_paid_call(receipt)
        return _failed("provider_rejected", provenance)
    except Exception:
        costs.mark_paid_call_unknown(receipt)
        return _failed("provider_outcome_unknown", provenance)
    usage = costs.UsageMeter(
        tokens_in=invocation.tokens_in,
        tokens_out=invocation.tokens_out,
        tokens_thoughts=invocation.tokens_thoughts,
        tokens_cached=invocation.tokens_cached,
        token_details=invocation.token_details,
        provider_request_id=invocation.provider_request_id,
        resolved_model=invocation.model_used,
    )
    cost = costs.settle_paid_call(receipt, usage)
    provenance = provenance.model_copy(
        update=dict(
            resolved_model=invocation.model_used,
            provider_request_id=invocation.provider_request_id,
            tokens_in=usage.tokens_in,
            tokens_out=usage.tokens_out,
            tokens_thoughts=usage.tokens_thoughts,
            tokens_cached=usage.tokens_cached,
            cost_usd=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
            finish_status=invocation.finish_status,
        )
    )
    if invocation.finish_status != "completed":
        return _failed("provider_incomplete", provenance)
    return BoundedReviewResponse(raw_text=invocation.raw_text, provenance=provenance)
