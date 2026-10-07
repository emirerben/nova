"""Project a spoken-excerpt montage into a native device render program (KRI-282).

Companion to `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan`
(the talking-to-camera speaker spine plus muted cutaways) and
`app.pipeline.phone_voiceover_montage_plan` (a montage with an audio track).
Same contract as every phone compiler: no media is downloaded or rendered
here, and anything the device cannot express fails closed with
`UnsupportedPhonePlan` and a plain-English reason.

The input is an ORDERED list of sections whose speech excerpts a worker has
already grounded to word timings (``app.services.speech_segments``):

* ``speech`` / ``visual="speaker"`` -- the speaker's own clip plays on the main
  track for the excerpt window, so its picture AND its audio play together.
* ``speech`` / ``visual="cutaways"`` -- the excerpt's audio plays from an audio
  track (the speaker's clip, source-offset to the excerpt) while muted b-roll
  cuts cover the picture on the main track. The speech is the spine; the b-roll
  windows are laid out to match it exactly.
* ``montage`` -- muted fast cuts over the other footage on the main track.

Every picture clip lives on ONE main video track, back to back, so the timeline
is exactly the sum of the sections. Source audio of every b-roll/montage cut is
muted (``volume=0``); only speech excerpts (and an optional music bed that
plays during montage sections only, so it never competes with the voice) are
heard. Excerpts start and end with short gain ramps (``audio_fade_in/out``) on
the excerpt's own clip, so a cut on a sentence boundary never clicks and two
excerpts of the same clip never smear into each other. The window itself was
already padded to silence by ``ground_excerpt``.

No new recipe field or version: the recipe is plain schema-v2 (main-track
clips, one ``audio`` track, per-clip ``volume`` and fades). Capabilities are
``basicComposition``, ``local1080Export`` and, when an audio track is emitted,
``audioMix``.

A landscape or square speaker clip is accepted and framed by the engine's
plain centre cover-fit (the same crop every other phone montage clip gets). We
deliberately emit NO ``source_crop``: with no face tracking the only window we
could name is the centred one, which is pixel-identical to the cover-fit, and a
crop would add the ``sourceCrop`` capability, which ``validate_phone_pilot_recipe``
refuses unless it is in ``PHONE_RENDER_VERIFIED_FEATURES``. The recipe is the same
shape for every orientation; the receipt records an adjustment so the creator can
be told the sides are cropped.

Rejected (all `UnsupportedPhonePlan`, with a message a creator can act on):
a speaker clip that is not a video or is too long, b-roll that is needed but
absent, an excerpt window that is not playable, and a timeline over the track's
clip budget.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from app.kria.render_assets import LibraryRenderAsset, RenderAssetManifest
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import (
    EXPORT_SAFETY_MARGIN_S,
    PhoneMusicBed,
    audio_fade,
    refit_source_window,
)
from app.services.phone_sources import PhoneSourceBinding

_STORY_CANVAS = Canvas(width=1080, height=1920)

MAIN_TRACK_ID = "speech-montage"
SPEECH_AUDIO_TRACK_ID = "speech-audio"
MUSIC_TRACK_ID = "music"

# Mirrors `phone_subtitled_plan._MAX_CLIP_DURATION_S`: the speaker clip cap.
_MAX_SPEAKER_CLIP_S = 300.0
# TimelineTrack allows 100 clips; stay clear of it so a later editor edit can
# still add a cut.
_MAX_MAIN_CLIPS = 92
_DEFAULT_CUT_S = 0.8
# Speech over b-roll: how long each cutaway holds. Long enough to read as a
# scene, short enough to keep the "montage" energy while someone talks.
CUTAWAY_HOLD_S = 2.4
_MIN_HOLD_S = 0.5
_MIN_EXCERPT_S = 0.8
# Gain ramps on an excerpt's own audio.
EXCERPT_FADE_IN_S = 0.05
EXCERPT_FADE_OUT_S = 0.18
# Music under montage sections only.
_MUSIC_GAIN_CAP = 0.5
_MUSIC_FADE_IN_S = 0.2
_MUSIC_FADE_OUT_S = 0.35
_MIN_MUSIC_RUN_S = 1.0


class PhoneSpeechSection(BaseModel):
    """One already-grounded section."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    kind: Literal["speech", "montage"]
    # speech
    speaker: PhoneSourceBinding | None = None
    source_start_s: float = Field(default=0.0, ge=0)
    source_end_s: float = Field(default=0.0, ge=0)
    visual: Literal["speaker", "cutaways"] = "speaker"
    quote: str = Field(default="", max_length=400)
    # montage
    duration_s: float = Field(default=0.0, ge=0, le=60)
    cut_s: float | None = Field(default=None, ge=0.3, le=3.0)

    @model_validator(mode="after")
    def _shape(self) -> PhoneSpeechSection:
        if self.kind == "speech":
            if self.speaker is None:
                raise ValueError("a speech section needs its speaker clip")
            if self.source_end_s - self.source_start_s < _MIN_EXCERPT_S:
                raise ValueError("a speech excerpt must be at least 0.8s")
        elif self.duration_s <= 0:
            raise ValueError("a montage section needs a duration")
        return self

    @property
    def length_s(self) -> float:
        if self.kind == "speech":
            return self.source_end_s - self.source_start_s
        return self.duration_s


