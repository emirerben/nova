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


def planner_catalog_song_blocked() -> bool:
    """True when the Kria planner must not freeze a catalog song section into a draft.

    Every draft the Kria planner writes renders as a creator job (the job factory stamps
    ``creator_generation_id`` on any job with a creator strategy), so no assembly plan is
    needed. The draft gets the footage fallback instead (``{"status": "fallback_footage"}``),
    which the render also reads as "no catalog song"; a creator-uploaded song never reaches the
    freeze and is unchanged.
    """
    from app.config import settings  # noqa: PLC0415

    return bool(settings.creator_jobs_no_catalog_song_freeze_enabled)


def withheld_song_notice(section: Mapping[str, Any] | None) -> str | None:
    """What the reply says when a creator asked for a kind of song the planner withheld.

    Only a genre ask (`music_genre.asked_genres`, recorded on the footage fallback the planner
    froze) gets a line: it replaces the suggested catalog track. With no genre asked there was
    no line before either.
    """
    from app.config import settings  # noqa: PLC0415
    from app.kria.reply_language import say  # noqa: PLC0415
    from app.services.music_genre import ASKED_GENRE, ASKED_GENRES  # noqa: PLC0415

    if not isinstance(section, Mapping):
        return None
    listed = section.get(ASKED_GENRES)
    names = [str(name) for name in listed if name] if isinstance(listed, list) else []
    genre = str(section.get(ASKED_GENRE) or "").strip()
    names = names or ([genre] if genre else [])
    if not names:
        return None
    en = (
        "I can't add songs from the music library, so this video won't have the "
        f"{' or '.join(names)} song you asked for."
    )
    tr = (
        "Müzik kütüphanesinden şarkı ekleyemiyorum, bu yüzden bu videoda istediğin "
        f"{' ya da '.join(names)} şarkısı olmayacak."
    )
    if settings.user_song_montage_enabled:
        en += " Upload your own song if you want one."
        tr += " İstersen kendi şarkını yükleyebilirsin."
    return say(en=en, tr=tr)
