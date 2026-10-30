"""A clip picker's "None of these" is the creator's answer, never a dead end (KRI-1025).

Plaza de Mayo M1 (thread A228E85B, 2026-10-28): "End on the Obelisco", which was never
filmed. The picker asked which clips show "the Obelisco" and the creator answered None. The
clip planner honoured it (``apply_creator_selections`` drops the pin), but the brief still
said ``last_clip: "the Obelisco"``, so the draft backstop's render contract refused with no
draft, no question and no way forward: "I couldn't tell which clip "the Obelisco" means,
so I can't confirm the order." Creator text whose described clip was answered None ("Casa
Rosada" on the pink building) hits the same wall (``text_clip_not_found``), or stays
pinned on an edit that prints it nowhere.

The answer is recorded like every other creator decision: a server-owned ``choice_answer``
on the draft strategy (``pin_absent`` / ``text_absent``), which ``answered_brief`` applies
to the pinned brief copy. That copy is what draft receipts judge, what approval binds and
what dispatch's contract reads, so nothing later re-asks or refuses what the creator
already answered. The draft goes ahead without the pin or text and says so, with the way
to change it.

Text answers also bind the described shot and an observed request turn preceding the
question. Their saved digest binds the requirement's content and source turn, so an old
answer cannot cancel the same words moved to another shot or a rewritten requirement.

Only the words the creator answered None to are voided: the newest picker answer for the
same words (case, articles and punctuation aside) must be None or a skip, and no other
resolved pin or placed text may already hold that spot. A typed new ending ("End on the
cathedral") rewrites the brief, so an old None never voids it.

Everything is derived from the thread's events, like ``fold_clip_selections``: no state,
no model call.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.kria.reply_language import say
from app.services.choice_questions import (
    ATTACHMENT_ORDER_KEY,
    _digest,
    _live,
    _pins_description,
    _placed_pin_positions,
)
from app.services.clip_order_sequence import ANCHOR_FACTS
from app.services.clip_selection import answered_rows
from app.text_fold import loose_words

CONFLICT_PIN_ABSENT = "pin_absent"
CONFLICT_TEXT_ABSENT = "text_absent"
OPT_LEFT_OUT = "left_out"

# Ops whose picker row asks "which clips show <words>?" (`clip_selection._SAME_CLIPS_OPS`).
_SHOWS_OPS = frozenset({"group", "include", "order", "caption"})
_TEXT_OPS = frozenset({"label", "caption"})
# The order keys whose basis the creator calls "the order I filmed them".
_FILMED_KEYS = frozenset({"capture_time", "chronological"})
_RULE_FACTS = ("by", "direction", "order")
_ARTICLES = frozenset({"the", "a", "an"})
_FACT_OF = {position: fact for fact, position in ANCHOR_FACTS}


@dataclass(frozen=True)
class _PickerAnswer:
    """One picker row's answer, in thread order (``seq``), as ``fold_clip_selections`` reads it."""

    op: str
    words: tuple[tuple[str, ...], ...]  # the row's key attribute and its label, as words
    label: str
    creator_text: str | None
    none: bool
    skipped: bool
    seq: int
    question_index: int


@dataclass(frozen=True)
class NoneAnswers:
    answers: tuple[dict[str, Any], ...] = ()
    notices: tuple[str, ...] = ()


def _content(text: object) -> tuple[str, ...]:
    return tuple(w for w in loose_words(str(text or "")) if w not in _ARTICLES)


