"""nova.audio.song_classifier — creative-direction labels for a music track.

Producer of `MusicLabels` (see `app/agents/_schemas/music_labels.py`). Runs
once per track at admin-publish time. The matcher (Phase 2) consumes
`ai_labels` to pick top-K tracks per auto-music job; `transition_picker`
and `text_designer` consume `copy_tone` + `transition_style` directly in
auto-music mode.

Inputs:
  - audio Gemini File API URI (the already-uploaded track)
  - the `AudioTemplateOutput` structural analysis dict (so we don't redo
    beat/slot/tempo work — only creative direction)

Output:
  - `MusicLabels` blob + a one-sentence rationale

The agent does NOT touch `MusicTrack.recipe_cached` or beat data. It only
writes `ai_labels` + `label_version`.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents._schemas.music_labels import CURRENT_LABEL_VERSION, MusicLabels
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()


class SongClassifierInput(BaseModel):
    file_uri: str
    file_mime: str = "audio/mp4"
    # The AudioTemplateOutput dict (audio_template's `.model_dump()`).
    # Read-only context — never re-derived by the classifier.
    audio_template_output: dict[str, Any] = Field(default_factory=dict)


class SongClassifierOutput(BaseModel):
    labels: MusicLabels
    rationale: str = Field(min_length=1)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()


class SongClassifierAgent(Agent[SongClassifierInput, SongClassifierOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.song_classifier",
        prompt_id="classify_song",
        prompt_version=CURRENT_LABEL_VERSION,
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = SongClassifierInput
    Output = SongClassifierOutput

    def media_uri(self, input: SongClassifierInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: SongClassifierInput) -> str:  # noqa: A002
        return input.file_mime or "audio/mp4"

    def required_fields(self) -> list[str]:
        # Top-level JSON keys; nested `labels` validation is Pydantic's job.
        return ["labels", "rationale"]

    def render_prompt(self, input: SongClassifierInput) -> str:  # noqa: A002
        at = input.audio_template_output or {}
        return load_prompt(
            "classify_song",
            label_version=CURRENT_LABEL_VERSION,
            copy_tone=str(at.get("copy_tone", "") or ""),
            transition_style=str(at.get("transition_style", "") or ""),
            pacing_style=str(at.get("pacing_style", "") or ""),
            creative_direction=str(at.get("creative_direction", "") or ""),
            subject_niche=str(at.get("subject_niche", "") or ""),
        )

    def parse(
        self,
        raw_text: str,
        input: SongClassifierInput,  # noqa: A002, ARG002
    ) -> SongClassifierOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"song_classifier: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("song_classifier: response is not a JSON object")

        labels_raw = data.get("labels")
        if not isinstance(labels_raw, dict):
            raise SchemaError("song_classifier: missing/invalid 'labels' object")

        # The matcher trusts label_version exactly. If the model echoes a
        # different one, force the current version — we just produced these
        # labels under CURRENT_LABEL_VERSION's prompt, so that IS their version.
        labels_raw["label_version"] = CURRENT_LABEL_VERSION

        try:
            labels = MusicLabels(**labels_raw)
        except ValidationError as exc:
            # Enum mismatch / empty mood / too-many vibe_tags / etc.
            # RefusalError so the runtime can retry once with a stricter suffix.
            raise RefusalError(f"song_classifier: labels validation — {exc}") from exc

        # Normalize vibe_tags: dedupe (case-insensitive), drop empties.
        seen: set[str] = set()
        cleaned: list[str] = []
        for tag in labels.vibe_tags:
            t = (tag or "").strip().lower()
            if not t or t in seen:
                continue
            seen.add(t)
            cleaned.append(t)
        if not cleaned:
            raise RefusalError("song_classifier: vibe_tags is empty after normalization")
        labels = labels.model_copy(update={"vibe_tags": cleaned})

        rationale = str(data.get("rationale", "") or "").strip()
        if not rationale:
            raise RefusalError("song_classifier: rationale is empty")

        try:
            return SongClassifierOutput(labels=labels, rationale=rationale)
        except ValidationError as exc:
            raise SchemaError(f"song_classifier: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY the JSON object described above. Every "
            "categorical field MUST use one of the listed enum values verbatim. "
            "`vibe_tags` MUST have between 1 and 8 short lowercase tokens. "
            "`mood` and `ideal_content_profile` MUST be non-empty."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
