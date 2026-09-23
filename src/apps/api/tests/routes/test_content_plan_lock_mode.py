"""Static guard: every ``ContentPlan`` row lock is ``FOR NO KEY UPDATE``.

A creator's chats all share one ContentPlan row, and every write to a row that
references it takes ``FOR KEY SHARE`` on the plan for the foreign-key check.
A plain ``FOR UPDATE`` on the plan blocks those checks, which closed a deadlock
cycle that the lock-order guard (``test_lock_order.py``) cannot see: it tracks
explicit ``FOR UPDATE`` reads, not the implicit foreign-key lock.  See
:data:`app.db_locks.CONTENT_PLAN_LOCK`.

This walks every module under ``app/`` and fails on any ContentPlan lock that
does not go through ``CONTENT_PLAN_LOCK`` (or spell out ``key_share=True``).
"""

from __future__ import annotations

import ast
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.db_locks import CONTENT_PLAN_LOCK
from app.models import ContentPlan

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
MODEL = "ContentPlan"
LOCK_NAME = "CONTENT_PLAN_LOCK"


def _is_lock_constant(node: ast.AST) -> bool:
    return (isinstance(node, ast.Name) and node.id == LOCK_NAME) or (
        isinstance(node, ast.Attribute) and node.attr == LOCK_NAME
    )


def _get_lock_is_no_key(value: ast.AST) -> bool:
    """``with_for_update=`` value of ``db.get(ContentPlan, ...)``."""

    if _is_lock_constant(value):
        return True
    if isinstance(value, ast.Constant) and value.value in (False, None):
        return True  # no lock at all
    if isinstance(value, ast.IfExp):
        return _get_lock_is_no_key(value.body) and _get_lock_is_no_key(value.orelse)
    if isinstance(value, ast.Dict):
        return any(
            isinstance(key, ast.Constant)
            and key.value == "key_share"
            and isinstance(item, ast.Constant)
            and item.value is True
            for key, item in zip(value.keys, value.values, strict=True)
        )
    return False


