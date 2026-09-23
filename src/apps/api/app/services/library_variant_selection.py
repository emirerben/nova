"""Owner-scoped creation-thread variant selection for library projections.

The library, poster refresh endpoint, and repair worker must all repair the
same cut the creator selected in their current thread.  Keep state parsing here
so malformed JSON never leaks into a rank-based mutation.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Any


def selected_variant_id(state: Any) -> str | None:
    """Return a nonblank selected variant id from an untrusted thread state."""
    if not isinstance(state, dict):
        return None
    selected = state.get("selected_variant_id")
    return selected.strip() if isinstance(selected, str) and selected.strip() else None


def preferred_variants_from_rows(
    rows: Iterable[tuple[uuid.UUID, Any]],
) -> dict[uuid.UUID, str]:
    """Map job to selection when rows are ordered oldest-to-newest.

    A malformed or blank newer state deliberately clears a prior choice; this
    mirrors fetching just the newest thread in the single-job path.
    """
    selected: dict[uuid.UUID, str] = {}
    for job_id, state in rows:
        variant_id = selected_variant_id(state)
        if variant_id is None:
            selected.pop(job_id, None)
        else:
            selected[job_id] = variant_id
    return selected
