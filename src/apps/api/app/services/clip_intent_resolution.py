"""Resolve creator clip intents to clips inside one chat turn (KRI-127,
extended for captions by KRI-129).

Contract frozen here; the pipeline is: text resolver over the shared clip
records -> capped, deadline-bounded vision re-query for clips the record cannot
answer -> grounding fence (``app.schemas.clip_intents.ground_label`` /
``ground_caption``) -> either fully resolved intents or ONE question for the
creator. Never a silent partial.

``op="caption"`` (KRI-129) resolves membership exactly like ``group`` (a
creator_text caption additionally holds per-clip membership to the LABEL bar,
mirroring the label+creator_text path — see the alias loop below), then
authors ONE on-screen phrase for the whole chapter AFTER membership is final:
``creator_text`` verbatim when quoted, or the resolver's own phrase re-checked
by ``ground_caption`` against the UNION of every member clip's vision
evidence, with at most one extra vision re-query (against one representative
member clip) if that first check fails.

Kept DB-free on purpose (no session, no row lock) — the caller does all
persistence (the resolved intents, plus ``IntentResolution.vision_answers``
onto each clip's stored ``analysis[ANSWERS_KEY]``) after this returns. This
module only ever touches the network for the resolver call and the capped
vision re-query calls; never the database.
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    RunContext,
    SchemaError,
    TerminalError,
    TerminalSchemaError,
    TransientError,
)
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionInput, ClipQuestionOutput
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ClipRequestResolverOutput,
    ResolverClipIn,
    ResolverIntentIn,
    ResolverIntentOut,
)
from app.config import settings
from app.kria.reply_language import current_reply_language, say
from app.schemas.clip_intents import (
    EXCLUDE_MIN_CONFIDENCE,
    LABEL_MIN_CONFIDENCE,
    MEMBERSHIP_MIN_CONFIDENCE,
    ONLY_VIDEO_EVIDENCE,
    ClipAssignment,
    ClipIntent,
    GroundedLabel,
    ResolvedClipIntent,
    chapter_name_caption,
    clean_caption_text,
    clean_label_text,
    creator_label_problem,
    footage_lines,
    ground_caption,
    ground_label,
    ground_placeholder_label,
    is_clip_description_label,
)
from app.schemas.clip_understanding import ClipUnderstanding
from app.services.clip_facts import order_by_capture_time
from app.services.clip_selection import (
    MAX_QUESTION_CLIPS,
    answered_rows,
    build_clip_question,
    category_label,
    clip_question_text,
    intent_key,
)
from app.services.clip_understanding import clip_record
from app.text_fold import title_like_str

log = structlog.get_logger()

# Key on a stored clip analysis holding cached vision answers (see IntentResolution).
ANSWERS_KEY = "answers"
# Durable, expiring per-question claims shared with the background worker.
ANSWER_QUERIES_KEY = "answer_queries"


def vision_query_marker(
    analysis: dict[str, Any] | None,
    question: str,
    generation: str | None = None,
) -> dict[str, Any]:
    queries = (analysis or {}).get(ANSWER_QUERIES_KEY)
    query = queries.get(normalize_question(question)) if isinstance(queries, dict) else None
    if not isinstance(query, dict) or str(query.get("generation") or "") != str(generation or ""):
        return {}
    expires_at = query.get("expires_at")
    if isinstance(expires_at, (int, float)) and expires_at <= time.time():
        return {}
    return query


def vision_query_pending(
    analysis: dict[str, Any] | None,
    question: str,
    generation: str | None = None,
) -> bool:
    query = vision_query_marker(analysis, question, generation)
    return (
        query.get("status") in {"queued", "running"}
        and isinstance(query.get("expires_at"), (int, float))
        and query["expires_at"] > time.time()
    )


ResolutionStatus = Literal[
    "resolved",
    "needs_creator",
    "pending",
    "provider_unavailable",
    "budget_exhausted",
    "media_unavailable",
]

# Generic fallback used when every intent fails before we can say anything
# more specific (resolver TerminalError, malformed clip data, etc.). Never a
# silent partial — always ask instead.
_GENERIC_QUESTION = (
    "I couldn't match that request to your clips automatically — can you "
    "tell me which clips you mean?"
)


def _generic_question() -> str:
    return say(
        en=_GENERIC_QUESTION,
        tr=(
            "Bu isteği kliplerinle otomatik olarak eşleştiremedim. "
            "Hangi klipleri kastettiğini söyler misin?"
        ),
    )


# Truncate the per-clip transcript excerpt embedded in the resolver prompt.
# Full transcripts blow up prompt size across a 30-clip batch for no benefit —
# the resolver is matching subject/setting/activity, not reading speech.
_TRANSCRIPT_CHARS_IN_PROMPT = 200


def normalize_question(question: str) -> str:
    """Stable cache key for a vision question."""
    return " ".join(question.casefold().split())[:200]


@dataclass(frozen=True)
class IntentClip:
    """One owned clip as the resolver sees it."""

    media_id: str
    kind: str  # "video" | "image"
    analysis: dict[str, Any] | None
    gcs_path: str | None = None
    # Persists a vision answer on the clip's stored analysis so a repeat is free.
    asset_id: str | None = None
    # Storage generation captured with the clip. Generation-bearing cache
    # entries are only valid for this exact media generation.
    generation: str | None = None
    # When the phone says it was filmed (UTC). Only the conflict detector reads it
    # (KRI-282): never sent to a model.
    capture_time: datetime | None = None
    # The name the app shows for the upload ("IMG_7382"). Only the deterministic file-name
    # match reads it (KRI-614, `apply_file_name_references`): never sent to the resolver.
    file_name: str | None = None
    # The clip's length in seconds. Only the "too short to use" rule reads it (KRI-1031,
    # `app.services.too_short_clips`): never sent to a model.
    duration_s: float | None = None


@dataclass(frozen=True)
class DeferredVisionQuery:
    media_id: str
    question: str


@dataclass(frozen=True)
class IntentResolution:
    intents: list[ResolvedClipIntent] = field(default_factory=list)
    # Set when any intent could not be resolved with enough certainty. The chat
    # turn must ask this instead of proposing the strategy.
    question: str | None = None
    # New vision answers from this turn, for the caller to persist on each
    # clip's stored analysis under ANSWERS_KEY so a repeat question is free:
    # {media_id: {normalized_question: {"answer", "confidence", "evidence"}}}.
    vision_answers: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)
    status: ResolutionStatus = "resolved"
    error_code: str | None = None
    deferred_queries: list[DeferredVisionQuery] = field(default_factory=list)
    # KRI-282: non-sensitive counts/codes/latencies for THIS resolution (shard
    # sizes + latencies, split retries, vision calls vs pending). Never creator
    # text or model output. The Kria planner persists it on a degraded turn so
    # the next incident needs no guessing.
    diagnostics: dict[str, Any] | None = None
    # KRI-282: the thumbnail clip-picker payload for a `needs_creator` question (see
    # ``services.clip_selection``); None keeps the question text-only.
    clip_question: dict[str, Any] | None = None
    # KRI-804: tappable plain-text answers to a `needs_creator` question (the tap sends
    # the option's text as the creator's message), e.g. how to fix a label's words.
    reply_options: tuple[str, ...] = ()

    # KRI-807: {intent id: clips, in clip order} for an exclude that left nothing out, the
    # clips the resolver put forward for it below the bar or for a vision check. Never left
    # out on their own: only the creator's exclude picker pre-ticks them. In memory only.
    exclusion_suspects: dict[str, list[str]] = field(default_factory=dict)

    @property
    def needs_creator(self) -> bool:
        # ``status`` was added after callers already constructed this value
        # from ``question`` alone. Preserve that legacy shape while ensuring
        # technical terminal states never masquerade as creator questions.
        return self.status in {"resolved", "needs_creator"} and self.question is not None


def grounded_labels(intents: list[ResolvedClipIntent] | None) -> list[GroundedLabel]:
    """Flatten resolved label intents into the only shape the render lane accepts."""
    labels: list[GroundedLabel] = []
    for intent in intents or []:
        if intent.op != "label" or intent.status != "resolved" or intent.label_source != "clip":
            continue
        for a in intent.assignments:
            if a.value and a.grounding:
                labels.append(
                    GroundedLabel(
                        media_id=a.media_id,
                        text=a.value,
                        grounding=a.grounding,
                        confidence=a.confidence,
                        intent_id=intent.intent_id,
                    )
                )
    return labels


# ── Internal bookkeeping ──────────────────────────────────────────────────────


def _alias_for(index: int) -> str:
    return f"m{index + 1:03d}"


def _invented_selection(evidence: object, alias: str, resolver_request: str) -> bool:
    """True when the resolver cites the internal handle ``alias`` as its grounds but the
    request it read never names that handle (KRI-763).

    Aliases (``m001``...) are internal: the only way one reaches the request is
    ``_replace_known_media_ids_with_aliases`` swapping a clip id the creator really named,
    and evidence is meant to quote the clip's record, which never contains one. Creator
    words like a file name ("M1_08") are not the alias ``m008``, yet the resolver answered
    "creator explicitly selected m008" for it, pinning the 8th attachment at confidence 1.0.
    Structural only: it compares the handle, never what the evidence or the request means.
    """
    handle = alias.casefold()
    return (
        isinstance(evidence, str)
        and handle in evidence.casefold()
        and handle not in resolver_request.casefold()
    )


def _replace_known_media_ids_with_aliases(
    creator_request: str,
    alias_to_media: dict[str, str],
) -> str:
    """Make explicit, owned media-id selections legible to the aliased resolver.

    The resolver prompt deliberately never includes opaque media ids. When a
    creator explicitly names one of the clips in this turn, replace that exact
    known id with its short alias before constructing the bounded agent input.
    Unknown ids remain untouched: they cannot create membership or bypass the
    resolver's record/vision grounding checks.
    """
    media_to_alias = {media_id: alias for alias, media_id in alias_to_media.items() if media_id}
    if not creator_request or not media_to_alias:
        return creator_request

    # Longest first prevents a known id that is a prefix of another known id
    # from taking its shorter alias. The surrounding token guard keeps an id
    # embedded in a different identifier untouched.
    ordered_media_ids = sorted(media_to_alias, key=len, reverse=True)
    alternatives = "|".join(re.escape(media_id) for media_id in ordered_media_ids)
    pattern = re.compile(rf"(?<![A-Za-z0-9_-])({alternatives})(?![A-Za-z0-9_-])")
    return pattern.sub(lambda match: media_to_alias[match.group(1)], creator_request)


@dataclass
class _VisionCandidate:
    media_id: str
    intent_id: str
    op: str
    question: str
    # For a label op, the resolver's own value guess (used verbatim once the
    # vision model confirms the clip, per the contract) — None if the resolver
    # had no opinion (e.g. the clip only ever showed up in needs_vision).
    fallback_value: str | None
    # For a label op with a creator-supplied title, membership only needs
    # confirming; the printed value is always the creator's own text.
    creator_text: str | None
    # The intent's own wording; membership checks are re-asked as yes/no about it.
    attribute: str = ""
    generation: str | None = None
    # True for the caption text-authoring re-query (KRI-129): a free-form
    # "what should this say" question about the caption's content, never the
    # closed yes/no membership check `is_membership_check` would otherwise
    # force for any non-"label" op.
    text_authoring: bool = False
    # KRI-282: a placeholder label's vision check only confirms membership; the
    # printed text is the fixed PLACEHOLDER_LABEL_TEXT.
    placeholder: bool = False

    @property
    def is_membership_check(self) -> bool:
        """True when the vision model confirms a clip BELONGS, rather than names a value."""
        return not self.text_authoring and (
            self.op != "label" or bool(self.creator_text) or self.placeholder
        )

    def __post_init__(self) -> None:
        # A membership check must be a closed yes/no question: a free-form
        # question ("is anyone playing a sport here?") answered confidently
        # "yes" would otherwise count as belonging to "people NOT playing".
        if self.is_membership_check and self.attribute:
            self.question = _membership_question(self.attribute)


@dataclass
class _IntentWork:
    intent: ClipIntent
    kept: list[ClipAssignment] = field(default_factory=list)
    # media_ids that were considered for this intent but never resolved
    # (ungrounded label guess, ambiguous membership, vision said unknown, over
    # the cap, or the deadline hit first).
    failed_media_ids: set[str] = field(default_factory=set)
    # Set when the resolver said the INTENT ITSELF (not a specific clip) was
    # too ambiguous to act on.
    intent_question: str | None = None
    # True once the resolver produced at least one assignment or needs_vision
    # entry for this intent — distinguishes "asked, found nothing" from
    # "genuinely nothing in this batch matches" for the final question copy.
    had_any_candidate: bool = False
    pending_media_ids: set[str] = field(default_factory=set)
    provider_unavailable_media_ids: set[str] = field(default_factory=set)
    provider_unknown_media_ids: set[str] = field(default_factory=set)
    ai_budget_exhausted_media_ids: set[str] = field(default_factory=set)
    provider_quota_exhausted_media_ids: set[str] = field(default_factory=set)
    budget_exhausted_media_ids: set[str] = field(default_factory=set)
    media_unavailable_media_ids: set[str] = field(default_factory=set)
    # op="caption" with no creator_text only: the resolver's own authored
    # phrase, cleaned. Re-grounded against the FINAL member set once
    # membership settles (see the caption text-authoring phase below).
    authored_caption: str | None = None
    # op="caption" only, set once text authoring finishes: the final grounded
    # on-screen phrase + its grounding source, or None if it never grounded.
    caption_text: str | None = None
    caption_grounding: str | None = None
    # op="label" with creator_text only (KRI-454): the resolver's below-bar guesses
    # sent to vision, {media_id: resolver confidence}, and the clips vision said
    # "no" to. See `_settle_named_label_guesses`.
    named_guesses: dict[str, float] = field(default_factory=dict)
    vision_no_media_ids: set[str] = field(default_factory=set)
    # KRI-779: every clip the resolver put forward for this intent (an assignment at any
    # confidence, or a vision check), whatever the check said after.
    considered_media_ids: set[str] = field(default_factory=set)


def _cached_answer(clip: IntentClip, question_norm: str) -> dict[str, Any] | None:
    analysis = clip.analysis if isinstance(clip.analysis, dict) else None
    if not analysis:
        return None
    answers = analysis.get(ANSWERS_KEY)
    if not isinstance(answers, dict):
        return None
    hit = answers.get(question_norm)
    if not isinstance(hit, dict):
        return None
    # A cache entry written for a generation must match exactly. Conversely,
    # a generation-bearing clip may never reuse an old generationless entry.
    cached_generation = hit.get("generation")
    if clip.generation is not None and str(cached_generation or "") != str(clip.generation):
        return None
    if cached_generation is not None and clip.generation is None:
        return None
    return hit


# The resolver's own spec allows 2 x 30s + 1s backoff. This is the hard stop the
# chat turn waits for before it degrades; a timed-out shard is split and retried
# once (see ``_run_resolver_shards``), so it must cover two sequential timeouts.
_RESOLVER_DEADLINE_S = 70.0

# KRI-282: the resolver's output grows with clips x intents (an assignment per
# matching clip per intent). Measured on the real 47-clip Olympics montage: a
# 12-clip x 8-intent shard (96 cells) emits ~2000 tokens and takes 7-16s; the
# original single call (376 cells) hit the old 20s timeout, and #1343's fixed
# 12-clip shards still sat within a 2x latency swing of the limit (any one shard
# timing out fails the whole turn). Shards are sized by CELLS (clips x intents)
# so each call carries about half that load, capped at 12 clips.
#
# KRI-454: a project that fits inside that measured 96-cell call keeps ONE call.
# Matching needs every clip side by side ("the only food hall here is the market
# the creator named"): split 5+5, the 10-clip x 9-intent Lisbon recap matched all
# five place labels in 3 of 6 live replays, as one call in 6 of 6 (6-8s, no
# vision). A timed-out call is still split and re-asked (`_run_resolver_shards`).
_RESOLVER_SHARD_MAX_CLIPS = 12
_RESOLVER_SHARD_CELLS = 48
_RESOLVER_SINGLE_CALL_CELLS = 96
_RESOLVER_MIN_SHARD_CLIPS = 3
# Concurrent shard calls. The Gemini invoke pool has 8 slots shared per process
# and a saturated pool fails a call after 1s, so never take the whole pool.
_RESOLVER_MAX_CONCURRENCY = 5


def _shard_clip_limit(intent_count: int) -> int:
    per_shard = _RESOLVER_SHARD_CELLS // max(1, intent_count)
    return max(_RESOLVER_MIN_SHARD_CLIPS, min(_RESOLVER_SHARD_MAX_CLIPS, per_shard))


def _shard_resolver_input(
    resolver_input: ClipRequestResolverInput,
) -> list[ClipRequestResolverInput]:
    """Split the clip list into balanced shards; every shard sees every intent."""
    clips = resolver_input.clips
    if (
        len(clips) <= _RESOLVER_SHARD_MAX_CLIPS
        and len(clips) * len(resolver_input.intents) <= _RESOLVER_SINGLE_CALL_CELLS
    ):
        return [resolver_input]
    limit = _shard_clip_limit(len(resolver_input.intents))
    shard_count = -(-len(clips) // limit)  # ceil
    if shard_count <= 1:
        return [resolver_input]
    base, extra = divmod(len(clips), shard_count)
    shards: list[ClipRequestResolverInput] = []
    start = 0
    for i in range(shard_count):
        end = start + base + (1 if i < extra else 0)
        shards.append(resolver_input.model_copy(update={"clips": clips[start:end]}))
        start = end
    return shards


@dataclass
class _ShardStats:
    """Redacted per-turn shard telemetry (counts, codes, latencies only)."""

    clips: int
    intents: int
    shard_ms: list[int] = field(default_factory=list)
    shard_clips: list[int] = field(default_factory=list)
    split_retries: int = 0
    failed_error_types: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "clips": self.clips,
            "intents": self.intents,
            "shards": len(self.shard_clips),
            "shard_clips": self.shard_clips[:24],
            "shard_ms": self.shard_ms[:24],
            "max_shard_ms": max(self.shard_ms, default=0),
            "split_retries": self.split_retries,
            "failed_error_types": self.failed_error_types[:8],
        }


async def _run_resolver_shards(
    agent: ClipRequestResolverAgent,
    shards: list[ClipRequestResolverInput],
    ctx: RunContext,
    stats: _ShardStats,
) -> list[ClipRequestResolverOutput]:
    """Run shards concurrently (bounded). A shard whose outcome is unknown (the
    provider timed out) is NOT blindly re-sent: it is split in half and each half
    re-asked once -- read-only, ~2x smaller work, bounded extra spend."""
    gate = asyncio.Semaphore(_RESOLVER_MAX_CONCURRENCY)

    async def call(shard: ClipRequestResolverInput) -> ClipRequestResolverOutput:
        async with gate:
            started = time.monotonic()
            try:
                return await asyncio.to_thread(agent.run, shard, ctx=ctx)
            except BaseException as exc:
                stats.failed_error_types.append(type(exc).__name__)
                raise
            finally:
                stats.shard_ms.append(int((time.monotonic() - started) * 1000))
                stats.shard_clips.append(len(shard.clips))

    async def one(shard: ClipRequestResolverInput) -> ClipRequestResolverOutput:
        try:
            return await call(shard)
        except ProviderOutcomeUnknownError:
            if len(shard.clips) < 2:
                raise
            stats.split_retries += 1
            mid = len(shard.clips) // 2
            halves = [
                shard.model_copy(update={"clips": shard.clips[:mid]}),
                shard.model_copy(update={"clips": shard.clips[mid:]}),
            ]
            return _merge_resolver_outputs(await asyncio.gather(*(call(h) for h in halves)))

    return list(await asyncio.gather(*(one(s) for s in shards)))


def _merge_resolver_outputs(
    outputs: list[ClipRequestResolverOutput],
) -> ClipRequestResolverOutput:
    """Merge per-shard outputs. Aliases are disjoint across shards, so membership
    simply concatenates; an intent-level question survives only when no shard
    produced a candidate for that intent (first wins); the single authored caption
    comes from the shard that matched the most clips (its text is re-verified against
    the final members downstream)."""
    if len(outputs) == 1:
        return outputs[0]
    order: list[str] = []
    parts: dict[str, list[ResolverIntentOut]] = {}
    for output in outputs:
        for intent in output.intents:
            if intent.intent_id not in parts:
                order.append(intent.intent_id)
                parts[intent.intent_id] = []
            parts[intent.intent_id].append(intent)
    merged: list[ResolverIntentOut] = []
    for intent_id in order:
        group = parts[intent_id]
        captioned = [g for g in group if g.caption]
        best = max(captioned, key=lambda g: len(g.assignments)) if captioned else None
        merged.append(
            ResolverIntentOut(
                intent_id=intent_id,
                assignments=[a for g in group for a in g.assignments],
                needs_vision=[v for g in group for v in g.needs_vision],
                # KRI-282: a shard with no matching clips may "ask" about an intent
                # another shard matched fine; a question only survives when NO
                # shard produced a candidate for the intent.
                question=(
                    None
                    if any(g.assignments or g.needs_vision for g in group)
                    else next((g.question for g in group if g.question), None)
                ),
                caption=best.caption if best else None,
            )
        )
    return ClipRequestResolverOutput(intents=merged)


# KRI-282: ops whose low-confidence / unrecorded clips get a soft vision re-query.
_SOFT_REQUERY_OPS = frozenset({"group", "include"})


def _record_is_empty(record: ClipUnderstanding) -> bool:
    """True when the stored understanding gives the resolver nothing to match on."""
    return not any(
        (record.subject, record.summary, record.activity, record.setting, record.speech.transcript)
    )


def _fallback_question(intent: ClipIntent) -> str:
    return f"What is the {intent.attribute}?"


def _describe_clips(
    intent: ClipIntent,
    resolved: ResolverIntentOut | None,
    *,
    clips: list[IntentClip],
    records_by_id: dict[str, ClipUnderstanding],
    media_to_alias: dict[str, str],
) -> list[ClipAssignment]:
    """One short line per clip for "explain / summarise each clip" (KRI-612).

    Every clip's record already says what happens in it, so nothing here asks the
    creator or re-checks the footage: the resolver's own line is kept when it passes the
    fence, and any clip it left out, could not ground, or gave a line an earlier clip
    already carries (the montage drops a repeat) gets the first unused line from its own
    record instead. A clip with an empty record gets nothing. The line is always Kria's
    reading of the footage (``record_span``), never reported as the creator's own words,
    even when the creator happened to use the same ones.
    """
    by_alias = {a.media: a for a in (resolved.assignments if resolved else [])}
    used: set[str] = set()
    kept: list[ClipAssignment] = []
    for clip in clips:
        if not intent.accepts_source_kind(clip.kind):
            continue
        record = records_by_id[clip.media_id]
        proposed = by_alias.get(media_to_alias.get(clip.media_id, ""))
        options = [(proposed.value, proposed.confidence, proposed.evidence)] if proposed else []
        options.extend((line, 1.0, "clip record") for line in footage_lines(record))
        for value, confidence, evidence in options:
            label = ground_label(
                media_id=clip.media_id,
                value=value,
                confidence=confidence,
                creator_request="",
                record=record,
                intent_id=intent.intent_id,
            )
            if label is None:
                continue
            key = " ".join(label.text.casefold().split())
            if key in used:
                continue
            used.add(key)
            kept.append(
                ClipAssignment(
                    media_id=clip.media_id,
                    value=label.text,
                    evidence=evidence,
                    confidence=label.confidence,
                    grounding=label.grounding,
                )
            )
            break
    return kept


def _membership_question(attribute: str) -> str:
    return f'Does this clip match this description: "{attribute}"? Answer only "yes" or "no".'


def _caption_question(intent: ClipIntent) -> str:
    """The creator-facing question when a caption never grounds.

    The attribute is the model's free-form phrase ("an introductory explanation", "pub
    clips"), so it is quoted rather than slotted behind a fixed article."""
    return say(
        en=f'What should the caption for "{intent.attribute}" say?',
        tr=f'"{intent.attribute}" için yazı ne olsun?',
    )


