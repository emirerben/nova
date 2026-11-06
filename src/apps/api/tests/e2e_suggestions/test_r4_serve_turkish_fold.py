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

R5 (KRI-667) replaced the word tables that used to sit here: whether the creator asked about the
music is read from the requirement's kind (``audio``), not from Turkish suffix lists, so the forms
below no longer need to be enumerated (``test_r5_serve_brief_asks.py`` carries the wider table and
the typed rules). What stays is the proof on the real GET (GET -> worker -> GET) on the Turkish
twin of the Nermin footage, with the control (no request) serving the card.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.brief import BriefUpdate
from tests.e2e_suggestions import threads
from tests.e2e_suggestions.driver import Phone
from tests.e2e_suggestions.report import Recorder
from tests.e2e_suggestions.threads import EDITOR
from tests.tasks import kria_pinned_kinds_harness as k

CASSETTES = Path(__file__).resolve().parent / "cassettes"
KEY = "nermin-matcha-update/tr"


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
