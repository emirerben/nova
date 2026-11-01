"""Additional production pilot limits, separate from renderer implementation."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any, get_args

from app.agents._schemas.edit_format import (
    GUIDED_EDIT_FORMATS,
    NARRATED_EDIT_FORMATS,
    PHONE_RENDER_SUPPORTED_FORMATS,
)
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


@cache
def _registry_fonts() -> dict[str, dict]:
    """The checked-in `font-registry.json` ``fonts`` table (family -> config)."""
    try:
        registry = json.loads(_FONT_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    fonts = registry.get("fonts") if isinstance(registry, dict) else None
    if not isinstance(fonts, dict):
        return {}
    return {name: config for name, config in fonts.items() if isinstance(config, dict)}


def _load_registered_font_filenames() -> frozenset[str]:
    """Every filename `bundled_font_asset` (render_library.py) can serve.

    This is the same `assets/fonts/font-registry.json` the cloud renderer and
    the guided-plan compiler (`portable_text_layout.py`) already resolve font
    families against, so a filename appearing here is exactly the set of
    bundled fonts the iOS app ships with their license files.
    """
    return frozenset(
        config["file"]
        for config in _registry_fonts().values()
        if isinstance(config.get("file"), str)
    )


_REGISTERED_FONT_FILENAMES = _load_registered_font_filenames()


class PhoneCapabilityUnavailable(ValueError):
    """The recipe needs a device capability that is not verified (yet).

    Unlike every other phone-plan reject this one is NOT a property of the plan:
    flipping the capability into ``phone_render_verified_features`` (or the
    rollout flag behind it) makes the very same plan render. Callers must keep
    it retryable (``phone_capability_unavailable``) instead of folding it into
    the deterministic ``phone_plan_unsupported`` bucket (KRI-286).

    ``capability`` names what is missing (comma-joined when several are).
    Subclasses ``ValueError`` so every existing ``except ValueError`` and
    ``pytest.raises(ValueError, match=...)`` keeps working byte-for-byte.
    """

    def __init__(self, message: str, *, capability: str) -> None:
        super().__init__(message)
        self.capability = capability


class PhoneFontUnqualified(ValueError):
    """A text layer's font/effect combination is not device-qualified.

    Deterministic (a property of the plan), but repairable at planning time:
    ``font_files`` are the bundled font filenames and ``effects`` the text
    effects that tripped the gate, so ``app.pipeline.phone_plan_repair`` can
    swap them for a qualified default instead of failing after approval.
    """

    def __init__(
        self,
        message: str,
        *,
        font_files: frozenset[str] = frozenset(),
        effects: frozenset[str] = frozenset(),
    ) -> None:
        super().__init__(message)
        self.font_files = font_files
        self.effects = effects


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


def _font_instance_checker():  # noqa: ANN202
    return (
        _strict_font_instance_unqualified
        if settings.phone_font_qualification_strict
        else _default_font_instance_unqualified
    )


def _has_unqualified_font_instance(recipe: EditRecipeV2) -> bool:
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    checker = _font_instance_checker()
    for layer in recipe.text_layers:
        for run in layer.runs:
            asset = manifest.get(run.font_asset_id)
            if checker(layer, run, asset):
                return True
    return False


def unqualified_font_causes(recipe: EditRecipeV2) -> tuple[frozenset[str], frozenset[str]]:
    """``(font_files, effects)`` behind every layer `_has_unqualified_font_instance` flags.

    Uses the SAME per-run predicate as the gate, so the planning-time repair can
    never disagree with what `validate_phone_pilot_recipe` rejects. A layer whose
    effect has no native painter is attributed to its effect (its font may be
    fine); anything else is attributed to its bundled font file.
    """
    manifest = {asset.id: asset for asset in recipe.asset_manifest.assets}
    checker = _font_instance_checker()
    strict = settings.phone_font_qualification_strict
    files: set[str] = set()
    effects: set[str] = set()
    for layer in recipe.text_layers:
        for run in layer.runs:
            asset = manifest.get(run.font_asset_id)
            if not checker(layer, run, asset):
                continue
            effect_bad = not strict and layer.effect not in _NATIVE_TEXT_EFFECTS
            if effect_bad:
                effects.add(layer.effect)
            # The font is judged on its own too: re-ask the gate with a native effect so
            # a layer with BOTH a non-native effect and a bad font reports both causes.
            font_bad = not effect_bad or checker(
                layer.model_copy(update={"effect": "fade-in"}), run, asset
            )
            if font_bad and asset is not None and getattr(asset, "catalog_id", None):
                files.add(asset.catalog_id)
    return frozenset(files), frozenset(effects)


def registry_font_file(font_family: str | None, *, font_style: str = "display") -> str | None:
    """The bundled filename the compiler resolves ``font_family`` to (None if unknown).

    Mirrors `text_overlay_skia._resolve_typeface_for_overlay` without needing a
    loaded typeface: exact registry key, else the registry's style default.
    """
    fonts = _registry_fonts()
    entry = fonts.get(font_family) if font_family else None
    if entry is None:
        try:
            registry = json.loads(_FONT_REGISTRY_PATH.read_text(encoding="utf-8"))
            default = (registry.get("style_defaults") or {}).get(font_style)
        except (OSError, ValueError, TypeError, AttributeError):
            default = None
        entry = fonts.get(default) if default else None
    file = entry.get("file") if entry else None
    return file if isinstance(file, str) else None


def qualified_default_font_family() -> str | None:
    """A bundled family that passes the default-mode font gate on any native effect.

    Static (no variation axes) faces only: a variable face left without resolved
    coordinates is exactly what the gate rejects. None when the registry has no
    such face or strict per-instance qualification is on (that mode qualifies
    exact byte/coordinate instances only, so no family can be assumed).
    """
    if settings.phone_font_qualification_strict:
        return None
    for family in ("Inter", "Inter Tight", "Montserrat"):
        file = (_registry_fonts().get(family) or {}).get("file")
        if (
            isinstance(file, str)
            and file in _REGISTERED_FONT_FILENAMES
            and not _bundled_font_is_variable(file)
        ):
            return family
    return None


def validate_phone_pilot_recipe(recipe: EditRecipeV2, *, allow_editor_media: bool = False) -> None:
    placements = [
        clip.visual_placement
        for track in recipe.tracks
        for clip in track.clips
        if clip.visual_placement is not None
    ]
    if recipe.visual_fills:
        raise ValueError("Visual blocks await native parity and device qualification")
    if recipe.audio.mute_windows and not phone_clip_sound_windows_supported():
        # KRI-118 L1 item 6: mute windows are a distinct feature from visual
        # blocks (silent overlay tracks) -- this used to reuse the "Visual
        # blocks..." message meant for that other rejection, which read as
        # nonsensical/misleading for a recipe with no visual fills at all.
        # KRI-942: the creator's part-of-a-clip "sound off" is the one sender, behind its flag.
        raise ValueError("Muted sections aren't supported on iPhone yet")
    if placements and not (
        (getattr(settings, "phone_editor_media_enabled", False) or allow_editor_media)
        and "visualBlocks" in settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "Visual blocks await native parity and device qualification",
            capability="visualBlocks",
        )
    if any(
        clip.overlay_dissolve_seed is not None
        or (
            clip.hold_duration is not None
            and not _main_track_freeze(clip, track, recipe.frame_rate)
        )
        or clip.overlay_above_text is not None
        or clip.overlay_pop_in is not None
        or clip.overlay_preserve_alpha is not None
        for track in recipe.tracks
        for clip in track.clips
    ):
        raise ValueError("Editor media await native parity and device qualification")
    # KRI-828: a creator's freeze (a main-video hold) is admitted only on a verified device.
    if any(
        _main_track_freeze(clip, track, recipe.frame_rate)
        for track in recipe.tracks
        for clip in track.clips
    ) and "freezeFrame" not in set(settings.phone_render_verified_features):
        raise PhoneCapabilityUnavailable(
            "Freeze frames await native parity and device qualification",
            capability="freezeFrame",
        )
    if recipe.motion_scenes is not None:
        raise ValueError("Motion scenes await native parity and device qualification")
    if recipe.camera_pulses:
        raise ValueError("Camera effects await native parity and device qualification")
    # KRI-589: an explicit gate (not only the `required_capabilities` check below) because
    # a caller may mutate a recipe after compile without recomputing the capabilities.
    if recipe.drawing_layers and "freehandDrawing" not in set(
        settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "Freehand drawings await native parity and device qualification",
            capability="freehandDrawing",
        )
    # KRI-646: same explicit gate for bent text (the capability set is not recomputed
    # when a caller mutates a recipe after compile).
    if any(layer.bend for layer in recipe.text_layers) and "textBend" not in set(
        settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "Bent text awaits native parity and device qualification",
            capability="textBend",
        )
    # Speaker background: same explicit gate; the compile strips the lane unless verified.
    if recipe.speaker_background is not None and "speakerBackground" not in set(
        settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "Speaker backgrounds await native parity and device qualification",
            capability="speakerBackground",
        )
    # Text behind people: same explicit gate (`compile_text_overlay` strips the flag before
    # it gets here unless verified; this catches a caller that stamped it afterwards).
    if any(layer.behind_subject for layer in recipe.text_layers) and "behindSubject" not in set(
        settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "Text behind people awaits native parity and device qualification",
            capability="behindSubject",
        )
    if _has_unqualified_font_instance(recipe):
        font_files, font_effects = unqualified_font_causes(recipe)
        raise PhoneFontUnqualified(
            "This font instance awaits native parity and device qualification",
            font_files=font_files,
            effects=font_effects,
        )
    authored_text_features = any(
        layer.animation_phases is not None
        or layer.background is not None
        or layer.effect == "caption-pop"
        or (layer.karaoke is not None and layer.karaoke.active_only is not None)
        for layer in recipe.text_layers
    )
    # These fields are implemented by the native authored-text renderer. Keep
    # the capability gate explicit: callers may mutate a recipe after compile
    # without recomputing ``required_capabilities``.
    if authored_text_features and "authoredText" not in settings.phone_render_verified_features:
        raise PhoneCapabilityUnavailable(
            "Authored text phases and backgrounds await native parity and device qualification",
            capability="authoredText",
        )
    # One-word-at-a-time captions (`karaoke.single_word`): same explicit gate, for a recipe
    # a caller mutated after compile.
    if recipe_has_single_word_text(recipe) and "singleWordText" not in set(
        settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "One-word captions await native parity and device qualification",
            capability="singleWordText",
        )
    if any(
        clip.source_crop is not None for track in recipe.tracks for clip in track.clips
    ) and "sourceCrop" not in set(settings.phone_render_verified_features):
        raise PhoneCapabilityUnavailable(
            "Cropped clips await native parity and device qualification",
            capability="sourceCrop",
        )
    if recipe_has_clip_tilt(recipe) and "clipTilt" not in set(
        settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "Tilted clips await native parity and device qualification",
            capability="clipTilt",
        )
    if recipe_has_clip_backdrop_media(recipe) and "clipBackdropMedia" not in set(
        settings.phone_render_verified_features
    ):
        raise PhoneCapabilityUnavailable(
            "A photo or video behind a clip awaits native parity and device qualification",
            capability="clipBackdropMedia",
        )
    missing = recipe.required_capabilities - set(settings.phone_render_verified_features)
    if missing:
        raise PhoneCapabilityUnavailable(
            "This edit needs a phone capability that is not enabled",
            capability=",".join(sorted(str(item) for item in missing)),
        )
    if any(
        layer.giant_title is not None and layer.effect == "handwriting"
        for layer in recipe.text_layers
    ):
        raise ValueError(
            "Giant-title handwriting is unavailable while phone performance is improved"
        )


def phone_guided_narration_supported() -> bool:
    """Single source of truth for "can the guided-story `.narration` plan lane
    (the voiceover-timed guided story, execution contract `guided_voiceover_v1`)
    render on the phone right now" (KRI-132 phone-voiceover-gate follow-up).

    Every site that decides this MUST call this function instead of
    re-deriving the rule: `app.services.creator_capabilities.resolve_creator_
    manifest` (whether `CAPABILITY_GUIDED_VOICEOVER` is available for a phone
    manifest), `app.tasks.content_plan_build`'s dispatch gate (whether an
    approved guided-voiceover proposal is refused before a Job is minted), and
    `app.tasks.generative_build._run_phone_guided_job` (whether the worker
    resolves and pins a narration bed at all). Keeping them in lockstep is the
    entire point -- a manifest that advertises the capability but a dispatch
    gate or worker that still refuses it (or vice versa) is exactly the class
    of bug this module exists to prevent.

    True iff ALL of:
      - `phone_guided_narration_rendering_enabled` (this lane's own rollout
        flag; independently reversible from the two below).
      - `phone_narration_rendering_enabled` (the lane reuses the same
        narration-audio device capability the montage-family and narrated-
        family voiceover renders already use).
      - `"narrationAudio"` is in `phone_render_verified_features` (the
        device-parity gate; a rollout flag alone never skips it).
    """

    return bool(
        settings.phone_guided_narration_rendering_enabled
        and settings.phone_narration_rendering_enabled
        and "narrationAudio" in settings.phone_render_verified_features
    )


def freehand_drawing_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build understand freehand drawings (KRI-589)?

    The build declares ``X-Kria-Client-Protocol``; at least
    ``freehand_drawing_min_client_protocol`` decodes ``drawingLayers`` and the
    ``drawings`` editor lane. ``require_client`` ``None`` applies the gate only
    inside an HTTP request (workers and chat edits have no app build to qualify);
    pass ``True``/``False`` to force it.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.freehand_drawing_min_client_protocol


def freehand_drawing_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "may a creator ADD or EDIT drawings right now" (KRI-589).

    Consulted by the editor capability map (whether ``drawings.add/edit/trim`` open) and
    by Save (whether a new or changed drawing is accepted or 422s ``drawings_disabled``),
    so the map never advertises an edit Save would refuse. Removal and replay of stored
    drawings never consult it: the kill switch stops authoring, not playback or deletion.

    True iff the kill switch (``freehand_drawing_enabled``) is on, ``freehandDrawing`` is in
    ``phone_render_verified_features`` (the device-parity gate) and the app-build gate
    holds (`freehand_drawing_client_supported`).
    """
    return bool(
        settings.freehand_drawing_enabled
        and "freehandDrawing" in set(settings.phone_render_verified_features)
        and freehand_drawing_client_supported(require_client=require_client)
    )


