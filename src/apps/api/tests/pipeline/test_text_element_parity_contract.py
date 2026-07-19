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
from app.pipeline.generative_overlays import (
    build_overlays_from_text_elements,
    resolve_letter_spacing_em,
    resolve_letter_spacing_px,
    resolve_line_spacing,
    resolve_max_width_frac,
)
from app.pipeline.text_overlay import CANVAS_W
from app.pipeline.text_overlay_skia import _wrap_text_to_lines

# repo_root/tests/fixtures/text-element-parity — shared with the Jest suite.
FIXTURES_DIR = Path(__file__).resolve().parents[5] / "tests" / "fixtures" / "text-element-parity"

# Fields whose gate is THIS suite (base fields predate the D17 mechanism).
GATED_STYLE_FIELDS = {"text_case", "letter_spacing", "line_spacing", "max_width_frac"}
NUMERIC_TOLERANCE = 1e-9


class _FakeFont:
    def __init__(self, char_width_px: float) -> None:
        self.char_width_px = char_width_px

    def measureText(self, text: str) -> float:  # noqa: N802 - mirrors skia.Font
        return len(text) * self.char_width_px


def _load_fixture(field: str) -> dict:
    path = FIXTURES_DIR / f"{field}.json"
    assert path.is_file(), f"missing shared parity fixture {path}"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _compile_one(element: dict) -> dict:
    elem = TextElement.model_validate(element)
    overlays = build_overlays_from_text_elements(
        [elem], video_duration_s=10.0, independent_box_alignment=True
    )
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


def test_masonry_layer_origin_reaches_the_burn_dict() -> None:
    elem = TextElement.model_validate(
        {
            "id": "late-pocket",
            "text": "later",
            "start_s": 4,
            "end_s": 7,
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.5,
            "source_params": {"masonry_motion": {"layer_origin_px": 600}},
        }
    )
    overlay = build_overlays_from_text_elements([elem], video_duration_s=8.0)[0]

    assert overlay["masonry_layer_origin_x_px"] == 600.0


@pytest.mark.parametrize(
    ("alignment", "placement", "x_frac"),
    [
        ("left", "left", 0.0),
        ("left", "center", 0.3),
        ("left", "right", 0.6),
        ("center", "left", 0.2),
        ("center", "center", 0.5),
        ("center", "right", 0.8),
        ("right", "left", 0.4),
        ("right", "center", 0.7),
        ("right", "right", 1.0),
    ],
)
def test_horizontal_box_geometry_reaches_the_burn_dict(
    alignment: str, placement: str, x_frac: float
) -> None:
    """The editor's derived anchor stays intact through the renderer bridge."""
    overlay = _compile_one(
        {
            "id": f"{alignment}-{placement}-box",
            "text": "two\nlines",
            "start_s": 0,
            "end_s": 2,
            "position": "custom",
            "x_frac": x_frac,
            "y_frac": 0.45,
            "alignment": alignment,
            "max_width_frac": 0.4,
        }
    )

    assert overlay["text_anchor"] == alignment
    assert overlay["position_x_frac"] == pytest.approx(x_frac)
    assert overlay["max_width_frac"] == pytest.approx(0.4)
    assert overlay["vertical_anchor"] == "center"


# ── letter_spacing ────────────────────────────────────────────────────────────


def _letter_spacing_cases() -> list[dict]:
    return _load_fixture("letter_spacing")["cases"]


@pytest.mark.parametrize("case", _letter_spacing_cases(), ids=lambda c: c["name"])
def test_letter_spacing_burn_dict_matches_fixture(case: dict) -> None:
    """The compiled burn dict carries the clamped em value only when authored."""
    overlay = _compile_one(case["element"])
    expected = case["expected"]
    if expected["burn_dict_has_field"]:
        assert overlay["letter_spacing"] == pytest.approx(
            expected["letter_spacing_em"], abs=NUMERIC_TOLERANCE
        )
    else:
        assert "letter_spacing" not in overlay


@pytest.mark.parametrize("case", _letter_spacing_cases(), ids=lambda c: c["name"])
def test_letter_spacing_helpers_match_fixture(case: dict) -> None:
    """Pure resolvers mirror resolveLetterSpacingEm/Px in overlay-layout.ts."""
    el = case["element"]
    expected = case["expected"]
    assert resolve_letter_spacing_em(el.get("letter_spacing")) == pytest.approx(
        expected["letter_spacing_em"], abs=NUMERIC_TOLERANCE
    )
    assert resolve_letter_spacing_px(el.get("letter_spacing"), el["size_px"]) == pytest.approx(
        expected["letter_spacing_px"], abs=NUMERIC_TOLERANCE
    )


@pytest.mark.parametrize("case", _letter_spacing_cases(), ids=lambda c: c["name"])
def test_letter_spacing_schema_clamp_matches_fixture(case: dict) -> None:
    """TextElement validation uses the same clamp as the burn/layout resolvers."""
    elem = TextElement.model_validate(case["element"])
    expected = case["expected"]
    if expected["burn_dict_has_field"]:
        assert elem.letter_spacing == pytest.approx(
            expected["letter_spacing_em"], abs=NUMERIC_TOLERANCE
        )
    else:
        assert elem.letter_spacing is None


