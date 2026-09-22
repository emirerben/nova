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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import (
    AiBudgetExceededError,
    ProviderOutcomeUnknownError,
    ProviderQuotaExceededError,
    RunContext,
    TerminalError,
)
from app.agents.clip_question import ClipQuestionAgent, ClipQuestionInput, ClipQuestionOutput
from app.agents.clip_request_resolver import (
    ClipRequestResolverAgent,
    ClipRequestResolverInput,
    ClipRequestResolverOutput,
    ResolverClipIn,
    ResolverIntentIn,
)
from app.config import settings
from app.schemas.clip_intents import (
    LABEL_MIN_CONFIDENCE,
    MEMBERSHIP_MIN_CONFIDENCE,
    ClipAssignment,
    ClipIntent,
    GroundedLabel,
    ResolvedClipIntent,
    clean_caption_text,
    clean_label_text,
    ground_caption,
    ground_label,
)
from app.schemas.clip_understanding import ClipUnderstanding
from app.services.clip_understanding import clip_record

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
        if intent.op != "label" or intent.status != "resolved":
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

    @property
    def is_membership_check(self) -> bool:
        """True when the vision model confirms a clip BELONGS, rather than names a value."""
        return not self.text_authoring and (self.op != "label" or bool(self.creator_text))

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


# The resolver's own spec allows 2 x 20s + 1s backoff; this is the hard stop the
# chat turn waits for before it degrades to a question.
_RESOLVER_DEADLINE_S = 45.0


def _fallback_question(intent: ClipIntent) -> str:
    return f"What is the {intent.attribute}?"


def _membership_question(attribute: str) -> str:
    return f'Does this clip match this description: "{attribute}"? Answer only "yes" or "no".'


def _caption_question(intent: ClipIntent) -> str:
    """The creator-facing question when a caption never grounds."""
    return f"What should the caption on the {intent.attribute} say?"


def _caption_authoring_question(intent: ClipIntent) -> str:
    """The ONE free-form vision re-query when the record-span check on the
    resolver's authored phrase fails (KRI-129's caption escalation)."""
    subject = intent.caption_attribute or intent.attribute
    return f"In a short phrase (10 words or fewer), what is {subject} in this clip?"


def _yes_no(answer: str) -> bool | None:
    word = answer.strip().casefold().split(" ")[0].strip(".,!") if answer.strip() else ""
    if word in {"yes", "true"}:
        return True
    if word in {"no", "false"}:
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
        return f"clip {ordered[0]}"
    if len(ordered) > max_refs:
        labels = ", ".join(str(p) for p in ordered[:max_refs])
        return f"clips {labels} and {len(ordered) - max_refs} more"
    labels = [str(p) for p in ordered]
    return f"clips {', '.join(labels[:-1])} and {labels[-1]}"


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
        if positions:
            phrase = f"I couldn't tell the {work.intent.attribute}"
            positions_by_phrase.setdefault(phrase, set()).update(positions)
            if phrase not in part_indexes:
                part_indexes[phrase] = len(parts)
                parts.append(
                    f"{phrase} for {_position_phrase(sorted(positions_by_phrase[phrase]))}"
                )
                seen.add(phrase)
            else:
                idx = part_indexes[phrase]
                parts[idx] = f"{phrase} for {_position_phrase(sorted(positions_by_phrase[phrase]))}"
        elif not work.kept:
            label = work.intent.creator_text or work.intent.attribute
            phrase = f'I couldn\'t find any clips for "{label}"'
            if phrase not in seen:
                parts.append(phrase)
                seen.add(phrase)
    if not parts:
        return _GENERIC_QUESTION
    suffix = ". Could you clarify?"
    joined = "; ".join(parts)
    if len(joined) + len(suffix) > 300:
        return "I couldn't resolve all requested clip matches. Could you clarify?"
    return f"{joined}{suffix}"