def _picker_answers(events: Iterable[tuple[str, Any]]) -> list[_PickerAnswer]:
    """Every answered picker row, oldest first (the same events and rows as the fold)."""
    questions: dict[str, tuple[dict[str, Any], int]] = {}
    out: list[_PickerAnswer] = []
    seq = 0
    for event_index, (role, payload) in enumerate(events):
        if not isinstance(payload, dict):
            continue
        if role == "assistant" and isinstance(payload.get("clip_question"), dict):
            question = payload["clip_question"]
            questions[str(question.get("question_id"))] = (question, event_index)
            continue
        selection = payload.get("clip_selection") if role == "user" else None
        if not isinstance(selection, dict):
            continue
        asked = questions.get(str(selection.get("question_id")))
        if asked is None:
            continue
        question, question_index = asked
        seq += 1
        skipped = bool(selection.get("skipped"))
        tapped = {
            answer.get("key"): bool(answer.get("media_ids"))
            for answer in selection.get("answers") or []
            if isinstance(answer, Mapping)
        }
        none_keys = set(selection.get("none_keys") or [])
        for category in question.get("categories") or []:
            if not isinstance(category, dict) or not category.get("key"):
                continue
            key = category["key"]
            if skipped or key in none_keys:
                none = True
            elif key in tapped:
                none = not tapped[key]
            else:
                continue
            label = str(category.get("label") or "")
            for row in answered_rows(category):
                attribute = str(row["key"]).partition(":")[2].replace("-", " ")
                out.append(
                    _PickerAnswer(
                        op=str(row.get("op") or str(row["key"]).partition(":")[0]),
                        words=(_content(attribute), _content(label)),
                        label=label,
                        creator_text=row.get("creator_text") or None,
                        none=none,
                        skipped=skipped,
                        seq=seq,
                        question_index=question_index,
                    )
                )
    return out


def _newest(answers: Sequence[_PickerAnswer]) -> _PickerAnswer | None:
    return max(answers, key=lambda a: a.seq) if answers else None


def _placed_texts(strategy: Mapping[str, Any]) -> set[tuple[str, ...]]:
    """Texts the draft already prints somewhere, as words: never "left out"."""
    texts: list[object] = [strategy.get("opening_title"), strategy.get("closing_title")]
    texts += list(strategy.get("shot_labels") or [])
    texts += [p.get("text") for p in strategy.get("pinned_texts") or [] if isinstance(p, Mapping)]
    for intent in strategy.get("resolved_clip_intents") or []:
        if (
            isinstance(intent, Mapping)
            and intent.get("op") in _TEXT_OPS
            and str(intent.get("status") or "resolved") == "resolved"
            and intent.get("assignments")
        ):
            texts.append(intent.get("creator_text"))
            texts += [a.get("value") for a in intent["assignments"] if isinstance(a, Mapping)]
    return {words for text in texts if text and (words := tuple(loose_words(str(text))))}


def _order_basis(req: Any) -> str | None:
    key = str((req.facts or {}).get("key") or "").casefold()
    if key in _FILMED_KEYS:
        return "filmed"
    if key == ATTACHMENT_ORDER_KEY:
        return "added"
    return None


def _pin_notice(
    position: str, words: str, basis: str | None, *, skipped: bool, withdrawn: bool = False
) -> str:
    if withdrawn:
        # KRI-1068: the creator typed "skip that" after a reply stuck on this clip.
        head = say(
            en=f'As you asked, I dropped "{words}"',
            tr=f'İstediğin gibi "{words}" kısmını çıkardım',
        )
    elif skipped:
        head = say(
            en=f'You didn\'t pick a clip for "{words}"', tr=f'"{words}" için bir klip seçmedin'
        )
    else:
        head = say(
            en=f'None of your clips show "{words}"',
            tr=f'Kliplerinin hiçbirinde "{words}" yok',
        )
    last = position == "last"
    if basis is not None:
        filmed = basis == "filmed"
        clip = say(
            en=f"{'last' if last else 'first'} clip you {'filmed' if filmed else 'added'}",
            tr=f"{'çektiğin' if filmed else 'eklediğin'} {'son' if last else 'ilk'} klip",
        )
        tail = (
            say(
                en=(
                    f"so the video ends on the {clip}. Tell me which clip to end on if you'd "
                    "like a different one."
                ),
                tr=(
                    f"o yüzden video {clip}te bitiyor. Başka bir klipte bitmesini istersen "
                    "hangisi olduğunu söyle."
                ),
            )
            if last
            else say(
                en=(
                    f"so the video starts with the {clip}. Tell me which clip to start with "
                    "if you'd like a different one."
                ),
                tr=(
                    f"o yüzden video {clip} ile başlıyor. Başka bir klip ile başlamasını "
                    "istersen hangisi olduğunu söyle."
                ),
            )
        )
    else:
        tail = (
            say(
                en=(
                    "so I didn't pick a clip to end on. Tell me which clip to end on if you "
                    "have one in mind."
                ),
                tr=(
                    "o yüzden sona özel bir klip koymadım. Aklında bir klip varsa hangisiyle "
                    "bitireyim söyle."
                ),
            )
            if last
            else say(
                en=(
                    "so I didn't pick a clip to start with. Tell me which clip to start with "
                    "if you have one in mind."
                ),
                tr=(
                    "o yüzden başa özel bir klip koymadım. Aklında bir klip varsa hangisiyle "
                    "başlayayım söyle."
                ),
            )
        )
    return f"{head}, {tail}"


