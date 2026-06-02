"""Unit tests for the Gemini video grader (plan M1 / T1).

All offline — the Gemini call is mocked via a fake dispatcher injected into
`VideoQualityGrader`. The live-Gemini calibration run is the only human-verify
step (see scripts/run_grader_calibration.py); everything here runs in CI.

Covers (D3): happy path, 3-band threshold mapping, determinism (per-dimension
score-variance fixture), failure paths (Gemini timeout, malformed/empty judge
JSON, low-confidence → forced escalate).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.services.video_grader import (
    DEFAULT_VIDEO_MODEL,
    T_FLOOR,
    T_PASS,
    T_REJECT,
    GradeBand,
    VideoGraderError,
    VideoQualityGrader,
    map_verdict,
)

RUBRIC_PATH = Path(__file__).resolve().parents[1] / "evals" / "rubrics" / "final_video.md"


# ── Test doubles ──────────────────────────────────────────────────────────────


@dataclass
class _FakeMedia:
    uri: str = "files/fake-123"
    mime_type: str = "video/mp4"
    name: str = "files/fake-123"


@dataclass
class _FakeInvocation:
    raw_text: str
    tokens_in: int = 1000
    tokens_out: int = 100
    raw_response: Any = None


class _FakeClient:
    """Stands in for ModelDispatcher: records calls, returns scripted output."""

    def __init__(
        self,
        *,
        raw_text: str = "",
        upload_exc: Exception | None = None,
        invoke_exc: Exception | None = None,
        tokens_in: int = 1000,
        tokens_out: int = 100,
    ) -> None:
        self._raw_text = raw_text
        self._upload_exc = upload_exc
        self._invoke_exc = invoke_exc
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        self.invoke_calls: list[dict[str, Any]] = []
        self.upload_calls: list[str] = []

    def upload_media(self, path: str, *, timeout: int = 120) -> _FakeMedia:  # noqa: ARG002
        self.upload_calls.append(path)
        if self._upload_exc:
            raise self._upload_exc
        return _FakeMedia()

    def invoke(self, **kwargs: Any) -> _FakeInvocation:
        self.invoke_calls.append(kwargs)
        if self._invoke_exc:
            raise self._invoke_exc
        return _FakeInvocation(
            raw_text=self._raw_text,
            tokens_in=self._tokens_in,
            tokens_out=self._tokens_out,
        )


def _judge_json(scores: dict[str, float], confidence: float, reasoning: str = "ok") -> str:
    return json.dumps({"scores": scores, "confidence": confidence, "reasoning": reasoning})


@pytest.fixture
def video_file(tmp_path: Path) -> str:
    p = tmp_path / "final.mp4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes")
    return str(p)


def _grader(client: _FakeClient) -> VideoQualityGrader:
    return VideoQualityGrader(RUBRIC_PATH, client=client)


# ── Rubric contract ───────────────────────────────────────────────────────────


def test_rubric_exists_and_parses_threshold() -> None:
    from app.services.video_grader import load_rubric

    text, threshold = load_rubric(RUBRIC_PATH)
    assert "hook_strength" in text
    assert "looks_filmed_not_templated" in text
    assert threshold == pytest.approx(3.5)


# ── Happy path ────────────────────────────────────────────────────────────────


def test_happy_path_auto_pass(video_file: str) -> None:
    raw = _judge_json(
        {
            "hook_strength": 5,
            "text_legibility_and_timing": 4,
            "looks_filmed_not_templated": 4,
            "overall_quality": 5,
        },
        confidence=0.9,
        reasoning="strong hook, clean text",
    )
    client = _FakeClient(raw_text=raw, tokens_in=5000, tokens_out=200)
    verdict = _grader(client).grade(video_file)

    assert verdict.band is GradeBand.AUTO_PASS
    assert verdict.avg == pytest.approx(4.5)
    assert verdict.confidence == pytest.approx(0.9)
    assert verdict.risk_tag == "low"
    assert verdict.tokens_in == 5000
    assert verdict.tokens_out == 200
    # The grader uploaded the local file and invoked with the video as media.
    assert client.upload_calls == [video_file]
    assert client.invoke_calls[0]["media_uri"] == "files/fake-123"
    assert client.invoke_calls[0]["media_mime"] == "video/mp4"
    assert client.invoke_calls[0]["model"] == DEFAULT_VIDEO_MODEL
    # One-line card for the phone/admin surface.
    assert "auto_pass" in verdict.summary_line
    assert "strong hook" in verdict.summary_line


def test_happy_path_auto_reject(video_file: str) -> None:
    raw = _judge_json(
        {
            "hook_strength": 2,
            "text_legibility_and_timing": 2,
            "looks_filmed_not_templated": 2,
            "overall_quality": 2,
        },
        confidence=0.85,
    )
    verdict = _grader(_FakeClient(raw_text=raw)).grade(video_file)
    assert verdict.band is GradeBand.AUTO_REJECT
    assert verdict.avg == pytest.approx(2.0)
    assert verdict.risk_tag == "reject"


# ── 3-band threshold mapping (pure function — no Gemini) ──────────────────────


@pytest.mark.parametrize(
    ("avg", "confidence", "expected_band", "expected_risk"),
    [
        # High confidence: avg drives the band.
        (4.5, 0.9, GradeBand.AUTO_PASS, "low"),
        (4.0, 0.9, GradeBand.AUTO_PASS, "low"),  # boundary: avg == T_PASS
        (2.5, 0.9, GradeBand.AUTO_REJECT, "reject"),  # boundary: avg == T_REJECT
        (2.0, 0.9, GradeBand.AUTO_REJECT, "reject"),
        (3.2, 0.9, GradeBand.ESCALATE, "borderline"),  # middle band
        (3.99, 0.9, GradeBand.ESCALATE, "borderline"),
        (2.51, 0.9, GradeBand.ESCALATE, "borderline"),
        # Low confidence FORCES escalate regardless of avg (the safety valve).
        (5.0, 0.5, GradeBand.ESCALATE, "low_confidence"),
        (1.0, 0.5, GradeBand.ESCALATE, "low_confidence"),
        (4.2, T_FLOOR - 0.01, GradeBand.ESCALATE, "low_confidence"),
        # Confidence exactly at the floor is NOT low (>= floor passes).
        (4.5, T_FLOOR, GradeBand.AUTO_PASS, "low"),
    ],
)
def test_three_band_mapping(
    avg: float, confidence: float, expected_band: GradeBand, expected_risk: str
) -> None:
    band, risk = map_verdict(avg=avg, confidence=confidence)
    assert band is expected_band
    assert risk == expected_risk


def test_band_thresholds_are_ordered() -> None:
    # Sanity: the band thresholds can't overlap or the mapping is ill-defined.
    assert T_REJECT < T_PASS
    assert 0.0 < T_FLOOR < 1.0


# ── Low confidence forces escalate end-to-end (not just the pure fn) ──────────


def test_low_confidence_forces_escalate_through_grade(video_file: str) -> None:
    # Scores would otherwise be a clean auto_pass (avg 4.5) — but low confidence
    # must override to escalate so a confidently-wrong auto-pass can't ship.
    raw = _judge_json(
        {
            "hook_strength": 5,
            "text_legibility_and_timing": 4,
            "looks_filmed_not_templated": 4,
            "overall_quality": 5,
        },
        confidence=0.3,
    )
    verdict = _grader(_FakeClient(raw_text=raw)).grade(video_file)
    assert verdict.band is GradeBand.ESCALATE
    assert verdict.risk_tag == "low_confidence"
    assert verdict.avg == pytest.approx(4.5)  # scores untouched; only the band flips


# ── Determinism: per-dimension score-variance fixture ────────────────────────


def _scores(hook: int, text: int, filmed: int, overall: int) -> dict[str, float]:
    return {
        "hook_strength": hook,
        "text_legibility_and_timing": text,
        "looks_filmed_not_templated": filmed,
        "overall_quality": overall,
    }


def test_low_variance_dimensions_band_consistently(video_file: str) -> None:
    """Repeated runs over low-variance per-dimension scores must agree on the band.

    Each "run" jitters the dimension scores within ±0.x of a stable mean. A
    dimension whose variance stays small must not flip the verdict band across
    runs — the property the determinism gate relies on.
    """
    # Stable means well inside the auto_pass region; small per-run jitter.
    runs = [
        _scores(5, 5, 4, 5),
        _scores(5, 4, 4, 5),
        _scores(4, 5, 4, 5),
        _scores(5, 5, 5, 4),
    ]
    bands = set()
    for scores in runs:
        raw = _judge_json(scores, confidence=0.9)
        verdict = _grader(_FakeClient(raw_text=raw)).grade(video_file)
        bands.add(verdict.band)
    # Low-variance dimensions → single stable band.
    assert bands == {GradeBand.AUTO_PASS}


def test_high_variance_dimension_may_not_gate(video_file: str) -> None:
    """A high-variance dimension can flip the band — documents why such dims can't gate.

    `looks_filmed_not_templated` swings 1↔5 run-to-run while the others are
    stable. The average crosses band boundaries, so the verdict is NOT stable.
    This is the negative control: the determinism gate must EXCLUDE such a
    dimension, never trust it to auto-act.
    """
    runs = [
        _scores(4, 4, 5, 4),  # avg 4.25 → auto_pass
        _scores(4, 4, 1, 4),  # avg 3.25 → escalate
    ]
    bands = set()
    for scores in runs:
        raw = _judge_json(scores, confidence=0.9)
        bands.add(_grader(_FakeClient(raw_text=raw)).grade(video_file).band)
    assert len(bands) > 1  # unstable → cannot gate on this dimension


# ── Failure paths ─────────────────────────────────────────────────────────────


def test_gemini_timeout_raises_grader_error(video_file: str) -> None:
    client = _FakeClient(invoke_exc=TimeoutError("gemini timed out"))
    with pytest.raises(VideoGraderError, match="invocation failed"):
        _grader(client).grade(video_file)


def test_upload_failure_raises_grader_error(video_file: str) -> None:
    client = _FakeClient(upload_exc=RuntimeError("file API 503"))
    with pytest.raises(VideoGraderError, match="upload failed"):
        _grader(client).grade(video_file)


def test_malformed_judge_json_raises(video_file: str) -> None:
    client = _FakeClient(raw_text="here are the scores: not-json at all")
    with pytest.raises(VideoGraderError):
        _grader(client).grade(video_file)


def test_empty_scores_raises(video_file: str) -> None:
    # Valid JSON, but the scores object is empty → no signal to band on.
    client = _FakeClient(raw_text=json.dumps({"scores": {}, "confidence": 0.9}))
    with pytest.raises(VideoGraderError, match="no scores"):
        _grader(client).grade(video_file)


def test_missing_video_file_raises() -> None:
    with pytest.raises(VideoGraderError, match="video not found"):
        _grader(_FakeClient(raw_text="{}")).grade("/nonexistent/path.mp4")


def test_missing_confidence_defaults_to_forced_escalate(video_file: str) -> None:
    # A judge that omits `confidence` defaults to 0.0 → forced escalate (safe).
    raw = json.dumps(
        {
            "scores": {
                "hook_strength": 5,
                "text_legibility_and_timing": 5,
                "looks_filmed_not_templated": 5,
                "overall_quality": 5,
            },
            "reasoning": "no confidence field",
        }
    )
    verdict = _grader(_FakeClient(raw_text=raw)).grade(video_file)
    assert verdict.confidence == pytest.approx(0.0)
    assert verdict.band is GradeBand.ESCALATE
    assert verdict.risk_tag == "low_confidence"


def test_fenced_json_is_parsed(video_file: str) -> None:
    # Models often wrap JSON in ```json fences — must still parse.
    inner = _judge_json(
        {
            "hook_strength": 4,
            "text_legibility_and_timing": 4,
            "looks_filmed_not_templated": 4,
            "overall_quality": 4,
        },
        confidence=0.9,
    )
    client = _FakeClient(raw_text=f"```json\n{inner}\n```")
    verdict = _grader(client).grade(video_file)
    assert verdict.avg == pytest.approx(4.0)
    assert verdict.band is GradeBand.AUTO_PASS
