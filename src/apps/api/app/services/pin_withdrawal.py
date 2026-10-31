"""After a reply stuck on an unplaced first / last clip, "skip that" withdraws it (KRI-1068).

Leverkusen–Nürnberg M1 (thread 4144DA86, 2026-10-29): "chronological order from arrival to
the final whistle" stored ``last_clip: "the final whistle"`` on the order requirement. No clip
was seated last, so the draft backstop's render contract refused: "I couldn't tell which clip
"the final whistle" means, so I can't confirm the order." The creator answered "Okay make it
simplier" and got the identical sentence back, then "Okay skip that part". The brief kept the
clip through both: a same-kind change keeps first / last clips (``STICKY_FACTS``, KRI-522),
and "simpler" only answers a simpler-version offer, which was never made.

The stuck reply records which brief clips it refused on (``pin_block_coverage``, kept on the
reply's ``brief_coverage``). On the creator's very next message, a short reply that only says
to drop it ("skip that part", "never mind", "forget the ending", "make it simpler", "boşver")
becomes a server-owned brief change without those clips, worded as the KRI-1025 None answer
words it, so the next brief version shows them gone and the draft says so. A longer or
different message is left to the planner, so a later rewording still keeps the clip (KRI-522).

When the same stuck reply would go out twice in a row, it names the ways forward instead
(``unrepeated_reply``).

The same words typed right after the clip picker that asks about the ending (KRI-1067) are that
picker's Skip (``typed_picker_skip``): the server answers it exactly as a tap would, so the
draft ends on the last clip filmed and says so (KRI-1025) instead of asking again.

Pure functions over the brief, the thread's events and the creator's text: no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.kria.brief import BriefUpdate, BriefUpdateBatchError, CreativeBrief, apply_updates
from app.kria.reply_language import reply_language_for, say
from app.services.choice_questions import _closes_open_question, _live, loose_reply
from app.services.clip_none_answers import (
    _FACT_OF,
    _content,
    _order_basis,
    _pin_notice,
    _without_pins,
)
from app.services.clip_order_sequence import ANCHOR_FACTS
from app.services.clip_selection import ClipSelectionIn, attribute_key

BLOCKED_PINS_KEY = "blocked_pins"
_POSITIONS = frozenset(_FACT_OF)


def _phrases(*texts: str) -> frozenset[str]:
    return frozenset(loose_reply(text) for text in texts)


# Words around the answer that change nothing ("Okay skip that part", "tamam o kısmı atla").
_FILLERS = _phrases(
    "ok", "okay", "okey", "oki", "k", "fine", "alright", "all right", "yes", "yeah", "yep",
    "sure", "then", "just", "please", "pls", "plz", "so", "well", "hmm", "thanks", "thank you",
    "tamam", "peki", "olur", "evet", "o zaman", "hadi", "lütfen", "teşekkürler", "sağol",
)  # fmt: skip
# Whole answers that drop whichever clip the reply was stuck on.
_ANY = _phrases(
    "skip", "skip it", "skip that", "skip this", "skip that part", "skip this part",
    "skip the part", "skip that one", "drop it", "drop that", "drop this", "drop that part",
    "never mind", "nevermind", "nvm", "never mind that", "forget it", "forget that",
    "forget about it", "forget about that", "forget that part", "leave it out",
    "leave that out", "leave that part out", "ignore it", "ignore that", "ignore that part",
    "don't worry about it", "don't worry about that", "doesn't matter", "it doesn't matter",
    "it does not matter", "go without it", "do it without it", "do it without that",
    "make it without it", "make it without that", "go ahead without it",
    "continue without it", "make it simpler", "make it simplier", "make it simple",
    "make it more simple", "simpler", "simplier", "keep it simple", "simplify", "simplify it",
    "simpler version", "the simpler version", "make a simpler version",
    "make the simpler version", "do the simpler version",
    "boşver", "boş ver", "boşver onu", "onu boşver", "geç", "onu geç", "geç onu", "atla",
    "onu atla", "atla onu", "o kısmı atla", "o kısmı geç", "o kısmı boşver", "o kısmı çıkar",
    "onu çıkar", "çıkar onu", "unut", "unut onu", "onu unut", "unut gitsin", "önemli değil",
    "farketmez", "fark etmez", "gerek yok", "daha basit yap", "basit yap", "daha basit olsun",
    "basit olsun", "daha basit hazırla", "sadeleştir", "basitleştir", "daha sade yap",
    "onsuz yap", "onsuz hazırla", "onsuz devam et", "onsuz olsun",
)  # fmt: skip
_VERBS_EN = (
    "skip", "drop", "forget", "forget about", "ignore", "never mind", "remove", "leave out",
    "don't worry about",
)  # fmt: skip
_VERBS_TR = ("boşver", "boş ver", "atla", "unut", "geç", "çıkar")
_OBJECTS = {
    "last": (
        ("the ending", "the end", "ending", "the last clip", "the last part", "the last shot",
         "the final part", "the final shot", "the final clip", "the closing shot",
         "the end part", "the ending clip"),
        ("sonu", "sonunu", "bitişi", "bitişini", "son klibi", "son kısmı"),
    ),
    "first": (
        ("the opening", "the start", "the beginning", "the first clip", "the first part",
         "the first shot", "the intro", "the opening shot", "the opener", "the opening clip"),
        ("başı", "başını", "açılışı", "girişi", "ilk klibi", "ilk kısmı", "başlangıcı"),
    ),
}  # fmt: skip
_AT = {
    position: _phrases(
        *(f"{verb} {thing}" for verb in _VERBS_EN for thing in en),
        *(f"leave {thing} out" for thing in en),
        *(f"{thing} {verb}" for verb in _VERBS_TR for thing in tr),
    )
    for position, (en, tr) in _OBJECTS.items()
}
_VERBS = tuple(sorted(_phrases(*_VERBS_EN, *_VERBS_TR), key=len, reverse=True))
_CLAUSE_BREAK = re.compile(r"[,.;!?\n]+")


@dataclass(frozen=True)
class PinWithdrawal:
    updates: tuple[BriefUpdate, ...]
    notices: tuple[str, ...] = ()


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


def _unpadded(clause: str) -> str:
    """``clause`` (already ``loose_reply``) without filler words at either end."""
    words = clause.split()
    changed = True
    while words and changed:
        changed = False
        for size in (2, 1):
            if len(words) >= size and " ".join(words[:size]) in _FILLERS:
                words, changed = words[size:], True
                break
            if len(words) >= size and " ".join(words[-size:]) in _FILLERS:
                words, changed = words[:-size], True
                break
    return " ".join(words)


def _clause_positions(clause: str, stuck: Iterable[Mapping[str, Any]]) -> frozenset[str] | None:
    if clause in _ANY:
        return _POSITIONS
    hit = frozenset(position for position, phrases in _AT.items() if clause in phrases)
    if hit:
        return hit
    # "forget the final whistle": a drop verb and the stuck clip's own words.
    for verb in _VERBS:
        rest = clause.removeprefix(verb + " ") if clause.startswith(verb + " ") else None
        if rest is None and clause.endswith(" " + verb):
            rest = clause.removesuffix(" " + verb)  # Turkish puts the verb last
        if rest:
            named = frozenset(
                str(pin.get("position"))
                for pin in stuck
                if _content(rest) and _content(rest) == _content(pin.get("words"))
            )
            if named:
                return named
    return None


def _positions(message: str, stuck: Iterable[Mapping[str, Any]] = ()) -> frozenset[str] | None:
    """The first / last spots a whole reply withdraws, or ``None`` when it says more or else.

    Every clause must be a withdrawal or filler: "make it simpler and add a title" or "skip
    that part of the video where I'm talking" are requests for the planner, never this.
    """
    stuck = list(stuck)
    clauses = [_unpadded(loose_reply(c)) for c in _CLAUSE_BREAK.split(str(message or ""))]
    clauses = [c for c in clauses if c]
    if not clauses:
        return None
    out: set[str] = set()
    for clause in clauses:
        positions = _clause_positions(clause, stuck)
        if positions is None:
            return None
        out |= positions
    return frozenset(out)


def withdrawal_positions(message: str) -> frozenset[str] | None:
    """The spots a reply like "skip it" / "forget the ending" withdraws (``None``: not one)."""
    return _positions(message)


def withdrawal_candidate(message: str) -> bool:
    """Cheap syntax filter only: every clause is a withdrawal or starts or ends with a drop
    verb. A named object ("forget the final whistle") still has to match the picker's clip."""
    clauses = [_unpadded(loose_reply(c)) for c in _CLAUSE_BREAK.split(str(message or ""))]
    clauses = [clause for clause in clauses if clause]
    return bool(clauses) and all(
        _clause_positions(clause, ()) is not None
        or any(clause.startswith(verb + " ") or clause.endswith(" " + verb) for verb in _VERBS)
        for clause in clauses
    )