def freeze_frame_planned_for(client_protocol: int | None) -> bool:
    """May a phone montage plan hold a frame for the creator's freeze (KRI-828, KRI-898)?

    ``client_protocol`` is the app build the creator acted from, stamped where they act:
    the message the draft answers (``client_protocol`` on the turn) and the approval the
    render serves (``creator_client_protocol`` on the Job). True iff ``freezeFrame`` is in
    ``phone_render_verified_features`` (device parity) and that build declares at least
    ``freeze_frame_min_client_protocol``. No stamp plans no hold: the freeze stays KRI-777's
    honest "Couldn't" and the rest of the video renders, instead of a held recipe the
    approving phone would be told to update for (426 on the whole video).

    There is deliberately no request context read here: the draft, the pre-approval check
    and the render all run in Celery, where a context fallback would skip the build check.
    """
    return (
        "freezeFrame" in set(settings.phone_render_verified_features)
        and client_protocol is not None
        and client_protocol >= settings.freeze_frame_min_client_protocol
    )


def text_bend_planned_for(client_protocol: int | None) -> bool:
    """May a phone montage plan bend the text the creator asked curved (KRI-1029)?

    The planner's twin of `text_bend_supported` (KRI-646), read like
    `freeze_frame_planned_for`: ``client_protocol`` is the app build the creator acted from
    (the drafting message, then ``creator_client_protocol`` on the Job). True iff the kill
    switch is on, ``textBend`` is in ``phone_render_verified_features`` and that build
    declares at least ``text_bend_min_client_protocol``. No stamp plans straight text: the
    receipt says so, instead of a bent recipe the approving phone would be told to update for.
    """
    return bool(
        settings.text_bend_enabled
        and "textBend" in set(settings.phone_render_verified_features)
        and client_protocol is not None
        and client_protocol >= settings.text_bend_min_client_protocol
    )


