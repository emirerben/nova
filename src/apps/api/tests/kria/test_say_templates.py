"""``say()`` templates: one English key per chat sentence (KRI-1362, "B").

``scripts/kria/say_templates_codemod.py`` rewrote ~500 ``say(en=f"...", tr=f"...")`` calls into
``say(en="... {name} ...", tr="... {name} ...", name=<expr>)`` so a catalog can look a sentence up
by its English text. Ways that rewrite (or a later hand edit in the same style) can go wrong:

1. Wrong placeholder: a field names a value the call does not pass, or a passed value is never
   used (the expression moved to the wrong field, or two different expressions share a name).
2. Lost conversion or format spec: ``{x!r}`` / ``{x:.1f}`` / ``{x:g}`` written back as ``{x}``,
   so the creator sees ``0.5`` instead of ``'0.5'`` or ``2.0`` instead of ``2``.
3. Unescaped brace: a literal ``{``/``}`` in the f-string text (or in a plain-literal ``tr`` of a
   templated call) left single, so ``str.format`` reads it as a field and raises or eats it.
4. Double evaluation turned single: an expression used in both ``en`` and ``tr`` is now evaluated
   once. Harmless for pure formatting; wrong for a side-effecting call (``next(it)``). The codemod
   lists every non-pure expression in its report for review.
5. KeyError/IndexError/ValueError at runtime: a template ``say()`` cannot fill with the values it
   gets (missing key, positional ``{}`` / ``{0}``, a spec the value type rejects), which would
   turn a reply into a 500 only on the turn that reaches that sentence.
6. A skipped call silently counted as converted: a call left as an f-string (nested f-string,
   non-literal ``en``) while the report says it was converted, so its English key still changes
   per turn and the catalog never matches it.
7. ``say()`` itself: values ignored for Turkish or the unbound default, the catalog consulted
   with the filled sentence instead of the template, or a catalog miss not falling back to
   English.

The codemod proves (1)-(4) per call against the pre-rewrite source (``--verify REV``). These
tests keep (1), (3), (5) and (6) true for every ``say()`` in ``app/`` from now on, prove that
``str.format`` gives the same text as the f-string a template stands for (2, 3), and pin (7).
"""

from __future__ import annotations

import ast
import importlib.util
import string
import sys
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

from app.config import settings
from app.kria import copy_catalog
from app.kria.reply_language import reply_language_for, say

_APP = Path(__file__).resolve().parents[2] / "app"
_BUILD = _APP.parents[3] / "scripts" / "kria" / "build_copy_catalog.py"

# Calls that pick copy like ``say()``: ``say_in(language, en=..., tr=..., **values)`` for an
# explicit language, and two module wrappers that forward their en/tr (and values) to it.
_SAY_FUNCTIONS = frozenset({"say", "say_in"})
_SAY_WRAPPERS = frozenset(
    {("agents/clip_intent_planner.py", "_copy"), ("services/song_order.py", "_stand_in")}
)

# say() calls that still build en/tr with f-strings, by (module, enclosing function). Each one's
# en or tr is not a plain literal (a conditional, a call) or holds a nested f-string, so the
# codemod left it alone; they are handled when non-literal sentences move to the catalog.
_KNOWN_FSTRING_CALLS = frozenset(
    {
        ("agents/editor_ops_v2/timeline.py", "_coerce_reorder_clips_by"),
        ("kria/editor_sequence_timing.py", "sequence_timing_plan"),
        ("services/choice_questions.py", "_text_placement"),
        ("services/kria_editor_ops_diff.py", "_describe_text"),
        ("services/kria_editor_ops_diff.py", "_plural"),
    }
)


