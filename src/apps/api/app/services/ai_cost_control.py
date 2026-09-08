"""Atomic reservations, metering, and circuit breakers for paid AI calls.

The ledger is deliberately provider-agnostic. Every caller reserves its
worst-case cost before contacting a provider, then settles from provider usage.
PostgreSQL advisory transaction locks serialize each budget scope without a
hot singleton row. A database outage fails closed whenever enforcement is on.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import text

from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    CostControlUnavailableError,
    ProviderOutcomeUnknownError,
    RunContext,
)
from app.config import settings

log = structlog.get_logger()

PRICE_VERSION = "google-ai-2026-09-08.v3"
DEFAULT_MAX_OUTPUT_TOKENS = 8_192
_IMAGE_TILE_SIZE_PX = 768
_IMAGE_TOKENS_PER_TILE = 258
_IMAGE_TOKEN_FLOOR = 2_000
_COUNTED_STATUSES = ("reserved", "provider_started", "settled", "unknown")
_TEST_PURPOSES = frozenset({"live_eval", "provider_smoke"})
_EXPERIMENT_PURPOSES = frozenset(
    {"live_eval", "provider_smoke", "manual_qa", "omni_lab", "experiment"}
)
_OPTIONAL_PURPOSES = frozenset({"optional_background", "style_vision"})
_CANARY_PURPOSES = frozenset({"release_canary", "internal_canary"})

# Standard paid-tier USD per one million tokens, verified against the official
# Gemini Developer API pricing page updated 2026-09-04. Unknown models use the
# highest known text rate so a newly configured alias cannot under-reserve.


@dataclass(frozen=True, slots=True)
class UsageMeter:
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_thoughts: int = 0
    tokens_cached: int = 0
    tokens_tool: int = 0
    token_details: dict[str, Any] = field(default_factory=dict)
    provider_request_id: str | None = None
    resolved_model: str | None = None


@dataclass(frozen=True, slots=True)
class ReservationReceipt:
    id: uuid.UUID
    environment: str
    usage_purpose: str
    feature: str
    requested_model: str
    estimated_cost_usd: float
    price_version: str = PRICE_VERSION


@dataclass(frozen=True, slots=True)
class PaidCallRequest:
    idempotency_key: str
    feature: str
    model: str
    estimated_cost_usd: float
    ctx: RunContext
    provider: str = "google"
    cached_behavior_available: bool = False


def _month_start(now: datetime) -> datetime:
    return datetime(now.year, now.month, 1, tzinfo=UTC)


def _next_month(now: datetime) -> datetime:
    if now.month == 12:
        return datetime(now.year + 1, 1, 1, tzinfo=UTC)
    return datetime(now.year, now.month + 1, 1, tzinfo=UTC)


def _next_day(now: datetime) -> datetime:
    return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)


def _price_for(
    model: str,
    *,
    prompt_tokens: int,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return text/non-audio input, output, and cached-input rates."""

    lowered = model.lower()
    long_context = prompt_tokens > 200_000
    if lowered.startswith("gemini-3.1-pro"):
        return (
            (Decimal("4.00"), Decimal("18.00"), Decimal("0.40"))
            if long_context
            else (Decimal("2.00"), Decimal("12.00"), Decimal("0.20"))
        )
    if lowered.startswith(("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash")):
        # Promotional Standard rates through 2026-12-31. The dated price
        # version makes the required January update visible in reconciliation.
        return (Decimal("0.75"), Decimal("3.75"), Decimal("0.075"))
    if lowered.startswith("gemini-3.5-flash"):
        return (Decimal("1.50"), Decimal("9.00"), Decimal("0.15"))
    if lowered.startswith("gemini-3-flash"):
        return (Decimal("0.50"), Decimal("3.00"), Decimal("0.05"))
    if lowered.startswith("gemini-2.5-pro"):
        return (
            (Decimal("2.50"), Decimal("15.00"), Decimal("0.25"))
            if long_context
            else (Decimal("1.25"), Decimal("10.00"), Decimal("0.125"))
        )
    if lowered.startswith("gemini-2.5-flash-lite"):
        return (Decimal("0.10"), Decimal("0.40"), Decimal("0.01"))
    if lowered.startswith("gemini-2.5-flash"):
        return (Decimal("0.30"), Decimal("2.50"), Decimal("0.03"))
    if lowered.startswith("gemini-omni-flash"):
        # Text output. Video output gets its modality surcharge below.
        return (Decimal("1.50"), Decimal("9.00"), Decimal("1.50"))
    return (Decimal("4.00"), Decimal("18.00"), Decimal("0.40"))


