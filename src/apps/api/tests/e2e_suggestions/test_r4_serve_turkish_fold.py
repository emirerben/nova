"""R4-D: a Turkish music or volume ask withholds the music card in every form the creator types it.

The creator's request wins (KRI-129). The music dimension of ``suggestions/brief_asks`` knew
"müzik" but not the softened "müziği / müziğin / müziğe" (the k becomes ğ before a vowel suffix),
so "müziği çok kıs" left the card "Müziği kıs" on screen over a creator who had just asked for
exactly that. Volume asks that name the sound without the word "kıs" were missing the same way.

Failure catalogue (written first):

  D1  a softened-k form of "müzik" (accusative, genitive, dative, possessives, compounds). The
      fold turns ğ into g, so the dimension needs "muzig" next to "muzik".
  D2  a volume ask that says the verb in another way ("sesi azalt", "sesini düşür", "sesi kapat",
      "sesini yükselt"). Same dimension, same withholding.
  D3  the other spellings of the same ask: ASCII-typed ("muzigi kis"), shouting ("MÜZİĞİ KAPAT"),
      with a title-case İ.
  D4  too much: asks about other lanes must NOT withhold the music card ("klipleri kısalt",
      "ses efekti ekle", "altyazı ekle", "geçiş olmasın", "başlığı değiştir").
  D5  the other dimensions' Turkish words survive their own softening: "altyazıyı kapat",
      "geçişleri kaldır", "efektleri kaldır", "animasyonu kaldır" still withhold their card.

D1-D3 and D5 are tables over the dimension's own matcher; ``test_d_the_card_is_withheld_on_the_
real_get`` proves the same words reach the screen path (GET -> worker -> GET) on the Turkish twin
of the Nermin footage, with the control (no request) serving the card.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.brief import BriefUpdate
from app.services.suggestions import brief_asks
from app.text_fold import has_marker
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update/tr"

MUSIC_ASKS = [
    # D1: the k of "müzik" softens to ğ before a vowel.
    "müziği çok kıs",
    "Müziği kısmanı istiyorum",
    "müziği kapat",
    "müziğin sesini azalt",
    "müziğe gerek yok",
    "müziğini değiştir",
    "müziğimi seç",
    "fon müziği ekle",
    "arka plan müziği olmasın",
    "müzik olmasın",
    "müziksiz olsun",
    "şarkıyı değiştir",
    "şarkının sesi çok yüksek",
    # D2: the volume asks.
    "sesi kıs lütfen",
    "sesini kıs",
    "sesi azalt",
    "sesini azalt",
    "sesi düşür",
    "sesini düşür",
    "sesi artır",
    "sesini artır",
    "sesi yükselt",
    "sesini yükselt",
    "sesi kapat",
    "sesini kapat",
    # D3: spelling and casing.
    "muzigi kis",
    "muzigin sesini azalt",
    "MÜZİĞİ KAPAT",
    "SESİNİ KIS",
]
NOT_MUSIC_ASKS = [
    "klipleri kısalt",
    "videoyu 20 saniye yap",
    "ses efekti ekle",
    "ses efektleri olmasın",
    "altyazı ekle",
    "geçiş olmasın",
    "başlığı değiştir",
    "kameraya konuşuyorum",
    "ilk klibi sona koy",
]
OTHER_DIMENSIONS = [
    (brief_asks.CAPTIONS, "altyazıyı kapat"),
    (brief_asks.CAPTIONS, "altyazısız olsun"),
    (brief_asks.TRANSITION, "geçişleri kaldır"),
    (brief_asks.TRANSITION, "geçişi sert yap"),
    (brief_asks.SFX, "efektleri kaldır"),
    (brief_asks.SFX, "ses efektini kapat"),
    (brief_asks.TITLE_ANIMATION, "animasyonu kaldır"),
    (brief_asks.TITLE_ANIMATION, "animasyonlu olmasın"),
]


@pytest.mark.parametrize("text", MUSIC_ASKS)
def test_d1_d3_a_music_ask_in_any_form_is_a_music_ask(text):
    assert has_marker(text, brief_asks.MUSIC.words), f"{text!r} is not read as a music ask"


@pytest.mark.parametrize("text", NOT_MUSIC_ASKS)
def test_d4_an_ask_about_another_lane_is_not_a_music_ask(text):
    assert not has_marker(text, brief_asks.MUSIC.words), f"{text!r} is read as a music ask"


@pytest.mark.parametrize(
    ("dimension", "text"), OTHER_DIMENSIONS, ids=[f"{d.name}-{t}" for d, t in OTHER_DIMENSIONS]
)
def test_d5_the_other_dimensions_keep_their_turkish_words(dimension, text):
    assert has_marker(text, dimension.words), f"{text!r} is not read as a {dimension.name} ask"


def prefs() -> list[dict]:
    path = CASSETTES / f"{KEY.replace('/', '_')}.json"
    return json.loads(path.read_text(encoding="utf-8"))["prefs"]


async def served_kinds(world, *updates):  # noqa: ANN001, ANN002, ANN202
    scn = threads.SCENARIOS[KEY]
    seeded = threads.seed_thread(scn, EDITOR)
    if updates:
        k.write_brief(seeded, *updates)
    phone = Phone(seeded, world, prefs(), language=scn.language)
    return [c.kind for c in (await phone.serve()).suggestions]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    ["müziği çok kıs", "müziğin sesini azalt", "sesi azalt", "MÜZİĞİ KAPAT", "müziğe gerek yok"],
)
async def test_d_the_card_is_withheld_on_the_real_get(talking_world, text):
    rec = Recorder(f"negative D music ask {text}")
    ask = BriefUpdate(operation="add", kind="audio", scope="global", description=text, facts={})
    before = await served_kinds(talking_world)
    after = await served_kinds(talking_world, ask)
    rec.negative(
        f"the creator wrote {text!r}: the music card is withheld and only it",
        KEY,
        "music served without the request, absent with it, the other cards untouched",
        "music" in before and "music" not in after and [x for x in before if x != "music"] == after,
        f"without {before}; with {after}",
    )
    rec.finish()