def _pin_sentences(words: str) -> tuple[str, ...]:
    from app.services.creator_render_contract import _pin_not_found  # noqa: PLC0415

    out = []
    for language in ("en", "tr"):
        with reply_language_for(language):
            out.append(_flat(_pin_not_found(words)))
    return tuple(out)


def pin_block_coverage(brief: Any, reply: str | None) -> dict[str, list[dict[str, str]]]:
    """``{"blocked_pins": [...]}`` for each brief first / last clip ``reply`` refused on.

    Found by the render contract's own sentence for that clip (``_pin_not_found``, either
    language), so a reply about something else records nothing. Stored on the reply's
    ``brief_coverage`` for the next turn to read.
    """
    text = _flat(reply)
    pins: list[dict[str, str]] = []
    for req in _live(brief):
        if getattr(req, "kind", None) != "order":
            continue
        for fact, position in ANCHOR_FACTS:
            words = _nfc((req.facts or {}).get(fact)) if isinstance(req.facts, Mapping) else ""
            if words and any(sentence in text for sentence in _pin_sentences(words)):
                pins.append({"requirement_id": req.id, "position": position, "words": words})
    return {BLOCKED_PINS_KEY: pins} if pins else {}


def _stuck_pins(events: Iterable[tuple[str, Any]]) -> list[Mapping[str, Any]]:
    """The clips the last reply before the creator's newest message was stuck on."""
    events = list(events)
    newest = max((i for i, (role, _payload) in enumerate(events) if role == "user"), default=None)
    if newest is None:
        return []
    for role, payload in reversed(events[:newest]):
        if role == "user":
            return []  # another message came in between: this one is not the answer to it
        if role != "assistant" or not isinstance(payload, Mapping):
            continue
        if not _closes_open_question(payload):
            continue  # a render finishing or a memory write is not a reply
        coverage = payload.get("brief_coverage")
        pins = coverage.get(BLOCKED_PINS_KEY) if isinstance(coverage, Mapping) else None
        return [pin for pin in pins or () if isinstance(pin, Mapping)]
    return []