def _build_resolver_input(
    intents: list[ClipIntent],
    creator_request: str,
    clips: list[IntentClip],
) -> tuple[ClipRequestResolverInput, dict[str, str], list[str]]:
    """Returns (resolver input, alias -> media_id, aliases in clip order)."""
    aliases: list[str] = []
    alias_to_media: dict[str, str] = {}
    resolver_clips: list[ResolverClipIn] = []
    for idx, clip in enumerate(clips):
        alias = _alias_for(idx)
        aliases.append(alias)
        alias_to_media[alias] = clip.media_id
        record = clip_record(clip.analysis, kind=clip.kind)
        resolver_clips.append(
            ResolverClipIn(
                alias=alias,
                kind="image" if clip.kind == "image" else "video",
                record=record.prompt_view(transcript_chars=_TRANSCRIPT_CHARS_IN_PROMPT),
            )
        )
    resolver_intents = [
        ResolverIntentIn(
            intent_id=i.intent_id,
            op=i.op,
            attribute=i.attribute,
            creator_text=i.creator_text,
            caption_attribute=i.caption_attribute,
            position=i.position,
        )
        for i in intents
    ]
    resolver_input = ClipRequestResolverInput(
        creator_request=_replace_known_media_ids_with_aliases(
            creator_request or "", alias_to_media
        ),
        intents=resolver_intents,
        clips=resolver_clips,
    )
    return resolver_input, alias_to_media, aliases


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


# ── Main entry point ──────────────────────────────────────────────────────────


