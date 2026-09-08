"""Canonical row-lock acquisition order for the creation graph.

Every ``SELECT ... FOR UPDATE`` on the creation graph must take its row locks
in the order declared by :data:`CANONICAL_LOCK_ORDER`.  Two transactions that
lock the same rows in opposite order deadlock: PostgreSQL detects the cycle and
aborts one of them with ``40P01``, which surfaced in production as a 500 on
``POST /creation-threads/{id}/media``.

The order below is the one already documented in ``app/tasks/kria_runtime.py``
("Global mutation order: Plan -> PlanItem -> Job -> Session -> Turn -> Draft ->
Approval -> Execution -> Thread"), extended at the front with the ownership
rows (``User`` -> ``Persona``) and with ``PlanItemAsset`` between ``PlanItem``
and ``Job``.  It follows ownership: a parent row is locked before its children,
and the ``CreationThread`` projection is locked last because it is derived
state that trails the rows it points at.

Rules:

* Acquire strictly in this order within one transaction.  Re-locking a row the
  transaction already holds is a no-op and is always allowed.
* Locking a *subset* is fine -- skipping ranks never inverts the order.
* A ``commit()``/``rollback()`` releases everything, so ordering restarts.

``tests/routes/test_lock_order.py`` statically enforces this over the route,
task, and service modules; use :func:`acquire_locked_rows` when a code path
needs several of these rows so the order cannot be typed wrong by hand.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    Persona,
    PlanItem,
    PlanItemAsset,
    User,
)

#: Locks must be taken left-to-right.  Keep this in sync with the module
#: docstring and with the guard test's ``CANONICAL_ORDER_NAMES``.
CANONICAL_LOCK_ORDER: tuple[type, ...] = (
    User,
    Persona,
    ContentPlan,
    PlanItem,
    PlanItemAsset,
    Job,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreationThread,
)

_RANK: dict[type, int] = {model: index for index, model in enumerate(CANONICAL_LOCK_ORDER)}

T = TypeVar("T")


def lock_rank(model: type) -> int:
    """Return the canonical rank of ``model``.

    Raises ``KeyError`` for models outside the creation graph; those carry no
    ordering obligation and are not part of the guard.
    """

    return _RANK[model]


def assert_canonical_order(models: list[type]) -> None:
    """Raise ``ValueError`` if ``models`` is not a canonical acquisition order."""

    seen: set[type] = set()
    highest = -1
    for model in models:
        if model in seen:
            # Re-locking a row this transaction already holds cannot deadlock.
            continue
        rank = lock_rank(model)
        if rank < highest:
            raise ValueError(
                f"{model.__name__} must be locked before "
                f"{CANONICAL_LOCK_ORDER[highest].__name__} "
                "(see app/db_locks.CANONICAL_LOCK_ORDER)"
            )
        highest = rank
        seen.add(model)


def _ordered(targets: Mapping[type, Any]) -> list[tuple[type, Any]]:
    requested = [(model, ident) for model, ident in targets.items() if ident is not None]
    requested.sort(key=lambda pair: lock_rank(pair[0]))
    return requested


async def acquire_locked_rows(
    db: AsyncSession,
    targets: Mapping[type, uuid.UUID | str | None],
    *,
    populate_existing: bool = False,
) -> dict[type, Any]:
    """``SELECT ... FOR UPDATE`` each requested row in canonical order.

    ``targets`` maps a model class to the primary key to lock; ``None`` skips
    that model.  Returns a ``{model: row_or_None}`` mapping.  The sort is what
    makes the call site order-proof -- callers may pass the models in any
    order.
    """

    # Only pass populate_existing when asked: it keeps the emitted db.get()
    # signature identical to the hand-written call it replaced.
    extra: dict[str, Any] = {"populate_existing": True} if populate_existing else {}
    rows: dict[type, Any] = {}
    for model, ident in _ordered(targets):
        rows[model] = await db.get(model, ident, with_for_update=True, **extra)
    return rows


def acquire_locked_rows_sync(
    db: Session,
    targets: Mapping[type, uuid.UUID | str | None],
    *,
    populate_existing: bool = False,
) -> dict[type, Any]:
    """Synchronous :func:`acquire_locked_rows`, for Celery task sessions."""

    extra: dict[str, Any] = {"populate_existing": True} if populate_existing else {}
    rows: dict[type, Any] = {}
    for model, ident in _ordered(targets):
        rows[model] = db.get(model, ident, with_for_update=True, **extra)
    return rows
