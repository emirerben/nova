"""KRI-667: brief checks read what an ask means from `facts`, not only from its words.

The Main Creator / brief extractor already read the creator's message in any language.
They now record the meaning of the asks the checkers judge in English keys and values
(`triggers`, `keep_whole_take`, `closing`, `speech_cleanup`, `captions`,
`caption_language`). The word recognisers stay as the fallback for a requirement without
those facts, so a Turkish phrasing nobody wrote a pattern for is still judged.

Failure modes pinned here (written before the code):
- a recorded fact is ignored and a Turkish ask stays "can't verify";
- an odd spelling of a value ("Word-By-Word", "tr-TR", "Türkçe", "true") misroutes or crashes;
- a fact and the words disagree and the words win;
- an explicit "no" (`speech_cleanup: false`) falls back to the words instead of meaning no;
- a fact on a kind the checker never judges bypasses the kind gate;
- an unknown value replaces the words instead of falling back to them.
"""

from __future__ import annotations

import pytest

from app.agents.main_creator import MainCreatorAgent
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement
from app.kria.reply_language import reply_language_for

_TALKING = {"edit_format": "subtitled", "caption_style": "karaoke", "speech_cleanup_offered": True}


def _req(kind: str, description: str, **facts) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, facts=facts
    )


def _receipt(req: BriefRequirement, **plan):
    return check_requirement(req, PlanFacts(**{**_TALKING, **plan}))


def _same(left, right) -> None:
    assert left is not None and right is not None
    assert (left.status, left.reason) == (right.status, right.reason)


# English words the recognisers know vs. a Turkish phrasing they do not, plus the fact the
# model records for it. The Turkish ask must get the English ask's receipt.
_PAIRS = [
    pytest.param(
        _req("timing", "Cut out the long pauses and the ums"),
        _req("timing", "Eee ve ııı seslerini at", speech_cleanup=True),
        {},
        id="speech-cleanup",
    ),
    pytest.param(
        _req("style", "Add captions"),
        _req("style", "Konuşmamın yazısı ekranda olsun", captions="on"),
        {},
        id="captions-on",
    ),
    pytest.param(
        _req("style", "No captions"),
        _req("style", "Ekranda konuşma yazısı görünmesin", captions="off"),
        {},
        id="captions-off",
    ),
    pytest.param(
        _req("style", "Karaoke captions"),
        _req("style", "Her kelime söylenirken yansın", captions="word_by_word"),
        {},
        id="word-captions",
    ),
    pytest.param(
        _req("style", "English subtitles"),
        _req("style", "Konuşma yazısı İngilizce olsun", captions="on", caption_language="en"),
        {"caption_language": "tr"},
        id="caption-language",
    ),
    pytest.param(
        _req("timing", "Keep my whole take"),
        _req("timing", "Videonun hepsi kalsın", keep_whole_take=True),
        {},
        id="whole-take",
    ),
    pytest.param(
        _req("style", "Show the photo when I say 'pasta'"),
        _req("style", "'pasta' lafında fotoğrafı", triggers=["pasta"]),
        {},
        id="word-beats",
    ),
    pytest.param(
        _req("style", "End on the sunset shot"),
        _req("style", "Gün batımı çekimiyle son ver", closing="gün batımı çekimi"),
        {},
        id="closing",
    ),
]


@pytest.mark.parametrize(("english", "turkish", "plan"), _PAIRS)
def test_a_turkish_ask_with_its_fact_gets_the_english_receipt(english, turkish, plan):
    _same(_receipt(turkish, **plan), _receipt(english, **plan))


@pytest.mark.parametrize(("english", "turkish", "plan"), _PAIRS)
def test_the_same_turkish_words_without_the_fact_are_not_judged_as_that_ask(english, turkish, plan):
    # The gap the facts close: these Turkish wordings match none of the recognisers.
    bare = turkish.model_copy(update={"facts": {}})
    english_receipt = _receipt(english, **plan)
    bare_receipt = _receipt(bare, **plan)
    assert bare_receipt is None or (bare_receipt.status, bare_receipt.reason) != (
        english_receipt.status,
        english_receipt.reason,
    )


def test_the_receipt_is_written_in_the_chat_language():
    req = _req("timing", "Eee ve ııı seslerini at", speech_cleanup=True)
    with reply_language_for("tr"):
        receipt = _receipt(req)
    assert receipt is not None and "Konuşmayı temizle" in (receipt.reason or "")


def test_a_recorded_fact_wins_over_words_that_say_the_opposite():
    _same(
        _receipt(_req("style", "No captions", captions="on")),
        _receipt(_req("style", "Add captions")),
    )


def test_an_explicit_no_means_no_instead_of_reading_the_words():
    asked = _receipt(_req("timing", "Cut out the long pauses"))
    declined = _receipt(_req("timing", "Cut out the long pauses", speech_cleanup=False))
    assert declined is not None and asked is not None
    assert declined.reason != asked.reason
    assert "Clean up speech" not in (declined.reason or "")


@pytest.mark.parametrize(
    ("facts", "plan", "english", "english_plan"),
    [
        ({"captions": "Word-By-Word"}, {}, "Karaoke captions", {}),
        ({"captions": True}, {}, "Add captions", {}),
        ({"captions": False}, {}, "No captions", {}),
        ({"speech_cleanup": "true"}, {}, None, {}),
        (
            {"captions": "on", "caption_language": "tr-TR"},
            {"caption_language": "en"},
            "Turkish subtitles",
            {"caption_language": "en"},
        ),
        (
            {"captions": "on", "caption_language": "Türkçe"},
            {"caption_language": "en"},
            "Turkish subtitles",
            {"caption_language": "en"},
        ),
    ],
)
def test_odd_spellings_of_a_fact_are_read_as_the_same_value(facts, plan, english, english_plan):
    kind = "timing" if "speech_cleanup" in facts else "style"
    got = _receipt(_req(kind, "x", **facts), **plan)
    want_words = english or "Cut out the long pauses"
    _same(got, _receipt(_req(kind, want_words), **english_plan))


def test_an_unknown_value_falls_back_to_the_words():
    _same(
        _receipt(_req("style", "Add captions", captions="maybe")),
        _receipt(_req("style", "Add captions")),
    )


def test_a_captions_fact_does_not_open_a_kind_the_captions_check_never_judges():
    _same(_receipt(_req("timing", "x", captions="on")), _receipt(_req("timing", "x")))


# The checkers read these keys; both prompts that write the brief must ask for them, or the
# facts never arrive and every Turkish ask falls back to the English-shaped recognisers.
_ASK_FACT_KEYS = (
    "triggers",
    "keep_whole_take",
    "closing",
    "speech_cleanup",
    "captions",
    "caption_language",
    "spellings",  # KRI-793
    # KRI-789: the quoted lines a take starts at / ends right after (a trim, not an order).
    "start_line",
    "end_line",
)


def test_both_brief_prompts_ask_for_every_fact_the_checkers_read():
    from app.agents._schemas.brief_extractor import BriefExtractionInput
    from app.agents.brief_extractor import BriefExtractorAgent
    from tests.agents.test_main_creator_agent import _input

    planner = MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input().model_copy(update={"brief_enabled": True})
    )
    extractor = BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(creator_request="", user_message="x", conversation=[])
    )
    for prompt in (planner, extractor):
        missing = [key for key in _ASK_FACT_KEYS if f'"{key}"' not in prompt]
        assert not missing, missing
