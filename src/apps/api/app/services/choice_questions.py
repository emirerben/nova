"""Conflict-choice questions (KRI-282): detect, ask, remember, follow.

When the creator's instructions cannot all be honoured at once (or can be honoured in
more than one reasonable way), the assistant must not silently pick one. It asks ONE
focused question with concrete options (``choice_question`` on the assistant question
event), the app answers with ``choice_selection`` (SubmitTurnBody), and the answer is
replayed from the thread's own events on every later turn, so it is followed and never
asked again. Same event-replay design as ``clip_selection``: no extra state.

Adding a conflict later = build a :class:`ConflictCandidate` (a deterministic detector
here, or one the planner flags) and give its ``conflict_id`` a consumer for the chosen
option key. The first case is ``order_vs_group``: "chronological" together with "group
by sport" when the clips were filmed mixed together.

KRI-476 (PR-C) adds ONE collector, :func:`collect_conflicts`, the only place that turns
an ambiguity into a question. It is route-independent (it reads the strategy, the brief
and the approved media snapshot, never a render route) and returns typed
:class:`UnresolvedChoice` items in a fixed priority order; :func:`resolve_choices`
folds the creator's earlier answers (scoped by ``input_digest``) into server-owned
strategy fields and returns at most the first still-open question.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import math
import re
import unicodedata
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.kria.brief_route import fold_text, loose_text
from app.kria.freeze_ask import times_one_moment
from app.kria.reply_language import current_reply_language, say
from app.services.clip_facts import (
    CAPTURE_ORDER_KEYS,
    capture_from_assignment,
    order_by_capture_time,
)
from app.services.clip_order_sequence import ANCHOR_FACTS

CHOICE_QUESTION_VERSION = 1
MAX_CHOICE_OPTIONS = 6
_NAME_MAX = 40

CONFLICT_ORDER_VS_GROUP = "order_vs_group"
OPT_GROUP_FIRST = "group_first"
OPT_CHRONOLOGICAL = "chronological"
ORDER_VS_GROUP_OPTIONS = (OPT_GROUP_FIRST, OPT_CHRONOLOGICAL)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChoiceSelectionIn(_Strict):
    """The app's answer to a ``choice_question`` (SubmitTurnBody.choice_selection)."""

    question_id: str = Field(min_length=1, max_length=64)
    option_key: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class ConflictOption:
    key: str
    label: str
    description: str | None = None
    recommended: bool = False
    # Short phrases a typed reply may use instead of the label ("extend it"). Matched
    # exactly after normalisation, never fuzzily (``match_open_choice``).
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictCandidate:
    """One detected (or planner-flagged) conflict, ready to be asked."""

    conflict_id: str
    reason: str
    intro: str
    options: tuple[ConflictOption, ...]
    # KRI-476: the collector's kind and the digest of the inputs the question depends on.
    # ``None`` keeps the legacy order-vs-group payload byte-identical.
    kind: str | None = None
    input_digest: str | None = None


def _option(
    key: str,
    *,
    label: tuple[str, str],
    description: tuple[str, str] | None = None,
    recommended: bool = False,
    aliases: Sequence[str] = (),
    tr_aliases: Sequence[str] = (),
) -> ConflictOption:
    """A :class:`ConflictOption` with ``(en, tr)`` copy for the chat's language (KRI-520).

    In a Turkish chat the label is Turkish and the English label stays a typed alias,
    because the app echoes the label text back and old questions replay. ``tr_aliases``
    are typed Turkish answers, accepted in every chat language (an English-bound chat
    can still get a Turkish reply). English chats keep the English label and description.
    """

    en_label, tr_label = label
    shown = say(en=en_label, tr=tr_label)
    extra = (en_label,) if shown != en_label else ()
    return ConflictOption(
        key=key,
        label=shown,
        description=say(en=description[0], tr=description[1]) if description else None,
        recommended=recommended,
        aliases=(*aliases, *tr_aliases, *extra),
    )


# ── Detection: ordering vs grouping ───────────────────────────────────────────

# The only two nouns the planner passes (``planner._ordering_conflict_question``).
_TR_GROUP_NOUNS = {"sport": "spor", "group": "grup"}


def _clean_name(name: str) -> str:
    return " ".join(str(name).split())[:_NAME_MAX]


def _join_names(names: Sequence[str]) -> str:
    shown = [_clean_name(n) for n in names if _clean_name(n)][:3]
    if len(shown) <= 1:
        return "".join(shown)
    return ", ".join(shown[:-1]) + say(en=" and ", tr=" ve ") + shown[-1]


def split_groups(
    ordered_ids: Sequence[str], groups: Sequence[tuple[str, Sequence[str]]]
) -> list[tuple[str, int]]:
    """``(name, stretches)`` for every group that is NOT one contiguous stretch.

    A clip in several groups is ambiguous and belongs to none; clips in no group are
    ignored (they never break a group's stretch). Same rule as the montage receipt, so
    "is there a conflict" and "did the render group them" cannot disagree.
    """
    position = {media_id: i for i, media_id in enumerate(ordered_ids)}
    tally: dict[str, int] = {}
    for _name, members in groups:
        for media_id in set(members):
            tally[media_id] = tally.get(media_id, 0) + 1
    owned = sorted(position[m] for m, n in tally.items() if n == 1 and m in position)
    split: list[tuple[str, int]] = []
    for name, members in groups:
        mine = {position[m] for m in set(members) if tally.get(m) == 1 and m in position}
        stretches, inside = 0, False
        for index in owned:
            if index in mine and not inside:
                stretches += 1
            inside = index in mine
        if stretches > 1:
            split.append((name, stretches))
    return split


def detect_order_vs_group(
    *,
    wants_capture_order: bool,
    groups: Sequence[tuple[str, Sequence[str]]],
    clips: Sequence[tuple[str, datetime | None]],
    noun: str = "group",
) -> ConflictCandidate | None:
    """Chronological order AND >=2 groups that the filming order interleaves.

    ``groups`` are the resolved group intents as ``(name, member media ids)``;
    ``clips`` are ``(media id, capture time)`` in attachment order. No conflict when the
    creator did not ask for filming order, when there is no capture time to order by
    (the montage then keeps attachment order and nothing is traded off), or when the
    clips already form one block per group in capture order.
    """
    if not wants_capture_order:
        return None
    known = {media_id for media_id, _t in clips}
    live = [(n, [m for m in members if m in known]) for n, members in groups]
    live = [(n, members) for n, members in live if n and members]
    if len(live) < 2:
        return None
    times = {media_id: moment for media_id, moment in clips if moment is not None}
    ordering = order_by_capture_time([media_id for media_id, _t in clips], times)
    if ordering.basis != "capture_time":
        return None
    if not split_groups(ordering.ordered_ids, live):
        return None
    names = _join_names([n for n, _m in live])
    word = noun if noun else "group"
    tr_word = _TR_GROUP_NOUNS.get(word, word)
    turkish = current_reply_language() == "tr"
    return ConflictCandidate(
        conflict_id=CONFLICT_ORDER_VS_GROUP,
        reason=say(
            en=f"your {names} clips were filmed mixed together, so I can't do both.",
            tr=(
                f"{names} klipleri birbirine karışık çekilmiş, bu yüzden ikisini birden "
                "yapamıyorum."
            ),
        ),
        intro=say(
            en=f"You asked for a chronological video and for the clips grouped by {word}.",
            tr=(
                f"Hem kronolojik bir video hem de kliplerin {tr_word} bazında "
                "gruplanmasını istedin."
            ),
        ),
        options=(
            _option(
                OPT_GROUP_FIRST,
                label=(
                    f"Group by {word}, chronological inside each {word}",
                    f"Her {tr_word} bir blok olsun; blok içinde kronolojik sırala",
                ),
                description=(
                    f"Each {word} plays as one block; blocks start in the order they first "
                    "appear, and clips inside a block keep the order you filmed them.",
                    f"Her {tr_word} tek bir blok olarak oynar; bloklar ilk göründükleri sırayla "
                    "başlar, blok içindeki klipler çektiğin sırayı korur.",
                ),
                recommended=True,
                tr_aliases=("grupla", "gruplandır", "önce grupla") if turkish else (),
            ),
            _option(
                OPT_CHRONOLOGICAL,
                label=(
                    f"Keep it strictly chronological; {word}s may interleave",
                    f"Tam kronolojik kalsın; {tr_word} klipleri iç içe geçebilir",
                ),
                description=(
                    "Clips play in the exact order you filmed them.",
                    "Klipler tam olarak çektiğin sırayla oynar.",
                ),
                tr_aliases=("kronolojik", "kronolojik kalsın", "tam kronolojik") if turkish else (),
            ),
        ),
    )


# ── Question payload ──────────────────────────────────────────────────────────


def build_choice_question(
    candidate: ConflictCandidate, *, question_id: str | None = None
) -> dict[str, Any]:
    """The additive ``choice_question`` payload for the assistant question event."""
    options = []
    for option in candidate.options[:MAX_CHOICE_OPTIONS]:
        row: dict[str, Any] = {
            "key": option.key,
            "label": option.label,
            "recommended": bool(option.recommended),
        }
        if option.description:
            row["description"] = option.description
        if option.aliases:
            row["aliases"] = list(option.aliases)
        options.append(row)
    payload: dict[str, Any] = {
        "version": CHOICE_QUESTION_VERSION,
        "question_id": question_id or str(uuid.uuid4()),
        # Replay key: which conflict this answers. Old/other clients ignore it.
        "conflict": candidate.conflict_id,
        "options": options,
        "allow_free_text": True,
    }
    # Additive fields (clients ignore unknown keys): the collector kind and the digest
    # that scopes the answer to the exact inputs this question was asked about.
    if candidate.kind:
        payload["kind"] = candidate.kind
    if candidate.input_digest:
        payload["input_digest"] = candidate.input_digest
    return payload


def _after_connector(reason: str) -> str:
    """``reason`` as it reads after a connector word ("Maalesef ..."): a plain capitalised
    first word loses its capital (Turkish I / İ aware). Acronyms, camel-case names, quotes,
    digits and a name with a suffix ("Caro'nun") are the creator's words and stay as written."""

    end = 0
    while end < len(reason) and reason[end].isalnum():
        end += 1
    head, after = reason[:end], reason[end : end + 1]
    if (
        len(head) < 2
        or not head[0].isupper()
        or head[1:] != head[1:].lower()
        or after in ("'", "’")
    ):
        return reason
    return {"I": "ı", "İ": "i"}.get(head[0], head[0].lower()) + reason[1:]


def choice_question_text(candidate: ConflictCandidate) -> str:
    """Self-sufficient plain text (old builds show only this)."""
    options = candidate.options[:MAX_CHOICE_OPTIONS]
    if candidate.kind == CONFLICT_TITLE_TEXT and len(options) == 1:
        # The one option is a way out, not a limit: the typed words are the main path.
        return say(
            en=(
                f"{candidate.intro} Type the words you want and I'll use them exactly, "
                f'or reply "{options[0].label}".'
            ),
            tr=(
                f"{candidate.intro} İstediğin kelimeleri yaz, aynen kullanırım; "
                f'ya da "{options[0].label}" yaz.'
            ),
        )
    if candidate.kind == CONFLICT_VOICE_VS_DURATION and len(options) == 1:
        # Only the length the clips and the voice allow: a statement, never "the most I can do".
        only = options[0]
        return say(
            en=(
                f"{candidate.intro} {candidate.reason} I can make it {only.label}. "
                f'Reply "{only.label}" to go with that, or change your request.'
            ),
            tr=(
                f"{candidate.intro} {candidate.reason} Şunu yapabilirim: {only.label}. "
                f'Bunu istiyorsan "{only.label}" yaz ya da isteğini değiştir.'
            ),
        )
    if len(options) == 1:
        # One way forward is a statement, not a choice.
        only = options[0]
        return say(
            en=(
                f"{candidate.intro} Unfortunately {candidate.reason} The most I can do is: "
                f'{only.label}. Reply "{only.label}" to go with that, or change your request.'
            ),
            tr=(
                f"{candidate.intro} Maalesef {_after_connector(candidate.reason)} "
                "Yapabileceğim en fazla şu: "
                f'{only.label}. Bunu istiyorsan "{only.label}" yaz ya da isteğini değiştir.'
            ),
        )
    if candidate.reason.endswith("?"):
        # The reason is itself the question ("How long should the video be?").
        lines = [f"{candidate.intro} {candidate.reason}"]
    else:
        lines = [
            say(
                en=f"{candidate.intro} Unfortunately {candidate.reason} Which do you prefer?",
                tr=(
                    f"{candidate.intro} Maalesef {_after_connector(candidate.reason)} "
                    "Hangisini tercih edersin?"
                ),
            )
        ]
    lines.extend(_numbered_options((o.label, o.recommended) for o in options))
    return "\n".join(lines)


def _numbered_options(options: Iterable[tuple[str, bool]]) -> list[str]:
    """``1. label (recommended)`` lines and the tap-or-type line that closes the list."""
    lines = [
        f"{index}. {label}{say(en=' (recommended)', tr=' (önerilen)') if recommended else ''}"
        for index, (label, recommended) in enumerate(options, start=1)
    ]
    lines.append(
        say(
            en="Tap an option, or tell me in your own words.",
            tr="Bir seçeneğe dokun ya da kendi sözlerinle anlat.",
        )
    )
    return lines


def reask_choice_question(
    question: Mapping[str, Any], *, refused: str | None = None
) -> dict[str, Any] | None:
    """A stored ``choice_question`` asked again: same conflict, inputs and options, new id.

    KRI-795: a render refused for an unresolved choice re-asks the question the gate already
    asked. Keeping its ``conflict`` and ``input_digest`` is what makes the answer count: the
    next turn's gate replays an answer only for the inputs it computes now. The new
    ``question_id`` keeps an earlier answer from closing it. ``refused`` is the answer the
    refused render carried: it is never the recommendation again, so a delegated "you
    choose" cannot hand the same refusal back (an order refusal recommends dropping the fixed
    order, the way forward that renders). ``None`` for a question with fewer than two
    options (one way forward is not a choice to tap).
    """
    options = [o for o in question.get("options") or [] if isinstance(o, Mapping) and o.get("key")]
    if len(options) < 2 or not question.get("conflict"):
        return None
    reasked = {**copy.deepcopy(dict(question)), "question_id": str(uuid.uuid4())}
    others = [str(o["key"]) for o in options if str(o["key"]) != refused]
    if refused is not None and len(others) < len(options):
        pick = OPT_UNORDERED if OPT_UNORDERED in others else others[0]
        for option in reasked["options"]:
            if isinstance(option, dict):
                option["recommended"] = option.get("key") == pick
    return reasked


def reasked_question_text(question: Mapping[str, Any]) -> str:
    """The question line and the numbered options of a re-asked question (old builds show
    only the text, so the options must be readable without the picker)."""
    options = [
        (str(o.get("label") or o.get("key")), bool(o.get("recommended")))
        for o in question.get("options") or []
        if isinstance(o, Mapping) and o.get("key")
    ][:MAX_CHOICE_OPTIONS]
    return "\n".join(
        [
            say(en="Which do you prefer?", tr="Hangisini tercih edersin?"),
            *_numbered_options(options),
        ]
    )


SIMPLER_VERSION_REASON = "simplification_requires_choice"
# Marks the question a server policy refusal asked (KRI-759), as opposed to the one the
# receipts path asks: only this one carries the fields it offered to leave out.
STRATEGY_REFUSAL_KIND = "strategy_refusal"
_SIMPLER_YES_ALIASES = (
    "evet",
    "yes",
    "yes please",
    "evet lütfen",
    "tamam",
    "evet daha sade sürümü hazırla",
    "evet daha basit sürümü hazırla",
    "daha sade sürümü hazırla",
    "daha sade sürüm",
)


def simpler_version_question(refusal_fields: Sequence[str] | None = None) -> dict[str, Any]:
    """ "Try a different approach" / "Make this simpler version" (exact-match options).

    ``refusal_fields`` is set by the planner when the server's own policy refused the
    strategy (KRI-759): the question then also records which fields the simpler version
    leaves out, and a plain "evet" / "yes" answers it, because that refusal ends in a
    yes/no ("Should I make a simpler version?"). The receipts path asks a two-way
    question where a bare "yes" would be ambiguous, so it passes nothing.
    """
    refusal = refusal_fields is not None
    # The payload carries only the options (the reply text asks the question), but the
    # candidate stays self-sufficient and in the chat's language for `choice_question_text`.
    question = build_choice_question(
        ConflictCandidate(
            conflict_id=SIMPLER_VERSION_REASON,
            intro=say(
                en="This edit cannot carry out that exact combination of requests.",
                tr="Bu düzenleme istediklerinin bu birleşimini aynen yapamıyor.",
            ),
            reason=say(
                en="Should I try a different approach, or make this simpler version?",
                tr="Farklı bir yol mu deneyeyim, yoksa bunun daha basit bir sürümünü mü "
                "hazırlayayım?",
            ),
            kind=STRATEGY_REFUSAL_KIND if refusal else None,
            options=(
                ConflictOption(
                    "different_approach",
                    label=say(en="Try a different approach", tr="Farklı bir yol dene"),
                ),
                ConflictOption(
                    "simpler_version",
                    label=say(en="Make this simpler version", tr="Daha basit sürümü hazırla"),
                    aliases=(
                        "simpler version",
                        "make it more basic",
                        "skip, decide for me",
                        "Make this simpler version",
                        "Daha basit sürümü hazırla",
                        *(_SIMPLER_YES_ALIASES if refusal else ()),
                    ),
                ),
            ),
        )
    )
    if refusal:
        question["dropped_fields"] = sorted(set(refusal_fields or ()))
    return question


