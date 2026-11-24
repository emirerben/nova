"""The story sequencer's server half (``KRIA_STORY_SEQUENCER_ENABLED``, KRI-1593).

Owner rule (KRI-1586): what the creator says about the clip order is kept exactly; where they
said nothing, "the AI decides what fits the story; if it can't, upload order". The model
(`app.agents.story_sequencer`) only RANKS clips. Everything that decides what may move, and
whether its answer is used at all, is here, in pure functions over ids:

* ``open_spans``: the stretches of the order the creator left open. A clip the creator placed
  (stated alone, dragged, edited by hand, or confirmed on the card) is fixed, and a clip inside a
  group they described can only trade places with the other clips of that group.
* ``apply_ranking``: the model's ranking laid over the order, span by span, slot for slot. A
  fixed clip keeps its exact position by construction, whatever the model returned.
* ``plan_story`` / ``settle_story``: the sequencer is asked only when the render contract would
  pin the order the snapshot carries (the unified phone montage shape), and its answer is used
  only when the contract then pins exactly the order the ledger entry and the card say. Anything
  else (no answer, "cannot decide", a mismatch) leaves the order the clips were added in.

The order rides the same carrier as a creator's own order (`clip_order_ledger.SNAPSHOT_KEY` on the
approval-bound media snapshot, plus ``STORY_SNAPSHOT_KEY``), so the contract, readiness, the phone
preflight and the worker seat the same order (KRI-503). No I/O, no model call, one settings read.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services import clip_order_ledger as ledger
from app.services.clip_order_ledger import AI_STORY, CREATOR_ACTORS, ClipOrderState, LedgerEntry

# A request is ranked only up to this many open clips; more than that stays in upload order
# (the prompt's per-clip evidence is bounded, and a ranking over a partial list is not a story).
MAX_STORY_CLIPS = 40
REQUEST_CHARS = 1500
ASK_CHARS = 240
MAX_ASKS = 12
_FIELD_CHARS = {"subject": 120, "summary": 220, "setting": 100, "activity": 120}
_SPEECH_CHARS = 200
_ON_SCREEN_CHARS = 80


def story_sequencer_on() -> bool:
    """``KRIA_STORY_SEQUENCER_ENABLED`` (and the ledger it writes to): the AI picks the order of
    the clips nobody placed. Off = no model call and no new entry; a story order an earlier turn
    already wrote is still the thread's order (the creator saw it on the card)."""
    from app.config import settings  # noqa: PLC0415

    return bool(settings.kria_story_sequencer_enabled) and ledger.clip_order_ledger_on()


@dataclass(frozen=True)
class StoryPlan:
    """What the sequencer may decide on one draft.

    ``before`` is the order the draft plays without it (the creator's rows seated on the order
    the turn started from). ``spans`` are the open stretches, each the clips of it in ``before``
    order (two or more: one clip has no order to choose). ``fixed`` are the clips the creator
    placed. ``played`` is the subset the edit shows, or None for all of them.
    """

    before: tuple[str, ...]
    spans: tuple[tuple[str, ...], ...]
    fixed: frozenset[str]
    played: frozenset[str] | None = None

    @property
    def open_ids(self) -> tuple[str, ...]:
        return tuple(item for span in self.spans for item in span)


def fixed_ids(
    state: ClipOrderState,
    strategy: Mapping[str, Any] | None,
    *,
    confirmed: Collection[str] = (),
) -> frozenset[str]:
    """The clips whose place the creator set: moved by a creator actor in the ledger (a message,
    a drag, a manual edit), named alone by an order row of this draft ("end with the beach
    clip", a listed order), or on the card when they tapped "use this order"."""
    from app.services.clip_order_card import stated_ids  # noqa: PLC0415

    moved = {item for item, actor in state.source.items() if actor in CREATOR_ACTORS}
    return frozenset(moved) | stated_ids(strategy) | frozenset(confirmed)


def open_spans(
    order: Sequence[str],
    resolved_intents: object,
    *,
    fixed: Collection[str],
    played: Collection[str] | None = None,
) -> tuple[tuple[str, ...], ...]:
    """The stretches of ``order`` the creator left open, each two or more clips in ``order``.

    Two clips share a span only when the creator's rows treat them alike: the same described
    group (or both in no group at all), by the rows that seat now AND by the rows an earlier turn
    seated (a group the creator has since dragged apart still keeps its clips together here).
    A row the server could not resolve says the creator placed something we cannot identify, so
    nothing is open then: guessing which clip they meant is how a placed clip would move.
    """
    from app.services.clip_order_sequence import seat_owners, sequence_rows  # noqa: PLC0415

    live = sequence_rows(resolved_intents)
    every = sequence_rows(resolved_intents, seated=True)
    if any(status != "resolved" for _position, _name, _members, status in every):
        return ()
    present = set(order)
    live_owner = seat_owners(live, present)
    seated_owner = seat_owners(every, present)
    fixed_set = set(fixed)
    grouped: dict[tuple[int | None, int | None], list[str]] = {}
    for item in dict.fromkeys(order):
        if item in fixed_set or (played is not None and item not in played):
            continue
        grouped.setdefault((live_owner.get(item), seated_owner.get(item)), []).append(item)
    return tuple(tuple(span) for span in grouped.values() if len(span) > 1)


