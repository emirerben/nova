"""German chat replies carry no English words the code filled in (KRI-1408, Emir's "1B").

ylab build 1977 (thread 8044373b, 2026-11-13), a German chat. The first hold reply said
"sie sind auf **German**; Ich schreibe Usersmagic, Nike, ... so, wie du **them** geschrieben
hast, und prüfe **them** im fertigen Video". ``say()`` looks a German sentence up by its English
template, then fills the ``{fields}`` from values the code computed; those values were English
("German", "it"/"them"), so no translation of the template could fix it. The KRI-1362 review
sheet listed 125 such lines.

Ways this can go wrong, written down before the fix:

1. A ``{field}`` the English template uses is filled with an English word or grammar bit the
   code picks (``"s"``, ``"it"``/``"them"``, ``"is"``/``"are"``, ``"first"``/``"last"``,
   ``"top"``), directly or through a local variable: German keeps the field, so it shows.
2. A field filled from an English-only value: a name ending in ``_en`` (``instead_en``), ``en``,
   or ``.en``.
3. A field run through ``str.capitalize()``/``.title()``, which lowercases German nouns
   ("Alle texte").
4. A ``say()`` call the catalog harvester cannot read (an f-string, a concatenation, a name
   bound twice, a call): no German line can exist, so the whole sentence stays English.
5. A shared helper that names a language for many sentences returning English in a German chat
   ("auf German").
6. The KRI-1408 line itself: "German", "them", a capital "Ich" after a semicolon.

(1)-(3) read every ``say()``/``say_in()`` call in ``app/`` (``ast``); a value built by a nested
``say()`` is the bound language's own text and passes. (4) is the harvester's skip list. (5)-(6)
run the real receipt with German bound and the shipped ``de.json``.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import string
import sys
from collections import defaultdict
from collections.abc import Iterator
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

from app.config import settings
from app.kria import reply_language as reply_language_module
from app.kria.brief import BriefRequirement

_API = Path(__file__).resolve().parents[2]
_APP = _API / "app"
_REPO = _API.parents[2]
_SAY = frozenset({"say", "say_in"})
_LETTER = re.compile(r"[^\W\d_]")
_ENGLISH_NAME = re.compile(r"(^|_)(en|english)$")
_CASE_CHANGERS = frozenset({"capitalize", "title"})


def _callee(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _literals(node: ast.AST | None) -> list[str] | None:
    """The English template(s) of an ``en=`` argument, or None when it is not literal."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body, orelse = _literals(node.body), _literals(node.orelse)
        if body is not None and orelse is not None:
            return body + orelse
    return None


def _fields(template: str) -> set[str]:
    names: set[str] = set()
    for _lit, name, _spec, _conv in string.Formatter().parse(template):
        if name:
            names.add(re.split(r"[.\[]", name, maxsplit=1)[0])
    return names


def _assignments(tree: ast.Module) -> dict[ast.AST, dict[str, list[ast.AST]]]:
    """Per function: local name -> the expressions assigned to it."""
    out: dict[ast.AST, dict[str, list[ast.AST]]] = {}
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        bound: dict[str, list[ast.AST]] = defaultdict(list)
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound[target.id].append(node.value)
            elif isinstance(node, ast.AnnAssign | ast.AugAssign) and node.value is not None:
                if isinstance(node.target, ast.Name):
                    bound[node.target.id].append(node.value)
        out[func] = bound
    return out


def _english_in(node: ast.AST, local: dict[str, list[ast.AST]], depth: int = 0) -> list[str]:
    """What in a value expression can put English (or a case change) into a German reply."""
    found: list[str] = []

    def visit(n: ast.AST, depth: int) -> None:
        if isinstance(n, ast.Call):
            name = _callee(n)
            if name in _SAY:
                return  # the bound language's own text
            if name in _CASE_CHANGERS:
                found.append(f".{name}()")
            if name == "get" and isinstance(n.func, ast.Attribute):
                visit(n.func.value, depth)  # a dict key is not text the creator reads
                for arg in n.args[1:]:
                    visit(arg, depth)
                return
        if isinstance(n, ast.Subscript):
            visit(n.value, depth)
            return
        if isinstance(n, ast.FormattedValue):
            visit(n.value, depth)  # a format spec (".1f") is not text
            return
        if isinstance(n, ast.Compare):
            return  # "x == 'top'" decides, it does not print
        if isinstance(n, ast.IfExp):
            visit(n.body, depth)
            visit(n.orelse, depth)
            return
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _LETTER.search(n.value):
            found.append(repr(n.value))
        if isinstance(n, ast.Name):
            if _ENGLISH_NAME.search(n.id):
                found.append(n.id)
            elif depth < 1 and n.id in local:
                for value in local[n.id]:
                    visit(value, depth + 1)
        if isinstance(n, ast.Attribute) and _ENGLISH_NAME.search(n.attr):
            found.append(f".{n.attr}")
        for child in ast.iter_child_nodes(n):
            if not isinstance(n, ast.Attribute | ast.Name):
                visit(child, depth)

    visit(node, depth)
    return found


