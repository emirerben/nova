"""The song rule for Kria creator jobs: only a creator-uploaded song ever plays.

The render-time auto-matcher (`generative_build._match_best_track`) ranks the whole analyzed
catalog. That is the generative product's behaviour, and it is wrong for a Kria creator job: a
catalog song there is one the creator never chose and we may not have the right to play. This is
the one predicate the matcher's callers ask before they run it.

Report-only on the product side: the answer is "do not match", never an error. A job that
matches nothing renders without a catalog song, exactly like an empty library.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Stamps only the Kria creator flow writes onto a job's assembly plan: the immutable creator
# generation, the plan-authority stamp, the device-only intent, and the phone source binding.
_CREATOR_JOB_FIELDS: tuple[str, ...] = (
    "creator_generation_id",
    "creator_plan_authority_version",
    "native_device_only",
    "_phone_sources_v1",
)


def is_creator_job(assembly_plan: Mapping[str, Any] | None) -> bool:
    """True when the job was minted by the Kria creator flow (not a web generative edit)."""
    plan = assembly_plan or {}
    return any(plan.get(field) is not None for field in _CREATOR_JOB_FIELDS)


def catalog_song_match_blocked(assembly_plan: Mapping[str, Any] | None) -> bool:
    """True when the auto-matcher must not run for this job."""
    from app.config import settings  # noqa: PLC0415

    return bool(
        settings.creator_jobs_never_auto_match_catalog_song_enabled
        and is_creator_job(assembly_plan)
    )


def phone_catalog_song_match_blocked() -> bool:
    """Every phone-writer job is a creator job: no assembly plan lookup needed."""
    from app.config import settings  # noqa: PLC0415

    return bool(settings.creator_jobs_never_auto_match_catalog_song_enabled)