async def resolve_clip_intents_for_turn(
    *,
    intents: list[ClipIntent],
    creator_request: str,
    clips: list[IntentClip],
    run_context: Any,
    background: bool = False,
    checkpoint: Callable[[dict[str, dict[str, dict[str, Any]]]], Awaitable[None]] | None = None,
) -> IntentResolution:
    """Resolve ``intents`` against ``clips``."""
    ctx: RunContext = run_context if isinstance(run_context, RunContext) else RunContext()

    if not intents:
        return IntentResolution(intents=[], question=None, vision_answers={})

    clip_by_id = {c.media_id: c for c in clips}
    records_by_id = {c.media_id: clip_record(c.analysis, kind=c.kind) for c in clips}
    position_by_media = {c.media_id: idx + 1 for idx, c in enumerate(clips)}

    resolver_input, alias_to_media, _aliases = _build_resolver_input(
        intents, creator_request, clips
    )

    resolver_agent = ClipRequestResolverAgent(default_client())
    try:
        resolver_call = asyncio.to_thread(resolver_agent.run, resolver_input, ctx=ctx)
        resolver_output: ClipRequestResolverOutput
        if background:
            resolver_output = await resolver_call
        else:
            resolver_output = await asyncio.wait_for(
                resolver_call,
                timeout=_RESOLVER_DEADLINE_S,
            )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never raise from a chat turn
        # Covers TerminalError (refusal/schema/transient-exhausted) and any
        # AI-cost-control TerminalError subclass (budget exhausted, policy
        # rejection) — none of those should ever surface as a 500 mid-chat.
        log.warning(
            "clip_intent_resolver_failed",
            error_type=type(exc).__name__,
            is_terminal_error=isinstance(exc, TerminalError),
        )
        status, error_code = _failure_status_and_code(exc)
        return IntentResolution(
            intents=[
                ResolvedClipIntent(
                    intent_id=i.intent_id,
                    op=i.op,
                    attribute=i.attribute,
                    creator_text=i.creator_text,
                    position=i.position,
                    status="needs_creator",
                    assignments=[],
                    question=_GENERIC_QUESTION,
                )
                for i in intents
            ],
            question=_GENERIC_QUESTION,
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

    for intent in intents:
        work = work_by_id[intent.intent_id]
        resolved = resolver_by_id.get(intent.intent_id)
        if resolved is None:
            continue
        if resolved.question:
            work.intent_question = resolved.question
            work.had_any_candidate = True
            continue

        assignment_by_media = {a.media: a for a in resolved.assignments}
        vision_media = {nv.media: nv.question for nv in resolved.needs_vision}
        work.had_any_candidate = bool(assignment_by_media or vision_media)
        if intent.op == "caption" and not intent.creator_text:
            # The resolver's ONE authored phrase for the whole chapter — text
            # authoring against the FINAL member set happens after membership
            # settles, below.
            work.authored_caption = resolved.caption

        candidate_media = list(dict.fromkeys([*assignment_by_media, *vision_media]))
        for alias in candidate_media:
            media_id = alias_to_media.get(alias)
            if media_id is None:
                continue
            assignment = assignment_by_media.get(alias)
            forced_question = vision_media.get(alias)

            if intent.op == "label":
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
                # else: a low-confidence membership guess is simply excluded,
                # not escalated — group/order/include never re-queries vision
                # on the resolver's own uncertainty, only on an explicit
                # needs_vision entry (handled above).

    # Both membership and caption checks share one budget, cache, concurrency
    # bound and checkpoint path. Caption work is admitted only after membership.
    calls_spent = 0
    foreground_deadline = (
        asyncio.get_running_loop().time() + settings.clip_intents_vision_deadline_s
    )

    def record_failure(candidates, error_code):
        fields = {
            "provider_outcome_unknown": "provider_unknown_media_ids",
            "ai_budget_exhausted": "ai_budget_exhausted_media_ids",
            "provider_quota_exceeded": "provider_quota_exhausted_media_ids",
            "clip_media_unavailable": "media_unavailable_media_ids",
        }
        for candidate in candidates:
            work = work_by_id[candidate.intent_id]
            getattr(work, fields.get(error_code, "provider_unavailable_media_ids")).add(
                candidate.media_id
            )
            if error_code in {"ai_budget_exhausted", "provider_quota_exceeded"}:
                work.budget_exhausted_media_ids.add(candidate.media_id)

    async def run_candidates(candidates, apply_result):
        nonlocal calls_spent
        # Build a stable round-robin order so the first intent cannot consume the
        # foreground budget, then collapse identical media/generation/question work.
        by_intent: dict[str, list[_VisionCandidate]] = {i.intent_id: [] for i in intents}
        for candidate in candidates:
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
                work_by_id[candidate.intent_id].media_unavailable_media_ids.add(candidate.media_id)
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
            cached = vision_answers.get(candidate.media_id, {}).get(
                question_norm
            ) or _cached_answer(clip, question_norm)
            if cached is not None:
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
            if not clip.gcs_path or clip.kind != "video":
                for dependent in dependents[
                    (candidate.media_id, str(clip.generation or ""), question_norm)
                ]:
                    work_by_id[dependent.intent_id].media_unavailable_media_ids.add(
                        dependent.media_id
                    )
                continue
            marker = vision_query_marker(clip.analysis, candidate.question, clip.generation)
            dependents_for_candidate = dependents[
                (candidate.media_id, str(clip.generation or ""), question_norm)
            ]
            if marker.get("status") == "failed" and marker.get("error_code"):
                record_failure(dependents_for_candidate, marker["error_code"])
                continue
            if vision_query_pending(clip.analysis, candidate.question, clip.generation):
                defer(candidate)
                for dependent in dependents_for_candidate:
                    work_by_id[dependent.intent_id].pending_media_ids.add(dependent.media_id)
                continue
            to_call.append(candidate)

        cap = (
            max(1, min(len(intents), 6) * 50)
            if background
            else min(4, settings.clip_intents_max_vision_requeries)
        )
        terminal_budget = any(
            work.ai_budget_exhausted_media_ids
            or work.provider_quota_exhausted_media_ids
            or work.provider_unknown_media_ids
            for work in work_by_id.values()
        )
        allowed = 0 if terminal_budget else min(max(0, cap - calls_spent), len(to_call))
        for candidate in to_call[allowed:]:
            if not terminal_budget:
                defer(candidate)
            key = (
                candidate.media_id,
                str(clip_by_id[candidate.media_id].generation or ""),
                normalize_question(candidate.question),
            )
            for dependent in dependents[key]:
                work_by_id[dependent.intent_id].pending_media_ids.add(dependent.media_id)
        to_call = to_call[:allowed]

        question_agent = ClipQuestionAgent(default_client())
        batch_size = 4
        for offset in range(0, len(to_call), batch_size):
            batch = to_call[offset : offset + batch_size]
            if not background and asyncio.get_running_loop().time() >= foreground_deadline:
                for candidate in batch:
                    defer(candidate)
                    key = (
                        candidate.media_id,
                        str(clip_by_id[candidate.media_id].generation or ""),
                        normalize_question(candidate.question),
                    )
                    for dependent in dependents[key]:
                        work_by_id[dependent.intent_id].pending_media_ids.add(dependent.media_id)
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
                    defer(candidate)
                    for dependent in dependents_for_candidate:
                        work_by_id[dependent.intent_id].pending_media_ids.add(dependent.media_id)
                    task.cancel()
                    continue
                try:
                    output = task.result()
                except Exception as exc:  # noqa: BLE001 — classify per candidate
                    _status, error_code = _failure_status_and_code(exc)
                    record_failure(dependents_for_candidate, error_code)
                    if error_code == "vision_provider_error":
                        defer(candidate)
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
                for work in work_by_id.values()
            ):
                for remaining in to_call[offset + batch_size :]:
                    remaining_key = (
                        remaining.media_id,
                        str(clip_by_id[remaining.media_id].generation or ""),
                        normalize_question(remaining.question),
                    )
                    for dependent in dependents[remaining_key]:
                        work_by_id[dependent.intent_id].pending_media_ids.add(dependent.media_id)
                break

    def apply_membership(candidate, output):
        _apply_vision_result(
            candidate,
            output,
            work_by_id=work_by_id,
            records_by_id=records_by_id,
            creator_request=creator_request,
        )

    await run_candidates(vision_candidates, apply_membership)

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
            )
            if grounded is not None:
                work.caption_text = grounded.text
                work.caption_grounding = grounded.grounding
            else:
                work.intent_question = _caption_question(intent)
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

    # ── Assemble the result ──────────────────────────────────────────────────
    resolved_intents: list[ResolvedClipIntent] = []
    unresolved_work: list[_IntentWork] = []
    creator_work: list[_IntentWork] = []
    for intent in intents:
        work = work_by_id[intent.intent_id]
        has_non_creator_failure = bool(
            work.pending_media_ids
            or work.provider_unavailable_media_ids
            or work.provider_unknown_media_ids
            or work.ai_budget_exhausted_media_ids
            or work.provider_quota_exhausted_media_ids
            or work.budget_exhausted_media_ids
            or work.media_unavailable_media_ids
        )
        # Technical/provider/media failures must not be turned into a creator
        # question. The caller can retry the preparation/resolution attempt;
        # only settled ambiguity or a genuinely empty result belongs in
        # `needs_creator`.
        is_creator_unresolved = not has_non_creator_failure and (
            bool(work.intent_question) or bool(work.failed_media_ids) or not work.kept
        )
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
                    creator_text=intent.creator_text,
                    caption_attribute=intent.caption_attribute,
                    position=intent.position,
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
                    creator_text=intent.creator_text,
                    caption_attribute=intent.caption_attribute,
                    position=intent.position,
                    status="resolved",
                    assignments=work.kept,
                    caption_text=work.caption_text,
                    caption_grounding=work.caption_grounding,
                )
            )

    if not unresolved_work:
        return IntentResolution(
            intents=resolved_intents,
            question=None,
            vision_answers=vision_answers,
            status="resolved",
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
    final_intents = [
        (i if i.status == "resolved" else i.model_copy(update={"question": turn_question}))
        for i in resolved_intents
    ]
    return IntentResolution(
        intents=final_intents,
        question=turn_question,
        vision_answers=vision_answers,
        status=status,
        error_code=error_code,
        deferred_queries=list(deferred.values()),
    )


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
        if verdict is False and output.confidence >= MEMBERSHIP_MIN_CONFIDENCE:
            return  # confidently NOT a member: settled, nothing to ask about
        if verdict is not True or output.confidence < MEMBERSHIP_MIN_CONFIDENCE:
            work.failed_media_ids.add(candidate.media_id)
            return

    if candidate.op == "label":
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

        value = candidate.fallback_value or clean_label_text(output.answer.title())
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
