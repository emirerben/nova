"""Integration test for scripts/analyze_waka_waka_diff.py.

Runs only when both local videos are present in ~/Downloads/. The test is
intentionally not a CI test — it depends on a specific pair of files the
maintainer has on disk. Acceptance criteria match the plan's verification
checklist.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

RECIPE_PATH = Path("/Users/yasinberk/Downloads/morocco.mp4")
OUTPUT_PATH = Path("/Users/yasinberk/Downloads/thisismorocco.mp4")
# __file__ = .../src/apps/api/tests/scripts/test_analyze_waka_waka_diff.py
# parents[2] = .../src/apps/api  (tests/scripts -> tests -> api)
API_ROOT = Path(__file__).resolve().parents[2]


pytestmark = pytest.mark.skipif(
    not RECIPE_PATH.exists() or not OUTPUT_PATH.exists() or shutil.which("ffmpeg") is None,
    reason="local Waka Waka videos or ffmpeg missing — analyzer integration is local-only",
)


def _run_analyzer(tmp_path: Path) -> dict:
    json_out = tmp_path / "diff.json"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(API_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [
            sys.executable, "-m", "scripts.analyze_waka_waka_diff",
            f"REF={RECIPE_PATH}",
            f"OURS={OUTPUT_PATH}",
            "--json", str(json_out),
            "--window", "4.0",
            "--sample-step", "0.05",
        ],
        cwd=API_ROOT,
        env=env,
        capture_output=True, text=True, check=False,
    )
    # Exit code 1 just means critical findings were surfaced, not a crash.
    if result.returncode not in (0, 1):
        pytest.fail(
            f"analyzer exited with {result.returncode}\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    assert json_out.exists(), "analyzer produced no JSON report"
    return json.loads(json_out.read_text())


def test_recipe_events_include_intro_words(tmp_path):
    report = _run_analyzer(tmp_path)
    recipe_texts = {(e["text"] or "").lower() for e in report["events"]["recipe"]}
    expected = {"this", "is", "africa"}
    # At least 2 of the 3 expected words should be detected — OCR may miss one
    # but not all when the analyzer is working.
    detected = recipe_texts & expected
    assert len(detected) >= 2, (
        f"expected to detect at least 2 of {expected} in recipe; got {recipe_texts}"
    )


def test_output_first_event_is_not_this(tmp_path):
    """Acceptance: the output is broken — first detected text must NOT be 'This'."""
    report = _run_analyzer(tmp_path)
    output_events = report["events"]["output"]
    assert output_events, "expected at least one event in output video"
    first_text = (output_events[0]["text"] or "").lower()
    assert first_text != "this", (
        "if output started with 'This' the pipeline wasn't broken — analyzer test "
        "is calibrated for the known-broken sample"
    )


def test_at_least_one_critical_finding(tmp_path):
    report = _run_analyzer(tmp_path)
    severities = [f["severity"] for f in report["diff"]]
    assert "critical" in severities, (
        f"expected at least one CRITICAL finding for known-broken sample; "
        f"diff={report['diff']}"
    )


def test_summary_mentions_morocco_or_missing(tmp_path):
    report = _run_analyzer(tmp_path)
    joined = "\n".join(report["summary"]).lower()
    assert any(kw in joined for kw in ["morocco", "missing", "unexpected"])


def test_aspect_ratios_detected_correctly(tmp_path):
    report = _run_analyzer(tmp_path)
    assert report["videos"]["recipe"]["aspect"] == "16:9"
    assert report["videos"]["output"]["aspect"] == "9:16"


def test_safe_crop_section_emitted(tmp_path):
    """When recipe is 16:9 and output is 9:16, the safe-crop projection must run."""
    report = _run_analyzer(tmp_path)
    assert "safe_crop_9x16" in report
    # Should have at least one entry corresponding to a recipe event.
    assert isinstance(report["safe_crop_9x16"], dict)


def test_audio_status_reported(tmp_path):
    report = _run_analyzer(tmp_path)
    assert report["audio"]["recipe"]["status"] in ("ok", "no-audio-stream", "extraction-error")
    assert report["audio"]["output"]["status"] in ("ok", "no-audio-stream", "extraction-error")
