"""Real UI test files the self-tests borrow, found by scanning the tree.

The self-tests need a real, single-class UI test file in a known group. Taking
one from the tree instead of spelling its path, class and methods out means a
rename or removal of a real UI test cannot break them.
"""

from types import SimpleNamespace
import re

CLASS = re.compile(r"\bclass\s+(\w+)\s*:\s*XCTestCase\b")
METHOD = re.compile(r"\bfunc\s+(test\w+)\s*\(")


def fixtures(ui, group="projects"):
    """Unmapped single-class files whose tests all select `smoke,<group>`."""
    data = ui.manifest()
    found = []
    for path in sorted((ui.ROOT / ui.UI_TEST_ROOT).rglob("*.swift")):
        relative = path.relative_to(ui.ROOT).as_posix()
        source = path.read_text()
        classes, methods = CLASS.findall(source), METHOD.findall(source)
        if len(classes) != 1 or len(methods) < 2 or relative in data["sources"]:
            continue
        if ui.select_groups([relative])[0] == f"smoke,{group}":
            found.append(
                SimpleNamespace(path=relative, cls=classes[0], methods=methods)
            )
    if len(found) < 2:
        raise AssertionError(f"fewer than two UI test files select smoke,{group}")
    return found


def file_of(ui, cls):
    return next(
        path
        for path in sorted((ui.ROOT / ui.UI_TEST_ROOT).rglob("*.swift"))
        if re.search(rf"\bclass\s+{cls}\b", path.read_text())
    )
