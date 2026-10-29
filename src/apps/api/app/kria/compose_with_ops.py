"""29 Ekim lane E (KRI-1039): compose an ask with the editor ops instead of refusing it.

One switch for every behavior the lane adds, so `KRIA_COMPOSE_UNMET_WITH_OPS_ENABLED=false`
restores the earlier routing, replies and op shapes exactly. Callers ask here; none read the
setting directly.
"""

from __future__ import annotations

from app.config import settings


def compose_unmet_with_ops() -> bool:
    return bool(settings.kria_compose_unmet_with_ops_enabled)


__all__ = ["compose_unmet_with_ops"]
