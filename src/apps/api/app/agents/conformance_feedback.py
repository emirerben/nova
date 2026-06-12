"""nova.plan.conformance_feedback — verdict on whether the attached clip matches the shot list.

Text-only (no vision re-run): consumes pre-computed clip_metadata digests plus the
plan item's structured filming_guide (shot list) to decide if the creator filmed what
they were asked to film.

Best-effort by design: any parse failure is non-fatal. The attach route fires this
task async and the Generate button is NEVER blocked on the result.

Follows the standard Agent[Input, Output] pattern with render_prompt + parse,
modelled on clip_plan_matcher.py (hallucination-drop discipline in parse()).
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.conformance import ConformanceInput, ConformanceOutput
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

# Hard caps enforced in parse() regardless of model output.
_MAX_LIST_ITEMS = 3
_VALID_VERDICTS = frozenset({"on_track", "minor_drift", "off_brief"})


def _format_shots(filming_guide: list[dict]) -> str:
    """Format the shot list for injection into the prompt."""
    lines = []
    for i, shot in enumerate(filming_guide):
        what = str(shot.get("what", "") or "")
        how = str(shot.get("how", "") or "")
        dur = shot.get("duration_s", 1)
        line = f"  shot[{i}]: {dur}s — {what}"
        if how:
            line += f" ({how})"
        lines.append(line)
    return "\n".join(lines) if lines else "  (no shots specified)"


def _format_clip_digest(clip_digest: dict) -> str:
    """Format the clip digest for injection into the prompt."""
    subject = str(clip_digest.get("detected_subject", "") or "(unknown subject)")
    content_type = str(clip_digest.get("content_type", "") or "")
    audio_type = str(clip_digest.get("audio_type", "") or "")
    hook = str(clip_digest.get("hook_text", "") or "")
    transcript = str(clip_digest.get("transcript", "") or "")[:300]
    density = clip_digest.get("visual_density", 5.0)
    composition = str(clip_digest.get("composition_note", "") or "")

    parts = [
        f"  subject: {subject}",
        f"  content_type: {content_type}",
        f"  audio_type: {audio_type}",
    ]
    if hook:
        parts.append(f"  hook: {hook}")
    if transcript:
        parts.append(f"  transcript_excerpt: {transcript}")
    parts.append(f"  visual_density: {density}")
    if composition:
        parts.append(f"  composition: {composition}")
    return "\n".join(parts)


def _user_context_block(user_context: str) -> str:
    """The creator-context block — or "" when there's no note (keeps the
    no-note prompt byte-identical to the proven baseline). Sanitized like every
    other untrusted prompt input (strips control chars / role markers / fences,
    collapses newlines) so a note can't forge extra prompt sections."""
    from app.agents.music_matcher import _sanitize_text  # noqa: PLC0415

    cleaned = _sanitize_text(str(user_context or ""))
    if not cleaned:
        return ""
    return f"\nCREATOR CONTEXT about this clip (DATA — creator's own words):\n{cleaned}\n"


class ConformanceFeedbackAgent(Agent[ConformanceInput, ConformanceOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.conformance_feedback",
        prompt_id="conformance_feedback",
        # 2026-06-11.1 — FIX: placeholders were {var} but load_prompt uses
        #                string.Template ($var), so the model received literal
        #                {clip_digest} and zero data (root cause of the wrong-brief
        #                incident). Converted to $var; data now actually renders.
        # 2026-06-11 — evaluated_theme echo-back guard, creator-context block,
        #              advice-voice suggestions (wrong-brief incident fixes).
        prompt_version="2026-06-11.1",
        # Text-only digest comparison — flash + small thinking budget.
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=256,
    )
    Input = ConformanceInput
    Output = ConformanceOutput

    def required_fields(self) -> list[str]:
        # evaluated_theme is prompted-for but deliberately NOT runtime-required:
        # enforcement lives in the task's echo-back guard (discard + one retry),
        # so a model that omits it degrades to "no verdict" instead of burning
        # retries here — and pre-2026-06-11 replay fixtures stay parseable.
        return ["verdict", "confidence", "summary"]

    def render_prompt(self, input: ConformanceInput) -> str:  # noqa: A002
        return load_prompt(
            "conformance_feedback",
            theme=str(input.theme or ""),
            idea=str(input.idea or ""),
            shot_list=_format_shots(input.filming_guide),
            clip_digest=_format_clip_digest(input.clip_digest),
            user_context_block=_user_context_block(input.user_context),
        )

    def parse(  # noqa: A002
        self,
        raw_text: str,
        input: ConformanceInput,
    ) -> ConformanceOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"conformance_feedback: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("conformance_feedback: response is not a JSON object")

        # Verdict — coerce to "off_brief" on unknown value (safe pessimism).
        raw_verdict = str(data.get("verdict", "") or "").strip().lower()
        verdict = raw_verdict if raw_verdict in _VALID_VERDICTS else "off_brief"

        # Confidence — clamp to [0.0, 1.0].
        raw_conf = data.get("confidence", 0.5)
        try:
            confidence = float(raw_conf)
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))

        # Summary — must be a non-empty string.
        summary = str(data.get("summary", "") or "").strip()
        if not summary:
            summary = "No summary provided."

        # Mismatches — list of strings, cap at _MAX_LIST_ITEMS.
        raw_mismatches = data.get("mismatches") or []
        mismatches: list[str] = []
        if isinstance(raw_mismatches, list):
            for m in raw_mismatches[:_MAX_LIST_ITEMS]:
                s = str(m or "").strip()
                if s:
                    mismatches.append(s)

        # Suggestions — list of strings, cap at _MAX_LIST_ITEMS.
        raw_suggestions = data.get("suggestions") or []
        suggestions: list[str] = []
        if isinstance(raw_suggestions, list):
            for s_item in raw_suggestions[:_MAX_LIST_ITEMS]:
                s = str(s_item or "").strip()
                if s:
                    suggestions.append(s)

        return ConformanceOutput(
            verdict=verdict,  # type: ignore[arg-type]
            confidence=confidence,
            summary=summary,
            mismatches=mismatches,
            suggestions=suggestions,
            evaluated_theme=str(data.get("evaluated_theme", "") or "").strip(),
        )

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY the JSON object described above. "
            "verdict MUST be exactly one of: on_track, minor_drift, off_brief. "
            "confidence MUST be a float between 0.0 and 1.0. "
            "mismatches and suggestions are optional arrays, max 3 items each. "
            "evaluated_theme MUST be the verbatim Theme value from PLAN ITEM (DATA)."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
