"""The creator's clip order as ONE live, event-folded state (``KRIA_CLIP_ORDER_LEDGER_ENABLED``).

Before this, the order lived in three places that could disagree: the clips as the creator
attached them, the `order` rows of whatever they had asked for in earlier messages (re-resolved
and re-applied on every plan), and the rendered timeline. A drag or a message after a render
was never in the AI's view, and an old row could undo it on the next re-plan.

The model here is small:

* ``LedgerEntry``: one change of the order, whatever produced it (``actor``): the creator's
  message, a drag, a manual timeline edit, the AI's own story order, or the server's upload-order
  default. Entries are folded from the assistant events of the thread (the way ``song_order`` is),
  so nothing new is stored.
* No lock. A later statement replaces an earlier one; nothing here refuses a change.
* ``current_clip_order``: the ONE reader. After a render it is the first-appearance media order of
  the editor timeline; before one it is the ledger replayed over the order the clips were added.
* Every change is normalised to one full sequence of media ids (``apply_order``), so chat, drag
  and manual edits produce identical state.

Pure functions over ids: no clips, no settings reads beyond the one flag helper, no I/O.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from app.kria.reply_language import say

LEDGER_KEY = "clip_order_entries"

SERVER_DEFAULT = "server_default"
CREATOR_TEXT = "creator_text"
CREATOR_DRAG = "creator_drag"  # the order card's drag (`clip_order_card`)
CREATOR_EDITOR = "creator_editor"
AI_STORY = "ai_story"
ACTORS = (SERVER_DEFAULT, CREATOR_TEXT, CREATOR_DRAG, CREATOR_EDITOR, AI_STORY)
CREATOR_ACTORS = frozenset({CREATOR_TEXT, CREATOR_DRAG, CREATOR_EDITOR})

Basis = Literal["attachment_order", "confirmed"]

# A thread never grows more than this many entries; older ones are dropped from the front (the
# fold keeps the replay exact because every entry names the full order it produced).
MAX_ENTRIES = 200
MAX_IDS = 200


def clip_order_ledger_on() -> bool:
    """``KRIA_CLIP_ORDER_LEDGER_ENABLED``: the order is one live, AI-aware state."""
    from app.config import settings  # noqa: PLC0415

    return bool(settings.kria_clip_order_ledger_enabled)


@dataclass(frozen=True)
class LedgerEntry:
    """One change of the clip order: who made it, from which order to which, on which turn.

    ``rows`` are the fingerprints of the creator's `order` rows this change SEATED (never set for
    a drag, an editor edit or the AI), so a later turn knows which rows are already applied.
    """

    actor: str
    before_ids: tuple[str, ...]
    after_ids: tuple[str, ...]
    turn_id: str | None = None
    rows: tuple[str, ...] = field(default=())
    # A drag right after another drag is the same gesture: it takes no undo step of its own.
    coalesce: bool = False
    # KRIA_STORY_SEQUENCER_ENABLED: the clips the story sequencer was asked to place on this
    # entry (`story_order`). Only its own ``ai_story`` entries carry it; an order the plan was
    # merely seen to have laid out (`observed_entry`) never does. The entry is written even when
    # the order did not change, so a later turn knows these clips were already looked at.
    story_ids: tuple[str, ...] = field(default=())

    def to_payload(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "before_ids": list(self.before_ids),
            "after_ids": list(self.after_ids),
            "turn_id": self.turn_id,
            **({"rows": list(self.rows)} if self.rows else {}),
            **({"coalesce": True} if self.coalesce else {}),
            **({"story_ids": list(self.story_ids)} if self.story_ids else {}),
        }

    @classmethod
    def from_payload(cls, raw: object) -> LedgerEntry | None:
        """The entry a stored payload row says, or None when it is not a well-formed one."""
        if not isinstance(raw, Mapping) or raw.get("actor") not in ACTORS:
            return None
        before, after = _ids(raw.get("before_ids")), _ids(raw.get("after_ids"))
        if before is None or after is None or not after:
            return None
        rows = raw.get("rows")
        fingerprints = (
            tuple(str(row) for row in rows if isinstance(row, str))
            if isinstance(rows, list)
            else ()
        )
        turn = raw.get("turn_id")
        story = _ids(raw.get("story_ids")) if raw["actor"] == AI_STORY else None
        return cls(
            str(raw["actor"]),
            before,
            after,
            str(turn) if turn else None,
            fingerprints,
            coalesce=raw.get("coalesce") is True,
            story_ids=story or (),
        )


def _ids(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or len(value) > MAX_IDS:
        return None
    if not all(isinstance(item, str) and item for item in value):
        return None
    return tuple(dict.fromkeys(value))


# ── taking entries back (KRI-1594) ─────────────────────────────────────────────────────────────
#
# A retraction rides the ledger rows of an event, as a row with this actor. `LedgerEntry.
# from_payload` rejects it (the actor is not in ACTORS), so a reader that only replays entries
# skips it, and only `fold_clip_order` acts on it.
RETRACT = "retract"


def retract_turns_row(turn_ids: Iterable[str]) -> dict[str, Any]:
    """A ledger row that takes back every entry the turns ``turn_ids`` wrote, wherever they are
    (a message the creator removed or stopped: its work never counts)."""
    return {"actor": RETRACT, "turn_ids": sorted({str(turn) for turn in turn_ids if turn})}


def reset_row() -> dict[str, Any]:
    """A ledger row that takes back every entry written BEFORE it: the creator restarted their
    request ("start fresh", or a new request after "Change my request") before any render, so the
    order the old words set goes with them and the clips are back in the order they were added."""
    return {"actor": RETRACT, "all": True}


def ledger_events_clause() -> Any:
    """The thread events the ledger is folded from: the assistant events (entries, draft
    checkpoints) and the system ``turn_cancelled`` events (a removed or stopped message's
    retraction). Every reader of the order selects these rows, so they all fold the same history.
    """
    from sqlalchemy import or_  # noqa: PLC0415

    from app.models import CreationThreadEvent  # noqa: PLC0415

    return or_(
        CreationThreadEvent.role == "assistant",
        CreationThreadEvent.event_type == "turn_cancelled",
    )


def _retracted_turns(events: Sequence[tuple[str, Mapping[str, Any] | None]]) -> frozenset[str]:
    out: set[str] = set()
    for role, payload in events:
        if role not in {"assistant", "system"} or not isinstance(payload, Mapping):
            continue
        for raw in payload.get(LEDGER_KEY) or ():
            if isinstance(raw, Mapping) and raw.get("actor") == RETRACT:
                out.update(str(turn) for turn in raw.get("turn_ids") or () if turn)
    return frozenset(out)


def fold_clip_order(
    events: Iterable[tuple[str, Mapping[str, Any] | None]],
) -> tuple[LedgerEntry, ...]:
    """The ledger entries the thread's events carry, in event order, minus the ones taken back.

    ``events`` are ``(role, payload)`` in chronological order (`ledger_events_clause`). A payload
    row that is not a well-formed entry is skipped, never raised on: the ledger describes a
    creator's thread and must not be able to break it. Taken back (KRI-1594):

    * the entries of a turn a ``retract`` row names (`retract_turns_row`), wherever they are;
    * every entry before a reset row (`reset_row`);
    * on a draft undo (an event that names the draft it restored, ``restored_from_draft_id``), the
      ledger goes back to what it was the moment that draft was last the newest one: the undone
      draft's own entries, and anything after them, leave with it. Undoing the undo restores the
      draft that was undone, and its entries with it. A restored draft with no event on record
      (the baseline revision under a first editor update) goes back to what the ledger was just
      before the undone draft's own event.
    """
    rows = [(role, payload) for role, payload in events]
    retracted = _retracted_turns(rows)
    entries: list[LedgerEntry] = []
    head: str | None = None
    # The entries in force just before each draft's event, and when it stopped being the newest.
    made_at: dict[str, tuple[LedgerEntry, ...]] = {}
    left_at: dict[str, tuple[LedgerEntry, ...]] = {}
    for role, payload in rows:
        if role != "assistant" or not isinstance(payload, Mapping):
            continue
        draft = payload.get("draft_id") if "draft_revision" in payload else None
        if isinstance(draft, str) and draft and draft != head:
            restored = payload.get("restored_from_draft_id")
            if head is not None:
                left_at[head] = tuple(entries)
            # Restoring the draft that is still the newest one (the undone draft had no event of
            # its own, such as one the app saved) leaves the ledger as it is.
            if isinstance(restored, str) and restored and restored != head:
                if restored in left_at:
                    entries = list(left_at[restored])
                elif head is not None and head in made_at:
                    entries = list(made_at[head])
            made_at.setdefault(draft, tuple(entries))
            head = draft
        raw_rows = payload.get(LEDGER_KEY)
        if not isinstance(raw_rows, list):
            continue
        for raw in raw_rows:
            if isinstance(raw, Mapping) and raw.get("actor") == RETRACT:
                if raw.get("all") is True:
                    entries = []
                continue
            entry = LedgerEntry.from_payload(raw)
            if entry is not None and entry.turn_id not in retracted:
                entries.append(entry)
    return tuple(entries[-MAX_ENTRIES:])


# ── one function that applies every order change ──────────────────────────────────────────────


def apply_order(
    current: Sequence[str],
    requested: Sequence[str],
    *,
    known: Collection[str] | None = None,
) -> list[str]:
    """``requested`` laid over ``current``, by media id. The one place an order change happens.

    ``requested`` ids that are not in ``current`` (a removed clip, a stale bundle) are dropped.

    * ``known=None``: a PARTIAL order. The named clips lead in the requested sequence and the
      clips it left out follow in the order they already had ("put the map clip first" and a full
      order that forgot a clip both read this way).
    * ``known`` given: a bundle written against an earlier timeline that knew exactly those ids.
      The named clips take the requested sequence, in the slots the clips known to the bundle
      held; a clip added since (not in ``known``) keeps its slot.
    """
    live = list(dict.fromkeys(current))
    present = set(live)
    named = [item for item in dict.fromkeys(requested) if item in present]
    if known is None:
        return [*named, *(item for item in live if item not in set(named))]
    known_set = set(known) | set(named)
    slots = [index for index, item in enumerate(live) if item in known_set]
    fill = [*named, *(item for item in live if item in known_set and item not in set(named))]
    result = list(live)
    for index, item in zip(slots, fill, strict=True):
        result[index] = item
    return result


def moved_ids(before: Sequence[str], after: Sequence[str]) -> tuple[str, ...]:
    """The clips an order change MOVED: those outside the longest run that kept its sequence.

    Moving one clip past three others moves that clip, not the three it passed.
    """
    common = [item for item in after if item in set(before)]
    position = {item: index for index, item in enumerate(before)}
    seq = [position[item] for item in common]
    # patience-sort longest increasing subsequence of the clips' old positions
    tails: list[int] = []
    tail_at: list[int] = []
    prev: list[int] = [-1] * len(seq)
    for index, value in enumerate(seq):
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if tails[mid] < value:
                lo = mid + 1
            else:
                hi = mid
        if lo == len(tails):
            tails.append(value)
            tail_at.append(index)
        else:
            tails[lo] = value
            tail_at[lo] = index
        prev[index] = tail_at[lo - 1] if lo else -1
    kept: set[int] = set()
    cursor = tail_at[-1] if tail_at else -1
    while cursor != -1:
        kept.add(cursor)
        cursor = prev[cursor]
    return tuple(item for index, item in enumerate(common) if index not in kept)


@dataclass(frozen=True)
class ClipOrderState:
    """What the order is now: the order, who set each card's place, and the step before."""

    order: tuple[str, ...]
    source: Mapping[str, str]
    basis: Basis
    previous: tuple[str, ...] | None
    entries: tuple[LedgerEntry, ...]
    # KRIA_STORY_SEQUENCER_ENABLED: the clips whose place the story sequencer chose and nobody
    # has moved since. Empty until it has changed an order.
    story_ids: frozenset[str] = frozenset()

    def source_of(self, media_id: str) -> str:
        return self.source.get(media_id, SERVER_DEFAULT)