def _text_notice(literal: str, label: str, *, skipped: bool) -> str:
    if skipped:
        head = say(
            en=f'You didn\'t pick a clip for "{label}"',
            tr=f'"{label}" için bir klip seçmedin',
        )
    else:
        head = say(
            en=f'None of your clips show "{label}"',
            tr=f'Kliplerinin hiçbirinde "{label}" yok',
        )
    return say(
        en=(
            f'{head}, so I left out "{literal}". Tell me which clip it goes on if you still '
            "want it."
        ),
        tr=(
            f'{head}, o yüzden "{literal}" yazısını koymadım. Yine de istersen hangi klibe '
            "gideceğini söyle."
        ),
    )


def _text_digest(req: Any) -> str:
    return _digest(
        CONFLICT_TEXT_ABSENT,
        req.id,
        req.kind,
        req.scope,
        req.literal,
        req.description,
        req.facts,
        req.source_turn_id,
    )


def none_answer_choices(
    brief: Any, strategy: Mapping[str, Any], events: Iterable[tuple[str, Any]]
) -> NoneAnswers:
    """The creator's None / skip picker answers that void a brief pin or text, and what to say.

    ``answers`` are ``choice_answer`` rows for the draft strategy; ``notices`` tell the
    creator, in the chat's language, what the draft does instead and how to change it.
    """
    events = list(events)
    picked = _picker_answers(events)
    if not picked or brief is None:
        return NoneAnswers()
    placed_spots = _placed_pin_positions(strategy)
    placed_texts = _placed_texts(strategy)
    request_turns: dict[str, int] = {}
    for index, (role, payload) in enumerate(events):
        if role == "user" and isinstance(payload, Mapping) and payload.get("turn_id"):
            request_turns.setdefault(str(payload["turn_id"]), index)
    answers: list[dict[str, Any]] = []
    notices: list[str] = []
    for req in _live(brief):
        facts = req.facts or {}
        if req.kind == "order":
            for fact, position in ANCHOR_FACTS:
                words = facts.get(fact)
                said = _content(words) if isinstance(words, str) else ()
                if not said or position in placed_spots:
                    continue
                newest = _newest([a for a in picked if a.op in _SHOWS_OPS and said in a.words])
                if newest is None or not newest.none:
                    continue
                words = " ".join(str(words).split())
                answers.append(
                    {
                        "conflict": f"{CONFLICT_PIN_ABSENT}:{req.id}:{position}",
                        "kind": CONFLICT_PIN_ABSENT,
                        "option": position,
                        "input_digest": _digest(CONFLICT_PIN_ABSENT, req.id, position, words),
                        "requirement_ids": [req.id],
                        "source": "creator",
                        "value": words[:300],
                    }
                )
                notices.append(
                    _pin_notice(position, words, _order_basis(req), skipped=newest.skipped)
                )
        elif req.kind == "text" and req.literal:
            text = tuple(loose_words(req.literal))
            shot = _content(req.description)
            request_index = request_turns.get(req.source_turn_id)
            if not text or text in placed_texts or not shot or request_index is None:
                continue
            newest = _newest(
                [
                    a
                    for a in picked
                    if a.op in _TEXT_OPS
                    and a.creator_text
                    and tuple(loose_words(a.creator_text)) == text
                    and shot in a.words
                    and request_index <= a.question_index
                ]
            )
            if newest is None or not newest.none:
                continue
            literal = " ".join(req.literal.split())
            answers.append(
                {
                    "conflict": f"{CONFLICT_TEXT_ABSENT}:{req.id}",
                    "kind": CONFLICT_TEXT_ABSENT,
                    "option": OPT_LEFT_OUT,
                    "input_digest": _text_digest(req),
                    "requirement_ids": [req.id],
                    "source": "creator",
                    "value": newest.label[:300],
                }
            )
            notices.append(_text_notice(literal, newest.label, skipped=newest.skipped))
    return NoneAnswers(tuple(answers), tuple(dict.fromkeys(notices)))


