"""The story sequencer's server half (``KRIA_STORY_SEQUENCER_ENABLED``, KRI-1593).

Owner rule (KRI-1586): what the creator says about the clip order is kept exactly; where they
said nothing, "the AI decides what fits the story; if it can't, upload order". The model
(`app.agents.story_sequencer`) only RANKS clips. Everything that decides what may move, and
whether its answer is used at all, is here, in pure functions over ids:

* ``carries_story``: the one eligibility rule, re-asked on EVERY draft. A story order may ride a
  draft only when the creator stated no order basis and nothing tied to a clip's position
  (`states_order`), the render contract pins exactly that order and the phone montage planner the
  worker runs plays exactly that order (`one_order`). So a story order exists only where the
  card, the pin and the render are one order (KRI-503).
* ``open_spans``: the stretches of the order the creator left open. A clip the creator placed
  (stated alone, dragged, edited by hand, or confirmed on the card) is fixed, and a clip inside a
  group they described can only trade places with the other clips of that group.
* ``apply_ranking``: the model's ranking laid over the order, span by span, slot for slot. A
  fixed clip keeps its exact position, and a clip an earlier run already placed keeps its place
  among the other placed ones (only clips nobody looked at are seated), by construction.
* ``review_story`` / ``settle_story`` / ``withdraw_story``: what a draft does about the story
  order: ask the sequencer, keep what stands, or put the AI's clips back in upload order when
  the draft can no longer carry them (the creator chose the upload order, another format, ...).

The order rides the same carrier as a creator's own order (`clip_order_ledger.SNAPSHOT_KEY` on the
approval-bound media snapshot, plus ``STORY_SNAPSHOT_KEY``). No model call and no I/O here.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services import clip_order_ledger as ledger
from app.services.clip_order_ledger import (
    AI_STORY,
    CREATOR_ACTORS,
    SERVER_DEFAULT,
    ClipOrderState,
    LedgerEntry,
)

# A request is ranked only up to this many open clips; more than that stays in upload order
# (the prompt's per-clip evidence is bounded, and a ranking over a partial list is not a story).
MAX_STORY_CLIPS = 40
REQUEST_CHARS = 1500
ASK_CHARS = 240
MAX_ASKS = 12
_FIELD_CHARS = {"subject": 120, "summary": 220, "setting": 100, "activity": 120}
_SPEECH_CHARS = 200
_ON_SCREEN_CHARS = 80
# An order requirement's key that names no basis: the clips' own sequence rows decide it.
_NO_BASIS_KEYS = frozenset({"", "narrative"})


def story_sequencer_on() -> bool:
    """``KRIA_STORY_SEQUENCER_ENABLED`` (and the ledger it writes to): the AI picks the order of
    the clips nobody placed. Off = no model call and no new story entry; a story order an earlier
    turn already wrote is still the thread's order (the creator saw it on the card) and is still
    withdrawn when a draft can no longer carry it."""
    from app.config import settings  # noqa: PLC0415

    return bool(settings.kria_story_sequencer_enabled) and ledger.clip_order_ledger_on()


@dataclass(frozen=True)
class StoryPlan:
    """What the sequencer may decide on one draft.

    ``before`` is the order the draft plays without it (the creator's rows seated on the order
    the turn started from). ``spans`` are the open stretches, each the clips of it in ``before``
    order (two or more: one clip has no order to choose). ``fixed`` are the clips the creator
    placed. ``played`` is the subset the edit shows, or None for all of them. ``placed`` are the
    open clips an earlier run already looked at: they keep their order among themselves.
    """

    before: tuple[str, ...]
    spans: tuple[tuple[str, ...], ...]
    fixed: frozenset[str]
    played: frozenset[str] | None = None
    placed: frozenset[str] = frozenset()

    @property
    def open_ids(self) -> tuple[str, ...]:
        return tuple(item for span in self.spans for item in span)


@dataclass(frozen=True)
class StoryResult:
    """One draft's outcome: the ledger entry to write and the media snapshot to bind."""

    entry: LedgerEntry
    snapshot: dict[str, Any]

    @property
    def changed(self) -> bool:
        return self.entry.before_ids != self.entry.after_ids


