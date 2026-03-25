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

    Raises PollingTimeoutError if the file doesn't become ACTIVE within `timeout` seconds.
    Retries up to 2× on ResourceExhausted (rate limit) with 15s backoff.
    Raises immediately on InvalidArgument.
    """
    from google.api_core import exceptions as gapi_exc  # type: ignore[import]

    client = _get_client()

    # Upload with retry for rate limits
    file_ref = None
    for attempt in range(3):
        try:
            file_ref = client.files.upload(file=path)
            break
        except gapi_exc.ResourceExhausted:
            if attempt >= 2:
                raise
            log.warning("gemini_upload_rate_limited", attempt=attempt, path=path)
            time.sleep(15)
        except gapi_exc.InvalidArgument:
            raise

    if file_ref is None:
        raise GeminiAnalysisError("Failed to upload file after retries")

    # Poll until ACTIVE
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            file_ref = client.files.get(name=file_ref.name)
        except gapi_exc.ResourceExhausted:
            log.warning("gemini_poll_rate_limited", name=file_ref.name)
            time.sleep(15)
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

    If start_s and end_s are provided, focuses analysis on that segment.
    Raises GeminiAnalysisError on failure.
    """
    client = _get_client()

    if start_s is not None and end_s is not None:
        segment_instruction = f"Analyze the video segment from {start_s:.1f}s to {end_s:.1f}s."
    else:
        segment_instruction = "Analyze this video clip."

    prompt = (
        f"{segment_instruction}\n\n"
        "Return a JSON object with these exact fields:\n"
        '- "transcript": string — full spoken text in the segment\n'
        '- "hook_text": string — the first compelling sentence that creates curiosity\n'
        '- "hook_score": float 0–10 — how strongly the opening hooks the viewer\n'
        '- "best_moments": list of 3–6 objects with '
        '{"start_s": float, "end_s": float, "energy": float 0–10, "description": string} '
        "covering a VARIETY of durations — include some short moments (3–5s) AND some "
        "medium moments (8–12s) AND at least one longer moment (13–20s) so this clip can "
        "fill both short and long template slots\n\n"
        "Return ONLY valid JSON, no markdown."
    )

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
        )

    except (GeminiRefusalError, GeminiAnalysisError):
        raise
    except Exception as exc:
        raise GeminiAnalysisError(f"Clip analysis failed: {exc}") from exc


# ── Template analysis ─────────────────────────────────────────────────────────


