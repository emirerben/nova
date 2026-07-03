"""nova.voiceover.script_writer — write the voiceover *script* the creator reads
aloud in the "Get a transcript" helper.

NOT an adaptation of `sequence_quote_writer`: that agent caps at 40 total words /
6 words per sentence (`sequence_quote_writer.py:51-57`) because each sentence is an
on-screen typography moment. A read-aloud voiceover for 30–60s of footage is
70–140 spoken words, so those validators would reject every real script. This is a
new agent on the shared `AgentSpec` harness with its own spoken-length band and a
speech-appropriate line splitter (sentences/clauses, not a 6-word on-screen cap).

TRUST BOUNDARY: `footage_summary`, `brief`, and interview answers are UNTRUSTED
(footage-derived / user free-text). The prompt frames them as DATA; `parse()`
strips URLs/@handles/control chars from the output. The script is spoken by the
creator, never rendered as on-screen overlay text, so this is the lighter of the
two trust postures (cf. intro_writer), but the same sanitize-on-output rule holds.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt
from app.schemas.voiceover_script import target_word_count

# Spoken-length band, generous on purpose: the narrated pipeline reflows clips to
# the voice, so exact length is not required — we only reject scripts that are
# wildly off (a 3-word "script" or a 400-word monologue). Bounds are multiples of
# the ~2.3 wps target: ~0.7× to ~1.4× of the footage's target word count.
_MIN_BAND_FACTOR = 0.7
_MAX_BAND_FACTOR = 1.4
# Absolute floor/ceiling so a 3s clip can't demand a 4-word script and a 5-min
# upload can't invite a 700-word wall.
_ABS_MIN_WORDS = 12
_ABS_MAX_WORDS = 220

_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_HANDLE_RE = re.compile(r"[@#]\w+")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?im)^\s*(system|assistant|user|tool|developer)\s*[:>]\s*")
_FENCE = re.compile(r"```+")
_REFUSAL_RE = re.compile(r"\b(as an ai|i cannot|i can't|unable to|need more information)\b", re.I)


def _strip_tokens(s: str) -> str:
    """Remove URLs and @handles/#hashtags — never legitimate in a spoken script."""
    s = _URL_RE.sub("", s)
    return _HANDLE_RE.sub("", s)


def _sanitize_script(s: str) -> str:
    """Sanitize the OUTPUT script: strip prompt-injection markers, control chars,
    code fences, URLs, and handles; collapse whitespace — but do NOT length-truncate.
    A voiceover script is legitimately longer than a short field, so `_sanitize_text`'s
    400-char clamp (music_matcher) would chop a 60s script mid-sentence.
    """
    s = _CONTROL_CHARS.sub(" ", s)
    s = _ROLE_MARKERS.sub("[role-marker-stripped] ", s)
    s = _FENCE.sub("'''", s)
    s = _strip_tokens(s)
    return re.sub(r"\s+", " ", s).strip()


def _clean_input(s: str) -> str:
    """Sanitize a prompt INPUT (footage summary, brief, answer): the capped
    short-field sanitizer plus URL/handle stripping (defense-in-depth, mirrors
    intro_writer._clean_persona_field). Capping input context is desirable here.
    """
    return _strip_tokens(_sanitize_text(s))


def split_script_lines(text: str) -> list[str]:
    """Split a spoken script into teleprompter lines at sentence/clause
    boundaries — speech-appropriate, NOT the 6-word on-screen cap of
    `phrase_sequence.split_sentences`. Long sentences are broken at commas /
    coordinating conjunctions so no single line is a wall to read, but natural
    clauses stay intact.
    """
    # First split on sentence-terminal punctuation, keeping the terminator.
    sentences = re.findall(r"[^.!?]+[.!?]*", text)
    lines: list[str] = []
    for raw in sentences:
        sent = raw.strip()
        if not sent:
            continue
        if len(sent.split()) <= 12:
            lines.append(sent)
            continue
        # Long sentence: break at commas / " and " / " but " / " so " boundaries.
        parts = re.split(r"(?<=,)\s+|\s+(?=(?:and|but|so|because|then)\s)", sent)
        buf = ""
        for p in parts:
            candidate = f"{buf} {p}".strip() if buf else p.strip()
            if len(candidate.split()) > 12 and buf:
                lines.append(buf.strip())
                buf = p.strip()
            else:
                buf = candidate
        if buf.strip():
            lines.append(buf.strip())
    return [ln for ln in lines if ln]


