"""Inventory creator instructions independently before grounding them in footage."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalSchemaError
from app.agents._schemas.creator_agent import CREATOR_REQUEST_MAX_CHARS
from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    ClipIntentPlannerOutput,
    ConfirmedTitleCopy,
    PlannedClipIntent,
    salvage_question,
)
from app.config import settings
from app.kria.brief_route import fold_text, loose_text
from app.kria.reply_language import current_reply_language, say
from app.kria.research_preview import RESEARCH_FACT
from app.kria.text_sequence import TEXT_SEQUENCE_KIND
from app.schemas.clip_intents import (
    CLIP_DESCRIPTION_ATTRIBUTE,
    CREATOR_EVIDENCE,
    CREATOR_SELECTED_EVIDENCE,
    FILE_NAME_EVIDENCE,
    MAX_CLIP_INTENTS,
    ClipAssignment,
    ClipIntent,
    ResolvedClipIntent,
    asks_clip_description,
    creator_label_problem,
    fit_intent_id,
    ground_caption,
    ground_label,
    ground_placeholder_label,
    is_clip_description_label,
)
from app.services.clip_facts import order_by_capture_time
from app.services.clip_file_names import ClipNameIndex, NameReference, names_only
from app.services.clip_intent_resolution import (
    IntentClip,
    IntentResolution,
    keep_chapters_in_filming_order,
    picker_eligible,
    resolve_clip_intents_for_turn,
)
from app.services.clip_selection import ClipSelections, RejectedClipSelection
from app.services.clip_understanding import clip_record, understanding_incomplete
from app.services.creator_label_reply import (
    LabelTextReply,
    can_keep_as_written,
    keeps_as_written,
    label_text_reply,
)
from app.services.spoken_quotes import regound_quoted_intents
from app.services.too_short_clips import resolve_too_short
from app.text_fold import loose_words

if TYPE_CHECKING:
    from app.kria.brief import CreativeBrief

log = structlog.get_logger()

ClipRefresh = Callable[[], Awaitable[list[IntentClip]]]


@dataclass(frozen=True)
class PlannedIntentResolution:
    requested_intents: list[ClipIntent]
    resolution: IntentResolution
    # KRI-680: "leave these clips out" asks the planner could not tie to clips
    # (`exclusion_dropped`); the turn's reply says so instead of dropping them silently.
    dropped_exclusions: int = 0
    # KRI-950: the planner's checked list (`_checked_inventory`), set when planning passed and
    # the resolver ran, so a turn that ends on a clip picker can keep it for the tap.
    inventory: dict[str, Any] | None = None
    checked_intents: tuple[ClipIntent, ...] = ()
    rejected_selections: tuple[RejectedClipSelection, ...] = ()
    # Leverkusen M2: previews of asks whose op the schema doesn't have (the planner's
    # ``unexpressed``). Never a question; the turn's draft names them (`unexpressed_notice`).
    unexpressed_asks: tuple[str, ...] = ()


# KRI-950: the record a picker turn keeps (`KriaTurnPlan.clip_intent_inventory`). A planner
# prompt change makes every kept record stale.
_INVENTORY_VERSION = 1
_PLANNER_PROMPT_VERSION = ClipIntentPlannerAgent.spec.prompt_version


def _inventory_key(planner_input: ClipIntentPlannerInput, request: str) -> str:
    """What the checked list was read from. The brief and candidate hints are left out: they are
    recall aids only, and the Main Creator rewords them on every turn."""
    basis = {
        "prompt_version": _PLANNER_PROMPT_VERSION,
        "request": request,
        "available_clip_count": planner_input.available_clip_count,
        "clip_facts": planner_input.clip_facts,
        "reply_language": planner_input.reply_language,
        "clip_exclusions": planner_input.clip_exclusions,
    }
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


def _checked_inventory(
    output: ClipIntentPlannerOutput, planner_input: ClipIntentPlannerInput
) -> dict[str, Any]:
    return {
        "version": _INVENTORY_VERSION,
        "key": _inventory_key(planner_input, planner_input.creator_request),
        "output": output.model_dump(mode="json"),
    }


def _reused_inventory(
    saved: Any, planner_input: ClipIntentPlannerInput
) -> ClipIntentPlannerOutput | None:
    """The asking turn's checked list, when the picker answer is this turn's only new line.

    The caller passes ``saved`` only for a turn whose sole input is that picker's tap or skip,
    whose stand-in sentence (``model_text``) ends ``creator_request``. Any other words, another
    planner prompt or a broken record re-plans, exactly as before KRI-950."""
    if not isinstance(saved, Mapping) or saved.get("version") != _INVENTORY_VERSION:
        return None
    latest = (planner_input.latest_user_message or "").strip()
    request = planner_input.creator_request
    if not latest or not request.endswith("\n" + latest):
        return None
    if saved.get("key") != _inventory_key(planner_input, request[: -len(latest) - 1]):
        return None
    try:
        output = ClipIntentPlannerOutput.model_validate(saved.get("output"))
    except ValidationError:
        return None
    if output.salvage_question or output.question:
        return None
    return output


# How often a turn re-reads clip analysis while background understanding lands.
UNDERSTANDING_POLL_S = 2.0


def _unanalysed(clips: list[IntentClip]) -> list[IntentClip]:
    return [c for c in clips if understanding_incomplete(c.analysis, kind=c.kind)]


def _with_fresh_analysis(clips: list[IntentClip], fresh: list[IntentClip]) -> list[IntentClip]:
    """Take newly landed analysis for the SAME clips; never add, drop, or re-identify one.

    The turn's manifest was pinned before the wait, so a clip attached, removed, or
    replaced meanwhile keeps its original record and the confirm fence handles it.
    """
    by_id = {clip.media_id: clip for clip in fresh}
    out: list[IntentClip] = []
    for clip in clips:
        update = by_id.get(clip.media_id)
        same_object = (
            update is not None
            and update.gcs_path == clip.gcs_path
            and update.generation == clip.generation
        )
        out.append(replace(clip, analysis=update.analysis) if same_object else clip)
    return out


async def wait_for_clip_understanding(
    clips: list[IntentClip], *, refresh: ClipRefresh, until: float
) -> tuple[list[IntentClip], float]:
    """Re-read clip analysis until every clip is understood or ``time.monotonic()`` passes
    ``until``. Returns the clips and the seconds spent.

    The turn's clip snapshot is taken before the Main Creator call, so analysis that
    landed during it was invisible and the creator was told to resend a request the
    server could already answer (prod thread b2a41da6: "still checking" at 10:56:02Z,
    all 7 clips analysed at 10:56:03Z). The first re-read happens even past ``until``.
    A failed re-read keeps the clips as they were (the caller replies pending).
    """
    started = time.monotonic()
    while True:
        try:
            clips = _with_fresh_analysis(clips, await refresh())
        except Exception:  # noqa: BLE001 - a missed re-read is the old "still checking" reply
            log.warning("clip_intent_understanding_refresh_failed", exc_info=True)
            break
        remaining = until - time.monotonic()
        if not _unanalysed(clips) or remaining <= 0:
            break
        await asyncio.sleep(min(UNDERSTANDING_POLL_S, remaining))
    return clips, round(time.monotonic() - started, 1)


def resolve_order_by_intent(intent: ClipIntent, clips: list[IntentClip]) -> ResolvedClipIntent:
    """Resolve a ``by_capture_time`` / ``by_route`` order deterministically (KRI-189).

    It orders EVERY clip by a fact the phone recorded, so there is nothing for a
    vision model to decide: membership is all clips, at full confidence, and the
    planner sorts by each clip's capture time.
    """
    return ResolvedClipIntent(
        **intent.model_dump(),
        status="resolved",
        assignments=[
            ClipAssignment(
                media_id=clip.media_id,
                evidence="filming order",
                confidence=1.0,
            )
            for clip in clips
            if intent.accepts_source_kind(clip.kind)
        ],
    )


# KRI-612: the label the server adds when the brief asks for a line on each clip.
CLIP_DESCRIPTIONS_INTENT_ID = "clip-descriptions"


def _caption_asking_description(intent: ClipIntent) -> bool:
    return (
        intent.op == "caption"
        and intent.creator_text is None
        and (
            asks_clip_description(intent.caption_attribute)
            or asks_clip_description(intent.attribute)
        )
    )


def with_clip_descriptions(intents: list[ClipIntent]) -> list[ClipIntent]:
    """Make ONE description label carry "explain / summarise each clip" (KRI-612).

    Only called when the Creative Brief asks for described text on each clip. The
    inventory planner may read "a summary" as one chapter caption (a phone montage never
    prints a caption Kria wrote, and one caption is not text on each clip) or drop the ask
    as context. Such a caption is replaced by the label and a missing label is added; a
    description label the planner already wrote is kept as it is.
    """
    intents = [
        intent.model_copy(
            update={
                "op": "label",
                "attribute": CLIP_DESCRIPTION_ATTRIBUTE,
                "caption_attribute": None,
            }
        )
        if intent.source_kind is not None and _caption_asking_description(intent)
        else intent
        for intent in intents
    ]
    descriptions = [intent for intent in intents if is_clip_description_label(intent)]
    has_global_label = any(intent.source_kind is None for intent in descriptions)
    has_global_caption = any(_caption_asking_description(intent) for intent in intents)
    kept = [intent for intent in intents if not _caption_asking_description(intent)]
    if has_global_label or (descriptions and not has_global_caption):
        return kept
    if len(kept) >= MAX_CLIP_INTENTS:
        return intents
    taken = {intent.intent_id for intent in kept}
    intent_id, n = CLIP_DESCRIPTIONS_INTENT_ID, 2
    while intent_id in taken:
        intent_id, n = f"{CLIP_DESCRIPTIONS_INTENT_ID}-{n}", n + 1
    return [
        *kept,
        ClipIntent(intent_id=intent_id, op="label", attribute=CLIP_DESCRIPTION_ATTRIBUTE),
    ]


def without_spoken_text_captions(
    intents: list[ClipIntent], spoken_text: Sequence[str]
) -> list[ClipIntent]:
    """Leave out the `caption` intents that are a text line timed to a spoken word (KRI-692).

    "Show '20 dk' when I say 'yirmi dakika'" is a moment in the voiceover, not a chapter of
    clips, so the picker's "which clips?" had no right answer and the English label the
    model gave it ("the moment ... is said") sat in a Turkish chat. ``spoken_text`` is the
    brief's own exact text for such asks; only a caption carrying exactly that text goes,
    so a real chapter caption, a label or an include is never touched. The ask itself is
    kept by the brief and answered by its receipt.
    """
    if not spoken_text:
        return intents
    cues = {fold_text(text) for text in spoken_text}
    return [
        intent
        for intent in intents
        if not (
            intent.op == "caption"
            and intent.creator_text is not None
            and fold_text(intent.creator_text) in cues
        )
    ]


def whole_video_text_asks(brief: CreativeBrief | None) -> list[str]:
    """The creator's words for each live whole-video text another lane writes (KRI-945).

    A researched global text (``facts.research``) is written and sourced by the research
    lane, a title by the title lane; the words are the research request quote, the literal
    and the description. A global text with only a literal or a description is left out:
    prod briefs scope chapter captions ("Bölüm başlıkları") and spoken-word texts global,
    and those are placed on clips.
    """
    if brief is None:
        return []
    asks: list[str] = []
    for requirement in brief.live():
        research = requirement.facts.get(RESEARCH_FACT)
        if requirement.kind != "text" or not (
            requirement.scope == "title"
            or (requirement.scope == "global" and isinstance(research, Mapping))
        ):
            continue
        quote = research.get("request_quote") if isinstance(research, Mapping) else None
        words = [quote, requirement.literal, requirement.description]
        asks.append(" ".join(w for w in words if isinstance(w, str)))
    return asks


# Share of a caption quote's words that must come from a whole-video ask for the caption to
# be that ask. A separate clip caption in the same message shares only glue words.
_WHOLE_VIDEO_QUOTE_SHARE = 0.75


def _quotes_ask(quote: str, ask: str) -> bool:
    said = set(loose_words(quote))
    shared = said & set(loose_words(ask))
    return len(shared) >= 2 and len(shared) >= _WHOLE_VIDEO_QUOTE_SHARE * len(said)


def sequence_text_literals(brief: CreativeBrief | None) -> list[str]:
    """The exact words of each live ``text_sequence`` ask (29 Ekim): shown a piece at a time
    over the video by the text lane, never a clip caption."""
    if brief is None:
        return []
    return [req.literal for req in brief.live() if req.kind == TEXT_SEQUENCE_KIND and req.literal]


def _same_copy(text: str, literal: str) -> bool:
    """Exact copy equality (Unicode form and spacing aside), never a reading of the words."""

    def canonical(value: str) -> str:
        return " ".join(unicodedata.normalize("NFC", value).split())

    return canonical(text) == canonical(literal)


def without_whole_video_captions(
    intents: list[PlannedClipIntent],
    asks: Sequence[str],
    sequence_texts: Sequence[str] = (),
) -> list[PlannedClipIntent]:
    """Leave out the `caption` intents the resolver would author for a whole-video text.

    "Some facts about Cunda, an introductory explanation" became a footage caption, footage
    never grounds researched facts, and every turn asked what the caption should say. Only
    a caption with no creator words whose quote is that ask goes: the creator's own words,
    and a caption on clips asked for separately, are never touched.

    ``sequence_texts`` (29 Ekim, `sequence_text_literals`): a caption whose exact words are a
    live ``text_sequence`` ask is that ask. Live, "add this text “...”. Show each word one
    after the other" also came back as a `caption` on "each word" with the whole sentence, the
    resolver put it on all 28 clips, and the sentence would have sat static under its own
    word-by-word sequence. A caption with any other words is kept.
    """
    return [
        intent
        for intent in intents
        if not (
            intent.op == "caption"
            and (
                (
                    intent.creator_text is None
                    and any(_quotes_ask(intent.source_quote, ask) for ask in asks)
                )
                or (
                    intent.creator_text is not None
                    and any(_same_copy(intent.creator_text, text) for text in sequence_texts)
                )
            )
        )
    ]


def _creator_label_texts(intents: list[ClipIntent]) -> list[str]:
    """The creator's own words on clip labels (never a placeholder or a narration label)."""
    return [
        intent.creator_text
        for intent in intents
        if intent.op == "label"
        and intent.label_source == "clip"
        and not intent.placeholder
        and intent.creator_text
    ]


