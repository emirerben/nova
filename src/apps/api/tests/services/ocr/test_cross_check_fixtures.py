"""Regression test: cross-check vs hand-verified ground-truth fixtures.

The autobuilder's trust model is "two engines agree → commit; one engine
hallucinates → ideally caught by the threshold gate." This test pins that
behavior against the two fixtures the autobuilder is meant to reproduce:

* ``fdaf3bbc.json`` — hand-verified during the rich_in_life_v2 review (PR #203).
* ``rich_in_life_v2.json`` — hand-verified for the Layer-2 eval baseline.

Two questions to answer here:

1. Perfect-agreement sanity: if both engines return the fixture's
   tokens verbatim, cross-check must produce ``status="agreed"`` and
   the same token set.
2. Hallucination probe: if engine_a fabricates an extra token above the
   threshold, the UNION-based ``tokens`` output will contain it. Pin
   that behavior so any future swap to intersection is a visible diff,
   not silent.

This is a *behavior* lock — it does not validate that real OCR engines
produce these tokens against the source videos. That validation is a
follow-up that requires the source videos in CI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.ocr.cross_check import (
    DEFAULT_AGREEMENT_THRESHOLD,
    _cross_check_from_words,
)
from app.services.ocr.engines import OCRWord

_FIXTURES_DIR = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "fixtures"
    / "agent_evals"
    / "template_text"
    / "ground_truth"
)


def _expected_tokens(fixture_path: Path) -> set[str]:
    """Pull the case-folded token set out of a hand-verified fixture.

    Tokenizes ``sample_text`` from every overlay the same way
    ``cross_check._normalize_tokens`` does — lowercase, whitespace-split,
    empty filtered — so this test's expected set is comparable to the
    cross-check output by construction.
    """
    payload = json.loads(fixture_path.read_text())
    tokens: set[str] = set()
    for overlay in payload.get("overlays", []):
        text = (overlay.get("sample_text") or "").lower()
        for piece in text.split():
            piece = piece.strip()
            if piece:
                tokens.add(piece)
    return tokens


def _fake_words(tokens: list[str]) -> list[OCRWord]:
    """Materialize a token list into OCRWord records with placeholder bboxes.

    The cross-check only inspects ``text``; bboxes are ignored, so we
    use the same placeholder for every word.
    """
    return [
        OCRWord(text=t, bbox=(0.0, 0.0, 0.01, 0.01), confidence=0.9) for t in tokens
    ]


_FIXTURE_NAMES = ("rich_in_life_v2.json", "fdaf3bbc.json")


@pytest.fixture(params=_FIXTURE_NAMES, ids=lambda n: n.removesuffix(".json"))
def fixture_tokens(request) -> tuple[str, set[str]]:
    name = request.param
    path = _FIXTURES_DIR / name
    if not path.exists():
        pytest.skip(f"fixture {path} not present in this checkout")
    return name, _expected_tokens(path)


def test_perfect_agreement_against_fixture(fixture_tokens):
    """Both engines return the hand-verified token set → status='agreed'
    and ``tokens`` equals the fixture's set (case-folded, deduped)."""
    name, expected = fixture_tokens
    if not expected:
        pytest.skip(f"fixture {name} has no extractable tokens")

    word_list = sorted(expected)  # deterministic insertion order
    a_words = _fake_words(word_list)
    b_words = _fake_words(word_list)

    result = _cross_check_from_words(
        "pytesseract", a_words, "cloud_vision", b_words, threshold=DEFAULT_AGREEMENT_THRESHOLD
    )

    assert result.status == "agreed", (
        f"identical engine outputs against {name} must agree (got {result.status}, "
        f"ratio={result.agreement})"
    )
    assert set(result.tokens) == expected, (
        f"agreed token set must equal the hand-verified ground truth for {name}"
    )


def test_one_hallucinated_token_either_caught_or_propagated(fixture_tokens):
    """Engine A fabricates one extra token; engine B is faithful.

    The threshold gate at 0.85 is a character-level SequenceMatcher
    ratio over sorted-joined tokens, so the gate's sensitivity depends
    on how big the new token is relative to the existing token set's
    total character count. One of two outcomes is acceptable:

    1. ``status="disagreed"`` — the gate caught the hallucination.
       The fixture's character-budget was small enough that one extra
       token dropped the ratio below threshold. ``tokens`` is empty.
    2. ``status="agreed"`` AND the hallucinated token appears in
       ``result.tokens`` — the UNION-mode behavior propagated it.
       This is the documented risk; a future intersection-mode switch
       would replace this branch with an empty-tokens assertion.

    What we MUST NOT see: ``status="agreed"`` with the hallucination
    silently dropped from ``tokens``. That would mean the cross-check
    is lying about what it has truth-checked.
    """
    name, expected = fixture_tokens
    if not expected:
        pytest.skip(f"fixture {name} has no extractable tokens")

    word_list = sorted(expected)
    hallucination = "zzhallucxxx"  # short + alphabetic so it doesn't dominate the ratio
    a_words = _fake_words([*word_list, hallucination])
    b_words = _fake_words(word_list)

    result = _cross_check_from_words(
        "pytesseract", a_words, "cloud_vision", b_words, threshold=DEFAULT_AGREEMENT_THRESHOLD
    )

    if result.status == "disagreed":
        assert result.tokens == [], "disagreed result must have an empty canonical token set"
        assert result.agreement < DEFAULT_AGREEMENT_THRESHOLD, (
            f"status=disagreed but agreement {result.agreement} is at/above threshold "
            f"{DEFAULT_AGREEMENT_THRESHOLD} — gate is mis-classifying on {name}"
        )
        return  # threshold gate caught it; UNION-mode question moot

    # status == "agreed" → the documented union-mode risk path. Lock that
    # the hallucination propagated visibly (not silently dropped) so a
    # future intersection-mode switch shows up as a deliberate diff.
    assert hallucination in set(result.tokens), (
        f"agreed cross-check ({name}, ratio={result.agreement}) must include the "
        f"hallucinated token in result.tokens (UNION mode, documented). If you "
        f"intentionally switched to intersection, flip this assertion and update "
        f"cross_check.py's 'Intersection would be safer...' note in the same PR."
    )


def test_complete_disagreement_blocks_fixture(fixture_tokens):
    """Wildly different engine outputs → status='disagreed', no tokens.

    Sanity check that the gate is actually doing something. If this ever
    flips to 'agreed', the threshold has drifted dangerously low.
    """
    name, expected = fixture_tokens
    if not expected:
        pytest.skip(f"fixture {name} has no extractable tokens")

    a_words = _fake_words(sorted(expected))
    b_words = _fake_words(["zzzz_nothing", "in", "common", "with", "engine_a"])

    result = _cross_check_from_words(
        "pytesseract", a_words, "cloud_vision", b_words, threshold=DEFAULT_AGREEMENT_THRESHOLD
    )

    assert result.status == "disagreed", (
        f"disjoint engine outputs against {name} must NOT pass the threshold gate "
        f"(got status={result.status}, ratio={result.agreement})"
    )
    assert result.tokens == [], "disagreed result must have an empty canonical token set"
