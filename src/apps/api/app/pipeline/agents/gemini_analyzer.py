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
    # Floor for slot count — when set, consolidate_slots will not merge below
    # this count (snappier pacing for templates that want all the cuts).
    min_slots: int = 0
    # Render-side controls.
    # output_fit: "crop" (default — center-crop sides on 16:9 source) or
    # "letterbox" (preserve full source frame, fill 9:16 canvas with blurred bg
    # of the same source). Use "letterbox" for sports / wide-action templates
    # where the action covers the full horizontal frame.
    # clip_filter_hint: optional natural-language constraint prepended to the
    # Gemini analyze_clip prompt — biases best_moments selection per template
    # (e.g. "ball must be in frame" for football highlights).
    output_fit: str = "crop"
    clip_filter_hint: str = ""


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
    filter_hint: str = "",
) -> ClipMeta:
    """Analyze a video clip / time range — returns ClipMeta.

    SHIM (v0.2): delegates to `app.agents.clip_metadata.ClipMetadataAgent`.
    Retry/refusal/schema handling is owned by the agent runtime now. This
    function survives only to preserve the legacy return type (`ClipMeta`)
    and exception classes (`GeminiAnalysisError` / `GeminiRefusalError`) for
    callers that haven't migrated yet.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import TerminalError  # noqa: PLC0415
    from app.agents.clip_metadata import (  # noqa: PLC0415
        ClipMetadataAgent,
        ClipMetadataInput,
        TimeRange,
    )

    segment = (
        TimeRange(start_s=start_s, end_s=end_s)
        if start_s is not None and end_s is not None
        else None
    )
    inp = ClipMetadataInput(
        file_uri=file_ref.uri,
        file_mime=getattr(file_ref, "mime_type", None) or "video/mp4",
        segment=segment,
        filter_hint=filter_hint or "",
    )
    agent = ClipMetadataAgent(default_client())
    try:
        out = agent.run(inp)
    except TerminalError as exc:
        # Translate runtime taxonomy back to legacy exception classes.
        from app.agents._runtime import RefusalError, SchemaError  # noqa: PLC0415

        cause = exc.__cause__
        if isinstance(cause, (RefusalError, SchemaError)) or "refusal" in str(exc) or "schema" in str(exc):
            raise GeminiRefusalError(str(exc)) from exc
        raise GeminiAnalysisError(str(exc)) from exc

    clip_id = getattr(file_ref, "name", str(id(file_ref)))
    return ClipMeta(
        clip_id=clip_id,
        transcript=out.transcript,
        hook_text=out.hook_text,
        hook_score=out.hook_score,
        best_moments=[m.model_dump() for m in out.best_moments],
        detected_subject=out.detected_subject,
    )


# ── Domain-specific moment post-filter ────────────────────────────────────
# When recipe.clip_filter_hint mentions football/ball, drop best_moments whose
# description marks them as non-action (celebration, walking, training, idle).
# Bilingual (EN + TR) keyword lists — Gemini may describe in either language
# depending on clip language hints.

_BALL_WHITELIST = (
    "ball", "top", "shot", "şut", "vuruş", "pass", "pas", "asist",
    "goal", "gol", "dribble", "çalım", "save", "kurtarış", "kurtardı",
    "tackle", "tekleme", "header", "kafa", "cross", "orta", "ortaladı",
    "strike", "volley", "voleyle", "korner", "corner", "freekick", "frikik",
    "penalty", "penaltı", "intercept", "kesme", "control", "kontrol",
    "attack", "atak", "hücum", "finish", "bitiriş",
)
_BALL_BLACKLIST = (
    "celebration", "kutlama", "celebrating",
    "empty", "boş", "geniş plan", "wide shot",
    "walking", "yürüyor", "walks",
    "training", "antrenman", "warmup", "ısınma",
    "bench", "yedek",
    "stoppage", "oyun durması", "stopped",
    "idle", "hareketsiz",
    "tribün", "stand", "audience", "crowd",
    "referee", "hakem", "linesman", "yan hakem",
    "interview", "röportaj",
    "halftime", "devre arası",
    "no ball", "topsuz",
)


def _filter_moments_by_action(moments: list, hint: str) -> list:
    """Filter moments down to ball-action-only when the hint is football.

    Three-tier output:
      1) Ball-action moments (whitelist match) — always kept.
      2) Neutral moments (no whitelist, no blacklist match) — dropped only if
         we have ≥2 whitelist hits already (better to keep variety than over-filter).
      3) Blacklisted/empty-description moments — always dropped when football.

    Only activates when the filter hint mentions football/ball. Other templates
    skip the filter and pass through unchanged.

    Fallback: if the filter would empty the list, keep the highest-energy
    moment so downstream matcher always has something to work with.
    """
    hint_lower = hint.lower()
    if not any(k in hint_lower for k in ("ball", "top", "futbol", "football", "soccer")):
        return moments

    whitelist_hits: list = []
    neutral: list = []
    for m in moments:
        if not isinstance(m, dict):
            continue
        desc = (m.get("description") or "").lower()
        if not desc:
            # Empty description is a red flag for football — Gemini knows the
            # description rules and silence usually means non-action filler.
            continue
        if any(b in desc for b in _BALL_BLACKLIST):
            continue
        if any(w in desc for w in _BALL_WHITELIST):
            whitelist_hits.append(m)
        else:
            neutral.append(m)

    if len(whitelist_hits) >= 2:
        kept = whitelist_hits  # plenty of action; drop neutral filler
    else:
        kept = whitelist_hits + neutral  # need fallback variety

    if not kept and moments:
        kept = [max(moments, key=lambda m: m.get("energy", 0) if isinstance(m, dict) else 0)]
    return kept


# ── Template analysis ─────────────────────────────────────────────────────────


def analyze_template(
    file_ref: Any,
    analysis_mode: str = "single",
    black_segments: list[dict] | None = None,
) -> TemplateRecipe:
    """Extract a structural 'recipe' from a template video — returns TemplateRecipe.

    SHIM (v0.2): delegates to `app.agents.template_recipe.TemplateRecipeAgent`.
    Two-pass mode is orchestrated here (Pass 1 creative_direction extracted via
    the existing `_extract_creative_direction` helper, then handed into the
    agent for Pass 2). Pass 1 will move to its own agent (`creative_direction`)
    in a follow-up; this shim keeps callers unchanged in the meantime.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RefusalError, SchemaError, TerminalError  # noqa: PLC0415
    from app.agents.template_recipe import (  # noqa: PLC0415
        BlackSegment,
        TemplateRecipeAgent,
        TemplateRecipeInput,
    )

    # ── Pass 1 (still inline — moves to creative_direction agent next) ──
    creative_direction = ""
    if analysis_mode == "two_pass":
        from google.genai import types as genai_types  # type: ignore[import]
        client = _get_client()
        creative_direction = _extract_creative_direction(client, file_ref, genai_types)

    # ── Pass 2 / single-pass via agent ───────────────────────────
    bsegs = [
        BlackSegment(
            start_s=float(s["start_s"]),
            end_s=float(s["end_s"]),
            duration_s=float(s["duration_s"]),
            likely_type=s.get("likely_type"),
        )
        for s in (black_segments or [])
    ]
    inp = TemplateRecipeInput(
        file_uri=file_ref.uri,
        file_mime=getattr(file_ref, "mime_type", None) or "video/mp4",
        analysis_mode="two_pass_part2" if (analysis_mode == "two_pass" and creative_direction) else "single",
        creative_direction=creative_direction,
        black_segments=bsegs,
    )

    log.info(
        "template_analysis_mode",
        mode=analysis_mode,
        has_creative_direction=bool(creative_direction),
    )

    agent = TemplateRecipeAgent(default_client())
    try:
        out = agent.run(inp)
    except TerminalError as exc:
        cause = exc.__cause__
        if isinstance(cause, (RefusalError, SchemaError)) or "refusal" in str(exc) or "schema" in str(exc):
            raise GeminiRefusalError(str(exc)) from exc
        raise GeminiAnalysisError(str(exc)) from exc

    log.info(
        "template_color_grade",
        global_grade=out.color_grade,
        per_slot_overrides=sum(
            1 for s in out.slots if s.get("color_hint", "none") != out.color_grade
        ),
    )

    return TemplateRecipe(
        shot_count=out.shot_count,
        total_duration_s=out.total_duration_s,
        hook_duration_s=out.hook_duration_s,
        slots=out.slots,
        copy_tone=out.copy_tone,
        caption_style=out.caption_style,
        beat_timestamps_s=out.beat_timestamps_s,
        creative_direction=out.creative_direction,
        transition_style=out.transition_style,
        color_grade=out.color_grade,
        pacing_style=out.pacing_style,
        sync_style=out.sync_style,
        interstitials=out.interstitials,
        subject_niche=out.subject_niche,
        has_talking_head=out.has_talking_head,
        has_voiceover=out.has_voiceover,
        has_permanent_letterbox=out.has_permanent_letterbox,
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


def _extract_creative_direction(client: Any, file_ref: Any, genai_types: Any) -> str:  # noqa: ARG001
    """Pass 1 shim — delegates to `CreativeDirectionAgent`.

    Returns empty string on any failure (graceful degradation, legacy contract).
    The `client` and `genai_types` args are ignored; kept for signature parity
    with internal callers that hand-pass them.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import TerminalError  # noqa: PLC0415
    from app.agents.creative_direction import (  # noqa: PLC0415
        CreativeDirectionAgent,
        CreativeDirectionInput,
    )

    inp = CreativeDirectionInput(
        file_uri=file_ref.uri,
        file_mime=getattr(file_ref, "mime_type", None) or "video/mp4",
    )
    try:
        out = CreativeDirectionAgent(default_client()).run(inp)
    except TerminalError as exc:
        log.warning("template_creative_direction_failed", error=str(exc))
        return ""
    log.info("template_creative_direction", length=len(out.text), preview=out.text[:200])
    return out.text


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
    """Transcribe audio from a Gemini file reference — returns Transcript.

    SHIM (v0.2): delegates to `app.agents.transcript.TranscriptAgent` for the
    Gemini path. On any agent failure, falls through to Whisper via
    `transcribe_whisper(local_path)` if `file_ref._local_path` is attached
    (set by callers in `app.pipeline.transcribe.transcribe()`). If both fail,
    returns a degraded Transcript with `low_confidence=True`.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import TerminalError  # noqa: PLC0415
    from app.agents.transcript import TranscriptAgent, TranscriptInput  # noqa: PLC0415
    from app.pipeline.transcribe import Transcript, Word, transcribe_whisper  # noqa: PLC0415

    inp = TranscriptInput(
        file_uri=file_ref.uri,
        file_mime=getattr(file_ref, "mime_type", None) or "video/mp4",
    )
    try:
        out = TranscriptAgent(default_client()).run(inp)
        return Transcript(
            words=[
                Word(text=w.text, start_s=w.start_s, end_s=w.end_s, confidence=w.confidence)
                for w in out.words
            ],
            full_text=out.full_text,
            low_confidence=out.low_confidence,
        )
    except TerminalError as exc:
        log.warning("gemini_transcribe_failed_falling_back", error=str(exc))

    clip_path = getattr(file_ref, "_local_path", None)
    if clip_path:
        try:
            return transcribe_whisper(clip_path)
        except Exception as whisper_exc:  # noqa: BLE001
            log.error("whisper_fallback_also_failed", error=str(whisper_exc))

    return Transcript(words=[], full_text="", low_confidence=True)


# ── Audio-only template analysis ─────────────────────────────────────────────


def analyze_audio_template(
    file_ref: Any,
    beat_timestamps_s: list[float],
    track_config: dict,
    duration_s: float,
) -> dict:
    """Analyze an audio file with Gemini to produce a visual recipe — returns dict.

    SHIM (v0.2): delegates to `app.agents.audio_template.AudioTemplateAgent`.
    Output dict shape preserved for backward compatibility with existing callers
    (music_orchestrate). Failures raise the legacy `GeminiAnalysisError` /
    `GeminiRefusalError` types.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import RefusalError, SchemaError, TerminalError  # noqa: PLC0415
    from app.agents.audio_template import (  # noqa: PLC0415
        AudioTemplateAgent,
        AudioTemplateInput,
    )

    best_start = float(track_config.get("best_start_s", 0.0))
    best_end = float(track_config.get("best_end_s", duration_s))

    inp = AudioTemplateInput(
        file_uri=file_ref.uri,
        file_mime=getattr(file_ref, "mime_type", None) or "audio/mp4",
        beat_timestamps_s=list(beat_timestamps_s),
        best_start_s=best_start,
        best_end_s=best_end,
        duration_s=duration_s,
    )

    try:
        out = AudioTemplateAgent(default_client()).run(inp)
    except TerminalError as exc:
        cause = exc.__cause__
        if isinstance(cause, (RefusalError, SchemaError)) or "refusal" in str(exc) or "schema" in str(exc):
            raise GeminiRefusalError(str(exc)) from exc
        raise GeminiAnalysisError(str(exc)) from exc

    log.info(
        "audio_template_analysis_done",
        slot_count=len(out.slots),
        color_grade=out.color_grade,
        interstitials=len(out.interstitials),
    )
    return out.to_dict()
