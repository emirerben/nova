"""Kria's chat sentences in other languages, one file per language (B, KRI-1362).

The English sentence in the code is each sentence's key (gettext-style): ``say(en=..., tr=...)``
call sites stay as they are, and a language other than English and Turkish is looked up here by
that English text. A sentence with names or numbers in it is written as a template
(``say(en="No pop-in for {names}", tr="...", names=...)``) so its key is the same on every turn.

Files live in ``app/kria/copy/<language>.json`` as ``{"messages": {english: translation}}``.
``tr.json`` is generated from the code's own ``tr=`` texts (``scripts/kria/build_copy_catalog.py``)
and must match them exactly; a missing translation falls back to English, never an error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

CATALOG_DIR = Path(__file__).parent / "copy"
# A test-only language: every sentence the catalog knows comes back wrapped, so a sentence that
# shows up unwrapped is still hard-coded outside the catalog.
PSEUDO_LANGUAGE = "qps"
_PSEUDO_OPEN, _PSEUDO_CLOSE = "⟦", "⟧"


@lru_cache(maxsize=8)
def catalog(language: str) -> Mapping[str, str]:
    """The ``english -> translation`` table for ``language``; empty when there is no file."""
    path = CATALOG_DIR / f"{language}.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages") if isinstance(data, Mapping) else None
    if not isinstance(messages, Mapping):
        return {}
    return {str(k): str(v) for k, v in messages.items() if isinstance(v, str) and v}


def translate(language: str, english: str) -> str | None:
    """``english`` in ``language``, or None when the file has no line for it."""
    if language == PSEUDO_LANGUAGE:
        known = english in catalog("tr")
        return f"{_PSEUDO_OPEN}{english}{_PSEUDO_CLOSE}" if known else None
    return catalog(language).get(english)


def is_pseudo_wrapped(text: str) -> bool:
    return text.startswith(_PSEUDO_OPEN) and text.endswith(_PSEUDO_CLOSE)
