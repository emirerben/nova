"""Resolve the one foreground narration source used by speech cleanup.

This module is deliberately independent of SQLAlchemy, routes, tasks, and media
I/O.  Callers must pass server-verified media registrations and, when a renderer
needs to choose between clips, the speech-coverage values produced by its probe
step.  The result is therefore safe to reuse at mutation, preflight, and render
dispatch boundaries without consulting "whatever object currently lives at a
path".

Selection mirrors the render contract:

* an active uploaded/recorded voiceover wins and is analyzed in its bounded window;
* a subtitled ("Talking to camera") edit uses its single embedded-audio clip;
* talking-head and multi-clip self-narrated edits use the highest-coverage clip;
* supporting/background media never participates in selection or fingerprinting.

The policy fingerprint intentionally contains only the selected foreground
source, its immutable storage generation, the exact renderer-owned window, and
format/audio/detector policy.  Adding or reordering visual-only footage therefore
cannot invalidate an unchanged active voiceover.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Literal

NarrationMediaKind = Literal["audio", "video"]
NarrationSourceKind = Literal["voiceover", "embedded_spine"]
NarrationRenderer = Literal["subtitled", "talking_head", "narrated"]
NarrationUnavailableReason = Literal[
    "unsupported_format",
    "no_media",
    "unverified_generation",
    "no_audio",
    "ambiguous_embedded_source",
    "speech_coverage_unavailable",
    "no_speech_spine",
    "spine_too_short",
    "pinned_spine_unavailable",
]

SUPPORTED_EDIT_FORMATS = frozenset(
    {"subtitled", "talking_head", "narrated", "narrated_planned", "narrated_ready"}
)
NARRATED_EDIT_FORMATS = frozenset({"narrated", "narrated_planned", "narrated_ready"})

# These values are the renderer invariants currently used by
# generative_build._resolve_archetype and talking_head_assembler.spine_cut_cap_s.
# The render adapter should import this module when it is migrated, leaving one
# canonical owner rather than maintaining a third copy.
MIN_SPINE_SPEECH_COVERAGE = 0.15
MIN_TALKING_HEAD_SPINE_WITH_BROLL_S = 2.0
SPINE_WINDOW_MIN_S = 120.0
SPINE_WINDOW_MAX_S = 300.0
MAX_ANALYSIS_WINDOW_S = 300.0


@dataclass(frozen=True, slots=True)
class RegisteredNarrationMedia:
    """One server-verified source candidate.

    ``generation`` is mandatory at resolution time.  ``manifest_identity`` is
    an optional stable assignment/snapshot identifier for the selected source;
    it is not a hash of the media bytes.  ``speech_coverage`` is needed only
    when the applicable renderer must select a spine from multiple clips.
    """

    media_id: str
    storage_path: str
    generation: str
    media_kind: NarrationMediaKind
    duration_s: float
    has_audio: bool
    speech_coverage: float | None = None
    trim_start_s: float = 0.0
    trim_end_s: float | None = None
    manifest_identity: str | None = None
    foreground: bool = True


@dataclass(frozen=True, slots=True)
class ActiveNarrationRequest:
    """Pure inputs needed to select and fingerprint active narration."""

    edit_format: str
    audio_mode: str
    detector_policy: str
    clips: tuple[RegisteredNarrationMedia, ...] = ()
    voiceover: RegisteredNarrationMedia | None = None
    pinned_spine_media_id: str | None = None
    target_duration_s: float | None = None


@dataclass(frozen=True, slots=True)
class ActiveNarrationSource:
    """Immutable foreground source selected for one cleanup policy."""

    source_kind: NarrationSourceKind
    media_id: str
    storage_path: str
    generation: str
    media_kind: NarrationMediaKind
    manifest_identity: str | None
    window_start_s: float
    window_end_s: float
    edit_format: str
    audio_mode: str
    resolved_renderer: NarrationRenderer
    detector_policy: str
    source_policy_fingerprint: str

    @property
    def window_duration_s(self) -> float:
        return self.window_end_s - self.window_start_s

    def to_private_dict(self) -> dict[str, object]:
        """Return the exact private source snapshot; never expose it publicly."""

        return {
            "source_kind": self.source_kind,
            "media_id": self.media_id,
            "storage_path": self.storage_path,
            "generation": self.generation,
            "media_kind": self.media_kind,
            "manifest_identity": self.manifest_identity,
            "window_start_s": self.window_start_s,
            "window_end_s": self.window_end_s,
            "edit_format": self.edit_format,
            "audio_mode": self.audio_mode,
            "resolved_renderer": self.resolved_renderer,
            "detector_policy": self.detector_policy,
            "source_policy_fingerprint": self.source_policy_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class ActiveNarrationResolution:
    """Status-bearing result; unavailable media is expected, not exceptional."""

    source: ActiveNarrationSource | None
    reason: NarrationUnavailableReason | None
    video_present: bool

    @property
    def available(self) -> bool:
        return self.source is not None


def talking_head_window_cap_s(target_duration_s: float | None) -> float:
    """Return the exact renderer cap for a talking-head analysis window."""

    if target_duration_s is None or float(target_duration_s) <= 0:
        return SPINE_WINDOW_MAX_S
    return min(
        max(float(target_duration_s) * 2.0, SPINE_WINDOW_MIN_S),
        SPINE_WINDOW_MAX_S,
    )


def _normalized_media_window(
    media: RegisteredNarrationMedia,
    *,
    renderer: NarrationRenderer,
    target_duration_s: float | None,
) -> tuple[float, float] | None:
    values = (media.duration_s, media.trim_start_s)
    if not all(math.isfinite(float(value)) for value in values):
        return None
    duration = float(media.duration_s)
    start = max(0.0, float(media.trim_start_s))
    raw_end = duration if media.trim_end_s is None else float(media.trim_end_s)
    if not math.isfinite(raw_end):
        return None
    # The shared preflight ceiling applies to every source kind. Talking-head
    # can impose a tighter target-derived cap below, but no voiceover or
    # subtitled source may accidentally hand the worker an unbounded window.
    end = min(duration, raw_end, start + MAX_ANALYSIS_WINDOW_S)
    if renderer == "talking_head":
        end = min(end, start + talking_head_window_cap_s(target_duration_s))
    if duration <= 0 or end <= start:
        return None
    return round(start, 6), round(end, 6)


def _fingerprint(
    *,
    request: ActiveNarrationRequest,
    media: RegisteredNarrationMedia,
    source_kind: NarrationSourceKind,
    renderer: NarrationRenderer,
    window: tuple[float, float],
) -> str:
    # Version the canonical payload so a future identity policy can coexist
    # with already-persisted decisions instead of silently changing their hash.
    payload = {
        "schema": "active-narration-v1",
        "source_kind": source_kind,
        "media_id": media.media_id,
        "storage_path": media.storage_path,
        "generation": media.generation,
        "manifest_identity": media.manifest_identity,
        "window": [window[0], window[1]],
        "edit_format": request.edit_format,
        "audio_mode": request.audio_mode,
        "resolved_renderer": renderer,
        "detector_policy": request.detector_policy,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _unavailable(
    reason: NarrationUnavailableReason,
    *,
    video_present: bool,
) -> ActiveNarrationResolution:
    return ActiveNarrationResolution(source=None, reason=reason, video_present=video_present)


def _build_source(
    request: ActiveNarrationRequest,
    media: RegisteredNarrationMedia,
    *,
    source_kind: NarrationSourceKind,
    renderer: NarrationRenderer,
    video_present: bool,
) -> ActiveNarrationResolution:
    if not media.media_id.strip() or not media.storage_path.strip() or not media.generation.strip():
        return _unavailable("unverified_generation", video_present=video_present)
    if not media.has_audio:
        return _unavailable("no_audio", video_present=video_present)
    window = _normalized_media_window(
        media,
        renderer=renderer,
        target_duration_s=request.target_duration_s,
    )
    if window is None:
        return _unavailable("no_audio", video_present=video_present)
    source = ActiveNarrationSource(
        source_kind=source_kind,
        media_id=media.media_id,
        storage_path=media.storage_path,
        generation=media.generation,
        media_kind=media.media_kind,
        manifest_identity=media.manifest_identity,
        window_start_s=window[0],
        window_end_s=window[1],
        edit_format=request.edit_format,
        audio_mode=request.audio_mode,
        resolved_renderer=renderer,
        detector_policy=request.detector_policy,
        source_policy_fingerprint=_fingerprint(
            request=request,
            media=media,
            source_kind=source_kind,
            renderer=renderer,
            window=window,
        ),
    )
    return ActiveNarrationResolution(source=source, reason=None, video_present=video_present)


def resolve_active_narration_source(
    request: ActiveNarrationRequest,
) -> ActiveNarrationResolution:
    """Resolve exactly one generation-pinned foreground narration source.

    The function never falls back from an explicit renderer-pinned spine to a
    different clip.  Missing coverage for a multi-clip decision also remains
    unavailable instead of guessing by upload order; guessing would bind a
    consent decision to speech the renderer may not use.
    """

    edit_format = str(request.edit_format or "").strip().lower()
    audio_mode = str(request.audio_mode or "").strip().lower()
    detector_policy = str(request.detector_policy or "").strip()
    normalized = ActiveNarrationRequest(
        edit_format=edit_format,
        audio_mode=audio_mode,
        detector_policy=detector_policy,
        clips=tuple(request.clips),
        voiceover=request.voiceover,
        pinned_spine_media_id=request.pinned_spine_media_id,
        target_duration_s=request.target_duration_s,
    )
    video_present = any(media.media_kind == "video" for media in normalized.clips)
    if edit_format not in SUPPORTED_EDIT_FORMATS or not detector_policy:
        return _unavailable("unsupported_format", video_present=video_present)

    # A retained voiceover is active only when the audio policy selects it.
    # Inactive/resumable takes must never displace embedded foreground speech.
    if normalized.voiceover is not None and audio_mode == "voiceover":
        return _build_source(
            normalized,
            normalized.voiceover,
            source_kind="voiceover",
            renderer="narrated",
            video_present=video_present,
        )

    embedded = tuple(
        media
        for media in normalized.clips
        if media.media_kind == "video" and media.foreground and media.has_audio
    )
    if not embedded:
        all_video = tuple(
            media for media in normalized.clips if media.media_kind == "video" and media.foreground
        )
        return _unavailable(
            "no_audio" if all_video else "no_media",
            video_present=video_present,
        )

    if normalized.pinned_spine_media_id:
        selected = next(
            (media for media in embedded if media.media_id == normalized.pinned_spine_media_id),
            None,
        )
        if selected is None:
            return _unavailable("pinned_spine_unavailable", video_present=video_present)
        renderer: NarrationRenderer = (
            "subtitled"
            if edit_format == "subtitled"
            or (edit_format in NARRATED_EDIT_FORMATS and len(embedded) == 1)
            else "talking_head"
        )
        return _build_source(
            normalized,
            selected,
            source_kind="embedded_spine",
            renderer=renderer,
            video_present=video_present,
        )

    if edit_format == "subtitled":
        if len(embedded) != 1:
            return _unavailable("ambiguous_embedded_source", video_present=video_present)
        return _build_source(
            normalized,
            embedded[0],
            source_kind="embedded_spine",
            renderer="subtitled",
            video_present=video_present,
        )

    if edit_format in NARRATED_EDIT_FORMATS and len(embedded) == 1:
        return _build_source(
            normalized,
            embedded[0],
            source_kind="embedded_spine",
            renderer="subtitled",
            video_present=video_present,
        )

    if any(media.speech_coverage is None for media in embedded):
        return _unavailable("speech_coverage_unavailable", video_present=video_present)
    selected = max(
        embedded,
        key=lambda media: float(media.speech_coverage or 0.0),
    )
    coverage = float(selected.speech_coverage or 0.0)
    if not math.isfinite(coverage) or coverage < MIN_SPINE_SPEECH_COVERAGE:
        return _unavailable("no_speech_spine", video_present=video_present)
    if (
        edit_format in NARRATED_EDIT_FORMATS
        and len(embedded) > 1
        and float(selected.duration_s) <= MIN_TALKING_HEAD_SPINE_WITH_BROLL_S
    ):
        return _unavailable("spine_too_short", video_present=video_present)
    return _build_source(
        normalized,
        selected,
        source_kind="embedded_spine",
        renderer="talking_head",
        video_present=video_present,
    )
