"""R5-S4: "the creator already asked" is read from the brief's typed data, not from their words.

KRI-667: nothing in the server guesses what a creator meant from word lists (every list needs a
twin per language and misses a paraphrase: this lane's own GAP-15 is a Turkish verb that sits one
word further from its noun than the list allows). The brief writer records the meaning; the server
reads the field. ``suggestions/brief_asks`` therefore decides from a requirement's KIND and FACTS.
Only the dimension with no typed signal at all (transitions) falls back to the shared fold, and
that reader is registered with the raw-chat-text guard.

Failure catalogue (written first; each row is a test below):

  A1  a volume ask the word list misses: "sesi biraz kıs", "sesi çok kıs", "sesi tamamen kapat",
      "ses çok yüksek", "daha az ses", "kısık ses olsun", "parçayı değiştir", "melodiyi değiştir",
      "ses olmasın", "sessiz olsun" and the English "tone down the sound", "mute the audio",
      "keep it quiet", "no background track". Recorded as an ``audio`` requirement (what the brief
      writer files every volume, music and sound ask under), each withholds the music card on the
      REAL GET, in both languages, whatever the wording, with no typed fact.
  A2  the music card is withheld by an ``audio`` requirement whatever it says (the kind decides),
      and by NO other kind: a title, an order, a timing or a style ask with the very same words
      leaves it on offer. The words are not read.
  A3  music against sound effects (both are ``audio``): an ``audio`` requirement that names sound
      effects withholds the sound-effects card and not the music card, any other ``audio``
      requirement withholds the music card and not the sound-effects card, and no other lane's card
      follows the kind. The names are the only words read for the split.
  A4  the typed facts still decide for the dimensions that have them: ``captions`` (any
      ``caption*`` fact), ``animation``, ``audio_intent: no_music``, ``music*``. A requirement
      that carries no fact and an unrelated kind is not an ask about them, in any wording.
  A5  a superseded ask is not an ask (the brief's own ``live()``).
  A6  transitions have no typed signal: the creator's description is read once, over the shared
      Turkish-aware fold (a gap-tolerant marker for the verb-last order), and ONLY there. Hard cuts,
      crossfade, "geçiş olmasın" withhold the transition card; "klipleri kısalt" does not.
  A7  the word readers are registered: every function that reads a description is on the raw chat
      text allow-list (tests/test_raw_chat_text_readers.py); only the sound-effects split and the
      transitions carry words.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief
from app.services.suggestions import brief_asks
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.test_r5_serve_common import build, kinds_of
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

TR = "nermin-matcha-update/tr"
EN = "nermin-matcha-update"

VOLUME_ASKS = {
    TR: [
        "sesi biraz kıs",
        "sesi çok kıs",
        "sesi tamamen kapat",
        "sesi daha düşük yap",
        "sesi biraz azalt",
        "sesi biraz düşür",
        "ses çok yüksek",
        "arka plan sesi çok yüksek",
        "fon sesi çok yüksek",
        "sesin seviyesini düşür",
        "daha az ses",
        "kısık ses olsun",
        "sesi daha kısık olsun",
        "sesi çok yüksek olmasın",
        "parçayı değiştir",
        "melodiyi değiştir",
        "ses olmasın",
        "sessiz olsun",
    ],
    EN: [
        "tone down the sound",
        "mute the audio",
        "keep it quiet",
        "no background track",
        "make the music quieter",
        "turn down the volume",
        "lower the background music",
        "I don't want any song",
    ],
}


def ask(kind: str, text: str, **facts) -> BriefUpdate:  # noqa: ANN003
    return BriefUpdate(operation="add", kind=kind, scope="global", description=text, facts=facts)


async def served(world, key: str, *updates: BriefUpdate) -> list[str]:  # noqa: ANN001
    scene = build(world, key, EDITOR)
    if updates:
        k.write_brief(scene.seeded, *updates)
    return kinds_of(await scene.phone.serve())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "text"),
    [(key, text) for key, texts in VOLUME_ASKS.items() for text in texts],
    ids=[f"{key}:{text}" for key, texts in VOLUME_ASKS.items() for text in texts],
)
async def test_a1_a_volume_or_music_ask_withholds_the_music_card_in_any_wording(
    talking_world, key, text
):
    rec = Recorder(f"R5 brief asks A1 {key} {text}")
    control = await served(talking_world, key)
    assert "music" in control, f"the control did not serve the music card: {control}"
    now = await served(talking_world, key, ask("audio", text))
    rec.negative(
        f"A1 the creator wrote {text!r}: the music card is withheld and only it",
        key,
        "music absent, the other cards untouched",
        "music" not in now and [x for x in control if x != "music"] == now,
        f"without {control}; with {now}",
    )
    rec.finish()


NOT_AUDIO = [
    ("order", "mute the audio"),
    ("text", "sesi biraz kıs"),
    ("timing", "make the music quieter"),
    ("style", "müziği kapat"),
    ("select", "tone down the sound"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "text"), NOT_AUDIO, ids=[f"{kind}:{text}" for kind, text in NOT_AUDIO]
)
async def test_a2_the_words_are_not_read_only_the_kind(talking_world, kind, text):
    rec = Recorder(f"R5 brief asks A2 {kind} {text}")
    control = await served(talking_world, TR)
    now = await served(talking_world, TR, ask(kind, text))
    rec.negative(
        f"A2 a {kind!r} requirement that says {text!r} is not an audio ask",
        TR,
        "the music card stays on offer",
        "music" in now,
        f"without {control}; with {now}",
    )
    rec.finish()


def requirement(kind: str, text: str | None, **facts) -> BriefRequirement:  # noqa: ANN003
    return BriefRequirement(id="r1", kind=kind, scope="global", description=text, facts=facts or {})


def ctx_with(*reqs: BriefRequirement):  # noqa: ANN202
    from app.services.suggestions.context import SuggestionContext

    brief = CreativeBrief(version=1, requirements=list(reqs))
    return SuggestionContext(
        thread_id="t", state="ready", language="tr", kind_inputs={brief_asks.BRIEF_KEY: brief}
    )


TYPED = [
    # (dimension, requirement that IS an ask, requirement that is NOT)
    (
        brief_asks.CAPTIONS,
        requirement("style", "x", captions="off"),
        requirement("style", "no captions please, hold off on any subtitles, altyazı eklemeyelim"),
    ),
    (
        brief_asks.CAPTIONS,
        requirement("style", "x", caption_size="small"),
        requirement("text", "altyazı"),
    ),
    (
        brief_asks.TITLE_ANIMATION,
        requirement("text", "x", animation="typewriter"),
        requirement("style", "no animation on the title, başlık animasyonlu olsun"),
    ),
    (
        brief_asks.MUSIC,
        requirement("audio", "x", audio_intent="no_music"),
        requirement("style", "no music, müzik olmasın"),
    ),
    (
        brief_asks.MUSIC,
        requirement("style", "x", music_level="low"),
        requirement("order", "music"),
    ),
    (
        brief_asks.SFX,
        requirement("audio", "x", licensed_sfx="whoosh"),
        requirement("text", "ses efekti ekle, add a whoosh on every cut"),
    ),
    (
        brief_asks.TRANSITION,
        requirement("style", "x", transition="crossfade"),
        requirement("order", "start with the harbour shot"),
    ),
]


@pytest.mark.parametrize("dimension,ask_req,other", TYPED, ids=lambda v: getattr(v, "name", None))
def test_a3_a4_typed_facts_and_kinds_decide(dimension, ask_req, other):
    assert brief_asks.spoke(ctx_with(ask_req), dimension), f"{dimension.name}: typed ask missed"
    assert not brief_asks.spoke(ctx_with(other), dimension), f"{dimension.name}: words were read"


def test_a3_music_against_sound_effects_is_the_only_split_of_the_audio_kind():
    music = requirement("audio", "make it calmer")
    assert brief_asks.spoke(ctx_with(music), brief_asks.MUSIC)
    assert not brief_asks.spoke(ctx_with(music), brief_asks.SFX)
    for text in ("no sound effects please", "ses efektleri olmasın", "add a whoosh on every cut"):
        sfx = requirement("audio", text)
        assert brief_asks.spoke(ctx_with(sfx), brief_asks.SFX), text
        assert not brief_asks.spoke(ctx_with(sfx), brief_asks.MUSIC), text
    for other in (brief_asks.CAPTIONS, brief_asks.TRANSITION, brief_asks.TITLE_ANIMATION):
        assert not brief_asks.spoke(ctx_with(music), other), other.name


def test_a5_a_superseded_ask_is_not_an_ask():
    dead = requirement("audio", "sesi biraz kıs").model_copy(update={"status": "superseded"})
    assert not brief_asks.spoke(ctx_with(dead), brief_asks.MUSIC)


TRANSITION_ASKS = [
    "hard cuts only, no transitions",
    "crossfade between the clips",
    "keep it as jump cuts",
    "geçiş olmasın",
    "geçişleri kaldır",
    "geçişi sert yap",
    "yumuşak geçişler ekle",
    "GEÇİŞ OLMASIN",
    "gecis olmasin",
]
NOT_TRANSITION = [
    "klipleri kısalt",
    "videoyu 20 saniye yap",
    "ses efekti ekle",
    "altyazı ekle",
    "başlığı değiştir",
    "ilk klibi sona koy",
    "kameraya konuşuyorum",
]


@pytest.mark.parametrize("text", TRANSITION_ASKS)
def test_a6_a_transition_ask_is_read_once_over_the_shared_fold(text):
    assert brief_asks.spoke(ctx_with(requirement("style", text)), brief_asks.TRANSITION)


@pytest.mark.parametrize("text", NOT_TRANSITION)
def test_a6_another_lane_is_not_a_transition_ask(text):
    assert not brief_asks.spoke(ctx_with(requirement("style", text)), brief_asks.TRANSITION)


def test_a6_the_literal_is_never_read():
    on_screen = BriefRequirement(
        id="r1", kind="text", scope="global", literal="Hard cuts only", description="the title"
    )
    assert not brief_asks.spoke(ctx_with(on_screen), brief_asks.TRANSITION)


def test_a7_only_the_two_splits_without_a_typed_signal_carry_words():
    dims = (
        brief_asks.CAPTIONS,
        brief_asks.MUSIC,
        brief_asks.SFX,
        brief_asks.TRANSITION,
        brief_asks.TITLE_ANIMATION,
    )
    assert {d.name for d in dims if d.words or d.kind_needs or d.kind_not} == {
        "music",
        "sfx",
        "transition",
    }
    assert not brief_asks.CAPTIONS.words and not brief_asks.TITLE_ANIMATION.words
