"""The receipt for "End with "comment READ" and an arrow" (item 14 / kit BUG-Q1-3).

The ask files the text and the arrow as ONE text row (literal "comment READ", the arrow in
its description). `_check_literal_text` found the literal and answered "met", so the arrow
vanished behind a "Done". The receipt now says "met" only when the arrow is really drawn
(the `closing-arrow` drawing the CTA ending adds), and judges the text across her two rows
("comment" + "READ").

Ways this could fail (written before the code):
- the text is found but nothing draws the arrow, and the receipt still says met;
- the two-row ending ("comment", "READ") is not recognised as "comment READ" -> a false miss;
- the creator deletes the arrow in the editor and the receipt keeps saying met;
- the word "arrow" inside her quoted text ("comment ARROW") is read as an arrow ask;
- the reason is English-only on a Turkish turn, or reads as the "simpler version?" question.

Facts are built the way production builds them: the draft through
`plan_facts_from_strategy` (like approval readiness for a phone Talking draft), the finished
video through `plan_facts_from_phone_variant` on the variant the worker persists.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.kria import brief_checks as bc
from app.kria.brief import BriefRequirement
from app.kria.reply_language import reply_language_for
from tests.tasks.test_phone_subtitled_narrated_dispatch import _lanes_features

_CTA = "comment READ"
ENDING = BriefRequirement(
    id="ending",
    kind="text",
    scope="global",
    description='End with "comment READ" and an arrow for the last 3 seconds',
    literal=_CTA,
    facts={"closing": _CTA},
)
_ARROW = {
    "id": "closing-arrow",
    "start_s": 77.0,
    "end_s": 80.0,
    "canvas_aspect": 1080 / 1920,
    "center_x": 0.86,
    "center_y": 0.72,
    "scale": 1.0,
    "rotation_deg": 0.0,
    "animation": {"entrance": "draw", "draw_duration_s": 0.6, "exit": "none"},
    "strokes": [
        {
            "color": "#FFFFFF",
            "width_frac": 0.009,
            "texture": "pen",
            "points": [[0.81, 0.66], [0.88, 0.70], [0.9, 0.75], [0.88, 0.79]],
        },
        {
            "color": "#FFFFFF",
            "width_frac": 0.009,
            "texture": "pen",
            "points": [[0.86, 0.77], [0.9, 0.79], [0.86, 0.81], [0.86, 0.77]],
        },
    ],
}


def _set_switch(monkeypatch, settings_obj, enabled: bool) -> None:
    """CREATOR_CTA_ENDING_ENABLED; a build without the setting (today's main) has it off."""
    if "creator_cta_ending_enabled" in type(settings_obj).model_fields:
        monkeypatch.setattr(settings_obj, "creator_cta_ending_enabled", enabled)


def _row(element_id: str, text: str, part: str | None) -> dict:
    params = {"source": "closing_title"}
    if part is not None:
        params["cta_part"] = part
    return {
        "id": element_id,
        "text": text,
        "start_s": 77.0,
        "end_s": 80.0,
        "source_params": params,
    }


def _variant(rows: list[dict], drawings: list[dict] | None = None) -> dict:
    variant = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "ok": True,
        "render_status": "ready",
        "text_elements": rows,
    }
    if drawings is not None:
        variant["drawings"] = drawings
    return variant


def _draft_facts(closing: str = _CTA) -> bc.PlanFacts:
    strategy = {
        "edit_format": "subtitled",
        "caption_style": "karaoke",
        "audio_strategy": "original_audio",
        "closing_title": closing,
    }
    return dataclasses.replace(
        bc.plan_facts_from_strategy(strategy, clip_ids=["take"]),
        phone_text=True,
        phone_talking=True,
        caption_words_at_render=True,
    )


_HER_ENDING = [
    _row("closing-title", "comment", "lead"),
    _row("closing-title-keyword", "READ", "keyword"),
]


def test_the_pill_without_an_arrow_is_not_met_and_names_the_arrow():
    facts = bc.plan_facts_from_phone_variant(_variant([_row("closing-title", _CTA, None)]))
    receipt = bc.check_requirement(ENDING, facts)
    assert receipt.status == "partial"
    assert "arrow" in (receipt.reason or "").lower()
    # A plain answer about what the video holds, never the "simpler version?" question.
    assert bc.is_judged(ENDING, receipt)


def test_her_ending_with_the_arrow_drawn_is_met():
    facts = bc.plan_facts_from_phone_variant(_variant(_HER_ENDING, [_ARROW]))
    assert bc.check_requirement(ENDING, facts).status == "met"


def test_her_ending_after_the_arrow_was_deleted_in_the_editor_is_not_met():
    facts = bc.plan_facts_from_phone_variant(_variant(_HER_ENDING, []))
    receipt = bc.check_requirement(ENDING, facts)
    assert receipt.status == "partial"
    assert "arrow" in (receipt.reason or "").lower()


def test_her_two_rows_count_as_the_closing_text_when_no_arrow_was_asked():
    plain = ENDING.model_copy(
        update={"description": 'End with "comment READ" for the last 3 seconds'}
    )
    facts = bc.plan_facts_from_phone_variant(_variant(_HER_ENDING))
    assert bc.check_requirement(plain, facts).status == "met"


def test_the_word_arrow_inside_her_quoted_text_is_not_an_arrow_ask():
    quoted = BriefRequirement(
        id="ending",
        kind="text",
        scope="global",
        description='End with "comment ARROW" for the last 3 seconds',
        literal="comment ARROW",
        facts={"closing": "comment ARROW"},
    )
    rows = [
        _row("closing-title", "comment", "lead"),
        _row("closing-title-keyword", "ARROW", "keyword"),
    ]
    facts = bc.plan_facts_from_phone_variant(_variant(rows))
    assert bc.check_requirement(quoted, facts).status == "met"


def test_a_draft_never_says_met_for_the_arrow():
    receipt = bc.check_requirement(ENDING, _draft_facts())
    assert receipt.status != "met"
    assert "arrow" in (receipt.reason or "").lower()


@pytest.mark.parametrize("enabled", [False, True])
def test_the_draft_says_plainly_what_happens_to_the_arrow(monkeypatch, enabled):
    """On: Kria will draw it with the ending. Off: Kria can't, and says where she can."""
    from app.config import settings

    _set_switch(monkeypatch, settings, enabled)
    # The phone Talking text lane as prod runs it (captions, title and closing lanes on),
    # with freehand drawings verified on the device.
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        _lanes_features("authoredText", "freehandDrawing"),
    )
    monkeypatch.setattr(settings, "phone_subtitled_title_enabled", True)
    monkeypatch.setattr(settings, "phone_subtitled_closing_title_enabled", True)
    receipt = bc.check_requirement(ENDING, _draft_facts())
    assert receipt.status == "partial"
    assert bc.is_judged(ENDING, receipt)
    reason = (receipt.reason or "").lower()
    if enabled:
        assert "draw" in reason and "arrow" in reason
        assert "editor" not in reason
    else:
        assert "editor" in reason


def test_the_reason_is_turkish_on_a_turkish_turn():
    facts = bc.plan_facts_from_phone_variant(_variant([_row("closing-title", _CTA, None)]))
    with reply_language_for("tr"):
        receipt = bc.check_requirement(ENDING, facts)
    assert receipt.status == "partial"
    assert "ok" in (receipt.reason or "").lower()
    assert "arrow" not in (receipt.reason or "").lower()
