"""R4-B: the hook copy gate's lookalike-letter check reads through the shared fold, unchanged.

The check used its own ``unicodedata.normalize("NFD", ...)``, which the KRI-786 guard
(``tests/kria/test_single_text_fold.py``) counts as a new private Turkish fold. It now reads
``app.text_fold.strip_marks``. The refactor must not move a single verdict, because this check is
what keeps a lookalike letter ("ᴏ", "ɡ", a fullwidth "Ｏ") out of the words that reach the screen.

Failure catalogue (written first):

  B1  the shared fold is looser than the old check. ``strip_marks`` is NFKD, so it also folds the
      compatibility forms the old NFD check refused ("ſ" -> "s", "Ｏ" -> "O", "𝗼" -> "o",
      "ﬁ" -> "fi"). Those must still be refused.
  B2  the shared fold is stricter than the old check. Accented Latin ("café", "naïve", "Ångström"),
      the Turkish letters and plain ASCII must still pass.
  B3  any other code point moves. Every Unicode letter is compared with a frozen copy of the old
      check; there is no difference.

No database or model: this file only needs the process it runs in.
"""

from __future__ import annotations

import sys
import unicodedata

import pytest

from app.services.suggestion_chip_copy import has_lookalike_letter

_TR_LETTERS = frozenset("çğıöşüÇĞİÖŞÜ")


def _frozen_old_check(text: str) -> bool:
    """The check as it was before it read the shared fold (the reference for B3)."""
    for ch in text:
        if not ch.isalpha() or ch.isascii() or ch in _TR_LETTERS:
            continue
        base, *marks = unicodedata.normalize("NFD", ch)
        if base.isascii() and base.isalpha() and all(unicodedata.combining(m) for m in marks):
            continue
        return True
    return False


ALLOWED = [
    "Whisk first",
    "café",
    "naïve",
    "Ångström",
    "résumé",
    "Müzik şimdi çok iyi ğ ı İ",
    "ÇAĞRI",
    "piñata",
    "Zoë",
    # Canonically equal to "K" and "Å" (not compatibility forms), so the old check passed them too.
    "\u212aeep going",
    "\u212bngström",
]
REFUSED = [
    ("small capital O", "ᴏrder now"),
    ("script G", "ɡet it"),
    ("fullwidth O", "Ｏrder now"),
    ("long s", "ſoon"),
    ("math bold o", "𝗼rder"),
    ("fi ligature", "ﬁrst"),
    ("dotless-j", "ȷust now"),
    ("o with stroke", "søn"),
    ("long s with dot above", "ẛoon"),
    ("cyrillic a", "аct now"),
    ("greek omicron", "οrder"),
]


@pytest.mark.parametrize("text", ALLOWED)
def test_b2_ordinary_letters_pass(text):
    assert has_lookalike_letter(text) is False


@pytest.mark.parametrize(("label", "text"), REFUSED, ids=[label for label, _ in REFUSED])
def test_b1_a_lookalike_is_still_refused(label, text):
    assert has_lookalike_letter(text) is True


def test_b3_no_unicode_letter_changed_its_verdict():
    moved = [
        hex(cp)
        for cp in range(sys.maxunicode + 1)
        if not 0xD800 <= cp <= 0xDFFF
        and (ch := chr(cp)).isalpha()
        and has_lookalike_letter(ch) != _frozen_old_check(ch)
    ]
    assert moved == []
