"""Unit tests for scripts/dev/scan_intro_slop.py's pure scan logic (plans/015).

The scanner is the measurement tool that replaced the runtime warn hook — it
must be tolerant of every malformed assembly_plan shape (per-row damage yields
zero findings, never an exception) and must flag both persisted `intro_text`
(the remediation target) and `sequence_quote` (report-only follow-up sizing).
"""

from __future__ import annotations

import importlib.util
import pathlib

# Repo root: src/apps/api/tests/scripts/this_file.py -> parents[5]
_SCRIPT = pathlib.Path(__file__).resolve().parents[5] / "scripts" / "dev" / "scan_intro_slop.py"
_spec = importlib.util.spec_from_file_location("scan_intro_slop", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

scan_assembly_plan = _mod.scan_assembly_plan

_MONKEY = "the monkey changed my whole marketing perspective"


def test_flags_slopped_intro_text():
    plan = {"variants": [{"intro_text": _MONKEY}]}
    findings = scan_assembly_plan("job-1", plan)
    assert len(findings) == 1
    f = findings[0]
    assert f["kind"] == "intro_text"
    assert f["variant_index"] == 0
    assert "en_changed_my" in f["patterns"]
    assert f["text"] == _MONKEY


def test_clean_intro_yields_no_findings():
    plan = {"variants": [{"intro_text": "the before nobody posted"}]}
    assert scan_assembly_plan("job-2", plan) == []


def test_sequence_quote_reported_separately():
    plan = {"variants": [{"sequence_quote": "this trip changed everything for us."}]}
    findings = scan_assembly_plan("job-3", plan)
    assert [f["kind"] for f in findings] == ["sequence_quote"]


def test_multiple_variants_indexed():
    plan = {
        "variants": [
            {"intro_text": "day 30 and the bar is still heavy"},
            {"intro_text": _MONKEY},
        ]
    }
    findings = scan_assembly_plan("job-4", plan)
    assert [(f["variant_index"], f["kind"]) for f in findings] == [(1, "intro_text")]


def test_tolerates_malformed_shapes():
    for plan in (
        None,
        "not-a-dict",
        {},
        {"variants": None},
        {"variants": "nope"},
        {"variants": [None, "nope", 3]},
        {"variants": [{"intro_text": None}, {"intro_text": 42}, {"intro_text": "   "}]},
    ):
        assert scan_assembly_plan("job-5", plan) == []
