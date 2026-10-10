"""KRI-543: a style ask is judged only from a closed-vocabulary `style_intent`.

KRI-524 stays true: with no intent, a changed text field never counts as met.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.kria.brief import BriefRequirement, BriefUpdate, apply_updates, normalize_style_intent
from app.kria.brief_checks import (
    check_requirement,
    is_judged,
    plan_facts_from_editor_payload,
)
from app.kria.reply_language import reply_language_for


def _intent(*pairs: tuple[str, str], target: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"set": [{"field": f, "value": v} for f, v in pairs]}
    if target:
        out["target"] = target
    return {"style_intent": out}


def _req(facts: dict[str, Any] | None = None) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="style", scope="global", description="x", facts=facts or {}
    )


def _row(row_id: str, text: str = "hello", **kw: Any) -> dict[str, Any]:
    return {"id": row_id, "text": text, "start_s": 0.0, "end_s": 2.0, **kw}


def _verdict(facts: dict[str, Any] | None, rows: list[dict[str, Any]]):
    req = _req(facts)
    receipt = check_requirement(req, plan_facts_from_editor_payload({"text_elements": rows}))
    return receipt, is_judged(req, receipt)


FADE = {"animation_phases": {"entrance": "fade"}}


def test_no_intent_never_counts_as_met_even_when_every_row_changed() -> None:
    receipt, judged = _verdict(None, [_row("a", **FADE), _row("b", **FADE)])
    assert receipt.status == "partial" and not judged


def test_every_row_holding_the_value_is_met_and_judged() -> None:
    receipt, judged = _verdict(
        _intent(("entrance", "fade"), target="all_text"), [_row("a", **FADE), _row("b", **FADE)]
    )
    assert receipt.status == "met" and judged


def test_legacy_effect_counts_as_the_same_entrance() -> None:
    receipt, judged = _verdict(
        _intent(("entrance", "fade"), target="all_text"),
        [_row("a", effect="fade-in"), _row("b", **FADE)],
    )
    assert receipt.status == "met" and judged


def test_captions_and_lyrics_are_not_eligible_rows() -> None:
    rows = [
        _row("a", **FADE),
        _row("narration-caption-1", effect="static"),
        _row("c", source_params={"source": "caption_cue"}, effect="static"),
        _row("l", role="lyric_line"),
    ]
    receipt, judged = _verdict(_intent(("entrance", "fade"), target="all_text"), rows)
    assert receipt.status == "met" and judged


def test_explicit_target_contradiction_is_a_judged_miss() -> None:
    receipt, judged = _verdict(
        _intent(("entrance", "fade"), target="all_text"), [_row("a", **FADE), _row("b")]
    )
    assert receipt.status == "partial" and judged
    assert "1 of 2" in receipt.reason


def test_contradiction_without_target_stays_unchecked() -> None:
    receipt, judged = _verdict(_intent(("entrance", "fade")), [_row("a", **FADE), _row("b")])
    assert receipt.status == "partial" and not judged


def test_turkish_turn_gets_a_turkish_miss_reason() -> None:
    with reply_language_for("tr"):
        receipt, judged = _verdict(
            _intent(("entrance", "fade"), target="all_text"), [_row("a", **FADE), _row("b")]
        )
    assert judged and "metinden" in receipt.reason


def test_unknown_font_or_color_is_unchecked_not_a_mismatch() -> None:
    receipt, judged = _verdict(_intent(("color", "#FFD400"), target="all_text"), [_row("a")])
    assert receipt.status == "partial" and not judged


def test_color_compares_case_insensitively() -> None:
    receipt, judged = _verdict(
        _intent(("color", "#ffd400"), target="all_text"), [_row("a", color="#FFD400")]
    )
    assert receipt.status == "met" and judged


def test_title_target_with_no_title_row_is_unchecked() -> None:
    rows = [_row("kria-1", role="generative_intro", start_s=5.0, end_s=7.0)]
    receipt, judged = _verdict(_intent(("alignment", "left"), target="title"), rows)
    assert receipt.status == "partial" and not judged


def test_title_target_only_judges_title_rows() -> None:
    rows = [
        _row("guided-title", alignment="left"),
        _row("kria-9", role="generative_intro", start_s=5.0, end_s=7.0, alignment="right"),
    ]
    receipt, judged = _verdict(_intent(("alignment", "left"), target="title"), rows)
    assert receipt.status == "met" and judged


def test_missing_alignment_and_case_use_schema_defaults() -> None:
    receipt, judged = _verdict(
        _intent(("alignment", "center"), ("text_case", "none"), target="all_text"), [_row("a")]
    )
    assert receipt.status == "met" and judged


def test_draft_turn_without_a_text_lane_stays_unchecked() -> None:
    req = _req(_intent(("entrance", "fade"), target="all_text"))
    receipt = check_requirement(req, plan_facts_from_editor_payload({"title": "x"}))
    assert receipt.status == "partial" and not is_judged(req, receipt)


@pytest.mark.parametrize(
    "raw",
    [
        {"set": [{"field": "glow", "value": "x"}]},
        {"set": [{"field": "color", "value": "yellow"}]},
        {"set": [{"field": "entrance", "value": "fade"}], "target": "clip-1"},
        {"set": [{"field": "entrance", "value": "fade"}], "ids": ["a"]},
        {"set": [{"field": "entrance", "value": "fade"}, {"field": "entrance", "value": "pop"}]},
        {"set": []},
        "fade",
    ],
)
def test_malformed_intent_is_dropped_not_raised(raw: object) -> None:
    assert normalize_style_intent(raw) is None
    update = BriefUpdate(
        kind="style",
        scope="global",
        description="x",
        facts={"style_intent": raw, "animation": "fade"},
    )
    assert update.facts == {"animation": "fade"}


@pytest.mark.parametrize("word", ["fade", "Fade-in", "fade in"])
def test_entrance_aliases_resolve_to_the_closed_vocabulary(word: str) -> None:
    assert normalize_style_intent({"set": [{"field": "entrance", "value": word}]}) == {
        "set": [{"field": "entrance", "value": "fade"}]
    }


def test_intent_is_not_sticky_across_a_change() -> None:
    first = apply_updates(
        None,
        [_upd(facts=_intent(("entrance", "fade")))],
        source_turn_id="t1",
    )
    target = first.requirements[0]
    assert "style_intent" in target.facts
    changed = apply_updates(
        first,
        [
            BriefUpdate(
                operation="change",
                target_requirement_id=target.id,
                expected_version=first.version,
                kind="style",
                scope="global",
                description="y",
            )
        ],
        source_turn_id="t2",
    )
    assert "style_intent" not in changed.requirements[0].facts


def _upd(**kw: Any) -> BriefUpdate:
    return BriefUpdate(kind="style", scope="global", description="x", **kw)