def unusable_label_reply(
    intents: list[ClipIntent], *, shot_captions: bool = True
) -> LabelTextReply | None:
    """A plain question when the creator's own label words can never be printed (KRI-678).

    Such a label can never be grounded, so the clip picker would ask "which clips?"
    again after every answer. The reply names every reason the text can't be a label
    and offers tappable ways forward (KRI-804); None when every creator label can print.
    "Keep it as written" is offered only when the label may become the shot's caption.
    """
    return label_text_reply(_creator_label_texts(intents), keep=shot_captions)


def shot_captions_safe(brief: CreativeBrief | None, placed_texts: Sequence[str]) -> bool:
    """May the server turn a creator's shot text into the shot's caption (KRI-804)?

    One creator caption puts the montage in described-shot mode
    (`unified_montage.plan_unified_montage`): positional ``shot_labels`` and the every-clip
    texts (filming time, place, placeholder) stop printing. So only when the edit asks
    for none of them; otherwise the words stay a label and the reply asks about them.
    """
    if any(str(text).strip() for text in placed_texts):
        return False
    return brief is None or all(
        requirement.is_shot_text
        for requirement in brief.live()
        if requirement.kind == "text" and requirement.scope == "per_clip"
    )


def with_labels_kept_as_written(
    intents: list[ClipIntent], creator_messages: str
) -> list[ClipIntent]:
    """A label the creator chose to keep "exactly as written" goes as its shot's caption (KRI-804).

    The described-shot caption lane prints the creator's own words on the clips it is
    resolved to (KRI-296), with none of a label's length or character bounds. The choice
    is one of the creator's messages, so it holds on every later turn. Labels that print,
    or that the creator did not choose to keep, are left as they are.
    """
    kept: list[ClipIntent] = []
    for intent in intents:
        text = intent.creator_text
        if (
            text
            and _creator_label_texts([intent])
            and creator_label_problem(text) is not None
            and can_keep_as_written(text)
            and keeps_as_written(text, creator_messages)
        ):
            intent = ClipIntent(
                intent_id=intent.intent_id,
                op="caption",
                attribute=intent.attribute,
                source_kind=intent.source_kind,
                creator_text=text,
            )
        kept.append(intent)
    return kept


