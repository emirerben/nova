"""nova.plan.passage_selector -- choose which passages of one spoken take to keep (KRI-1555).

Text-only. It reads the creator's request, an optional outline, a length target and
every timed sentence of ONE transcript (or one window of a long one), each with an
integer ``id``. It answers with inclusive id ranges, never timestamps: the caller
(``app.services.passage_selector``) owns the clock, so a range can only begin and end
on a sentence boundary and a range the model made up is rejected.

``mode="pick"`` returns the passages worth keeping, each with a verbatim ``quote``
(checked against the words of that id range) and a ``strength`` the caller uses to
fit the length. ``mode="drop"`` keeps everything except what the creator asked to
remove, so only ``dropped`` matters.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents._skills import load_skill
from app.agents.speech_excerpt_planner import defang
from app.pipeline.prompt_loader import load_prompt

MAX_UNITS = 1500
MAX_PASSAGES = 80
MAX_DROP_TOPICS = 12
OUTLINE_MAX_CHARS = 12_000
MAX_QUOTE_CHARS = 400
MAX_REASON_CHARS = 160

DropReason = Literal["off_topic", "requested_drop"]


class PassageUnitView(BaseModel):
    id: int = Field(ge=0)
    s: float = Field(ge=0)
    e: float = Field(ge=0)
    text: str = Field(max_length=1200)


class PassageSelectorInput(BaseModel):
    mode: Literal["pick", "drop"] = "pick"
    request: str = Field(default="", max_length=CREATOR_REQUEST_MAX_CHARS)
    target_s: float | None = Field(default=None, gt=0)
    target_is_ceiling: bool = False
    candidate_budget_s: float | None = Field(default=None, gt=0)
    outline: str | None = Field(default=None, max_length=OUTLINE_MAX_CHARS)
    drop_topics: list[str] = Field(default_factory=list, max_length=MAX_DROP_TOPICS)
    language: str = Field(default="", max_length=8)
    window_index: int = Field(default=0, ge=0)
    window_count: int = Field(default=1, ge=1)
    units: list[PassageUnitView] = Field(min_length=1, max_length=MAX_UNITS)


class PickedPassage(BaseModel):
    first: int = Field(ge=0)
    last: int = Field(ge=0)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)
    reason: str = Field(default="", max_length=MAX_REASON_CHARS)
    strength: int = Field(default=3, ge=1, le=5)


class DroppedRange(BaseModel):
    first: int = Field(ge=0)
    last: int = Field(ge=0)
    reason: DropReason = "off_topic"


class PassageSelectorOutput(BaseModel):
    passages: list[PickedPassage] = Field(default_factory=list, max_length=MAX_PASSAGES)
    dropped: list[DroppedRange] = Field(default_factory=list, max_length=MAX_PASSAGES)


def _clean(value: object, limit: int) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


class PassageSelectorAgent(Agent[PassageSelectorInput, PassageSelectorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.passage_selector",
        prompt_id="passage_selector",
        prompt_version="2026-11-28-passages-v1",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=1024,
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=45.0,
        # Transcripts, the outline and the creator's words are private text.
        sensitive_io=True,
    )
    Input = PassageSelectorInput
    Output = PassageSelectorOutput

    def required_fields(self) -> list[str]:
        return []

    def project_input_for_observability(
        self, input_dict: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(input_dict, dict):
            return None
        request = str(input_dict.get("request") or "")
        outline = str(input_dict.get("outline") or "")
        return {
            "mode": input_dict.get("mode"),
            "request": {
                "redacted": True,
                "chars": len(request),
                "sha256": sha256(request.encode()).hexdigest(),
            },
            "outline_chars": len(outline),
            "units": len(input_dict.get("units") or []),
            "window": [input_dict.get("window_index"), input_dict.get("window_count")],
        }

    def render_prompt(self, input: PassageSelectorInput) -> str:  # noqa: A002
        prompt = load_prompt(
            "passage_selector",
            mode=input.mode,
            request=defang(input.request) or "(none)",
            target_s="null" if input.target_s is None else f"{input.target_s:g}",
            target_kind="ceiling" if input.target_is_ceiling else "target",
            candidate_budget_s=(
                "null" if input.candidate_budget_s is None else f"{input.candidate_budget_s:g}"
            ),
            outline=defang(input.outline or "") or "(none)",
            drop_topics=json.dumps([defang(t) for t in input.drop_topics], ensure_ascii=False),
            language=input.language or "unknown",
            window=f"{input.window_index + 1} of {input.window_count}",
            units=json.dumps([unit.model_dump() for unit in input.units], ensure_ascii=False),
        )
        return load_skill("meaningful-moment") + "\n\n" + prompt

    def parse(self, raw_text: str, input: PassageSelectorInput) -> PassageSelectorOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"passage_selector: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("passage_selector: response is not a JSON object")
        passages = _rows(data, "passages")
        dropped = _rows(data, "dropped")
        valid = {unit.id for unit in input.units}
        picked: list[PickedPassage] = []
        removed: list[DroppedRange] = []
        try:
            if input.mode == "pick":
                for row in passages:
                    row = {**row, "quote": _clean(row.get("quote"), MAX_QUOTE_CHARS)}
                    row["reason"] = _clean(row.get("reason"), MAX_REASON_CHARS)
                    row["strength"] = _strength(row.get("strength"))
                    picked.append(PickedPassage.model_validate(row))
            for row in dropped:
                removed.append(DroppedRange.model_validate(row))
        except ValidationError as exc:
            raise SchemaError(f"passage_selector: invalid row — {exc}") from exc
        for row in (*picked, *removed):
            if row.first > row.last or row.first not in valid or row.last not in valid:
                raise SchemaError(
                    f"passage_selector: range {row.first}-{row.last} is not within the "
                    "units you were given"
                )
        if input.mode == "pick" and not picked and input.window_count == 1:
            raise SchemaError("passage_selector: pick mode needs at least one passage")
        return PassageSelectorOutput(passages=picked, dropped=removed)

    def schema_clarification(self) -> str:
        return (
            "\n\nReturn ONLY the JSON object. Every passage needs first and last copied from "
            "the unit ids you were given (first <= last), a verbatim quote from those units, "
            "and a strength from 1 to 5. In pick mode return at least one passage unless you are "
            "reading one window of a longer talk."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


def _rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = data.get(key) or []
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SchemaError(f"passage_selector: {key} must be a list of objects")
    return rows


def _strength(value: object) -> int:
    try:
        return min(max(int(value), 1), 5)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 3


__all__ = [
    "DroppedRange",
    "PassageSelectorAgent",
    "PassageSelectorInput",
    "PassageSelectorOutput",
    "PassageUnitView",
    "PickedPassage",
]
