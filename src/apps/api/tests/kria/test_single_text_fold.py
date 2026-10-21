"""KRI-786 guard: one Turkish text fold, in ``app/kria/brief_route.py``.

Map section 5 (N01-N13): 15+ private copies of "the Turkish fold" drifted apart. One maps
capital ``I`` to ``ı`` for English patterns (N01, broke "TRY AGAIN"), others delete ``ı``
(N06-N08) or keep it as a separate letter (N09). ``brief_route.fold_text`` / ``loose_text``
are the reference: İ/I/ı -> i, casefold, then (loose) diacritics stripped.

A scope (a function, or a module-level statement) is a fold copy when it contains any of:

- a dict literal with two or more single-character Turkish keys (``{"ı": "i", "ş": "s"}``),
- ``str.maketrans(...)`` with a Turkish letter in its arguments,
- ``.replace("ı", ...)`` / ``.replace("İ", ...)`` etc. on a Turkish letter, or
- ``unicodedata.normalize("NFKD" | "NFD", ...)`` (diacritic stripping; with
  ``.encode("ascii", "ignore")`` it deletes ``ı`` outright).

Each module may have at most its ``BASELINE`` count of such scopes; the count may only go
down (lane B of the Turkish parity project is replacing them with ``brief_route``). A new
module starts at zero. A fold that is deliberately different (``silence_cut`` maps I to ı on
purpose) or does not read creator/model text carries ``# single-fold: <why>`` on the flagged
line.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_CANONICAL = "kria/brief_route.py"
_EXEMPT = "# single-fold:"
_TURKISH = frozenset("ıİşŞğĞ")

# Fold-copy scopes per module, counted on codex/turkish-parity (KRI-786). Lower a number
# when you replace a copy with brief_route.fold_text/loose_text; never raise one.
BASELINE: dict[str, int] = {
    "agents/clip_intent_planner.py": 1,
    "agents/landmark_guess.py": 1,
    "kria/brief.py": 1,
    "kria/brief_checks.py": 1,
    "pipeline/caption_correct.py": 1,
    "pipeline/caption_language.py": 2,
    "pipeline/caption_spelling.py": 2,
    "pipeline/lyrics_alignment.py": 1,
    "pipeline/silence_cut.py": 1,
    "pipeline/song_alignment.py": 1,
    "schemas/clip_intents.py": 2,
    "schemas/edit_proposal.py": 1,
    "services/clip_facts.py": 1,
    "services/clip_file_names.py": 1,
    "services/clip_selection.py": 1,
    "services/content_plan_dedup.py": 1,
    "services/edit_direction_planner.py": 1,
    "services/lrclib_client.py": 1,
    "services/phone_reaction_grounding.py": 2,
    "services/seed_provenance.py": 1,
    "services/sfx_catalog.py": 1,
    "services/transcript_quality.py": 1,
    "smart_edit/compiler.py": 1,
    "smart_edit/planner.py": 1,
}


def _has_turkish(value: object) -> bool:
    return isinstance(value, str) and bool(_TURKISH & set(value))


def _fold_signal(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
        return sum(1 for k in keys if _has_turkish(k) and len(k) == 1) >= 2
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    attr, args = node.func.attr, node.args
    if attr == "maketrans":
        return any(
            _has_turkish(n.value)
            for arg in args
            for n in ast.walk(arg)
            if isinstance(n, ast.Constant)
        )
    if attr == "replace" and args and isinstance(args[0], ast.Constant):
        value = args[0].value
        return _has_turkish(value) and len(value) <= 2
    if attr == "normalize" and args and isinstance(args[0], ast.Constant):
        return args[0].value in {"NFKD", "NFD"}
    return False


def _scopes(tree: ast.Module) -> list[ast.AST]:
    """Every function (nested ones are their own scope) plus each other module statement."""
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    top = [
        n
        for n in tree.body
        if not isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]
    return [*funcs, *top]


def _own_nodes(scope: ast.AST):
    """Nodes in ``scope`` that do not belong to a nested function."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def fold_copies(path: Path) -> list[int]:
    """Line numbers of the fold-copy scopes in ``path``."""
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    found = []
    for scope in _scopes(ast.parse(source)):
        signals = [
            n for n in _own_nodes(scope) if _fold_signal(n) and _EXEMPT not in lines[n.lineno - 1]
        ]
        if signals:
            found.append(min(n.lineno for n in signals))
    return found


def current_counts() -> dict[str, int]:
    counts = {}
    for path in sorted(_APP.rglob("*.py")):
        rel = str(path.relative_to(_APP))
        if rel == _CANONICAL:
            continue
        if n := len(fold_copies(path)):
            counts[rel] = n
    return counts


def test_canonical_fold_still_exists():
    tree = ast.parse((_APP / _CANONICAL).read_text(encoding="utf-8"))
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert {"fold_text", "loose_text"} <= names


def test_no_new_private_turkish_fold():
    counts = current_counts()
    grew = {rel: (n, BASELINE.get(rel, 0)) for rel, n in counts.items() if n > BASELINE.get(rel, 0)}
    shrank = {
        rel: counts.get(rel, 0) for rel, base in BASELINE.items() if counts.get(rel, 0) < base
    }
    if shrank:
        hint = ", ".join(f'"{rel}": {n}' for rel, n in sorted(shrank.items()))
        warnings.warn(f"KRI-786 fold copies went down; lower BASELINE: {hint}", stacklevel=1)
    assert not grew, (
        "KRI-786: new private Turkish text fold (scopes now, baseline): "
        f"{grew}. Use app.kria.brief_route.fold_text (İ/I/ı -> i, casefold) or loose_text "
        "(plus diacritics stripped) instead of another copy; the copies drift (parity map "
        "N01-N13). A deliberately different fold carries `# single-fold: <why>` on its line. "
        "See agents/DECISIONS.md 'Language policy'."
    )
