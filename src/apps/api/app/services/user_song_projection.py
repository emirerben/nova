"""Editor-facing projection of a creator's own song on a phone montage (KRI-374).

The device recipe already carries the song track, so playback needs nothing from the
server. The editor's Sounds tab, however, has to know a creator song exists (and what
it is called) so it shows the song instead of an empty "Add music" form. This module
builds that one additive, optional ``user_song`` variant field.

It is deliberately separate from ``song_reference_variant_fields`` (which feeds the
receipt-equality check and the catalog-music gating): the creator song stays
unreachable through the catalog-music edit controls.
"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.user_song import UserSongPlan

# Only a plausible audio extension is stripped, so "Song v1.2" keeps its ".2".
_EXTENSION = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


class UserSongOut(BaseModel):
    """Wire shape of ``variants[].user_song``."""

    title: str | None = None
    mode: Literal["background", "lipsync"]
    duration_s: float
    window_start_s: float
    window_end_s: float
    volume: float = 1.0
    # KRI-561: each pinned lip-sync take's song offset (media_id -> delta_s), so the editor can
    # keep the song start on the footage when the creator trims the video. Omitted when empty
    # (background songs, older plans), so those responses stay byte-identical.
    takes: dict[str, float] = Field(default_factory=dict, exclude_if=lambda value: not value)


def user_song_title(filename: str | None) -> str | None:
    """The uploaded file name without its extension, trimmed; None when blank."""
    if not isinstance(filename, str):
        return None
    name = os.path.basename(filename.replace("\\", "/")).strip()
    if _EXTENSION.match(name):  # a bare ".m4a" has no title
        return None
    stem, ext = os.path.splitext(name)
    if ext and _EXTENSION.match(ext) and any(c.isalpha() for c in ext):
        name = stem.strip()
    return name or None


def _current_plan(job: Any, raw_variant: dict[str, Any]) -> dict[str, Any] | None:
    """The plan the next render uses: saved editor plan, else the pinned plan."""
    from app.services.phone_editor import (  # noqa: PLC0415
        PHONE_EDITOR_PLAN_FIELD,
        PHONE_EDITOR_SAVED_PLAN_FIELD,
    )

    for field in (PHONE_EDITOR_SAVED_PLAN_FIELD, PHONE_EDITOR_PLAN_FIELD):
        plan = raw_variant.get(field)
        if isinstance(plan, dict):
            return plan
    if raw_variant.get("resolved_archetype") == "guided_story":
        plan = (getattr(job, "assembly_plan", None) or {}).get("guided_story_execution_plan")
        if isinstance(plan, dict):
            return plan
    return None


def user_song_for_variant(
    job: Any, raw_variant: dict[str, Any], *, song_filename: str | None
) -> dict[str, Any] | None:
    """``user_song`` for one variant, or None when its plan has no creator song."""
    plan = _current_plan(job, raw_variant)
    raw_song = plan.get("user_song") if plan else None
    if not isinstance(raw_song, dict):
        return None
    try:
        song = UserSongPlan.model_validate(raw_song)
    except Exception:  # noqa: BLE001 -- a malformed pin must never fail a status read
        return None
    return UserSongOut(
        title=user_song_title(song_filename),
        mode=song.mode,
        duration_s=song.duration_s,
        window_start_s=song.window_start_s,
        window_end_s=song.window_end_s,
        volume=song.volume,
        takes={media_id: take.delta_s for media_id, take in song.takes.items()}
        if song.mode == "lipsync"
        else {},
    ).model_dump(mode="json")


def job_has_user_song(job: Any) -> bool:
    return any(
        isinstance(v, dict) and user_song_for_variant(job, v, song_filename=None) is not None
        for v in ((getattr(job, "assembly_plan", None) or {}).get("variants") or [])
    )


def attach_user_song(
    variants: list[dict[str, Any]], job: Any, *, song_filename: str | None
) -> None:
    """Add ``user_song`` in place to response variants whose plan has a creator song.

    Variants without one are left untouched, so their responses stay byte-identical.
    """
    raw_by_id = {
        str(v.get("variant_id")): v
        for v in ((getattr(job, "assembly_plan", None) or {}).get("variants") or [])
        if isinstance(v, dict)
    }
    for variant in variants:
        raw = raw_by_id.get(str(variant.get("variant_id")))
        if raw is None:
            continue
        song = user_song_for_variant(job, raw, song_filename=song_filename)
        if song is not None:
            variant["user_song"] = song
