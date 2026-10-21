"""nova.chat.suggester: rank and word the suggestion chips the server already allowed (KRI-756).

A small, text-only Flash agent that runs in a worker, off the request path
(``app/tasks/kria_suggester.py``). The deterministic generator
(``app.services.suggestion_chips.build_candidates``) decides which chips MAY exist; this agent
only chooses which of them matter most for THIS footage, in what order, and how to word the
display label and the one-line ``why``. It can only return candidate ids it was given:
``kind``, ``capability``, ``message``, ``target`` and ``anchor`` are always copied from the
candidate, and ``suggestion_chip_copy`` validates every label and ``why`` it writes.

Input uses opaque handles (``c1`` for a clip, ``c2.m1`` for a moment) mapped server side, so
the model never types a media id or a timestamp. No transcript and no creator-typed text is
sent at first_prompt; at ready only the creator's own latest request is.

Prompt: ``prompts/chip_suggester.txt``. Bump ``PROMPT_VERSION`` (suggestion_chips.py) with it.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt
from app.services.suggestion_chip_copy import lexicon_words
from app.services.suggestion_chips import PROMPT_VERSION

_TEXT_CHARS = 80


class FootageMomentIn(BaseModel):
    ref: str = Field(max_length=16)
    description: str = Field(max_length=60)


class FootageClipIn(BaseModel):
    ref: str = Field(max_length=8)
    kind: Literal["video", "image"] = "video"
    subject: str = Field(default="", max_length=48)
    setting: str = Field(default="", max_length=60)
    speech: Literal["none", "voiceover", "to_camera"] = "none"
    moments: list[FootageMomentIn] = Field(default_factory=list, max_length=3)


class CandidateIn(BaseModel):
    cid: str = Field(min_length=1, max_length=32)
    kind: str = Field(min_length=1, max_length=32)
    gap_tags: list[str] = Field(default_factory=list, max_length=6)
    facts: dict[str, Any] = Field(default_factory=dict)
    label_slots: list[str] = Field(default_factory=list, max_length=4)
    fallback_label: str = Field(max_length=48)


class ChipSuggesterInput(BaseModel):
    state: Literal["first_prompt", "ready"]
    language: Literal["en", "tr"] = "en"
    max_chips: int = Field(ge=1, le=4)
    footage: list[FootageClipIn] = Field(default_factory=list, max_length=12)
    already_in_edit: list[str] = Field(default_factory=list, max_length=12)
    candidates: list[CandidateIn] = Field(default_factory=list, max_length=8)
    # ready only; the creator's own latest request (<= 200 chars).
    typed_request: str | None = Field(default=None, max_length=200)


class RankedChipOut(BaseModel):
    cid: str
    label_template: str = ""
    why: str | None = None


class ChipSuggesterOutput(BaseModel):
    default_label: str | None = None
    ranked: list[RankedChipOut] = Field(default_factory=list)
    # Filled by parse() (never by the model): what structural checks removed, by reason.
    drop_reasons: dict[str, int] = Field(default_factory=dict)


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())[:_TEXT_CHARS]


class ChipSuggesterAgent(Agent[ChipSuggesterInput, ChipSuggesterOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.chat.suggester",
        prompt_id="chip_suggester",
        prompt_version=PROMPT_VERSION,
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Ranking a handful of candidates and rewording three labels needs no reasoning depth.
        thinking_budget=128,
        # Runs in a background task whose result the GET waits at most a few seconds for.
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=12.0,
        sensitive_io=False,
    )
    Input = ChipSuggesterInput
    Output = ChipSuggesterOutput
    response_json = True
    max_output_tokens = 900

    def required_fields(self) -> list[str]:
        # An empty ranking is a legitimate answer ("nothing helps here").
        return []

    def render_prompt(self, input: ChipSuggesterInput) -> str:  # noqa: A002
        kinds = sorted({c.kind for c in input.candidates} | {"default_edit"})
        vocabulary = "\n".join(
            f"- {kind}: {', '.join(lexicon_words(kind, input.language))}"
            for kind in kinds
            if lexicon_words(kind, input.language)
        )
        return load_prompt(
            "chip_suggester",
            state=input.state,
            language=input.language,
            max_chips=str(input.max_chips),
            vocabulary=vocabulary,
            payload=json.dumps(
                input.model_dump(exclude={"state", "language", "max_chips"}, exclude_none=True),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

    def parse(self, raw_text: str, input: ChipSuggesterInput) -> ChipSuggesterOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"chip_suggester: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("chip_suggester: response is not a JSON object")
        raw_ranked = data.get("ranked")
        if raw_ranked is None:
            raw_ranked = []
        if not isinstance(raw_ranked, list):
            raise SchemaError("chip_suggester: ranked must be an array")

        kind_of = {c.cid: c.kind for c in input.candidates}
        reasons: dict[str, int] = {}

        def drop(reason: str) -> None:
            reasons[reason] = reasons.get(reason, 0) + 1

        ranked: list[RankedChipOut] = []
        seen_cids: set[str] = set()
        seen_kinds: set[str] = set()
        for row in raw_ranked:
            if not isinstance(row, dict):
                drop("not_object")
                continue
            cid = str(row.get("cid") or "")
            if cid not in kind_of:
                drop("unknown_cid")
                continue
            if cid in seen_cids or kind_of[cid] in seen_kinds:
                drop("duplicate_kind")
                continue
            if len(ranked) >= input.max_chips:
                drop("over_limit")
                continue
            why = _clean(row.get("why")) or None
            seen_cids.add(cid)
            seen_kinds.add(kind_of[cid])
            ranked.append(
                RankedChipOut(cid=cid, label_template=_clean(row.get("label_template")), why=why)
            )
        default_label = _clean(data.get("default_label")) or None
        if input.state != "first_prompt":
            default_label = None
        try:
            return ChipSuggesterOutput(
                default_label=default_label, ranked=ranked, drop_reasons=reasons
            )
        except ValidationError as exc:
            raise SchemaError(f"chip_suggester: output validation — {exc}") from exc

    def _log_outcome(self, **kwargs: Any) -> None:
        # Keep the run's model and cost on the instance so the task can put them in its own
        # `suggestion_chips.agent_run` event (the framework event carries them too).
        stats = kwargs.get("stats")
        if stats is not None:
            cost = (
                stats.settled_cost_usd
                if stats.price_version is not None
                else (stats.tokens_in / 1000.0) * self.spec.cost_per_1k_input_usd
                + (stats.tokens_out / 1000.0) * self.spec.cost_per_1k_output_usd
            )
            self.last_run = {"model": kwargs.get("model"), "cost_usd": round(cost, 6)}
        super()._log_outcome(**kwargs)

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY the JSON object described above, no markdown and no "
            'prose. Each "cid" must be copied exactly from the candidates.'
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = [
    "ChipSuggesterAgent",
    "ChipSuggesterInput",
    "ChipSuggesterOutput",
    "RankedChipOut",
]
