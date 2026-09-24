"""KRI-190: the flag, its allowlist, and the review reply built from a unified plan's receipts."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.config import Settings, settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.tasks import kria_runtime

DEFAULT = "The guided story cut is ready."


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=2,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="landmarks"),
            BriefRequirement(id="r2", kind="order", scope="global", facts={"key": "capture_time"}),
        ],
    )


def _job(receipts, **extra):
    record = {"requirement_receipts": receipts, **extra}
    return SimpleNamespace(assembly_plan={"unified_montage": record})


def _thread():
    return SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())


@pytest.fixture
def brief_on(monkeypatch):
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(kria_runtime, "load_latest_brief_sync", lambda _db, _thread_id: _brief())


def test_flag_and_allowlist(monkeypatch):
    user = uuid.uuid4()
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", False)
    monkeypatch.setattr(settings, "montage_unified_plan_user_ids", [])
    assert settings.montage_unified_plan_for(user) is False
    monkeypatch.setattr(settings, "montage_unified_plan_user_ids", [str(user)])
    assert settings.montage_unified_plan_for(user) is True
    assert settings.montage_unified_plan_for(uuid.uuid4()) is False
    monkeypatch.setattr(settings, "montage_unified_plan_enabled", True)
    assert settings.montage_unified_plan_for(uuid.uuid4()) is True
    assert Settings.model_fields["montage_unified_plan_enabled"].default is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("abc-123", ["abc-123"]), ("a, b", ["a", "b"]), ('["a","b"]', ["a", "b"]), ("", [])],
)
def test_allowlist_env_loads_single_csv_and_json(monkeypatch, raw, expected):
    monkeypatch.setenv("MONTAGE_UNIFIED_PLAN_USER_IDS", raw)
    assert Settings(_env_file=None).montage_unified_plan_user_ids == expected


def test_reply_lists_what_was_partial_and_what_was_guessed(brief_on):
    receipts = [
        {
            "requirement_id": "r1",
            "status": "partial",
            "reason": "Text landed on 10 of 14 clips.",
            "inferred": ["Dolmabahçe"],
        },
        {"requirement_id": "r2", "status": "met", "reason": None, "inferred": []},
    ]
    text, payload = kria_runtime._unified_montage_review(None, _thread(), _job(receipts), DEFAULT)
    assert text.startswith("Not everything you asked for made it in:")
    assert "Partly:" in text and "10 of 14 clips" in text
    assert "Done:" in text
    assert "I guessed these, tell me if any is wrong: Dolmabahçe" in text
    assert [row["requirement_id"] for row in payload] == ["r1", "r2"]


def test_reply_keeps_the_default_summary_when_everything_was_met(brief_on):
    receipts = [
        {"requirement_id": "r1", "status": "met", "reason": None, "inferred": []},
        {"requirement_id": "r2", "status": "met", "reason": None, "inferred": []},
    ]
    text, _payload = kria_runtime._unified_montage_review(None, _thread(), _job(receipts), DEFAULT)
    assert text.startswith(DEFAULT)
    assert "Done:" in text


def test_default_review_is_unchanged_without_the_brief_or_a_unified_record(monkeypatch):
    receipts = [{"requirement_id": "r1", "status": "partial", "reason": "x", "inferred": []}]
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", False)
    monkeypatch.setattr(settings, "kria_creative_brief_user_ids", [])
    assert kria_runtime._unified_montage_review(None, _thread(), _job(receipts), DEFAULT) == (
        DEFAULT,
        [],
    )
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    plain = SimpleNamespace(assembly_plan={})
    assert kria_runtime._unified_montage_review(None, _thread(), plain, DEFAULT) == (DEFAULT, [])
    empty = _job([])
    assert kria_runtime._unified_montage_review(None, _thread(), empty, DEFAULT) == (DEFAULT, [])


def test_receipts_for_superseded_requirements_are_ignored(brief_on):
    receipts = [{"requirement_id": "gone", "status": "partial", "reason": "x", "inferred": []}]
    assert kria_runtime._unified_montage_review(None, _thread(), _job(receipts), DEFAULT) == (
        DEFAULT,
        [],
    )