def _entity(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    # ContentPlan.id locks the plan row too.
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id
    return None


def _chain_entities(node: ast.AST | None, env: dict[str, frozenset[str]]) -> frozenset[str]:
    """Entities a ``select(...)`` chain reads or joins, following ``stmt = ...`` names."""

    found: set[str] = set()
    cursor = node
    while True:
        if isinstance(cursor, ast.Name):
            return frozenset(found | env.get(cursor.id, frozenset()))
        if isinstance(cursor, ast.Attribute):
            cursor = cursor.value
            continue
        if not isinstance(cursor, ast.Call):
            return frozenset()
        inner = cursor.func
        if isinstance(inner, ast.Name) and inner.id == "select":
            found.update(name for arg in cursor.args if (name := _entity(arg)))
            return frozenset(found)
        if isinstance(inner, ast.Attribute) and inner.attr in {"join", "outerjoin"}:
            if cursor.args and (name := _entity(cursor.args[0])):
                found.add(name)
        cursor = inner


def _locked_entities(call: ast.Call, entities: frozenset[str]) -> frozenset[str]:
    """The subset of ``entities`` a ``with_for_update(...)`` call locks (``of=``)."""

    of = next((kw.value for kw in call.keywords if kw.arg == "of"), None)
    if of is None:
        return entities
    targets = of.elts if isinstance(of, ast.Tuple | ast.List) else [of]
    return frozenset(name for target in targets if (name := _entity(target)))


def _chain_lock_is_no_key(call: ast.Call) -> bool:
    for keyword in call.keywords:
        if keyword.arg is None and _is_lock_constant(keyword.value):
            return True  # .with_for_update(**CONTENT_PLAN_LOCK)
        if (
            keyword.arg == "key_share"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is True
        ):
            return True
    return False


def _scan(tree: ast.AST, label: str) -> tuple[list[str], list[str]]:
    """Return ``(ContentPlan lock sites, the ones that are not NO KEY UPDATE)``."""

    sites: list[str] = []
    violations: list[str] = []
    scopes = [tree] + [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    ]
    seen: set[int] = set()
    for scope in scopes:
        # Names bound to a select() chain in this scope, in source order.
        env: dict[str, frozenset[str]] = {}
        nodes = sorted(
            (n for n in ast.walk(scope) if hasattr(n, "lineno")),
            key=lambda n: (n.lineno, n.col_offset),
        )
        for node in nodes:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    entities = _chain_entities(node.value, env)
                    if entities:
                        env[target.id] = entities
                continue
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if id(node) in seen:
                continue
            site = f"{label}:{node.lineno}"
            if node.func.attr == "get" and node.args:
                first = node.args[0]
                if not (isinstance(first, ast.Name) and first.id == MODEL):
                    continue
                lock = next((kw.value for kw in node.keywords if kw.arg == "with_for_update"), None)
                if lock is None:
                    continue
                seen.add(id(node))
                sites.append(site)
                if not _get_lock_is_no_key(lock):
                    violations.append(site)
            elif node.func.attr == "with_for_update":
                entities = _chain_entities(node.func.value, env)
                if MODEL not in _locked_entities(node, entities):
                    continue
                seen.add(id(node))
                sites.append(site)
                if not _chain_lock_is_no_key(node):
                    violations.append(site)
    return sites, violations


def _content_plan_locks() -> tuple[list[str], list[str]]:
    sites: list[str] = []
    violations: list[str] = []
    for path in sorted(APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        found, bad = _scan(tree, str(path.relative_to(APP_ROOT.parent)))
        sites += found
        violations += bad
    return sites, violations


def test_every_content_plan_lock_is_no_key_update() -> None:
    sites, violations = _content_plan_locks()
    # The matcher must actually be finding the call sites, or an empty
    # violation list proves nothing.  34 existed when this guard landed.
    assert len(sites) >= 30, sites
    assert violations == [], (
        "Lock ContentPlan with app.db_locks.CONTENT_PLAN_LOCK "
        "(SELECT ... FOR NO KEY UPDATE); plain FOR UPDATE blocks the "
        "foreign-key checks of every chat that shares the plan:\n  " + "\n  ".join(violations)
    )


def test_guard_sees_every_lock_spelling() -> None:
    source = """
async def direct(db, pid):
    return await db.get(ContentPlan, pid, with_for_update=True)

async def direct_ok(db, pid, lock):
    await db.get(ContentPlan, pid, with_for_update=CONTENT_PLAN_LOCK)
    return await db.get(ContentPlan, pid, with_for_update=CONTENT_PLAN_LOCK if lock else False)

async def chained(db, pid):
    return await db.execute(select(ContentPlan).where(ContentPlan.id == pid).with_for_update())

async def column(db, uid):
    await db.execute(select(ContentPlan.id).where(ContentPlan.user_id == uid).with_for_update())

async def two_step(db, pid, for_update):
    stmt = select(ContentPlan).where(ContentPlan.id == pid)
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return await db.execute(stmt)

async def two_step_ok(db, pid):
    stmt = select(ContentPlan).where(ContentPlan.id == pid)
    stmt = stmt.with_for_update(**CONTENT_PLAN_LOCK)
    return await db.execute(stmt)

async def other_model(db, pid):
    stmt = select(PlanItem).where(PlanItem.id == pid)
    return await db.execute(stmt.with_for_update())

async def joined(db, pid):
    await db.execute(select(PlanItem).join(ContentPlan).with_for_update())
    await db.execute(select(PlanItem, ContentPlan).with_for_update(**CONTENT_PLAN_LOCK))
    await db.execute(select(PlanItem).join(ContentPlan).with_for_update(of=PlanItem))
    await db.execute(select(ContentPlan).join(PlanItem).with_for_update(of=(ContentPlan,)))
"""
    sites, violations = _scan(ast.parse(source), "snippet")
    assert sites == [
        "snippet:3",
        "snippet:6",
        "snippet:7",
        "snippet:10",
        "snippet:13",
        "snippet:18",
        "snippet:23",
        "snippet:31",
        "snippet:32",
        "snippet:34",
    ]
    assert violations == [
        "snippet:3",
        "snippet:10",
        "snippet:13",
        "snippet:18",
        "snippet:31",
        "snippet:34",
    ]


def test_content_plan_lock_renders_for_no_key_update() -> None:
    statement = select(ContentPlan).where(ContentPlan.id == ContentPlan.id)
    sql = str(statement.with_for_update(**CONTENT_PLAN_LOCK).compile(dialect=postgresql.dialect()))
    assert sql.rstrip().endswith("FOR NO KEY UPDATE")
