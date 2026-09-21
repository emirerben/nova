"""Project the single-clip "Talking to camera" (subtitled) edit format into a
native device render program (KRI-132).

Companion to `app.pipeline.phone_montage_plan.compile_phone_montage_plan` and
`app.pipeline.phone_guided_plan.compile_phone_guided_plan` -- see those
modules' docstrings for the general contract every phone compiler follows:
no media is downloaded or rendered here, and unsupported lanes fail closed
with `UnsupportedPhonePlan` until their native implementation and parity
fixtures exist (see docs/runbooks/phone-rendering.md).

Mirrors the cloud lean path, `_render_subtitled_variant` in
`app.tasks.generative_build` (~generative_build.py:20462-20780): edit_format
``subtitled`` is exactly ONE clip, rendered 1:1 (source_start=0, full
duration, rate=1.0 -- no trim, no speed change, no reframe crop) keeping its
OWN audio at full volume (no music bed, no narration track), with
Whisper-derived caption cues burned as `text_layers`
(`app.pipeline.phone_captions.compile_caption_layers`) instead of an ASS
file. The cloud path additionally reframes the clip to 9:16 via
`reframe_and_export`/`resolve_output_fit`, which can letterbox or center-crop
a non-portrait source; the phone engine only aspect-fills (center-crops) with
no face tracking, so this compiler rejects a non-portrait source clip
outright rather than silently cropping the speaker out of frame -- a v1
limitation, not a permanently-gated capability (hence `semanticCamera`, a
named-but-unimplemented `MediaCapability`, rather than leaving it bare).
"""

from __future__ import annotations

from app.kria.recipes import (
    AssetFingerprint,
    AudioMixRecipe,
    Canvas,
    MediaAsset,
    MediaSize,
    TimelineClip,
    TimelineTrack,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest
from app.pipeline.phone_captions import caption_font_assets, compile_caption_layers
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.services.phone_sources import PhoneSourceBinding

# Mirrors `_render_subtitled_variant`'s own cap (generative_build.py, the
# `probe.duration_s > 300.0` check ~line 20690): "subtitled clips are capped
# at 5 minutes". Named up front rather than surfacing as a bare recipe
# validation error.
_MAX_CLIP_DURATION_S = 300.0

# The phone story canvas for every subtitled render -- subtitled has no
# `decision.orientation`/`extras["canvas"]` to read (unlike the montage
# compiler), and the portrait-source requirement below makes a landscape
# canvas meaningless here.
_STORY_CANVAS = Canvas(width=1080, height=1920)


def compile_phone_subtitled_plan(
    bindings: tuple[PhoneSourceBinding, ...],
    *,
    caption_cues: list[dict],
    caption_style: str = "sentence",
) -> EditRecipeV2:
    """Compile the subtitled edit format's phone recipe.

    ``bindings`` follows the same one-binding-per-clip contract as
    `compile_phone_montage_plan`/`compile_phone_guided_plan`, but subtitled is
    single-clip by product definition: exactly one binding is required (the
    uploader already caps new subtitled items at one clip; an item switched
    from montage can still carry more, and the cloud path silently uses only
    the first -- this compiler fails closed instead, since dropping clips
    silently on the phone would be a surprising device/cloud divergence).

    ``caption_cues`` are the cloud caption-cue dicts exactly as
    `_render_subtitled_variant` builds them -- see
    `app.pipeline.phone_captions`'s module docstring for the exact shape.
    Passing an empty list is explicitly supported: the clip still renders,
    just without captions (matches the cloud's own "no detectable speech"
    fallback, which ships the clean clip).

    ``caption_style`` is ``"sentence"`` (default, pop-in blocks) or ``"word"``
    (word-by-word lime pop) -- forwarded verbatim to
    `compile_caption_layers`.

    Rejects (all `UnsupportedPhonePlan`, fail-closed):
      - zero or more than one binding.
      - a non-video source (no probed width/height).
      - a source clip whose (rotation-corrected) display is landscape or
        square -- the phone engine's center-fill crop has no face tracking.
      - a clip longer than 300s (`_MAX_CLIP_DURATION_S`), matching the
        cloud's own cap.
    """
    if len(bindings) != 1:
        raise UnsupportedPhonePlan(
            f"phone subtitled plan requires exactly one clip, got {len(bindings)}"
        )
    binding = bindings[0]
    original = binding.original
    if original.width is None or original.height is None:
        raise UnsupportedPhonePlan("phone subtitled plan requires a video source clip")
    if original.duration_s > _MAX_CLIP_DURATION_S:
        raise UnsupportedPhonePlan(
            "subtitled clips are capped at 5 minutes -- trim the clip and re-upload"
        )
    display_width, display_height = _display_dims(original)
    if display_width >= display_height:
        raise UnsupportedPhonePlan(
            "phone subtitled plan requires a portrait source clip -- the phone engine "
            "aspect-fills with no face tracking, so a landscape or square clip would "
            "silently center-crop the speaker",
            capability="semanticCamera",
        )

    duration_s = float(original.duration_s)
    asset = binding.render_asset()
    assets = {
        asset.id: MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=original.sha256, byte_count=original.byte_count),
            duration=original.duration_s,
            natural_size=MediaSize(width=original.width, height=original.height),
            orientation_degrees=original.orientation_degrees,
            # `PhoneSourceBinding.require_proxy` already guarantees a reserved
            # analysis proxy for every binding reaching this compiler.
            is_proxy_available=True,
        )
    }
    manifest: dict[str, object] = {asset.id: asset}
    clip = TimelineClip(
        id="clip-0",
        source_asset_id=asset.id,
        source_start=0.0,
        source_duration=duration_s,
        timeline_start=0.0,
        rate=1.0,
    )

    try:
        layers = compile_caption_layers(
            caption_cues,
            canvas_width=_STORY_CANVAS.width,
            canvas_height=_STORY_CANVAS.height,
            style=caption_style,
            timeline_duration_s=duration_s,
        )
    except UnsupportedPhonePlan:
        raise
    except Exception as exc:  # noqa: BLE001 - untrusted transcript/edited cue content
        raise UnsupportedPhonePlan(f"unable to compile captions: {exc}") from exc

    for font_id, font_asset in caption_font_assets(layers).items():
        manifest[font_id] = font_asset
        assets[font_id] = MediaAsset(
            id=font_id,
            relative_path=font_id,
            fingerprint=AssetFingerprint(
                hex=font_asset.fingerprint.sha256,
                byte_count=font_asset.fingerprint.byte_count,
            ),
        )

    required_capabilities = {"basicComposition", "local1080Export"} | (
        {"positionedText"} if layers else set()
    )
    if any(layer.effect not in {"static", "none"} for layer in layers):
        required_capabilities |= {"animatedText"}

    return EditRecipeV2(
        canvas=_STORY_CANVAS,
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=[TimelineTrack(id="subtitled", kind="video", clips=[clip])],
        text_layers=layers,
        audio=AudioMixRecipe(original_volume=1.0),
        required_capabilities=required_capabilities,
    )


def _display_dims(original) -> tuple[int, int]:
    """(width, height) as actually DISPLAYED once `orientation_degrees` is
    applied -- a 1080x1920-pixel file flagged 90/270 degrees is portrait on
    screen despite carrying landscape pixel dimensions (mirrors the
    golden-hour exact-canvas check in `phone_montage_plan.py`/
    `phone_guided_plan.py`, which reasons about the same rotation flag)."""
    if original.orientation_degrees in (90, 270):
        return original.height, original.width
    return original.width, original.height
