"""Creative Brief: the thread-level, versioned requirement ledger (KRI-188).

The brief is what the creator asked for, stored as typed requirements instead of
chat text. Everything here is pure and deterministic except the two small
persistence helpers at the bottom; the model only ever *proposes* requirement
updates (``BriefUpdate``), and the server owns ids, supersession, routing and
status.

Scope router
------------
``route_requirements`` decides, without a model, whether a turn needs a fresh
plan (``replan`` -> ``draft.apply_strategy``) or can be served by reversible
editor operations (``editor_ops`` -> ``draft.apply_editor_ops``):

* ``replan`` when any new requirement is ``order`` or ``select``; when a
  per-clip text requirement arrives and the current plan has no per-clip text
  lane; when the request mixes requirement kinds; when there is no render to
  edit; or when the creator explicitly asks to redo it from their prompt.
* ``editor_ops`` otherwise (including turns with no new requirement).
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic.json_schema import SkipJsonSchema
from sqlalchemy import func, select

from app.agents._schemas.text_element import _ALLOWED_FONTS, _HEX_COLOR_RE
from app.kria.asset_holds import Authority, resolve_clip_scope, stamp_authority
from app.kria.brief_route import fold_text, loose_text, wants_filming_time_text
from app.kria.compose_with_ops import compose_unmet_with_ops
from app.kria.duplicate_send import is_duplicate_answer
from app.kria.order_replacement import OrderReplacementProof
from app.kria.reply_language import detect_chat_language
from app.kria.select_asks import withdrawn_photo_asks
from app.kria.text_shape_ask import TEXT_SHAPE_FACT, with_text_shape
from app.models import CreationThreadEvent, CreativeBriefVersion, CreatorAgentTurn
from app.schemas.clip_intents import asks_clip_description
from app.schemas.text_style_intent import normalize_title_animation
from app.services.creative_copy_decisions import APPROVED_COPY_FACT
from app.services.kria_editor_ops_diff import font_family_key
from app.services.order_mode import order_mode, stamp_order_facts

if TYPE_CHECKING:
    from app.kria.order_replacement_persistence import OrderReplacementWriteContext


# `text_sequence` (29 Ekim): the creator's own words shown on screen in pieces, never captions.
RequirementKind = Literal["text", "order", "select", "timing", "audio", "style", "text_sequence"]
RequirementStatus = Literal["open", "met", "partial", "not_possible", "superseded"]
# Who removed a requirement, stamped into its facts when the removal is applied (never inferred
# later): "creator" for the creator's own words (the default; a model-emitted remove, a fresh
# start), "server" for a waiver the server made (the simpler version, an automatic omission).
# A requirement removed before this existed has no mark: its provenance is unknown.
SUPERSEDED_BY_FACT = "superseded_by"
RemovalProvenance = Literal["creator", "server"]
Route = Literal["replan", "editor_ops"]

# Two titles plus six dictated shot texts used all of the old 8, so a duration
# or "no stock images" ask in the same message was cut (KRI-422).
MAX_UPDATES_PER_TURN = 16
# This bounds a single planner response, not the ledger.  A larger model batch
# is rejected as a whole so the caller can recover without losing its tail.
MAX_BRIEF_REQUEST_CHARS = 9_000
_SCOPE_RE = re.compile(r"^(title|per_clip|global|clip:[A-Za-z0-9._:-]{1,100})$")
_MAX_FACTS_BYTES = 2_000
_MAX_DESCRIPTION_CHARS = 300


def _nfc(value: str) -> str:
    """Turkish (and every other) text stays NFC; never folded to ASCII."""
    return unicodedata.normalize("NFC", value).strip()


_LIST_MARKER = re.compile(r"^\s*\d{1,3}\s*[.):-]\s*")
_EDGE_PUNCT = re.compile(r"^[\W_]+|[\W_]+$")


def _shot_key(description: str) -> str:
    """Match key for a described shot: "1. The Bookshop photo:" == "the bookshop photo".

    Only used to compare requirements; the stored description keeps the
    creator's own spelling.
    """
    return _EDGE_PUNCT.sub("", loose_text(_LIST_MARKER.sub("", description)))


# KRI-543: the structured style intent a creator's words resolve to, so a style ask can be
# checked against the saved text rows instead of staying "can't verify". Closed vocabulary;
# anything outside it is dropped (the requirement then stays unchecked), never raised.
STYLE_INTENT_TARGETS = frozenset({"all_text", "title", "labels"})
_STYLE_TEXT_CASE_ALIASES = {
    "none": "none",
    "normal": "none",
    "original": "none",
    "upper": "upper",
    "uppercase": "upper",
    "upper case": "upper",
    "all caps": "upper",
    "lower": "lower",
    "lowercase": "lower",
    "lower case": "lower",
    "title": "title",
    "titlecase": "title",
    "title case": "title",
    "capitalize": "title",
    "capitalized": "title",
}
_STYLE_ALIGNMENTS = {"left": "left", "center": "center", "centre": "center", "right": "right"}


def _style_value(field: str, value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    folded = re.sub(r"[\s_\-]+", " ", text.casefold())
    if field == "entrance":
        return "none" if folded == "none" else normalize_title_animation(text)
    if field == "alignment":
        return _STYLE_ALIGNMENTS.get(folded)
    if field == "text_case":
        return _STYLE_TEXT_CASE_ALIASES.get(folded)
    if field == "color":
        return text.upper() if _HEX_COLOR_RE.match(text) else None
    if field == "font_family":
        return resolve_font_name(text)
    return None


def resolve_font_name(text: str) -> str | None:
    """The registry font a creator's words name, else None (KRI-558).

    An exact (case-insensitive) name wins, so "Inter Tight" is never read as "Inter". A
    family word ("inter") resolves to the family's plain face; a leading part of one name
    ("alte haas") resolves only when exactly one family starts that way. Anything ambiguous
    names no font, so a wrong guess can never decide a verdict.
    """
    wanted = re.sub(r"[\s_\-]+", " ", text.strip().casefold())
    if not wanted:
        return None
    if text in _ALLOWED_FONTS:
        return text
    exact = next(
        (n for n in sorted(_ALLOWED_FONTS) if n.casefold() == text.strip().casefold()), None
    )
    if exact:
        return exact
    key = font_family_key(wanted)
    family = sorted((n for n in _ALLOWED_FONTS if font_family_key(n) == key), key=len)
    if family:
        return family[0]
    starts: dict[str | None, str] = {}
    for name in sorted(_ALLOWED_FONTS, key=lambda n: (len(n), n)):
        if re.sub(r"[\s_\-]+", " ", name.casefold()).startswith(wanted + " "):
            starts.setdefault(font_family_key(name), name)
    return next(iter(starts.values())) if len(starts) == 1 else None


def normalize_style_intent(raw: object) -> dict[str, Any] | None:
    """The canonical ``facts["style_intent"]``, or None when it is not well formed.

    Shape: ``{"set": [{"field", "value"}, ...], "target"?: all_text|title|labels}``. One value
    per field, no other keys (ids are never accepted), every value from the closed
    vocabulary of the matching ``TextElement`` field. Pure; never raises.
    """
    if not isinstance(raw, dict) or not set(raw) <= {"set", "target"}:
        return None
    items = raw.get("set")
    if not isinstance(items, list) or not items:
        return None
    resolved: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or set(item) != {"field", "value"}:
            return None
        field = item["field"]
        value = _style_value(field, item["value"]) if isinstance(field, str) else None
        if value is None or resolved.setdefault(field, value) != value:
            return None
    target = raw.get("target")
    if target is not None and target not in STYLE_INTENT_TARGETS:
        return None
    out: dict[str, Any] = {"set": [{"field": f, "value": v} for f, v in resolved.items()]}
    if target is not None:
        out["target"] = target
    return out


class _BriefModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefUpdate(_BriefModel):
    """One requirement as proposed by the planner model. Ids/status are server-owned.

    A change/remove may arrive without `expected_version`: the version is the server's
    (`bind_updates` stamps the brief the prompt showed) and `apply_updates` refuses an unbound
    one. A change without `kind` or `scope` is partial: the fields it leaves out keep its
    target's values (KRI-805).
    """

    operation: Literal["add", "change", "remove"] = "add"
    target_requirement_id: str | None = Field(default=None, min_length=1, max_length=24)
    expected_version: int | None = Field(default=None, ge=0)
    kind: RequirementKind | None = None
    scope: str | None = None
    literal: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_CHARS)
    facts: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: object) -> str | None:
        if value is None:
            return None
        scope = _nfc(str(value)).replace(" ", "")
        if not _SCOPE_RE.match(scope):
            raise ValueError("scope must be title | per_clip | clip:<id> | global")
        return scope

    @field_validator("literal", "description", mode="before")
    @classmethod
    def _text(cls, value: object) -> str | None:
        if value is None:
            return None
        text = _nfc(str(value))
        return text or None

    @field_validator("facts", mode="before")
    @classmethod
    def _facts(cls, value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("facts must be an object")
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            raise ValueError("facts must be JSON serializable") from None
        if len(encoded.encode("utf-8")) > _MAX_FACTS_BYTES:
            raise ValueError(f"facts exceed {_MAX_FACTS_BYTES} bytes")
        if "style_intent" in value:
            facts = {k: v for k, v in value.items() if k != "style_intent"}
            intent = normalize_style_intent(value["style_intent"])
            return {**facts, "style_intent": intent} if intent else facts
        return value

    @model_validator(mode="after")
    def _needs_content(self) -> BriefUpdate:
        content = (self.kind, self.scope, self.literal, self.description)
        if self.operation == "remove":
            if self.target_requirement_id is None:
                raise ValueError("remove needs target_requirement_id")
            if any(value is not None for value in content) or self.facts:
                raise ValueError(
                    "remove carries only operation, target_requirement_id and expected_version"
                )
            return self
        if self.operation == "add":
            if self.target_requirement_id is not None or self.expected_version is not None:
                raise ValueError("add cannot name a target_requirement_id or expected_version")
            if self.kind is None or self.scope is None:
                raise ValueError("add needs kind and scope")
        elif self.target_requirement_id is None:
            raise ValueError("change needs target_requirement_id")
        elif self.kind is None or self.scope is None:
            if all(value is None for value in content) and not self.facts:
                raise ValueError("change needs at least one replacement field")
            return self
        if not self.literal and not self.description:
            raise ValueError("a requirement needs a literal or a description")
        return self


class BriefRequirement(_BriefModel):
    id: str = Field(min_length=1, max_length=24)
    kind: RequirementKind
    scope: str
    literal: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=_MAX_DESCRIPTION_CHARS)
    facts: dict[str, Any] = Field(default_factory=dict)
    source_turn_id: str | None = None
    status: RequirementStatus = "open"
    # KRI-824: who chose this decision, stamped by the server from the creator's own turn
    # (``asset_holds.stamp_authority``); never model or client data. None (every
    # requirement before this field, and every unstamped one) is omitted, so stored briefs
    # and approval digests re-dump byte-identically; the AI default is "no stamp".
    authority: SkipJsonSchema[Authority | None] = Field(
        default=None, exclude_if=lambda value: value is None
    )
    order_replacement: SkipJsonSchema[OrderReplacementProof | None] = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _replacement_scope(self) -> BriefRequirement:
        if self.order_replacement is not None and (
            self.kind != "order"
            or self.scope != "global"
            or self.literal is not None
            or self.id == self.order_replacement.proposal.predecessor_requirement_id
        ):
            raise ValueError("A confirmed replacement belongs to a new global order requirement")
        return self

    @property
    def live(self) -> bool:
        return self.status != "superseded"

    @property
    def is_shot_text(self) -> bool:
        """Exact words the creator dictated for one described shot.

        "1. The bookshop photo: "..." 2. The bowling video: "..."" arrives as one
        per-clip text per shot, each with the words (`literal`) and the shot
        (`description`). A rule that fills every clip has no such pair.
        """
        return bool(
            self.kind == "text" and self.scope == "per_clip" and self.literal and self.description
        )

    @property
    def key(self) -> tuple[str, ...]:
        """A later requirement with the same key supersedes this one.

        Dictated shot texts are keyed by their shot too, so six shots in one
        message all stay live and restating a shot replaces only that shot.
        """
        if self.is_shot_text:
            return (self.kind, self.scope, _shot_key(self.description or ""))
        return (self.kind, self.scope)

    def text(self) -> str:
        """Creator-facing one-liner: the literal when exact, else the description."""
        if self.literal and self.description:
            return f'{self.description} ("{self.literal}")'
        if self.literal:
            return f'"{self.literal}"'
        return self.description or ""


class CreativeBrief(_BriefModel):
    version: int = Field(default=0, ge=0)
    requirements: list[BriefRequirement] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_requirement_ids(self) -> CreativeBrief:
        ids = [req.id for req in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate requirement ids in creative brief")
        return self

    def live(self) -> list[BriefRequirement]:
        return [req for req in self.requirements if req.live]


class BriefUpdateBatchError(ValueError):
    """The model proposed a malformed or unsafe update batch."""


# KRI-805: spellings a planner model writes for one unambiguous change/remove. The contract
# renders ids as "[r2]"; "update" is the list's own name. A synonym counts only beside a target.
_OPERATION_SYNONYMS = {
    "update": "change",
    "replace": "change",
    "edit": "change",
    "modify": "change",
    "delete": "remove",
}
_RENDERED_ID_RE = re.compile(r"\[?\s*[rR](\d{1,6})\s*\]?")  # kri667: server-owned ids, no words
_CONTENT_FIELDS = frozenset({"kind", "scope", "literal", "description", "facts"})
_FIELD_NAME_RE = re.compile(r"[a-z_]{1,32}")  # kri667: error-location key names, no words


def _model_version(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _repair_entry(item: object) -> object:
    """Rewrite only the spellings of one update whose meaning is unambiguous (KRI-805).

    The rendered id becomes the id, an operation synonym naming a target becomes
    change/remove, a change/remove version that is not a number becomes unbound (the caller
    binds the version its prompt showed), and null/empty padding is dropped. Anything else
    reaches validation exactly as the model wrote it.
    """
    if not isinstance(item, dict):
        return item
    entry = dict(item)
    target = entry.get("target_requirement_id")
    if isinstance(target, str) and (match := _RENDERED_ID_RE.fullmatch(target.strip())):
        entry["target_requirement_id"] = f"r{int(match.group(1))}"
    operation = entry.get("operation")
    if isinstance(operation, str) and entry.get("target_requirement_id") is not None:
        entry["operation"] = _OPERATION_SYNONYMS.get(operation.strip().lower(), operation)
    if entry.get("operation") in {"change", "remove"} and "expected_version" in entry:
        entry["expected_version"] = _model_version(entry["expected_version"])
    if "facts" in entry and entry["facts"] is None:
        del entry["facts"]
    # KRI-824: authority is server-owned; a model that writes one is ignored, never trusted.
    entry.pop("authority", None)
    if entry.get("operation") == "remove":
        entry = {
            key: value
            for key, value in entry.items()
            if key not in _CONTENT_FIELDS or value not in (None, "", {})
        }
    return entry


def _entry_problem(exc: ValidationError) -> str:
    """The broken rule, value-free: field names and fixed rule text, never the model's values."""
    problems = []
    for error in exc.errors(include_input=False, include_context=False, include_url=False)[:3]:
        loc = error["loc"][0] if error["loc"] else None
        rule = str(error["msg"]).removeprefix("Value error, ")
        if loc is None:
            problems.append(rule)
        else:
            field = loc if isinstance(loc, str) and _FIELD_NAME_RE.fullmatch(loc) else "a field"
            problems.append(f"{field}: {rule}")
    return "; ".join(problems)


