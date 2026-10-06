"""The iOS editor previews a server text row where the burn draws it. It mirrors
the burn's named-position presets and default face as Swift constants
(`EditorTextElement.presetY` / `centerY` / `defaultFontFamily` in
NativeEditorDocument.swift). If either side changes alone, the preview drifts
from the video again: a title shows mid-frame or in the wrong face, and a text
Save can then rewrite the burned face.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.agents._schemas.text_element import TextElement
from app.pipeline.generative_overlays import (
    _DEFAULT_POSITION,
    build_overlays_from_text_elements,
)
from app.pipeline.text_overlay import _POSITION_Y

API_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[4]
REGISTRY = API_ROOT / "assets" / "fonts" / "font-registry.json"
SWIFT_DOCUMENT = REPO_ROOT / "src/apps/ios/Kria/Core/NativeEditorDocument.swift"


def _swift_presets() -> dict[str, float]:
    text = SWIFT_DOCUMENT.read_text(encoding="utf-8")
    center = re.search(r"static let centerY\s*=\s*([0-9.]+)", text)
    table = re.search(r"static let presetY: \[String: Double\] = \[([^\]]*)\]", text)
    assert center and table, "presetY / centerY not found in NativeEditorDocument.swift"
    values = {"centerY": float(center.group(1))}
    return {
        key: values[raw] if raw in values else float(raw)
        for key, raw in re.findall(r'"(\w+)":\s*([\w.]+)', table.group(1))
    }


def _burned_y(position: str) -> float:
    element = TextElement(id="t", text="Hi", start_s=0, end_s=1, position=position)
    overlay = build_overlays_from_text_elements([element], video_duration_s=2)[0]
    return _POSITION_Y[overlay["position"]]


def test_swift_presets_match_the_burned_positions() -> None:
    presets = _swift_presets()
    for position in ("top", "middle", "bottom"):
        assert presets[position] == _burned_y(position), position
    assert presets["center"] == _POSITION_Y["center"]
    # A custom row with no y burns at the default position; iOS uses centerY.
    assert presets["center"] == _POSITION_Y[_DEFAULT_POSITION]


def test_named_rows_burn_at_the_preset_whatever_fracs_they_carry() -> None:
    # iOS ignores x_frac/y_frac on a named row (older app builds saved y 0.5 on
    # them); this pins the burn rule that choice mirrors.
    for position in ("top", "middle", "bottom"):
        element = TextElement(
            id="t", text="Hi", start_s=0, end_s=1, position=position, x_frac=0.1, y_frac=0.9
        )
        overlay = build_overlays_from_text_elements([element], video_duration_s=2)[0]
        assert overlay.get("position_x_frac") is None, position
        assert overlay.get("position_y_frac") is None, position


def test_swift_default_face_is_the_registry_display_face() -> None:
    text = SWIFT_DOCUMENT.read_text(encoding="utf-8")
    match = re.search(r'static let defaultFontFamily\s*=\s*"([^"]+)"', text)
    assert match, "defaultFontFamily not found in NativeEditorDocument.swift"
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    assert match.group(1) == registry["style_defaults"]["display"]