def _say_calls() -> list[tuple[str, str, ast.Call]]:
    """(module path under app/, enclosing function, call) for every say() call."""
    found: list[tuple[str, str, ast.Call]] = []
    for path in sorted(_APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "say(" not in text:
            continue
        rel = path.relative_to(_APP).as_posix()

        def walk(node: ast.AST, func: str, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                name = func
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                    name = child.name
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and (child.func.id in _SAY_FUNCTIONS or (rel, child.func.id) in _SAY_WRAPPERS)
                    and not _forwards(child)
                ):
                    found.append((rel, name, child))
                walk(child, name)

        walk(ast.parse(text), "<module>")
    return found


def _forwards(call: ast.Call) -> bool:
    """A wrapper's own ``say(en=en, tr=tr, **values)``: en/tr are its parameters (or halves of
    a pair parameter), not copy, and the values come from its caller."""
    sides = [k.value for k in call.keywords if k.arg in ("en", "tr")]
    spread = any(k.arg is None for k in call.keywords)
    return spread and len(sides) == 2 and not any(isinstance(v, ast.Constant) for v in sides)


def _load_build() -> ModuleType:
    name = "build_copy_catalog"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _BUILD)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


@cache
def _index() -> object:
    build = _load_build()
    return build, build.Index(build.app_sources())