def clip_speed_planned_for() -> bool:
    """May a phone montage plan retime a cut for the creator's speed ask (KRI-689)?

    True iff ``variableSpeed`` is in ``phone_render_verified_features`` (device parity: the
    same gate the editor's clip speed control reads, `_phone_clip_speed_reason`). Off, the
    plan writes no rate and the receipt keeps KRI-689's honest "can't change clip speed on
    your iPhone yet", so an approved recipe never asks the phone for a capability it lacks.
    """
    return "variableSpeed" in set(settings.phone_render_verified_features)


def stamped_client_protocol(value: object) -> int | None:
    """A stored app-protocol stamp read back, or ``None`` when absent or malformed."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _main_track_freeze(clip: Any, track: Any, frame_rate: float) -> bool:
    """A main-video hold longer than one frame: the creator's freeze (KRI-828)."""
    return (
        track.kind == "video"
        and clip.hold_duration is not None
        and clip.hold_duration > 1 / frame_rate + 1e-6
    )


def recipe_has_freeze_frame(recipe: Any) -> bool:
    """Does this recipe hold a main-video frame for a creator's freeze (KRI-828)?"""
    return any(
        _main_track_freeze(clip, track, recipe.frame_rate)
        for track in getattr(recipe, "tracks", None) or []
        for clip in track.clips
    )


def freeze_frame_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build decode a main-track ``freezeFrame`` hold (KRI-828)?

    The render worker plans the hold with no app build in hand, so the build that fetches
    or edits the recipe is qualified here (mirrors `text_bend_client_supported`): below
    ``freeze_frame_min_client_protocol`` it is told to update instead of receiving a recipe
    it cannot decode (or opening the held slot as slow motion in an old editor).
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.freeze_frame_min_client_protocol


def text_bend_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build understand text ``bend`` (KRI-646)?

    The build declares ``X-Kria-Client-Protocol``; at least
    ``text_bend_min_client_protocol`` decodes ``bend`` on a recipe text layer and
    draws it. ``require_client`` ``None`` applies the gate only inside an HTTP request
    (workers and chat edits have no app build to qualify); pass ``True``/``False`` to
    force it. Mirrors `freehand_drawing_client_supported`.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.text_bend_min_client_protocol


def caption_animation_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build author and round-trip caption ``animation_phases``?

    At least ``caption_animation_min_client_protocol`` keeps a caption row's phases and the
    all-captions default on Save; an older build would drop them. ``require_client`` as in
    `text_bend_client_supported`.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.caption_animation_min_client_protocol


def caption_animation_renderable() -> bool:
    """Do caption phases reach the compiled caption layers (KRI-1007)?

    A layer with ``animation_phases`` requires ``authoredText``; without it verified, or
    with the kill switch off, captions compile exactly as they did before phases existed.
    """
    return bool(
        settings.caption_animation_enabled
        and "authoredText" in set(settings.phone_render_verified_features)
    )


def caption_animation_supported(*, require_client: bool | None = None) -> bool:
    """May a creator author caption animation now (``editor_capabilities.caption_animation``)?"""
    return caption_animation_renderable() and caption_animation_client_supported(
        require_client=require_client
    )


def text_bend_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "may a creator author Bend right now" (KRI-646).

    True iff the kill switch (``text_bend_enabled``) is on, ``textBend`` is in
    ``phone_render_verified_features`` (the device-parity gate) and the app-build gate
    holds (`text_bend_client_supported`). Stored bend keeps compiling and playing when
    this is False; only authoring is closed.
    """
    return bool(
        settings.text_bend_enabled
        and "textBend" in set(settings.phone_render_verified_features)
        and text_bend_client_supported(require_client=require_client)
    )


def text_behind_subject_phone_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build decode a text layer's ``behind_subject``?

    At least ``text_behind_subject_phone_min_client_protocol`` (21) knows the key; an older
    build refuses the whole recipe (`rejectUnknownAssetFields`). ``require_client`` ``None``
    applies the gate only inside an HTTP request (workers have no app build to qualify);
    pass ``True``/``False`` to force it. Mirrors `text_bend_client_supported`.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return (
        protocol is not None and protocol >= settings.text_behind_subject_phone_min_client_protocol
    )


def text_behind_subject_phone_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "may a phone recipe carry ``behind_subject`` right now".

    True iff the kill switch (``text_behind_subject_phone_enabled``, default off) is on,
    ``behindSubject`` is in ``phone_render_verified_features`` (the device-parity gate) and
    the app-build gate holds (`text_behind_subject_phone_client_supported`). When False,
    `compile_text_overlay` strips the flag and the layer renders as plain text.
    """
    return bool(
        settings.text_behind_subject_phone_enabled
        and "behindSubject" in set(settings.phone_render_verified_features)
        and text_behind_subject_phone_client_supported(require_client=require_client)
    )


def speaker_background_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build decode a recipe's ``speaker_background``?

    At least ``speaker_background_min_client_protocol`` (27) knows the key; an older build
    refuses the whole recipe (`rejectUnknownAssetFields`). ``require_client`` ``None``
    applies the gate only inside an HTTP request (workers have no app build to qualify);
    pass ``True``/``False`` to force it. Mirrors `text_behind_subject_phone_client_supported`.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.speaker_background_min_client_protocol


