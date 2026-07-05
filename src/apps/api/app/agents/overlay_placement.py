"""nova.compose.overlay_placement — match pool assets to spoken moments (plans/005 PR1a).

ONE LLM call per match run: reads the word-level transcript + the analyzed asset
pool and proposes which asset pops in at which spoken moment. The agent owns
MATCHING + TIMING INTENT only:

  - it picks from named slots ("top" | "center" | "full") — never raw coordinates;
    the server resolves slot × asset aspect → x/y/scale (decision 5A, aspect-aware).
  - its confidence is treated as UNCALIBRATED: `confidence_tier` drives copy
    hedging only (decision 10A); the server drops/caps/clamps everything
    (decision 6A per-item validation, decision 11A cap).
  - `sfx_intent` is a vocabulary word; the server maps it to a curated glossary
    effect rule-based — the agent never picks audio files.

Output text NEVER reaches the screen: `reason` renders in the review rail only.
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

SLOT_NAMES = ("top", "center", "full")
SFX_INTENTS = ("pop_in", "whoosh", "click", "none")
_MAX_ASSETS = 20
_MAX_WORDS = 1200


class PlacementAsset(BaseModel):
    asset_id: str
    kind: str = "image"  # "image" | "video"
    subject: str = ""
    description: str = ""
    on_screen_text: str = ""
    duration_s: float | None = None
    aspect: float | None = None  # width / height
    # Pixel dims (plan 009 E1) — persisted by ANALYSIS_VERSION 3; None on
    # legacy assets until the backfill heals them.
    width: int | None = None
    height: int | None = None


class OverlayPlacementInput(BaseModel):
    # {"word","start_s","end_s"} records, transcript order.
    words: list[dict] = Field(min_length=1, max_length=_MAX_WORDS)
    assets: list[PlacementAsset] = Field(min_length=1, max_length=_MAX_ASSETS)
    # [start_s, end_s] intervals already occupied by existing placements.
    occupied: list[list[float]] = Field(default_factory=list)
    duration_s: float = Field(gt=0)
    archetype: str = "talking_head"


class RawPlacement(BaseModel):
    asset_id: str
    slot: str = "top"
    start_s: float = Field(ge=0)
    end_s: float = Field(ge=0)
    confidence_tier: str = "likely"
    reason: str = ""
    transcript_anchor: str = ""
    sfx_intent: str = "pop_in"

    @field_validator("slot")
    @classmethod
    def _slot(cls, v: str) -> str:
        return v if v in SLOT_NAMES else "top"

    @field_validator("sfx_intent")
    @classmethod
    def _intent(cls, v: str) -> str:
        return v if v in SFX_INTENTS else "pop_in"


class OverlayPlacementOutput(BaseModel):
    placements: list[RawPlacement] = Field(default_factory=list)
    # For unmatched moments: what asset WOULD help ("Add a screenshot of the
    # settings toggle you mention at 0:32"). Rendered as the wishlist.
    wishlist: list[str] = Field(default_factory=list)

    @field_validator("wishlist")
    @classmethod
    def _wishlist(cls, v: list[str]) -> list[str]:
        return [(s or "").strip()[:160] for s in v if (s or "").strip()][:5]


class OverlayPlacementAgent(Agent[OverlayPlacementInput, OverlayPlacementOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.overlay_placement",
        prompt_id="overlay_placement",
        # 1.1.0 (plan 009): rule 5 rewritten — "full" is a real cover-crop
        # takeover with its own grammar (1.5-4s, never in hook/intro, max 2);
        # assets_payload gains aspect + pixel dims.
        prompt_version="1.1.0",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        enable_json_repair=True,
    )
    Input = OverlayPlacementInput
    Output = OverlayPlacementOutput

    def required_fields(self) -> list[str]:
        return ["placements"]

    def render_prompt(self, input: OverlayPlacementInput) -> str:  # noqa: A002
        words_payload = [
            {
                "w": _sanitize_text(str(w.get("word", ""))),
                "s": round(float(w.get("start_s", 0.0)), 2),
                "e": round(float(w.get("end_s", 0.0)), 2),
            }
            for w in input.words
        ]
        assets_payload = [
            {
                "asset_id": a.asset_id,
                "kind": a.kind,
                "subject": _sanitize_text(a.subject),
                "description": _sanitize_text(a.description),
                "on_screen_text": _sanitize_text(a.on_screen_text),
                "duration_s": a.duration_s,
                # Plan 009: the model was guessing landscape-ness — slot "full"
                # amplifies wrong guesses, so aspect + dims are now explicit.
                "aspect": a.aspect,
                "width": a.width,
                "height": a.height,
            }
            for a in input.assets
        ]
        return load_prompt(
            "overlay_placement",
            words_json=json.dumps(words_payload, ensure_ascii=False),
            assets_json=json.dumps(assets_payload, ensure_ascii=False),
            occupied_json=json.dumps(
                [[round(a, 2), round(b, 2)] for a, b in (input.occupied or [])]
            ),
            duration_s=f"{input.duration_s:.1f}",
            archetype=_sanitize_text(input.archetype) or "talking_head",
        )

    def parse(
        self,
        raw_text: str,
        input: OverlayPlacementInput,  # noqa: A002
    ) -> OverlayPlacementOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"overlay_placement: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("overlay_placement: response is not a JSON object")
        placements_raw = data.get("placements")
        if not isinstance(placements_raw, list):
            raise SchemaError("overlay_placement: 'placements' must be a list")
        # Per-item drop (decision 6A) happens at the SERVER resolution layer,
        # which needs the drop reasons for tracing — here we only require shape.
        placements: list[RawPlacement] = []
        for item in placements_raw:
            if not isinstance(item, dict):
                continue
            try:
                placements.append(RawPlacement.model_validate(item))
            except Exception:  # noqa: BLE001 — malformed item, dropped at parse
                continue
        wishlist = data.get("wishlist") if isinstance(data.get("wishlist"), list) else []
        return OverlayPlacementOutput(placements=placements, wishlist=wishlist)
