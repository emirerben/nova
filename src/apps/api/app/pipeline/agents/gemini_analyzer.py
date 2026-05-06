"""
Gemini video analysis adapter.

Handles all Gemini File API interactions:
  gemini_upload_and_wait  → upload video + poll until ACTIVE
  analyze_clip            → hook score + best moments + transcript (optional timestamps)
  analyze_template        → template recipe extraction
  transcribe              → audio transcription (Gemini primary, Whisper fallback)
  _check_refusal          → detect safety refusals + missing required fields
  _get_client             → module-level singleton (lazy init)
"""

import json
import math
import time
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.config import settings

log = structlog.get_logger()

# ── Custom Exceptions ─────────────────────────────────────────────────────────


class PollingTimeoutError(Exception):
    pass


class GeminiRefusalError(Exception):
    pass


class GeminiAnalysisError(Exception):
    pass


# ── Dataclasses ───────────────────────────────────────────────────────────────


@dataclass
class ClipMeta:
    clip_id: str  # GCS path or a hash
    transcript: str
    hook_text: str
    hook_score: float
    best_moments: list[dict]  # [{start_s, end_s, energy, description}]
    detected_subject: str = ""  # location, topic, or main subject detected from visuals/audio
    analysis_degraded: bool = False
    failed: bool = False
    clip_path: str = ""  # set by caller for fallback


@dataclass
class TemplateRecipe:
    shot_count: int
    total_duration_s: float
    hook_duration_s: float
    slots: list[dict]  # [{position, target_duration_s, priority, slot_type}]
    copy_tone: str
    caption_style: str
    beat_timestamps_s: list[float] = field(default_factory=list)
    # Aesthetic fields (from prompt improvement — two-pass analysis)
    creative_direction: str = ""
    transition_style: str = ""
    color_grade: str = "none"
    pacing_style: str = ""
    sync_style: str = "freeform"
    interstitials: list[dict] = field(default_factory=list)
    # Content-type context fields
    subject_niche: str = ""
    has_talking_head: bool = False
    has_voiceover: bool = False
    has_permanent_letterbox: bool = False


@dataclass
class AssemblyStep:
    slot: dict
    clip_id: str
    moment: dict  # {start_s, end_s, energy, description}


@dataclass
class AssemblyPlan:
    steps: list[AssemblyStep] = field(default_factory=list)


# ── Gemini client singleton ───────────────────────────────────────────────────

_gemini_client: Any = None  # genai.Client | None


def _get_client() -> Any:
    """Return module-level singleton Gemini client (lazy init)."""
    global _gemini_client
    if _gemini_client is None:
        from google import genai  # type: ignore[import]

        _gemini_client = genai.Client(api_key=settings.gemini_api_key)
    return _gemini_client


# ── File upload ───────────────────────────────────────────────────────────────


