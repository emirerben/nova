"""Additional production pilot limits, separate from renderer implementation."""

from app.config import settings
from app.kria.recipes_v2 import EditRecipeV2

# Exact production font instances; changing bundled bytes or any coordinate
# requires a fresh cloud/native/device qualification. Never match by alias alone.
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


def _has_unqualified_font_instance(recipe: EditRecipeV2) -> bool:
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    for layer in recipe.text_layers:
        for run in layer.runs:
            asset = manifest.get(run.font_asset_id)
            if not run.font_variations:
                # Omitting a variable face's coordinates would let CoreText
                # choose its own optical size instead of the qualified one.
                if (
                    asset is not None
                    and asset.kind == "library"
                    and asset.catalog == "font"
                    and asset.catalog_id in _QUALIFIED_FONT_INSTANCES
                ):
                    return True
                continue
            if layer.effect != "fade-in" or layer.giant_title is not None:
                return True
            if asset is None or asset.kind != "library" or asset.catalog != "font":
                return True
            qualified = _QUALIFIED_FONT_INSTANCES.get(asset.catalog_id)
            if qualified is None:
                return True
            digest, byte_count, coordinates = qualified
            if (
                asset.fingerprint.sha256 != digest
                or asset.fingerprint.byte_count != byte_count
                or run.font_variations != coordinates
            ):
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