def _modality_tokens(details: dict[str, Any] | None, key: str, modality: str) -> int:
    if not isinstance(details, dict):
        return 0
    rows = details.get(key)
    if not isinstance(rows, list):
        return 0
    total = 0
    for row in rows:
        if isinstance(row, dict):
            raw_modality = row.get("modality")
            raw_count = row.get("token_count", row.get("tokenCount", 0))
        else:
            raw_modality = getattr(row, "modality", None)
            raw_count = getattr(row, "token_count", 0)
        name = getattr(raw_modality, "name", None) or str(raw_modality or "")
        if name.rsplit(".", 1)[-1].upper() != modality:
            continue
        try:
            total += max(0, int(raw_count or 0))
        except (TypeError, ValueError):
            continue
    return total


def calculate_cost_usd(model: str, usage: UsageMeter) -> float:
    """Calculate settled cost without double-counting cached prompt tokens."""

    input_rate, output_rate, cached_rate = _price_for(
        model,
        prompt_tokens=max(0, usage.tokens_in),
    )
    cached = max(0, usage.tokens_cached)
    uncached_in = max(0, usage.tokens_in - cached) + max(0, usage.tokens_tool)
    billable_out = max(0, usage.tokens_out) + max(0, usage.tokens_thoughts)
    cost = (
        Decimal(uncached_in) * input_rate
        + Decimal(cached) * cached_rate
        + Decimal(billable_out) * output_rate
    )

    lowered = model.lower()
    if lowered.startswith(("gemini-2.5-flash", "gemini-2.5-flash-lite")):
        # Audio prompt/cache tokens have a distinct rate on the 2.5 Flash
        # family. Prompt details include cached tokens, so remove the cached
        # audio subset before applying the uncached surcharge.
        prompt_audio = _modality_tokens(usage.token_details, "prompt_tokens_details", "AUDIO")
        cached_audio = min(
            prompt_audio,
            _modality_tokens(usage.token_details, "cache_tokens_details", "AUDIO"),
        )
        uncached_audio = max(0, prompt_audio - cached_audio)
        if lowered.startswith("gemini-2.5-flash-lite"):
            audio_input_rate = Decimal("0.30")
            audio_cache_rate = Decimal("0.03")
        else:
            audio_input_rate = Decimal("1.00")
            audio_cache_rate = Decimal("0.10")
        cost += Decimal(uncached_audio) * (audio_input_rate - input_rate)
        cost += Decimal(cached_audio) * (audio_cache_rate - cached_rate)
    if lowered.startswith("gemini-omni-flash"):
        video_output = min(
            max(0, usage.tokens_out),
            _modality_tokens(usage.token_details, "candidates_tokens_details", "VIDEO"),
        )
        cost += Decimal(video_output) * (Decimal("17.50") - output_rate)

    cost /= Decimal(1_000_000)
    return float(cost.quantize(Decimal("0.000001")))