# ── may this draft carry a story order at all ──────────────────────────────────────────────────


def states_order(strategy: Mapping[str, Any], brief: Any) -> bool:
    """Whether the creator said something about the order, or tied something to a clip's place,
    that a re-order would overrule. Fields only, never words.

    * an answer to the order-basis question ("use the order I added the clips", "no fixed
      order"): the creator chose, so the AI's order has no say;
    * a live order ask that names a basis (filming time, the order they were added, random, any
      order, a route, a direction): the basis is the order. A described sequence ("the map first,
      then the train") names none and is seated by its own rows;
    * text given by position: per-shot labels in on-screen order, the creator's own sentences
      laid over the clips in turn, or a route's start / end labels. Those were written against
      the order the clips are in; moving the clips under them would put the creator's words on
      other footage.
    """
    from app.kria.brief_route import END_KEYS, START_KEYS  # noqa: PLC0415
    from app.pipeline.unified_montage import brief_view  # noqa: PLC0415
    from app.services.choice_questions import (  # noqa: PLC0415
        CONFLICT_ORDER_BASIS,
        is_pin_only_order,
    )
    from app.services.order_mode import order_mode  # noqa: PLC0415

    answers = strategy.get("choice_answers")
    if any(
        isinstance(answer, Mapping) and answer.get("conflict") == CONFLICT_ORDER_BASIS
        for answer in (answers if isinstance(answers, Sequence) else ())
    ):
        return True
    if strategy.get("shot_labels"):
        return True
    live = getattr(brief, "live", None)
    for req in live() if callable(live) else ():
        if getattr(req, "kind", None) != "order":
            continue
        if order_mode(req) is not None:
            return True
        if is_pin_only_order(req):
            continue  # "this clip first, that one last": two pins, seated by their rows
        facts = getattr(req, "facts", None) or {}
        key = str(facts.get("key") or facts.get("by") or "").casefold()
        if key not in _NO_BASIS_KEYS or facts.get("direction") or facts.get("order"):
            return True
    view = brief_view(brief)
    return bool(
        view.sequence_literals
        or any(view.facts.get(key) for key in START_KEYS)
        or any(view.facts.get(key) for key in END_KEYS)
    )


def one_order(
    strategy: Mapping[str, Any],
    brief: Any,
    snapshot: Mapping[str, Any],
    order: Sequence[str],
    *,
    played: Collection[str] | None,
    creator_id: object,
    client_protocol: int | None = None,
) -> bool:
    """Whether the render contract pins exactly ``order`` AND the worker's own phone montage
    plan plays exactly ``order``, when the snapshot carries it.

    Both are asked, never copied: the contract through `build_render_contract`, the worker
    through `preflight_montage_plan` (the planner dispatch and the worker run, with this
    creator's route). The phone verifier refuses a render whose clip order is not exactly the
    pinned ids, so anything short of both agreeing would be an approved draft that cannot render
    (KRI-503). It also means: only the unified phone montage for this creator, no Visuals, no
    voiceover or song, not the native program, no clip the plan would drop or reorder.
    """
    from app.kria.brief_checks import plan_facts_from_unified_montage  # noqa: PLC0415
    from app.services.creator_render_contract import build_render_contract  # noqa: PLC0415
    from app.services.phone_montage_preflight import preflight_montage_plan  # noqa: PLC0415

    carried = ledger.with_order(snapshot, order, story=True)
    expected = [item for item in order if played is None or item in played]
    try:
        contract = build_render_contract(
            strategy, generation_id="story-order", brief=brief, media_snapshot=carried
        )
        if not (
            contract is not None
            and contract.order_required
            and contract.order_basis == "attachment_order"
            and list(contract.order_ids) == expected
        ):
            return False
        planned = preflight_montage_plan(
            creator_id=creator_id,
            edit_format=strategy.get("edit_format"),
            brief=brief,
            strategy=strategy,
            media_snapshot=carried,
            visuals=[],
            client_protocol=client_protocol,
        )
        if not isinstance(planned, tuple):
            return False
        return list(plan_facts_from_unified_montage(planned[0].record()).clip_ids) == expected
    except Exception:  # noqa: BLE001 - an order nobody can vouch for is not carried
        return False


