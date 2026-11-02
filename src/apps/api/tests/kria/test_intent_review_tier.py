"""Failure matrix row 41: a flag flip can never switch on a stage that has not shipped.

The tier is the only thing the rest of the code reads. A child flag set without its parent
does nothing, an empty allowlist admits nobody, and the per-PR ceiling caps everything.
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
        monkeypatch.setattr(tier, "_CODE_TIER_CEILING", overrides.pop("ceiling", "repair"))

    return apply


def test_shipping_ceiling_caps_every_flag_combination(flags, monkeypatch):
    flags()
    for ceiling, expected in (
        ("evidence", "evidence"),
        ("review", "review"),
        ("repair", "repair"),
    ):
        monkeypatch.setattr(tier, "_CODE_TIER_CEILING", ceiling)
        assert tier.intent_review_tier(USER) == expected


def test_shipped_ceiling_reviews_but_never_repairs_even_with_every_flag_on(monkeypatch):
    for name, value in dict(
        kria_intent_review_enabled=True,
        kria_intent_review_paid_enabled=True,
        kria_intent_review_repair_enabled=True,
        kria_intent_review_user_ids=[USER],
        openai_api_key="sk-test",
        ai_openai_intent_review_budget_usd=50.0,
        ai_openai_monthly_budget_usd=500.0,
    ).items():
        monkeypatch.setattr(settings, name, value)
    assert tier._CODE_TIER_CEILING == "review"
    assert tier.intent_review_tier(USER) == "review"


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
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "evidence")
    assert tier.intent_review_replaces_legacy(USER) is False
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "review")
    assert tier.intent_review_replaces_legacy(USER) is True
    assert tier.intent_review_replaces_legacy(USER, ended_without_verdict=True) is False
    assert tier.intent_review_replaces_legacy("someone-else") is False


def test_every_flag_is_documented_where_operators_look():
    from pathlib import Path

    from app.config import Settings

    root = Path(__file__).resolve().parents[5]
    env = (root / ".env.example").read_text(encoding="utf-8")
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    runbook = (root / "docs/runbooks/kria-intent-review.md").read_text(encoding="utf-8")
    names = sorted(
        field.upper()
        for field in Settings.model_fields
        if field.startswith("kria_intent_review_") or field == "ai_openai_intent_review_budget_usd"
    )
    assert "KRIA_INTENT_REVIEW_DAILY_LIMIT_PER_USER" in names
    assert "KRIA_INTENT_REVIEW_EVIDENCE_QUEUE" in names
    for name in names:
        assert f"{name}=" in env, name
        assert name in runbook, name
    assert (
        "KRIA_INTENT_REVIEW_ENABLED" in claude and "docs/runbooks/kria-intent-review.md" in claude
    )
