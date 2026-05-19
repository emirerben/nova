"""nova.compose.text_classification — Stage F of the Layer-2 text-overlay pipeline.

Classifies each phrase along four dimensions:
  - effect:          how the text enters/animates (typewriter, pop-in, …)
  - role:            semantic purpose (hook/reaction/cta/label)
  - size_class:      visual weight bucket (small/medium/large/jumbo)
  - font_color_hex:  dominant glyph pixel colour at the phrase's sample frame

Inputs: phrase list (post-Stage-E transcript alignment) + a JPEG thumbnail
per phrase (frame nearest to `phrase.start_t_s`, extracted in Stage A).

Image-attachment convention: thumbnails are JPEG files on disk. We read each
file and pass it to Gemini as an inline `Part.from_bytes()` rather than
uploading to the File API — thumbnails are small (≤ 100 KB each), ephemeral
(scratch dir cleaned up after the run), and the File API round-trip would add
latency without benefit for ≤ 30 JPEG payloads. The `ModelClient` interface
does not support multi-image inline Parts, so this agent implements its own
`_classify_with_images()` path that talks to the Gemini SDK directly. The
standard `render_prompt()` / `media_uri()` / `parse()` interface is still
honoured so tests can drive the agent without the SDK.

Defensive policy on invalid enums: CLAMP to the nearest safe default and log.
Rationale: dropping a phrase entirely because of an unexpected enum value is
more harmful than misclassifying it (the renderer degrades gracefully on
effect='none' / role='label'). The clamped value is logged at WARNING level so
evals can track regression frequency.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import ClassVar

import structlog

from app.agents._runtime import Agent, AgentSpec, ModelClient, SchemaError
from app.agents._schemas.template_text import (
    VALID_EFFECTS,
    VALID_ROLES,
    VALID_SIZE_CLASSES,
)
from app.agents._schemas.text_classification import (
    ClassifiedPhrase,
    TextClassificationInput,
    TextClassificationOutput,
)
from app.agents._schemas.text_overlay_pipeline import Phrase
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

# Safe fallbacks used when Gemini returns out-of-enum / invalid values.
_DEFAULT_EFFECT = "none"
_DEFAULT_ROLE = "label"
_DEFAULT_SIZE_CLASS = "medium"
_DEFAULT_COLOR = "#FFFFFF"

__all__ = ["TextClassificationAgent"]


class TextClassificationAgent(Agent[TextClassificationInput, TextClassificationOutput]):
    """Stage F: classify each phrase's effect, role, size_class, font_color_hex.

    Multi-image path: when `input.frame_paths` contains JPEG paths, the agent
    reads each file and sends it as an inline `Part.from_bytes()` alongside the
    phrase JSON. This bypasses the base class `_run_on_model()` loop (which only
    supports a single `media_uri`) — see `run()` override below.

    Single-text fallback: when no frame paths are provided (e.g. in tests that
    don't supply images), the agent falls back to the standard `invoke()` path
    with just the text prompt. Classification quality degrades (no visual colour
    sampling) but the agent remains functional.
    """

    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.text_classification",
        prompt_id="classify_overlay",
        prompt_version="2026-05-18.1",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        timeout_s=30.0,
    )
    Input = TextClassificationInput
    Output = TextClassificationOutput

    def render_prompt(self, input: TextClassificationInput) -> str:  # noqa: A002
        phrases_json = self._build_phrases_json(input.phrases)
        return load_prompt("classify_overlay", phrases_json=phrases_json)

    def parse(
        self,
        raw_text: str,
        input: TextClassificationInput,  # noqa: A002
    ) -> TextClassificationOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"text_classification: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("text_classification: response is not a JSON object")

        raw_list = data.get("classifications", [])
        if not isinstance(raw_list, list):
            raise SchemaError("text_classification: 'classifications' is not a list")

        # Build phrase_index → entry lookup for O(1) access.
        by_idx: dict[int, dict] = {}
        for entry in raw_list:
            if not isinstance(entry, dict):
                continue
            idx = entry.get("phrase_index")
            if isinstance(idx, (int, float)):
                by_idx[int(idx)] = entry

        classified: list[ClassifiedPhrase] = []
        clamped_count = 0
        missing_count = 0

        for i, phrase in enumerate(input.phrases):
            entry = by_idx.get(i)
            if entry is None:
                log.warning(
                    "text_classification_phrase_missing",
                    phrase_index=i,
                    sample_text=phrase.sample_text[:60],
                )
                missing_count += 1
                classified.append(
                    ClassifiedPhrase(
                        phrase=phrase,
                        effect=_DEFAULT_EFFECT,
                        role=_DEFAULT_ROLE,
                        size_class=_DEFAULT_SIZE_CLASS,
                        font_color_hex=_DEFAULT_COLOR,
                    )
                )
                continue

            effect, clamped = _resolve_enum(
                entry.get("effect", _DEFAULT_EFFECT),
                VALID_EFFECTS,
                _DEFAULT_EFFECT,
                "effect",
                i,
            )
            clamped_count += clamped

            role, clamped = _resolve_enum(
                entry.get("role", _DEFAULT_ROLE),
                VALID_ROLES,
                _DEFAULT_ROLE,
                "role",
                i,
            )
            clamped_count += clamped

            size_class, clamped = _resolve_enum(
                entry.get("size_class", _DEFAULT_SIZE_CLASS),
                VALID_SIZE_CLASSES,
                _DEFAULT_SIZE_CLASS,
                "size_class",
                i,
            )
            clamped_count += clamped

            raw_hex = entry.get("font_color_hex", _DEFAULT_COLOR)
            hex_color, clamped = _resolve_hex(raw_hex, i)
            clamped_count += clamped

            classified.append(
                ClassifiedPhrase(
                    phrase=phrase,
                    effect=effect,
                    role=role,
                    size_class=size_class,
                    font_color_hex=hex_color,
                )
            )

        if clamped_count:
            log.info(
                "text_classification_clamped",
                count=clamped_count,
                n_phrases=len(input.phrases),
            )
        if missing_count:
            log.info(
                "text_classification_missing",
                count=missing_count,
                n_phrases=len(input.phrases),
            )

        return TextClassificationOutput(classified=classified)

    # ── Custom run() with multi-image Gemini support ──────────────────────────

    def run(  # type: ignore[override]
        self,
        input: TextClassificationInput | dict,  # noqa: A002
        *,
        ctx=None,
    ) -> TextClassificationOutput:
        """Override to inject per-phrase JPEG thumbnails as inline image Parts.

        When `input.frame_paths` is populated and the underlying client is a
        `GeminiClient` (or `ModelDispatcher` wrapping one), we call the Gemini
        SDK directly with a multi-part `contents` list. This bypasses the base
        class's single-`media_uri` limitation.

        Falls back to the standard `super().run()` path when:
          - `frame_paths` is empty (no thumbnails available)
          - the client does not expose a Gemini SDK client (e.g. tests using
            `MockModelClient`)
        """
        from app.agents._runtime import RunContext  # noqa: PLC0415

        ctx = ctx or RunContext()
        validated_input = self._validate_input(input)

        if not validated_input.phrases:
            return TextClassificationOutput(classified=[])

        if not validated_input.frame_paths:
            # No thumbnails — fall through to standard single-call path.
            return super().run(validated_input, ctx=ctx)

        # Attempt the multi-image Gemini path.
        gemini_client = _extract_gemini_client(self.client)
        if gemini_client is None:
            # Client doesn't expose a Gemini SDK (e.g. mock in tests).
            # Fall through to standard path without images.
            return super().run(validated_input, ctx=ctx)

        return self._run_with_images(validated_input, ctx=ctx, gemini_client=gemini_client)

    def _run_with_images(
        self,
        input: TextClassificationInput,  # noqa: A002
        *,
        ctx,
        gemini_client,
    ) -> TextClassificationOutput:
        """Build a multi-Part Gemini request (text prompt + inline JPEG images)."""
        import time  # noqa: PLC0415

        from app.agents._runtime import SchemaError, TerminalError  # noqa: PLC0415

        try:
            from google.genai import errors as genai_errors  # noqa: PLC0415
            from google.genai import types as genai_types  # noqa: PLC0415
        except ImportError as exc:
            log.warning("text_classification_no_genai_sdk", error=str(exc))
            return super().run(input, ctx=ctx)

        prompt_text = self.render_prompt(input)
        contents = _build_image_contents(prompt_text, input)

        start = time.monotonic()
        for attempt in range(self.spec.max_attempts):
            try:
                response = gemini_client.models.generate_content(
                    model=self.spec.model,
                    contents=contents,
                    config=genai_types.GenerateContentConfig(
                        response_mime_type="application/json",
                    ),
                )
            except genai_errors.APIError as exc:
                from app.agents._model_client import _is_genai_transient  # noqa: PLC0415

                if _is_genai_transient(exc):
                    if attempt >= self.spec.max_attempts - 1:
                        msg = f"text_classification: transient exhausted — {exc}"
                        raise TerminalError(msg) from exc
                    backoff = self.spec.backoff_s[min(attempt, len(self.spec.backoff_s) - 1)]
                    log.warning(
                        "text_classification_transient_retry",
                        attempt=attempt + 1,
                        backoff_s=backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise TerminalError(f"text_classification: terminal — {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                raise TerminalError(f"text_classification: sdk error — {exc}") from exc

            raw_text = getattr(response, "text", "") or ""
            usage = getattr(response, "usage_metadata", None)
            tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
            tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0
            latency_ms = int((time.monotonic() - start) * 1000)
            log.info(
                "agent_run",
                agent=self.spec.name,
                prompt_version=self.spec.prompt_version,
                model=self.spec.model,
                outcome="ok",
                attempts=attempt + 1,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                latency_ms=latency_ms,
                job_id=ctx.job_id,
            )
            try:
                return self.parse(raw_text, input)
            except (SchemaError, ValueError, KeyError, TypeError) as exc:
                if attempt >= self.spec.max_attempts - 1:
                    if isinstance(exc, SchemaError):
                        raise
                    raise SchemaError(f"text_classification: parse failed — {exc}") from exc
                contents_retry = list(contents) + [self.schema_clarification()]
                contents = contents_retry
                continue

        raise TerminalError(f"text_classification: exhausted {self.spec.max_attempts} attempts")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _build_phrases_json(phrases: list[Phrase]) -> str:
        items = []
        for i, phrase in enumerate(phrases):
            items.append(
                {
                    "phrase_index": i,
                    "sample_text": phrase.sample_text,
                    "start_t_s": phrase.start_t_s,
                    "end_t_s": phrase.end_t_s,
                    "aabb": list(phrase.aabb),
                }
            )
        return json.dumps(items, ensure_ascii=False, indent=None)


# ── Module-level helpers ───────────────────────────────────────────────────────


def _resolve_enum(
    value: object,
    valid: frozenset[str],
    default: str,
    field_name: str,
    phrase_index: int,
) -> tuple[str, int]:
    """Validate `value` against `valid`; clamp to `default` if invalid.

    Returns (resolved_value, clamped_count) where clamped_count is 0 or 1.
    """
    if isinstance(value, str) and value in valid:
        return value, 0
    log.warning(
        "text_classification_invalid_enum",
        field=field_name,
        phrase_index=phrase_index,
        received=repr(value),
        clamped_to=default,
    )
    return default, 1


def _resolve_hex(value: object, phrase_index: int) -> tuple[str, int]:
    """Validate hex color string; clamp to #FFFFFF if invalid.

    Returns (resolved_value, clamped_count).
    """
    if isinstance(value, str) and _HEX_RE.match(value):
        return value, 0
    # Attempt simple normalisation: strip "#", pad to 6, upper-case.
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate.startswith("#"):
            candidate = "#" + candidate
        if _HEX_RE.match(candidate):
            return candidate, 0
    log.warning(
        "text_classification_invalid_hex",
        phrase_index=phrase_index,
        received=repr(value),
        clamped_to=_DEFAULT_COLOR,
    )
    return _DEFAULT_COLOR, 1


def _extract_gemini_client(client: ModelClient):
    """Return the underlying `google.genai` client object, or None.

    Walks the `ModelDispatcher` → `GeminiClient` chain to reach
    `gemini_client._get()`. Returns None for mock clients used in tests.
    """
    # ModelDispatcher wraps GeminiClient
    gemini_wrapper = getattr(client, "_gemini", None)
    if gemini_wrapper is not None:
        try:
            return gemini_wrapper._get()
        except Exception:  # noqa: BLE001
            return None
    # GeminiClient directly
    if hasattr(client, "_get"):
        try:
            return client._get()  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            return None
    return None


def _build_image_contents(prompt_text: str, input: TextClassificationInput) -> list:
    """Build a Gemini `contents` list: interleaved phrase context + JPEG Parts.

    Layout:
      [ prompt_text, (image_part_0, caption_0), (image_part_1, caption_1), ... ]

    Only includes images for phrases where a readable JPEG path exists.
    Missing/unreadable frames are silently skipped — the model still sees the
    phrase in the JSON prompt and can classify based on text context alone.
    """
    try:
        from google.genai import types as genai_types  # noqa: PLC0415
    except ImportError:
        return [prompt_text]

    parts: list = [prompt_text]
    for i, phrase in enumerate(input.phrases):
        frame_path = input.frame_paths.get(i)
        if frame_path is None:
            continue
        path = Path(frame_path)
        if not path.exists():
            log.warning(
                "text_classification_frame_missing",
                phrase_index=i,
                path=str(path),
            )
            continue
        try:
            image_bytes = path.read_bytes()
        except OSError as exc:
            log.warning(
                "text_classification_frame_read_error",
                phrase_index=i,
                path=str(path),
                error=str(exc),
            )
            continue
        parts.append(genai_types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"))
        parts.append(f"[phrase_index={i}: {phrase.sample_text[:80]!r}]")
    return parts