# ── Replaying thread events ───────────────────────────────────────────────────

Events = Iterable[tuple[str, "dict[str, Any] | None"]]


def _option_keys(question: Mapping[str, Any]) -> set[str]:
    return {
        str(o.get("key"))
        for o in question.get("options") or []
        if isinstance(o, Mapping) and o.get("key")
    }


# A recovery reply that restates an exhausted question in words keeps it answerable.
KEEP_OPEN_REASON = "unresolved_choice"

# Thread events can carry tags the loaders add (``tag_event``): the producing event type,
# and a user message's text. Untagged rows (older callers, tests) fall back to the payload.
EVENT_TYPE_KEY = "_event_type"
CONTENT_KEY = "_content"

# What each assistant-role event type does to an OPEN choice question. Only a conversational
# reply to a user turn taken AFTER the question may supersede it; everything asynchronous or
# non-conversational must leave it answerable (a render finishing or a memory write used to
# make a tap fail with "no longer open" and burned one of the two allowed asks). Verified
# against the producers in tasks/kria_runtime.py, kria/runtime.py, kria/drafts.py,
# routes/creation_threads.py, routes/creator_agent.py, services/creation_editor_actions.py,
# services/creator_memory_learning.py and tasks/creator_preparation.py.
CLOSES_AFTER_USER_REPLY = "closes_after_user_reply"
NEVER_CLOSES = "never_closes"
QUESTION_EVENT_EFFECT: dict[str, str] = {
    # The planner's / editor copilot's reply to a user turn: a text question, plan or
    # answer that supersedes the open question ("how many clips?" must not turn the next
    # "2" into this question's answer).
    "assistant_response": CLOSES_AFTER_USER_REPLY,
    # A reply that asks something else (voiceover required, dispatch unavailable).
    "assistant_question": CLOSES_AFTER_USER_REPLY,
    # The turn failed; the user's reply was not processed, so the question is still the
    # live ask.
    "assistant_error": NEVER_CLOSES,
    # Asynchronous: an earlier render failed, finished, or was reviewed. Not a reply.
    "assistant_render_failed": NEVER_CLOSES,
    "generation_ready": NEVER_CLOSES,
    "assistant_review": NEVER_CLOSES,
    # Outbox / background workers (creator memory) and thread bookkeeping.
    "memory_updated": NEVER_CLOSES,
    "status_update": NEVER_CLOSES,
    # Format / media prerequisite prompts raised by thread actions, not by a reply.
    "format_prompt": NEVER_CLOSES,
    "media_prompt": NEVER_CLOSES,
    # Draft bookkeeping written by the draft tools themselves.
    "draft_applied": NEVER_CLOSES,
    "draft_undone": NEVER_CLOSES,
}


def tag_event(
    role: str, payload: Mapping[str, Any] | None, event_type: str | None, content: str | None
) -> tuple[str, dict[str, Any]]:
    """``(role, payload)`` plus the event type and (for a user message) its text."""

    tagged: dict[str, Any] = dict(payload or {})
    if event_type:
        tagged[EVENT_TYPE_KEY] = event_type
    if role == "user" and content:
        tagged[CONTENT_KEY] = " ".join(str(content).split())[:400]
    return role, tagged


def _closes_open_question(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get(EVENT_TYPE_KEY)
    if event_type is None:
        # Untagged: only something that looks like a conversational reply may close.
        event_type = "assistant_response" if "turn_value" in payload else None
    return QUESTION_EVENT_EFFECT.get(str(event_type)) == CLOSES_AFTER_USER_REPLY


def _keeps_question_open(payload: Mapping[str, Any]) -> bool:
    coverage = payload.get("brief_coverage")
    return isinstance(coverage, Mapping) and coverage.get("reason") == KEEP_OPEN_REASON


def latest_open_choice_question(events: Events) -> dict[str, Any] | None:
    """The choice question a plain reply or a tap may still answer, else ``None``.

    ``events`` are ``(role, payload)`` in chronological order. A question stays open
    until, and only until:

    * a ``choice_selection`` answers it;
    * a newer ``choice_question`` replaces it;
    * the assistant replies to a user turn taken AFTER it with a text question / plan
      (``QUESTION_EVENT_EFFECT``; asynchronous events never close it), except a recovery
      that restates it in words (``brief_coverage.reason == "unresolved_choice"``);
    * it has been asked ``MAX_ASKS_PER_QUESTION`` times and a user message that is not an
      answer follows.
    """
    last: dict[str, Any] | None = None
    is_open = False
    user_since = False
    asked: dict[tuple[Any, Any], int] = {}
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant":
            question = payload.get("choice_question")
            if isinstance(question, dict):
                key = (question.get("conflict"), question.get("input_digest"))
                asked[key] = asked.get(key, 0) + 1
                last, is_open, user_since = question, True, False
            elif last is not None and _keeps_question_open(payload):
                is_open = True
            elif user_since and _closes_open_question(payload):
                is_open = False
        elif role == "user":
            selection = payload.get("choice_selection")
            if (
                isinstance(selection, dict)
                and last is not None
                and selection.get("question_id") == last.get("question_id")
            ):
                last, is_open = None, False
            elif last is not None and is_open:
                # Any other user turn, including a typed re-answer of an EARLIER question
                # (KRI-794), is a turn taken after this one that did not answer it.
                user_since = True
                key = (last.get("conflict"), last.get("input_digest"))
                if asked.get(key, 0) >= MAX_ASKS_PER_QUESTION:
                    is_open = False
    return last if is_open else None


def fold_choice_answers(events: Events) -> dict[str, str]:
    """``conflict_id -> chosen option key`` replayed from the thread (last answer wins).

    Only a selection that answers a question the server actually asked, with an option
    that question offered, counts.
    """
    questions: dict[str, dict[str, Any]] = {}
    answers: dict[str, str] = {}
    for role, payload in events:
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("choice_question"), dict):
            question = payload["choice_question"]
            questions[str(question.get("question_id"))] = question
            continue
        selection = payload.get("choice_selection") if role == "user" else None
        if not isinstance(selection, dict):
            continue
        question = questions.get(str(selection.get("question_id")))
        conflict = question.get("conflict") if question else None
        if not conflict or selection.get("option_key") not in _option_keys(question):
            continue
        answers[str(conflict)] = str(selection["option_key"])
    return answers


def simpler_version_accepted(events: Events, dropped_fields: Iterable[str]) -> bool:
    """Did THIS turn's answer take the simpler version a policy refusal offered? (KRI-759)

    ``events`` are ``(role, payload)`` in chronological order, the current turn's user
    message last. True only when that message carries a ``choice_selection`` of
    ``simpler_version`` for the question that was open just before it, and that question
    offered to leave out every field the policy is dropping now (only a policy refusal
    records ``dropped_fields``; the receipts path's question has none). Consent is per
    question: an older "yes", the receipts path's question, or a refusal about something
    else never counts.
    """
    rows = [(role, payload) for role, payload in events if isinstance(payload, dict)]
    last_user = next(
        (i for i in range(len(rows) - 1, -1, -1) if rows[i][0] == "user"),
        None,
    )
    if last_user is None:
        return False
    selection = rows[last_user][1].get("choice_selection")
    if not isinstance(selection, dict) or selection.get("option_key") != "simpler_version":
        return False
    question = latest_open_choice_question(rows[:last_user])
    if (
        question is None
        or question.get("question_id") != selection.get("question_id")
        or question.get("conflict") != SIMPLER_VERSION_REASON
    ):
        return False
    offered = {str(name) for name in question.get("dropped_fields") or ()}
    dropping = set(dropped_fields)
    return bool(dropping) and dropping <= offered


# ── The collector (KRI-476 / PR-C) ────────────────────────────────────────────

CONFLICT_DURATION_VS_COUNT = "duration_vs_count"
CONFLICT_ORDER_BASIS = "order_basis"
CONFLICT_TEXT_PLACEMENT = "text_placement"
CONFLICT_TITLE_TEXT = "title_text"
# KRI-479 (voice behind footage; only for a `voice_mode == "continuous"` plan).
CONFLICT_VOICE_VS_DURATION = "voice_vs_duration"
CONFLICT_WHICH_VOICE = "which_voice"
CONFLICT_LENGTH_VS_NATURAL = "length_vs_natural"
CONFLICT_SOURCE_COVERAGE = "source_coverage"
# KRI-849: "if you can't tell what he says, ask me instead of guessing" (see ``_unclear_word``).
CONFLICT_UNCLEAR_WORD = "unclear_word"
OPT_UPDATE_FOOTAGE = "update_footage"
# Fixed priority: an answer can change a later detector's inputs, so exactly one question
# is asked per turn, in this order.
CONFLICT_PRIORITY = (
    CONFLICT_ORDER_BASIS,
    CONFLICT_WHICH_VOICE,
    CONFLICT_TEXT_PLACEMENT,
    CONFLICT_TITLE_TEXT,
    CONFLICT_SOURCE_COVERAGE,
    CONFLICT_DURATION_VS_COUNT,
    CONFLICT_VOICE_VS_DURATION,
    CONFLICT_LENGTH_VS_NATURAL,
    CONFLICT_UNCLEAR_WORD,
)

OPT_EXTEND = "extend"
OPT_FEWER = "fewer"
OPT_ATTACHMENT_ORDER = "attachment_order"
OPT_UNORDERED = "unordered"
OPT_OWN_SEQUENCE = "own_sequence"
OPT_NO_TITLE = "no_title"
OPT_MATCH_VOICE = "match_voice"
OPT_SILENT_TAIL = "silent_tail"
OPT_ACCEPT_NATURAL = "accept_natural"
OPT_TRIM_TO_REQUESTED = "trim_to_requested"
OPT_LEAVE_OUT = "leave_out"
OPT_USE_PREFIX = "use_"

# What an "order I filmed them" requirement means to the contract (brief order keys).
ATTACHMENT_ORDER_KEY = "attachment"

# The most clips a draft names; a hard bound on the evenly-spaced subset.
_MAX_FIT_FRAMES_FPS = 30
# A choice is asked at most this many times for the same inputs: the question and ONE
# re-ask. After that the creator's plan goes through unchanged (never a default for them).
MAX_ASKS_PER_QUESTION = 2


@dataclass(frozen=True)
class ChoiceCapability:
    """What the pipeline can execute, so a question only offers what will happen."""

    # Readable-shot floor; ``None`` = the montage planner's own ``MIN_READABLE_SHOT_S``.
    min_readable_shot_s: float | None = None
    max_duration_s: float = 120.0
    # Whether a creator-typed clip sequence can be received and executed. It cannot
    # today (the tray order is set in the editor, never inside a question).
    creator_sequence_supported: bool = False
    # Whose draft this is. ``title_text`` needs it to know whether the unified phone
    # montage will render the draft (its title receipt is only judged at render time);
    # ``None`` = unknown, so that question is never asked.
    creator_id: object = None
    # KRI-479: the continuous-voice route can actually render for new jobs
    # (`Settings.voice_behind_footage_enabled`). False => no voice question is ever asked.
    voice_route: bool = False


@dataclass(frozen=True)
class UnresolvedChoice:
    """One material, askable ambiguity and everything needed to ask and follow it."""

    kind: str
    conflict_id: str
    field_path: str
    requirement_ids: tuple[str, ...]
    intro: str
    reason: str
    options: tuple[ConflictOption, ...]
    input_digest: str
    # option key -> {"strategy": {...}} rewrites applied when that option is chosen.
    effects: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    # option key -> what the recorded answer carries beyond the option itself (KRI-849: the
    # span an unclear-word answer is for, and the creator's chosen words). Server-owned: it
    # is written from the option the creator tapped, never from model output.
    answer_detail: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def candidate(self) -> ConflictCandidate:
        return ConflictCandidate(
            conflict_id=self.conflict_id,
            reason=self.reason,
            intro=self.intro,
            options=self.options,
            kind=self.kind,
            input_digest=self.input_digest,
        )

    def recommended(self) -> ConflictOption:
        return next((o for o in self.options if o.recommended), self.options[0])