def gemini_upload_and_wait(path: str, timeout: int = 120) -> Any:
    """Upload a local file to Gemini File API and poll until state == ACTIVE.

    Retry policy: 5 attempts with backoff 3s/9s/27s/60s on transient
    Google errors (503 ServerError from genai SDK, 429 rate-limit
    ClientError). Same shape as analyze_clip — every Gemini call
    should survive a ~99s capacity dip without failing the whole job.
    Raises PollingTimeoutError if the file doesn't become ACTIVE
    within `timeout` seconds. Raises immediately on permanent 4xx
    errors (InvalidArgument etc.) or after retry exhaustion.

    Catches `google.genai.errors.APIError` subclasses (NOT
    `google.api_core.exceptions`) since the genai SDK has its own
    error hierarchy.
    """
    from google.genai import errors as genai_errors  # type: ignore[import]

    client = _get_client()

    def _is_transient_api_error(exc: Exception) -> bool:
        """503 ServerError or 429 rate-limit ClientError."""
        if not isinstance(exc, genai_errors.APIError):
            return False
        if isinstance(exc, genai_errors.ServerError):
            return True
        code = getattr(exc, "code", None)
        return code == 429 or (isinstance(code, int) and 500 <= code < 600)

    # Upload with retry for rate limits and 503 spikes
    backoff_schedule = [3.0, 9.0, 27.0, 60.0]
    max_attempts = len(backoff_schedule) + 1  # 5 total
    file_ref = None
    for attempt in range(max_attempts):
        try:
            file_ref = client.files.upload(file=path)
            break
        except genai_errors.APIError as exc:
            if not _is_transient_api_error(exc):
                raise  # permanent 4xx → fail immediately
            if attempt >= max_attempts - 1:
                raise
            backoff_s = backoff_schedule[attempt]
            log.warning(
                "gemini_upload_transient_retry",
                attempt=attempt + 1,
                of=max_attempts,
                backoff_s=backoff_s,
                error_type=type(exc).__name__,
                http_code=getattr(exc, "code", None),
                path=path,
            )
            time.sleep(backoff_s)

    if file_ref is None:
        raise GeminiAnalysisError("Failed to upload file after retries")

    # Poll until ACTIVE — same retry shape on transient errors
    deadline = time.time() + timeout
    poll_attempt = 0
    while time.time() < deadline:
        try:
            file_ref = client.files.get(name=file_ref.name)
            poll_attempt = 0  # reset on success
        except genai_errors.APIError as exc:
            if not _is_transient_api_error(exc):
                raise  # permanent error during polling — bail
            backoff_s = backoff_schedule[min(poll_attempt, len(backoff_schedule) - 1)]
            log.warning(
                "gemini_poll_transient_retry",
                attempt=poll_attempt + 1,
                backoff_s=backoff_s,
                error_type=type(exc).__name__,
                http_code=getattr(exc, "code", None),
                name=file_ref.name,
            )
            poll_attempt += 1
            time.sleep(backoff_s)
            continue

        state_name = file_ref.state.name if hasattr(file_ref.state, "name") else str(file_ref.state)
        if state_name == "ACTIVE":
            log.info("gemini_file_active", name=file_ref.name)
            return file_ref
        if state_name == "FAILED":
            raise GeminiAnalysisError(f"Gemini file processing failed: {file_ref.name}")

        log.debug("gemini_file_polling", state=state_name, name=file_ref.name)
        time.sleep(5)

    raise PollingTimeoutError(
        f"Gemini file {file_ref.name} did not become ACTIVE within {timeout}s"
    )


# ── Refusal detection ─────────────────────────────────────────────────────────


def _check_refusal(response: Any, required_fields: list[str]) -> dict:
    """Parse Gemini response JSON and check for safety refusals / missing fields.

    Returns the parsed dict on success.
    Raises GeminiRefusalError on safety refusal, JSON errors, or missing required fields.
    """
    candidates = getattr(response, "candidates", None)
    if candidates and len(candidates) > 0:
        finish_reason = candidates[0].finish_reason
        reason_name = finish_reason.name if hasattr(finish_reason, "name") else str(finish_reason)
        if reason_name == "SAFETY":
            raise GeminiRefusalError("Content policy refusal")

    try:
        data = json.loads(response.text)
    except (json.JSONDecodeError, AttributeError, TypeError) as exc:
        raise GeminiRefusalError("Invalid JSON response") from exc

    for field_name in required_fields:
        val = data.get(field_name)
        if val is None or val == "" or val == []:
            raise GeminiRefusalError(f"Missing required field: {field_name}")

    return data


# ── Clip analysis ─────────────────────────────────────────────────────────────