def _withdrawal_update(req: Any, pins: Mapping[str, str], version: int) -> BriefUpdate:
    """The brief update that leaves ``pins`` out of ``req``: a change, or a remove when
    nothing else was asked of it ("end on the final whistle" alone)."""
    without = _without_pins(req, dict(pins), withdrawn=True)
    if not without.live:
        return BriefUpdate(
            operation="remove", target_requirement_id=req.id, expected_version=version
        )
    return BriefUpdate(
        operation="change",
        target_requirement_id=req.id,
        expected_version=version,
        kind=without.kind,
        scope=without.scope,
        literal=without.literal,
        description=without.description,
        # Null withdraws a sticky fact; a plain omission would be carried back (KRI-522).
        facts={**without.facts, **{_FACT_OF[position]: None for position in pins}},
    )


def withdraw_stuck_pins(
    prior: CreativeBrief | None,
    updates: Iterable[BriefUpdate],
    events: Iterable[tuple[str, Any]],
    message: str | None,
) -> PinWithdrawal:
    """This turn's brief updates with the stuck clips the creator just dropped left out.

    Only right after a reply stuck on them (``_stuck_pins``), only for a reply that says
    nothing but to drop them (``_positions``), and only while the brief still names the same
    words (a clip named since then stays). The model's own change of that requirement this
    turn is kept, without the dropped clips; its remove stands as it is.
    """
    updates = tuple(updates)
    unchanged = PinWithdrawal(updates)
    if prior is None or not message:
        return unchanged
    stuck = _stuck_pins(events)
    positions = _positions(message, stuck) if stuck else None
    if not positions:
        return unchanged
    live = {req.id: req for req in prior.live()}
    gone: dict[str, dict[str, str]] = {}
    for pin in stuck:
        req = live.get(str(pin.get("requirement_id") or ""))
        position, words = str(pin.get("position")), _nfc(pin.get("words"))
        if req is None or req.kind != "order" or position not in positions:
            continue
        if not _content(words) or _content(req.facts.get(_FACT_OF[position])) != _content(words):
            continue
        gone.setdefault(req.id, {})[position] = words
    if not gone:
        return unchanged
    own = {u.target_requirement_id: u for u in updates if u.operation != "add"}
    out = [u for u in updates if u.operation == "add" or u.target_requirement_id not in gone]
    notices: list[str] = []
    for req_id, pins in gone.items():
        req, mine = live[req_id], own.get(req_id)
        if mine is not None and mine.operation == "remove":
            out.append(mine)
        else:
            if mine is not None:
                try:
                    changed = apply_updates(prior, [mine], source_turn_id=None)
                except BriefUpdateBatchError:
                    return unchanged  # the model's own change is refused downstream as before
                req = next(r for r in changed.live() if r.id == req_id)
                pins = {  # noqa: PLW2901 - the model may have named another clip this turn
                    position: words
                    for position, words in pins.items()
                    if _content(req.facts.get(_FACT_OF[position])) == _content(words)
                }
                if not pins:
                    out.append(mine)
                    continue
            out.append(_withdrawal_update(req, pins, prior.version))
        basis = _order_basis(req)
        notices.extend(
            _pin_notice(position, words, basis, skipped=False, withdrawn=True)
            for position, words in pins.items()
        )
    return PinWithdrawal(tuple(out), tuple(dict.fromkeys(notices)))