def _without_pins(req: Any, gone: dict[str, str], *, withdrawn: bool = False) -> Any:
    """``req`` without the first / last clips in ``gone`` (``{position: words}``).

    ``withdrawn``: the creator took them back (KRI-1068), rather than answering that no clip
    shows them, which is what a rule this module cannot re-word then says.
    """
    facts = dict(req.facts or {})
    for position, words in gone.items():
        fact = _FACT_OF[position]
        if _content(facts.get(fact)) == _content(words):
            facts.pop(fact)
    if facts == (req.facts or {}):
        return req  # a stale answer (the creator has since named another clip)
    pins = {
        position: " ".join(str(facts[fact]).split())
        for fact, position in ANCHOR_FACTS
        if isinstance(facts.get(fact), str) and facts[fact].strip()
    }
    basis = _order_basis(req)
    rule = bool(facts.get("key")) or any(facts.get(k) for k in _RULE_FACTS)
    if not rule:
        if not pins:
            # "End on the Obelisco" and nothing else: nothing is left to promise.
            return req.model_copy(update={"status": "superseded"})
        description = _pins_description(pins)
    elif basis is not None:
        description = (
            say(en="in the order you filmed them", tr="çektiğin sırayla")
            if basis == "filmed"
            else say(en="in the order the clips were added", tr="klipleri eklediğin sırayla")
        )
        if "first" in pins:
            description += say(
                en=f", starting with {pins['first']}", tr=f", {pins['first']} ile başlayarak"
            )
        if "last" in pins:
            description += say(
                en=f", ending on {pins['last']}", tr=f", {pins['last']} ile bitirerek"
            )
    else:
        # A rule this module cannot re-word ("along my route"): keep it, say what is gone.
        missing = ", ".join(f'"{words}"' for words in gone.values())
        description = (
            say(
                en=f"{req.description} (without {missing})",
                tr=f"{req.description} ({missing} olmadan)",
            )
            if withdrawn
            else say(
                en=f"{req.description} (none of your clips show {missing})",
                tr=f"{req.description} (kliplerinde {missing} yok)",
            )
        )
    return req.model_copy(update={"facts": facts, "description": description})


def apply_none_answers(
    brief: Any, answers: Sequence[Mapping[str, Any]], changed: dict[str, Any]
) -> None:
    """``answered_brief``'s step for ``pin_absent`` / ``text_absent`` answers.

    Applied on top of whatever the other answers already changed, so the order of the
    answers does not matter; a requirement another answer superseded stays superseded.
    """
    gone: dict[str, dict[str, str]] = {}
    left_out: dict[str, set[str]] = {}
    for answer in answers:
        ids = [str(i) for i in answer.get("requirement_ids") or []]
        if answer.get("kind") == CONFLICT_PIN_ABSENT and answer.get("option") in _FACT_OF:
            for req_id in ids:
                gone.setdefault(req_id, {})[str(answer["option"])] = str(answer.get("value") or "")
        elif answer.get("kind") == CONFLICT_TEXT_ABSENT and answer.get("option") == OPT_LEFT_OUT:
            digest = answer.get("input_digest")
            if isinstance(digest, str):
                for req_id in ids:
                    left_out.setdefault(req_id, set()).add(digest)
    if not gone and not left_out:
        return
    for req in _live(brief):
        base = changed.get(req.id, req)
        if not base.live:
            continue
        if req.id in gone and base.kind == "order":
            updated = _without_pins(base, gone[req.id])
            if updated is not base:
                changed[req.id] = updated
        elif base.kind == "text" and _text_digest(base) in left_out.get(req.id, set()):
            changed[req.id] = base.model_copy(update={"status": "superseded"})
