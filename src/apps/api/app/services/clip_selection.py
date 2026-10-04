"""Creator clip-picker selections (KRI-282): contract models, keys, and folding.

The server asks "which clips show X?" as a thumbnail question (``clip_question`` on the
assistant ``question`` event). The app answers with a structured ``clip_selection`` that
is AUTHORITATIVE for the intents it names: the resolver never second-guesses a clip the
creator tapped, and never re-asks an intent the creator said is empty.

Everything is derived from the thread's own events (question payloads + the user events
carrying ``clip_selection``), so selections persist across later turns without extra
state: the full creator request is re-planned every turn and these are re-applied to the
same intents.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.clip_intents import ClipIntent

CLIP_QUESTION_VERSION = 1
MAX_QUESTION_CLIPS = 50  # == MAX_CREATOR_MEDIA_REFS
MAX_QUESTION_CATEGORIES = 8  # a tap-to-answer screen; <= MAX_CLIP_INTENTS
_KEY_MAX = 120
# Token-set similarity needed for the fallback intent<->selection-key match.
_FALLBACK_MIN_SIMILARITY = 0.5
_MEMBERSHIP_OPS = frozenset({"group", "include"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClipSelectionAnswerIn(_Strict):
    key: str = Field(min_length=1, max_length=_KEY_MAX)
    media_ids: list[str] = Field(default_factory=list, max_length=MAX_QUESTION_CLIPS)


class ClipSelectionIn(_Strict):
    """The app's answer to a ``clip_question`` (SubmitTurnBody.clip_selection)."""

    question_id: str = Field(min_length=1, max_length=64)
    answers: list[ClipSelectionAnswerIn] = Field(
        default_factory=list, max_length=MAX_QUESTION_CATEGORIES
    )
    none_keys: list[str] = Field(default_factory=list, max_length=MAX_QUESTION_CATEGORIES)
    skipped: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> ClipSelectionIn:
        keys = [a.key for a in self.answers]
        if len(set(keys)) != len(keys):
            raise ValueError("clip_selection.answers must not repeat a key")
        if any(len(k) > _KEY_MAX or not k for k in self.none_keys):
            raise ValueError("clip_selection.none_keys entries must be 1-120 chars")
        if set(keys) & set(self.none_keys):
            raise ValueError("a category cannot be both answered and marked none")
        for answer in self.answers:
            if len(set(answer.media_ids)) != len(answer.media_ids):
                raise ValueError("clip_selection media_ids must not repeat")
            if any(not m or len(m) > 200 for m in answer.media_ids):
                raise ValueError("clip_selection media_ids entries must be 1-200 chars")
        return self


# ── Keys ──────────────────────────────────────────────────────────────────────


def _words(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text).casefold()
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return [t for t in re.split(r"[^\w]+", plain, flags=re.UNICODE) if t]


def intent_key(intent: ClipIntent) -> str:
    """Stable per-category key: normalised op + attribute (``include:dodgeball``)."""
    slug = "-".join(_words(intent.attribute))
    return f"{intent.op}:{slug}"[:_KEY_MAX]


def _key_attribute_tokens(key: str) -> set[str]:
    _op, _sep, rest = key.partition(":")
    return {t for t in rest.split("-") if t}


def _ops_compatible(a: str, b: str) -> bool:
    return a == b or (a in _MEMBERSHIP_OPS and b in _MEMBERSHIP_OPS)


# ── Question construction ─────────────────────────────────────────────────────


def category_label(intent: ClipIntent) -> str:
    """The creator's own description of WHICH clips (never the text printed on them)."""
    return " ".join(intent.attribute.split()) or "these clips"


