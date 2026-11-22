"""The song under a phone Talking edit (KRI-1562, KRI-1578).

One owner for what every path that gives a Talking edit a song agrees on. The song is the
creator's own upload (KRI-1578): `uploaded_song` reads it off the plan item, the variant keeps the
creator's choices for it as ``talking_song`` (`talking_song_for`), and `talking_bed_choice` is the
one precedence rule the worker and the Save share. The music LIBRARY is legacy, behind
``TALKING_LIBRARY_MUSIC_ENABLED`` (`library_music_enabled`): which tracks may play
(`song_eligible`), the treatment a chosen track becomes (`song_treatment`, the variant's
``smart_music_treatment``) and the rows chat may choose from (`candidate_row`). Whether a compiled
recipe really plays a song is `recipe_plays_music`, and why a requested song is missing is the
worker's `RECEIPT_FIELD`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.schemas.user_song import UserSongPlan

# The bed sits well under the voice and drops further while anyone speaks (`MusicBedTreatment`).
GAIN_DB = -18.0
SPEECH_DUCK_DB = -12.0
FINAL_LUFS = -14.0
# KRI-1578: the creator's song plays at the library bed's level until she moves it. It is the
# absolute clip gain (0-1) the editor's song volume shows, not a scale on top of anything.
CREATOR_SONG_VOLUME = round(10 ** (GAIN_DB / 20), 6)
# The variant's record of the creator's choices for her song (a `UserSongPlan`, background mode),
# or ``{"removed": true}`` once she took it off this edit.
TALKING_SONG_FIELD = "talking_song"
# How many library songs chat may choose from. The copilot's MUSIC block prints at most 20.
CANDIDATE_LIMIT = 20
# Rows read before the Python predicate filters them (labels and sections live in JSON).
LIBRARY_SCAN_LIMIT = 80

# The Talking worker's record of a song the creator asked for (`variant[RECEIPT_FIELD]`).
RECEIPT_FIELD = "talking_music"
PLAYED = "played"
NOT_PLAYED = "not_played"
# Why a requested song is missing, as the worker records it.
APP_TOO_OLD = "app_too_old"
EMPTY_LIBRARY = "empty_library"
NO_MATCH = "no_match"
SONG_UNAVAILABLE = "song_unavailable"
NO_MUSIC_ASKED = "no_music_asked"
# KRI-1578: music was asked for, no song is uploaded and the library is closed.
NO_UPLOAD = "no_upload"
# Whose song a played receipt names.
SOURCE_CREATOR_SONG = "creator_song"
SOURCE_LIBRARY = "library"


def library_music_enabled() -> bool:
    """May a Talking edit get a NEW song from the music library (KRI-1578)? A library song
    already on a variant plays either way."""
    from app.config import settings  # noqa: PLC0415

    return bool(settings.talking_library_music_enabled)


@dataclass(frozen=True)
class UploadedSong:
    """The song the creator uploaded to a plan item, as its columns hold it now."""

    plan_item_id: str
    gcs_path: str
    generation: int
    duration_s: float | None
    filename: str | None = None


@dataclass(frozen=True)
class UploadedSongRead:
    """What a Save's caller read off the job's plan item; ``song`` None means none is there.

    A Save given no read keeps the song its recipe pinned and adds none."""

    song: UploadedSong | None


def uploaded_song(item: Any) -> UploadedSong | None:
    """The plan item's uploaded song when its soundtrack lets it play, else None.

    A montage plays it only in ``song`` audio mode. A Talking item always keeps ``original``:
    the voice is its soundtrack and ``audio_mode`` is part of the speech fingerprint the
    creator's cleanup consent is bound to (`active_narration_source`), so a song there is the
    upload alone. The device grant asks this same question.
    """
    if item is None or not getattr(item, "song_gcs_path", None):
        return None
    try:
        generation = int(getattr(item, "song_generation", None) or 0)
    except (TypeError, ValueError):
        return None
    if generation <= 0:
        return None
    if not (
        getattr(item, "audio_mode", None) == "song"
        or str(getattr(item, "edit_format", None) or "") == "subtitled"
    ):
        return None
    try:
        duration = float(getattr(item, "song_duration_s", None) or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return UploadedSong(
        plan_item_id=str(item.id),
        gcs_path=str(item.song_gcs_path),
        generation=generation,
        duration_s=duration if math.isfinite(duration) and duration > 0 else None,
        filename=getattr(item, "song_filename", None),
    )


def talking_song_removed(variant: Mapping[str, Any] | None) -> bool:
    raw = (variant or {}).get(TALKING_SONG_FIELD)
    return isinstance(raw, Mapping) and raw.get("removed") is True


def talking_song(variant: Mapping[str, Any] | None) -> UserSongPlan | None:
    """The creator's song choices stored on this Talking variant, None when it plays none."""
    from app.schemas.user_song import UserSongPlan  # noqa: PLC0415

    raw = (variant or {}).get(TALKING_SONG_FIELD)
    if not isinstance(raw, Mapping) or raw.get("removed") is True:
        return None
    try:
        return UserSongPlan.model_validate(raw)
    except ValueError:
        return None


