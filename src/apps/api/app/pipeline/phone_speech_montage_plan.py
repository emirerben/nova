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

KRI-479 adds a second, simpler composition next to the excerpt one:
`compile_phone_voice_behind_footage_plan` -- ONE clip's voice plays continuously
under the whole edit while the other clips are the silent picture, each shown
once in the order it is given. Its signature takes typed facts only (clips,
timings, the approved opening words); it never reads a request, so a render path
cannot re-decide what the approved plan already decided.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
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


# --- KRI-479: one voice behind silent footage ------------------------------------------

VOICE_FOOTAGE_TRACK_ID = "voice-footage"
VOICE_AUDIO_TRACK_ID = "voice"
# The voice may stop up to this long before the picture ends when it is cut on a sentence
# end instead of mid-word. The verifier (`voice_covers_timeline`) allows exactly this slack.
VOICE_TAIL_SLACK_S = 3.0
_FPS = 30
_VOICE_HARD_CUT_FADE_S = 0.5
_VOICE_LEAD_S = 0.06  # mirrors `speech_segments.EXCERPT_LEAD_S`
_VOICE_TAIL_S = 0.22  # mirrors `speech_segments.EXCERPT_TAIL_S`
_MIN_VOICE_WORDS = 6  # mirrors `speech_segments.MIN_SPEECH_WORDS`
_DEFAULT_TITLE_HOLD_S = 3.0
_VOICE_FIELD_PATH = "montage_audio.source_media_ids[]"
_ALT_PICK_VOICE = "Pick a clip where you talk to use as the voice, or ask for a plain montage."


def _decline(
    message: str, *, reason: str, field_path: str | None, alternative: str
) -> UnsupportedPhonePlan:
    """A typed decline (the same class the verifier raises) so the reason reaches the creator."""
    from app.services.creator_render_contract import CreatorRenderContractError  # noqa: PLC0415

    return CreatorRenderContractError(
        message,
        decline_reason=reason,  # type: ignore[arg-type]
        field_path=field_path,
        alternative=alternative,
    )


def _seconds(value: float) -> str:
    return f"{value:.0f}" if abs(value - round(value)) < 0.05 else f"{value:.1f}"


@dataclass(frozen=True)
class VoiceWindow:
    """The stretch of the voice clip that plays, in source seconds, from timeline 0."""

    start_s: float
    end_s: float
    # Cut mid-speech (no sentence end close to the cap): a longer fade-out hides it.
    hard_cut: bool = False
    adjustments: tuple[str, ...] = ()

    @property
    def length_s(self) -> float:
        return self.end_s - self.start_s