def first_appearance(media_ids: Iterable[object]) -> list[str]:
    """Each media id once, in the order it first plays. A slot with no media id (a legacy row,
    a blank card) is skipped; a take split into several slots counts once."""
    return list(dict.fromkeys(str(item) for item in media_ids if isinstance(item, str) and item))


# Inside `_replay` only: a place the story sequencer chose (`LedgerEntry.story_ids`), told apart
# from an ``ai_story`` order the plan was merely seen to have laid out. Readers get ``ai_story``.
_STORY = "ai_story:sequenced"


def replay(
    attachment_ids: Sequence[str], entries: Sequence[LedgerEntry]
) -> tuple[list[str], dict[str, str], tuple[str, ...] | None]:
    """The ledger replayed over the order the clips were added: (order, source by id, previous).
    See `_replay`; every source is a ledger actor."""
    order, source, previous = _replay(attachment_ids, entries)
    return (
        order,
        {item: AI_STORY if actor == _STORY else actor for item, actor in source.items()},
        previous,
    )


def _replay(
    attachment_ids: Sequence[str], entries: Sequence[LedgerEntry]
) -> tuple[list[str], dict[str, str], tuple[str, ...] | None]:
    """The ledger replayed over the order the clips were added: (order, source by id, previous).

    Each entry names the full order it produced, so a clip added since is kept in place and a
    removed one is dropped (`apply_order` with the entry's own ``before_ids`` as the known set).
    ``previous`` is the order a "undo my reorder" restores: the order before the last change that
    has not been undone. A change that puts back exactly the order before the last one IS that
    undo (no flag needed: ids only), so it takes a step off the history instead of adding one,
    and the next undo goes one step further back.
    """
    order = list(dict.fromkeys(attachment_ids))
    source: dict[str, str] = {}
    history: list[tuple[str, ...]] = []
    for entry in entries:
        if entry.actor == SERVER_DEFAULT:
            # The server put the story sequencer's clips back in upload order
            # (`story_order.withdraw_story`): no clip reads as its choice any more, moved or not.
            source = {item: actor for item, actor in source.items() if actor != _STORY}
        updated = apply_order(order, entry.after_ids, known=entry.before_ids or entry.after_ids)
        if updated == order:
            continue
        for item in moved_ids(order, updated):
            source[item] = entry.actor
        if entry.story_ids:
            # The sequencer placed every clip it was asked about, the ones it left where they
            # were included: all of them read as its choice, not as the upload order.
            present = set(updated)
            source.update({item: _STORY for item in entry.story_ids if item in present})
        if history and tuple(updated) == history[-1]:
            history.pop()
        elif not (entry.coalesce and history):
            history.append(tuple(order))
        order = updated
    return order, source, (history[-1] if history else None)