def analyze_template(file_ref: Any) -> TemplateRecipe:
    """Extract a structural 'recipe' from a template video.

    Failure is fatal (raises) — caller must handle.
    """
    client = _get_client()

    prompt = (
        "Analyze this TikTok/short-form video template. Extract its structural recipe "
        "so user footage can be inserted into each slot.\n\n"
        "Rules:\n"
        "- Count every shot cut as a separate slot\n"
        "- slots array length MUST equal shot_count\n"
        "- The sum of all target_duration_s values must approximately equal total_duration_s\n"
        "- Each slot's target_duration_s is the duration of that shot in the template\n"
        "- position is 1-indexed temporal order (slot 1 = first shot)\n"
        "- EVERY slot object MUST include an 'energy' field (float 0-10) reflecting the "
        "musical intensity during that slot — listen to the audio track and rate each slot\n\n"
        "Return a JSON object with these exact fields:\n"
        '- "shot_count": int — total number of shots/cuts\n'
        '- "total_duration_s": float — total video duration in seconds\n'
        '- "hook_duration_s": float — duration of the opening hook section\n'
        '- "slots": list of objects with '
        '{"position": int (1-indexed), "target_duration_s": float, '
        '"priority": int 1–10, "slot_type": "hook"|"broll"|"outro", '
        '"energy": float 0–10} where energy reflects the musical intensity at that '
        "moment — 0 is silence/calm, 5 is moderate, 10 is a hard beat drop or climax. "
        "This is critical for matching footage energy to music energy.\n"
        '- "copy_tone": string — one of: casual, formal, energetic, calm\n'
        '- "caption_style": string — description of caption/text overlay style\n'
        '- "beat_timestamps_s": list of float — timestamps (in seconds) of significant '
        "musical beats, drops, transitions, or energy changes in the audio track. "
        "Include every timestamp where a visual cut coincides with a musical beat. "
        "Return empty list if the template has no music.\n\n"
        "Return ONLY valid JSON, no markdown."
    )

    from google.genai import types as genai_types  # type: ignore[import]

    response = client.models.generate_content(
        model="gemini-2.5-flash",
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

    slots = data.get("slots", [])
    total_duration_s = float(data.get("total_duration_s", 0.0))
    slot_duration_sum = sum(float(s.get("target_duration_s", 0.0)) for s in slots)
    if total_duration_s > 0 and abs(slot_duration_sum - total_duration_s) > 5.0:
        log.warning(
            "template_slot_duration_mismatch",
            slot_sum=round(slot_duration_sum, 1),
            total_duration_s=round(total_duration_s, 1),
            delta=round(abs(slot_duration_sum - total_duration_s), 1),
        )

    beat_timestamps_s = sorted(
        float(t) for t in data.get("beat_timestamps_s", [])
    )

    # Validate and normalize slot fields (transitions, effects, speed_factor)
    slots = _validate_slots(slots)

    return TemplateRecipe(
        shot_count=int(data.get("shot_count", 0)),
        total_duration_s=total_duration_s,
        hook_duration_s=float(data.get("hook_duration_s", 0.0)),
        slots=slots,
        copy_tone=str(data.get("copy_tone", "casual")),
        caption_style=str(data.get("caption_style", "")),
        beat_timestamps_s=beat_timestamps_s,
    )


# ── Slot validation & migration ──────────────────────────────────────────────

_VALID_TRANSITIONS = {"crossfade", "fade_black", "wipe_left", "wipe_right", "none"}
_VALID_EFFECTS = {"fade-in", "typewriter", "slide-up", "font-cycle", "static", "none"}
_VALID_SPEED_FACTORS = {0.5, 0.75, 1.0, 1.25, 1.5, 2.0}

# Migration mapping: old Gemini values → closest supported equivalent
_TRANSITION_MIGRATION: dict[str, str] = {
    "dissolve": "crossfade",
    "zoom-in": "fade_black",
    "whip-pan": "wipe_left",
    "hard-cut": "none",
}
_EFFECT_MIGRATION: dict[str, str] = {
    "pop-in": "fade-in",
    "scale-up": "fade-in",
    "bounce": "fade-in",
    "glitch": "static",
    "slide-in": "slide-up",
}


def _validate_slots(slots: list[dict]) -> list[dict]:
    """Normalize and constrain slot fields to the supported effects vocabulary.

    Applies migration mapping for old Gemini values, then enforces enum constraints.
    Unknown values default to "none" after migration attempt.
    """
    for slot in slots:
        # Transition
        trans = str(slot.get("transition_in", "none"))
        if trans not in _VALID_TRANSITIONS:
            trans = _TRANSITION_MIGRATION.get(trans, "none")
        slot["transition_in"] = trans

        # Speed factor — snap to nearest valid discrete value
        raw_speed = float(slot.get("speed_factor", 1.0))
        slot["speed_factor"] = min(
            _VALID_SPEED_FACTORS, key=lambda v: abs(v - raw_speed)
        )

        # Text overlays
        for ov in slot.get("text_overlays", []):
            effect = str(ov.get("effect", "none"))
            if effect not in _VALID_EFFECTS:
                effect = _EFFECT_MIGRATION.get(effect, "none")
            ov["effect"] = effect

            # Clamp speed_factor on overlay (if present)
            if "speed_factor" in ov:
                ov_speed = float(ov["speed_factor"])
                ov["speed_factor"] = min(
                    _VALID_SPEED_FACTORS, key=lambda v: abs(v - ov_speed)
                )

        # Ensure energy field exists (defaults to 5.0)
        if "energy" not in slot:
            slot["energy"] = 5.0

    return slots


# ── Transcription ─────────────────────────────────────────────────────────────


def transcribe(file_ref: Any) -> "Transcript":  # noqa: F821
    """Transcribe audio from a Gemini file reference.

    Falls back to Whisper (via transcribe.py) on any failure.
    Returns a Transcript with low_confidence=True if both fail.
    """
    from app.pipeline.transcribe import Transcript, Word, transcribe_whisper  # noqa: PLC0415

    client = _get_client()

    prompt = (
        "Transcribe all speech in this video.\n\n"
        "Return a JSON object with these exact fields:\n"
        '- "full_text": string — complete transcript\n'
        '- "words": list of {"text": string, "start_s": float, "end_s": float}\n'
        '- "low_confidence": boolean — true if transcription quality is poor\n\n'
        "Return ONLY valid JSON, no markdown."
    )

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
