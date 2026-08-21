"""Stale-lock-read regression gate.

Catches the bug class behind PR #813, where a row lock was real but the data
behind it was not.

Background — read this before touching the allow-list below
===========================================================
``session.get(Model, pk, with_for_update=True)`` DOES emit ``SELECT ... FOR
UPDATE`` and genuinely serializes. But SQLAlchemy only writes the freshly
locked row onto a Python object it is loading for the FIRST time in that
session. If the same session already read that primary key *unlocked*, the
locked call returns the **cached instance**, still carrying its pre-lock
attribute values.

The lock is real. The data behind it is stale.

``dispatch_item_render_for`` hit exactly this: an unlocked pre-read resolved
``content_plan_id``, so the later locked re-read handed back ``current_job_id``
as it looked BEFORE the lock. Two concurrent Generate posts each saw ``None``
and each minted a Job — the precise duplicate the lock exists to prevent. The
same staleness let the ownership fence read a stale ``ownership_epoch``, so a
stale worker could pass the guard built to stop it.

The fix is to pair ``populate_existing=True`` with ``with_for_update=True``
wherever the object may already be cached. That is already the idiom in this
codebase (``content_plan_build._lock_owned_plan_persona``,
``load_owned_plan_persona_sync``); it was simply applied inconsistently.

What this test does
===================
It finds the *shape*, not the convention: within a single SESSION, an unlocked
read of a primary key followed by a locked re-read of the SAME key that does not
refresh. That is high signal — a bare ``with_for_update`` in a fresh session
with nothing cached is perfectly safe, and flagging all ~78 of those would be
noise nobody reads.

Adding a new occurrence fails this test. Fix it by adding
``populate_existing=True`` to the locked read.

Matching is SESSION-scoped, not function-scoped
===============================================
The identity map lives on the session, so two reads only interact when they
share one. A function that opens a session, closes it, does slow work, then
opens a SECOND session to take the lock is the safest possible shape — the new
session has nothing cached. Pairing those would be a false positive, and a gate
that cries wolf is a gate people mute.

This is not hypothetical. A function-scoped first draft of this gate flagged
``autoplace.generate_pool_asset_preview`` (two separate sessions around the
media work) plus three others, so 4 of its 6 findings were noise. Scoping to the
enclosing ``with ... as <session>:`` block removed all four.

The allow-list is a BUG BACKLOG, not an approval list
=====================================================
It was seeded with two verified live instances, read and confirmed to share one
session with their unlocked pre-read, so the gate could start protecting new
code immediately without blocking on individually reviewing each one. It is now
EMPTY -- both were fixed (issue #845; ``populate_existing=True`` added to each
locked re-read). ``test_known_stale_lock_backlog_does_not_grow`` enforces that
it stays empty: fix new violations at the source, never re-add an entry here.

The count is still a floor: matching keys on the source text of the identifier
expression, so ``db.get(PlanItem, content_plan_item_id)`` followed by
``db.get(PlanItem, item_ref.id, with_for_update=True)`` is the same row reached
two ways and does not match.
"""

from __future__ import annotations

import ast
import pathlib

APP_ROOT = pathlib.Path(__file__).resolve().parents[1] / "app"

# (module path relative to app/, enclosing function, Model, identifier source)
# Empty as of issue #845 -- keep it that way. Fix new violations at the
# source (add populate_existing=True to the locked read); do not quarantine
# them here.
KNOWN_STALE_LOCK_READS: frozenset[tuple[str, str, str, str]] = frozenset()


def _get_call_key(call: ast.Call) -> tuple[str, str, str] | None:
    """(receiver, Model, ident-source) for a ``<session>.get(Model, ident, ...)`` call."""
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "get"):
        return None
    if len(call.args) < 2:
        return None
    try:
        return (
            ast.unparse(call.func.value),
            ast.unparse(call.args[0]),
            ast.unparse(call.args[1]),
        )
    except Exception:  # pragma: no cover - defensive, unparse is total in 3.9+
        return None