def current_clip_order(
    *,
    attachment_ids: Sequence[str],
    entries: Sequence[LedgerEntry] = (),
    rendered_ids: Sequence[str] | None = None,
) -> ClipOrderState:
    """THE reader of the creator's clip order.

    After a render (``rendered_ids``: the first-appearance media order of the editor timeline,
    which already holds every drag, manual edit and chat edit) that timeline is the order. Before
    one it is the ledger replayed over ``attachment_ids``. ``basis`` is ``confirmed`` once any
    creator actor touched the order and ``attachment_order`` while nobody did.
    """
    replayed, source, previous = _replay(attachment_ids, entries)
    order = replayed
    if rendered_ids:
        shown = [item for item in dict.fromkeys(rendered_ids)]
        # Clips the timeline does not show (left out of the edit) follow, as the clips were
        # attached: they are in the project and a later "add it back" seats them last.
        order = [*shown, *(item for item in replayed if item not in set(shown))]
    touched = any(entry.actor in CREATOR_ACTORS for entry in entries)
    story = frozenset(item for item, actor in source.items() if actor == _STORY)
    return ClipOrderState(
        tuple(order),
        {item: AI_STORY if actor == _STORY else actor for item, actor in source.items()},
        "confirmed" if touched else "attachment_order",
        previous,
        tuple(entries),
        story_ids=story,
    )


