"""Word→visual scene matcher: validation walls + planner integration.

The 2026-07-21 report: 4 flags + 4 players in the pool, English speech naming
each one — the token-overlap heuristic cross-matched them ("silly matching")
and the Turkish-only vocab produced zero chapters. These tests pin the brain's
grounding contract and the planner's hint precedence + fallback semantics.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agents.scene_matcher import SceneMatcherAgent, SceneMatcherInput
from app.agents.smart_edit_planner import SmartPlannerAsset
from app.config import settings
from app.smart_edit import planner as planner_mod
from app.smart_edit.planner import (
    _merge_hint_chapters,
    _run_scene_matcher,
    _SceneHints,
    _semantic_timeline_v2,
)
from app.smart_edit.presets import load_preset


def _agent() -> SceneMatcherAgent:
    return SceneMatcherAgent(MagicMock())


def _input(words: list[str], assets: list[SmartPlannerAsset]) -> SceneMatcherInput:
    return SceneMatcherInput(
        words=[{"word_id": f"w{i + 1:06d}", "text": tok} for i, tok in enumerate(words)],
        assets=assets,
        language="en",
    )


def _asset(asset_id: str, subject: str = "", brands: list[str] | None = None) -> SmartPlannerAsset:
    return SmartPlannerAsset(
        asset_id=asset_id, subject=subject, brands=brands or [], filename=f"{asset_id}.jpg"
    )


# ── parse() validation walls ─────────────────────────────────────────────────


def test_parse_drops_unknown_assets_words_and_low_confidence() -> None:
    inp = _input(["Spain", "wins"], [_asset("a-spain", "flag of Spain")])
    raw = json.dumps(
        {
            "matches": [
                {"asset_id": "a-spain", "anchor_word_id": "w000001", "confidence": "high"},
                {"asset_id": "a-ghost", "anchor_word_id": "w000001", "confidence": "high"},
                {"asset_id": "a-spain", "anchor_word_id": "w999999", "confidence": "high"},
                {"asset_id": "a-spain", "anchor_word_id": "w000002", "confidence": "low"},
                {"asset_id": "a-spain", "anchor_word_id": "not-a-word", "confidence": "high"},
            ],
            "cue_tags": [
                {"anchor_word_id": "w000002", "role": "cta"},
                {"anchor_word_id": "w000002", "role": "hook"},
                {"anchor_word_id": "w999999", "role": "payoff"},
            ],
        }
    )

    out = _agent().parse(raw, inp)

    assert [(m.asset_id, m.anchor_word_id) for m in out.matches] == [("a-spain", "w000001")]
    assert [(t.anchor_word_id, t.role) for t in out.cue_tags] == [("w000002", "cta")]


def test_parse_caps_matches_per_asset_and_rejects_duplicate_sequences() -> None:
    words = ["one", "two", "three", "four", "five"]
    inp = _input(words, [_asset("a-1")])
    raw = json.dumps(
        {
            "matches": [
                {"asset_id": "a-1", "anchor_word_id": f"w{i + 1:06d}", "confidence": "high"}
                for i in range(4)
            ],
            "cue_tags": [
                {"anchor_word_id": "w000002", "role": "list_item", "sequence_number": 1},
                {"anchor_word_id": "w000003", "role": "list_item", "sequence_number": 1},
                {"anchor_word_id": "w000004", "role": "list_item", "sequence_number": 99},
            ],
        }
    )

    out = _agent().parse(raw, inp)

    assert len(out.matches) == 2  # per-asset cap
    assert [(t.anchor_word_id, t.sequence_number) for t in out.cue_tags] == [("w000002", 1)]


# ── planner integration: the user's English scenario ─────────────────────────


def _english_scenario() -> tuple[list[str], list[SmartPlannerAsset]]:
    words = (
        "today I rank football nations number one Spain is elite number two "
        "Argentina the home of Messi then England and finally France follow for more"
    ).split()
    assets = [
        _asset("a-spain", "flag of Spain", ["Spain"]),
        _asset("a-arg", "flag of Argentina", ["Argentina"]),
        _asset("a-messi", "footballer in Argentina number 10 jersey"),
    ]
    return words, assets


def _cues(words: list[str], step: float = 0.42) -> list[dict]:
    # BaselineCaptionCue caps a cue at 24 word_ids — chunk like real ASR output.
    chunk = 8
    cues = []
    for start in range(0, len(words), chunk):
        group = words[start : start + chunk]
        cues.append(
            {
                "text": " ".join(group),
                "start_s": start * step,
                "end_s": (start + len(group)) * step,
                "words": [
                    {"text": tok, "start_s": (start + j) * step, "end_s": (start + j + 1) * step}
                    for j, tok in enumerate(group)
                ],
            }
        )
    return cues


def _hints_for(words: list[str]) -> _SceneHints:
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    index = {tok: i for i, tok in enumerate(words)}
    return _SceneHints(
        matches=[
            (index["Spain"], "a-spain", wid["Spain"]),
            (index["Argentina"], "a-arg", wid["Argentina"]),
            (index["Messi"], "a-messi", wid["Messi"]),
        ],
        chapter_tags={index["one"]: 1, index["two"]: 2},
        role_tags={index["follow"]: "cta"},
    )


def _timeline(words: list[str], assets, hints):
    from app.smart_edit.planner import _normalize_captions

    normalized, baseline, _ = _normalize_captions(_cues(words), language="en")
    preset = load_preset("cigdem", "v2")
    return _semantic_timeline_v2(normalized, baseline, assets, preset, scene_hints=hints)


def test_english_hints_produce_chapters_and_word_anchored_matches() -> None:
    words, assets = _english_scenario()

    semantic = _timeline(words, assets, _hints_for(words))

    # Chapters detected from agent tags — no Turkish vocab involved.
    assert semantic.detected_chapters == [1, 2]
    # Every hinted asset lands on a candidate anchored at ITS spoken word.
    anchors: dict[str, str] = {}
    for candidate in semantic.candidates:
        for asset_id, anchor in candidate.suggested_asset_anchor_word_ids.items():
            anchors.setdefault(asset_id, anchor)
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    assert anchors.get("a-spain") == wid["Spain"]
    assert anchors.get("a-arg") == wid["Argentina"]
    assert anchors.get("a-messi") == wid["Messi"]


def test_without_hints_english_scenario_stays_prior_behavior() -> None:
    words, assets = _english_scenario()

    semantic = _timeline(words, assets, None)

    # Byte-identity guard: no hints → the Turkish-only vocab detects nothing.
    assert semantic.detected_chapters == []


def test_hint_chapters_never_override_vocab_detected_markers() -> None:
    words, assets = _english_scenario()
    hints = _hints_for(words)

    from app.smart_edit.planner import _normalize_captions

    normalized, _, _ = _normalize_captions(_cues(words), language="en")
    detected = [(1, 3, 4)]  # pretend the vocab already found chapter 1 elsewhere

    merged = _merge_hint_chapters(normalized, detected, hints)

    numbers = [number for number, _, _ in merged]
    assert numbers.count(1) == 1  # deduped by sequence number, vocab marker kept
    assert (1, 3, 4) in merged
    assert 2 in numbers  # the agent-only chapter is added


# ── _run_scene_matcher gating ────────────────────────────────────────────────


def test_run_scene_matcher_disabled_paths(monkeypatch) -> None:
    words, assets = _english_scenario()
    from app.smart_edit.planner import _normalize_captions

    normalized, _, _ = _normalize_captions(_cues(words), language="en")

    hints, receipt = _run_scene_matcher(
        normalized, assets, language="en", job_id=None, use_agent=False
    )
    assert hints is None and receipt["status"] == "disabled_use_agent"

    monkeypatch.setattr(settings, "smart_scene_matcher_enabled", False, raising=False)
    hints, receipt = _run_scene_matcher(
        normalized, assets, language="en", job_id=None, use_agent=True
    )
    assert hints is None and receipt["status"] == "disabled_by_flag"

    monkeypatch.setattr(settings, "smart_scene_matcher_enabled", True, raising=False)
    monkeypatch.setattr(settings, "gemini_api_key", "", raising=False)
    hints, receipt = _run_scene_matcher(
        normalized, assets, language="en", job_id=None, use_agent=True
    )
    assert hints is None and receipt["status"] == "no_api_key"


def test_run_scene_matcher_agent_failure_fails_open(monkeypatch) -> None:
    words, assets = _english_scenario()
    from app.smart_edit.planner import _normalize_captions

    normalized, _, _ = _normalize_captions(_cues(words), language="en")
    monkeypatch.setattr(settings, "gemini_api_key", "test-key", raising=False)

    class _Boom:
        def __init__(self, *_a, **_k):
            raise RuntimeError("model down")

    monkeypatch.setattr("app.agents.scene_matcher.SceneMatcherAgent", _Boom)

    hints, receipt = _run_scene_matcher(
        normalized, assets, language="en", job_id=None, use_agent=True
    )

    assert hints is None
    assert receipt == {"status": "failed_open", "error_class": "RuntimeError"}


def test_planner_module_exports_scene_hint_surface() -> None:
    # The render path and future callers rely on these names existing.
    assert hasattr(planner_mod, "_run_scene_matcher")
    assert hasattr(planner_mod, "_SceneHints")
    with pytest.raises(TypeError):
        _SceneHints()  # frozen dataclass with required fields
