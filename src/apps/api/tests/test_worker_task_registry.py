"""Guard: every app/tasks module that defines a Celery task must be reachable
from the worker's import surface.

A module that defines ``@celery_app.task`` but is never imported by the worker
is SILENTLY discarded at runtime: the producer's ``.delay()`` succeeds, the
worker logs "Received unregistered task" and drops it, and nothing retries.
This has bitten before (see the celery-worker-include-coupling learning) —
every new ``app/tasks/*.py`` had to be remembered by hand until this test.

Pure AST analysis — no Celery app boot, no DB, no network — so it runs in the
plain CI pytest job.
"""

from __future__ import annotations

import ast
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
APP = API_ROOT / "app"
TASKS_DIR = APP / "tasks"

# Modules that define Celery tasks but are INTENTIONALLY not wired into the
# worker. Every entry needs a reason — an entry here means any .delay() call
# against its tasks is dropped by the worker.
KNOWN_UNREGISTERED = {
    # tasks.send_waitlist_confirmation is enqueued by routes/waitlist.py but the
    # worker never imports app.tasks.email, so waitlist confirmation sends are
    # dropped today. Deliberately left dark pending the "Resend confirmation
    # email" TODO (email creds + go-live decision). When that ships, add
    # "app.tasks.email" to worker.py's include list and remove this entry.
    "app.tasks.email",
}


def _imports_of(path: Path) -> set[str]:
    """app.tasks.* module names imported anywhere in this file (incl. nested)."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names if a.name.startswith("app.tasks."))
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "app.tasks":
                found.update(f"app.tasks.{a.name}" for a in node.names)
            elif node.module.startswith("app.tasks."):
                found.add(node.module)
    return found


def _defines_celery_task(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                if isinstance(target, ast.Attribute) and target.attr == "task":
                    return True
                if isinstance(target, ast.Name) and target.id == "shared_task":
                    return True
    return False


def _worker_include_list() -> set[str]:
    for node in ast.walk(ast.parse((APP / "worker.py").read_text())):
        if isinstance(node, ast.keyword) and node.arg == "include":
            return {
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            }
    raise AssertionError("could not find the include=[...] list in app/worker.py")


def _reachable_from_worker() -> set[str]:
    """Include list + app.tasks.* imports of worker.py, transitively closed."""
    reachable = _worker_include_list() | _imports_of(APP / "worker.py")
    frontier = list(reachable)
    while frontier:
        mod = frontier.pop()
        mod_path = APP / (mod.removeprefix("app.").replace(".", "/") + ".py")
        if not mod_path.exists():
            continue
        for imp in _imports_of(mod_path) - reachable:
            reachable.add(imp)
            frontier.append(imp)
    return reachable


def test_every_task_module_is_reachable_from_worker() -> None:
    task_modules = {
        f"app.tasks.{p.stem}"
        for p in TASKS_DIR.glob("*.py")
        if p.name != "__init__.py" and _defines_celery_task(p)
    }
    missing = task_modules - _reachable_from_worker() - KNOWN_UNREGISTERED
    assert not missing, (
        f"These app/tasks modules define Celery tasks but the worker never "
        f"imports them — their tasks are silently discarded at runtime: "
        f"{sorted(missing)}. Add them to the include=[...] list in "
        f"app/worker.py (or, if the module is deliberately dark, to "
        f"KNOWN_UNREGISTERED in this test with a reason)."
    )


def test_allowlist_has_no_stale_entries() -> None:
    stale = KNOWN_UNREGISTERED & _reachable_from_worker()
    assert not stale, (
        f"KNOWN_UNREGISTERED entries are now wired into the worker — remove "
        f"them from the allowlist: {sorted(stale)}"
    )
