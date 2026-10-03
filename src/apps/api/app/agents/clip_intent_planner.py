"""Extract complete, source-backed clip operations from creator chat text."""

from __future__ import annotations

import json
import re
import unicodedata
from hashlib import sha256
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError, model_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.pipeline.prompt_loader import load_prompt
from app.schemas.clip_intents import MAX_CLIP_INTENTS, ClipIntent

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(r"(?i)(^|[\s.;!?])(system|assistant|user|tool|developer)\s*[:>]")
_FENCE = re.compile(r"```+")

# Rendered onto the `order` bullet ONLY when CLIP_FACTS is on ("" otherwise, so the
# flag-off prompt is byte-identical). Ordering by when clips were filmed needs no
# footage reading: the server sorts by each clip's capture-time fact.
_ORDER_BY_NOTE = (
    '\n  A request to arrange ALL the clips by when or where they were filmed ("in the order'
    ' I filmed them", "chronological", "in the order of my route", "from first stop to last")'
    ' is an order intent with `order_by`: "capture_time" for filming order, "route" for a'
    " route walked or driven. You MUST include that `order_by` value on the returned intent;"
    " never omit it. Leave `position` null for it; it is never combined with first/last."
    " Attribute: the creator's own words for the order."
)

_ORDER_BY_FIELD = ',"order_by":"capture_time|route|null"'


def _sanitize_text(value: str) -> str:
    value = _CONTROL_CHARS.sub(" ", value)
    value = _ROLE_MARKERS.sub(r"\1[role-marker-stripped]", value)
    return _FENCE.sub("'''", value)


_QUOTE_TABLE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "′": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        "–": "-",
        "—": "-",
        "−": "-",
    }
)
_CREATOR_TEXT_MAX = 60
_LABEL_PREVIEW_CHARS = 40
_QUESTION_MAX_CHARS = 400
_KNOWN_OPS = {"label", "group", "order", "include", "caption"}
# KRI-282: "add a text placeholder so I can replace it with their real names".
# Flash does not reliably set `placeholder: true` (it has mapped this to a
# creator_text caption or a grounded "person's name" label), so a quote that
# literally asks for a placeholder is the deterministic signal.
_PLACEHOLDER_REQUEST = re.compile(r"\bplace[\s-]?holders?\b", re.IGNORECASE)
_STOPWORDS = frozenset(
    "a an the and or of to for in on at by with all my our your their clips clip shots shot "
    "videos video content footage".split()
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?\n])\s+")


def _norm(value: str) -> str:
    """Compare creator text the way the model saw it, tolerant of typography.

    The prompt shows the model ``_sanitize_text(...)`` output, and models freely
    swap curly/straight quotes, dash styles and line breaks when copying. None of
    that changes what the creator wrote, so provenance is checked on a form with
    sanitization applied, quotes/dashes unified and whitespace collapsed.
    """
    value = unicodedata.normalize("NFKC", _sanitize_text(value)).translate(_QUOTE_TABLE)
    return " ".join(value.split())


class ClipIntentSchemaError(SchemaError):
    """Planner output failed validation.

    ``error_class`` is a closed vocabulary (never model or creator text) so a
    sensitive agent can still record WHY a run failed. ``dropped`` carries short
    attribute previews of instructions that could not be verified, used only to
    word a specific question back to the creator.
    """

    def __init__(self, message: str, *, error_class: str, dropped: list[str] | None = None) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.dropped = dropped or []