def observed_entry(
    state: ClipOrderState,
    observed_ids: Sequence[str],
    turn_id: str | None,
    *,
    actor: str = CREATOR_EDITOR,
) -> LedgerEntry | None:
    """The change the creator made by hand since the ledger last spoke, as one entry.

    ``observed_ids`` is the timeline the app sent (unsaved edits) or the saved variant's. When
    it differs from the ledger's current order over the same clips, the creator reordered in the
    editor; the difference is recorded as a ``creator_editor`` entry so the next undo can restore
    what it replaced. A timeline nobody edited by hand is the order the plan itself laid out: that
    difference is the AI's (``actor=ai_story``). Clips the timeline does not show are not a reorder.
    """
    shown = [item for item in dict.fromkeys(observed_ids) if item in set(state.order)]
    expected = [item for item in state.order if item in set(shown)]
    if not shown or shown == expected:
        return None
    return LedgerEntry(actor, tuple(expected), tuple(shown), turn_id)


# ── the creator's order rows are applied ONCE ─────────────────────────────────────────────────


def row_fingerprint(position: str | None, words: str) -> str:
    """Identity of an order row across turns: where it seats and the creator's words for it."""
    return f"{position or 'then'}|{' '.join(str(words or '').casefold().split())}"


def applied_known_ids(entries: Iterable[LedgerEntry]) -> frozenset[str]:
    """The clips that existed when an earlier turn seated its order rows."""
    return frozenset(
        item for entry in entries if entry.rows for item in (*entry.before_ids, *entry.after_ids)
    )


def applied_row_fingerprints(entries: Iterable[LedgerEntry]) -> frozenset[str]:
    """The order rows an earlier turn already seated."""
    return frozenset(row for entry in entries for row in entry.rows)


# ── labels: how a clip is named to the creator ────────────────────────────────────────────────

_UNSAFE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|analysis-proxy|\basset-|\blegacy-clip-|\bclip-\d+$"
    r"|\.(?:mp4|mov|m4v|heic|jpe?g|png|webp)\b|\bIMG_\d+|\bMOV_\d+|\bDSC_?\d+",
    re.IGNORECASE,
)
_MAX_LABEL_CHARS = 48


def safe_words(value: object) -> str:
    """A creator-visible name for a clip, or "" when ``value`` would show an id or file name."""
    text = " ".join(str(value or "").split())
    if not text or len(text) > _MAX_LABEL_CHARS or _UNSAFE.search(text):
        return ""
    return text


def clip_label(
    *,
    position: int,
    group_words: object = None,
    note: object = None,
    description: object = None,
) -> str:
    """How one clip is named: the creator's group words, else the note they typed on the upload,
    else a short description from the clip's own record, else ``Clip N`` (N = its play position).

    Never a media id, UUID or file name: a candidate that carries one is skipped for the next.
    """
    for candidate in (group_words, note, description):
        words = safe_words(candidate)
        if words:
            return words
    return f"Clip {position}"


def is_fallback_label(label: str) -> bool:
    return re.fullmatch(r"Clip \d+", label or "") is not None


def clip_phrase(position: int, label: str) -> str:
    """ "your clip 2, “swing video”": the play position paired with the name, always.

    A clip with no name but its number is just "your clip 2" (the number IS its name). The number
    is a digit so every language reads the same sentence shape.
    """
    if is_fallback_label(label):
        return say(en="your clip {position}", tr="{position}. klibin", position=position)
    return say(
        en="your clip {position}, “{label}”",
        tr="{position}. klibin, “{label}”",
        position=position,
        label=label,
    )


def named(label: str) -> str:
    """A clip as a sentence names it: “swing video”, or "clip 3" when it has no name."""
    return label.lower() if is_fallback_label(label) else f"“{label}”"