def _texts(where: str, node: ast.AST) -> list[str] | None:
    """The templates a templated en/tr can be: a string literal, a module constant (also
    imported) holding one, each branch of ``A if c else B``, or every entry of a module table
    read as ``T[k]`` / ``T[k][i]`` (whole sentences per value, so German gets whole sentences,
    KRI-1408). None when one of them is not a fixed string (it would not be a catalog key)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body, orelse = _texts(where, node.body), _texts(where, node.orelse)
        return None if body is None or orelse is None else [*body, *orelse]
    build, index = _index()
    mod = index.modules[build.module_name("app/" + where.rsplit(":", 1)[0])]
    if isinstance(node, ast.Name):
        try:
            value = index.resolve_name(mod, node.id)
        except build.Unresolved:
            return None
        return [value] if isinstance(value, str) else None
    part = None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Subscript):
        if not (isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int)):
            return None
        part, node = node.slice.value, node.value
    if not (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name)):
        return None
    try:
        table = index.resolve_name(mod, node.value.id)
    except build.Unresolved:
        return None
    if not isinstance(table, build.TableRef):
        return None
    out = []
    for _key, value, _line, problem in index.table_entries(table):
        picked = value if part is None else (value[part] if isinstance(value, tuple) else None)
        if problem is not None or not isinstance(picked, str):
            return None
        out.append(picked)
    return out or None


_CALLS = _say_calls()


def _templated() -> list[tuple[str, ast.Call]]:
    out = []
    for rel, _func, call in _CALLS:
        if any(k.arg not in ("en", "tr") for k in call.keywords):
            out.append((f"{rel}:{call.lineno}", call))
    return out


_TEMPLATED = _templated()


def _kw(call: ast.Call, name: str) -> ast.AST:
    return next(k.value for k in call.keywords if k.arg == name)


def _fields(template: str) -> list[tuple[str, str | None, str | None]]:
    return [
        (name, conv, spec)
        for _lit, name, spec, conv in string.Formatter().parse(template)
        if name is not None
    ]


def test_the_scan_sees_the_rewritten_calls() -> None:
    assert len(_CALLS) > 600
    assert len(_TEMPLATED) > 450


def test_no_say_call_builds_its_english_with_an_fstring_unless_known() -> None:
    """(6) An f-string en/tr changes its catalog key per turn; only the known skips may."""
    still = {
        (rel, func)
        for rel, func, call in _CALLS
        if any(k.arg in ("en", "tr") and isinstance(k.value, ast.JoinedStr) for k in call.keywords)
    }
    assert still - _KNOWN_FSTRING_CALLS == set(), "new f-string say(): use a template + values"
    stale = _KNOWN_FSTRING_CALLS - still
    assert not stale, f"converted since: drop from _KNOWN_FSTRING_CALLS: {sorted(stale)}"


@pytest.mark.parametrize(("where", "call"), _TEMPLATED, ids=[w for w, _ in _TEMPLATED])
def test_template_fields_match_the_values_passed(where: str, call: ast.Call) -> None:
    """(1) Every field has a value, every value is used, en/tr are plain string literals."""
    values = {k.arg for k in call.keywords if k.arg not in ("en", "tr")}
    assert None not in values, f"{where}: **kwargs hides which values a template gets"
    used: set[str] = set()
    for lang in ("en", "tr"):
        texts = _texts(where, _kw(call, lang))
        assert texts is not None, (
            f"{where}: templated {lang} must be a string literal, a module constant, a "
            "conditional of those or a module table of them (it is the catalog key)"
        )
        for text in texts:
            for name, _conv, spec in _fields(text):
                assert name and name.isidentifier(), f"{where}: {lang} field {name!r} not a name"
                assert name in values, f"{where}: {lang} uses {{{name}}} but the call lacks it"
                assert "{" not in (spec or ""), f"{where}: nested field in a format spec"
                used.add(name)
    assert used == values, f"{where}: values never used: {sorted(values - used)}"


def _formats(template: str, names: set[str]) -> None:
    """Fill ``template`` with dummy values the way say() does; any value type may do per field."""
    candidates: list[object] = ["x", 3, 1.5]
    for name, conv, spec in _fields(template):
        ok = False
        for dummy in candidates:
            try:
                ("{v" + (f"!{conv}" if conv else "") + (f":{spec}" if spec else "") + "}").format(
                    v=dummy
                )
            except (ValueError, TypeError):
                continue
            ok = True
            break
        assert ok, f"no value fits {{{name}!{conv}:{spec}}}"
    # The whole template with a value of each type: no KeyError / IndexError ever.
    for dummy in candidates:
        try:
            template.format(**dict.fromkeys(names, dummy))
        except (ValueError, TypeError):
            continue  # a spec rejecting this dummy type; checked per field above


@pytest.mark.parametrize(("where", "call"), _TEMPLATED, ids=[w for w, _ in _TEMPLATED])
def test_every_template_formats(where: str, call: ast.Call) -> None:
    """(3, 5) str.format never raises KeyError/IndexError on a template say() fills."""
    values = {k.arg for k in call.keywords if k.arg not in ("en", "tr")}
    for lang in ("en", "tr"):
        for text in _texts(where, _kw(call, lang)) or []:
            _formats(text, values)


def _as_fstring(template: str) -> str:
    """The f-string source a template stands for, reading each field from a same-named local."""
    parts = []
    for literal, name, spec, conv in string.Formatter().parse(template):
        parts.append(literal.replace("{", "{{").replace("}", "}}"))
        if name is not None:
            parts.append(
                "{" + name + (f"!{conv}" if conv else "") + (f":{spec}" if spec else "") + "}"
            )
    return "f" + repr("".join(parts))


@pytest.mark.parametrize(("where", "call"), _TEMPLATED, ids=[w for w, _ in _TEMPLATED])
def test_str_format_gives_the_fstring_text(where: str, call: ast.Call) -> None:
    """(2, 3) Filling a template equals evaluating the f-string it replaced, for each value type."""
    values = {k.arg for k in call.keywords if k.arg not in ("en", "tr")}
    for lang in ("en", "tr"):
        for template in _texts(where, _kw(call, lang)) or []:
            source = _as_fstring(template)
            for dummy in ("x", 3, 1.5, ["a", "b"], None):
                env = dict.fromkeys(values, dummy)
                try:
                    expected = eval(source, {}, env)  # noqa: S307 - our own literal, no input
                except (ValueError, TypeError):
                    with pytest.raises((ValueError, TypeError)):
                        template.format(**env)
                    continue
                assert template.format(**env) == expected, f"{where}: {lang} differs, {dummy!r}"


# ---------------------------------------------------------------- say() itself


def test_say_fills_the_template_in_english_when_unbound() -> None:
    assert say(en="No pop-in for {names}", tr="{names} için pop-in yok", names="A, B") == (
        "No pop-in for A, B"
    )


def test_say_fills_the_template_in_each_bound_language() -> None:
    kwargs = {"en": "Cut {n:g}s from {who!r}", "tr": "{who!r} içinden {n:g} sn kesildi"}
    with reply_language_for("en"):
        assert say(**kwargs, n=2.0, who="intro") == "Cut 2s from 'intro'"
    with reply_language_for("tr"):
        assert say(**kwargs, n=2.0, who="intro") == "'intro' içinden 2 sn kesildi"


def test_say_without_values_returns_the_text_untouched() -> None:
    # No values: not a template, so braces are plain text (the pre-B behaviour).
    assert say(en="keep {this}", tr="bunu {tut}") == "keep {this}"


def test_literal_braces_round_trip() -> None:
    with reply_language_for("tr"):
        assert say(en="{{x}} = {x}", tr="{{x}} şu: {x}", x=1) == "{x} şu: 1"
    assert say(en="{{x}} = {x}", tr="{{x}} şu: {x}", x=1) == "{x} = 1"


@pytest.fixture
def german_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_german_replies_enabled", True)


def test_german_catalog_hit_is_keyed_by_the_template(
    german_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    table = {"No pop-in for {names}": "Kein Pop-in für {names}"}
    seen: list[str] = []

    def fake_catalog(language: str) -> dict[str, str]:
        seen.append(language)
        return table if language == "de" else {}

    monkeypatch.setattr(copy_catalog, "catalog", fake_catalog)
    with reply_language_for("de"):
        text = say(en="No pop-in for {names}", tr="{names} için pop-in yok", names="A und B")
    assert text == "Kein Pop-in für A und B"
    assert seen == ["de"]


def test_german_catalog_miss_falls_back_to_english(
    german_on: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(copy_catalog, "catalog", lambda language: {})
    with reply_language_for("de"):
        assert say(en="Cut {n} clips", tr="{n} klip kesildi", n=3) == "Cut 3 clips"
        assert say(en="Done", tr="Tamam") == "Done"


def _option_calls() -> list[tuple[str, ast.Call]]:
    path = _APP / "services" / "choice_questions.py"
    found = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_option"
            and any(k.arg == "values" for k in node.keywords)
        ):
            found.append((f"services/choice_questions.py:{node.lineno}", node))
    return found


_OPTION_CALLS = _option_calls()


def test_option_calls_with_values_are_scanned() -> None:
    assert len(_OPTION_CALLS) >= 8


@pytest.mark.parametrize(("where", "call"), _OPTION_CALLS, ids=[w for w, _ in _OPTION_CALLS])
def test_option_templates_match_their_values(where: str, call: ast.Call) -> None:
    """(1, 5) ``_option(label=(en, tr), description=(en, tr), values={...})``: every field in a
    literal label/description has a value, and each value is used by some template."""
    values_node = next(k.value for k in call.keywords if k.arg == "values")
    assert isinstance(values_node, ast.Dict), f"{where}: values must be a dict literal"
    names = {k.value for k in values_node.keys if isinstance(k, ast.Constant)}
    assert len(names) == len(values_node.keys), f"{where}: values keys must be literals"
    used: set[str] = set()
    for arg in ("label", "description"):
        pair = next((k.value for k in call.keywords if k.arg == arg), None)
        if not isinstance(pair, ast.Tuple):
            continue
        for side in pair.elts:
            if not (isinstance(side, ast.Constant) and isinstance(side.value, str)):
                continue  # a template picked in a local (``natural_description``)
            for name, _conv, _spec in _fields(side.value):
                assert name in names, f"{where}: {{{name}}} has no value"
                used.add(name)
            _formats(side.value, names)
    assert used <= names
    assert used, f"{where}: values given but no literal template uses them"
