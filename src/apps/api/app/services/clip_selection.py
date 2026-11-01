"""Creator clip-picker selections (KRI-282): contract models, keys, and folding.

The server asks "which clips show X?" as a thumbnail question (``clip_question`` on the
assistant ``question`` event). The app answers with a structured ``clip_selection`` that
is AUTHORITATIVE for the intents it names: the resolver never second-guesses a clip the
creator tapped, and never re-asks an intent the creator said is empty.

Everything is derived from the thread's own events (question payloads + the user events
carrying ``clip_selection``), so selections persist across later turns without extra
state: the full creator request is re-planned every turn and these are re-applied to the
same intents.

KRI-697: a re-plan rewords the model-written ``attribute`` (and so the key), so each
category also carries the intent's ``intent_id`` and ``creator_text``; an answer stays
bound to those across turns and the newest answer for an intent wins.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from itertools import groupby
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.kria.reply_language import current_reply_language, detect_chat_language, say
from app.kria.text_trim import trim_at_word
from app.schemas.clip_intents import ClipIntent, ClipIntentOp
from app.schemas.story_intents import RejectedClipSelection
from app.text_fold import loose_text

CLIP_QUESTION_VERSION = 1
MAX_QUESTION_CLIPS = 50  # == MAX_CREATOR_MEDIA_REFS
MAX_QUESTION_CATEGORIES = 8  # a tap-to-answer screen; <= MAX_CLIP_INTENTS
_KEY_MAX = 120
# Token-set similarity needed for the fallback intent<->selection-key match.
_FALLBACK_MIN_SIMILARITY = 0.5
# Ops whose picker answers may stand in for each other even through the loose word-overlap
# fallback. KRI-667: never `exclude`, so the clips a creator tapped for "include: laundry"
# can never become the clips an "exclude: laundry" leaves out (an exclude is its own picker
# category, KRI-761).
_MEMBERSHIP_OPS = frozenset({"group", "include"})
# KRI-947: ops whose picker row asks the same "which clips show <attribute>?", so an answer
# settles that attribute under any of them (the re-plan filed the tapped "include: the
# queue" as "order: the queue" and asked again). Exact attribute or identity only, never
# word overlap; never `exclude` (KRI-667) and never `label` (its per-clip text is its own ask).
_SAME_CLIPS_OPS = frozenset({"group", "include", "order", "caption"})
# KRI-697: an `intent_id` binds an answer only with a 4+ letter word outside these generic
# words AND not as one word plus digits ("clip_1", "media2"): a re-plan may reuse those
# positional ids for a different intent. A descriptive id ("caption_rule3_text") binds.
_GENERIC_ID_WORDS = frozenset({*get_args(ClipIntentOp), "intent", "item"})


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
    # KRI-781: the shared Turkish-aware fold ("IŞIK" and "ışık" both key "isik").
    return [t for t in re.split(r"[^\w]+", loose_text(text), flags=re.UNICODE) if t]


def attribute_key(op: str, attribute: str) -> str:
    """The category key for ``op`` and ``attribute`` (``include:dodgeball``)."""
    slug = "-".join(_words(attribute))
    return f"{op}:{slug}"[:_KEY_MAX]


def intent_key(intent: ClipIntent) -> str:
    """Per-category key: normalised op + attribute (``include:dodgeball``).

    Stable within a turn only: a re-plan may reword the attribute (see ``_identities``).
    """
    return attribute_key(intent.op, intent.attribute)


def _key_attribute_tokens(key: str) -> set[str]:
    _op, _sep, rest = key.partition(":")
    return {t for t in rest.split("-") if t}


def _ops_compatible(a: str, b: str) -> bool:
    return a == b or (a in _MEMBERSHIP_OPS and b in _MEMBERSHIP_OPS)


def _shares_answer(a: str, b: str) -> bool:
    return a == b or (a in _SAME_CLIPS_OPS and b in _SAME_CLIPS_OPS)


def _same_attribute(a_key: str, b_key: str) -> bool:
    """Two keys name the same attribute, whatever their ops.

    The ``_KEY_MAX`` cut falls at a different place under each op prefix, so a cut key's
    attribute may be a prefix of the other's.
    """
    a, b = a_key.partition(":")[2], b_key.partition(":")[2]
    if not a or not b:
        return a_key == b_key
    return (
        a == b
        or (len(a_key) >= _KEY_MAX and b.startswith(a))
        or (len(b_key) >= _KEY_MAX and a.startswith(b))
    )


def answered_rows(category: dict[str, Any]) -> list[dict[str, Any]]:
    """A picker row and every row joined behind it (KRI-951): one answer answers them all."""
    hidden = category.get("also_answers") or []
    return [category, *(r for r in hidden if isinstance(r, dict) and r.get("key"))]


def _identities(intent_id: object, creator_text: object) -> list[str]:
    """What a picker answer stays bound to when a later turn rewords the row (KRI-697)."""
    out: list[str] = []
    if isinstance(intent_id, str):
        folded = intent_id.casefold()
        words = ["".join(run) for alpha, run in groupby(folded, str.isalpha) if alpha]
        # One word, then an optional separator and digits: "clip_1", "media2", "slot".
        tail = folded.removeprefix(words[0]) if len(words) == 1 else None
        if tail is not None and tail[:1] in ("_", "-"):
            tail = tail[1:]
        positional = tail is not None and (tail == "" or tail.isdigit())
        if not positional and any(len(w) >= 4 and w not in _GENERIC_ID_WORDS for w in words):
            out.append(f"id:{intent_id}")
    if isinstance(creator_text, str) and (text := "-".join(_words(creator_text))):
        out.append(f"text:{text}")
    return out


# ── Question construction ─────────────────────────────────────────────────────


_LABEL_MAX = 80  # build_clip_question's row-label bound


def category_label(intent: ClipIntent, *, creator_words: str | None = None) -> str:
    """The creator's own description of WHICH clips (never the text printed on them).

    KRI-692: in a Turkish chat a model-written label that clearly reads English ("the moment
    'yirmi dakika' is said") is shown as ``creator_words`` instead, the creator's verbatim
    sentence for this intent. Display only: the intent's ``attribute`` (what the resolver
    searches by and the key an answer binds to) is never rewritten.
    """
    label = " ".join(intent.attribute.split()) or "these clips"
    if creator_words and current_reply_language() == "tr" and detect_chat_language(label) == "en":
        return trim_at_word(" ".join(creator_words.split()), _LABEL_MAX) or label
    return label


def _photo_candidates(
    kept: list[dict[str, Any]], media_kinds: dict[str, str]
) -> list[dict[str, str]]:
    """KRI-693: the question's photo candidates, named in the chat's language.

    A candidate id says nothing about its kind, so the app drew a Visuals photo as
    "Clip 9". Only photos get an entry (video rows are unchanged), numbered in clip
    order among the photos this question actually offers.
    """
    offered = {m for cat in kept for m in cat["candidate_media_ids"]}
    photos = [m for m, kind in media_kinds.items() if kind == "image" and m in offered]
    return [
        {
            "media_id": media_id,
            "kind": "image",
            "label": say(en=f"Photo {n}", tr=f"Fotoğraf {n}"),
        }
        for n, media_id in enumerate(photos, 1)
    ]


def _row_identity(row: dict[str, Any]) -> dict[str, Any]:
    """What an answer must be recorded under for a row joined behind another (KRI-951)."""
    return {
        "key": row["key"],
        "op": row.get("op"),
        **{f: row[f] for f in ("intent_id", "creator_text") if row.get(f)},
    }


def _joins(row: dict[str, Any], cat: dict[str, Any], candidates: list[str]) -> bool:
    return (
        row["op"] in _SAME_CLIPS_OPS
        and cat.get("op") in _SAME_CLIPS_OPS
        and _same_attribute(row["key"], cat["key"])
        and row["candidate_media_ids"] == candidates
    )


def build_clip_question(
    categories: list[dict[str, Any]],
    *,
    question_id: str | None = None,
    media_kinds: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """The additive ``clip_question`` payload; ``None`` when there is nothing to pick from.

    ``media_kinds`` (media id -> ``"video"``/``"image"``, in clip order) lets the payload
    name its photo candidates; without it, or with no photo offered, the payload is unchanged.

    KRI-951: rows about the same attribute under ``_SAME_CLIPS_OPS`` ops ("include: the
    ribeye photo" + "caption: the ribeye photo") that offer the same clips are ONE row; the
    others ride its ``also_answers`` (the app ignores it) and the answer is recorded for
    each. Rows offering different clips stay apart: a tap replaces the clips, so a joined
    answer could drop a clip already found for one of them.
    """
    kept: list[dict[str, Any]] = []
    seen: set[str] = set()
    for cat in categories:
        candidates = list(dict.fromkeys(cat["candidate_media_ids"]))[:MAX_QUESTION_CLIPS]
        if cat["key"] in seen or not candidates:
            continue
        rows = answered_rows(cat)
        seen.update(r["key"] for r in rows)
        allowed = set(candidates)
        suggested = [m for m in dict.fromkeys(cat.get("suggested_media_ids") or []) if m in allowed]
        hidden = [_row_identity(r) for r in rows[1:]]
        same = next((row for row in kept if _joins(row, cat, candidates)), None)
        if same is not None:
            same["also_answers"] = [*same.get("also_answers", []), _row_identity(cat), *hidden]
            same["suggested_media_ids"] = list(
                dict.fromkeys([*same["suggested_media_ids"], *suggested])
            )
            continue
        kept.append(
            {
                "key": cat["key"],
                "label": cat["label"][:80],
                "op": cat["op"],
                # KRI-697: the answer's identity across re-plans (the app ignores these).
                **{f: cat[f] for f in ("intent_id", "creator_text") if cat.get(f)},
                "candidate_media_ids": candidates,
                "suggested_media_ids": suggested,
                **({"also_answers": hidden} if hidden else {}),
            }
        )
    if not kept:
        return None
    kept = kept[:MAX_QUESTION_CATEGORIES]
    photos = _photo_candidates(kept, media_kinds) if media_kinds else []
    return {
        "version": CLIP_QUESTION_VERSION,
        "question_id": question_id or str(uuid.uuid4()),
        "categories": kept,
        "allow_none": True,
        **({"candidate_media": photos} if photos else {}),
    }


def clip_question_text(categories: list[dict[str, Any]], *, only_video: bool = False) -> str:
    """Self-sufficient human copy for the question (the app also shows thumbnails).

    ``only_video`` (KRI-779): every category's one candidate is the project's only video,
    so there is no "which clip" to ask: the question is whether the moment is in it at all.
    KRI-951: each moment is named once, however many rows ask about it.
    """
    labels = _distinct_labels([c["label"] for c in categories])
    if only_video:
        return _not_in_the_video_text(labels)
    if len(labels) == 1:
        return say(
            en=(
                f'I couldn\'t tell which of your clips show "{labels[0]}". '
                "Tap the clips that do, or tell me there aren't any."
            ),
            tr=(
                f'Kliplerinden hangilerinde "{labels[0]}" olduğunu anlayamadım. '
                "Olanlara dokun ya da hiç olmadığını söyle."
            ),
        )
    shown = ", ".join(f'"{label}"' for label in labels[:4])
    more = say(
        en=f" and {len(labels) - 4} more" if len(labels) > 4 else "",
        tr=f" ve {len(labels) - 4} tane daha" if len(labels) > 4 else "",
    )
    return say(
        en=(
            f"I couldn't tell which of your clips match {shown}{more}. "
            "Tap the clips for each one, or mark the ones that have none."
        ),
        tr=(
            f"Şunlar için hangi klipleri kastettiğini anlayamadım: {shown}{more}. "
            "Her biri için klipleri seç ya da hiç klibi olmayanları işaretle."
        ),
    )


def _distinct_labels(labels: list[str]) -> list[str]:
    """First of each label; capitals, spacing and punctuation do not make a new moment."""
    seen: set[str] = set()
    out: list[str] = []
    for label in labels:
        folded = "-".join(_words(label)) or label.strip().casefold()
        if folded not in seen:
            seen.add(folded)
            out.append(label)
    return out


def _not_in_the_video_text(labels: list[str]) -> str:
    if len(labels) == 1:
        return say(
            en=(
                f'About when does "{labels[0]}" appear in your video? '
                "Give a rough time, such as near the start or 30 seconds, "
                "or tell me it isn't there."
            ),
            tr=(
                f'Videonda "{labels[0]}" yaklaşık ne zaman görünüyor? '
                "Başlara doğru veya 30. saniye gibi yaklaşık bir zaman ver ya da olmadığını söyle."
            ),
        )
    shown = ", ".join(f'"{label}"' for label in labels[:4])
    more = say(
        en=f" and {len(labels) - 4} more" if len(labels) > 4 else "",
        tr=f" ve {len(labels) - 4} tane daha" if len(labels) > 4 else "",
    )
    return say(
        en=(
            f"About when do {shown}{more} appear in your video? "
            "Give a rough time for each, or tell me which aren't there."
        ),
        tr=(
            f"Videonda şunlar yaklaşık ne zaman görünüyor: {shown}{more}? "
            "Her biri için yaklaşık bir zaman ver ya da hangilerinin olmadığını söyle."
        ),
    )


# ── Folding thread events into authoritative selections ───────────────────────


@dataclass(frozen=True)
class SelectionEntry:
    media_ids: tuple[str, ...] = ()
    none: bool = False
    # Order of the creator's answer in the thread: the newest answer for an intent wins.
    seq: int = field(default=0, compare=False)
    question_id: str | None = field(default=None, compare=False)
    category_key: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class ClipSelections:
    by_key: dict[str, SelectionEntry] = field(default_factory=dict)
    # ``_identities`` value -> (op the answer was given under, newest answer).
    by_identity: dict[str, tuple[str, SelectionEntry]] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.by_key)

    def rejected(self, intents: Iterable[ClipIntent]) -> tuple[RejectedClipSelection, ...]:
        out = []
        for intent in intents:
            entry = self.match(intent)
            if entry and entry.none and entry.question_id and entry.category_key:
                out.append(
                    RejectedClipSelection(
                        intent=intent.model_copy(deep=True),
                        question_id=entry.question_id,
                        category_key=entry.category_key,
                        selection_sequence=entry.seq,
                    )
                )
        return tuple(out)

    def without(self, intent: ClipIntent) -> ClipSelections:
        """These selections minus every answer that settles ``intent`` (KRI-1125).

        What a typed correction reopens: the beat goes back to the resolver as if nobody had
        tapped it. Answers for other intents stay.
        """
        by_key, by_identity = dict(self.by_key), dict(self.by_identity)
        for _ in range(len(by_key) + len(by_identity)):
            entry = ClipSelections(by_key, by_identity).match(intent)
            if entry is None:
                break
            by_key = {k: v for k, v in by_key.items() if v is not entry}
            by_identity = {k: v for k, v in by_identity.items() if v[1] is not entry}
        return ClipSelections(by_key, by_identity)

    def match(self, intent: ClipIntent) -> SelectionEntry | None:
        """Newest of the same-attribute / identity answers, else token-set similarity.

        Every route is op-compatible. Identity (``intent_id`` / ``creator_text``) keeps an
        answer when a re-plan rewords the attribute ("when I say ..." -> "the clip for
        rule 3"), which token similarity cannot (KRI-697). The same attribute or identity
        also carries an answer to another ``_SAME_CLIPS_OPS`` op (KRI-947); word overlap
        only within ``_MEMBERSHIP_OPS``.
        """
        key = intent_key(intent)
        hits = [
            entry
            for answered, entry in self.by_key.items()
            if answered == key
            or (
                _shares_answer(answered.partition(":")[0], intent.op)
                and _same_attribute(answered, key)
            )
        ]
        for identity in _identities(intent.intent_id, intent.creator_text):
            op, entry = self.by_identity.get(identity, ("", None))
            if entry is not None and _shares_answer(op, intent.op):
                hits.append(entry)
        if hits:
            return max(hits, key=lambda e: e.seq)
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
    by_identity: dict[str, tuple[str, SelectionEntry]] = {}

    def record(category: dict[str, Any], entry: SelectionEntry) -> None:
        for row in answered_rows(category):
            bound = replace(
                entry, question_id=str(question.get("question_id")), category_key=row["key"]
            )
            by_key[row["key"]] = bound
            for identity in _identities(row.get("intent_id"), row.get("creator_text")):
                by_identity[identity] = (str(row.get("op") or ""), bound)

    seq = 0
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
        categories = {
            c["key"]: c
            for c in question.get("categories", [])
            if isinstance(c, dict) and c.get("key")
        }
        seq += 1
        if selection.get("skipped"):
            for category in categories.values():
                record(category, SelectionEntry(none=True, seq=seq))
            continue
        for answer in selection.get("answers", []):
            category = categories.get(answer.get("key"))
            if category is not None:
                ids = tuple(answer.get("media_ids") or ())
                record(category, SelectionEntry(media_ids=ids, none=not ids, seq=seq))
        for key in selection.get("none_keys", []):
            if key in categories:
                record(categories[key], SelectionEntry(none=True, seq=seq))
    return ClipSelections(by_key, by_identity)