def analyze_clip(
    file_ref: Any,
    start_s: float | None = None,
    end_s: float | None = None,
) -> ClipMeta:
    """Analyze a video clip or time range. Returns ClipMeta.

    Retry policy: up to 5 attempts on transient Google API errors
    (503 ServiceUnavailable from "high demand" spikes, 429
    ResourceExhausted from rate limits). Exponential backoff:
    3s, 9s, 27s, 60s — total worst-case patience ~99s, which covers
    most observed Google capacity dips ("Spikes are usually
    temporary" per their 503 message). Refusals and 4xx (except
    429) fail immediately — those won't get better with retries.

    Without this retry loop a single Google capacity dip fails the
    entire template job (>50% clip-fail threshold trips at 11/20).
    With 5 attempts + 99s budget per clip, jobs survive multi-minute
    Google overload windows.

    If start_s and end_s are provided, focuses analysis on that segment.
    Raises GeminiAnalysisError on terminal failure.

    NOTE: the `google.genai` SDK (which `client.models.generate_content`
    calls into) raises its own ServerError/ClientError classes, NOT the
    `google.api_core.exceptions` ones. An earlier version of this code
    caught `gapi_exc.ServiceUnavailable` and never matched — every 503
    fell through to the catch-all and was wrapped as GeminiAnalysisError
    on the first attempt with no retries. We now catch the genai SDK's
    own error hierarchy.
    """
    from google.genai import errors as genai_errors  # type: ignore[import]

    from app.pipeline.prompt_loader import load_prompt  # noqa: PLC0415

    client = _get_client()

    if start_s is not None and end_s is not None:
        segment_instruction = f"Analyze the video segment from {start_s:.1f}s to {end_s:.1f}s."
    else:
        segment_instruction = "Analyze this video clip."

    prompt = load_prompt("analyze_clip", segment_instruction=segment_instruction)

    from google.genai import types as genai_types  # type: ignore[import]

    last_transient_exc: Exception | None = None
    # Backoff schedule: 3s, 9s, 27s, 60s between attempts 0→1, 1→2, 2→3, 3→4
    backoff_schedule = [3.0, 9.0, 27.0, 60.0]
    max_attempts = len(backoff_schedule) + 1  # 5 total attempts
    for attempt in range(max_attempts):
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=[
                    genai_types.Part.from_uri(
                        file_uri=file_ref.uri,
                        mime_type=file_ref.mime_type or "video/mp4",
                    ),
                    prompt,
                ],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )

            data = _check_refusal(response, ["hook_text", "hook_score", "best_moments"])

            hook_score = float(data.get("hook_score", 5.0))
            hook_score = max(0.0, min(10.0, hook_score))

            clip_id = getattr(file_ref, "name", str(id(file_ref)))

            return ClipMeta(
                clip_id=clip_id,
                transcript=data.get("transcript", ""),
                hook_text=data.get("hook_text", ""),
                hook_score=hook_score,
                best_moments=data.get("best_moments", []),
                detected_subject=data.get("detected_subject", ""),
            )

        except (GeminiRefusalError, GeminiAnalysisError):
            raise
        except genai_errors.APIError as exc:
            # genai SDK errors carry HTTP status code in .code (int).
            # ServerError = 5xx (Google capacity dip). ClientError 429 = rate limit.
            # Anything else (4xx) is a permanent client-side problem — bail.
            code = getattr(exc, "code", None)
            is_transient = (
                isinstance(exc, genai_errors.ServerError)
                or code == 429
                or (isinstance(code, int) and 500 <= code < 600)
            )
            if not is_transient:
                raise GeminiAnalysisError(f"Clip analysis failed: {exc}") from exc
            last_transient_exc = exc
            if attempt >= max_attempts - 1:
                break
            backoff_s = backoff_schedule[attempt]
            log.warning(
                "gemini_analyze_clip_transient_retry",
                attempt=attempt + 1,
                of=max_attempts,
                backoff_s=backoff_s,
                error_type=type(exc).__name__,
                http_code=code,
            )
            time.sleep(backoff_s)
        except Exception as exc:
            # Non-transient failure (parse errors, network) — raise immediately.
            raise GeminiAnalysisError(f"Clip analysis failed: {exc}") from exc

    # All retries exhausted on a transient error.
    raise GeminiAnalysisError(
        f"Clip analysis failed after {max_attempts} attempts "
        f"(last error: {last_transient_exc})"
    ) from last_transient_exc