@dataclass
class SpeechMontageReceipt:
    """What the compiler actually put on the timeline (never what a model claimed)."""

    sections: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0
    cut_count: int = 0
    music: bool = False
    adjustments: list[str] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        return {
            "sections": self.sections,
            "duration_s": round(self.duration_s, 3),
            "cut_count": self.cut_count,
            "music": self.music,
            "adjustments": self.adjustments,
        }


def _display_dims(original: Any) -> tuple[int, int]:
    width, height = int(original.width), int(original.height)
    if int(original.orientation_degrees) % 180 == 90:
        return height, width
    return width, height


class _Assets:
    def __init__(self) -> None:
        self.assets: dict[str, MediaAsset] = {}
        self.manifest: dict[str, object] = {}

    def add_video(self, binding: PhoneSourceBinding) -> str:
        asset = binding.render_asset()
        if asset.id not in self.assets:
            original = binding.original
            self.manifest[asset.id] = asset
            self.assets[asset.id] = MediaAsset(
                id=asset.id,
                relative_path=asset.id,
                fingerprint=AssetFingerprint(hex=original.sha256, byte_count=original.byte_count),
                duration=original.duration_s,
                natural_size=MediaSize(width=original.width, height=original.height),
                orientation_degrees=original.orientation_degrees,
                is_proxy_available=True,
            )
        return asset.id


class _BrollPool:
    """Round-robin b-roll with a per-clip source cursor, so a clip met again later
    shows a fresh part of it instead of repeating the same frames."""

    def __init__(self, bindings: list[PhoneSourceBinding]) -> None:
        self._bindings = bindings
        self._cursor: dict[str, float] = {b.media_id: 0.0 for b in bindings}
        self._next = 0

    def __bool__(self) -> bool:
        return bool(self._bindings)

    def take(self, duration_s: float) -> tuple[PhoneSourceBinding, float, float]:
        """The next cut: (binding, source_start_s, duration_s)."""
        for _ in range(len(self._bindings)):
            binding = self._bindings[self._next % len(self._bindings)]
            self._next += 1
            usable = float(binding.original.duration_s) - EXPORT_SAFETY_MARGIN_S
            want = min(duration_s, usable)
            if want < _MIN_HOLD_S and usable < duration_s:
                continue  # a clip too short to hold this cut; try the next one
            start = self._cursor[binding.media_id]
            if start + want > usable:
                start = 0.0  # wrapped: reuse the clip from its start
            self._cursor[binding.media_id] = start + want
            start, want = refit_source_window(start, want, float(binding.original.duration_s))
            return binding, start, want
        raise UnsupportedPhonePlan(
            "the other footage is too short to cut between; add a few longer clips for the "
            "fast-cut parts"
        )


def _cut_scale(sections: tuple[PhoneSpeechSection, ...]) -> float:
    """A global factor >= 1 that lengthens every fast cut until the main track fits.

    Applies to explicit per-section cut lengths too: a request for 0.4s cuts over
    80 seconds would need 200 clips, and the track holds 100.
    """
    cuts = sum(s.duration_s / (s.cut_s or _DEFAULT_CUT_S) for s in sections if s.kind == "montage")
    cutaway_s = sum(s.length_s for s in sections if s.kind == "speech" and s.visual == "cutaways")
    speaker_n = sum(1 for s in sections if s.kind == "speech" and s.visual == "speaker")
    fixed = speaker_n + math.ceil(cutaway_s / CUTAWAY_HOLD_S) + len(sections)
    room = max(1, _MAX_MAIN_CLIPS - fixed)
    return max(1.0, cuts / room)


