"""Additional production pilot limits, separate from renderer implementation."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import get_args

from app.config import settings
from app.kria.portable_text import PortableTextLayer, PositionedTextRun
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAsset

# Exact production font instances used when PHONE_FONT_QUALIFICATION_STRICT is
# true; changing bundled bytes or any coordinate requires a fresh
# cloud/native/device qualification. Never match by alias alone.
_QUALIFIED_FONT_INSTANCES = {
    "Fraunces-Bold.ttf": (
        "177ff6c0f14e5550a3c624247cd1189611d4eb65d000b14944c63d967958abbb",
        360440,
        {"opsz": 9.0, "wght": 900.0, "SOFT": 0.0, "WONK": 1.0},
    ),
    "DMSans-Bold.ttf": (
        "8cd08d97e89c24d0aa92edd2f0f4c8ee6195eee9b7c9f154865a58b02f0c1c0d",
        240164,
        {"opsz": 9.0, "wght": 400.0},
    ),
}

_FONT_REGISTRY_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "fonts" / "font-registry.json"
)


def _load_registered_font_filenames() -> frozenset[str]:
    """Every filename `bundled_font_asset` (render_library.py) can serve.

    This is the same `assets/fonts/font-registry.json` the cloud renderer and
    the guided-plan compiler (`portable_text_layout.py`) already resolve font
    families against, so a filename appearing here is exactly the set of
    bundled fonts the iOS app ships with their license files.
    """
    try:
        registry = json.loads(_FONT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return frozenset()
    fonts = registry.get("fonts") if isinstance(registry, dict) else None
    if not isinstance(fonts, dict):
        return frozenset()
    return frozenset(
        config["file"]
        for config in fonts.values()
        if isinstance(config, dict) and isinstance(config.get("file"), str)
    )


_REGISTERED_FONT_FILENAMES = _load_registered_font_filenames()

# docs/reviews/kri-29/coverage.md ("## Text"): "All shared editor effect names
# now have native implementations and compiler paths" lists exactly these 17
# of `PortableTextLayer.effect`'s full vocabulary. `lyric-line` (full
# style/language comparisons still open) and `caption-pop` (already gated
# separately below, in `validate_phone_pilot_recipe`) are deliberately held
# back from the default-mode set.
_STAGED_TEXT_EFFECTS = frozenset({"lyric-line", "caption-pop"})
_NATIVE_TEXT_EFFECTS = (
    frozenset(get_args(PortableTextLayer.model_fields["effect"].annotation)) - _STAGED_TEXT_EFFECTS
)


@cache
def _bundled_font_is_variable(filename: str) -> bool:
    """True when the checked-in font file itself carries variation axes.

    A non-variable face (e.g. Inter-Bold.ttf) has nothing for CoreText to
    default differently, so it's safe to qualify with no coordinates at all.
    A variable face left without coordinates would let CoreText pick its own
    optical size instead of the one the cloud compiler resolved.
    """
    from fontTools.ttLib import TTFont  # noqa: PLC0415

    directory = _FONT_REGISTRY_PATH.parent
    path = (directory / filename).resolve()
    if path.parent != directory or not path.is_file():
        return False
    try:
        font = TTFont(str(path), lazy=True)
    except Exception:  # noqa: BLE001 - an unreadable bundled font never qualifies
        return False
    try:
        return "fvar" in font
    finally:
        font.close()


def _strict_font_instance_unqualified(
    layer: PortableTextLayer, run: PositionedTextRun, asset: RenderAsset | None
) -> bool:
    """Byte-identical to the original per-instance gate (2026-09-14)."""
    if not run.font_variations:
        return (
            asset is not None
            and asset.kind == "library"
            and asset.catalog == "font"
            and asset.catalog_id in _QUALIFIED_FONT_INSTANCES
        )
    if layer.effect != "fade-in" or layer.giant_title is not None:
        return True
    if asset is None or asset.kind != "library" or asset.catalog != "font":
        return True
    qualified = _QUALIFIED_FONT_INSTANCES.get(asset.catalog_id)
    if qualified is None:
        return True
    digest, byte_count, coordinates = qualified
    return (
        asset.fingerprint.sha256 != digest
        or asset.fingerprint.byte_count != byte_count
        or run.font_variations != coordinates
    )


def _default_font_instance_unqualified(
    layer: PortableTextLayer, run: PositionedTextRun, asset: RenderAsset | None
) -> bool:
    """Any bundled-registry font on a native-supported effect (2026-09-18).

    Relaxes the exact-instance pilot gate to the product owner's decision to
    build the local renderer for all features: font identity now only needs
    to resolve to a checked-in, license-cleared bundled font (not one exact
    hand-qualified instance), and any effect the native engine has actually
    implemented (not just `fade-in`) is allowed. Giant-title combinations are
    allowed here too -- `giant-title-wipe` has native support for every
    effect except handwriting, which `validate_phone_pilot_recipe` still
    rejects unconditionally below.
    """
    if layer.effect not in _NATIVE_TEXT_EFFECTS:
        return True
    if (
        asset is None
        or asset.kind != "library"
        or asset.catalog != "font"
        or asset.catalog_id not in _REGISTERED_FONT_FILENAMES
    ):
        return True
    if _bundled_font_is_variable(asset.catalog_id) and not run.font_variations:
        # CoreText would otherwise pick its own optical size for this face.
        return True
    return False


def _has_unqualified_font_instance(recipe: EditRecipeV2) -> bool:
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    checker = (
        _strict_font_instance_unqualified
        if settings.phone_font_qualification_strict
        else _default_font_instance_unqualified
    )
    for layer in recipe.text_layers:
        for run in layer.runs:
            asset = manifest.get(run.font_asset_id)
            if checker(layer, run, asset):
                return True
    return False


def validate_phone_pilot_recipe(recipe: EditRecipeV2) -> None:
    if (
        recipe.visual_fills
        or recipe.audio.mute_windows
        or any(clip.visual_placement is not None for track in recipe.tracks for clip in track.clips)
    ):
        raise ValueError("Visual blocks await native parity and device qualification")
    if any(
        clip.overlay_dissolve_seed is not None
        or clip.hold_duration is not None
        or clip.overlay_above_text is not None
        or clip.overlay_pop_in is not None
        or clip.overlay_preserve_alpha is not None
        for track in recipe.tracks
        for clip in track.clips
    ):
        raise ValueError("Editor media await native parity and device qualification")
    if recipe.motion_scenes is not None:
        raise ValueError("Motion scenes await native parity and device qualification")
    if recipe.camera_pulses:
        raise ValueError("Camera effects await native parity and device qualification")
    if _has_unqualified_font_instance(recipe):
        raise ValueError("This font instance awaits native parity and device qualification")
    if any(
        layer.animation_phases is not None
        or layer.background is not None
        or layer.effect == "caption-pop"
        or (layer.karaoke is not None and layer.karaoke.active_only is not None)
        for layer in recipe.text_layers
    ):
        raise ValueError(
            "Authored text phases and backgrounds await native parity and device qualification"
        )
    if not recipe.required_capabilities.issubset(settings.phone_render_verified_features):
        raise ValueError("This edit needs a phone capability that is not enabled")
    if any(
        layer.giant_title is not None and layer.effect == "handwriting"
        for layer in recipe.text_layers
    ):
        raise ValueError(
            "Giant-title handwriting is unavailable while phone performance is improved"
        )
