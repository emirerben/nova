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


def _timeline(words: list[str], assets, hints, step: float = 0.42):
    from app.smart_edit.planner import _normalize_captions

    normalized, baseline, _ = _normalize_captions(_cues(words, step), language="en")
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


def test_empty_agent_output_is_byte_identical_to_no_hints() -> None:
    # Variance envelope: a run where the model matches NOTHING must produce
    # exactly the timeline the deterministic path produces on its own.
    words, assets = _english_scenario()
    empty = _SceneHints(matches=[], chapter_tags={}, role_tags={})

    with_empty = _timeline(words, assets, empty)
    without = _timeline(words, assets, None)

    assert with_empty.detected_chapters == without.detected_chapters
    assert [
        (c.candidate_id, c.role, c.suggested_asset_ids, c.suggested_asset_anchor_word_ids)
        for c in with_empty.candidates
    ] == [
        (c.candidate_id, c.role, c.suggested_asset_ids, c.suggested_asset_anchor_word_ids)
        for c in without.candidates
    ]


# ── badge hijack + chapter-cue absorption (2026-07-21 Messi/Anderson report) ─


def _player_scenario() -> tuple[list[str], _SceneHints]:
    words = (
        "today I present rankings for football now number one Messi Anderson defends "
        "greatly then more end"
    ).split()
    index = {tok: i for i, tok in enumerate(words)}
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    hints = _SceneHints(
        matches=[
            (index["Messi"], "a-messi", wid["Messi"]),
            (index["Anderson"], "a-anderson", wid["Anderson"]),
        ],
        chapter_tags={index["one"]: 1},
        role_tags={},
    )
    return words, hints


def test_hinted_asset_never_becomes_persistent_badge() -> None:
    words, hints = _player_scenario()
    assets = [
        # Gemini describes the jersey crest as a "badge" — the term must not
        # turn a word-anchored player photo into corner furniture.
        SmartPlannerAsset(
            asset_id="a-messi",
            subject="Lionel Messi in Argentina jersey",
            description="jersey with club badge and logo",
            filename="messi.jpg",
        ),
        SmartPlannerAsset(asset_id="a-skipper", subject="skip ad badge", filename="skip-ad.png"),
    ]

    semantic = _timeline(words, assets, hints)

    badge = [c for c in semantic.candidates if c.evidence == "persistent badge asset"]
    assert len(badge) == 1 and badge[0].suggested_asset_ids == ["a-skipper"]
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    chapter = next(c for c in semantic.candidates if c.sequence_number == 1)
    assert chapter.suggested_asset_anchor_word_ids.get("a-messi") == wid["Messi"]


def test_chapter_absorbs_hint_match_from_overlapping_cue() -> None:
    words, hints = _player_scenario()
    assets = [
        SmartPlannerAsset(asset_id="a-messi", subject="Messi", filename="messi.jpg"),
        SmartPlannerAsset(asset_id="a-anderson", subject="Anderson", filename="anderson.jpg"),
    ]

    semantic = _timeline(words, assets, hints)

    # "Anderson" sits past the chapter title but inside the same (skipped)
    # cue — it must land on the chapter candidate, not vanish.
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    chapter = next(c for c in semantic.candidates if c.sequence_number == 1)
    assert chapter.suggested_asset_anchor_word_ids.get("a-messi") == wid["Messi"]
    assert chapter.suggested_asset_anchor_word_ids.get("a-anderson") == wid["Anderson"]


