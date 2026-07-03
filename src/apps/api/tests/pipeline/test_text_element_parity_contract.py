"""Layout-contract tests for parity-gated TextElement style fields (D9/D17).

These are the Python half of the shared-fixture contract that feeds
``PARITY_VERIFIED_FIELDS``: every fixture under
``tests/fixtures/text-element-parity/`` (repo root) is asserted by BOTH this
suite (burn-dict / resolved-geometry output of the Python compiler) and the
Jest suite ``src/apps/web/src/__tests__/lib/text-element-parity-contract.test.ts``
(TS layout output of ``resolveTextElementsLayout`` and its resolvers) — same
JSON, same expected values, so the two renderers cannot drift silently.

A style field may be added to the parity registries (Python:
``text_element.PARITY_VERIFIED_FIELDS``; TS: ``parity-verified-fields.ts``)
only together with its fixture here plus a Skia render-verification test in
``test_text_overlay_skia_style_fields.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents._schemas.text_element import (
    PARITY_VERIFIED_FIELDS,
    TextElement,
    apply_text_case,
)
from app.pipeline.generative_overlays import build_overlays_from_text_elements

# repo_root/tests/fixtures/text-element-parity — shared with the Jest suite.
FIXTURES_DIR = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "text-element-parity"

# Fields whose gate is THIS suite (base fields predate the D17 mechanism).
GATED_STYLE_FIELDS = {"text_case"}


def _load_fixture(field: str) -> dict:
    path = FIXTURES_DIR / f"{field}.json"
    assert path.is_file(), f"missing shared parity fixture {path}"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _compile_one(element: dict) -> dict:
    elem = TextElement.model_validate(element)
    overlays = build_overlays_from_text_elements([elem], video_duration_s=10.0)
    assert overlays, f"element {element.get('id')} compiled to no overlays"
    return overlays[0]


# ── Registry / fixture coverage invariants ────────────────────────────────────


def test_fixtures_dir_exists() -> None:
    assert FIXTURES_DIR.is_dir(), FIXTURES_DIR


def test_every_gated_field_has_a_fixture_and_is_registered() -> None:
    """Each gated style field must carry its shared fixture AND be present in
    the Python parity registry (the TS registry mirrors it — asserted by the
    Jest half against the same fixture directory)."""
    fixture_fields = {p.stem for p in FIXTURES_DIR.glob("*.json")}
    assert GATED_STYLE_FIELDS <= fixture_fields, (
        f"gated fields missing fixtures: {GATED_STYLE_FIELDS - fixture_fields}"
    )
    assert GATED_STYLE_FIELDS <= PARITY_VERIFIED_FIELDS


def test_no_orphan_fixture_files() -> None:
    """Every fixture file must correspond to a gated field this suite asserts —
    an orphan fixture would look verified without being tested."""
    fixture_fields = {p.stem for p in FIXTURES_DIR.glob("*.json")}
    assert fixture_fields <= GATED_STYLE_FIELDS, (
        f"fixtures without a contract test: {fixture_fields - GATED_STYLE_FIELDS}"
    )


# ── text_case ─────────────────────────────────────────────────────────────────


def _text_case_cases() -> list[dict]:
    return _load_fixture("text_case")["cases"]


@pytest.mark.parametrize("case", _text_case_cases(), ids=lambda c: c["name"])
def test_text_case_burn_dict_matches_fixture(case: dict) -> None:
    """The compiled burn dict carries the transformed text — the same string
    the TS layout produces for the CSS preview."""
    overlay = _compile_one(case["element"])
    assert overlay["text"] == case["expected"]["text"]


@pytest.mark.parametrize("case", _text_case_cases(), ids=lambda c: c["name"])
def test_text_case_helper_matches_fixture(case: dict) -> None:
    """apply_text_case in isolation (mirrors applyTextCase in overlay-layout.ts)."""
    el = case["element"]
    assert apply_text_case(el["text"], el.get("text_case")) == case["expected"]["text"]


def test_text_case_does_not_mutate_stored_element() -> None:
    """Compile-time transform only: the validated element keeps user casing."""
    elem = TextElement.model_validate(
        {"id": "keep", "text": "keep My Casing", "start_s": 0, "end_s": 2, "text_case": "upper"}
    )
    build_overlays_from_text_elements([elem], video_duration_s=10.0)
    assert elem.text == "keep My Casing"
    assert elem.text_case == "upper"


def test_text_case_transforms_karaoke_word_timings() -> None:
    """karaoke-line burns words from word_timings — those must be cased too,
    without mutating the stored element's timing dicts."""
    timings = [
        {"text": "hello", "start_s": 0.0, "end_s": 0.5},
        {"text": "world", "start_s": 0.5, "end_s": 1.0},
    ]
    elem = TextElement.model_validate(
        {
            "id": "kara",
            "text": "hello world",
            "start_s": 0,
            "end_s": 2,
            "effect": "karaoke-line",
            "text_case": "upper",
            "word_timings": timings,
        }
    )
    overlay = build_overlays_from_text_elements([elem], video_duration_s=10.0)[0]
    assert [w["text"] for w in overlay["word_timings"]] == ["HELLO", "WORLD"]
    # Stored element untouched (the compiler copies, never mutates).
    assert [w["text"] for w in elem.word_timings] == ["hello", "world"]


def test_unknown_text_case_coerces_to_none() -> None:
    """A drifted client value degrades to no transform — never a dropped element."""
    elem = TextElement.model_validate(
        {"id": "x", "text": "AbC", "start_s": 0, "end_s": 2, "text_case": "sTuDlY"}
    )
    assert elem.text_case is None
    overlay = build_overlays_from_text_elements([elem], video_duration_s=10.0)[0]
    assert overlay["text"] == "AbC"