class VoiceoverScriptWriterInput(BaseModel):
    # Light single-pass footage summary (subjects/actions/mood). Empty when the
    # analyze step fell back to brief-only (e.g. Gemini unavailable).
    footage_summary: str = ""
    # The one-line context the creator typed in the Brief step. UNTRUSTED.
    brief: str = ""
    # Clarifying-question answers, oldest first. UNTRUSTED. Empty when skipped.
    answers: list[str] = Field(default_factory=list)
    # Total footage duration in seconds — sizes the target word count.
    target_duration_s: float = 30.0


class VoiceoverScriptWriterOutput(BaseModel):
    text: str = Field(min_length=1)
    lines: list[str] = Field(default_factory=list)


class VoiceoverScriptWriterAgent(Agent[VoiceoverScriptWriterInput, VoiceoverScriptWriterOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.voiceover.script_writer",
        prompt_id="write_voiceover_script",
        prompt_version="2026-07-01",
        model="gemini-2.5-flash",
        max_attempts=3,
        timeout_s=25.0,
        thinking_budget=1024,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = VoiceoverScriptWriterInput
    Output = VoiceoverScriptWriterOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["text"]

    def _band(self, duration_s: float) -> tuple[int, int]:
        target = target_word_count(duration_s)
        lo = max(_ABS_MIN_WORDS, round(target * _MIN_BAND_FACTOR))
        hi = min(_ABS_MAX_WORDS, round(target * _MAX_BAND_FACTOR))
        # Guard against inverted band on tiny/huge durations.
        return (min(lo, hi), max(lo, hi))

    def render_prompt(self, input: VoiceoverScriptWriterInput) -> str:  # noqa: A002
        lo, hi = self._band(input.target_duration_s)
        answers = "\n".join(f"- {_clean_input(a)}" for a in input.answers if a.strip()) or "(none)"
        footage = _clean_input(input.footage_summary) or "(no footage summary available)"
        return load_prompt(
            "write_voiceover_script",
            footage_summary=footage,
            brief=_clean_input(input.brief) or "(none)",
            answers=answers,
            target_words=str(target_word_count(input.target_duration_s)),
            min_words=str(lo),
            max_words=str(hi),
            duration_s=str(round(input.target_duration_s)),
        )

    def parse(
        self,
        raw_text: str,
        input: VoiceoverScriptWriterInput,  # noqa: A002
    ) -> VoiceoverScriptWriterOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"script_writer: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("script_writer: response is not a JSON object")

        text = _sanitize_script(str(data.get("text", "") or ""))
        if not text:
            raise RefusalError("script_writer: empty text after sanitization")
        if _REFUSAL_RE.search(text):
            raise RefusalError("script_writer: refusal/meta text")

        lo, hi = self._band(input.target_duration_s)
        n_words = len(text.split())
        if not (lo <= n_words <= hi):
            # Out of the generous spoken band → retry via the harness.
            raise RefusalError(
                f"script_writer: {n_words} words outside spoken band [{lo}, {hi}] "
                f"for {round(input.target_duration_s)}s"
            )

        lines = split_script_lines(text)
        try:
            return VoiceoverScriptWriterOutput(text=text, lines=lines)
        except ValidationError as exc:
            raise SchemaError(f"script_writer: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            '\n\nIMPORTANT: return ONLY a JSON object {"text": "..."} — the spoken '
            "voiceover script as plain sentences, no markdown, no URLs/@handles, "
            "sized to the target word count in the prompt."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
