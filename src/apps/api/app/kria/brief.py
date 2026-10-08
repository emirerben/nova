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
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select

from app.kria.brief_route import loose_text, wants_filming_time_text
from app.kria.reply_language import detect_chat_language
from app.models import CreativeBriefVersion

RequirementKind = Literal["text", "order", "select", "timing", "audio", "style"]
RequirementStatus = Literal["open", "met", "partial", "not_possible", "superseded"]
Route = Literal["replan", "editor_ops"]

# Two titles plus six dictated shot texts used all of the old 8, so a duration
# or "no stock images" ask in the same message was cut (KRI-422).
MAX_UPDATES_PER_TURN = 16
# This bounds a single planner response, not the ledger.  A larger model batch
# is rejected as a whole so the caller can recover without losing its tail.
MAX_BRIEF_REQUEST_CHARS = 9_000
_SCOPE_RE = re.compile(r"^(title|per_clip|global|clip:[A-Za-z0-9._:-]{1,100})$")
_MAX_FACTS_BYTES = 2_000


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


class _BriefModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BriefUpdate(_BriefModel):
    """One requirement as proposed by the planner model. Ids/status are server-owned."""

    operation: Literal["add", "change", "remove"] = "add"
    target_requirement_id: str | None = Field(default=None, min_length=1, max_length=24)
    expected_version: int | None = Field(default=None, ge=0)
    kind: RequirementKind | None = None
    scope: str | None = None
    literal: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=300)
    facts: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scope", mode="before")
    @classmethod
    def _scope(cls, value: object) -> str:
        scope = _nfc(str(value or "")).replace(" ", "")
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
        return value

    @model_validator(mode="after")
    def _needs_content(self) -> BriefUpdate:
        if self.operation == "remove":
            if (
                self.target_requirement_id is None
                or self.expected_version is None
                or self.literal is not None
                or self.description is not None
                or self.kind is not None
                or self.scope is not None
                or self.facts
            ):
                raise ValueError("remove needs only target_requirement_id and expected_version")
            return self
        if self.kind is None or self.scope is None:
            raise ValueError("add/change need kind and scope")
        if self.operation == "change" and self.target_requirement_id is None:
            raise ValueError("change needs target_requirement_id")
        if self.operation in {"change", "remove"} and self.expected_version is None:
            raise ValueError("change/remove need expected_version")
        if self.operation == "add" and (
            self.target_requirement_id is not None or self.expected_version is not None
        ):
            raise ValueError("add cannot target a requirement or version")
        if not self.literal and not self.description:
            raise ValueError("a requirement needs a literal or a description")
        return self


class BriefRequirement(_BriefModel):
    id: str = Field(min_length=1, max_length=24)
    kind: RequirementKind
    scope: str
    literal: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=300)
    facts: dict[str, Any] = Field(default_factory=dict)
    source_turn_id: str | None = None
    status: RequirementStatus = "open"

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


def parse_brief_updates(raw: object) -> list[BriefUpdate]:
    """Strictly parse a complete update batch without silently dropping entries."""
    if not isinstance(raw, list):
        if raw in (None, []):
            return []
        raise BriefUpdateBatchError("brief_updates must be a list")
    if len(raw) > MAX_UPDATES_PER_TURN:
        raise BriefUpdateBatchError(f"brief_updates exceeds {MAX_UPDATES_PER_TURN} entries")
    out: list[BriefUpdate] = []
    for item in raw:
        try:
            out.append(BriefUpdate.model_validate(item))
        except (ValidationError, ValueError, TypeError):
            raise BriefUpdateBatchError("brief_updates contains an invalid entry") from None
    targets = [update.target_requirement_id for update in out if update.operation != "add"]
    if len(targets) != len(set(targets)):
        raise BriefUpdateBatchError("brief_updates contains duplicate targets")
    return out


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
STICKY_FACTS = frozenset({"first_clip", "last_clip", "animation", "position", "placeholder"})


def _carry_sticky_facts(target: BriefRequirement, update: BriefUpdate) -> dict[str, Any]:
    if update.kind != target.kind or update.scope != target.scope:
        return update.facts
    kept = {k: v for k, v in target.facts.items() if k in STICKY_FACTS and k not in update.facts}
    return {**kept, **update.facts} if kept else update.facts


