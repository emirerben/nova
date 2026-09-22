"""nova.plan.clip_request_resolver — resolve an open-vocabulary clip intent
to specific clips (KRI-127 Lane C).

Text-only (no vision re-run here — that is the separate, multimodal
``ClipQuestionAgent`` in ``app.agents.clip_question``, called per-clip by
``app.services.clip_intent_resolution`` only for clips whose stored record
cannot answer the intent). This agent reads the SAME shared clip-understanding
projection every other clip consumer reads
(``app.services.clip_understanding.clip_record(...).prompt_view()``) and
decides, per intent, which clips it is; it never invents a keyword list or a
per-feature field — a brand-new request shape ("name the dish", "label each
city") is handled by the SAME prompt and schema as every other one.

Anti-hallucination / prompt-injection defenses, modeled on
``app.agents.clip_plan_matcher``:
  - Clips are referenced by SHORT ALIASES (``m001``, ``m002``, ...) assigned by
    the caller. Real media ids / GCS paths are NEVER rendered into the prompt —
    unlike ``clip_plan_matcher``'s GCS-path echo (structurally required there),
    an intent can span dozens of clips and a long id is needless copy-risk.
  - ``parse()`` drops any assignment/needs_vision entry referencing an unknown
    alias, and any intent block referencing an unknown ``intent_id`` — both are
    hallucination defenses, not user input (the alias/id space is ours).
  - ``creator_request`` / ``attribute`` / ``creator_text`` are free text a
    creator typed; defanged the same way ``clip_plan_matcher`` defangs
    clip transcripts (role markers, code fences, control chars).
  - Clip records themselves are NOT re-sanitized here: they are the output of
    ``ClipUnderstanding``, whose own field validators already defang role
    markers / fences / control chars at construction time.

Label values are capped to <=3 words in ``parse()`` (reusing
``app.schemas.clip_intents.clean_label_text`` — the SAME rule the render-lane
grounding fence enforces) and forced to ``None`` for membership ops
(``group``/``order``/``include``/``caption``): only ``label`` prints a
per-clip value. ``caption`` (KRI-129) is membership-only at the assignment
level too — its ONE authored on-screen phrase for the whole chapter lives on
the intent-level ``caption`` output field instead, capped to <=10 words via
``app.schemas.clip_intents.clean_caption_text``. A caption intent that quotes
the creator's own words (``creator_text`` set) never authors anything here —
the resolver only ever decides membership for it; the caller applies the
creator's text verbatim.

Unlike ``music_matcher``, an EMPTY result is valid per intent — a "group the
pub videos" request over a clip batch with no pub clips returns no assignments
for that intent (the service then asks the creator; see
``clip_intent_resolution.py``), not a fabricated match.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import (
    CAPTION_MAX_WORDS,
    LABEL_MAX_WORDS,
    clean_caption_text,
    clean_label_text,
)

# ── Prompt-injection sanitization (creator-authored free text only; clip
# records are pre-sanitized by ClipUnderstanding's own field validators) ─────
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?i)(^|[\s.;!?])(system|assistant|user|tool|developer)\s*[:>]")
_FENCE = re.compile(r"```+")
_MAX_FREE_TEXT_CHARS = 400


def _sanitize_text(s: str, *, limit: int = _MAX_FREE_TEXT_CHARS) -> str:
    if not s:
        return ""
    s = _CONTROL_CHARS.sub(" ", s)
    s = _ROLE_MARKERS.sub(r"\1[role-marker-stripped]", s)
    s = _FENCE.sub("'''", s)
    s = " ".join(s.split())
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


# ── Schemas ───────────────────────────────────────────────────────────────────

ClipRequestIntentOp = Literal["label", "group", "order", "include", "caption"]


class ResolverIntentIn(BaseModel):
    """One creator intent, as the resolver sees it (mirrors ClipIntent)."""

    intent_id: str = Field(min_length=1, max_length=40)
    op: ClipRequestIntentOp
    attribute: str = Field(min_length=1, max_length=160)
    creator_text: str | None = Field(default=None, max_length=60)
    # Only for op="caption" with no creator_text: what the caption should be
    # ABOUT, as distinct from `attribute` (which clips it's for).
    caption_attribute: str | None = Field(default=None, max_length=160)
    position: Literal["first", "last"] | None = None


class ResolverClipIn(BaseModel):
    """One clip, aliased. ``record`` is ``ClipUnderstanding.prompt_view()``."""

    alias: str = Field(min_length=1, max_length=12)
    kind: Literal["video", "image"] = "video"
    record: dict[str, Any] = Field(default_factory=dict)


class ClipRequestResolverInput(BaseModel):
    creator_request: str = Field(default="", max_length=CREATOR_REQUEST_MAX_CHARS)
    intents: list[ResolverIntentIn]
    clips: list[ResolverClipIn]


class ResolverAssignment(BaseModel):
    media: str = Field(min_length=1, max_length=12)
    value: str | None = Field(default=None, max_length=60)
    evidence: str = Field(default="", max_length=240)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("evidence", mode="before")
    @classmethod
    def _evidence(cls, v: object) -> str:
        if not isinstance(v, str):
            return ""
        return " ".join(v.split())[:240]


class ResolverVisionQuestion(BaseModel):
    media: str = Field(min_length=1, max_length=12)
    question: str = Field(min_length=1, max_length=200)


class ResolverIntentOut(BaseModel):
    intent_id: str
    assignments: list[ResolverAssignment] = Field(default_factory=list)
    needs_vision: list[ResolverVisionQuestion] = Field(default_factory=list)
    # Set when the INTENT ITSELF is ambiguous (e.g. the attribute doesn't map to
    # anything recognizable), independent of any per-clip vision question.
    question: str | None = Field(default=None, max_length=300)
    # Only for op="caption" with no creator_text: the resolver's ONE authored
    # phrase for the whole chapter (<=10 words, reusing only words already in
    # the matched clips' records) — never set for any other op, and never used
    # when the intent has a creator_text (the caller applies that verbatim).
    caption: str | None = Field(default=None, max_length=200)


class ClipRequestResolverOutput(BaseModel):
    intents: list[ResolverIntentOut] = Field(default_factory=list)


# ── Prompt rendering ──────────────────────────────────────────────────────────


def _format_intent(intent: ResolverIntentIn) -> str:
    bits = [f'attribute="{_sanitize_text(intent.attribute, limit=160)}"']
    if intent.creator_text:
        bits.append(f'creator_text="{_sanitize_text(intent.creator_text, limit=60)}"')
    if intent.caption_attribute:
        bits.append(f'caption_attribute="{_sanitize_text(intent.caption_attribute, limit=160)}"')
    if intent.position:
        bits.append(f"position={intent.position}")
    return f"- intent_id={intent.intent_id} | op={intent.op} | " + " | ".join(bits)


def _format_clip(clip: ResolverClipIn) -> str:
    # `record` is pre-sanitized ClipUnderstanding.prompt_view() output; embed as
    # compact JSON so every field the vision analyzer wrote is visible verbatim.
    return f"- {clip.alias} ({clip.kind}): {json.dumps(clip.record, ensure_ascii=False)}"


class ClipRequestResolverAgent(Agent[ClipRequestResolverInput, ClipRequestResolverOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.clip_request_resolver",
        prompt_id="clip_request_resolver",
        prompt_version="2026-09-22.1",  # KRI-133: preserve the full creator request.
        # Text-only match against pre-computed clip records; flash + a small
        # thinking budget mirrors clip_plan_matcher's measured setting.
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=384,
        # Runs synchronously inside a chat turn: keep the worst case short
        # (defaults allow ~249s of retries). A failure degrades to a question.
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=20.0,
    )
    Input = ClipRequestResolverInput
    Output = ClipRequestResolverOutput

    def required_fields(self) -> list[str]:
        return ["intents"]

    def render_prompt(self, input: ClipRequestResolverInput) -> str:  # noqa: A002
        intent_lines = "\n".join(_format_intent(i) for i in input.intents)
        clip_lines = "\n".join(_format_clip(c) for c in input.clips)
        valid_aliases = ", ".join(c.alias for c in input.clips)
        valid_intent_ids = ", ".join(i.intent_id for i in input.intents)
        return load_prompt(
            "clip_request_resolver",
            creator_request=_sanitize_text(input.creator_request, limit=CREATOR_REQUEST_MAX_CHARS),
            intent_count=str(len(input.intents)),
            clip_count=str(len(input.clips)),
            intent_lines=intent_lines,
            clip_lines=clip_lines,
            valid_aliases=valid_aliases,
            valid_intent_ids=valid_intent_ids,
        )

    def parse(
        self,
        raw_text: str,
        input: ClipRequestResolverInput,  # noqa: A002
    ) -> ClipRequestResolverOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"clip_request_resolver: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("clip_request_resolver: response is not a JSON object")

        raw_intents = data.get("intents")
        if raw_intents is None:
            raw_intents = []
        if not isinstance(raw_intents, list):
            raise SchemaError("clip_request_resolver: 'intents' must be a list")

        by_id = {i.intent_id: i for i in input.intents}
        valid_aliases = {c.alias for c in input.clips}
        op_by_id = {i.intent_id: i.op for i in input.intents}

        kept_intents: list[ResolverIntentOut] = []
        for entry in raw_intents:
            if not isinstance(entry, dict):
                continue
            intent_id = entry.get("intent_id")
            if not isinstance(intent_id, str) or intent_id not in by_id:
                # Hallucinated intent id — the id space is ours, not the
                # creator's, so silently drop rather than surface a schema
                # error over a single bad block.
                continue
            op = op_by_id[intent_id]

            seen_assignment_media: set[str] = set()
            assignments: list[ResolverAssignment] = []
            for a in entry.get("assignments") or []:
                if not isinstance(a, dict):
                    continue
                media = a.get("media")
                if not isinstance(media, str) or media not in valid_aliases:
                    continue
                if media in seen_assignment_media:
                    continue
                try:
                    confidence = float(a.get("confidence", 0.0) or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                confidence = max(0.0, min(1.0, confidence))
                value: str | None = None
                if op == "label":
                    value = clean_label_text(a.get("value"))
                    # A label op with no usable (<=3 word, clean) value carries
                    # no information the render fence could ever accept — drop
                    # the assignment rather than keep a value that will always
                    # fail grounding downstream.
                    if value is None:
                        continue
                try:
                    assignments.append(
                        ResolverAssignment(
                            media=media,
                            value=value,
                            evidence=str(a.get("evidence", "") or ""),
                            confidence=confidence,
                        )
                    )
                except ValidationError:
                    continue
                seen_assignment_media.add(media)

            seen_vision_media: set[str] = set()
            needs_vision: list[ResolverVisionQuestion] = []
            for nv in entry.get("needs_vision") or []:
                if not isinstance(nv, dict):
                    continue
                media = nv.get("media")
                question = nv.get("question")
                if not isinstance(media, str) or media not in valid_aliases:
                    continue
                if not isinstance(question, str) or not question.strip():
                    continue
                if media in seen_vision_media:
                    continue
                try:
                    needs_vision.append(
                        ResolverVisionQuestion(media=media, question=question.strip())
                    )
                except ValidationError:
                    continue
                seen_vision_media.add(media)

            question = entry.get("question")
            question = question.strip() if isinstance(question, str) and question.strip() else None

            # An authored `caption` only ever means anything for op="caption"
            # with no creator_text (a quoted caption's text is the creator's
            # own words, applied verbatim by the caller — nothing to author).
            caption: str | None = None
            if op == "caption" and not by_id[intent_id].creator_text:
                caption = clean_caption_text(entry.get("caption"))

            kept_intents.append(
                ResolverIntentOut(
                    intent_id=intent_id,
                    assignments=assignments,
                    needs_vision=needs_vision,
                    question=question,
                    caption=caption,
                )
            )

        try:
            return ClipRequestResolverOutput(intents=kept_intents)
        except ValidationError as exc:
            raise SchemaError(f"clip_request_resolver: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY the JSON object described above. Every "
            "`intent_id` you return MUST be copied verbatim from the listed "
            "intent_ids, and every `media` value MUST be one of the listed "
            "aliases — do not invent either. A `label` op's `value` MUST be "
            f"{LABEL_MAX_WORDS} words or fewer and reuse words that already "
            "appear in that clip's record. If a clip's record does not name "
            "the thing being asked about, put it in `needs_vision` instead of "
            "guessing. If nothing in the batch matches an intent, return an "
            "empty `assignments` list for it rather than forcing a weak match. "
            "A `caption` op's `assignments` decide MEMBERSHIP only (leave "
            "`value` empty, like `group`); when the intent has no "
            "`creator_text`, also fill the intent-level `caption` field with "
            f"ONE phrase of {CAPTION_MAX_WORDS} words or fewer, reusing only "
            "words already in the matched clips' records — never invent it. "
            "When the intent HAS `creator_text`, leave `caption` null; that "
            "text is applied verbatim by the caller."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = [
    "ClipRequestResolverAgent",
    "ClipRequestResolverInput",
    "ClipRequestResolverOutput",
    "ResolverAssignment",
    "ResolverClipIn",
    "ResolverIntentIn",
    "ResolverIntentOut",
    "ResolverVisionQuestion",
]