def _enclosing(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    owner: dict[ast.AST, ast.AST] = {}

    def walk(node: ast.AST, func: ast.AST | None) -> None:
        for child in ast.iter_child_nodes(node):
            here = child if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) else func
            if func is not None:
                owner[child] = func
            walk(child, here)

    walk(tree, None)
    return owner


@cache
def _english_values() -> tuple[str, ...]:
    problems: list[str] = []
    for path in sorted(_APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "say(" not in source and "say_in(" not in source:
            continue
        tree = ast.parse(source)
        locals_by_func = _assignments(tree)
        owner = _enclosing(tree)
        rel = path.relative_to(_API)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _callee(node) in _SAY):
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            templates = _literals(keywords.get("en"))
            if templates is None:
                used = set(keywords) - {"en", "tr", "language"}
            else:
                used = set().union(*(_fields(t) for t in templates))
            local = locals_by_func.get(owner.get(node), {})
            for name in sorted(used & set(keywords)):
                found = _english_in(keywords[name], local)
                if found:
                    problems.append(f"{rel}:{node.lineno} {{{name}}} <- {', '.join(found)}")
    return tuple(problems)


def test_english_templates_get_no_english_from_the_code() -> None:
    """(1)-(3): every field an English template names is filled by text the bound language
    wrote (a nested ``say()``), creator data, or numbers; never an English word the code picks.
    Split the sentence per case instead (``say(en="... it ...") if one else say(en="... them
    ...")``), or pass the word through ``say()``."""
    problems = _english_values()
    assert not problems, f"{len(problems)} field(s) filled with English:\n" + "\n".join(problems)


def _load_build() -> ModuleType:
    name = "build_copy_catalog"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _REPO / "scripts" / "kria" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_every_say_call_has_a_catalog_line() -> None:
    """(4): the harvester reads every ``say()`` call, so each sentence has a German line."""
    build = _load_build()
    skipped = [
        f"{s.file}:{s.line} {s.reason}"
        for s in build.harvest(build.app_sources()).skipped
        if not s.reason.startswith("table ")
    ]
    assert not skipped, f"{len(skipped)} say() call(s) German cannot reach:\n" + "\n".join(skipped)


# ---------------------------------------------------------------- the real German text


@pytest.fixture
def german() -> Iterator[None]:
    """German bound with the shipped ``de.json`` (the switch is read by ``bind`` only)."""
    token = reply_language_module._CURRENT.set("de")
    try:
        yield
    finally:
        reply_language_module._CURRENT.reset(token)


@pytest.fixture
def phone_talking_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "subtitled_archetype_enabled", True)


_ENGLISH_WORDS = re.compile(r"\b(German|English|Turkish|them|it|they|their|is|are|the|and|of)\b")


def _ylab_reason(description: str) -> str:
    import dataclasses

    from app.kria.brief_checks import build_receipts, plan_facts_from_strategy

    req = BriefRequirement.model_validate(
        {
            "id": "r6",
            "kind": "style",
            "facts": {"captions": "on"},
            "scope": "global",
            "status": "open",
            "literal": None,
            "description": description,
            "source_turn_id": "8044373b-b401-4b03-b13f-60d623e06ddb",
        }
    )
    facts = dataclasses.replace(
        plan_facts_from_strategy({"edit_format": "subtitled", "audio_strategy": "original_audio"}),
        phone_talking=True,
        caption_words_at_render=True,
    )
    [receipt] = build_receipts([req], facts, include_unchecked=True)
    return receipt.reason or ""


@pytest.mark.parametrize(
    ("description", "spelled"),
    [
        (
            "German captions for every voice, spell these right: Usersmagic, Nike, Delivery Hero",
            True,
        ),
        ("German captions for every voice, spell this right: Usersmagic", True),
        ("German captions for every voice", False),
    ],
    ids=["many-names", "one-name", "no-names"],
)
def test_kri1408_ylab_caption_line_reads_german(
    german: None, phone_talking_on: None, description: str, spelled: bool
) -> None:
    """(5)-(6): the line from the ylab hold reply, in a German chat."""
    reason = _ylab_reason(description)
    assert "Deutsch" in reason, reason
    assert not _ENGLISH_WORDS.search(reason), reason
    assert "; Ich" not in reason, reason
    if spelled:
        assert "Usersmagic" in reason


def test_language_names_read_german(german: None) -> None:
    from app.kria.brief_checks import _language_name

    assert [_language_name(code) for code in ("de", "en", "tr")] == [
        "Deutsch",
        "Englisch",
        "Türkisch",
    ]
