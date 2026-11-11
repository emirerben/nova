"""Last-line consent check, shared by draft commitment and render approval.

The conversation planner may suggest copy; durable server-validated answers decide
whether it is usable. This check is independent of model output and question budgets.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.kria.reply_language import say
from app.services.creative_copy_decisions import (
    Events,
    fold_creative_copy,
    media_digest,
    normalize_copy,
)

CREATIVE_COPY_PENDING = "creative_copy_pending"

# The editor ops a tapped suggestion card may run that leave the on-screen title alone. Mirrors
# ``services/suggestions/pinned.PINNED_OP_ALLOW`` (that module imports the suggestion engine, which
# this one must not; a test pins the two equal). The hook card's ``set_title`` is not here: it is
# the one tapped card that writes the title, so it stays under every decision.
TITLE_NEUTRAL_OPS = frozenset(
    {"add_sfx", "set_transition", "set_mix", "set_caption_meta", "remove_clip"}
)
# The wording discussions still being had. Mirrors ``suggestion_chips._OPEN_COPY_STATES`` (a test
# pins the two equal): nothing is prepared, and no card is offered, while one is open.
OPEN_COPY_STATES = frozenset({"authorship", "write_my_own", "generate", "revise"})


def _finish_wording() -> str:
    return say(
        en="Let's finish choosing the wording before preparing the edit.",
        tr="Düzenlemeyi hazırlamadan önce ekrandaki yazıyı netleştirelim.",
    )


def creative_copy_problem(
    events: Events,
    media_snapshot: Mapping[str, Any] | None,
    *,
    strategy: Mapping[str, Any] | None = None,
    pinned_ops: Sequence[str] | None = None,
    editor_text: tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]] | None = None,
) -> str | None:
    """Why this draft may not be committed over the thread's decisions about on-screen titles.

    ``pinned_ops`` names the ops of a draft that is a TAPPED suggestion card's editor ops (the
    caller knows; it is never read from the conversation). Such a draft has no plan to compare the
    copy against, and used to be refused under any decision about a title. When every op leaves the
    title lane alone (``TITLE_NEUTRAL_OPS``) it carries no copy, so a settled decision (approved,
    cancelled, or stale: the footage moved since the answer) is none of its business. An open
    wording discussion still refuses it, as does any op that is not on that list, an empty list, a
    draft with a plan, and every draft that is not a tapped card (``pinned_ops`` None).

    ``editor_text`` is ``(before, after)``: the text rows of an editor-ops draft (no plan) before
    and after its ops compile. A settled decision admits it unless it changes approved words: any
    text, or word sequence read in order, that equals approved words must read the same after.
    Timing, position and style stay free. An open wording discussion still refuses it.
    """
    states = fold_creative_copy(events, dependency_digest=media_digest(media_snapshot))
    if editor_text is not None and strategy is None:
        before, after = editor_text
        for state in states.values():
            if state.status in OPEN_COPY_STATES:
                return _finish_wording()
            if state.approved and _rewrites(normalize_copy(state.approved), before, after):
                return say(
                    en="That would change the wording you approved, so I left it as it is.",
                    tr="Bu, onayladığın yazıyı değiştirirdi; o yüzden olduğu gibi bıraktım.",
                )
        return None
    neutral = (
        strategy is None
        and bool(pinned_ops)
        and all(str(op) in TITLE_NEUTRAL_OPS for op in pinned_ops or ())
    )
    for target, state in states.items():
        if neutral and state.status not in OPEN_COPY_STATES:
            continue
        if not state.approved and not state.cancelled:
            return _finish_wording()
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


def _readings(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Each live text, a word sequence joined in display order, keyed by its lineage."""
    from app.services.kria_editor_ops_text import sequence_source  # noqa: PLC0415

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if isinstance(row, Mapping) and not row.get("removed") and isinstance(row.get("id"), str):
            grouped.setdefault(sequence_source(dict(row)) or row["id"], []).append(row)
    return {
        key: normalize_copy(
            " ".join(
                str(row.get("text") or "")
                for row in sorted(group, key=lambda row: float(row.get("start_s") or 0.0))
            )
        )
        for key, group in grouped.items()
    }


def _rewrites(
    approved: str, before: Sequence[Mapping[str, Any]], after: Sequence[Mapping[str, Any]]
) -> bool:
    readings = _readings(after)
    return any(
        readings.get(key) != approved for key, text in _readings(before).items() if text == approved
    )