def _ways_forward(position: str, words: str, basis: str | None) -> str:
    from app.services.creator_render_contract import _pin_not_found  # noqa: PLC0415

    last = position == "last"
    if basis is None:
        then = say(en="leave it out", tr="onu çıkarayım")
    else:
        filmed = basis == "filmed"
        clip = say(
            en=f"the {'last' if last else 'first'} clip you {'filmed' if filmed else 'added'}",
            tr=f"{'çektiğin' if filmed else 'eklediğin'} {'son' if last else 'ilk'} klip",
        )
        then = (
            say(en=f"leave it out and end on {clip}", tr=f"onu çıkarıp videoyu {clip}te bitireyim")
            if last
            else say(
                en=f"leave it out and start with {clip}",
                tr=f"onu çıkarıp videoya {clip} ile başlayayım",
            )
        )
    ways = say(
        en=f'You can tell me which clip shows it, or reply "skip it" and I\'ll {then}.',
        tr=f'Hangi klipte olduğunu söyleyebilirsin ya da "boşver" yaz, {then}.',
    )
    return f"{_pin_not_found(words)} {ways}"


def unrepeated_reply(reply: str, previous: str | None, brief: Any) -> str:
    """``reply``, unless it is word for word the reply just sent; then the ways forward.

    A reply stuck on a clip names it again and offers both ways out, naming the words that
    drop it; anything else says it is still stuck and asks what to change or leave out. The
    reply after that may be the first wording again, never one wording twice in a row.
    """
    if not _flat(reply) or _flat(reply) != _flat(previous):
        return reply
    pins = pin_block_coverage(brief, reply).get(BLOCKED_PINS_KEY) or []
    if pins:
        live = {req.id: req for req in _live(brief)}
        unchanged = say(en="Your current draft is unchanged.", tr="Mevcut taslağın değişmedi.")
        lines = [
            _ways_forward(pin["position"], pin["words"], _order_basis(live[pin["requirement_id"]]))
            for pin in pins
        ]
        return " ".join([*lines, *([unchanged] if unchanged in reply else [])])
    return " ".join(
        (
            reply,
            say(
                en="I'm still stuck on the same thing. Tell me what to change or leave out, and "
                "I'll try again.",
                tr="Hâlâ aynı yere takıldım. Neyi değiştireyim ya da neyi çıkarayım söyle, "
                "yeniden deneyeyim.",
            ),
        )
    )


