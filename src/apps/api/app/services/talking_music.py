"""The creator's library song under a phone Talking edit (KRI-1562).

One owner for what every path that gives a Talking edit a song agrees on: which library tracks
may play (`song_eligible`), the treatment a chosen track becomes (`song_treatment`, the variant's
``smart_music_treatment``), the rows chat may choose from (`candidate_row`), whether a compiled
recipe really plays a song (`recipe_plays_music`), and why a requested song is missing (the
worker's `RECEIPT_FIELD`). Creation (the Talking worker), chat (`swap_music`) and the editor
Save all read these, so a song one path picks is one the others can pin and play.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

# The bed sits well under the voice and drops further while anyone speaks (`MusicBedTreatment`).
GAIN_DB = -18.0
SPEECH_DUCK_DB = -12.0
FINAL_LUFS = -14.0
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
    raw = getattr(track, "ai_labels", None) or {}
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