def carries_story(
    strategy: Mapping[str, Any],
    brief: Any,
    snapshot: Mapping[str, Any],
    order: Sequence[str],
    *,
    played: Collection[str] | None,
    creator_id: object,
    client_protocol: int | None = None,
) -> bool:
    """THE eligibility rule: may this draft carry a story order that plays as ``order``?"""
    from app.services.clip_order_card import card_applies  # noqa: PLC0415

    return bool(
        card_applies(strategy)
        # a story outline owns its own beat order: the Main Creator already chose it
        and not strategy.get("editorial_plan")
        and not states_order(strategy, brief)
        and one_order(
            strategy,
            brief,
            snapshot,
            order,
            played=played,
            creator_id=creator_id,
            client_protocol=client_protocol,
        )
    )


# ── what is open, and how a ranking is laid over it ────────────────────────────────────────────


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
    order: Sequence[str],
    spans: Sequence[Sequence[str]],
    ranking: Sequence[str],
    *,
    placed: Collection[str] = (),
) -> list[str]:
    """``order`` with each span's clips re-seated by ``ranking``, in the slots that span holds.

    ``ranking`` is the model's play order over the open clips. A clip it did not rank follows
    the ranked ones of its span in the place it had (never dropped); an id that is in no span is
    ignored. Every clip outside the spans keeps its exact position.

    ``placed`` are clips an earlier run already seated. They keep their order AMONG THEMSELVES:
    the ranking decides only where the other clips go between them (the ranks the placed clips
    took are handed back to them in the order they already play), so a later run never reshuffles
    an order the creator has already seen.
    """
    rank = {item: index for index, item in enumerate(dict.fromkeys(ranking))}
    kept = set(placed)
    result = list(order)
    position = {item: index for index, item in enumerate(result)}
    for span in spans:
        members = [item for item in span if item in position]
        slots = sorted(position[item] for item in members)
        ranked = sorted((item for item in members if item in rank), key=lambda item: rank[item])
        fill = [*ranked, *(item for item in members if item not in rank)]
        earlier = iter([item for item in members if item in kept])
        fill = [next(earlier) if item in kept else item for item in fill]
        for slot, item in zip(slots, fill, strict=True):
            result[slot] = item
    return result


# ── what one draft does about the story order ──────────────────────────────────────────────────


def review_story(
    strategy: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None,
    state: ClipOrderState,
    *,
    brief: Any,
    creator_id: object,
    attachment_ids: Sequence[str],
    confirmed: Collection[str] = (),
    rendered: bool = False,
    client_protocol: int | None = None,
) -> StoryPlan | StoryResult | None:
    """What this draft does about the story order.

    * a ``StoryResult``: the thread has a story order and this draft cannot carry it
      (`carries_story`: the creator chose the upload order, another format, a basis, ...), so the
      AI's clips go back to upload order (`withdraw_story`);
    * a ``StoryPlan``: the draft can carry one and clips nobody looked at are open: ask;
    * None: nothing to do (after a render the timeline is the order; nothing is open; every
      open clip was already looked at; too many clips; or no story order and none possible).
    """
    from app.services.clip_order_card import played_ids  # noqa: PLC0415
    from app.services.clip_order_sequence import apply_sequence, sequence_rows  # noqa: PLC0415

    if rendered or not isinstance(strategy, Mapping) or not isinstance(snapshot, Mapping):
        return None
    intents = strategy.get("resolved_clip_intents")
    before = apply_sequence(list(state.order), sequence_rows(intents))
    played = played_ids(strategy)
    if not carries_story(
        strategy,
        brief,
        snapshot,
        before,
        played=played,
        creator_id=creator_id,
        client_protocol=client_protocol,
    ):
        return withdraw_story(state, snapshot, attachment_ids) if state.story_ids else None
    fixed = fixed_ids(state, strategy, confirmed=confirmed)
    spans = open_spans(before, intents, fixed=fixed, played=played)
    open_ids = [item for span in spans for item in span]
    looked_at = ledger.story_decided_ids(state.entries)
    if not spans or len(open_ids) > MAX_STORY_CLIPS or set(open_ids) <= looked_at:
        return None
    return StoryPlan(tuple(before), spans, fixed, played, frozenset(open_ids) & looked_at)