def speaker_background_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "may a phone recipe carry ``speaker_background`` right now".

    True iff the kill switch (``speaker_background_enabled``, default off) is on,
    ``speakerBackground`` is in ``phone_render_verified_features`` (the device-parity
    gate) and the app-build gate holds (`speaker_background_client_supported`). When
    False, the compile strips the lane and the edit keeps the creator's own background.
    """
    return bool(
        settings.speaker_background_enabled
        and "speakerBackground" in set(settings.phone_render_verified_features)
        and speaker_background_client_supported(require_client=require_client)
    )


def speaker_background_unavailable_reason(*, require_client: bool | None = None) -> str | None:
    """Why `speaker_background_supported` is False, or None when the lane is open.

    Same three checks in the same order, named for the job-debug trace
    (`speaker_background_phone_fallback`) and the editor's 422/426 answers.
    """
    if not settings.speaker_background_enabled:
        return "disabled"
    if "speakerBackground" not in set(settings.phone_render_verified_features):
        return "token_not_verified"
    if not speaker_background_client_supported(require_client=require_client):
        return "client_protocol_below_min"
    return None


def recipe_has_clip_tilt(recipe: Any) -> bool:
    """Does any clip of this recipe turn (`rotation_degrees != 0`) or carry a `backdrop`?"""
    return any(
        clip.transform.rotation_degrees != 0 or getattr(clip, "backdrop", None) is not None
        for track in getattr(recipe, "tracks", None) or []
        for clip in track.clips
    )


def clip_tilt_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build decode a recipe's ``clipTilt`` capability?

    At least ``media_tilt_min_client_protocol`` (28) knows the capability token; an older
    build cannot decode ``required_capabilities`` and fails the recipe. ``require_client``
    ``None`` applies the gate only inside an HTTP request (workers have no app build to
    qualify); pass ``True``/``False`` to force it.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.media_tilt_min_client_protocol


def recipe_has_clip_backdrop_media(recipe: Any) -> bool:
    """Does any clip of this recipe draw a Visuals-pool photo or video behind it?"""
    return any(
        getattr(getattr(clip, "backdrop", None), "kind", None) == "media"
        for track in getattr(recipe, "tracks", None) or []
        for clip in track.clips
    )


def clip_backdrop_media_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build decode a clip backdrop of kind ``media``?

    At least ``media_backdrop_min_client_protocol`` (31) knows the ``clipBackdropMedia``
    token and the ``asset_id`` key; an older build fails the whole recipe on either.
    ``require_client`` follows `clip_tilt_client_supported`.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.media_backdrop_min_client_protocol


def clip_backdrop_media_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "may a tilted clip take a pool photo or video behind it".

    The tilt gate (`clip_tilt_supported`: ``media_tilt_enabled``, ``clipTilt`` verified, a
    build at protocol 28) plus ``clipBackdropMedia`` in ``phone_render_verified_features`` and
    a build at protocol 31.
    """
    return bool(
        clip_tilt_supported(require_client=require_client)
        and "clipBackdropMedia" in set(settings.phone_render_verified_features)
        and clip_backdrop_media_client_supported(require_client=require_client)
    )


def clip_tilt_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "may a phone edit be tilted right now".

    True iff the kill switch (``media_tilt_enabled``, default off) is on, ``clipTilt`` is in
    ``phone_render_verified_features`` (the device-parity gate) and the app-build gate holds
    (`clip_tilt_client_supported`).
    """
    return bool(
        settings.media_tilt_enabled
        and "clipTilt" in set(settings.phone_render_verified_features)
        and clip_tilt_client_supported(require_client=require_client)
    )


# Variant field an HTTP editor Save writes when it compiled one-word captions for a build
# that declared the protocol (`single_word_captions_enabled`): a later compile with no
# request (a worker applying a chat edit) has no app build to ask, so it trusts this.
SINGLE_WORD_CAPTIONS_STAMP = "caption_single_word_text"


def recipe_has_single_word_text(recipe: Any) -> bool:
    """Does any text layer of this recipe use `karaoke.single_word`?"""
    return any(
        layer.karaoke is not None and layer.karaoke.single_word
        for layer in getattr(recipe, "text_layers", None) or []
    )


def single_word_text_client_supported(*, require_client: bool | None = None) -> bool:
    """Does the requesting app build decode `karaoke.single_word`?

    The build declares ``X-Kria-Client-Protocol``; at least
    ``single_word_text_min_client_protocol`` (11) understands it. ``require_client``
    ``None`` applies the gate only inside an HTTP request (workers have no app build to
    qualify); pass ``True``/``False`` to force it. Mirrors `text_bend_client_supported`.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.single_word_text_min_client_protocol


def single_word_text_verified() -> bool:
    """Is one-word captions' device parity verified (the production gate)?

    The mode is an ``active_only`` karaoke layer, which `EditRecipeV2` also tags
    ``authoredText``, so both must be verified: otherwise the recipe would be refused
    where today's full-line sweep is not.
    """
    verified = set(settings.phone_render_verified_features)
    return {"singleWordText", "authoredText"} <= verified


def single_word_captions_enabled(variant: dict | None = None) -> bool:
    """May caption compilation emit `Display: Word` as one-word layers right now?

    Off (the production default) unless ``singleWordText`` (and ``authoredText``) are in
    ``phone_render_verified_features``; when it is off, captions compile to the full-line
    karaoke sweep exactly as before. When on, the compile must know the app build can decode
    the shape: inside an HTTP request that is the declared ``X-Kria-Client-Protocol``;
    outside one (workers: chat-edit approvals) the build is unknown, so the answer is the
    stamp (`SINGLE_WORD_CAPTIONS_STAMP`) an earlier qualifying Save left on the variant.
    """
    from app.services.client_protocol import in_http_request  # noqa: PLC0415

    if not single_word_text_verified():
        return False
    if in_http_request():
        return single_word_text_client_supported(require_client=True)
    return bool(variant and variant.get(SINGLE_WORD_CAPTIONS_STAMP) is True)


def text_elements_have_bend(elements: object) -> bool:
    """Does a stored ``text_elements`` lane carry any non-zero ``bend``?"""
    if not isinstance(elements, list):
        return False
    return any(
        isinstance(element, dict) and _is_nonzero_bend(element.get("bend")) for element in elements
    )