class _IntentRejected(Exception):  # noqa: N818 - internal control flow
    def __init__(self, error_class: str, detail: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.detail = detail


def _preview(raw_intent: object) -> str:
    """Short, sanitized description of a returned intent, for a creator question."""
    if not isinstance(raw_intent, dict):
        return "an instruction"
    attribute = raw_intent.get("attribute")
    op = raw_intent.get("op")
    text = " ".join(_sanitize_text(attribute).split()) if isinstance(attribute, str) else ""
    text = text[:_LABEL_PREVIEW_CHARS].strip() or "an instruction"
    return f"{op}: {text}" if isinstance(op, str) and op in _KNOWN_OPS else text


def salvage_question(kept: int, labels: list[str], overflow: int = 0) -> str:
    """One focused question naming what could not be verified (labels are previews)."""
    named = "; ".join(f'"{label}"' for label in labels[:3])
    extra = len(labels) - 3
    if extra > 0:
        named += f" (and {extra} more)"
    pieces: list[str] = []
    if named:
        pieces.append(f"I couldn't safely verify {named}")
    if overflow:
        pieces.append(
            f"I can apply at most {MAX_CLIP_INTENTS} clip instructions at a time, "
            f"so {overflow} more weren't included"
        )
    lead = f"I understood {kept} of your clip instructions"
    body = "; ".join(pieces)
    text = f"{lead}. {body}." if body else f"{lead}."
    return (text + " Please restate just those so I can add them.")[:_QUESTION_MAX_CHARS]


def _repair_placeholder(data: dict[str, Any], sources: tuple[str, ...]) -> None:
    """Normalise a placeholder request into ``label`` + ``placeholder=true`` in place.

    The text is the system's fixed stand-in, never creator copy, so any
    ``creator_text`` the model attached ("NAME", "[Name]") is discarded rather
    than rejected by the source fence -- the creator is never asked to restate
    something the system can resolve itself.
    """
    quote = data.get("source_quote")
    flagged = data.get("placeholder") is True
    asked = (
        isinstance(quote, str)
        and bool(_PLACEHOLDER_REQUEST.search(quote))
        and data.get("op") in {"label", "caption"}
        and any(_norm(quote) in source for source in sources)
    )
    if not (flagged or asked):
        data.pop("placeholder", None)
        return
    data.update(
        op="label",
        placeholder=True,
        label_source="clip",
        creator_text=None,
        caption_attribute=None,
        position=None,
        order_by=None,
    )
    data.pop("transcript_kind", None)
    if not str(data.get("attribute") or "").strip():
        data["attribute"] = "individual shots of people"


def _sentence_naming(attribute: str, sources: tuple[str, ...]) -> str | None:
    """The creator sentence that contains every content word of ``attribute``."""
    words = {w for w in re.findall(r"\w+", attribute.casefold()) if w not in _STOPWORDS}
    if not words:
        return None
    for source in sources:
        for sentence in _SENTENCE_SPLIT.split(source):
            sentence = sentence.strip()
            if sentence and words <= set(re.findall(r"\w+", sentence.casefold())):
                return sentence[:600]
    return None


class PlannedClipIntent(ClipIntent):
    """A clip operation with its exact creator-written provenance."""

    source_quote: str = Field(min_length=1, max_length=600)

    @model_validator(mode="after")
    def _validate_caption_shape(self) -> PlannedClipIntent:
        if self.op != "caption":
            return self
        if self.creator_text is None and self.caption_attribute is None:
            raise ValueError("authored captions require caption_attribute")
        if self.creator_text is not None and self.caption_attribute is not None:
            raise ValueError("exact creator caption copy requires caption_attribute=null")
        return self


class ClipIntentPlannerInput(BaseModel):
    creator_request: str = Field(min_length=1, max_length=CREATOR_REQUEST_MAX_CHARS)
    latest_user_message: str | None = Field(default=None, max_length=CREATOR_REQUEST_MAX_CHARS)
    # A generated Creative Brief can aid recall but is not creator-authored
    # provenance. Quotes from it never satisfy the parser's source fence.
    generated_brief: str | None = Field(default=None, max_length=CREATOR_REQUEST_MAX_CHARS)
    # Strategy-produced candidates can aid recall, but never authorize output.
    candidate_intents: list[ClipIntent] | None = Field(default=None, max_length=MAX_CLIP_INTENTS)
    # KRI-189: CLIP_FACTS is on for this creator, so the prompt also teaches the
    # fact-based `order_by` field. Omitted from dumps when off (byte-identical).
    clip_facts: bool = Field(default=False, exclude_if=lambda value: not value)


class ClipIntentPlannerOutput(BaseModel):
    intents: list[PlannedClipIntent] = Field(default_factory=list, max_length=MAX_CLIP_INTENTS)
    question: str | None = Field(default=None, max_length=300)
    # Parser-authored (never model-authored): set when some instructions were
    # dropped or exceeded the cap. The kept ``intents`` are valid, but the
    # caller must ask this instead of silently acting on a subset.
    salvage_question: str | None = Field(
        default=None, max_length=500, exclude_if=lambda value: value is None
    )


class ClipIntentPlannerAgent(Agent[ClipIntentPlannerInput, ClipIntentPlannerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.clip_intent_planner",
        prompt_id="clip_intent_planner",
        prompt_version="2026-10-03.2",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=384,
        max_attempts=2,
        backoff_s=(1.0,),
        timeout_s=20.0,
        sensitive_io=True,
    )
    Input = ClipIntentPlannerInput
    Output = ClipIntentPlannerOutput

    def required_fields(self) -> list[str]:
        # Empty is the valid result for a request that only changes style,
        # duration, music, or other non-clip operations.
        return []

    def project_input_for_observability(
        self, input_dict: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not isinstance(input_dict, dict):
            return None

        def redact(value: object) -> dict[str, object]:
            text = str(value or "")
            return {
                "redacted": True,
                "chars": len(text),
                "sha256": sha256(text.encode()).hexdigest(),
            }

        candidates = input_dict.get("candidate_intents") or []
        return {
            "creator_request": redact(input_dict.get("creator_request")),
            "latest_user_message": redact(input_dict.get("latest_user_message")),
            "generated_brief": redact(input_dict.get("generated_brief")),
            "candidate_intents_count": len(candidates) if isinstance(candidates, list) else 0,
        }

    def render_prompt(self, input: ClipIntentPlannerInput) -> str:  # noqa: A002
        candidates = [intent.model_dump(mode="json") for intent in (input.candidate_intents or [])]
        return load_prompt(
            "clip_intent_planner",
            creator_request=_sanitize_text(input.creator_request),
            latest_user_message=_sanitize_text(input.latest_user_message or ""),
            generated_brief=_sanitize_text(input.generated_brief or ""),
            candidate_intents=json.dumps(candidates, ensure_ascii=False),
            max_intents=str(MAX_CLIP_INTENTS),
            order_by_note=_ORDER_BY_NOTE if input.clip_facts else "",
            order_by_field=_ORDER_BY_FIELD if input.clip_facts else "",
        )

    def parse(self, raw_text: str, input: ClipIntentPlannerInput) -> ClipIntentPlannerOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (TypeError, ValueError) as exc:
            raise ClipIntentSchemaError(
                f"clip_intent_planner: invalid JSON — {exc}", error_class="invalid_json"
            ) from exc
        if not isinstance(data, dict):
            raise ClipIntentSchemaError(
                "clip_intent_planner: response is not a JSON object", error_class="not_object"
            )
        raw_intents = data.get("intents")
        if not isinstance(raw_intents, list):
            raise ClipIntentSchemaError(
                "clip_intent_planner: intents must be a list", error_class="intents_not_list"
            )
        question = data.get("question")
        if question is not None and (not isinstance(question, str) or not question.strip()):
            raise ClipIntentSchemaError(
                "clip_intent_planner: question must be a non-empty string or null",
                error_class="bad_question",
            )

        # Validate provenance against the text the model actually saw (sanitized),
        # tolerant of quote/dash/whitespace typography.
        sources = tuple(
            _norm(source) for source in (input.creator_request, input.latest_user_message or "")
        )
        kept: list[tuple[PlannedClipIntent, object]] = []
        dropped: list[str] = []
        drop_classes: list[str] = []
        failures: list[str] = []
        seen: set[tuple[str, str, str | None, str | None, str | None, str, str | None, bool]] = (
            set()
        )
        seen_ids: set[str] = set()
        for index, raw_intent in enumerate(raw_intents):
            try:
                intent = self._build_intent(raw_intent, sources, input)
            except _IntentRejected as rejected:
                dropped.append(_preview(raw_intent))
                drop_classes.append(rejected.error_class)
                failures.append(f"intents[{index}]: {rejected.detail}")
                continue
            if intent is None:
                continue
            if intent.intent_id in seen_ids:
                dropped.append(_preview(raw_intent))
                drop_classes.append("duplicate_intent_id")
                failures.append(f"intents[{index}]: intent_id must be unique")
                continue
            key = (
                intent.op,
                intent.attribute.casefold(),
                intent.creator_text,
                intent.position,
                intent.order_by,
                intent.label_source,
                intent.transcript_kind,
                intent.placeholder,
            )
            if key in seen:
                continue  # a verbatim repeat adds nothing; not worth failing the output
            seen.add(key)
            seen_ids.add(intent.intent_id)
            kept.append((intent, raw_intent))

        if question is not None:
            if kept:
                raise ClipIntentSchemaError(
                    "clip_intent_planner: question cannot accompany partial intents",
                    error_class="question_with_intents",
                )
            return ClipIntentPlannerOutput(intents=[], question=question.strip())
        if not kept and dropped:
            raise ClipIntentSchemaError(
                "clip_intent_planner: every intent was rejected — " + "; ".join(failures[:3]),
                error_class=drop_classes[0],
                dropped=dropped,
            )
        # Over the cap: keep the first valid ones in the creator's order and ask about
        # the rest instead of failing the whole inventory.
        overflow = [_preview(raw) for _, raw in kept[MAX_CLIP_INTENTS:]]
        kept = kept[:MAX_CLIP_INTENTS]
        salvage = None
        if dropped or overflow:
            salvage = salvage_question(len(kept), dropped + overflow, len(overflow))
        try:
            return ClipIntentPlannerOutput(
                intents=[intent for intent, _ in kept], salvage_question=salvage
            )
        except ValidationError as exc:
            raise ClipIntentSchemaError(
                f"clip_intent_planner: output validation — {exc}",
                error_class="output_validation",
            ) from exc

    @staticmethod
    def _build_intent(
        raw_intent: object,
        sources: tuple[str, ...],
        input: ClipIntentPlannerInput,  # noqa: A002
    ) -> PlannedClipIntent | None:
        """One validated intent; None drops it silently; ``_IntentRejected`` drops it loudly."""
        if not isinstance(raw_intent, dict):
            raise _IntentRejected("intent_not_object", "is not an object")
        # Flash copies the template's nullable neighbours and writes
        # `"label_source": null`; null means the field's default, "clip".
        data = {
            k: v
            for k, v in raw_intent.items()
            if not (k in {"label_source", "placeholder"} and v is None)
        }
        _repair_placeholder(data, sources)
        # Benign shape repairs: none of these change what the creator asked for.
        if isinstance(data.get("creator_text"), str):
            data["creator_text"] = " ".join(data["creator_text"].split()) or None
        if data.get("op") != "caption" or data.get("creator_text") is not None:
            data["caption_attribute"] = None  # exact copy wins over an authored topic
        if data.get("op") != "order":
            data["position"] = None
        creator_text = data.get("creator_text")
        if isinstance(creator_text, str) and len(creator_text) > _CREATOR_TEXT_MAX:
            raise _IntentRejected(
                "creator_text_too_long",
                f"creator_text is over {_CREATOR_TEXT_MAX} characters; copy only the exact"
                " on-screen words, not the whole sentence",
            )
        try:
            intent = PlannedClipIntent.model_validate(data)
        except ValidationError as exc:
            fields = sorted(
                {
                    re.sub(r"[^a-z_]", "", str(err["loc"][0]).lower())
                    for err in exc.errors()
                    if err.get("loc")
                }
            )
            # pydantic `msg` strings carry no input values (those live in `input`),
            # so they are safe to relay to the retry prompt.
            messages = "; ".join(str(err.get("msg", "")) for err in exc.errors())[:200]
            raise _IntentRejected(
                "intent_invalid:" + (",".join(fields) or "model"),
                f"failed validation on {', '.join(fields) or 'intent'} ({messages})",
            ) from exc
        quote = _norm(intent.source_quote)
        if (not quote or not any(quote in source for source in sources)) and intent.op == "group":
            # A group prints nothing, so its quote is only evidence the creator asked
            # for it. When the model garbled the quote but the creator's own sentence
            # names the group ("we played football, then dodgeball ... group content by
            # sport"), pick that sentence ourselves instead of making them restate it.
            repaired = _sentence_naming(intent.attribute, sources)
            if repaired is not None:
                intent = intent.model_copy(update={"source_quote": repaired})
                quote = _norm(repaired)
        if not quote or not any(quote in source for source in sources):
            raise _IntentRejected(
                "source_quote_not_creator_text",
                "source_quote is not an exact contiguous substring of the creator text",
            )
        if intent.creator_text is not None:
            text = _norm(intent.creator_text)
            if text not in quote or not any(text in source for source in sources):
                raise _IntentRejected(
                    "creator_text_not_source_backed",
                    "creator_text must be exact creator words inside source_quote",
                )
        if intent.order_by is not None and not input.clip_facts:
            # The flag-off prompt never teaches `order_by`, so a stray one behaves as if the
            # field did not exist: dropped without retrying the model for nothing.
            return None
        return intent

    def schema_clarification(self) -> str:
        hint = getattr(self, "_last_schema_error", "")
        return (
            "\n\nReturn only the requested JSON. Every intent needs an exact "
            "source_quote copied from the creator text and a non-empty attribute; "
            "do not emit a partial inventory or invent clip facts. For caption, "
            "attribute is always the target clips/chapter. When creator_text has "
            "exact copy, caption_attribute must be null; otherwise caption_attribute "
            "is only the factual topic to author."
            + (f"\nFix this schema error: {hint}" if hint else "")
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()


__all__ = [
    "ClipIntentPlannerAgent",
    "ClipIntentPlannerInput",
    "ClipIntentPlannerOutput",
    "PlannedClipIntent",
]