def test_hook_window_matches_are_not_duplicated_into_straddling_cues() -> None:
    # A cue that straddles the hook boundary must not re-carry a hook-group
    # match as its own visual — that rendered the same flag twice at once.
    words = (
        "welcome everyone Spain Argentina flags are cool right now we discuss "
        "deeper things about clubs today"
    ).split()
    index = {tok: i for i, tok in enumerate(words)}
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    assets = [_asset("a-spain", "flag of Spain"), _asset("a-arg", "flag of Argentina")]
    hints = _SceneHints(
        matches=[
            (index["Spain"], "a-spain", wid["Spain"]),
            (index["Argentina"], "a-arg", wid["Argentina"]),
        ],
        chapter_tags={},
        role_tags={},
    )

    semantic = _timeline(words, assets, hints, step=1.0)

    hook = next(c for c in semantic.candidates if c.role == "hook")
    hook_end_index = int(hook.end_word_id[1:]) - 1
    assert hook_end_index >= index["Argentina"], "scenario must keep flags in the hook"
    assert {"a-spain", "a-arg"} <= set(hook.suggested_asset_ids)
    for candidate in semantic.candidates:
        if candidate.role == "hook":
            continue
        for asset_id, anchor in candidate.suggested_asset_anchor_word_ids.items():
            assert int(anchor[1:]) - 1 > hook_end_index, (
                f"hook-window match {asset_id}@{anchor} duplicated on {candidate.candidate_id}"
            )


# ── compile walls: hint anchors + truncation (run-3 chapter-loss report) ─────


def _compile(proposals, words, hints):
    from app.smart_edit.planner import _events_from_proposals_v2, _normalize_captions

    normalized, _, _ = _normalize_captions(_cues(words), language="en")
    preset = load_preset("cigdem", "v2")
    return _events_from_proposals_v2(
        proposals,
        words=normalized,
        preset=preset,
        hook_end_word_id=normalized[0].word_id,
        scene_hints=hints,
    )


def test_out_of_span_hint_anchor_compiles_instead_of_killing_the_event() -> None:
    from app.agents.smart_edit_planner import SmartPlannerProposal

    words, hints = _player_scenario()
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    proposal = SmartPlannerProposal(
        role="list_item",
        start_word_id=wid["number"],
        end_word_id=wid["Messi"],
        anchor_word_id=wid["number"],
        confidence_tier="high",
        sequence_number=1,
        text_token="section_heading",
        scene_token="single_example",
        visual_asset_ids=["a-anderson"],
        # Absorbed from the overlapping cue — spoken AFTER the heading span.
        visual_anchor_word_ids={"a-anderson": wid["Anderson"]},
        rationale="test",
    )

    events, omissions = _compile([proposal], words, hints)

    assert not omissions
    visuals = [lane for ev in events for lane in ev.lanes if lane.kind == "visual"]
    assert [lane.asset_id for lane in visuals] == ["a-anderson"]

    # Without the hint the same anchor is the hallucination class the wall
    # exists for — the visual is dropped (and with it the sole event).
    events, omissions = _compile([proposal], words, None)
    assert [o["reason"] for o in omissions] == ["unknown_visual_anchor"]
    assert not events


def test_event_cap_truncates_chronologically_not_by_list_order() -> None:
    from app.agents.smart_edit_planner import SmartPlannerProposal

    words, hints = _player_scenario()
    wid = {tok: f"w{i + 1:06d}" for i, tok in enumerate(words)}
    preset = load_preset("cigdem", "v2")
    filler = [
        SmartPlannerProposal(
            role="example",
            start_word_id=wid["greatly"],
            end_word_id=wid["greatly"],
            anchor_word_id=wid["greatly"],
            rationale=f"filler-{i}",
        )
        for i in range(preset.density.max_events + 1)
    ]
    chapter = SmartPlannerProposal(
        role="list_item",
        start_word_id=wid["number"],
        end_word_id=wid["Messi"],
        anchor_word_id=wid["number"],
        sequence_number=1,
        text_token="section_heading",
        rationale="backfilled chapter",
    )

    # Merge appends backfilled fallbacks at the END — the cap must still keep
    # this early-video chapter and shed the latest events instead.
    events, _ = _compile([*filler, chapter], words, hints)

    roles = [ev.role for ev in events]
    assert "list_item" in roles


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


# ── emphasis spans: fail-soft parsing (plan 011, Feature A) ───────────────────