def estimate_call_cost_usd(
    *,
    model: str,
    prompt: str,
    max_output_tokens: int | None,
    media_mime: str | None,
    media_duration_s: float | None = None,
    media_count: int = 1,
    media_width_px: int | None = None,
    media_height_px: int | None = None,
) -> float:
    """Conservative preflight estimate from the media actually sent."""

    prompt_tokens = max(1, (len(prompt.encode("utf-8")) + 2) // 3)
    media_tokens = 0
    if media_mime:
        if media_mime.startswith("video/"):
            # The product currently validates ordinary uploads at 30 minutes,
            # but this shared estimator also serves direct/background callers.
            # Never clamp an explicit duration downward: doing so would let an
            # out-of-contract caller reserve less than the provider may bill.
            duration = (
                max(0.1, float(media_duration_s)) if media_duration_s is not None else 1_800.0
            )
            media_tokens = int(duration * 320)
        elif media_mime.startswith("audio/"):
            duration = max(0.1, float(media_duration_s)) if media_duration_s is not None else 600.0
            media_tokens = int(duration * 40)
        elif media_mime.startswith("image/"):
            # Gemini tokenizes larger images in tiles. Preserve the historical
            # 2k/image floor for small thumbnails while allowing callers that
            # know the submitted dimensions to reserve the real tiled bound.
            image_tokens = _IMAGE_TOKEN_FLOOR
            if media_width_px is not None and media_height_px is not None:
                width = max(1, int(media_width_px))
                height = max(1, int(media_height_px))
                tiles = math.ceil(width / _IMAGE_TILE_SIZE_PX) * math.ceil(
                    height / _IMAGE_TILE_SIZE_PX
                )
                image_tokens = max(image_tokens, tiles * _IMAGE_TOKENS_PER_TILE)
            media_tokens = max(1, int(media_count)) * image_tokens
    output_tokens = effective_max_output_tokens(max_output_tokens)
    prompt_details: list[dict[str, Any]] = [{"modality": "TEXT", "token_count": prompt_tokens}]
    if media_tokens:
        prompt_details.append(
            {
                "modality": (
                    "AUDIO"
                    if media_mime and media_mime.startswith("audio/")
                    else ("IMAGE" if media_mime and media_mime.startswith("image/") else "VIDEO")
                ),
                "token_count": media_tokens,
            }
        )
    estimate = calculate_cost_usd(
        model,
        UsageMeter(
            tokens_in=prompt_tokens + media_tokens,
            tokens_out=output_tokens,
            # Preview reasoning may consume output-priced tokens not represented
            # in max_output_tokens on every SDK version.
            tokens_thoughts=output_tokens,
            token_details={"prompt_tokens_details": prompt_details},
        ),
    )
    return max(0.000001, estimate)


def effective_max_output_tokens(max_output_tokens: int | None) -> int:
    """Provider cap used by both reservation estimates and SDK requests.

    Individual agents keep their deliberate explicit contracts. Agents whose
    class-level contract is ``None`` inherit the same conservative default the
    ledger has always reserved, closing the gap where the estimate was bounded
    but the provider request was not.
    """

    return max_output_tokens if max_output_tokens is not None else DEFAULT_MAX_OUTPUT_TOKENS


def logical_call_id(
    *,
    feature: str,
    model: str,
    prompt: str,
    ctx: RunContext,
    attempt: int,
    media_identity: str | None = None,
) -> str:
    """Stable on task redelivery, distinct across paid runtime retries."""

    execution_id = ctx.request_id
    if not execution_id:
        try:
            from celery import current_task  # noqa: PLC0415

            execution_id = str(getattr(current_task.request, "id", "") or "") or None
        except Exception:  # noqa: BLE001 - Celery is optional in lightweight tests
            execution_id = None
    if not execution_id:
        # No durable execution identity exists for this synchronous call. A
        # random ID avoids blocking a legitimate later invocation; HTTP paid
        # endpoints provide request_id explicitly.
        execution_id = uuid.uuid4().hex
    parts = [
        feature,
        model,
        execution_id,
        ctx.job_id or "",
        ctx.creator_agent_session_id or "",
        ctx.test_run_id or "",
        str(ctx.segment_idx if ctx.segment_idx is not None else ""),
        str(attempt),
    ]
    # A user/client intent key is the idempotency authority: a buggy retry
    # with changed text must not mint a second paid request. Server-generated
    # execution IDs still bind the exact prompt/media so unrelated work cannot
    # collide merely because it reused an HTTP or Celery request identifier.
    if not (ctx.request_id and ctx.request_id_authoritative):
        parts.extend(
            (
                hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                hashlib.sha256((media_identity or "").encode("utf-8")).hexdigest(),
            )
        )
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _environment_cap(environment: str) -> float:
    return {
        "production": settings.ai_production_monthly_budget_usd,
        "development": settings.ai_development_monthly_budget_usd,
        "lab": settings.ai_omni_lab_monthly_budget_usd,
        "test": 0.0,
    }.get(environment, 0.0)


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except (ValueError, TypeError, AttributeError):
        return None


def _resolve_creator(
    conn: Any, ctx: RunContext
) -> tuple[uuid.UUID | None, uuid.UUID | None, uuid.UUID | None]:
    creator_id = _uuid_or_none(ctx.creator_id)
    job_id = _uuid_or_none(ctx.job_id)
    session_id = _uuid_or_none(ctx.creator_agent_session_id)
    if creator_id is None and job_id is not None:
        creator_id = conn.execute(
            text("SELECT user_id FROM jobs WHERE id = :id"), {"id": str(job_id)}
        ).scalar_one_or_none()
    if creator_id is None and session_id is not None:
        creator_id = conn.execute(
            text("SELECT creator_id FROM creator_agent_sessions WHERE id = :id"),
            {"id": str(session_id)},
        ).scalar_one_or_none()
    return creator_id, job_id, session_id


def _is_internal(conn: Any, creator_id: uuid.UUID | None) -> bool:
    if creator_id is None:
        return False
    return bool(
        conn.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM internal_account_grants "
                "WHERE creator_id = :creator_id AND status = 'active')"
            ),
            {"creator_id": str(creator_id)},
        ).scalar_one()
    )


def _reject(
    *,
    scope: str,
    reset_at: datetime,
    cached_behavior_available: bool,
    reason: str = "ai_budget_exhausted",
) -> None:
    if reason == "invalid_ai_usage_environment":
        raise CostControlUnavailableError(reason)
    if reason in {
        "paid_test_attribution_required",
        "invalid_usage_purpose",
        "estimated_cost_exceeds_approval",
        "creator_attribution_required",
        "release_canary_attribution_required",
        "release_canary_requires_internal_account",
    }:
        raise AiCostControlPolicyError(
            scope=scope,
            reason=reason,
            status_code=(403 if reason.startswith("release_canary") else 422),
        )
    raise AiBudgetExceededError(
        scope=scope,
        reset_at=reset_at.isoformat(),
        cached_behavior_available=cached_behavior_available,
        reason=reason,
    )


