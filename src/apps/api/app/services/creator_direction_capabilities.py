"""Single typed-key and execution-capability registry for creator direction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CapabilityStatus = Literal["enforced", "advisory", "unsupported", "conflicted"]


@dataclass(frozen=True, slots=True)
class DirectionCapability:
    value_type: type | tuple[type, ...]
    planner_supported: bool
    renderer_modes: frozenset[str] = frozenset()

    @property
    def renderer_enforced(self) -> bool:
        return bool(self.renderer_modes)


ALL_RENDER_MODES = frozenset(
    {
        "preview",
        "pillow",
        "skia",
        "classic",
        "music",
        "generative",
        "reburn",
    }
)

# New executable keys must be registered here before they can enter a snapshot.
# Text-only planning preferences are typed too, but remain advisory unless a
# deterministic renderer/compiler path is declared.
DIRECTION_CAPABILITIES: dict[str, DirectionCapability] = {
    "content_type": DirectionCapability((str, list), True),
    "tone": DirectionCapability(str, True),
    "pacing": DirectionCapability(str, True),
    "edit_format_mix": DirectionCapability(dict, True),
    "font_family": DirectionCapability(str, True, ALL_RENDER_MODES),
    "text_color": DirectionCapability(str, True),
    "highlight_color": DirectionCapability(str, True),
    "text_size": DirectionCapability((int, float, str), True),
    "text_position": DirectionCapability(str, True),
    "text_alignment": DirectionCapability(str, True),
    "font_cycling": DirectionCapability(bool, True),
    "stroke_width": DirectionCapability((int, float), True),
    "shadow_enabled": DirectionCapability(bool, True, ALL_RENDER_MODES),
}

SUPPORTED_STRUCTURED_KEYS = frozenset(DIRECTION_CAPABILITIES)
_SAFE_LABEL = re.compile(r"^[^\n\r\t<>]{1,80}$")
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_PACING = frozenset({"slow", "calm", "balanced", "fast", "energetic"})
_TEXT_POSITION = frozenset({"top", "upper", "center", "lower", "bottom"})
_TEXT_ALIGNMENT = frozenset({"left", "center", "right"})
_TEXT_SIZE_LABEL = frozenset({"small", "medium", "large"})
_EDIT_FORMATS = frozenset({"montage", "talking_head", "subtitled", "narrated", "music"})


def _load_enforceable_fonts() -> frozenset[str]:
    registry_path = Path(__file__).resolve().parents[2] / "assets" / "fonts" / "font-registry.json"
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return frozenset()
    fonts = registry.get("fonts") if isinstance(registry, dict) else None
    if not isinstance(fonts, dict):
        return frozenset()
    return frozenset(
        name
        for name, config in fonts.items()
        if isinstance(name, str) and isinstance(config, dict) and not bool(config.get("deprecated"))
    )


ENFORCEABLE_FONT_FAMILIES = _load_enforceable_fonts()


def capability_status(
    normalized_key: str | None,
    *,
    enforcement: str,
    conflicted: bool = False,
) -> CapabilityStatus:
    if conflicted:
        return "conflicted"
    capability = DIRECTION_CAPABILITIES.get(str(normalized_key or ""))
    if capability is None:
        return "advisory" if normalized_key is None else "unsupported"
    if enforcement == "advisory" or not capability.renderer_enforced:
        return "advisory"
    return "enforced"


def validate_capability_values(value: dict[str, Any] | None) -> bool:
    if value is None:
        return True
    if len(value) != 1:
        return False
    for key, item in value.items():
        capability = DIRECTION_CAPABILITIES.get(key)
        if capability is None or not isinstance(item, capability.value_type):
            return False
        if key in {"tone", "font_family"} and (
            not isinstance(item, str) or _SAFE_LABEL.fullmatch(item) is None
        ):
            return False
        if key == "font_family" and item not in ENFORCEABLE_FONT_FAMILIES:
            return False
        if key == "content_type" and (
            isinstance(item, list)
            and (
                not 1 <= len(item) <= 8
                or any(
                    not isinstance(label, str) or _SAFE_LABEL.fullmatch(label) is None
                    for label in item
                )
            )
        ):
            return False
        if key in {"text_color", "highlight_color"} and (
            not isinstance(item, str) or _HEX_COLOR.fullmatch(item) is None
        ):
            return False
        if key == "pacing" and item not in _PACING:
            return False
        if key == "text_position" and item not in _TEXT_POSITION:
            return False
        if key == "text_alignment" and item not in _TEXT_ALIGNMENT:
            return False
        if key == "text_size" and not (
            (isinstance(item, int) and not isinstance(item, bool) and 12 <= item <= 240)
            or (isinstance(item, str) and item in _TEXT_SIZE_LABEL)
        ):
            return False
        if key == "stroke_width" and not (
            isinstance(item, (int, float)) and not isinstance(item, bool) and 0 <= float(item) <= 20
        ):
            return False
        if key == "edit_format_mix" and (
            not isinstance(item, dict)
            or not 1 <= len(item) <= 5
            or set(item) - _EDIT_FORMATS
            or any(
                not isinstance(weight, (int, float))
                or isinstance(weight, bool)
                or not 0 <= float(weight) <= 1
                for weight in item.values()
            )
            or sum(float(weight) for weight in item.values()) <= 0
        ):
            return False
    return True


__all__ = [
    "ALL_RENDER_MODES",
    "CapabilityStatus",
    "DIRECTION_CAPABILITIES",
    "DirectionCapability",
    "ENFORCEABLE_FONT_FAMILIES",
    "SUPPORTED_STRUCTURED_KEYS",
    "capability_status",
    "validate_capability_values",
]
