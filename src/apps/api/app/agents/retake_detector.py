"""nova.audio.retake_detector — flag abandoned takes in a talk-to-camera transcript.

Part of plans/010 (silence + filler + retake cutting), task T7. Detection ONLY:
this module is deliberately NOT wired into any orchestrator yet — T5 merges its
spans into the CutPlan (word ranges → time ranges → ``removed[]`` entries with
``reason: "retake"``) behind ``RETAKE_CUT_ENABLED``.

Contract for the T5 integration:
  - entrypoint: ``await detect_retakes(words, language=...)`` (async; wraps the
    sync ``Agent.run`` in a worker thread the same way routes wrap sibling
    agents) or the sync ``run_retake_detector(inp)``.
  - input words are the VERBATIM transcript as 0-based contiguous indexed
    entries ``[{"i": int, "text": str, "start_s": float, "end_s": float}, …]``.
  - output spans are INCLUSIVE word-index ranges identifying the ABANDONED
    take (the earlier, discarded delivery) — never the final kept take.
  - failure semantics: raises ``TerminalError`` (agent exhausted its retries)
    or pydantic ``ValidationError`` (malformed input). The caller MUST catch
    and degrade to ZERO retake cuts (pipeline event ``retake_detector_failed``)
    — a retake-detector failure can never fail the job or block the
    silence/filler cuts (plans/010 "Failure isolation").

Conservative by construction: ``parse()`` funnels every model-returned span
through ``normalize_retake_spans`` which DROPS anything malformed, out of
bounds, reversed, or reaching the transcript's final word — an abandoned take
must be FOLLOWED by its kept re-delivery, so a span touching the last word can
never be a retake. Empty output is always a valid answer (never a refusal).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError, model_validator

from app.agents._runtime import Agent, AgentSpec, ModelClient, RunContext, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

# A retake needs an abandoned delivery AND a kept re-delivery after it; below
# this floor there is nothing to detect, so the async entrypoint returns the
# empty output without spending an LLM call.
_MIN_WORDS_FOR_DETECTION = 4

# Reasons render in the admin cut-plan viewer — keep them one line and short.
_MAX_REASON_CHARS = 240

# Language hints are ISO-ish short codes ("en", "tr", "pt-br"). Anything else
# is caller garbage — treat as "unknown" rather than splicing it into the prompt.
_LANGUAGE_RE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$")


# ── Schemas ───────────────────────────────────────────────────────────────────


class IndexedWord(BaseModel):
    """One verbatim transcript word. ``i`` is the word's 0-based position —
    enforced contiguous by ``RetakeDetectorInput`` so span indices returned by
    the model map 1:1 onto the caller's word list."""

    i: int = Field(ge=0)
    text: str
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)


class RetakeDetectorInput(BaseModel):
    words: list[IndexedWord] = Field(default_factory=list)
    # ISO 639-1-style hint ("en", "tr"). Empty = unknown; the model detects it.
    language: str = ""

    @model_validator(mode="after")
    def _contiguous_indices(self) -> RetakeDetectorInput:
        for pos, w in enumerate(self.words):
            if w.i != pos:
                raise ValueError(
                    f"words[{pos}].i == {w.i} — indices must be contiguous 0-based "
                    "positions so output spans map exactly onto the caller's word list"
                )
        return self


class RetakeSpan(BaseModel):
    """INCLUSIVE word-index range covering one abandoned take (the earlier,
    discarded delivery INCLUDING its restart-marker words) — never the final
    kept take."""

    start_word: int = Field(ge=0)
    end_word: int = Field(ge=0)
    reason: str = Field(min_length=1)


class RetakeDetectorOutput(BaseModel):
    retakes: list[RetakeSpan] = Field(default_factory=list)


# ── Pure span helpers (unit-tested; no LLM, no I/O) ───────────────────────────


