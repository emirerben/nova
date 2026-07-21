"""nova.compose.sfx_placement — propose SFX placements at spoken-word moments.

ONE LLM call per run: reads the variant's speech map (words + pauses), the
clip-moment windows, and the published SFX catalog, and proposes a small set of
sound-effect placements. The agent owns SELECTION + TIMING INTENT only:

  - `at_s` must be copied from a supplied mark (word start/end, pause start,
    moment edge) — the server drops out-of-range and invents nothing;
  - its output is ADVISORY: the server validates (known effect_id, spacing,
    keep-out, voice ban) and persists `pending_sfx_suggestions`; nothing
    renders until a user or the copilot realizes a suggestion as an ordinary
    SoundEffectPlacement.

Mirrors overlay_placement's posture (agent proposes, server disposes).
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

SFX_ANCHORS = ("word_start", "word_end", "pause", "moment")
_MAX_WORDS = 1200
_MAX_PAUSES = 60
_MAX_MOMENTS = 24
_MAX_EFFECTS = 30


class SfxCatalogEffect(BaseModel):
    effect_id: str
    name: str = ""
    role_tags: list[str] = Field(default_factory=list)
    duration_s: float | None = None


class SfxPlacementInput(BaseModel):
    # {"word","start_s","end_s"} records, transcript order.
    words: list[dict] = Field(min_length=1, max_length=_MAX_WORDS)
    # {"s","e","after"} pause records (speech_map shape).
    pauses: list[dict] = Field(default_factory=list, max_length=_MAX_PAUSES)
    # {"start_s","end_s","description"} clip-moment windows (may be empty).
    moments: list[dict] = Field(default_factory=list, max_length=_MAX_MOMENTS)
    effects: list[SfxCatalogEffect] = Field(min_length=1, max_length=_MAX_EFFECTS)
    duration_s: float = Field(gt=0)


class RawSfxSuggestion(BaseModel):
    effect_id: str
    at_s: float = Field(ge=0)
    gain: float = 0.8
    anchor: str = "pause"
    reason: str = ""

    @field_validator("anchor")
    @classmethod
    def _anchor(cls, v: str) -> str:
        return v if v in SFX_ANCHORS else "pause"


class SfxPlacementOutput(BaseModel):
    placements: list[RawSfxSuggestion] = Field(default_factory=list)


class SfxPlacementAgent(Agent[SfxPlacementInput, SfxPlacementOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.sfx_placement",
        prompt_id="sfx_placement",
        prompt_version="1.0.0",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        enable_json_repair=True,
    )
    Input = SfxPlacementInput
    Output = SfxPlacementOutput

    def required_fields(self) -> list[str]:
        return ["placements"]

    def render_prompt(self, input: SfxPlacementInput) -> str:  # noqa: A002
        words_payload = [
            {
                "w": _sanitize_text(str(w.get("word", w.get("w", "")))),
                "s": round(float(w.get("start_s", w.get("s", 0.0))), 2),
                "e": round(float(w.get("end_s", w.get("e", 0.0))), 2),
            }
            for w in input.words
        ]
        pauses_payload = [
            {
                "s": round(float(p.get("s", 0.0)), 2),
                "e": round(float(p.get("e", 0.0)), 2),
                "after": _sanitize_text(str(p.get("after") or "")),
            }
            for p in input.pauses
        ]
        moments_payload = [
            {
                "start_s": round(float(m.get("start_s", 0.0)), 2),
                "end_s": round(float(m.get("end_s", 0.0)), 2),
                "description": _sanitize_text(str(m.get("description") or ""))[:120],
            }
            for m in input.moments
        ]
        effects_payload = [
            {
                "effect_id": e.effect_id,
                "name": _sanitize_text(e.name)[:60],
                "roles": [_sanitize_text(r)[:40] for r in e.role_tags[:6]],
                "duration_s": e.duration_s,
            }
            for e in input.effects
        ]
        return load_prompt(
            "sfx_placement",
            words_json=json.dumps(words_payload, ensure_ascii=False),
            pauses_json=json.dumps(pauses_payload, ensure_ascii=False),
            moments_json=json.dumps(moments_payload, ensure_ascii=False),
            effects_json=json.dumps(effects_payload, ensure_ascii=False),
            duration_s=f"{input.duration_s:.1f}",
        )

    def parse(
        self,
        raw_text: str,
        input: SfxPlacementInput,  # noqa: A002
    ) -> SfxPlacementOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"sfx_placement: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("sfx_placement: response is not a JSON object")
        placements_raw = data.get("placements")
        if not isinstance(placements_raw, list):
            raise SchemaError("sfx_placement: 'placements' must be a list")
        placements: list[RawSfxSuggestion] = []
        for item in placements_raw:
            if not isinstance(item, dict):
                continue
            try:
                placements.append(RawSfxSuggestion.model_validate(item))
            except Exception:  # noqa: BLE001 — malformed item, dropped at parse
                continue
        return SfxPlacementOutput(placements=placements)
