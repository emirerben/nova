"""Keeps the Swift/Python `MediaCapability` mirrors and the capability
classification doc from silently drifting apart.

Unlike the mobile OpenAPI contract (`app.cli.kria_contracts`, generated from
these same Pydantic models and checked by `--check`), the Swift
`KriaMediaEngine.MediaCapability` enum is hand-written and lives in a package
the OpenAPI generator never touches. Nothing enforced its case list matched
the Python `Literal` before this test existed.
"""

from __future__ import annotations

import re
import typing
from pathlib import Path

from app.kria.recipes import MediaCapability

REPO_ROOT = Path(__file__).resolve().parents[5]
SWIFT_MODELS = (
    REPO_ROOT / "src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Models.swift"
)
MATRIX_DOC = REPO_ROOT / "docs/reviews/kri-29/capability-matrix.md"


def _python_capabilities() -> set[str]:
    return set(typing.get_args(MediaCapability))


def _swift_capabilities() -> set[str]:
    text = SWIFT_MODELS.read_text(encoding="utf-8")
    match = re.search(r"public enum MediaCapability:[^{]*\{(.*?)\}", text, re.DOTALL)
    assert match, "MediaCapability enum not found in Models.swift"
    # A single `case` keyword can list its cases across multiple lines,
    # joined only by trailing commas, so join the whole body before
    # splitting rather than matching `case` per line.
    body = re.sub(r"\bcase\s+", "", match.group(1))
    return {name.strip() for name in body.split(",") if name.strip()}


def _matrix_doc_capabilities() -> set[str]:
    text = MATRIX_DOC.read_text(encoding="utf-8")
    # Row entries look like "| `capabilityName` | ..." in the three
    # classification tables; the prose/heading backticks elsewhere in the
    # doc don't start a table row, so anchoring on "| `" excludes them.
    return set(re.findall(r"^\| `([a-zA-Z0-9]+)` \|", text, re.MULTILINE))


def test_swift_and_python_media_capability_enums_match():
    python_caps = _python_capabilities()
    swift_caps = _swift_capabilities()
    assert python_caps == swift_caps, (
        f"MediaCapability drift: only in Python={python_caps - swift_caps}, "
        f"only in Swift={swift_caps - python_caps}"
    )


def test_every_capability_has_a_matrix_row():
    python_caps = _python_capabilities()
    matrix_caps = _matrix_doc_capabilities()
    missing = python_caps - matrix_caps
    assert not missing, f"capability-matrix.md is missing a row for: {sorted(missing)}"
    # The doc's cloud-fallback section intentionally lists non-enum names
    # (no MediaCapability case), so orphans are expected there; only assert
    # coverage is complete, not that the doc names nothing extra.