def compile_phone_speech_montage_plan(
    sections: tuple[PhoneSpeechSection, ...],
    broll: tuple[PhoneSourceBinding, ...],
    *,
    music: PhoneMusicBed | None = None,
    target_lufs: float | None = None,
) -> tuple[EditRecipeV2, SpeechMontageReceipt]:
    """Compile ordered, grounded sections into a schema-v2 recipe plus its receipt.

    ``broll`` is every clip that may appear as a cutaway or montage cut. A
    speaker clip is never used as b-roll for its own speech section, and is
    left out of montage sections too (a cut of the person mid-sentence with the
    sound off reads as a glitch).
    """
    if not sections:
        raise UnsupportedPhonePlan("the spoken-excerpt montage has no sections")
    if not any(s.kind == "speech" for s in sections):
        raise UnsupportedPhonePlan("the spoken-excerpt montage has no speech excerpt")

    speaker_ids = {s.speaker.media_id for s in sections if s.speaker is not None}
    centre_cropped: set[str] = set()
    for section in sections:
        if section.kind != "speech" or section.speaker is None:
            continue
        original = section.speaker.original
        if original.width is None or original.height is None:
            raise UnsupportedPhonePlan("the speaking clip must be a video")
        if original.duration_s > _MAX_SPEAKER_CLIP_S:
            raise UnsupportedPhonePlan(
                "the speaking clip is longer than 5 minutes; trim it and try again"
            )
        display_w, display_h = _display_dims(original)
        if display_w <= 0 or display_h <= 0:
            raise UnsupportedPhonePlan("the speaking clip has no readable video picture")
        if display_w >= display_h:
            centre_cropped.add(section.speaker.media_id)
    pool_bindings = [
        b
        for b in broll
        if b.media_id not in speaker_ids
        and b.original.width is not None
        and b.original.height is not None
    ]
    needs_broll = any(
        s.kind == "montage" or (s.kind == "speech" and s.visual == "cutaways") for s in sections
    )
    if needs_broll and not pool_bindings:
        raise UnsupportedPhonePlan(
            "this edit plays your speech over other footage, but there is no other video clip "
            "to show. Add the clips to cut between"
        )
    pool = _BrollPool(pool_bindings)
    cut_scale = _cut_scale(sections)

    registry = _Assets()
    receipt = SpeechMontageReceipt()
    main: list[TimelineClip] = []
    audio_clips: list[TimelineClip] = []
    music_runs: list[tuple[float, float]] = []
    cursor = 0.0

    def add_main(
        binding: PhoneSourceBinding,
        source_start: float,
        duration: float,
        *,
        volume: float,
        fade_in: float | None = None,
        fade_out: float | None = None,
    ) -> None:
        nonlocal cursor
        asset_id = registry.add_video(binding)
        main.append(
            TimelineClip(
                id=f"clip-{len(main)}",
                source_asset_id=asset_id,
                source_start=round(source_start, 4),
                source_duration=round(duration, 4),
                timeline_start=round(cursor, 4),
                rate=1.0,
                volume=volume,
                audio_fade_in=fade_in,
                audio_fade_out=fade_out,
            )
        )
        cursor += round(duration, 4)

    for index, section in enumerate(sections):
        section_start = cursor
        row: dict[str, Any] = {"index": index, "kind": section.kind, "start_s": round(cursor, 3)}
        if section.kind == "montage":
            cut_s = (section.cut_s or _DEFAULT_CUT_S) * cut_scale
            count = max(1, round(section.duration_s / cut_s))
            each = section.duration_s / count
            for _ in range(count):
                binding, start, length = pool.take(each)
                add_main(binding, start, length, volume=0.0)
            music_runs.append((section_start, cursor))
            row["cuts"] = count
        else:
            assert section.speaker is not None  # validated by PhoneSpeechSection
            speaker = section.speaker
            length = section.length_s
            window = float(speaker.original.duration_s) - EXPORT_SAFETY_MARGIN_S
            start, length = section.source_start_s, length
            if start + length > window:
                if window - start < _MIN_EXCERPT_S:
                    raise UnsupportedPhonePlan(
                        "a chosen line runs past the end of your speaking clip; pick another line"
                    )
                length = window - start
                receipt.adjustments.append("trimmed a line that ran to the very end of the clip")
            fade_in = min(EXCERPT_FADE_IN_S, length / 4)
            fade_out = min(EXCERPT_FADE_OUT_S, length / 3)
            row.update(
                {
                    "visual": section.visual,
                    "media_id": speaker.media_id,
                    "quote": section.quote,
                    "source_start_s": round(start, 3),
                    "source_end_s": round(start + length, 3),
                }
            )
            if section.visual == "speaker":
                add_main(speaker, start, length, volume=1.0, fade_in=fade_in, fade_out=fade_out)
            else:
                speaker_asset = registry.add_video(speaker)
                audio_clips.append(
                    TimelineClip(
                        id=f"speech-{len(audio_clips)}",
                        source_asset_id=speaker_asset,
                        source_start=round(start, 4),
                        source_duration=round(length, 4),
                        timeline_start=round(cursor, 4),
                        rate=1.0,
                        volume=1.0,
                        audio_fade_in=fade_in,
                        audio_fade_out=fade_out,
                    )
                )
                count = max(1, round(length / CUTAWAY_HOLD_S))
                each = length / count
                covered = 0.0
                for hold_index in range(count):
                    hold = each if hold_index < count - 1 else length - covered
                    binding, b_start, b_len = pool.take(hold)
                    if b_len < hold - 1e-6:
                        # The b-roll clip ran out: keep the timeline exact by
                        # taking the remainder from the next clip.
                        add_main(binding, b_start, b_len, volume=0.0)
                        rest = hold - b_len
                        while rest > 1e-6:
                            binding, b_start, b_len = pool.take(rest)
                            add_main(binding, b_start, b_len, volume=0.0)
                            rest -= b_len
                    else:
                        add_main(binding, b_start, b_len, volume=0.0)
                    covered += hold
        row["end_s"] = round(cursor, 3)
        receipt.sections.append(row)

    if len(main) > 100:
        raise UnsupportedPhonePlan(
            "this edit needs more cuts than the phone can lay out; ask for fewer or longer cuts"
        )

    tracks = [TimelineTrack(id=MAIN_TRACK_ID, kind="video", clips=main)]
    caps = {"basicComposition", "local1080Export"}
    mix = AudioMixRecipe(original_volume=1.0, target_lufs=target_lufs)
    if audio_clips:
        tracks.append(TimelineTrack(id=SPEECH_AUDIO_TRACK_ID, kind="audio", clips=audio_clips))
        caps |= {"audioMix"}

    if music is not None:
        music_clips = _music_clips(music, music_runs, total_s=cursor, registry=registry)
        if music_clips:
            tracks.append(TimelineTrack(id=MUSIC_TRACK_ID, kind="audio", clips=music_clips))
            caps |= {"audioMix", "musicBed"}
            # The bed is its audio-track clips (silent under speech). It must NOT also
            # name `audio.music_asset_id`: the device plays that asset as a second bed
            # from source 0 on top of the clips (the KRI-481 double play), and the
            # contract verifier now refuses such a recipe.
            receipt.music = True

    if centre_cropped:
        receipt.adjustments.append(
            "your speaking clip is wider than vertical, so its sides are cropped to fill the "
            "9:16 frame (centred)"
        )
    receipt.duration_s = cursor
    receipt.cut_count = len(main)
    recipe = EditRecipeV2(
        canvas=_STORY_CANVAS,
        assets=list(registry.assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(registry.manifest.values())),
        tracks=tracks,
        text_layers=[],
        audio=mix,
        required_capabilities=caps,
    )
    return recipe, receipt


