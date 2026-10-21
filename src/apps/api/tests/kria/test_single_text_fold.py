"""KRI-786 guard: one Turkish text fold, in ``app/text_fold.py``.

Map section 5 (N01-N13): 15+ private copies of "the Turkish fold" drifted apart. One maps
capital ``I`` to ``ı`` for English patterns (N01, broke "TRY AGAIN"), others delete ``ı``
(N06-N08) or keep it as a separate letter (N09). ``brief_route.fold_text`` / ``loose_text``
are the reference: İ/I/ı -> i, casefold, then (loose) diacritics stripped. Since KRI-781
they live in ``app/text_fold.py`` (stdlib-only, importable from agents/pipeline/services);
``app/kria/brief_route.py`` re-exports them under the same names. Turkish-aware casing
(``lower`` / ``upper`` / ``capitalize``) lives there too, so ``.replace("I", "ı").lower()``
copies belong there as ``text_fold.lower(text, "tr")``.

A scope (a function, or a module-level statement) is a fold copy when it contains any of:

- a dict literal (keys may be ``ord("ı")``) that maps ``İ``, maps ``I`` to ``ı``, or has
  two Turkish-only keys / three keys from ıİşŞğĞçÇöÖüÜ (``{"ı": "i", "ş": "s"}``,
  ``{"ç": "c", "ğ": "g", "ö": "o"}``),
- ``str.maketrans(...)`` with a Turkish letter in its arguments,
- ``.replace("ı", ...)`` / ``.replace("İ", ...)`` on a Turkish letter, or
  ``.replace("I", "ı")`` (the N01 drift), or
- ``normalize("NFKD" | "NFD", ...)``, called as ``unicodedata.normalize`` or a bare
  imported ``normalize`` (diacritic stripping; with ``.encode("ascii", "ignore")`` it deletes
  ``ı`` outright).

Class-level tables count as their own scope.

Each module may have at most its ``BASELINE`` count of such scopes; the count may only go
down (KRI-801 replaces the rest with ``app.text_fold``). A new
module starts at zero. A fold that is deliberately different (``silence_cut`` maps I to ı on
purpose) or does not read creator/model text carries ``# single-fold: <why>`` on the flagged
line.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_CANONICAL = "text_fold.py"
_EXEMPT = "# single-fold:"
_TURKISH = frozenset("ıİşŞğĞ")
# Letters a Turkish diacritic table maps (``{"ç": "c", "ğ": "g", ...}``).
_FOLD_LETTERS = frozenset("ıİşŞğĞçÇöÖüÜ")

# Fold-copy scopes per module, recounted on main after Turkish parity lanes A-E (KRI-786).
# Lower a number when you replace a copy with app.text_fold (fold_text / loose_text /
# lower / upper); never raise one. The remaining copies are tracked in KRI-801.
BASELINE: dict[str, int] = {
    "agents/clip_intent_planner.py": 1,
    "pipeline/caption_correct.py": 1,
    "pipeline/song_alignment.py": 1,
    "schemas/clip_intents.py": 2,
    "schemas/edit_proposal.py": 1,
    "services/choice_questions.py": 1,
    "services/clip_facts.py": 1,
    "services/clip_file_names.py": 1,
    "services/phone_reaction_grounding.py": 2,
    "services/transcript_quality.py": 1,
    "smart_edit/planner.py": 1,
}


def _has_turkish(value: object) -> bool:
    return isinstance(value, str) and bool(_TURKISH & set(value))


def _const_str(node: ast.AST | None) -> str | None:
    """A str constant, or the character behind ``ord("ı")`` (translate-table keys)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "ord"
        and len(node.args) == 1
    ):
        return _const_str(node.args[0])
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        try:
            return chr(node.value)
        except (ValueError, OverflowError):
            return None
    return None


def _dotted_i_mapping(key: str | None, value: str | None) -> bool:
    """``"İ" -> ...`` or ``"I" -> "ı"``: the Turkish dotted/dotless I rule, alone a fold."""
    return key == "İ" or (key == "I" and value is not None and "ı" in value)


def _fold_signal(node: ast.AST) -> bool:
    if isinstance(node, ast.Dict):
        pairs = [(_const_str(k), _const_str(v)) for k, v in zip(node.keys, node.values)]
        if any(_dotted_i_mapping(k, v) for k, v in pairs):
            return True
        letters = [k for k, _ in pairs if k is not None and len(k) == 1 and k in _FOLD_LETTERS]
        return (len(letters) >= 2 and any(k in _TURKISH for k in letters)) or len(letters) >= 3
    if not isinstance(node, ast.Call):
        return False
    if isinstance(node.func, ast.Attribute):
        attr = node.func.attr
    elif isinstance(node.func, ast.Name):
        attr = node.func.id
    else:
        return False
    args = node.args
    if attr == "maketrans":
        return any(
            _has_turkish(n.value)
            for arg in args
            for n in ast.walk(arg)
            if isinstance(n, ast.Constant)
        )
    if attr == "replace" and isinstance(node.func, ast.Attribute) and len(args) >= 2:
        old, new = _const_str(args[0]), _const_str(args[1])
        if old is not None and _has_turkish(old) and len(old) <= 2:
            return True
        # ``.replace("I", "ı")``: the N01 drift (English "TRY AGAIN" -> "try agaın").
        return old in {"I", "i"} and new is not None and bool({"ı", "İ"} & set(new))
    if attr == "normalize" and args:
        return _const_str(args[0]) in {"NFKD", "NFD"}
    return False


def _scopes(tree: ast.Module) -> list[ast.AST]:
    """Every function (nested ones are their own scope), each other module statement, and
    each non-method statement of a class body (class-level tables)."""
    funcs = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    not_scope = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    top = [n for n in tree.body if not isinstance(n, not_scope)]
    class_level = [
        stmt
        for cls in ast.walk(tree)
        if isinstance(cls, ast.ClassDef)
        for stmt in cls.body
        if not isinstance(stmt, not_scope)
    ]
    return [*funcs, *top, *class_level]


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
    assert {"fold_text", "loose_text", "lower", "upper"} <= names
    # brief_route keeps its historical names as a re-export, never a second copy.
    from app import text_fold  # noqa: PLC0415
    from app.kria import brief_route  # noqa: PLC0415

    assert brief_route.fold_text is text_fold.fold_text
    assert brief_route.loose_text is text_fold.loose_text


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
        f"{grew}. Use app.text_fold.fold_text (İ/I/ı -> i, casefold), loose_text (plus "
        "diacritics stripped) or lower/upper(text, language) instead of another copy; the "
        "copies drift (parity map N01-N13). A deliberately different fold carries "
        "`# single-fold: <why>` on its line. See agents/DECISIONS.md 'Language policy'."
    )
