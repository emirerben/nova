"""KRI-667 guard: no new regexes in the code that reads what a creator (or a model) meant.

Every regex that guesses meaning from words needs a twin per language (KRI-520 added ~170
Turkish ones in a week) and still misses paraphrases. The model that reads the message
records the meaning in a structured field instead (brief `facts`, the clip-intent `exclude`
op, the copilot's `claimed_ops`), and the server checks that field.

Two ratchets per module, both fail only when a count goes UP (KRI-786):

1. ``re.*`` calls (``BASELINE``).
2. Word-membership tests on lowered/folded text (``MEMBERSHIP_BASELINE``). The same
   English-only guess as a regex, written as ``"always" in text.casefold()``,
   ``any(m in folded for m in MARKERS)`` or ``folded.startswith("just")`` (map D49/D50).

   Heuristic, kept narrow so it stays low on false positives. A "folded" expression is a
   call to ``.lower()`` / ``.casefold()`` or to a text-fold helper (``fold_text``,
   ``loose_text``, ``loose_words``, ``text_fold.lower``, ``fold_tokens``, ``_fold*`` /
   ``*_fold``), anything derived from one by a
   str-call chain (``x.lower().strip()``, ``" ".join(x.casefold().split())``,
   ``set(fold_text(x).split())``), or a local name assigned from any of these inside the
   same function. Counted:
     - ``<str literal or loop name> in|not in <folded>`` (substring or word test), and
     - ``<folded>.startswith(...)`` / ``<folded>.endswith(...)``.
   Not counted: ``<folded> in {...}`` (exact match of the whole text against a set), and
   checks whose operand is a file extension or path, for ``in`` and the affix methods alike
   (a literal starting with ``.`` or containing ``/``, or a name like ``ext``,
   ``image_suffixes``, ``_IMAGE_FILE_EXTS``, or a loop name bound over one).

When a count goes DOWN the tests stay green and print a hint to lower the baseline, so lanes
that delete regexes in parallel never fail each other. A check that reads no creator or
model text (ids, file names, ffmpeg output) is exempt with ``# kri667: <why>`` on its line.
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

_API = Path(__file__).resolve().parents[2]
_APP = _API / "app"
_RE_FUNCS = frozenset(
    {"compile", "search", "match", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
)
_EXEMPT = "# kri667:"
_FOLD_METHODS = frozenset({"lower", "casefold"})
# app.text_fold helpers included: ``lower(text, "tr")`` / ``loose_words(text)`` are folded text.
_FOLD_FUNCS = frozenset(
    {
        "fold",
        "fold_case",
        "fold_text",
        "fold_tokens",
        "loose_case",
        "loose_creator_text",
        "loose_text",
        "loose_words",
        "lower",
    }
)
_AFFIX_METHODS = frozenset({"startswith", "endswith"})
# str calls that keep folded text folded (``x.lower().strip()`` is still folded text).
_CHAIN_METHODS = frozenset(
    {
        "strip",
        "lstrip",
        "rstrip",
        "replace",
        "split",
        "rsplit",
        "splitlines",
        "translate",
        "removeprefix",
        "removesuffix",
    }
)
_WRAPPERS = frozenset({"set", "frozenset", "list", "tuple", "sorted"})
_PATH_NAME = re.compile(
    r"(?:^|_)(?:ext|exts|extension|extensions|suffix|suffixes|mime|path|paths|filename)(?:$|_)",
    re.IGNORECASE,
)

# Modules outside app/kria that read creator or model wording, mapped to the top-level
# functions to scan (None = the whole module). Every app/kria module is scanned too; a new
# one starts at zero.
_OTHER_MODULES: dict[str, tuple[str, ...] | None] = {
    "agents/_schemas/creator_policy.py": None,
    "agents/brief_extractor.py": None,
    "agents/clip_intent_planner.py": None,
    "agents/clip_request_resolver.py": None,
    "agents/edit_copilot.py": None,
    # KRI-786 (parity map D30, D31): exclusive-quote and ungrounded-caption guards.
    "agents/edit_proposal.py": None,
    # KRI-786 (D47): model-refusal detectors.
    "agents/intro_writer.py": None,
    "agents/main_creator.py": None,
    # KRI-786 (D38): quote harvest.
    "agents/semantic_edit_proposal.py": None,
    "agents/spoken_script.py": None,
    # KRI-786 (D47).
    "agents/voiceover_script_writer.py": None,
    # KRI-786 (D42, D43): hook filler penalty, spoken score labels.
    "pipeline/narration_labels.py": None,
    "pipeline/score.py": None,
    "routes/_copilot.py": None,
    # KRI-786 (D01, O19): v1 chat routing.
    "routes/creation_threads.py": None,
    "routes/creator_agent.py": None,
    # KRI-786 (D11): draft-review timing opt-outs.
    "routes/plan_items.py": None,
    "schemas/edit_proposal.py": None,
    "services/choice_questions.py": None,
    "services/clip_intent_resolution.py": None,
    # KRI-796: reads where a creator seated a Visual among named moments, by folded words.
    "services/clip_order_sequence.py": None,
    "services/clip_selection.py": None,
    "services/creative_copy_decisions.py": None,
    "services/creator_direction.py": None,
    "services/creator_memory_learning.py": None,
    # KRI-786 (D32): generic-SFX notice.
    "services/creator_sessions.py": None,
    # KRI-786 (D50): "just this once" markers.
    "services/finished_style_learning.py": None,
    "services/kria_editor_ops_diff.py": None,
    "services/sfx_catalog.py": None,
    "services/spoken_quotes.py": None,
    # KRI-585: reads the clips' own speech for its spoken role, by folded tokens only.
    "services/spoken_roles.py": None,
    "services/visual_treatment_planner.py": None,
    # KRI-786 (D44, D45): only the narrated-storyboard readers of the creator brief; the
    # rest of this 25k-line module parses ids, paths and ffmpeg output.
    "tasks/generative_build.py": (
        "_creator_requests_narrated_treatment",
        "_narrated_storyboard_text_elements",
    ),
    "tasks/kria_runtime.py": None,
}

# re.* calls per module, recounted on main after Turkish parity lanes A-E merged (KRI-786).
# Lower a number when you delete regexes; never raise one.
BASELINE: dict[str, int] = {
    "agents/_schemas/creator_policy.py": 8,
    "agents/clip_intent_planner.py": 19,
    "agents/clip_request_resolver.py": 3,
    "agents/edit_copilot.py": 26,
    # +1 from the merged Turkish parity lanes (#1712-#1721).
    "agents/edit_proposal.py": 11,
    "agents/intro_writer.py": 20,
    "agents/main_creator.py": 4,
    "agents/semantic_edit_proposal.py": 1,
    "agents/spoken_script.py": 11,
    "agents/voiceover_script_writer.py": 9,
    "kria/brief.py": 27,
    "kria/brief_checks.py": 70,
    "kria/brief_route.py": 4,
    "kria/clip_scope.py": 20,
    "kria/editor_receipts.py": 48,
    "kria/language.py": 1,
    "kria/opening_cut_ask.py": 8,
    "kria/planner.py": 12,
    "kria/reply_language.py": 8,
    "kria/speaker_framing_ask.py": 29,
    "kria/strategy_policy.py": 4,
    "kria/style_asks.py": 22,
    "kria/voiceover_clips.py": 12,
    "pipeline/narration_labels.py": 2,
    "routes/_copilot.py": 8,
    "routes/creation_threads.py": 2,
    "routes/creator_agent.py": 39,
    "routes/plan_items.py": 6,
    "schemas/edit_proposal.py": 62,
    "services/choice_questions.py": 2,
    "services/clip_intent_resolution.py": 2,
    "services/clip_selection.py": 1,
    "services/creator_direction.py": 6,
    "services/creator_sessions.py": 1,
    "services/kria_editor_ops_diff.py": 3,
    "services/sfx_catalog.py": 2,
    "services/spoken_quotes.py": 2,
    "services/visual_treatment_planner.py": 8,
    # +1 from the merged Turkish parity lanes (#1712-#1721).
    "tasks/generative_build.py": 8,
}

# Word-membership tests on folded text per module (see the module docstring).
MEMBERSHIP_BASELINE: dict[str, int] = {
    "agents/edit_copilot.py": 1,
    "agents/main_creator.py": 2,
    "kria/brief_route.py": 1,
    "kria/editor_receipts.py": 1,
    "kria/planner.py": 1,
    # Lane D's (#1717) Turkish detector: ``loose in _TR_WORDS or loose.endswith(_TR_VERB_ENDINGS)``.
    "kria/reply_language.py": 1,
    # D42: hook filler penalty, English prefixes plus lane C2's (#1718) Turkish-hook checks.
    "pipeline/score.py": 3,
    "services/clip_intent_resolution.py": 2,
    "services/creator_direction.py": 3,
    "services/sfx_catalog.py": 1,
    # Lane C2 (#1718): ``transcript_language.startswith("tr")``, a language code, not wording.
    "tasks/generative_build.py": 1,
}


def _modules() -> list[tuple[str, Path, tuple[str, ...] | None]]:
    kria = [(str(p.relative_to(_APP)), p, None) for p in sorted((_APP / "kria").rglob("*.py"))]
    return [*kria, *((rel, _APP / rel, scope) for rel, scope in _OTHER_MODULES.items())]


def _scoped_roots(tree: ast.Module, scope: tuple[str, ...] | None) -> list[ast.AST]:
    if scope is None:
        return [tree]
    return [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in scope
    ]


def _walk_unique(roots: list[ast.AST]):
    seen: set[int] = set()
    for root in roots:
        for node in ast.walk(root):
            if id(node) not in seen:
                seen.add(id(node))
                yield node


def _is_regex_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "re"
        and node.func.attr in _RE_FUNCS
    )


def _callee_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _is_fold_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _callee_name(node)
    if name is None:
        return False
    if isinstance(node.func, ast.Attribute) and name in _FOLD_METHODS:
        return True
    # Exact helper names or a ``_fold`` prefix / ``_fold`` suffix: not any substring, so
    # ``scaffold()`` and the event folds (``fold_song_orders``) are not text folds.
    return name in _FOLD_FUNCS or name.startswith("_fold") or name.endswith("_fold")


def _is_folded(node: ast.AST, names: set[str]) -> bool:
    """True when ``node`` is folded text, or is derived from it by a chain of str calls.

    Walks the receiver chain, so ``x.lower().strip()``, ``fold_text(x).split()``,
    ``" ".join(x.casefold().split())``, ``set(x.casefold().split())`` and
    ``lowered[:40]`` all count.
    """
    if _is_fold_call(node) or (isinstance(node, ast.Name) and node.id in names):
        return True
    if isinstance(node, ast.Subscript):
        return _is_folded(node.value, names)
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Attribute):
        if func.attr in _CHAIN_METHODS:
            return _is_folded(func.value, names)
        if func.attr == "join" and node.args:
            return _is_folded_iterable(node.args[0], names)
        return False
    if isinstance(func, ast.Name) and func.id in _WRAPPERS and node.args:
        return _is_folded_iterable(node.args[0], names)
    return False


def _is_folded_iterable(node: ast.AST, names: set[str]) -> bool:
    if isinstance(node, ast.GeneratorExp | ast.ListComp | ast.SetComp):
        return any(_is_folded(gen.iter, names) for gen in node.generators)
    return _is_folded(node, names)


def _folded_names(func: ast.AST) -> set[str]:
    names: set[str] = set()
    assigns: list[tuple[list[str], ast.AST]] = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            assigns.append((targets, node.value))
        elif (
            isinstance(node, ast.AnnAssign | ast.NamedExpr)
            and node.value is not None
            and isinstance(node.target, ast.Name)
        ):
            assigns.append(([node.target.id], node.value))
    # Repeat until stable so ``a = x.lower(); b = a.strip()`` marks both names.
    changed = True
    while changed:
        changed = False
        for targets, value in assigns:
            if set(targets) <= names or not _is_folded(value, names):
                continue
            names.update(targets)
            changed = True
    return names


def _path_like_name(name: str) -> bool:
    return bool(_PATH_NAME.search(name))


def _path_like_literal(value: str) -> bool:
    return value.startswith(".") or "/" in value


def _path_like_operand(node: ast.AST, loop_iters: dict[str, ast.AST]) -> bool:
    """A file-extension or path operand: ``".mp4"``, ``ext``, ``image_suffixes``, or a loop
    name bound over ``_IMAGE_FILE_EXTS``."""
    literals = [
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    if literals and all(_path_like_literal(v) for v in literals):
        return True
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            if _path_like_name(n.id):
                return True
            bound = loop_iters.get(n.id)
            if bound is not None and _path_like_operand(bound, {}):
                return True
        elif isinstance(n, ast.Attribute) and _path_like_name(n.attr):
            return True
    return False


def _loop_iters(func: ast.AST) -> dict[str, ast.AST]:
    iters: dict[str, ast.AST] = {}
    for node in ast.walk(func):
        if isinstance(node, ast.comprehension | ast.For) and isinstance(node.target, ast.Name):
            iters[node.target.id] = node.iter
    return iters


def _membership_nodes(roots: list[ast.AST]) -> list[ast.AST]:
    found: list[ast.AST] = []
    funcs = [
        n
        for n in _walk_unique(roots)
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda)
    ]
    seen: set[int] = set()
    for func in funcs:
        names = _folded_names(func)
        iters = _loop_iters(func)
        for node in ast.walk(func):
            if id(node) in seen:
                continue
            if isinstance(node, ast.Compare):
                left = node.left
                left_ok = (
                    (isinstance(left, ast.Constant) and isinstance(left.value, str))
                    or isinstance(left, ast.Name)
                ) and not _path_like_operand(left, iters)
                for op, right in zip(node.ops, node.comparators, strict=True):
                    if left_ok and isinstance(op, ast.In | ast.NotIn) and _is_folded(right, names):
                        seen.add(id(node))
                        found.append(node)
                        break
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _AFFIX_METHODS
                and _is_folded(node.func.value, names)
                and not any(_path_like_operand(arg, iters) for arg in node.args)
            ):
                seen.add(id(node))
                found.append(node)
    return found


def _counts(path: Path, scope: tuple[str, ...] | None) -> tuple[int, int]:
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    roots = _scoped_roots(ast.parse(source), scope)

    def live(node: ast.AST) -> bool:
        return _EXEMPT not in lines[node.lineno - 1]

    regexes = sum(1 for n in _walk_unique(roots) if _is_regex_call(n) and live(n))
    membership = sum(1 for n in _membership_nodes(roots) if live(n))
    return regexes, membership


def _all_counts() -> dict[str, tuple[int, int]]:
    return {rel: _counts(path, scope) for rel, path, scope in _modules() if path.exists()}


def current_counts() -> dict[str, int]:
    return {rel: pair[0] for rel, pair in _all_counts().items()}


def current_membership_counts() -> dict[str, int]:
    return {rel: pair[1] for rel, pair in _all_counts().items()}


def _ratchet(counts: dict[str, int], baseline: dict[str, int], name: str) -> dict:
    grew = {
        rel: (count, baseline.get(rel, 0))
        for rel, count in counts.items()
        if count > baseline.get(rel, 0)
    }
    shrank = {
        rel: (counts.get(rel, 0), base)
        for rel, base in baseline.items()
        if counts.get(rel, 0) < base
    }
    if shrank:
        hint = ", ".join(f'"{rel}": {now}' for rel, (now, _) in sorted(shrank.items()))
        warnings.warn(
            f"KRI-667 {name}: counts went down, nice. Lower the baseline to lock it in: {hint}",
            stacklevel=2,
        )
    return grew


def test_every_scanned_module_exists():
    missing = [rel for rel in _OTHER_MODULES if not (_APP / rel).exists()]
    assert not missing, f"update _OTHER_MODULES: {missing}"


def test_scoped_functions_exist():
    missing = []
    for rel, scope in _OTHER_MODULES.items():
        if scope is None:
            continue
        tree = ast.parse((_APP / rel).read_text(encoding="utf-8"))
        names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        missing += [f"{rel}::{fn}" for fn in scope if fn not in names]
    assert not missing, f"scoped functions renamed or removed; update _OTHER_MODULES: {missing}"


def test_no_module_gains_a_regex_that_reads_meaning_from_words():
    grew = _ratchet(current_counts(), BASELINE, "BASELINE")
    assert not grew, (
        "KRI-667: these modules gained regexes (now, baseline): "
        f"{grew}. Don't guess what the creator meant from words; have the model that reads "
        "the message record it in a structured field (brief `facts`, clip-intent op, "
        "`claimed_ops`) and check that field. A regex that reads no creator or model text "
        "can carry `# kri667: <why>` on its line. See agents/DECISIONS.md 'Language policy'."
    )


def test_no_module_gains_a_word_membership_test_on_folded_text():
    grew = _ratchet(current_membership_counts(), MEMBERSHIP_BASELINE, "MEMBERSHIP_BASELINE")
    assert not grew, (
        "KRI-786: these modules gained `word in folded_text` / `.startswith(...)` checks "
        f"(now, baseline): {grew}. That is a regex by another name: it reads English (or "
        "Turkish) words and misses the other language. Trust a structured field the model "
        "returns, or use a shared bilingual helper (kria/language.py, kria/brief_route.py). "
        "A check that reads no creator or model text can carry `# kri667: <why>` on its line."
    )


def test_baselines_name_only_scanned_modules():
    scanned = {rel for rel, _, _ in _modules()}
    unknown = sorted((set(BASELINE) | set(MEMBERSHIP_BASELINE)) - scanned)
    assert not unknown, f"baseline names modules that are not scanned: {unknown}"
