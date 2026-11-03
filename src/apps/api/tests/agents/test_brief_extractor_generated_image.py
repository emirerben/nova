"""'The <generated image> when I say W' is a typed trigger ask; a video cutaway is not."""

from __future__ import annotations

from app.agents.brief_extractor import _NARROW_BRIEF_SECTION, BRIEF_EXTRACTOR_PROMPT_VERSION
from app.kria.brief import BriefRequirement
from app.kria.cutaway_show_ask import full_screen_show_ask


def _req(**facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind="style", scope="global", description="show it full screen", facts=facts
    )


def test_extractor_version_is_bumped():
    assert BRIEF_EXTRACTOR_PROMPT_VERSION > "2026-11-02-v47"


def test_extractor_teaches_generated_images_as_trigger_asks():
    section = _NARROW_BRIEF_SECTION
    assert "image you generated" in section
    assert "ALWAYS" in section.split("image you generated", 1)[1][:600]
    assert "never translated" in section.split("image you generated", 1)[1][:900]


def test_the_trigger_rule_does_not_swallow_video_cutaways():
    tail = _NARROW_BRIEF_SECTION.split("image you generated", 1)[1][:1200]
    assert "video" in tail and "never carries `triggers`" in tail


def test_typed_cutaway_ask_still_ignores_trigger_asks():
    assert full_screen_show_ask([_req(display_mode="fullscreen", triggers=["alkol"])]) is None
    assert full_screen_show_ask([_req(display_mode="fullscreen")]) is not None