def _is_nonzero_bend(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value != 0


def phone_user_song_supported() -> bool:
    """Single source of truth for "can a creator-uploaded song render on the phone"
    (KRI-374): the kill switch plus the two device capabilities the song lane
    needs. The manifest, the attach route's capability advertisement, the dispatch
    gate and the worker all call this so they cannot disagree.
    """
    from app.schemas.user_song import USER_SONG_REQUIRED_CAPABILITIES

    return bool(
        settings.user_song_montage_enabled
        and USER_SONG_REQUIRED_CAPABILITIES.issubset(settings.phone_render_verified_features)
    )


# Device features the subtitled overlay lane needs at compile + validation
# time: `compile_phone_subtitled_plan` adds `visualBlocks`/`alphaOverlay`/
# `audioMix` to `required_capabilities` for the `subtitled-overlays` track,
# `validate_phone_pilot_recipe(allow_editor_media=True)` still requires
# `visualBlocks`, and `_run_phone_subtitled_job` only binds image cards when
# `stillImages` is verified. Kept as one tuple so the manifest gate below and
# the worker can never disagree on the list.
PHONE_SUBTITLED_OVERLAY_FEATURES: tuple[str, ...] = (
    "stillImages",
    "visualBlocks",
    "alphaOverlay",
    "audioMix",
)


def phone_subtitled_overlays_supported() -> bool:
    """Single source of truth for "can a phone-rendered `subtitled`
    (talk-to-camera) edit carry picture-in-picture Visuals cards right now"
    (KRI-176).

    Consulted by `app.services.creator_capabilities.resolve_creator_manifest`
    (whether `media_overlays` is advertised on a phone `subtitled` manifest
    instead of the blanket `unsupported_on_phone` refusal) and by
    `app.tasks.generative_build._run_phone_subtitled_job` (whether the worker
    grounds overlay cards from the transcript at all). A manifest that
    advertises the lane while the worker skips it -- or vice versa -- is
    exactly the drift this helper exists to prevent.

    True iff ALL of:
      - `phone_subtitled_media_lanes_enabled` (KRI-174 Phase 1 lane flag; the
        compiler/runner only honour overlay lanes behind it).
      - `media_overlays_enabled` (the generic overlay feature gate, cloud and
        phone alike).
      - every feature in `PHONE_SUBTITLED_OVERLAY_FEATURES` is in
        `phone_render_verified_features` (device-parity gate; a rollout flag
        alone never skips it).

    Deliberately says nothing about the edit format or clip count -- callers
    combine it with their own `subtitled` shape check.
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_subtitled_media_lanes_enabled
        and settings.media_overlays_enabled
        and all(feature in verified for feature in PHONE_SUBTITLED_OVERLAY_FEATURES)
    )


# Device features the reaction-beats lane needs on top of the overlay lane:
# `soundEffects` (SFX-track compositing) and `audioMix` (already required by
# the overlay lane, listed again here so this tuple alone documents what the
# beats lane specifically adds).
PHONE_SUBTITLED_SFX_FEATURES: tuple[str, ...] = ("soundEffects", "audioMix")


def phone_subtitled_reaction_beats_supported() -> bool:
    """Single source of truth for "can a phone-rendered `subtitled`
    (talk-to-camera) edit carry name-triggered reaction beats (photo/sticker
    pop-ins + sound effects timed to the transcript, plus a held closing
    shot) right now" (KRI-178).

    Consulted by `app.services.creator_capabilities.resolve_creator_manifest`
    (whether `reaction_beats` is advertised on a phone `subtitled` manifest
    instead of the blanket `unsupported_on_phone`/`phone_talking_only`
    refusal) and by `app.tasks.generative_build._run_phone_subtitled_job`
    (whether the worker grounds reaction beats from the transcript at all).
    A manifest that advertises the lane while the worker skips it -- or vice
    versa -- is exactly the drift this helper exists to prevent.

    True iff ALL of:
      - `phone_subtitled_reaction_beats_enabled` (KRI-178 lane flag).
      - `phone_subtitled_overlays_supported()` (the beats lane reuses the
        KRI-176 overlay lane's grounding primitives -- media_overlays, and
        every feature in `PHONE_SUBTITLED_OVERLAY_FEATURES`).
      - `sound_effects_enabled` (the generic SFX feature gate, cloud and
        phone alike).
      - every feature in `PHONE_SUBTITLED_SFX_FEATURES` is in
        `phone_render_verified_features` (device-parity gate; a rollout flag
        alone never skips it).

    Deliberately says nothing about the edit format or clip count -- callers
    combine it with their own `subtitled` shape check.
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_subtitled_reaction_beats_enabled
        and phone_subtitled_overlays_supported()
        and settings.sound_effects_enabled
        and all(feature in verified for feature in PHONE_SUBTITLED_SFX_FEATURES)
    )


# Device features an editable phone `subtitled` Save needs (KRI-182 step 1):
# a superset of `PHONE_SUBTITLED_OVERLAY_FEATURES` plus `soundEffects` --
# Save can recompile both the overlay lane and the sfx lane from the
# committed editor sections in one commit, so both device-parity gates must
# hold before either section is advertised as editable. Kept as one tuple so
# the manifest gate and `prepare_phone_editor_commit` can never disagree.
PHONE_SUBTITLED_EDITOR_FEATURES: tuple[str, ...] = (
    "stillImages",
    "visualBlocks",
    "alphaOverlay",
    "audioMix",
    "soundEffects",
)


def phone_subtitled_editor_lanes_supported() -> bool:
    """Single source of truth for "can a phone-rendered `subtitled` (Talking
    to camera) variant's `sound_effects`/`media_overlays` editor sections be
    Saved right now" (KRI-182 step 1).

    Consulted by `app.services.phone_editor.prepare_phone_editor_commit`
    (whether the subtitled branch recompiles
    `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan` from the
    committed editor sections, or 422s `unsupported_phone_edit`) and by
    `app.routes.generative_jobs._clamp_phone_editor_capabilities` (whether
    `sfx`/`overlays` stay open instead of closed for a subtitled device
    variant). A manifest that advertises the lanes while Save still 422s --
    or vice versa -- is exactly the drift this helper exists to prevent.

    True iff ALL of:
      - `phone_subtitled_editor_lanes_enabled` (this feature's own rollout
        flag; independently reversible from the two below).
      - `phone_subtitled_media_lanes_enabled` (the underlying KRI-174 lane
        compiler must already be live -- an editor Save that recompiles a
        lanes request is pointless if the worker itself never honours one).
      - every feature in `PHONE_SUBTITLED_EDITOR_FEATURES` is in
        `phone_render_verified_features` (device-parity gate; a rollout flag
        alone never skips it).

    Deliberately says nothing about the edit format or clip count -- callers
    combine it with their own `subtitled` shape check.
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_subtitled_editor_lanes_enabled
        and settings.phone_subtitled_media_lanes_enabled
        and all(feature in verified for feature in PHONE_SUBTITLED_EDITOR_FEATURES)
    )


# Device features a phone Voiceover (`narrated` / montage `voiceover`) Save needs
# to recompile its sound-effect and Visuals lanes (KRI-281): the same set the
# Talking lanes need, since both reuse `phone_subtitled_plan`'s lane compilers.
PHONE_VOICEOVER_EDITOR_FEATURES: tuple[str, ...] = PHONE_SUBTITLED_EDITOR_FEATURES


def phone_voiceover_editor_lanes_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "can a phone-rendered Voiceover variant
    (`narrated` or montage `voiceover`) carry editable `sound_effects` /
    `media_overlays` right now" (KRI-281, mirrors KRI-182 for Talking).

    Consulted by `app.routes.generative_jobs._clamp_phone_editor_capabilities`
    (whether `sfx`/`overlays` stay open) and by
    `app.services.phone_editor.prepare_phone_editor_commit` (whether Save
    recompiles the lanes or 422s), so capabilities never advertise a lane
    Save would refuse.

    True iff ALL of:
      - `phone_voiceover_editor_lanes_enabled` (this feature's own flag).
      - `sound_effects_enabled` and `media_overlays_enabled` (the generic
        lane kill switches, cloud and phone alike).
      - every feature in `PHONE_VOICEOVER_EDITOR_FEATURES` is in
        `phone_render_verified_features` (device-parity gate).
      - the app-build gate, when it applies: the request declares an
        `X-Kria-Client-Protocol` of at least
        `phone_voiceover_editor_min_client_protocol`, i.e. a build that
        hydrates the locked source clips these lanes edit. ``require_client``
        ``None`` (default) applies it only inside an HTTP request, so older
        builds stay closed while worker / sync callers (copilot ops, runtime
        approvals, SFX path resolution) are judged on the server-side gates
        alone; pass ``True``/``False`` to force it. Save passes ``False``.
    """
    from app.services.client_protocol import (  # noqa: PLC0415
        current_client_protocol,
        in_http_request,
    )

    verified = set(settings.phone_render_verified_features)
    if not (
        settings.phone_voiceover_editor_lanes_enabled
        and settings.sound_effects_enabled
        and settings.media_overlays_enabled
        and all(feature in verified for feature in PHONE_VOICEOVER_EDITOR_FEATURES)
    ):
        return False
    if require_client is None:
        require_client = in_http_request()
    if not require_client:
        return True
    protocol = current_client_protocol()
    return protocol is not None and protocol >= settings.phone_voiceover_editor_min_client_protocol