# ── Template analysis ─────────────────────────────────────────────────────────


def analyze_template(
    file_ref: Any,
    analysis_mode: str = "single",
    black_segments: list[dict] | None = None,
) -> TemplateRecipe:
    """Extract a structural 'recipe' from a template video.

    Args:
        file_ref: Gemini file reference (must be ACTIVE).
        analysis_mode: "single" for one-pass expanded prompt,
                       "two_pass" for creative direction + structural extraction.
        black_segments: Optional list of black segments from FFmpeg blackdetect.
                        Passed as context to the Gemini prompt for interstitial detection.

    Failure is fatal (raises) — caller must handle.
    """
    from app.pipeline.prompt_loader import load_prompt  # noqa: PLC0415

    client = _get_client()
    from google.genai import types as genai_types  # type: ignore[import]

    # ── Build black segments context for prompts ─────────────────────
    black_segments_context = ""
    if black_segments:
        lines = ["Black segments detected by frame analysis:"]
        has_classified = False
        for seg in black_segments:
            likely_type = seg.get("likely_type")
            line = (
                f"  - {seg['start_s']:.2f}s to {seg['end_s']:.2f}s "
                f"(duration: {seg['duration_s']:.2f}s)"
            )
            if likely_type == "curtain-close":
                line += " — CURTAIN-CLOSE detected (black bars closing from top/bottom)"
                has_classified = True
            elif likely_type == "fade-black-hold":
                line += " — FADE-TO-BLACK detected (uniform darkening)"
                has_classified = True
            lines.append(line)

        if has_classified:
            lines.append(
                "Use the transition type classifications above when populating "
                "each interstitial's type field. Segments marked CURTAIN-CLOSE "
                "must use type \"curtain-close\" in the interstitials array."
            )
        else:
            lines.append(
                "These may indicate curtain-close, fade-to-black, or other "
                "transitions with a black hold between clips."
            )
        black_segments_context = "\n".join(lines)

    # ── Pass 1: creative direction (two_pass mode only) ──────────────
    creative_direction = ""
    if analysis_mode == "two_pass":
        creative_direction = _extract_creative_direction(
            client, file_ref, genai_types,
        )

    # ── Pass 2 / single-pass: structural JSON extraction ─────────────
    schema = load_prompt("analyze_template_schema")

    if analysis_mode == "two_pass" and creative_direction:
        prompt = load_prompt(
            "analyze_template_pass2",
            creative_direction=creative_direction,
            schema=schema,
            black_segments_context=black_segments_context,
        )
    else:
        prompt = load_prompt(
            "analyze_template_single",
            schema=schema,
            black_segments_context=black_segments_context,
        )

    log.info(
        "template_analysis_mode",
        mode=analysis_mode,
        has_creative_direction=bool(creative_direction),
    )

    response = client.models.generate_content(
        model=settings.gemini_model,
        contents=[
            genai_types.Part.from_uri(
                file_uri=file_ref.uri,
                mime_type=file_ref.mime_type or "video/mp4",
            ),
            prompt,
        ],
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )

    data = _check_refusal(response, ["shot_count", "slots"])

    # Semantic validation: each slot must have meaningful content
    slots = data.get("slots", [])
    for i, slot in enumerate(slots):
        duration = slot.get("target_duration_s")
        try:
            dur_val = float(duration) if duration is not None else 0.0
        except (TypeError, ValueError):
            raise GeminiRefusalError(
                f"Slot {i + 1} has non-numeric target_duration_s: {duration}"
            )
        if dur_val <= 0 or math.isnan(dur_val) or math.isinf(dur_val):
            raise GeminiRefusalError(
                f"Slot {i + 1} missing or invalid target_duration_s"
            )
        if not slot.get("slot_type"):
            raise GeminiRefusalError(
                f"Slot {i + 1} missing slot_type"
            )

    total_duration_s = float(data.get("total_duration_s", 0.0))
    slot_duration_sum = sum(float(s.get("target_duration_s", 0.0)) for s in slots)
    if total_duration_s > 0 and abs(slot_duration_sum - total_duration_s) > 5.0:
        log.warning(
            "template_slot_duration_mismatch",
            slot_sum=round(slot_duration_sum, 1),
            total_duration_s=round(total_duration_s, 1),
            delta=round(abs(slot_duration_sum - total_duration_s), 1),
        )

    # ── Validate per-slot fields ─────────────────────────────────────
    global_color_grade = str(data.get("color_grade", "none"))
    if global_color_grade not in _VALID_COLOR_HINTS:
        global_color_grade = "none"

    _validate_slots(slots, global_color_grade)

    beat_timestamps_s = sorted(
        float(t) for t in data.get("beat_timestamps_s", [])
    )

    # Use Gemini's creative_direction from JSON if single-pass, or Pass 1 output
    if not creative_direction:
        creative_direction = str(data.get("creative_direction", ""))

    # Validate sync_style
    sync_style = str(data.get("sync_style", "freeform"))
    if sync_style not in _VALID_SYNC_STYLES:
        sync_style = "freeform"

    log.info(
        "template_color_grade",
        global_grade=global_color_grade,
        per_slot_overrides=sum(
            1 for s in slots
            if s.get("color_hint", "none") != global_color_grade
        ),
    )

    # ── Validate interstitials ──────────────────────────────────────
    interstitials = _validate_interstitials(
        data.get("interstitials", []), int(data.get("shot_count", 0))
    )

    return TemplateRecipe(
        shot_count=int(data.get("shot_count", 0)),
        total_duration_s=total_duration_s,
        hook_duration_s=float(data.get("hook_duration_s", 0.0)),
        slots=slots,
        copy_tone=str(data.get("copy_tone", "casual")),
        caption_style=str(data.get("caption_style", "")),
        beat_timestamps_s=beat_timestamps_s,
        creative_direction=creative_direction,
        transition_style=str(data.get("transition_style", "")),
        color_grade=global_color_grade,
        pacing_style=str(data.get("pacing_style", "")),
        sync_style=sync_style,
        interstitials=interstitials,
        subject_niche=str(data.get("subject_niche", "")),
        has_talking_head=bool(data.get("has_talking_head", False)),
        has_voiceover=bool(data.get("has_voiceover", False)),
        has_permanent_letterbox=bool(data.get("has_permanent_letterbox", False)),
    )


