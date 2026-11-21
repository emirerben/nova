"""Borrowed intent-review fixtures must run under the shared lock, taken first.

Ways this goes wrong, written before the fix:

- a test file borrows a fixture that writes review or execution rows from a file
  that holds `exclusive_intent_review_ledger` for all its tests, but does not hold
  the lock itself. The rows are then written outside the lock, and a sweep test on
  another xdist worker (same database) settles them. Seen 2026-11-21 in CI:
  `test_intent_review_catalog_postgres.py` got `ignored` instead of `completed`
  while `test_review_sweep_is_time_boxed...` ran on another worker;
- the lock arrives through a later argument (`approved_publication, paid`): pytest
  builds fixtures in argument order, so the rows exist before `paid` takes the lock;
- a new file borrows these fixtures and repeats either mistake.

`usefixtures` fixtures are built before a test's own arguments, so the lock named
there is held before any borrowed fixture writes a row.
"""

import ast
from pathlib import Path

TESTS = Path(__file__).resolve().parent
LOCK = "exclusive_intent_review_ledger"


def _usefixtures(node) -> set[str]:
    """Fixture names in a `pytest.mark.usefixtures(...)` call anywhere under node."""
    names = set()
    for call in ast.walk(node):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "usefixtures"
        ):
            names.update(a.value for a in call.args if isinstance(a, ast.Constant))
    return names


def _module_holds_lock(tree) -> bool:
    return any(
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
        and LOCK in _usefixtures(node.value)
        for node in tree.body
    )


def _fixtures(tree) -> set[str]:
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        node.name
        for node in tree.body
        if isinstance(node, functions)
        and any("fixture" in ast.unparse(decorator) for decorator in node.decorator_list)
    }


def _dotted(path: Path) -> str:
    return ".".join(path.relative_to(TESTS.parent).with_suffix("").parts)


def unlocked_borrowers() -> list[str]:
    texts = {path: path.read_text() for path in sorted(TESTS.rglob("test_*.py"))}
    # Parse only the files that can matter: the suite has about 2,000 test files.
    locked = {
        path: tree
        for path, text in texts.items()
        if LOCK in text and _module_holds_lock(tree := ast.parse(text))
    }
    row_fixtures = {_dotted(path): _fixtures(tree) for path, tree in locked.items()}
    problems = []
    for path, text in texts.items():
        if path in locked or not any(module in text for module in row_fixtures):
            continue
        tree = ast.parse(text)
        borrowed = {
            alias.asname or alias.name
            for node in tree.body
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name in row_fixtures.get(node.module or "", ())
        }
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            used = borrowed & {arg.arg for arg in node.args.args}
            if used and LOCK not in set().union(*map(_usefixtures, node.decorator_list), set()):
                problems.append(
                    f"{path.relative_to(TESTS.parent)}::{node.name} uses {sorted(used)} "
                    f"without usefixtures({LOCK!r})"
                )
    return problems


def test_every_borrower_of_a_locked_files_fixtures_takes_the_lock_first():
    assert unlocked_borrowers() == []
