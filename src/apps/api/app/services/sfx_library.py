"""The seeded creator sound library (KRI-173) by slug, for sounds the engine itself places
(KRI-1613: an intro scene's sound).

`scripts/seed_sfx_library.py` uploads every `scripts/sfx_library/catalog.py` effect as a
`SoundEffect` row with a server-made id, then tags it: ``provenance`` starts with
``<LIBRARY_VERSION>:<slug> `` and ``source_filename`` is ``<slug>.m4a``. Nothing else maps a slug
to a row, so the lookup reads that same ownership mark. The scripts folder is not in the prod
image, hence the copy of the version tag (guard: tests/services/test_sfx_library.py).
"""

from __future__ import annotations

from typing import Any

LIBRARY_VERSION = "kria-sfx-library-v1"


def library_effect_id(session: Any, slug: str) -> str | None:
    """The id of the published, ready library row the seed script owns for ``slug``, newest
    first, or ``None`` when there is none. The tag keeps its trailing space, as the seed's own
    match does, so "sparkle" never finds "sparkle-short"."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.models import SoundEffect  # noqa: PLC0415

    return session.execute(
        select(SoundEffect.id)
        .where(
            SoundEffect.provenance.startswith(f"{LIBRARY_VERSION}:{slug} ", autoescape=True),
            SoundEffect.source_filename == f"{slug}.m4a",
            SoundEffect.status == "ready",
            SoundEffect.published_at.is_not(None),
            SoundEffect.archived_at.is_(None),
        )
        .order_by(SoundEffect.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