def build_clip_question(
    categories: list[dict[str, Any]], *, question_id: str | None = None
) -> dict[str, Any] | None:
    """The additive ``clip_question`` payload; ``None`` when there is nothing to pick from."""
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cat in categories:
        candidates = list(dict.fromkeys(cat["candidate_media_ids"]))[:MAX_QUESTION_CLIPS]
        if cat["key"] in seen or not candidates:
            continue
        seen.add(cat["key"])
        allowed = set(candidates)
        kept.append(
            {
                "key": cat["key"],
                "label": cat["label"][:80],
                "op": cat["op"],
                "candidate_media_ids": candidates,
                "suggested_media_ids": [
                    m for m in dict.fromkeys(cat.get("suggested_media_ids") or []) if m in allowed
                ],
            }
        )
    if not kept:
        return None
    return {
        "version": CLIP_QUESTION_VERSION,
        "question_id": question_id or str(uuid.uuid4()),
        "categories": kept[:MAX_QUESTION_CATEGORIES],
        "allow_none": True,
    }


def clip_question_text(categories: list[dict[str, Any]]) -> str:
    """Self-sufficient human copy for the question (the app also shows thumbnails)."""
    labels = [c["label"] for c in categories]
    if len(labels) == 1:
        return (
            f'I couldn\'t tell which of your clips show "{labels[0]}". '
            "Tap the clips that do, or tell me there aren't any."
        )
    shown = ", ".join(f'"{label}"' for label in labels[:4])
    more = f" and {len(labels) - 4} more" if len(labels) > 4 else ""
    return (
        f"I couldn't tell which of your clips match {shown}{more}. "
        "Tap the clips for each one, or mark the ones that have none."
    )


# ── Folding thread events into authoritative selections ───────────────────────


@dataclass(frozen=True)
class SelectionEntry:
    media_ids: tuple[str, ...] = ()
    none: bool = False


@dataclass(frozen=True)
class ClipSelections:
    by_key: dict[str, SelectionEntry] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_key)

    def match(self, intent: ClipIntent) -> SelectionEntry | None:
        """Exact key first, then token-set similarity on the attribute (op-compatible)."""
        key = intent_key(intent)
        if key in self.by_key:
            return self.by_key[key]
        want = set(_words(intent.attribute))
        if not want:
            return None
        best: tuple[float, str] | None = None
        for candidate in self.by_key:
            op, _sep, _rest = candidate.partition(":")
            if not _ops_compatible(op, intent.op):
                continue
            have = _key_attribute_tokens(candidate)
            if not have:
                continue
            score = len(want & have) / len(want | have)
            if score >= _FALLBACK_MIN_SIMILARITY and (best is None or score > best[0]):
                best = (score, candidate)
        return self.by_key[best[1]] if best else None


def latest_open_clip_question(
    events: Iterable[tuple[str, dict[str, Any] | None]],
) -> dict[str, Any] | None:
    """The latest ``clip_question`` not yet answered by a ``clip_selection``.

    ``events`` are ``(role, payload)`` in chronological order.
    """
    latest: dict[str, Any] | None = None
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("clip_question"), dict):
            latest = payload["clip_question"]
        elif role == "user" and isinstance(payload.get("clip_selection"), dict):
            if latest is not None and payload["clip_selection"].get("question_id") == latest.get(
                "question_id"
            ):
                latest = None
    return latest


def fold_clip_selections(
    events: Iterable[tuple[str, dict[str, Any] | None]],
) -> ClipSelections:
    """Replay question/selection events (chronological) into the standing selections."""
    questions: dict[str, dict[str, Any]] = {}
    by_key: dict[str, SelectionEntry] = {}
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("clip_question"), dict):
            question = payload["clip_question"]
            questions[str(question.get("question_id"))] = question
            continue
        selection = payload.get("clip_selection") if role == "user" else None
        if not isinstance(selection, dict):
            continue
        question = questions.get(str(selection.get("question_id")))
        if question is None:
            continue
        keys = [c.get("key") for c in question.get("categories", []) if c.get("key")]
        if selection.get("skipped"):
            for key in keys:
                by_key[key] = SelectionEntry(none=True)
            continue
        for answer in selection.get("answers", []):
            key = answer.get("key")
            if key in keys:
                ids = tuple(answer.get("media_ids") or ())
                by_key[key] = SelectionEntry(media_ids=ids, none=not ids)
        for key in selection.get("none_keys", []):
            if key in keys:
                by_key[key] = SelectionEntry(none=True)
    return ClipSelections(by_key)
