"""nova.compose.template_text — extract every on-screen text overlay.

Runs as a focused second pass on the same template video used by
`TemplateRecipeAgent`. The recipe agent is good at structural decomposition
(slots, transitions, style) but its `text_overlays` field is a side-concern in
a 60-line omnibus prompt — small text gets dropped, positions collapse to
top/center/bottom buckets, timing drifts.

This agent does one thing: enumerate every visible text overlay with a required
normalized bounding box, font color, and timing. Per-overlay completeness and
position fidelity are then measurable end-to-end via the eval harness against
OCR-derived ground truth.

Wired into `agentic_template_build` only — manual templates and music-job
templates are out of scope.

Flag-gated routing (slice G):
  When `settings.text_overlay_v2_enabled=True`, `run()` calls `_run_layer2()`
  which downloads the template video locally, runs the A→B→C→D→E→F→G pipeline,
  and returns a `TemplateTextOutput` in the same schema as the Layer-1 path.
  The Layer-1 single-call path is unchanged when the flag is False — all
  existing callers are unaffected. See
  `app/pipeline/text_overlay_v2/pipeline.py::run_full_pipeline` for the
  implementation.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.template_text import (
    TemplateTextInput,
    TemplateTextOutput,
    TemplateTextOverlay,
)
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()


__all__ = [
    "TemplateTextAgent",
    "TemplateTextInput",
    "TemplateTextOutput",
    "TemplateTextOverlay",
]


class TemplateTextAgent(Agent[TemplateTextInput, TemplateTextOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.template_text",
        prompt_id="extract_template_text",
        prompt_version="2026-05-17",
        model="gemini-2.5-flash",
        # Same per-token rates as template_recipe; this is a second focused
        # pass on the same uploaded video, so input tokens are dominated by
        # the prompt text + video bytes (cached by Gemini's File API).
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Text extraction is more output-heavy than recipe extraction — a
        # template with 15 overlays produces ~3KB of JSON. Bump the implicit
        # max_output cap by setting timeout higher; the real cap is set by
        # the client. Five attempts is plenty.
        timeout_s=60.0,
    )
    Input = TemplateTextInput
    Output = TemplateTextOutput

    # ── Flag-gated run() override (slice G) ──────────────────────────────────

    def run(  # type: ignore[override]
        self,
        input: TemplateTextInput | dict,  # noqa: A002
        *,
        ctx=None,
    ) -> TemplateTextOutput:
        """Run the agent, routing to the Layer-2 pipeline when the flag is on.

        When `settings.text_overlay_v2_enabled=True`: delegates to
        `_run_layer2()` which runs the full A→B→C→D→E→F→G pipeline.

        When False: runs the existing Layer-1 single-Gemini-call path via
        `super().run()`. The Layer-1 path is byte-for-byte identical to the
        pre-slice-G code — no behavior change for existing callers.
        """
        from app.agents._runtime import RunContext  # noqa: PLC0415
        from app.config import settings  # noqa: PLC0415

        ctx = ctx or RunContext()
        validated = self._validate_input(input)

        if settings.text_overlay_v2_enabled:
            return self._run_layer2(validated, ctx=ctx)

        return super().run(validated, ctx=ctx)

    def _run_layer2(
        self,
        input: TemplateTextInput,  # noqa: A002
        *,
        ctx,
    ) -> TemplateTextOutput:
        """Layer-2 path: A→B→C→D→E→F→G pipeline on the template video.

        The video is downloaded to a tempdir from GCS/S3 (via `download_to_file`).
        The pipeline then runs frame extraction → OCR → grouping → phrase
        reconstruction → transcript alignment → classification → output conversion.

        The transcript_words field on the input is forwarded to stage E.
        When empty (the default for existing callers), stage E passes phrases
        through unchanged — a safe degraded mode.

        On pipeline failure, surfaces a TerminalError so the caller's
        error-handling logic (e.g. `extract_template_text_overlays` returning
        success=False) works identically to the Layer-1 failure mode.
        """
        import os  # noqa: PLC0415
        import tempfile  # noqa: PLC0415

        from app.agents._runtime import TerminalError  # noqa: PLC0415
        from app.agents._schemas.text_alignment import TranscriptWord  # noqa: PLC0415
        from app.pipeline.text_overlay_v2.pipeline import run_full_pipeline  # noqa: PLC0415
        from app.storage import download_to_file  # noqa: PLC0415

        # Parse transcript_words from raw dicts to TranscriptWord objects.
        transcript_words: list[TranscriptWord] = []
        for w in input.transcript_words:
            try:
                transcript_words.append(TranscriptWord.model_validate(w))
            except (ValueError, TypeError, ValidationError) as exc:
                log.warning(
                    "template_text_layer2_transcript_word_invalid",
                    error=str(exc),
                    word=repr(w)[:80],
                )

        log.info(
            "template_text_layer2_start",
            file_uri=input.file_uri,
            n_transcript_words=len(transcript_words),
            n_slots=len(input.slot_boundaries_s),
            job_id=ctx.job_id,
        )

        # Download the template video from GCS/S3 to a local tempfile so
        # ffmpeg (stage A) and the OCR backend (stage B) can access it.
        with tempfile.TemporaryDirectory(prefix="nova-layer2-") as tmpdir:
            local_video = os.path.join(tmpdir, "template.mp4")
            try:
                download_to_file(input.file_uri, local_video)
            except Exception as exc:
                raise TerminalError(f"template_text_layer2: download failed — {exc}") from exc

            try:
                output = run_full_pipeline(
                    local_video,
                    transcript_words=transcript_words,
                    slot_boundaries_s=input.slot_boundaries_s,
                    job_id=ctx.job_id,
                )
            except TerminalError:
                raise
            except Exception as exc:
                raise TerminalError(f"template_text_layer2: pipeline failed — {exc}") from exc

        log.info(
            "template_text_layer2_done",
            n_overlays=len(output.overlays),
            job_id=ctx.job_id,
        )
        return output

    # ── Layer-1 single-call path (unchanged) ──────────────────────────────────

    def media_uri(self, input: TemplateTextInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: TemplateTextInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        return ["overlays"]

    def render_prompt(self, input: TemplateTextInput) -> str:  # noqa: A002
        return load_prompt(
            "extract_template_text",
            slot_boundaries_context=_render_slot_boundaries(input.slot_boundaries_s),
        )

    def parse(self, raw_text: str, input: TemplateTextInput) -> TemplateTextOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"template_text: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("template_text: response is not a JSON object")
        raw_overlays = data.get("overlays", [])
        if not isinstance(raw_overlays, list):
            raise SchemaError("template_text: overlays is not a list")

        # Clamp slot_index to the number of slots the agent was told about.
        # Out-of-range slot_index is silently coerced to the nearest valid
        # value (1 or len(slot_boundaries)) and logged. We DON'T drop the
        # overlay — losing the text entirely is worse than putting it on a
        # neighboring slot, since the renderer's cross-slot merge can recover.
        max_slot = max(len(input.slot_boundaries_s), 1)
        validated: list[TemplateTextOverlay] = []
        dropped = 0
        for i, ov in enumerate(raw_overlays):
            if not isinstance(ov, dict):
                log.warning("template_text_overlay_not_dict", index=i)
                dropped += 1
                continue
            si = ov.get("slot_index")
            if isinstance(si, (int, float)):
                si_int = int(si)
                if si_int < 1:
                    log.warning(
                        "template_text_overlay_slot_index_clamped",
                        index=i,
                        original=si_int,
                        clamped_to=1,
                    )
                    ov["slot_index"] = 1
                elif si_int > max_slot:
                    log.warning(
                        "template_text_overlay_slot_index_clamped",
                        index=i,
                        original=si_int,
                        clamped_to=max_slot,
                    )
                    ov["slot_index"] = max_slot
            # start_s <= end_s — try a swap salvage like template_recipe does.
            try:
                s = float(ov.get("start_s", 0.0))
                e = float(ov.get("end_s", 0.0))
            except (TypeError, ValueError):
                log.warning(
                    "template_text_overlay_non_numeric_timing",
                    index=i,
                    sample_text=ov.get("sample_text"),
                )
                dropped += 1
                continue
            if s > e and e >= 0.0:
                log.warning(
                    "template_text_overlay_timing_salvaged",
                    index=i,
                    sample_text=ov.get("sample_text"),
                    original_start=s,
                    original_end=e,
                )
                ov["start_s"], ov["end_s"] = e, s
                # Update s/e to reflect the corrected window so the
                # unconditional clamp below uses the post-swap values.
                s, e = e, s
            # Always clamp bbox.sample_frame_t into the resolved [s, e] window.
            # This covers two failure modes:
            #   1. Inversion case (salvage ran above): sample_frame_t may sit
            #      outside the corrected window (PR #198 prod failure).
            #   2. Non-inversion case: Gemini returns valid start/end but
            #      sample_frame_t drifts just outside the window (e.g. 0.07
            #      with start_s=2.0) — salvage never fires, but the downstream
            #      structural validator still rejects (life_s_richness_3 prod
            #      failure surfaced during PR #199).
            _clamp_sample_frame_t(ov, s, e, i)
            try:
                validated.append(TemplateTextOverlay.model_validate(ov))
            except ValidationError as exc:
                log.warning(
                    "template_text_overlay_invalid",
                    index=i,
                    sample_text=ov.get("sample_text"),
                    errors=str(exc),
                )
                dropped += 1
                continue

        if dropped:
            log.info("template_text_overlays_dropped", count=dropped, kept=len(validated))

        try:
            return TemplateTextOutput(overlays=validated)
        except ValidationError as exc:
            raise SchemaError(f"template_text: output validation — {exc}") from exc


# ── Parse helpers ─────────────────────────────────────────────────────────────


def _clamp_sample_frame_t(ov: dict, start_s: float, end_s: float, index: int) -> None:
    """Clamp ``bbox.sample_frame_t`` into ``[start_s, end_s]`` in-place.

    Mutates *ov* only when the value actually moves. Logs once per clamped
    overlay so callers can trace which overlays were adjusted without flooding
    the log on every parse call.

    This runs unconditionally after timing resolution so it covers both:
    - the inversion case (salvage swapped start/end, s/e reflect new window)
    - the non-inversion case (valid timings but sample_frame_t drifts outside)
    """
    bbox = ov.get("bbox")
    if not isinstance(bbox, dict):
        return
    try:
        raw_t = float(bbox.get("sample_frame_t", start_s))
        clamped_t = max(start_s, min(end_s, raw_t))
        if clamped_t != raw_t:
            log.warning(
                "template_text_overlay_bbox_sample_frame_t_clamped",
                index=index,
                sample_text=ov.get("sample_text"),
                sample_frame_t_clamped_from=raw_t,
                sample_frame_t_clamped_to=clamped_t,
            )
            bbox["sample_frame_t"] = clamped_t
    except (TypeError, ValueError):
        pass  # non-numeric sample_frame_t caught by model_validate in caller


# ── Prompt helpers ────────────────────────────────────────────────────────────


def _render_slot_boundaries(boundaries: list[tuple[float, float]]) -> str:
    """Format slot boundaries for prompt injection.

    Empty list returns an empty string so the prompt's $slot_boundaries_context
    substitution becomes a no-op when the agent is invoked without recipe
    context (e.g. in unit tests).
    """
    if not boundaries:
        return ""
    lines = ["Slot boundaries (global timeline, seconds):"]
    for i, (s, e) in enumerate(boundaries, start=1):
        lines.append(f"  slot {i}: {s:.2f}s → {e:.2f}s")
    lines.append(
        "Attribute each overlay to the slot whose window contains its start_s. "
        "An overlay that straddles a slot boundary belongs to the slot it starts in."
    )
    return "\n".join(lines)