def _as_dict(strategy: Any) -> dict[str, Any]:
    if strategy is None:
        return {}
    if isinstance(strategy, Mapping):
        return dict(strategy)
    dump = getattr(strategy, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json", exclude_none=True))
    return {}


def _digest(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def _norm_text(value: object) -> str:
    # Same normalisation as the render contract's text matching (NFC, one space).
    return " ".join(unicodedata.normalize("NFC", str(value)).split())


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _live(brief: Any) -> list[Any]:
    live = getattr(brief, "live", None)
    return list(live()) if callable(live) else []


def _rows(snapshot: Mapping[str, Any] | None) -> list[Any]:
    return list((snapshot or {}).get("clip_assignments") or [])


def _requested_durations(brief: Any) -> tuple[list[float], list[str], str]:
    """The creator's own lengths: LIVE brief timing requirements only.

    A strategy ``target_duration_s`` is NOT evidence: the Creator model must always emit
    one (it picks a length from the footage when the creator named none), and the server
    marks it ``target_duration_requested`` either way.
    """

    durations: list[float] = []
    requirement_ids: list[str] = []
    quoted = ""
    for req in _live(brief):
        # KRI-777: a freeze's or a moment's seconds are not a length the creator asked for.
        if req.kind != "timing" or times_one_moment(req):
            continue
        value = _number((req.facts or {}).get("duration_s"))
        if value is not None:
            durations.append(value)
            requirement_ids.append(str(req.id))
            quoted = quoted or " ".join(str(req.description or "").split())[:80]
    return durations, requirement_ids, quoted


def _evenly_spaced(ids: Sequence[str], count: int) -> list[str]:
    total = len(ids)
    if count >= total:
        return list(ids)
    if count <= 1:
        return list(ids[:1])
    picks = [round(i * (total - 1) / (count - 1)) for i in range(count)]
    return [ids[i] for i in picks]


def _clean_seconds(value: float) -> float | int:
    return int(value) if float(value).is_integer() else round(value, 1)


def _montage_exempt(strategy: Mapping[str, Any], cap: ChoiceCapability) -> bool:
    """Strategies whose length is not a promise about N flash-cut clips.

    Only a plain montage cuts every clip to ``target_duration_s``. Voiceover, creator-song
    and lip-sync montages are timed by the recording / song, speech and day-vlog timelines
    by their own material (the voiceover and lip-sync planners never read the strategy
    length), and a cadence or exact photo/video timing is the creator's own cut length.
    """

    from app.services.length_policy import natural_length_owner  # noqa: PLC0415

    owner = natural_length_owner(strategy)
    window_owned = owner in {"footage", "voiceover"} or (
        owner == "song" and strategy.get("audio_strategy") == "licensed_music"
    )
    return bool(
        (not window_owned and _continuous_voice(strategy, cap) is None)
        or strategy.get("montage_cadence")
        or strategy.get("mixed_media_timing")
        # A continuous voice (KRI-479) is composed by the voice-behind-footage composer, which
        # cuts the picture clips to the length: unlike a camera-audio excerpt montage, the
        # clips-per-length question applies to it.
        or (strategy.get("montage_audio") and _continuous_voice(strategy, cap) is None)
    )


def _included_ids(strategy: Mapping[str, Any]) -> set[str] | None:
    """Clips the creator's resolved ``include`` intents keep (None = no such intent)."""

    kept: set[str] = set()
    found = False
    for intent in strategy.get("resolved_clip_intents") or []:
        if not isinstance(intent, Mapping) or intent.get("op") != "include":
            continue
        if intent.get("status", "resolved") != "resolved":
            continue
        found = True
        kept |= {
            str(a["media_id"])
            for a in intent.get("assignments") or []
            if isinstance(a, Mapping) and a.get("media_id")
        }
    return kept if found else None


# Typed Turkish answers to the length question; matched in every chat language.
_EXTEND_TR_ALIASES = (
    "uzat",
    "uzatalım",
    "süreyi uzat",
    "daha uzun yap",
    "daha uzun",
    "uzun olsun",
    "videoyu uzat",
    "süreyi artır",
)
_FEWER_TR_ALIASES = (
    "daha az klip",
    "az klip",
    "daha az",
    "kısa kalsın",
    "kısa tut",
    "süreyi koru",
    "süreyi değiştirme",
    "klip sayısını azalt",
    "klipleri azalt",
)


def _duration_vs_count(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    if _montage_exempt(strategy, cap):
        return None
    durations, requirement_ids, quoted = _requested_durations(brief)
    if not durations:
        # Only a length the CREATOR stated is a promise. The model's own pick (and the 24 s
        # default) never asks, however many clips there are.
        return None
    if any(abs(value - durations[0]) > 0.001 for value in durations[1:]):
        return None  # conflicting lengths are the contract's own typed error
    if any(req.kind == "select" for req in _live(brief)) and _included_ids(strategy) is None:
        return None  # the creator chose a subset we cannot count: never guess N
    seconds = durations[0]
    ids = [str(r["media_id"]) for r in rows if isinstance(r, Mapping) and r.get("media_id")]
    if not ids or len(set(ids)) != len(ids):
        return None
    kept = _included_ids(strategy)
    voice_id = _continuous_voice(strategy, cap)
    clip_ids = [m for m in ids if (kept is None or m in kept) and m != voice_id]
    # The selection the draft carries IS the clip set (like `_order_basis`): 8 selected
    # clips of 42 are not 42, and `fewer` must never re-add a clip that was left out.
    chosen = {str(m) for m in strategy.get("selected_media_ids") or []}
    if chosen:
        clip_ids = [m for m in clip_ids if m in chosen]
    if not clip_ids:
        return None
    floor_s = cap.min_readable_shot_s
    if floor_s is None:
        from app.pipeline.best_parts import MIN_INCLUDED_PART_S  # noqa: PLC0415

        floor_s = MIN_INCLUDED_PART_S
    floor_frames = max(1, round(floor_s * _MAX_FIT_FRAMES_FPS))
    count = len(clip_ids)
    if count * floor_frames <= round(seconds * 1.10 * _MAX_FIT_FRAMES_FPS):
        return None
    needed = math.ceil(count * floor_frames / _MAX_FIT_FRAMES_FPS * 10 - 1e-9) / 10
    can_extend = needed <= cap.max_duration_s
    fit = max(1, int(seconds * _MAX_FIT_FRAMES_FPS) // floor_frames)
    subset = _evenly_spaced(clip_ids, fit)
    shown = _clean_seconds(needed)
    asked = _clean_seconds(seconds)
    said = quoted or say(en=f"{asked} seconds", tr=f"{asked} saniye")
    options = [
        _option(
            OPT_FEWER,
            label=(
                f"Keep {asked} seconds with {len(subset)} clips",
                f"{asked} saniyeyi koru, {len(subset)} klip kullan",
            ),
            description=(
                f"Uses {len(subset)} of your {count} clips, spread evenly through your "
                "footage; the others are left out.",
                f"{count} klibinden {len(subset)} tanesini kullanır, çekimlerine eşit "
                "aralıklarla dağıtır; diğerleri dışarıda kalır.",
            ),
            recommended=not can_extend,
            aliases=("fewer", "fewer clips", "use fewer", "use fewer clips", "keep it short"),
            tr_aliases=_FEWER_TR_ALIASES,
        )
    ]
    effects: dict[str, Mapping[str, Any]] = {
        OPT_FEWER: {
            "strategy": {
                # The continuous voice's clip stays selected: it is heard, never counted.
                "selected_media_ids": [*subset, voice_id] if voice_id else subset,
                "media_scope": "selected",
                "target_duration_s": _clean_seconds(seconds),
                "target_duration_requested": True,
                "creator_duration_s": _clean_seconds(seconds),
            }
        }
    }
    if can_extend:
        options.insert(
            0,
            _option(
                OPT_EXTEND,
                label=(f"Extend it to {shown} seconds", f"Süreyi {shown} saniyeye uzat"),
                description=(
                    f"Every clip gets at least {floor_s:g} seconds, so all {count} stay in.",
                    f"Her klip en az {floor_s:g} saniye görünür, böylece {count} klibin "
                    "hepsi kalır.",
                ),
                recommended=True,
                aliases=("extend", "extend it", "longer", "make it longer", "extend the length"),
                tr_aliases=_EXTEND_TR_ALIASES,
            ),
        )
        effects[OPT_EXTEND] = {
            "strategy": {
                "target_duration_s": _clean_seconds(needed),
                "target_duration_requested": True,
                "creator_duration_s": _clean_seconds(needed),
            }
        }
    return UnresolvedChoice(
        kind=CONFLICT_DURATION_VS_COUNT,
        conflict_id=CONFLICT_DURATION_VS_COUNT,
        field_path="target_duration_s",
        requirement_ids=tuple(requirement_ids),
        intro=say(
            en=f'Your brief says "{said}" and this edit uses {count} clips.',
            tr=f'İsteğinde "{said}" yazıyor ama bu düzenlemede {count} klip var.',
        ),
        reason=say(
            en=(
                f"{count} clips can't each stay on screen long enough to be seen in {asked} "
                f"seconds (each needs about {floor_s:g} seconds)."
            ),
            tr=(
                f"{asked} saniyede {count} klibin her biri görülecek kadar ekranda kalamaz "
                f"(her birine yaklaşık {floor_s:g} saniye lazım)."
            ),
        ),
        options=tuple(options),
        # The creator's inputs only: the model's own selection or length never reopens it.
        input_digest=_digest(CONFLICT_DURATION_VS_COUNT, sorted(ids), sorted(clip_ids), seconds),
        effects=effects,
    )


_ATTACHMENT_ALIASES = (
    "upload order",
    "order i added them",
    "the order i added them",
    "order i uploaded them",
    "attachment order",
    "as uploaded",
    "as added",
)
# Typed Turkish answers; matched in every chat language. "çektiğim sıra" / "çekim sırası"
# ("the order I filmed") are deliberately absent: that restates the order this question
# says can't be checked, it does not accept the order the clips were added.
_ATTACHMENT_TR_ALIASES = (
    "eklediğim sıra",
    "eklediğim sırayla",
    "yüklediğim sıra",
    "yüklediğim sırayla",
    "ekleme sırası",
    "yükleme sırası",
    "eklenme sırası",
    "ekleme sırasına göre",
    "eklediğim gibi",
    "yüklediğim gibi",
)
_UNORDERED_TR_ALIASES = (
    "sırasız",
    "sırasız devam et",
    "sıra olmasın",
    "sıra yok",
    "sıra olmadan",
    "sıra olmadan devam et",
    "sabit sıra olmasın",
    "sabit sıra yok",
    "sırayı atla",
    "sıra önemli değil",
    "fark etmez",
)


def _placed_sequence(strategy: Mapping[str, Any]) -> bool:
    """Did the server already place the creator's described sequence ("start with X")?"""

    return any(
        isinstance(i, Mapping)
        and i.get("op") == "order"
        and i.get("status", "resolved") == "resolved"
        and i.get("assignments")
        for i in strategy.get("resolved_clip_intents") or []
    )


def pin_anchors(req: Any) -> dict[str, str]:
    """The first / last clips an order requirement names in the creator's own words."""

    facts = getattr(req, "facts", None) or {}
    out: dict[str, str] = {}
    for fact, position in ANCHOR_FACTS:
        words = facts.get(fact)
        if isinstance(words, str) and _norm_text(words):
            out[position] = _norm_text(words)
    return out


def is_pin_only_order(req: Any) -> bool:
    """KRI-762: "this clip first, that clip last" and nothing else about the order.

    Such a requirement is two clip PINS, not an ordering rule the contract cannot verify:
    the clip-intent resolver places them or raises the clip picker, so the basis question
    ("upload order or no fixed order?") has nothing to add. A rule fact (`by` / `direction`
    / `order`) or a capture-time / upload-order key makes it a real ordering ask again.
    """

    facts = getattr(req, "facts", None) or {}
    if not pin_anchors(req) or any(facts.get(k) for k in ("by", "direction", "order")):
        return False
    key = str(facts.get("key") or "").casefold()
    return key not in CAPTURE_ORDER_KEYS and key != ATTACHMENT_ORDER_KEY


def one_clip_edit(rows: Sequence[Any]) -> bool:
    """KRI-661 / KRI-789: the edit is exactly one clip, so it has no order to promise.

    ``rows`` are the edit's clip rows as the caller narrowed them (selected media; the render
    contract also drops a hidden voice clip). The order_basis question and the render
    contract both read this, so the gate never asks about an order the contract would not
    check and the contract never leaves unverified an order the gate stayed quiet about.
    Unknown rows (no approved media snapshot) are never one clip.
    """

    return len(rows) == 1 and isinstance(rows[0], Mapping) and bool(rows[0].get("media_id"))


def _pin_intents(strategy: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The creator's first / last clip asks, as the server read them (any resolution)."""

    return [
        i
        for i in strategy.get("resolved_clip_intents") or []
        if isinstance(i, Mapping)
        and i.get("op") == "order"
        and i.get("position") in ("first", "last")
        and not i.get("order_by")
        and not i.get("placeholder")
    ]


def _placed_pin_positions(strategy: Mapping[str, Any]) -> set[str]:
    """Positions ("first" / "last") whose clip the server actually found."""

    return {
        str(i["position"])
        for i in _pin_intents(strategy)
        if i.get("status", "resolved") == "resolved" and i.get("assignments")
    }


def _named_pin_positions(strategy: Mapping[str, Any], brief: Any) -> set[str]:
    """Positions the creator named a clip for, in the brief or as a clip intent."""

    named = {str(i["position"]) for i in _pin_intents(strategy)}
    for req in _live(brief):
        if req.kind == "order":
            named |= set(pin_anchors(req))
    return named


def _order_basis(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    capture_ids: list[str] = []
    rule_ids: list[str] = []
    rule_text: list[str] = []
    for req in _live(brief):
        if req.kind != "order":
            continue
        key = str((req.facts or {}).get("key") or "").casefold()
        if key in CAPTURE_ORDER_KEYS:
            capture_ids.append(str(req.id))
        elif key != ATTACHMENT_ORDER_KEY and not is_pin_only_order(req):
            # A key-less or non-capture rule ("clips 1, 2, 3 in that sequence", "along my
            # route") the contract cannot verify from the approved media. First / last
            # pins are not one (KRI-762): the clip-intent resolver owns them.
            rule_ids.append(str(req.id))
            rule_text.append(_norm_text(req.description or ""))
    wants_capture = strategy.get("ordering_choice") == "chronological" or bool(capture_ids)
    selected = {str(m) for m in strategy.get("selected_media_ids") or []}
    if selected:
        rows = [r for r in rows if isinstance(r, Mapping) and r.get("media_id") in selected]
    # Exactly the contract's own "can I resolve the order" shape, so a capture question is
    # asked only for the one thing a creator can fix: clips with no capture time.
    valid = bool(rows) and all(isinstance(r, Mapping) and r.get("media_id") for r in rows)
    ids = [str(r["media_id"]) for r in rows] if valid else []
    if valid and (len(set(ids)) != len(ids) or (selected and set(ids) != selected)):
        valid, ids = False, []
    missing = []
    # KRI-661: one clip has no order to promise (a talk-to-camera recording with photo
    # Visuals), so a missing filming time on it is not a question worth asking. KRI-789: nor
    # is an ordering rule ("start at '…' and end right after '…'" inside the one take).
    one_clip = one_clip_edit(rows)
    if valid and wants_capture and not one_clip:
        for row in rows:
            capture = capture_from_assignment(row)
            if not (capture and capture.capture_time):
                missing.append(str(row["media_id"]))
    ask_capture = bool(missing)
    ask_rule = bool(rule_ids) and not _placed_sequence(strategy) and not one_clip
    if not (ask_capture or ask_rule):
        return None
    if ask_capture:
        many = len(ids)
        some = (
            say(
                en="none of your clips have a filming time",
                tr="hiçbir klibinde çekim saati yok",
            )
            if len(missing) == many
            else say(
                en=f"{len(missing)} of your {many} clips have no filming time",
                tr=f"{many} klibinden {len(missing)} tanesinde çekim saati yok",
            )
        )
        filmed = strategy.get("ordering_choice") == "chronological" or any(
            str((req.facts or {}).get("key") or "").casefold() in ("capture_time", "chronological")
            for req in _live(brief)
            if req.kind == "order"
        )
        if filmed:
            intro = say(
                en="You asked for the clips in the order you filmed them.",
                tr="Klipleri çektiğin sırayla istedin.",
            )
        else:
            intro = say(
                en=(
                    "You asked for the clips in a specific order that follows when they "
                    "were filmed."
                ),
                tr="Klipleri çekildikleri zamana göre belirli bir sırayla istedin.",
            )
        reason = say(
            en=f"{some}, so I can't put them in that order.",
            tr=f"{some}, bu yüzden onları o sıraya koyamıyorum.",
        )
        covered = capture_ids + (rule_ids if ask_rule else [])
    else:
        intro = say(
            en="You gave me a specific order for the clips.",
            tr="Klipler için belirli bir sıra verdin.",
        )
        reason = say(
            en="I can't check that order against your clips, so I can't promise it.",
            tr="Bu sırayı kliplerinle karşılaştırıp doğrulayamıyorum, bu yüzden söz veremem.",
        )
        covered = rule_ids
    # KRI-762: upload order is never the recommended answer against a first / last clip the
    # creator named and the server has not placed (it would open or close on the wrong clip).
    # Placed pins are seated on top of any basis, so they do not argue with it.
    pins_unplaced = bool(_named_pin_positions(strategy, brief) - _placed_pin_positions(strategy))
    options = [
        _option(
            OPT_ATTACHMENT_ORDER,
            label=("Use the order you added the clips", "Klipleri eklediğin sırayı kullan"),
            description=(
                "The clips play in the order they were added to this project.",
                "Klipler projeye eklendikleri sırayla oynar.",
            ),
            recommended=not pins_unplaced,
            aliases=_ATTACHMENT_ALIASES,
            tr_aliases=_ATTACHMENT_TR_ALIASES,
        )
    ]
    if cap.creator_sequence_supported:
        options.append(
            _option(
                OPT_OWN_SEQUENCE,
                label=("Use the order I give you", "Sırayı ben vereyim"),
                description=(
                    "You tell me the order of the clips.",
                    "Klip sırasını sen söylersin.",
                ),
                aliases=("my own order", "my own sequence"),
                tr_aliases=("kendi sıram", "kendi sıramı veririm", "sırayı ben vereyim"),
            )
        )
    options.append(
        _option(
            OPT_UNORDERED,
            label=("Continue without a fixed order", "Sabit bir sıra olmadan devam et"),
            description=(
                "I won't promise or check a particular order; the clips still play in a "
                "sensible sequence.",
                "Belirli bir sıra vaat etmem ya da kontrol etmem; klipler yine de "
                "mantıklı bir sırayla oynar.",
            ),
            recommended=pins_unplaced,
            aliases=(
                "without order",
                "no order",
                "skip the order",
                "continue without order",
                "no fixed order",
            ),
            tr_aliases=_UNORDERED_TR_ALIASES,
        )
    )
    return UnresolvedChoice(
        kind=CONFLICT_ORDER_BASIS,
        conflict_id=CONFLICT_ORDER_BASIS,
        field_path="ordering_choice",
        requirement_ids=tuple(covered),
        intro=intro,
        reason=reason,
        options=tuple(options),
        input_digest=_digest(
            CONFLICT_ORDER_BASIS,
            sorted(ids),
            sorted(missing),
            sorted(rule_text) if ask_rule else [],
        ),
    )


def _text_placement(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> list[UnresolvedChoice]:
    labels = [str(t) for t in strategy.get("shot_labels") or []]
    found: list[UnresolvedChoice] = []
    for req in _live(brief):
        if not getattr(req, "is_shot_text", False):
            continue
        matching = [
            i for i, text in enumerate(labels) if _norm_text(text) == _norm_text(req.literal)
        ]
        if len(matching) < 2:
            # One match is placed; none is a planner miss (a repair, never a question).
            continue
        picked = matching[:MAX_CHOICE_OPTIONS]
        options = tuple(
            _option(
                f"shot_{i + 1}",
                label=(f"On shot {i + 1}", f"{i + 1}. sahnede"),
                description=(
                    f"This line belongs on shot {i + 1} of the draft.",
                    f"Bu yazı taslağın {i + 1}. sahnesine ait.",
                ),
                recommended=n == 0,
                aliases=(f"shot {i + 1}", f"shot number {i + 1}"),
                tr_aliases=(
                    f"{i + 1}. sahne",
                    f"sahne {i + 1}",
                    f"{i + 1} numaralı sahne",
                    f"sahne numarası {i + 1}",
                    f"{i + 1}. sahnede",
                ),
            )
            for n, i in enumerate(picked)
        )
        found.append(
            UnresolvedChoice(
                kind=CONFLICT_TEXT_PLACEMENT,
                conflict_id=f"{CONFLICT_TEXT_PLACEMENT}:{req.id}",
                field_path="shot_labels[]",
                requirement_ids=(str(req.id),),
                intro=say(
                    en=f'You gave me the words "{_clean_name(str(req.literal))}" for one shot.',
                    tr=f'Bir sahne için "{_clean_name(str(req.literal))}" yazısını verdin.',
                ),
                reason=say(
                    en=(
                        "those words appear on "
                        f"{_join_names([f'shot {i + 1}' for i in matching])} of the draft, "
                        "so I can't tell which shot you mean."
                    ),
                    tr=(
                        "bu yazı taslakta birden fazla sahnede geçiyor "
                        f"({_join_names([f'{i + 1}. sahne' for i in matching])}), "
                        "hangisini kastettiğini anlayamıyorum."
                    ),
                ),
                options=options,
                input_digest=_digest(
                    CONFLICT_TEXT_PLACEMENT, str(req.id), _norm_text(req.literal), matching
                ),
            )
        )
    return found


# Exact (after `normalize_reply`) replies that decline the title: never a substring match, so
# real words ("No Plans", "none of us slept") are never taken for an answer.
_NO_TITLE_ALIASES = (
    "no",
    "none",
    "nope",
    "no thanks",
    "skip",
    "skip it",
    "skip title",
    "skip the title",
    "no title",
    "no title please",
    "no title thanks",
    "no hook",
    "without title",
    "without a title",
    "continue without title",
    "dont add a title",
    "don t add a title",
    "do not add a title",
    "leave it off",
    "leave the title off",
    "başlık olmasın",
    "başlıksız",
    "başlık yok",
    # Typed Turkish declines (matched in every chat language).
    "hayır",
    "hayır teşekkürler",
    "yok",
    "atla",
    "geç",
    "gerek yok",
    "başlık istemiyorum",
    "başlığa gerek yok",
    "başlık koyma",
    "başlık ekleme",
    "başlıksız devam et",
    "başlık istemem",
)


def _title_text(
    strategy: Mapping[str, Any],
    brief: Any,
    rows: list[Any],
    cap: ChoiceCapability,
    *,
    clip_paths: Sequence[Any] = (),
) -> UnresolvedChoice | None:
    """A title was asked for with no words and nothing the renderer may title with.

    The unified phone montage settles a ``text`` requirement at render time and then
    BLOCKS the render when the title receipt is not met (a title is never a default, a
    model-written hook or unrequested place text), so the draft-time receipt check skips
    it (``UNIFIED_SETTLED_KINDS``) and the creator only learns after approving. Asked
    here instead, before approval. Whether a title source exists is decided by the
    renderer's own ``title_source_exists`` so the question and the render cannot disagree.
    """

    if cap.creator_id is None:
        return None
    from app.kria.brief_checks import defers_to_unified_montage  # noqa: PLC0415
    from app.pipeline.unified_montage import title_source_exists  # noqa: PLC0415

    wanted = [
        req
        for req in _live(brief)
        if req.kind == "text" and req.scope == "title" and not str(req.literal or "").strip()
    ]
    if not wanted:
        return None
    audio = strategy.get("montage_audio")
    if (
        isinstance(audio, Mapping)
        and audio.get("preserve_source_audio")
        and audio.get("source_media_ids")
    ):
        # `contract.audio_source_ids` takes the speech-excerpt lane
        # (`run_phone_speech_montage_job`): it has no title handling and never blocks on one.
        return None
    if not defers_to_unified_montage(
        creator_id=cap.creator_id,
        edit_format=strategy.get("edit_format") or "montage",
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    ):
        return None  # the draft-time receipts judge this format themselves
    if title_source_exists(strategy, brief):
        return None
    return title_text_choice(str(req.id) for req in wanted)


def title_text_choice(requirement_ids: Iterable[str]) -> UnresolvedChoice:
    """The ``title_text`` question for these wordless title requirements.

    Shared by the draft-time detector and the observer that re-opens the same question
    after the render-time block, so both carry the same ``conflict_id`` / ``input_digest``
    and an answer given to either is the answer the gate replays.
    """

    ids = sorted({str(i) for i in requirement_ids})
    return UnresolvedChoice(
        kind=CONFLICT_TITLE_TEXT,
        conflict_id=CONFLICT_TITLE_TEXT,
        field_path="opening_title",
        requirement_ids=tuple(ids),
        intro=say(
            en=(
                "You asked for a title on the opening, but you didn't tell me the words, and I "
                "don't write on-screen text for you."
            ),
            tr=(
                "Açılışa bir başlık istedin ama yazılacak kelimeleri söylemedin; ekrandaki "
                "yazıları senin yerine yazmıyorum."
            ),
        ),
        reason=say(
            en="I can't add a title without them.",
            tr="Kelimeler olmadan başlık ekleyemem.",
        ),
        options=(
            _option(
                OPT_NO_TITLE,
                label=("Continue without a title", "Başlıksız devam et"),
                description=(
                    "The video is made without an opening title.",
                    "Video açılış başlığı olmadan hazırlanır.",
                ),
                # Never recommended: a delegation ("you decide") must not drop the title the
                # creator asked for.
                recommended=False,
                aliases=_NO_TITLE_ALIASES,
            ),
        ),
        input_digest=_digest(CONFLICT_TITLE_TEXT, ids),
    )


# ── Continuous voice (KRI-479) ────────────────────────────────────────────────

# A voice that plays longer than this with NO stated length is a question ("how long?"); at or
# under it the edit simply follows the voice (the short-form ceiling).
_VOICE_IMPLICIT_ASK_S = 60.0
# A voice shorter than the asked length by more than this is a question.
_VOICE_SHORT_BY_S = 1.0
_MIN_ASKED_VOICE_S = 3.0  # ProposalDuration floor
_STANDARD_LENGTHS_S = (30, 45, 60)
_MAX_LENGTH_OPTIONS = 3
_SPEECH_PAD_S = 0.28  # the lead + tail the composer keeps around the first and last word


def _voice_audio(strategy: Mapping[str, Any], cap: ChoiceCapability) -> Mapping[str, Any] | None:
    """``montage_audio`` of a ``voice_mode == "continuous"`` strategy, when the route can render.

    ``cap.voice_route`` is false unless the continuous-voice route is actually available to new
    jobs (stamped jobs and the camera-audio renderer): then no voice question is ever asked and
    ``montage_audio`` keeps its legacy exemptions.
    """

    if not cap.voice_route or strategy.get("voice_mode") != "continuous":
        return None
    audio = strategy.get("montage_audio")
    if not isinstance(audio, Mapping) or not audio.get("preserve_source_audio"):
        return None
    return audio


def _continuous_voice(strategy: Mapping[str, Any], cap: ChoiceCapability) -> str | None:
    """The ONE named voice clip of a continuous-voice plan, else None."""

    audio = _voice_audio(strategy, cap)
    ids = [str(m) for m in (audio or {}).get("source_media_ids") or [] if m]
    return ids[0] if len(ids) == 1 else None


def _row_duration_s(row: Any) -> float | None:
    """Source length of one snapshot row (explicit, else the phone receipt's original)."""

    if not isinstance(row, Mapping):
        return None
    contract = row.get("upload_contract")
    proxy = contract.get("proxy") if isinstance(contract, Mapping) else None
    original = proxy.get("original") if isinstance(proxy, Mapping) else None
    for candidate in (
        row.get("duration_s"),
        original.get("duration_s") if isinstance(original, Mapping) else None,
        proxy.get("duration_s") if isinstance(proxy, Mapping) else None,
    ):
        value = _number(candidate)
        if value is not None:
            return value
    return None


def _speech_length_s(row: Mapping[str, Any]) -> float | None:
    """Seconds of actual speech in a clip when its timed segments are known, else None."""

    analysis = row.get("analysis")
    understanding = analysis.get("understanding") if isinstance(analysis, Mapping) else None
    speech = understanding.get("speech") if isinstance(understanding, Mapping) else None
    segments = speech.get("segments") if isinstance(speech, Mapping) else None
    starts, ends = [], []
    for seg in segments or []:
        if isinstance(seg, Mapping):
            start, end = _number(seg.get("start_s")), _number(seg.get("end_s"))
            if end is not None:
                starts.append(start if start is not None else 0.0)
                ends.append(end)
    if not ends:
        return None
    return max(ends) - min(starts) + _SPEECH_PAD_S


def _voice_length_s(row: Mapping[str, Any]) -> float | None:
    """How long the voice really plays: the speech when timed, else the clip."""

    clip = _row_duration_s(row)
    speech = _speech_length_s(row)
    if speech is None:
        return clip
    return min(speech, clip) if clip is not None else speech


def _spoken_quote(row: Mapping[str, Any]) -> str:
    """The first few words of a clip's speech: enough to recognise the take, no more."""

    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    kind = "image" if str(row.get("kind") or "video") == "image" else "video"
    speech = clip_record(row.get("analysis"), kind=kind).speech
    words = " ".join(str(speech.transcript or "").split()).split(" ")
    quote = " ".join(w for w in words[:8] if w)
    return quote[:60]


def _has_speech(row: Mapping[str, Any]) -> bool:
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    kind = "image" if str(row.get("kind") or "video") == "image" else "video"
    return bool(clip_record(row.get("analysis"), kind=kind).speech.has_speech)


def _picture_ids(strategy: Mapping[str, Any], rows: list[Any], cap: ChoiceCapability) -> list[str]:
    """The clips a continuous voice's picture is cut from: the plan's clip set minus the voice."""

    voice_id = _continuous_voice(strategy, cap)
    ids = [str(r["media_id"]) for r in rows if isinstance(r, Mapping) and r.get("media_id")]
    kept = _included_ids(strategy)
    chosen = {str(m) for m in strategy.get("selected_media_ids") or []}
    return [
        m
        for m in ids
        if m != voice_id and (kept is None or m in kept) and (not chosen or m in chosen)
    ]


def _picture_bounds_s(
    strategy: Mapping[str, Any], rows: list[Any], cap: ChoiceCapability
) -> tuple[float, float] | None:
    """(shortest, longest) edit the picture clips allow, from their real lengths; None = unknown."""

    by_id = {str(r["media_id"]): r for r in rows if isinstance(r, Mapping) and r.get("media_id")}
    durations = []
    for media_id in _picture_ids(strategy, rows, cap):
        length = _row_duration_s(by_id[media_id])
        if length is None:
            return None
        durations.append(length)
    if not durations:
        return None
    from app.pipeline.phone_speech_montage_plan import frame_bounds_for_durations  # noqa: PLC0415

    floor_s = cap.min_readable_shot_s
    lo, hi = frame_bounds_for_durations(durations, min_shot_s=floor_s)
    return lo / _MAX_FIT_FRAMES_FPS, hi / _MAX_FIT_FRAMES_FPS


def _picture_identity(strategy: Mapping[str, Any], rows: list[Any], cap: ChoiceCapability) -> list:
    """Ids and lengths of the picture clips: part of the digest, so a media change reopens it."""

    by_id = {str(r["media_id"]): r for r in rows if isinstance(r, Mapping) and r.get("media_id")}
    return sorted(
        (m, round(_row_duration_s(by_id[m]) or 0.0, 2)) for m in _picture_ids(strategy, rows, cap)
    )


def _which_voice(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    audio = _voice_audio(strategy, cap)
    if audio is None:
        return None
    named = [str(m) for m in audio.get("source_media_ids") or [] if m]
    if len(named) == 1:
        return None
    by_id = {str(r["media_id"]): r for r in rows if isinstance(r, Mapping) and r.get("media_id")}
    pool = (
        [m for m in named if m in by_id]
        if named
        else [m for m, r in by_id.items() if r.get("kind", "video") != "image" and _has_speech(r)]
    )
    if len(pool) < 2:
        return None  # one speaker is not a question, and nothing is guessed either
    pool = pool[:MAX_CHOICE_OPTIONS]
    options: list[ConflictOption] = []
    effects: dict[str, Mapping[str, Any]] = {}
    for index, media_id in enumerate(pool, start=1):
        row = by_id[media_id]
        length = _voice_length_s(row)
        quote = _spoken_quote(row)
        label = say(en=f"Clip {index}", tr=f"{index}. klip") + (
            f" ({_clean_seconds(length)} s)" if length else ""
        )
        label += f": \u201c{quote}\u201d" if quote else ""
        key = f"clip_{index}"
        options.append(
            ConflictOption(
                key=key,
                label=label,
                description=say(
                    en="Use this recording's voice under the other clips.",
                    tr="Bu kaydın sesini diğer kliplerin altında kullan.",
                ),
                aliases=(f"clip {index}", f"{index}. klip"),
            )
        )
        effects[key] = {
            "strategy": {
                "montage_audio": {
                    **{k: v for k, v in audio.items() if k != "source_media_ids"},
                    "source_media_ids": [media_id],
                }
            }
        }
    return UnresolvedChoice(
        kind=CONFLICT_WHICH_VOICE,
        conflict_id=CONFLICT_WHICH_VOICE,
        field_path="montage_audio.source_media_ids[]",
        requirement_ids=(),
        intro=say(
            en="More than one of your clips has someone talking.",
            tr="Birden fazla klibinde biri konuşuyor.",
        ),
        reason=say(
            en="only one recording can be the voice that plays under the rest.",
            tr="diğerlerinin altında yalnızca bir kayıt ses olarak çalabilir.",
        ),
        options=tuple(options),
        input_digest=_digest(CONFLICT_WHICH_VOICE, sorted(pool)),
        effects=effects,
    )


def length_option_key(seconds: int) -> str:
    return f"length_{int(seconds)}"


def _length_options(base: int, ceiling: float) -> list[int]:
    """The lengths worth offering: the shortest that works first, then sensible larger ones.

    ``base`` is the least that lets every clip be seen (a whole second, rounded UP); every value
    stays within ``ceiling`` (the voice and the footage). Never the fixed {30, 60}: those only
    appear when they are real, larger-than-needed choices.
    """

    values = [base]
    values += [v for v in _STANDARD_LENGTHS_S if base < v <= ceiling][:2]
    if len(values) == 1 and ceiling - base >= 2:
        values.append(int(math.floor(ceiling)))  # "all of it": the voice / footage you have
    return values[:_MAX_LENGTH_OPTIONS]


def _voice_vs_duration(
    strategy: Mapping[str, Any], brief: Any, rows: list[Any], cap: ChoiceCapability
) -> UnresolvedChoice | None:
    voice_id = _continuous_voice(strategy, cap)
    if voice_id is None:
        return None
    row = next((r for r in rows if isinstance(r, Mapping) and r.get("media_id") == voice_id), None)
    length = _voice_length_s(row) if row else None
    if length is None or length < _MIN_ASKED_VOICE_S:
        return None
    durations, requirement_ids, quoted = _requested_durations(brief)
    if any(abs(value - durations[0]) > 0.001 for value in durations[1:]):
        return None  # conflicting lengths are the contract's own typed error
    bounds = _picture_bounds_s(strategy, rows, cap)  # (shortest, longest) the clips allow
    identity = _picture_identity(strategy, rows, cap)
    digest = _digest(
        CONFLICT_VOICE_VS_DURATION,
        voice_id,
        round(length * 1000),
        durations[0] if durations else None,
        identity,
    )
    if durations:
        asked = durations[0]
        # A longer voice is trimmed to the asked length and disclosed: never a question.
        if length >= asked - _VOICE_SHORT_BY_S:
            return None
        whole = int(math.floor(length))
        asked_s, voice_s = _clean_seconds(asked), _clean_seconds(whole)
        said = quoted or say(en=f"{asked_s} seconds", tr=f"{asked_s} saniye")
        options: list[ConflictOption] = []
        effects: dict[str, Mapping[str, Any]] = {}
        # Each option only if the clips can actually be cut to it (never a promise that ends in
        # "won't loop": the footage must reach it and every clip must be seen).
        if whole >= _MIN_ASKED_VOICE_S and (bounds is None or bounds[0] <= whole <= bounds[1]):
            options.append(
                ConflictOption(
                    key=OPT_MATCH_VOICE,
                    label=say(
                        en=f"End the edit when your voice ends ({voice_s} seconds)",
                        tr=f"Sesin bittiğinde bitir ({voice_s} saniye)",
                    ),
                    description=say(
                        en="The video is exactly as long as your voice.",
                        tr="Video tam olarak sesin kadar uzun olur.",
                    ),
                    recommended=True,
                    aliases=("end when my voice ends", "match my voice", "match the voice"),
                )
            )
            effects[OPT_MATCH_VOICE] = {
                "strategy": {
                    "target_duration_s": whole,
                    "target_duration_requested": True,
                    "creator_duration_s": whole,
                }
            }
        if bounds is None or asked <= bounds[1]:
            options.append(
                ConflictOption(
                    key=OPT_SILENT_TAIL,
                    label=say(
                        en=(
                            f"Keep {asked_s} seconds, the last {_clean_seconds(asked - whole)} "
                            "seconds play without voice"
                        ),
                        tr=(
                            f"{asked_s} saniye kalsın, son {_clean_seconds(asked - whole)} "
                            "saniye sessiz oynasın"
                        ),
                    ),
                    description=say(
                        en="Your voice plays first, then the footage carries on in silence.",
                        tr="Önce sesin çalar, sonra çekim sessizce devam eder.",
                    ),
                    aliases=("keep the length", "keep the length without voice"),
                )
            )
            # The answered length rides with the answer, so it can never be lost.
            effects[OPT_SILENT_TAIL] = {
                "strategy": {
                    "target_duration_s": _clean_seconds(asked),
                    "target_duration_requested": True,
                    "creator_duration_s": _clean_seconds(asked),
                }
            }
        if not options:
            return None
        intro = say(
            en=f'Your brief says "{said}" and your voice runs {voice_s} seconds.',
            tr=f'İsteğinde "{said}" yazıyor ve sesin {voice_s} saniye sürüyor.',
        )
        reason = say(
            en="your voice can't fill the whole video.",
            tr="sesin videonun tamamını dolduramıyor.",
        )
    elif length > _VOICE_IMPLICIT_ASK_S:
        count = len(_picture_ids(strategy, rows, cap))
        lo_s, hi_s = bounds if bounds is not None else (None, None)
        if lo_s is None:
            from app.pipeline.unified_montage import MIN_READABLE_SHOT_S  # noqa: PLC0415

            lo_s = count * (cap.min_readable_shot_s or MIN_READABLE_SHOT_S)
        ceiling = min(length, hi_s) if hi_s is not None else length
        base = max(int(_MIN_ASKED_VOICE_S), math.ceil(lo_s - 1e-9))
        if base > ceiling + 1e-9:
            return None  # nothing works: the composer's typed decline says what is true
        values = _length_options(base, ceiling)
        options = []
        effects = {}
        for index, seconds in enumerate(values):
            key = length_option_key(seconds)
            options.append(
                ConflictOption(
                    key=key,
                    label=say(en=f"{seconds} seconds", tr=f"{seconds} saniye"),
                    description=(
                        say(
                            en=(
                                "The shortest length where every clip stays on screen long "
                                "enough to be seen."
                            ),
                            tr=("Her klibin görülecek kadar ekranda kaldığı en kısa süre."),
                        )
                        if index == 0
                        else say(
                            en="Longer: each clip stays on screen a little longer.",
                            tr="Daha uzun: her klip ekranda biraz daha uzun kalır.",
                        )
                    ),
                    recommended=index == 0,
                    aliases=(str(seconds), f"{seconds} seconds"),
                )
            )
            effects[key] = {
                "strategy": {
                    "target_duration_s": seconds,
                    "target_duration_requested": True,
                    "creator_duration_s": seconds,
                }
            }
        intro = say(
            en=f"Your voice runs {_clean_seconds(length)} seconds.",
            tr=f"Sesin {_clean_seconds(length)} saniye sürüyor.",
        )
        reason = (
            say(en="How long should the video be?", tr="Video ne kadar uzun olsun?")
            if len(values) > 1
            else say(
                en=f"{count} clips need at least {_clean_seconds(lo_s)} seconds to each be seen.",
                tr=(
                    f"{count} klibin her birinin görülmesi için en az "
                    f"{_clean_seconds(lo_s)} saniye gerekir."
                ),
            )
        )
        requirement_ids = []
    else:
        return None
    return UnresolvedChoice(
        kind=CONFLICT_VOICE_VS_DURATION,
        conflict_id=CONFLICT_VOICE_VS_DURATION,
        field_path="target_duration_s",
        requirement_ids=tuple(requirement_ids),
        intro=intro,
        reason=reason,
        options=tuple(options),
        input_digest=digest,
        effects=effects,
    )


def _snapshot_number(snapshot: Mapping[str, Any] | None, *paths: tuple[str, ...]) -> float | None:
    """First authoritative positive duration in a snapshot, never strategy data."""
    for path in paths:
        value: Any = snapshot
        for key in path:
            value = value.get(key) if isinstance(value, Mapping) else None
        number = _number(value)
        if number is not None:
            return number
    return None


def _best_parts_capacity_s(snapshot: Mapping[str, Any] | None) -> float | None:
    """Return selected footage capacity from the server-produced receipt only."""
    parts = (snapshot or {}).get("best_parts")
    if not isinstance(parts, list):
        return None
    durations = []
    for part in parts:
        if not isinstance(part, Mapping):
            return None
        raw_start = part.get("source_start_s")
        start = 0.0 if raw_start == 0 else _number(raw_start)
        end = _number(part.get("source_end_s"))
        if start is None or end is None or end <= start:
            return None
        durations.append(end - start)
    return min(120.0, sum(durations)) if durations else None


def _song_alignment_capacity_s(snapshot: Mapping[str, Any] | None, rows: list[Any]) -> float | None:
    """Return the placed song window when alignment supplies authoritative bounds.

    A full track duration is not the duration of a lip-sync edit.  Prefer explicit
    placement bounds and support the persisted per-take ``delta_s`` shape by joining
    each delta to its server-snapshotted source duration.
    """
    alignment = (snapshot or {}).get("song_alignment")
    if not isinstance(alignment, Mapping):
        return None
    row_durations = {
        str(row.get("media_id")): _number(row.get("duration_s"))
        for row in rows
        if isinstance(row, Mapping) and row.get("media_id")
    }
    placements: list[Mapping[str, Any]] = []
    for key in ("placements", "segments"):
        value = alignment.get(key)
        if isinstance(value, list):
            placements.extend(item for item in value if isinstance(item, Mapping))
    takes = alignment.get("takes")
    if isinstance(takes, Mapping):
        placements.extend(
            {**item, "media_id": media_id}
            for media_id, item in takes.items()
            if isinstance(item, Mapping)
        )
    intervals: list[tuple[float, float]] = []
    for placement in placements:
        raw_start = placement.get("start_s")
        raw_end = placement.get("end_s")
        start = 0.0 if raw_start == 0 else _number(raw_start)
        end = _number(raw_end)
        if start is None:
            raw_start = placement.get("song_start_s")
            start = 0.0 if raw_start == 0 else _number(raw_start)
        if end is None:
            end = _number(placement.get("song_end_s"))
        if start is None:
            raw_delta = placement.get("delta_s")
            start = 0.0 if raw_delta == 0 else _number(raw_delta)
        if end is None and start is not None:
            duration = _number(placement.get("duration_s")) or row_durations.get(
                str(placement.get("media_id"))
            )
            end = start + duration if duration is not None else None
        if start is not None and end is not None and end > start:
            intervals.append((start, end))
    if not intervals:
        return None
    # Song placement is one output window, including gaps bridged by B-roll.
    return max(end for _, end in intervals) - min(start for start, _ in intervals)


def _natural_capacity_s(
    strategy: Mapping[str, Any], rows: list[Any], snapshot: Mapping[str, Any] | None
) -> tuple[str, float, bool] | None:
    """Known natural source capacity as ``(owner, seconds, can_trim)``.

    The snapshot is the authority.  Missing source timing intentionally returns
    None: a question may not invent a recording, song, or selected-footage
    capacity from a model target.
    """
    from app.services.length_policy import natural_length_owner  # noqa: PLC0415

    owner = natural_length_owner(strategy)
    if owner == "song":
        section = strategy.get("selected_music_section")
        if isinstance(section, Mapping) and section.get("status") == "selected":
            start, end = section.get("start_s"), section.get("end_s")
            if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
                raw_capacity = sum(
                    _number(row.get("duration_s")) or 0 for row in rows if isinstance(row, Mapping)
                )
                return owner, min(end - start, raw_capacity or end - start), True
        placed_duration = _song_alignment_capacity_s(snapshot, rows) or _snapshot_number(
            snapshot, ("song_placement", "duration_s")
        )
        if strategy.get("song_sync") == "lipsync" and placed_duration is None:
            # The recording's full length says nothing about the placed takes.
            # Wait for source-backed alignment instead of offering invented timing.
            return None
        duration = placed_duration or _snapshot_number(
            snapshot,
            ("user_song", "duration_s"),
            ("song", "duration_s"),
            ("song_analysis", "duration_s"),
            ("song_duration_s",),
        )
        return (
            (owner, duration, bool((snapshot or {}).get("song_trim_supported")))
            if duration
            else None
        )
    if owner == "voiceover":
        recording_duration = _snapshot_number(
            snapshot,
            ("voiceover", "duration_s"),
            ("narration", "duration_s"),
            ("voiceover_duration_s",),
        )
        footage_duration = (
            _snapshot_number(snapshot, ("length_plan", "capacity_raw_s"))
            or _snapshot_number(snapshot, ("best_parts_capacity_s",))
            or (_best_parts_capacity_s(snapshot))
        )
        if recording_duration is None:
            return None
        # Voiceover is the natural clock, but it cannot produce a longer edit than
        # its already-selected, source-bounded footage.  A shorter footage result is
        # executable; trimming the recording is deliberately not promised here.
        duration = (
            min(recording_duration, footage_duration) if footage_duration else recording_duration
        )
        return owner, duration, True
    if owner == "take":
        voice_id = _continuous_voice(strategy, ChoiceCapability(voice_route=True))
        row = next(
            (r for r in rows if isinstance(r, Mapping) and r.get("media_id") == voice_id), None
        )
        if row is None:
            video_rows = [
                r for r in rows if isinstance(r, Mapping) and r.get("kind", "video") != "image"
            ]
            row = video_rows[0] if len(video_rows) == 1 else None
        duration = _voice_length_s(row) if isinstance(row, Mapping) else None
        return (owner, duration, False) if duration else None
    if owner == "footage":
        duration = (
            _snapshot_number(snapshot, ("length_plan", "capacity_raw_s"))
            or _snapshot_number(snapshot, ("best_parts_capacity_s",))
            or (_best_parts_capacity_s(snapshot))
        )
        if duration is None:
            return None
        # Selector capacity has already kept every included source at the
        # founder-approved 100 ms floor and applied its 120-second cap.
        return owner, min(120.0, duration), True
    return None


def _asks_speech_cleanup(brief: Any) -> bool:
    """The live brief already asks to cut pauses / fillers / retakes from the speech."""

    from app.kria.brief_checks import _wants_cleanup  # noqa: PLC0415

    return any(_wants_cleanup(req) for req in _live(brief))


def _length_vs_natural(
    strategy: Mapping[str, Any],
    brief: Any,
    rows: list[Any],
    cap: ChoiceCapability,
    *,
    snapshot: Mapping[str, Any] | None,
) -> UnresolvedChoice | None:
    # An exclusive creator cap is satisfied by a naturally shorter whole output.
    # Do not turn it into an exact target and ask the creator to lengthen it. Route
    # adapters that add a tail must validate their measured final export separately.
    from app.services.length_policy import (  # noqa: PLC0415
        creator_max_export_duration_s,
        strict_duration_matches,
    )

    strict_max = creator_max_export_duration_s(brief=brief)
    durations, requirement_ids, quoted = _requested_durations(brief)
    if len(set(durations)) != 1 or not durations:
        return None
    # The continuous-voice montage already has its own source-aware question
    # (including the supported silent-tail path). Do not layer a second take
    # question over it or invalidate its saved answer/digest.
    if strategy.get("voice_mode") == "continuous":
        return None
    natural = _natural_capacity_s(strategy, rows, snapshot)
    if natural is None:
        return None
    owner, capacity, can_trim = natural
    if strict_max is not None and owner == "take":
        # Phone exports append the standard ending to a spoken-take edit. A strict
        # whole-file cap must compare against that final length, not the raw take.
        from app.kria.device_render import BRAND_TAIL_SECONDS  # noqa: PLC0415

        capacity += BRAND_TAIL_SECONDS["standard"]
    if strict_max is not None and strict_duration_matches(capacity, strict_max):
        return None
    if owner == "take" and _asks_speech_cleanup(brief):
        # KRI-629: the take's raw length is not what the edit will run. Pause/filler
        # cleanup is consented to at approval, after this question would have been
        # asked; the length ask is judged against the cleaned render instead.
        return None
    source_capacity = capacity
    if capacity > cap.max_duration_s:
        if not can_trim:
            # There is no executable alternative until a supported source
            # window is known. Never offer a value the public schema rejects.
            return None
        capacity = cap.max_duration_s
    asked = durations[0]
    # A ten-percent tolerance avoids a needless card for ordinary source timing
    # rounding (e.g. a 24.0s voice rendered as 23.8s).
    if strict_max is None and abs(asked - capacity) <= max(0.01, asked * 0.10):
        return None
    if owner == "footage" and asked < 3 <= capacity:
        from app.services.length_policy import creator_duration_matches  # noqa: PLC0415

        if creator_duration_matches(3, asked):
            return None
        capacity = 3.0
        can_trim = False
    if owner == "footage" and 3 <= asked < capacity:
        # Ordinary source-window allocation supports this target. Coverage
        # conflicts have their own focused duration-vs-count question.
        return None
    source = {
        "song": "song",
        "voiceover": "voiceover with the selected footage",
        "take": "spoken take",
        "footage": "footage",
    }[owner]
    source_tr = {
        "song": "şarkı",
        "voiceover": "seçili çekimlerle birlikte seslendirme",
        "take": "konuşma kaydı",
        "footage": "çekim",
    }[owner]
    asked_s, capacity_s = _clean_seconds(asked), _clean_seconds(capacity)
    natural_description = (
        f"Use {capacity_s} seconds of the recording and selected footage; "
        "the remaining recording is left out."
        if owner == "voiceover"
        else f"The video follows the {source} without reuse or a silent extension."
    )
    if source_capacity > capacity:
        natural_description = (
            f"Use a supported {capacity_s}-second window of the {source}; "
            "the remaining source is left out."
        )
    options = [
        _option(
            OPT_ACCEPT_NATURAL,
            label=(
                f"Use the available {capacity_s} seconds",
                f"Mevcut {capacity_s} saniyeyi kullan",
            ),
            description=(
                natural_description,
                f"Video tekrar veya sessiz uzatma olmadan {source_tr} süresini takip eder.",
            ),
            recommended=asked > capacity,
            aliases=("use available length", "use the available length", "match the source"),
        )
    ]
    effects: dict[str, Mapping[str, Any]] = {
        OPT_ACCEPT_NATURAL: {
            "strategy": {
                "target_duration_s": capacity_s,
                "target_duration_requested": True,
                "creator_duration_s": capacity_s,
            }
        }
    }
    if asked < capacity and can_trim:
        options.insert(
            0,
            _option(
                OPT_TRIM_TO_REQUESTED,
                label=(f"Trim it to {asked_s} seconds", f"{asked_s} saniyeye kısalt"),
                description=(
                    f"Use the supported trim for this {source}.",
                    f"Bu {source_tr} için desteklenen kırpmayı kullanır.",
                ),
                recommended=True,
                aliases=("trim it", "trim", "keep requested length"),
            ),
        )
        effects[OPT_TRIM_TO_REQUESTED] = {
            "strategy": {
                "target_duration_s": asked_s,
                "target_duration_requested": True,
                "creator_duration_s": asked_s,
            }
        }
    said = quoted or say(en=f"{asked_s} seconds", tr=f"{asked_s} saniye")
    return UnresolvedChoice(
        kind=CONFLICT_LENGTH_VS_NATURAL,
        conflict_id=CONFLICT_LENGTH_VS_NATURAL,
        field_path="target_duration_s",
        requirement_ids=tuple(requirement_ids),
        intro=say(
            en=f'Your brief says "{said}", but the available {source} is {capacity_s} seconds.',
            tr=f'İsteğinde "{said}" yazıyor ama mevcut {source_tr} {capacity_s} saniye.',
        ),
        reason=say(
            en=(
                f"That {source} cannot make a {asked_s}-second edit without changing the "
                "source timing."
            ),
            tr=(f"kaynağın zamanlamasını değiştirmeden {asked_s} saniyelik bir video yapamam."),
        ),
        options=tuple(options),
        input_digest=_digest(CONFLICT_LENGTH_VS_NATURAL, owner, asked, capacity, snapshot),
        effects=effects,
    )


_COVERAGE_LABELS_TR = {
    "Update the selected clips": "Seçili klipleri güncelle",
    "Choose fewer clips": "Daha az klip seç",
    "Add longer footage": "Daha uzun çekim ekle",
}


def _source_coverage(
    strategy: Mapping[str, Any],
    brief: Any,
    rows: list[Any],
    cap: ChoiceCapability,
    *,
    snapshot: Mapping[str, Any] | None,
) -> UnresolvedChoice | None:
    """Keep impossible included-source coverage at the durable input gate."""
    del brief, rows, cap
    envelope = (snapshot or {}).get("length_plan")
    if not isinstance(envelope, Mapping):
        return None
    conflicts = envelope.get("conflicts") or []
    codes = {item.get("code") for item in conflicts if isinstance(item, Mapping)}
    sources = envelope.get("sources") or []
    raw_duration = sum(
        _number(source.get("duration_s")) or 0 for source in sources if isinstance(source, Mapping)
    )
    if sources and raw_duration < 3:
        codes.add("snapshot_min_duration")
    source_limit = envelope.get("max_source_count", 80)
    if type(source_limit) is not int or not 1 <= source_limit <= 80:
        source_limit = 80
    if len(sources) > source_limit:
        codes.add("source_count_limit")
    if envelope.get("target_duration_s") is None and "insufficient_capacity" in codes:
        codes.add("natural_source_capacity")
    if "target_below_coverage" in codes and (
        strategy.get("mixed_media_timing") or strategy.get("montage_cadence")
    ):
        codes.add("authored_hold_coverage")
    if not codes.intersection(
        {
            "source_below_minimum",
            "snapshot_min_duration",
            "cap_below_coverage",
            "no_usable_sources",
            "source_count_limit",
            "natural_source_capacity",
            "authored_hold_coverage",
            "single_hero_order_conflict",
            "insufficient_media",
            "duration_out_of_bounds",
        }
    ):
        return None
    if "single_hero_order_conflict" in codes:
        reason = say(
            en=(
                "The selected order conflicts with the single-hero composition. "
                "Choose the hero first, followed by its supporting clips."
            ),
            tr=(
                "Seçili sıra tek ana klipli kurguyla çelişiyor. Önce ana klibi, "
                "ardından destekleyen klipleri seç."
            ),
        )
        label = "Update the selected clips"
    elif "insufficient_media" in codes or "duration_out_of_bounds" in codes:
        reason = say(
            en=(
                "Choose a hero clip with at least 3 seconds and usable supporting clips "
                "so the hero can keep at least 60% of this edit."
            ),
            tr=(
                "En az 3 saniyelik bir ana klip ve kullanılabilir destek klipleri seç; "
                "böylece ana klip bu videonun en az %60'ını alabilir."
            ),
        )
        label = "Update the selected clips"
    elif "authored_hold_coverage" in codes:
        requested = envelope.get("target_duration_s")
        reason = say(
            en=(
                f"The requested {requested:g} seconds cannot include these clips with their "
                "approved holds. Choose fewer clips or change those holds before rendering."
            ),
            tr=(
                f"İstenen {requested:g} saniye, onaylı bekleme süreleriyle bu klipleri "
                "kapsayamıyor. Oluşturmadan önce daha az klip seç ya da bekleme "
                "sürelerini değiştir."
            ),
        )
        label = "Update the selected clips"
    elif "source_count_limit" in codes:
        reason = say(
            en=(
                f"Choose at most {source_limit} clips for this edit so the recipe can "
                "include each one."
            ),
            tr=f"Her biri kurguya girebilsin diye bu video için en fazla {source_limit} klip seç.",
        )
        label = "Choose fewer clips"
    elif "natural_source_capacity" in codes:
        natural_owner = envelope.get("natural_owner")
        natural_duration = envelope.get("natural_duration_s")
        reason = say(
            en=(
                f"The selected {natural_owner} runs {natural_duration:g} seconds, but the "
                f"footage can provide only {raw_duration:g} seconds without reuse. Add "
                "longer footage."
            ),
            tr=(
                f"Seçili kaynak {natural_duration:g} saniye sürüyor ama çekimler tekrar "
                f"olmadan yalnızca {raw_duration:g} saniye sağlayabiliyor. Daha uzun çekim ekle."
            ),
        )
        label = "Add longer footage"
    elif "cap_below_coverage" in codes:
        reason = say(
            en="Choose fewer clips so each can appear for at least 0.1 seconds within 120 seconds.",
            tr="Her biri 120 saniye içinde en az 0,1 saniye görünebilsin diye daha az klip seç.",
        )
        label = "Choose fewer clips"
    elif "source_below_minimum" in codes or "no_usable_sources" in codes:
        reason = say(
            en="Replace clips shorter than 0.1 seconds, or leave them out of this edit.",
            tr="0,1 saniyeden kısa klipleri değiştir ya da bu videoya alma.",
        )
        label = "Update the selected clips"
    else:
        reason = say(
            en="Add footage so the selected clips can provide at least 3 seconds without reuse.",
            tr="Seçili klipler tekrar olmadan en az 3 saniye sağlayabilsin diye çekim ekle.",
        )
        label = "Add longer footage"
    return UnresolvedChoice(
        kind=CONFLICT_SOURCE_COVERAGE,
        conflict_id=CONFLICT_SOURCE_COVERAGE,
        field_path="selected_media_ids",
        requirement_ids=(),
        intro=say(
            en="The selected footage cannot meet the included-clip constraints.",
            tr="Seçili çekimler, dahil edilen kliplerin koşullarını karşılayamıyor.",
        ),
        reason=reason,
        options=(
            _option(
                OPT_UPDATE_FOOTAGE,
                label=(label, _COVERAGE_LABELS_TR[label]),
                description=(
                    "I will wait for your updated footage before making this edit.",
                    "Bu düzenlemeyi yapmadan önce güncel çekimlerini bekleyeceğim.",
                ),
                recommended=True,
                aliases=("update footage", "add footage", "choose fewer clips"),
            ),
        ),
        input_digest=_digest(
            CONFLICT_SOURCE_COVERAGE, envelope.get("sources"), source_limit, sorted(codes)
        ),
    )


# ── Unclear word (KRI-849) ────────────────────────────────────────────────────

_UNCLEAR_LEAVE_OUT_ALIASES = (
    "leave it out",
    "leave that out",
    "leave the word out",
    "leave that word out",
    "skip it",
    "skip that word",
    "i don't know it either, leave that word out of the captions",
)
_UNCLEAR_LEAVE_OUT_TR = (
    "çıkar",
    "kelimeyi çıkar",
    "o kelimeyi çıkar",
    "atla",
    "boş bırak",
)
_UNCLEAR_LABEL_MAX = _NAME_MAX


def _unclear_word(
    strategy: Mapping[str, Any],
    brief: Any,
    rows: list[Any],
    cap: ChoiceCapability,
    *,
    snapshot: Mapping[str, Any] | None,
    clip_paths: Sequence[Any] = (),
) -> list[UnresolvedChoice] | None:
    """A Talking draft whose creator said "if you can't tell what he says, ask me".

    The model records that sentence as ``facts["ask_if_unclear"]`` (KRI-667: the server never
    reads the words). whisper-1 returns no per-word confidence, so the signal is the two
    independent hearings the approved snapshot row already holds before approval: Gemini's
    transcript and whisper-1's timed sentences. Where they disagree on a stretch of words (inside
    the part of the take the creator's start / end lines keep) the creator is asked which words
    the captions should say, or to leave them out. One question per unsettled span; a hearing
    pair that cannot be compared, or agrees, asks nothing (the render still never prints a guess:
    ``caption_unclear.apply_unclear_answers``). Asked only where the phone Talking writer will
    honour the answer.
    """

    if cap.creator_id is None:
        return None
    from app.kria.brief_checks import (  # noqa: PLC0415
        brief_caption_spellings,
        defers_caption_words_to_phone_render,
    )
    from app.kria.unclear_word_ask import (  # noqa: PLC0415
        ask_requirements,
        detect,
        model_answer,
        row_identity,
        unsettled,
    )
    from app.pipeline.caption_unclear import STATE_UNCLEAR  # noqa: PLC0415

    live = _live(brief)
    asks = ask_requirements(live)
    if not asks or model_answer(live) is not None:
        return None  # no ask, or the creator already told us in their own words
    if not defers_caption_words_to_phone_render(
        creator_id=cap.creator_id,
        edit_format=strategy.get("edit_format"),
        audio_strategy=strategy.get("audio_strategy"),
        clip_paths=clip_paths,
    ):
        return None
    finding = detect(live, snapshot, strategy=strategy, spellings=brief_caption_spellings(brief))
    if finding.state != STATE_UNCLEAR:
        return None
    identity = row_identity(finding.row)
    ids = tuple(sorted(str(req.id) for req in asks))
    choices = [
        choice
        for position, span in unsettled(finding.spans, live)
        if (choice := unclear_word_choice(span, position, ids, identity, finding.ask)) is not None
    ]
    return choices or None


def _unclear_label(text: str) -> str | None:
    label = " ".join(str(text).split())
    return label if label and len(label) <= _UNCLEAR_LABEL_MAX else None


def unclear_word_choice(
    span: Any,
    position: int,
    requirement_ids: Sequence[str],
    identity: Sequence[Any],
    ask: str,
) -> UnresolvedChoice | None:
    """The question for one unclear span: leave it out, or write one of the two things the
    listens heard (the creator's own pick, never ours). ``None`` when no heard words fit a
    button; the render then leaves the span out by itself."""

    from app.pipeline.caption_unclear import (  # noqa: PLC0415
        ACTION_OMIT,
        ACTION_USE,
        format_time,
    )

    heard: list[str] = []
    for text in (span.heard, span.other):
        label = _unclear_label(text)
        if label and label.casefold() not in {h.casefold() for h in heard}:
            heard.append(label)
    if not heard:
        return None
    at = format_time(span.start_s)
    window = {"start_s": round(span.start_s, 3), "end_s": round(span.end_s, 3)}
    options = [
        _option(
            OPT_LEAVE_OUT,
            label=("Leave it out", "Çıkar"),
            description=(
                "The captions skip that word.",
                "Altyazılar o kelimeyi atlar.",
            ),
            # Never recommended: a delegation ("you decide") must not choose a word for them.
            recommended=False,
            aliases=_UNCLEAR_LEAVE_OUT_ALIASES,
            tr_aliases=_UNCLEAR_LEAVE_OUT_TR,
        )
    ]
    detail: dict[str, dict[str, Any]] = {OPT_LEAVE_OUT: {**window, "action": ACTION_OMIT}}
    for number, label in enumerate(heard, start=1):
        key = f"{OPT_USE_PREFIX}{number}"
        options.append(
            _option(
                key,
                label=(label, label),
                description=("Write this in the captions.", "Bunu altyazıya yaz."),
            )
        )
        detail[key] = {**window, "action": ACTION_USE, "text": label}
    conflict_id = (
        CONFLICT_UNCLEAR_WORD if position == 0 else f"{CONFLICT_UNCLEAR_WORD}_{position + 1}"
    )
    return UnresolvedChoice(
        kind=CONFLICT_UNCLEAR_WORD,
        conflict_id=conflict_id,
        field_path="captions",
        requirement_ids=tuple(requirement_ids),
        intro=say(
            en=(
                f"I couldn't make out one word around {at} in your clip: the two listens "
                "heard different words, and I won't guess."
            ),
            tr=(
                f"Klibinde {at} civarında bir kelimeyi anlayamadım: klibini iki kez dinledim, "
                "farklı kelimeler duydum ve tahmin yürütmeyeceğim."
            ),
        ),
        reason=say(en="What should the captions say?", tr="Altyazıda ne yazsın?"),
        options=tuple(options),
        input_digest=_digest(CONFLICT_UNCLEAR_WORD, list(identity), list(span.key), ask),
        answer_detail=detail,
    )


def collect_conflicts(
    strategy: Any,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    capability: ChoiceCapability | None = None,
) -> list[UnresolvedChoice]:
    """Every askable, route-independent ambiguity, in fixed priority order.

    The only place that turns an ambiguity into a question. It reads the strategy, the
    brief and the SAME approved media snapshot approval later binds; it never reads chat
    text or a render route. A conflict is returned only when the creator can resolve it
    with one of the options listed; an unsupported combination is not a question (it is a
    typed decline), and neither is a technical failure.
    """

    data = _as_dict(strategy)
    cap = capability or ChoiceCapability()
    rows = _rows(media_snapshot)
    found: list[UnresolvedChoice] = []
    clip_paths = list((media_snapshot or {}).get("clip_paths") or [])
    for detector in (
        _order_basis,
        _text_placement,
        functools.partial(_title_text, clip_paths=clip_paths),
        functools.partial(_source_coverage, snapshot=media_snapshot),
        _duration_vs_count,
        _which_voice,
        _voice_vs_duration,
        functools.partial(_length_vs_natural, snapshot=media_snapshot),
        functools.partial(_unclear_word, snapshot=media_snapshot, clip_paths=clip_paths),
    ):
        result = detector(data, brief, rows, cap)
        if isinstance(result, list):
            found.extend(result)
        elif result is not None:
            found.append(result)
    rank = {kind: i for i, kind in enumerate(CONFLICT_PRIORITY)}
    return sorted(found, key=lambda c: rank.get(c.kind, len(rank)))  # stable within a kind


def answered_conflict_ids(strategy: Any) -> set[str]:
    return {
        str(a.get("conflict"))
        for a in _as_dict(strategy).get("choice_answers") or []
        if isinstance(a, Mapping)
    }


def open_conflicts(
    strategy: Any,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    capability: ChoiceCapability | None = None,
) -> list[UnresolvedChoice]:
    """Conflicts the strategy has NOT recorded an answer for (the approval backstop).

    Answers are attached only by :func:`resolve_choices` after its digest check, so a
    conflict id present in ``choice_answers`` counts as answered here.
    """

    answered = answered_conflict_ids(strategy)
    return [
        c
        for c in collect_conflicts(strategy, brief, media_snapshot, capability)
        if c.conflict_id not in answered
    ]


# ── Answers: scoped replay, free text, folding ────────────────────────────────


@dataclass(frozen=True)
class ScopedAnswer:
    option_key: str
    input_digest: str | None
    # The creator handed the decision to us ("you choose"): the recommended option,
    # recorded as such and disclosed. Never set for an unanswered question.
    delegated: bool = False
    # Position of the answering event in the history (later non-answer messages follow it).
    index: int = -1


def _event_list(events: Events) -> list[tuple[str, Any]]:
    return [(role, payload) for role, payload in events]


def fold_scoped_answers(events: Events) -> dict[str, ScopedAnswer]:
    """``conflict_id -> (option, input_digest of the question it answered)``, last wins."""

    questions: dict[str, dict[str, Any]] = {}
    answers: dict[str, ScopedAnswer] = {}
    for position, (role, payload) in enumerate(_event_list(events)):
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("choice_question"), dict):
            question = payload["choice_question"]
            questions[str(question.get("question_id"))] = question
            continue
        selection = payload.get("choice_selection") if role == "user" else None
        if not isinstance(selection, dict):
            continue
        question = questions.get(str(selection.get("question_id")))
        conflict = question.get("conflict") if question else None
        if not conflict or selection.get("option_key") not in _option_keys(question):
            continue
        digest = question.get("input_digest")
        answers[str(conflict)] = ScopedAnswer(
            str(selection["option_key"]),
            str(digest) if digest else None,
            selection.get("delegated") is True,
            position,
        )
    return answers


def count_asks(events: Events, conflict_id: str, input_digest: str) -> int:
    """How many times this exact question (same conflict, same inputs) was asked."""

    return sum(
        1
        for role, payload in _event_list(events)
        if role == "assistant"
        and isinstance(payload, dict)
        and isinstance(payload.get("choice_question"), dict)
        and payload["choice_question"].get("conflict") == conflict_id
        and payload["choice_question"].get("input_digest") == input_digest
    )


_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)


def normalize_reply(text: object) -> str:
    """Case, punctuation and whitespace folded away: the only fuzz a typed reply gets.

    Turkish-aware (``fold_text``): "İLERİ" and "ileri" are the same reply, and the dotted
    capital İ no longer leaves a stray combining dot that splits the word. English text
    folds exactly as before.
    """

    folded = fold_text(unicodedata.normalize("NFKC", str(text)))
    return _NON_WORD.sub(" ", folded).strip()


def loose_reply(text: object) -> str:
    """``normalize_reply`` with diacritics dropped too: "baslik yok" == "başlık yok".

    Turkish typed on an English keyboard ("hayir", "ucuncu") must still answer. Used only
    as a fallback after the exact comparison finds nothing, so two options that differ
    only by an accent are never merged.
    """

    folded = loose_text(unicodedata.normalize("NFKC", str(text)))
    return _NON_WORD.sub(" ", folded).strip()


_RECOMMENDED_TAG = re.compile(r"\s*\((?:recommended|önerilen)\)\s*$", re.IGNORECASE)

# "the first one" in Turkish. Digits are matched everywhere ("1", "1.", "1. seçenek");
# these add the spelled-out forms. Options are capped at ``MAX_CHOICE_OPTIONS`` (6).
_TR_ORDINALS: dict[int, tuple[str, ...]] = {
    1: ("birinci", "birincisi", "birincisini", "ilk", "ilki", "ilkini", "bir"),
    2: ("ikinci", "ikincisi", "ikincisini", "iki"),
    3: ("üçüncü", "üçüncüsü", "üçüncüsünü", "üç"),
    4: ("dördüncü", "dördüncüsü", "dördüncüsünü", "dört"),
    5: ("beşinci", "beşincisi", "beşincisini", "beş"),
    6: ("altıncı", "altıncısı", "altıncısını", "altı"),
}
# Spelled-out cardinals that are also plain Turkish words ("bir" = "a/one") count only
# next to a noun that makes them a number ("bir numara", "seçenek iki").
_TR_BARE_CARDINALS = frozenset({"bir", "iki", "üç", "dört", "beş", "altı"})


def _ordinal_names(number: int) -> list[str]:
    """Typed ways to name option ``number``: "ikinci", "2. seçenek", "bir numara", ..."""

    names = [
        f"{number} seçenek",
        f"seçenek {number}",
        f"{number} numara",
        f"numara {number}",
        f"{number} numaralı",
        f"{number} numaralı seçenek",
        f"{number} nolu",
        f"{number} şık",
        f"şık {number}",
    ]
    for word in _TR_ORDINALS.get(number, ()):
        if word in _TR_BARE_CARDINALS:
            names += [f"{word} numara", f"numara {word}", f"seçenek {word}", f"{word} numaralı"]
            continue
        names += [word, f"{word} seçenek", f"{word} şık"]
    return names


def _option_names(
    number: int, option: Mapping[str, Any], *, ordinals: bool = True, names_only: bool = False
) -> list[str]:
    """Every raw string a typed reply may use for this option (before normalisation).

    ``ordinals=False`` for a one-option question: there "İlk" or "birinci" is far more
    likely the creator's own words (a title) than a pick of "the first option".
    ``names_only``: just the label and the server-defined aliases. With no card on screen
    a "2" or an internal key names nothing the creator can see (KRI-794).
    """

    label = _RECOMMENDED_TAG.sub("", str(option.get("label") or ""))
    if names_only:
        return [label, *(str(a) for a in option.get("aliases") or [])]
    return [
        str(option["key"]),
        label,
        str(number),
        f"option {number}",
        f"number {number}",
        *(_ordinal_names(number) if ordinals else ()),
        *(str(a) for a in option.get("aliases") or []),
    ]


def match_open_choice(
    question: Mapping[str, Any], message: object, *, names_only: bool = False
) -> str | None:
    """The option key a plain message names, or ``None``.

    Deterministic and exact: after normalisation the message must equal exactly ONE
    option's key, label, list number ("1", "option 2", "ikinci", "2. seçenek") or
    server-defined alias. Anything else (including a message that fits two options) is
    not an answer. When nothing matches exactly, Turkish typed without its special letters
    gets one more chance against the diacritic-free forms. ``names_only`` accepts just
    the label and the aliases (a question no longer on screen, ``match_answered_choice``).
    """

    reply = normalize_reply(message)
    if not reply:
        return None
    loose = loose_reply(message)
    exact: set[str] = set()
    fuzzy: set[str] = set()
    options = question.get("options") or []
    several = sum(1 for o in options if isinstance(o, Mapping) and o.get("key")) > 1
    for number, option in enumerate(options, start=1):
        if not isinstance(option, Mapping) or not option.get("key"):
            continue
        names = _option_names(number, option, ordinals=several, names_only=names_only)
        if reply in {n for n in map(normalize_reply, names) if n}:
            exact.add(str(option["key"]))
        if loose in {n for n in map(loose_reply, names) if n}:
            fuzzy.add(str(option["key"]))
    hits = exact or fuzzy
    return next(iter(hits)) if len(hits) == 1 else None


# Bare "whatever" / "I don't care" are often a non-answer, so they are NOT delegations.
_DELEGATION_PHRASES = frozenset(
    normalize_reply(p)
    for p in (
        "you choose",
        "you decide",
        "up to you",
        "surprise me",
        # Turkish (KRI-520); compared diacritic-free too, so "sen sec" works.
        "sen seç",
        "sen seçersin",
        "sen karar ver",
        "sen karar verirsin",
        "sen bilirsin",
        "sen belirle",
        "sana bırakıyorum",
        "sana bıraktım",
        "sana kalmış",
        "kararı sana bırakıyorum",
        "karar senin",
        # No "fark etmez" / "hangisi olursa": like English "whatever", a non-answer.
        "sürpriz yap",
        "beni şaşırt",
        "dilediğin gibi",
        "dilediğin gibi yap",
        "nasıl istersen",
        "ne istersen",
    )
)
_DELEGATION_LOOSE = frozenset(loose_reply(p) for p in _DELEGATION_PHRASES)


def delegated_choice(question: Mapping[str, Any], message: object) -> str | None:
    """The recommended option key when the creator explicitly hands the choice over.

    Only a message that IS a delegation ("you choose", "up to you", "surprise me", "sen
    seç", "sana bırakıyorum") counts; repeating the request, or any instruction, never does.
    """

    if (
        normalize_reply(message) not in _DELEGATION_PHRASES
        and loose_reply(message) not in _DELEGATION_LOOSE
    ):
        return None
    if question.get("conflict") == "creative_copy":
        # Delegating authorship is not permission to use unreviewed wording.
        return None
    if question.get("kind") == CONFLICT_TITLE_TEXT:
        # Handing over a wordless title would drop the title the creator asked for (and we
        # never write on-screen words): not a delegation. The gate asks once more.
        return None
    if question.get("kind") == CONFLICT_UNCLEAR_WORD:
        # "You decide" would have us choose a word the creator asked not to be guessed.
        return None
    options = [o for o in question.get("options") or [] if isinstance(o, Mapping) and o.get("key")]
    if not options:
        return None
    pick = next((o for o in options if o.get("recommended")), options[0])
    return str(pick["key"])


# KRI-794: the strategy field a typed render refusal names (its ``field_path``) when it could
# not keep a gate conflict's answer: ``creator_render_contract.unresolved_field_path`` tags
# the unresolved ORDER lines only, so only the order basis is listed.
REFUSED_CHOICE_FIELD_PATHS: dict[str, str] = {CONFLICT_ORDER_BASIS: "ordering_choice"}
RENDER_REFUSAL_EVENT_TYPE = "assistant_render_failed"
# Kinds whose server-defined aliases are about that question's topic ("no fixed order",
# "upload order"), so they still name the option once the card is gone. Every other kind's
# aliases ("longer", "30", "clip 2") read as a new edit request without the card: only the
# option's exact label re-answers those.
_TOPICAL_ALIAS_KINDS = frozenset({CONFLICT_ORDER_BASIS})
# Aliases too generic to name an option without its card ("doesn't matter").
_CARD_ONLY_ALIASES = frozenset(normalize_reply(a) for a in ("fark etmez",))


def _turn_source_index(history: Sequence[tuple[str, Any]], turn_id: object) -> int | None:
    """Position of the user message that started ``turn_id`` (``None`` when not in view)."""

    if not turn_id:
        return None
    return next(
        (
            position
            for position, (role, payload) in enumerate(history)
            if role == "user" and isinstance(payload, dict) and payload.get("turn_id") == turn_id
        ),
        None,
    )


def _refused_since(history: Sequence[tuple[str, Any]], index: int, conflict_id: str) -> bool:
    """Was a render built with the answer at position ``index`` refused over this conflict?

    The ``needs_choice`` refusal naming the conflict must follow the answer AND come from a
    turn planned with it. A render approved BEFORE a typed re-answer can be refused after the
    re-answer is stored (the observer posts the card, then promotes the queued turn): that
    refusal is about the old answer, never the new one. A refusal whose turn is not in view
    counts.
    """

    path = REFUSED_CHOICE_FIELD_PATHS.get(conflict_id)
    if path is None:
        return False
    for role, payload in history[index + 1 :]:
        if not (
            role == "assistant"
            and isinstance(payload, dict)
            and payload.get(EVENT_TYPE_KEY, RENDER_REFUSAL_EVENT_TYPE) == RENDER_REFUSAL_EVENT_TYPE
            and payload.get("decline_reason") == "needs_choice"
            and payload.get("field_path") == path
        ):
            continue
        source = _turn_source_index(history, payload.get("turn_id"))
        if source is None or source >= index:
            return True
    return False


def _latest_questions(history: Sequence[tuple[str, Any]]) -> dict[str, dict[str, Any]]:
    """The latest question asked for each gate conflict, as the creator saw it."""

    latest: dict[str, dict[str, Any]] = {}
    for role, payload in history:
        question = payload.get("choice_question") if isinstance(payload, dict) else None
        if (
            role == "assistant"
            and isinstance(question, dict)
            and question.get("conflict") in CONFLICT_PRIORITY
            and question.get("question_id")
        ):
            latest[str(question["conflict"])] = question
    return latest


def _without_card(question: Mapping[str, Any]) -> dict[str, Any]:
    """``question`` with only the option names that still make sense once it is closed."""

    topical = question.get("kind") in _TOPICAL_ALIAS_KINDS
    options = []
    for option in question.get("options") or []:
        if not isinstance(option, Mapping):
            continue
        aliases = [
            alias
            for alias in option.get("aliases") or []
            if topical and normalize_reply(alias) not in _CARD_ONLY_ALIASES
        ]
        options.append({**option, "aliases": aliases})
    return {**question, "options": options}


def match_answered_choice(events: Events, message: object) -> tuple[dict[str, Any], str] | None:
    """KRI-794: ``(question, option key)`` when a plain message re-answers a CLOSED question.

    "Continue without a fixed order", typed after "Use the order you added the clips" was
    picked and the card had closed, was read as a new edit and the old answer stayed. Only
    the gate's own questions (``CONFLICT_PRIORITY``, replayed last-wins by
    ``resolve_choices``) that the creator HAS answered qualify, each through the latest
    question asked for its conflict (the current inputs). Consent-scoped questions (the
    simpler version, creative wording) belong to one ask and never do.

    The message must exactly name one option by its label, or (order basis only) a
    topical alias. List numbers, internal keys and "you choose" name nothing without the
    card. A message naming options of two questions answers neither. Naming the standing
    answer again is not a new answer (``None``: it stays a plain message, so an identical
    re-send is still a copy, KRI-552) unless a render refusal of that choice followed it:
    then it is the creator confirming it again.
    """

    history = _event_list(events)
    answers = fold_scoped_answers(history)
    latest = _latest_questions(history)
    hits: list[tuple[dict[str, Any], str, bool]] = []
    for conflict, prior in answers.items():
        question = latest.get(conflict)
        option_key = (
            match_open_choice(_without_card(question), message, names_only=True)
            if question is not None
            else None
        )
        if option_key is not None:
            changes = option_key != prior.option_key or _refused_since(
                history, prior.index, conflict
            )
            hits.append((question, option_key, changes))
    if len(hits) != 1 or not hits[0][2]:
        return None
    question, option_key, _ = hits[0]
    return question, option_key


@dataclass(frozen=True)
class ChoiceResolution:
    strategy: dict[str, Any]
    answers: tuple[dict[str, Any], ...]
    question: UnresolvedChoice | None
    notices: tuple[str, ...] = ()
    # Conflicts asked ``MAX_ASKS_PER_QUESTION`` times and still unanswered: the creator's
    # plan goes through UNCHANGED and the honest receipts / refusal say what is unmet.
    exhausted: tuple[UnresolvedChoice, ...] = ()
    # Answers dropped because the creator restated the requirement on a later turn (kept
    # as provenance only; a superseded answer never reaches the strategy or the pinned brief).
    superseded: tuple[str, ...] = ()


def length_inputs_waiting(resolution: ChoiceResolution) -> str | None:
    """An unanswered timing choice or required media update never becomes a draft."""
    timing_kinds = {
        CONFLICT_DURATION_VS_COUNT,
        CONFLICT_VOICE_VS_DURATION,
        CONFLICT_LENGTH_VS_NATURAL,
        CONFLICT_SOURCE_COVERAGE,
    }
    if any(choice.kind in timing_kinds for choice in resolution.exhausted):
        return say(
            en=(
                "Your length choice is still waiting. "
                "Choose an offered option or update the footage."
            ),
            tr="Uzunluk seçimin hâlâ bekliyor. Önerilen seçeneklerden birini seç ya da "
            "çekimlerini güncelle.",
        )
    if any(answer.get("kind") == CONFLICT_SOURCE_COVERAGE for answer in resolution.answers):
        return say(
            en="Add or update the selected footage, and I will continue with those clips.",
            tr="Seçtiğin çekimleri ekle ya da güncelle, ben de o kliplerle devam edeyim.",
        )
    return None


def _apply_effect(strategy: dict[str, Any], choice: UnresolvedChoice, option_key: str) -> None:
    for key, value in (choice.effects.get(option_key) or {}).get("strategy", {}).items():
        strategy[key] = value


_DISCLOSURES = {
    (CONFLICT_DURATION_VS_COUNT, OPT_EXTEND): (
        "I extended it so every clip stays on screen long enough to be seen."
    ),
    (CONFLICT_DURATION_VS_COUNT, OPT_FEWER): (
        "I kept your length and used fewer clips, spread evenly through your footage."
    ),
    (CONFLICT_ORDER_BASIS, OPT_ATTACHMENT_ORDER): ("I'm using the order you added the clips."),
    (CONFLICT_ORDER_BASIS, OPT_UNORDERED): "I'm not promising a particular order.",
    (CONFLICT_TITLE_TEXT, OPT_NO_TITLE): (
        "I'm leaving the title off, since you didn't give me the words."
    ),
    (CONFLICT_UNCLEAR_WORD, OPT_LEAVE_OUT): "I'm leaving that word out of the captions.",
    (CONFLICT_VOICE_VS_DURATION, OPT_MATCH_VOICE): "I ended the edit where your voice ends.",
    (CONFLICT_VOICE_VS_DURATION, OPT_SILENT_TAIL): (
        "I kept your length: the last seconds play without voice."
    ),
    **{
        (CONFLICT_WHICH_VOICE, f"clip_{i}"): "I'm using that clip's voice under the other clips."
        for i in range(1, MAX_CHOICE_OPTIONS + 1)
    },
}
# The same notes for a Turkish chat (KRI-520), keyed identically.
_DISCLOSURES_TR = {
    (CONFLICT_DURATION_VS_COUNT, OPT_EXTEND): (
        "Süreyi uzattım, böylece her klip görülecek kadar ekranda kalıyor."
    ),
    (CONFLICT_DURATION_VS_COUNT, OPT_FEWER): (
        "Süreni korudum ve daha az klip kullandım; klipleri çekimlerine eşit aralıklarla dağıttım."
    ),
    (CONFLICT_ORDER_BASIS, OPT_ATTACHMENT_ORDER): "Klipleri eklediğin sırayla kullanıyorum.",
    (CONFLICT_ORDER_BASIS, OPT_UNORDERED): "Belirli bir sıra vaat etmiyorum.",
    (CONFLICT_TITLE_TEXT, OPT_NO_TITLE): ("Kelimeleri söylemediğin için başlığı koymuyorum."),
    (CONFLICT_UNCLEAR_WORD, OPT_LEAVE_OUT): "O kelimeyi altyazıdan çıkarıyorum.",
    (CONFLICT_VOICE_VS_DURATION, OPT_MATCH_VOICE): "Düzenlemeyi sesinin bittiği yerde bitirdim.",
    (CONFLICT_VOICE_VS_DURATION, OPT_SILENT_TAIL): (
        "Süreni korudum: son saniyeler sesin olmadan oynuyor."
    ),
    **{
        (CONFLICT_WHICH_VOICE, f"clip_{i}"): (
            "Diğer kliplerin altında o klibin sesini kullanıyorum."
        )
        for i in range(1, MAX_CHOICE_OPTIONS + 1)
    },
}


def _disclosure(kind: object, option: object) -> str | None:
    """The plain-language note for an answered option, in the chat's language.

    Dynamic ``length_N`` keys (KRI-479) included. KRI-667: KRI-520 had added a second
    definition that shadowed KRI-479's, so the ``length_N`` note was never shown.
    """

    key = (kind, option)
    english = _DISCLOSURES.get(key)  # type: ignore[arg-type]
    if english is not None:
        return say(en=english, tr=_DISCLOSURES_TR.get(key, english))  # type: ignore[arg-type]
    if kind == CONFLICT_VOICE_VS_DURATION and str(option).startswith("length_"):
        seconds = str(option).removeprefix("length_")
        return say(
            en=f"I'm making it {seconds} seconds.",
            tr=f"Videoyu {seconds} saniye yapıyorum.",
        )
    return None


def _emits_answered_value(
    data: Mapping[str, Any], choice: UnresolvedChoice, option_key: str
) -> bool:
    """Does the model's strategy already carry the answered length / clip subset?"""

    expected = (choice.effects.get(option_key) or {}).get("strategy", {})
    if "target_duration_s" in expected:
        seen = _number(data.get("target_duration_s", 24))
        if seen is None or abs(seen - float(expected["target_duration_s"])) > 0.05:
            return False
    if "selected_media_ids" in expected:
        if {str(m) for m in data.get("selected_media_ids") or []} != {
            str(m) for m in expected["selected_media_ids"]
        }:
            return False
    return True


def _answer_superseded(
    history: Sequence[tuple[str, Any]],
    prior: ScopedAnswer,
    choice: UnresolvedChoice,
    data: Mapping[str, Any],
) -> bool:
    """Did the creator restate the requirement AFTER answering?

    The answer wins on the turn(s) that follow the answer itself (the model may echo the
    option's label or re-emit the old value). On a LATER turn, a new user message that is
    not an answer, and not a verbatim re-send of an earlier message, together with a model
    plan that no longer carries the answered value, is the creator changing their mind:
    the answer is dropped and the gate evaluates afresh (it asks again, within the cap).
    """

    if choice.kind != CONFLICT_DURATION_VS_COUNT:
        return False

    def plain_user_text(payload: Any) -> bool:
        return isinstance(payload, dict) and not isinstance(payload.get("choice_selection"), dict)

    before = {
        payload.get(CONTENT_KEY)
        for role, payload in history[: prior.index]
        if role == "user" and plain_user_text(payload) and payload.get(CONTENT_KEY)
    }
    restated = [
        payload
        for role, payload in history[prior.index + 1 :]
        if role == "user"
        and plain_user_text(payload)
        and not (payload.get(CONTENT_KEY) and payload[CONTENT_KEY] in before)
    ]
    return bool(restated) and not _emits_answered_value(data, choice, prior.option_key)


_TIMING_KINDS = frozenset(
    {
        CONFLICT_DURATION_VS_COUNT,
        CONFLICT_VOICE_VS_DURATION,
        CONFLICT_LENGTH_VS_NATURAL,
        CONFLICT_SOURCE_COVERAGE,
    }
)


def _ask_limit(choice: UnresolvedChoice) -> int:
    """How many times this conflict may be asked: once for a length, else the question and
    one re-ask."""

    return 1 if choice.kind in _TIMING_KINDS else MAX_ASKS_PER_QUESTION


def _reask_after_refusal(
    history: Sequence[tuple[str, Any]], prior: ScopedAnswer, choice: UnresolvedChoice
) -> bool:
    """KRI-794: a render refusal of this answer came after it, and one more ask is allowed.

    Re-applying it would draft the same refused edit, so the next reply offers the choice
    again. At the ask limit the answer keeps applying (it is never dropped for the creator).
    """

    return _refused_since(history, prior.index, choice.conflict_id) and count_asks(
        history, choice.conflict_id, choice.input_digest
    ) < _ask_limit(choice)


# The option a refusal re-ask recommends: the way forward that cannot hit the same refusal
# (no order promise to verify). It is also what "you choose" picks on that re-ask.
_AFTER_REFUSAL = {CONFLICT_ORDER_BASIS: OPT_UNORDERED}


def _refusal_reask(choice: UnresolvedChoice, refused: str) -> UnresolvedChoice:
    """The question again, saying why, recommending a way forward other than the refused one.

    Some option stays recommended, so a delegated "you choose" never lands on the option that
    was just refused (``delegated_choice`` falls back to the first option otherwise).
    """

    others = [option.key for option in choice.options if option.key != refused]
    pick = _AFTER_REFUSAL.get(choice.kind)
    if pick not in others:
        pick = others[0] if others else None
    lead = say(
        en="I couldn't make your last video with this choice.",
        tr="Son videonu bu seçimle oluşturamadım.",
    )
    return replace(
        choice,
        intro=f"{lead} {choice.intro}",
        options=tuple(replace(option, recommended=option.key == pick) for option in choice.options),
    )


def _answered_earlier(history: Sequence[tuple[str, Any]], prior: ScopedAnswer) -> bool:
    """Has the creator sent another message since this answer (a later turn keeps it)?"""

    return any(
        role == "user" and isinstance(payload, dict) and not payload.get("inert")
        for role, payload in history[prior.index + 1 :]
    )


def _kept_note(
    note: str,
    choice: UnresolvedChoice,
    option_key: str,
    history: Sequence[tuple[str, Any]],
) -> str:
    """A standing answer on a later turn: said as kept, with the way to change it.

    "I'm using the order you added the clips." on every later draft read as a fresh
    promise, even right after the creator typed the other option (KRI-794). The labels
    quoted are those of the question as it was asked, the exact words
    ``match_answered_choice`` accepts, even if the chat's language changed since.
    """

    kept = say(en=f"As agreed earlier: {note}", tr=f"Daha önce anlaştığımız gibi: {note}")
    asked = _latest_questions(history).get(choice.conflict_id)
    options = (
        [(str(o.get("key")), str(o.get("label") or "")) for o in asked.get("options") or []]
        if asked
        else [(o.key, o.label) for o in choice.options]
    )
    others = [f'"{label}"' for key, label in options if key != option_key and label]
    if not others or len(others) > 2:
        return kept
    return kept + say(
        en=f" To change it, reply {' or '.join(others)}.",
        tr=f" Değiştirmek için {' veya '.join(others)} yaz.",
    )


def resolve_choices(
    strategy: Any,
    brief: Any,
    media_snapshot: Mapping[str, Any] | None,
    events: Events,
    capability: ChoiceCapability | None = None,
) -> ChoiceResolution:
    """Apply earlier answers to the strategy and return at most ONE open question.

    * An answer counts only while its question's ``input_digest`` equals the digest the
      collector computes now: a changed media set or length reopens just that question.
    * The server-owned answer WINS: once answered, the chosen length / clip subset is
      written over whatever the model emitted on this turn (it may re-emit the old value
      or follow the option's label); the matching brief requirement is superseded in the
      pinned copy (``answered_brief``).
    * A LATER user message that restates the requirement (and a model plan without the
      answered value) supersedes the answer: see ``_answer_superseded``.
    * The same question is asked at most twice (``MAX_ASKS_PER_QUESTION``). After that
      nothing is chosen for the creator and nothing is rewritten: the conflict is returned
      in ``exhausted`` and the plan passes through (receipts or the backstop then state the
      unmet requirement). Only an explicit delegation ("you choose") picks the
      recommended option, recorded as ``source="creator_delegated"``.
    * KRI-794: a render refusal that names an answered choice after the answer reopens it
      once (``_reask_after_refusal``); a standing answer on a later turn is disclosed as
      kept from earlier (``_kept_note``), never as a fresh promise.
    * Model-authored ``choice_answers`` are discarded: answers are server-owned.
    """

    data = _as_dict(strategy)
    data.pop("choice_answers", None)
    history = _event_list(events)
    scoped = fold_scoped_answers(history)
    answers: list[dict[str, Any]] = []
    notices: list[str] = []
    exhausted: list[UnresolvedChoice] = []
    superseded: list[str] = []
    for choice in collect_conflicts(data, brief, media_snapshot, capability):
        keys = {o.key for o in choice.options}
        prior = scoped.get(choice.conflict_id)
        valid = bool(
            prior and prior.input_digest == choice.input_digest and prior.option_key in keys
        )
        answer_superseded = False
        if valid and _answer_superseded(history, prior, choice, data):
            superseded.append(choice.conflict_id)
            valid = False
            answer_superseded = True
        refused = bool(valid and _reask_after_refusal(history, prior, choice))
        if valid and not refused:
            option_key = prior.option_key
            source = "creator_delegated" if prior.delegated else "creator"
        elif count_asks(
            history[prior.index + 1 :] if answer_superseded else history,
            choice.conflict_id,
            choice.input_digest,
        ) >= _ask_limit(choice):
            exhausted.append(choice)
            continue
        else:
            asked = _refusal_reask(choice, prior.option_key) if refused else choice
            return ChoiceResolution(
                data, tuple(answers), asked, tuple(notices), tuple(exhausted), tuple(superseded)
            )
        answers.append(
            {
                "conflict": choice.conflict_id,
                "kind": choice.kind,
                "option": option_key,
                "input_digest": choice.input_digest,
                "requirement_ids": list(choice.requirement_ids),
                "source": source,
            }
        )
        if choice.answer_detail.get(option_key):
            answers[-1]["detail"] = dict(choice.answer_detail[option_key])
        _apply_effect(data, choice, option_key)
        note = _disclosure(choice.kind, option_key)
        if note:
            note = _disclose(note, source)
            if _answered_earlier(history, prior):
                note = _kept_note(note, choice, option_key, history)
            notices.append(note)
    if answers:
        data["choice_answers"] = answers
    return ChoiceResolution(
        data, tuple(answers), None, tuple(notices), tuple(exhausted), tuple(superseded)
    )


def _disclose(note: str, source: object) -> str:
    if source != "creator_delegated":
        return note
    return say(
        en=f"{note} (You left it to me, so I went with it.)",
        tr=f"{note} (Kararı bana bıraktığın için bunu seçtim.)",
    )


def _pins_description(pins: Mapping[str, str]) -> str:
    """The pin-only requirement text that outlives an "unordered" answer."""

    first, last = pins.get("first"), pins.get("last")
    if first and last:
        return say(
            en=f"start with {first}; end with {last}",
            tr=f"{first} ile başla; {last} ile bitir",
        )
    if first:
        return say(en=f"start with {first}", tr=f"{first} ile başla")
    return say(en=f"end with {last}", tr=f"{last} ile bitir")


def answered_brief(brief: Any, strategy: Any) -> Any:
    """``brief`` with the creator's answers applied to the requirements they resolve.

    A copy: the thread's stored brief keeps what the creator typed. The copy is what
    approval pins (``BriefBinding``), what draft receipts judge and what the render reads,
    so "Keep 15 seconds" does not keep failing a 24-second edit the creator chose.
    """

    data = _as_dict(strategy)
    answers = [a for a in data.get("choice_answers") or [] if isinstance(a, Mapping)]
    omitted = data.get("omitted_copy_targets") or []
    if brief is None or (not answers and not omitted):
        return brief
    changed: dict[str, Any] = {
        req.id: req.model_copy(update={"status": "superseded"})
        for req in _live(brief)
        if "opening_title" in omitted and req.kind == "text" and req.scope == "title"
    }
    for answer in answers:
        ids = {str(i) for i in answer.get("requirement_ids") or []}
        kind, option = answer.get("kind"), answer.get("option")
        for req in _live(brief):
            if req.id not in ids:
                continue
            if kind == CONFLICT_DURATION_VS_COUNT and option == OPT_EXTEND and req.kind == "timing":
                new = _number(data.get("target_duration_s"))
                old = _number((req.facts or {}).get("duration_s"))
                if new is None or old is None or abs(new - old) < 0.001:
                    continue
                changed[req.id] = req.model_copy(
                    update={
                        "facts": {**req.facts, "duration_s": data["target_duration_s"]},
                        "description": say(
                            en=(
                                f"Keep {_clean_seconds(new)} seconds (extended from "
                                f"{_clean_seconds(old)} so every clip can be seen)"
                            ),
                            # No "her klip ... N saniye": that reads as a per-clip length.
                            tr=(
                                f"{_clean_seconds(new)} saniye olsun ({_clean_seconds(old)} "
                                "saniyeden uzatıldı, böylece her klip görülebiliyor)"
                            ),
                        ),
                    }
                )
            elif kind == CONFLICT_ORDER_BASIS and req.kind == "order":
                if option == OPT_ATTACHMENT_ORDER:
                    changed[req.id] = req.model_copy(
                        update={
                            "facts": {**req.facts, "key": ATTACHMENT_ORDER_KEY},
                            "description": say(
                                en="in the order the clips were added",
                                tr="klipleri eklediğin sırayla",
                            ),
                        }
                    )
                elif option == OPT_UNORDERED:
                    pins = pin_anchors(req)
                    if pins and _placed_pin_positions(data):
                        # KRI-762: "no fixed order" drops the basis, not the clips the
                        # creator pinned first / last and the server placed.
                        changed[req.id] = req.model_copy(
                            update={
                                "facts": {
                                    fact: pins[position]
                                    for fact, position in ANCHOR_FACTS
                                    if position in pins
                                },
                                "description": _pins_description(pins),
                            }
                        )
                    else:
                        changed[req.id] = req.model_copy(update={"status": "superseded"})
            elif (
                kind == CONFLICT_VOICE_VS_DURATION
                and option == OPT_MATCH_VOICE
                and req.kind == "timing"
            ):
                new = _number(data.get("target_duration_s"))
                old = _number((req.facts or {}).get("duration_s"))
                if new is None or old is None or abs(new - old) < 0.001:
                    continue
                changed[req.id] = req.model_copy(
                    update={
                        "facts": {**req.facts, "duration_s": data["target_duration_s"]},
                        "description": say(
                            en=(
                                f"Keep {_clean_seconds(new)} seconds (as long as your voice, "
                                f"shortened from {_clean_seconds(old)})"
                            ),
                            tr=(
                                f"{_clean_seconds(new)} saniye olsun (sesin kadar, "
                                f"{_clean_seconds(old)} saniyeden kısaltıldı)"
                            ),
                        ),
                    }
                )
            elif (
                kind == CONFLICT_LENGTH_VS_NATURAL
                and option == OPT_ACCEPT_NATURAL
                and req.kind == "timing"
            ):
                new = _number(data.get("target_duration_s"))
                old = _number((req.facts or {}).get("duration_s"))
                if new is None or old is None:
                    continue
                from app.services.length_policy import strict_duration_limit_s  # noqa: PLC0415

                # Choosing the available source is an explicit waiver of an
                # exclusive cap, even when both values print as the same
                # number (e.g. take 38.4 + standard 1.6 s tail = 40).
                if abs(new - old) < 0.001 and strict_duration_limit_s(req) is None:
                    continue
                changed[req.id] = req.model_copy(
                    update={
                        "facts": {**req.facts, "duration_s": data["target_duration_s"]},
                        "description": say(
                            en=(
                                f"Keep {_clean_seconds(new)} seconds (the available source "
                                f"length, instead of {_clean_seconds(old)})"
                            ),
                            tr=(
                                f"{_clean_seconds(new)} saniye olsun ({_clean_seconds(old)} "
                                "saniye yerine, eldeki çekimlerin uzunluğu)"
                            ),
                        ),
                    }
                )
            elif (
                kind == CONFLICT_TITLE_TEXT
                and option == OPT_NO_TITLE
                and req.kind == "text"
                and req.scope == "title"
            ):
                changed[req.id] = req.model_copy(update={"status": "superseded"})
    _apply_unclear_answers(brief, answers, changed)
    if not changed:
        return brief
    return brief.model_copy(
        update={"requirements": [changed.get(r.id, r) for r in brief.requirements]}
    )


def _apply_unclear_answers(brief: Any, answers: Sequence[Mapping[str, Any]], changed: dict) -> None:
    """KRI-849: each unclear-word answer becomes a structured entry in the ask requirement's
    ``facts["unclear_answers"]`` (``[{start_s, end_s, action, text?}]``) on the pinned copy, so
    the approved brief the writer reads says which span the creator left out or worded."""

    for answer in answers:
        detail = answer.get("detail")
        if answer.get("kind") != CONFLICT_UNCLEAR_WORD or not isinstance(detail, Mapping):
            continue
        entry = {
            key: detail[key] for key in ("start_s", "end_s", "action", "text") if key in detail
        }
        ids = {str(i) for i in answer.get("requirement_ids") or []}
        for req in _live(brief):
            if req.id not in ids or not (req.facts or {}).get("ask_if_unclear"):
                continue
            current = changed.get(req.id, req)
            known = [
                row
                for row in (current.facts or {}).get("unclear_answers") or []
                if isinstance(row, Mapping)
            ]
            if any(
                (row.get("start_s"), row.get("end_s")) == (entry.get("start_s"), entry.get("end_s"))
                for row in known
            ):
                continue
            changed[req.id] = current.model_copy(
                update={"facts": {**current.facts, "unclear_answers": [*known, entry]}}
            )


def choice_notices(strategy: Any) -> list[str]:
    """Plain-language disclosure of the decisions a strategy carries."""

    out: list[str] = []
    for answer in _as_dict(strategy).get("choice_answers") or []:
        if not isinstance(answer, Mapping):
            continue
        note = _disclosure(answer.get("kind"), answer.get("option"))
        if note:
            out.append(_disclose(note, answer.get("source")))
    return out


def ask_user_choice(
    question: str, reason_code: str, options: Sequence[str]
) -> tuple[str, dict[str, Any]] | None:
    """Turn the Creator agent's ``AskUser`` options into a tappable question.

    Needs at least two distinct options; otherwise the caller keeps the text-only question.
    The text lists the options too, so a client that cannot render the card still shows
    every choice. Returns ``(response_text, choice_question_payload)``.
    """

    labels: list[str] = []
    for raw in options:
        label = " ".join(str(raw).split())[:120]
        if label and label not in labels:
            labels.append(label)
    labels = labels[:MAX_CHOICE_OPTIONS]
    if len(labels) < 2:
        return None
    candidate = ConflictCandidate(
        conflict_id=f"ask_user:{reason_code}"[:80],
        reason="",
        intro=question.strip(),
        options=tuple(
            ConflictOption(key=f"option_{i}", label=text) for i, text in enumerate(labels, 1)
        ),
    )
    lines = [question.strip()]
    lines += [f"{i}. {label}" for i, label in enumerate(labels, start=1)]
    lines.append(
        say(
            en="Tap an option, or tell me in your own words.",
            tr="Bir seçeneğe dokun ya da kendi sözlerinle anlat.",
        )
    )
    text = "\n".join(lines)
    if len(text) > 1200:
        return None
    return text, build_choice_question(candidate)


__all__ = [
    "ATTACHMENT_ORDER_KEY",
    "CAPTURE_ORDER_KEYS",
    "CHOICE_QUESTION_VERSION",
    "CONFLICT_DURATION_VS_COUNT",
    "CONFLICT_LENGTH_VS_NATURAL",
    "CONFLICT_ORDER_BASIS",
    "CONFLICT_PRIORITY",
    "CONFLICT_TEXT_PLACEMENT",
    "CONFLICT_TITLE_TEXT",
    "CONFLICT_UNCLEAR_WORD",
    "CONFLICT_VOICE_VS_DURATION",
    "CONFLICT_WHICH_VOICE",
    "KEEP_OPEN_REASON",
    "MAX_ASKS_PER_QUESTION",
    "OPT_ATTACHMENT_ORDER",
    "OPT_EXTEND",
    "OPT_ACCEPT_NATURAL",
    "OPT_TRIM_TO_REQUESTED",
    "OPT_FEWER",
    "OPT_LEAVE_OUT",
    "OPT_MATCH_VOICE",
    "OPT_NO_TITLE",
    "OPT_SILENT_TAIL",
    "OPT_UNORDERED",
    "title_text_choice",
    "unclear_word_choice",
    "ChoiceCapability",
    "ChoiceResolution",
    "ScopedAnswer",
    "UnresolvedChoice",
    "answered_brief",
    "answered_conflict_ids",
    "ask_user_choice",
    "choice_notices",
    "collect_conflicts",
    "count_asks",
    "delegated_choice",
    "fold_scoped_answers",
    "match_answered_choice",
    "match_open_choice",
    "normalize_reply",
    "one_clip_edit",
    "open_conflicts",
    "resolve_choices",
    "CONFLICT_ORDER_VS_GROUP",
    "OPT_CHRONOLOGICAL",
    "OPT_GROUP_FIRST",
    "ORDER_VS_GROUP_OPTIONS",
    "ChoiceSelectionIn",
    "ConflictCandidate",
    "ConflictOption",
    "build_choice_question",
    "choice_question_text",
    "detect_order_vs_group",
    "fold_choice_answers",
    "latest_open_choice_question",
    "split_groups",
    "SIMPLER_VERSION_REASON",
    "STRATEGY_REFUSAL_KIND",
    "simpler_version_accepted",
    "simpler_version_question",
]