def apply_creator_selections(
    intents: list[ClipIntent],
    *,
    clips: list[IntentClip],
    selections: ClipSelections | None,
    creator_request: str,
) -> tuple[list[ResolvedClipIntent], list[ClipIntent], set[str]]:
    """Honour the creator's clip-picker answers BEFORE the resolver runs (KRI-282).

    Returns ``(settled, remaining, dropped_ids)``:

    * ``settled``   intents the creator's tapped clips fully answer, as resolved intents
                    (confidence 1.0, evidence "creator selected"); they never reach the
                    resolver or the vision lane.
    * ``remaining`` everything else, resolved as usual.
    * ``dropped``   intents the creator said have no clips: empty-by-creator, removed from
                    the request entirely so they are never asked about again.
    """
    if not selections:
        return [], list(intents), set()
    by_id = {c.media_id: c for c in clips}
    order = {c.media_id: i for i, c in enumerate(clips)}
    settled: list[ResolvedClipIntent] = []
    remaining: list[ClipIntent] = []
    dropped: set[str] = set()
    for intent in intents:
        entry = selections.match(intent)
        if entry is None:
            remaining.append(intent)
            continue
        if entry.none:
            dropped.add(intent.intent_id)
            continue
        media_ids = sorted((m for m in entry.media_ids if m in by_id), key=order.__getitem__)
        resolved = _settle(
            intent, media_ids, by_id, creator_request, evidence=CREATOR_SELECTED_EVIDENCE
        )
        if resolved is None:
            remaining.append(intent)
            continue
        settled.append(resolved)
    return settled, remaining, dropped


def _nameable(intent: ClipIntent) -> bool:
    """True when a clip the creator names by file name fully answers this intent.

    Whatever a tapped clip answers (``picker_eligible``), plus ``exclude`` (KRI-763): "skip
    M1_05" names the clip to leave out, and an exclude is never a picker category.
    """
    return intent.op == "exclude" or picker_eligible(intent)


def superseded_pin_ids(
    *,
    intents: list[ClipIntent],
    pinned: list[ResolvedClipIntent],
    latest_user_message: str | None,
    quotes: Mapping[str, str],
) -> set[str]:
    """Earlier first/last pins that a file-name pin in the latest message replaces (KRI-763).

    The whole conversation is re-planned every turn, so "first = the clip where Caro brings
    the tray" (message 1) and "first clip M1_08" (message 2) both come back as ``first``
    pins: two clips led the edit, or the first one was asked about again though the creator
    had just named the clip. A later message that pins a position by file name decides it.

    ``pinned`` are the intents settled by file name this turn; ``quotes`` maps intent ids to
    the creator words each came from. Only a pin whose words are NOT in the latest message
    is replaced: pins from one message stack as before, and an intent of unknown origin
    stays. ``then`` rows (no position) are never touched.
    """
    latest = fold_text(latest_user_message or "")
    if not latest:
        return set()

    def in_latest(intent_id: str) -> bool:
        quote = quotes.get(intent_id)
        return bool(quote) and fold_text(quote) in latest

    positions = {
        pin.position
        for pin in pinned
        if pin.op == "order"
        and pin.position in ("first", "last")
        and len(pin.assignments) == 1
        and in_latest(pin.intent_id)
    }
    return {
        intent.intent_id
        for intent in intents
        if intent.op == "order"
        and intent.position in positions
        and not intent.order_by
        and intent.intent_id in quotes
        and not in_latest(intent.intent_id)
    }


def apply_file_name_references(
    intents: list[ClipIntent],
    *,
    clips: list[IntentClip],
    creator_request: str,
) -> tuple[list[ResolvedClipIntent], list[ClipIntent], dict[str, list[ClipIntent]]]:
    """Settle intents that name clips by the file name the app shows (KRI-614).

    "first ımg_7382 then ımg_7820" names clips the resolver cannot see: its records carry
    what a clip shows, never its file name, so it used to ask "which of your clips match
    ımg_7382?". An intent whose attribute is nothing but file names, each naming exactly
    one clip (``services.clip_file_names``), is settled here like a tapped answer. So is
    one whose words the creator tied to file names in a sentence of their own ("The goals
    are IMG_1037 and IMG_1039." for "the two goal clips", KRI-1112:
    ``ClipNameIndex.subject_references``).

    Returns ``(settled, remaining, split)``. An ``order`` intent naming several clips is a
    sequence: ``split`` maps its id to one order intent per clip, in the order named, each
    keeping the intent's ``position``. A clip in it that a single-clip first/last intent
    already pins stays with that intent, so "... and at the end Img_7833" still closes the
    edit after the clips nobody named.
    """
    index = ClipNameIndex({clip.media_id: clip.file_name for clip in clips})
    if not index:
        return [], list(intents), {}
    by_id = {clip.media_id: clip for clip in clips}
    named: dict[str, list[NameReference]] = {}
    for intent in intents:
        if intent.label_source != "clip" or intent.order_by or not _nameable(intent):
            continue
        refs, others = index.references(intent.attribute)
        if refs and names_only(others) and all(ref.media_id for ref in refs):
            named[intent.intent_id] = refs
        elif not refs:
            # KRI-1112: "the two goal clips" after "The goals are IMG_1037 and IMG_1039."
            bound = index.subject_references(creator_request, intent.attribute)
            if bound is not None:
                named[intent.intent_id] = bound
    pinned: set[str] = set()
    for intent in intents:
        refs = named.get(intent.intent_id, [])
        if intent.op == "order" and intent.position is not None and len(refs) == 1:
            pinned.add(str(refs[0].media_id))
    settled: list[ResolvedClipIntent] = []
    remaining: list[ClipIntent] = []
    split: dict[str, list[ClipIntent]] = {}
    for intent in intents:
        refs = named.get(intent.intent_id)
        if refs is None:
            remaining.append(intent)
            continue
        surface = {str(ref.media_id): ref.surface for ref in reversed(refs)}
        media_ids = list(dict.fromkeys(str(ref.media_id) for ref in refs))
        if intent.op == "order" and len(media_ids) > 1:
            kept = [media_id for media_id in media_ids if media_id not in pinned]
            pieces = [
                intent.model_copy(
                    update={
                        "intent_id": fit_intent_id(f"{intent.intent_id}-{number}"),
                        "attribute": surface[media_id],
                    }
                )
                for number, media_id in enumerate(kept, start=1)
            ]
            seated = [
                _settle(piece, [media_id], by_id, creator_request, evidence=FILE_NAME_EVIDENCE)
                for piece, media_id in zip(pieces, kept, strict=True)
            ]
            if any(piece is None for piece in seated):
                remaining.append(intent)
                continue
            settled.extend(piece for piece in seated if piece is not None)
            split[intent.intent_id] = pieces
            continue
        resolved = _settle(intent, media_ids, by_id, creator_request, evidence=FILE_NAME_EVIDENCE)
        if resolved is None:
            remaining.append(intent)
            continue
        settled.append(resolved)
    return settled, remaining, split


