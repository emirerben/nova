"""KRI-699: a text line timed to a spoken word on a voiceover edit is said to be impossible.

Tigre night N2 (thread FEFCCF5C, turns 27d4e287 / 51853185): "Put a big text line on screen
for each rule when I say it: "1 · leave before 11", "2 · you're all amigos" ..." on a phone
Voiceover edit. The brief kept both texts verbatim (`·` and the apostrophe intact) with spoken
`triggers`, and the clip planner kept them as `caption` clip intents. A voiceover edit draws
only its title, its captions and word-timed photos/sounds (`phone_narrated_plan`): nothing
draws a creator text on a spoken word, so the draft never had them. The receipt said "That
exact text isn't in this draft." -- as if a retry could fix it -- and the "different approach"
offer could never work.

Ways this can go wrong (written before the fix):

1. A spoken-word text on a voiceover draft still reads "isn't in this draft" (the bug).
2. A whole-video text with no spoken cue on a voiceover draft gets the spoken-word reason,
   including copy that itself contains cue words ("Meet at the words").
3. A spoken-word text on a montage or Talking draft gets the voiceover reason.
4. A spoken-word text the draft really shows (the title, a pinned line) is no longer met.
5. The limit becomes a format limit, so the draft goes ahead without asking the creator.
6. A Turkish chat gets the English reason.
7. A title ask that names a spoken cue is judged as a missing text line.
8. The reply line drops `·` or the apostrophe from the creator's quoted text.

Replayed from the real N2 brief and strategy (`~/Downloads/kria-boat-test/
N2_three_rules_english_voiceover/runs/2026-10-15_FEFCCF5C/`), no model calls.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    needs_creator_choice,
    plan_facts_from_strategy,
    reply_from_receipts,
)
from app.kria.reply_language import bind_reply_language, release_reply_language

SPOKEN_TEXT_REASON = "A voiceover edit can't show text at a word you say yet."

R2 = BriefRequirement(
    id="r2",
    kind="text",
    scope="global",
    literal="1 · leave before 11",
    description="big text line when rule 1 is said",
    facts={"triggers": ["1", "leave before 11"]},
)
R3 = BriefRequirement(
    id="r3",
    kind="text",
    scope="global",
    literal="2 · you're all amigos",
    description="big text line when rule 2 is said",
    facts={"triggers": ["2", "you're all amigos"]},
)
# KRI-692 (Tigre night N1): the Turkish cue words, no `triggers` fact.
R_TR = BriefRequirement(
    id="r4",
    kind="text",
    scope="global",
    literal="20 dk",
    description="'yirmi dakika' dediğimde ekrana '20 dk' yazısı gelsin",
)
WHOLE_VIDEO = BriefRequirement(
    id="r5", kind="text", scope="global", literal="Tigre 2026", description="Tigre 2026 text"
)
# Cue words inside the creator's own copy are text, not a spoken cue.
CUE_IN_COPY = BriefRequirement(
    id="r6", kind="text", scope="global", literal="Meet at the words", description="big text"
)


def _caption_intent(intent_id: str, text: str) -> dict:
    return {
        "op": "caption",
        "status": "resolved",
        "attribute": f'when I say "{text}"',
        "intent_id": intent_id,
        "assignments": [
            {
                "evidence": "speech",
                "media_id": "analysis-proxy-ios-7D8277E2-7F2F-4B16-A36F-46AD7C60A2D2.mp4",
                "confidence": 0.9,
            }
        ],
        "caption_text": text,
        "creator_text": text,
        "caption_grounding": "creator_text",
    }


# The simpler-version strategy of turn 51853185 (draft 43d5a100), trimmed to what the
# receipts read.
N2_STRATEGY = {
    "edit_format": "narrated_planned",
    "audio_strategy": "voiceover",
    "render_program": "native",
    "caption_style": "clean",
    "opening_title": "3 rules of a night boat in Tigre",
    "reaction_beats": [
        {
            "beat_id": "park",
            "trigger": "amusement park",
            "visual_id": "asset-a",
            "visual_role": "photo",
        },
        {"beat_id": "dock", "trigger": "the dock", "visual_id": "asset-b", "visual_role": "photo"},
    ],
    "resolved_clip_intents": [
        _caption_intent("caption_rule1_text", "1 · leave before 11"),
        _caption_intent("caption_rule2_text", "2 · you're all amigos"),
    ],
}


def _receipts(strategy: dict, *reqs: BriefRequirement):
    # As `kria_runtime` builds them for a brief-bound writer (the held-draft path).
    return build_receipts(list(reqs), plan_facts_from_strategy(strategy), include_unchecked=True)


def test_n2_rule_texts_say_the_voiceover_edit_cannot_time_them() -> None:
    receipts = _receipts(N2_STRATEGY, R2, R3)

    assert [(r.status, r.reason) for r in receipts] == [
        ("not_possible", SPOKEN_TEXT_REASON),
        ("not_possible", SPOKEN_TEXT_REASON),
    ]
    # Still the creator's call (held draft + "simpler version?"), never a silent format limit.
    assert all(needs_creator_choice(r) for r in receipts)
    reply = reply_from_receipts(CreativeBrief(version=2, requirements=[R2, R3]), receipts)
    assert "isn't in this draft" not in reply
    assert '"1 · leave before 11"' in reply
    assert '"2 · you\'re all amigos"' in reply


def test_turkish_cue_words_without_a_trigger_fact_get_the_turkish_reason() -> None:
    token = bind_reply_language("tr")
    try:
        (receipt,) = _receipts(N2_STRATEGY, R_TR)
    finally:
        release_reply_language(token)

    assert receipt.status == "not_possible"
    assert (
        receipt.reason == "Seslendirmeli düzenleme henüz söylediğin bir kelimede yazı gösteremiyor."
    )


def test_text_the_draft_shows_is_still_met() -> None:
    pinned = {
        **N2_STRATEGY,
        "opening_title": "1 · leave before 11",
        "pinned_texts": [{"text": "2 · you're all amigos", "corner": "top_left"}],
    }

    assert [r.status for r in _receipts(pinned, R2, R3)] == ["met", "met"]


def test_only_spoken_word_texts_on_voiceover_edits_change() -> None:
    for plain in (WHOLE_VIDEO, CUE_IN_COPY):
        (receipt,) = _receipts(N2_STRATEGY, plain)
        assert (receipt.status, receipt.reason) == (
            "partial",
            "That exact text isn't in this draft.",
        )

    (other,) = _receipts(
        {**N2_STRATEGY, "edit_format": "montage", "audio_strategy": "original_audio"}, R2
    )
    assert (other.status, other.reason) == ("partial", "That exact text isn't in this draft.")
    # KRI-705: a Talking edit has the same gap and says so plainly (a format limit).
    (talking,) = _receipts(
        {**N2_STRATEGY, "edit_format": "subtitled", "audio_strategy": "original_audio"}, R2
    )
    assert talking.status == "not_possible"
    assert talking.reason == "A Talking video can't show text at a word you say yet"


def test_a_title_that_names_a_spoken_cue_keeps_the_title_check() -> None:
    title = BriefRequirement(
        id="r1",
        kind="text",
        scope="title",
        literal="3 rules",
        description="title when I say three rules",
        facts={"triggers": ["three rules"]},
    )

    (receipt,) = _receipts({**N2_STRATEGY, "opening_title": "Night boat"}, title)
    assert receipt.reason != SPOKEN_TEXT_REASON
