"""Acceptance coverage for the paid-call reservation authority.

These tests use PostgreSQL deliberately: the cap guarantee depends on
``pg_advisory_xact_lock`` and cannot be proved by a mocked connection.
CI migrates ``nova_test`` before running the suite.
"""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.agents._runtime import (
    AiBudgetExceededError,
    AiCostControlPolicyError,
    CostControlUnavailableError,
    ProviderOutcomeUnknownError,
    RunContext,
)
from app.config import settings
from app.database import sync_engine
from app.services.ai_cost_control import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    PaidCallRequest,
    UsageMeter,
    calculate_cost_usd,
    effective_max_output_tokens,
    estimate_call_cost_usd,
    execute_metered_fixed_cost_google_call,
    logical_call_id,
    mark_paid_call_started,
    mark_paid_call_unknown,
    reserve_paid_call,
    settle_paid_call_cost,
)


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch):
    prefix = f"test-ai-cost-control:{uuid.uuid4()}"
    try:
        with sync_engine.connect() as conn:
            conn.execute(text("SELECT 1 FROM ai_cost_reservations LIMIT 0"))
    except OperationalError as exc:
        pytest.skip(f"Postgres not reachable for reservation integration test: {exc!r}")
    except ProgrammingError as exc:
        pytest.fail(f"nova_test was not migrated to the cost-control schema: {exc!r}")

    monkeypatch.setattr(settings, "ai_cost_control_enabled", True)
    monkeypatch.setattr(settings, "ai_paid_test_attribution_required", True)
    monkeypatch.setattr(settings, "ai_production_monthly_budget_usd", 1_000.0)
    monkeypatch.setattr(settings, "ai_development_monthly_budget_usd", 1_000.0)
    monkeypatch.setattr(settings, "ai_omni_lab_monthly_budget_usd", 1_000.0)
    monkeypatch.setattr(settings, "ai_release_canary_monthly_budget_usd", 1_000.0)
    monkeypatch.setattr(settings, "ai_weekly_smoke_max_cost_usd", 0.20)
    monkeypatch.setattr(settings, "ai_reservation_ttl_seconds", 300)
    monkeypatch.setattr(settings, "ai_unknown_reservation_ttl_seconds", 300)
    yield prefix
    with sync_engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ai_cost_reservations WHERE idempotency_key LIKE :prefix"),
            {"prefix": f"{prefix}%"},
        )
        conn.execute(
            text("DELETE FROM ai_budget_overrides WHERE reason = :reason"),
            {"reason": prefix},
        )
        conn.execute(
            text("DELETE FROM users WHERE email LIKE :prefix"),
            {"prefix": f"{prefix}%"},
        )


def _request(
    prefix: str,
    suffix: str,
    *,
    estimate: float,
    purpose: str = "live_eval",
    run_id: str | None = None,
    feature: str = "test_ai_cost_control:agent",
    creator_id: uuid.UUID | None = None,
) -> PaidCallRequest:
    return PaidCallRequest(
        idempotency_key=f"{prefix}:{suffix}",
        feature=feature,
        model="gemini-3.1-pro-preview",
        estimated_cost_usd=estimate,
        ctx=RunContext(
            request_id=f"{prefix}:{suffix}",
            creator_id=str(creator_id) if creator_id else None,
            usage_purpose=purpose,
            test_run_id=run_id or f"{prefix}:run",
            estimated_max_cost_usd=2.0,
            reservation_approved=True,
        ),
    )


def _create_creator(prefix: str, suffix: str, *, internal: bool = False) -> uuid.UUID:
    creator_id = uuid.uuid4()
    with sync_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": str(creator_id), "email": f"{prefix}:{suffix}@example.test"},
        )
        if internal:
            conn.execute(
                text(
                    "INSERT INTO internal_account_grants "
                    "(id, creator_id, status, granted_by, idempotency_key) "
                    "VALUES (:id, :creator_id, 'active', 'pytest', :key)"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "creator_id": str(creator_id),
                    "key": f"{prefix}:{suffix}",
                },
            )
    return creator_id