# Device features a phone guided / montage story needs to carry a sound-effect
# lane (KRI-608): the compiled `sfx` track is the same one Talking plays.
PHONE_GUIDED_SFX_FEATURES: tuple[str, ...] = PHONE_SUBTITLED_SFX_FEATURES


def phone_guided_sfx_supported() -> bool:
    """Single source of truth for "can a phone-rendered guided / montage story
    carry sound effects right now" (KRI-608).

    Consulted by `app.pipeline`'s worker (`_run_phone_guided_job`), by
    `app.services.phone_editor.prepare_phone_editor_commit` (Save recompiles
    the lane or 422s), by `routes.generative_jobs._editor_capabilities`
    (whether `sfx` opens on a guided device variant) and by
    `services.creator_capabilities` (whether creation may plan a licensed
    effect), so capabilities never advertise a lane Save would refuse.

    True iff ALL of:
      - `phone_guided_sfx_enabled` (this feature's own flag).
      - `sound_effects_enabled` (the generic lane kill switch).
      - every feature in `PHONE_GUIDED_SFX_FEATURES` is in
        `phone_render_verified_features` (device-parity gate).

    No client-protocol gate: shipped builds already play any `kind: audio`
    track and fetch `sound_effect` library assets (KRI-182).
    """
    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_guided_sfx_enabled
        and settings.sound_effects_enabled
        and all(feature in verified for feature in PHONE_GUIDED_SFX_FEATURES)
    )


def phone_voiceover_reaction_beats_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "can a phone-rendered Voiceover edit
    (`narrated*` with a recorded voiceover) carry KRI-178 reaction beats --
    photo/sticker pop-ins and sound effects at phrases of the voiceover --
    right now" (KRI-519).

    Consulted by `app.services.creator_capabilities.resolve_creator_manifest`
    (whether `reaction_beats` is advertised on a phone Voiceover manifest) and
    by `app.tasks.generative_build._run_phone_narrated_job` (whether the
    worker grounds beats against the voiceover at all), so the Main Creator is
    never offered a lane the worker then skips.

    True iff ALL of:
      - `phone_voiceover_reaction_beats_enabled` (this lane's kill switch).
      - `phone_subtitled_reaction_beats_supported()`: the beats grounding,
        overlay and sound-effect gates Talking already rolls out.
      - `phone_voiceover_editor_lanes_supported(require_client=...)`: the
        beats compile into the KRI-281 Voiceover lanes, so the device and the
        editor can draw and edit them. ``require_client`` means the same as
        there; the worker passes ``False``.
    """
    return bool(
        settings.phone_voiceover_reaction_beats_enabled
        and phone_subtitled_reaction_beats_supported()
        and phone_voiceover_editor_lanes_supported(require_client=require_client)
    )


# Device features an exact-timed small card needs: the same ones the Talking overlay lane
# compiles against (`subtitled-overlays` is the track the card lands on).
PHONE_TIMED_CARD_FEATURES: tuple[str, ...] = PHONE_SUBTITLED_OVERLAY_FEATURES


def phone_timed_cards_supported() -> bool:
    """Single source of truth for "can a phone-rendered Montage (guided / unified
    montage, montage-family voiceover) or narrated Voiceover edit draw a Visuals photo
    as a small picture-in-picture card at an exact second right now".

    Consulted by `app.services.creator_capabilities.resolve_creator_manifest` (whether
    `timed_cards` is advertised), by `repair_creator_timed_cards` (whether a strategy's
    cards survive approval), by the three phone writers in `app.tasks.generative_build`
    (whether the worker grounds and compiles them) and by `phone_editor` (whether a Save
    keeps them), so none advertises a lane another would refuse.

    True iff ALL of:
      - `phone_timed_cards_enabled` (this feature's own flag, default off).
      - `media_overlays_enabled` (the generic overlay lane kill switch).
      - every feature in `PHONE_TIMED_CARD_FEATURES` is in
        `phone_render_verified_features` (device-parity gate).

    Says nothing about the edit format or a recorded voiceover -- callers combine it with
    their own shape check. A VIDEO card additionally needs
    `phone_timed_video_cards_supported()`.
    """
    verified = set(settings.phone_render_verified_features)
    return bool(
        getattr(settings, "phone_timed_cards_enabled", False)
        and settings.media_overlays_enabled
        and all(feature in verified for feature in PHONE_TIMED_CARD_FEATURES)
    )


def phone_clip_sound_windows_supported() -> bool:
    """Can a phone montage silence PART of a clip's own sound right now (KRI-942)?

    True iff `phone_clip_sound_windows_enabled` AND `visualBlocks` is verified (the
    capability recipe `mute_windows` require, `EditRecipeV2._references`). Read by the guided
    phone writer (whether it sends the windows) and by `validate_phone_pilot_recipe` (whether
    a recipe carrying them may pin). Whole-clip on/off never needs it.
    """
    return bool(
        getattr(settings, "phone_clip_sound_windows_enabled", False)
        and "visualBlocks" in settings.phone_render_verified_features
    )


def phone_timed_video_cards_supported() -> bool:
    """`phone_timed_cards_supported()` AND the device plays Visuals videos
    (`visualVideos` verified): a timed card may then bind a video, played muted
    from its start (the same muted-card shape KRI-183 draws)."""
    return phone_timed_cards_supported() and (
        "visualVideos" in set(settings.phone_render_verified_features)
    )


# Device features a photo or video added in the phone editor needs (KRI-287):
# the set the guided editor's own media import is gated on
# (`generative_jobs._phone_editor_media_available`).
PHONE_EDITOR_MEDIA_FEATURES: tuple[str, ...] = (
    "stillImages",
    "visualVideos",
    "visualBlocks",
    "alphaOverlay",
    "audioMix",
)


def phone_voiceover_editor_media_supported(*, require_client: bool | None = None) -> bool:
    """Single source of truth for "can a phone-rendered Voiceover variant
    (`narrated` or montage `voiceover`) import a photo or video as a media
    Visual block right now" (KRI-287, the media half of KRI-281).

    Consulted by `generative_jobs._phone_editor_media_available` (the
    `phone_editor_media` capability, source registration and the
    `visual_blocks` clamp) and by `phone_editor.prepare_phone_editor_commit`
    (whether Save compiles `visual_blocks` or 422s), so the editor never
    offers an import that registration or Save would refuse.

    True iff ALL of:
      - `phone_editor_media_enabled` and `visual_blocks_enabled` (the editor
        media import and the visual-block lane kill switches).
      - every feature in `PHONE_EDITOR_MEDIA_FEATURES` is verified.
      - `phone_voiceover_editor_lanes_supported(require_client=...)`: the
        Voiceover lanes rollout, including its app-build gate. No newer build
        is needed: the protocol-3 app already registers, places, previews and
        reopens editor media for any archetype. ``require_client`` has the
        same meaning there; Save and the source-admission worker pass
        ``False``.
    """
    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_editor_media_enabled
        and settings.visual_blocks_enabled
        and all(feature in verified for feature in PHONE_EDITOR_MEDIA_FEATURES)
        and phone_voiceover_editor_lanes_supported(require_client=require_client)
    )


# Device features every phone caption layer needs (KRI-280).
PHONE_NARRATED_CAPTION_FEATURES: tuple[str, ...] = ("positionedText", "animatedText")


def phone_narrated_caption_edits_supported() -> bool:
    """Single source of truth for "can a phone-rendered `narrated` (recorded
    voiceover) variant's `caption_cues`/`caption_meta` editor sections be
    Saved right now" (KRI-280).

    Consulted by `app.services.phone_editor.prepare_phone_editor_commit`
    (whether the narrated branch swaps the pinned recipe's caption layers or
    422s `unsupported_phone_edit`) and by `app.routes.generative_jobs`
    (`_prepare_editor_commit`'s caption guards and
    `_clamp_phone_editor_capabilities`), so the manifest never advertises a
    caption control that Save would refuse.

    True iff `phone_narrated_caption_edits_enabled` AND every feature in
    `PHONE_NARRATED_CAPTION_FEATURES` is verified: every caption layer is
    positioned text that animates (`pop-in` sentences, `karaoke-line` words).
    A Save is still checked against the verified features by
    `validate_phone_pilot_recipe` (a variable caption font adds
    `authoredText`).
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_narrated_caption_edits_enabled
        and all(feature in verified for feature in PHONE_NARRATED_CAPTION_FEATURES)
    )


