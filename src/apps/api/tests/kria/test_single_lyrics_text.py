"""One requested text is one on-screen text (prod thread 6db7ecd4, 2026-11-14).

The planner carried pasted lyrics twice: a caption clip intent with the whole text and the
same words split into three pinned lines. The contract then demanded all four and the text
pass refused the overlap (and its retry drifted on the same duplicate bars).
"""

from __future__ import annotations

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import adapt_creator_action
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.creator_render_contract import build_render_contract

LINES = ("Every road leads home", "Through the rain and sun", "Forever with you, Lions")
WHOLE = " ".join(LINES)


def _brief() -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="text",
                scope="global",
                description="Use these supplied lyrics as the text.",
                literal=WHOLE,
            )
        ],
    )


def _caption(text: str | None = WHOLE, *, status: str = "resolved") -> tuple:
    intent = ClipIntent(
        intent_id="lyrics_caption_1",
        op="caption",
        attribute="this video",
        creator_text=text,
        caption_attribute=None if text else "the stadium",
    )
    resolved = ResolvedClipIntent(
        **intent.model_dump(),
        status=status,
        assignments=[ClipAssignment(media_id="clip-1", confidence=1.0)]
        if status == "resolved"
        else [],
        question="Which clip?" if status != "resolved" else None,
        caption_text=text or "Match day",
        caption_grounding="creator_text" if text else "vision_verified",
    )
    return [intent], [resolved]


def _strategy(pins: list[str], caption: tuple | None) -> dict:
    action = ProposeStrategy(
        kind="propose_strategy",
        strategy=CreativeStrategy(
            direction="guided_story",
            edit_format="day_vlog",
            audio_strategy="original_audio",
            pacing="balanced",
            render_program="guided",
            selected_media_ids=["clip-1"],
            rationale="The lyrics over the clip.",
            target_duration_s=14,
            pinned_texts=[{"text": pin, "corner": "bottom_center"} for pin in pins] or None,
        ),
        summary="The lyrics over the clip.",
    )
    intents, resolved = caption if caption is not None else (None, None)
    plan = adapt_creator_action(
        action, server_clip_intents=intents, server_resolved_clip_intents=resolved
    )
    return plan.intents[0].arguments["strategy"]


def _pins(strategy: dict) -> list[str]:
    return [pin["text"] for pin in strategy.get("pinned_texts") or ()]


def test_lyrics_split_into_pins_and_a_caption_become_one_text() -> None:
    strategy = _strategy(list(LINES), _caption())

    assert _pins(strategy) == []
    assert [i["creator_text"] for i in strategy["resolved_clip_intents"]] == [WHOLE]
    contract = build_render_contract(strategy, generation_id="g1", brief=_brief())
    assert contract is not None
    assert [row.text for row in contract.exact_texts] == [WHOLE]


def test_case_punctuation_and_spacing_do_not_hide_the_duplicate() -> None:
    pins = ["EVERY ROAD LEADS HOME,", "through the rain  and sun", "Forever with you Lions!"]
    assert _pins(_strategy(pins, _caption())) == []


def test_a_single_pin_repeating_the_caption_is_dropped() -> None:
    assert _pins(_strategy([WHOLE], _caption())) == []


def test_other_pins_stay_and_only_the_repeated_run_goes() -> None:
    strategy = _strategy(["Part 1", *LINES], _caption())
    assert _pins(strategy) == ["Part 1"]


def test_pins_with_different_words_are_kept() -> None:
    assert _pins(_strategy(["Go Lions"], _caption())) == ["Go Lions"]


def test_reordered_lines_are_not_the_same_text() -> None:
    reordered = [LINES[1], LINES[0], LINES[2]]
    assert _pins(_strategy(reordered, _caption())) == reordered


def test_an_unresolved_caption_leaves_the_pins_as_the_only_copy() -> None:
    assert _pins(_strategy(list(LINES), _caption(status="needs_creator"))) == list(LINES)


def test_a_caption_with_no_creator_words_does_not_drop_pins() -> None:
    assert _pins(_strategy(list(LINES), _caption(None))) == list(LINES)


def test_pins_without_clip_intents_are_unchanged() -> None:
    assert _pins(_strategy(list(LINES), None)) == list(LINES)