def default_talking_song(
    song: UploadedSong, *, volume: float = CREATOR_SONG_VOLUME
) -> UserSongPlan:
    """The whole uploaded song from its start, at the Talking level, looped under the talk."""
    from app.schemas.user_song import UserSongPlan  # noqa: PLC0415

    if song.duration_s is None:
        raise ValueError("the uploaded song has no known length")
    return UserSongPlan(
        mode="background",
        plan_item_id=song.plan_item_id,
        generation=song.generation,
        duration_s=song.duration_s,
        window_start_s=0.0,
        window_end_s=song.duration_s,
        volume=volume,
    )


def talking_song_for(
    variant: Mapping[str, Any] | None, uploaded: UploadedSong | None, *, adopt: bool
) -> UserSongPlan | None:
    """The choices the uploaded song plays with on this variant, or None.

    None when nothing is uploaded or the creator took her song off this edit. Choices made for
    this exact upload are kept; a replaced upload keeps only her volume (a window belongs to one
    song). With no choices stored, ``adopt`` (a first render, a Save adding the song) starts from
    the defaults; otherwise the variant plays none (a later Save never adds a song by itself).
    """
    if uploaded is None or talking_song_removed(variant):
        return None
    stored = talking_song(variant)
    if stored is not None and (stored.plan_item_id, stored.generation) == (
        uploaded.plan_item_id,
        uploaded.generation,
    ):
        return stored
    if stored is None and not adopt:
        return None
    return default_talking_song(
        uploaded, volume=stored.volume if stored is not None else CREATOR_SONG_VOLUME
    )


@dataclass(frozen=True)
class TalkingBedChoice:
    """Which song a Talking compile plays: the creator's upload, a stored library song, or none."""

    song: UserSongPlan | None = None
    treatment: Mapping[str, Any] | None = None


def talking_bed_choice(
    variant: Mapping[str, Any] | None,
    uploaded: UploadedSong | None,
    *,
    no_music: bool,
    adopt: bool,
) -> TalkingBedChoice:
    """The one precedence rule (KRI-1578): a live "no music" plays nothing; else the creator's
    own song (unless she took it off); else a library song already stored on the variant."""
    if no_music:
        return TalkingBedChoice()
    from app.services.phone_rollout import phone_user_song_supported  # noqa: PLC0415

    song = talking_song_for(variant, uploaded, adopt=adopt) if phone_user_song_supported() else None
    if song is not None:
        return TalkingBedChoice(song=song)
    treatment = (variant or {}).get("smart_music_treatment")
    return TalkingBedChoice(treatment=treatment if isinstance(treatment, Mapping) else None)


def variant_plays_song(variant: Mapping[str, Any] | None) -> bool:
    """Does this variant's stored state still ask for a song (a library treatment or a live
    ``talking_song``)? The Save's app-build gate reads it."""
    return isinstance((variant or {}).get("smart_music_treatment"), Mapping) or (
        talking_song(variant) is not None
    )


