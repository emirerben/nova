"""Ledger metering for the raw OpenAI calls made outside the agent runtime (KRI-1395).

Caption correction, caption translation and lyric Whisper called OpenAI directly, so none of
their spend reached ``ai_cost_reservations``. They go through ``run_metered`` now.

Failure modes, and what each one does:

- Reservation refused (budget, policy, attribution, ledger unavailable): the provider is never
  contacted and the error propagates. Every caller is best-effort and already degrades on any
  exception (no correction, spoken-language captions, ``TerminalError`` for lyrics), so a render
  never fails on a refusal.
- The ledger is off (``AI_COST_CONTROL_ENABLED=false``): ``reserve_paid_call`` returns None and
  the call runs unmetered, as every non-generated-media capability does. Local dev is this case.
- An unpriced model (a configured name the registry does not know) while the ledger is on:
  the estimate cannot be made, so the call is refused like any other refusal. With the ledger
  off it runs.
- Provider 4xx (including 429): the provider declared the call failed, so the reservation is
  released and costs nothing.
- Provider 5xx, timeout, dropped connection or any other error after the call started: the
  outcome is unknown, so the reservation is marked ``unknown`` and keeps its estimate.
- The provider answered but settlement cannot price it (an unregistered resolved model, a
  missing usage block): the response is returned and the reservation settles at its estimate,
  never at zero.
- The same text corrected twice in one task (three variants share cues): each call reserves
  under its own key. A key derived only from the task and prompt would refuse the second call
  as "already settled" and silently drop its correction. A redelivered task can therefore bill
  one more call, bounded by the per-call estimate and the monthly budget.
- SDK retries: a retry after an unbilled failure costs nothing, so the reservation covers one
  call per allowed attempt and settlement books the final response's usage. A timeout that was
  billed and then retried can bill twice without the ledger seeing the first charge, which is
  why the ledger total still needs comparing with the provider dashboard.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

import structlog

from app.agents._runtime import RunContext
from app.config import settings
from app.services import ai_cost_control as ledger

log = structlog.get_logger()

T = TypeVar("T")

_MIN_OUTPUT_TOKENS = 1_024
# Output of a line-for-line rewrite or translation is about the size of its input. The provider
# request keeps no output cap (its body is pinned byte-for-byte by the caption tests), so this
# prices the reservation and any output beyond it is booked at its real usage on settlement.
_OUTPUT_PER_INPUT_TOKEN = 4

# Stamped on every reservation made here: gpt-4o and Whisper rates are newer than
# ``ai_cost_control.PRICE_VERSION``, which the intent-review preregistration pins.
PRICE_VERSION = "openai-captions-whisper-2026-11-13.v1"

CAPTION_CORRECTION_FEATURE = "caption_correction"
CAPTION_TRANSLATION_FEATURE = "caption_translation"
LYRICS_WHISPER_FEATURE = "lyrics_whisper"


def output_bound_tokens(prompt: str) -> int:
    """The output size a reservation is priced for."""

    input_tokens = max(1, (len(prompt.encode("utf-8")) + 2) // 3)
    return min(
        ledger.DEFAULT_MAX_OUTPUT_TOKENS,
        max(_MIN_OUTPUT_TOKENS, input_tokens * _OUTPUT_PER_INPUT_TOKEN),
    )


def _fresh_key(feature: str, model: str, identity: str, ctx: RunContext) -> str:
    base = ledger.logical_call_id(feature=feature, model=model, prompt=identity, ctx=ctx, attempt=0)
    return hashlib.sha256(f"{base}:{uuid.uuid4().hex}".encode()).hexdigest()


def _declared_failure(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500


def _reserve(
    feature: str,
    model: str,
    estimate_usd: Callable[[], float],
    attempts: int,
    ctx: RunContext,
    identity: str,
) -> ledger.ReservationReceipt | None:
    """The reservation for one call, or None when the ledger is off and the call runs unmetered."""

    try:
        try:
            estimate = estimate_usd()
        except ledger.AiCostControlPolicyError:
            if not settings.ai_cost_control_enabled:
                return None
            raise
        return ledger.reserve_paid_call(
            ledger.PaidCallRequest(
                idempotency_key=_fresh_key(feature, model, identity, ctx),
                feature=feature,
                model=model,
                estimated_cost_usd=estimate * max(1, attempts),
                ctx=ctx,
                provider="openai",
                price_version=PRICE_VERSION,
            )
        )
    except Exception as exc:
        log.warning(
            "metered_openai_refused",
            feature=feature,
            model=model,
            error_type=type(exc).__name__,
            reason=getattr(exc, "reason", None) or str(exc)[:120],
        )
        raise


def run_metered(
    *,
    feature: str,
    model: str,
    estimate_usd: Callable[[], float],
    attempts: int,
    ctx: RunContext,
    identity: str,
    operation: Callable[[], T],
    settle: Callable[[ledger.ReservationReceipt, T], None],
) -> T:
    """Reserve, run ``operation``, then settle (or release / mark unknown) its reservation."""

    receipt = _reserve(feature, model, estimate_usd, attempts, ctx, identity)
    if receipt is None:
        return operation()
    try:
        ledger.mark_paid_call_started(receipt)
    except BaseException:
        ledger.release_paid_call(receipt)
        raise
    try:
        response = operation()
    except BaseException as exc:
        if _declared_failure(exc):
            ledger.release_paid_call(receipt)
        else:
            ledger.mark_paid_call_unknown(receipt)
        raise
    try:
        settle(receipt, response)
    except Exception as exc:  # noqa: BLE001 - the paid answer is good; keep the estimate booked
        log.error(
            "metered_openai_settle_failed",
            feature=feature,
            model=model,
            error_type=type(exc).__name__,
        )
        ledger.settle_paid_call_cost(
            receipt,
            cost_usd=receipt.estimated_cost_usd,
            resolved_model=model,
            usage_json={"settled_at_estimate": True, "error_type": type(exc).__name__},
        )
    return response


def metered_chat_completion(
    client: Any,
    *,
    feature: str,
    model: str,
    messages: list[dict[str, Any]],
    job_id: str | None,
    attempts: int = 3,
    **kwargs: Any,
) -> Any:
    """``client.chat.completions.create`` through the ledger, request unchanged."""

    prompt = "\n".join(str(message.get("content", "")) for message in messages)

    def settle(receipt: ledger.ReservationReceipt, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            ledger.settle_paid_call_cost(
                receipt,
                cost_usd=receipt.estimated_cost_usd,
                resolved_model=model,
                usage_json={"usage_missing": True},
            )
            return
        details = getattr(usage, "prompt_tokens_details", None)
        ledger.settle_paid_call(
            receipt,
            ledger.UsageMeter(
                tokens_in=int(getattr(usage, "prompt_tokens", 0) or 0),
                tokens_out=int(getattr(usage, "completion_tokens", 0) or 0),
                tokens_cached=int(getattr(details, "cached_tokens", 0) or 0),
                provider_request_id=_request_id(response),
                resolved_model=str(getattr(response, "model", None) or model),
            ),
        )

    return run_metered(
        feature=feature,
        model=model,
        estimate_usd=lambda: ledger.estimate_call_cost_usd(
            model=model,
            prompt=prompt,
            max_output_tokens=output_bound_tokens(prompt),
            media_mime=None,
        ),
        attempts=attempts,
        ctx=RunContext(job_id=job_id),
        identity=prompt,
        operation=lambda: client.chat.completions.create(model=model, messages=messages, **kwargs),
        settle=settle,
    )


def _request_id(response: Any) -> str | None:
    value = getattr(response, "_request_id", None) or getattr(response, "id", None)
    return str(value)[:200] if isinstance(value, str) and value else None