def test_emphasis_span_fail_soft_per_item() -> None:
    inp = _input(["Bir", "numara", "Messi", "iki", "Ronaldo", "harika"], [])
    raw = json.dumps(
        {
            "matches": [],
            "cue_tags": [{"anchor_word_id": "w000002", "role": "list_item", "sequence_number": 1}],
            "emphasis_spans": [
                {"word_ids": ["w000003"], "kind": "standalone"},  # Messi — valid
                {"word_ids": ["w000001", "w000002"], "kind": "keep_together"},  # valid pair
                {"word_ids": ["w000003", "w000004"], "kind": "keep_together"},  # overlaps -> drop
                {
                    "word_ids": ["w000005", "w000004"],
                    "kind": "keep_together",
                },  # non-contiguous -> drop
                {"word_ids": ["w999999"], "kind": "standalone"},  # unknown word -> drop
                {"word_ids": ["w000006"], "kind": "loud"},  # bad kind -> drop
                {"word_ids": [], "kind": "standalone"},  # empty -> drop
                "not-a-dict",  # junk -> drop
            ],
        }
    )
    out = _agent().parse(raw, inp)
    # matches + cue_tags are untouched by malformed emphasis items.
    assert out.matches == []
    assert len(out.cue_tags) == 1 and out.cue_tags[0].role == "list_item"
    kept = [(s.word_ids, s.kind) for s in out.emphasis_spans]
    assert kept == [(["w000003"], "standalone"), (["w000001", "w000002"], "keep_together")]


def test_emphasis_malformed_field_never_touches_other_output() -> None:
    inp = _input(["a", "b", "c"], [])
    raw = json.dumps(
        {
            "matches": [],
            "cue_tags": [{"anchor_word_id": "w000002", "role": "payoff"}],
            "emphasis_spans": "garbage",
        }
    )
    out = _agent().parse(raw, inp)
    assert len(out.cue_tags) == 1
    assert out.emphasis_spans == []


# ── emphasis prompt gating (byte-identical off, present on) ────────────────────


def test_render_prompt_gates_emphasis_block_on_flag(monkeypatch) -> None:
    inp = _input(["Bir", "numara", "Messi"], [])
    agent = _agent()

    monkeypatch.setattr(settings, "smart_caption_emphasis_cues_enabled", False, raising=False)
    off = agent.render_prompt(inp)
    assert "emphasis_spans" not in off
    assert "EMPHASIS SPANS" not in off

    monkeypatch.setattr(settings, "smart_caption_emphasis_cues_enabled", True, raising=False)
    on = agent.render_prompt(inp)
    assert '"emphasis_spans"' in on
    assert "## EMPHASIS SPANS" in on
    assert on.index("## EMPHASIS SPANS") < on.index("## Inputs")


# ── emphasis span budgeting + spacing (plan 011, Feature A) ────────────────────


def _emphasis_words(count: int, *, step_ms: int) -> list:
    from app.smart_edit.schemas import SmartWord

    words = []
    for i in range(count):
        word_id = f"w{i + 1:06d}"
        words.append(
            SmartWord(
                word_id=word_id,
                spoken_text="w",
                display_text="w",
                normalized_text="w",
                start_ms=i * step_ms,
                end_ms=i * step_ms + 100,
                timing_quality="aligned",
                display_alignment=[word_id],
            )
        )
    return words


def test_validate_emphasis_spans_disabled_is_noop() -> None:
    from app.agents.scene_matcher import EmphasisSpan
    from app.smart_edit.planner import _validate_emphasis_spans

    words = _emphasis_words(3, step_ms=1000)
    index_by_id = {w.word_id: i for i, w in enumerate(words)}
    spans = [EmphasisSpan(word_ids=["w000001"], kind="standalone")]
    kept, dropped = _validate_emphasis_spans(spans, words, index_by_id, enabled=False)
    assert kept == [] and dropped == 0