def phone_narrated_title_supported() -> bool:
    """Can a phone-rendered `narrated` edit carry a confirmed opening title
    (KRI-455)? The title is one fade-in positioned text layer, so it needs the
    same device features as the captions beside it.
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_narrated_title_enabled
        and all(feature in verified for feature in PHONE_NARRATED_CAPTION_FEATURES)
    )


def phone_subtitled_title_supported() -> bool:
    """Can a phone-rendered `subtitled` (Talking) edit carry an opening title
    and a text lane (KRI-467)? The title is one fade-in positioned text layer,
    like the narrated one, so it needs the same device features as captions.
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_subtitled_title_enabled
        and all(feature in verified for feature in PHONE_NARRATED_CAPTION_FEATURES)
    )


def phone_subtitled_closing_title_supported() -> bool:
    """Can a phone-rendered `subtitled` (Talking) edit carry the creator's
    closing text (KRI-514)? It is a text row on the KRI-467 lane drawn as a
    tag with a background, which the device draws with `authoredText`."""

    return bool(
        settings.phone_subtitled_closing_title_enabled
        and phone_subtitled_title_supported()
        and "authoredText" in settings.phone_render_verified_features
    )


def phone_subtitled_spoken_text_supported() -> bool:
    """Can a phone-rendered `subtitled` (Talking) edit show a creator's text at
    the word they say (KRI-707)? It is a plain text row on the KRI-467 lane,
    drawn like the title (one fade-in positioned text layer)."""

    return bool(settings.phone_subtitled_spoken_text_enabled and phone_subtitled_title_supported())


def phone_narrated_title_preview_supported() -> bool:
    """Can the requesting app build show a phone `narrated` edit's opening
    title in its editor preview (KRI-455)?

    The title may be read-only there (KRI-455, or whenever
    `phone_narrated_title_edits_supported` is False), so only builds whose
    `X-Kria-Client-Protocol` is at least
    `phone_narrated_title_preview_min_client_protocol` get it: they keep a
    `read_only` element out of every editing control and edit an unmarked one.
    Outside an HTTP request there is no app build to qualify, so the answer is
    False.
    """
    from app.services.client_protocol import current_client_protocol  # noqa: PLC0415

    protocol = current_client_protocol()
    return (
        protocol is not None
        and protocol >= settings.phone_narrated_title_preview_min_client_protocol
    )


def phone_narrated_title_edits_supported(*, require_client: bool = True) -> bool:
    """Can a phone-rendered `narrated` edit's opening title be edited and
    deleted in the editor right now (KRI-465)?

    Consulted by `generative_jobs._with_phone_narrated_title` (whether the title
    comes out `read_only`), `_clamp_phone_editor_capabilities` (whether
    `text_elements` opens) and `phone_editor._compile_narrated_editor_commit`
    (whether a `text_elements` Save recompiles the title or 422s), so the map
    never advertises an edit Save would refuse.

    True iff ALL of:
      - `phone_narrated_title_edits_enabled` (this feature's own flag). Not
        `phone_narrated_title_enabled`: that is the planner's gate, and a title
        that was already rendered stays editable.
      - every feature in `PHONE_NARRATED_CAPTION_FEATURES` is verified (the
        title compiles to the same positioned, animated text layers).
      - the app-build gate, when ``require_client`` (the default): the request
        declares a protocol of at least
        `phone_narrated_title_preview_min_client_protocol`, a build that
        understands an editable title element. Save and other worker-side
        callers pass ``False`` and are judged on the server gates alone.
    """
    verified = set(settings.phone_render_verified_features)
    if not (
        settings.phone_narrated_title_edits_enabled
        and all(feature in verified for feature in PHONE_NARRATED_CAPTION_FEATURES)
    ):
        return False
    return phone_narrated_title_preview_supported() if require_client else True


# Device feature the video-PiP lane needs on top of the overlay lane: the
# compiler emits a video overlay card as a muted, trimmed `TimelineClip` on
# the `subtitled-overlays` track, which the device composites through the
# same `visualVideos` path the ending clip already uses.
PHONE_SUBTITLED_VIDEO_OVERLAY_FEATURES: tuple[str, ...] = ("visualVideos",)


