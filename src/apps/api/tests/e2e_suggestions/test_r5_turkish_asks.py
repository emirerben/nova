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
  T2  the other lanes' Turkish words break under the same softening. Each lane's own forms
      ("altyazıyı", "altyazının", "efektleri", "geçişi", "animasyonu") withhold that lane's card and
      only that card.
  T3  the card is withheld too widely. An ask about another lane, or about the length of a clip
      ("klipleri kısalt", "ses efekti ekle", "altyazı ekle", "geçiş olmasın"), leaves the music
      card on offer.
  T4  a volume ask with a word between the noun and the verb ("sesi BİRAZ kıs", "sesi ÇOK kıs",
      "sesi TAMAMEN kapat", "ses çok yüksek") is the same ask as "sesi kıs", which is read. Recorded
      as observed with every form that leaks (named gap).
  T5  English: the same lane, the same rule ("make the music quieter", "turn down the volume").

The footage is the Turkish twin of the Nermin edit (``nermin-matcha-update/tr``): a voice, no
title, captions off, hard cuts, a music track, so the editor profile serves hook, captions, music,
transition and sfx. Each form is one brief requirement written through the brief module's own
update type, then served by the unmodified GET -> worker -> GET.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefUpdate
from tests.e2e_suggestions import gaps
from tests.e2e_suggestions import scenes as s
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
# T2: the other lanes' forms. (lane card, text)
OTHER_LANES = [
    ("captions", "altyazıyı kapat"),
    ("captions", "altyazının boyutunu büyüt"),
    ("captions", "altyazılar çok küçük"),
    ("sfx", "efektleri kaldır"),
    ("sfx", "ses efektini kapat"),
    ("transition", "geçişleri kaldır"),
    ("transition", "geçişi sert yap"),
    ("transition", "yumuşak geçişler ekle"),
]
# T3: not about the music.
NOT_ABOUT_MUSIC = [
    "klipleri kısalt",
    "videoyu 20 saniye yap",
    "ses efekti ekle",
    "altyazı ekle",
    "geçiş olmasın",
    "başlığı değiştir",
    "ilk klibi sona koy",
    "kameraya konuşuyorum",
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


async def kinds_with(world, key: str, text: str | None) -> list[str]:  # noqa: ANN001
    scene = s.build(world, key, EDITOR)
    if text is not None:
        k.write_brief(scene.seeded, ask(text))
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
async def test_t2_t3_other_lanes_keep_their_own_words_and_do_not_over_withhold(talking_world):
    rec = Recorder("turkish T2/T3 other lanes")
    control = await kinds_with(talking_world, TR, None)
    wrong = []
    for lane, text in OTHER_LANES:
        now = await kinds_with(talking_world, TR, text)
        if lane in now or "music" not in now:
            wrong.append((lane, text, now))
    rec.negative(
        "T2 each lane's own Turkish forms withhold that lane's card, and the music card stays",
        TR,
        f"{len(OTHER_LANES)} forms; the lane card is gone, music is not",
        not wrong,
        f"control {control}; wrong {wrong}",
    )
    over = []
    for text in NOT_ABOUT_MUSIC:
        now = await kinds_with(talking_world, TR, text)
        if "music" not in now:
            over.append((text, now))
    rec.negative(
        "T3 an ask about something else leaves the music card on offer",
        TR,
        f"{len(NOT_ABOUT_MUSIC)} forms, music served each time",
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
        gap=gaps.TURKISH_VOLUME_ASK_LEAKS,
    )
    rec.sections["volume_forms_that_leak"] = leaks
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
