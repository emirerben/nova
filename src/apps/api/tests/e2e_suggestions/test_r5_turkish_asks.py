"""R5-D: the creator's own words about the music, in the forms a Turkish creator types them.

The creator's request wins (KRI-129): when the brief already says something about the music, the
music card ("Müziği kıs") must not be offered over it. Where the brief writer records no typed fact
the creator's own description decides, over the Turkish-aware fold (``app/text_fold``). Turkish
is agglutinative and softens a final consonant before a vowel suffix (the k of "müzik" becomes the
ğ of "müziği"), and the fold writes ğ as g.

Failure catalogue (written first; each row is a test below):

  T1  the softened-k paradigm of "müzik" is not read as a music ask. Every case and possessive
      ("müziği", "müziğin", "müziğe", "müziğimi", "müziğinden", "müziğiyle") plus the unsoftened
      forms ("müzikte", "müzikten", "müzikle", "müziksiz", "müzikler"), in lower, upper and
      ASCII-typed spelling, withholds the card on the REAL GET (not just in the table).
  T2  the other lanes' Turkish asks, filed the way the brief writer files them (KRI-667: by kind
      and typed fact, not by a word list), withhold that lane's card and only that card: captions
      ("altyazıyı kapat" with a ``captions`` fact), sound effects (an ``audio`` requirement naming
      them), transitions (the one lane with no typed signal: the description is read once).
  T3  the card is not withheld too widely. An ask about another lane, filed under its own kind
      ("klipleri kısalt", "videoyu 20 saniye yap", "altyazı ekle", "geçiş olmasın", "başlığı
      değiştir", "ilk klibi sona koy"), leaves the music card on offer. (Filing every one of them
      as ``audio``, which the writer never does, withholds the music card by design: the safe
      direction. The first version of this test did that and failed for it.)
  T4  a volume ask with a word between the noun and the verb ("sesi BİRAZ kıs", "sesi ÇOK kıs",
      "sesi TAMAMEN kapat", "ses çok yüksek") is an ``audio`` requirement like "sesi kıs": the kind
      decides, so none leaks (GAP-15, closed).
  T5  English: the same lane, the same rule ("make the music quieter", "turn down the volume").
  T6  under a Turkish volume ask every card the GET still serves executes (no dead card), and the
      music card is not among them: the creator's request wins (KRI-129).

The footage is the Turkish twin of the Nermin edit (``nermin-matcha-update/tr``): a voice, no
title, captions off, hard cuts, a music track, so the editor profile serves hook, captions, music,
transition and sfx. Each form is one brief requirement written through the brief module's own
update type, then served by the unmodified GET -> worker -> GET.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefUpdate
from tests.e2e_suggestions import scenes as s
from tests.e2e_suggestions.flow import tap_every_card
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

TR = "nermin-matcha-update/tr"
EN = "nermin-matcha-update"

# T1: every form of "müzik" the creator can type, each of which must read as a music ask.
SOFTENED_K = [
    "müziği çok kıs",
    "müziğin sesini azalt",
    "müziğe gerek yok",
    "müziğimi değiştir",
    "müziğimizi değiştir",
    "müziğinden vazgeç",
    "müziğiyle ilgili bir şey istemiyorum",
    "müziğini kıs",
    "fon müziği ekle",
    "fon müziğini kapat",
    "arka plandaki müziği kıs",
    "müzikte bir sorun var",
    "müzikten rahatsızım",
    "müzikle ilgili",
    "müziksiz olsun",
    "müzikler çok yüksek",
    "MÜZİĞİ KAPAT",
    "MÜZİĞİN SESİNİ AZALT",
    "muzigi kis",
    "muzigin sesini azalt",
    "muzikten vazgec",
]
# T2: the other lanes' asks, as the brief writer files them: (lane card, kind, description, facts).
OTHER_LANES = [
    ("captions", "style", "altyazıyı kapat", {"captions": "off"}),
    ("captions", "style", "altyazının boyutunu büyüt", {"caption_size": "large"}),
    ("captions", "style", "altyazılar çok küçük", {"caption_size": "large"}),
    ("sfx", "audio", "efektleri kaldır", {}),
    ("sfx", "audio", "ses efektini kapat", {}),
    ("sfx", "audio", "ses efekti ekle", {}),
    ("transition", "style", "geçişleri kaldır", {}),
    ("transition", "style", "geçişi sert yap", {}),
    ("transition", "style", "yumuşak geçişler ekle", {}),
]
# T3: not about the music, filed under the kind the writer files it under: (kind, text, facts).
NOT_ABOUT_MUSIC = [
    ("timing", "klipleri kısalt", {}),
    ("timing", "videoyu 20 saniye yap", {"duration_s": 20}),
    ("style", "altyazı ekle", {"captions": "on"}),
    ("style", "geçiş olmasın", {}),
    ("text", "başlığı değiştir", {}),
    ("order", "ilk klibi sona koy", {}),
    ("style", "kameraya konuşuyorum", {}),
]
# T4: the volume ask with a word between the noun and the verb, and bare complaints about volume.
VOLUME_ASKS = [
    "sesi biraz kıs",
    "sesi çok kıs",
    "sesi biraz azalt",
    "sesi daha düşük yap",
    "sesi daha kısık olsun",
    "sesi tamamen kapat",
    "sesi biraz düşür",
    "ses çok yüksek",
    "sesi çok yüksek olmasın",
    "arka plan sesi çok yüksek",
    "fon sesi çok yüksek",
    "sesin seviyesini düşür",
    "daha az ses",
    "kısık ses olsun",
]
ENGLISH_ASKS = [
    "make the music quieter",
    "turn down the volume",
    "lower the background music",
    "I don't want any song",
    "music too loud",
    "drop the music a bit",
]


def ask(text: str) -> BriefUpdate:
    return BriefUpdate(operation="add", kind="audio", scope="global", description=text, facts={})


def typed(kind: str, text: str, facts: dict) -> BriefUpdate:
    return BriefUpdate(
        operation="add", kind=kind, scope="global", description=text, facts=dict(facts)
    )


async def kinds_with(world, key: str, text: str | None) -> list[str]:  # noqa: ANN001
    scene = s.build(world, key, EDITOR)
    if text is not None:
        k.write_brief(scene.seeded, ask(text))
    return s.kinds_of(await scene.phone.serve())


async def kinds_with_updates(world, key: str, *updates: BriefUpdate) -> list[str]:  # noqa: ANN001
    scene = s.build(world, key, EDITOR)
    if updates:
        k.write_brief(scene.seeded, *updates)
    return s.kinds_of(await scene.phone.serve())


@pytest.mark.asyncio
async def test_t1_the_softened_k_paradigm_withholds_the_music_card(talking_world):
    rec = Recorder("turkish T1 softened-k forms of müzik")
    control = await kinds_with(talking_world, TR, None)
    assert "music" in control, f"the control did not serve the music card: {control}"
    leaks = []
    for text in SOFTENED_K:
        now = await kinds_with(talking_world, TR, text)
        if "music" in now or [x for x in control if x != "music"] != [
            x for x in now if x != "music"
        ]:
            leaks.append((text, now))
    rec.negative(
        "T1 every form of müzik withholds the music card and only that card",
        TR,
        f"{len(SOFTENED_K)} forms, none leaks",
        not leaks,
        f"control {control}; leaks {leaks}",
    )
    rec.sections["forms_checked"] = len(SOFTENED_K)
    rec.finish()


@pytest.mark.asyncio
async def test_t2_t3_other_lanes_keep_their_own_asks_and_do_not_over_withhold(talking_world):
    rec = Recorder("turkish T2/T3 other lanes")
    control = await kinds_with_updates(talking_world, TR)
    assert "music" in control, f"the control did not serve the music card: {control}"
    wrong = []
    for lane, kind, text, facts in OTHER_LANES:
        now = await kinds_with_updates(talking_world, TR, typed(kind, text, facts))
        if lane in now or "music" not in now:
            wrong.append((lane, text, now))
    rec.negative(
        "T2 each lane's own Turkish ask withholds that lane's card, and the music card stays",
        TR,
        f"{len(OTHER_LANES)} asks; the lane card is gone, music is not",
        not wrong,
        f"control {control}; wrong {wrong}",
    )
    over = []
    for kind, text, facts in NOT_ABOUT_MUSIC:
        now = await kinds_with_updates(talking_world, TR, typed(kind, text, facts))
        if "music" not in now:
            over.append((text, now))
    rec.negative(
        "T3 an ask about something else, under its own kind, leaves the music card on offer",
        TR,
        f"{len(NOT_ABOUT_MUSIC)} asks, music served each time",
        not over,
        f"withheld wrongly: {over}",
    )
    rec.finish()


@pytest.mark.asyncio
async def test_t4_a_volume_ask_with_a_word_in_the_middle_is_the_same_ask(talking_world):
    rec = Recorder("turkish T4 volume asks")
    leaks = []
    for text in VOLUME_ASKS:
        now = await kinds_with(talking_world, TR, text)
        if "music" in now:
            leaks.append(text)
    rec.negative(
        "T4 'sesi biraz kıs' and its kin withhold the music card like 'sesi kıs' does",
        TR,
        f"{len(VOLUME_ASKS)} forms, none leaks",
        not leaks,
        f"the music card is still offered over: {leaks}",
    )
    rec.sections["volume_forms_checked"] = len(VOLUME_ASKS)
    rec.finish()


@pytest.mark.asyncio
async def test_t5_english_music_asks_withhold_the_card(talking_world):
    rec = Recorder("turkish T5 english asks")
    control = await kinds_with(talking_world, EN, None)
    assert "music" in control
    leaks = []
    for text in ENGLISH_ASKS:
        now = await kinds_with(talking_world, EN, text)
        if "music" in now:
            leaks.append(text)
    rec.negative(
        "T5 an English music or volume ask withholds the music card",
        EN,
        f"{len(ENGLISH_ASKS)} forms, none leaks",
        not leaks,
        f"leaks {leaks}",
    )
    rec.finish()


T6_ASKS = [
    (TR, "sesi biraz kıs"),
    (TR, "ses çok yüksek"),
    (TR, "parçayı değiştir"),
    (EN, "tone down the sound"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("key", "text"), T6_ASKS, ids=[f"{k}:{t}" for k, t in T6_ASKS])
async def test_t6_under_a_volume_ask_every_served_card_executes(
    talking_world, monkeypatch, key, text
):
    """The creator said something about the sound; the music card is gone and every card the GET
    still serves is tapped end to end (accepted, one real change, receipts, Undo, second tap)."""
    rec = Recorder(f"turkish T6 {key} {text}")
    scene = s.build(talking_world, key, EDITOR)
    k.write_brief(scene.seeded, ask(text))
    out = await scene.phone.serve()
    kinds = s.kinds_of(out)
    rec.negative(
        f"T6 {text!r}: the music card is withheld, the other cards are still offered",
        key,
        "no music; hook, captions, transition and sfx",
        "music" not in kinds and {"captions", "transition", "sfx"} <= set(kinds),
        f"served {kinds}",
    )
    await tap_every_card(
        rec,
        scene.phone,
        scene.scn,
        EDITOR,
        expect=kinds,
        state="volume-ask-in-force",
    )
    rec.finish()
