"""Web/API parity for the SFX library vocabulary.

The web editor's SFX pickers (``src/apps/web/src/lib/sfx-browse.ts``) keep
hand-copied mirrors of two resolver constants: the category list (grouping
order) and the query filler words. A category added here without the web
list silently files every new effect under "Other"; a filler drift makes the
picker and the chat resolver disagree on "a buzzer sound".
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services.sfx_catalog import _FILLER, SFX_CATEGORIES

WEB_SFX_BROWSE = Path(__file__).resolve().parents[3] / "web" / "src" / "lib" / "sfx-browse.ts"


def _ts_string_array(pattern: str) -> set[str]:
    source = WEB_SFX_BROWSE.read_text(encoding="utf-8")
    match = re.search(pattern, source, re.DOTALL)
    assert match, f"{pattern!r} not found in {WEB_SFX_BROWSE}"
    return set(re.findall(r'"([^"]+)"', match.group(1)))


def test_web_category_order_covers_exactly_the_api_categories() -> None:
    web = _ts_string_array(r"export const SFX_CATEGORY_ORDER = \[(.*?)\] as const;")
    assert web == set(SFX_CATEGORIES)


def test_web_query_filler_matches_the_resolver() -> None:
    web = _ts_string_array(r"const QUERY_FILLER = new Set\(\[(.*?)\]\);")
    assert web == set(_FILLER)
