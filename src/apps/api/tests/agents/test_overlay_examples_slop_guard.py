"""Guard: exemplars and recorded eval outputs must pass the transformation-slop lint.

plans/015 (2026-08-05, "the monkey changed my whole marketing perspective"
incident). The prompt bans the retrospective transformation / lesson-learned
frame as a PATTERN CLASS, but the exemplar library is part of the same prompt —
PR #338 shipped an exemplar ("this is what changed everything") that PR #507
later banned as a phrase, and nothing caught the contradiction. This module
makes that drift class impossible to repeat:

  1. every exemplar in overlay_examples.json passes `slop_structural_failures`
     (an exemplar that demonstrates a banned frame is a license to produce it);
  2. every recorded intro_writer eval fixture OUTPUT passes too (the structural
     floor in tests/evals/runners/structural.py enforces the same fn — this is
     the fast, fixture-local canary);
  3. the pattern fn itself is pinned: known-slop positives, boundary negatives,
     and the Turkish casefold trap (İ → i + U+0307) that breaks naive matching.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.agents.intro_writer import slop_structural_failures
from app.agents.overlay_examples import load_overlay_examples

_FIXTURES = (
    pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "agent_evals" / "intro_writer"
)


@pytest.mark.parametrize("example", load_overlay_examples(), ids=lambda e: e.id)
def test_every_exemplar_passes_slop_lint(example):
    failures = slop_structural_failures(example.text)
    assert not failures, (
        f"exemplar {example.id!r} text {example.text!r} matches slop patterns {failures} — "
        "an exemplar demonstrating a banned frame licenses the model to produce it "
        "(the PR #338/#507 drift class). Retext the exemplar."
    )


@pytest.mark.parametrize(
    "fixture_path",
    sorted(_FIXTURES.rglob("*.json")),
    ids=lambda p: f"{p.parent.name}/{p.stem}",
)
def test_every_recorded_intro_fixture_output_passes_slop_lint(fixture_path):
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    text = str((data.get("output") or {}).get("text") or "")
    assert text, f"{fixture_path.name}: fixture has no output.text"
    failures = slop_structural_failures(text)
    assert not failures, (
        f"{fixture_path.name}: recorded output {text!r} matches slop patterns {failures}"
    )


# The reported prod line — the signature of the whole class.
_MONKEY_LINE = "the monkey changed my whole marketing perspective"

_SLOP_POSITIVES = [
    (_MONKEY_LINE, {"en_changed_my", "en_perspective"}),
    ("this trip changed everything for us", {"en_changed_my"}),
    ("what this valley taught me about patience", {"en_taught_me"}),
    ("this morning made me realize what matters", {"en_made_realize"}),
    ("one bite reshaped my whole mindset", {"en_shifted_my", "en_perspective"}),
    ("this view opened my eyes", {"en_opened_eyes"}),
    # Transformation claim on purpose — even a plausible-sounding hook in this
    # frame is banned; the prompt should say "berlin at 6am was not the plan".
    ("changed my mind about berlin", {"en_changed_my"}),
    ("bu rutin hayatımı değiştirdi", {"tr_degistirdi"}),
    ("bu sabah bakış açımı değiştirdi", {"tr_degistirdi"}),
    ("bu yolculuk bana sabrı öğretti", {"tr_ogretti"}),
]

_CLEAN_NEGATIVES = [
    # In-the-moment / concrete lines the lint must NOT flag.
    "this bite changed plans",
    "nobody's awake but the first set isn't waiting",
    "thirty euro hostel and a view like this",
    "this is what my tuesdays look like now",
    "the before nobody posted",
    # Word-boundary traps (outside-voice #6): "my/our" must be whole words.
    "changed myth night at the hostel",
    "shifted mystery tour starts now",
    "keşke daha önce bilseydim",
    "pov: strangers became the plan",
]


@pytest.mark.parametrize(
    "text,expected", _SLOP_POSITIVES, ids=lambda v: v if isinstance(v, str) else ""
)
def test_slop_positives_match(text, expected):
    got = set(slop_structural_failures(text))
    assert expected <= got, f"{text!r}: expected at least {expected}, got {got}"


@pytest.mark.parametrize("text", _CLEAN_NEGATIVES)
def test_clean_negatives_pass(text):
    assert slop_structural_failures(text) == []


def test_turkish_uppercase_casefold_trap():
    # str.casefold() maps İ (U+0130) to "i" + U+0307 combining dot; without the
    # strip in _normalize_for_slop, this uppercase line would silently pass.
    assert slop_structural_failures("BU RUTİN HAYATIMI DEĞİŞTİRDİ") == ["tr_degistirdi"]


def test_empty_and_none_are_clean():
    assert slop_structural_failures("") == []
    assert slop_structural_failures(None) == []  # type: ignore[arg-type]
