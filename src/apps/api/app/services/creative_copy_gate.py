"""Last-line consent check, shared by draft commitment and render approval.

The conversation planner may suggest copy; durable server-validated answers decide
whether it is usable. This check is independent of model output and question budgets.
"""

from collections.abc import Mapping
from typing import Any

from app.kria.reply_language import say
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
            return say(
                en="Let's finish choosing the wording before preparing the edit.",
                tr="Düzenlemeyi hazırlamadan önce ekrandaki yazıyı netleştirelim.",
            )
        if strategy is None:
            return say(
                en="Let's review the edit plan with your saved wording before changing this video.",
                tr=(
                    "Bu videoyu değiştirmeden önce düzenleme planını kaydettiğin yazıyla "
                    "birlikte gözden geçirelim."
                ),
            )
        if state.approved is not None and strategy.get(target) != state.approved:
            return say(
                en=(
                    "The proposed text differs from the wording you approved. "
                    "Let's review it again."
                ),
                tr="Önerilen yazı onayladığın yazıdan farklı. Tekrar gözden geçirelim.",
            )
        if state.cancelled and (
            strategy.get(target) or target not in (strategy.get("omitted_copy_targets") or [])
        ):
            return say(
                en="You asked to leave that text out. Let's update the plan before rendering.",
                tr="O yazının olmamasını istedin. Videoyu oluşturmadan önce planı güncelleyelim.",
            )
    return None
