"""Provider error classification shared by model clients and legacy shims."""

from __future__ import annotations

import json
import re
from typing import Any

from app.agents._runtime import ProviderQuotaExceededError

_MONTHLY_SPEND_PATTERNS = (
    r"monthly\s+(?:spend|spending)\s+(?:limit|cap)",
    r"(?:spend|spending)\s+(?:limit|cap).*monthly",
)
_BILLING_PATTERNS = (
    r"billing\s+(?:account|limit|plan).*(?:disabled|exceed|denied|blocked|limit)",
    r"(?:disabled|denied|blocked).{0,40}billing",
)
_QUOTA_PATTERNS = (r"exceeded your current quota.*check your plan and billing",)


def _structured_error_text(exc: Any) -> str:
    """Extract provider error fields without depending on one SDK version."""

    values: list[str] = []
    for name in ("message", "status", "reason", "details", "response", "body"):
        value = getattr(exc, name, None)
        if value is not None:
            if isinstance(value, (dict, list, tuple)):
                try:
                    value = json.dumps(value, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    value = str(value)
            values.append(str(value))
    values.extend(str(arg) for arg in getattr(exc, "args", ()) if arg is not None)
    return " ".join(values).lower()


def provider_quota_reason(exc: Any) -> str | None:
    """Return a stable reason only for explicit, provider-side quota denial.

    A bare HTTP 429 is deliberately not enough: capacity/rate-limit responses
    must continue through the normal transient retry path.
    """

    if getattr(exc, "code", None) != 429:
        return None
    text = _structured_error_text(exc)
    for reason, patterns in (
        ("monthly_spend_limit", _MONTHLY_SPEND_PATTERNS),
        ("billing_denied", _BILLING_PATTERNS),
        ("quota_exceeded", _QUOTA_PATTERNS),
    ):
        if any(re.search(pattern, text) for pattern in patterns):
            return reason
    return None


def classify_provider_error(
    exc: Any, *, provider: str = "gemini"
) -> ProviderQuotaExceededError | None:
    """Build the typed terminal error for a known quota denial, if any."""

    reason = provider_quota_reason(exc)
    if reason is None:
        return None
    return ProviderQuotaExceededError(
        provider=provider,
        reason=reason,
        status_code=429,
    )
