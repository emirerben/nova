"""Gemini video judge — grades a final rendered MP4 against a markdown rubric.

The keystone of the dev-loop output gate (plan M1 / T1). `LLMJudge`
(`tests/evals/runners/llm_judge.py`) is Claude-only and cannot *watch* an MP4,
so this is a NEW judge built around Gemini's video understanding. It reuses
ONLY `LLMJudge`'s rubric-loading + `Pass threshold` regex contract (so the
20 sibling rubrics and `final_video.md` share one format), then:

  1. uploads the MP4 via `ModelDispatcher.upload_media` (Gemini File API),
  2. invokes a `gemini-*` model with the rubric as prompt + the video as media,
  3. parses `{"scores": {...}, "confidence": <float>, "reasoning": "..."}`,
  4. maps avg + self-reported confidence to a 3-band `GradeVerdict`.

Deliberately stateless + side-effect-free: the Celery task
(`app/tasks/grade_final_video.py`) owns DB persistence, GCS download, and
cost-cap orchestration. This module is pure-ish (one Gemini call) so it unit
-tests offline with a mocked dispatcher.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Gemini 2.5 Flash is the house video-understanding model. The dispatcher
# rewrites every `gemini-*` call to `settings.gemini_model` anyway (see
# _model_client.GeminiClient.invoke), so this is the family selector, not a pin.
DEFAULT_VIDEO_MODEL = "gemini-2.5-flash"
DEFAULT_PASS_THRESHOLD = 3.5
DEFAULT_MAX_TOKENS = 800

# ── 3-band verdict thresholds (avg is on the 1-5 rubric scale) ───────────────
#
# Tuned conservatively pre-calibration: a WIDE escalate band absorbs
# uncertainty, so auto_reject almost never kills a good render and auto_pass
# almost never ships a bad one. The shadow-calibration runner (T2) re-tunes
# these against the founder's VideoFeedback before any auto-action is enabled.
T_PASS = 4.0  # avg ≥ T_PASS  → auto_pass (and confidence high enough)
T_REJECT = 2.5  # avg ≤ T_REJECT → auto_reject (and confidence high enough)
# Below T_FLOOR confidence, the verdict is FORCED to escalate regardless of
# avg — the safety valve against confidently-wrong auto-passes.
T_FLOOR = 0.6


class GradeBand(StrEnum):
    """The 3-band triage outcome."""

    AUTO_PASS = "auto_pass"
    AUTO_REJECT = "auto_reject"
    ESCALATE = "escalate"


class VideoGraderError(Exception):
    """Raised when the grader call fails or returns malformed/empty output.

    Distinct from a low score: this is an *infrastructure* failure (Gemini
    timeout, malformed JSON, empty scores). The task layer catches it and
    persists a `failed` AgentRun rather than a verdict — a broken judge must
    not masquerade as an auto_reject.
    """


@dataclass
class GradeVerdict:
    """The grader's decision for one rendered video."""

    band: GradeBand
    scores: dict[str, float] = field(default_factory=dict)
    avg: float = 0.0
    confidence: float = 0.0
    threshold: float = DEFAULT_PASS_THRESHOLD
    reasoning: str = ""
    risk_tag: str = "low"
    raw_response: str = ""
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def summary_line(self) -> str:
        """The one-line 'what changed + why' card for the phone / admin view."""
        parts = [f"{k}={v:.1f}" for k, v in sorted(self.scores.items())]
        reason = self.reasoning.strip() or "no reasoning"
        return (
            f"[{self.band.value}] avg={self.avg:.2f} "
            f"conf={self.confidence:.2f} risk={self.risk_tag} "
            f"({', '.join(parts)}) — {reason}"
        )


# ── Rubric loading (reuses LLMJudge's contract, no Claude dependency) ─────────


def load_rubric(rubric_path: Path | str) -> tuple[str, float]:
    """Load rubric text + parse its `Pass threshold: avg >= N` line.

    Identical regex contract to `LLMJudge._load_rubric` so `final_video.md`
    and the 20 sibling rubrics share one format. Kept as a free function (not a
    method) so it has no client/SDK dependency.
    """
    path = Path(rubric_path)
    if not path.exists():
        raise VideoGraderError(f"rubric not found: {path}")
    text = path.read_text()
    threshold = DEFAULT_PASS_THRESHOLD
    match = re.search(r"Pass threshold:\s*avg\s*[≥>=]+\s*([0-9.]+)", text)
    if match:
        try:
            threshold = float(match.group(1))
        except ValueError:
            pass
    return text, threshold