def test_validate_emphasis_spans_spaces_standalone_starts() -> None:
    from app.agents.scene_matcher import EmphasisSpan
    from app.smart_edit.planner import _validate_emphasis_spans

    words = _emphasis_words(6, step_ms=1000)  # starts at 0,1000,2000,...
    index_by_id = {w.word_id: i for i, w in enumerate(words)}
    # standalone at 0ms, 1000ms (too close), 2000ms (>=1500 from the kept 0ms).
    spans = [
        EmphasisSpan(word_ids=["w000001"], kind="standalone"),
        EmphasisSpan(word_ids=["w000002"], kind="standalone"),
        EmphasisSpan(word_ids=["w000003"], kind="standalone"),
    ]
    kept, dropped = _validate_emphasis_spans(spans, words, index_by_id, enabled=True)
    assert [k[1][0] for k in kept] == ["w000001", "w000003"]
    assert dropped == 1


def test_validate_emphasis_spans_keeps_close_list_cadence() -> None:
    # A real spoken list delivers items ~2s apart — none must be dropped.
    from app.agents.scene_matcher import EmphasisSpan
    from app.smart_edit.planner import _validate_emphasis_spans

    words = _emphasis_words(6, step_ms=2000)
    index_by_id = {w.word_id: i for i, w in enumerate(words)}
    spans = [EmphasisSpan(word_ids=[f"w{i:06d}"], kind="standalone") for i in (1, 3, 5)]
    kept, dropped = _validate_emphasis_spans(spans, words, index_by_id, enabled=True)
    assert len(kept) == 3 and dropped == 0


def test_validate_emphasis_spans_budget_caps_total() -> None:
    from app.agents.scene_matcher import EmphasisSpan
    from app.smart_edit.planner import _EMPHASIS_MAX_SPANS, _validate_emphasis_spans

    words = _emphasis_words(30, step_ms=100)
    index_by_id = {w.word_id: i for i, w in enumerate(words)}
    # keep_together spans (no gap rule) so only the budget bounds them.
    spans = [EmphasisSpan(word_ids=[f"w{i:06d}"], kind="keep_together") for i in range(1, 20)]
    kept, dropped = _validate_emphasis_spans(spans, words, index_by_id, enabled=True)
    assert len(kept) == _EMPHASIS_MAX_SPANS
    assert dropped == 19 - _EMPHASIS_MAX_SPANS


def test_validate_emphasis_spans_gap_boundary_is_inclusive() -> None:
    # Strict < 1500ms drop: two standalones exactly 1500ms apart are BOTH kept.
    from app.agents.scene_matcher import EmphasisSpan
    from app.smart_edit.planner import _validate_emphasis_spans

    words = _emphasis_words(4, step_ms=1500)  # starts 0, 1500, 3000, 4500
    index_by_id = {w.word_id: i for i, w in enumerate(words)}
    spans = [
        EmphasisSpan(word_ids=["w000001"], kind="standalone"),  # 0ms
        EmphasisSpan(word_ids=["w000002"], kind="standalone"),  # 1500ms exactly
    ]
    kept, dropped = _validate_emphasis_spans(spans, words, index_by_id, enabled=True)
    assert len(kept) == 2 and dropped == 0


def test_validate_emphasis_spans_exactly_at_budget_keeps_all() -> None:
    from app.agents.scene_matcher import EmphasisSpan
    from app.smart_edit.planner import _EMPHASIS_MAX_SPANS, _validate_emphasis_spans

    words = _emphasis_words(_EMPHASIS_MAX_SPANS + 2, step_ms=100)
    index_by_id = {w.word_id: i for i, w in enumerate(words)}
    spans = [
        EmphasisSpan(word_ids=[f"w{i:06d}"], kind="keep_together")
        for i in range(1, _EMPHASIS_MAX_SPANS + 1)
    ]
    kept, dropped = _validate_emphasis_spans(spans, words, index_by_id, enabled=True)
    assert len(kept) == _EMPHASIS_MAX_SPANS and dropped == 0
