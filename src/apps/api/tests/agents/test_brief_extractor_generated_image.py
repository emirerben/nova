"""'The <generated image> when I say W' is a typed trigger ask; the creator's own footage is not.

The extractor prompt carries the shared brief section (which types "a photo, video, sticker ...
timed to spoken words" as `triggers`) plus the extractor-only section. The two must agree, and
the server (`cutaway_show_ask`) must read the facts they produce as the creator meant them.
"""

from __future__ import annotations

import json

from app.agents._schemas.brief_extractor import BriefExtractionInput
from app.agents.brief_extractor import (
    _NARROW_BRIEF_SECTION,
    BRIEF_EXTRACTOR_PROMPT_VERSION,
    BriefExtractorAgent,
)
from app.kria.brief import apply_updates
from app.kria.cutaway_show_ask import brief_full_screen_show_ask


def _brief_from(update: dict, message: str):
    out = BriefExtractorAgent(None).parse(  # type: ignore[arg-type]
        json.dumps({"brief_updates": [update]}),
        BriefExtractionInput(creator_request="", user_message=message, conversation=[]),
    )
    return apply_updates(None, out.brief_updates, source_turn_id="t1", creator_text=message)


def _style(description: str, facts: dict) -> dict:
    return {
        "operation": "add",
        "kind": "style",
        "scope": "global",
        "literal": None,
        "description": description,
        "facts": facts,
    }


def test_extractor_version_is_the_next_free_name():
    # v48 belongs to the talking-insert-at-mention PR and v49 to generated-image grounding;
    # v50 is #2089, v53 is #2088 (KRI-1173 colour, both merged); v54 is KRI-1189 (#2092; was
    # v52 on its branch). Whichever lands next renumbers.
    assert BRIEF_EXTRACTOR_PROMPT_VERSION == "2026-11-04-v60"


def test_extractor_teaches_generated_images_as_trigger_asks():
    section = _NARROW_BRIEF_SECTION.split("an image you generated", 1)[1]
    assert "ALWAYS" in section[:700]
    assert "never translated" in section[:900]


def test_the_extractor_has_no_media_context_it_cannot_read():
    assert "media_context" not in _NARROW_BRIEF_SECTION


def test_one_rule_separates_visuals_from_the_creators_own_footage():
    """The shared section says a photo/video/sticker at a spoken word carries `triggers`; this
    section must not say the opposite about videos without saying which videos."""
    tail = _NARROW_BRIEF_SECTION.split("an image you generated", 1)[1]
    assert "own footage" in tail
    own = tail.split("own footage", 1)[1]
    assert "display_mode" in own and "clip_sound" in own
    assert "never carries `triggers`" in own
    assert "never carries `triggers`" not in tail.split("own footage", 1)[0]


def test_a_visual_at_a_spoken_word_is_not_read_as_a_cutaway_of_the_creators_clips():
    brief = _brief_from(
        _style(
            "the generated image full screen",
            {"triggers": ["kahve"], "display_mode": "fullscreen"},
        ),
        "kahve dediğimde kahve görselini tam ekran göster",
    )
    assert brief_full_screen_show_ask(brief) is None


def test_the_creators_own_clips_shown_while_they_talk_are_a_cutaway_ask():
    brief = _brief_from(
        _style("show the talks full screen when I talk about them", {"display_mode": "fullscreen"}),
        "Show the talks when I mention them in full screen",
    )
    ask = brief_full_screen_show_ask(brief)
    assert ask is not None and ask.whole_video


def test_dropping_the_trigger_would_turn_a_visual_into_a_cutaway():
    """Why the rule must not tell the extractor to drop `triggers` for a Visual."""
    brief = _brief_from(
        _style("the generated image full screen", {"display_mode": "fullscreen"}),
        "kahve dediğimde kahve görselini tam ekran göster",
    )
    assert brief_full_screen_show_ask(brief) is not None


def test_both_lanes_coexist_when_inserts_are_open_and_each_ask_lands_in_its_own_lane():
    """Insert lane (PR #2086) and Visuals lane (this PR) in one extractor prompt. One wording per
    lane is parsed the way the model must emit it, then read back with the server's own readers:
    a Visual at a word is `triggers` (and never a cutaway), the creator's own footage in between
    their speech is an insert with no position, and over their speech is a plain overlay."""
    from app.agents.brief_extractor import _GENERATED_IMAGE_SECTION
    from app.agents.main_creator import _BRIEF_INSERT_CLIP_SENTENCE
    from app.kria.brief_checks import _insert_clip_fact

    prompt = BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(
            creator_request="", user_message="x", conversation=[], insert_clips_open=True
        )
    )
    assert _BRIEF_INSERT_CLIP_SENTENCE in prompt
    assert _GENERATED_IMAGE_SECTION in prompt

    visual = _brief_from(
        _style("the beer image", {"triggers": ["alkol"]}),
        "show the beer image when I say alkol",
    )
    insert = _brief_from(
        _style("the talks, in between my speech", {"insert_clip": "the talks"}),
        "put the talks in between my speech",
    )
    overlay = _brief_from(
        _style("the talks over my speech", {"display_mode": "fullscreen"}),
        "show them over my speech",
    )

    (visual_req,) = visual.live()
    assert visual_req.facts["triggers"] == ["alkol"]
    assert not _insert_clip_fact(visual_req)
    assert brief_full_screen_show_ask(visual) is None

    (insert_req,) = insert.live()
    assert _insert_clip_fact(insert_req)
    assert "triggers" not in insert_req.facts
    assert "insert_at_phrase" not in insert_req.facts and "insert_at_s" not in insert_req.facts
    assert brief_full_screen_show_ask(insert) is None

    (overlay_req,) = overlay.live()
    assert "triggers" not in overlay_req.facts and not _insert_clip_fact(overlay_req)
    ask = brief_full_screen_show_ask(overlay)
    assert ask is not None and ask.whole_video
