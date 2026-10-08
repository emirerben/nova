"""Portable captured-model regressions with outcome-based mutation controls."""

import copy
import json

import pytest

from .system_composition import independent_assertions, load_cases, replay_capture

CASES = load_cases()


def test_captures_are_portable_and_complete():
    assert len(CASES) == 28
    assert len({r["case"] for r in CASES}) == 28
    assert all(r["calls"] and r["input"] and r["expected_outcome"] for r in CASES)


@pytest.mark.parametrize("row", CASES, ids=lambda r: r["case"])
def test_model_request_outcome(row, prod_profile):
    if row["case"] == "kria_v2_timeline_speed_up_clip":
        # Historical snapshot lacked field-level capability context. Preserve
        # the observed false proposal; current context has a separate regression.
        from app.services.kria_editor_ops import KriaEditorOpError

        with pytest.raises(KriaEditorOpError, match="Speed changes are not available"):
            replay_capture(row)
        return
    independent_assertions(row, replay_capture(row))


def test_noop_cannot_pass_supported_request():
    row = copy.deepcopy(CASES[0])
    row["calls"][-1]["raw_text"] = json.dumps(
        {"intent": "edit", "confidence": 1, "ops": [], "reply": "Done."}
    )
    with pytest.raises(AssertionError):
        replay_capture(row)


def test_valid_but_wrong_caption_fails_outcome_check():
    row = copy.deepcopy(CASES[0])
    raw = json.loads(row["calls"][-1]["raw_text"])
    raw["ops"][0]["text"] = "Wrong text"
    row["calls"][-1]["raw_text"] = json.dumps(raw)
    result = replay_capture(row)
    with pytest.raises(AssertionError):
        independent_assertions(row, result)


def test_saved_caption_changes_are_visible_to_next_model_turn():
    from app.services.kria_editor_ops import build_editor_snapshot

    row = next(r for r in CASES if r["case"] == "caption_word_style_meta")
    result = replay_capture(row)
    snapshot = build_editor_snapshot(result["job"], result["after"])
    assert snapshot["captions"]["meta"]["style"] == "word"
    assert snapshot["captions"]["meta"]["y_frac"] == pytest.approx(0.85)