def select_voice_window(
    words: Sequence[Any], *, source_duration_s: float, max_length_s: float | None
) -> VoiceWindow:
    """Where the voice starts and stops, from word timings alone.

    Starts a hair before the first spoken word. Plays all of the speech when it fits
    ``max_length_s``; otherwise ends at the last sentence end inside the cap when that is
    within ``VOICE_TAIL_SLACK_S`` of it, else at the last word that fits (``hard_cut``).
    Never runs past the source. Raises a typed decline when the clip has next to no speech.
    """
    from app.services.speech_segments import words_to_segments  # noqa: PLC0415

    rows = [
        w
        for w in (
            w
            if isinstance(w, dict)
            else {
                "text": getattr(w, "text", ""),
                "start_s": getattr(w, "start_s", 0.0),
                "end_s": getattr(w, "end_s", 0.0),
            }
            for w in words or []
        )
        if str(w.get("text") or "").strip() and float(w.get("end_s") or 0) > 0
    ]
    if len(rows) < _MIN_VOICE_WORDS:
        raise _decline(
            "I couldn't find clear speech in the clip you picked as the voice.",
            reason="capability_unavailable",
            field_path=_VOICE_FIELD_PATH,
            alternative=_ALT_PICK_VOICE,
        )
    start = max(0.0, float(rows[0]["start_s"]) - _VOICE_LEAD_S)
    playable_end = float(source_duration_s) - EXPORT_SAFETY_MARGIN_S
    cap_end = playable_end if max_length_s is None else min(playable_end, start + max_length_s)
    last_end = float(rows[-1]["end_s"]) + _VOICE_TAIL_S
    if last_end <= cap_end:
        return VoiceWindow(start_s=round(start, 3), end_s=round(last_end, 3))
    # More speech than room: cut it at the cap and say so.
    sentence_ends = [
        seg.end_s + _VOICE_TAIL_S
        for seg in words_to_segments(rows)
        if seg.end_s + _VOICE_TAIL_S <= cap_end
    ]
    word_ends = [
        float(w["end_s"]) + _VOICE_TAIL_S
        for w in rows
        if float(w["end_s"]) + _VOICE_TAIL_S <= cap_end
    ]
    if not word_ends or word_ends[-1] - start < _MIN_EXCERPT_S:
        raise _decline(
            "The speech in the voice clip starts too late to fit this edit.",
            reason="capability_unavailable",
            field_path=_VOICE_FIELD_PATH,
            alternative=_ALT_PICK_VOICE,
        )
    if sentence_ends and cap_end - sentence_ends[-1] <= VOICE_TAIL_SLACK_S:
        end, hard = sentence_ends[-1], False
    else:
        end, hard = word_ends[-1], True
    note = f"used the first {_seconds(end - start)} seconds of your voice"
    return VoiceWindow(
        start_s=round(start, 3),
        end_s=round(end, 3),
        hard_cut=hard,
        adjustments=(note + ("" if hard else ", ending on a full sentence"),),
    )


@dataclass
class VoiceBehindFootageReceipt:
    """What the composer actually put on the timeline."""

    voice_media_id: str = ""
    voice_source_start_s: float = 0.0
    voice_span_s: float = 0.0
    duration_s: float = 0.0
    shots: list[dict[str, Any]] = field(default_factory=list)
    title_hold_s: float | None = None
    min_shot_s: float = 0.0
    adjustments: list[str] = field(default_factory=list)

    def record(self) -> dict[str, Any]:
        return {
            "voice": {
                "media_id": self.voice_media_id,
                "source_start_s": round(self.voice_source_start_s, 3),
                "span_s": round(self.voice_span_s, 3),
            },
            "duration_s": round(self.duration_s, 3),
            "shots": self.shots,
            "title_hold_s": None if self.title_hold_s is None else round(self.title_hold_s, 3),
            "min_shot_s": round(self.min_shot_s, 3),
            "adjustments": self.adjustments,
        }


def _allocate_frames(usable: list[int], total: int, floor: int) -> list[int] | None:
    """Split ``total`` frames over the clips: every clip once, at least ``floor`` (or all it has),
    at most what it has, the rest spread evenly. ``None`` when it cannot be done."""
    if sum(usable) < total:
        return None
    alloc = [min(u, floor) for u in usable]
    remaining = total - sum(alloc)
    if remaining < 0:
        return None
    active = [i for i, u in enumerate(usable) if alloc[i] < u]
    while remaining > 0 and active:
        share, extra = divmod(remaining, len(active))
        for rank, i in enumerate(active):
            give = min(share + (1 if rank < extra else 0), usable[i] - alloc[i])
            alloc[i] += give
            remaining -= give
        active = [i for i in active if alloc[i] < usable[i]]
    return alloc if remaining == 0 else None


