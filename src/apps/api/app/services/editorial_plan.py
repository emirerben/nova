"""Shared approval/worker rules for a grounded editorial sequence."""

from collections.abc import Sequence

from app.schemas.editorial_plan import EditorialPlan
from app.services.clip_order_sequence import SequenceRow, apply_sequence


def editorial_order(
    plan: object,
    available_ids: Sequence[str],
    *,
    required_order: Sequence[str] = (),
    sequence: Sequence[SequenceRow] = (),
) -> list[str]:
    typed = plan if isinstance(plan, EditorialPlan) else EditorialPlan.model_validate(plan)
    ids = [media_id for beat in typed.beats for media_id in beat.media_ids]
    if set(ids) != set(available_ids):
        raise ValueError("editorial plan must cover exactly the selected footage")
    if required_order and ids != list(required_order):
        raise ValueError("editorial plan conflicts with the creator's clip order")
    if sequence and (
        any(status != "resolved" for _, _, _, status in sequence)
        or apply_sequence(ids, sequence) != ids
    ):
        raise ValueError("editorial plan conflicts with the creator's sequence")
    return ids


def editorial_summary(plan: object, duration_s: float | None = None) -> str:
    typed = plan if isinstance(plan, EditorialPlan) else EditorialPlan.model_validate(plan)
    duration = f"{duration_s:.1f}s · " if duration_s else ""
    sequence = " → ".join(beat.purpose for beat in typed.beats)
    text = (
        "With footage-grounded explanatory text."
        if any(beat.text_goal for beat in typed.beats)
        else ""
    )
    return f"{duration}{typed.viewer_goal}. {sequence}. {text}".strip()[:1000]