# ── receipts and replies, built from before/after ids ─────────────────────────────────────────


def reorder_receipt(
    before: Sequence[str],
    after: Sequence[str],
    label_of: Mapping[str, str],
) -> str:
    """One plain sentence for an order change: "Moved “swing video” before “map”.".

    One clip moved: where it went, named by its new neighbour. Several moved: how many, and where
    the first of them now plays. An unchanged order says so. Names only (``label_of``), never ids.
    """
    moved = moved_ids(before, after)
    if not moved:
        return say(en="The clips are already in that order.", tr="Klipler zaten bu sırada.")
    shown = [item for item in after if item in set(before)]

    def label(media_id: str) -> str:
        return label_of.get(media_id) or f"Clip {shown.index(media_id) + 1}"

    if len(moved) == 1:
        item = moved[0]
        at = shown.index(item)
        if at == 0:
            return say(
                en="Moved {moved} to the start.",
                tr="{moved} klibini başa aldım.",
                moved=named(label(item)),
            )
        if at == len(shown) - 1:
            return say(
                en="Moved {moved} to the end.",
                tr="{moved} klibini sona aldım.",
                moved=named(label(item)),
            )
        return say(
            en="Moved {moved} before {anchor}.",
            tr="{moved} klibini {anchor} klibinin önüne aldım.",
            moved=named(label(item)),
            anchor=named(label(shown[at + 1])),
        )
    return say(
        en="Moved {count} clips, starting with {first}.",
        tr="{count} klibi taşıdım, ilki {first}.",
        count=len(moved),
        first=named(label(next(item for item in shown if item in set(moved)))),
    )


def order_listing(order: Sequence[str], label_of: Mapping[str, str], *, story: bool = False) -> str:
    """The current order as a plain numbered list, each number paired with its name.

    ``story``: the story sequencer chose some of these places, so the list says whose order it
    is before it lists it (a build with no card has no badge to say it)."""
    lines = [
        f"{position}. {label_of.get(media_id) or f'Clip {position}'}"
        for position, media_id in enumerate(order, start=1)
    ]
    head = (
        say(
            en=(
                "Where you didn't set the order, I picked the one that tells the story best. "
                "Your clips play in this order:"
            ),
            tr=(
                "Sırayı belirlemediğin yerlerde hikâyeyi en iyi anlatan sırayı ben seçtim. "
                "Kliplerin şu sırayla oynuyor:"
            ),
        )
        if story
        else say(en="Your clips play in this order:", tr="Kliplerin şu sırayla oynuyor:")
    )
    return "\n".join([head, *lines])


def scrub_ids(text: str, label_of: Mapping[str, str]) -> str:
    """``text`` with every media id, UUID or file name replaced by the clip's name.

    The last fence on a creator-visible string: a model that echoes an id it was shown reads as
    the clip's name, and an id it was never shown reads as "a clip".
    """
    out = text
    for media_id, label in sorted(label_of.items(), key=lambda pair: -len(pair[0])):
        if media_id:
            out = out.replace(media_id, named(label))
    return _UNSAFE_TOKEN.sub(say(en="a clip", tr="bir klip"), out)


_UNSAFE_TOKEN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|analysis-proxy-ios-[\w-]+(?:\.\w+)?|\basset-[\w-]+|\blegacy-clip-\d+"
    r"|\b[\w-]+\.(?:mp4|mov|m4v|heic|jpe?g|png|webp)\b|\bIMG_\d+",
    re.IGNORECASE,
)


# ── what a turn reads and writes ───────────────────────────────────────────────────────────────

SNAPSHOT_KEY = "clip_order_ids"
# KRIA_STORY_SEQUENCER_ENABLED: set beside SNAPSHOT_KEY when the order holds places the story
# sequencer chose, so a receipt says "an order that fits the story", not "the order you have".
STORY_SNAPSHOT_KEY = "clip_order_story"


def group_words_by_clip(resolved_intents: object) -> dict[str, str]:
    """media id -> the creator's own words for the group that names it ("the map video").

    The first order / group row that names a clip wins. Words that would show an id or a file
    name are skipped (`safe_words`), so a creator who named a clip by its file name still reads
    as "Clip N" to everyone else.
    """
    out: dict[str, str] = {}
    for intent in resolved_intents if isinstance(resolved_intents, Sequence) else ():
        if not isinstance(intent, Mapping) or intent.get("op") not in {"order", "group"}:
            continue
        words = safe_words(intent.get("attribute"))
        if not words:
            continue
        for assignment in intent.get("assignments") or ():
            media_id = assignment.get("media_id") if isinstance(assignment, Mapping) else None
            if isinstance(media_id, str) and media_id:
                out.setdefault(media_id, words)
    return out


