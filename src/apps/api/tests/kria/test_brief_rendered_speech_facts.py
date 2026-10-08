"""KRI-541: captions and speech-cleanup asks judged against a finished phone render.

`plan_facts_from_phone_variant` keeps `editor=True` for the variant's text lanes and sets
`rendered_variant`, plus the edit format, caption style and cleanup outcome the render
recorded. A real editor edit (no `rendered_variant`) keeps answering "can't check".
"""

from __future__ import annotations

import pytest

from app.agents._schemas.edit_format import NARRATED_EDIT_FORMATS
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    plan_facts_from_editor_payload,
    plan_facts_from_phone_variant,
)

_CUES = [{"text": "I ran my first marathon", "start_s": 0.0, "end_s": 1.8}]


def _variant(**overrides) -> dict:
    variant = {
        "variant_id": "v1",
        "render_destination": "device",
        "resolved_archetype": "narrated",
        "caption_cues": list(_CUES),
        "voiceover_caption_style": "sentence",
        "silence_cut_outcome": "applied",
        "text_elements": [],
    }
    variant.update(overrides)
    return variant


def _req(kind: str, description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, facts=facts
    )


# ---------------------------------------------------------------- facts off the variant


def test_narrated_variant_reads_format_captions_and_cleanup():
    facts = plan_facts_from_phone_variant(_variant())

    assert facts.rendered_variant is True
    assert facts.editor is True  # its text lanes are still editor facts
    assert facts.edit_format in NARRATED_EDIT_FORMATS
    assert facts.caption_style == "clean"
    assert facts.speech_cleanup_enabled is True
    assert facts.speech_cleanup_outcome == "applied"


def test_talking_variant_is_subtitled_with_word_captions():
    facts = plan_facts_from_phone_variant(
        _variant(resolved_archetype="subtitled", voiceover_caption_style="word")
    )

    assert facts.edit_format == "subtitled"
    assert facts.caption_style == "karaoke"


@pytest.mark.parametrize(
    ("overrides", "style"),
    [
        ({"caption_cues": None}, "none"),
        ({"caption_cues": []}, "none"),
        ({"caption_cues": None, "render_destination": "cloud"}, None),
        ({"voiceover_caption_style": None}, "clean"),
    ],
)
def test_caption_style_from_the_cues(overrides, style):
    assert plan_facts_from_phone_variant(_variant(**overrides)).caption_style == style


@pytest.mark.parametrize(
    ("overrides", "enabled", "outcome"),
    [
        ({"silence_cut_outcome": "no_change"}, True, "no_change"),
        ({"silence_cut_outcome": None}, False, "not_run"),
        ({"silence_cut_outcome": None, "render_destination": "cloud"}, None, None),
        ({"silence_cut_outcome": "insufficient_source_speech"}, None, None),
    ],
)
def test_cleanup_outcome(overrides, enabled, outcome):
    facts = plan_facts_from_phone_variant(_variant(**overrides))
    assert facts.speech_cleanup_enabled is enabled
    assert facts.speech_cleanup_outcome == outcome


def test_other_archetypes_add_no_speech_facts():
    facts = plan_facts_from_phone_variant(_variant(resolved_archetype="montage"))

    assert facts.rendered_variant is False
    assert facts.edit_format is None
    assert facts.caption_style is None
    assert facts.speech_cleanup_outcome is None


# ---------------------------------------------------------------- captions


