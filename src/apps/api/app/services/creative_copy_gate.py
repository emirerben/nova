"""Last-line consent check, shared by draft commitment and render approval.

The conversation planner may suggest copy; durable server-validated answers decide
whether it is usable. This check is independent of model output and question budgets.
"""

from collections.abc import Mapping
from typing import Any

from app.services.creative_copy_decisions import Events, fold_creative_copy, media_digest

CREATIVE_COPY_PENDING = "creative_copy_pending"


def creative_copy_problem(
    events: Events,
    media_snapshot: Mapping[str, Any] | None,
    *,
    strategy: Mapping[str, Any] | None = None,
) -> str | None:
    states = fold_creative_copy(events, dependency_digest=media_digest(media_snapshot))
    for target, state in states.items():
        if not state.approved and not state.cancelled:
            return "Let's finish choosing the wording before preparing the edit."
        if strategy is None:
            return "Let's review the edit plan with your saved wording before changing this video."
        if state.approved is not None and strategy.get(target) != state.approved:
            return "The proposed text differs from the wording you approved. Let's review it again."
        if state.cancelled and (
            strategy.get(target) or target not in (strategy.get("omitted_copy_targets") or [])
        ):
            return "You asked to leave that text out. Let's update the plan before rendering."
    return None
