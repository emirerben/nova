"""nova.qa.output_validator — pre-publish gate.

Catches the bad outputs that should never ship to a user:
  - wrong aspect ratio (must be ~9:16)
  - wrong codec (must be H.264 video + AAC audio or no audio)
  - duration out of [3, 90]s range
  - empty hook text
  - placeholder copy ("Auto-copy failed")
  - empty text overlay sample_text
  - hook text equals caption (lazy duplicate)

This is `rule_based` — no LLM call. Predictable, ~10ms, runs on every render.
LLM-based content-quality checks (hook quality, language consistency, etc.) can
graduate later once we see which deterministic gaps slip through.

Failure policy at the orchestrator: BLOCK on validator failure (don't fail-open).
Better to drop one job than ship a broken clip.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec

# ── Schemas ───────────────────────────────────────────────────────────────────


class ProbeData(BaseModel):
    """Subset of `pipeline.probe.VideoProbe` needed for validation."""

    width: int = Field(..., gt=0)
    height: int = Field(..., gt=0)
    duration_s: float = Field(..., ge=0)
    video_codec: str = ""
    audio_codec: str = ""  # empty if no audio track
    fps: float = 0.0


class TextOverlaySummary(BaseModel):
    sample_text: str = ""
    role: str = ""
    start_s: float = 0.0
    end_s: float = 0.0


class CopySummary(BaseModel):
    """Subset of `PlatformCopy` we validate against — primarily looking for placeholders."""

    tiktok_caption: str = ""
    instagram_caption: str = ""
    youtube_description: str = ""
    tiktok_hook: str = ""


class OutputValidatorInput(BaseModel):
    # `platform_copy` rather than `copy` to avoid shadowing BaseModel.copy().
    probe: ProbeData
    hook_text: str
    platform_copy: CopySummary
    text_overlays: list[TextOverlaySummary] = Field(default_factory=list)


Severity = Literal["error", "warning"]


class Issue(BaseModel):
    severity: Severity
    code: str
    message: str


class OutputValidatorOutput(BaseModel):
    pass_: bool = Field(..., alias="pass")  # `pass` is a Python keyword; expose via alias
    issues: list[Issue] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0

    model_config = {"populate_by_name": True}


# ── Constants ─────────────────────────────────────────────────────────────────

_TARGET_ASPECT = 9 / 16  # width / height for a portrait clip
_ASPECT_TOLERANCE = 0.02
_MIN_DURATION_S = 3.0
_MAX_DURATION_S = 90.0
_VALID_VIDEO_CODECS = {"h264", "avc1"}
_VALID_AUDIO_CODECS = {"aac", "mp4a", ""}  # empty = no audio track is acceptable
_PLACEHOLDER_PHRASES = (
    "auto-copy failed",
    "edit before posting",
    "watch this",  # the fallback hook in _template_copy
)


# ── Agent ─────────────────────────────────────────────────────────────────────


class OutputValidatorAgent(Agent[OutputValidatorInput, OutputValidatorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.qa.output_validator",
        prompt_id="_unused",
        prompt_version="2026-05-09",
        model="rule_based",
    )
    Input = OutputValidatorInput
    Output = OutputValidatorOutput

    # rule_based agents implement compute() instead of render_prompt + parse.
    # The runtime dispatches to compute() and skips the LLM path entirely.

    def render_prompt(self, input: OutputValidatorInput) -> str:  # noqa: A002, ARG002
        return ""  # unused for rule_based

    def parse(
        self, raw_text: str, input: OutputValidatorInput  # noqa: A002, ARG002
    ) -> OutputValidatorOutput:
        raise NotImplementedError  # rule_based bypasses parse

    def compute(self, input: OutputValidatorInput) -> OutputValidatorOutput:  # noqa: A002
        issues: list[Issue] = []

        # ── Aspect ratio (must be ~9:16) ──────────────────────────
        actual_aspect = input.probe.width / input.probe.height if input.probe.height else 0
        if abs(actual_aspect - _TARGET_ASPECT) > _ASPECT_TOLERANCE:
            issues.append(
                Issue(
                    severity="error",
                    code="aspect_ratio",
                    message=(
                        f"Aspect ratio {input.probe.width}×{input.probe.height} "
                        f"({actual_aspect:.3f}) is not ~9:16 ({_TARGET_ASPECT:.3f})"
                    ),
                )
            )

        # ── Duration in [3, 90]s ──────────────────────────────────
        if input.probe.duration_s < _MIN_DURATION_S:
            issues.append(
                Issue(
                    severity="error",
                    code="duration_too_short",
                    message=f"Duration {input.probe.duration_s:.2f}s < {_MIN_DURATION_S}s",
                )
            )
        elif input.probe.duration_s > _MAX_DURATION_S:
            issues.append(
                Issue(
                    severity="error",
                    code="duration_too_long",
                    message=f"Duration {input.probe.duration_s:.2f}s > {_MAX_DURATION_S}s",
                )
            )

        # ── Codecs ────────────────────────────────────────────────
        if input.probe.video_codec.lower() not in _VALID_VIDEO_CODECS:
            issues.append(
                Issue(
                    severity="error",
                    code="video_codec",
                    message=(
                        f"Video codec {input.probe.video_codec!r} is not H.264 "
                        f"({sorted(_VALID_VIDEO_CODECS)})"
                    ),
                )
            )
        if input.probe.audio_codec.lower() not in _VALID_AUDIO_CODECS:
            issues.append(
                Issue(
                    severity="warning",  # AAC mismatch is recoverable downstream
                    code="audio_codec",
                    message=(
                        f"Audio codec {input.probe.audio_codec!r} is unexpected "
                        f"({sorted(_VALID_AUDIO_CODECS)})"
                    ),
                )
            )

        # ── Hook text ─────────────────────────────────────────────
        hook = (input.hook_text or "").strip()
        if not hook:
            issues.append(
                Issue(severity="error", code="empty_hook", message="Hook text is empty")
            )
        elif hook.lower() in _PLACEHOLDER_PHRASES:
            issues.append(
                Issue(
                    severity="error",
                    code="placeholder_hook",
                    message=f"Hook text {hook!r} is a fallback placeholder",
                )
            )

        # ── Copy placeholder detection ────────────────────────────
        for field_name, value in [
            ("tiktok_caption", input.platform_copy.tiktok_caption),
            ("instagram_caption", input.platform_copy.instagram_caption),
            ("youtube_description", input.platform_copy.youtube_description),
            ("tiktok_hook", input.platform_copy.tiktok_hook),
        ]:
            if not value:
                continue
            value_lower = value.lower()
            if any(p in value_lower for p in _PLACEHOLDER_PHRASES):
                issues.append(
                    Issue(
                        severity="error",
                        code="placeholder_copy",
                        message=f"{field_name} contains placeholder text: {value[:80]!r}",
                    )
                )

        # ── Hook == caption (lazy duplicate) ──────────────────────
        tiktok_caption = input.platform_copy.tiktok_caption
        if hook and tiktok_caption and hook == tiktok_caption.strip():
            issues.append(
                Issue(
                    severity="warning",
                    code="hook_equals_caption",
                    message="Hook text exactly equals TikTok caption — likely lazy duplicate",
                )
            )

        # ── Text overlay sample_text empty ────────────────────────
        for i, ov in enumerate(input.text_overlays):
            if not ov.sample_text.strip():
                issues.append(
                    Issue(
                        severity="warning",
                        code="empty_overlay_text",
                        message=f"Text overlay {i + 1} ({ov.role}) has empty sample_text",
                    )
                )

        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")

        return OutputValidatorOutput(
            **{"pass": error_count == 0},
            issues=issues,
            error_count=error_count,
            warning_count=warning_count,
        )