def _music_clips(
    music: PhoneMusicBed,
    runs: list[tuple[float, float]],
    *,
    total_s: float,
    registry: _Assets,
) -> list[TimelineClip]:
    """Split music clips that sound only during montage runs (silent under speech).

    The bed's own position advances only while it is heard, so after a speech
    section the song resumes where it left off instead of jumping.
    """
    merged: list[list[float]] = []
    for start, end in runs:
        if merged and abs(merged[-1][1] - start) < 1e-6:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    asset_id = f"music-{music.catalog_id}"
    asset = LibraryRenderAsset(
        id=asset_id,
        catalog="music",
        catalog_id=music.catalog_id,
        generation=music.generation,
        fingerprint=music.fingerprint,
    )
    clips: list[TimelineClip] = []
    position = max(0.0, float(music.start_s))
    available = float(music.duration_s) if music.duration_s else None
    gain = max(0.0, min(float(music.volume), _MUSIC_GAIN_CAP))
    for start, end in merged:
        length = end - start
        if length < _MIN_MUSIC_RUN_S:
            continue
        if available is not None:
            length = min(length, available - position - EXPORT_SAFETY_MARGIN_S)
            if length < _MIN_MUSIC_RUN_S:
                break
        clips.append(
            TimelineClip(
                id=f"music-{len(clips)}",
                source_asset_id=asset_id,
                source_start=round(position, 4),
                source_duration=round(length, 4),
                timeline_start=round(start, 4),
                rate=1.0,
                volume=gain,
                audio_fade_in=min(_MUSIC_FADE_IN_S, audio_fade(length)),
                audio_fade_out=min(_MUSIC_FADE_OUT_S, audio_fade(length)),
            )
        )
        position += length
    if clips:
        registry.manifest[asset_id] = asset
        registry.assets[asset_id] = MediaAsset(
            id=asset_id,
            relative_path=asset_id,
            fingerprint=AssetFingerprint(
                hex=music.fingerprint.sha256, byte_count=music.fingerprint.byte_count
            ),
            duration=music.duration_s,
        )
    return clips


__all__ = [
    "CUTAWAY_HOLD_S",
    "EXCERPT_FADE_IN_S",
    "EXCERPT_FADE_OUT_S",
    "MAIN_TRACK_ID",
    "MUSIC_TRACK_ID",
    "SPEECH_AUDIO_TRACK_ID",
    "PhoneSpeechSection",
    "SpeechMontageReceipt",
    "compile_phone_speech_montage_plan",
]
