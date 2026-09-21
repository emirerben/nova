"""Project a decided "narrated" walkthrough generative variant into a native
device render program (KRI-132).

Companion to `app.pipeline.phone_montage_plan.compile_phone_montage_plan` for
the ``narrated`` / ``narrated_planned`` / ``narrated_ready`` archetypes.
Exactly like the other phone compilers, no media is downloaded or rendered in
this module -- it only projects an already-decided plan (step-to-clip
assignments, a resolved voiceover receipt, and editable caption cues) into an
``EditRecipeV2``; unsupported content fails closed with ``UnsupportedPhonePlan``
until its native implementation exists (see `docs/reviews/kri-132/`).

Cloud reference: `app.tasks.generative_build._render_narrated_variant` picks
one clip per narration step, hard-cuts each clip to its step's window
(reflow-slowing a too-short clip instead of freezing), burns synced captions,
and mixes the recorded voiceover over a ducked footage bed
(`app.pipeline.narrated_assembler.assemble_narrated`,
`app.tasks.template_orchestrate._mix_user_voiceover`). This compiler mirrors
the VISUAL half of that exactly (one clip per step, contiguous, same
trim-or-slow decision) and the AUDIO half approximately -- see "Audio mix
approximation" below.

Step-timing contract
---------------------

Callers (`_narrated_script_steps` + `align_script_to_voiceover` for a scripted
narrated/narrated_planned variant, or `contiguous_step_timings` for an
auto-segmented narrated_ready one -- both in
`app.pipeline.narrated_alignment`) already guarantee their ``StepTiming``
list tiles ``[0, voiceover_duration_s]`` contiguously: step 0 starts at 0,
each step ends exactly where the next one starts, and the last step ends at
the voiceover's own duration. `compile_phone_narrated_plan` re-validates that
contract (within `phone_recipe_shared.TIMING_ROUNDING_TOLERANCE_S`) rather
than trusting it blindly -- a caller bug here would otherwise silently
desync the compiled visual timeline from the narration audio.

Short-clip retiming (schema support confirmed)
------------------------------------------------

`TimelineClip.rate` (`app/kria/recipes.py`) already exists and is already
load-bearing: `compile_phone_montage_plan` sets it from
`GenerativeAssemblyStepDecision.rate` and adds the `variableSpeed` capability
whenever any clip's rate isn't 1. This IS a legitimate retime field (a
rate < 1.0 plays a clip slower, stretching its output beyond its source
duration -- exactly the ``output_duration = source_duration / rate`` formula
`EditRecipeV1.duration` already uses). So the schema answer is **yes**, and
this compiler uses it exactly as the cloud path does: `_fit_clip_segment`'s
pure decision (`app/pipeline/narrated_assembler.py:139-200`) is replicated
below as `_fit_step_window` -- trim when the clip has enough footage for its
step (`rate=1.0`), else slow it down (`rate < 1.0`) so its output still
exactly fills the step, never freeze-holding. The cloud helper itself is
NOT imported: `narrated_assembler.py` imports
`app.tasks.template_orchestrate` (an ~8k-line Celery task module, `redis`
import included) at module scope purely for `_mix_user_voiceover`/
`_probe_duration`, which is exactly the kind of heavy, side-effect-bearing
dependency the other phone compilers deliberately avoid. `_fit_step_window`
is a direct, from-scratch reimplementation of the same float math against
`PhoneSourceBinding.original.duration_s` (already a verified, on-device
measurement -- no ffprobe call needed here, unlike the cloud path's runtime
probe).

Landscape footage policy
------------------------

`compile_phone_montage_plan` never rejects a landscape clip outright -- it
only rejects the "letterboxed" (`fit`) landscape presentation, accepting
"fill" (center-crop) for any source orientation; golden-hour color grading is
the only lane that additionally demands an exact-canvas, unrotated source.
Narrated footage is B-roll cut to a spoken narration, not a face-forward
shot, so the same face-tracking concern that would justify a stricter policy
doesn't apply here either. This compiler matches montage's policy exactly:
no orientation check, center-crop fill for whatever the source measures.

Captions
--------

There is no dedicated timed-caption field anywhere in the on-device recipe
contract (`app/kria/recipes.py` / `recipes_v2.py` -- confirmed by
`docs/reviews/kri-132/phone-format-matrix.md`, "Speech captions" row).
Captions are instead expressed through the SAME mechanism static/animated
overlay text already uses: `EditRecipeV2.text_layers` /
`PortableTextLayer`. `app.pipeline.phone_captions.compile_caption_layers`
(companion module, KRI-132) turns the cloud's editable
``caption_cues`` (``[{text, start_s, end_s}]``, already in assembled/
narration-timeline seconds -- see `assemble_narrated`'s return contract) into
ready-to-render `PortableTextLayer`s. This module only calls it when cues are
supplied (an empty-caption narrated render is not an error -- mirrors the
generative pipeline's "best-effort" stance on intro text) and then calls that
module's companion `caption_font_assets(layers)` helper -- its own documented
contract for callers assembling a full recipe -- to resolve + register every
referenced font as a manifest asset, exactly like `phone_montage_plan.py` and
`phone_guided_plan.py` already do with the font asset `compile_text_overlay`
returns directly.

Audio mix approximation (documented divergence, accepted for v1)
------------------------------------------------------------------

Cloud (`_mix_user_voiceover`, `app/tasks/template_orchestrate.py:6501-6601`):
voice at gain 1.0, footage bed side-chain DUCKED under the voice (dips while
narration plays, rises in pauses), loudnorm, and a 0.5s voice fade-out. The
phone engine has no ducking/loudnorm/ramp primitive at all -- only a flat,
constant `AudioMixRecipe.original_volume` for the whole timeline and a flat
per-clip `volume` on the narration track. Exactly mirroring
`compile_phone_montage_plan`'s already-shipped approximation for its own
voiceover case: a single constant attenuated footage-bed gain under a
full-volume voice is the accepted v1 approximation (no ducking, no
loudnorm, no fade). ``mix`` here uses the SAME convention
`GenerativeVariantDecision.mix` does in the montage compiler (1.0 = voice
fully dominant/default, 0.0 = footage bed at its loudest) --
``footage_bed_gain = max(0.0, 1.0 - mix)``. The cloud narrated path's actual
knob is `voiceover_bed_level` (0 = voice-only, ~0.25 default, 1 = loudest --
the DIRECT bed gain, not a voice-prominence slider) -- converting it to this
compiler's ``mix`` is the wiring step's job (``mix = 1.0 - bed_level``), not
resolved in this module.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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
from app.kria.render_assets import RenderAssetManifest, VoiceoverRenderAsset
from app.pipeline.canvas import canvas_for_orientation
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import TIMING_ROUNDING_TOLERANCE_S, PhoneNarrationBed
from app.services.phone_sources import PhoneSourceBinding

# Mirrors `app.pipeline.narrated_assembler._MIN_USABLE_S` / `_EOF_GUARD_S` --
# see the module docstring for why they're reimplemented here rather than
# imported. Below this a clip is treated as unusable/unprobeable (restart at
# source 0); the EOF guard keeps a slowed-down read a hair under the clip's
# true end so it never overshoots.
_MIN_USABLE_S = 0.05
_EOF_GUARD_S = 0.05


class NarratedPhoneStep(BaseModel):
    """One narrated step: its bound clip and its window on the narration
    timeline (voiceover-absolute seconds, contiguous across the whole list --
    see the module docstring's "Step-timing contract").

    ``media_id`` matches a `PhoneSourceBinding.media_id`, exactly like
    `phone_montage_plan`'s ``clip_id_to_media_id`` indirection (the assignment
    step and the binding are resolved by identity, not by trusting a client-
    supplied path). ``source_start_s`` is the clip's own in-point (mirrors
    `app.pipeline.narrated_assembler.NarratedClip.source_start_s`); the
    clip's available footage from there is read off
    `PhoneSourceBinding.original.duration_s`, an already on-device-verified
    measurement, so no separate "usable duration" field is needed here.
    """

    model_config = ConfigDict(extra="forbid")

    step_id: str = Field(min_length=1, max_length=160)
    media_id: str = Field(min_length=1, max_length=160)
    source_start_s: float = Field(default=0.0, ge=0)
    start_s: float = Field(ge=0, le=1800)
    end_s: float = Field(gt=0, le=1800)


def _fit_step_window(
    binding: PhoneSourceBinding, source_start_s: float, target_duration_s: float
) -> tuple[float, float, float]:
    """Reimplements `narrated_assembler._fit_clip_segment`'s pure decision.

    Returns ``(source_start, source_duration, rate)`` such that
    ``source_duration / rate == target_duration_s`` always holds: trim
    (``rate=1.0``) when the clip has enough footage from ``source_start_s``
    onward, else slow the clip down (``rate < 1.0``) so playing out its
    remaining footage exactly fills the step -- the visuals and the voice
    always end together, never a freeze-hold.
    """
    total = float(binding.original.duration_s)
    start = max(0.0, source_start_s)
    available = total - start
    if available <= _MIN_USABLE_S:
        # Assigned in-point sits past (or at) this clip's own measured EOF --
        # restart from 0 exactly like the cloud fallback does.
        start = 0.0
        available = total
    if available <= _MIN_USABLE_S:
        raise UnsupportedPhonePlan("narrated clip has no usable footage for its step")
    if available >= target_duration_s:
        return start, target_duration_s, 1.0
    usable = max(_MIN_USABLE_S, available - _EOF_GUARD_S)
    rate = usable / target_duration_s
    if rate <= 0:
        raise UnsupportedPhonePlan("narrated clip has no usable footage for its step")
    return start, usable, rate


def _register_caption_fonts(
    layers: list[Any], assets: dict[str, MediaAsset], manifest: dict[str, object]
) -> None:
    """Resolve + register every bundled font a caption layer's runs reference.

    Delegates to `phone_captions.caption_font_assets`, that module's own
    documented helper for exactly this purpose (see its docstring: callers
    assembling a full recipe call it right after `compile_caption_layers` and
    merge the result into their own `assets`/`asset_manifest` dicts).
    """
    from app.pipeline.phone_captions import caption_font_assets

    for font_asset_id, font_asset in caption_font_assets(layers).items():
        if font_asset_id in assets:
            continue
        manifest[font_asset_id] = font_asset
        assets[font_asset_id] = MediaAsset(
            id=font_asset_id,
            relative_path=font_asset_id,
            fingerprint=AssetFingerprint(
                hex=font_asset.fingerprint.sha256,
                byte_count=font_asset.fingerprint.byte_count,
            ),
        )


def _compile_caption_layers(
    caption_cues: list[dict],
    *,
    canvas_width: int,
    canvas_height: int,
    caption_style: str,
    timeline_duration_s: float,
) -> list[Any]:
    from app.pipeline.phone_captions import compile_caption_layers

    try:
        return compile_caption_layers(
            caption_cues,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            style=caption_style,
            id_prefix="caption",
            timeline_duration_s=timeline_duration_s,
        )
    except TypeError:
        # `timeline_duration_s` is documented as optional keyword-only --
        # tolerate an earlier signature that doesn't accept it yet.
        return compile_caption_layers(
            caption_cues,
            canvas_width=canvas_width,
            canvas_height=canvas_height,
            style=caption_style,
            id_prefix="caption",
        )


def compile_phone_narrated_plan(
    steps: list[NarratedPhoneStep],
    bindings: tuple[PhoneSourceBinding, ...],
    narration: PhoneNarrationBed,
    *,
    voiceover_duration_s: float,
    mix: float | None = None,
    caption_cues: list[dict] | None = None,
    caption_style: str = "sentence",
    orientation: str = "portrait",
) -> EditRecipeV2:
    """See the module docstring for the full contract.

    ``steps`` must already be in narration-timeline order and tile
    ``[0, voiceover_duration_s]`` contiguously (see "Step-timing contract").
    ``narration`` is the same `PhoneNarrationBed` receipt
    `compile_phone_montage_plan` takes -- built by
    `_resolve_phone_voiceover_bed`. ``mix`` follows
    `GenerativeVariantDecision.mix`'s convention (see "Audio mix
    approximation" in the module docstring).
    """
    if not steps:
        raise ValueError("phone narrated plan has no steps")
    if voiceover_duration_s <= 0:
        raise ValueError("phone narrated plan requires a positive voiceover duration")

    bindings_by_media_id = {binding.media_id: binding for binding in bindings}
    if len(bindings_by_media_id) != len(bindings):
        raise ValueError("phone bindings must have unique media identities")

    pipeline_canvas = canvas_for_orientation(orientation)
    story_canvas = Canvas(width=pipeline_canvas.width, height=pipeline_canvas.height)

    # Re-validate the contiguous-tiling contract the step timings are
    # supposed to already satisfy (see module docstring) rather than trust it
    # blindly -- a caller bug here would silently desync visuals from audio.
    expected_start = 0.0
    for step in steps:
        if step.end_s <= step.start_s:
            raise UnsupportedPhonePlan("narrated step has a non-positive duration")
        if abs(step.start_s - expected_start) > TIMING_ROUNDING_TOLERANCE_S:
            raise UnsupportedPhonePlan(
                "narrated steps must tile the narration timeline contiguously"
            )
        expected_start = step.end_s
    if abs(expected_start - voiceover_duration_s) > TIMING_ROUNDING_TOLERANCE_S:
        raise UnsupportedPhonePlan("narrated steps must cover the full voiceover duration")

    assets: dict[str, MediaAsset] = {}
    manifest: dict[str, object] = {}
    clips: list[TimelineClip] = []
    cursor = 0.0
    for index, step in enumerate(steps):
        binding = bindings_by_media_id.get(step.media_id)
        if binding is None:
            raise UnsupportedPhonePlan("narrated step has no phone source binding")
        target_duration_s = step.end_s - step.start_s
        source_start, source_duration, rate = _fit_step_window(
            binding, step.source_start_s, target_duration_s
        )

        original = binding.original
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
                id=f"step-{index}-{step.step_id}",
                source_asset_id=asset.id,
                source_start=source_start,
                source_duration=source_duration,
                timeline_start=cursor,
                rate=rate,
            )
        )
        cursor += target_duration_s

    if len({clip.id for clip in clips}) != len(clips):
        raise ValueError("phone narrated clips must have unique identities")
    total_duration_s = cursor

    layers: list[Any] = []
    if caption_cues:
        try:
            layers = list(
                _compile_caption_layers(
                    caption_cues,
                    canvas_width=story_canvas.width,
                    canvas_height=story_canvas.height,
                    caption_style=caption_style,
                    timeline_duration_s=total_duration_s,
                )
            )
        except UnsupportedPhonePlan:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted cue content
            raise UnsupportedPhonePlan(f"unable to compile captions: {exc}") from exc
        _register_caption_fonts(layers, assets, manifest)

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
    voice_mix = max(0.0, min(1.0, float(mix if mix is not None else 1.0)))
    footage_bed_gain = max(0.0, 1.0 - voice_mix)
    narration_duration_s = max(0.1, min(float(voiceover_duration_s), narration.duration_s))

    tracks = [
        TimelineTrack(id="narrated", kind="video", clips=clips),
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
        ),
    ]
    audio = AudioMixRecipe(
        narration_asset_id=narration_asset.id,
        original_volume=footage_bed_gain,
    )

    required_capabilities = (
        {"basicComposition", "local1080Export", "narrationAudio", "audioMix"}
        | ({"positionedText"} if layers else set())
        | (
            {"animatedText"}
            if any(layer.effect not in {"static", "none"} for layer in layers)
            else set()
        )
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
