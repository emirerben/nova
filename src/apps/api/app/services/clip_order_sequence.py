"""The creator's stated clip sequence ("start with X, then Y, end with Z") as ONE rule.

A resolved ``order`` clip intent with a ``first`` / ``last`` position (or neither, "then Y")
and no ``order_by`` describes where a group of clips sits on top of the brief's basis order
(filming time). Two components must agree on where those clips land or the render is a
dead end: the unified montage planner that lays the clips out, and the render contract that
pins the order the finished edit is verified against (KRI-503: the planner honoured "the
blue video first" while the contract still pinned pure filming order, so the edit that did
what the creator said was refused after rendering). Both read the seating from here.

Pure functions over ids: no clips, no settings, no I/O.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, TypeVar

from app.kria.brief_route import loose_text

# (position, name, member ids, status): the order intents, listed order.
SequenceRow = tuple[str | None, str, list[str], str]
SequenceGroupKey = tuple[str | None, str, tuple[str, ...]]

# What a group's render outcome says when nobody said how its clips are ordered inside, in
# the creator's terms (Leverkusen M1 run 2: "this saved group" meant nothing to a creator).
UNCONFIRMED_MEMBER_ORDER = "you didn't say what order its clips play in"

T = TypeVar("T")


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value or "")).strip()


def _members(intent: Mapping[str, Any]) -> list[str]:
    return [
        str(a["media_id"])
        for a in intent.get("assignments") or []
        if isinstance(a, Mapping) and a.get("media_id")
    ]


def _sequence(value: object) -> Sequence[Any]:
    """Any list / tuple of intents (never a string), like the planner always accepted."""
    return value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else []


def unresolved_questions(resolved_intents: object) -> list[str]:
    """What to ask for each sequence intent the server could not resolve: the intent's own
    question when it has one, else its words, else nothing (the caller says it generically)."""
    out: list[str] = []
    for intent in _sequence(resolved_intents):
        if not isinstance(intent, Mapping) or intent.get("op") != "order":
            continue
        if intent.get("order_by") or intent.get("placeholder"):
            continue
        if intent.get("position") not in (None, "first", "last"):
            continue
        if str(intent.get("status") or "resolved") == "resolved":
            continue
        question = _nfc(intent.get("question"))
        if question:
            out.append(question)
            continue
        words = _nfc(intent.get("attribute"))
        if words:
            out.append(unclear_sequence_words(words))
    return out


def unclear_sequence_words(words: str) -> str:
    """A described sequence the server could not place, in the chat's language (KRI-795:
    the render worker writes English, and the chat re-words it with this same function)."""
    from app.kria.reply_language import say  # noqa: PLC0415 - keeps this module import-light

    return say(
        en=f"I couldn't tell which clips \"{words}\" means, so I can't confirm the order.",
        tr=(
            f'"{words}" derken hangi klipleri kastettiğini anlayamadım, '
            "o yüzden sırayı doğrulayamıyorum."
        ),
    )


ANCHOR_FACTS = (("first_clip", "first"), ("last_clip", "last"))


def stated_anchors(brief: object) -> dict[str, str]:
    """The clips a brief order requirement says go first / last, by the creator's words.

    KRI-522: "chronological, starting with the blue video" is stored as ``first_clip`` on
    the order requirement, so it outlives the turn whose planner resolved it. Returns
    ``{"first": "the video that is blue"}``; empty when the brief states none.
    """
    live = getattr(brief, "live", None)
    out: dict[str, str] = {}
    for req in live() if callable(live) else ():
        if getattr(req, "kind", None) != "order":
            continue
        facts = getattr(req, "facts", None) or {}
        for key, position in ANCHOR_FACTS:
            words = facts.get(key)
            if isinstance(words, str) and _nfc(words):
                out.setdefault(position, _nfc(words))
    return out


def sequence_rows(resolved_intents: object) -> list[SequenceRow]:
    """The creator's stated sequence: (position, name, member ids, status), listed order.

    Only ``order`` intents with a ``first`` / ``last`` position, or no position and no
    ``order_by`` ("then the beach volleyball"), describe a sequence of groups. A basis
    order (``order_by``: capture time / route) is the brief's, not this one's.
    """
    rows: list[SequenceRow] = []
    for intent in _sequence(resolved_intents):
        if not isinstance(intent, Mapping) or intent.get("op") != "order":
            continue
        if intent.get("order_by") or intent.get("placeholder"):
            continue
        position = intent.get("position")
        if position not in (None, "first", "last"):
            continue
        status = str(intent.get("status") or "resolved")
        members = _members(intent) if status == "resolved" else []
        name = _nfc(intent.get("attribute"))
        if intent.get("ordered_members") is True and members:
            rows.extend((position, name, [member], status) for member in dict.fromkeys(members))
        else:
            rows.append((position, name, members, status))
    return rows


def unconfirmed_order_groups(resolved_intents: object) -> frozenset[SequenceGroupKey]:
    legacy = [
        intent
        for intent in _sequence(resolved_intents)
        if isinstance(intent, Mapping) and intent.get("ordered_members") is None
    ]
    return frozenset(
        (position, name, tuple(members))
        for position, name, members, status in sequence_rows(legacy)
        if status == "resolved" and len(set(members)) > 1
    )


def unconfirmed_order_reason(names: Sequence[str] = ()) -> str:
    """Asks the creator how the clips of the named groups play inside, in their own words."""
    from app.kria.reply_language import say  # noqa: PLC0415

    quoted = ", ".join(f'"{name}"' for name in dict.fromkeys(_nfc(n) for n in names) if name)
    return say(
        en=(
            f"You didn't say what order the clips in {quoted} should play in. "
            if quoted
            else "You didn't say what order these clips should play in. "
        )
        + "Tell me the order, or say any order is fine.",
        tr=(
            f"{quoted} içindeki kliplerin hangi sırayla oynayacağını söylemedin. "
            if quoted
            else "Bu kliplerin hangi sırayla oynayacağını söylemedin. "
        )
        + "Sırayı yaz ya da herhangi bir sıranın olur olduğunu söyle.",
    )


def seat_owners(
    rows: Sequence[SequenceRow], present: Collection[str] | None = None
) -> dict[str, int]:
    """The row that seats each clip: the FIRST first / last row naming it, else the first
    ``then`` row naming it (KRI-764, KRI-1109).

    A pin names one place for its clips, a positionless group only gathers them, so a pin
    takes its clip out of a group however the planner listed the two (Leverkusen M3: the
    whole match as one group, then "ending on IMG_1043"). Between two pins the earlier
    claim stands. ``present`` limits the clips considered; None considers them all.
    """
    owner: dict[str, int] = {}
    for pinned in (True, False):
        for at, (position, _name, members, status) in enumerate(rows):
            if status != "resolved" or (position is not None) != pinned:
                continue
            for member in members:
                if present is None or member in present:
                    owner.setdefault(member, at)
    return owner


def row_relations(rows: Sequence[SequenceRow], present: Collection[str] | None = None) -> list[str]:
    """How each row relates to the others, by the rule ``apply_sequence`` seats by
    (``seat_owners``).

    * ``new``: the row seats a clip (or names none at all, e.g. unresolved).
    * ``repeat``: a first / last row whose clips an earlier row already seated at that same
      spot. The creator said it twice (a description, then the clip's file name): satisfied.
    * ``conflict``: a first / last row whose clips an earlier pin seated at the other end.
      One clip cannot be both first and last; the earlier claim stands.
    * ``contained``: a ``then`` group made only of clips other rows seat.

    ``present`` limits the clips considered (the ones in the edit); None considers them all.
    """
    owner = seat_owners(rows, present)
    out: list[str] = []
    for at, (position, _name, members, status) in enumerate(rows):
        mine = [m for m in members if status == "resolved" and (present is None or m in present)]
        if not mine or any(owner[m] == at for m in mine):
            out.append("new")
        elif position is None:
            out.append("contained")
        elif {rows[owner[m]][0] for m in mine} == {position}:
            out.append("repeat")
        else:
            out.append("conflict")
    return out


def apply_sequence(
    ordered: Sequence[T],
    rows: Sequence[SequenceRow],
    key: Callable[[T], str] | None = None,
) -> list[T]:
    """Seat the described groups: ``first`` ones lead, ``last`` ones close, the rest follow.

    A group is the items the server matched to the creator's words (clip facts), kept in
    the order they already had. An item named by two groups belongs to the one
    ``seat_owners`` gives it: a first / last group before a ``then`` group, else the first
    one listed. Items no group names keep their relative order between the leading and the
    closing groups.
    """
    ident: Callable[[T], str] = key or (lambda item: str(item))
    buckets: dict[str, list[T]] = {"first": [], "mid": [], "last": []}
    owner = seat_owners(rows, present={ident(item) for item in ordered})
    for at, (position, _name, _members, _status) in enumerate(rows):
        wanted = {m for m, row in owner.items() if row == at}
        buckets[position or "mid"].extend(item for item in ordered if ident(item) in wanted)
    if not owner:
        return list(ordered)
    rest = [item for item in ordered if ident(item) not in owner]
    return [*buckets["first"], *buckets["mid"], *rest, *buckets["last"]]


# ── moments of one take (KRI-796) ─────────────────────────────────────────────
# "Six moments from it, in this order: A, B, C ... put the photo right after B": one take
# cut into windows (KRI-596 / KRI-776), and a Visual the creator seats among them. Group
# seating above cannot say it: every moment is the same clip, and Visuals are no clip.

# Words that say where a Visual goes relative to the moment named after them.
_AFTER, _BEFORE = "after", "before"
# Words that never tell two moments apart: "right after HER hello wave" is the hello wave.
_FILLER = frozenset(
    {"a", "an", "the", "her", "his", "their", "its", "my", "our", "your", "s"}
    | {"right", "just", "straight", "immediately", "directly", "then", "and"}
)


@dataclass(frozen=True)
class MomentSeats:
    """Where the creator put things among the moment windows of ONE take.

    The approved length plan cut ``take`` into ``moments`` windows in take order; the
    creator named that many moments, so the i-th named moment is the i-th window. Which
    window shows which moment is the length plan's choice, not this rule's.

    ``after``: Visual id -> the moment (1-based) it plays right after, or 0 for an explicit
    first pin. ``named``: sequence row index -> the moment that row names. ``placed``: sequence row
    index -> (moment, its Visual ids) for each row that seated a Visual. ``listed`` (KRI-723):
    sequence row index -> the moments one row lists ("A, B, C"); ``skipped``: rows whose
    every moment the creator said is not in their clips. ``positions`` retains explicit
    boundary groups, whose first/last placement also surrounds unrelated Visuals.
    """

    take: str
    moments: int
    after: dict[str, int] = field(default_factory=dict)
    named: dict[int, int] = field(default_factory=dict)
    placed: dict[int, tuple[int, tuple[str, ...]]] = field(default_factory=dict)
    listed: dict[int, tuple[int, ...]] = field(default_factory=dict)
    skipped: tuple[int, ...] = ()
    conflicted: tuple[int, ...] = ()
    positions: dict[int, Literal["first", "last"]] = field(default_factory=dict)


def _words(text: object) -> list[str]:
    """Folded words (Turkish case + diacritics), split at anything not a letter or digit."""
    folded = loose_text(str(text or ""))
    return "".join(ch if ch.isalnum() else " " for ch in folded).split()


def _content(words: Sequence[str]) -> frozenset[str]:
    return frozenset(word for word in words if word not in _FILLER)


def _listed_moments(name: str) -> list[frozenset[str]]:
    """The moments one row lists, comma by comma ("A, B, and C" is three)."""
    items = [_content(_words(part)) for part in name.replace(";", ",").split(",")]
    return [item for item in items if item]


def _anchored_seat(name: str, moments: Sequence[frozenset[str]]) -> tuple[bool, int | None]:
    """(names an anchor, the moment it plays right after) for "the photo right after B".

    The anchor is the words after the one "after" / "before" in ``name``; it names the
    moment whose words hold it (or that it holds, so "after the hello wave for 1 second"
    still finds the hello wave). No moment, or two of them, is no seat: never a guess.
    """
    words = _words(name)
    cues = [i for i, word in enumerate(words) if word in (_AFTER, _BEFORE)]
    if len(cues) != 1:
        return bool(cues), None
    anchor = _content(words[cues[0] + 1 :])
    if not anchor:
        return True, None
    hits = [i for i, item in enumerate(moments) if anchor <= item or item <= anchor]
    if len(hits) != 1:
        return True, None
    return True, hits[0] + 1 if words[cues[0]] == _AFTER else hits[0]


def moment_seats(
    rows: Sequence[SequenceRow],
    *,
    windows: Mapping[str, int],
    visuals: Collection[str],
    pinned: Sequence[tuple[int, str]] | None = None,
    skipped_rows: Collection[int] = (),
) -> MomentSeats | None:
    """The creator's sequence read as moments of ONE take, else None (group seating stands).

    ``windows``: clip id -> how many approved windows the length plan cut it into.
    ``visuals``: the ids of the Visuals in this edit. It applies only when every row is a
    resolved row naming either that one take or only Visuals, with positional pins only on
    Visuals. The rows name exactly the take's windows: one listed row ("A, B, C"), or one row
    per moment. A Visual's row seats it after the moment its own words anchor ("the photo
    right after B"); with one row per moment, a row with no anchor seats it where it is
    listed. A Visual before the first moment is seated only with an explicit first pin.

    ``pinned`` (KRI-723): the approved plan's named moments as (row index, the creator's words),
    in the order their windows play (the creator's final order, skipped ones left out). They
    replace the rows' listing, so the i-th window is the i-th pinned moment by construction,
    not by assumption. ``skipped_rows``: rows the creator said are not in their clips.
    """
    if not rows or any(status != "resolved" for _pos, _n, _m, status in rows):
        return None
    footage = {m for _p, _n, members, _s in rows for m in members if m not in visuals}
    if len(footage) != 1:
        return None
    take = next(iter(footage))
    count = windows.get(take, 0)
    take_rows = [i for i, (_p, _n, members, _s) in enumerate(rows) if set(members) == {take}]
    visual_rows = [
        i
        for i, (_p, _n, members, _s) in enumerate(rows)
        if members and set(members) <= set(visuals)
    ]
    if len(take_rows) + len(visual_rows) != len(rows):
        return None
    if any(rows[index][0] is not None for index in take_rows):
        return None
    listed: dict[int, tuple[int, ...]] = {}
    if pinned is not None:
        moments = [_content(_words(words)) for _row, words in pinned]
        owners: dict[int, list[int]] = {}
        for moment, (row, _words_) in enumerate(pinned, start=1):
            owners.setdefault(row, []).append(moment)
        named = {row: own[0] for row, own in owners.items() if len(own) == 1 and len(take_rows) > 1}
        listed = {row: tuple(own) for row, own in owners.items() if row not in named}
    elif len(take_rows) == 1:
        moments = _listed_moments(rows[take_rows[0]][1])
        named = {}
        listed = {take_rows[0]: tuple(range(1, len(moments) + 1))}
    else:
        moments = [_content(_words(rows[i][1])) for i in take_rows]
        named = {row: moment for moment, row in enumerate(take_rows, start=1)}
    if count < 2 or len(moments) != count:
        return None
    seats = MomentSeats(
        take=take,
        moments=count,
        named=named,
        listed=listed,
        skipped=tuple(row for row in take_rows if row in set(skipped_rows)),
    )
    conflicts: list[int] = []
    for index in visual_rows:
        position = rows[index][0]
        anchored, seat = _anchored_seat(rows[index][1], moments)
        if position in {"first", "last"}:
            explicit_seat = 0 if position == "first" else count
            if anchored and seat != explicit_seat:
                conflicts.append(index)
                continue
            seat = explicit_seat
        elif not anchored and named:
            # One row per moment, the Visual listed among them: after the moment of the
            # nearest take row above it (the creator's final order may have moved it).
            above = [row for row in take_rows if row < index and row in named]
            seat = named[max(above)] if above else None
        if seat is None or not 0 <= seat <= count or (seat == 0 and position != "first"):
            continue
        members = tuple(rows[index][2])
        seats.placed[index] = (seat, members)
        if position in {"first", "last"}:
            seats.positions[index] = position
        for member in members:
            seats.after.setdefault(member, seat)
    seats = replace(seats, conflicted=tuple(conflicts))
    return seats if seats.after or seats.named or seats.listed else None