def test_cost_calculation_separates_cached_thought_and_tool_tokens() -> None:
    # 2.5 Flash: input .30/M, output 2.50/M, cache .075/M.
    cost = calculate_cost_usd(
        "gemini-2.5-flash",
        UsageMeter(
            tokens_in=1_000,
            tokens_cached=400,
            tokens_tool=200,
            tokens_out=300,
            tokens_thoughts=100,
        ),
    )
    assert cost == pytest.approx(0.001252)


def test_cost_calculation_honors_audio_and_video_modality_rates() -> None:
    flash_audio = calculate_cost_usd(
        "gemini-2.5-flash",
        UsageMeter(
            tokens_in=1_000,
            tokens_out=100,
            token_details={
                "prompt_tokens_details": [
                    {"modality": "AUDIO", "token_count": 600},
                    {"modality": "TEXT", "token_count": 400},
                ]
            },
        ),
    )
    omni_video = calculate_cost_usd(
        "gemini-omni-flash-preview",
        UsageMeter(
            tokens_in=1_000,
            tokens_out=5_792,
            token_details={
                "candidates_tokens_details": [{"modality": "VIDEO", "token_count": 5_792}]
            },
        ),
    )

    assert flash_audio == pytest.approx(0.00097)
    assert omni_video == pytest.approx(0.10286)


def test_specific_flash_prefix_is_not_priced_as_generic_gemini_three() -> None:
    cost = calculate_cost_usd(
        "gemini-3.6-flash",
        UsageMeter(tokens_in=1_000_000, tokens_out=1_000_000),
    )
    assert cost == pytest.approx(4.5)


def test_media_estimate_counts_every_image_and_full_explicit_duration() -> None:
    single_image = estimate_call_cost_usd(
        model="gemini-2.5-flash",
        prompt="classify",
        max_output_tokens=100,
        media_mime="image/jpeg",
        media_count=1,
    )
    image_batch = estimate_call_cost_usd(
        model="gemini-2.5-flash",
        prompt="classify",
        max_output_tokens=100,
        media_mime="image/jpeg",
        media_count=31,
    )
    thirty_minutes = estimate_call_cost_usd(
        model="gemini-2.5-flash",
        prompt="analyze",
        max_output_tokens=100,
        media_mime="video/mp4",
        media_duration_s=1_800,
    )
    sixty_minutes = estimate_call_cost_usd(
        model="gemini-2.5-flash",
        prompt="analyze",
        max_output_tokens=100,
        media_mime="video/mp4",
        media_duration_s=3_600,
    )

    assert image_batch > single_image
    assert sixty_minutes > thirty_minutes


def test_media_estimate_counts_large_image_tiles() -> None:
    small = estimate_call_cost_usd(
        model="gemini-2.5-flash",
        prompt="classify",
        max_output_tokens=100,
        media_mime="image/jpeg",
        media_width_px=1_536,
        media_height_px=1_536,
    )
    large = estimate_call_cost_usd(
        model="gemini-2.5-flash",
        prompt="classify",
        max_output_tokens=100,
        media_mime="image/jpeg",
        media_width_px=7_000,
        media_height_px=7_000,
    )

    assert large > small


def test_default_provider_output_cap_matches_estimator_default() -> None:
    assert effective_max_output_tokens(None) == DEFAULT_MAX_OUTPUT_TOKENS
    assert effective_max_output_tokens(2_048) == 2_048


def test_pro_long_context_uses_the_over_200k_tier() -> None:
    cost = calculate_cost_usd(
        "gemini-3.1-pro-preview",
        UsageMeter(tokens_in=200_001, tokens_out=1_000),
    )
    assert cost == pytest.approx(0.818004)


