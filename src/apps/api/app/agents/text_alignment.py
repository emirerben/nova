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
import re
import unicodedata
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

_FORBIDDEN_MARKERS = ("[overlap_truncated]", "<truncated>")
_ESCAPE_SEQUENCES = ("\\n", "\\N")
_WHITESPACE_RE = re.compile(r"\s+")
# ASS subtitle tag pattern: {\an5}, {\fs120}, {\fad(500,500)}, etc.
# These reach the renderer via `sample_text` → drawtext/ASS filter and would
# be interpreted as positioning/style overrides instead of literal text.
# Strip pre-emptively; the LLM should never emit them but defense-in-depth.
_ASS_TAG_RE = re.compile(r"\{[^}]*\}")
# Unicode control + format codepoints that aren't tab. Includes zero-width
# joiners (U+200B-200D), RTL override (U+202E), line/paragraph separators
# (U+2028/2029), C0/C1 controls. Tabs survive because some legitimate text
# might use them; whitespace normalization collapses them anyway.
_ALLOWED_CONTROL_CHARS = {"\t", "\n"}


def _strip_unicode_controls(text: str) -> str:
    """Remove Unicode control + format codepoints except tab/newline.

    Newlines survive this pass because `_ESCAPE_SEQUENCES` and `_WHITESPACE_RE`
    handle them downstream — bypassing them here lets the existing logic do
    its job. RTL overrides and zero-width joiners cannot be normalised away
    by whitespace collapse, so they get stripped explicitly here.
    """
    return "".join(
        ch for ch in text if ch in _ALLOWED_CONTROL_CHARS or unicodedata.category(ch)[0] != "C"
    )


def _sanitize_aligned_line(line: str) -> str:
    """Deterministic cleanup of structurally forbidden content in LLM output.

    The transcript-authoritative prompt explicitly forbids literal `\\n`,
    `\\N`, and debug markers. This function also defends against shapes the
    prompt cannot prevent: ASS subtitle tags `{\\an5}`/`{\\fs120}` would reach
    the renderer and be interpreted as positioning/style overrides instead of
    text; Unicode control + format codepoints (RTL overrides, zero-width
    joiners, line separators) can flip rendering direction or break layout
    even if the model never emits them deliberately.

    Intentionally does NOT collapse adjacent identical word tokens. Stage D's
    OCR-level dedup already catches re-detection garbage upstream, and the
    LLM prompt instructs the model to dedup at content level. Doing it again
    here would corrupt legitimate transcript content like song refrains
    ("rain rain go away", "Whoa whoa whoa") where the repetition IS the
    intended on-screen text.
    """
    if not line:
        return line
    cleaned = line
    cleaned = _ASS_TAG_RE.sub("", cleaned)
    cleaned = _strip_unicode_controls(cleaned)
    for marker in _FORBIDDEN_MARKERS:
        cleaned = cleaned.replace(marker, "")
    for esc in _ESCAPE_SEQUENCES:
        cleaned = cleaned.replace(esc, " ")
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    return cleaned


