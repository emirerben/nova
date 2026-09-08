"""Static guard against row-lock ordering inversions.

Two transactions that take the same row locks in opposite order deadlock.  In
production this surfaced as ``asyncpg.exceptions.DeadlockDetectedError`` and a
500 on ``POST /creation-threads/{id}/media``: the media-attach guard locked
``PlanItem`` then ``Job`` while the retry-partial path locked ``Job`` then
``PlanItem``.

This test walks the AST of every module under ``app/`` and reconstructs, per
function, the order in which ``SELECT ... FOR UPDATE`` row locks are taken --
following calls into module-local helpers, because the inversion above spanned
two functions.  Any sequence that contradicts
:data:`app.db_locks.CANONICAL_LOCK_ORDER` fails, unless it is listed in
:data:`KNOWN_INVERSIONS`.

``KNOWN_INVERSIONS`` is a debt list, not an escape hatch: it is a closed set of
call sites that predate the canonical order and each need their own
restructuring.  Adding to it should be a deliberate, reviewed decision --
the point of the test is that a *new* inversion cannot land silently.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path

from app.db_locks import CANONICAL_LOCK_ORDER

APP_ROOT = Path(__file__).resolve().parents[2] / "app"

RANK = {model.__name__: index for index, model in enumerate(CANONICAL_LOCK_ORDER)}

# Pre-existing inversions, each keyed by "<module>::<function>".  Every entry
# needs its own fix; none may be added without a matching rationale.
KNOWN_INVERSIONS: frozenset[str] = frozenset(
    {
        # Locks the CreationThread root first because the whole delete cascade
        # is discovered *through* that row (content_plan_id, active_plan_item_id).
        # Fixing it means re-reading the thread unlocked, locking children in
        # canonical order, then re-verifying under the thread lock.
        "routes/creation_threads.py::delete_thread",
        # Overlay asset identities are only known after the agent's commands are
        # parsed, which happens under the Job lock.
        "routes/creator_agent.py::_resolve_creator_overlay_asset",
        # Locks Job before ContentPlan/PlanItem in the render orchestrator's
        # ownership fence.
        "tasks/generative_build.py::_lock_owned_entry_job",
        # Locks the CreationThread projection before the plan/item/job graph.
        "tasks/edit_proposal_build.py::_bind_creator_job_after_auto_design",
        # Locks ContentPlan before Persona; app/routes/creation_threads.py::_project
        # takes the opposite order.
        "routes/me.py::_provision_editor_plan",
        "services/speech_cleanup_preflight.py::prepare_snapshot_mismatch_reanalysis",
        # Locks Job before the PlanItem it belongs to.
        "routes/creator_workspace.py::decide_relevance_proposal",
    }
)


def _locked_model(node: ast.AST) -> str | None:
    """Return the model name locked by ``node``, if it is a FOR UPDATE read."""

    if not isinstance(node, ast.Call):
        return None
    func = node.func
    # db.get(Model, ident, with_for_update=True)
    if isinstance(func, ast.Attribute) and func.attr == "get":
        if any(kw.arg == "with_for_update" for kw in node.keywords) and node.args:
            first = node.args[0]
            return first.id if isinstance(first, ast.Name) else None
        return None
    # select(Model)....with_for_update()
    if isinstance(func, ast.Attribute) and func.attr == "with_for_update":
        cursor: ast.AST | None = func.value
        while isinstance(cursor, ast.Call):
            inner = cursor.func
            if isinstance(inner, ast.Name) and inner.id == "select":
                if not cursor.args:
                    return None
                first = cursor.args[0]
                if isinstance(first, ast.Name):
                    return first.id
                if isinstance(first, ast.Attribute):
                    return first.attr
                return None
            cursor = inner.value if isinstance(inner, ast.Attribute) else None
        return None
    return None


def _is_transaction_boundary(node: ast.AST) -> bool:
    """commit()/rollback() release every lock, so ordering restarts after one."""

    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"commit", "rollback"}
    )


#: The canonical-order helpers in ``app/db_locks.py``.  They sort internally, so
#: a call is equivalent to locking every requested model in canonical order --
#: but the guard still has to *see* those locks, or a later direct lock in the
#: same function would be compared against an empty history.
_ORDERING_HELPERS = frozenset({"acquire_locked_rows", "acquire_locked_rows_sync"})


def _helper_locked_models(node: ast.Call) -> list[str] | None:
    """Return the models locked by an ``acquire_locked_rows`` call, in order."""

    func = node.func
    name = (
        func.id
        if isinstance(func, ast.Name)
        else func.attr
        if isinstance(func, ast.Attribute)
        else None
    )
    if name not in _ORDERING_HELPERS:
        return None
    mapping = next(
        (kw.value for kw in node.keywords if kw.arg == "targets"),
        node.args[1] if len(node.args) > 1 else None,
    )
    if not isinstance(mapping, ast.Dict):
        # Not a literal -- the helper still orders correctly at runtime, but the
        # models cannot be read statically.
        return []
    models = [key.id for key in mapping.keys if isinstance(key, ast.Name)]
    return sorted((m for m in models if m in RANK), key=lambda m: RANK[m])


class _EventCollector(ast.NodeVisitor):
    """Record locks, transaction boundaries, and local calls in evaluation order."""

    def __init__(self, local_names: set[str]) -> None:
        self._local = local_names
        self.events: list[tuple[str, str | None, int]] = []

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # Arguments are evaluated before the call itself.
        for child in ast.iter_child_nodes(node):
            self.visit(child)
        model = _locked_model(node)
        if model is not None:
            self.events.append(("lock", model, node.lineno))
            return
        helper_models = _helper_locked_models(node)
        if helper_models is not None:
            for helper_model in helper_models:
                self.events.append(("lock", helper_model, node.lineno))
            return
        if _is_transaction_boundary(node):
            self.events.append(("boundary", None, node.lineno))
            return
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name is not None and name in self._local:
            self.events.append(("call", name, node.lineno))


@cache
def _collect(path: Path) -> dict[str, tuple[int, list[tuple[str, str | None, int]]]]:
    tree = ast.parse(path.read_text(), filename=str(path))
    functions: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.setdefault(node.name, node)
    names = set(functions)
    collected = {}
    for name, node in functions.items():
        collector = _EventCollector(names)
        for statement in node.body:
            collector.visit(statement)
        collected[name] = (node.lineno, collector.events)
    return collected


def _flatten(
    name: str,
    collected: dict[str, tuple[int, list[tuple[str, str | None, int]]]],
    stack: tuple[str, ...] = (),
) -> list[tuple[str, str | None, int, str]]:
    """Inline module-local calls so cross-function orders are visible."""

    if name in stack:  # recursion guard
        return []
    flat: list[tuple[str, str | None, int, str]] = []
    for kind, payload, lineno in collected.get(name, (0, []))[1]:
        if kind == "call":
            assert payload is not None
            flat.extend(_flatten(payload, collected, stack + (name,)))
        else:
            flat.append((kind, payload, lineno, name))
    return flat


def _lock_windows(
    name: str, collected: dict[str, tuple[int, list[tuple[str, str | None, int]]]]
) -> list[list[tuple[str, int, str]]]:
    """Split the lock stream into the windows a single transaction can hold."""

    windows: list[list[tuple[str, int, str]]] = []
    current: list[tuple[str, int, str]] = []
    for kind, payload, lineno, owner in _flatten(name, collected):
        if kind == "boundary":
            if current:
                windows.append(current)
                current = []
        elif payload in RANK:
            current.append((payload, lineno, owner))
    if current:
        windows.append(current)
    return windows


def _violations() -> list[tuple[frozenset[str], str]]:
    """Return ``(blamed_functions, message)`` for every out-of-order window.

    ``blamed_functions`` holds the call sites that could own the fix: the
    function being analysed, the one holding the earlier lock, and the one
    taking the late lock.  Listing any of them in :data:`KNOWN_INVERSIONS`
    silences the finding, because for a cross-function inversion either side
    is a legitimate place to correct it.
    """

    found: list[tuple[frozenset[str], str]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        collected = _collect(path)
        relative = path.relative_to(APP_ROOT).as_posix()
        for name in collected:
            for window in _lock_windows(name, collected):
                highest = -1
                previous = ""
                previous_owner = name
                held: set[str] = set()
                for model, lineno, owner in window:
                    if model in held:
                        # Re-locking a row this transaction already holds is a
                        # no-op in PostgreSQL and cannot deadlock.
                        continue
                    held.add(model)
                    rank = RANK[model]
                    if rank < highest:
                        blame = frozenset(
                            f"{relative}::{fn}" for fn in (name, owner, previous_owner)
                        )
                        found.append(
                            (
                                blame,
                                f"{relative}::{owner}:{lineno} locks {model} after "
                                f"{previous} (canonical order puts {model} first)",
                            )
                        )
                    else:
                        highest = rank
                        previous = model
                        previous_owner = owner
    return found


def test_row_locks_follow_the_canonical_order() -> None:
    """No function may acquire creation-graph row locks out of canonical order."""

    offenders = sorted(
        {message for blame, message in _violations() if not (blame & KNOWN_INVERSIONS)}
    )
    assert not offenders, "row-lock order inversions (deadlock risk):\n" + "\n".join(offenders)


def test_known_inversions_are_all_real() -> None:
    """The debt list may not outlive the inversions it excuses."""

    blamed: set[str] = set()
    for blame, _message in _violations():
        blamed |= blame
    stale = sorted(KNOWN_INVERSIONS - blamed)
    assert not stale, (
        "these entries no longer invert -- delete them from KNOWN_INVERSIONS:\n" + "\n".join(stale)
    )