def test_clean_captions_style_ask_is_met():
    receipt = check_requirement(
        _req("style", "clean captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "met"
    assert receipt.reason is None


def test_add_captions_text_ask_is_met():
    receipt = check_requirement(
        _req("text", "add captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "met"


def test_word_by_word_ask_against_sentence_captions_is_partial():
    receipt = check_requirement(
        _req("style", "word by word captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "partial"
    assert "full sentences" in receipt.reason


def test_word_by_word_ask_against_word_captions_is_met():
    facts = plan_facts_from_phone_variant(_variant(voiceover_caption_style="word"))
    receipt = check_requirement(_req("style", "word by word captions"), facts)
    assert receipt.status == "met"


def test_no_captions_ask_on_a_captioned_render_says_video_not_draft():
    receipt = check_requirement(
        _req("style", "no captions"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "partial"
    assert receipt.reason == "Captions are on in this video."


def test_captions_ask_on_an_uncaptioned_render():
    facts = plan_facts_from_phone_variant(_variant(caption_cues=None))
    receipt = check_requirement(_req("style", "clean captions"), facts)
    assert receipt.status == "partial"
    assert receipt.reason == "This video has no captions."


def test_caption_look_ask_stays_unchecked():
    receipt = check_requirement(
        _req("style", "yellow captions at the bottom"), plan_facts_from_phone_variant(_variant())
    )
    req = _req("style", "yellow captions at the bottom")
    assert not is_judged(req, receipt)


# ---------------------------------------------------------------- speech cleanup


def test_applied_cleanup_meets_a_pause_ask():
    receipt = check_requirement(
        _req("audio", "cut the long pauses"), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "met"
    assert receipt.reason is None


def test_cleanup_that_found_nothing_is_met_and_says_so():
    facts = plan_facts_from_phone_variant(_variant(silence_cut_outcome="no_change"))
    receipt = check_requirement(_req("audio", "cut the long pauses"), facts)
    assert receipt.status == "met"
    assert "found no long pauses" in receipt.reason


def test_cleanup_that_did_not_run_is_an_honest_partial():
    facts = plan_facts_from_phone_variant(_variant(silence_cut_outcome=None))
    req = _req("audio", "cut the long pauses")
    receipt = check_requirement(req, facts)
    assert receipt.status == "partial"
    assert "didn't run on this video" in receipt.reason
    assert is_judged(req, receipt)
    assert is_format_limit(receipt.reason)


@pytest.mark.parametrize(
    "description",
    [
        "cut long pauses and the restart of the kilometer thirty sentence",
        "cut the pauses and the retake",
    ],
)
def test_a_named_retake_is_left_for_the_editor(description):
    receipt = check_requirement(
        _req("audio", description), plan_facts_from_phone_variant(_variant())
    )
    assert receipt.status == "partial"
    assert receipt.reason.startswith("Speech cleanup cut the long pauses")
    assert "trim that in the editor" in receipt.reason


def test_a_length_in_the_cleanup_ask_follows_the_cut_take():
    req = _req("timing", "cut the long pauses and keep it under 45 s", duration_s=45)
    receipt = check_requirement(req, plan_facts_from_phone_variant(_variant()))
    assert receipt.status == "partial"
    assert "45s" in receipt.reason


def test_unknown_cleanup_outcome_stays_unchecked():
    facts = plan_facts_from_phone_variant(
        _variant(silence_cut_outcome="insufficient_source_speech")
    )
    req = _req("audio", "cut the long pauses")
    assert not is_judged(req, check_requirement(req, facts))


# ---------------------------------------------------------------- editor edits unchanged


def test_a_real_editor_edit_still_cannot_check_captions_or_cleanup():
    """An editor turn's facts carry only on-screen text: even with a speech format and a
    caption style somehow set, nothing about captions or cleanup is claimed."""
    import dataclasses

    facts = dataclasses.replace(
        plan_facts_from_editor_payload({"text_elements": [{"id": "t1", "text": "Hi"}]}),
        edit_format="narrated",
        caption_style="clean",
        speech_cleanup_enabled=True,
    )
    assert facts.editor is True
    assert facts.rendered_variant is False
    captions = _req("text", "add captions")
    cleanup = _req("audio", "cut the long pauses")
    receipts = build_receipts([captions, cleanup], facts, include_unchecked=True)
    assert [r.verification for r in receipts] == ["unchecked", "unchecked"]


def test_a_draft_still_reports_cleanup_at_approval():
    facts = PlanFacts(edit_format="narrated", speech_cleanup_offered=True)
    receipt = check_requirement(_req("audio", "cut the long pauses"), facts)
    assert receipt.status == "partial"
    assert receipt.reason.startswith("Choose Clean up speech")
