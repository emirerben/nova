"""One bounded, separately priced image-input review call with shared reservation accounting.

The text adapter keeps rejecting media; this is the only path that sends frames to OpenAI.
Cost is reserved before any frame leaves the process, an unknown outcome keeps its liability
and is never retried, and a definite provider rejection is the only thing released after the
start fence. The response is handed to `persist_response` before the ledger is settled so a
crash between the two is recoverable from durable state.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from app.agents._openai_vision import (
    DEFAULT_TIMEOUT_S,
    MAX_IMAGES,
    MAX_OUTPUT_TOKENS,
    VISION_MODEL,
    OpenAIVisionClient,
    VisionResult,
)
from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    CostControlUnavailableError,
    ProviderOutcomeUnknownError,
    RunContext,
    TerminalError,
    TransientError,
)
from app.services import ai_cost_control as costs

INTENT_REVIEW_FEATURE = "intent_review"
MAX_PROMPT_CHARS = 24_000

MediaReviewStatus = Literal[
    "ok",
    "context_too_large",
    "budget_review",
    "budget_monthly",
    "paid_gate_closed",
    "ledger_unavailable",
    "stale",
    "provider_unknown_outcome",
    "provider_rejected",
]


@dataclass(frozen=True)
class MediaReviewRequest:
    prompt: str
    frames: tuple[bytes, ...]
    frame_dims: tuple[tuple[int, int], ...]
    response_schema: dict[str, Any]
    idempotency_key: str
    run_context: RunContext
    max_usd: float
    prompt_version: str
    model: str = VISION_MODEL
    timeout_s: float = DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class MediaReviewProvenance:
    model: str
    prompt_version: str
    frames_sent: int
    frame_hashes: tuple[str, ...]
    frame_set_digest: str
    provider_outcome: Literal["ok", "rejected", "unknown", "not_sent"]
    reserved_usd: float
    price_version: str
    response_id: str | None = None
    response_status: str | None = None
    resolved_model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    settled_usd: float | None = None
    latency_ms: int | None = None
    reservation: dict[str, Any] | None = None

    @property
    def visual_input_received(self) -> bool:
        return (
            self.frames_sent >= 1
            and self.response_status == "completed"
            and self.provider_outcome == "ok"
        )

    def as_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["frame_hashes"] = list(self.frame_hashes)
        payload["visual_input_received"] = self.visual_input_received
        return payload


@dataclass(frozen=True)
class MediaReviewResult:
    status: MediaReviewStatus
    provenance: MediaReviewProvenance
    raw_text: str | None = None
    detail: str | None = None


def reservation_json(receipt: costs.ReservationReceipt) -> dict[str, Any]:
    return {**asdict(receipt), "id": str(receipt.id)}


def frame_set_digest(frame_hashes: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(frame_hashes).encode()).hexdigest()


def estimate_media_review_cost(request: MediaReviewRequest) -> float:
    return costs.estimate_call_cost_usd(
        model=request.model,
        prompt=request.prompt + json.dumps(request.response_schema, sort_keys=True),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        media_mime="image/jpeg",
        media_count=len(request.frames),
        media_dims=request.frame_dims,
    )


def _provenance(request, estimate, outcome, **changes) -> MediaReviewProvenance:
    hashes = tuple(hashlib.sha256(frame).hexdigest() for frame in request.frames)
    return MediaReviewProvenance(
        model=request.model,
        prompt_version=request.prompt_version,
        frames_sent=len(request.frames) if outcome != "not_sent" else 0,
        frame_hashes=hashes,
        frame_set_digest=frame_set_digest(hashes),
        provider_outcome=outcome,
        reserved_usd=estimate,
        price_version=costs.PRICE_VERSION,
        **changes,
    )


def invoke_bounded_media_review(
    request: MediaReviewRequest,
    *,
    client: OpenAIVisionClient | None = None,
    before_transfer: Callable[[costs.ReservationReceipt], bool] | None = None,
    persist_response: Callable[[VisionResult, MediaReviewProvenance], None] | None = None,
) -> MediaReviewResult:
    """Reserve, send once, persist, settle. Never retries and never returns a partial success."""
    estimate = estimate_media_review_cost(request)

    def fail(status: MediaReviewStatus, detail: str | None = None, **changes) -> MediaReviewResult:
        outcome = changes.pop("outcome", "not_sent")
        return MediaReviewResult(
            status=status,
            provenance=_provenance(request, estimate, outcome, **changes),
            detail=detail,
        )

    if len(request.prompt) > MAX_PROMPT_CHARS:
        return fail("context_too_large")
    if not 1 <= len(request.frames) <= MAX_IMAGES or len(request.frame_dims) != len(request.frames):
        return fail("context_too_large", "frame_count")
    if estimate > request.max_usd:
        return fail("budget_review")
    try:
        receipt = costs.reserve_paid_call(
            costs.PaidCallRequest(
                idempotency_key=request.idempotency_key,
                feature=INTENT_REVIEW_FEATURE,
                model=request.model,
                estimated_cost_usd=estimate,
                ctx=request.run_context,
                provider="openai",
                capability="intent_review",
            )
        )
    except AiBudgetExceededError as exc:
        return fail("budget_monthly", exc.scope)
    except AiCostControlPolicyError as exc:
        return fail("paid_gate_closed", exc.reason)
    except ProviderOutcomeUnknownError:
        return fail("provider_unknown_outcome", "key_already_reserved", outcome="unknown")
    except CostControlUnavailableError:
        return fail("ledger_unavailable")
    if receipt is None:
        return fail("ledger_unavailable", "unmetered")
    reservation = reservation_json(receipt)
    if before_transfer is not None:
        try:
            proceed = before_transfer(receipt)
        except BaseException:
            costs.release_paid_call(receipt)
            raise
        if not proceed:
            costs.release_paid_call(receipt)
            return fail("stale", reservation=reservation)
    try:
        costs.mark_paid_call_started(receipt)
    except (ProviderOutcomeUnknownError, CostControlUnavailableError):
        costs.release_paid_call(receipt)
        return fail("ledger_unavailable", "start_fence", reservation=reservation)
    began = time.monotonic()
    try:
        vision = (client or OpenAIVisionClient()).invoke_images(
            request.prompt,
            request.frames,
            request.response_schema,
            timeout_s=request.timeout_s,
        )
    except ProviderOutcomeUnknownError:
        costs.mark_paid_call_unknown(receipt)
        return fail("provider_unknown_outcome", outcome="unknown", reservation=reservation)
    except (TerminalError, TransientError):
        costs.release_paid_call(receipt)
        return fail("provider_rejected", outcome="rejected", reservation=reservation)
    except Exception:
        costs.mark_paid_call_unknown(receipt)
        return fail("provider_unknown_outcome", outcome="unknown", reservation=reservation)
    latency_ms = int((time.monotonic() - began) * 1000)
    provenance = _provenance(
        request,
        estimate,
        "ok",
        response_id=vision.response_id,
        response_status=vision.status,
        resolved_model=vision.model_used,
        usage={
            "input_tokens": vision.usage.input_tokens,
            "output_tokens": vision.usage.output_tokens,
            "reasoning_tokens": vision.usage.reasoning_tokens,
            "cached_tokens": vision.usage.cached_tokens,
        },
        latency_ms=latency_ms,
        reservation=reservation,
    )
    if persist_response is not None:
        persist_response(vision, provenance)
    settled = costs.settle_paid_call(receipt, usage_meter(provenance.usage, vision.response_id))
    return MediaReviewResult(
        status="ok",
        provenance=replace(provenance, settled_usd=settled),
        raw_text=vision.raw_text,
    )


def usage_meter(usage: dict[str, int], response_id: str | None) -> costs.UsageMeter:
    return costs.UsageMeter(
        tokens_in=int(usage.get("input_tokens", 0)),
        tokens_out=int(usage.get("output_tokens", 0)),
        tokens_thoughts=0,
        tokens_cached=int(usage.get("cached_tokens", 0)),
        token_details={
            "reasoning_tokens": int(usage.get("reasoning_tokens", 0)),
            "output_tokens_inclusive": int(usage.get("output_tokens", 0)),
        },
        provider_request_id=response_id,
        resolved_model=VISION_MODEL,
    )