def phone_subtitled_video_overlays_supported() -> bool:
    """Single source of truth for "may a phone-rendered `subtitled`
    (talk-to-camera) edit carry VIDEO Visuals as picture-in-picture cards
    right now" (KRI-183).

    Consulted by `app.tasks.generative_build._run_phone_subtitled_job`
    (whether transcript grounding offers video Visuals to the matcher and
    binds them as `kind="video"` cards) and by
    `app.services.creator_capabilities.resolve_creator_manifest` (whether the
    phone `subtitled` manifest's overlay capability advertises videos). A
    manifest that advertises video cards while the worker still reports them
    `video_not_supported` -- or vice versa -- is exactly the drift this
    helper exists to prevent.

    True iff ALL of:
      - `phone_subtitled_video_overlays_enabled` (KRI-183 lane flag).
      - `phone_subtitled_overlays_supported()` (video cards ride the same
        overlay track, gates, and grounding as photo cards).
      - every feature in `PHONE_SUBTITLED_VIDEO_OVERLAY_FEATURES` is in
        `phone_render_verified_features` (device-parity gate; a rollout flag
        alone never skips it).
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_subtitled_video_overlays_enabled
        and phone_subtitled_overlays_supported()
        and all(feature in verified for feature in PHONE_SUBTITLED_VIDEO_OVERLAY_FEATURES)
    )


# KRI-136: device features a multi-clip Talking-head recipe needs -- the
# cutaway track is placed (`visualBlocks`), video (`visualVideos`), represented
# as an overlay (`alphaOverlay` is conservatively inferred natively for every
# non-empty overlay track), and muted under the speaker's audio (`audioMix`).
PHONE_TALKING_HEAD_FEATURES: tuple[str, ...] = (
    "visualBlocks",
    "visualVideos",
    "alphaOverlay",
    "audioMix",
)


def phone_talking_head_supported() -> bool:
    """Single source of truth for "may a self-narrated item with 2+ clips
    render on the phone as a multi-clip Talking head right now" (KRI-136).

    Consulted by the dispatch gate (`content_plan_build.py`), the planner
    manifest (`creator_capabilities.resolve_creator_manifest`), and the
    worker (`generative_build._run_phone_subtitled_job`), so none of them can
    disagree about it.

    True iff ALL of: `phone_talking_head_rendering_enabled`,
    `narrated_self_narration_enabled` (the only path that resolves
    `talking_head` on the phone), phone `subtitled` rendering (the same
    compiler and caption lane), and every `PHONE_TALKING_HEAD_FEATURES` entry
    in `phone_render_verified_features`.
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        settings.phone_talking_head_rendering_enabled
        and settings.narrated_self_narration_enabled
        and "subtitled" in phone_render_supported_formats()
        and all(feature in verified for feature in PHONE_TALKING_HEAD_FEATURES)
    )


# KRI-719: most clips a declared Talking-to-camera item may carry when
# `phone_talking_multi_clip_supported()` (speakers + cutaways together).
TALKING_MULTI_CLIP_MAX_CLIPS = 10


def phone_talking_multi_clip_supported() -> bool:
    """Single source of truth for "may a declared Talking-to-camera
    (`subtitled`) item carry 2+ clips right now" (KRI-719).

    Consulted by the clip cap (`routes/creation_threads._format_clip_limit`),
    the dispatch gate (`content_plan_build.py`), the planner manifest
    (`creator_capabilities.resolve_creator_manifest`) and the worker
    (`generative_build._run_phone_subtitled_job`), so none of them disagree.

    True iff ALL of: `talking_multi_clip_enabled`, phone `subtitled` rendering,
    and every `PHONE_TALKING_HEAD_FEATURES` entry in
    `phone_render_verified_features` (the cutaway track is the same KRI-136
    shape).
    """

    verified = set(settings.phone_render_verified_features)
    return bool(
        getattr(settings, "talking_multi_clip_enabled", False)
        and "subtitled" in phone_render_supported_formats()
        and all(feature in verified for feature in PHONE_TALKING_HEAD_FEATURES)
    )


def phone_render_supported_formats() -> frozenset[str]:
    """The single source of truth for "which edit formats can render on the
    phone for THIS deployment, right now" -- settings-aware, unlike the
    static `PHONE_RENDER_SUPPORTED_FORMATS` allowlist (compiler existence
    only). Every runtime consumer (the dispatch gate in
    `content_plan_build.py`, the worker's own dispatch fork in
    `generative_build.py`, and the chat format picker in
    `routes/creation_threads.py`) MUST call this instead of reading the
    static constant directly, so the picker, the planner, and the render
    path can never disagree about what actually works right now.

    Always includes the montage family (`GUIDED_EDIT_FORMATS`:
    montage/day_vlog/single_hero) -- unconditional, matching the existing
    KRI-114 precedent; per-item nuances for that family (guided approval,
    voiceover's own `phone_narration_rendering_enabled` gate) are handled
    downstream by the dispatch gate, not here.

    Adds `subtitled` when `settings.phone_subtitled_rendering_enabled` AND
    `settings.subtitled_archetype_enabled` both hold -- the format itself
    must be enabled at all (phone or not) before the phone compiler is
    reachable.

    Adds the whole `NARRATED_EDIT_FORMATS` family (narrated/narrated_planned/
    narrated_ready) when `settings.phone_narrated_rendering_enabled` AND
    `settings.phone_narration_rendering_enabled` AND `"narrationAudio"` is in
    `settings.phone_render_verified_features` AND
    `settings.narrated_archetype_enabled` all hold. The narrated family
    reuses the SAME narration-audio device capability and rollout flag the
    montage-family voiceover render already uses (`phone_narration_rendering
    _enabled` / `narrationAudio`) -- a phone deployment that hasn't verified
    on-device narration audio at all cannot render narrated either.

    This function only ever says whether the DECLARED format has a reachable
    phone compiler -- it says nothing about per-item nuances a caller must
    still check itself: `subtitled` additionally requires exactly one clip
    (`compile_phone_subtitled_plan` also enforces this, but the dispatch gate
    checks it earlier to fail closed before a Job is minted); a `narrated*`
    item with NO recorded voiceover only reaches the phone through a
    dispatch-gate-checked exception (self-narration onto exactly one clip,
    which resolves to `subtitled`, or -- KRI-136, when
    `phone_talking_head_supported()` -- onto 2+ clips, which resolves to
    `talking_head` and renders through the same Subtitled compiler with
    cutaways). A DECLARED `talking_head` format is never included in this
    set under any settings combination.
    """

    formats: set[str] = set(GUIDED_EDIT_FORMATS)
    if settings.phone_subtitled_rendering_enabled and settings.subtitled_archetype_enabled:
        formats.add("subtitled")
    if (
        settings.phone_narrated_rendering_enabled
        and settings.phone_narration_rendering_enabled
        and "narrationAudio" in settings.phone_render_verified_features
        and getattr(settings, "narrated_archetype_enabled", False)
    ):
        formats |= NARRATED_EDIT_FORMATS
    # Defense in depth: never advertise a format the static compiler-existence
    # allowlist doesn't even recognize, regardless of how settings resolve.
    return frozenset(formats) & PHONE_RENDER_SUPPORTED_FORMATS


def phone_voiceover_spoken_text_supported(*, require_client: bool = True) -> bool:
    """Use the existing editable narrated text lane for grounded spoken text."""
    return bool(
        settings.phone_voiceover_spoken_text_enabled
        and phone_narrated_title_edits_supported(require_client=require_client)
    )


def phone_voiceover_source_audio_supported(*, require_client: bool = True) -> bool:
    """New authoring requires a preview that preserves selected camera sound."""
    if not (
        settings.phone_voiceover_source_audio_enabled
        and "audioMix" in settings.phone_render_verified_features
    ):
        return False
    if not require_client:
        return True
    from app.services.client_protocol import current_client_protocol

    protocol = current_client_protocol()
    return (
        protocol is not None
        and protocol >= settings.phone_voiceover_source_audio_min_client_protocol
    )
