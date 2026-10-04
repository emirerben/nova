"""Builders shared by the KRI-374 user-song planner / compiler tests."""

from __future__ import annotations

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.phone_recipe_shared import PhoneSongBed
from app.pipeline.unified_montage import UnifiedClip, UnifiedMontagePlan
from app.schemas.user_song import (
    AlignmentAlternate,
    SongAlignment,
    SongAnalysis,
    SongLine,
    TakeAlignment,
)
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding

SONG_ITEM_ID = "plan-item-1"
SONG_GENERATION = 7
SONG_DURATION_S = 120.0


def take(media_id: str, duration: float = 20.0, **changes) -> UnifiedClip:
    return UnifiedClip(
        media_id=media_id,
        proxy_path=f"users/u/analysis-proxy-{media_id}.mp4",
        generation="1",
        duration_s=duration,
        width=1080,
        height=1920,
        **changes,
    )


def photo(media_id: str = "11111111-1111-4111-8111-111111111111") -> UnifiedClip:
    return UnifiedClip(
        media_id=media_id,
        proxy_path=f"users/u/pool/{media_id}.jpg",
        generation="9",
        duration_s=1.0,
        width=1080,
        height=1920,
        lane="asset",
        kind="image",
    )


def analysis(
    *,
    duration_s: float = SONG_DURATION_S,
    beat_step: float = 0.5,
    line_starts: tuple[float, ...] = tuple(float(i) for i in range(2, 118, 4)),
) -> SongAnalysis:
    beats = []
    value = 0.25
    while value < duration_s:
        beats.append(round(value, 3))
        value += beat_step
    return SongAnalysis(
        generation=SONG_GENERATION,
        status="ready",
        duration_s=duration_s,
        beats_s=beats,
        lines=[SongLine(start_s=s, end_s=s + 3.0, text="la la") for s in line_starts],
    )


def confident(media_id: str, delta_s: float) -> TakeAlignment:
    return TakeAlignment(
        media_id=media_id, status="confident", delta_s=delta_s, confidence=0.95, peak_z=14.0
    )


def ambiguous(media_id: str, delta_s: float, *alternates: float) -> TakeAlignment:
    return TakeAlignment(
        media_id=media_id,
        status="ambiguous",
        delta_s=delta_s,
        confidence=0.4,
        alternates=[AlignmentAlternate(delta_s=a, score=0.3) for a in alternates],
    )


def unmatched(media_id: str) -> TakeAlignment:
    return TakeAlignment(media_id=media_id, status="unmatched")


def alignment(*rows: TakeAlignment) -> SongAlignment:
    return SongAlignment(song_generation=SONG_GENERATION, takes={r.media_id: r for r in rows})


def bindings_for(
    plan: UnifiedMontagePlan, *, original_duration: dict[str, float] | None = None
) -> tuple[tuple[PhoneSourceBinding, ...], tuple[PhoneVisualBinding, ...]]:
    """Phone bindings for every clip/visual a planned montage uses."""
    footage: list[PhoneSourceBinding] = []
    visuals: list[PhoneVisualBinding] = []
    for ref in plan.snapshot.media:
        if ref.lane == "asset":
            visuals.append(
                PhoneVisualBinding(
                    media_id=ref.media_id,
                    gcs_path=ref.gcs_path,
                    generation=ref.generation,
                    sha256="d" * 64,
                    byte_count=4096,
                    kind=ref.kind,
                )
            )
            continue
        footage.append(
            PhoneSourceBinding(
                media_id=ref.media_id,
                proxy_path=ref.gcs_path,
                generation=ref.generation,
                original=OriginalMediaDescriptor(
                    sha256="a" * 64,
                    byte_count=1000,
                    duration_s=(original_duration or {}).get(ref.media_id, ref.duration_s or 5),
                    width=1080,
                    height=1920,
                    has_audio=True,
                ),
            )
        )
    return tuple(footage), tuple(visuals)


def compiled_plan(plan: UnifiedMontagePlan) -> GuidedStoryExecutionPlan:
    """The strict compiler's execution plan for a planned montage (no catalog track)."""
    return GuidedStoryExecutionPlan.model_validate(
        compile_execution_plan(plan.guided_edit(), track=None)
    )


def song_bed(**changes) -> PhoneSongBed:
    return PhoneSongBed(
        **(
            {
                "plan_item_id": SONG_ITEM_ID,
                "generation": str(SONG_GENERATION),
                "fingerprint": RenderFingerprint(sha256="e" * 64, byte_count=3_000_000),
                "duration_s": SONG_DURATION_S,
            }
            | changes
        )
    )