def test_logical_call_fence_distinguishes_media_with_identical_prompts() -> None:
    ctx = RunContext(request_id="one-style-review")
    first = logical_call_id(
        feature="style_observation",
        model="gemini-2.5-flash",
        prompt="same prompt",
        ctx=ctx,
        attempt=1,
        media_identity="https://files.example/videos/one",
    )
    second = logical_call_id(
        feature="style_observation",
        model="gemini-2.5-flash",
        prompt="same prompt",
        ctx=ctx,
        attempt=1,
        media_identity="https://files.example/videos/two",
    )

    assert first != second


def test_authoritative_client_intent_fences_changed_retry_payload() -> None:
    ctx = RunContext(request_id="client-edit-1", request_id_authoritative=True)
    first = logical_call_id(
        feature="edit-copilot",
        model="gemini-2.5-pro",
        prompt="make it tighter",
        ctx=ctx,
        attempt=1,
        media_identity="clip-one",
    )
    changed_retry = logical_call_id(
        feature="edit-copilot",
        model="gemini-2.5-pro",
        prompt="make it much tighter",
        ctx=ctx,
        attempt=1,
        media_identity="clip-two",
    )
    different_intent = logical_call_id(
        feature="edit-copilot",
        model="gemini-2.5-pro",
        prompt="make it much tighter",
        ctx=RunContext(request_id="client-edit-2", request_id_authoritative=True),
        attempt=1,
        media_identity="clip-two",
    )

    assert changed_retry == first
    assert different_intent != first


def test_logical_call_fence_distinguishes_paid_test_runs() -> None:
    def key(test_run_id: str) -> str:
        return logical_call_id(
            feature="song-classifier",
            model="gemini-2.5-flash",
            prompt="same fixture",
            ctx=RunContext(request_id="eval:baseline", test_run_id=test_run_id),
            attempt=1,
        )

    assert key("github-100-1") == key("github-100-1")
    assert key("github-100-1") != key("github-101-1")


def test_fixed_cost_google_call_reserves_then_settles(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, object]] = []
    receipt = object()
    monkeypatch.setattr(
        "app.services.ai_cost_control.reserve_paid_call",
        lambda request: events.append(("reserve", request)) or receipt,
    )
    monkeypatch.setattr(
        "app.services.ai_cost_control.settle_paid_call_cost",
        lambda got, **kwargs: events.append(("settle", (got, kwargs))),
    )
    monkeypatch.setattr(
        "app.services.ai_cost_control.mark_paid_call_started",
        lambda got: events.append(("started", got)),
    )
    request = PaidCallRequest(
        idempotency_key="vision-one",
        feature="text_overlay_ocr",
        model="cloud-vision-document-text-detection",
        estimated_cost_usd=0.0015,
        ctx=RunContext(usage_purpose="optional_background"),
        provider="google-cloud-vision",
    )
    response = object()

    assert (
        execute_metered_fixed_cost_google_call(
            request=request,
            operation=lambda: response,
            resolved_model=request.model,
            actual_cost_usd=0.0015,
            usage_json={"images": 1},
        )
        is response
    )
    assert events[0] == ("reserve", request)
    assert events[1] == ("started", receipt)
    assert events[2][0] == "settle"
    assert events[2][1][0] is receipt
    assert events[2][1][1]["cost_usd"] == 0.0015


def test_fixed_cost_google_declared_failure_releases_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt = object()
    released: list[object] = []
    monkeypatch.setattr(
        "app.services.ai_cost_control.reserve_paid_call",
        lambda _request: receipt,
    )
    monkeypatch.setattr(
        "app.services.ai_cost_control.release_paid_call",
        lambda got: released.append(got),
    )
    monkeypatch.setattr("app.services.ai_cost_control.mark_paid_call_started", lambda _got: None)
    request = PaidCallRequest(
        idempotency_key="vision-error",
        feature="text_overlay_ocr",
        model="cloud-vision-document-text-detection",
        estimated_cost_usd=0.0015,
        ctx=RunContext(usage_purpose="optional_background"),
        provider="google-cloud-vision",
    )

    with pytest.raises(RuntimeError, match="quota"):
        execute_metered_fixed_cost_google_call(
            request=request,
            operation=lambda: {"error": "quota"},
            resolved_model=request.model,
            actual_cost_usd=0.0015,
            provider_error=lambda response: str(response["error"]),
        )
    assert released == [receipt]


