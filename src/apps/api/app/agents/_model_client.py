"""Model client implementations.

`ModelDispatcher` is the public facade: agents are constructed with one and call
`.invoke(model=...)`; the dispatcher routes to the right provider by model-name
prefix. `gemini-*` → `GeminiClient`, `whisper-1` → `WhisperClient`.

Each provider client is responsible for:
  - translating runtime args into the SDK call shape
  - extracting `raw_text` + token counts into a `ModelInvocation`
  - classifying errors: 5xx/429/timeout → `TransientError`, anything else → `TerminalError`

Media upload (Gemini File API) lives on the dispatcher as a convenience —
orchestrators upload once per source file, then pass `uri`+`mime_type` into agent
inputs for the per-segment loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import structlog

from app.agents._runtime import (
    ModelClient,
    ModelInvocation,
    TerminalError,
    TransientError,
)
from app.config import settings

log = structlog.get_logger()


@dataclass(slots=True)
class MediaRef:
    """Provider-agnostic media reference. Currently only Gemini File API is supported."""

    uri: str
    mime_type: str
    name: str  # SDK-internal handle (Gemini files/<id>)


# ── Gemini ────────────────────────────────────────────────────────────────────


class GeminiClient(ModelClient):
    """Wraps `google.genai`. Lifts upload+poll+invoke logic from `gemini_analyzer.py`.

    Retry policy mirrors the existing 5-attempt `[3, 9, 27, 60]` backoff for
    upload+poll. For `invoke()`, a single attempt is made — the agent runtime
    handles retries (because it owns the retry budget across schema/refusal as well).
    """

    _DEFAULT_BACKOFF: tuple[float, ...] = (3.0, 9.0, 27.0, 60.0)
    _UPLOAD_MAX_ATTEMPTS = 5

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.gemini_api_key

    # ── Lazy SDK ──────────────────────────────────────────────────

    def _get(self) -> Any:
        # Delegate to the existing module-level singleton in `gemini_analyzer`
        # so that existing test mocks of `_get_client` (which there are many of)
        # continue to work when the legacy shims call through the agent runtime.
        # This is intentional and not a circular dependency: the import is lazy.
        from app.pipeline.agents.gemini_analyzer import _get_client  # noqa: PLC0415

        return _get_client()

    # ── Media upload (mirrors gemini_upload_and_wait) ─────────────

    def upload_media(self, path: str, *, timeout: int = 120) -> MediaRef:
        """Upload a local file to Gemini File API and poll until ACTIVE.

        Lifted from `gemini_analyzer.gemini_upload_and_wait` — same retry
        policy ([3,9,27,60] x 5 attempts), same transient/permanent classification.
        """
        from google.genai import errors as genai_errors  # type: ignore[import]

        client = self._get()

        # Upload with retry on 5xx/429
        file_ref: Any = None
        for attempt in range(self._UPLOAD_MAX_ATTEMPTS):
            try:
                file_ref = client.files.upload(file=path)
                break
            except genai_errors.APIError as exc:
                if not _is_genai_transient(exc):
                    raise TerminalError(f"gemini upload failed: {exc}") from exc
                if attempt >= self._UPLOAD_MAX_ATTEMPTS - 1:
                    raise TransientError(f"gemini upload exhausted: {exc}") from exc
                backoff = self._DEFAULT_BACKOFF[attempt]
                log.warning(
                    "gemini_upload_transient_retry",
                    attempt=attempt + 1,
                    of=self._UPLOAD_MAX_ATTEMPTS,
                    backoff_s=backoff,
                    error_type=type(exc).__name__,
                    http_code=getattr(exc, "code", None),
                    path=path,
                )
                time.sleep(backoff)

        if file_ref is None:
            raise TerminalError("gemini upload returned None")

        # Poll until ACTIVE
        deadline = time.time() + timeout
        poll_attempt = 0
        while time.time() < deadline:
            try:
                file_ref = client.files.get(name=file_ref.name)
                poll_attempt = 0
            except genai_errors.APIError as exc:
                if not _is_genai_transient(exc):
                    raise TerminalError(f"gemini poll failed: {exc}") from exc
                backoff = self._DEFAULT_BACKOFF[
                    min(poll_attempt, len(self._DEFAULT_BACKOFF) - 1)
                ]
                log.warning(
                    "gemini_poll_transient_retry",
                    attempt=poll_attempt + 1,
                    backoff_s=backoff,
                    error_type=type(exc).__name__,
                    http_code=getattr(exc, "code", None),
                )
                poll_attempt += 1
                time.sleep(backoff)
                continue

            state = file_ref.state.name if hasattr(file_ref.state, "name") else str(file_ref.state)
            if state == "ACTIVE":
                log.info("gemini_file_active", name=file_ref.name)
                return MediaRef(
                    uri=file_ref.uri,
                    mime_type=getattr(file_ref, "mime_type", None) or "video/mp4",
                    name=file_ref.name,
                )
            if state == "FAILED":
                raise TerminalError(f"gemini file processing failed: {file_ref.name}")
            time.sleep(5)

        raise TerminalError(f"gemini file did not become ACTIVE within {timeout}s")

    # ── Single invocation (no retry — runtime owns that) ──────────

    def invoke(
        self,
        *,
        model: str,
        prompt: str,
        media_uri: str | None = None,
        media_mime: str | None = None,
        response_json: bool = True,
        max_output_tokens: int | None = None,
        timeout_s: float = 30.0,  # noqa: ARG002 — genai SDK doesn't accept per-call timeout currently
    ) -> ModelInvocation:
        from google.genai import errors as genai_errors  # type: ignore[import]
        from google.genai import types as genai_types  # type: ignore[import]

        client = self._get()

        # All gemini-* calls funnel through the deployed model setting. This
        # preserves the legacy behavior — every Gemini invocation uses
        # `settings.gemini_model`, settable per-deploy via env var.
        # We look up settings via the gemini_analyzer module so existing tests
        # that `patch("app.pipeline.agents.gemini_analyzer.settings")` continue
        # to drive the model selection through the agent path. When a real
        # cross-SKU fallback (Flash → Pro) is needed, add a per-family setting.
        if model.startswith("gemini-"):
            from app.pipeline.agents import gemini_analyzer as _ga  # noqa: PLC0415

            model = getattr(_ga.settings, "gemini_model", None) or model

        contents: list[Any] = []
        if media_uri:
            contents.append(
                genai_types.Part.from_uri(
                    file_uri=media_uri,
                    mime_type=media_mime or "video/mp4",
                )
            )
        contents.append(prompt)

        cfg_kwargs: dict[str, Any] = {}
        if response_json:
            cfg_kwargs["response_mime_type"] = "application/json"
        if max_output_tokens is not None:
            cfg_kwargs["max_output_tokens"] = max_output_tokens

        try:
            response = client.models.generate_content(
                model=model,
                contents=contents,
                config=genai_types.GenerateContentConfig(**cfg_kwargs),
            )
        except genai_errors.APIError as exc:
            if _is_genai_transient(exc):
                raise TransientError(f"gemini transient: {exc}") from exc
            raise TerminalError(f"gemini terminal: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — unknown SDK error class
            # Network / parse / timeout errors at the SDK boundary count as transient.
            raise TransientError(f"gemini sdk error: {exc}") from exc

        raw_text = getattr(response, "text", "") or ""
        usage = getattr(response, "usage_metadata", None)
        tokens_in = int(getattr(usage, "prompt_token_count", 0) or 0) if usage else 0
        tokens_out = int(getattr(usage, "candidates_token_count", 0) or 0) if usage else 0

        return ModelInvocation(
            raw_text=raw_text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            raw_response=response,
        )


def _is_genai_transient(exc: Exception) -> bool:
    """5xx ServerError or 429 rate-limit. Mirrors the classification in `gemini_analyzer.py`."""
    from google.genai import errors as genai_errors  # type: ignore[import]

    if not isinstance(exc, genai_errors.APIError):
        return False
    if isinstance(exc, genai_errors.ServerError):
        return True
    code = getattr(exc, "code", None)
    return code == 429 or (isinstance(code, int) and 500 <= code < 600)


# ── OpenAI Whisper ────────────────────────────────────────────────────────────


class WhisperClient(ModelClient):
    """OpenAI Whisper API. Used as transcript fallback when Gemini fails.

    The `media_uri` here is interpreted as a LOCAL FILE PATH, not a Gemini URI —
    Whisper's API takes a file upload directly. The transcript agent caller is
    responsible for routing the right path in.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or getattr(settings, "openai_api_key", None)
        self._client: Any = None  # lazy

    def _get(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # type: ignore[import]

            self._client = OpenAI(api_key=self._api_key)
        return self._client

    def invoke(
        self,
        *,
        model: str,
        prompt: str,  # noqa: ARG002 — Whisper takes audio, not a prompt; runtime passes empty
        media_uri: str | None = None,
        media_mime: str | None = None,  # noqa: ARG002
        response_json: bool = True,  # noqa: ARG002 — Whisper returns its own JSON shape
        max_output_tokens: int | None = None,  # noqa: ARG002
        timeout_s: float = 30.0,  # noqa: ARG002
    ) -> ModelInvocation:
        if not media_uri:
            raise TerminalError("whisper: media_uri (local audio path) required")
        try:
            client = self._get()
            with open(media_uri, "rb") as fh:
                resp = client.audio.transcriptions.create(
                    model=model,
                    file=fh,
                    response_format="verbose_json",
                    timestamp_granularities=["word"],
                )
        except FileNotFoundError as exc:
            raise TerminalError(f"whisper: file not found — {media_uri}") from exc
        except Exception as exc:  # noqa: BLE001 — openai SDK error class is broad
            cls = type(exc).__name__.lower()
            if any(k in cls for k in ("rate", "timeout", "apiconnection", "internal")):
                raise TransientError(f"whisper transient: {exc}") from exc
            raise TerminalError(f"whisper terminal: {exc}") from exc

        # OpenAI SDK returns a dict-like; serialize to JSON the runtime can parse.
        # Shape mirrors what the transcript agent will expect to parse.
        import json as _json

        if hasattr(resp, "model_dump"):
            data = resp.model_dump()
        elif hasattr(resp, "to_dict"):
            data = resp.to_dict()
        else:
            data = dict(resp)  # type: ignore[arg-type]

        return ModelInvocation(
            raw_text=_json.dumps(data),
            tokens_in=0,
            tokens_out=0,
            raw_response=resp,
        )


# ── Dispatcher ────────────────────────────────────────────────────────────────


class ModelDispatcher(ModelClient):
    """Routes to provider client by model-name prefix. The single client agents hold."""

    def __init__(
        self,
        *,
        gemini: GeminiClient | None = None,
        whisper: WhisperClient | None = None,
    ) -> None:
        self._gemini = gemini or GeminiClient()
        self._whisper = whisper or WhisperClient()

    def invoke(
        self,
        *,
        model: str,
        prompt: str,
        media_uri: str | None = None,
        media_mime: str | None = None,
        response_json: bool = True,
        max_output_tokens: int | None = None,
        timeout_s: float = 30.0,
    ) -> ModelInvocation:
        if model.startswith("gemini"):
            return self._gemini.invoke(
                model=model,
                prompt=prompt,
                media_uri=media_uri,
                media_mime=media_mime,
                response_json=response_json,
                max_output_tokens=max_output_tokens,
                timeout_s=timeout_s,
            )
        if model == "whisper-1":
            return self._whisper.invoke(
                model=model,
                prompt=prompt,
                media_uri=media_uri,
                media_mime=media_mime,
                response_json=response_json,
                max_output_tokens=max_output_tokens,
                timeout_s=timeout_s,
            )
        raise TerminalError(f"unknown model: {model!r}")

    def upload_media(self, path: str, *, timeout: int = 120) -> MediaRef:
        """Convenience pass-through: all media currently goes through Gemini File API."""
        return self._gemini.upload_media(path, timeout=timeout)


# ── Module-level singleton ────────────────────────────────────────────────────

_default: ModelDispatcher | None = None


def default_client() -> ModelDispatcher:
    """Get the process-wide default ModelDispatcher (lazy)."""
    global _default
    if _default is None:
        _default = ModelDispatcher()
    return _default
