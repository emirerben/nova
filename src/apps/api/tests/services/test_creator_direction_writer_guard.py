"""Guard the canonical mutation boundary for legacy Persona.style writes."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

from app.services.creator_direction import CreatorDirectionService

APP_ROOT = Path(__file__).resolve().parents[2] / "app"
WRITER_ROOTS = (APP_ROOT / "routes", APP_ROOT / "tasks")


def test_compatibility_style_adapter_preserves_legacy_assignment_semantics() -> None:
    persona = SimpleNamespace(style={"status": "ready"})
    replacement = {"status": "edited", "knobs": {"font_family": "Inter"}}

    CreatorDirectionService.set_compatibility_persona_style(persona, replacement)

    assert persona.style is replacement


def test_routes_and_tasks_do_not_assign_persona_style_directly() -> None:
    bypasses: list[str] = []
    for root in WRITER_ROOTS:
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = list(node.targets)
                elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                    targets = [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "style":
                        bypasses.append(f"{path.relative_to(APP_ROOT)}:{node.lineno}")

    assert bypasses == [], (
        "Persona.style writes must use "
        "CreatorDirectionService.set_compatibility_persona_style: " + ", ".join(bypasses)
    )
