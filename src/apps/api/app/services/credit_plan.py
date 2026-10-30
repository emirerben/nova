"""Offline allocation checks. Planning ceilings never authorize provider calls."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class CreditAllocation:
    openai_evaluation: float = 40
    openai_image: float = 40
    openai_narration: float = 20
    openai_reserve: float = 30
    openai_total: float = 130
    anthropic_evaluation: float = 60
    anthropic_reserve: float = 140
    anthropic_total: float = 200

    def __post_init__(self):
        values = [Decimal(str(getattr(self, name))) for name in self.__dataclass_fields__]
        if any(not value.is_finite() or value < 0 for value in values):
            raise ValueError("allocations must be finite nonnegative USD amounts")
        for provider, capabilities in (
            ("openai", ("evaluation", "image", "narration", "reserve")),
            ("anthropic", ("evaluation", "reserve")),
        ):
            allocated = sum(Decimal(str(getattr(self, f"{provider}_{c}"))) for c in capabilities)
            if allocated > Decimal(str(getattr(self, f"{provider}_total"))):
                raise ValueError("allocations exceed provider planning total")

    @property
    def authorizes_live_calls(self) -> bool:
        return False

    def ceiling(self, provider: str, capability: str) -> float:
        allowed = {
            ("openai", "evaluation"),
            ("openai", "image"),
            ("openai", "narration"),
            ("anthropic", "evaluation"),
        }
        if (provider, capability) not in allowed:
            raise ValueError("unsupported provider capability")
        return getattr(self, f"{provider}_{capability}")


def comparison_credit_preflight(packet, *, allocation=None, prepared=None):
    if prepared is None:
        from app.services.creator_model_comparison import prepare_packet

        prepared = prepare_packet(packet)
    allocation = allocation or CreditAllocation()
    providers = {
        "gpt-6.1-sol": "openai",
        "claude-sonnet-5-5": "anthropic",
        "gemini-3.1-pro-preview": "google",
    }
    repetitions = packet["repetitions"]
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("positive integer repetitions required")
    totals = {}
    arms = {arm["id"]: arm["model"] for arm in packet["arms"]}
    for row in prepared["rows"]:
        provider = providers.get(arms.get(row["arm"]))
        if provider is None:
            raise ValueError("unpriced comparison provider")
        cost = Decimal(str(row["estimated_max_usd"]))
        if not cost.is_finite() or cost < 0:
            raise ValueError("invalid estimated cost")
        totals[provider] = totals.get(provider, Decimal(0)) + cost * repetitions
    for provider in ("openai", "anthropic"):
        if totals.get(provider, 0) > Decimal(str(allocation.ceiling(provider, "evaluation"))):
            raise ValueError("comparison exceeds provider evaluation allocation")
    unfunded = sorted(provider for provider in totals if provider not in ("openai", "anthropic"))
    return {
        "mode": "offline_credit_preflight",
        "packet_sha256": prepared.get("packet_sha256"),
        "provider_estimated_max_usd": {p: float(v) for p, v in sorted(totals.items())},
        "estimated_max_usd": prepared["estimated_max_usd"],
        "allocation_status": "blocked" if unfunded else "within_planning_ceiling",
        "unfunded_providers": unfunded,
        "live_authorized": False,
        "quality_verdict": "unassessed",
        "actual_export": "unassessed",
        "required_before_live": [
            "verified billing organization and available credits",
            "exact model and current price verification",
            "explicit manifest-bound bounded spend authorization",
            "existing enforced ledger and source eligibility checks",
            "separate funding for any Google baseline calls",
        ],
    }