def _scope_spend(conn: Any, *, environment: str, period_start: datetime) -> Decimal:
    value = conn.execute(
        text(
            """
            SELECT COALESCE(SUM(
                CASE WHEN status = 'settled'
                     THEN COALESCE(settled_cost_usd, estimated_cost_usd)
                     WHEN status IN ('provider_started', 'unknown') THEN estimated_cost_usd
                     WHEN status = 'reserved' AND expires_at > now() THEN estimated_cost_usd
                     ELSE 0 END
            ), 0)
            FROM ai_cost_reservations
            WHERE environment = :environment
              AND period_start = :period_start
              AND status IN ('reserved', 'provider_started', 'settled', 'unknown')
            """
        ),
        {"environment": environment, "period_start": period_start},
    ).scalar_one()
    return Decimal(str(value or 0))


def _purpose_spend(
    conn: Any,
    *,
    purposes: set[str] | frozenset[str],
    period_start: datetime,
) -> Decimal:
    value = conn.execute(
        text(
            """
            SELECT COALESCE(SUM(
                CASE WHEN status = 'settled'
                     THEN COALESCE(settled_cost_usd, estimated_cost_usd)
                     WHEN status IN ('provider_started', 'unknown') THEN estimated_cost_usd
                     WHEN status = 'reserved' AND expires_at > now() THEN estimated_cost_usd
                     ELSE 0 END
            ), 0)
            FROM ai_cost_reservations
            WHERE usage_purpose = ANY(:purposes)
              AND period_start = :period_start
              AND status IN ('reserved', 'provider_started', 'settled', 'unknown')
            """
        ),
        {"purposes": list(purposes), "period_start": period_start},
    ).scalar_one()
    return Decimal(str(value or 0))


def _test_run_spend(conn: Any, *, test_run_id: str) -> Decimal:
    value = conn.execute(
        text(
            """
            SELECT COALESCE(SUM(
                CASE WHEN status = 'settled'
                     THEN COALESCE(settled_cost_usd, estimated_cost_usd)
                     WHEN status IN ('provider_started', 'unknown') THEN estimated_cost_usd
                     WHEN status = 'reserved' AND expires_at > now() THEN estimated_cost_usd
                     ELSE 0 END
            ), 0)
            FROM ai_cost_reservations
            WHERE test_run_id = :test_run_id
              AND status IN ('reserved', 'provider_started', 'settled', 'unknown')
            """
        ),
        {"test_run_id": test_run_id},
    ).scalar_one()
    return Decimal(str(value or 0))


def _override_total(conn: Any, scope: str, now: datetime) -> Decimal:
    value = conn.execute(
        text(
            "SELECT COALESCE(SUM(additional_cost_usd), 0) FROM ai_budget_overrides "
            "WHERE scope = :scope AND revoked_at IS NULL AND expires_at > :now"
        ),
        {"scope": scope, "now": now},
    ).scalar_one()
    return Decimal(str(value or 0))