def apply_ranking(
    order: Sequence[str], spans: Sequence[Sequence[str]], ranking: Sequence[str]
) -> list[str]:
    """``order`` with each span's clips re-seated by ``ranking``, in the slots that span holds.

    ``ranking`` is the model's play order over the open clips. A clip it did not rank keeps its
    place among the ranked ones of its span after them (never dropped); an id that is in no span
    is ignored. Every clip outside the spans keeps its exact position.
    """
    rank = {item: index for index, item in enumerate(dict.fromkeys(ranking))}
    result = list(order)
    position = {item: index for index, item in enumerate(result)}
    for span in spans:
        members = [item for item in span if item in position]
        slots = sorted(position[item] for item in members)
        ranked = sorted((item for item in members if item in rank), key=lambda item: rank[item])
        fill = [*ranked, *(item for item in members if item not in rank)]
        for slot, item in zip(slots, fill, strict=True):
            result[slot] = item
    return result


def pinned_order(
    strategy: Mapping[str, Any],
    brief: Any,
    snapshot: Mapping[str, Any],
    order: Sequence[str],
    *,
    played: Collection[str] | None,
) -> bool:
    """Whether the render contract pins exactly ``order`` when the snapshot carries it.

    The one eligibility rule: the contract's own (`build_render_contract`), asked, never copied.
    It pins a carried order only for the unified phone montage shape (a guided format, phone
    proxy clips, the clips' own sound or library music, no voiceover or song, no Visuals, not the
    native program, no random / filming-time / "any order" / "group first" basis), which is the
    shape the worker plays in that order and the phone verifier refuses any other for. So a story
    order is only ever written where the card, the pin and the render are the same order.
    """
    from app.services.creator_render_contract import build_render_contract  # noqa: PLC0415

    try:
        contract = build_render_contract(
            strategy,
            generation_id="story-order",
            brief=brief,
            media_snapshot=ledger.with_order(snapshot, order, story=True),
        )
    except Exception:  # noqa: BLE001 - a contract that cannot be built pins nothing
        return False
    expected = [item for item in order if played is None or item in played]
    return bool(
        contract is not None
        and contract.order_required
        and contract.order_basis == "attachment_order"
        and list(contract.order_ids) == expected
    )


def plan_story(
    strategy: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    state: ClipOrderState,
    *,
    brief: Any,
    confirmed: Collection[str] = (),
    rendered: bool = False,
) -> StoryPlan | None:
    """What the sequencer may decide on this draft, or None when it has nothing to decide.

    None when: a video already exists (the timeline is the order), the draft's order is not the
    upload order the creator can re-arrange (`clip_order_card.card_applies`), nothing is open,
    every open clip was already looked at by an earlier run (`story_decided_ids`: it never runs
    twice over the same clips), there are too many, or the render contract would not pin the
    order (`pinned_order`).
    """
    from app.services.clip_order_card import card_applies, played_ids  # noqa: PLC0415
    from app.services.clip_order_sequence import apply_sequence, sequence_rows  # noqa: PLC0415

    if rendered or not isinstance(strategy, Mapping) or not isinstance(snapshot, Mapping):
        return None
    if not card_applies(strategy) or strategy.get("editorial_plan"):
        return None
    intents = strategy.get("resolved_clip_intents")
    before = apply_sequence(list(state.order), sequence_rows(intents))
    played = played_ids(strategy)
    fixed = fixed_ids(state, strategy, confirmed=confirmed)
    spans = open_spans(before, intents, fixed=fixed, played=played)
    open_ids = [item for span in spans for item in span]
    if not spans or len(open_ids) > MAX_STORY_CLIPS:
        return None
    if set(open_ids) <= ledger.story_decided_ids(state.entries):
        return None
    if not pinned_order(strategy, brief, snapshot, before, played=played):
        return None
    return StoryPlan(tuple(before), spans, fixed, played)


@dataclass(frozen=True)
class StoryResult:
    """One run's outcome: the ledger entry to write, and the snapshot that carries its order.

    ``snapshot`` is None when the order did not change (the model kept the clips as they are, or
    could not decide): the entry is then only the note that these clips were looked at.
    """

    entry: LedgerEntry
    snapshot: dict[str, Any] | None

    @property
    def changed(self) -> bool:
        return self.entry.before_ids != self.entry.after_ids


