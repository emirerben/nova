"""Slide-post text fonts: the font registry, the agent-schema allowlist and the iOS default
must not drift apart (KRI-305). The iOS font chips come from the registry, so a registry
font the allowlist rejects would fail on save; a default the Swift side forgets to mirror
would round-trip as a different face.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.agents._schemas.text_element import _ALLOWED_FONTS
from app.schemas.slide_post import DEFAULT_SLIDE_TEXT_FONT

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY = API_ROOT / "assets" / "fonts" / "font-registry.json"
SWIFT_SLIDE_POST = REPO_ROOT / "src/apps/ios/Kria/Core/SlidePost.swift"


def test_every_live_registry_font_is_allowed() -> None:
    fonts = json.loads(REGISTRY.read_text(encoding="utf-8"))["fonts"]
    live = {name for name, meta in fonts.items() if not meta.get("deprecated")}
    assert live, "registry has no live fonts"
    missing = (live | {"Inter-Bold", "Inter"}) - _ALLOWED_FONTS
    assert not missing, f"fonts missing from _ALLOWED_FONTS: {sorted(missing)}"


def test_swift_default_font_matches_python_default() -> None:
    text = SWIFT_SLIDE_POST.read_text(encoding="utf-8")
    match = re.search(r'static let defaultFont\s*=\s*"([^"]+)"', text)
    assert match, "defaultFont constant not found in SlidePost.swift"
    assert match.group(1) == DEFAULT_SLIDE_TEXT_FONT
    assert DEFAULT_SLIDE_TEXT_FONT in _ALLOWED_FONTS