class TextAlignmentAgent(Agent[TextAlignmentInput, TextAlignmentOutput]):
    """Stage E: correct OCR phrase text using the audio transcript as ground truth."""

    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.text_alignment",
        prompt_id="align_overlay_to_transcript",
        # 2026-05-21: prompt rewrite — keep-OCR-on-no-match fallback +
        # one-transcript-word-per-phrase uniqueness rule. Replaces the prior
        # "drop on no match" guidance that was silently producing missing
        # overlays AND stacked-duplicate overlays whenever the transcript
        # was shorter than the OCR phrase set (template fdaf3bbc 2026-05-21).
        prompt_version="2026-05-21-ocr-fallback",
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
        # `mode_directive` is the load-bearing instruction in the prompt:
        # atomized input (one OCR word per phrase) needs single-word output
        # per phrase, while phrase-mode input (multi-word OCR per phrase)
        # may concatenate multiple transcript words. Picking the wrong
        # directive at runtime causes the prompt to either drop content
        # (atomized→phrase mode collapses words) or over-stuff content
        # (phrase→atomized mode merged 'if you put in' into one overlay
        # spanning 2 seconds in prod after the v0.4.34.0 reanalyze).
        if input.atomize_mode:
            mode_directive = (
                "ATOMIZED INPUT. Each phrase has one line representing ONE OCR "
                "word at its first-appeared moment. For each phrase, output "
                "exactly ONE transcript word — the one whose timestamp is "
                "closest to `phrase.start_t_s`, preferring a fuzzy text match "
                "to the OCR line over pure timestamp proximity when the two "
                "disagree. NEVER concatenate multiple transcript words into a "
                "single output line. Each transcript word may be assigned to "
                "AT MOST ONE phrase — see the Uniqueness section below. If no "
                "transcript word is within [phrase.start_t_s - 0.5, "
                "phrase.start_t_s + 0.5], OR no eligible word shares any "
                "letters with the OCR line, OR every eligible word has been "
                "claimed by an earlier phrase, fall back to OCR per the OCR "
                "Fallback section — do NOT drop the phrase."
            )
        else:
            mode_directive = (
                "PHRASE-MODE INPUT. Each phrase represents a multi-word OCR "
                "block. For each phrase, output the transcript word(s) whose "
                "timestamps overlap the phrase's visible window with a small "
                "tolerance, concatenated in transcript order with single "
                "spaces. Drop phrases with no transcript overlap."
            )
        return load_prompt(
            "align_overlay_to_transcript",
            phrases_json=json.dumps(phrases_payload, ensure_ascii=False),
            transcript_json=json.dumps(transcript_payload, ensure_ascii=False),
            mode_directive=mode_directive,
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
            sanitized = [_sanitize_aligned_line(str(ln)) for ln in lines]
            # If sanitization emptied every line, treat the phrase as dropped
            # (model returned only forbidden content for it).
            if not any(s for s in sanitized):
                log.warning(
                    "text_alignment_entry_emptied_by_sanitizer",
                    phrase_index=idx,
                    raw=[str(ln)[:40] for ln in lines],
                )
                continue
            corrected_lines_by_index[idx] = sanitized

        # ── 3b. Atomized-mode uniqueness defense ─────────────────────────────
        # The prompt instructs the LLM to assign each transcript word to at
        # most one OCR phrase, but the LLM repeatedly violates this when the
        # transcript is shorter than the OCR phrase set — see template
        # fdaf3bbc 2026-05-21 ("allow" emitted 3× for phrases "allow" /
        # "anyone" / "diminish" at 9.5-10.0). Walk phrases in start-order and
        # strip later assignments of identical text when they overlap within
        # the same window the Stage-D dedup uses, so the OCR-fallback path
        # below takes over and the surviving overlay set stays distinct.
        if input.atomize_mode:
            from app.pipeline.text_overlay_v2.phrases import (  # noqa: PLC0415
                ATOMIZED_DEDUP_GAP_THRESHOLD_S,
            )

            ordered = sorted(enumerate(input.phrases), key=lambda pair: pair[1].start_t_s)
            last_end_by_text: dict[str, float] = {}
            for original_idx, phrase in ordered:
                assigned = corrected_lines_by_index.get(original_idx)
                if assigned is None:
                    continue
                key = "\n".join(line.strip().casefold() for line in assigned)
                prev_end = last_end_by_text.get(key)
                if prev_end is not None and (
                    phrase.start_t_s - prev_end <= ATOMIZED_DEDUP_GAP_THRESHOLD_S
                ):
                    log.info(
                        "text_alignment_uniqueness_revert",
                        phrase_index=original_idx,
                        conflicting_text=key[:60],
                        template_id=input.template_id,
                    )
                    del corrected_lines_by_index[original_idx]
                    continue
                last_end_by_text[key] = phrase.end_t_s

        # ── 3c. Atomized-mode single-word defense ────────────────────────────
        # Stage D atomized phrases carry exactly one OCR word per phrase. The
        # prompt directive at runtime (`mode_directive`) explicitly says
        # "NEVER concatenate multiple transcript words into a single output
        # line" — but the LLM violates it, producing strings like "luck just
        # is a" for an OCR phrase that was just ["luck"]. The downstream
        # `_is_atomized` check (line_grouping.py) then rejects the phrase as
        # non-atomized, so it falls out of cumulative-reveal grouping and
        # emits as a multi-word singleton overlay — visually a different
        # shape from the surrounding cumulative reveals, and often clashing
        # with neighbours at similar y_norm.
        #
        # Revert any multi-word output for an atomized input back to the OCR
        # text, which goes through the same single-word fallback path used
        # when the LLM doesn't return an entry at all. Evidence: prod
        # template 89cde014 reanalyze (2026-05-22 18:19): phrases #12-#18
        # emerged as singletons because Stage E expanded single-word OCR
        # ("luck", "just", "combination", "and", "good", "timing", "allow",
        # "diminish", ...) into multi-word strings.
        if input.atomize_mode:
            multi_word_violations: list[tuple[int, list[str]]] = []
            for original_idx, lines in list(corrected_lines_by_index.items()):
                if any(len(ln.split()) > 1 for ln in lines):
                    multi_word_violations.append((original_idx, lines))
                    del corrected_lines_by_index[original_idx]
            if multi_word_violations:
                log.info(
                    "text_alignment_atomized_multi_word_revert",
                    count=len(multi_word_violations),
                    sample=[(idx, lines[0][:40]) for idx, lines in multi_word_violations[:5]],
                    template_id=input.template_id,
                )

        # ── 4. Rebuild the Phrase list with corrected lines ───────────────────
        kept: list[Phrase] = []
        dropped = 0

        for original_idx, original_phrase in enumerate(input.phrases):
            corrected = corrected_lines_by_index.get(original_idx)
            if corrected is None:
                # Fix B (2026-05-21): keep OCR text when the LLM has no match
                # to offer instead of dropping. Dropping produced missing
                # overlays whenever the transcript was incomplete; the OCR
                # text is what's actually visible on screen during that
                # window, so showing it is strictly better than nothing.
                # Run the same sanitiser the LLM output goes through so we
                # don't slip ASS tags or controls through.
                ocr_fallback = [_sanitize_aligned_line(ln) for ln in original_phrase.lines]
                ocr_fallback = [ln for ln in ocr_fallback if ln]
                if not ocr_fallback:
                    log.info(
                        "text_alignment_phrase_dropped_empty_ocr",
                        phrase_index=original_idx,
                        sample_text=original_phrase.sample_text[:60],
                        template_id=input.template_id,
                    )
                    dropped += 1
                    continue
                log.info(
                    "text_alignment_phrase_kept_via_ocr_fallback",
                    phrase_index=original_idx,
                    ocr=original_phrase.sample_text[:60],
                    template_id=input.template_id,
                )
                corrected = ocr_fallback

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
        # Best-effort stage_e_summary so /admin/jobs Debug tab surfaces the
        # leakage signal: phrases_in vs kept, with drop attribution.
        try:
            from app.services.pipeline_trace import record_pipeline_event  # noqa: PLC0415

            record_pipeline_event(
                "overlay",
                "stage_e_summary",
                {
                    "phrases_in": len(input.phrases),
                    "transcript_words_in": len(input.transcript_words),
                    "phrases_kept": len(kept),
                    "phrases_dropped": dropped,
                    "atomize_mode": input.atomize_mode,
                },
            )
        except Exception:  # noqa: BLE001
            pass
        return TextAlignmentOutput(phrases=kept, dropped_count=dropped)

    # ── Override run() for empty-input short-circuit ──────────────────────────

    def run(  # type: ignore[override]
        self,
        input: TextAlignmentInput | dict,  # noqa: A002
        *,
        ctx=None,
    ) -> TextAlignmentOutput:
        """Skip the LLM call entirely when there are no phrases to align,
        or when the transcript is empty (music-only / caption-only templates)."""
        from app.agents._runtime import RunContext  # noqa: PLC0415

        ctx = ctx or RunContext()
        validated = self._validate_input(input)

        # ── 1. Empty-transcript early return ─────────────────────────────────
        # Music-only and caption-only templates produce an empty Whisper
        # transcript (no speech to align against).  Without this guard the LLM
        # would receive transcript_words=[] and drop every phrase, producing
        # zero overlays.  Apple Vision / Cloud Vision OCR is already accurate
        # on these templates; pass phrases through unchanged.
        if not validated.transcript_words and validated.phrases:
            log.info(
                "text_alignment_skipped_empty_transcript",
                phrase_count=len(validated.phrases),
                template_id=validated.template_id,
            )
            return TextAlignmentOutput(phrases=list(validated.phrases), dropped_count=0)

        # ── 2. Empty-phrase early return ──────────────────────────────────────
        if not validated.phrases:
            log.info("text_alignment_skipped_empty_phrases", template_id=validated.template_id)
            return TextAlignmentOutput(phrases=[], dropped_count=0)

        return super().run(validated, ctx=ctx)
