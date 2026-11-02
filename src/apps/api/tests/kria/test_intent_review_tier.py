"""Failure matrix row 41: a flag flip can never switch on a stage that has not shipped.

The tier is the only thing the rest of the code reads. A child flag set without its parent
does nothing, an empty allowlist admits nobody, and the per-PR ceiling caps everything.
"""

from types import SimpleNamespace

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


def test_the_ceiling_in_this_stack_position_is_evidence_only():
    assert tier._CODE_TIER_CEILING == "evidence"


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


def test_only_a_reviewing_tier_on_a_runtime_v2_thread_retires_the_legacy_review(flags, monkeypatch):
    flags()
    v2, v1 = SimpleNamespace(runtime_version=2), SimpleNamespace(runtime_version=1)
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "evidence")
    assert tier.intent_review_replaces_legacy(USER, v2) is False
    monkeypatch.setattr(tier, "_CODE_TIER_CEILING", "review")
    assert tier.intent_review_replaces_legacy(USER, v2) is True
    assert tier.intent_review_replaces_legacy(USER, v1) is False
    assert tier.intent_review_replaces_legacy("someone-else", v2) is False


def test_every_flag_is_documented_where_operators_look():
    from pathlib import Path

    root = Path(__file__).resolve().parents[5]
    env = (root / ".env.example").read_text(encoding="utf-8")
    claude = (root / "CLAUDE.md").read_text(encoding="utf-8")
    runbook = (root / "docs/runbooks/kria-intent-review.md").read_text(encoding="utf-8")
    for name in (
        "KRIA_INTENT_REVIEW_ENABLED",
        "KRIA_INTENT_REVIEW_PAID_ENABLED",
        "KRIA_INTENT_REVIEW_REPAIR_ENABLED",
        "KRIA_INTENT_REVIEW_USER_IDS",
        "KRIA_INTENT_REVIEW_MAX_USD",
        "KRIA_INTENT_REVIEW_CANARY_ID",
        "AI_OPENAI_INTENT_REVIEW_BUDGET_USD",
    ):
        assert f"{name}=" in env, name
        assert name in runbook, name
    assert (
        "KRIA_INTENT_REVIEW_ENABLED" in claude and "docs/runbooks/kria-intent-review.md" in claude
    )
