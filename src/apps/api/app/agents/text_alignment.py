"""nova.compose.text_alignment — stage E of the Layer-2 text-overlay pipeline.

Receives the phrase list from stage D (phrase reconstruction) alongside the
Whisper audio transcript.  Uses a small Gemini-Flash call to:

  - Find each OCR phrase's matching span in the transcript.
  - Replace OCR character errors with the transcript's spelling/casing/punctuation.
  - Drop phrases that have no plausible transcript match (likely OCR hallucinations
    on background graphics).

Line breaks inside a phrase's `lines` list are PRESERVED — they encode visual
build-up / typewriter structure that the audio transcript does not capture.

This agent is NOT wired into the pipeline orchestrator yet; that happens in slice G
behind the `text_overlay_v2_enabled` flag.  See the Layer-2 architecture doc at
~/.claude/plans/template-text-overlay-layer-2-architecture.md for sequencing.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.text_alignment import TextAlignmentInput, TextAlignmentOutput
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

__all__ = [
    "TextAlignmentAgent",
    "TextAlignmentInput",
    "TextAlignmentOutput",
]


class TextAlignmentAgent(Agent[TextAlignmentInput, TextAlignmentOutput]):
    """Stage E: correct OCR phrase text using the audio transcript as ground truth."""

    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.text_alignment",
        prompt_id="align_overlay_to_transcript",
        prompt_version="2026-05-18",
        model="gemini-2.5-flash",
        # Alignment is a small focused call: ~1k tokens in, ~200 tokens out.
        # Cost estimate per design doc: ~$0.0005 per call.
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        timeout_s=30.0,
    )
    Input = TextAlignmentInput
    Output = TextAlignmentOutput

    def required_fields(self) -> list[str]:
        return ["aligned_phrases"]

    def render_prompt(self, input: TextAlignmentInput) -> str:  # noqa: A002
        phrases_payload = [
            {
                "index": i,
                "start_t_s": p.start_t_s,
                "end_t_s": p.end_t_s,
                "lines": p.lines,
            }
            for i, p in enumerate(input.phrases)
        ]
        transcript_payload = [
            {
                "text": w.text,
                "start_s": w.start_s,
                "end_s": w.end_s,
            }
            for w in input.transcript_words
        ]
        return load_prompt(
            "align_overlay_to_transcript",
            phrases_json=json.dumps(phrases_payload, ensure_ascii=False),
            transcript_json=json.dumps(transcript_payload, ensure_ascii=False),
        )

    def parse(self, raw_text: str, input: TextAlignmentInput) -> TextAlignmentOutput:  # noqa: A002
        # ── 1. Empty-input early return (no LLM needed) ───────────────────────
        # The `run()` path still calls `parse()` after `invoke()`, but we guard
        # here too so tests that bypass `run()` and call `parse()` directly also
        # short-circuit cleanly.  (The real early-return that skips the LLM call
        # entirely lives in `run()` — see below.)
        if not input.phrases:
            return TextAlignmentOutput(phrases=[], dropped_count=0)

        # ── 2. Parse JSON ─────────────────────────────────────────────────────
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"text_alignment: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("text_alignment: response is not a JSON object")

        raw_aligned = data.get("aligned_phrases")
        if not isinstance(raw_aligned, list):
            raise SchemaError("text_alignment: 'aligned_phrases' is not a list")

        # ── 3. Build an index → lines map from the LLM output ────────────────
        corrected_lines_by_index: dict[int, list[str]] = {}
        for i, entry in enumerate(raw_aligned):
            if not isinstance(entry, dict):
                log.warning("text_alignment_entry_not_dict", entry_index=i)
                continue
            idx = entry.get("index")
            if not isinstance(idx, int):
                log.warning("text_alignment_entry_missing_index", entry_index=i)
                continue
            lines = entry.get("lines")
            if not isinstance(lines, list) or not lines:
                log.warning("text_alignment_entry_missing_lines", phrase_index=idx)
                continue
            if not all(isinstance(ln, str) for ln in lines):
                log.warning("text_alignment_entry_non_string_lines", phrase_index=idx)
                continue
            corrected_lines_by_index[idx] = [str(ln) for ln in lines]

        # ── 4. Rebuild the Phrase list with corrected lines ───────────────────
        kept: list[Phrase] = []
        dropped = 0

        for original_idx, original_phrase in enumerate(input.phrases):
            corrected = corrected_lines_by_index.get(original_idx)
            if corrected is None:
                log.info(
                    "text_alignment_phrase_dropped",
                    phrase_index=original_idx,
                    sample_text=original_phrase.sample_text[:60],
                    template_id=input.template_id,
                )
                dropped += 1
                continue

            # Guard: LLM must return the same number of lines so line-break
            # structure is preserved.  If it collapses or splits lines, fall
            # back to the original OCR lines and log a warning — a partial
            # fix is better than losing the phrase entirely.
            if len(corrected) != len(original_phrase.lines):
                log.warning(
                    "text_alignment_line_count_mismatch",
                    phrase_index=original_idx,
                    expected=len(original_phrase.lines),
                    got=len(corrected),
                    template_id=input.template_id,
                )
                corrected = original_phrase.lines  # fall back to OCR text

            try:
                rebuilt = Phrase(
                    lines=corrected,
                    start_t_s=original_phrase.start_t_s,
                    end_t_s=original_phrase.end_t_s,
                    aabb=original_phrase.aabb,
                    mean_confidence=original_phrase.mean_confidence,
                )
            except (ValidationError, ValueError) as exc:
                log.warning(
                    "text_alignment_phrase_rebuild_failed",
                    phrase_index=original_idx,
                    error=str(exc),
                )
                dropped += 1
                continue

            log.debug(
                "text_alignment_phrase_aligned",
                phrase_index=original_idx,
                original=original_phrase.sample_text[:60],
                corrected=rebuilt.sample_text[:60],
            )
            kept.append(rebuilt)

        log.info(
            "text_alignment_done",
            kept=len(kept),
            dropped=dropped,
            template_id=input.template_id,
        )
        return TextAlignmentOutput(phrases=kept, dropped_count=dropped)

    # ── Override run() for empty-input short-circuit ──────────────────────────

    def run(  # type: ignore[override]
        self,
        input: TextAlignmentInput | dict,  # noqa: A002
        *,
        ctx=None,
    ) -> TextAlignmentOutput:
        """Skip the LLM call entirely when there are no phrases to align."""
        from app.agents._runtime import RunContext  # noqa: PLC0415

        ctx = ctx or RunContext()
        validated = self._validate_input(input)
        if not validated.phrases:
            log.info("text_alignment_skipped_empty_phrases", template_id=validated.template_id)
            return TextAlignmentOutput(phrases=[], dropped_count=0)
        return super().run(validated, ctx=ctx)
