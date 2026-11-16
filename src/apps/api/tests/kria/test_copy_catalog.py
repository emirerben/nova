"""``app/kria/copy/tr.json``: the code's Turkish chat sentences, keyed by English (KRI-1362, "B").

``scripts/kria/build_copy_catalog.py`` harvests every ``say(en=..., tr=...)`` pair (literals,
templates, constant names, same-condition conditionals, ``*_TR`` tables, wrapper functions) into
``tr.json``; a catalog language (German first) is then translated key by key, and ``say()`` looks
a sentence up by its English text. Ways this can go wrong, written down before the tests:

1. A harvested key that never matches what ``say()`` looks up at runtime: the harvester resolves a
   constant name, a table entry or a wrapper argument to a different string than the imported
   module holds, so the catalog line is dead and German falls back to English.
2. A table key that is a name the harvester did not resolve (``_REASON_TR = {_NO_TITLE: ...}``),
   dropped silently instead of listed, or resolved to the wrong constant.
3. ``tr.json`` drifting from the code: a ``tr=`` text edited (or a sentence added) without
   regenerating, so the mirror German is translated from is stale.
4. A template key stored with values filled in (``"No pop-in for A, B"``) instead of the template
   (``"No pop-in for {names}"``), so no turn's lookup ever matches it.
5. Conflicts silently collapsed: one English key with two Turkish texts (context-dependent copy)
   reduced to one without a trace, so a translator never sees that the key needs two meanings.
6. German falling back to English for a key that exists: ``say()`` not consulting the catalog for
   a catalog language, consulting it with the filled sentence, or the pseudo language ``qps``
   never reaching ``translate`` (so the "is this still hard-coded?" check proves nothing).
7. The codemod that turned f-strings into templates changing an (en, tr) pair: the set of pairs
   before the rewrite (base revision, f-strings converted the codemod's own way) differs from
   HEAD's.
8. New hard-coded creator copy (not through ``say()``) slipping into the builders the chat uses:
   under ``qps`` it shows up unwrapped.

(1)-(2) and (5) are checked against the imported modules, (3) by ``--check``, (4) by matching
every templated ``say()`` call's template to a key, (6)-(8) by running ``say()`` and real copy
builders.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import re
import subprocess
import sys
import uuid
from collections import Counter
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.kria import copy_catalog
from app.kria import reply_language as reply_language_module
from app.kria.reply_language import reply_language_for, say

_API = Path(__file__).resolve().parents[2]
_REPO = _API.parents[2]
_SCRIPTS = _REPO / "scripts" / "kria"
_TR_JSON = _API / "app" / "kria" / "copy" / "tr.json"
_CONFLICTS_JSON = _API / "app" / "kria" / "copy" / "conflicts.json"
_FIXTURES = Path(__file__).resolve().parent


def _load_script(name: str) -> ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


build = _load_script("build_copy_catalog")


@cache
def _harvest() -> Any:
    return build.harvest(build.app_sources())


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- (a) drift


def test_tr_json_matches_the_code() -> None:
    """``build_copy_catalog --check``: tr.json and conflicts.json are a fresh harvest."""
    done = subprocess.run(
        [sys.executable, str(_SCRIPTS / "build_copy_catalog.py"), "--check"],
        cwd=_REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr or done.stdout


def test_catalog_file_shape() -> None:
    data = _json(_TR_JSON)
    assert data["source"] == build.SOURCE_NOTE
    messages = data["messages"]
    assert list(messages) == sorted(messages)
    assert messages and all(isinstance(v, str) and v for v in messages.values())
    # what say() reads is exactly the file
    assert dict(copy_catalog.catalog("tr")) == messages


# ---------------------------------------------------------------- (b) runtime truth


def _runtime_value(module: str, name: str, index: int | None = None) -> Any:
    value = getattr(importlib.import_module(module), name)
    return value if index is None else value[index]


def _runtime_problems(pair: Any) -> list[str]:
    """Where the imported code disagrees with what the harvester read for ``pair``."""
    origin = pair.origin
    where = f"{pair.file}:{pair.line}"
    problems: list[str] = []
    if origin[0] == "names":
        for side, found, text in (("en", origin[1], pair.en), ("tr", origin[2], pair.tr)):
            if found is not None and _runtime_value(*found) != text:
                problems.append(f"{where}: {side} {found[0]}.{found[1]} is not {text!r}")
    elif origin[0] == "table":
        _, module, name, key = origin
        if _runtime_value(module, name).get(key) != pair.tr:
            problems.append(f"{where}: {name}.get({key!r}) is not {pair.tr!r}")
    elif origin[0] == "parallel":
        _, tr_module, tr_name, en_module, en_name, en_index, key = origin
        english = _runtime_value(en_module, en_name)[key]
        if en_index is not None:
            english = english[en_index]
        if english != pair.en:
            problems.append(f"{where}: {en_name}[{key!r}] is not {pair.en!r}")
        if _runtime_value(tr_module, tr_name)[key] != pair.tr:
            problems.append(f"{where}: {tr_name}[{key!r}] is not {pair.tr!r}")
    elif origin[0] == "pairs":
        _, module, name, key, en_index, tr_index = origin
        if name.startswith("<"):
            return problems  # a dict literal at the call site: read from the AST only
        row = _runtime_value(module, name)[key]
        if (row[en_index], row[tr_index]) != (pair.en, pair.tr):
            problems.append(f"{where}: {name}[{key!r}] is not {(pair.en, pair.tr)!r}")
    else:
        assert origin == ("say",), origin
    return problems


def test_every_pair_is_what_the_imported_code_holds() -> None:
    """Constant names, table entries and parallel tables read back the same through import."""
    problems = [p for pair in _harvest().pairs for p in _runtime_problems(pair)]
    assert problems == []


def test_every_catalog_value_is_what_say_returns_in_turkish() -> None:
    """For each harvested place, ``say()`` bound to Turkish returns that place's Turkish, and
    tr.json holds it, or the key is a listed conflict naming that exact place."""
    messages = _json(_TR_JSON)["messages"]
    conflicts = _json(_CONFLICTS_JSON)["conflicts"]
    wrong: list[str] = []
    with reply_language_for("tr"):
        for pair in _harvest().pairs:
            assert say(en=pair.en, tr=pair.tr) == pair.tr
            if messages.get(pair.en) == pair.tr:
                continue
            listed = {(c["file"], c["line"], c["tr"]) for c in conflicts.get(pair.en, [])}
            if (pair.file, pair.line, pair.tr) not in listed:
                wrong.append(f"{pair.file}:{pair.line}: {pair.en!r}")
    assert wrong == []


def test_conflicts_are_listed_not_collapsed() -> None:
    """Every key with two Turkish texts is in conflicts.json with all its places; the first
    place in file:line order is the one tr.json keeps."""
    by_key: dict[str, set[str]] = {}
    for pair in _harvest().pairs:
        by_key.setdefault(pair.en, set()).add(pair.tr)
    expected = {k for k, texts in by_key.items() if len(texts) > 1}
    conflicts = _json(_CONFLICTS_JSON)["conflicts"]
    messages = _json(_TR_JSON)["messages"]
    assert set(conflicts) == expected
    for key, places in conflicts.items():
        assert {p["tr"] for p in places} == by_key[key]
        assert messages[key] == places[0]["tr"]


def test_table_keys_are_resolved_names_not_identifiers() -> None:
    """``_REASON_TR = {_NO_TITLE: ...}``: the key is the constant's text, never its name, and an
    entry the harvester cannot read is listed, not dropped."""
    from app.kria import brief_checks

    messages = _json(_TR_JSON)["messages"]
    for key, turkish in brief_checks._REASON_TR.items():
        assert messages.get(key) == turkish, key
    identifiers = [k for k in messages if re.fullmatch(r"_?[A-Z][A-Z0-9_]+", k)]
    assert identifiers == []
    table_skips = [s for s in _harvest().skipped if s.reason.startswith("table ")]
    for skip in table_skips:
        assert skip.line > 0, skip


def _templated_say_calls() -> list[tuple[str, str]]:
    calls = []
    for path in sorted((_API / "app").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "say(" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if not build._is_say(node):
                continue
            names = {k.arg for k in node.keywords}
            en = build._kw(node, "en")
            if names - {"en", "tr"} and isinstance(en, ast.Constant):
                calls.append((f"{path.relative_to(_API)}:{node.lineno}", en.value))
    return calls


def test_templated_calls_are_keyed_by_their_template() -> None:
    """A ``say(en="No pop-in for {names}", ..., names=x)`` key is the template itself."""
    messages = _json(_TR_JSON)["messages"]
    calls = _templated_say_calls()
    assert len(calls) > 400  # the codemod's ~495 rewritten calls
    missing = [where for where, template in calls if template not in messages]
    assert missing == []


def test_german_finds_every_key_in_its_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """A catalog language with a line for every key never falls back to English."""
    messages = _json(_TR_JSON)["messages"]
    german = {key: f"DE<{key}>" for key in messages}
    monkeypatch.setattr(
        copy_catalog, "catalog", lambda language: german if language == "de" else messages
    )
    token = reply_language_module._CURRENT.set("de")
    try:
        missed = [key for key in messages if say(en=key, tr=messages[key]) != german[key]]
    finally:
        reply_language_module._CURRENT.reset(token)
    assert missed == []


# ---------------------------------------------------------------- (c) codemod snapshot


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=_REPO, check=True, capture_output=True, text=True
    ).stdout


def _merge_base() -> str:
    try:
        return _git("merge-base", "HEAD", "origin/main").strip()
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:  # shallow CI clone
        pytest.skip(f"no merge-base with origin/main: {exc}")


def _sources_at(rev: str) -> dict[str, str]:
    prefix = "src/apps/api/"
    names = [
        n
        for n in _git("ls-tree", "-r", "--name-only", rev, "--", prefix + "app").split("\n")
        if n.endswith(".py")
    ]
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=_REPO,
        input="".join(f"{rev}:{n}\n" for n in names).encode(),
        capture_output=True,
        check=True,
    ).stdout
    out: dict[str, str] = {}
    pos = 0
    for name in names:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        body = batch[header_end + 1 : header_end + 1 + size]
        pos = header_end + 1 + size + 1
        out[name[len(prefix) :]] = body.decode("utf-8")
    return out


def _sentence_counts(result: Any) -> Counter:
    # Not the file either: moving a table to another module (the one job-failure reason table,
    # KRI-1382) keeps every sentence pair, and that is what must not be lost.
    return Counter((p.en, p.tr) for p in result.pairs)


# Pairs the branch adds on purpose: copy that picked Turkish outside say() (``language == "tr"``
# branches, English-keyed ``*_TR`` tables, concatenations, f-strings) now routed through it so a
# catalog language reaches it (KRI-1362). Byte-identical English/Turkish is pinned per builder in
# test_copy_routing.py. Once merged, the merge base holds them and this list matches nothing.
_ROUTED_PAIRS = _FIXTURES / "copy_routed_pairs.json"


# Converts every say() file at the merge base (~30 s locally): above the 30 s default.
@pytest.mark.timeout(180)
def test_codemod_kept_every_en_tr_pair() -> None:
    """The (en, tr) pairs at the merge base, f-strings converted by the codemod's own function,
    are all still HEAD's pairs: the template rewrite changed no sentence. HEAD may only add the
    pinned routed pairs."""
    codemod = _load_script("say_templates_codemod")
    base = _sources_at(_merge_base())
    converted = {}
    for rel, text in base.items():
        new = None
        if "say(" in text:
            new = codemod.convert_source(_API / rel, text, codemod.Report())
        converted[rel] = new if new is not None else text
    before = _sentence_counts(build.harvest(converted))
    after = _sentence_counts(_harvest())
    # A sentence the chat stopped saying on purpose is listed, never silently lost.
    retired = {(row[1], row[2]) for row in _json(_ROUTED_PAIRS).get("retired", [])}
    assert {k: v for k, v in (before - after).items() if k not in retired} == {}
    routed = {(row[1], row[2]) for row in _json(_ROUTED_PAIRS)["pairs"]}
    added = set(after - before)
    assert added - routed == set(), "new pairs: route copy through say() or pin them"
    assert {row for row in before if row not in retired} <= set(after)


# ---------------------------------------------------------------- (d) pseudo language

# Text the builders below emit outside say(), today. A new entry means new hard-coded creator
# copy that a catalog language will not translate: route it through say() instead of adding it.
_KNOWN_UNWRAPPED: frozenset[str] = frozenset()

_BRIEF_CASES = (
    ("leverkusen_m2_run3.json", "brief", "strategy"),
    ("kri1068_leverkusen_m1.json", "brief_v1", "strategy"),
    ("lev_m1_run2_filming_order.json", "brief_v1", "strategy"),
    ("m3_kri1109_order.json", "brief_v4", "base_strategy"),
    ("m3_kri1110_labels.json", "brief_v3", "strategy_base"),
    ("m3_kri1112_listed_order.json", "brief_v5", "strategy_1339"),
    ("m3_kri1113_title_animation.json", "brief_v8", "strategy"),
)

# The status answers `app/kria/runtime.py` writes inline in the chat route (Postgres-bound, so
# their say() calls are exercised here by their exact text).
_STATUS_SENTENCES = (
    "I’m working on the edit plan. Your project is saved.",
    "Your draft is ready. Review the pinned approval before I start the render.",
    "Your approved render is in progress. Your draft is saved.",
    "There’s no edit running right now. Your latest project state is saved.",
)


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _unwrapped(text: str, data: list[str] = ()) -> list[str]:
    """Pieces of ``text`` outside ⟦…⟧ that still hold words, after removing creator data."""
    rest = re.sub(r"⟦.*?⟧", "\x00", text, flags=re.S)
    for piece in sorted({s for s in data if len(s) > 2}, key=len, reverse=True):
        rest = rest.replace(piece, "\x00")
    pieces = (p.strip() for p in re.split(r"[\x00\n]", rest))
    return [p for p in pieces if re.search(r"[^\W\d_]", p)]


@pytest.fixture
def pseudo() -> Any:
    """Bind ``qps`` directly: ``bind_reply_language`` only binds offered languages."""
    token = reply_language_module._CURRENT.set(copy_catalog.PSEUDO_LANGUAGE)
    try:
        yield
    finally:
        reply_language_module._CURRENT.reset(token)


def test_pseudo_language_reaches_the_catalog(pseudo: None) -> None:
    messages = _json(_TR_JSON)["messages"]
    key = "There’s no edit running right now. Your latest project state is saved."
    assert say(en=key, tr=messages[key]) == f"⟦{key}⟧"
    template = "No pop-in for {names_missing}"
    filled = say(en=template, tr=messages[template], names_missing="A")
    assert filled == "⟦No pop-in for A⟧"  # looked up by the template, then filled
    assert say(en="never harvested sentence", tr="hiç") == "never harvested sentence"


def test_status_sentences_are_catalog_keys(pseudo: None) -> None:
    source = (_API / "app" / "kria" / "runtime.py").read_text(encoding="utf-8")
    messages = _json(_TR_JSON)["messages"]
    for sentence in _STATUS_SENTENCES:
        assert sentence in source
        assert copy_catalog.is_pseudo_wrapped(say(en=sentence, tr=messages[sentence]))


def test_real_copy_builders_emit_only_catalog_sentences(pseudo: None) -> None:
    from app.kria.brief import CreativeBrief
    from app.kria.brief_checks import build_receipts, plan_facts_from_strategy, reply_from_receipts
    from app.kria.duplicate_send import DuplicateSend, duplicate_reply
    from app.services.choice_questions import simpler_version_question

    found: list[str] = []
    replies = 0
    for name, brief_key, strategy_key in _BRIEF_CASES:
        case = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
        brief = CreativeBrief.model_validate(case[brief_key])
        facts = plan_facts_from_strategy(case[strategy_key])
        receipts = build_receipts(brief.live(), facts, include_unchecked=True)
        reply = reply_from_receipts(brief, receipts)
        replies += "⟦" in reply
        creator_words = _strings(case[brief_key])
        found += _unwrapped(reply, creator_words)
        for receipt in receipts:
            found += _unwrapped(receipt.reason or "", creator_words)
    assert replies == len(_BRIEF_CASES)

    for fields in (None, ["title"]):
        question = simpler_version_question(fields)
        for option in question["options"]:
            assert copy_catalog.is_pseudo_wrapped(option["label"]), option
            found += _unwrapped(option["label"])
            found += _unwrapped(option.get("description") or "")

    for state in ("awaiting_approval", "rendering", "finished", "planning"):
        text = duplicate_reply(DuplicateSend(turn_id=uuid.uuid4(), state=state, review="Review"))
        found += _unwrapped(text, ["Review"])

    assert set(found) <= _KNOWN_UNWRAPPED, sorted(set(found) - _KNOWN_UNWRAPPED)