def withdraw_story(
    state: ClipOrderState, snapshot: Mapping[str, Any], attachment_ids: Sequence[str]
) -> StoryResult:
    """The AI's clips put back in the order they were added, in the slots they hold.

    A clip the creator placed keeps its exact position. The entry's actor is the server's
    (``server_default``), which also clears the story mark from every clip (`_replay`), so the
    snapshot stops carrying the order unless the creator's own entries still stand.
    """
    story = state.story_ids
    upload = [item for item in dict.fromkeys(attachment_ids) if item in story]
    after = ledger.apply_order(list(state.order), upload, known=story)
    entry = LedgerEntry(SERVER_DEFAULT, state.order, tuple(after), None)
    settled = ledger.current_clip_order(
        attachment_ids=list(attachment_ids), entries=(*state.entries, entry)
    )
    bare = {
        key: value
        for key, value in snapshot.items()
        if key not in (ledger.SNAPSHOT_KEY, ledger.STORY_SNAPSHOT_KEY)
    }
    return StoryResult(entry, ledger.with_clip_order_ids(bare, settled))


def settle_story(
    plan: StoryPlan,
    ranking: Sequence[str] | None,
    strategy: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    brief: Any,
    creator_id: object,
    client_protocol: int | None = None,
) -> StoryResult:
    """The entry and snapshot for the model's ``ranking`` (None = it could not decide).

    The order is used only when it survives every reader's own rule: no fixed clip changed
    position, the creator's rows seated again on it change nothing (`apply_sequence` is how the
    contract, the preflight and the worker read the snapshot), and the contract and the worker's
    plan both hold exactly it (`one_order`). Otherwise the order stays as it was and the entry
    only records that the clips were looked at, so the next turn does not ask again.
    """
    from app.services.clip_order_sequence import apply_sequence, sequence_rows  # noqa: PLC0415

    before = list(plan.before)
    looked_at = LedgerEntry(AI_STORY, plan.before, plan.before, None, story_ids=plan.open_ids)
    unchanged = StoryResult(looked_at, dict(snapshot))
    if not ranking:
        return unchanged
    after = apply_ranking(before, plan.spans, ranking, placed=plan.placed)
    if after == before:
        return unchanged
    rows = sequence_rows(strategy.get("resolved_clip_intents"))
    kept = all(after[index] == item for index, item in enumerate(before) if item in plan.fixed)
    if (
        not kept
        or sorted(after) != sorted(before)
        or apply_sequence(after, rows) != after
        or not one_order(
            strategy,
            brief,
            snapshot,
            after,
            played=plan.played,
            creator_id=creator_id,
            client_protocol=client_protocol,
        )
    ):
        return unchanged
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
    ``group`` and ``note`` are the creator's own words, dropped when they would show an id or a
    file name (`safe_words`) or ARE any clip's id, file name or file stem (`names_a_file`).
    """
    rows = {
        str(row.get("media_id")): row
        for row in snapshot.get("clip_assignments") or ()
        if isinstance(row, Mapping) and row.get("media_id")
    }
    files = ledger.file_tokens(plan.before, rows.values())
    span_of = {item: f"s{index}" for index, span in enumerate(plan.spans, 1) for item in span}
    shown = [item for item in plan.before if plan.played is None or item in plan.played]
    clips: list[dict[str, Any]] = []
    alias_to_id: dict[str, str] = {}

    def words(value: object) -> str:
        text = ledger.safe_words(value)
        return "" if ledger.names_a_file(text, files) else text

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
        group = words((groups or {}).get(media_id))
        if group:
            clip["group"] = group
        note = words(row.get("user_note") or row.get("creator_note"))
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
