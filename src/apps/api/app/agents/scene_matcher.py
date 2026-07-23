"""nova.compose.scene_matcher — the word→visual matching brain for Smart Captions.

The deterministic token-overlap matcher can only pair a visual with a spoken
word when the asset's metadata literally contains that token — "footballer in
an Argentina #10 jersey" never matches the spoken word "Messi", and the bare
token "flag" cross-matches every flag in the pool (the 2026-07-21 "silly
matching" report). This agent reads the word-timed transcript next to the
analyzed asset catalog and decides, with world knowledge, WHICH asset belongs
to WHICH spoken moment. It also tags semantic cues (chapter numbers, context
shifts, payoff, CTA) language-agnostically, replacing the Turkish-only vocab
tables as the primary signal.

Grounding contract (mirrors smart_edit_planner): the agent may only reference
asset IDs from the provided catalog and word IDs from the provided transcript.
Timing, coordinates, and rendering primitives are never model-controlled. The
parser validates every item independently — one bad match cannot erase the
rest — and the planner treats the whole output as advisory with the
deterministic heuristics as fallback.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents.smart_edit_planner import SmartPlannerAsset
from app.pipeline.prompt_loader import load_prompt

_WORD_ID_RE = re.compile(r"^w\d{6}$")
# Roles the matcher may tag. "hook" is deliberately absent: the first cue is
# always the hook, a transcript-derived fact the model must not reassign.
_TAG_ROLES = {"list_item", "context_shift", "payoff", "cta"}
_MAX_MATCHES = 12
_MAX_TAGS = 30
_MAX_MATCHES_PER_ASSET = 2
# Word-granular presentation hints (Smart Captions plan 011, Feature A). Bounded
# like every other output field so a "tag everything" failure mode cannot balloon
# output tokens on the render critical path. The parser slices [: _MAX * 2] before
# validating, mirroring matches/cue_tags.
_MAX_EMPHASIS = 16
_MAX_EMPHASIS_WORDS = 3

# Flag-gated prompt fragments (SMART_CAPTION_EMPHASIS_CUES_ENABLED). When the flag
# is off these render to "" and the prompt is byte-identical to the pre-feature
# body — flipping the flag off is a real rollback, not a consumption veto. Pinned
# by test_render_prompt_flag_off_is_byte_identical.
_EMPHASIS_SCHEMA_BLOCK = (
    ",\n"
    '  "emphasis_spans": [\n'
    '    {"word_ids": ["<word_id from words>"], "kind": "standalone" | "keep_together"}\n'
    "  ]"
)
_EMPHASIS_RULES_BLOCK = """
## EMPHASIS SPANS — give the key thing its own caption

Decide, from meaning, which words deserve their OWN caption. Keep this rare — at
most ~10 spans in a whole video, only for moments that genuinely land harder alone.

- "standalone": the salient thing being NAMED or introduced gets its own caption,
  alone, with nothing else on screen. This is the headline behavior. When a
  creator introduces a person, place, team, brand, or number — "number one ...
  Lionel Messi", "the top scorer is ... Mbappe", "my favorite city ... New York" —
  tag the NAME ITSELF as standalone so it shows by itself, not glued to the words
  around it. A full name is ONE standalone span of its 2-3 words: tag
  ["<Lionel's id>", "<Messi's id>"] as standalone — do NOT leave it merged with
  "he is" or the rest of the sentence. Prefer standalone for the actual entity.
- "keep_together": 2-3 words that stay on one line but remain INSIDE the normal
  caption (not isolated) — a lead-in marker ("number one", "birinci") or a unit
  ("3 million"). Use this far less than standalone; it is only for the words that
  set up the standalone, never for the named entity itself.

