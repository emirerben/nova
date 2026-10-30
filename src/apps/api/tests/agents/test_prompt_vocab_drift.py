"""Guard: AI-facing text never hand-lists part of a closed vocabulary.

PR #1898 added six text animations to ``TextAnimationEdge``; the chat editor kept telling
creators it couldn't do Word Relay because its prompt hand-listed the five older values. Every
prompt and AI-layer literal now renders or derives those lists from the registry
(``app.agents.prompt_vocab``). This test fails when a line or literal names three or more
members of a registered vocabulary but not all of them.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.agents import editor_ops_v2
from app.agents._schemas.text_animation_phases import TEXT_ANIMATION_VOCAB
from app.agents.prompt_vocab import PROMPT_VOCABULARIES, render_vocab_tokens

API_ROOT = Path(__file__).resolve().parents[2]
_MIN_MEMBERS = 3

# (path, assignment target) -> why the partial list is the right shape there.
ALLOWED: dict[tuple[str, str], str] = {
    ("app/agents/_schemas/text_animation_phases.py", "*"): (
        "the registry, plus the legacy `effect` map designed edges never had"
    ),
    ("app/agents/_schemas/visual_editor.py", "entrance"): (
        "image overlays have their own edge set (zoom, no typewriter)"
    ),
    ("app/agents/_schemas/visual_editor.py", "exit"): (
        "image overlays have their own edge set (zoom, no typewriter)"
    ),
    ("app/kria/editor_receipts.py", "_ANIMATION"): (
        "loose stems (bare pop, slide\\w*) kept beyond the registry aliases it also matches"
    ),
}
# Members that are everyday words ("float seconds", "or none") count only when they sit in a
# list next to another member.
_GENERIC_WORDS = frozenset({"none", "float"})
_LIST_SEPARATOR = re.compile(
    r"""(?:[\s"'`|,/()\[\]?*+:;.\\-]|\\[wbs]|\b(?:or|and|ve|veya)\b)*""", re.IGNORECASE
)


def _scanned_files() -> Iterator[Path]:
    yield from sorted((API_ROOT / "prompts").rglob("*.txt"))
    for package in ("app/agents", "app/kria", "app/schemas"):
        yield from sorted((API_ROOT / package).rglob("*.py"))
    yield from sorted((API_ROOT / "app/services").glob("kria_*.py"))


def _word_pattern(member: str) -> re.Pattern[str]:
    # "wordRelay" is also written "word relay" / "Word-Relay"; hyphens join words, so
    # "fade-in" (a legacy `effect` value) is not a mention of "fade".
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", member).split()
    body = r"[ _-]?".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![\w-]){body}(?![\w-])", re.IGNORECASE)


_PATTERNS = {
    name: {member: _word_pattern(member) for member in vocab.members}
    for name, vocab in PROMPT_VOCABULARIES.items()
}


def _mentions_in_text(name: str, text: str) -> set[str]:
    spans = sorted(
        (match.start(), match.end(), member)
        for member, pattern in _PATTERNS[name].items()
        for match in pattern.finditer(text)
    )
    named = {member for _, _, member in spans if member not in _GENERIC_WORDS}
    for (_, end, left), (start, _, right) in zip(spans, spans[1:], strict=False):
        if left != right and _LIST_SEPARATOR.fullmatch(text[end:start]):
            named |= {left, right}
    return named


def _python_units(tree: ast.Module) -> Iterator[tuple[int, str, str, set[str] | str]]:
    """(line, owner, kind, payload): a string constant's text, or a collection's exact values."""
    owner_of: dict[int, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = next((t.id for t in targets if isinstance(t, ast.Name)), "")
            for child in ast.walk(node):
                owner_of.setdefault(id(child), name)
    for node in ast.walk(tree):
        owner = owner_of.get(id(node), "")
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, owner, "text", node.value
        elif isinstance(node, (ast.Set, ast.List, ast.Tuple, ast.Dict)):
            values = {
                child.value.strip()
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
            yield node.lineno, owner, "values", values


def _violations() -> list[str]:
    found: list[str] = []
    for path in _scanned_files():
        rel = path.relative_to(API_ROOT).as_posix()
        if (rel, "*") in ALLOWED:
            continue
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".txt":
            units = [(n, "", "text", line) for n, line in enumerate(source.splitlines(), 1)]
        else:
            units = list(_python_units(ast.parse(source)))
        for line, owner, kind, payload in units:
            if (rel, owner) in ALLOWED:
                continue
            for name, vocab in PROMPT_VOCABULARIES.items():
                named = (
                    _mentions_in_text(name, payload)
                    if kind == "text"
                    else payload & set(vocab.members)
                )
                if len(named) >= _MIN_MEMBERS and len(named) < len(vocab.members):
                    missing = [m for m in vocab.members if m not in named]
                    found.append(
                        f"{rel}:{line} hand-lists {name} ({', '.join(sorted(named))}) "
                        f"without {', '.join(missing)}"
                    )
    return found


def test_no_partial_hand_lists_of_a_registered_vocabulary() -> None:
    violations = sorted(set(_violations()))
    assert not violations, (
        "Render the list with a {{TOKEN}} (app/agents/prompt_vocab.py) or derive it from "
        "TEXT_ANIMATION_VOCAB instead of typing it; if a partial list is genuinely right, "
        "add it to ALLOWED with a reason.\n" + "\n".join(violations)
    )


@pytest.mark.parametrize("edge", list(TEXT_ANIMATION_VOCAB))
def test_rendered_edit_copilot_text_lane_offers_every_edge(edge: str) -> None:
    prompt = editor_ops_v2.prompt_fragments()
    assert "{{" not in prompt
    assert re.search(rf"(?<![\w-]){edge}(?![\w-])", prompt), f"text lane never offers {edge}"


def test_unknown_vocab_token_raises() -> None:
    with pytest.raises(KeyError, match="TEXT_ANIMATION_EDGEZ"):
        render_vocab_tokens("entrance: {{TEXT_ANIMATION_EDGEZ}}")