def _parse_grade_json(raw: str) -> tuple[dict[str, float], float, str]:
    """Tolerant parse of the judge JSON → (scores, confidence, reasoning).

    Mirrors `llm_judge._parse_judge_json` but additionally extracts the
    self-reported `confidence` float (defaults to 0.0 → forces escalate when
    the model omits it, which is the safe failure).
    """
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise VideoGraderError(f"no JSON object in grader response: {raw[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise VideoGraderError(f"grader response was not valid JSON: {exc}") from exc

    raw_scores = data.get("scores", {})
    if not isinstance(raw_scores, dict):
        raise VideoGraderError("grader `scores` field is not a dict")
    scores: dict[str, float] = {}
    for k, v in raw_scores.items():
        try:
            scores[str(k)] = float(v)
        except (TypeError, ValueError):
            continue

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    # Clamp into [0, 1] — a model returning 1.5 or -0.2 shouldn't poison the band logic.
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(data.get("reasoning", ""))
    return scores, confidence, reasoning


def map_verdict(
    *,
    avg: float,
    confidence: float,
    t_pass: float = T_PASS,
    t_reject: float = T_REJECT,
    t_floor: float = T_FLOOR,
) -> tuple[GradeBand, str]:
    """Map (avg, confidence) → (band, risk_tag) via the 3-band thresholds.

    Pure function so the calibration runner (T2) can sweep thresholds without
    a Gemini call. Rules, in order:

      1. confidence < t_floor               → ESCALATE  (forced; the safety valve)
      2. avg ≥ t_pass   (and conf ≥ t_floor) → AUTO_PASS
      3. avg ≤ t_reject (and conf ≥ t_floor) → AUTO_REJECT
      4. otherwise                           → ESCALATE  (the uncertainty band)

    risk_tag flags WHY a verdict warrants attention:
      - "low"            — confident auto_pass
      - "reject"         — confident auto_reject (a render the gate would kill)
      - "low_confidence" — forced escalate from low confidence
      - "borderline"     — escalate because avg fell in the middle band
    """
    if confidence < t_floor:
        return GradeBand.ESCALATE, "low_confidence"
    if avg >= t_pass:
        return GradeBand.AUTO_PASS, "low"
    if avg <= t_reject:
        return GradeBand.AUTO_REJECT, "reject"
    return GradeBand.ESCALATE, "borderline"


# ── The grader ───────────────────────────────────────────────────────────────


class VideoQualityGrader:
    """Grades a local MP4 against a rubric using a Gemini video judge.

    Stateless. Holds a `ModelDispatcher` (injectable for tests) + a rubric path.
    `grade(video_path)` uploads → invokes → parses → bands, returning a
    `GradeVerdict`. Raises `VideoGraderError` on any infrastructure failure so
    the task layer can distinguish "judge broke" from "video is bad".
    """

    def __init__(
        self,
        rubric_path: Path | str,
        *,
        client: Any = None,
        model: str = DEFAULT_VIDEO_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        upload_timeout_s: int = 120,
        t_pass: float = T_PASS,
        t_reject: float = T_REJECT,
        t_floor: float = T_FLOOR,
    ) -> None:
        self.rubric_path = Path(rubric_path)
        self.model = model
        self.max_tokens = max_tokens
        self.upload_timeout_s = upload_timeout_s
        self.t_pass = t_pass
        self.t_reject = t_reject
        self.t_floor = t_floor
        self._client = client
        self._rubric_cache: tuple[str, float] | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            from app.agents._model_client import default_client  # noqa: PLC0415

            self._client = default_client()
        return self._client

    def _rubric(self) -> tuple[str, float]:
        if self._rubric_cache is None:
            self._rubric_cache = load_rubric(self.rubric_path)
        return self._rubric_cache

    def _build_prompt(self, rubric: str) -> str:
        return (
            "You are a strict but fair quality judge for short-form (TikTok / Reels / "
            "Shorts) video. Watch the attached video in full, then score it against "
            "the rubric below EXACTLY as written.\n\n"
            f"Rubric:\n\n{rubric}\n\n"
            "Return ONLY the JSON object the rubric specifies — no prose outside the JSON."
        )

    def grade(self, video_path: str) -> GradeVerdict:
        """Upload + judge one local MP4. Raises `VideoGraderError` on failure."""
        if not Path(video_path).exists():
            raise VideoGraderError(f"video not found: {video_path}")

        rubric, threshold = self._rubric()

        # 1. Upload via the Gemini File API (poll-until-ACTIVE inside).
        try:
            media = self.client.upload_media(video_path, timeout=self.upload_timeout_s)
        except Exception as exc:  # noqa: BLE001 — surface ALL upload failures as grader errors
            raise VideoGraderError(f"video upload failed: {exc}") from exc

        # 2. Invoke the Gemini judge with the video as media.
        try:
            invocation = self.client.invoke(
                model=self.model,
                prompt=self._build_prompt(rubric),
                media_uri=media.uri,
                media_mime=getattr(media, "mime_type", None) or "video/mp4",
                response_json=True,
                max_output_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001 — Gemini timeout / transient / terminal
            raise VideoGraderError(f"grader invocation failed: {exc}") from exc

        raw_text = getattr(invocation, "raw_text", "") or ""
        tokens_in = int(getattr(invocation, "tokens_in", 0) or 0)
        tokens_out = int(getattr(invocation, "tokens_out", 0) or 0)

        # 3. Parse scores + confidence (raises on malformed/empty).
        scores, confidence, reasoning = _parse_grade_json(raw_text)
        if not scores:
            raise VideoGraderError(f"grader returned no scores; raw response: {raw_text[:500]!r}")

        avg = sum(scores.values()) / len(scores)

        # 4. Band it.
        band, risk_tag = map_verdict(
            avg=avg,
            confidence=confidence,
            t_pass=self.t_pass,
            t_reject=self.t_reject,
            t_floor=self.t_floor,
        )

        verdict = GradeVerdict(
            band=band,
            scores=scores,
            avg=avg,
            confidence=confidence,
            threshold=threshold,
            reasoning=reasoning,
            risk_tag=risk_tag,
            raw_response=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        log.info(
            "video_graded",
            band=band.value,
            avg=round(avg, 2),
            confidence=round(confidence, 2),
            risk=risk_tag,
        )
        return verdict