def _is_caption_authoring_failure(work: _IntentWork) -> bool:
    """Membership settled (clips were kept) and the caption text itself is what is unsettled."""
    return work.intent.op == "caption" and bool(work.kept) and bool(work.intent_question)


def _caption_authoring_question(intent: ClipIntent) -> str:
    """The ONE free-form vision re-query when the record-span check on the
    resolver's authored phrase fails (KRI-129's caption escalation)."""
    subject = intent.caption_attribute or intent.attribute
    return f"In a short phrase (10 words or fewer), what is {subject} in this clip?"


def _yes_no(answer: str) -> bool | None:
    word = answer.strip().casefold().split(" ")[0].strip(".,!") if answer.strip() else ""
    if word in {"yes", "true", "evet"}:
        return True
    if word in {"no", "false", "hayır", "hayir"}:
        return False
    return None


def query_clip_vision(
    clip: IntentClip,
    question: str,
    *,
    question_agent: ClipQuestionAgent,
    run_context: RunContext,
    cancelled: threading.Event | None = None,
) -> ClipQuestionOutput:
    """Shared chat/worker media path. The caller owns failure handling.

    Keep the temporary file's entire lifetime in one thread: cancelling the
    chat await must not remove a file while an upload still reads it.
    """
    from app.pipeline.agents.gemini_analyzer import gemini_upload_and_wait  # noqa: PLC0415
    from app.storage import download_generation_to_file, download_to_file  # noqa: PLC0415

    if not clip.gcs_path or clip.kind != "video":
        raise FileNotFoundError("Vision re-query requires a stored video")
    with tempfile.TemporaryDirectory() as tmpdir:
        suffix = os.path.splitext(clip.gcs_path)[1] or ".mp4"
        path = os.path.join(tmpdir, f"clip{suffix}")
        if clip.generation:
            download_generation_to_file(clip.gcs_path, path, generation=clip.generation)
        else:
            download_to_file(clip.gcs_path, path)
        if cancelled is not None and cancelled.is_set():
            raise TimeoutError("Chat vision deadline expired")
        file_ref = gemini_upload_and_wait(path)
        if cancelled is not None and cancelled.is_set():
            raise TimeoutError("Chat vision deadline expired")
        return question_agent.run(
            ClipQuestionInput(
                file_uri=file_ref.uri,
                file_mime=getattr(file_ref, "mime_type", None) or "video/mp4",
                # KRI-784: no reply language here. Membership checks parse an
                # English "yes"/"no", and answers are cached per question on the
                # clip with no language in the key.
                question=question,
            ),
            ctx=run_context,
        )


