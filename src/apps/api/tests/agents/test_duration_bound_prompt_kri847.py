"""KRI-847: both brief writers are told how to record "under N seconds".

The model, not the server, reads the creator's words: it records ``duration_bound: "at_most"``
next to ``duration_s`` for a ceiling, and the server only checks that field (KRI-667). Without
the instruction the field is never written and every ceiling stays "N seconds +/- 10 %". Failure
modes written first:

1. The Main Creator prompt lists "under 15 seconds" as a plain length (the old wording).
2. The Brief Extractor derives its prompt from the Main Creator section with exact-text
   ``.replace`` calls; one whose anchor no longer matches silently does nothing, and the
   extractor drifts from the Main Creator.
3. Only the English ceiling phrases are taught, so Turkish "en fazla 15 saniye" and
   "15 saniyenin altında" stay exact.
4. The instruction forgets that a later change must restate (or drop) the bound.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.agents import brief_extractor
from app.agents.brief_extractor import BriefExtractionInput, BriefExtractorAgent
from app.agents.main_creator import _BRIEF_PROMPT_SECTION, MainCreatorAgent
from tests.agents.test_main_creator_agent import _input

# Exclusive wording (the merged KRI-629 strict cap reads these from the description) and
# inclusive wording (the fact alone relaxes the lower side of the +/- 10 % check): the model
# records the same fact for both.
EXCLUSIVE_PHRASES = (
    "under 15 seconds",
    "less than 15 seconds",
    "shorter than 15 seconds",
    "15 saniyenin altında",
)
INCLUSIVE_PHRASES = (
    "at most 15 s",
    "max 15 s",
    "no more than 15 seconds",
    "up to 15 seconds",
    "15 seconds or less",
    "en fazla 15 saniye",
    "15 saniyeyi geçmesin",
)
CEILING_PHRASES = (*EXCLUSIVE_PHRASES, *INCLUSIVE_PHRASES)
BOUND_EXAMPLE = '{"duration_s": 15, "duration_bound": "at_most"}'


def _flat(text: str) -> str:
    return " ".join(text.split())


def _main_prompt() -> str:
    return MainCreatorAgent(None).render_prompt(  # type: ignore[arg-type]
        _input().model_copy(update={"brief_enabled": True})
    )


def _extractor_prompt() -> str:
    return BriefExtractorAgent(None).render_prompt(  # type: ignore[arg-type]
        BriefExtractionInput(creator_request="", user_message="x", conversation=[])
    )


@pytest.fixture(params=["main_creator", "brief_extractor"])
def prompt(request) -> str:
    return _flat(_main_prompt() if request.param == "main_creator" else _extractor_prompt())


def test_the_prompt_teaches_the_ceiling_fact_with_an_example(prompt):
    assert '"duration_bound"' in prompt
    assert BOUND_EXAMPLE in prompt


@pytest.mark.parametrize("phrase", CEILING_PHRASES)
def test_the_prompt_names_each_ceiling_phrasing(prompt, phrase):
    assert f'"{phrase}"' in prompt


def test_the_prompt_says_a_plain_length_gets_no_bound_and_a_change_restates_it(prompt):
    assert "omitting it means exact" in prompt
    assert "restate `duration_bound`" in prompt


def test_a_ceiling_is_no_longer_listed_among_the_plain_lengths(prompt):
    """The old line filed "under 15 seconds" as the number 15 with no direction."""
    assert '("a 60 second montage", "under 15 seconds"' not in prompt


# ------------------------------------------- the extractor's replace chain still matches


def _extractor_replacements() -> list[tuple[str, str]]:
    """Every ``.replace(old, new)`` that builds ``_NARROW_BRIEF_SECTION``, in the order applied."""
    tree = ast.parse(Path(brief_extractor.__file__).read_text())
    [chain] = [
        node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_NARROW_BRIEF_SECTION" for t in node.targets)
    ]
    pairs: list[tuple[str, str]] = []
    node = chain
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        assert node.func.attr == "replace"
        old, new = (ast.literal_eval(arg) for arg in node.args)
        pairs.append((old, new))
        node = node.func.value
    assert isinstance(node, ast.Name) and node.id == "_BRIEF_PROMPT_SECTION"
    return pairs[::-1]


def test_every_extractor_replace_anchor_matches_the_shared_section():
    """A ``.replace`` whose anchor moved is a silent no-op: the extractor keeps the Main
    Creator's wording where it should have its own."""
    text = _BRIEF_PROMPT_SECTION
    pairs = _extractor_replacements()
    assert len(pairs) >= 10  # the chain was found, not an empty walk
    for old, new in pairs:
        assert old in text, f"extractor anchor no longer matches: {old[:70]!r}"
        text = text.replace(old, new)
    assert brief_extractor._NARROW_BRIEF_SECTION.startswith(text)


def test_the_extractor_section_differs_from_the_shared_one_at_the_timing_rule():
    shared = _flat(_BRIEF_PROMPT_SECTION)
    narrow = _flat(brief_extractor._NARROW_BRIEF_SECTION)

    # The replaced opening is the extractor's own ...
    assert 'ALWAYS add a `timing` requirement with `scope`: "global"' in narrow
    assert 'ALWAYS add a `timing` requirement with `scope`: "global"' not in shared
    # ... and the ceiling instruction that follows it survived the replacement.
    assert BOUND_EXAMPLE in shared and BOUND_EXAMPLE in narrow
