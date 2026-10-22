"""KRI-793: "Captions for both voices, and spell these right: Oblea de la Casa, ..." gets a line.

Café kit T1 (Caro at Merope; prod 042c405c9, thread 32A5B608, jobs 0701fcc9 + f88555bc,
2026-10-21). Brief r5 = `style`, facts `{captions: "on"}`, description "Captions for both
voices, ensuring correct spellings: Oblea de la Casa, dulce de leche, leche condensada,
Merope". Across the held first draft and two approval cards r5 never appeared: no Done,
Partly or Couldn't. The phone Talking draft left every caption language / spelling ask to
the render (KRI-549), and the render never received the list: the spelling step only read
quoted single words from the raw request (KRI-631), so "Ole de la Casa" (the take's own
transcript) would have shipped.

Ways this can go wrong, written down before the code (each has a case here, in
`tests/tasks/test_kri793_talking_caption_spellings.py` (render) or in
`test_kri793_t1_every_row_gets_a_line_before_approval` (runtime, DB)):

Before approval
 1. The captions + spelling row is left out of the draft's checks, so it is silent (the bug).
 2. "spellings" (plural) is not a spelling cue, so no word is read.
 3. The list reader drops lowercase items ("dulce de leche") and cuts "Oblea de la Casa" to
    "Oblea" (two particles in a row).
 4. The line is a plain "Partly" that is not a format limit, so a first draft turns into the
    "should I make a simpler version?" question.
 5. The line says "Done" before any caption exists.
 6. "Both voices" is never answered (are off-camera lines captioned?).
 7. A Turkish chat gets English.
 8. The model's structured `facts.spellings` is ignored, or a malformed one crashes.
 9. A "none" caption style reads as "captions are off", though the Talking writer
    always captions.
10. Another row of the brief stays silent (an ask no checker judges before render).
11. Drafts that never reach the phone Talking writer change.

At render
12. The brief's words never reach the caption spelling step (the bug).
13. A several-word name is never matched (the spelling step went word by word).
14. A look-alike that is not the name is rewritten, or the word count / timings change.
15. Caption correction runs after the spelling step and undoes it.
16. An unreadable brief binding fails the render.

After render
17. "Meropa" in one cue beside "Merope" in three others reads "spelled as you wrote them".
18. A name that is never said reads as a miss instead of "doesn't appear".

Replayed from the real brief and transcripts (fixture `kri793_t1_caro`), no model calls.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    PlanFacts,
    _spelled_names,
    brief_caption_spellings,
    build_receipts,
    check_requirement,
    is_format_limit,
    is_judged,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    plan_facts_from_strategy,
    reply_from_receipts,
    requirements_to_check_at_draft,
)
from app.kria.reply_language import bind_reply_language, release_reply_language

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "kri793_t1_caro"
        / "t1_brief_and_speech.json"
    ).read_text(encoding="utf-8")
)
T1_BRIEF = CreativeBrief.model_validate(FIXTURE["brief"])
R5 = next(req for req in T1_BRIEF.requirements if req.id == "r5")
SPELLINGS = ("Oblea de la Casa", "dulce de leche", "leche condensada", "Merope")
# The approved draft's strategy (job f88555bc `creator_strategy`), trimmed to what is read.
T1_STRATEGY = {
    "edit_format": "subtitled",
    "audio_strategy": "original_audio",
    "direction": "native",
    "caption_style": "clean",
    "pacing": "balanced",
    "target_duration_s": 35,
    "opening_title": "Colombian dessert, Ecuadorian hands",
}
PROXY = "u/analysis-proxy-ios-B22C7409.mp4"
PLAN_LINE = (
    "Every voice in your take gets captions, off-camera ones too; I'll spell Oblea de la "
    "Casa, dulce de leche, leche condensada and Merope the way you wrote them and check them "
    "in the finished video"
)


def _draft_facts(strategy: dict | None = None, **changes) -> PlanFacts:
    """The facts a phone Talking strategy draft is checked against (`kria_runtime`)."""
    facts = plan_facts_from_strategy(strategy or T1_STRATEGY)
    return dataclasses.replace(
        facts,
        phone_talking=True,
        caption_words_at_render=True,
        take_duration_s=FIXTURE["take_duration_s"],
        **changes,
    )


def _one(req: BriefRequirement, facts: PlanFacts):
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt


@pytest.fixture
def phone_talking_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)


# -- 1, 4, 5, 6: the T1 row before approval ------------------------------------------------


def test_the_t1_captions_row_is_kept_for_the_draft_check(phone_talking_on):
    kept = requirements_to_check_at_draft(
        T1_BRIEF.live(),
        creator_id="b13f779a-4534-4167-8fa9-2686a28f4d9a",
        strategy=T1_STRATEGY,
        item_edit_format="subtitled",
        clip_paths=(PROXY,),
    )
    # On main r5 was dropped here (KRI-549 left it to the render) and nothing said a word.
    assert [req.id for req in kept] == ["r1", "r2", "r3", "r4", "r5", "r6"]


def test_the_t1_captions_row_names_the_four_spellings_and_both_voices():
    receipt = _one(R5, _draft_facts())
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert receipt.reason == PLAN_LINE
    # An answer about the format, never the "simpler version?" question, never "Done".
    assert is_format_limit(receipt.reason)
    assert not needs_creator_choice(receipt)
    reply = reply_from_receipts(T1_BRIEF, [receipt])
    assert (
        "- Partly: Captions for both voices, ensuring correct spellings: Oblea de la Casa, "
        f"dulce de leche, leche condensada, Merope ({PLAN_LINE})"
    ) in reply


def test_the_t1_draft_answers_every_live_row():
    receipts = build_receipts(T1_BRIEF.live(), _draft_facts(), include_unchecked=True)
    assert [r.requirement_id for r in receipts] == ["r1", "r2", "r3", "r4", "r5", "r6"]
    reply = reply_from_receipts(T1_BRIEF, receipts)
    for req in T1_BRIEF.live():
        assert req.text() in reply, req.id


def test_turkish_chat_reads_the_line_in_turkish():
    token = bind_reply_language("tr")
    try:
        receipt = _one(R5, _draft_facts())
    finally:
        release_reply_language(token)
    assert receipt.reason == (
        "Çekimindeki her ses altyazıya girer, kamera dışındaki sesler de; şunları senin "
        "yazdığın gibi yazıp bitmiş videoda kontrol edeceğim: Oblea de la Casa, dulce de leche, "
        "leche condensada ve Merope"
    )
    assert is_format_limit(receipt.reason)
    assert is_judged(R5, receipt)


# -- 2, 3, 8: which words ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "facts", "names"),
    [
        (R5.description, {"captions": "on"}, SPELLINGS),
        # The creator's own sentence (the brief reworded it).
        (
            "Captions for both voices, and spell these right: Oblea de la Casa, dulce de "
            "leche, leche condensada, Merope. Title next",
            {},
            SPELLINGS,
        ),
        ("I want Juan de la Cruz spelled correctly in the captions", {}, ("Juan de la Cruz",)),
        # The model's structured list wins over the wording (KRI-667).
        (
            "captions please",
            {"spellings": ["Merope", " Oblea  de la Casa "]},
            ("Merope", "Oblea de la Casa"),
        ),
        ("captions please", {"spellings": "Merope"}, ("Merope",)),
        # A malformed fact is ignored, never a crash; the wording decides.
        (R5.description, {"spellings": [3, None, ""]}, SPELLINGS),
        # A lone lowercase item after a colon is not a list of names.
        ("spelling: make sure it is right", {}, ()),
        # The list ends at the first item that is not a name or a short phrase.
        ("correct spelling: Merope not Meropa", {}, ("Merope",)),
        ("spelling: Merope, and keep it short", {}, ("Merope",)),
        ("Spelling: names must be correct, and captions on", {}, ()),
        # The model's words are read as a listed name is: no edge punctuation, no ending.
        ("captions please", {"spellings": ["Kadıköy'ü", "Merope."]}, ("Kadıköy", "Merope")),
    ],
)
def test_the_words_to_spell(description, facts, names):
    req = BriefRequirement(
        id="c", kind="style", scope="global", description=description, facts=facts
    )
    assert _spelled_names(req) == names


def test_the_render_reads_the_same_list_from_the_brief():
    assert brief_caption_spellings(T1_BRIEF) == SPELLINGS
    no_captions = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="style",
                scope="global",
                description="no captions, spell Merope right",
                facts={"captions": "off"},
            )
        ],
    )
    assert brief_caption_spellings(no_captions) == ()
    assert brief_caption_spellings(None) == ()


def test_a_spellings_fact_alone_makes_a_captions_ask():
    req = BriefRequirement(
        id="c",
        kind="style",
        scope="global",
        description="get the names right",
        facts={"spellings": ["Merope"]},
    )
    receipt = _one(req, _draft_facts())
    assert receipt.reason == (
        "Every voice in your take gets captions, off-camera ones too; I'll spell Merope the "
        "way you wrote it and check it in the finished video"
    )


# -- 9, 11 and the other shapes of the ask --------------------------------------------------


def test_a_none_caption_style_still_gets_the_plan_line():
    # The phone Talking writer captions every take; the strategy's style only picks word or
    # sentence lines, so "none" must not turn into a false "captions are off" miss.
    receipt = _one(R5, _draft_facts({**T1_STRATEGY, "caption_style": "none"}))
    assert receipt.reason == PLAN_LINE
    assert not needs_creator_choice(receipt)


def test_a_language_only_ask_says_the_language():
    req = BriefRequirement(id="c", kind="style", scope="global", description="English subtitles")
    receipt = _one(req, _draft_facts())
    assert receipt.reason == (
        "Every voice in your take gets captions, off-camera ones too; they'll be in English, "
        "and I'll check that in the finished video"
    )
    assert not needs_creator_choice(receipt)


def test_language_and_names_together():
    req = BriefRequirement(
        id="c",
        kind="text",
        scope="global",
        description=(
            "altyazılar Türkçe olsun; Moda, Bahariye, Yeldeğirmeni ve Kadıköy doğru yazılsın"
        ),
    )
    receipt = _one(req, _draft_facts())
    assert receipt.reason == (
        "Every voice in your take gets captions, off-camera ones too; they'll be in Turkish; "
        "I'll spell Moda, Bahariye, Yeldeğirmeni and Kadıköy the way you wrote them and check "
        "them in the finished video"
    )


def test_a_spelling_ask_without_readable_words_asks_for_them():
    req = BriefRequirement(
        id="c", kind="text", scope="global", description="add captions with the exact names"
    )
    receipt = _one(req, _draft_facts())
    assert receipt.reason == (
        "Every voice in your take gets captions, off-camera ones too; write out the words to "
        "spell and I'll spell them your way"
    )
    assert not needs_creator_choice(receipt)


def test_word_by_word_against_sentence_captions_is_a_real_miss():
    req = BriefRequirement(
        id="c",
        kind="style",
        scope="global",
        description="karaoke captions, spell Merope right",
        facts={"captions": "word_by_word"},
    )
    receipt = _one(req, _draft_facts())
    assert receipt.reason == "Captions are on as full sentences, not word by word."


def test_drafts_outside_the_phone_talking_writer_keep_their_answer():
    # A cloud render's draft judges "captions on" against its caption style, as on main.
    facts = plan_facts_from_strategy(T1_STRATEGY)
    assert facts.caption_words_at_render is False
    receipt = check_requirement(R5, facts)
    assert (receipt.status, receipt.reason) == ("met", None)


# -- 17, 18: after render ------------------------------------------------------------------


def _variant(lines: tuple[str, ...]) -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_language": "en",
        "voiceover_caption_style": "sentence",
        "caption_cues": [
            {"text": text, "start_s": float(i), "end_s": i + 0.9} for i, text in enumerate(lines)
        ],
    }


# The finished captions as the Talking writer now writes them (see the render test).
T1_LINES = (
    "Hi, I'm Carolina from Ecuador.",
    "And this is the Oblea de la Casa.",
    "It's wafer cookie with red no, with blackberry jam, cheese, dulce de leche,",
    "leche condensada and whipped cream.",
    "And where we are right now is Merope, how are you going to...",
    "Merope, it's a Colombian coffee and restaurant and Merope means god of honey.",
    "Yeah, we definitely recommend coming to Merope, right?",
)


def _rendered(lines: tuple[str, ...]):
    return _one(R5, plan_facts_from_phone_variant(_variant(lines)))


def test_the_finished_captions_are_met_when_every_word_is_spelled_right():
    receipt = _rendered(T1_LINES)
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == (
        "Oblea de la Casa, dulce de leche, leche condensada and Merope are spelled as you "
        "wrote them."
    )


@pytest.mark.parametrize(
    ("line", "heard", "name"),
    [
        (4, "Meropa", "Merope"),  # the analysis transcript's hearing of the first one
        (1, "Ole de la Casa", "Oblea de la Casa"),  # the transcript agent's hearing
    ],
)
def test_one_misheard_cue_beside_right_ones_is_a_named_miss(line, heard, name):
    lines = list(T1_LINES)
    lines[line] = lines[line].replace(name, heard, 1)
    receipt = _rendered(tuple(lines))
    assert (receipt.status, receipt.verification) == ("partial", "checked")
    assert f'The captions write "{heard}" for {name}.' in receipt.reason
    assert "fix that in the editor" in receipt.reason


def test_a_word_never_said_is_not_called_misspelled():
    lines = tuple(line for line in T1_LINES if "leche condensada" not in line)
    receipt = _rendered(lines)
    assert receipt.status == "partial"
    assert receipt.reason.startswith(
        "leche condensada doesn't appear in the captions, so I couldn't check its spelling."
    )
    assert "The captions write" not in receipt.reason
