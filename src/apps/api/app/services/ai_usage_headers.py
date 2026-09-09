"""Parse explicit paid-call attribution from operator/test HTTP requests."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from app.config import settings


@dataclass(frozen=True, slots=True)
class PaidCallHeaders:
    usage_purpose: str | None
    test_run_id: str | None
    estimated_max_cost_usd: float | None
    reservation_approved: bool
    release_canary_id: str | None

    def as_kwargs(self) -> dict[str, object]:
        return {
            "usage_purpose": self.usage_purpose,
            "test_run_id": self.test_run_id,
            "estimated_max_cost_usd": self.estimated_max_cost_usd,
            "reservation_approved": self.reservation_approved,
            "release_canary_id": self.release_canary_id,
        }


def paid_call_headers(
    request: Request,
    *,
    usage_purpose: str | None = None,
) -> PaidCallHeaders:
    raw_purpose = request.headers.get("x-nova-usage-purpose")
    # Production creator routes derive classification server-side. The only
    # header-driven production exception is a release canary; the reservation
    # layer independently proves that its creator is an internal account.
    production_canary = (
        settings.ai_usage_environment == "production"
        and raw_purpose in {"release_canary", "internal_canary"}
        and bool(request.headers.get("x-nova-release-canary-id"))
    )
    purpose = (
        raw_purpose
        if production_canary
        else usage_purpose
        if usage_purpose is not None
        else raw_purpose
        if settings.ai_usage_environment != "production"
        else None
    )
    accept_test_headers = settings.ai_usage_environment != "production"
    raw_estimate = request.headers.get("x-nova-estimated-max-cost-usd")
    estimate: float | None = None
    if raw_estimate and accept_test_headers:
        try:
            estimate = float(raw_estimate)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="x-nova-estimated-max-cost-usd must be a number",
            ) from exc
        if estimate <= 0 or estimate > 2:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="x-nova-estimated-max-cost-usd must be within (0, 2]",
            )
    return PaidCallHeaders(
        usage_purpose=purpose,
        test_run_id=(request.headers.get("x-nova-test-run-id") if accept_test_headers else None),
        estimated_max_cost_usd=estimate,
        reservation_approved=(
            accept_test_headers
            and request.headers.get("x-nova-reservation-approved", "").lower() == "true"
        ),
        release_canary_id=(
            request.headers.get("x-nova-release-canary-id") if production_canary else None
        ),
    )
