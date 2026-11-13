"""No module outside the model registry spells a non-Gemini model name (KRI-1385).

A second spelling is where a model-specific branch starts: before the registry, adding one
text model meant changing a dozen sites (dispatch, price, efforts, ledger SQL, comparison).
Settings defaults may name a model, but only one the registry declares.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from app.agents import model_registry

APP = Path(__file__).resolve().parents[2] / "app"
REGISTRY = APP / "agents" / "model_registry.py"
CONFIG = APP / "config.py"
_NAME = (
    r"(?:gpt-\d[\w.-]*|gpt-(?:image|realtime)-[\w.-]+|whisper-\d"
    r"|claude-(?:opus|sonnet|haiku|fable|mythos)-[\w.-]+|o\d(?:-mini|-pro)?)"
)
# A string that is a model name, or quotes one (SQL, a JSON snippet). Prose that mentions a
# model in a docstring is documentation, not a spelling the code branches on.
MODEL_NAME = re.compile(rf"^({_NAME})$|['\"]({_NAME})['\"]")
DECLARED = {
    value
    for name, value in vars(model_registry).items()
    if name.isupper() and isinstance(value, str)
} | {spec.name for spec in model_registry._SPECS}


def _model_names(path: Path) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found += [
                (node.lineno, whole or quoted)
                for whole, quoted in MODEL_NAME.findall(node.value.strip())
            ]
    return found


def test_only_the_registry_names_non_gemini_models():
    offenders = [
        f"{path.relative_to(APP.parent)}:{line}: {name}"
        for path in sorted(APP.rglob("*.py"))
        if path not in (REGISTRY, CONFIG)
        for line, name in _model_names(path)
    ]
    offenders += [
        f"app/config.py:{line}: {name} (not declared in model_registry)"
        for line, name in _model_names(CONFIG)
        if name not in DECLARED
    ]
    assert not offenders, "import the name from app.agents.model_registry:\n" + "\n".join(offenders)