def reserve_paid_call(request: PaidCallRequest) -> ReservationReceipt | None:
    """Reserve a paid call or raise before provider contact.

    Returns ``None`` while the rollout flag is off. Once enabled, any database
    failure is a hard stop: a missing ledger must never become unmetered spend.
    """

    if not settings.ai_cost_control_enabled:
        return None
    now = datetime.now(UTC)
    environment = settings.ai_usage_environment
    estimate = max(0.000001, float(request.estimated_cost_usd))
    period_start = _month_start(now)
    reset_at = _next_month(now)

    try:
        from app.database import sync_engine  # noqa: PLC0415

        with sync_engine.begin() as conn:
            creator_id, job_id, session_id = _resolve_creator(conn, request.ctx)
            internal = _is_internal(conn, creator_id)
            purpose = (request.ctx.usage_purpose or "").strip().lower()
            test_run_id = request.ctx.test_run_id
            approved_max_cost_usd = request.ctx.estimated_max_cost_usd
            reservation_approved = request.ctx.reservation_approved

            # A local manual-QA render fans out into Celery tasks, so request
            # headers cannot be its only attribution carrier. An operator may
            # explicitly approve one development envelope in process config;
            # ordinary customer/background contexts then share that atomic run
            # cap. Partially attributed live-eval/smoke requests still fail.
            if (
                environment == "development"
                and not test_run_id
                and approved_max_cost_usd is None
                and not reservation_approved
                and (not purpose or purpose in _OPTIONAL_PURPOSES)
                and settings.ai_manual_qa_reservation_approved
                and settings.ai_manual_qa_test_run_id.strip()
                and settings.ai_manual_qa_max_cost_usd is not None
            ):
                purpose = purpose or "manual_qa"
                test_run_id = settings.ai_manual_qa_test_run_id.strip()
                approved_max_cost_usd = settings.ai_manual_qa_max_cost_usd
                reservation_approved = True

            if environment in {"development", "lab", "test"}:
                if settings.ai_paid_test_attribution_required and (
                    not purpose
                    or not test_run_id
                    or approved_max_cost_usd is None
                    or not reservation_approved
                ):
                    _reject(
                        scope=f"{environment}:attribution",
                        reset_at=reset_at,
                        cached_behavior_available=request.cached_behavior_available,
                        reason="paid_test_attribution_required",
                    )
                if purpose not in _TEST_PURPOSES | _EXPERIMENT_PURPOSES | _OPTIONAL_PURPOSES:
                    _reject(
                        scope=f"{environment}:usage_purpose",
                        reset_at=reset_at,
                        cached_behavior_available=request.cached_behavior_available,
                        reason="invalid_usage_purpose",
                    )
                if estimate > float(approved_max_cost_usd or 0):
                    _reject(
                        scope=f"{environment}:call_estimate",
                        reset_at=reset_at,
                        cached_behavior_available=request.cached_behavior_available,
                        reason="estimated_cost_exceeds_approval",
                    )
            elif environment == "production":
                if creator_id is None:
                    if purpose in _CANARY_PURPOSES and request.ctx.release_canary_id:
                        pass
                    elif purpose in _OPTIONAL_PURPOSES:
                        pass
                    else:
                        _reject(
                            scope="production:creator_attribution",
                            reset_at=reset_at,
                            cached_behavior_available=request.cached_behavior_available,
                            reason="creator_attribution_required",
                        )
                elif internal:
                    if purpose not in _CANARY_PURPOSES or not request.ctx.release_canary_id:
                        _reject(
                            scope="production:internal_untagged",
                            reset_at=reset_at,
                            cached_behavior_available=request.cached_behavior_available,
                            reason="release_canary_attribution_required",
                        )
                elif purpose in _CANARY_PURPOSES:
                    _reject(
                        scope="production:canary_principal",
                        reset_at=reset_at,
                        cached_behavior_available=request.cached_behavior_available,
                        reason="release_canary_requires_internal_account",
                    )
                elif not purpose:
                    purpose = "customer"
            else:
                _reject(
                    scope="configuration:environment",
                    reset_at=reset_at,
                    cached_behavior_available=request.cached_behavior_available,
                    reason="invalid_ai_usage_environment",
                )

            if purpose == "provider_smoke" and estimate > settings.ai_weekly_smoke_max_cost_usd:
                _reject(
                    scope="provider_smoke:run",
                    reset_at=reset_at,
                    cached_behavior_available=request.cached_behavior_available,
                )

            scopes = [f"environment:{environment}"]
            if purpose in _CANARY_PURPOSES:
                scopes.append("purpose:release_canary")
            if request.feature == "edit_director" and creator_id is not None:
                scopes.append(f"director:{creator_id}:{now.date().isoformat()}")
            if test_run_id:
                scopes.append(f"test_run:{test_run_id}")
            for scope in sorted(scopes):
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"),
                    {"scope": scope},
                )

            existing = (
                conn.execute(
                    text(
                        "SELECT id, status, expires_at FROM ai_cost_reservations "
                        "WHERE idempotency_key = :key FOR UPDATE"
                    ),
                    {"key": request.idempotency_key},
                )
                .mappings()
                .one_or_none()
            )
            if (
                existing
                and existing["status"] in _COUNTED_STATUSES
                and (
                    existing["status"] in {"provider_started", "settled", "unknown"}
                    or existing["expires_at"] > now
                )
            ):
                raise ProviderOutcomeUnknownError(
                    f"paid call {request.idempotency_key[:12]} already {existing['status']}"
                )

            base_cap = Decimal(str(_environment_cap(environment)))
            env_scope = f"environment:{environment}"
            limit = base_cap + _override_total(conn, env_scope, now)
            spend = _scope_spend(conn, environment=environment, period_start=period_start)
            projected = spend + Decimal(str(estimate))
            fraction = projected / limit if limit > 0 else Decimal("Infinity")

            if purpose in _EXPERIMENT_PURPOSES | _OPTIONAL_PURPOSES and fraction >= Decimal("0.80"):
                _reject(
                    scope=env_scope,
                    reset_at=reset_at,
                    cached_behavior_available=request.cached_behavior_available,
                )
            if (
                purpose in _CANARY_PURPOSES or request.feature == "edit_director"
            ) and fraction >= Decimal("0.90"):
                _reject(
                    scope=env_scope,
                    reset_at=reset_at,
                    cached_behavior_available=request.cached_behavior_available,
                )
            if projected > limit:
                _reject(
                    scope=env_scope,
                    reset_at=reset_at,
                    cached_behavior_available=request.cached_behavior_available,
                )

            if purpose in _CANARY_PURPOSES:
                canary_scope = "purpose:release_canary"
                canary_limit = Decimal(str(settings.ai_release_canary_monthly_budget_usd))
                canary_limit += _override_total(conn, canary_scope, now)
                canary_spend = _purpose_spend(
                    conn, purposes=_CANARY_PURPOSES, period_start=period_start
                )
                if canary_spend + Decimal(str(estimate)) > canary_limit:
                    _reject(
                        scope=canary_scope,
                        reset_at=reset_at,
                        cached_behavior_available=request.cached_behavior_available,
                    )

            if test_run_id:
                run_limit = Decimal(str(approved_max_cost_usd or 0))
                run_spend = _test_run_spend(
                    conn,
                    test_run_id=test_run_id,
                )
                if run_spend + Decimal(str(estimate)) > run_limit:
                    _reject(
                        scope=f"test_run:{test_run_id}",
                        reset_at=reset_at,
                        cached_behavior_available=request.cached_behavior_available,
                    )

            if request.feature == "edit_director" and creator_id is not None:
                used = int(
                    conn.execute(
                        text(
                            "SELECT COUNT(*) FROM ai_cost_reservations "
                            "WHERE creator_id = :creator_id AND feature = 'edit_director' "
                            "AND created_at >= :day_start "
                            "AND status IN ('reserved','provider_started','settled','unknown')"
                        ),
                        {
                            "creator_id": str(creator_id),
                            "day_start": datetime(now.year, now.month, now.day, tzinfo=UTC),
                        },
                    ).scalar_one()
                )
                if used >= settings.edit_director_daily_paid_limit:
                    _reject(
                        scope="edit_director:user_day",
                        reset_at=_next_day(now),
                        cached_behavior_available=request.cached_behavior_available,
                    )

            reservation_id = uuid.UUID(str(existing["id"])) if existing else uuid.uuid4()
            params = {
                "id": str(reservation_id),
                "key": request.idempotency_key,
                "environment": environment,
                "purpose": purpose,
                "feature": request.feature,
                "provider": request.provider,
                "model": request.model,
                "creator_id": str(creator_id) if creator_id else None,
                "job_id": str(job_id) if job_id else None,
                "session_id": str(session_id) if session_id else None,
                "principal_type": (
                    "system" if creator_id is None else "internal" if internal else "customer"
                ),
                "test_run_id": test_run_id,
                "canary_id": request.ctx.release_canary_id,
                "estimate": estimate,
                "price_version": PRICE_VERSION,
                "period_start": period_start,
                "expires_at": now + timedelta(seconds=settings.ai_reservation_ttl_seconds),
            }
            if existing:
                conn.execute(
                    text(
                        """
                        UPDATE ai_cost_reservations SET
                            status = 'reserved', environment = :environment,
                            usage_purpose = :purpose, feature = :feature,
                            provider = :provider, requested_model = :model,
                            resolved_model = NULL, creator_id = :creator_id,
                            job_id = :job_id, creator_agent_session_id = :session_id,
                            principal_type = :principal_type,
                            test_run_id = :test_run_id, release_canary_id = :canary_id,
                            estimated_cost_usd = :estimate, settled_cost_usd = NULL,
                            price_version = :price_version, provider_request_id = NULL,
                            usage_json = NULL, period_start = :period_start,
                            expires_at = :expires_at, settled_at = NULL,
                            created_at = now(), updated_at = now()
                        WHERE id = :id
                        """
                    ),
                    params,
                )
            else:
                conn.execute(
                    text(
                        """
                        INSERT INTO ai_cost_reservations (
                            id, idempotency_key, environment, usage_purpose,
                            feature, provider, requested_model, creator_id, job_id,
                            creator_agent_session_id, principal_type, test_run_id,
                            release_canary_id,
                            status, estimated_cost_usd, price_version, period_start,
                            expires_at
                        ) VALUES (
                            :id, :key, :environment, :purpose, :feature, :provider,
                            :model, :creator_id, :job_id, :session_id, :principal_type,
                            :test_run_id, :canary_id, 'reserved', :estimate, :price_version,
                            :period_start, :expires_at
                        )
                        """
                    ),
                    params,
                )
            return ReservationReceipt(
                id=reservation_id,
                environment=environment,
                usage_purpose=purpose,
                feature=request.feature,
                requested_model=request.model,
                estimated_cost_usd=estimate,
            )
    except (AiBudgetExceededError, AiCostControlPolicyError, ProviderOutcomeUnknownError):
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed on every ledger error
        log.error(
            "ai_cost_reservation_failed_closed",
            feature=request.feature,
            model=request.model,
            error_type=type(exc).__name__,
        )
        raise CostControlUnavailableError("AI cost-control ledger unavailable") from exc


