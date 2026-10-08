"""Project a decided "narrated" walkthrough generative variant into a native
device render program (KRI-132).

Companion to `app.pipeline.phone_voiceover_montage_plan.compile_phone_voiceover_montage_plan` for
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
load-bearing: `compile_phone_voiceover_montage_plan` sets it from
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

`compile_phone_voiceover_montage_plan` never rejects a landscape clip outright --
since KRI-285 it letterboxes one when the creator's `landscape_fit` is "fit"
(`phone_recipe_shared.fit_transform`) and center-crops ("fill") otherwise;
golden-hour color grading is the only lane that additionally demands an
exact-canvas, unrotated source. Narrated footage is B-roll cut to a spoken
narration, not a face-forward shot, so the same face-tracking concern that
would justify a stricter policy doesn't apply here either.

This compiler INTENTIONALLY IGNORES `landscape_fit` (it takes no such
argument): no orientation check, center-crop fill for whatever the source
measures, whatever the item's fit preference says. Letterboxing narrated
footage is a deliberate gap tracked by KRI-307 (the device editor advertises
the fit control closed for narrated/speech-montage, with a reason).

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
referenced font as a manifest asset, exactly like `phone_voiceover_montage_plan.py` and
`phone_guided_plan.py` already do with the font asset `compile_text_overlay`
returns directly.

Opening title (KRI-455)
-----------------------

The cloud narrated render burns a confirmed ``opening_title`` as one
``generative_intro`` TextElement: top, large, fade-in, from 0 until a second
after the first spoken word (`generative_build._narrated_storyboard_text_elements`).
This compiler builds the same element and compiles it through the shared
`build_overlays_from_text_elements` + `compile_text_overlay` path the phone
guided compiler uses, so the device draws it where the cloud would. Title
layers carry the ``title-`` id prefix; `replace_narrated_captions` keeps them
(and their fonts) when it swaps the caption layers.

The element spells out what "top" / "large" resolve to on the cloud (y 0.15,
120 px, Playfair Display) and compiles to exactly the same layers. That is for
the iOS editor preview, which draws text from the variant's ``text_elements``
rather than the pinned recipe and has no cloud defaults of its own:
`narrated_title_text_elements` is what the worker persists, and the status
route shows it to app builds that know the element (read-only, or editable
since KRI-465: `phone_narrated_title_edits_supported`).

Editing the title (KRI-465): a text Save sends the title and any text the
creator added as ordinary `text_elements`; `replace_narrated_title` recompiles
exactly the ``title-`` layers from them (`replace_narrated_captions`' sibling),
and the Save persists them back as `narrated_title_text_elements` -- the one
store the status route reads, so the title can never show twice.

Both the cloud and the phone element take that look from
`narrated_title.narrated_title_placement`, which also fits a long title into
the top band of the frame (smaller font, top below the safe margin) instead of
letting its centred block run off the top.

Audio mix approximation (documented divergence, accepted for v1)
------------------------------------------------------------------

Cloud (`_mix_user_voiceover`, `app/tasks/template_orchestrate.py:6501-6601`):
voice at gain 1.0, footage bed side-chain DUCKED under the voice (dips while
narration plays, rises in pauses), loudnorm, and a 0.5s voice fade-out.
Since KRI-139 the phone expresses all three: the voice clip carries a 0.5s
``audio_fade_out``, ``AudioMixRecipe.target_lufs`` asks the device to
normalize the exported mix, and -- only when the caller passes
``duck_footage_bed`` (the worker's `audioDucking` verified-feature gate) --
``duck_original_during_music`` asks the device to side-chain duck the footage
under the voice with the cloud's compressor settings, at the cloud's
``_NARRATED_FOOTAGE_BED_MAX_GAIN`` resting level. Without the gate the bed
stays the shipped v1 approximation: one constant attenuated footage-bed gain
under a full-volume voice. ``mix`` here uses the SAME convention
`GenerativeVariantDecision.mix` does in the montage compiler (1.0 = voice
fully dominant/default, 0.0 = footage bed at its loudest) --
``footage_bed_gain = max(0.0, 1.0 - mix)``. The cloud narrated path's actual
knob is `voiceover_bed_level` (0 = voice-only, ~0.25 default, 1 = loudest --
the DIRECT bed gain, not a voice-prominence slider) -- converting it to this
compiler's ``mix`` is the wiring step's job (``mix = 1.0 - bed_level``), not
resolved in this module.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
from app.pipeline.phone_recipe_shared import (
    NARRATED_FOOTAGE_BED_MAX_GAIN,
    TIMING_ROUNDING_TOLERANCE_S,
    PhoneNarrationBed,
    audio_fade,
    snap_text_overshoot,
    timeline_end_s,
)
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.agents._schemas.text_element import TextElement
    from app.pipeline.phone_captions import PhoneCaptionLook
    from app.pipeline.phone_subtitled_lanes import PhoneSubtitledLanes
    from app.schemas.edit_proposal import PinnedText

# Only text layers (captions and the opening title) ask for these in a
# narrated recipe. `authoredText` is added by the `EditRecipeV2` validator for
# variable-font runs, so the swap clears it with the rest and the validator
# puts it back when it applies.
_CAPTION_CAPABILITIES = frozenset({"positionedText", "animatedText", "authoredText"})

# Opening-title layers (KRI-455). Every other text layer in a narrated recipe
# is a caption, so recipes pinned before titles existed swap exactly as before.
TITLE_LAYER_PREFIX = "title-"
# Mirrors the cloud narrated intro window (`_narrated_storyboard_text_elements`).
_TITLE_MIN_S = 0.5
_TITLE_MAX_S = 3.0
_TITLE_AFTER_FIRST_WORD_S = 1.0
_TITLE_MAX_CHARS = 80
NARRATED_TITLE_ELEMENT_ID = "narrated-title"

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
    `phone_voiceover_montage_plan`'s ``clip_id_to_media_id`` indirection (the assignment
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


def _compile_caption_layers(
    caption_cues: list[dict],
    *,
    canvas: Canvas,
    caption_style: str,
    timeline_duration_s: float,
    look: PhoneCaptionLook | None,
) -> list[Any]:
    from app.pipeline.phone_captions import compile_caption_layers

    try:
        return list(
            compile_caption_layers(
                caption_cues,
                canvas_width=canvas.width,
                canvas_height=canvas.height,
                style=caption_style,
                id_prefix="caption",
                timeline_duration_s=timeline_duration_s,
                look=look,
            )
        )
    except UnsupportedPhonePlan:
        raise
    except Exception as exc:  # noqa: BLE001 - untrusted cue content
        raise UnsupportedPhonePlan(f"unable to compile captions: {exc}") from exc


def narrated_title_end_s(first_word_end_s: float | None) -> float:
    """When the opening title fades out: a second after the first spoken word,
    held between 0.5 s and 3 s, like the cloud narrated intro. With no spoken
    word to anchor it, the title holds the full 3 s."""
    if first_word_end_s is None:
        return _TITLE_MAX_S
    return max(_TITLE_MIN_S, min(_TITLE_MAX_S, float(first_word_end_s) + _TITLE_AFTER_FIRST_WORD_S))


def narrated_title_element(
    opening_title: str | None, *, end_s: float | None, timeline_duration_s: float, canvas: Canvas
) -> TextElement | None:
    """The opening title as one TextElement, or ``None`` when there is none.

    The cloud narrated intro element with its look spelled out (see "Opening
    title"), fitted to ``canvas`` like the cloud's: the recipe compiles it, and
    the variant row carries it for the editor preview, so the two can't drift.
    ``end_s`` ``None`` holds the title the full 3 s (`narrated_title_end_s`).
    """
    from app.agents._schemas.text_element import TextElement
    from app.pipeline.narrated_title import narrated_title_placement

    text = " ".join((opening_title or "").split())[:_TITLE_MAX_CHARS]
    end_s = min(
        float(end_s if end_s is not None else narrated_title_end_s(None)), timeline_duration_s
    )
    if not text or end_s <= 0:
        return None
    return TextElement(
        id=NARRATED_TITLE_ELEMENT_ID,
        text=text,
        start_s=0.0,
        end_s=end_s,
        role="generative_intro",
        **narrated_title_placement(text, canvas=canvas, explicit=True),
        effect="fade-in",
        # No `read_only` marker (KRI-465): the status route decides per request
        # whether the title is editable (`_with_phone_narrated_title`), so rows
        # persisted before and after this change read the same way.
        source_params={"narrated_storyboard": "intro"},
    )


def narrated_title_text_elements(
    recipe: EditRecipeV2, opening_title: str | None, *, end_s: float | None
) -> list[dict]:
    """The title element behind ``recipe``'s title layers, as variant-row JSON.

    The editor preview compiles text from the variant, never from the pinned
    recipe, so the phone worker persists this beside the recipe it compiled
    with the same ``opening_title`` / ``end_s``. Empty when the recipe has no
    title layer.
    """
    video = next((track for track in recipe.tracks if track.id == "narrated"), None)
    if video is None or not any(_is_title_layer(layer) for layer in recipe.text_layers):
        return []
    element = narrated_title_element(
        opening_title,
        end_s=end_s,
        timeline_duration_s=timeline_end_s(video.clips),
        canvas=recipe.canvas,
    )
    return [element.model_dump(mode="json", exclude_none=True)] if element is not None else []


def _compile_title_layers(
    elements: list[TextElement],
    *,
    canvas: Canvas,
    timeline_duration_s: float,
    first_index: int = 0,
) -> list[Any]:
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.portable_text_layout import compile_text_overlay

    try:
        overlays = build_overlays_from_text_elements(
            elements, video_duration_s=timeline_duration_s, independent_box_alignment=True
        )
        return [
            compile_text_overlay(
                overlay,
                layer_id=f"{TITLE_LAYER_PREFIX}{first_index + index}",
                canvas=canvas,
                dissolve_seed=101 + (first_index + index) * 37,
            )[0]
            for index, overlay in enumerate(overlays)
        ]
    except Exception as exc:  # noqa: BLE001 - untrusted title text
        raise UnsupportedPhonePlan(f"unable to compile the title: {exc}") from exc


def with_pinned_text_layers(
    recipe: EditRecipeV2,
    pins: Sequence[PinnedText] | None,
    *,
    font_family: str | None = None,
    text_color: str | None = None,
) -> tuple[EditRecipeV2, list[dict]]:
    """``recipe`` plus the creator's pinned corner text (KRI-523/525/527), and its rows.

    The layers use the ``title-`` id prefix so every title/caption Save keeps treating them as
    authored text; their indices continue after any layer the recipe already has. A clip scope
    resolves against the recipe's video clips in timeline order. The returned rows
    (``guided-pinned-<i>`` TextElements) are what the variant row carries, because the editor
    preview compiles text from the variant, never from the pinned recipe. A pin with no usable
    window is dropped, so the caller must read what was drawn from the rows.
    """

    from app.agents._schemas.text_element import TextElement
    from app.pipeline.pinned_text import pinned_text_elements, resolve_pin_windows

    if not pins:
        return recipe, []
    clips = [clip for track in recipe.tracks for clip in track.clips]
    duration = timeline_end_s(clips)
    clip_windows = sorted(
        (
            clip.timeline_start,
            clip.timeline_start + clip.source_duration / clip.rate + (clip.hold_duration or 0),
        )
        for track in recipe.tracks
        if track.kind == "video"
        for clip in track.clips
    )
    rows = pinned_text_elements(
        resolve_pin_windows(pins, duration, clip_windows),
        font_family=font_family,
        text_color=text_color,
    )
    if not rows:
        return recipe, []
    existing = list(recipe.text_layers)
    layers = _compile_title_layers(
        [TextElement.model_validate(row) for row in rows],
        canvas=recipe.canvas,
        timeline_duration_s=duration,
        first_index=sum(1 for layer in existing if _is_title_layer(layer)),
    )
    return _with_text_layers(recipe, [*existing, *layers]), rows


def narrated_authored_text_elements(rows: list[dict] | None) -> list[dict]:
    """The creator-visible text a Voiceover editor Save carries, minus what the
    recipe owns elsewhere (KRI-465).

    The editor document sends every text element it shows: the opening title,
    any text the creator added, and one mirror per caption cue. The mirrors
    (``source_params.source == caption_cue``) are the caption lane's, lyric
    lines are not a narrated concept, and removed rows are tombstones; none of
    them compile as title layers.
    """
    from app.agents._schemas.text_element import CAPTION_CUE_SOURCE

    return [
        row
        for row in rows or []
        if isinstance(row, dict)
        and (row.get("source_params") or {}).get("source") != CAPTION_CUE_SOURCE
        and row.get("role") != "lyric_line"
        and not row.get("removed")
    ]


def replace_narrated_title(recipe: EditRecipeV2, elements: list[dict]) -> EditRecipeV2:
    """``recipe`` with only its title layers recompiled from ``elements`` (KRI-465).

    The title counterpart of `replace_narrated_captions`: a text Save keeps
    every clip, the narration bed, the audio mix and the caption layers exactly
    as pinned, and swaps just the ``title-`` layers (and the fonts and text
    capabilities they need). ``elements`` are authored text rows (see
    `narrated_authored_text_elements`); each is clamped to the video's end, and
    one that no longer has a visible window is dropped. An empty list removes
    the title. Titles stay first, like `compile_phone_narrated_plan`.
    """
    from app.agents._schemas.text_element import TextElement

    video = next((track for track in recipe.tracks if track.id == "narrated"), None)
    if video is None or not video.clips:
        raise UnsupportedPhonePlan("pinned narrated recipe has no video track")
    duration = timeline_end_s(video.clips)

    kept: list[TextElement] = []
    for row in elements:
        try:
            element = TextElement.model_validate(row)
        except Exception as exc:  # noqa: BLE001 - untrusted editor rows
            raise UnsupportedPhonePlan(f"unable to read the title: {exc}") from exc
        if element.removed:
            continue
        end_s = min(float(element.end_s), duration)
        if end_s <= float(element.start_s):
            continue
        kept.append(element.model_copy(update={"end_s": end_s}))

    titles = (
        _compile_title_layers(kept, canvas=recipe.canvas, timeline_duration_s=duration)
        if kept
        else []
    )
    captions = [layer.model_copy() for layer in recipe.text_layers if not _is_title_layer(layer)]
    return _with_text_layers(recipe, [*titles, *captions])


def _is_title_layer(layer: object) -> bool:
    return str(getattr(layer, "id", "")).startswith(TITLE_LAYER_PREFIX)


def _is_text_font(asset: object) -> bool:
    # Text layers (captions and the title) are the only users of bundled fonts
    # in a narrated recipe.
    return getattr(asset, "kind", None) == "library" and str(getattr(asset, "id", "")).startswith(
        "font-"
    )


def _with_text_layers(recipe: EditRecipeV2, layers: list[Any]) -> EditRecipeV2:
    """``recipe`` carrying exactly ``layers`` as its text, with the bundled
    fonts and text capabilities they need (and none they don't)."""
    from app.pipeline.phone_captions import caption_font_assets

    fonts = caption_font_assets(layers)
    dropped = {asset.id for asset in recipe.asset_manifest.assets if _is_text_font(asset)}
    voiceover_ids = {
        asset.id
        for asset in recipe.asset_manifest.assets
        if isinstance(asset, VoiceoverRenderAsset)
    }

    def _with_fonts(entries: list[Any], font_entries: list[Any]) -> list[Any]:
        # Fonts sit right before the narration bed, the order the compiler
        # has always registered them in.
        kept = [entry for entry in entries if entry.id not in dropped]
        at = next((i for i, entry in enumerate(kept) if entry.id in voiceover_ids), len(kept))
        return kept[:at] + font_entries + kept[at:]

    manifest = _with_fonts(list(recipe.asset_manifest.assets), list(fonts.values()))
    assets = _with_fonts(
        list(recipe.assets),
        [
            MediaAsset(
                id=font_asset_id,
                relative_path=font_asset_id,
                fingerprint=AssetFingerprint(
                    hex=font_asset.fingerprint.sha256,
                    byte_count=font_asset.fingerprint.byte_count,
                ),
            )
            for font_asset_id, font_asset in fonts.items()
        ],
    )
    # A caption that ends at the voiceover's nominal end can overshoot the recipe's own
    # duration by float noise (a slowed-down clip's `usable / (usable / target)` lands
    # 1 ULP short, and the narration bed may be a millisecond shorter): `EditRecipeV2`
    # rejects `layer.end > duration` strictly (KRI-209, same class as KRI-190).
    snap_text_overshoot(
        layers, timeline_end_s(clip for track in recipe.tracks for clip in track.clips)
    )
    required_capabilities = (
        (set(recipe.required_capabilities) - _CAPTION_CAPABILITIES)
        | ({"positionedText"} if layers else set())
        | (
            {"animatedText"}
            if any(layer.effect not in {"static", "none"} for layer in layers)
            else set()
        )
    )
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields.update(
        assets=assets,
        asset_manifest=RenderAssetManifest(assets=tuple(manifest)),
        text_layers=layers,
        required_capabilities=required_capabilities,
    )
    return EditRecipeV2(**fields)


def replace_narrated_captions(
    recipe: EditRecipeV2,
    *,
    caption_cues: list[dict] | None,
    caption_style: str = "sentence",
    look: PhoneCaptionLook | None = None,
) -> EditRecipeV2:
    """``recipe`` with only its caption layers recompiled (KRI-280).

    A narrated device variant has no guided plan and no cloud base: its only
    program is the recipe pinned for the phone. A caption Save (text, timing,
    style or look) therefore keeps every clip, the narration bed and the
    audio mix exactly as pinned and swaps just the caption layers, their
    bundled fonts and the text capabilities. Nothing is re-derived from the
    voiceover, so a Save can never move a cut or change the mix. The opening
    title (KRI-455) is not a caption: it stays exactly as pinned.

    `compile_phone_narrated_plan` builds its own captions through this
    function, so recompiling a recipe with the cues and style it was compiled
    with returns an equal recipe.
    """
    video = next((track for track in recipe.tracks if track.id == "narrated"), None)
    if video is None or not video.clips:
        raise UnsupportedPhonePlan("pinned narrated recipe has no video track")

    layers: list[Any] = []
    if caption_cues:
        layers = _compile_caption_layers(
            caption_cues,
            canvas=recipe.canvas,
            caption_style=caption_style,
            timeline_duration_s=timeline_end_s(video.clips),
            look=look,
        )
    titles = [layer.model_copy() for layer in recipe.text_layers if _is_title_layer(layer)]
    return _with_text_layers(recipe, [*titles, *layers])


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
    target_lufs: float | None = None,
    duck_footage_bed: bool = False,
    caption_look: PhoneCaptionLook | None = None,
    lanes: PhoneSubtitledLanes | None = None,
    visuals: tuple[PhoneVisualBinding, ...] = (),
    opening_title: str | None = None,
    opening_title_end_s: float | None = None,
) -> EditRecipeV2:
    """See the module docstring for the full contract.

    ``target_lufs`` / ``duck_footage_bed`` (KRI-139): see "Audio mix
    approximation" -- both are passed in so this module stays settings-free.

    ``caption_look`` (KRI-280): the caption appearance a phone-editor Save
    persisted (`phone_captions.caption_look_from_variant`); ``None`` is the
    default look every first render uses.

    ``opening_title`` / ``opening_title_end_s`` (KRI-455): the creator's
    confirmed title and when it fades out (`narrated_title_end_s`; ``None``
    holds it the full 3 s). See "Opening title" in the module docstring.

    ``steps`` must already be in narration-timeline order and tile
    ``[0, voiceover_duration_s]`` contiguously (see "Step-timing contract").
    ``narration`` is the same `PhoneNarrationBed` receipt
    `compile_phone_voiceover_montage_plan` takes -- built by
    `_resolve_phone_voiceover_bed`. ``mix`` follows
    `GenerativeVariantDecision.mix`'s convention (see "Audio mix
    approximation" in the module docstring).

    ``lanes`` / ``visuals`` (KRI-281): the editor's sound-effect and Visuals
    lanes, compiled by `replace_editor_lanes` -- the same swap a phone-editor
    Save uses, so a first compile and every later Save agree.
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
    if duck_footage_bed:
        footage_bed_gain *= NARRATED_FOOTAGE_BED_MAX_GAIN
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
                    audio_fade_out=audio_fade(narration_duration_s),
                )
            ],
        ),
    ]
    audio = AudioMixRecipe(
        narration_asset_id=narration_asset.id,
        original_volume=footage_bed_gain,
        # Nothing to duck when the bed is silent.
        duck_original_during_music=duck_footage_bed and footage_bed_gain > 0,
        target_lufs=target_lufs,
    )
    required_capabilities = (
        {"basicComposition", "local1080Export", "narrationAudio", "audioMix"}
        | ({"variableSpeed"} if any(clip.rate != 1 for clip in clips) else set())
        | ({"audioDucking"} if audio.duck_original_during_music else set())
    )

    recipe = EditRecipeV2(
        canvas=story_canvas,
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=tracks,
        audio=audio,
        required_capabilities=required_capabilities,
    )
    title = narrated_title_element(
        opening_title,
        end_s=opening_title_end_s,
        timeline_duration_s=timeline_end_s(clips),
        canvas=story_canvas,
    )
    if title is not None:
        recipe = _with_text_layers(
            recipe,
            _compile_title_layers(
                [title], canvas=story_canvas, timeline_duration_s=timeline_end_s(clips)
            ),
        )
    if caption_cues:
        # Captions go through the same swap a phone-editor caption Save uses
        # (KRI-280), so the first render and every later Save compile them alike.
        # An empty-caption narrated render is not an error (see "Captions").
        recipe = replace_narrated_captions(
            recipe, caption_cues=caption_cues, caption_style=caption_style, look=caption_look
        )
    if lanes is not None and (lanes.overlays or lanes.sound_effects):
        recipe = replace_editor_lanes(recipe, lanes=lanes, visuals=visuals)
    return recipe


# Tracks and capabilities owned by the editor's sound-effect / Visuals lanes
# (`phone_subtitled_plan`'s lane compilers write exactly these).
_EDITOR_LANE_TRACK_IDS = frozenset({"subtitled-overlays", "sfx"})
_EDITOR_LANE_CAPABILITIES = frozenset(
    {"visualBlocks", "alphaOverlay", "visualVideos", "soundEffects"}
)
# The track an editor-added photo/video compiles to
# (`phone_editor_visuals.compile_editor_media_track`) and what it needs: the
# guided compiler declares the same set for it (KRI-287).
EDITOR_MEDIA_TRACK_ID = "editor-media"
_EDITOR_MEDIA_CAPABILITIES = frozenset({"visualBlocks", "alphaOverlay", "audioMix"})


def _media_last(tracks: list[TimelineTrack]) -> list[TimelineTrack]:
    """Keep editor media on top of every other layer, whichever lane a Save
    recompiled last, so layering never depends on the order of Saves."""
    return [t for t in tracks if t.id != EDITOR_MEDIA_TRACK_ID] + [
        t for t in tracks if t.id == EDITOR_MEDIA_TRACK_ID
    ]


def _has_editor_media(tracks: list[TimelineTrack]) -> bool:
    return any(track.id == EDITOR_MEDIA_TRACK_ID and track.clips for track in tracks)


def replace_editor_media(
    recipe: EditRecipeV2,
    *,
    blocks: list[dict[str, Any]],
    visuals: tuple[PhoneVisualBinding, ...],
    video_track_id: str = "narrated",
) -> EditRecipeV2:
    """``recipe`` with only its editor media Visual blocks recompiled (KRI-287),
    the media counterpart of `replace_editor_lanes`.

    ``blocks`` is the variant's complete saved ``visual_blocks`` list (media
    kind only); an empty list removes the layer. Every clip, caption, lane,
    bed and the audio mix stay exactly as pinned. Blocks compile through the
    guided editor's own `compile_editor_media_track`, so an added photo or
    video renders the same on a Voiceover edit as on a guided story, and must
    sit inside the main video track (``video_track_id``).

    Raises `UnsupportedPhonePlan` (capability ``visualBlocks``) for a block the
    phone can't render.
    """
    from app.pipeline.phone_editor_visuals import (  # noqa: PLC0415
        UnsupportedEditorMedia,
        compile_editor_media_track,
    )

    video = next((track for track in recipe.tracks if track.id == video_track_id), None)
    if video is None or not video.clips:
        raise UnsupportedPhonePlan("pinned voiceover recipe has no video track")

    kept_tracks = [track for track in recipe.tracks if track.id != EDITOR_MEDIA_TRACK_ID]
    still_used = {clip.source_asset_id for track in kept_tracks for clip in track.clips}
    media_only = {
        clip.source_asset_id
        for track in recipe.tracks
        if track.id == EDITOR_MEDIA_TRACK_ID
        for clip in track.clips
    } - still_used
    assets = {asset.id: asset for asset in recipe.assets if asset.id not in media_only}
    manifest = {
        asset.id: asset for asset in recipe.asset_manifest.assets if asset.id not in media_only
    }

    tracks = list(kept_tracks)
    required = set(recipe.required_capabilities)
    # Drop what only the old media layer needed (mirrors the device's own
    # content-derived requirements); kept overlay lanes still need theirs.
    if not any(clip.visual_placement for track in tracks for clip in track.clips):
        required.discard("visualBlocks")
    if not any(track.kind == "overlay" and track.clips for track in tracks):
        required.discard("alphaOverlay")
    if blocks:
        try:
            media_track = compile_editor_media_track(
                blocks,
                visuals=visuals,
                timeline_duration_s=timeline_end_s(video.clips),
                assets=assets,
                manifest=manifest,
            )
        except UnsupportedEditorMedia as exc:
            raise UnsupportedPhonePlan(str(exc), capability="visualBlocks") from exc
        if media_track.clips:
            tracks.append(media_track)
            required |= _EDITOR_MEDIA_CAPABILITIES

    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields.update(
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=_media_last(tracks),
        required_capabilities=required,
    )
    return EditRecipeV2(**fields)


def replace_editor_lanes(
    recipe: EditRecipeV2,
    *,
    lanes: PhoneSubtitledLanes,
    visuals: tuple[PhoneVisualBinding, ...],
    video_track_id: str = "narrated",
) -> EditRecipeV2:
    """``recipe`` with only its sound-effect and Visuals (overlay) lanes recompiled
    (KRI-281), the Voiceover counterpart of `replace_narrated_captions`.

    Phone Voiceover has no guided plan to recompile from, so a lane Save keeps
    every clip, caption layer, the narration/music beats and the audio mix
    exactly as pinned and swaps just the ``subtitled-overlays`` / ``sfx``
    tracks (and the assets only they used). The lane compilers are
    `phone_subtitled_plan`'s own, so a card or effect compiles the same on a
    Voiceover edit as on a Talking one; windows clamp to the main video
    track's end (``video_track_id``: ``"narrated"`` or the montage's
    ``"montage"``).

    Raises `UnsupportedPhonePlan` (a lane error names the failing lane) for an
    ending clip, which Voiceover edits do not carry.
    """
    from app.pipeline.phone_subtitled_lanes import _lane_error  # noqa: PLC0415
    from app.pipeline.phone_subtitled_plan import (  # noqa: PLC0415
        _compile_overlay_track,
        _compile_sfx_track,
    )

    video = next((track for track in recipe.tracks if track.id == video_track_id), None)
    if video is None or not video.clips:
        raise UnsupportedPhonePlan("pinned voiceover recipe has no video track")
    if lanes.ending_clip is not None:
        raise UnsupportedPhonePlan("an ending clip isn't supported on phone Voiceover edits")

    kept_tracks = [track for track in recipe.tracks if track.id not in _EDITOR_LANE_TRACK_IDS]
    still_used = {clip.source_asset_id for track in kept_tracks for clip in track.clips}
    lane_only = {
        clip.source_asset_id
        for track in recipe.tracks
        if track.id in _EDITOR_LANE_TRACK_IDS
        for clip in track.clips
    } - still_used
    assets = {asset.id: asset for asset in recipe.assets if asset.id not in lane_only}
    manifest = {
        asset.id: asset for asset in recipe.asset_manifest.assets if asset.id not in lane_only
    }

    timeline_end = timeline_end_s(video.clips)
    required = set(recipe.required_capabilities) - _EDITOR_LANE_CAPABILITIES
    tracks = list(kept_tracks)
    if lanes.overlays:
        try:
            overlay_track, has_video = _compile_overlay_track(
                lanes.overlays,
                visuals=visuals,
                speaker_end=timeline_end,
                assets=assets,
                manifest=manifest,
            )
        except UnsupportedPhonePlan:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted lane content
            raise _lane_error("overlays", str(exc), capability="visualBlocks") from exc
        if overlay_track.clips:
            tracks.append(overlay_track)
            required |= {"visualBlocks", "alphaOverlay", "audioMix"}
            if has_video:
                required |= {"visualVideos"}
    if lanes.sound_effects:
        try:
            sfx_track = _compile_sfx_track(
                lanes.sound_effects,
                timeline_end=timeline_end,
                assets=assets,
                manifest=manifest,
            )
        except UnsupportedPhonePlan:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted lane content
            raise _lane_error("sound_effects", str(exc), capability="soundEffects") from exc
        if sfx_track.clips:
            tracks.append(sfx_track)
            required |= {"soundEffects", "audioMix"}
    if _has_editor_media(tracks):
        # The kept editor media layer (KRI-287) still needs what the lane
        # strip above removed.
        required |= _EDITOR_MEDIA_CAPABILITIES

    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields.update(
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=_media_last(tracks),
        required_capabilities=required,
    )
    return EditRecipeV2(**fields)