def apply_updates(
    prior: CreativeBrief | None,
    updates: Iterable[BriefUpdate],
    *,
    source_turn_id: str | None,
) -> CreativeBrief:
    """Server-assign ids and merge ``updates`` into ``prior`` (version is set on persist)."""
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
            changed[target.id] = target.model_copy(
                update={
                    "kind": update.kind,
                    "scope": update.scope,
                    "literal": update.literal,
                    "description": update.description,
                    "facts": _carry_sticky_facts(target, update),
                    "source_turn_id": source_turn_id,
                    "status": "open",
                }
            )
            continue
        incoming.append(
            BriefRequirement(
                id=f"r{counter}",
                kind=update.kind,
                scope=update.scope,
                literal=update.literal,
                description=update.description,
                facts=update.facts,
                source_turn_id=source_turn_id,
            )
        )
        counter += 1
    requirements = []
    for req in prior.requirements:
        if req.id in removed and req.live:
            requirements.append(req.model_copy(update={"status": "superseded"}))
        elif req.id in changed and req.live:
            requirements.append(changed[req.id])
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
    # Turkish dotted/dotless I: casefold() turns "\u0130" into i + combining dot,
    # which no pattern would match, so map both capitals first. Kept as it was for the
    # English patterns, so English routing is unchanged by KRI-520.
    text = unicodedata.normalize("NFC", message or "").replace("\u0130", "i").replace("I", "\u0131")
    return " ".join(text.casefold().split())


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
    can_edit_timeline = "clip" in (snapshot.get("allowed_op_families") or [])
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


def _structural_reqs_editable(
    reqs: list[BriefRequirement | BriefUpdate], message: str | None
) -> bool:
    """True when every order/select requirement is an explicit clip move/removal."""
    text = message or ""
    for req in reqs:
        if req.kind == "order":
            if not req.facts.get("key") or _asks_move(text):
                continue
            return False
        if req.kind == "select":
            if (
                req.scope.startswith("clip:")
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
) -> Route:
    """Deterministic scope router. See the module docstring for the rules."""
    reqs = list(new_reqs)
    if current_plan is None or not current_plan.has_render:
        return "replan"
    if message and wants_full_replan(message):
        return "replan"
    if not reqs:
        return "editor_ops"
    kinds = {req.kind for req in reqs}
    if _capture_time_ask_editable(reqs, current_plan):
        return "editor_ops"
    if kinds & {"order", "select"}:
        if not (
            current_plan.can_edit_timeline
            and kinds <= {"order", "select"}
            and _structural_reqs_editable(reqs, message)
        ):
            return "replan"
        return "editor_ops"
    title_local_timing = all(req.kind != "timing" or req.scope == "title" for req in reqs)
    mixed_editor_kinds = kinds <= {"text", "style"} or (
        kinds <= {"text", "style", "timing"} and title_local_timing
    )
    if len(kinds) > 1 and not mixed_editor_kinds:
        # Text and style are both in-place label/title tweaks the editor ops express
        # (KRI-219: "move the timestamps top left, make them smaller, just the hour");
        # any other mix (audio, timing, ...) still needs the planner.
        return "replan"
    if "timing" in kinds and not title_local_timing:
        return "replan"
    per_clip_text = any(
        req.kind == "text" and (req.scope == "per_clip" or req.scope.startswith("clip:"))
        for req in reqs
    )
    if per_clip_text and not current_plan.has_per_clip_text_lane:
        return "replan"
    fills_every_clip = any(req.kind == "text" and req.scope == "per_clip" for req in reqs)
    can_fill = (
        current_plan.has_per_clip_text_lane
        if current_plan.can_fill_per_clip_text is None
        else current_plan.can_fill_per_clip_text
    )
    if fills_every_clip and not can_fill:
        return "replan"
    return "editor_ops"


# ---------------------------------------------------------------- request rendering


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


def _render_requirement(req: BriefRequirement) -> str:
    facts = (
        " facts=" + json.dumps(req.facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
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


def persist_brief_version_sync(
    db: Any,
    *,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
    updates: Iterable[BriefUpdate],
) -> CreativeBrief | None:
    """Append one version for ``turn_id``; idempotent per turn.

    Caller holds the thread row lock (the turn-completion transactions do), which
    serializes version numbering. Returns the effective brief, or None when there
    is neither a prior brief nor a new update (nothing to record).
    """
    updates = list(updates)
    existing = db.execute(
        select(CreativeBriefVersion).where(
            CreativeBriefVersion.thread_id == thread_id,
            CreativeBriefVersion.source_turn_id == turn_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _brief_from_row(existing)
    prior = load_latest_brief_sync(db, thread_id)
    if not updates:
        return prior
    merged = apply_updates(prior, updates, source_turn_id=str(turn_id))
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
    "brief_context",
    "load_latest_brief",
    "load_latest_brief_sync",
    "merge_requirements",
    "new_requirements",
    "parse_brief_updates",
    "persist_brief_version_sync",
    "plan_shape_from_editor_snapshot",
    "render_brief_request",
    "route_requirements",
    "wants_full_replan",
]
