"""R5-S5: the Turkish receipt forms the suggestion cards need do not move anyone else's receipts.

The cards' prompts are Turkish sentences ("3. klibin sesini kapat", "2. ve 3. kliplerden daha
iyisini tut"), and a Turkish creator's receipts for them need the softened k (muzik -> muzigi,
klip -> klibi), the count before the noun and "ses efektleri" as sound effects, not the volume.
Those edits to
``kria/editor_receipts`` first landed unconditionally and changed the receipts of every Turkish
creator with every suggestion flag off ("ses efektleri ekle" used to be a sound-effects AND a volume
ask; "2. klibi sil" an ask no dimension named). Dark means byte-identical.

Failure catalogue (written first; each row is a test below):

  R1  flag off (``KRIA_SUGGESTION_PINNED_TAPS_ENABLED``): the receipts of 38 English and Turkish
      asks equal what origin/main produced, field for field (the dimensions the ask names, whether
      any dimension was named, the clip ordinal). The table below was captured from origin/main.
  R2  flag on: exactly the 12 Turkish asks listed in ``ON`` change, to the receipt the card's own
      prompt needs, and every other ask in the table is unchanged.

The table is the before/after: ``OFF`` is the receipt every creator had, ``ON`` lists only the
asks the cards' flag changes.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.editor_receipts import ask_shape

# ask -> (dimensions named, any dimension named, clip ordinal): captured from origin/main.
OFF = {
    "make the music quieter": (["music", "volume"], True, None),
    "turn down the volume": (["volume"], True, None),
    "add a whoosh on every cut": (["sfx"], True, None),
    "no sound effects please": (["sfx"], True, None),
    "remove the second clip": (["clips"], True, 2),
    "move the first clip to the end": (["reorder"], True, 1),
    "make the captions bigger": (["captions"], True, None),
    "crossfade between the clips": (["transition"], True, None),
    "mute clip 3": (["volume"], True, 3),
    "Keep the better of clips 2 and 3": (["audio"], False, None),
    "Cut the filler words": (["audio"], False, None),
    "shorten the video": (["length"], True, None),
    "sesi kıs": (["volume"], True, None),
    "sesi aç": (["volume"], True, None),
    "ses çok yüksek": (["volume"], True, None),
    "müzik ekle": (["music"], True, None),
    "müzik olmasın": (["music"], True, None),
    "şarkıyı değiştir": (["music"], True, None),
    "altyazıyı büyüt": (["captions"], True, None),
    "geçişleri kaldır": (["transition"], True, None),
    "klipleri kısalt": (["length"], True, None),
    "ilk klibi sona koy": (["audio"], False, None),
    "ikinci klip": (["audio"], False, 2),
    "klipi sil": (["clips"], True, None),
    "3. klip": (["audio"], False, None),
    "Dolgu sözcüklerini kes": (["audio"], False, None),
    "fon sesi çok yüksek": (["volume"], True, None),
    "efekt ekle": (["sfx"], True, None),
    "ses efektleri ekle": (["sfx", "volume"], True, None),
    "kliplerde ses efekti": (["sfx", "volume"], True, None),
    "ses efekti olmasın": (["sfx", "volume"], True, None),
    "müziği kıs": (["audio"], False, None),
    "Müziği kıs": (["audio"], False, None),
    "fon müziğini kapat": (["audio"], False, None),
    "müziğin sesini azalt": (["volume"], True, None),
    "2. klibi sil": (["audio"], False, None),
    "3. klibin sesini kapat": (["volume"], True, None),
    "2. ve 3. kliplerden daha iyisini tut": (["audio"], False, None),
}
# The asks whose receipt the flag changes, and what they become.
ON = {
    "ilk klibi sona koy": (["audio"], False, 1),
    "3. klip": (["audio"], False, 3),
    "ses efektleri ekle": (["sfx"], True, None),
    "kliplerde ses efekti": (["sfx"], True, None),
    "ses efekti olmasın": (["sfx"], True, None),
    "müziği kıs": (["music"], True, None),
    "Müziği kıs": (["music"], True, None),
    "fon müziğini kapat": (["music"], True, None),
    "müziğin sesini azalt": (["music", "volume"], True, None),
    "2. klibi sil": (["clips"], True, 2),
    "3. klibin sesini kapat": (["volume"], True, 3),
    "2. ve 3. kliplerden daha iyisini tut": (["audio"], False, 3),
}


def shape(text: str) -> tuple[list[str], bool, int | None]:
    req = BriefRequirement(id="r1", kind="audio", scope="global", description=text)
    found = ask_shape(req, [])
    return sorted(g.label for g in found.groups), found.cued, found.ordinal


@pytest.mark.parametrize("text", list(OFF))
def test_r1_flag_off_the_receipts_are_what_origin_main_produced(monkeypatch, text):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", False)
    assert shape(text) == OFF[text]


@pytest.mark.parametrize("text", list(OFF))
def test_r2_flag_on_only_the_listed_turkish_asks_change(monkeypatch, text):
    monkeypatch.setattr(settings, "kria_suggestion_pinned_taps_enabled", True)
    want = ON[text] if text in ON else OFF[text]
    assert shape(text) == want