@dataclass(frozen=True)
class BriefLabel:
    """Exact words the creator dictated for one described shot (a brief ``is_shot_text``)."""

    literal: str
    description: str


@dataclass(frozen=True)
class KeptLabel:
    """A label the current draft already printed on these clips."""

    text: str
    media_ids: tuple[str, ...]


def label_key(text: str | None) -> str:
    """A label's words without case, accents or punctuation ("Oblea de la Casa!" == itself)."""
    return " ".join(re.findall(r"\w+", loose_text(text or "")))


def brief_label_asks(
    brief: CreativeBrief | None,
    placed_texts: Sequence[str],
    spoken_text: Sequence[str] = (),
) -> list[BriefLabel]:
    """The brief's live dictated shot texts that no positional ``shot_labels`` row carries.

    A text in ``placed_texts`` is already pinned by shot index (the render contract reads
    that row), so asking where it goes would be a second, conflicting placement. A text
    timed to a spoken word (``spoken_text``) is a moment in the voiceover, never a clip.
    """
    if brief is None:
        return []
    placed = {label_key(text) for text in [*placed_texts, *spoken_text]}
    asks: list[BriefLabel] = []
    seen: set[str] = set()
    for requirement in brief.live():
        if not requirement.is_shot_text:
            continue
        key = label_key(requirement.literal)
        if not key or key in placed or key in seen:
            continue
        seen.add(key)
        asks.append(BriefLabel(str(requirement.literal), str(requirement.description)))
    return asks


