"""When an editor draft head is "current". Read-only: nothing here writes a row.

A head is current only while its stamps name the variant's generation (or the generation a phone
publish was made from). The server never moves a head when the generation moves: a head on an
older generation is not current and readers ignore it, as before this module existed.

The readers that decide currency each keep the rule ``main`` gave them, built from the same parts
so they cannot drift: the chat planner reads the payload stamp, the Draft PUT and the saved draft
read the column. ``is_head_current`` is all of it at once; it is what the draft snapshot reports
as ``head_current``.
"""

from __future__ import annotations

from typing import Any


def variant_baseline(variant: dict[str, Any]) -> str:
    from app.routes.generative_jobs import variant_render_baseline  # noqa: PLC0415

    return variant_render_baseline(variant)


def current_bases(job: Any, variant: dict[str, Any]) -> frozenset[str]:  # noqa: ANN401
    """Generations a draft may name and still be on the variant's current document.

    The render baseline, plus the generation a phone publish was made from: publishing
    replaces ``render_generation_id`` with the upload attempt id while the Save that
    produced it pinned the generation the phone was asked to render.
    """
    from app.services.device_render import published_source_generation  # noqa: PLC0415

    published = published_source_generation(job, variant) if job is not None else None
    return frozenset(base for base in (variant_baseline(variant), published) if base)


def payload_base(head: Any) -> str | None:  # noqa: ANN401
    """The generation stamped inside the head's editor payload; None when it has none."""
    snapshot = head.snapshot_json
    payload = snapshot.get("editor_payload") if isinstance(snapshot, dict) else None
    if not isinstance(payload, dict):
        return None
    return str(payload.get("base_generation") or "")


def column_current(job: Any, variant: dict[str, Any], head: Any) -> bool:  # noqa: ANN401
    """The head's ``base_generation_id`` column names the variant's generation."""
    column = head.base_generation_id
    if column == (variant_baseline(variant) or None):
        return True
    return column is not None and column in current_bases(job, variant)


def payload_current(job: Any, variant: dict[str, Any], head: Any) -> bool:  # noqa: ANN401
    """The head's editor payload was built on the variant's generation."""
    base = payload_base(head) or ""
    return base == variant_baseline(variant) or (base != "" and base in current_bases(job, variant))


def is_head_current(job: Any, variant: dict[str, Any], head: Any) -> bool:  # noqa: ANN401
    """Same job, and every stamp the head has is current."""
    if head.base_job_id != job.id or not column_current(job, variant, head):
        return False
    return payload_base(head) is None or payload_current(job, variant, head)


def is_unstamped_main_head(job: Any, variant: dict[str, Any], head: Any) -> bool:  # noqa: ANN401
    """A head ``main`` wrote for a variant that has only ``render_finished_at``: column None.

    ``main`` stamped such rows from ``render_generation_id`` alone, so the column is None. Its
    Draft PUT and saved-draft attach compared that column with the variant's absent
    ``render_generation_id`` and accepted the row; they still do.
    """
    return (
        head.base_job_id == job.id
        and head.base_generation_id is None
        and not variant.get("render_generation_id")
    )


def column_accepted(job: Any, variant: dict[str, Any], head: Any) -> bool:  # noqa: ANN401
    """The rule the Draft PUT and the saved-draft attach apply: same job, column acceptable."""
    return head.base_job_id == job.id and (
        column_current(job, variant, head) or is_unstamped_main_head(job, variant, head)
    )
