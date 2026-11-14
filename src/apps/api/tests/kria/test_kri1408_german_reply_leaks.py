"""KRI-1408: German replies leaked English and cut a reason mid-word (ylab, build 1977).

Thread 8044373b, 2026-11-13, reply language German. The captions ask (r6) read:

    Jede Stimme in deinem Take bekommt Untertitel, auch die außerhalb des Bildes; sie sind auf
    German; Ich schreibe Usersmagic, Nike, ... so, wie du them geschrieben hast, und prüfe them
    im fertigen Video

and the chat message cut it at "…sie sind auf German; Ich s…".

Ways this can go wrong, written down before the code:

1. The caption language name stays English ("German") in a German chat: the language-name
   table is read by index, which the catalog cannot key.
2. An English pronoun ("it"/"them") is glued into the German sentence through a template
   field.
3. A clause after "; " starts with a capital "Ich" (German writes "ich" mid-sentence).
4. A reply over the length limit is cut mid-word ("Ich s…") instead of after a whole word.
5. The fix changes the English or Turkish line by a byte.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria import copy_catalog
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    MAX_REPLY_CHARS,
    PlanFacts,
    _caption_words_plan,
    plan_facts_from_strategy,
    reply_and_lead_from_receipts,
)
from app.kria.contracts import RequirementReceipt
from app.kria.reply_language import reply_language_for

_NAMES = "Usersmagic, Nike, Delivery Hero, Mina Foundation, Mexico City, Kria"


@pytest.fixture
def german_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True, raising=False)
    copy_catalog.catalog.cache_clear()


def _captions_req(names: str = _NAMES) -> BriefRequirement:
    return BriefRequirement(
        id="r6",
        kind="style",
        facts={"captions": "on", "caption_language": "de", "spellings": names.split(", ")},
        scope="global",
        status="open",
        description=f"German captions for every voice, spelled right: {names}",
    )


def _talking_facts() -> PlanFacts:
    import dataclasses

    facts = plan_facts_from_strategy(
        {"edit_format": "subtitled", "audio_strategy": "original_audio", "caption_style": "clean"}
    )
    return dataclasses.replace(facts, phone_talking=True, caption_words_at_render=True)


def _reason(language: str, names: str = _NAMES) -> str:
    with reply_language_for(language):
        return str(_caption_words_plan(_captions_req(names), _talking_facts()).reason)


@pytest.mark.parametrize("names", [_NAMES, "Kria"], ids=["several", "one"])
def test_german_caption_line_has_no_english(german_on: None, names: str) -> None:
    reason = _reason("de", names)
    assert "Deutsch" in reason, reason
    words = set(reason.replace(";", " ").replace(",", " ").split())
    assert not words & {"German", "them", "it", "they'll", "I'll"}, reason
    # Mid-sentence, after "; ", German writes "ich" (case 3).
    assert "; Ich " not in reason, reason


def test_english_and_turkish_caption_lines_are_unchanged() -> None:
    assert _reason("en", "Kria") == (
        "Every voice in your take gets captions, off-camera ones too; they'll be in German; "
        "I'll spell Kria the way you wrote it and check it in the finished video"
    )
    assert _reason("en", "Kria, Nike") == (
        "Every voice in your take gets captions, off-camera ones too; they'll be in German; "
        "I'll spell Kria and Nike the way you wrote them and check them in the finished video"
    )
    assert _reason("tr", "Kria, Nike") == (
        "Çekimindeki her ses altyazıya girer, kamera dışındaki sesler de; altyazılar Almanca "
        "olacak; şunları senin yazdığın gibi yazıp bitmiş videoda kontrol edeceğim: Kria ve Nike"
    )


@pytest.mark.parametrize("repeats", range(10, 26))
def test_an_over_long_reply_is_never_cut_mid_word(german_on: None, repeats: int) -> None:
    """Case 4: the reply's last resort cut ends on a whole word ("Ich s…" was the bug)."""
    word = "Untertitelzeile"
    reqs = [
        BriefRequirement(
            id=f"r{i}",
            kind="style",
            facts={},
            scope="global",
            status="open",
            description=f"Ask {i}",
        )
        for i in range(1, 9)
    ]
    receipts = [
        RequirementReceipt(
            requirement_id=req.id,
            status="partial",
            verification="unchecked",
            reason=" ".join([word] * repeats)[:300],
        )
        for req in reqs
    ]
    with reply_language_for("de"):
        text, _lead = reply_and_lead_from_receipts(
            CreativeBrief(version=1, requirements=reqs), receipts
        )
    assert len(text) <= MAX_REPLY_CHARS
    assert text.endswith("…"), text[-80:]
    last = text[:-1].split()[-1].strip("(),;:.-")
    assert not (word.startswith(last) and last != word), text[-80:]


@pytest.mark.parametrize("extra", range(0, 15))
def test_a_held_reply_body_is_never_cut_mid_word(extra: int) -> None:
    """Case 4 on the hold reply (the chat message in the bug): the receipt lines above the
    question are shortened on a word boundary."""
    from app.tasks.kria_runtime import _MAX_TURN_RESPONSE_CHARS, _fit_reply

    word = "Untertitelzeile"
    tail = "Soll ich eine einfachere Version machen?"
    body = "- Teilweise: " + " ".join([word] * (_MAX_TURN_RESPONSE_CHARS // 10)) + "x" * extra
    text = _fit_reply(body, tail)
    assert len(text) <= _MAX_TURN_RESPONSE_CHARS
    shortened = text.removesuffix("\n" + tail)
    assert shortened.endswith("…"), shortened[-60:]
    assert shortened[:-1].split()[-1] == word, shortened[-60:]