# ── line_spacing ──────────────────────────────────────────────────────────────


def _line_spacing_cases() -> list[dict]:
    return _load_fixture("line_spacing")["cases"]


def _block_metrics(line_count: int, line_height_px: float, line_spacing: float) -> dict[str, int]:
    """Mirror the renderer's _measure_block height math."""
    line_step = int(line_height_px * line_spacing)
    block_h = line_step * (line_count - 1) + int(line_height_px) if line_count > 0 else 0
    return {"line_step": line_step, "block_h": block_h}


@pytest.mark.parametrize("case", _line_spacing_cases(), ids=lambda c: c["name"])
def test_line_spacing_burn_dict_matches_fixture(case: dict) -> None:
    """The compiled burn dict carries the clamped multiplier only when authored."""
    overlay = _compile_one(case["element"])
    expected = case["expected"]
    if expected["burn_dict_has_field"]:
        assert overlay["line_spacing"] == pytest.approx(
            expected["line_spacing"], abs=NUMERIC_TOLERANCE
        )
    else:
        assert "line_spacing" not in overlay


@pytest.mark.parametrize("case", _line_spacing_cases(), ids=lambda c: c["name"])
def test_line_spacing_helper_and_geometry_match_fixture(case: dict) -> None:
    """resolve_line_spacing + block-height math mirror the TS layout helper."""
    el = case["element"]
    expected = case["expected"]
    resolved = resolve_line_spacing(el.get("line_spacing"))
    assert resolved == pytest.approx(expected["line_spacing"], abs=NUMERIC_TOLERANCE)

    geometry = case["geometry"]
    metrics = _block_metrics(
        int(geometry["line_count"]),
        float(geometry["line_height_px"]),
        resolved,
    )
    assert metrics["line_step"] == expected["line_step"]
    assert metrics["block_h"] == expected["block_h"]


@pytest.mark.parametrize("case", _line_spacing_cases(), ids=lambda c: c["name"])
def test_line_spacing_schema_clamp_matches_fixture(case: dict) -> None:
    """TextElement validation uses the same clamp as the burn/layout resolvers."""
    elem = TextElement.model_validate(case["element"])
    expected = case["expected"]
    if expected["burn_dict_has_field"]:
        assert elem.line_spacing == pytest.approx(expected["line_spacing"], abs=NUMERIC_TOLERANCE)
    else:
        assert elem.line_spacing is None


# ── max_width_frac ───────────────────────────────────────────────────────────


def _max_width_frac_cases() -> list[dict]:
    return _load_fixture("max_width_frac")["cases"]


@pytest.mark.parametrize("case", _max_width_frac_cases(), ids=lambda c: c["name"])
def test_max_width_frac_burn_dict_matches_fixture(case: dict) -> None:
    """The compiled burn dict carries the clamped wrap width only when authored."""
    overlay = _compile_one(case["element"])
    expected = case["expected"]
    if expected["burn_dict_has_field"]:
        assert overlay["max_width_frac"] == pytest.approx(
            expected["max_width_frac"], abs=NUMERIC_TOLERANCE
        )
    else:
        assert "max_width_frac" not in overlay


@pytest.mark.parametrize("case", _max_width_frac_cases(), ids=lambda c: c["name"])
def test_max_width_frac_helper_and_wrap_geometry_match_fixture(case: dict) -> None:
    """resolve_max_width_frac + Skia wrap helper mirror the TS layout contract."""
    el = case["element"]
    geometry = case["geometry"]
    expected = case["expected"]
    resolved = resolve_max_width_frac(el.get("max_width_frac"))
    max_width_px = CANVAS_W * resolved
    lines = _wrap_text_to_lines(
        str(el["text"]),
        _FakeFont(float(geometry["char_width_px"])),
        max_width_px,
    )

    assert resolved == pytest.approx(expected["max_width_frac"], abs=NUMERIC_TOLERANCE)
    assert max_width_px == pytest.approx(expected["max_width_px"], abs=NUMERIC_TOLERANCE)
    assert len(lines) == expected["line_count"]
    assert max((len(line) * float(geometry["char_width_px"]) for line in lines), default=0) <= (
        max_width_px + NUMERIC_TOLERANCE
    )


@pytest.mark.parametrize("case", _max_width_frac_cases(), ids=lambda c: c["name"])
def test_max_width_frac_schema_clamp_matches_fixture(case: dict) -> None:
    """TextElement validation uses the same clamp as the burn/layout resolvers."""
    elem = TextElement.model_validate(case["element"])
    expected = case["expected"]
    if expected["burn_dict_has_field"]:
        assert elem.max_width_frac == pytest.approx(
            expected["max_width_frac"], abs=NUMERIC_TOLERANCE
        )
    else:
        assert elem.max_width_frac is None