def test_paid_test_missing_attribution_fails_before_insert(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    request = _request(ledger, "missing", estimate=0.01)
    request.ctx.test_run_id = None

    with pytest.raises(AiCostControlPolicyError) as caught:
        reserve_paid_call(request)

    assert caught.value.reason == "paid_test_attribution_required"
    with sync_engine.connect() as conn:
        count = conn.scalar(
            text("SELECT COUNT(*) FROM ai_cost_reservations WHERE idempotency_key = :key"),
            {"key": request.idempotency_key},
        )
    assert count == 0


def test_manual_qa_envelope_attributes_background_calls(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "ai_manual_qa_test_run_id", f"{ledger}:manual")
    monkeypatch.setattr(settings, "ai_manual_qa_max_cost_usd", 0.05)
    monkeypatch.setattr(settings, "ai_manual_qa_reservation_approved", True)
    request = PaidCallRequest(
        idempotency_key=f"{ledger}:manual-background",
        feature="clip_analysis",
        model="gemini-2.5-flash",
        estimated_cost_usd=0.01,
        ctx=RunContext(usage_purpose="optional_background"),
    )

    receipt = reserve_paid_call(request)

    assert receipt is not None
    assert receipt.usage_purpose == "optional_background"
    with sync_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT test_run_id, usage_purpose FROM ai_cost_reservations "
                    "WHERE idempotency_key = :key"
                ),
                {"key": request.idempotency_key},
            )
            .mappings()
            .one()
        )
    assert row["test_run_id"] == f"{ledger}:manual"
    assert row["usage_purpose"] == "optional_background"


def test_production_call_without_creator_or_system_purpose_fails_closed(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    request = _request(ledger, "unattributed-prod", estimate=0.01, purpose="customer")
    request.ctx.test_run_id = None
    request.ctx.estimated_max_cost_usd = None

    with pytest.raises(AiCostControlPolicyError) as caught:
        reserve_paid_call(request)

    assert caught.value.reason == "creator_attribution_required"


def test_external_creator_cannot_self_label_as_release_canary(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    creator_id = _create_creator(ledger, "external-canary")
    request = _request(
        ledger,
        "external-canary",
        estimate=0.01,
        purpose="release_canary",
        creator_id=creator_id,
    )
    request.ctx.test_run_id = None
    request.ctx.estimated_max_cost_usd = None
    request.ctx.release_canary_id = "release-test"

    with pytest.raises(AiCostControlPolicyError) as caught:
        reserve_paid_call(request)

    assert caught.value.reason == "release_canary_requires_internal_account"


def test_development_rejects_invalid_purpose_and_estimate_above_approval(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    invalid = _request(ledger, "invalid-purpose", estimate=0.01, purpose="customer")
    with pytest.raises(AiCostControlPolicyError) as caught:
        reserve_paid_call(invalid)
    assert caught.value.reason == "invalid_usage_purpose"

    oversized = _request(ledger, "oversized-call", estimate=0.03)
    oversized.ctx.estimated_max_cost_usd = 0.02
    with pytest.raises(AiCostControlPolicyError) as caught:
        reserve_paid_call(oversized)
    assert caught.value.reason == "estimated_cost_exceeds_approval"


def test_ledger_outage_fails_closed_before_a_reservation(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")

    class _BrokenEngine:
        def begin(self):
            raise OperationalError("begin", {}, RuntimeError("database offline"))

    monkeypatch.setattr("app.database.sync_engine", _BrokenEngine())
    with pytest.raises(CostControlUnavailableError):
        reserve_paid_call(_request(ledger, "ledger-outage", estimate=0.01))


def test_concurrent_reservations_cannot_cross_test_run_cap(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    run_id = f"{ledger}:concurrent"
    requests = [
        _request(ledger, f"concurrent:{index}", estimate=0.015, run_id=run_id) for index in range(2)
    ]
    for request in requests:
        request.ctx.estimated_max_cost_usd = 0.02

    def attempt(request: PaidCallRequest) -> str:
        try:
            reserve_paid_call(request)
            return "reserved"
        except AiBudgetExceededError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, requests))

    assert sorted(outcomes) == ["rejected", "reserved"]
    with sync_engine.connect() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT COUNT(*) AS calls, COALESCE(SUM(estimated_cost_usd), 0) AS cost "
                    "FROM ai_cost_reservations WHERE test_run_id = :run_id"
                ),
                {"run_id": run_id},
            )
            .mappings()
            .one()
        )
    assert row["calls"] == 1
    assert float(row["cost"]) == pytest.approx(0.015)


