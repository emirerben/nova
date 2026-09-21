"""Project a decided montage-family generative variant into a native device
render program (KRI-114 P1-2/P1-3).

Companion to `app.pipeline.phone_guided_plan.compile_phone_guided_plan` for
the montage / day_vlog / single_hero archetypes -- see
`app.pipeline.generative_decision.GenerativeVariantDecision`'s module
docstring for what the decision phase (`_decide_generative_variant`) has
already resolved by the time it reaches here. Exactly like the guided
compiler, no media is downloaded or rendered in this module; unsupported
montage-family lanes (masonry/collage presets, lyric overlays, carousel-
moment splices, letterboxed landscape fit, audio ducking, an unexpressible
intro text style, an unpublished/unresolved music track) fail closed with
`UnsupportedPhonePlan` until their native implementations and parity
fixtures exist -- see docs/runbooks/phone-rendering.md.
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
    Transition,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import LibraryRenderAsset, RenderAssetManifest, VoiceoverRenderAsset
from app.pipeline.canvas import Canvas as PipelineCanvas
from app.pipeline.canvas import canvas_for_orientation
from app.pipeline.generative_decision import GenerativeVariantDecision
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import (
    EXPORT_SAFETY_MARGIN_S,
    PhoneMusicBed,
    PhoneNarrationBed,
    refit_source_window,
)
from app.services.phone_sources import PhoneSourceBinding

# Mirrors `app.tasks.template_orchestrate._VOICEOVER_MUSIC_BED_MAX_GAIN`: a
# matched-track bed under a recorded voiceover can never overpower the
# narration, no matter where the mix slider sits. Kept in sync with that
# constant's value (not imported -- this module never imports the cloud
# orchestrator).
_VOICEOVER_MUSIC_BED_MAX_GAIN = 0.5

# `GenerativeAssemblyStepDecision.transition_in` values observed from the
# montage matcher's slot vocabulary are "cut"/"none"/"crossfade" (see
# `_assembly_step_to_decision`); the fuller Gemini/template vocabulary
# (dip_to_black/flash/wipe_*) is mapped here too so a future matcher change
# that starts emitting it degrades to an explicit reject instead of an
# incorrect cut. Kept a strict positive allowlist on purpose.
_TRANSITION_NAMES: dict[str, str] = {
    "crossfade": "crossfade",
    "dip_to_black": "fade_black",
    "fade_black": "fade_black",
    "flash": "fade_white",
    "fade_white": "fade_white",
    "wipe_left": "wipe_left",
    "wipe_right": "wipe_right",
}
_NO_TRANSITION = {None, "none", "cut"}
_DEFAULT_TRANSITION_DURATION_S = 0.35

# Mirrors `app.tasks.generative_build.MAX_INTRO_S`. Not imported directly:
# that module imports this one (compiling the montage phone recipe happens
# from inside `_run_phone_montage_job`), so a top-level import back would be
# circular. `phone_guided_plan.py` makes the same call for its own timing
# constants -- keep this one in sync if the cloud value ever changes.
_MAX_INTRO_S = 3.0

_SUPPORTED_TEXT_MODES = {"agent_text", "none"}


def compile_phone_montage_plan(
    decision: GenerativeVariantDecision,
    bindings: tuple[PhoneSourceBinding, ...],
    *,
    music: PhoneMusicBed | None = None,
    narration: PhoneNarrationBed | None = None,
) -> EditRecipeV2:
    """See the module docstring for the general contract.

    `narration` (KRI-132): required whenever `decision.extras
    ["voiceover_gcs_path"]` is set (the montage-family "voiceover" archetype
    -- see `_specs_for_archetype`/`_resolve_archetype` in
    `app.tasks.generative_build`), resolved by that module's
    `_resolve_phone_voiceover_bed` the same way `music` is resolved by
    `_resolve_phone_music_bed`. Compiles to a `VoiceoverRenderAsset` + a
    second `TimelineTrack(id="narration", kind="audio")`, mirroring
    `_mix_user_voiceover`'s gain math exactly (voice always at full volume;
    the bed -- footage's own audio, or a matched-track music bed -- is
    attenuated by `1 - decision.mix`, with a matched-track bed additionally
    capped at `_VOICEOVER_MUSIC_BED_MAX_GAIN` so it can never bury the
    voice). A voiceover with no binding fails closed with
    `UnsupportedPhonePlan(capability="narrationAudio")`, same as every other
    lane this compiler cannot yet express.
    """
    from app.pipeline.generative_overlays import build_persistent_intro_overlays
    from app.pipeline.portable_text_layout import compile_text_overlay

    if decision.text_mode not in _SUPPORTED_TEXT_MODES:
        raise UnsupportedPhonePlan(f"unsupported text_mode: {decision.text_mode}")
    extras = decision.extras
    if extras.get("masonry_requested"):
        raise UnsupportedPhonePlan(
            "masonry/collage presets are not yet supported on the phone",
            capability="mediaCards",
        )
    if extras.get("lyrics_rendered") or decision.text_mode == "lyrics":
        raise UnsupportedPhonePlan(
            "lyric overlays are not yet supported on the phone", capability="musicBed"
        )
    if decision.base.get("carousel_moment"):
        raise UnsupportedPhonePlan(
            "carousel moments are not yet supported on the phone",
            capability="carouselEffects",
        )
    landscape_fit = extras.get("assembly_landscape_fit")
    if landscape_fit not in (None, "fill"):
        raise UnsupportedPhonePlan("letterboxed landscape fit is not yet supported on the phone")
    if extras.get("duck_original_during_music"):
        raise UnsupportedPhonePlan(
            "audio ducking is not yet supported on the phone", capability="audioDucking"
        )
    if not decision.assembly_steps:
        raise ValueError("phone montage plan has no assembly steps")

    bindings_by_media_id = {binding.media_id: binding for binding in bindings}
    if len(bindings_by_media_id) != len(bindings):
        raise ValueError("phone bindings must have unique media identities")
    clip_id_to_media_id: dict[str, str] = dict(extras.get("clip_id_to_media_id") or {})

    canvas_dims = extras.get("canvas")
    pipeline_canvas = (
        PipelineCanvas(**canvas_dims)
        if canvas_dims
        else canvas_for_orientation(decision.orientation)
    )
    story_canvas = Canvas(width=pipeline_canvas.width, height=pipeline_canvas.height)

    color_grade = str((extras.get("recipe") or {}).get("color_grade") or "none")
    if color_grade not in {"none", "golden_hour"}:
        raise UnsupportedPhonePlan(f"unsupported color grade: {color_grade}")

    assets: dict[str, MediaAsset] = {}
    manifest: dict[str, object] = {}
    clips: list[TimelineClip] = []
    cursor = 0.0
    for index, step in enumerate(decision.assembly_steps):
        media_id = clip_id_to_media_id.get(step.clip_id)
        binding = bindings_by_media_id.get(media_id) if media_id else None
        if binding is None:
            raise UnsupportedPhonePlan("assembly step has no phone source binding")
        if step.in_s is None or (step.out_s is None and step.target_duration_s is None):
            raise UnsupportedPhonePlan("assembly step is missing its exact source window")
        source_start = float(step.in_s)
        source_duration = float(
            step.target_duration_s if step.target_duration_s is not None else step.out_s - step.in_s
        )
        if source_duration <= 0:
            raise UnsupportedPhonePlan("assembly step has a non-positive duration")
        rate = float(step.rate) if step.rate is not None else 1.0
        if rate <= 0:
            raise UnsupportedPhonePlan("assembly step rate must be positive")

        original = binding.original
        if source_start + source_duration > original.duration_s or source_start >= (
            original.duration_s
        ):
            # See `refit_source_window`'s docstring: the proxy (server
            # ffprobe) and the original (on-device) measurements of
            # conceptually the same file routinely disagree.
            try:
                source_start, source_duration = refit_source_window(
                    source_start, source_duration, original.duration_s
                )
            except ValueError as exc:
                raise UnsupportedPhonePlan(
                    "phone moment timing must preserve its exact source window"
                ) from exc

        if color_grade == "golden_hour" and (
            (original.width, original.height) != (story_canvas.width, story_canvas.height)
            or original.orientation_degrees != 0
        ):
            raise UnsupportedPhonePlan("phone looks require exact-canvas unrotated sources")

        transition = None
        timeline_start = cursor
        if index > 0 and step.transition_in not in _NO_TRANSITION:
            kind = _TRANSITION_NAMES.get(str(step.transition_in))
            if kind is None:
                raise UnsupportedPhonePlan(f"unsupported transition: {step.transition_in}")
            duration = float(step.transition_duration_s or _DEFAULT_TRANSITION_DURATION_S)
            transition = Transition(kind=kind, duration=duration)
            timeline_start = max(0.0, cursor - duration)

        asset = binding.render_asset()
        manifest[asset.id] = asset
        assets[asset.id] = MediaAsset(
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
        clips.append(
            TimelineClip(
                id=f"step-{index}-{step.clip_id}",
                source_asset_id=asset.id,
                source_start=source_start,
                source_duration=source_duration,
                timeline_start=timeline_start,
                rate=rate,
                transition=transition,
                look="golden_hour" if color_grade == "golden_hour" else None,
            )
        )
        cursor = timeline_start + source_duration / rate

    if len({clip.id for clip in clips}) != len(clips):
        raise ValueError("phone montage clips must have unique identities")
    total_duration_s = cursor

    layers = []
    if decision.text_mode == "agent_text":
        at_params = extras.get("intro_overlay_params")
        if at_params:
            # Bound both the reveal and the static hold to slightly inside
            # this recipe's own real duration -- left unbounded, the hold
            # overlay's `end_s` is the "hold to EOF" sentinel (3600s), which
            # is fine for the cloud burn (clipped by the real render) but
            # exceeds `PortableTextLayer.end`'s 1800s cap; and even bounded
            # exactly to `total_duration_s`, the text compiler's own frame-
            # grid rounding can round a fractional-second boundary UP by a
            # sub-frame amount, which would then land just past the
            # timeline's own `duration` and fail the recipe's cross-field
            # validator. The export safety margin (already used for the
            # source-window refit above) comfortably exceeds one video
            # frame at any realistic frame rate.
            text_bound_s = max(0.0, total_duration_s - EXPORT_SAFETY_MARGIN_S)
            reveal_window_s = min(text_bound_s, _MAX_INTRO_S) if text_bound_s > 0 else 0.0
            try:
                overlays = build_persistent_intro_overlays(
                    reveal_window_s=reveal_window_s,
                    beats=list(extras.get("beats") or []),
                    cluster_style=None,
                    canvas=pipeline_canvas,
                    end_s=text_bound_s,
                    **at_params,
                )
            except UnsupportedPhonePlan:
                raise
            except Exception as exc:  # noqa: BLE001 - untrusted decision content
                raise UnsupportedPhonePlan(f"unable to compile intro text: {exc}") from exc
            for layer_index, overlay in enumerate(overlays):
                try:
                    layer, font = compile_text_overlay(
                        overlay,
                        layer_id=f"intro-{layer_index}",
                        canvas=story_canvas,
                        dissolve_seed=101 + layer_index * 37,
                    )
                except Exception as exc:  # noqa: BLE001 - untrusted decision content
                    raise UnsupportedPhonePlan(f"unable to compile intro text: {exc}") from exc
                if font is not None:
                    manifest[font.id] = font
                    assets[font.id] = MediaAsset(
                        id=font.id,
                        relative_path=font.id,
                        fingerprint=AssetFingerprint(
                            hex=font.fingerprint.sha256,
                            byte_count=font.fingerprint.byte_count,
                        ),
                    )
                layers.append(layer)

    tracks = [TimelineTrack(id="montage", kind="video", clips=clips)]
    audio = AudioMixRecipe()
    has_voiceover = bool(extras.get("voiceover_gcs_path"))
    if has_voiceover and narration is None:
        raise UnsupportedPhonePlan(
            "recorded voiceover requires a phone narration binding",
            capability="narrationAudio",
        )
    # Voice-prominence slider (`_VOICEOVER_ONLY_DEFAULT_MIX`/`_VOICEOVER_MUSIC
    # _DEFAULT_MIX` in generative_build.py pick the default; a user override
    # rides `decision.mix` unchanged). Meaningless when there's no voiceover.
    voice_mix = max(0.0, min(1.0, float(decision.mix if decision.mix is not None else 1.0)))
    if decision.music_track_id:
        if music is None:
            raise UnsupportedPhonePlan(
                "music bed metadata is required for a music variant",
                capability="musicBed",
            )
        music_asset_id = f"music-{music.catalog_id}"
        music_asset = LibraryRenderAsset(
            id=music_asset_id,
            catalog="music",
            catalog_id=music.catalog_id,
            generation=music.generation,
            fingerprint=music.fingerprint,
        )
        manifest[music_asset.id] = music_asset
        assets[music_asset.id] = MediaAsset(
            id=music_asset.id,
            relative_path=music_asset.id,
            fingerprint=AssetFingerprint(
                hex=music.fingerprint.sha256, byte_count=music.fingerprint.byte_count
            ),
            duration=music.duration_s,
        )
        # `_mix_template_audio`'s plain song-variant path REPLACES source
        # audio entirely (audio_gain default 1.0, no fade, no ducking) --
        # mirror that exactly. `_mix_user_voiceover`'s `music_gcs_path`
        # branch caps a voiceover's matched-track bed at
        # `_VOICEOVER_MUSIC_BED_MAX_GAIN` and never mixes footage audio in at
        # all -- mirror that too rather than inventing new defaults.
        music_gain = (
            max(0.0, min(1.0 - voice_mix, _VOICEOVER_MUSIC_BED_MAX_GAIN)) if has_voiceover else 1.0
        )
        tracks.append(
            TimelineTrack(
                id="music",
                kind="audio",
                clips=[
                    TimelineClip(
                        id="music-bed",
                        source_asset_id=music_asset.id,
                        source_start=max(0.0, float(music.start_s)),
                        source_duration=max(total_duration_s, 0.1),
                        timeline_start=0.0,
                        rate=1.0,
                        volume=music_gain,
                    )
                ],
            )
        )
        audio = AudioMixRecipe(music_volume=music_gain, original_volume=0.0)

    if has_voiceover and narration is not None:
        # `narration is not None` always holds here (the earlier check
        # raises when `has_voiceover` is True and `narration` is None) --
        # spelled out again so type checkers can narrow it.
        narration_asset = VoiceoverRenderAsset(
            id=f"voiceover-{narration.plan_item_id}",
            plan_item_id=narration.plan_item_id,
            generation=narration.generation,
            fingerprint=narration.fingerprint,
        )
        manifest[narration_asset.id] = narration_asset
        assets[narration_asset.id] = MediaAsset(
            id=narration_asset.id,
            relative_path=narration_asset.id,
            fingerprint=AssetFingerprint(
                hex=narration.fingerprint.sha256, byte_count=narration.fingerprint.byte_count
            ),
            duration=narration.duration_s,
        )
        # Cap to the same window the decide phase sized the footage montage
        # to (`voiceover_target_s` = min(footage, voice, the short-form
        # ceiling) -- see `_decide_generative_variant`), further bounded by
        # what this recipe's own assembled steps actually total. The cloud
        # additionally hard-trims the WHOLE rendered output to this window
        # (`-t` in `_mix_user_voiceover`); on the phone the video timeline is
        # already ~this long by construction (the slots were sized to it),
        # so only the audio needs the defensive cap -- a real mismatch would
        # leave trailing silence rather than a truncated video, a known,
        # accepted phone/cloud difference (see docs/runbooks/phone-rendering.md).
        narration_duration_s = max(
            0.1,
            min(
                float(extras.get("voiceover_target_s") or narration.duration_s),
                max(total_duration_s, 0.1),
            ),
        )
        tracks.append(
            TimelineTrack(
                id="narration",
                kind="audio",
                clips=[
                    TimelineClip(
                        id="narration-voice",
                        source_asset_id=narration_asset.id,
                        source_start=0.0,
                        source_duration=narration_duration_s,
                        timeline_start=0.0,
                        rate=1.0,
                        volume=1.0,
                    )
                ],
            )
        )
        # No matched-track bed: the clips' own audio is the bed, attenuated
        # by `1 - mix` (mix=1.0, the default, fully ducks it -- the voice is
        # the whole track, matching `_mix_user_voiceover`'s `mix >= 0.999`
        # branch). A matched-track bed instead: footage audio is never
        # mixed in at all, matching `_mix_user_voiceover`'s `music_gcs_path`
        # branch exactly.
        footage_bed_gain = 0.0 if decision.music_track_id else max(0.0, 1.0 - voice_mix)
        audio = audio.model_copy(
            update={"narration_asset_id": narration_asset.id, "original_volume": footage_bed_gain}
        )

    required_capabilities = (
        {"basicComposition", "local1080Export"}
        | (
            {"crossfade"}
            if any(clip.transition and clip.transition.kind == "crossfade" for clip in clips)
            else set()
        )
        | ({"positionedText"} if layers else set())
        | (
            {"animatedText"}
            if any(layer.effect not in {"static", "none"} for layer in layers)
            else set()
        )
        | ({"audioMix", "musicBed"} if decision.music_track_id else set())
        | ({"narrationAudio", "audioMix"} if has_voiceover else set())
        | ({"variableSpeed"} if any(clip.rate != 1 for clip in clips) else set())
    )

    return EditRecipeV2(
        canvas=story_canvas,
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=tracks,
        text_layers=layers,
        audio=audio,
        required_capabilities=required_capabilities,
    )