def settle_paid_call(receipt: ReservationReceipt | None, usage: UsageMeter) -> float:
    if receipt is None:
        return calculate_cost_usd(usage.resolved_model or "gemini", usage)
    cost = calculate_cost_usd(usage.resolved_model or receipt.requested_model, usage)
    try:
        from app.database import sync_engine  # noqa: PLC0415

        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE ai_cost_reservations SET
                        status = 'settled', settled_cost_usd = :cost,
                        resolved_model = :resolved_model,
                        provider_request_id = :provider_request_id,
                        usage_json = CAST(:usage_json AS JSONB), settled_at = now(),
                        updated_at = now()
                    WHERE id = :id AND status IN ('provider_started', 'unknown')
                    """
                ),
                {
                    "id": str(receipt.id),
                    "cost": cost,
                    "resolved_model": usage.resolved_model or receipt.requested_model,
                    "provider_request_id": usage.provider_request_id,
                    "usage_json": json.dumps(
                        {
                            "tokens_in": usage.tokens_in,
                            "tokens_out": usage.tokens_out,
                            "tokens_thoughts": usage.tokens_thoughts,
                            "tokens_cached": usage.tokens_cached,
                            "tokens_tool": usage.tokens_tool,
                            "token_details": usage.token_details,
                        },
                        default=str,
                    ),
                },
            )
    except Exception as exc:  # noqa: BLE001 - retain reservation estimate on failure
        log.error(
            "ai_cost_settlement_failed",
            reservation_id=str(receipt.id),
            error_type=type(exc).__name__,
        )
    return cost


def settle_paid_call_cost(
    receipt: ReservationReceipt | None,
    *,
    cost_usd: float,
    resolved_model: str,
    provider_request_id: str | None = None,
    usage_json: dict[str, Any] | None = None,
) -> float:
    """Settle duration/image-priced APIs that do not report token usage."""

    cost = max(0.0, round(float(cost_usd), 6))
    if receipt is None:
        return cost
    try:
        from app.database import sync_engine  # noqa: PLC0415

        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    """
                    UPDATE ai_cost_reservations SET
                        status = 'settled', settled_cost_usd = :cost,
                        resolved_model = :resolved_model,
                        provider_request_id = :provider_request_id,
                        usage_json = CAST(:usage_json AS JSONB), settled_at = now(),
                        updated_at = now()
                    WHERE id = :id AND status IN ('provider_started', 'unknown')
                    """
                ),
                {
                    "id": str(receipt.id),
                    "cost": cost,
                    "resolved_model": resolved_model,
                    "provider_request_id": provider_request_id,
                    "usage_json": json.dumps(usage_json or {}, default=str),
                },
            )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "ai_cost_settlement_failed",
            reservation_id=str(receipt.id),
            error_type=type(exc).__name__,
        )
    return cost


def _transition_reservation(
    receipt: ReservationReceipt | None,
    *,
    status: str,
    expires_in_seconds: int | None = None,
    from_statuses: tuple[str, ...] = ("reserved",),
) -> None:
    if receipt is None:
        return
    try:
        from app.database import sync_engine  # noqa: PLC0415

        with sync_engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE ai_cost_reservations SET status = :status, "
                    "expires_at = COALESCE(:expires_at, expires_at), updated_at = now() "
                    "WHERE id = :id AND status = ANY(:from_statuses)"
                ),
                {
                    "id": str(receipt.id),
                    "status": status,
                    "from_statuses": list(from_statuses),
                    "expires_at": (
                        datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
                        if expires_in_seconds is not None
                        else None
                    ),
                },
            )
    except Exception as exc:  # noqa: BLE001 - a reserved estimate remains safe
        log.error(
            "ai_cost_reservation_transition_failed",
            reservation_id=str(receipt.id),
            target_status=status,
            error_type=type(exc).__name__,
        )


def release_paid_call(receipt: ReservationReceipt | None) -> None:
    _transition_reservation(
        receipt,
        status="released",
        from_statuses=("reserved", "provider_started"),
    )


def mark_paid_call_started(receipt: ReservationReceipt | None) -> None:
    """Persist the no-retry fence before any provider operation can begin."""

    if receipt is None:
        return
    try:
        from app.database import sync_engine  # noqa: PLC0415

        with sync_engine.begin() as conn:
            result = conn.execute(
                text(
                    "UPDATE ai_cost_reservations SET status = 'provider_started', "
                    "provider_started_at = now(), updated_at = now() "
                    "WHERE id = :id AND status = 'reserved'"
                ),
                {"id": str(receipt.id)},
            )
            if result.rowcount != 1:
                raise ProviderOutcomeUnknownError(
                    f"paid call reservation {str(receipt.id)[:12]} is not startable"
                )
    except ProviderOutcomeUnknownError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CostControlUnavailableError("AI cost-control start fence unavailable") from exc


def mark_paid_call_unknown(receipt: ReservationReceipt | None) -> None:
    _transition_reservation(
        receipt,
        status="unknown",
        expires_in_seconds=settings.ai_unknown_reservation_ttl_seconds,
        from_statuses=("provider_started",),
    )


def google_usage_meter(response: Any, *, requested_model: str) -> UsageMeter:
    """Project the Google SDK's evolving usage object into the stable ledger."""

    usage = getattr(response, "usage_metadata", None)
    details: dict[str, Any] = {}
    if usage is not None:
        for name in (
            "prompt_tokens_details",
            "candidates_tokens_details",
            "cache_tokens_details",
            "tool_use_prompt_tokens_details",
            "traffic_type",
            "total_token_count",
        ):
            value = getattr(usage, name, None)
            if value is None:
                continue
            details[name] = _json_safe_usage_detail(value)
    request_id = getattr(response, "response_id", None) or getattr(response, "request_id", None)
    return UsageMeter(
        tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0,
        tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0,
        tokens_thoughts=(int(getattr(usage, "thoughts_token_count", 0) or 0) if usage else 0),
        tokens_cached=(int(getattr(usage, "cached_content_token_count", 0) or 0) if usage else 0),
        tokens_tool=(int(getattr(usage, "tool_use_prompt_token_count", 0) or 0) if usage else 0),
        token_details=details,
        provider_request_id=str(request_id)[:200] if request_id else None,
        resolved_model=str(getattr(response, "model_version", None) or requested_model),
    )