# ── Validation constants ─────────────────────────────────────────────────────

_VALID_OVERLAY_ROLES = {"hook", "reaction", "cta", "label"}
_VALID_OVERLAY_EFFECTS = {
    "pop-in", "fade-in", "scale-up", "none",
    "font-cycle", "typewriter", "glitch", "bounce", "slide-in",
    "slide-up", "static",
}
_VALID_OVERLAY_POSITIONS = {"center", "top", "bottom"}
_VALID_OVERLAY_FONT_SIZES = {"small", "medium", "large", "jumbo"}
_VALID_CAMERA_MOVEMENTS = {
    "static", "pan-left", "pan-right", "tilt-up", "tilt-down",
    "zoom-in", "zoom-out", "handheld", "tracking",
}
_VALID_TRANSITION_TYPES = {"hard-cut", "whip-pan", "zoom-in", "dissolve", "curtain-close", "none"}
_VALID_COLOR_HINTS = {"warm", "cool", "high-contrast", "desaturated", "vintage", "none"}
_VALID_SYNC_STYLES = {"cut-on-beat", "transition-on-beat", "energy-match", "freeform"}


def _extract_creative_direction(client: Any, file_ref: Any, genai_types: Any) -> str:
    """Pass 1: extract freeform creative direction from template video.

    Returns empty string on any failure (graceful degradation).
    """
    from app.pipeline.prompt_loader import load_prompt  # noqa: PLC0415

    try:
        prompt = load_prompt("analyze_template_pass1")

        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[
                genai_types.Part.from_uri(
                    file_uri=file_ref.uri,
                    mime_type=file_ref.mime_type or "video/mp4",
                ),
                prompt,
            ],
            config=genai_types.GenerateContentConfig(
                max_output_tokens=400,
            ),
        )

        text = (response.text or "").strip()
        log.info(
            "template_creative_direction",
            length=len(text),
            preview=text[:200],
        )
        return text

    except Exception as exc:
        log.warning("template_creative_direction_failed", error=str(exc))
        return ""