def parse_brief_updates(raw: object) -> list[BriefUpdate]:
    """Strictly parse a complete update batch without silently dropping entries.

    One entry the parser cannot read still refuses the whole batch, and the error names that
    entry and the rule it broke so the model's retry can fix it (KRI-805).
    """
    if not isinstance(raw, list):
        if raw in (None, []):
            return []
        raise BriefUpdateBatchError("brief_updates must be a list")
    if len(raw) > MAX_UPDATES_PER_TURN:
        raise BriefUpdateBatchError(f"brief_updates exceeds {MAX_UPDATES_PER_TURN} entries")
    out: list[BriefUpdate] = []
    for index, item in enumerate(raw):
        try:
            out.append(BriefUpdate.model_validate(_repair_entry(item)))
        except ValidationError as exc:
            raise BriefUpdateBatchError(
                f"brief_updates[{index}] is invalid: {_entry_problem(exc)}"
            ) from None
        except (ValueError, TypeError):
            raise BriefUpdateBatchError(f"brief_updates[{index}] is invalid") from None
    targets = [update.target_requirement_id for update in out if update.operation != "add"]
    if len(targets) != len(set(targets)):
        raise BriefUpdateBatchError("brief_updates changes or removes one requirement twice")
    return out


def _complete_change(target: BriefRequirement, update: BriefUpdate) -> BriefUpdate:
    """KRI-805: a partial change keeps every field it leaves out; a complete one replaces."""
    if update.operation != "change" or (update.kind is not None and update.scope is not None):
        return update
    facts = {**target.facts, **update.facts}
    if "duration_s" in update.facts and "duration_bound" not in update.facts:
        # KRI-847: a new length is exact until the update says it is a ceiling again, so
        # "at most 10 seconds" then "make it 15 seconds" does not become "at most 15".
        facts.pop("duration_bound", None)
    return update.model_copy(
        update={
            "kind": update.kind or target.kind,
            "scope": update.scope or target.scope,
            "literal": target.literal if update.literal is None else update.literal,
            "description": target.description if update.description is None else update.description,
            "facts": facts,
        }
    )