def test_unknown_outcome_permanently_blocks_the_same_paid_attempt(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    request = _request(ledger, "unknown", estimate=0.01)
    receipt = reserve_paid_call(request)
    assert receipt is not None
    mark_paid_call_started(receipt)
    with sync_engine.connect() as conn:
        provider_started_at = conn.scalar(
            text("SELECT provider_started_at FROM ai_cost_reservations WHERE id = :id"),
            {"id": str(receipt.id)},
        )
    assert provider_started_at is not None
    mark_paid_call_unknown(receipt)

    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(request)

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ai_cost_reservations SET expires_at = :expired WHERE idempotency_key = :key"
            ),
            {
                "expired": datetime.now(UTC) - timedelta(seconds=1),
                "key": request.idempotency_key,
            },
        )
    with pytest.raises(ProviderOutcomeUnknownError):
        reserve_paid_call(request)


def test_expired_provider_started_estimate_still_counts_against_monthly_cap(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    monkeypatch.setattr(settings, "ai_development_monthly_budget_usd", 0.015)
    first = _request(ledger, "started", estimate=0.01)
    receipt = reserve_paid_call(first)
    assert receipt is not None
    mark_paid_call_started(receipt)
    with sync_engine.begin() as conn:
        conn.execute(
            text("UPDATE ai_cost_reservations SET expires_at = :expired WHERE id = :id"),
            {
                "expired": datetime.now(UTC) - timedelta(days=1),
                "id": str(receipt.id),
            },
        )

    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(_request(ledger, "second", estimate=0.01))

    assert caught.value.scope == "environment:development"


def test_eighty_percent_breaker_stops_experiments(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "lab")
    monkeypatch.setattr(settings, "ai_omni_lab_monthly_budget_usd", 1.0)
    first = reserve_paid_call(_request(ledger, "lab-seed", estimate=0.79, purpose="omni_lab"))
    assert first is not None
    mark_paid_call_started(first)
    settle_paid_call_cost(first, cost_usd=0.79, resolved_model="gemini-omni-flash-preview")

    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(_request(ledger, "lab-blocked", estimate=0.01, purpose="omni_lab"))
    assert caught.value.scope == "environment:lab"


def test_ninety_percent_breaker_stops_new_director_review(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "ai_production_monthly_budget_usd", 1.0)
    creator_id = _create_creator(ledger, "director")
    seed = _request(
        ledger,
        "prod-seed",
        estimate=0.89,
        purpose="customer",
        creator_id=creator_id,
    )
    seed.ctx.test_run_id = None
    seed.ctx.estimated_max_cost_usd = None
    seed.ctx.reservation_approved = False
    receipt = reserve_paid_call(seed)
    assert receipt is not None
    mark_paid_call_started(receipt)
    settle_paid_call_cost(receipt, cost_usd=0.89, resolved_model=seed.model)

    director = _request(
        ledger,
        "director-blocked",
        estimate=0.01,
        purpose="customer",
        feature="edit_director",
        creator_id=creator_id,
    )
    director.ctx.test_run_id = None
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(director)
    assert caught.value.scope == "environment:production"


def test_director_daily_limit_counts_reserved_reviews_atomically(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "edit_director_daily_paid_limit", 3)
    creator_id = _create_creator(ledger, "director-daily")
    for index in range(3):
        request = _request(
            ledger,
            f"director-daily:{index}",
            estimate=0.001,
            purpose="customer",
            feature="edit_director",
            creator_id=creator_id,
        )
        request.ctx.test_run_id = None
        request.ctx.estimated_max_cost_usd = None
        assert reserve_paid_call(request) is not None

    blocked = _request(
        ledger,
        "director-daily:blocked",
        estimate=0.001,
        purpose="customer",
        feature="edit_director",
        creator_id=creator_id,
    )
    blocked.ctx.test_run_id = None
    blocked.ctx.estimated_max_cost_usd = None
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(blocked)
    assert caught.value.scope == "edit_director:user_day"


def test_release_canary_has_a_separate_monthly_cap(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "ai_release_canary_monthly_budget_usd", 0.015)
    creator_id = _create_creator(ledger, "canary-cap", internal=True)

    first = _request(
        ledger,
        "canary-cap:first",
        estimate=0.01,
        purpose="release_canary",
        creator_id=creator_id,
    )
    first.ctx.test_run_id = None
    first.ctx.estimated_max_cost_usd = None
    first.ctx.release_canary_id = "release-one"
    assert reserve_paid_call(first) is not None

    second = _request(
        ledger,
        "canary-cap:second",
        estimate=0.01,
        purpose="release_canary",
        creator_id=creator_id,
    )
    second.ctx.test_run_id = None
    second.ctx.estimated_max_cost_usd = None
    second.ctx.release_canary_id = "release-one"
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(second)
    assert caught.value.scope == "purpose:release_canary"


def test_expiring_override_extends_the_hard_environment_cap(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "ai_production_monthly_budget_usd", 0.01)
    request = _request(ledger, "override", estimate=0.015, purpose="optional_background")
    request.ctx.test_run_id = None

    with pytest.raises(AiBudgetExceededError):
        reserve_paid_call(request)

    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ai_budget_overrides "
                "(id, scope, additional_cost_usd, reason, approved_by, expires_at) "
                "VALUES (:id, 'environment:production', 0.01, :reason, 'pytest', :expires_at)"
            ),
            {
                "id": str(uuid.uuid4()),
                "reason": ledger,
                "expires_at": datetime.now(UTC) + timedelta(minutes=5),
            },
        )

    assert reserve_paid_call(request) is not None


def test_internal_production_user_requires_a_tagged_release_canary(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    creator_id = _create_creator(ledger, "internal", internal=True)
    request = _request(
        ledger,
        "internal-untagged",
        estimate=0.01,
        purpose="customer",
        creator_id=creator_id,
    )
    request.ctx.test_run_id = None

    with pytest.raises(AiCostControlPolicyError) as caught:
        reserve_paid_call(request)
    assert caught.value.reason == "release_canary_attribution_required"

    request.ctx.usage_purpose = "release_canary"
    request.ctx.release_canary_id = "release-2026-09-08"
    assert reserve_paid_call(request) is not None


def test_weekly_smoke_call_cannot_exceed_twenty_cents(
    ledger: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "ai_usage_environment", "development")
    with pytest.raises(AiBudgetExceededError) as caught:
        reserve_paid_call(
            _request(ledger, "oversize-smoke", estimate=0.200001, purpose="provider_smoke")
        )
    assert caught.value.scope == "provider_smoke:run"