def normalize_retake_spans(raw_entries: Sequence[Any], n_words: int) -> list[RetakeSpan]:
    """Validate, clamp, and merge model-returned span entries.

    Conservative default (plans/010): anything ambiguous is DROPPED, never
    repaired into a cut. Rules:

      - fewer than 2 words ⇒ no span can have a kept take after it ⇒ ``[]``
      - non-dict entries and non-integer indices are dropped
      - indices are clamped into ``[0, n_words - 1]``
      - reversed spans (start > end after clamping) are dropped
      - spans reaching the FINAL word (``end_word >= n_words - 1``) are dropped
        — an abandoned take must be followed by its kept re-delivery, so a
        span touching the last word can never be a retake (protects the
        clip's actual ending from ever being cut)
      - spans with an empty ``reason`` are dropped (sibling convention —
        ``music_matcher`` drops rationale-less entries; the reason renders in
        the admin cut-plan viewer)
      - surviving spans are sorted and overlapping/adjacent ones merged
    """
    if n_words < 2:
        return []

    candidates: list[RetakeSpan] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            continue
        try:
            start = int(entry.get("start_word"))  # type: ignore[arg-type]
            end = int(entry.get("end_word"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        start = max(0, min(start, n_words - 1))
        end = max(0, min(end, n_words - 1))
        if start > end:
            continue
        if end >= n_words - 1:
            continue
        reason = _sanitize_text(str(entry.get("reason", "") or ""))[:_MAX_REASON_CHARS]
        if not reason:
            continue
        candidates.append(RetakeSpan(start_word=start, end_word=end, reason=reason))

    candidates.sort(key=lambda s: (s.start_word, s.end_word))
    merged: list[RetakeSpan] = []
    for span in candidates:
        if merged and span.start_word <= merged[-1].end_word + 1:
            prev = merged[-1]
            reason = prev.reason
            if span.reason not in reason:
                reason = f"{reason}; {span.reason}"[:_MAX_REASON_CHARS]
            merged[-1] = RetakeSpan(
                start_word=prev.start_word,
                end_word=max(prev.end_word, span.end_word),
                reason=reason,
            )
        else:
            merged.append(span)
    return merged


def retake_structural_failures(retakes: Sequence[RetakeSpan], n_words: int) -> list[str]:
    """Every structural invariant a parsed output must satisfy, as
    human-readable failure strings (empty list = pass).

    ``parse()`` guarantees these by construction via ``normalize_retake_spans``;
    the eval structural check imports THIS function so it can never drift from
    the runtime's own validation (same pattern as ``sequence_quote``).
    """
    failures: list[str] = []
    if n_words < 2 and retakes:
        failures.append(f"{len(retakes)} spans on a {n_words}-word transcript (must be empty)")
        return failures
    prev_end = -2
    for idx, span in enumerate(retakes):
        if span.start_word < 0 or span.end_word < 0:
            failures.append(f"retakes[{idx}]: negative index")
        if span.start_word > span.end_word:
            failures.append(
                f"retakes[{idx}]: start_word {span.start_word} > end_word {span.end_word}"
            )
        if span.end_word >= n_words - 1:
            failures.append(
                f"retakes[{idx}]: end_word {span.end_word} reaches the final word "
                f"(last index {n_words - 1}) — no kept take can follow"
            )
        if span.start_word <= prev_end:
            failures.append(f"retakes[{idx}]: overlaps the previous span (not sorted/merged)")
        prev_end = max(prev_end, span.end_word)
        if not span.reason.strip():
            failures.append(f"retakes[{idx}]: empty reason")
    return failures


def _normalize_language(language: str) -> str:
    lang = (language or "").strip().lower()
    return lang if _LANGUAGE_RE.match(lang) else ""


def _word_line(w: IndexedWord) -> str:
    # Transcript text is UNTRUSTED user speech — sanitized before it enters the
    # prompt (control chars, role markers, fences), same defense as the
    # clip-summary fields in music_matcher.
    return f"{w.i}  [{w.start_s:.2f}-{w.end_s:.2f}]  {_sanitize_text(w.text)}"


# ── Agent ─────────────────────────────────────────────────────────────────────


class RetakeDetectorAgent(Agent[RetakeDetectorInput, RetakeDetectorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.retake_detector",
        prompt_id="retake_detector",
        prompt_version="1",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Comparing candidate passages ("is this the same content re-delivered,
        # or rhetorical repetition?") needs some reasoning headroom — same
        # budget as sequence_quote, well under the multi-thousand-token default.
        thinking_budget=512,
    )
    Input = RetakeDetectorInput
    Output = RetakeDetectorOutput

    def required_fields(self) -> list[str]:
        # Key must be PRESENT; an empty list is a legitimate zero-result answer
        # (the refusal check in _runtime treats [] as valid).
        return ["retakes"]

    def render_prompt(self, input: RetakeDetectorInput) -> str:  # noqa: A002
        n = len(input.words)
        lang = _normalize_language(input.language)
        language_hint = (
            f"The transcript language hint is: {lang}."
            if lang
            else "No language hint — detect the language from the words."
        )
        word_lines = "\n".join(_word_line(w) for w in input.words) or "(empty transcript)"
        return load_prompt(
            "retake_detector",
            language_hint=language_hint,
            word_count=str(n),
            max_index=str(max(0, n - 1)),
            word_lines=word_lines,
        )

    def parse(self, raw_text: str, input: RetakeDetectorInput) -> RetakeDetectorOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"retake_detector: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("retake_detector: response is not a JSON object")

        retakes_raw = data.get("retakes")
        if not isinstance(retakes_raw, list):
            raise SchemaError("retake_detector: 'retakes' is missing or not a list")

        spans = normalize_retake_spans(retakes_raw, len(input.words))
        dropped = len(retakes_raw) - len(spans)
        if dropped > 0:
            # Dropping is the designed conservative behavior, not an error —
            # log so live-eval runs and prod traces can count over-flagging.
            log.info(
                "retake_detector_spans_dropped",
                returned=len(retakes_raw),
                kept=len(spans),
                n_words=len(input.words),
            )

        try:
            return RetakeDetectorOutput(retakes=spans)
        except ValidationError as exc:  # pragma: no cover — spans are pre-validated
            raise SchemaError(f"retake_detector: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            '\n\nIMPORTANT: Return ONLY the JSON object {"retakes": [...]} — no '
            "markdown, no prose. Every entry MUST have integer `start_word` and "
            "`end_word` (inclusive indices into the numbered word list), "
            "`start_word` <= `end_word`, `end_word` strictly before the final "
            "word's index, and a non-empty one-line `reason`. If there are no "
            'abandoned takes, or you are unsure, return {"retakes": []}.'
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


# ── Entrypoints ───────────────────────────────────────────────────────────────


def run_retake_detector(
    inp: RetakeDetectorInput,
    *,
    client: ModelClient | None = None,
    ctx: RunContext | None = None,
) -> RetakeDetectorOutput:
    """Synchronous entrypoint (mirrors ``run_shot_list_writer``). Raises
    ``TerminalError`` on agent failure — callers degrade to zero retake cuts."""
    from app.agents._model_client import default_client  # noqa: PLC0415

    agent = RetakeDetectorAgent(client or default_client())
    return agent.run(inp, ctx=ctx or RunContext(job_id=None))


async def detect_retakes(
    words: Sequence[IndexedWord | dict[str, Any]],
    language: str = "",
    *,
    client: ModelClient | None = None,
    ctx: RunContext | None = None,
) -> RetakeDetectorOutput:
    """Async entrypoint for the T5 CutPlan integration.

    ``words`` is the verbatim transcript as indexed entries
    ``[{"i": int, "text": str, "start_s": float, "end_s": float}, …]`` (dicts
    or ``IndexedWord`` instances); ``language`` is an optional ISO 639-1 hint.

    Short-circuits to the empty output (no LLM call) when the transcript is too
    short to contain an abandoned take plus its re-delivery. Otherwise runs the
    sync agent in a worker thread — the same ``asyncio.to_thread`` pattern the
    routes use for sibling agents.

    Raises ``ValidationError`` on malformed input and ``TerminalError`` when
    the agent exhausts its retries. Callers MUST catch and proceed with zero
    retake cuts (plans/010: agent failure ⇒ never job failure).
    """
    inp = RetakeDetectorInput.model_validate({"words": list(words), "language": language})
    if len(inp.words) < _MIN_WORDS_FOR_DETECTION:
        return RetakeDetectorOutput(retakes=[])
    return await asyncio.to_thread(run_retake_detector, inp, client=client, ctx=ctx)