def bind_updates(
    prior: CreativeBrief | None, updates: Iterable[BriefUpdate]
) -> tuple[BriefUpdate, ...]:
    """Bind model-written updates to the brief the model's prompt showed (KRI-611, KRI-805).

    The model owns which requirement a change/remove targets and what it says; the server
    owns the concurrency token. Every change/remove gets the shown brief's version and a
    partial change is completed from its live target. This never re-reads a newer ledger:
    apply_updates and the persist fence still refuse a missing target or stale work.
    """
    shown = prior or CreativeBrief()
    live_by_id = {req.id: req for req in shown.live()}
    bound: list[BriefUpdate] = []
    for update in updates:
        if update.operation == "add":
            bound.append(update)
            continue
        versioned = update.model_copy(update={"expected_version": shown.version})
        target = live_by_id.get(update.target_requirement_id or "")
        bound.append(versioned if target is None else _complete_change(target, versioned))
    return tuple(bound)


def start_fresh_updates(
    prior: CreativeBrief | None, updates: Iterable[BriefUpdate]
) -> tuple[BriefUpdate, ...]:
    """KRI-816: the creator asked to start fresh, so every saved requirement is withdrawn.

    Driven only by the extraction step's typed ``starts_fresh`` declaration (never a word
    match). This turn's own adds stand, and a change this turn already aimed at a saved
    requirement keeps that target (it is not removed twice). Removals are bound to the
    brief the prompt showed, like any other change/remove.
    """
    updates = tuple(updates)
    shown = prior or CreativeBrief()
    targeted = {update.target_requirement_id for update in updates if update.operation != "add"}
    removals = tuple(
        BriefUpdate(
            operation="remove", target_requirement_id=req.id, expected_version=shown.version
        )
        for req in shown.live()
        if req.id not in targeted
    )
    return (*removals, *updates)