def _duration(track: Any) -> float:
    try:
        value = float(getattr(track, "duration_s", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) and 0 < value <= 1800 else 0.0


def song_eligible(track: Any) -> bool:
    """A library track a Talking edit may play: published, analysed, not archived, a catalog
    path, a length, current readable labels (the matcher and chat read them) and a current
    rank-1 section (the part that plays). Stricter than both the Save route's and the worker
    pin's checks, so a song chosen here is never refused later."""
    from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION  # noqa: PLC0415
    from app.services.music_sections import current_best_section_for_track  # noqa: PLC0415

    return bool(
        track is not None
        and getattr(track, "analysis_status", None) == "ready"
        and getattr(track, "published_at", None) is not None
        and getattr(track, "archived_at", None) is None
        and str(getattr(track, "audio_gcs_path", "") or "").startswith("music/")
        and _duration(track) > 0
        and getattr(track, "label_version", None) == CURRENT_LABEL_VERSION
        and _readable_labels(track)
        and current_best_section_for_track(track) is not None
    )


def _readable_labels(track: Any) -> bool:
    """The matcher drops a track whose labels don't parse, so it is never offered."""
    from app.agents._schemas.music_labels import MusicLabels  # noqa: PLC0415

    try:
        MusicLabels(**_labels(track))
    except (TypeError, ValueError):
        return False
    return True


def library_filter() -> tuple[Any, ...]:
    """The SQL half of `song_eligible`, for the sync worker and the async chat read alike."""
    from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION  # noqa: PLC0415
    from app.agents._schemas.song_sections import CURRENT_SECTION_VERSION  # noqa: PLC0415
    from app.models import MusicTrack  # noqa: PLC0415

    return (
        MusicTrack.analysis_status == "ready",
        MusicTrack.published_at.is_not(None),
        MusicTrack.archived_at.is_(None),
        MusicTrack.audio_gcs_path.like("music/%"),
        MusicTrack.ai_labels.is_not(None),
        MusicTrack.label_version == CURRENT_LABEL_VERSION,
        MusicTrack.best_sections.is_not(None),
        MusicTrack.section_version == CURRENT_SECTION_VERSION,
    )


def song_treatment(track: Any, *, gain_db: float = GAIN_DB) -> dict[str, Any]:
    """``track`` as the variant's ``smart_music_treatment``: its rank-1 section (at most
    `MAX_SECTION_DURATION_S`), the Talking levels, and the track's length so the phone bed keeps
    its export margin (`compile_music_bed_track`). Call only for a `song_eligible` track."""
    from app.services.music_sections import current_best_section_for_track  # noqa: PLC0415

    section = current_best_section_for_track(track)
    if section is None:
        raise ValueError("song has no current section")
    return {
        "track_id": str(track.id),
        "src_gcs_path": str(track.audio_gcs_path),
        "section_start_s": round(float(section[0]), 3),
        "section_end_s": round(float(section[1]), 3),
        "gain_db": float(gain_db),
        "speech_duck_db": SPEECH_DUCK_DB,
        "final_lufs": FINAL_LUFS,
        "duration_s": round(_duration(track), 3),
    }


def _labels(track: Any) -> Mapping[str, Any]:
    raw = getattr(track, "ai_labels", None)
    if not isinstance(raw, Mapping):
        return {}
    labels = raw.get("labels") if isinstance(raw.get("labels"), Mapping) else raw
    return labels if isinstance(labels, Mapping) else {}


def candidate_row(track: Any) -> dict[str, Any]:
    """What chat sees of a song it may choose: its id, title and how it sounds (MusicLabels)."""
    labels = _labels(track)
    row: dict[str, Any] = {"id": str(track.id), "title": str(track.title or "")[:80]}
    for key in ("mood", "genre", "energy"):
        value = labels.get(key)
        if isinstance(value, str) and value.strip():
            row[key] = " ".join(value.split())[:60]
    return row


def recipe_plays_music(recipe: Any) -> bool:
    """Does this compiled phone recipe play a song (a ``role="music"`` track with clips)?

    The recipe, not the variant's stored treatment, says what the export plays: a Save with the
    rollout off drops the bed and keeps the treatment."""
    tracks = getattr(recipe, "tracks", None)
    if tracks is None and isinstance(recipe, Mapping):
        tracks = recipe.get("tracks")
    for track in tracks or ():
        role = track.get("role") if isinstance(track, Mapping) else getattr(track, "role", None)
        clips = track.get("clips") if isinstance(track, Mapping) else getattr(track, "clips", None)
        if role == "music" and clips:
            return True
    return False