Rules: word_ids must be contiguous in spoken order; a span is 1-3 words; the named
entity is standalone (even when it is 2-3 words); spans may not overlap. When
unsure, omit — a plain caption is always fine, a wrong split is not.
"""


class SceneMatch(BaseModel):
    asset_id: str
    anchor_word_id: str
    confidence: Literal["high", "medium"] = "medium"
    reason: str = Field(default="", max_length=200)


class SceneCueTag(BaseModel):
    anchor_word_id: str
    role: Literal["list_item", "context_shift", "payoff", "cta"]
    sequence_number: int | None = Field(default=None, ge=1, le=20)
    reason: str = Field(default="", max_length=200)


class EmphasisSpan(BaseModel):
    """A word-granular presentation hint the caption chunker honors.

    ``standalone``: the span renders as its OWN caption cue — the founder's
    "number 1 → Messi shows Messi alone" case. ``keep_together``: the span may
    never be split across cues OR wrapped across lines ("number 1"). Both are
    presentation-only: they may never own semantics, so a semantic close (role
    change, authored-title boundary) still wins over them (see the chunker).
    """

    word_ids: list[str] = Field(min_length=1, max_length=_MAX_EMPHASIS_WORDS)
    kind: Literal["standalone", "keep_together"]


class SceneMatcherInput(BaseModel):
    words: list[dict] = Field(min_length=1, max_length=600)
    assets: list[SmartPlannerAsset] = Field(default_factory=list, max_length=20)
    language: str = ""


class SceneMatcherOutput(BaseModel):
    matches: list[SceneMatch] = Field(default_factory=list, max_length=_MAX_MATCHES)
    cue_tags: list[SceneCueTag] = Field(default_factory=list, max_length=_MAX_TAGS)
    emphasis_spans: list[EmphasisSpan] = Field(default_factory=list, max_length=_MAX_EMPHASIS)


class SceneMatcherAgent(Agent[SceneMatcherInput, SceneMatcherOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.scene_matcher",
        prompt_id="scene_matcher",
        prompt_version="2026-07-22.2",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=512,
        timeout_s=45.0,
        max_attempts=2,
        backoff_s=(3.0,),
        enable_json_repair=True,
    )
    Input = SceneMatcherInput
    Output = SceneMatcherOutput

    def required_fields(self) -> list[str]:
        return ["matches"]

    def render_prompt(self, input: SceneMatcherInput) -> str:  # noqa: A002
        from app.config import settings  # noqa: PLC0415

        emphasis_on = bool(getattr(settings, "smart_caption_emphasis_cues_enabled", False))
        return load_prompt(
            "scene_matcher",
            words_json=json.dumps(input.words, ensure_ascii=False),
            assets_json=json.dumps(
                [asset.model_dump(mode="json") for asset in input.assets],
                ensure_ascii=False,
            ),
            language=input.language,
            emphasis_schema=_EMPHASIS_SCHEMA_BLOCK if emphasis_on else "",
            emphasis_rules=_EMPHASIS_RULES_BLOCK if emphasis_on else "",
        )

    def parse(
        self,
        raw_text: str,
        input: SceneMatcherInput,  # noqa: A002
    ) -> SceneMatcherOutput:
        try:
            payload = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"scene_matcher: invalid JSON — {exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("matches"), list):
            raise SchemaError("scene_matcher: response must contain a matches list")

        known_words = {str(word.get("word_id")) for word in input.words}
        known_assets = {asset.asset_id for asset in input.assets}

        matches: list[SceneMatch] = []
        per_asset: dict[str, int] = {}
        for raw in payload["matches"][: _MAX_MATCHES * 2]:
            if not isinstance(raw, dict):
                continue
            asset_id = str(raw.get("asset_id") or "")
            anchor = str(raw.get("anchor_word_id") or "")
            if asset_id not in known_assets:
                continue
            if not _WORD_ID_RE.fullmatch(anchor) or anchor not in known_words:
                continue
            confidence = str(raw.get("confidence") or "medium")
            if confidence == "low":
                # Low-confidence guesses are exactly the "silly matching" class
                # this agent exists to eliminate — drop rather than render.
                continue
            if confidence not in {"high", "medium"}:
                confidence = "medium"
            if per_asset.get(asset_id, 0) >= _MAX_MATCHES_PER_ASSET:
                continue
            per_asset[asset_id] = per_asset.get(asset_id, 0) + 1
            try:
                matches.append(
                    SceneMatch(
                        asset_id=asset_id,
                        anchor_word_id=anchor,
                        confidence=confidence,
                        reason=str(raw.get("reason") or "")[:200],
                    )
                )
            except Exception:  # noqa: BLE001 — one bad item never erases the rest
                continue
            if len(matches) >= _MAX_MATCHES:
                break

        cue_tags: list[SceneCueTag] = []
        seen_anchor_roles: set[tuple[str, str]] = set()
        seen_sequence_numbers: set[int] = set()
        for raw in (payload.get("cue_tags") or [])[: _MAX_TAGS * 2]:
            if not isinstance(raw, dict):
                continue
            role = str(raw.get("role") or "")
            anchor = str(raw.get("anchor_word_id") or "")
            if role not in _TAG_ROLES:
                continue
            if not _WORD_ID_RE.fullmatch(anchor) or anchor not in known_words:
                continue
            if (anchor, role) in seen_anchor_roles:
                continue
            sequence_number = raw.get("sequence_number")
            if sequence_number is not None:
                try:
                    sequence_number = int(sequence_number)
                except (TypeError, ValueError):
                    sequence_number = None
            if role == "list_item" and sequence_number is not None:
                if not 1 <= sequence_number <= 20 or sequence_number in seen_sequence_numbers:
                    # A spoken list has exactly one "2" — a duplicate or wild
                    # number is a hallucination, not a chapter.
                    continue
                seen_sequence_numbers.add(sequence_number)
            if role != "list_item":
                sequence_number = None
            seen_anchor_roles.add((anchor, role))
            try:
                cue_tags.append(
                    SceneCueTag(
                        anchor_word_id=anchor,
                        role=role,
                        sequence_number=sequence_number,
                        reason=str(raw.get("reason") or "")[:200],
                    )
                )
            except Exception:  # noqa: BLE001
                continue
            if len(cue_tags) >= _MAX_TAGS:
                break

        # Emphasis spans — presentation-only, validated per item and fully
        # isolated: even a wholly malformed emphasis_spans field leaves matches
        # and cue_tags untouched (fail-soft, the caption_role_classifier
        # all-or-nothing incident is the anti-pattern). Timing-dependent checks
        # (the min gap between standalone cues) need SmartWord.start_ms, which
        # this agent's input does not carry — they run in the planner. Here we
        # enforce only what the raw output can prove: word existence, 1-3
        # contiguous words in spoken order, and no overlap between spans.
        emphasis_spans: list[EmphasisSpan] = []
        word_index = {str(word.get("word_id")): idx for idx, word in enumerate(input.words)}
        used_indexes: set[int] = set()
        for raw in (payload.get("emphasis_spans") or [])[: _MAX_EMPHASIS * 2]:
            if not isinstance(raw, dict):
                continue
            raw_ids = raw.get("word_ids")
            if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= _MAX_EMPHASIS_WORDS:
                continue
            kind = str(raw.get("kind") or "")
            if kind not in {"standalone", "keep_together"}:
                continue
            span_ids = [str(word_id) for word_id in raw_ids]
            if any(
                not _WORD_ID_RE.fullmatch(word_id) or word_id not in word_index
                for word_id in span_ids
            ):
                continue
            indexes = [word_index[word_id] for word_id in span_ids]
            # Contiguous and strictly ascending in spoken order.
            if indexes != list(range(indexes[0], indexes[0] + len(indexes))):
                continue
            if used_indexes.intersection(indexes):
                # Overlapping spans — the earlier accepted span wins.
                continue
            try:
                emphasis_spans.append(EmphasisSpan(word_ids=span_ids, kind=kind))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 — one bad span never erases the rest
                continue
            used_indexes.update(indexes)
            if len(emphasis_spans) >= _MAX_EMPHASIS:
                break

        return SceneMatcherOutput(matches=matches, cue_tags=cue_tags, emphasis_spans=emphasis_spans)
