"""Lip-sync montage replay (KRI-1097 wave 2, lane L4): `scripts/kria/score_route.py` contract.

Calls the route's render entry, `plan_lipsync_montage`, once with the fixture's own plan and
asks as they are. The takes come from the fixture's clips; the song and where each take sits
on it come from the fixture's ``replay_inputs`` (reconstructed, see the fixture README).
"""

from __future__ import annotations

from app.pipeline.lipsync_montage import plan_lipsync_montage
from app.pipeline.unified_montage import UnifiedClip
from app.schemas.user_song import SongAlignment, SongAnalysis, SongLine, TakeAlignment


def _times(first: float, step: float, end: float) -> list[float]:
    out = []
    value = first
    while value < end:
        out.append(round(value, 3))
        value += step
    return out


def recipe(fx):
    inputs = fx["inputs"]
    song = inputs["song"]
    line_s = song["line_length_s"]
    line_starts = _times(song["first_line_s"], song["line_step_s"], song["duration_s"] - line_s)
    analysis = SongAnalysis(
        generation=song["generation"],
        status="ready",
        duration_s=song["duration_s"],
        beats_s=_times(song["first_beat_s"], song["beat_step_s"], song["duration_s"]),
        lines=[
            SongLine(start_s=start, end_s=start + line_s, text=song["line_text"])
            for start in line_starts
        ],
    )
    alignment = SongAlignment(
        song_generation=song["generation"],
        takes={row["media_id"]: TakeAlignment(**row) for row in inputs["takes"]},
    )
    clips = [
        UnifiedClip(
            media_id=clip["id"],
            proxy_path=f"users/synthetic/threads/synthetic/analysis-proxy-ios-{clip['id']}.mp4",
            generation="1",
            duration_s=clip["duration_s"],
            width=(clip.get("analysis") or {}).get("width"),
            height=(clip.get("analysis") or {}).get("height"),
            analysis=clip.get("analysis") or {},
        )
        for clip in fx["clips"]
    ]
    return plan_lipsync_montage(
        clips,
        alignment,
        analysis,
        None,
        fx["strategy"],
        None,
        plan_item_id=inputs["plan_item_id"],
        requirements=fx["requirements"],
    )
