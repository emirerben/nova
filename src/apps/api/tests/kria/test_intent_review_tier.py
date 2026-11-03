"""Failure matrix row 41: a flag flip never switches on a stage its parent flag has not enabled.

The tier is the only thing the rest of the code reads. A child flag set without its parent
does nothing, and an empty allowlist admits nobody.
"""

import pytest

from app.config import settings
from app.kria import intent_review_tier as tier

USER = "5b0f4a0e-6d2f-4a52-9f0b-5cbb3c6b2f10"


@pytest.fixture
def flags(monkeypatch):
    def apply(**overrides):
        values = dict(
            kria_intent_review_enabled=True,
            kria_intent_review_paid_enabled=True,
            kria_intent_review_repair_enabled=True,
            kria_intent_review_user_ids=[USER],
            openai_api_key="sk-test",
            ai_openai_intent_review_budget_usd=50.0,
            ai_openai_monthly_budget_usd=500.0,
        )
        values.update(overrides)
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return apply


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({}, "repair"),
        ({"kria_intent_review_repair_enabled": False}, "review"),
        ({"kria_intent_review_paid_enabled": False}, "evidence"),
        (
            {"kria_intent_review_paid_enabled": False, "kria_intent_review_repair_enabled": True},
            "evidence",
        ),
        ({"kria_intent_review_enabled": False}, "off"),
        ({"kria_intent_review_user_ids": []}, "off"),
        ({"kria_intent_review_user_ids": ["someone-else"]}, "off"),
        ({"kria_intent_review_user_ids": ["*"]}, "repair"),
        ({"openai_api_key": ""}, "evidence"),
        ({"ai_openai_intent_review_budget_usd": 0}, "evidence"),
        ({"ai_openai_monthly_budget_usd": 0}, "evidence"),
    ],
)
def test_child_flags_do_nothing_without_their_parents(flags, overrides, expected):
    flags(**overrides)
    assert tier.intent_review_tier(USER) == expected


def test_only_a_reviewing_tier_retires_the_legacy_review_until_the_review_has_no_verdict(
    flags, monkeypatch
):
    flags()
    assert tier.intent_review_replaces_legacy(USER) is True
    monkeypatch.setattr(settings, "kria_intent_review_paid_enabled", False)
    assert tier.intent_review_replaces_legacy(USER) is False
    monkeypatch.setattr(settings, "kria_intent_review_paid_enabled", True)
    assert tier.intent_review_replaces_legacy(USER, ended_without_verdict=True) is False
    assert tier.intent_review_replaces_legacy("someone-else") is False


def test_every_flag_is_documented_where_operators_look():
    from pathlib import Path

    from app.config import Settings

    root = Path(__file__).resolve().parents[5]
    env = (root / ".env.example").read_text(encoding="utf-8")
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    runbook = (root / "docs/runbooks/kria-intent-review.md").read_text(encoding="utf-8")
    rollout = (root / "docs/runbooks/intent-review-rollout.md").read_text(encoding="utf-8")
    names = sorted(
        field.upper()
        for field in Settings.model_fields
        if field.startswith("kria_intent_review_") or field == "ai_openai_intent_review_budget_usd"
    )
    assert "KRIA_INTENT_REVIEW_DAILY_LIMIT_PER_USER" in names
    assert "KRIA_INTENT_REVIEW_EVIDENCE_QUEUE" in names
    for name in names:
        assert f"{name}=" in env, name
        assert name in runbook and name in rollout, name
    assert (
        "KRIA_INTENT_REVIEW_ENABLED" in claude
        and "docs/runbooks/intent-review-rollout.md" in claude
    )