def labels_for(
    order: Sequence[str],
    rows: Iterable[Mapping[str, Any]],
    *,
    group_words: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """media id -> the name the creator sees, for every clip in ``order``.

    ``rows`` are the clip assignments (the upload note and the clip's own record). The number in
    a fallback name is the clip's place in ``order``: "Clip 2" is the second clip that plays.
    A name that is exactly ANY clip's media id, file name or file stem (KRI-1594: a note the
    creator typed as "IMG_0412" or pasted an id into, whatever its shape) is never a name.
    """
    rows = [row for row in rows if isinstance(row, Mapping)]
    by_id = {str(row.get("media_id")): row for row in rows}
    project_tokens = _id_tokens([*order, *(token for row in rows for token in _row_tokens(row))])
    out: dict[str, str] = {}
    for position, media_id in enumerate(order, start=1):
        row = by_id.get(media_id) or {}
        analysis = row.get("analysis") if isinstance(row.get("analysis"), Mapping) else {}
        understanding = (
            analysis.get("understanding")
            if isinstance(analysis.get("understanding"), Mapping)
            else {}
        )
        # The clip's own id and file name are never its name, even without an extension.
        banned = [
            token.casefold()
            for token in (media_id, row.get("source_filename"), row.get("filename"))
            if isinstance(token, str) and len(token.rsplit(".", 1)[0]) >= 4
        ]

        def clean(value: object, banned: list[str] = banned) -> object:
            text = " ".join(str(value or "").casefold().split())
            if text in project_tokens:
                return None
            return None if any(token.rsplit(".", 1)[0] in text for token in banned) else value

        out[media_id] = clip_label(
            position=position,
            group_words=clean((group_words or {}).get(media_id)),
            note=clean(row.get("user_note") or row.get("creator_note")),
            description=clean(understanding.get("subject") or analysis.get("subject")),
        )
    # Two clips that read the same ("friends", "friends") would make "moved friends before
    # friends" say nothing: the number is the tie-break, and always paired with the name.
    counts: dict[str, int] = {}
    for label in out.values():
        counts[label] = counts.get(label, 0) + 1
    return {
        media_id: label
        if counts[label] == 1 or is_fallback_label(label)
        else f"{label} ({position})"
        for position, (media_id, label) in enumerate(out.items(), start=1)
    }


def file_tokens(order: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> frozenset[str]:
    """Every clip's own identifiers (media id, file names, storage file), casefolded, whole and
    without the extension: what `names_a_file` checks a creator's words against."""
    rows = [row for row in rows if isinstance(row, Mapping)]
    return _id_tokens([*order, *(token for row in rows for token in _row_tokens(row))])


def names_a_file(text: object, tokens: Collection[str]) -> bool:
    """Whether ``text`` is exactly one of the project's clip ids, file names or file stems
    (``tokens``: `file_tokens`). Such words name a file, not what a clip shows."""
    folded = " ".join(str(text or "").casefold().split())
    return bool(folded) and folded in tokens


def _row_tokens(row: Mapping[str, Any]) -> list[str]:
    """A clip row's own identifiers: its media id, file names and storage path."""
    out: list[str] = []
    for key in ("media_id", "source_filename", "filename", "gcs_path"):
        value = row.get(key)
        if isinstance(value, str) and value:
            out.append(value.rsplit("/", 1)[-1] if key == "gcs_path" else value)
    return out


def _id_tokens(values: Iterable[str]) -> frozenset[str]:
    """Each identifier casefolded, whole and without its extension."""
    out: set[str] = set()
    for value in values:
        text = " ".join(str(value or "").casefold().split())
        if text:
            out.add(text)
            out.add(text.rsplit(".", 1)[0])
    return frozenset(out)


def ordered_by_ids(items: Sequence[Any], ids: Sequence[str], key: Any = str) -> list[Any]:
    """``items`` in the order of ``ids``; an item whose id is not listed keeps its place after
    them (a clip added since is never lost)."""
    rank = {media_id: index for index, media_id in enumerate(ids)}
    listed = sorted((item for item in items if key(item) in rank), key=lambda i: rank[key(i)])
    return [*listed, *(item for item in items if key(item) not in rank)]


def with_clip_order_ids(snapshot: Mapping[str, Any], state: ClipOrderState) -> dict[str, Any]:
    """The approval-bound media snapshot, carrying the order every reader of it must seat on.

    Only when a creator actor changed the order, or the story sequencer chose one
    (``state.story_ids``, which then also sets ``STORY_SNAPSHOT_KEY``): an untouched order IS the
    order the clips were added, which the snapshot's own lists already say, and an order the plan
    was merely seen to have laid out is the plan's, not a statement to pin. Source identity
    (`media_identity`) never reads these keys, so they never invalidate an approval.
    """
    if state.basis != "confirmed" and not state.story_ids:
        return dict(snapshot)
    return with_order(snapshot, state.order, story=bool(state.story_ids))


def with_order(
    snapshot: Mapping[str, Any], order: Sequence[str], *, story: bool = False
) -> dict[str, Any]:
    """``snapshot`` carrying ``order``; ``story`` says the story sequencer chose part of it."""
    out = {key: value for key, value in snapshot.items() if key != STORY_SNAPSHOT_KEY}
    out[SNAPSHOT_KEY] = list(order)
    if story:
        out[STORY_SNAPSHOT_KEY] = True
    return out


def snapshot_order(snapshot: Mapping[str, Any] | None) -> list[str]:
    """The ledger order a snapshot carries ([] when it carries none)."""
    raw = (snapshot or {}).get(SNAPSHOT_KEY)
    return [str(item) for item in raw if isinstance(item, str)] if isinstance(raw, list) else []


def snapshot_story(snapshot: Mapping[str, Any] | None) -> bool:
    """Whether the order a snapshot carries holds places the story sequencer chose."""
    return bool(snapshot_order(snapshot)) and (snapshot or {}).get(STORY_SNAPSHOT_KEY) is True


def story_decided_ids(entries: Iterable[LedgerEntry]) -> frozenset[str]:
    """The clips the story sequencer has already been asked to place (whatever it answered)."""
    return frozenset(item for entry in entries for item in entry.story_ids)


def _is_order_row(intent: Any) -> bool:
    get = (
        intent.get
        if isinstance(intent, Mapping)
        else lambda name, default=None: getattr(intent, name, default)
    )
    return bool(
        get("op") == "order"
        and not get("order_by")
        and not get("placeholder")
        and get("position") in (None, "first", "last")
    )


def mark_seated_rows(
    intents: Sequence[Any],
    *,
    applied: frozenset[str],
    fresh_order_ask: bool,
    base_ids: Sequence[str] = (),
    known_ids: Collection[str] | None = None,
) -> list[Any]:
    """The resolved intents with each order row an earlier turn seated, and the order has moved
    away from since, marked ``already_seated``.

    A marked row seats nothing (`clip_order_sequence.sequence_rows`), so a drag, a manual edit or
    a chat reorder is never undone by something the creator said turns ago, and the old ask is
    never judged against what they did after it. Rows that would not move anything on
    ``base_ids`` (the order still satisfies them, for the clips in ``known_ids``) stay as they
    are: seating them again is a no-op, and every reader keeps judging them. When this turn
    states an order of its own (``fresh_order_ask``) every row seats again, on the current order:
    the newest statement wins, and a restated row is not mistaken for an old one.
    """
    from app.services.clip_order_sequence import apply_sequence, sequence_rows  # noqa: PLC0415

    if fresh_order_ask or not applied or not base_ids:
        return list(intents)
    plain = [
        intent.model_dump(mode="json") if hasattr(intent, "model_dump") else intent
        for intent in intents
    ]
    # Only the clips that existed when the rows were seated can have been moved away from: a clip
    # added since joins the group the resolver now says it belongs to, as a new clip should.
    base = [item for item in base_ids if known_ids is None or item in known_ids]

    def fingerprint(intent: Any) -> str:
        get = intent.get if isinstance(intent, Mapping) else lambda n, d=None: getattr(intent, n, d)
        return row_fingerprint(get("position"), get("attribute"))

    # Keep a row live while the rows kept so far (in listed order) still leave the order as it is;
    # the first one that would move a clip is the one the creator has since moved away from.
    kept: set[int] = set()
    marked: set[int] = set()
    for index, intent in enumerate(intents):
        if not _is_order_row(intent) or fingerprint(intent) not in applied:
            continue
        trial = [row for at, row in enumerate(plain) if at in kept or at == index]
        if apply_sequence(base, sequence_rows(trial)) == base:
            kept.add(index)
        else:
            marked.add(index)
    out: list[Any] = []
    for index, intent in enumerate(intents):
        if index in marked:
            intent = (  # noqa: PLW2901 - same intent, marked
                {**intent, "already_seated": True}
                if isinstance(intent, Mapping)
                else intent.model_copy(update={"already_seated": True})
            )
        out.append(intent)
    return out


def seated_entry(
    base_ids: Sequence[str],
    resolved_intents: object,
    *,
    actor: str,
    turn_id: str | None,
) -> LedgerEntry | None:
    """The ledger entry for the order rows a draft seats on ``base_ids``, else None.

    Reads the same rows the planner, the contract and the receipts read
    (`clip_order_sequence.sequence_rows`), through the same seating (`apply_sequence`), so the
    entry IS the order the draft plays. Marked (already seated) rows are not in ``sequence_rows``.
    """
    from app.services.clip_order_sequence import apply_sequence, sequence_rows  # noqa: PLC0415

    rows = sequence_rows(resolved_intents)
    if not rows or not base_ids:
        return None
    after = apply_sequence(list(base_ids), rows)
    fingerprints = tuple(
        row_fingerprint(position, name) for position, name, _members, _status in rows
    )
    return LedgerEntry(actor, tuple(base_ids), tuple(after), turn_id, fingerprints)


@dataclass(frozen=True)
class TurnClipOrder:
    """The clip order as one turn's planning reads it (set once per turn by the planner)."""

    state: ClipOrderState
    attachment_ids: tuple[str, ...]
    rendered: bool
    labels: Mapping[str, str]
    # What the creator did by hand since the ledger last spoke (a drag, a manual timeline edit).
    observed: LedgerEntry | None = None
    # The creator's own words for each clip's group (`known_group_words`): the names `labels`
    # was built with, kept so a receipt written later in the turn names clips the same way.
    groups: Mapping[str, str] = field(default_factory=dict)
    # KRIA_STORY_SEQUENCER_ENABLED: the clips of the newest card when the creator tapped "use
    # this order" on it: each is where they want it, so the story sequencer never moves one.
    confirmed_ids: tuple[str, ...] = ()
    # Why this turn restarts the creator's request, noted while it plans (`note_restarted`).
    # Mutable on purpose: planning runs below the context this object was set in.
    restarted: list[str] = field(default_factory=list, compare=False)


# The clip order the turn being planned reads. Set once by the planner (`plan_live_turn`), read by
# its order hook and the Main Creator input; None outside a planned turn and with the flag off.
turn_clip_order: contextvars.ContextVar[TurnClipOrder | None] = contextvars.ContextVar(
    "kria_turn_clip_order", default=None
)

RESTART_STARTS_FRESH = "starts_fresh"
RESTART_CHANGE_REQUEST = "change_request"


def note_restarted(reason: str) -> None:
    """The turn being planned restarts the creator's request (KRI-1594): the extraction step typed
    ``starts_fresh``, or the message is the new request after a "Change my request" tap. No-op
    outside a planned turn and with the flag off (no turn order is set then)."""
    turn = turn_clip_order.get()
    if turn is not None and reason not in turn.restarted:
        turn.restarted.append(reason)


def restarts_order(turn: TurnClipOrder) -> bool:
    """Whether this turn resets the order: it restarts the request and no video exists yet (once
    one does, the timeline is the order and the editor owns it)."""
    return bool(turn.restarted) and not turn.rendered


def planning_state(turn: TurnClipOrder) -> ClipOrderState:
    """The order this turn plans on: what the ledger says, or, when the turn restarts the request
    before a render (`restarts_order`), the order the clips were added (the reset row this turn
    writes takes the older entries back for every later reader)."""
    if not restarts_order(turn):
        return turn.state
    return current_clip_order(attachment_ids=turn.attachment_ids)


# ── what the Main Creator is shown ───────────────────────────────────────────────────────────

_SOURCE_WORDS = {
    SERVER_DEFAULT: ("as added", "eklendiği gibi"),
    CREATOR_TEXT: ("you asked", "isteğin üzerine"),
    CREATOR_DRAG: ("you moved it", "sen taşıdın"),
    CREATOR_EDITOR: ("you edited it", "sen düzenledin"),
    AI_STORY: ("my story order", "benim hikaye sıram"),
}


def order_line(turn: TurnClipOrder, *, room_chars: int) -> str:
    """ONE line naming the clips in the order they play now: position, name, who set the spot.

    Clip numbers are play order everywhere the prompt says "clip N", so this is the line that
    says what "clip 2" is. It never exceeds ``room_chars``: a long list is cut with a count.
    """
    state = turn.state
    parts: list[str] = []
    for position, media_id in enumerate(state.order, start=1):
        label = turn.labels.get(media_id) or f"Clip {position}"
        words = _SOURCE_WORDS.get(state.source_of(media_id), _SOURCE_WORDS[SERVER_DEFAULT])[0]
        parts.append(f"{position} {label} ({words})")
    head = "Clips in the order they play now: "
    line = head + "; ".join(parts) + "."
    if len(line) <= room_chars:
        return line
    kept: list[str] = []
    for index, part in enumerate(parts):
        rest = len(parts) - index - 1
        tail = f"; and {rest} more." if rest else "."
        if len(head + "; ".join([*kept, part]) + tail) > room_chars:
            break
        kept.append(part)
    left = len(parts) - len(kept)
    return head + "; ".join(kept) + (f"; and {left} more." if left else ".")


def prompt_view(
    turn: TurnClipOrder | None,
    media_context: Sequence[Mapping[str, Any]],
    item_context: str,
    *,
    item_context_max: int = 4000,
) -> tuple[list[dict[str, Any]], str] | None:
    """The Main Creator's ``media_context`` and ``item_context`` in the creator's current order,
    or None when the ledger has nothing to say (no entry: the prompt stays byte-identical).

    ``media_context`` is sorted by the order (clips the order does not list, such as Visuals,
    keep their place after them); ``item_context`` gains the one line from `order_line`. The
    hashed manifest is NOT reordered here: its hash is the approval fence.
    """
    # A request restarted before a render (KRI-1594) has no entries left: the prompt is as plain.
    if turn is None or not planning_state(turn).entries:
        return None
    sorted_media = ordered_by_ids(
        [dict(row) for row in media_context],
        turn.state.order,
        key=lambda row: str(row.get("media_id")),
    )
    separator = "\n" if item_context else ""
    room = item_context_max - len(item_context) - len(separator)
    if room < 80:
        return sorted_media, item_context
    return sorted_media, f"{item_context}{separator}{order_line(turn, room_chars=room)}"