# Questions that share a reply with the clip picker; a typed word could answer any of them.
_OTHER_QUESTIONS = (
    "choice_question",
    "song_order_question",
    "song_range_question",
    "moment_order_question",
    "protected_region_question",
)


def _ending_picker(events: list[tuple[str, Any]]) -> tuple[Mapping[str, Any], str, int] | None:
    """The picker the creator is answering: ``(question, category key, its event index)``.

    Only the reply right before this message counts, and only a picker that asks about one
    first / last clip and nothing else.
    """
    for index in range(len(events) - 1, -1, -1):
        role, payload = events[index]
        if role == "user":
            return None  # the reply was already answered, or another message came in between
        if role != "assistant" or not isinstance(payload, Mapping):
            continue
        if not _closes_open_question(payload):
            continue  # a render finishing or a memory write is not the question
        question = payload.get("clip_question")
        if any(payload.get(key) for key in _OTHER_QUESTIONS) or not isinstance(question, Mapping):
            return None
        categories = question.get("categories") or []
        if not question.get("question_id") or len(categories) != 1:
            return None
        category = categories[0]
        if category.get("op") != "order" or category.get("also_answers"):
            return None
        return question, str(category.get("key") or ""), index
    return None


def typed_picker_skip(
    brief: CreativeBrief | None, events: Iterable[tuple[str, Any]], message: str
) -> ClipSelectionIn | None:
    """KRI-1066: a whole typed withdrawal right after the ending picker is that picker's Skip.

    ``events`` are the thread's ``(role, payload)`` before this message. The picker must ask
    about exactly one brief first / last clip, every clause of the message must be a
    withdrawal that covers that spot, and the brief's clip must have been stated before the
    picker asked. The result is the same ``ClipSelectionIn`` a tap on Skip sends, so
    everything after it is the tap's path. The message itself still reaches the planner as
    usual: another spot it also drops ("skip that, forget the opening") is read there.
    """
    if brief is None or not withdrawal_candidate(message):
        return None
    events = list(events)
    picker = _ending_picker(events)
    if picker is None:
        return None
    question, key, picker_index = picker
    pins = [
        (req, position, words)
        for req in brief.live()
        if req.kind == "order"
        for fact, position in ANCHOR_FACTS
        if (words := _nfc((req.facts or {}).get(fact))) and attribute_key("order", words) == key
    ]
    if len(pins) != 1:
        return None
    req, position, words = pins[0]
    if position not in (_positions(message, [{"position": position, "words": words}]) or ()):
        return None
    stated_before = any(
        role == "user"
        and isinstance(payload, Mapping)
        and req.source_turn_id
        and payload.get("turn_id") == req.source_turn_id
        for role, payload in events[:picker_index]
    )
    if not stated_before:
        return None
    return ClipSelectionIn(question_id=str(question["question_id"]), skipped=True)
