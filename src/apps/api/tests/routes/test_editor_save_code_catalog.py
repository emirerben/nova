"""Every machine code the editor Save can refuse with is accounted for (KRI-770).

The app shows ONE generic line ("This save was rejected") for any 422 code it does not word.
Troya's trimmed-clip Save died on exactly that. This guard makes adding a server code a
conscious decision: a new code must be listed in ``editor_save_codes.json`` either as
``mapped`` (the app has specific recovery copy, pinned by ``EditorSaveCodeCatalogTests``) or as
``generic`` (the honest outcome is the generic line, with the real code in diagnostics).

Plain-English ``detail`` strings are not codes: the app shows those as written.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT.parents[2] / "src/apps/ios/Tests/Fixtures/editor_save_codes.json"
# The modules that refuse an editor Save with a 422 code.
SOURCES = (
    "app/services/authored_editor.py",
    "app/services/guided_timeline.py",
    "app/services/phone_editor.py",
    "app/routes/generative_jobs.py",
)


def _is_422(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return node.value == 422
    return isinstance(node, ast.Attribute) and node.attr.startswith("HTTP_422")


def _code_of(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values, strict=False):
            if (
                isinstance(key, ast.Constant)
                and key.value == "code"
                and isinstance(value, ast.Constant)
            ):
                return value.value
    return None


def raised_machine_codes() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for relative in SOURCES:
        tree = ast.parse((ROOT / relative).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", getattr(node.func, "attr", None))
            code = None
            if name == "_timeline_error" and len(node.args) >= 2 and _is_422(node.args[0]):
                code = _code_of(node.args[1])
            elif name == "GuidedTimelineError" and node.args:
                code = _code_of(node.args[0])
            elif name == "HTTPException":
                status = node.args[0] if node.args else None
                detail = next((k.value for k in node.keywords if k.arg == "detail"), None)
                for keyword in node.keywords:
                    if keyword.arg == "status_code":
                        status = keyword.value
                if detail is None and len(node.args) > 1:
                    detail = node.args[1]
                if status is not None and _is_422(status):
                    code = _code_of(detail)
            if code and " " not in code and "`" not in code:
                found.setdefault(code, set()).add(Path(relative).name)
    return found


def test_catalog_lists_are_disjoint() -> None:
    catalog = json.loads(CATALOG.read_text())
    assert not set(catalog["mapped"]) & set(catalog["generic"])


def test_every_code_the_save_can_raise_is_in_the_catalog() -> None:
    catalog = json.loads(CATALOG.read_text())
    known = set(catalog["mapped"]) | set(catalog["generic"])
    unknown = {
        code: sorted(files) for code, files in raised_machine_codes().items() if code not in known
    }
    assert not unknown, (
        "A Save refusal code has no catalog entry, so the app would show 'This save was "
        f"rejected' for it: {unknown}. Add it to "
        "src/apps/ios/Tests/Fixtures/editor_save_codes.json as 'mapped' (and word it in "
        "EditorSaveError.timelineCopy) or as 'generic'."
    )


def test_trim_family_is_always_mapped() -> None:
    """The window codes a trim can hit must never fall back to the generic line."""
    catalog = json.loads(CATALOG.read_text())
    for code in raised_machine_codes():
        if code.startswith("TIMELINE_") or code in {
            "GUIDED_REVISION_INVALID",
            "authored_seconds_required",
        }:
            assert code in catalog["mapped"], code