def kept_labels_from_strategy(strategy: Mapping[str, Any] | None) -> list[KeptLabel]:
    """Creator-worded labels a draft's strategy already resolved, with the clips they sit on.

    Only an intent's own ``creator_text`` counts: a per-clip ``value`` the model wrote
    ("Name", "Players stretching") is not words the creator chose and never binds.
    """
    rows = strategy.get("resolved_clip_intents") if isinstance(strategy, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        return []
    texts: dict[str, str] = {}
    media: dict[str, list[str]] = {}
    for intent in rows:
        if not isinstance(intent, Mapping) or intent.get("op") != "label":
            continue
        if str(intent.get("status") or "resolved") != "resolved":
            continue
        for row in intent.get("assignments") or []:
            if not isinstance(row, Mapping):
                continue
            media_id = str(row.get("media_id") or "")
            text = intent.get("creator_text")
            key = label_key(text) if isinstance(text, str) else ""
            if not media_id or not key:
                continue
            texts.setdefault(key, str(text))
            if media_id not in media.setdefault(key, []):
                media[key].append(media_id)
    return [KeptLabel(text=texts[key], media_ids=tuple(ids)) for key, ids in media.items()]


def with_brief_labels(
    intents: list[ClipIntent], asks: Sequence[BriefLabel], *, shot_captions: bool = True
) -> list[ClipIntent]:
    """Add the dictated shot text the inventory planner left out of this turn's intents (KRI-766).

    The planner re-reads the whole thread every turn and may not repeat a label the creator
    asked for earlier ("first = the tray clip" says nothing about it). The brief still holds
    the words and the shot, and the render contract refuses a text nobody placed, so the
    label joins the inventory and is resolved, or asked with the picker, like any other.
    One the planner did emit (same words, whatever the case) is left as it is, whether it
    wrote a label or a caption for it. Words a label can never print (too long, a "+")
    were valid dictated text, so asking to shorten them would blame the creator (KRI-766)
    and skipping them dead-ended the turn on "which clip" (KRI-804): they join as the
    shot's caption, the lane that prints the creator's words as written (KRI-296). Words
    no lane can print, or a caption that would turn off other texts (``shot_captions``
    False, see ``shot_captions_safe``), join as a label, whose reply names every reason.
    """
    have = {
        label_key(i.creator_text)
        for i in intents
        if i.op in {"label", "caption"} and i.creator_text
    }
    taken = {intent.intent_id for intent in intents}
    added: list[ClipIntent] = []
    for ask in asks:
        key = label_key(ask.literal)
        if not key or key in have or len(intents) + len(added) >= MAX_CLIP_INTENTS:
            continue
        op = (
            "caption"
            if shot_captions
            and creator_label_problem(ask.literal) is not None
            and can_keep_as_written(ask.literal)
            else "label"
        )
        intent_id, n = fit_intent_id(f"{op}-{key.replace(' ', '-')}"), 2
        while intent_id in taken:
            intent_id, n = fit_intent_id(f"{op}-{key.replace(' ', '-')}-{n}"), n + 1
        try:
            added.append(
                ClipIntent(
                    intent_id=intent_id,
                    op=op,
                    attribute=" ".join(ask.description.split())[:160],
                    creator_text=ask.literal,
                )
            )
        except ValidationError:
            continue
        have.add(key)
        taken.add(intent_id)
    return [*intents, *added] if added else intents


# KRI-1111: words that never tell two things apart ("my ticket" is the ticket), as in
# `clip_order_sequence`'s moment names.
_THING_FILLER = frozenset({"a", "an", "the", "my", "our", "your", "his", "her", "its", "their"})
# Ops whose text the resolver writes from the footage unless the creator dictated it.
_AUTHORED_TEXT_OPS = frozenset({"label", "caption"})


def _thing(text: str | None) -> tuple[str, ...]:
    return tuple(word for word in loose_words(text or "") if word not in _THING_FILLER)


def excluded_anchors(intents: Sequence[ClipIntent], anchors: Mapping[str, str]) -> dict[str, str]:
    """The brief's first / last clips this turn's inventory leaves out instead (KRI-1111).

    Leverkusen M3 (thread 83f2dcaa, 2026-10-31): "Start with my ticket", then "Then leave the
    ticket out" and "Leave the ticket out completely". The brief kept ``first_clip: "my
    ticket"`` (a change that leaves a sticky fact out carries it back, KRI-522); the planner,
    which reads every creator message, pinned no first clip and wrote an ``exclude`` for "the
    ticket" (13:21 and 13:27). A clip left out of the edit cannot open it, so such an end is
    not added back (``with_brief_anchors``) and the turn takes it off the brief
    (``anchor_exclusions.withdraw_excluded_anchors``).

    Only a leave-out of the same thing counts: exactly the end's words, case, accents,
    articles and "my" / "your" aside. "The blurry clip" or "the ticket stub" leave it as it
    is, and so does a spot the planner pinned itself.
    """
    pinned = {i.position for i in intents if i.op == "order" and not i.order_by and i.position}
    left_out = {_thing(i.attribute) for i in intents if i.op == "exclude"} - {()}
    out: dict[str, str] = {}
    for position in ("first", "last"):
        words = " ".join(str(anchors.get(position) or "").split())
        if words and position not in pinned and _thing(words) in left_out:
            out[position] = words
    return out


def authored_text_withdrawn(brief: CreativeBrief | None) -> bool:
    """The creator withdrew every brief ask for clip text the server writes (KRI-1111).

    Leverkusen M3: "Label each goal clip with who scored and in which minute" (r5) was
    superseded by "Leave the names and minutes out, just write "TOR!"" (13:22), yet the planner,
    reading every creator message, wrote the scorer and minute labels again on three turns and
    the creator was asked "I couldn't find any clips for "who scored"" each time. Only text
    asks with no dictated words count (a title, or "TOR!", is never authored). False when one
    is still live, or when the brief never recorded one: the planner's labels then stand.
    """
    if brief is None:
        return False
    asks = [
        req
        for req in brief.requirements
        if req.kind == "text" and req.scope != "title" and not req.literal
    ]
    return bool(asks) and not any(req.live for req in asks)


def with_brief_anchors(intents: list[ClipIntent], anchors: Mapping[str, str]) -> list[ClipIntent]:
    """Seat the first / last clip a filming-order brief names that no planned row places (KRI-1067).

    Leverkusen M1 (2026-10-29): "chronological order from arrival to the final whistle" kept
    ``last_clip: "the final whistle"`` on the brief, but the planner wrote no ``last`` pin for
    it. Nothing asked which clip that is, and the render contract refuses a stated first / last
    clip no sequence row seats (``stated_anchors``), so the turn ended on a reply with nothing
    to tap. Like a dictated label (``with_brief_labels``), the clip joins the inventory and is
    resolved, or asked with the picker; a tap or a None answer settles it the usual way
    (KRI-282, KRI-1025). Its id and attribute come from the brief's words alone, so the answer
    still binds on the next turn, when the planner may leave it out again.

    A spot the planner already pinned (in any words) is left as it is, and so is an end the
    creator has since left out (``excluded_anchors``, KRI-1111): a clip left out of the edit
    is never seated or added back as its first / last clip. Words it already wrote as a plain
    order row are that row's spot, not a second row's (two rows for the same words would
    argue, KRI-764), so that row takes the position in place (KRI-1123). Leverkusen M4
    (2026-10-31): "tell the evening as a story, in this order: the stadium from outside, ...
    the Nürnberg players thanking their fans at the end" was planned as one plain row per beat,
    so the brief's ``first_clip`` / ``last_clip`` each had a row and neither was seated; the
    contract refused by name, and the readiness gate copied the refusal onto every requirement.
    A row for the same words pinned to the other spot or ordered by a fact (``order_by``) is
    left alone.
    """
    rows = [i for i in intents if i.op == "order" and not i.order_by]
    pinned = {i.position for i in rows if i.position}
    have = {label_key(i.attribute) for i in rows}
    taken = {intent.intent_id for intent in intents}
    left_out = excluded_anchors(intents, anchors)
    seated = list(intents)
    added: list[ClipIntent] = []
    for position in ("first", "last"):
        words = " ".join(str(anchors.get(position) or "").split())
        key = label_key(words)
        if not key or position in pinned or position in left_out:
            continue
        same = next(
            (
                n
                for n, intent in enumerate(seated)
                if intent.op == "order"
                and not intent.order_by
                and intent.position is None
                and label_key(intent.attribute) == key
            ),
            None,
        )
        if same is not None:
            seated[same] = seated[same].model_copy(update={"position": position})
            pinned.add(position)
            continue
        if key in have or len(intents) + len(added) >= MAX_CLIP_INTENTS:
            continue
        intent_id, n = fit_intent_id(f"order-{position}-{key.replace(' ', '-')}"), 2
        while intent_id in taken:
            intent_id, n = fit_intent_id(f"order-{position}-{key.replace(' ', '-')}-{n}"), n + 1
        try:
            added.append(
                ClipIntent(intent_id=intent_id, op="order", position=position, attribute=words)
            )
        except ValidationError:
            continue
        have.add(key)
        taken.add(intent_id)
    return [*seated, *added]


def with_filming_end_ticks(
    resolution: IntentResolution, positions: Mapping[str, str], clips: Sequence[IntentClip]
) -> IntentResolution:
    """Pre-tick the end of filming order on the picker row of a brief's first / last clip.

    ``positions`` maps the ids ``with_brief_anchors`` added to their spot. On a filming-order
    edit the clip a creator most likely means by "to the final whistle" is the last one they
    filmed, so that row ticks it (the first filmed for a first clip) and the reply says so. A
    tick is a suggestion: nothing is placed until the creator taps. A row the resolver already
    ticked keeps its own suggestion, and a clip with no filming time is never ticked. The ends
    come from the sort the montage uses (``order_by_capture_time``), so with fewer than two
    dated clips there is no filming order and nothing is ticked.
    """
    question = resolution.clip_question
    if not question or not positions:
        return resolution
    filmed = {c.media_id: c.capture_time for c in clips if c.capture_time is not None}
    categories: list[Any] = []
    notes: list[str] = []
    for category in question.get("categories") or []:
        position = positions.get(str(category.get("intent_id") or ""))
        dated = [m for m in category.get("candidate_media_ids") or [] if m in filmed]
        ordering = order_by_capture_time(dated, filmed)
        if (
            position is None
            or category.get("suggested_media_ids")
            or ordering.basis != "capture_time"
        ):
            categories.append(category)
            continue
        end = ordering.ordered_ids[-1 if position == "last" else 0]
        categories.append({**category, "suggested_media_ids": [end]})
        last = position == "last"
        notes.append(
            say(
                en=f"I ticked the {'last' if last else 'first'} clip you filmed for "
                f'"{category.get("label")}".',
                tr=f'"{category.get("label")}" için çektiğin {"son" if last else "ilk"} klibi '
                "işaretledim.",
            )
        )
    if not notes:
        return resolution
    return replace(
        resolution,
        clip_question={**question, "categories": categories},
        question=" ".join([(resolution.question or "").strip(), *notes]).strip(),
    )


# Words that mean "the text on a clip" in the chats we serve (folded: no case, no accents).
_LABEL_NOUNS = ("label", "etiket", "yazi", "caption")
_LONGER_WORD = 4


def _touches_label(said: str, label_text: str) -> bool:
    """True when the creator's message (already ``label_key``-folded) is about this label.

    Its whole phrase, any word of it with at least four letters ("the Oblea label" for
    "Oblea de la casa"), or a general word for on-screen text: any of them can be the
    creator re-describing where the label goes, so the old placement must not decide.
    """
    if not said:
        return False
    words = said.split()
    label_words = label_key(label_text).split()
    return (
        f" {' '.join(label_words)} " in f" {said} "
        or any(w in words for w in label_words if len(w) >= _LONGER_WORD)
        or any(w.startswith(_LABEL_NOUNS) for w in words)
    )


def apply_kept_labels(
    intents: list[ClipIntent],
    *,
    clips: list[IntentClip],
    kept: Sequence[KeptLabel],
    latest_user_message: str | None,
    creator_request: str,
) -> tuple[list[ResolvedClipIntent], list[ClipIntent]]:
    """A label the draft already placed stays on the same clips unless the creator touches it.

    Returns ``(settled, remaining)``. The planner and resolver re-decide every intent each
    turn, so an unrelated message (a new order, a title) could move a label the creator
    never mentioned (KRI-766). The latest message touching the label (its words, any longer
    word of them, or the word "label"/"etiket"/"yazı") is a change and resolves it fresh,
    as does a bound clip that left the project. A tapped picker answer is applied before
    this and wins.
    """
    if not kept:
        return [], list(intents)
    by_id = {c.media_id: c for c in clips}
    order = {c.media_id: i for i, c in enumerate(clips)}
    bound = {label_key(entry.text): entry for entry in kept}
    said = label_key(latest_user_message)
    settled: list[ResolvedClipIntent] = []
    remaining: list[ClipIntent] = []
    for intent in intents:
        entry = bound.get(label_key(intent.creator_text)) if intent.op == "label" else None
        if (
            entry is None
            or _touches_label(said, entry.text)
            or not all(media_id in by_id for media_id in entry.media_ids)
        ):
            remaining.append(intent)
            continue
        media_ids = sorted(entry.media_ids, key=order.__getitem__)
        resolved = _settle(
            intent, media_ids, by_id, creator_request, evidence="kept from your draft"
        )
        if resolved is None:
            remaining.append(intent)
            continue
        settled.append(resolved)
    return settled, remaining


def _settle(
    intent: ClipIntent,
    media_ids: list[str],
    by_id: dict[str, IntentClip],
    creator_request: str,
    *,
    evidence: str,
) -> ResolvedClipIntent | None:
    """``intent`` resolved on exactly these clips, or None when its text cannot be grounded."""
    if any(not intent.accepts_source_kind(by_id[m].kind) for m in media_ids):
        return ResolvedClipIntent(
            **intent.model_dump(),
            status="needs_creator",
            assignments=[],
            question=say(
                en=(
                    "Your chosen clip differs from the source type you requested. "
                    "Please confirm the source type or change the clip choice."
                ),
                tr=(
                    "Seçtiğin klip istediğin kaynak türünden farklı. "
                    "Lütfen kaynak türünü onayla veya klip seçimini değiştir."
                ),
            ),
        )
    assignments = _selected_assignments(
        intent, media_ids, by_id, creator_request, evidence=evidence
    )
    if not media_ids or not _nameable(intent) or assignments is None:
        return None
    caption_text = caption_grounding = None
    if intent.op == "caption":
        # KRI-696: picker-eligible captions carry the creator's own copy, so ground it
        # the way the resolver does; the short-phrase bounds refused "·" / "’" and the
        # picker asked the same question forever.
        grounded = ground_caption(
            value=intent.creator_text,
            confidence=1.0,
            creator_request=creator_request,
            records=[clip_record(by_id[m].analysis, kind=by_id[m].kind) for m in media_ids],
            intent_id=intent.intent_id,
            creator_copy=True,
        )
        if grounded is None:
            return None
        caption_text, caption_grounding = grounded.text, grounded.grounding
    return ResolvedClipIntent(
        **intent.model_dump(),
        status="resolved",
        assignments=assignments,
        caption_text=caption_text,
        caption_grounding=caption_grounding,
    )


def _as_listed(
    resolved: list[ResolvedClipIntent], requested: list[ClipIntent]
) -> list[ResolvedClipIntent]:
    """Resolved intents in the order the creator listed them.

    The sequence rule (``clip_order_sequence``) seats ``order`` rows in list order, so an
    intent settled before the resolver (a tapped answer, a file name) must not move to the
    end of the list just because it skipped the resolver.
    """
    rank = {intent.intent_id: position for position, intent in enumerate(requested)}
    return sorted(resolved, key=lambda intent: rank.get(intent.intent_id, len(rank)))


def _selected_assignments(
    intent: ClipIntent,
    media_ids: list[str],
    by_id: dict[str, IntentClip],
    creator_request: str,
    *,
    evidence: str,
) -> list[ClipAssignment] | None:
    """Assignments for tapped clips, or None when the intent's text cannot be grounded."""
    assignments: list[ClipAssignment] = []
    for media_id in media_ids:
        value = grounding = None
        if intent.op == "label":
            if intent.placeholder:
                label = ground_placeholder_label(media_id=media_id, intent_id=intent.intent_id)
            else:
                clip = by_id[media_id]
                label = ground_label(
                    media_id=media_id,
                    value=intent.creator_text,
                    confidence=1.0,
                    creator_request=creator_request,
                    record=clip_record(clip.analysis, kind=clip.kind),
                    intent_id=intent.intent_id,
                )
            if label is None:
                return None
            value, grounding = label.text, label.grounding
        assignments.append(
            ClipAssignment(
                media_id=media_id,
                value=value,
                evidence=evidence,
                confidence=1.0,
                grounding=grounding,
            )
        )
    return assignments


# KRI-991: the evidence on a match the asking turn found and the answering turn kept. Not the
# creator's word (``CREATOR_EVIDENCE``): a tap still outranks it.
ASKING_TURN_EVIDENCE = "asking turn"
_CARRIED_OPS = frozenset({"order", "include", "group"})


def _inventory_with_matches(
    inventory: dict[str, Any] | None, resolution: IntentResolution
) -> dict[str, Any] | None:
    """The checked list plus the confident order/include/group matches this turn found.

    A picker turn keeps it (``KriaTurnPlan.clip_intent_inventory``); the tap's turn re-resolves
    and ``carried_matches`` gives an intent that comes back unmatched the earlier match. The
    creator's own taps are not kept: they come back from the thread's answers every turn.
    """
    if inventory is None:
        return None
    matches = [
        {
            "intent_id": intent.intent_id,
            "op": intent.op,
            "attribute": intent.attribute,
            "position": intent.position,
            **({"source_kind": intent.source_kind} if intent.source_kind is not None else {}),
            "assignments": [
                {"media_id": a.media_id, "confidence": a.confidence, "evidence": a.evidence}
                for a in intent.assignments
            ],
        }
        for intent in resolution.intents
        if intent.op in _CARRIED_OPS
        and intent.status == "resolved"
        and intent.assignments
        and not any(a.evidence in CREATOR_EVIDENCE for a in intent.assignments)
    ]
    return {**inventory, "matches": matches} if matches else inventory


def carried_matches(
    saved: Any, intents: Sequence[ClipIntent], clips: Sequence[IntentClip]
) -> list[ResolvedClipIntent]:
    """The asking turn's matches for the intents this turn would otherwise re-resolve.

    ``saved`` is the asking turn's inventory, passed only when the planner list was reused (the
    tap was the turn's only new input, so no new words could have changed an ask). Applied only
    to the same intent (id, op, attribute and position all unchanged) and only to clips still
    attached. The creator's own taps never come here (they settle their intents first).
    Reads no words.
    """
    rows = saved.get("matches") if isinstance(saved, Mapping) else None
    if not isinstance(rows, list):
        return []
    attached = {clip.media_id: clip for clip in clips}
    out: list[ResolvedClipIntent] = []
    for intent in intents:
        row = next(
            (r for r in rows if isinstance(r, Mapping) and r.get("intent_id") == intent.intent_id),
            None,
        )
        if (
            row is None
            or intent.op not in _CARRIED_OPS
            or row.get("source_kind") != intent.source_kind
            or (row.get("op"), row.get("attribute"), row.get("position"))
            != (intent.op, intent.attribute, intent.position)
        ):
            continue
        try:
            assignments = [
                ClipAssignment(
                    media_id=a["media_id"],
                    confidence=a["confidence"],
                    evidence=ASKING_TURN_EVIDENCE,
                )
                for a in row.get("assignments") or []
                if a.get("media_id") in attached
                and intent.accepts_source_kind(attached[a["media_id"]].kind)
            ]
        except (KeyError, TypeError, ValidationError):
            continue
        if assignments:
            out.append(
                ResolvedClipIntent(
                    **intent.model_dump(), status="resolved", assignments=assignments
                )
            )
    return out


async def plan_and_resolve_clip_intents(
    *,
    creator_request: str,
    latest_user_message: str | None,
    candidate_intents: list[ClipIntent] | None,
    clips: list[IntentClip],
    run_context: RunContext,
    confirmed_titles: Sequence[ConfirmedTitleCopy] | None = None,
    generated_brief: str | None = None,
    background: bool = False,
    checkpoint: Any = None,
    max_vision_requeries: int | None = None,
    vision_deadline_s: float | None = None,
    require_clip_understanding: bool = False,
    refresh_clips: ClipRefresh | None = None,
    understanding_wait_until: float | None = None,
    clip_selections: ClipSelections | None = None,
    describe_clips: bool = False,
    spoken_text: Sequence[str] = (),
    brief_labels: Sequence[BriefLabel] = (),
    kept_labels: Sequence[KeptLabel] = (),
    shot_captions: bool = True,
    whole_video_texts: Sequence[str] = (),
    sequence_texts: Sequence[str] = (),
    checked_inventory: Any = None,
    brief_anchors: Mapping[str, str] | None = None,
    authored_text_asks_withdrawn: bool = False,
) -> PlannedIntentResolution:
    """``describe_clips`` (KRI-612): the Creative Brief asks for a line on each clip saying
    what it shows, so the inventory always carries one description label for it.

    ``spoken_text`` (KRI-692): exact texts the creator wants on screen when they say a word,
    on an edit that cannot draw them. A caption intent for one is never a clip question.

    ``brief_labels`` / ``kept_labels`` (KRI-766): the brief's dictated shot texts the planner may
    not repeat this turn (added to the inventory, so each is resolved or asked with the picker),
    and the labels the current draft already placed (kept on the same clips unless the creator
    names them again).

    ``shot_captions`` (KRI-804, ``shot_captions_safe``): whether a creator text a label can't
    print may become the shot's caption, which prints it as written.

    ``whole_video_texts`` (KRI-945, ``whole_video_text_asks``): the brief's researched texts and
    titles. A caption the resolver would author for one is never a clip question.

    ``sequence_texts`` (29 Ekim, `sequence_text_literals`): the brief's word-by-word texts; a
    caption carrying exactly those words is never planned (`without_whole_video_captions`).

    ``checked_inventory`` (KRI-950): the ``inventory`` of the turn that asked the clip picker
    this turn only answers. Reused instead of a new planner call when it was read from the same
    words (``_reused_inventory``): a re-plan is a fresh model answer, and one row it gets wrong
    asked the creator to restate an instruction they never touched.

    ``brief_anchors`` (KRI-1067, ``stated_anchors``): a filming-order brief's first / last
    clip, by the creator's words. One the inventory leaves unplaced joins it
    (``with_brief_anchors``), and its picker row ticks the end of filming order.

    ``authored_text_asks_withdrawn`` (KRI-1111, ``authored_text_withdrawn``): the creator
    withdrew every brief ask for clip text the server writes, so a label or caption the
    planner wrote without the creator's own words (fresh or reused) is left out, never asked."""
    if len(creator_request) > CREATOR_REQUEST_MAX_CHARS:
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=say(
                    en=(
                        "Please restate the complete clip instructions in a shorter message "
                        "so I can preserve all of them."
                    ),
                    tr=(
                        "Klip talimatlarının hepsini daha kısa bir mesajda yeniden yazar mısın? "
                        "Hiçbirini kaçırmak istemiyorum."
                    ),
                ),
            ),
        )
    # KRI-189: fact-based ordering is only understood for accounts with CLIP_FACTS.
    clip_facts_on = settings.clip_facts_for(getattr(run_context, "creator_id", None))
    # Candidates improve recall but are never the authority: even an empty
    # creative strategy must pass through the complete request inventory.
    planner_input = ClipIntentPlannerInput(
        creator_request=creator_request,
        latest_user_message=latest_user_message,
        generated_brief=generated_brief,
        candidate_intents=candidate_intents,
        confirmed_titles=list(confirmed_titles or ()),
        available_clip_count=len(clips),
        **({"clip_facts": True} if clip_facts_on else {}),
        reply_language=current_reply_language(),
        # KRI-667: the `exclude` op; omitted when off (byte-identical input).
        **({"clip_exclusions": True} if settings.clip_intent_exclude_enabled else {}),
    )
    reused = _reused_inventory(checked_inventory, planner_input)
    if checked_inventory is not None:
        log.info("clip_intent_planner.inventory_reuse", reused=reused is not None)
    try:
        output = reused or await asyncio.to_thread(
            ClipIntentPlannerAgent(default_client()).run, planner_input, ctx=run_context
        )
    except TerminalSchemaError as exc:
        # Invalid model output is a content-recovery problem, not a failed
        # preparation. Ask for a restatement and trust none of the candidate
        # intents. Refusals, transient exhaustion, and control-plane failures
        # remain terminal and are handled by their existing callers.
        cause = exc.__cause__
        error_class = getattr(cause, "error_class", None) or type(cause).__name__
        dropped = list(getattr(cause, "dropped", None) or [])
        # Redacted on purpose: the error CLASS and sizes only, never creator or
        # model text, so the next incident is diagnosable from logs alone.
        log.warning(
            "clip_intent_planner.terminal_schema",
            error_class=error_class,
            dropped_count=len(dropped),
            request_chars=len(creator_request),
            latest_chars=len(latest_user_message or ""),
            candidate_count=len(candidate_intents or []),
            clip_count=len(clips),
        )
        if dropped:
            question = salvage_question(0, dropped)
        else:
            question = say(
                en=(
                    "I couldn't safely verify the clip-specific instructions. "
                    "Please restate which clips to use, group, order, label, or caption."
                ),
                tr=(
                    "Klip talimatlarını güvenle doğrulayamadım. "
                    "Hangi klipleri kullanacağımı, gruplayacağımı, sıralayacağımı, "
                    "etiketleyeceğimi ya da yazı ekleyeceğimi yeniden yazar mısın?"
                ),
            )
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=question,
                error_code="planner_rejected_all",
                diagnostics={
                    "stage": "planning",
                    "drop_classes": list(getattr(cause, "drop_classes", None) or [error_class]),
                    "kept": 0,
                    "dropped": len(dropped),
                },
            ),
        )
    planned_intents = without_whole_video_captions(
        output.intents, whole_video_texts, sequence_texts
    )
    silent_drops = dict(output.silent_drops)
    if len(planned_intents) < len(output.intents):
        silent_drops["whole_video_caption_dropped"] = len(output.intents) - len(planned_intents)
    if authored_text_asks_withdrawn:
        # KRI-1111: the planner re-reads every message, so the withdrawn "who scored" ask
        # came back every turn; the brief says the creator took it back.
        kept = [i for i in planned_intents if i.op not in _AUTHORED_TEXT_OPS or i.creator_text]
        if len(kept) < len(planned_intents):
            silent_drops["withdrawn_text_dropped"] = len(planned_intents) - len(kept)
            planned_intents = kept
    if silent_drops:
        # KRI-456 / KRI-945: style asks, title lines and whole-video texts the planner minted
        # as caption intents, dropped without asking. Closed-vocabulary counts only, never text.
        log.info(
            "clip_intent_planner.silent_drops",
            silent_drops=silent_drops,
            kept=len(planned_intents),
        )
    if output.salvage_question:
        # Some instructions were valid but others could not be verified (or the
        # inventory exceeded the cap). Never act on a silent subset: ask about
        # exactly the remainder. KRI-422: the closed-vocabulary reasons ride on
        # the turn's diagnostics, so the next incident is provable from the turn.
        log.warning(
            "clip_intent_planner.salvaged",
            reasons=output.salvage_reasons,
            kept=len(output.intents),
            request_chars=len(creator_request),
            candidate_count=len(candidate_intents or []),
            clip_count=len(clips),
        )
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=output.salvage_question,
                error_code="planner_salvage",
                diagnostics={
                    "stage": "planning",
                    "drop_classes": output.salvage_reasons,
                    "kept": len(output.intents),
                },
            ),
        )
    if output.question:
        return PlannedIntentResolution(
            [],
            IntentResolution(
                status="needs_creator",
                question=output.question,
            ),
        )
    dropped_exclusions = output.silent_drops.get("exclusion_dropped", 0)
    inventory = _checked_inventory(output, planner_input)
    intents = [
        ClipIntent.model_validate(intent.model_dump(exclude={"source_quote"}))
        for intent in planned_intents
    ]
    if not clip_facts_on:
        # Defense in depth: the flag-off prompt never teaches `order_by`, so a
        # stray one is dropped rather than half-honored.
        intents = [
            intent.model_copy(update={"order_by": None}) if intent.order_by else intent
            for intent in intents
        ]
    intents = without_spoken_text_captions(intents, spoken_text)
    if describe_clips:
        intents = with_clip_descriptions(intents)
    if brief_labels:
        intents = with_brief_labels(intents, brief_labels, shot_captions=shot_captions)
    planned_ids = {intent.intent_id for intent in intents}
    if brief_anchors:
        intents = with_brief_anchors(intents, brief_anchors)
    anchor_spots = {
        intent.intent_id: str(intent.position)
        for intent in intents
        if intent.intent_id not in planned_ids
    }
    if shot_captions:
        # KRI-804: the creator's "keep it exactly as written" answer to the question below.
        intents = with_labels_kept_as_written(
            intents, "\n".join(t for t in (creator_request, latest_user_message) if t)
        )
    # KRI-678: a creator label that can never be printed is a question about the TEXT,
    # never about clips. Asked before the picker, so a tapped answer cannot loop.
    label_reply = unusable_label_reply(intents, shot_captions=shot_captions)
    if label_reply is not None:
        return PlannedIntentResolution(
            intents,
            IntentResolution(
                status="needs_creator",
                question=label_reply.question,
                error_code="label_text_unusable",
                diagnostics={"stage": "planning", "reason": "label_text_unusable"},
                reply_options=label_reply.options,
            ),
        )
    visual_intents = [intent for intent in intents if intent.label_source == "clip"]
    order_by_intents = [intent for intent in visual_intents if intent.order_by]
    visual_intents = [intent for intent in visual_intents if not intent.order_by]
    resolved_orders = [resolve_order_by_intent(intent, clips) for intent in order_by_intents]
    # KRI-282: the creator's tapped clips are authoritative for the intents they answer.
    # Fact-ordered intents stay deterministic and are never matched to a selection.
    checked_intents = tuple(intent.model_copy(deep=True) for intent in intents)
    rejected_selections = clip_selections.rejected(visual_intents) if clip_selections else ()

    def selected_result(*args, **kwargs) -> PlannedIntentResolution:
        return PlannedIntentResolution(
            *args,
            **kwargs,
            checked_intents=checked_intents,
            rejected_selections=rejected_selections,
            unexpressed_asks=tuple(output.unexpressed),
        )

    selected, visual_intents, dropped_ids = apply_creator_selections(
        visual_intents, clips=clips, selections=clip_selections, creator_request=creator_request
    )
    # KRI-614: clips the creator names by the file name the app shows ("IMG_7382").
    named, visual_intents, split = apply_file_name_references(
        visual_intents, clips=clips, creator_request=creator_request
    )
    # KRI-763: a first/last pin named by file in the latest message replaces an earlier one.
    superseded = superseded_pin_ids(
        intents=intents,
        pinned=named,
        latest_user_message=latest_user_message,
        quotes={i.intent_id: i.source_quote for i in output.intents},
    )
    if superseded:
        dropped_ids = dropped_ids | superseded
        selected = [s for s in selected if s.intent_id not in superseded]
        visual_intents = [i for i in visual_intents if i.intent_id not in superseded]
    # KRI-766: a label the draft already placed stays there unless the creator names it again.
    kept, visual_intents = apply_kept_labels(
        visual_intents,
        clips=clips,
        kept=kept_labels,
        latest_user_message=latest_user_message,
        creator_request=creator_request,
    )
    if dropped_ids or split:
        intents = [
            piece
            for intent in intents
            if intent.intent_id not in dropped_ids
            for piece in split.get(intent.intent_id, [intent])
        ]
    # KRI-1031: "skip anything too short to use" is settled from the clips' lengths (after the
    # creator's taps, which win), never matched by the resolver and never asked.
    measured = [
        resolved
        for intent in visual_intents
        if (resolved := resolve_too_short(intent, clips)) is not None
    ]
    if measured:
        measured_ids = {intent.intent_id for intent in measured}
        visual_intents = [i for i in visual_intents if i.intent_id not in measured_ids]
        log.info(
            "clip_intent_too_short_measured",
            intents=len(measured),
            left_out=sum(len(intent.assignments) for intent in measured),
        )
    resolved_orders = [*selected, *named, *kept, *measured, *resolved_orders]
    conflicts = [intent for intent in resolved_orders if intent.status == "needs_creator"]
    if conflicts:
        return selected_result(
            intents,
            IntentResolution(
                intents=_as_listed(resolved_orders, intents),
                status="needs_creator",
                question=conflicts[0].question,
                error_code="source_kind_conflict",
            ),
            inventory=inventory,
        )
    # Transcript labels are fulfilled later from the pinned narration and
    # final timeline. They remain in the complete requested inventory, but
    # must never enter the vision resolver.
    if not visual_intents:
        if resolved_orders:
            return selected_result(
                intents,
                IntentResolution(intents=_as_listed(resolved_orders, intents)),
                dropped_exclusions=dropped_exclusions,
                inventory=inventory,
            )
        return selected_result(
            intents, IntentResolution(), dropped_exclusions=dropped_exclusions, inventory=inventory
        )
    if require_clip_understanding:
        # KRI-282 L2: a clip whose background analysis has not landed has an empty
        # record, so the resolver would ask the creator about it. That is OUR gap, not
        # theirs: wait (bounded) for the analysis already in flight, and only then
        # answer with the converging "still checking" reply.
        unanalysed = _unanalysed(clips)
        waited_s = 0.0
        if unanalysed and refresh_clips is not None:
            clips, waited_s = await wait_for_clip_understanding(
                clips,
                refresh=refresh_clips,
                until=(
                    time.monotonic()
                    if understanding_wait_until is None
                    else understanding_wait_until
                ),
            )
            log.info(
                "clip_intent_understanding_waited",
                waited_s=waited_s,
                unanalysed_before=len(unanalysed),
                unanalysed_after=len(_unanalysed(clips)),
                total_clips=len(clips),
            )
            unanalysed = _unanalysed(clips)
        if unanalysed:
            return selected_result(
                intents,
                IntentResolution(
                    status="pending",
                    error_code="clip_understanding_incomplete",
                    diagnostics={
                        "reason": "clip_understanding_incomplete",
                        "unanalysed_clips": len(unanalysed),
                        "total_clips": len(clips),
                        "understanding_wait_s": waited_s,
                    },
                ),
            )

    async def _resolve(pending: list[ClipIntent]) -> IntentResolution:
        return await resolve_clip_intents_for_turn(
            intents=pending,
            creator_request=creator_request,
            clips=clips,
            run_context=run_context,
            background=background,
            checkpoint=checkpoint,
            max_vision_requeries=max_vision_requeries,
            vision_deadline_s=vision_deadline_s,
            # KRI-516: chapter captions under a filming-time order are stretches of the day.
            # Omitted otherwise, so every other call is unchanged.
            **({"filming_order": True} if order_by_intents else {}),
            # KRI-692: a Turkish chat's picker row never shows a model-written English label.
            **(
                {"creator_words": {i.intent_id: i.source_quote for i in output.intents}}
                if current_reply_language() == "tr"
                else {}
            ),
        )

    # KRI-991: the resolver is not deterministic and re-reads the whole list every turn, so a
    # match the asking turn already found stands and only the rest is resolved (once, or not at
    # all when nothing remains).
    carried = carried_matches(
        checked_inventory if reused is not None else None, visual_intents, clips
    )
    if carried:
        carried_ids = {c.intent_id for c in carried}
        visual_intents = [i for i in visual_intents if i.intent_id not in carried_ids]
        resolved_orders = [*resolved_orders, *carried]
        log.info(
            "clip_intent_matches_carried",
            carried=len(carried),
            skipped_resolver=not visual_intents,
        )
    resolution = await _resolve(visual_intents) if visual_intents else IntentResolution()
    if order_by_intents:
        kept, dropped = keep_chapters_in_filming_order(resolution.intents, clips, creator_request)
        if dropped:
            log.info("clip_intent_chapter_order_repaired", dropped_assignments=dropped)
            resolution = replace(resolution, intents=kept)
    # KRI-597: the resolver reads only a transcript's opening, so "end on the moment I
    # say '...'" can land on a clip that never says it. The full transcripts decide.
    reseated, moved = regound_quoted_intents(resolution.intents, clips)
    if moved:
        resolution = replace(resolution, intents=reseated)
    if resolved_orders:
        resolution = replace(
            resolution, intents=_as_listed([*resolution.intents, *resolved_orders], intents)
        )
    resolution = with_filming_end_ticks(resolution, anchor_spots, clips)
    return selected_result(
        intents,
        resolution,
        dropped_exclusions=dropped_exclusions,
        inventory=_inventory_with_matches(inventory, resolution),
    )