def _validate_slots(slots: list[dict], global_color_grade: str) -> None:
    """Validate and normalize all per-slot fields in place.

    Handles text_overlays, transition_in, color_hint, and speed_factor.
    Called both at analysis time and can be called on cached recipe load.
    """
    for slot in slots:
        # ── transition_in ────────────────────────────────────────────
        transition_in = slot.get("transition_in", "none")
        if transition_in not in _VALID_TRANSITION_TYPES:
            slot["transition_in"] = "none"
        else:
            slot["transition_in"] = transition_in

        # ── color_hint (inherits global if missing/invalid) ──────────
        color_hint = slot.get("color_hint")
        if not color_hint or color_hint not in _VALID_COLOR_HINTS:
            slot["color_hint"] = global_color_grade
        # else: keep explicit per-slot override

        # ── speed_factor (clamped to [0.25, 4.0]) ───────────────────
        try:
            speed_factor = float(slot.get("speed_factor", 1.0))
        except (TypeError, ValueError):
            speed_factor = 1.0
        slot["speed_factor"] = max(0.25, min(4.0, speed_factor))

        # ── Ensure energy field exists (defaults to 5.0) ────────────
        if "energy" not in slot:
            slot["energy"] = 5.0

        # ── camera_movement (defaults to "static") ───────────────────
        camera_movement = slot.get("camera_movement", "static")
        if camera_movement not in _VALID_CAMERA_MOVEMENTS:
            slot["camera_movement"] = "static"
        else:
            slot["camera_movement"] = camera_movement

        # ── text_overlays ────────────────────────────────────────────
        raw_overlays = slot.get("text_overlays", [])
        if not isinstance(raw_overlays, list):
            slot["text_overlays"] = []
            continue
        validated = []
        slot_dur = float(slot.get("target_duration_s", 0.0))
        for ov in raw_overlays:
            if not isinstance(ov, dict):
                continue
            role = ov.get("role", "")
            if role not in _VALID_OVERLAY_ROLES:
                log.warning("template_overlay_invalid_role", role=role)
                continue
            start = float(ov.get("start_s", 0.0))
            end = float(ov.get("end_s", 0.0))
            if start >= end:
                log.warning("template_overlay_bad_timing", start_s=start, end_s=end)
                continue
            if slot_dur > 0 and start >= slot_dur:
                log.warning("template_overlay_outside_slot", start_s=start, slot_dur=slot_dur)
                continue
            ov["effect"] = (
                ov.get("effect", "none")
                if ov.get("effect") in _VALID_OVERLAY_EFFECTS
                else "none"
            )
            ov["position"] = (
                ov.get("position", "center")
                if ov.get("position") in _VALID_OVERLAY_POSITIONS
                else "center"
            )
            ov["font_size_hint"] = (
                ov.get("font_size_hint", "large")
                if ov.get("font_size_hint") in _VALID_OVERLAY_FONT_SIZES
                else "large"
            )
            ov.setdefault("has_darkening", False)
            ov.setdefault("has_narrowing", False)
            # Gemini returns overlay text in "text"; the burn pipeline reads
            # "sample_text".  Copy text → sample_text so overlays aren't
            # silently dropped when sample_text is empty.
            if not ov.get("sample_text") and ov.get("text"):
                ov["sample_text"] = ov["text"]
            ov.setdefault("sample_text", "")
            validated.append(ov)
        slot["text_overlays"] = validated


