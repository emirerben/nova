"""Shared building blocks for phone-recipe compilers (KRI-114 P1-2/P1-3).

`app.pipeline.phone_guided_plan` was the first phone compiler
(`compile_phone_guided_plan`) and keeps its own inline copies of the
export-safety-margin refit math and its rounding tolerance -- it is left
untouched here (byte-identical behavior, its test suite unmodified) since its
logic is already load-bearing and verified. This module exists so the SECOND
compiler (`app.pipeline.phone_montage_plan`, for the montage/day_vlog/
single_hero archetypes) doesn't have to reinvent that math, and so a THIRD
compiler (voiceover/subtitled/etc., future phases) has somewhere to import it
from instead of copy-pasting again.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.kria.render_assets import RenderFingerprint

# Mirrors `phone_guided_plan._EXPORT_SAFETY_MARGIN_S` -- see that module's
# docstring for the full AVFoundation-boundary rationale. Never fit a refit
# window exactly to the device-measured duration.
EXPORT_SAFETY_MARGIN_S = 0.05

# Mirrors `phone_guided_plan._TIMING_ROUNDING_TOLERANCE_S` -- two
# independently millisecond-rounded timing quantities can legitimately differ
# by a couple of milliseconds without that being a real timing-program
# mismatch.
TIMING_ROUNDING_TOLERANCE_S = 0.005


def refit_source_window(
    source_start: float, source_duration: float, original_duration_s: float
) -> tuple[float, float]:
    """Refit a proxy-measured ``[source_start, source_start + source_duration)``
    window into what the device's own original file actually measures.

    The proxy is measured by server-side ffprobe; the original by the
    client's on-device measurement of conceptually the same file -- the two
    routinely disagree by a small amount. Prefer to keep the full requested
    duration by shifting the start rather than truncating it; when that still
    doesn't fit, truncate to whatever's available minus the export safety
    margin. Raises ``ValueError`` when essentially nothing usable remains.
    """
    available = max(0.0, original_duration_s - EXPORT_SAFETY_MARGIN_S)
    fitted_duration = min(source_duration, available)
    if fitted_duration < 0.1:
        raise ValueError("insufficient source duration remains after the on-device refit")
    start = min(source_start, max(0.0, available - fitted_duration))
    return start, round(fitted_duration, 6)


class PhoneMusicBed(BaseModel):
    """A resolved, immutable music-catalog receipt ready to compile into a
    phone recipe's audio track.

    Built by `app.tasks.generative_build._resolve_phone_music_bed`, which
    bridges the sync Celery worker to the (async-shaped) catalog lookup
    `app.services.render_library.catalog_path` normally uses -- there is no
    running event loop in a Celery worker, so it re-validates the same
    publish/ready/path-prefix contract directly against a sync DB session
    instead of calling that async function. `inspect_library_asset` (already
    sync) then pins the exact generation + fingerprint. Consumed by
    `compile_phone_montage_plan`, which never touches the database or GCS
    itself -- it only reads this already-verified value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str = Field(min_length=1, max_length=160)
    generation: str = Field(min_length=1, max_length=160)
    fingerprint: RenderFingerprint
    duration_s: float | None = Field(default=None, gt=0, le=1800)
    start_s: float = Field(default=0.0, ge=0)
    volume: float = Field(default=1.0, ge=0, le=2)


class PhoneNarrationBed(BaseModel):
    """A resolved, immutable voiceover receipt ready to compile into a phone
    recipe's narration audio track (KRI-132).

    Built by `app.tasks.generative_build._resolve_phone_voiceover_bed`, which
    re-reads the owning `PlanItem`'s current `voiceover_gcs_path`/
    `voiceover_generation` in a sync session, then calls
    `app.services.phone_voiceover.inspect_voiceover_asset` (mirrors
    `inspect_library_asset`'s pin-then-hash pattern) to pin the exact
    generation + fingerprint that `app.routes.device_render
    .download_device_asset` will independently recompute when the device
    fetches the asset -- they must agree exactly. Unlike `PhoneMusicBed`'s
    shared catalog, this is private creator media addressed by plan item, not
    catalog id -- see `app.kria.render_assets.VoiceoverRenderAsset`.

    Consumed by `compile_phone_montage_plan`, which never touches the
    database or GCS itself -- it only reads this already-verified value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_item_id: str = Field(min_length=1, max_length=160)
    generation: str = Field(min_length=1, max_length=160)
    fingerprint: RenderFingerprint
    # The server-verified duration for the captured generation (`PlanItem
    # .voiceover_duration_s`, probed once at registration -- the generation
    # is immutable so that probe cannot drift from these exact bytes).
    duration_s: float = Field(gt=0, le=1800)