def _json_safe_usage_detail(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_json_safe_usage_detail(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_usage_detail(item) for key, item in value.items()}
    name = getattr(value, "name", None)
    return str(name if name is not None else value)


def execute_metered_google_call(
    *,
    request: PaidCallRequest,
    operation: Callable[[], Any],
) -> Any:
    """Run a direct Google SDK call through the same reservation ledger."""

    receipt = reserve_paid_call(request)
    mark_paid_call_started(receipt)
    try:
        response = operation()
    except (TimeoutError, ProviderOutcomeUnknownError):
        mark_paid_call_unknown(receipt)
        raise ProviderOutcomeUnknownError("Google provider outcome unknown")
    except Exception as exc:
        # APIError with a concrete HTTP status is a provider-declared failure;
        # unknown transport/SDK failures conservatively retain the estimate.
        if getattr(exc, "code", None) is not None:
            release_paid_call(receipt)
        else:
            mark_paid_call_unknown(receipt)
        raise
    settle_paid_call(
        receipt,
        google_usage_meter(response, requested_model=request.model),
    )
    return response


def execute_metered_fixed_cost_google_call(
    *,
    request: PaidCallRequest,
    operation: Callable[[], Any],
    resolved_model: str,
    actual_cost_usd: float,
    provider_error: Callable[[Any], str | None] | None = None,
    usage_json: dict[str, Any] | None = None,
) -> Any:
    """Meter an image/duration-priced Google API without token metadata."""

    receipt = reserve_paid_call(request)
    mark_paid_call_started(receipt)
    try:
        response = operation()
    except (TimeoutError, ProviderOutcomeUnknownError):
        mark_paid_call_unknown(receipt)
        raise ProviderOutcomeUnknownError("Google provider outcome unknown")
    except Exception as exc:
        # Match the direct Gemini boundary: a concrete provider status proves
        # the operation failed; an unclassified transport failure does not.
        if getattr(exc, "code", None) is not None:
            release_paid_call(receipt)
        else:
            mark_paid_call_unknown(receipt)
        raise

    declared_error = provider_error(response) if provider_error is not None else None
    if declared_error:
        release_paid_call(receipt)
        raise RuntimeError(declared_error)
    settle_paid_call_cost(
        receipt,
        cost_usd=actual_cost_usd,
        resolved_model=resolved_model,
        provider_request_id=(
            str(getattr(response, "response_id", None))[:200]
            if getattr(response, "response_id", None)
            else None
        ),
        usage_json=usage_json,
    )
    return response