_VALID_INTERSTITIAL_TYPES = {"curtain-close", "fade-black-hold", "flash-white"}


def _validate_interstitials(raw: list, shot_count: int) -> list[dict]:
    """Validate and normalize interstitial definitions from Gemini response.

    Returns a list of validated interstitial dicts. Invalid entries are dropped.
    """
    if not isinstance(raw, list):
        return []

    validated = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        itype = item.get("type", "")
        if itype not in _VALID_INTERSTITIAL_TYPES:
            log.warning("interstitial_invalid_type", type=itype)
            continue

        try:
            after_slot = int(item.get("after_slot", 0))
        except (TypeError, ValueError):
            continue
        if after_slot < 1 or after_slot > shot_count:
            log.warning("interstitial_invalid_slot", after_slot=after_slot)
            continue

        try:
            animate_s = max(0.0, min(2.0, float(item.get("animate_s", 0.5))))
            hold_s = max(0.1, min(3.0, float(item.get("hold_s", 1.0))))
        except (TypeError, ValueError):
            animate_s = 0.5
            hold_s = 1.0

        hold_color = item.get("hold_color", "#000000")
        if hold_color not in ("#000000", "#FFFFFF"):
            hold_color = "#000000"

        validated.append({
            "after_slot": after_slot,
            "type": itype,
            "animate_s": animate_s,
            "hold_s": hold_s,
            "hold_color": hold_color,
        })

    if validated:
        log.info("interstitials_validated", count=len(validated))

    return validated


# ── Transcription ─────────────────────────────────────────────────────────────


def transcribe(file_ref: Any) -> "Transcript":  # noqa: F821
    """Transcribe audio from a Gemini file reference.

    Falls back to Whisper (via transcribe.py) on any failure.
    Returns a Transcript with low_confidence=True if both fail.
    """
    from app.pipeline.prompt_loader import load_prompt  # noqa: PLC0415
    from app.pipeline.transcribe import Transcript, Word, transcribe_whisper  # noqa: PLC0415

    client = _get_client()
    prompt = load_prompt("transcribe")

    try:
        from google.genai import types as genai_types  # type: ignore[import]

        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=[
                genai_types.Part.from_uri(
                    file_uri=file_ref.uri,
                    mime_type=file_ref.mime_type or "video/mp4",
                ),
                prompt,
            ],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )

        data = _check_refusal(response, ["full_text", "words"])

        words = [
            Word(
                text=w.get("text", ""),
                start_s=float(w.get("start_s", 0.0)),
                end_s=float(w.get("end_s", 0.0)),
                confidence=1.0,  # Gemini doesn't return per-word confidence
            )
            for w in data.get("words", [])
        ]

        return Transcript(
            words=words,
            full_text=data.get("full_text", ""),
            low_confidence=bool(data.get("low_confidence", False)),
        )

    except (GeminiRefusalError, GeminiAnalysisError, Exception) as exc:
        log.warning("gemini_transcribe_failed_falling_back", error=str(exc))

        # Whisper fallback — needs a local file path, but we only have a file_ref here.
        # The caller (transcribe.py) handles the path routing; here we signal unavailability.
        # If called directly with no path context, return degraded transcript.
        clip_path = getattr(file_ref, "_local_path", None)
        if clip_path:
            try:
                return transcribe_whisper(clip_path)
            except Exception as whisper_exc:
                log.error("whisper_fallback_also_failed", error=str(whisper_exc))

        return Transcript(words=[], full_text="", low_confidence=True)