def _next_id_number(requirements: Iterable[BriefRequirement]) -> int:
    highest = 0
    for req in requirements:
        match = re.fullmatch(r"r(\d+)", req.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def merge_requirements(
    old: Iterable[BriefRequirement],
    new: Iterable[BriefRequirement],
) -> list[BriefRequirement]:
    """Append independent requirements, retaining stable-ID tombstones."""
    merged = list(old)
    known_ids = {req.id for req in merged}
    for req in new:
        if req.id in known_ids:
            raise BriefUpdateBatchError("duplicate requirement id")
        merged.append(req.model_copy(update={"status": "open"}))
        known_ids.add(req.id)
    return merged


# KRI-522: facts that record HOW the creator wants a requirement done ("starting with the blue
# video", "animated with typewriter", "bottom left", "placeholder"). A later turn that only
# changes the words ("the hook should say ...") restates the requirement without them; the
# creator never withdrew them, so a same-kind, same-scope change keeps them unless it sets them.
# KRI-841: so is the video a Talking edit ends on (`ending_clip`) and whether it keeps its sound.
# KRI-945: and the server's mark on a title literal it copied from an approved wording.
# KRI-1029: and the text's shape ("curved title"), which "make the title just X" restates
# without.
STICKY_FACTS = frozenset(
    {
        "first_clip",
        "last_clip",
        "animation",
        "position",
        "placeholder",
        "ending_clip",
        "keep_audio",
        APPROVED_COPY_FACT,
        TEXT_SHAPE_FACT,
    }
)


def _order_basis_key(facts: Mapping[str, Any]) -> str:
    key = str(facts.get("key") or "").strip().casefold()
    return "capture_time" if key in _CAPTURE_ORDER_KEYS else key


def _replaces_order(target: BriefRequirement, update: BriefUpdate) -> bool:
    """KRI-1112: an order change from one stated basis to another is a new order.

    Leverkusen M3 (13:38): "Use exactly this order: IMG_1028, ... IMG_1043. This replaces my
    earlier order and ending requests." became ``key: exact_sequence`` over
    ``capture_time``, yet v5 kept the old ``first_clip`` / ``last_clip``, which held the
    creator's complete list back. The "start with" / "end on" words were said of the old
    order; the new one keeps only the ends its own change restates. A reworded order on the
    same basis (KRI-522), or the first basis given to a pin-only "start with X", keeps them.
    """
    if target.kind != "order" or update.kind != "order":
        return False
    old, new = _order_basis_key(target.facts), _order_basis_key(update.facts)
    return bool(old) and bool(new) and old != new


def _carry_sticky_facts(target: BriefRequirement, update: BriefUpdate) -> dict[str, Any]:
    facts = update.facts
    if (
        update.kind == target.kind
        and update.scope == target.scope
        and not _replaces_order(target, update)
    ):
        kept = {
            k: v for k, v in target.facts.items() if k in STICKY_FACTS and k not in update.facts
        }
        facts = {**kept, **update.facts} if kept else update.facts
    if any(facts[k] is None for k in STICKY_FACTS & facts.keys()):
        # KRI-1068: a change that sets a sticky fact to null withdraws it ("skip that"
        # about the clip a reply was stuck on), so the fact is gone rather than kept as null.
        facts = {k: v for k, v in facts.items() if not (k in STICKY_FACTS and v is None)}
    mark = facts.get(APPROVED_COPY_FACT)
    if mark is not None and not (isinstance(mark, Mapping) and mark.get("text") == update.literal):
        # KRI-945: the mark belongs to the words it copied; other words are someone else's.
        facts = {k: v for k, v in facts.items() if k != APPROVED_COPY_FACT}
    return facts


def _with_cut_quote(update: BriefUpdate, *sources: str | None) -> str | None:
    """``update``'s description, with a quoted line it cut from the creator's words put back.

    Leverkusen M1 run 2 (thread 1a2189cc): "...the train clip with the announcement "Nächster
    Halt: Leverkusen Mitte"..." was recorded without its quote, so the clip played a moment that
    cut the line (`kept_sound_lines`). Only a sound ask with no literal: its quote names the line
    to keep, and the clip-sound toggle reads facts, never these words. ``sources`` are the
    creator's own message, then (for a change) the description it replaces.
    """
    if update.kind != "audio" or update.literal:
        return update.description
    from app.services.spoken_quotes import restore_cut_quote  # noqa: PLC0415

    for source in sources:
        restored = restore_cut_quote(update.description, source)
        if restored and len(restored) <= _MAX_DESCRIPTION_CHARS:
            return _nfc(restored)
    return update.description


def apply_updates(
    prior: CreativeBrief | None,
    updates: Iterable[BriefUpdate],
    *,
    source_turn_id: str | None,
    creator_text: str | None = None,
    removal_provenance: RemovalProvenance = "creator",
) -> CreativeBrief:
    """Server-assign ids and merge ``updates`` into ``prior`` (version is set on persist).

    ``creator_text`` is the creator's own message for this turn (KRI-824): each new or
    changed requirement's ``authority`` is stamped from it, never from the update.

    ``removal_provenance`` says who removed what ``updates`` remove; each removed requirement
    records it in its facts (``SUPERSEDED_BY_FACT``). Only a server-made waiver passes "server".
    """
    prior = prior or CreativeBrief()
    updates = list(updates)
    live_by_id = {req.id: req for req in prior.live()}
    if any(
        update.operation != "add" and update.expected_version != prior.version for update in updates
    ):
        raise BriefUpdateBatchError("change/remove requires the exact current brief version")
    targets = [update.target_requirement_id for update in updates if update.operation != "add"]
    if len(targets) != len(set(targets)):
        raise BriefUpdateBatchError("conflicting updates target the same requirement")
    for update in updates:
        if update.operation != "add" and update.target_requirement_id not in live_by_id:
            raise BriefUpdateBatchError("target requirement is missing or superseded")
    counter = _next_id_number(prior.requirements)
    incoming: list[BriefRequirement] = []
    changed: dict[str, BriefRequirement] = {}
    removed: set[str] = set()
    for update in updates:
        if update.operation == "remove":
            removed.add(update.target_requirement_id or "")
            continue
        if update.operation == "change":
            target = live_by_id[update.target_requirement_id or ""]
            update = _complete_change(target, update)  # noqa: PLW2901 - same update, completed
            if not update.literal and not update.description:
                raise BriefUpdateBatchError(
                    "a changed requirement needs a literal or a description"
                )
            changed[target.id] = target.model_copy(
                update={
                    "kind": update.kind,
                    "scope": update.scope,
                    "literal": update.literal,
                    "description": _with_cut_quote(update, creator_text, target.description),
                    # KRI-1029: this turn's own shape words win over a carried shape.
                    "facts": with_text_shape(
                        update.kind,
                        update.description,
                        stamp_order_facts(
                            update.kind,
                            update.description,
                            update.literal,
                            _carry_sticky_facts(target, update),
                            req_id=target.id,
                            turn_id=source_turn_id,
                        ),
                    ),
                    "source_turn_id": source_turn_id,
                    "status": "open",
                    "order_replacement": None,
                }
            )
            changed[target.id] = changed[target.id].model_copy(
                update={"authority": stamp_authority(changed[target.id], creator_text, target)}
            )
            continue
        added = BriefRequirement(
            id=f"r{counter}",
            kind=update.kind,
            scope=update.scope,
            literal=update.literal,
            description=_with_cut_quote(update, creator_text),
            facts=with_text_shape(
                update.kind,
                update.description,
                stamp_order_facts(
                    update.kind,
                    update.description,
                    update.literal,
                    update.facts,
                    req_id=f"r{counter}",
                    turn_id=source_turn_id,
                ),
            ),
            source_turn_id=source_turn_id,
        )
        incoming.append(
            added.model_copy(update={"authority": stamp_authority(added, creator_text)})
        )
        counter += 1
    # KRI-994: a later typed "video clips only" ask withdraws an earlier typed photo ask.
    withdrawn = withdrawn_photo_asks(
        prior.live(),
        [*incoming, *changed.values()],
        source_turn_id=source_turn_id,
        same_batch_ids=[*changed, *removed],
    )
    requirements = []
    for req in prior.requirements:
        if req.id in removed and req.live:
            requirements.append(
                req.model_copy(
                    update={
                        "status": "superseded",
                        "facts": {**req.facts, SUPERSEDED_BY_FACT: removal_provenance},
                    }
                )
            )
        elif req.id in changed and req.live:
            requirements.append(changed[req.id])
        elif req.id in withdrawn and req.live:
            requirements.append(req.model_copy(update={"status": "superseded"}))
        else:
            requirements.append(req)
    requirements.extend(incoming)
    return CreativeBrief(version=prior.version, requirements=requirements)


def new_requirements(before: CreativeBrief | None, after: CreativeBrief) -> list[BriefRequirement]:
    """Live additions and changed requirements in ``after``."""
    prior = {req.id: req for req in (before.live() if before else [])}
    return [
        req
        for req in after.live()
        if req.id not in prior
        or req.model_dump(mode="json") != prior[req.id].model_dump(mode="json")
    ]


# --------------------------------------------------------------------------- router


@dataclass(frozen=True)
class CurrentPlanShape:
    """What the router needs to know about the plan a render already carries."""

    has_render: bool
    # A per-clip label bar exists (a single label can be corrected in place).
    has_per_clip_text_lane: bool = False
    # The copilot can also FILL labels for every clip: a legacy lane, or the
    # server-only `label_each_clip` capability (clip facts on the snapshot).
    # None = same as `has_per_clip_text_lane` (callers that never distinguish).
    can_fill_per_clip_text: bool | None = None
    # KRI-219: the editor can reorder/remove clips on this variant (its snapshot
    # advertises the `clip` op family). None/False = today's behaviour: order and
    # select requirements always re-plan.
    can_edit_timeline: bool | None = None
    # KRI-219: the v2 `reorder_clips_by` op exists on this variant AND at least two
    # clips carry a capture-time fact, so "order by when I filmed them" is an
    # editor op, not a planner re-run.
    can_order_by_capture_time: bool = False
    # Independent editor lanes exposed by the current snapshot.  These stay
    # optional for legacy callers that only know about text/timeline.
    can_edit_audio: bool = False
    can_edit_captions: bool = False
    can_edit_global_timing: bool = False
    can_edit_text: bool | None = None


_PER_CLIP_LANE_ROLES = {"shot_label", "clip_label", "per_clip", "label"}

# Whole-edit redo phrases only. "make the title bigger again" / "cut the intro
# again" are ordinary edits and must stay on the editor-op path, so "again" only
# counts right after a whole-edit object or next to prompt/brief/request.
#
# English patterns run on ``_legacy_fold`` output, exactly as before KRI-520.
_REDO_PATTERNS = (
    re.compile(
        r"\b(do|make|create|generate|render|build|try|run)\s+"
        r"(it|this|that|the (video|edit)|everything|all of it)(\s+all)?\s+again\b"
    ),
    re.compile(r"\b(try|start)\s+again\b"),
    re.compile(r"\b(prompt|brief|request|instructions?)\b.{0,20}\bagain\b"),
    re.compile(r"\bagain\b.{0,20}\b(prompt|brief|request|instructions?)\b"),
    re.compile(r"\b(redo|re-do|start over|from scratch)\b"),
    re.compile(r"\bbased on (my|the) (prompt|brief|request|instructions?)\b"),
)
# KRI-520: Turkish patterns run on ``loose_text`` output (case-folded, Turkish letters
# reduced to ASCII), so they are written in that form ("baştan" is "bastan") and
# ASCII-typed Turkish ("hazirla") matches too. Never applied to English messages.
_TR_REDO_PATTERNS = (
    # "baştan" alone is a redo ("baştan yap"); "baştan sona" is start-to-finish.
    re.compile(r"\bbastan\b(?!\s+sona\b)"),
    re.compile(r"\bsifirdan\b"),
)
# "yeniden yap", "tekrar dene", "bir daha oluştur", "tekrar hazırla". A trailing
# -ma/-me is the negative ("tekrar yapma" = don't do it again), so it never counts.
_TR_REDO_AGAIN = re.compile(
    r"\b(?:yeniden|tekrar|bir daha)\s+(?:yap|dene|olustur|hazirla)(?!m[ae])\w*"
)
# The element a nearby word names ("başlığı tekrar yap" = redo the TITLE): a
# redo aimed at one element is an ordinary edit, like "make the title bigger again".
_TR_ELEMENT_STEMS = (
    "baslik",
    "baslig",
    "yazi",
    "altyazi",
    "metin",
    "metn",
    "etiket",
    "muzik",
    "muzig",
    "sarki",
    "klip",
    "klib",
    "renk",
    "reng",
    "font",
    "ses",
    "efekt",
    "gecis",
    "sure",
    "hiz",
    "logo",
    "intro",
    "giris",
    "kapanis",
    "gorsel",
    "resim",
    "resm",
    "foto",
)


_NEGATED_AGAIN = re.compile(r"\b(don'?t|dont|do not|never|stop)\b[^.!?]{0,20}\bagain\b")


def _legacy_fold(message: str) -> str:
    """The English patterns' fold: the shared ``fold_text`` (KRI-781).

    It used to map "I" to dotless "ı", so "DO IT AGAIN" read "do ıt agaın" and an
    uppercase English redo missed every pattern (a KRI-520 regression).
    """
    return fold_text(message or "")


def _fold_for_redo(message: str) -> str:
    """The Turkish patterns' fold: ``loose_text``, so "HAZIRLA" matches "hazirla"."""
    return loose_text(message or "")


def _reads_english(message: str) -> bool:
    """English messages never reach the Turkish patterns: "at", "al", "son" and "once"
    are English words too ("put video 3 at the start")."""
    return detect_chat_language(message) == "en"


def _asks_turkish_redo(text: str) -> bool:
    for match in _TR_REDO_AGAIN.finditer(text):
        before = text[: match.start()].split()[-2:]
        if any(word.startswith(_TR_ELEMENT_STEMS) for word in before):
            continue
        return True
    return False


def wants_full_replan(message: str) -> bool:
    """ "Do it again based on my prompt" is a supported re-plan, not an editor op."""
    english = _NEGATED_AGAIN.sub(" ", _legacy_fold(message))
    if any(pattern.search(english) for pattern in _REDO_PATTERNS):
        return True
    if _reads_english(message):
        return False
    turkish = _fold_for_redo(message)
    return any(pattern.search(turkish) for pattern in _TR_REDO_PATTERNS) or _asks_turkish_redo(
        turkish
    )


def plan_shape_from_editor_snapshot(snapshot: Mapping[str, Any] | None) -> CurrentPlanShape:
    """Derive the router's view of the current plan from the editor snapshot.

    Conservative on purpose: an unknown/absent per-clip lane reads as "no lane",
    which routes to the stronger re-plan path instead of a lossy editor op.
    """
    if not snapshot:
        return CurrentPlanShape(has_render=False)
    bars = snapshot.get("text_bars") or []
    legacy_lane = any(
        isinstance(bar, Mapping) and str(bar.get("role") or "") in _PER_CLIP_LANE_ROLES
        for bar in bars
    )
    # The unified montage writes its per-clip labels as `clip-label-*` bars with
    # role "generative_intro" (KRI-191), so the id prefix is the reliable lane
    # marker. Such a lane supports correcting ONE label (`edit_text`), but filling
    # every clip needs `label_each_clip`, which exists only when the snapshot
    # carries clip facts; without them the editor tool cannot serve "label every
    # clip" and the router must keep re-planning.
    label_bars = any(
        isinstance(bar, Mapping) and str(bar.get("id") or "").startswith("clip-label-")
        for bar in bars
    )
    allowed_families = set(snapshot.get("allowed_op_families") or [])
    can_edit_timeline = "clip" in allowed_families
    return CurrentPlanShape(
        has_render=True,
        has_per_clip_text_lane=legacy_lane or label_bars or _has_seen(snapshot),
        # `seen` (stored clip understanding) lets the copilot write per-clip captions with
        # add_text even where no place/time fact exists for label_each_clip (KRI-219).
        can_fill_per_clip_text=legacy_lane
        or snapshot.get("label_facts") is True
        or _has_seen(snapshot),
        can_edit_timeline=can_edit_timeline,
        can_order_by_capture_time=can_edit_timeline and _timed_slot_count(snapshot) >= 2,
        can_edit_audio=bool({"music", "sfx"} & allowed_families),
        can_edit_captions="caption" in allowed_families,
        can_edit_text=(
            None if "allowed_op_families" not in snapshot else "text" in allowed_families
        ),
        # v2's clip timeline compiler owns duration boundaries; there is no
        # separate public ``trim_output`` family in the snapshot contract.
        can_edit_global_timing=(
            "clip" in allowed_families and snapshot.get("editor_ops_version") == 2
        ),
    )


def _has_seen(snapshot: Mapping[str, Any]) -> bool:
    return any(
        isinstance(slot, Mapping)
        and not slot.get("removed")
        and isinstance(slot.get("seen"), Mapping)
        and slot["seen"].get("text")
        for slot in snapshot.get("slots") or []
    )


def _timed_slot_count(snapshot: Mapping[str, Any]) -> int:
    """Active slots carrying a capture-time fact (v2 snapshots only)."""
    if snapshot.get("editor_ops_version") != 2:
        return 0
    count = 0
    for slot in snapshot.get("slots") or []:
        if not isinstance(slot, Mapping) or slot.get("removed"):
            continue
        facts = slot.get("facts")
        if isinstance(facts, list) and any(
            isinstance(f, Mapping) and f.get("kind") == "capture_time" for f in facts
        ):
            count += 1
    return count


# Positional/explicit clip moves and removals an editor op can express. Semantic
# selection ("only the funniest clips") and basis-driven ordering ("by when I
# filmed them") are NOT matched: those need the planner.
_MOVE_MESSAGE = re.compile(
    r"\b(move|swap|put|place|shift|bring|send|reorder)\b.{0,80}"
    r"\b(start|beginning|begin|first|last|end|before|after|front|back|position|spot|second|third)\b"
)
_REMOVE_MESSAGE = re.compile(
    r"\b(remove|delete|drop|cut|get rid of|take out)\b.{0,20}"
    r"\b(clip|shot|video|segment|scene)\s*(#|no\.?\s*)?\d+\b"
    r"|\b(remove|delete|drop|cut|get rid of|take out)\b.{0,20}\b(the\s+)?"
    r"(first|last|second|third|fourth|fifth)\s+(clip|shot|video|segment|scene)\b"
)

# Turkish: the object and its position come first, the verb last ("2. klibi başa
# al", "son klibi sil"). Written against ``loose_text`` output (ASCII, lower case).
# A verb only counts in its imperative / polite-request forms: "silme", "koyma" or
# "silinmesin" ("don't ...") never match, and the number must be an ordinal ("3.",
# "ilk", "son") so "3 klibi sil" ("delete 3 clips") still goes to the planner.
_TR_VERB_SUFFIX = r"(?:in|sana|sene|elim|alim|yalim|abilir\w*|ir\w*|iver\w*|yabilir\w*)?"
_TR_REMOVE_VERB = rf"(?:(?:sil|cikar|kaldir){_TR_VERB_SUFFIX}|silinsin|cikarilsin|kaldirilsin|at)\b"
_TR_MOVE_VERB = rf"(?:(?:tasi|getir|koy|al|kaydir|gonder|yerlestir){_TR_VERB_SUFFIX}|at|atsana)\b"
# A singular clip noun ("klibi", "klipini", "videoyu"); "klipleri" is a group.
_TR_CLIP_NOUN = r"(?:klib|klip|video|sahne|cekim)(?![a-z]*l[ae]r)[a-z]*"
_TR_ORDINAL = (
    r"(?:ilk|son|sonuncu|birinci|ikinci|ucuncu|dorduncu|besinci|altinci|yedinci|sekizinci"
    r"|dokuzuncu|onuncu|\d{1,3}\s*\.|\d{1,3}\s*'?\s*(?:inci|nci|uncu|ncu)"
    r"|\d{1,3}\s*(?:numarali|nolu))"
)
_TR_REMOVE_MESSAGE = re.compile(
    rf"\b{_TR_ORDINAL}\s+{_TR_CLIP_NOUN}\s+(?:[a-z]+\s+){{0,2}}{_TR_REMOVE_VERB}"
    # "klip 3'ü sil", "video no 4'ü çıkar": the noun first, then its number.
    rf"|\b{_TR_CLIP_NOUN}\s*(?:#|no\.?\s*)?\d{{1,3}}\b\S*\s+(?:[a-z]+\s+){{0,2}}{_TR_REMOVE_VERB}"
)
# "başa al", "sona taşı", "en sona koy", "kafe klibinden önce getir". Every anchor is
# a Turkish-only word (basa, sona, ...), so English text can't collide with it.
_TR_POSITION = (
    r"(?:basa|basina|sona|sonuna|ortaya|ortasina|arkaya|arkasina|onune|oncesine|sonrasina"
    r"|siraya|sirasina|pozisyona"
    r"|[a-z]+(?:den|dan|ten|tan)\s+(?:hemen\s+)?(?:once|sonra))"
)
_TR_MOVE_MESSAGE = re.compile(
    rf"\b{_TR_POSITION}\s+(?:[a-z]+\s+){{0,2}}{_TR_MOVE_VERB}"
    # "ilk ve son klibin yerini değiştir": a swap that names its positions.
    r"|\b(?:ilk|son|ikinci|ucuncu|\d{1,3}\s*\.)\s.{0,60}\byer(?:ini|lerini)?\s+degistir"
    r"(?:in|sene|elim|ebilir\w*|ir\w*)?\b"
)
_POSITIONAL_FACT_KEYS = {
    "index",
    "indices",
    "position",
    "positions",
    "clip_index",
    "clip_number",
    "clip_numbers",
}


def _asks_move(message: str) -> bool:
    """``message`` asks to move a clip somewhere (English as before, or Turkish)."""
    if _MOVE_MESSAGE.search(_legacy_fold(message)):
        return True
    return not _reads_english(message) and bool(_TR_MOVE_MESSAGE.search(_fold_for_redo(message)))


def _asks_remove(message: str) -> bool:
    """``message`` asks to drop one named clip (English as before, or Turkish)."""
    if _REMOVE_MESSAGE.search(_legacy_fold(message)):
        return True
    return not _reads_english(message) and bool(_TR_REMOVE_MESSAGE.search(_fold_for_redo(message)))


def is_select_all_requirement(req: BriefRequirement | BriefUpdate) -> bool:
    """ "Show all content": the extractor marked the whole set (``facts.all`` / ``scope: all``).

    A whole-set statement picks nothing, so it is never a reason to re-plan. The meaning is
    read from the structured fact the extractor records, never from the creator's words
    (KRI-667); a requirement that names a clip, a position or any other fact stays a real
    selection.
    """
    if req.kind != "select" or req.scope != "global":
        return False
    facts = req.facts or {}
    marked = facts.get("all") is True or str(facts.get("scope") or "").casefold() in {
        "all",
        "everything",
    }
    return marked and all(k in {"all", "scope"} for k in facts)


def _structural_reqs_editable(
    reqs: list[BriefRequirement | BriefUpdate], message: str | None
) -> bool:
    """True when every order/select requirement is an explicit clip move/removal."""
    text = message or ""
    for req in reqs:
        if req.kind == "order":
            # A shuffle is one seeded `reorder_clips_by`; "any order" changes nothing.
            if not req.facts.get("key") or _asks_move(text) or order_mode(req) is not None:
                continue
            return False
        if req.kind == "select":
            if (
                is_select_all_requirement(req)
                or req.scope.startswith("clip:")
                or _POSITIONAL_FACT_KEYS & set(req.facts)
                or _asks_remove(text)
            ):
                continue
            return False
    return True


_CAPTURE_ORDER_KEYS = {"capture_time", "chronological", "time"}
_ORDER_ONLY_FACTS = {"key", "by", "direction", "order"}


def is_capture_order_requirement(req: BriefRequirement | BriefUpdate) -> bool:
    """An order requirement that is exactly "by when it was filmed" (no route)."""
    if req.kind != "order":
        return False
    facts = req.facts or {}
    key = str(facts.get("key") or facts.get("by") or "").casefold()
    return key in _CAPTURE_ORDER_KEYS and set(facts) <= _ORDER_ONLY_FACTS


def is_filming_time_label_requirement(req: BriefRequirement | BriefUpdate) -> bool:
    """A per-clip text requirement whose text is the hour each clip was filmed."""
    return wants_filming_time_text(req.kind, req.scope, req.literal, req.description, req.facts)


def _capture_time_ask_editable(
    reqs: list[BriefRequirement | BriefUpdate], current_plan: CurrentPlanShape
) -> bool:
    """True when EVERY requirement is an editor op over server-known capture times.

    "Order them by the time they were filmed and add the hour to each" is a
    `reorder_clips_by` + `label_each_clip` bundle, not a re-plan. Anything else in
    the ask (selection, place labels, audio, timing) keeps the planner.
    """
    if not current_plan.can_order_by_capture_time:
        return False
    can_fill = (
        current_plan.has_per_clip_text_lane
        if current_plan.can_fill_per_clip_text is None
        else current_plan.can_fill_per_clip_text
    )
    has_order = False
    for req in reqs:
        if is_capture_order_requirement(req):
            has_order = True
        elif is_filming_time_label_requirement(req):
            if not can_fill:
                return False
        elif req.kind == "text" and req.scope == "title" and req.literal:
            continue
        elif req.kind == "style":
            continue
        else:
            return False
    return has_order


def route_requirements(
    new_reqs: Iterable[BriefRequirement | BriefUpdate],
    current_plan: CurrentPlanShape | None,
    *,
    message: str | None = None,
    full_replan: bool | None = None,
) -> Route:
    """Deterministic scope router. See the module docstring for the rules.

    ``full_replan=False`` is a semantic extractor verdict and suppresses the
    legacy message regex. ``None`` preserves the historical fallback for callers
    that do not have a semantic verdict yet.
    """
    reqs = list(new_reqs)
    if current_plan is None or not current_plan.has_render:
        return "replan"
    if full_replan is True or (full_replan is None and message and wants_full_replan(message)):
        return "replan"
    if not reqs:
        return "editor_ops"
    from app.kria.freeze_ask import asks_freeze_frame  # noqa: PLC0415 - freeze_ask reads briefs

    if any(req.kind is not None and asks_freeze_frame(req) for req in reqs):  # type: ignore[arg-type]
        # KRI-899: a freeze (or a change to one) is grounded and timed by the planner
        # (`unified_montage._ground_freezes`); the editor copilot has no freeze operation.
        return "replan"
    for req in reqs:
        if (
            compose_unmet_with_ops()
            and req.kind == "text_sequence"
            and current_plan.can_edit_text is False
        ):
            # 29 Ekim: words shown a piece at a time are text ops; a render with no text lane
            # at all is the only one the planner has to rebuild.
            return "replan"
        if req.kind in {"text", "style"} and current_plan.can_edit_text is False:
            # Captions are editable text on a separate lane. Title-specific
            # requests still require the actual title/text capability.
            if req.scope == "title" or not current_plan.can_edit_captions:
                return "replan"
        if req.kind in {"order", "select"}:
            if is_select_all_requirement(req):
                continue
            if is_capture_order_requirement(req) and current_plan.can_order_by_capture_time:
                continue
            if not current_plan.can_edit_timeline or not _structural_reqs_editable([req], message):
                return "replan"
    per_clip_text = any(
        req.kind == "text" and (req.scope == "per_clip" or req.scope.startswith("clip:"))
        for req in reqs
    )
    if per_clip_text and not (
        current_plan.has_per_clip_text_lane or current_plan.can_edit_captions
    ):
        return "replan"
    fills_every_clip = any(req.kind == "text" and req.scope == "per_clip" for req in reqs)
    can_fill = (
        current_plan.has_per_clip_text_lane
        if current_plan.can_fill_per_clip_text is None
        else current_plan.can_fill_per_clip_text
    )
    if fills_every_clip and not (can_fill or current_plan.can_edit_captions):
        return "replan"
    for req in reqs:
        if req.kind == "audio" and not current_plan.can_edit_audio:
            return "replan"
        if (
            req.kind == "timing"
            and req.scope != "title"
            and not current_plan.can_edit_global_timing
        ):
            return "replan"
    return "editor_ops"


# ---------------------------------------------------------------- request rendering


# KRI-594 (PR 1550 review): a select requirement on one clip may leave the medium OUT
# ("remove the bathtub photo", "küvet fotoğrafını çıkar") or only restrict how it looks ("show
# the bathtub photo, no zoom", "zoom olmasın"). Only the first is a leave-out, so a negation
# counts only when it governs the medium, never a treatment of it. When unsure it reads as an
# ask: a kept ask costs a question, a lost one silently drops the photo. Words are matched
# whole, on `loose_text` (lower case, no diacritics) with punctuation as spaces.
_MEDIA_NOUN = re.compile(
    r"photos?|pictures?|pics?|images?|clips?|videos?|shots?|footage|stills?"
    r"|foto\w*|resim\w*|resm\w*|gorsel\w*|kli[pb]\w*|video\w*|cekim\w*|goruntu\w*"
)
_TREATMENT_NOUN = re.compile(
    r"zoom\w*|pan|panning|motion|movement|animations?|effects?|filters?|music|sounds?|audio"
    r"|sfx|text|captions?|subtitles?|titles?|labels?|borders?|frames?|transitions?|crop\w*"
    r"|blur\w*|stickers?|emojis?|fade\w*|shake\w*"
    r"|efekt\w*|filtre\w*|muzik\w*|ses\w*|yazi\w*|altyazi\w*|baslik\w*|etiket\w*"
    r"|gecis\w*|animasyon\w*|hareket\w*|kirpma\w*|bulanik\w*|cerceve\w*|titreme\w*"
)
_MEDIUM_PRONOUNS = frozenset(
    {"it", "this", "that", "them", "these", "those", "bunu", "onu", "sunu", "bunlari", "onlari"}
)
# A removal's object ends at the next conjunction ("skip the intro AND show the photo") or,
# in English, at a preposition ("remove the zoom ON the photo", "the photo OF the bathtub").
_SEGMENT_BREAK = frozenset({"and", "then", "plus", "also", "ve", "sonra", "ardindan"})
_OBJECT_END = frozenset(
    {"of", "on", "in", "at", "from", "with", "for", "during", "to", "into", "onto", "over"}
    | {"under", "after", "before", "while", "so", "instead"}
)
_EN_REMOVE_VERBS = frozenset({"remove", "removing", "delete", "drop", "skip", "exclude", "avoid"})
_EN_NEGATED_VERBS = frozenset({"use", "show", "include", "put", "add", "want", "need", "keep"})
# Turkish puts the object first and the verb last ("küvet fotoğrafını çıkar"). Removal verbs
# leave the medium out unless the nearest noun before them is a treatment ("efekti çıkar");
# the weak ones ("olmasın", "koyma") only with a medium noun there ("fotoğraf olmasın").
_TR_REMOVE_VERB = re.compile(
    r"cikar(?:t)?(?:in|iniz|sin|alim)?|sil(?:in|iniz|sin)?|atla(?:yin|sin)?"
    r"|kaldir(?:in|iniz|sin)?|kullanma(?:yin|yiniz|sin)?|kullanilmasin"
    r"|gosterme(?:yin|sin)?|gosterilmesin"
)
_TR_WEAK_NEGATION = re.compile(
    r"olmasin|istemiyorum|istemiyoruz|yok|koyma(?:yin|sin)?|konmasin|ekleme(?:yin|sin)?"
    r"|eklenmesin"
)
_CLAUSE_BREAK = re.compile(r"[,.;:!?()]+|\b(?:but|however|ama|fakat|ancak)\b")


def _noun_kind(word: str) -> str | None:
    if _TREATMENT_NOUN.fullmatch(word):
        return "treatment"
    if _MEDIA_NOUN.fullmatch(word):
        return "media"
    return None


def _first_noun(words: list[str]) -> str | None:
    return next((kind for kind in map(_noun_kind, words) if kind), None)


def _object_kind(words: list[str]) -> str | None:
    """What an English removal's object is: its head noun, past a possessive "'s"."""

    words = list(words)
    for index, word in enumerate(words):
        if word in _OBJECT_END:
            words = words[:index]
            break
    if "s" in words:  # "the bathtub photo's zoom": the zoom is removed, not the photo
        words = words[len(words) - words[::-1].index("s") :]
    kinds = [kind for kind in map(_noun_kind, words) if kind]
    if kinds:
        return kinds[-1]
    return "media" if words[:1] and words[0] in _MEDIUM_PRONOUNS else None


def _segment_leaves_medium_out(words: list[str]) -> bool:
    for index, word in enumerate(words):
        before, after = words[:index], words[index + 1 :]
        # English: a removal verb leaves the medium out when its own object is the medium
        # ("remove the bathtub photo", "get rid of it") or there is none ("Skip."); never when
        # it is a treatment or something else ("remove the zoom", "skip the intro").
        governed: list[str] | None = None
        if word in _EN_REMOVE_VERBS:
            governed = after
        elif word == "get" and after[:2] == ["rid", "of"]:
            governed = after[2:]
        elif word in ("cut", "take", "leave") and after[:1] == ["out"]:
            governed = after[1:]
        elif word == "leave" and "out" in after[:5]:
            # "leave the bathtub photo out"; a far "out" belongs to another object.
            governed = after[: after.index("out")]
        else:
            negated: list[str] = []
            if (word == "don" and after[:1] == ["t"]) or (word == "do" and after[:1] == ["not"]):
                negated = after[1:]
            elif word in ("dont", "never"):
                negated = after
            if negated[:1] and negated[0] in _EN_NEGATED_VERBS:
                governed = negated[1:]
        if governed is not None and (not governed or _object_kind(governed) == "media"):
            return True
        # A bare "no" / "without" / "never" counts only when it governs the medium:
        # "without the bathtub photo", "no photo here" -- never "no zoom".
        if word in ("no", "without", "never") and _object_kind(after) == "media":
            return True
        # Turkish puts the object first: the nearest noun before the verb is what it removes.
        # "çıkar" is also "comes out", so "fotoğraf ekrana çıkar" stays an ask.
        if _TR_REMOVE_VERB.fullmatch(word) and before[-1:] not in (["ekrana"], ["ekranda"]):
            if not before or before[-1] in _MEDIUM_PRONOUNS or _first_noun(before[::-1]) == "media":
                return True
        if _TR_WEAK_NEGATION.fullmatch(word) and _first_noun(before[::-1]) == "media":
            return True
    return False


def _leaves_medium_out(words: str) -> bool:
    """Whether a requirement's words leave its medium out, rather than ask for it.

    Each clause is split at conjunctions, so a removal only governs its own object: "skip
    the intro and show the bathtub photo" asks for the photo. When unsure it is an ask.
    """

    for clause in _CLAUSE_BREAK.split(loose_text(words)):
        segment: list[str] = []
        for word in [*re.sub(r"[^\w\s]", " ", clause).split(), "and"]:
            if word not in _SEGMENT_BREAK:
                segment.append(word)
                continue
            if segment and _segment_leaves_medium_out(segment):
                return True
            segment = []
    return False


def wants_clip_descriptions(brief: CreativeBrief | None) -> bool:
    """KRI-612: does a live requirement ask Kria to write what each clip shows?

    "explain the footages" / "a summary of the match day event" is described text on each
    clip (no literal): a line per clip read from its footage. Exact words, a placeholder
    and any other scope are something else.
    """
    return brief is not None and any(
        req.kind == "text"
        and req.scope == "per_clip"
        and not req.literal
        and not (req.facts or {}).get("placeholder")
        and asks_clip_description(req.description)
        for req in brief.live()
    )


def requested_media(brief: CreativeBrief | None, media_ids: Iterable[str]) -> dict[str, str]:
    """KRI-594: each medium a live requirement asks to show, with that requirement's words.

    A ``clip:<id>`` scope names the medium by its id, or by a unique tail of it (the brief
    may drop an own clip's "analysis-proxy-ios-" prefix). A select requirement that leaves the
    medium out ("remove the bathtub photo") is the opposite of an ask and never counts; one
    that only restricts how it looks ("show the bathtub photo, no zoom") still does.
    """

    if brief is None:
        return {}
    ids = list(dict.fromkeys(media_ids))
    asked: dict[str, str] = {}
    for req in brief.live():
        if not req.scope.startswith("clip:"):
            continue
        media_id = resolve_clip_scope(req.scope.split(":", 1)[1], ids)
        if media_id is None:
            continue
        words = req.text()
        if req.kind == "select" and _leaves_medium_out(words):
            continue
        asked.setdefault(media_id, words)
    return asked


def render_brief_request(brief: CreativeBrief | None, *, latest_message: str = "") -> str:
    """Render the brief as the strategy's creator request.

    This replaces the chip-concatenated chat string: every live requirement is
    listed verbatim, so a requirement typed after a render can never be lost to
    truncation or chip boilerplate.  This is the persistence-safe full request;
    bounded planning contexts are made by ``brief_context`` below.
    """
    lines: list[str] = []
    if brief is not None:
        for req in brief.live():
            lines.append(_render_requirement(req))
    if not lines:
        return _nfc(latest_message)
    body = (
        f"Creative brief v{brief.version} (everything the creator has asked for, still in force):\n"
        + "\n".join(lines)
    )
    latest = _nfc(latest_message)
    if latest:
        body += f"\nLatest message: {latest}"
    return body


class BriefCoverageError(ValueError):
    """A bounded request cannot represent every applicable requirement intact."""


# Compatibility spelling for callers that distinguish a recoverable supported
# stage limit from invalid model output.
BriefContextOverflow = BriefCoverageError


class BriefCoverage(_BriefModel):
    """Explicit accounting for requirements entering and leaving a context."""

    applicable_ids: tuple[str, ...] = ()
    retrieved_ids: tuple[str, ...] = ()
    enforced_ids: tuple[str, ...] = ()
    unresolved_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_subsets(self) -> BriefCoverage:
        applicable = set(self.applicable_ids)
        for name in ("retrieved_ids", "enforced_ids", "unresolved_ids"):
            values = getattr(self, name)
            if len(values) != len(set(values)) or not set(values) <= applicable:
                raise ValueError(f"{name} must be a unique subset of applicable_ids")
        return self

    @property
    def complete(self) -> bool:
        return not self.unresolved_ids and set(self.applicable_ids) <= (
            set(self.retrieved_ids) | set(self.enforced_ids)
        )

    def mark_retrieved(self, ids: Iterable[str]) -> BriefCoverage:
        seen = set(self.retrieved_ids) | set(ids)
        if not seen <= set(self.applicable_ids):
            raise BriefCoverageError("retrieved ids must be applicable")
        return self.model_copy(update={"retrieved_ids": tuple(sorted(seen))})

    def mark_seen(self, ids: Iterable[str]) -> BriefCoverage:
        return self.mark_retrieved(ids)

    def mark_enforced(self, ids: Iterable[str]) -> BriefCoverage:
        enforced = set(self.enforced_ids) | set(ids)
        if not enforced <= set(self.applicable_ids):
            raise BriefCoverageError("enforced ids must be applicable")
        return self.model_copy(
            update={
                "enforced_ids": tuple(sorted(enforced)),
                "unresolved_ids": tuple(sorted(set(self.applicable_ids) - enforced)),
            }
        )


@dataclass(frozen=True)
class BriefRequestBatch:
    text: str
    requirement_ids: tuple[str, ...]
    latest_message: str


@dataclass(frozen=True)
class BriefContext:
    batches: tuple[BriefRequestBatch, ...]
    coverage: BriefCoverage

    def __iter__(self):
        yield self.batches
        yield self.coverage


def applicable_requirements(
    brief: CreativeBrief | None,
    *,
    stage: str | None = None,
    scope: str | None = None,
    target_clip_id: str | None = None,
) -> list[BriefRequirement]:
    """Return live requirements matching the requested planning slice."""
    if brief is None:
        return []
    result: list[BriefRequirement] = []
    for req in brief.live():
        if scope is not None and req.scope != scope:
            continue
        if target_clip_id is not None and req.scope not in {
            "global",
            "per_clip",
            f"clip:{target_clip_id}",
        }:
            continue
        if stage is not None and req.facts.get("stage") not in {None, stage}:
            continue
        result.append(req)
    return result


def _render_fact(value: Any) -> str:
    """One fact value as the creator wrote it; non-text values keep a compact JSON form.

    KRI-677: ``json.dumps`` rendered a quoted spoken line as ``\\"Hadi gidelim\\" dediğimiz
    iskele klibi``. The Main Creator copied that escaped form into ``source_quote`` and
    the clip-intent planner, which verifies quotes against the creator's own words,
    rejected the ask on every turn after the first.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _render_requirement(req: BriefRequirement) -> str:
    facts = (
        " facts={"
        + "; ".join(f"{key}: {_render_fact(req.facts[key])}" for key in sorted(req.facts))
        + "}"
        if req.facts
        else ""
    )
    return f"- [{req.id}] [{req.kind}/{req.scope}] {req.text()}{facts}"


def batch_brief_requests(
    brief: CreativeBrief | None,
    *,
    latest_message: str = "",
    stage: str | None = None,
    scope: str | None = None,
    target_clip_id: str | None = None,
    max_chars: int = MAX_BRIEF_REQUEST_CHARS,
) -> list[BriefRequestBatch]:
    """Batch whole entries without dropping requirements or latest-message text."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    reqs = applicable_requirements(brief, stage=stage, scope=scope, target_clip_id=target_clip_id)
    latest = _nfc(latest_message)
    version = brief.version if brief is not None else 0
    prefix = f"Creative brief v{version} (everything the creator has asked for, still in force):\n"
    suffix = f"\nLatest message: {latest}" if latest else ""
    if len(prefix) + len(suffix) > max_chars:
        raise BriefCoverageError("latest message cannot fit planning budget")
    batches: list[BriefRequestBatch] = []
    current: list[BriefRequirement] = []
    for req in reqs:
        candidate = prefix + "\n".join(_render_requirement(row) for row in [*current, req]) + suffix
        if len(candidate) <= max_chars:
            current.append(req)
            continue
        if not current:
            raise BriefCoverageError(f"requirement {req.id} cannot fit planning budget")
        batches.append(
            BriefRequestBatch(
                prefix + "\n".join(_render_requirement(row) for row in current) + suffix,
                tuple(row.id for row in current),
                latest,
            )
        )
        current = [req]
        if len(prefix + _render_requirement(req) + suffix) > max_chars:
            raise BriefCoverageError(f"requirement {req.id} cannot fit planning budget")
    if current:
        batches.append(
            BriefRequestBatch(
                prefix + "\n".join(_render_requirement(row) for row in current) + suffix,
                tuple(row.id for row in current),
                latest,
            )
        )
    return batches


def brief_context(
    brief: CreativeBrief | None,
    *,
    latest_message: str = "",
    stage: str | None = None,
    scope: str | None = None,
    target_clip_id: str | None = None,
    max_batches: int = 3,
    max_chars: int = MAX_BRIEF_REQUEST_CHARS,
) -> BriefContext:
    """Build bounded context with explicit initial coverage accounting."""
    if max_batches <= 0:
        raise ValueError("max_batches must be positive")
    reqs = applicable_requirements(brief, stage=stage, scope=scope, target_clip_id=target_clip_id)
    batches = batch_brief_requests(
        brief,
        latest_message=latest_message,
        stage=stage,
        scope=scope,
        target_clip_id=target_clip_id,
        max_chars=max_chars,
    )
    if len(batches) > max_batches:
        raise BriefCoverageError("applicable requirements exceed context batch budget")
    ids = tuple(req.id for req in reqs)
    return BriefContext(
        batches=tuple(batches),
        coverage=BriefCoverage(applicable_ids=ids, unresolved_ids=ids),
    )


def apply_receipt_statuses(
    brief: CreativeBrief, receipts: Iterable[Mapping[str, Any]]
) -> CreativeBrief:
    """Overlay the latest receipt outcome onto each live requirement (read-time view)."""
    by_id = {str(r.get("requirement_id")): str(r.get("status")) for r in receipts}
    updated = [
        req.model_copy(update={"status": by_id[req.id]})
        if req.live and by_id.get(req.id) in {"met", "partial", "not_possible"}
        else req
        for req in brief.requirements
    ]
    return CreativeBrief(version=brief.version, requirements=updated)


# --------------------------------------------------------------------- persistence


def _brief_from_row(row: CreativeBriefVersion | None) -> CreativeBrief | None:
    if row is None:
        return None
    # A corrupt historical row requires recovery. Returning only its valid
    # prefix would silently discard a creator requirement.
    return CreativeBrief.model_validate(
        {"version": int(row.version), "requirements": row.requirements or []}
    )


async def load_latest_brief(db: Any, thread_id: uuid.UUID) -> CreativeBrief | None:
    """Async read of the newest brief version (None when the thread has none)."""
    row = (
        await db.execute(
            select(CreativeBriefVersion)
            .where(CreativeBriefVersion.thread_id == thread_id)
            .order_by(CreativeBriefVersion.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return _brief_from_row(row)


def load_latest_brief_sync(db: Any, thread_id: uuid.UUID) -> CreativeBrief | None:
    row = db.execute(
        select(CreativeBriefVersion)
        .where(CreativeBriefVersion.thread_id == thread_id)
        .order_by(CreativeBriefVersion.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    return _brief_from_row(row)


def _creator_turn_text_sync(db: Any, thread_id: uuid.UUID, turn_id: uuid.UUID) -> str | None:
    """The creator's own message a turn answers (KRI-824), read from the transcript.

    Authority is stamped from this server-held text only. A turn id that is not a creator
    turn of this thread (a system turn, a repair) yields None: nothing is stamped.
    """
    row = db.execute(
        select(CreationThreadEvent.content, CreationThreadEvent.role)
        .join(CreatorAgentTurn, CreatorAgentTurn.source_event_id == CreationThreadEvent.id)
        .where(CreatorAgentTurn.id == turn_id, CreatorAgentTurn.thread_id == thread_id)
        .where(CreationThreadEvent.thread_id == thread_id)
    ).first()
    if row is None or row.role != "user" or not isinstance(row.content, str):
        return None
    return row.content


UNRECORDED_REQUEST_MAX_CHARS = 12_000


def render_unrecorded_request(earlier_lines: Iterable[str], latest_message: str) -> str | None:
    """KRI-814: the creator's earlier lines that never reached a brief version, read with the
    latest message as one request. None when there is no earlier line or it is over the bound.
    """
    lines = [line.strip() for line in earlier_lines if line and line.strip()]
    if not lines:
        return None
    text = (
        "Creative brief v0 (nothing recorded yet). Earlier creator messages that were never "
        "read into the brief; every requirement they state counts as newly stated now:\n"
        + "\n".join(f"- {line}" for line in lines)
        + f"\nLatest message: {latest_message.strip()}"
    )
    return text if len(text) <= UNRECORDED_REQUEST_MAX_CHARS else None


def _delegation_covers_request(
    db: Any, *, thread_id: uuid.UUID, turn_id: uuid.UUID, read_event_ids: Iterable[str] = ()
) -> bool:
    """True only when every creator line up to the turn's source message was read.

    The source message itself was read by the extraction that declared the delegation.
    Every earlier non-empty user line (re-sent copies excluded, as in the pinned full
    request) must be one the server fed to that same extraction (``read_event_ids``,
    recorded by the planner when no brief version existed). A line it did not read keeps
    the readiness hold: a delegation declared now cannot speak for words it never saw.
    """
    turn = db.get(CreatorAgentTurn, turn_id)
    source = db.get(CreationThreadEvent, turn.source_event_id) if turn is not None else None
    if source is None or source.thread_id != thread_id or source.role != "user":
        return False
    earlier = db.execute(
        select(
            CreationThreadEvent.id, CreationThreadEvent.content, CreationThreadEvent.payload
        ).where(
            CreationThreadEvent.thread_id == thread_id,
            CreationThreadEvent.role == "user",
            CreationThreadEvent.sequence < source.sequence,
            CreationThreadEvent.content.is_not(None),
        )
    ).all()
    read = {str(event_id) for event_id in read_event_ids}
    return all(
        str(event_id) in read
        for event_id, content, payload in earlier
        if str(content).strip() and not is_duplicate_answer(payload)
    )


def persist_brief_version_sync(
    db: Any,
    *,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
    updates: Iterable[BriefUpdate],
    record_empty: bool = False,
    delegation_read_event_ids: Iterable[str] = (),
    order_replacement: OrderReplacementWriteContext | None = None,
    removal_provenance: RemovalProvenance = "creator",
) -> CreativeBrief | None:
    """Append one version for ``turn_id``; idempotent per turn.

    Caller holds the thread row lock (the turn-completion transactions do), which
    serializes version numbering. Returns the effective brief, or None when there
    is neither a prior brief nor a new update (nothing to record).

    ``record_empty`` (KRI-814): the extraction step typed that this turn states no
    requirement and delegates every choice. With no prior brief, an empty version is
    written as the evidence that the request was read, so approval readiness does not
    mistake it for a missing extraction. A prior brief is returned unchanged: its live
    requirements stay in force. The declaration only describes the turn's own message,
    so the empty version is written only when the extraction also read every earlier
    creator line (`_delegation_covers_request`, ``delegation_read_event_ids``): a held
    natural first turn recovers in the same chat, while a line the extraction never saw
    keeps the readiness hold.
    """
    updates = list(updates)
    existing = db.execute(
        select(CreativeBriefVersion).where(
            CreativeBriefVersion.thread_id == thread_id,
            CreativeBriefVersion.source_turn_id == turn_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        result = _brief_from_row(existing)
        if order_replacement is not None:
            from app.kria.order_replacement_persistence import check_replacement_retry

            check_replacement_retry(
                result, order_replacement, db=db, thread_id=thread_id, turn_id=turn_id
            )
        return result
    prior = load_latest_brief_sync(db, thread_id)
    if (
        order_replacement is None
        and not updates
        and not (
            record_empty
            and prior is None
            and _delegation_covers_request(
                db,
                thread_id=thread_id,
                turn_id=turn_id,
                read_event_ids=delegation_read_event_ids,
            )
        )
    ):
        return prior
    creator_text = _creator_turn_text_sync(db, thread_id, turn_id)
    if order_replacement is not None:
        from app.kria.order_replacement_persistence import (
            apply_order_replacement,
            validate_persisted_replacement,
        )

        proof = validate_persisted_replacement(
            db, thread_id=thread_id, turn_id=turn_id, prior=prior, context=order_replacement
        )
        merged = apply_order_replacement(
            prior, updates, proof=proof, source_turn_id=str(turn_id), creator_text=creator_text
        )
    else:
        merged = apply_updates(
            prior,
            updates,
            source_turn_id=str(turn_id),
            creator_text=creator_text,
            removal_provenance=removal_provenance,
        )
    next_version = (
        int(
            db.execute(
                select(func.coalesce(func.max(CreativeBriefVersion.version), 0)).where(
                    CreativeBriefVersion.thread_id == thread_id
                )
            ).scalar_one()
        )
        + 1
    )
    db.add(
        CreativeBriefVersion(
            thread_id=thread_id,
            version=next_version,
            requirements=[req.model_dump(mode="json") for req in merged.requirements],
            source_turn_id=turn_id,
        )
    )
    db.flush()
    return CreativeBrief(version=next_version, requirements=merged.requirements)


__all__ = [
    "BriefContextOverflow",
    "BriefCoverage",
    "BriefCoverageError",
    "BriefContext",
    "BriefRequestBatch",
    "BriefRequirement",
    "BriefUpdateBatchError",
    "BriefUpdate",
    "CreativeBrief",
    "CurrentPlanShape",
    "apply_receipt_statuses",
    "apply_updates",
    "applicable_requirements",
    "batch_brief_requests",
    "bind_updates",
    "brief_context",
    "load_latest_brief",
    "render_unrecorded_request",
    "load_latest_brief_sync",
    "merge_requirements",
    "new_requirements",
    "parse_brief_updates",
    "persist_brief_version_sync",
    "plan_shape_from_editor_snapshot",
    "render_brief_request",
    "requested_media",
    "route_requirements",
    "wants_full_replan",
]