async def _run_vision_candidate(
    candidate: _VisionCandidate,
    clip: IntentClip,
    *,
    question_agent: ClipQuestionAgent,
    run_context: RunContext,
) -> ClipQuestionOutput:
    """One clip's failure must never crash the chat turn."""
    cancelled = threading.Event()
    try:
        return await asyncio.to_thread(
            query_clip_vision,
            clip,
            candidate.question,
            question_agent=question_agent,
            run_context=run_context,
            cancelled=cancelled,
        )
    finally:
        cancelled.set()


def _position_phrase(positions: list[int], *, max_refs: int = 5) -> str:
    ordered = sorted(set(positions))
    if len(ordered) == 1:
        return say(en=f"clip {ordered[0]}", tr=f"klip {ordered[0]}")
    if len(ordered) > max_refs:
        labels = ", ".join(str(p) for p in ordered[:max_refs])
        more = len(ordered) - max_refs
        return say(en=f"clips {labels} and {more} more", tr=f"klip {labels} ve {more} klip daha")
    labels = [str(p) for p in ordered]
    return say(
        en=f"clips {', '.join(labels[:-1])} and {labels[-1]}",
        tr=f"klip {', '.join(labels[:-1])} ve {labels[-1]}",
    )


def _build_question(work_items: list[_IntentWork], position_by_media: dict[str, int]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    part_indexes: dict[str, int] = {}
    positions_by_phrase: dict[str, set[int]] = {}
    for work in work_items:
        if work.intent_question:
            if work.intent_question not in seen:
                parts.append(work.intent_question)
                seen.add(work.intent_question)
            continue
        positions = [position_by_media[m] for m in work.failed_media_ids if m in position_by_media]
        if positions and work.intent.placeholder:
            phrase = say(
                en=f'I couldn\'t tell if these are "{work.intent.attribute}"',
                tr=f'Şunların "{work.intent.attribute}" olup olmadığını anlayamadım',
            )
            parts.append(f"{phrase}: {_position_phrase(sorted(positions))}")
            seen.add(phrase)
        elif positions:
            phrase = say(
                en=f"I couldn't tell the {work.intent.attribute}",
                tr=f"Anlayamadım: {work.intent.attribute}",
            )
            positions_by_phrase.setdefault(phrase, set()).update(positions)
            where = _position_phrase(sorted(positions_by_phrase[phrase]))
            part = say(en=f"{phrase} for {where}", tr=f"{phrase} ({where})")
            if phrase not in part_indexes:
                part_indexes[phrase] = len(parts)
                parts.append(part)
                seen.add(phrase)
            else:
                parts[part_indexes[phrase]] = part
        elif not work.kept and work.intent.placeholder:
            phrase = say(
                en=f'I couldn\'t find any "{work.intent.attribute}" to put the name placeholder on',
                tr=f'İsim yer tutucusunu koyacak "{work.intent.attribute}" klibi bulamadım',
            )
            if phrase not in seen:
                parts.append(phrase)
                seen.add(phrase)
        elif not work.kept:
            label = work.intent.creator_text or work.intent.attribute
            phrase = say(
                en=f'I couldn\'t find any clips for "{label}"',
                tr=f'Şunun için klip bulamadım: "{label}"',
            )
            if phrase not in seen:
                parts.append(phrase)
                seen.add(phrase)
    if not parts:
        return _generic_question()
    joined = "; ".join(parts)
    # KRI-613: a resolver question already asks; "?. Could you clarify?" read as a typo.
    suffix = (
        ""
        if joined.rstrip().endswith("?")
        else say(en=". Could you clarify?", tr=". Biraz açıklar mısın?")
    )
    if len(joined) + len(suffix) > 300:
        return say(
            en="I couldn't resolve all requested clip matches. Could you clarify?",
            tr="İstediğin klip eşleşmelerinin hepsini çözemedim. Biraz açıklar mısın?",
        )
    return f"{joined}{suffix}"


# Leverkusen M2: the asks a confident match settles although checks on other clips were
# unsure. An allowlist, so an ask added later (one that hides footage) never settles by default.
_FOUND_SETTLES_OPS = frozenset({"group", "order", "include", "label", "caption"})


def _found_rows_text(labels: list[str]) -> str:
    """The question for picker rows that come with the clips already found ticked."""
    shown = ", ".join(f'"{label}"' for label in labels[:4])
    more = say(
        en=f" and {len(labels) - 4} more" if len(labels) > 4 else "",
        tr=f" ve {len(labels) - 4} tane daha" if len(labels) > 4 else "",
    )
    return say(
        en=(
            f"I ticked the clips I found for {shown}{more}. "
            "Tap any others that show it too, or untick one that doesn't."
        ),
        tr=(
            f"{shown}{more} için bulduğum klipleri işaretledim. "
            "Bunu gösteren başka klip varsa ona da dokun, göstermeyenin işaretini kaldır."
        ),
    )


def picker_eligible(intent: ClipIntent) -> bool:
    """True when the creator tapping clips fully answers this intent.

    Membership ops, and labels/captions whose TEXT is already fixed (creator's words or
    the placeholder). A per-clip authored label or an authored caption still needs a
    model, so those stay text questions. An exclude (KRI-667) is never asked about by the
    resolver, so it is never in ``creator_work``; but the clips a creator taps (or names by
    file name) fully answer it, so it settles like the other membership ops (KRI-761).
    """
    if intent.op in {"group", "include", "order", "exclude", "hide_part"}:
        return True
    if intent.op == "label":
        if intent.placeholder:
            return True
        # KRI-678: words that can never be printed are not answered by tapping clips.
        return bool(intent.creator_text) and creator_label_problem(intent.creator_text) is None
    if intent.op == "caption":
        return bool(intent.creator_text)
    return False


def _hidden_part_fields(intent: ClipIntent) -> dict[str, Any]:
    """A ``hide_part``'s edge, seconds and content, carried onto its resolved row.

    Leverkusen M2 run 2: the resolver settles WHICH clip, never the part. Rebuilt without
    these, every hidden part reached the turn empty and "at the start, the first 3 seconds"
    was asked "start or end?" again, forever. Unset on every other op, and omitted from
    dumps when unset, so their rows stay byte-identical.
    """
    return {
        "part_edge": intent.part_edge,
        "part_s": intent.part_s,
        "part_attribute": intent.part_attribute,
    }


def _only_video(clips: list[IntentClip]) -> IntentClip | None:
    """The project's one video, or None when there are none or several."""
    videos = [c for c in clips if c.kind == "video"]
    return videos[0] if len(videos) == 1 else None


def _live_photo_ids(
    work: _IntentWork, clips: list[IntentClip], records_by_id: Mapping[str, ClipUnderstanding]
) -> set[str]:
    """KRI-779: the photos that could still answer this intent.

    A still's record describes all of it, so a photo the resolver and the vision check left
    unflagged does not show the moment. One they flagged, or one we know nothing about (an
    empty record), still might. A take's record cannot list every moment of a long take, so
    a video is never ruled out this way.

    KRI-949: that holds only when the resolver read the intent as something in the take.
    When it put NOTHING forward (Don Julio walk-in M2: "the ribeye photo", matched to the
    photo in rounds 1 and 3, nothing in rounds 4 and 6), its silence about a photo rules
    nothing out, so every photo stays a real answer.
    """
    if not work.intent.accepts_source_kind("image"):
        return set()
    if not work.considered_media_ids:
        return {c.media_id for c in clips if c.kind == "image"}
    return {
        c.media_id
        for c in clips
        if c.kind == "image"
        and (c.media_id in work.failed_media_ids or _record_is_empty(records_by_id[c.media_id]))
    }


def _settle_on_only_video(
    work: _IntentWork,
    take: IntentClip,
    clips: list[IntentClip],
    *,
    records_by_id: dict[str, ClipUnderstanding],
    creator_request: str,
) -> bool:
    """KRI-779: settle a tappable intent nothing matched on the project's only video.

    When the resolver put the take forward (its record could not rule the moment out), a
    picker's only real answer is that take: what is left open is WHERE in the take the moment
    is, which tapping a clip cannot say (the moment-window step owns that). The whole-take
    check is no evidence the moment is absent: on Merope M2 it answered "no" at confidence
    1.0 for a thumbs-up that is at 1:01.5. A take the resolver ruled out from its record
    still asks, in plain words (``clip_question_text``), and so does a live photo (a real
    second answer). The text on a label/caption is the creator's and is grounded exactly as
    a tap would ground it.
    """
    intent = work.intent
    if (
        work.kept
        or not intent.accepts_source_kind(take.kind)
        or intent.op == "exclude"
        or not picker_eligible(intent)
        or take.media_id not in work.considered_media_ids
        or _live_photo_ids(work, clips, records_by_id)
    ):
        return False
    value = grounding = None
    if intent.op == "label":
        label = (
            ground_placeholder_label(media_id=take.media_id, intent_id=intent.intent_id)
            if intent.placeholder
            else ground_label(
                media_id=take.media_id,
                value=intent.creator_text,
                confidence=1.0,
                creator_request=creator_request,
                record=records_by_id[take.media_id],
                intent_id=intent.intent_id,
            )
        )
        if label is None:
            return False
        value, grounding = label.text, label.grounding
    elif intent.op == "caption":
        caption = ground_caption(
            value=intent.creator_text,
            confidence=1.0,
            creator_request=creator_request,
            records=[records_by_id[take.media_id]],
            intent_id=intent.intent_id,
            creator_copy=True,
        )
        if caption is None:
            return False
        work.caption_text, work.caption_grounding = caption.text, caption.grounding
    work.kept.append(
        ClipAssignment(
            media_id=take.media_id,
            value=value,
            evidence=ONLY_VIDEO_EVIDENCE,
            confidence=1.0,
            grounding=grounding,
        )
    )
    work.failed_media_ids.clear()
    work.intent_question = None
    return True


def _build_clip_question(
    creator_work: list[_IntentWork],
    clips: list[IntentClip],
    creator_words: Mapping[str, str] | None = None,
    records_by_id: Mapping[str, ClipUnderstanding] | None = None,
) -> dict[str, Any] | None:
    """One picker row per TAPPABLE unresolved attribute, or None when none can be tapped.

    Intents about the same attribute that offer the same clips share one row (KRI-951).

    Intents the creator cannot answer by tapping (per-clip authored labels/captions) are
    skipped here and stay in the text part of the question (KRI-282: one such intent used
    to drop the picker for every other intent).

    Candidates are every clip the intent accepts (the creator may know a clip the analysis
    never named, so we do not narrow to what the model considered). A tap replaces the
    resolver's answer, so the clips already found come ticked (Leverkusen M2: the TOR clip
    vision had found was left out of its own row). Low-confidence matches the resolver
    could not settle are pre-ticked as suggestions only when nothing was found; beside a
    found clip they are offered un-ticked, unless the intent is an "only about X" scope
    (an un-ticked clip leaves the edit) or not one of ``_FOUND_SETTLES_OPS``.
    KRI-779: in a project with one video, a photo is offered only while it could still
    answer the intent (``_live_photo_ids``); with several videos nothing is narrowed.
    """
    creator_work = [w for w in creator_work if picker_eligible(w.intent)]
    if not creator_work:
        return None
    order = {c.media_id: i for i, c in enumerate(clips)}
    narrow_photos = records_by_id is not None and _only_video(clips) is not None
    categories: list[dict[str, Any]] = []
    for work in creator_work:
        found = {a.media_id for a in work.kept}
        # KRI-991: the clips the resolver put forward below the bar, or a check could not
        # settle, are the picker's pre-ticks. A clip a check confidently ruled out is not.
        # Suggestions only: nothing is applied without the creator's tap.
        near_misses = (work.failed_media_ids | work.considered_media_ids) - (
            work.vision_no_media_ids - work.failed_media_ids
        )
        if found and work.intent.op in _FOUND_SETTLES_OPS and not work.intent.exclusive:
            near_misses = set()
        live = _live_photo_ids(work, clips, records_by_id) if narrow_photos else set()
        candidates = [
            c.media_id
            for c in clips
            if work.intent.accepts_source_kind(c.kind)
            and (
                c.media_id in found or not narrow_photos or c.kind != "image" or c.media_id in live
            )
        ]
        categories.append(
            {
                "key": intent_key(work.intent),
                "label": category_label(
                    work.intent, creator_words=(creator_words or {}).get(work.intent.intent_id)
                ),
                "op": work.intent.op,
                "intent_id": work.intent.intent_id,
                "creator_text": work.intent.creator_text,
                "candidate_media_ids": candidates[:MAX_QUESTION_CLIPS],
                "suggested_media_ids": sorted(
                    (m for m in found | near_misses if m in candidates),
                    key=order.__getitem__,
                ),
            }
        )
    question = build_clip_question(categories, media_kinds={c.media_id: c.kind for c in clips})
    # KRI-951: a row may carry other intents about the same attribute; count those too.
    if question is None or sum(len(answered_rows(c)) for c in question["categories"]) != len(
        creator_work
    ):
        return None
    return question


def _filming_sequence(clips: list[IntentClip]) -> tuple[list[IntentClip], dict[str, int]]:
    """``clips`` in the montage's filming order and each timed clip's 1-based place in it.

    The same ``order_by_capture_time`` the montage sorts by (a clip with no capture
    time keeps its slot and gets no place). Fewer than two timed clips: no sequence.
    """
    times = {c.media_id: c.capture_time for c in clips if c.capture_time is not None}
    ordering = order_by_capture_time([c.media_id for c in clips], times)
    if ordering.basis != "capture_time":
        return clips, {}
    by_id = {c.media_id: c for c in clips}
    ordered = [by_id[media_id] for media_id in ordering.ordered_ids]
    return ordered, {c.media_id: n for n, c in enumerate(ordered, 1) if c.media_id in times}


def _chapter_rank(intents: list[ResolvedClipIntent], creator_request: str) -> dict[str, int]:
    """Chapter captions (creator-written) ranked by where the creator wrote them, or {}.

    Needs two or more, every one found as a whole word or phrase in the request, at
    distinct places; otherwise the creator's chapter sequence is unknown.
    """
    from app.kria.brief_route import fold_text  # noqa: PLC0415

    request = fold_text(creator_request or "")
    spots: dict[str, int] = {}
    for intent in intents:
        if intent.op != "caption" or intent.status != "resolved" or not intent.creator_text:
            continue
        text = fold_text(intent.creator_text)
        match = re.search(rf"(?<!\w){re.escape(text)}(?!\w)", request) if text else None
        if match is None:
            return {}
        spots[intent.intent_id] = match.start()
    if len(spots) < 2 or len(set(spots.values())) != len(spots):
        return {}
    ordered = sorted(spots, key=spots.__getitem__)
    return {intent_id: rank for rank, intent_id in enumerate(ordered)}


def keep_chapters_in_filming_order(
    intents: list[ResolvedClipIntent], clips: list[IntentClip], creator_request: str
) -> tuple[list[ResolvedClipIntent], int]:
    """KRI-516: chapter captions under a filming-time order never run backwards.

    "Order by when I filmed them; chapters: Morning, University, Lunch, Gym, Evening"
    makes each chapter one stretch of the day. When the resolved chapters break that
    (an evening clip under "Lunch" after "Gym"), keep the largest-confidence set of
    chapter memberships that reads forward in filming order, at most one chapter per
    clip, and drop the rest: an unlabelled clip just continues the chapter before it,
    a backwards title never prints. Returns ``(intents, dropped count)``; unchanged
    (0) when the chapters already read forward, the chapter order is unknown, or
    fewer than two clips have a capture time. Clips without one are left alone.
    """
    rank = _chapter_rank(intents, creator_request)
    _ordered, place = _filming_sequence(clips)
    if not rank or not place:
        return intents, 0
    options: dict[str, list[tuple[int, float, str]]] = {}
    for intent in intents:
        if intent.intent_id in rank:
            for a in intent.assignments:
                if a.media_id in place:
                    options.setdefault(a.media_id, []).append(
                        (rank[intent.intent_id], a.confidence, intent.intent_id)
                    )
    timeline = sorted(options, key=place.__getitem__)
    seen_max = -1
    forward = True
    for media_id in timeline:
        ranks = [r for r, _c, _i in options[media_id]]
        if len(ranks) > 1 or ranks[0] < seen_max:
            forward = False
            break
        seen_max = ranks[0]
    if forward:
        return intents, 0
    # best[k]: (score, kept) for the best forward choice so far whose last chapter is k.
    best: dict[int, tuple[float, tuple[tuple[str, str], ...]]] = {-1: (0.0, ())}
    for media_id in timeline:
        step = dict(best)  # leaving this clip out of every chapter
        for chapter, confidence, intent_id in options[media_id]:
            prior = [value for k, value in best.items() if k <= chapter]
            score, kept = max(prior, key=lambda value: value[0])
            candidate = (score + confidence, (*kept, (media_id, intent_id)))
            if chapter not in step or candidate[0] > step[chapter][0]:
                step[chapter] = candidate
        best = step
    keep = set(max(best.values(), key=lambda value: value[0])[1])
    dropped = 0
    out: list[ResolvedClipIntent] = []
    for intent in intents:
        if intent.intent_id not in rank:
            out.append(intent)
            continue
        assignments = [
            a
            for a in intent.assignments
            if a.media_id not in place or (a.media_id, intent.intent_id) in keep
        ]
        dropped += len(intent.assignments) - len(assignments)
        out.append(intent.model_copy(update={"assignments": assignments}))
    return out, dropped


def _build_resolver_input(
    intents: list[ClipIntent],
    creator_request: str,
    clips: list[IntentClip],
    *,
    filming_order: bool = False,
) -> tuple[ClipRequestResolverInput, dict[str, str], list[str]]:
    """Returns (resolver input, alias -> media_id, aliases in clip order).

    ``filming_order`` (KRI-516): the creator asked for the clips in the order they
    were filmed. The resolver then reads the clips in that order, each with its
    ``filmed_order`` place, so a sequence of chapter captions ("Morning, University,
    Lunch, Gym, Evening") can be matched as consecutive stretches of the day instead
    of by look alone (an evening dinner clip read as "lunch"). Shards stay contiguous
    stretches of filming time. Only the place is sent, never the time itself.
    """
    places: dict[str, int] = {}
    if filming_order:
        clips, places = _filming_sequence(clips)
    aliases: list[str] = []
    alias_to_media: dict[str, str] = {}
    resolver_clips: list[ResolverClipIn] = []
    for idx, clip in enumerate(clips):
        alias = _alias_for(idx)
        aliases.append(alias)
        alias_to_media[alias] = clip.media_id
        record = clip_record(clip.analysis, kind=clip.kind)
        view = record.prompt_view(transcript_chars=_TRANSCRIPT_CHARS_IN_PROMPT)
        if clip.media_id in places:
            view = {"filmed_order": places[clip.media_id], **view}
        resolver_clips.append(
            ResolverClipIn(
                alias=alias,
                kind="image" if clip.kind == "image" else "video",
                record=view,
            )
        )
    resolver_intents = [
        ResolverIntentIn(
            intent_id=i.intent_id,
            # KRI-282: a placeholder label is membership-only for the resolver ("which
            # clips are <attribute>"); it must never author text for it. KRI-667: so is
            # an exclude ("which clips are the quad bike clip"); the resolver has no
            # exclude op, and leaving the matched clips out is the server's step. Leverkusen
            # M2: so is a hidden part ("which clip is the second goal clip").
            op="include" if i.placeholder or i.op in {"exclude", "hide_part"} else i.op,
            attribute=i.attribute,
            creator_text=i.creator_text,
            caption_attribute=i.caption_attribute,
            position=i.position,
            ordered_members=i.ordered_members,
        )
        for i in intents
    ]
    resolver_input = ClipRequestResolverInput(
        creator_request=_replace_known_media_ids_with_aliases(
            creator_request or "", alias_to_media
        ),
        intents=resolver_intents,
        clips=resolver_clips,
        reply_language=current_reply_language(),
    )
    return resolver_input, alias_to_media, aliases


def _error_type(exc: BaseException) -> str:
    """KRI-952: a failure's class chain and HTTP status, never its message.

    ``str(exc)`` can quote the creator's request or the vision question built from it, and
    media ids are file names (KRI-536 keeps class names only for the same reason).
    ``TerminalError<-ClientError:400`` names the runtime's wrapper and the provider error
    it hides; the status is the innermost HTTP-like ``code`` / ``status_code``.
    """
    names: list[str] = []
    status: int | None = None
    link: BaseException | None = exc
    for _ in range(4):
        if link is None:
            break
        if not names or names[-1] != type(link).__name__:
            names.append(type(link).__name__)
        for attr in ("code", "status_code"):
            value = getattr(link, attr, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                status = value
        link = link.__cause__ or (None if link.__suppress_context__ else link.__context__)
    return "<-".join(names) + (f":{status}" if status is not None else "")


def _failure_status_and_code(exc: BaseException) -> tuple[ResolutionStatus, str]:
    if isinstance(exc, (FileNotFoundError, OSError)) and not isinstance(exc, TimeoutError):
        return "media_unavailable", "clip_media_unavailable"
    if isinstance(exc, ProviderOutcomeUnknownError):
        return "provider_unavailable", "provider_outcome_unknown"
    if isinstance(exc, AiBudgetExceededError):
        return "budget_exhausted", "ai_budget_exhausted"
    if isinstance(exc, ProviderQuotaExceededError):
        return "budget_exhausted", "provider_quota_exceeded"
    return "provider_unavailable", "vision_provider_error"


def _resolver_failure_status_and_code(exc: BaseException) -> tuple[ResolutionStatus, str]:
    """Reason code for a failed TEXT-resolver stage (distinct from the vision stage,
    where an OSError means an unreadable clip): every code names a different next
    step, so an operator can tell a slow provider from a bad payload from a quota."""
    if isinstance(exc, ProviderOutcomeUnknownError):
        return "provider_unavailable", "provider_outcome_unknown"
    if isinstance(exc, AiBudgetExceededError):
        return "budget_exhausted", "ai_budget_exhausted"
    if isinstance(exc, ProviderQuotaExceededError):
        return "budget_exhausted", "provider_quota_exceeded"
    if isinstance(exc, TimeoutError):
        return "provider_unavailable", "resolver_deadline_exceeded"
    if isinstance(exc, TransientError):
        return "provider_unavailable", "resolver_transient_exhausted"
    if isinstance(exc, (SchemaError, TerminalSchemaError)):
        return "provider_unavailable", "resolver_schema_error"
    if isinstance(exc, TerminalError):
        return "provider_unavailable", "resolver_terminal_error"
    return "provider_unavailable", "resolver_error"


# ── Main entry point ──────────────────────────────────────────────────────────


async def resolve_clip_intents_for_turn(
    *,
    intents: list[ClipIntent],
    creator_request: str,
    clips: list[IntentClip],
    run_context: Any,
    background: bool = False,
    checkpoint: Callable[[dict[str, dict[str, dict[str, Any]]]], Awaitable[None]] | None = None,
    max_vision_requeries: int | None = None,
    vision_deadline_s: float | None = None,
    filming_order: bool = False,
    creator_words: Mapping[str, str] | None = None,
) -> IntentResolution:
    """Resolve ``intents`` against ``clips``.

    ``max_vision_requeries`` / ``vision_deadline_s`` raise the FOREGROUND vision
    budget for callers (the Kria v2 chat turn) that have no background lane.
    ``filming_order`` (KRI-516): the creator asked for filming order, so the resolver
    reads the clips in that order (see ``_build_resolver_input``).
    ``creator_words`` (KRI-692): intent_id -> the creator's verbatim sentence, shown as a
    picker row's label when the model wrote that label in the wrong language."""
    ctx: RunContext = run_context if isinstance(run_context, RunContext) else RunContext()

    # This resolver has no pinned narration/timeline authority. Never send
    # transcript requests (including forged assignments) to a vision model.
    if any(intent.label_source != "clip" for intent in intents):
        return IntentResolution(
            question=say(
                en="Narration labels must be resolved against the recorded voiceover.",
                tr="Anlatım etiketleri kaydedilen seslendirmeye göre çözülmeli.",
            )
        )
    if not intents:
        return IntentResolution(intents=[], question=None, vision_answers={})

    clip_by_id = {c.media_id: c for c in clips}
    records_by_id = {c.media_id: clip_record(c.analysis, kind=c.kind) for c in clips}
    position_by_media = {c.media_id: idx + 1 for idx, c in enumerate(clips)}

    resolver_input, alias_to_media, _aliases = _build_resolver_input(
        intents, creator_request, clips, filming_order=filming_order
    )

    resolver_agent = ClipRequestResolverAgent(default_client())
    shard_stats = _ShardStats(clips=len(clips), intents=len(intents))
    resolver_started = time.monotonic()
    try:
        resolver_call = _run_resolver_shards(
            resolver_agent, _shard_resolver_input(resolver_input), ctx, shard_stats
        )
        shard_outputs: list[ClipRequestResolverOutput]
        if background:
            shard_outputs = await resolver_call
        else:
            shard_outputs = await asyncio.wait_for(
                resolver_call,
                timeout=_RESOLVER_DEADLINE_S,
            )
        resolver_output = _merge_resolver_outputs(shard_outputs)
        resolver_ms = int((time.monotonic() - resolver_started) * 1000)
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never raise from a chat turn
        # Covers TerminalError (refusal/schema/transient-exhausted) and any
        # AI-cost-control TerminalError subclass (budget exhausted, policy
        # rejection) — none of those should ever surface as a 500 mid-chat.
        status, error_code = _resolver_failure_status_and_code(exc)
        failure_diagnostics = {
            "stage": "resolver",
            "reason": error_code,
            "error_type": type(exc).__name__,
            "resolver_ms": int((time.monotonic() - resolver_started) * 1000),
            **shard_stats.as_dict(),
        }
        # Redacted on purpose: codes, counts and latencies only.
        log.warning(
            "clip_intent_resolver_failed",
            error_type=type(exc).__name__,
            is_terminal_error=isinstance(exc, TerminalError),
            **{k: v for k, v in failure_diagnostics.items() if k != "error_type"},
        )
        if all(i.op == "exclude" for i in intents):
            # KRI-667: exclusions alone never hold the turn: with the resolver down,
            # nothing is left out (the edit planner still reads the creator's words),
            # exactly as before exclusions were resolved at all.
            return IntentResolution(
                diagnostics=failure_diagnostics,
                intents=[
                    ResolvedClipIntent(
                        intent_id=i.intent_id,
                        op=i.op,
                        attribute=i.attribute,
                        source_kind=i.source_kind,
                        requirement_id=i.requirement_id,
                        status="resolved",
                        assignments=[],
                    )
                    for i in intents
                ],
                question=None,
                vision_answers={},
                status="resolved",
            )
        return IntentResolution(
            diagnostics=failure_diagnostics,
            intents=[
                ResolvedClipIntent(
                    intent_id=i.intent_id,
                    op=i.op,
                    attribute=i.attribute,
                    source_kind=i.source_kind,
                    creator_text=i.creator_text,
                    placeholder=i.placeholder,
                    position=i.position,
                    ordered_members=i.ordered_members,
                    exclusive=i.exclusive,
                    requirement_id=i.requirement_id,
                    **_hidden_part_fields(i),
                    status="needs_creator",
                    assignments=[],
                    question=_generic_question(),
                )
                for i in intents
            ],
            question=_generic_question(),
            vision_answers={},
            status=status,
            error_code=error_code,
        )

    resolver_by_id = {r.intent_id: r for r in resolver_output.intents}
    work_by_id: dict[str, _IntentWork] = {i.intent_id: _IntentWork(intent=i) for i in intents}

    vision_candidates: list[_VisionCandidate] = []
    deferred: dict[tuple[str, str], DeferredVisionQuery] = {}

    def defer(candidate: _VisionCandidate) -> None:
        clip = clip_by_id[candidate.media_id]
        if not background and clip.asset_id and clip.gcs_path and clip.kind == "video":
            key = (clip.media_id, normalize_question(candidate.question))
            deferred[key] = DeferredVisionQuery(clip.media_id, candidate.question)

    vision_answers: dict[str, dict[str, dict[str, Any]]] = {}
    question_intents = 0
    intent_stats: list[str] = []
    stray_questions_ignored = 0
    soft_candidates: list[_VisionCandidate] = []
    media_to_alias = {m: a for a, m in alias_to_media.items()}

    for intent in intents:
        work = work_by_id[intent.intent_id]
        resolved = resolver_by_id.get(intent.intent_id)
        if is_clip_description_label(intent):
            # KRI-612: "explain the footages" is answered by every clip's own record. It
            # never asks the creator what "explain" means, and never pays for vision.
            work.kept.extend(
                _describe_clips(
                    intent,
                    resolved,
                    clips=clips,
                    records_by_id=records_by_id,
                    media_to_alias=media_to_alias,
                )
            )
            work.had_any_candidate = bool(work.kept)
            if resolved is not None and resolved.question:
                stray_questions_ignored += 1
            intent_stats.append(f"{intent.intent_id}:described={len(work.kept)}")
            continue
        if resolved is None:
            continue
        has_candidates = bool(resolved.assignments or resolved.needs_vision)
        confs = [a.confidence for a in resolved.assignments]
        intent_stats.append(
            f"{intent.intent_id}:n={len(confs)}"
            f":min={min(confs, default=0.0):.2f}:max={max(confs, default=0.0):.2f}"
            f":nv={len(resolved.needs_vision)}:q={int(bool(resolved.question))}"
        )
        if resolved.question and intent.op == "exclude":
            # KRI-667: an exclusion never asks the creator anything; what the resolver
            # could not match is simply not left out.
            stray_questions_ignored += 1
        elif resolved.question and not has_candidates:
            work.intent_question = resolved.question
            work.had_any_candidate = True
            question_intents += 1
            continue
        elif resolved.question:
            # KRI-282: a model-authored question must never discard matches it also
            # returned -- the creator would be asked to do the matching themselves.
            stray_questions_ignored += 1

        assignment_by_media = {
            a.media: a
            for a in resolved.assignments
            if not _invented_selection(a.evidence, a.media, resolver_input.creator_request)
        }
        vision_media = {nv.media: nv.question for nv in resolved.needs_vision}
        work.had_any_candidate = bool(assignment_by_media or vision_media)
        if intent.op == "caption" and not intent.creator_text:
            # The resolver's ONE authored phrase for the whole chapter — text
            # authoring against the FINAL member set happens after membership
            # settles, below.
            work.authored_caption = resolved.caption

        candidate_media = list(dict.fromkeys([*assignment_by_media, *vision_media]))
        soft_low: list[tuple[float, str]] = []
        for alias in candidate_media:
            media_id = alias_to_media.get(alias)
            if media_id is None or not intent.accepts_source_kind(clip_by_id[media_id].kind):
                continue
            work.considered_media_ids.add(media_id)
            assignment = assignment_by_media.get(alias)
            forced_question = vision_media.get(alias)

            if intent.op == "exclude":
                # KRI-667: a clip is left out only on the resolver's confident match.
                # Removing footage the creator wanted is worse than keeping one they
                # skipped, so an unsure match (below the label bar, or one the resolver
                # wants a vision check for) keeps the clip: no vision spend, no question.
                if (
                    forced_question is None
                    and assignment is not None
                    and assignment.confidence >= EXCLUDE_MIN_CONFIDENCE
                ):
                    work.kept.append(
                        ClipAssignment(
                            media_id=media_id,
                            value=None,
                            evidence=assignment.evidence,
                            confidence=assignment.confidence,
                            grounding=None,
                        )
                    )
                continue
            if intent.op == "label" and intent.placeholder:
                # KRI-282: the text is the system's fixed stand-in (no source fence
                # applies); only WHICH clips it lands on is a judgment, and it prints,
                # so hold membership to the label bar, then a yes/no vision check.
                if forced_question is None and assignment is None:
                    continue
                if (
                    forced_question is None
                    and assignment is not None
                    and assignment.confidence >= LABEL_MIN_CONFIDENCE
                ):
                    placeholder_label = ground_placeholder_label(
                        media_id=media_id, intent_id=intent.intent_id
                    )
                    work.kept.append(
                        ClipAssignment(
                            media_id=media_id,
                            value=placeholder_label.text,
                            evidence=assignment.evidence,
                            confidence=placeholder_label.confidence,
                            grounding=placeholder_label.grounding,
                        )
                    )
                    continue
                vision_candidates.append(
                    _VisionCandidate(
                        media_id=media_id,
                        intent_id=intent.intent_id,
                        op=intent.op,
                        attribute=intent.attribute,
                        question=forced_question or _fallback_question(intent),
                        fallback_value=None,
                        creator_text=None,
                        placeholder=True,
                    )
                )
            elif intent.op == "label":
                if forced_question is not None:
                    vision_candidates.append(
                        _VisionCandidate(
                            media_id=media_id,
                            intent_id=intent.intent_id,
                            op=intent.op,
                            attribute=intent.attribute,
                            question=forced_question,
                            fallback_value=(assignment.value if assignment else None),
                            creator_text=intent.creator_text,
                        )
                    )
                    continue
                if assignment is None:
                    continue
                if intent.creator_text:
                    # The text is the creator's, but WHICH clip it lands on is the
                    # resolver's call, and it prints: hold it to the label bar (0.8),
                    # not the membership bar. Below that, the vision model confirms
                    # membership with a yes/no check instead.
                    if assignment.confidence >= LABEL_MIN_CONFIDENCE:
                        label = ground_label(
                            media_id=media_id,
                            value=intent.creator_text,
                            confidence=1.0,
                            creator_request=creator_request,
                            record=records_by_id[media_id],
                            intent_id=intent.intent_id,
                        )
                        if label is not None:
                            work.kept.append(
                                ClipAssignment(
                                    media_id=media_id,
                                    value=label.text,
                                    evidence=assignment.evidence,
                                    confidence=label.confidence,
                                    grounding=label.grounding,
                                )
                            )
                            continue
                    if assignment.confidence >= MEMBERSHIP_MIN_CONFIDENCE:
                        work.named_guesses[media_id] = assignment.confidence
                    vision_candidates.append(
                        _VisionCandidate(
                            media_id=media_id,
                            intent_id=intent.intent_id,
                            op=intent.op,
                            attribute=intent.attribute,
                            question=_fallback_question(intent),
                            fallback_value=None,
                            creator_text=intent.creator_text,
                        )
                    )
                    continue
                label = ground_label(
                    media_id=media_id,
                    value=assignment.value,
                    confidence=assignment.confidence,
                    creator_request=creator_request,
                    record=records_by_id[media_id],
                    intent_id=intent.intent_id,
                )
                if label is not None:
                    work.kept.append(
                        ClipAssignment(
                            media_id=media_id,
                            value=label.text,
                            evidence=assignment.evidence,
                            confidence=label.confidence,
                            grounding=label.grounding,
                        )
                    )
                else:
                    vision_candidates.append(
                        _VisionCandidate(
                            media_id=media_id,
                            intent_id=intent.intent_id,
                            op=intent.op,
                            attribute=intent.attribute,
                            question=_fallback_question(intent),
                            fallback_value=assignment.value,
                            creator_text=None,
                        )
                    )
            elif intent.op == "caption" and intent.creator_text:
                # A quoted caption ("say ... on the X clips"): the text is the
                # creator's, applied verbatim once membership is final (below)
                # — mirrors op="label"'s creator_text path, except there is no
                # per-clip text to (re)ground here (a caption is one phrase for
                # the whole chapter, not a per-clip value).
                if forced_question is not None:
                    vision_candidates.append(
                        _VisionCandidate(
                            media_id=media_id,
                            intent_id=intent.intent_id,
                            op=intent.op,
                            attribute=intent.attribute,
                            question=forced_question,
                            fallback_value=None,
                            creator_text=intent.creator_text,
                        )
                    )
                    continue
                if assignment is None:
                    continue
                if assignment.confidence >= LABEL_MIN_CONFIDENCE:
                    work.kept.append(
                        ClipAssignment(
                            media_id=media_id,
                            value=None,
                            evidence=assignment.evidence,
                            confidence=assignment.confidence,
                            grounding=None,
                        )
                    )
                    continue
                vision_candidates.append(
                    _VisionCandidate(
                        media_id=media_id,
                        intent_id=intent.intent_id,
                        op=intent.op,
                        attribute=intent.attribute,
                        question=_fallback_question(intent),
                        fallback_value=None,
                        creator_text=intent.creator_text,
                    )
                )
            else:  # group / order / include / described caption: membership only, never printed
                if forced_question is not None:
                    vision_candidates.append(
                        _VisionCandidate(
                            media_id=media_id,
                            intent_id=intent.intent_id,
                            op=intent.op,
                            attribute=intent.attribute,
                            question=forced_question,
                            fallback_value=None,
                            creator_text=None,
                        )
                    )
                    continue
                if assignment is None:
                    continue
                # Leverkusen M2: a hidden part on the wrong clip shows what the creator hid on
                # the right one, so its clip is held to the label bar; below it the vision
                # model confirms, else the creator taps it.
                bar = LABEL_MIN_CONFIDENCE if intent.op == "hide_part" else None
                if bar is not None and assignment.confidence < bar:
                    vision_candidates.append(
                        _VisionCandidate(
                            media_id=media_id,
                            intent_id=intent.intent_id,
                            op=intent.op,
                            attribute=intent.attribute,
                            question=_fallback_question(intent),
                            fallback_value=None,
                            creator_text=None,
                        )
                    )
                    continue
                if assignment.confidence >= MEMBERSHIP_MIN_CONFIDENCE:
                    work.kept.append(
                        ClipAssignment(
                            media_id=media_id,
                            value=None,
                            evidence=assignment.evidence,
                            confidence=assignment.confidence,
                            grounding=None,
                        )
                    )
                elif intent.op in _SOFT_REQUERY_OPS:
                    # KRI-282: a low-confidence guess is no longer silently dropped;
                    # it is re-checked with a yes/no vision query while foreground
                    # budget remains (soft: a failed check just excludes the clip).
                    soft_low.append((assignment.confidence, media_id))
                # else (order / described caption): excluded, only an explicit
                # needs_vision entry escalates.

        if intent.op in _SOFT_REQUERY_OPS and not background:
            settled = {a.media_id for a in work.kept} | {
                c.media_id for c in vision_candidates if c.intent_id == intent.intent_id
            }
            empties = [
                c.media_id
                for c in clips
                if c.kind == "video"
                and c.media_id not in settled
                and media_to_alias.get(c.media_id) not in assignment_by_media
                and _record_is_empty(records_by_id[c.media_id])
            ]
            lows = [m for _conf, m in sorted(soft_low, key=lambda t: -t[0]) if m not in settled]
            for media_id in dict.fromkeys([*empties, *lows]):
                if clip_by_id[media_id].kind != "video":
                    continue
                soft_candidates.append(
                    _VisionCandidate(
                        media_id=media_id,
                        intent_id=intent.intent_id,
                        op=intent.op,
                        attribute=intent.attribute,
                        question=_fallback_question(intent),
                        fallback_value=None,
                        creator_text=None,
                    )
                )

    # Both membership and caption checks share one budget, cache, concurrency
    # bound and checkpoint path. Caption work is admitted only after membership.
    calls_spent = 0
    foreground_deadline = asyncio.get_running_loop().time() + (
        vision_deadline_s
        if vision_deadline_s is not None
        else settings.clip_intents_vision_deadline_s
    )
    vision_cap = (
        max(1, min(len(intents), 6) * 50)
        if background
        else (
            max_vision_requeries
            if max_vision_requeries is not None
            else min(4, settings.clip_intents_max_vision_requeries)
        )
    )
    # KRI-433: which limit a pending turn hit (the call cap or the deadline), how much
    # the cache saved, and how many clips needed more than one distinct question (two
    # intents about the same chapter worded differently each pay for a check).
    # Required work only; soft re-queries are opportunistic. Redacted counts.
    vision_stats = {"cached": 0, "over_cap": 0, "deadline_cut": 0}
    # KRI-952: why each required vision check failed (``_error_type``), for the diagnostics.
    vision_errors: list[str] = []
    questions_by_clip: dict[str, set[str]] = {}

    def record_failure(candidates, error_code, works=None):
        works = work_by_id if works is None else works
        fields = {
            "provider_outcome_unknown": "provider_unknown_media_ids",
            "ai_budget_exhausted": "ai_budget_exhausted_media_ids",
            "provider_quota_exceeded": "provider_quota_exhausted_media_ids",
            "clip_media_unavailable": "media_unavailable_media_ids",
        }
        for candidate in candidates:
            work = works[candidate.intent_id]
            getattr(work, fields.get(error_code, "provider_unavailable_media_ids")).add(
                candidate.media_id
            )
            if error_code in {"ai_budget_exhausted", "provider_quota_exceeded"}:
                work.budget_exhausted_media_ids.add(candidate.media_id)

    async def run_candidates(candidates, apply_result, *, soft=False):
        nonlocal calls_spent
        # ``soft`` (KRI-282) = opportunistic re-query of a clip the resolver was unsure
        # about: its failures/pending/over-cap outcomes land on a scratch ledger and are
        # never deferred to the background worker, so they cannot turn into questions,
        # a `pending` turn, or extra paid work beyond the foreground cap.
        works = soft_scratch if soft else work_by_id
        _defer = (lambda _c: None) if soft else defer
        # Build a stable round-robin order so the first intent cannot consume the
        # foreground budget, then collapse identical media/generation/question work.
        by_intent: dict[str, list[_VisionCandidate]] = {i.intent_id: [] for i in intents}
        for candidate in candidates:
            clip = clip_by_id.get(candidate.media_id)
            if clip is not None and not works[candidate.intent_id].intent.accepts_source_kind(
                clip.kind
            ):
                continue
            by_intent.setdefault(candidate.intent_id, []).append(candidate)
        ordered_candidates: list[_VisionCandidate] = []
        while any(by_intent.values()):
            for intent in intents:
                pending = by_intent[intent.intent_id]
                if pending:
                    ordered_candidates.append(pending.pop(0))

        unique: list[_VisionCandidate] = []
        dependents: dict[tuple[str, str, str], list[_VisionCandidate]] = {}
        for candidate in ordered_candidates:
            clip = clip_by_id.get(candidate.media_id)
            if clip is None:
                works[candidate.intent_id].media_unavailable_media_ids.add(candidate.media_id)
                continue
            key = (
                candidate.media_id,
                str(clip.generation or ""),
                normalize_question(candidate.question),
            )
            if key not in dependents:
                unique.append(candidate)
                dependents[key] = []
            dependents[key].append(candidate)

        to_call: list[_VisionCandidate] = []
        for candidate in unique:
            clip = clip_by_id[candidate.media_id]
            question_norm = normalize_question(candidate.question)
            if not soft:
                questions_by_clip.setdefault(candidate.media_id, set()).add(question_norm)
            cached = vision_answers.get(candidate.media_id, {}).get(
                question_norm
            ) or _cached_answer(clip, question_norm)
            if cached is not None:
                if not soft:
                    vision_stats["cached"] += 1
                output = ClipQuestionOutput(
                    answer=str(cached.get("answer", "") or ""),
                    confidence=float(cached.get("confidence", 0.0) or 0.0),
                    evidence=str(cached.get("evidence", "") or ""),
                )
                for dependent in dependents[
                    (candidate.media_id, str(clip.generation or ""), question_norm)
                ]:
                    apply_result(dependent, output)
                continue
            if clip.kind == "image":
                # Vision re-query is video-only (see ClipQuestionAgent), so a photo
                # the record can't settle is an "unknown" the creator resolves.
                # Reporting it as unavailable media told them to "try again
                # shortly", which could never succeed (KRI-291).
                for dependent in dependents[
                    (candidate.media_id, str(clip.generation or ""), question_norm)
                ]:
                    apply_result(dependent, ClipQuestionOutput())
                continue
            if not clip.gcs_path or clip.kind != "video":
                for dependent in dependents[
                    (candidate.media_id, str(clip.generation or ""), question_norm)
                ]:
                    works[dependent.intent_id].media_unavailable_media_ids.add(dependent.media_id)
                continue
            marker = vision_query_marker(clip.analysis, candidate.question, clip.generation)
            dependents_for_candidate = dependents[
                (candidate.media_id, str(clip.generation or ""), question_norm)
            ]
            if marker.get("status") == "failed" and marker.get("error_code"):
                record_failure(dependents_for_candidate, marker["error_code"], works)
                continue
            if vision_query_pending(clip.analysis, candidate.question, clip.generation):
                _defer(candidate)
                for dependent in dependents_for_candidate:
                    works[dependent.intent_id].pending_media_ids.add(dependent.media_id)
                continue
            to_call.append(candidate)

        cap = vision_cap
        terminal_budget = any(
            work.ai_budget_exhausted_media_ids
            or work.provider_quota_exhausted_media_ids
            or work.provider_unknown_media_ids
            for work in work_by_id.values()
        )
        allowed = 0 if terminal_budget else min(max(0, cap - calls_spent), len(to_call))
        if not soft and not terminal_budget:
            vision_stats["over_cap"] += len(to_call) - allowed
        for candidate in to_call[allowed:]:
            if not terminal_budget:
                _defer(candidate)
            key = (
                candidate.media_id,
                str(clip_by_id[candidate.media_id].generation or ""),
                normalize_question(candidate.question),
            )
            for dependent in dependents[key]:
                works[dependent.intent_id].pending_media_ids.add(dependent.media_id)
        to_call = to_call[:allowed]

        question_agent = ClipQuestionAgent(default_client())
        batch_size = 4 if max_vision_requeries is None else 6
        for offset in range(0, len(to_call), batch_size):
            batch = to_call[offset : offset + batch_size]
            if not background and asyncio.get_running_loop().time() >= foreground_deadline:
                if not soft:
                    vision_stats["deadline_cut"] += len(batch)
                for candidate in batch:
                    _defer(candidate)
                    key = (
                        candidate.media_id,
                        str(clip_by_id[candidate.media_id].generation or ""),
                        normalize_question(candidate.question),
                    )
                    for dependent in dependents[key]:
                        works[dependent.intent_id].pending_media_ids.add(dependent.media_id)
                continue
            calls_spent += len(batch)
            tasks = [
                asyncio.ensure_future(
                    _run_vision_candidate(
                        candidate,
                        clip_by_id[candidate.media_id],
                        question_agent=question_agent,
                        run_context=ctx,
                    )
                )
                for candidate in batch
            ]
            if background:
                # The provider agent and its SDK calls have their own bounded
                # runtime. Await the worker threads to completion so a retry cannot
                # overlap an in-flight paid request after this resolver returns.
                await asyncio.gather(*tasks, return_exceptions=True)
                done = set(tasks)
            else:
                done, _pending = await asyncio.wait(
                    tasks, timeout=max(0, foreground_deadline - asyncio.get_running_loop().time())
                )
            # Fold completed siblings even if one slow provider call exhausts the
            # batch deadline; cancelled thread work is intentionally left alone.
            for candidate, task in zip(batch, tasks, strict=True):
                key = (
                    candidate.media_id,
                    str(clip_by_id[candidate.media_id].generation or ""),
                    normalize_question(candidate.question),
                )
                dependents_for_candidate = dependents[key]
                if task not in done:
                    if not soft:
                        vision_stats["deadline_cut"] += 1
                    _defer(candidate)
                    for dependent in dependents_for_candidate:
                        works[dependent.intent_id].pending_media_ids.add(dependent.media_id)
                    task.cancel()
                    continue
                try:
                    output = task.result()
                except Exception as exc:  # noqa: BLE001 — classify per candidate
                    _status, error_code = _failure_status_and_code(exc)
                    # KRI-952: Don Julio M2 round 7 failed 3 ms after the upload and said
                    # only "vision_provider_error". Name the exception, never its message.
                    error_type = _error_type(exc)
                    log.warning(
                        "clip_intent_vision_failed",
                        error_type=error_type,
                        error_code=error_code,
                        op=candidate.op,
                        soft=soft,
                    )
                    if not soft:
                        vision_errors.append(error_type)
                    record_failure(dependents_for_candidate, error_code, works)
                    if error_code == "vision_provider_error":
                        _defer(candidate)
                    continue
                question_norm = normalize_question(candidate.question)
                answer_record = {
                    "answer": output.answer,
                    "confidence": output.confidence,
                    "evidence": output.evidence,
                }
                if clip_by_id[candidate.media_id].generation is not None:
                    answer_record["generation"] = clip_by_id[candidate.media_id].generation
                vision_answers.setdefault(candidate.media_id, {})[question_norm] = answer_record
                for dependent in dependents_for_candidate:
                    apply_result(dependent, output)
            await asyncio.gather(*tasks, return_exceptions=True)
            if checkpoint is not None and background and done:
                await checkpoint(vision_answers)
            if background and any(
                work.ai_budget_exhausted_media_ids
                or work.provider_quota_exhausted_media_ids
                or work.provider_unknown_media_ids
                for work in works.values()
            ):
                for remaining in to_call[offset + batch_size :]:
                    remaining_key = (
                        remaining.media_id,
                        str(clip_by_id[remaining.media_id].generation or ""),
                        normalize_question(remaining.question),
                    )
                    for dependent in dependents[remaining_key]:
                        works[dependent.intent_id].pending_media_ids.add(dependent.media_id)
                break

    soft_scratch: dict[str, _IntentWork] = {}

    def apply_membership_soft(candidate, output):
        _apply_vision_result(
            candidate,
            output,
            work_by_id=soft_scratch,
            records_by_id=records_by_id,
            creator_request=creator_request,
        )

    def apply_membership(candidate, output):
        _apply_vision_result(
            candidate,
            output,
            work_by_id=work_by_id,
            records_by_id=records_by_id,
            creator_request=creator_request,
        )

    take = (
        _only_video(clips)
        if settings.kria_clip_selection_questions_enabled and len(clips) == 1
        else None
    )
    if take is not None:
        for work in work_by_id.values():
            if (
                not work.kept
                and not work.considered_media_ids
                and work.intent.op != "exclude"
                and picker_eligible(work.intent)
            ):
                vision_candidates.append(
                    _VisionCandidate(
                        media_id=take.media_id,
                        intent_id=work.intent.intent_id,
                        op=work.intent.op,
                        attribute=work.intent.attribute,
                        question=_fallback_question(work.intent),
                        fallback_value=None,
                        creator_text=work.intent.creator_text,
                    )
                )
    await run_candidates(vision_candidates, apply_membership)

    if soft_candidates:
        # Runs AFTER the required work so it only spends leftover foreground cap
        # (empty-record clips first, then the resolver's low-confidence guesses).
        soft_scratch.update({i.intent_id: _IntentWork(intent=i) for i in intents})
        await run_candidates(soft_candidates, apply_membership_soft, soft=True)
        for intent_id, scratch in soft_scratch.items():
            have = {a.media_id for a in work_by_id[intent_id].kept}
            work_by_id[intent_id].kept.extend(a for a in scratch.kept if a.media_id not in have)

    # ── Caption text authoring (intent-level, after membership is final) ────
    # A caption is ONE phrase for the whole chapter, so it can only be
    # grounded once every member clip for the intent has settled — unlike a
    # label, which grounds per clip inline in the alias loop above.
    caption_to_call: list[_VisionCandidate] = []
    caption_member_records: dict[str, list[ClipUnderstanding]] = {}
    for intent in intents:
        if intent.op != "caption":
            continue
        work = work_by_id[intent.intent_id]
        if (
            work.intent_question
            or work.failed_media_ids
            or not work.kept
            or work.pending_media_ids
            or work.provider_unavailable_media_ids
            or work.provider_unknown_media_ids
            or work.budget_exhausted_media_ids
            or work.media_unavailable_media_ids
        ):
            # Membership itself never fully settled (ambiguous intent, a
            # member clip still unresolved, or zero members) — that is
            # already reported by the generic membership question below;
            # there is nothing to author a caption ABOUT yet.
            continue
        member_records = [
            records_by_id[a.media_id] for a in work.kept if a.media_id in records_by_id
        ]
        caption_member_records[intent.intent_id] = member_records

        if intent.creator_text:
            grounded = ground_caption(
                value=intent.creator_text,
                confidence=1.0,
                creator_request=creator_request,
                records=member_records,
                intent_id=intent.intent_id,
                creator_copy=True,
            )
            if grounded is not None:
                work.caption_text = grounded.text
                work.caption_grounding = grounded.grounding
            else:
                work.intent_question = _caption_question(intent)
            continue

        # KRI-282: "a text for the pub" wants the chapter's own name, in the
        # creator's words, not a sentence the resolver writes about the footage.
        chapter_name = chapter_name_caption(
            attribute=intent.attribute,
            caption_attribute=intent.caption_attribute,
            creator_request=creator_request,
        )
        if chapter_name is not None:
            named = ground_caption(
                value=chapter_name,
                confidence=1.0,
                creator_request=creator_request,
                records=member_records,
                intent_id=intent.intent_id,
            )
            if named is not None:
                work.caption_text = named.text
                work.caption_grounding = named.grounding
                continue

        # Described caption: ground the resolver's authored phrase first —
        # confidence is fixed at the label bar (there is no per-intent
        # resolver confidence for an authored phrase; the word-membership
        # check against the union evidence IS the real gate).
        grounded = ground_caption(
            value=work.authored_caption,
            confidence=LABEL_MIN_CONFIDENCE if work.authored_caption else 0.0,
            creator_request=creator_request,
            records=member_records,
            intent_id=intent.intent_id,
        )
        if grounded is not None:
            work.caption_text = grounded.text
            work.caption_grounding = grounded.grounding
            continue

        # Ungrounded — escalate to exactly ONE vision re-query, against one
        # representative member clip (earliest in clip order), reusing the
        # SAME per-turn cap/deadline budget as the membership round above.
        first_member = min(work.kept, key=lambda a: position_by_media.get(a.media_id, 10**9))
        clip = clip_by_id.get(first_member.media_id)
        if clip is None:
            work.media_unavailable_media_ids.add(first_member.media_id)
            continue
        question = _caption_authoring_question(intent)
        caption_to_call.append(
            _VisionCandidate(
                media_id=first_member.media_id,
                intent_id=intent.intent_id,
                op=intent.op,
                attribute=intent.attribute,
                question=question,
                fallback_value=None,
                creator_text=None,
                text_authoring=True,
            )
        )

    by_intent_id = {i.intent_id: i for i in intents}

    def apply_caption(candidate, output):
        work = work_by_id[candidate.intent_id]
        cand_intent = by_intent_id[candidate.intent_id]
        if output.is_unknown():
            work.intent_question = _caption_question(cand_intent)
            return
        value = work.authored_caption or clean_caption_text(output.answer)
        re_grounded = ground_caption(
            value=value,
            confidence=0.0,
            creator_request=creator_request,
            records=caption_member_records.get(candidate.intent_id, []),
            vision_answer=output.answer,
            vision_confidence=output.confidence,
            intent_id=candidate.intent_id,
        )
        if re_grounded is not None:
            work.caption_text = re_grounded.text
            work.caption_grounding = re_grounded.grounding
        else:
            work.intent_question = _caption_question(cand_intent)

    await run_candidates(caption_to_call, apply_caption)

    named_guesses_settled = _settle_named_label_guesses(
        work_by_id, records_by_id=records_by_id, creator_request=creator_request
    )

    # ── Assemble the result ──────────────────────────────────────────────────
    resolved_intents: list[ResolvedClipIntent] = []
    unresolved_work: list[_IntentWork] = []
    creator_work: list[_IntentWork] = []
    only_video = _only_video(clips)
    single_take_settled = 0
    found_settled = 0
    for intent in intents:
        work = work_by_id[intent.intent_id]
        # KRI-667: an exclude is settled by whatever the resolver confidently matched,
        # even nothing: it never asks the creator and never holds the turn.
        settles_itself = intent.op == "exclude"
        has_non_creator_failure = not settles_itself and bool(
            work.pending_media_ids
            or work.provider_unavailable_media_ids
            or work.provider_unknown_media_ids
            or work.ai_budget_exhausted_media_ids
            or work.provider_quota_exhausted_media_ids
            or work.budget_exhausted_media_ids
            or work.media_unavailable_media_ids
        )
        # KRI-779: one video is the only answer a clip picker could get; never ask it.
        if (
            only_video is not None
            and not has_non_creator_failure
            and _settle_on_only_video(
                work,
                only_video,
                clips,
                records_by_id=records_by_id,
                creator_request=creator_request,
            )
        ):
            single_take_settled += 1
        # Leverkusen M2: an ask a clip confidently matched is answered by that match. Unsure
        # checks on OTHER clips beside it no longer hold it open: the picker asked "which
        # clips show the TOR" with the TOR clip vision had found left out and the unsure
        # ones pre-ticked. Only for asks that place, add or print on clips: an "only about X"
        # scope still asks (settling it would cut those unsure clips from the edit), and so
        # does any ask that hides footage (an unsure clip may show what must not be seen).
        found_settles = (
            bool(work.kept)
            and bool(work.failed_media_ids)
            and not work.intent_question
            and not intent.exclusive
            and intent.op in _FOUND_SETTLES_OPS
            and picker_eligible(intent)
        )
        # Technical/provider/media failures must not be turned into a creator
        # question. The caller can retry the preparation/resolution attempt;
        # only settled ambiguity or a genuinely empty result belongs in
        # `needs_creator`.
        is_creator_unresolved = (
            not settles_itself
            and not has_non_creator_failure
            and (
                bool(work.intent_question)
                or (bool(work.failed_media_ids) and not found_settles)
                or not work.kept
            )
        )
        if found_settles and not settles_itself and not has_non_creator_failure:
            found_settled += 1
        is_unresolved = is_creator_unresolved or has_non_creator_failure
        if is_unresolved:
            unresolved_work.append(work)
            if is_creator_unresolved:
                creator_work.append(work)
            resolved_intents.append(
                ResolvedClipIntent(
                    intent_id=intent.intent_id,
                    op=intent.op,
                    attribute=intent.attribute,
                    source_kind=intent.source_kind,
                    creator_text=intent.creator_text,
                    placeholder=intent.placeholder,
                    caption_attribute=intent.caption_attribute,
                    position=intent.position,
                    ordered_members=intent.ordered_members,
                    exclusive=intent.exclusive,
                    requirement_id=intent.requirement_id,
                    **_hidden_part_fields(intent),
                    status="needs_creator",
                    assignments=work.kept,
                    question=None,  # set on the turn-level question below
                    caption_text=work.caption_text,
                    caption_grounding=work.caption_grounding,
                )
            )
        else:
            resolved_intents.append(
                ResolvedClipIntent(
                    intent_id=intent.intent_id,
                    op=intent.op,
                    attribute=intent.attribute,
                    source_kind=intent.source_kind,
                    creator_text=intent.creator_text,
                    placeholder=intent.placeholder,
                    caption_attribute=intent.caption_attribute,
                    position=intent.position,
                    ordered_members=intent.ordered_members,
                    exclusive=intent.exclusive,
                    requirement_id=intent.requirement_id,
                    **_hidden_part_fields(intent),
                    status="resolved",
                    assignments=work.kept,
                    caption_text=work.caption_text,
                    caption_grounding=work.caption_grounding,
                )
            )

    redacted_counts = {
        # Redacted counts only (no creator text / model output).
        "resolver_question_intents": question_intents,
        "stray_questions_ignored": stray_questions_ignored,
        "empty_records": sum(1 for r in records_by_id.values() if _record_is_empty(r)),
        "soft_requeries_queued": len(soft_candidates),
        "intent_stats": intent_stats[:24],
        "vision_cap": vision_cap,
        "vision_cached": vision_stats["cached"],
        "vision_over_cap": vision_stats["over_cap"],
        "vision_deadline_cut": vision_stats["deadline_cut"],
        "vision_multi_question_clips": sum(1 for q in questions_by_clip.values() if len(q) > 1),
        "named_guesses_settled": named_guesses_settled,
        "single_take_settled": single_take_settled,
        "found_settled": found_settled,
        # Omitted when nothing failed, so every other turn's diagnostics are unchanged.
        **({"vision_error_types": vision_errors[:8]} if vision_errors else {}),
    }
    clip_order = {clip.media_id: index for index, clip in enumerate(clips)}
    exclusion_suspects = {
        w.intent.intent_id: sorted(
            w.considered_media_ids, key=lambda m: clip_order.get(m, len(clip_order))
        )
        for w in work_by_id.values()
        if w.intent.op == "exclude" and not w.kept and w.considered_media_ids
    }

    if not unresolved_work:
        return IntentResolution(
            intents=resolved_intents,
            question=None,
            vision_answers=vision_answers,
            exclusion_suspects=exclusion_suspects,
            status="resolved",
            diagnostics={
                "stage": "membership",
                "reason": "resolved",
                **shard_stats.as_dict(),
                "vision_calls": calls_spent,
                **redacted_counts,
            },
        )

    if creator_work:
        status = "needs_creator"
        error_code = None
    else:
        status = "resolved"
        error_code = None
    if any(w.provider_unknown_media_ids for w in unresolved_work):
        status = "provider_unavailable"
        error_code = "provider_outcome_unknown"
    elif any(w.ai_budget_exhausted_media_ids for w in unresolved_work):
        status = "budget_exhausted"
        error_code = "ai_budget_exhausted"
    elif any(w.provider_quota_exhausted_media_ids for w in unresolved_work):
        status = "budget_exhausted"
        error_code = "provider_quota_exceeded"
    elif any(w.provider_unavailable_media_ids for w in unresolved_work):
        status = "provider_unavailable"
        error_code = "vision_provider_error"
    elif any(w.media_unavailable_media_ids for w in unresolved_work):
        status = "media_unavailable"
        error_code = "clip_media_unavailable"
    elif any(w.pending_media_ids for w in unresolved_work):
        status = "pending"
        error_code = "vision_batch_deadline_or_foreground_cap"
    if error_code in {"provider_outcome_unknown", "ai_budget_exhausted", "provider_quota_exceeded"}:
        deferred.clear()  # Do not bypass provider/budget stops by dispatching new paid work.
    turn_question = (
        _build_question(creator_work, position_by_media)
        if status == "needs_creator" and creator_work
        else None
    )
    clip_question: dict[str, Any] | None = None
    if (
        status == "needs_creator"
        and creator_work
        and settings.kria_clip_selection_questions_enabled
    ):
        clip_question = _build_clip_question(
            creator_work, clips, creator_words, records_by_id=records_by_id
        )
        if clip_question is not None:
            # Leverkusen M2: a row that already has its clips ticked never says "I couldn't
            # tell which of your clips show it".
            found_keys = {intent_key(w.intent) for w in creator_work if w.kept}
            found_rows = [
                c
                for c in clip_question["categories"]
                if found_keys & {r["key"] for r in answered_rows(c)}
            ]
            open_rows = [c for c in clip_question["categories"] if c not in found_rows]
            # KRI-779: a picker whose only candidate is the one video asks whether the
            # moment is in it at all, in plain words, never "which of your clips".
            in_the_video = (
                not found_rows
                and only_video is not None
                and all(
                    c["candidate_media_ids"] == [only_video.media_id]
                    for c in clip_question["categories"]
                )
            )
            turn_question = " ".join(
                part
                for part in (
                    _found_rows_text([c["label"] for c in found_rows]) if found_rows else "",
                    clip_question_text(open_rows, only_video=in_the_video) if open_rows else "",
                )
                if part
            )
            if in_the_video:
                clip_question = None
            leftover = [w for w in creator_work if not picker_eligible(w.intent)]
            if leftover:
                # Mixed case: picker for the tappable intents, text for the rest.
                also = say(en="Also:", tr="Ayrıca:")
                turn_question = (
                    f"{turn_question} {also} {_build_question(leftover, position_by_media)}"
                )
    final_intents = [
        (i if i.status == "resolved" else i.model_copy(update={"question": turn_question}))
        for i in resolved_intents
    ]
    if status != "needs_creator":
        diagnostics_stage = "vision"
    elif creator_work and all(_is_caption_authoring_failure(w) for w in creator_work):
        diagnostics_stage = "caption_authoring"
    else:
        diagnostics_stage = "membership"
    final_diagnostics = {
        "stage": diagnostics_stage,
        "reason": error_code or status,
        "resolver_ms": resolver_ms,
        **shard_stats.as_dict(),
        "vision_calls": calls_spent,
        **redacted_counts,
        "vision_answers": sum(len(v) for v in vision_answers.values()),
        "pending_clips": len(set().union(*(w.pending_media_ids for w in unresolved_work))),
        "deferred_queries": len(deferred),
    }
    if status not in {"resolved", "needs_creator"}:
        log.warning(
            "clip_intent_resolution_degraded",
            **{k: v for k, v in final_diagnostics.items() if k != "stage"},
            stage=final_diagnostics["stage"],
            status=status,
        )
    return IntentResolution(
        clip_question=clip_question,
        intents=final_intents,
        question=turn_question,
        vision_answers=vision_answers,
        exclusion_suspects=exclusion_suspects,
        status=status,
        error_code=error_code,
        deferred_queries=list(deferred.values()),
        diagnostics=final_diagnostics,
    )


def _settle_named_label_guesses(
    work_by_id: dict[str, _IntentWork],
    *,
    records_by_id: dict[str, ClipUnderstanding],
    creator_request: str,
) -> int:
    """KRI-454: keep the resolver's one guess for a name the creator wrote when the
    vision check could only say "unknown".

    The vision model answers from pixels alone, so it cannot confirm a proper name
    ("is this Alfama?"): it says "unknown" even on the right clip. The creator has
    already said the place is in their footage and the text printed is theirs, so a
    lone guess at the membership bar stands unless vision said "no" or another named
    label wants the same clip. Returns how many labels were settled this way."""
    named = {
        intent_id: work
        for intent_id, work in work_by_id.items()
        if work.intent.op == "label" and work.intent.creator_text and not work.intent.placeholder
    }
    claimed: dict[str, int] = {}
    for work in named.values():
        for media_id in {a.media_id for a in work.kept} | set(work.named_guesses):
            claimed[media_id] = claimed.get(media_id, 0) + 1

    settled = 0
    for work in named.values():
        if (
            work.kept
            or work.intent_question
            or len(work.failed_media_ids) != 1
            or work.pending_media_ids
            or work.provider_unavailable_media_ids
            or work.provider_unknown_media_ids
            or work.budget_exhausted_media_ids
            or work.media_unavailable_media_ids
        ):
            continue
        [media_id] = work.failed_media_ids
        if (
            media_id not in work.named_guesses
            or media_id in work.vision_no_media_ids
            or claimed.get(media_id, 0) > 1
            or media_id not in records_by_id
        ):
            continue
        label = ground_label(
            media_id=media_id,
            value=work.intent.creator_text,
            confidence=1.0,
            creator_request=creator_request,
            record=records_by_id[media_id],
            intent_id=work.intent.intent_id,
        )
        if label is None:
            continue
        work.kept.append(
            ClipAssignment(
                media_id=media_id,
                value=label.text,
                evidence="best match for a name the creator gave; the clip check could not name it",
                confidence=label.confidence,
                grounding=label.grounding,
            )
        )
        work.failed_media_ids.clear()
        settled += 1
    return settled


def _apply_vision_result(
    candidate: _VisionCandidate,
    output: ClipQuestionOutput | None,
    *,
    work_by_id: dict[str, _IntentWork],
    records_by_id: dict[str, Any],
    creator_request: str,
) -> None:
    """Fold one vision answer (or a failure) into the owning intent's work."""
    work = work_by_id[candidate.intent_id]
    if output is None or output.is_unknown():
        work.failed_media_ids.add(candidate.media_id)
        return

    if candidate.is_membership_check:
        verdict = _yes_no(output.answer)
        if verdict is False:
            work.vision_no_media_ids.add(candidate.media_id)
        if verdict is False and output.confidence >= MEMBERSHIP_MIN_CONFIDENCE:
            return  # confidently NOT a member: settled, nothing to ask about
        if verdict is not True or output.confidence < MEMBERSHIP_MIN_CONFIDENCE:
            work.failed_media_ids.add(candidate.media_id)
            return

    if candidate.op == "label" and candidate.placeholder:
        # Membership was just confirmed above (is_membership_check): print the fixed text.
        if output.confidence >= LABEL_MIN_CONFIDENCE:
            placeholder_label = ground_placeholder_label(
                media_id=candidate.media_id, intent_id=candidate.intent_id
            )
            work.kept.append(
                ClipAssignment(
                    media_id=candidate.media_id,
                    value=placeholder_label.text,
                    evidence=output.evidence,
                    confidence=placeholder_label.confidence,
                    grounding=placeholder_label.grounding,
                )
            )
        else:
            work.failed_media_ids.add(candidate.media_id)
    elif candidate.op == "label":
        if candidate.creator_text:
            if output.confidence >= LABEL_MIN_CONFIDENCE:
                label = ground_label(
                    media_id=candidate.media_id,
                    value=candidate.creator_text,
                    confidence=1.0,
                    creator_request=creator_request,
                    record=records_by_id[candidate.media_id],
                    intent_id=candidate.intent_id,
                )
                if label is not None:
                    work.kept.append(
                        ClipAssignment(
                            media_id=candidate.media_id,
                            value=label.text,
                            evidence=output.evidence,
                            confidence=label.confidence,
                            grounding=label.grounding,
                        )
                    )
                    return
            work.failed_media_ids.add(candidate.media_id)
            return

        value = candidate.fallback_value or clean_label_text(title_like_str(output.answer))
        label = ground_label(
            media_id=candidate.media_id,
            value=value,
            confidence=0.0,
            creator_request=creator_request,
            record=records_by_id[candidate.media_id],
            vision_answer=output.answer,
            vision_confidence=output.confidence,
            intent_id=candidate.intent_id,
        )
        if label is not None:
            work.kept.append(
                ClipAssignment(
                    media_id=candidate.media_id,
                    value=label.text,
                    evidence=output.evidence,
                    confidence=label.confidence,
                    grounding=label.grounding,
                )
            )
        else:
            work.failed_media_ids.add(candidate.media_id)
    else:  # membership
        if output.confidence >= MEMBERSHIP_MIN_CONFIDENCE:
            work.kept.append(
                ClipAssignment(
                    media_id=candidate.media_id,
                    value=None,
                    evidence=output.evidence,
                    confidence=output.confidence,
                    grounding=None,
                )
            )
        else:
            work.failed_media_ids.add(candidate.media_id)
