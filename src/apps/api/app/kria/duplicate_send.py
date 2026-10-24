"""An identical re-send of the creator's message is a copy, not a new request (KRI-552).

The chat can look stuck for a minute or two while a turn plans, so creators send the same
words again. A copy used to queue behind the running turn: its events changed the thread
revision (throwing the first turn back to plan again, and with it the brief it was about to
bind), and once the first render finished it planned a second time and asked the creator to
restate things the render had already done.

A copy is recognised by the creator's own words being the same as their latest message, and
by that message's turn being in flight, or finished with a render review in the last couple of
minutes. It is answered in one line, in the chat's language, and never planned. Anything else
(a failed turn, a question waiting for an answer, a message that answered a tapped question,
a re-send long after the render) is the creator asking again and runs as before.

No regex: two messages are the same when their normalized words are equal.
"""

from __future__ import annotations

import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.kria.reply_language import say
from app.kria.text_trim import trim_at_word
from app.models import CreationThread, CreationThreadEvent, CreatorAgentTurn

# A re-send this soon after a finished render is the creator's thumb, not a new ask.
FINISHED_WINDOW = timedelta(seconds=120)
# The review the copy points back at is a few lines; never paste a wall of text.
_REVIEW_MAX_CHARS = 1_200
# Payload keys of a message that is not plain free text: a tapped answer, a scoped edit.
_STRUCTURED_KEYS = (
    "clip_selection",
    "choice_selection",
    "song_order",
    "song_range",
    "scope",
    "protected_region",  # KRI-901: a mark answers the mark card
)
_IN_FLIGHT = frozenset(
    {"pending", "queued", "planning", "executing", "awaiting_approval", "observing"}
)

DuplicateState = Literal["working", "awaiting_approval", "rendering", "finished"]


@dataclass(frozen=True)
class DuplicateSend:
    """The earlier turn an identical message repeats, and where it stands."""

    turn_id: uuid.UUID
    state: DuplicateState
    # The review the first turn already showed the creator (state "finished" only).
    review: str = ""


def normalized(message: str | None) -> str:
    """The message's words, ignoring case, spacing and compatibility forms."""
    return " ".join(unicodedata.normalize("NFKC", message or "").split()).casefold()


def is_duplicate_answer(payload: object) -> bool:
    """Whether a thread event is the copy of a message or the answer given to it."""
    return isinstance(payload, dict) and bool(payload.get("duplicate_of"))


def is_plain_message(payload: object) -> bool:
    """A free-text message: it carries no tapped answer and no scoped edit."""
    return isinstance(payload, dict) and not any(payload.get(key) for key in _STRUCTURED_KEYS)


def _state_of(turn: CreatorAgentTurn, review: str, *, now: datetime) -> DuplicateState | None:
    status = turn.status
    if status in {"pending", "queued", "planning"}:
        return "working"
    if status == "awaiting_approval":
        return "awaiting_approval"
    if status in {"executing", "observing"}:
        return "rendering"
    if status != "completed" or not review:
        return None
    # Finished: only a turn that produced a draft and a render review counts, and only soon
    # after (a question or a recovery needs the creator's retry, a failed render too).
    plan = turn.plan_json if isinstance(turn.plan_json, dict) else {}
    if plan.get("mode") != "act" or turn.completed_at is None:
        return None
    if now - turn.completed_at > FINISHED_WINDOW:
        return None
    return "finished"


async def find_duplicate_send(
    db: AsyncSession, thread: CreationThread, message: str, *, now: datetime | None = None
) -> DuplicateSend | None:
    """The turn ``message`` repeats, or None when it is a new request.

    Only the creator's latest message counts: anything said since makes the words a
    deliberate new ask.
    """
    wanted = normalized(message)
    if not wanted:
        return None
    latest = (
        await db.execute(
            select(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == thread.id,
                CreationThreadEvent.role == "user",
                CreationThreadEvent.event_type == "user_message",
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    payload: Any = latest.payload if latest is not None else None
    if (
        latest is None
        or normalized(latest.content) != wanted
        or not is_plain_message(payload)
        or payload.get("inert")  # a status or help question gets its answer again
    ):
        return None
    try:
        turn_id = uuid.UUID(str(payload.get("turn_id")))
    except ValueError:
        return None
    turn = (
        await db.execute(
            select(CreatorAgentTurn).where(
                CreatorAgentTurn.id == turn_id, CreatorAgentTurn.thread_id == thread.id
            )
        )
    ).scalar_one_or_none()
    if turn is None or turn.status not in _IN_FLIGHT | {"completed"}:
        return None
    review = ""
    if turn.status == "completed" and turn.observed_event_id is not None:
        observed = await db.get(CreationThreadEvent, turn.observed_event_id)
        if observed is not None and observed.event_type == "assistant_review":
            review = str(observed.content or "").strip()
    state = _state_of(turn, review, now=now or datetime.now(UTC))
    if state is None:
        return None
    return DuplicateSend(turn_id=turn.id, state=state, review=review)


def duplicate_reply(duplicate: DuplicateSend) -> str:
    """One line for the chat's language (call inside ``reply_language_for``)."""
    if duplicate.state == "awaiting_approval":
        return say(
            en=(
                "That's the same message as the draft waiting for you. Review the pinned "
                "approval before I start the render."
            ),
            tr=(
                "Bu, onayını bekleyen taslağın mesajıyla aynısı. Videoyu oluşturmaya "
                "başlamadan önce sabitlenen onayı incele."
            ),
        )
    if duplicate.state == "rendering":
        return say(
            en=(
                "That's the same message as the render in progress, so I won't start it "
                "twice. Your approved render is on its way."
            ),
            tr=(
                "Bu, devam eden videonun mesajıyla aynısı; o yüzden iki kez başlatmıyorum. "
                "Onayladığın video hazırlanıyor."
            ),
        )
    if duplicate.state == "finished":
        lead = say(
            en="That's the same message I just finished, so I didn't plan it again.",
            tr="Bu, az önce bitirdiğim mesajın aynısı; o yüzden yeniden planlamadım.",
        )
        return f"{lead}\n{trim_at_word(duplicate.review, _REVIEW_MAX_CHARS)}"
    return say(
        en=(
            "That's the same message I'm already working on, so I won't start it twice. "
            "Your project is saved."
        ),
        tr=(
            "Bu, üzerinde zaten çalıştığım mesajın aynısı; o yüzden iki kez başlatmıyorum. "
            "Projen kaydedildi."
        ),
    )