def _session_scope(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> int | None:
    """Identity of the innermost ``with ... as <name>:`` block enclosing ``node``.

    The identity map is per-SESSION, so two reads only interact when they share
    one. Without this, a function that opens a session, closes it, then opens a
    second one to take the lock reads as a violation when it is the safest
    possible shape — the second session has nothing cached. That false positive
    is not hypothetical: it is exactly ``autoplace.generate_pool_asset_preview``,
    which does its slow media work between two separate sessions.
    """
    cur: ast.AST | None = node
    while cur is not None:
        parent = parents.get(cur)
        if isinstance(parent, (ast.With, ast.AsyncWith)) and any(
            item.optional_vars is not None for item in parent.items
        ):
            return id(parent)
        cur = parent
    return None


def _find_stale_lock_reads() -> list[tuple[str, str, str, str, int]]:
    """Unlocked-then-locked-without-refresh reads of one PK in ONE session."""
    found: list[tuple[str, str, str, str, int]] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        rel = path.relative_to(APP_ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Keyed by session scope as well as receiver, so reads in two
            # different `with session()` blocks never pair with each other.
            unlocked: dict[tuple[int | None, str, str, str], int] = {}
            locked: list[tuple[tuple[int | None, str, str, str], int, bool]] = []
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                key = _get_call_key(node)
                if key is None:
                    continue
                scoped = (_session_scope(node, parents), *key)
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                if "with_for_update" in kwargs:
                    locked.append((scoped, node.lineno, "populate_existing" in kwargs))
                else:
                    # Earliest unlocked read is what poisons the identity map.
                    unlocked.setdefault(scoped, node.lineno)
            for scoped, lineno, refreshed in locked:
                if refreshed:
                    continue
                first_unlocked = unlocked.get(scoped)
                if first_unlocked is not None and first_unlocked < lineno:
                    _, _receiver, model, ident = scoped
                    found.append((rel, fn.name, model, ident, lineno))
    return found


def test_no_new_stale_lock_reads() -> None:
    """A locked re-read of an already-cached row must refresh, or be quarantined."""
    found = _find_stale_lock_reads()
    offenders = {(rel, fn, model, ident) for rel, fn, model, ident, _ in found}

    new = sorted(offenders - KNOWN_STALE_LOCK_READS)
    assert not new, (
        "New stale lock read(s) introduced.\n\n"
        + "\n".join(
            f"  app/{rel}  in {fn}()  ->  {model}[{ident}]" for rel, fn, model, ident in new
        )
        + "\n\nThis function reads that row UNLOCKED first, so the later\n"
        "`with_for_update=True` call returns the cached object with pre-lock\n"
        "values. The lock serializes; the data behind it does not.\n\n"
        "Fix: add `populate_existing=True` to the locked read.\n\n"
        "This is the PR #813 bug: two concurrent Generate posts each read a\n"
        "stale `current_job_id` of None and each minted a Job.\n"
        "Do NOT add yourself to KNOWN_STALE_LOCK_READS -- that list is a bug\n"
        "backlog for pre-existing instances and must never grow (issue #845)."
    )


def test_known_stale_lock_backlog_does_not_grow() -> None:
    """Quarantined instances may be fixed; none may be added, and none may rot.

    A stale entry (fixed, or the function renamed) is as bad as a missing one:
    it makes the backlog untrustworthy, and an allow-list nobody trusts is an
    allow-list nobody reads.
    """
    found = _find_stale_lock_reads()
    offenders = {(rel, fn, model, ident) for rel, fn, model, ident, _ in found}

    stale_entries = sorted(KNOWN_STALE_LOCK_READS - offenders)
    assert not stale_entries, (
        "KNOWN_STALE_LOCK_READS lists entries that no longer match:\n\n"
        + "\n".join(
            f"  app/{rel}  in {fn}()  ->  {model}[{ident}]"
            for rel, fn, model, ident in stale_entries
        )
        + "\n\nIf you fixed them, delete them from the list -- that is the win.\n"
        "If you renamed the function, update the entry."
    )