def compile_phone_voice_behind_footage_plan(
    voice: PhoneSourceBinding,
    voice_window: VoiceWindow,
    picture: Sequence[PhoneSourceBinding],
    *,
    duration_s: float,
    opening_title: str | None = None,
    opening_title_hold_s: float | None = None,
    min_shot_s: float | None = None,
    allow_silent_tail: bool = False,
    target_lufs: float | None = None,
) -> tuple[EditRecipeV2, VoiceBehindFootageReceipt]:
    """One clip's voice plays under the whole edit; the other clips are the silent picture.

    ``picture`` is already in the order the plan fixed (the contract's ``order_ids``, voice
    clip excluded): every clip is shown once, never wrapped or re-sorted. The voice window is
    contiguous from timeline 0 and never longer than the picture, so it cannot stretch
    ``recipe.duration``. A shot is never shorter than ``min_shot_s`` (default
    ``MIN_READABLE_SHOT_S``) unless its whole clip is: when the clips cannot fit the length,
    or the footage cannot fill it, or the voice is shorter than the edit without a chosen
    silent tail, this raises a typed decline instead of flash-cutting, looping or guessing.
    """
    from app.pipeline.unified_montage import MIN_READABLE_SHOT_S  # noqa: PLC0415

    floor_s = MIN_READABLE_SHOT_S if min_shot_s is None else float(min_shot_s)
    if voice.original.has_audio is not True:
        raise _decline(
            "The clip you picked as the voice has no sound.",
            reason="capability_unavailable",
            field_path=_VOICE_FIELD_PATH,
            alternative=_ALT_PICK_VOICE,
        )
    shots_in = list(picture)
    if not shots_in:
        raise _decline(
            "This edit plays your voice over other footage, but there is no other clip to show.",
            reason="capability_unavailable",
            field_path=_VOICE_FIELD_PATH,
            alternative="Add the clips to cut between, or ask for a plain montage.",
        )
    if any(b.media_id == voice.media_id for b in shots_in):
        raise _decline(
            "The voice clip can't also be one of the clips shown.",
            reason="requirement_conflict",
            field_path="ordering_choice",
            alternative="Ask for the voice clip's picture to stay hidden, or pick another voice.",
        )
    if any(b.original.width is None or b.original.height is None for b in shots_in):
        raise _decline(
            "One of the clips shown has no picture.",
            reason="capability_unavailable",
            field_path="ordering_choice",
            alternative="Use video clips for the picture.",
        )

    total_frames = max(1, round(float(duration_s) * _FPS))
    duration = total_frames / _FPS
    floor_frames = max(1, math.ceil(floor_s * _FPS - 1e-9))
    usable = [
        max(0, int(math.floor((float(b.original.duration_s) - EXPORT_SAFETY_MARGIN_S) * _FPS)))
        for b in shots_in
    ]
    if sum(min(u, floor_frames) for u in usable) > total_frames:
        needed = math.ceil(sum(min(u, floor_frames) for u in usable) / _FPS * 10 - 1e-9) / 10
        raise _decline(
            f"{len(shots_in)} clips can't each stay on screen long enough to be seen in "
            f"{_seconds(duration)} seconds.",
            reason="requirement_conflict",
            field_path="target_duration_s",
            alternative=(
                f"Extend it to {_seconds(needed)} seconds so every clip is seen, "
                "or use fewer clips."
            ),
        )
    frames = _allocate_frames(usable, total_frames, floor_frames)
    if frames is None:
        available = sum(usable) / _FPS
        raise _decline(
            f"The other clips add up to {_seconds(available)} seconds, not "
            f"{_seconds(duration)}, and I won't loop them.",
            reason="requirement_conflict",
            field_path="target_duration_s",
            alternative=(
                f"Shorten the edit to about {_seconds(math.floor(available))} seconds, "
                "or add more footage."
            ),
        )

    receipt = VoiceBehindFootageReceipt(
        voice_media_id=voice.media_id, duration_s=duration, min_shot_s=floor_frames / _FPS
    )
    registry = _Assets()
    footage: list[TimelineClip] = []
    cursor = 0
    for index, (binding, count) in enumerate(zip(shots_in, frames, strict=True)):
        length = count / _FPS
        start, length = refit_source_window(0.0, length, float(binding.original.duration_s))
        footage.append(
            TimelineClip(
                id=f"clip-{index}",
                source_asset_id=registry.add_video(binding),
                source_start=round(start, 4),
                source_duration=round(count / _FPS, 4),
                timeline_start=round(cursor / _FPS, 4),
                rate=1.0,
                volume=0.0,
            )
        )
        receipt.shots.append(
            {
                "media_id": binding.media_id,
                "start_s": round(cursor / _FPS, 3),
                "duration_s": round(count / _FPS, 3),
            }
        )
        cursor += count

    # The voice: one contiguous clip from timeline 0, never past the picture.
    cap = duration - EXPORT_SAFETY_MARGIN_S
    voice_len = min(voice_window.length_s, cap)
    hard_cut = voice_window.hard_cut
    receipt.adjustments.extend(voice_window.adjustments)
    if voice_window.length_s > cap + 1e-6:
        hard_cut = True
        receipt.adjustments.append(f"used the first {_seconds(voice_len)} seconds of your voice")
    if voice_len < duration - VOICE_TAIL_SLACK_S - 1e-6:
        if not allow_silent_tail:
            raise _decline(
                f"Your voice runs {_seconds(voice_len)} seconds but the edit is "
                f"{_seconds(duration)}.",
                reason="requirement_conflict",
                field_path="target_duration_s",
                alternative=(
                    f"End the edit when your voice ends ({_seconds(voice_len)} seconds), or keep "
                    "the length and let the last seconds play without voice."
                ),
            )
        receipt.adjustments.append(
            f"the last {_seconds(duration - voice_len)} seconds play without voice"
        )
    fade_in = min(EXCERPT_FADE_IN_S, voice_len / 4)
    fade_out = min(_VOICE_HARD_CUT_FADE_S if hard_cut else EXCERPT_FADE_OUT_S, voice_len / 3)
    voice_asset = registry.add_video(voice)
    voice_clip = TimelineClip(
        id="voice-0",
        source_asset_id=voice_asset,
        source_start=round(voice_window.start_s, 4),
        source_duration=round(voice_len, 4),
        timeline_start=0.0,
        rate=1.0,
        volume=1.0,
        audio_fade_in=fade_in,
        audio_fade_out=fade_out,
    )
    receipt.voice_source_start_s = voice_window.start_s
    receipt.voice_span_s = voice_len

    recipe = EditRecipeV2(
        canvas=_STORY_CANVAS,
        assets=list(registry.assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(registry.manifest.values())),
        tracks=[
            TimelineTrack(id=VOICE_FOOTAGE_TRACK_ID, kind="video", clips=footage),
            TimelineTrack(id=VOICE_AUDIO_TRACK_ID, kind="audio", clips=[voice_clip]),
        ],
        text_layers=[],
        audio=AudioMixRecipe(original_volume=1.0, target_lufs=target_lufs),
        required_capabilities={"basicComposition", "local1080Export", "audioMix"},
    )
    if opening_title:
        from app.pipeline.phone_narrated_plan import (  # noqa: PLC0415
            _compile_title_layers,
            _with_text_layers,
            narrated_title_element,
        )

        hold = (
            _DEFAULT_TITLE_HOLD_S if opening_title_hold_s is None else float(opening_title_hold_s)
        )
        title = narrated_title_element(
            opening_title, end_s=hold, timeline_duration_s=duration, canvas=_STORY_CANVAS
        )
        if title is not None:
            recipe = _with_text_layers(
                recipe,
                _compile_title_layers([title], canvas=_STORY_CANVAS, timeline_duration_s=duration),
            )
            receipt.title_hold_s = float(title.end_s) - float(title.start_s)
    return recipe, receipt


__all__ = [
    "VOICE_AUDIO_TRACK_ID",
    "VOICE_FOOTAGE_TRACK_ID",
    "VOICE_TAIL_SLACK_S",
    "VoiceBehindFootageReceipt",
    "VoiceWindow",
    "compile_phone_voice_behind_footage_plan",
    "select_voice_window",
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