def settle_story(
    plan: StoryPlan,
    ranking: Sequence[str] | None,
    strategy: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    brief: Any,
) -> StoryResult:
    """The entry and snapshot for the model's ``ranking`` (None = it could not decide).

    The order is used only when it survives every reader's own rule: the creator's rows seated
    again on it change nothing (`apply_sequence` is how the contract, the preflight and the
    worker read the snapshot), no fixed clip changed position, and the contract pins exactly
    it. Otherwise the order stays as it was and the entry only records that the clips were
    looked at, so the next turn does not ask again.
    """
    from app.services.clip_order_sequence import apply_sequence, sequence_rows  # noqa: PLC0415

    before = list(plan.before)
    looked_at = LedgerEntry(AI_STORY, plan.before, plan.before, None, story_ids=plan.open_ids)
    if not ranking:
        return StoryResult(looked_at, None)
    after = apply_ranking(before, plan.spans, ranking)
    if after == before:
        return StoryResult(looked_at, None)
    rows = sequence_rows(strategy.get("resolved_clip_intents"))
    kept = all(after[index] == item for index, item in enumerate(before) if item in plan.fixed)
    if (
        not kept
        or sorted(after) != sorted(before)
        or apply_sequence(after, rows) != after
        or not pinned_order(strategy, brief, snapshot, after, played=plan.played)
    ):
        return StoryResult(looked_at, None)
    entry = LedgerEntry(AI_STORY, plan.before, tuple(after), None, story_ids=plan.open_ids)
    return StoryResult(entry, ledger.with_order(snapshot, after, story=True))


# ── what the model is shown ────────────────────────────────────────────────────────────────────


def _cut(value: object, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """One clip's stored record for the prompt: what it shows and says, never a name or a path.
    The same projection every other reader of the record uses (`clip_record`)."""
    from app.services.clip_facts import assignment_facts  # noqa: PLC0415
    from app.services.clip_understanding import clip_record  # noqa: PLC0415

    kind = "image" if row.get("kind") == "image" else "video"
    analysis = row.get("analysis") if isinstance(row.get("analysis"), dict) else None
    view = clip_record(analysis, kind=kind).prompt_view(transcript_chars=_SPEECH_CHARS)
    out: dict[str, Any] = {
        key: _cut(view[key], limit) for key, limit in _FIELD_CHARS.items() if view.get(key)
    }
    if view.get("on_screen_text"):
        out["on_screen_text"] = _cut(view["on_screen_text"], _ON_SCREEN_CHARS)
    speech = view.get("speech")
    if isinstance(speech, dict) and speech.get("transcript"):
        out["says"] = _cut(speech["transcript"], _SPEECH_CHARS)
        if speech.get("to_camera"):
            out["to_camera"] = True
    for fact in assignment_facts(row):
        if fact.kind == "capture_time" and fact.value:
            out["filmed_at"] = fact.value
        elif fact.kind in {"place", "landmark"} and fact.value:
            out.setdefault(fact.kind, _cut(fact.value, 80))
    return out


def sequencer_clips(
    plan: StoryPlan,
    snapshot: Mapping[str, Any],
    *,
    groups: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The clips as the model sees them, in ``plan.before`` order, and alias -> media id.

    Each clip is named by a short alias (``c1`` is the clip that plays first now), never its
    media id or file name: the model has nothing to echo. ``span`` names the open stretch a clip
    belongs to; a clip with no span is one the creator placed and is there for context only.
    ``group`` and ``note`` are the creator's own words (`safe_words`: nothing that would show an
    id or a file name).
    """
    rows = {
        str(row.get("media_id")): row
        for row in snapshot.get("clip_assignments") or ()
        if isinstance(row, Mapping) and row.get("media_id")
    }
    span_of = {item: f"s{index}" for index, span in enumerate(plan.spans, 1) for item in span}
    shown = [item for item in plan.before if plan.played is None or item in plan.played]
    clips: list[dict[str, Any]] = []
    alias_to_id: dict[str, str] = {}
    for position, media_id in enumerate(shown, start=1):
        alias = f"c{position}"
        alias_to_id[alias] = media_id
        row = rows.get(media_id) or {}
        duration = row.get("duration_s")
        clip: dict[str, Any] = {
            "id": alias,
            "span": span_of.get(media_id),
            "kind": "image" if row.get("kind") == "image" else "video",
        }
        if isinstance(duration, int | float) and not isinstance(duration, bool) and duration > 0:
            clip["duration_s"] = round(float(duration), 1)
        group = ledger.safe_words((groups or {}).get(media_id))
        if group:
            clip["group"] = group
        note = ledger.safe_words(row.get("user_note") or row.get("creator_note"))
        if note:
            clip["note"] = note
        clip.update(_evidence(row))
        clips.append(clip)
    return clips, alias_to_id


def creator_asks(brief: Any) -> list[str]:
    """The creator's live asks in the brief's own words, so the model knows what the video is for.
    Bounded; an ask about text, sound or length is still context for what the story is."""
    live = getattr(brief, "live", None)
    asks = [
        _cut(getattr(req, "description", None), ASK_CHARS)
        for req in (live() if callable(live) else ())
    ]
    return [ask for ask in asks if ask][:MAX_ASKS]


def ranking_ids(order: Sequence[str], alias_to_id: Mapping[str, str]) -> list[str]:
    """The model's ranking as media ids (an alias it was never given is dropped)."""
    return [alias_to_id[alias] for alias in order if alias in alias_to_id]
