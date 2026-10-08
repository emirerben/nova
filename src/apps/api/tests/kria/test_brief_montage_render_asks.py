"""KRI-546: which asks the finished phone montage judges, and how a short clip id resolves.

"Use one of two copies of the same video" and "end on Elif's sentence in her own voice" are
judged only from a finished montage's evidence (`plan_facts_from_rendered_montage`); every
other plan keeps the neutral "can't verify" it gave before. The recognisers read the
creator's own words in English and Turkish and must not swallow look-alike asks.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (  # noqa: PLC2701
    PlanFacts,
    _scope_clip_id,
    _wants_closing_speech,
    _wants_one_of_duplicates,
    check_requirement,
    judged_at_render,
    plan_facts_from_rendered_montage,
)


def _req(kind: str, description: str, scope: str = "global", **facts) -> BriefRequirement:
    return BriefRequirement(id="r", kind=kind, scope=scope, description=description, facts=facts)


@pytest.mark.parametrize(
    ("kind", "description", "wanted"),
    [
        ("select", "Aynı videodan iki tane varsa birini kullan", True),
        ("select", "If the same video is in there twice, only use one", True),
        ("select", "Remove duplicate clips", True),
        ("select", "Don't use the same clip twice", True),
        ("style", "Kopya videoları çıkar", True),
        ("select", "Tekrar eden videolardan sadece birini kullan", True),
        # Wanting the same clip twice, or no copy named at all, is something else.
        ("select", "Use the same clip at the start and the end", False),
        ("select", "Aynı videoyu kullanmak istiyorum", False),
        ("select", "Skip the quad bike", False),
        ("order", "Remove duplicate clips", False),
    ],
)
def test_the_duplicate_ask_is_read_from_the_creators_words(kind, description, wanted):
    assert _wants_one_of_duplicates(_req(kind, description)) is wanted


@pytest.mark.parametrize(
    ("kind", "description", "facts", "wanted"),
    [
        ("audio", "En sonda Elif'in kameraya söylediği cümleyi kendi sesiyle kullan", {}, True),
        ("audio", "End with Elif's sentence to the camera in her own voice", {}, True),
        ("order", "End on Elif saying goodbye", {"last_clip": "Elif saying goodbye"}, True),
        # A closing photo, a muted or musical ending, a voiceover mix, a whole-video voice
        # ask and a timing ask keep their own checks.
        ("style", "End with my medal photo", {}, False),
        ("audio", "Mute the last clip", {}, False),
        ("audio", "At the end let the music get louder", {}, False),
        ("audio", "Keep crowd noise quiet under the voiceover", {}, False),
        ("audio", "Use her own voice for the whole video", {}, False),
        ("order", "Order by capture time", {"key": "capture_time"}, False),
        ("timing", "End on her sentence", {}, False),
    ],
)
def test_the_closing_line_ask_is_read_from_the_creators_words(kind, description, facts, wanted):
    assert _wants_closing_speech(_req(kind, description, **facts)) is wanted


def test_a_short_clip_id_names_exactly_one_clip():
    elif_clip = "analysis-proxy-ios-F0CECCF8-5871-4F5B-99AB-E90473FFD96F.mp4"
    other = "analysis-proxy-ios-145EF73F-D98E-4378-97CE-0B0E6C3F8DE2.mp4"
    ids = [other, elif_clip]

    assert _scope_clip_id(_req("audio", "x", "clip:F0CECCF8"), ids) == elif_clip
    assert _scope_clip_id(_req("audio", "x", "clip:f0ceccf8"), ids) == elif_clip
    assert _scope_clip_id(_req("audio", "x", f"clip:{elif_clip}"), ids) == elif_clip
    # Too short to be a reliable reference, unknown, or carried by two clips: no clip.
    assert _scope_clip_id(_req("audio", "x", "clip:5871"), ids) is None
    assert _scope_clip_id(_req("audio", "x", "clip:ABCDEF12"), ids) is None
    assert _scope_clip_id(_req("audio", "x", "clip:F0CECCF8"), [elif_clip, elif_clip + "x"]) is None


@pytest.mark.parametrize(
    "facts",
    [
        PlanFacts(),  # a strategy draft
        PlanFacts(editor=True),  # an editor payload or a non-montage phone variant
        PlanFacts(rendered_output=True, clip_ids=("a", "b")),  # a plan record at dispatch
    ],
)
def test_no_other_plan_judges_either_ask(facts):
    for req in (
        _req("select", "Aynı videodan iki tane varsa birini kullan"),
        _req("audio", "En sonda Elif'in kameraya söylediği cümleyi kendi sesiyle kullan"),
    ):
        assert judged_at_render(req)
        receipt = check_requirement(req, facts)
        assert receipt.status == "partial"
        assert receipt.reason == "I can't verify this one automatically yet."


def test_rendered_montage_facts_need_the_record():
    variant = {"text_elements": [], "story_timeline": [{"lane": "clip", "media_id": "a"}]}

    assert not plan_facts_from_rendered_montage(variant, None).rendered_montage
    facts = plan_facts_from_rendered_montage(variant, {"ordering_basis": "capture_time"})
    assert facts.rendered_montage
    assert facts.clip_ids == ("a",)
    assert facts.ordering_basis == "capture_time"
    # No fingerprints: nothing about duplicates is claimed.
    assert facts.duplicate_clip_positions is None
    # No held line in the record: nothing about the closing line is claimed.
    assert facts.closing_speech is None
