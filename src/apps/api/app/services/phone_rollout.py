"""Additional production pilot limits, separate from renderer implementation."""

from app.config import settings
from app.kria.recipes_v2 import EditRecipeV2


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
    if any(
        any(run.font_variations for run in layer.runs)
        or layer.animation_phases is not None
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
