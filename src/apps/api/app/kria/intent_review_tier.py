"""A creator's intent-review tier, from the KRIA_INTENT_REVIEW_* flags.

This is the only place that decides a tier: a child flag set without its parent does
nothing, and the allowlist gates every stage. KRIA_INTENT_REVIEW_ENABLED is also read
directly, as the global kill switch, wherever admission, publishing, recovery and the
legacy interlock must stop. The code ceiling stops a flag flip from switching on a stage
that has not shipped: each PR in the stack raises it.
"""

from __future__ import annotations

from typing import Literal

from app.config import settings

Tier = Literal["off", "evidence", "review", "repair"]
_RANK: dict[str, int] = {"off": 0, "evidence": 1, "review": 2, "repair": 3}
_CODE_TIER_CEILING: Tier = "review"


def tier_rank(tier: str) -> int:
    return _RANK[tier]


def lower_tier(first: str, second: str) -> Tier:
    return first if _RANK[first] <= _RANK[second] else second  # type: ignore[return-value]


def _admitted(user_id: object) -> bool:
    allowed = {str(value).strip() for value in settings.kria_intent_review_user_ids}
    return "*" in allowed or (user_id is not None and str(user_id) in allowed)


def intent_review_tier(user_id: object) -> Tier:
    if not (settings.kria_intent_review_enabled and _admitted(user_id)):
        return "off"
    tier: Tier = "evidence"
    if (
        settings.kria_intent_review_paid_enabled
        and settings.openai_api_key
        and settings.ai_openai_intent_review_budget_usd > 0
        and settings.ai_openai_monthly_budget_usd > 0
    ):
        tier = "repair" if settings.kria_intent_review_repair_enabled else "review"
    return lower_tier(tier, _CODE_TIER_CEILING)


def intent_review_replaces_legacy(user_id: object, ended_without_verdict: bool = False) -> bool:
    """Only a tier that reviews the finished video retires the legacy review, and only until
    that review ends without a verdict for the generation."""
    return not ended_without_verdict and _RANK[intent_review_tier(user_id)] >= _RANK["review"]
