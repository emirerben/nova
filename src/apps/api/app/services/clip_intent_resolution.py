"""Resolve creator clip intents to clips inside one chat turn (KRI-127).

Contract frozen here; the pipeline is: text resolver over the shared clip
records -> capped, deadline-bounded vision re-query for clips the record cannot
answer -> grounding fence (``app.schemas.clip_intents.ground_label``) -> either
fully resolved intents or ONE question for the creator. Never a silent partial.

Kept DB-free on purpose (no session, no row lock) — the caller does all
persistence (the resolved intents, plus ``IntentResolution.vision_answers``
onto each clip's stored ``analysis[ANSWERS_KEY]``) after this returns. This
module only ever touches the network for the resolver call and the capped
vision re-query calls; never the database.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.agents._model_client import default_client
from app.agents._runtime import RunContext, TerminalError
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
    clean_label_text,
    ground_label,
)
from app.services.clip_understanding import clip_record

log = structlog.get_logger()

# Key on a stored clip analysis holding cached vision answers (see IntentResolution).
ANSWERS_KEY = "answers"

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

    @property
    def needs_creator(self) -> bool:
        return self.question is not None


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

    @property
    def is_membership_check(self) -> bool:
        """True when the vision model confirms a clip BELONGS, rather than names a value."""
        return self.op != "label" or bool(self.creator_text)

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


def _cached_answer(clip: IntentClip, question_norm: str) -> dict[str, Any] | None:
    analysis = clip.analysis if isinstance(clip.analysis, dict) else None
    if not analysis:
        return None
    answers = analysis.get(ANSWERS_KEY)
    if not isinstance(answers, dict):
        return None
    hit = answers.get(question_norm)
    return hit if isinstance(hit, dict) else None


# The resolver's own spec allows 2 x 20s + 1s backoff; this is the hard stop the
# chat turn waits for before it degrades to a question.
_RESOLVER_DEADLINE_S = 45.0


def _fallback_question(intent: ClipIntent) -> str:
    return f"What is the {intent.attribute}?"


def _membership_question(attribute: str) -> str:
    return f'Does this clip match this description: "{attribute}"? Answer only "yes" or "no".'


def _yes_no(answer: str) -> bool | None:
    word = answer.strip().casefold().split(" ")[0].strip(".,!") if answer.strip() else ""
    if word in {"yes", "true"}:
        return True
    if word in {"no", "false"}:
        return False
    return None


async def _run_vision_candidate(
    candidate: _VisionCandidate,
    clip: IntentClip,
    *,
    question_agent: ClipQuestionAgent,
    run_context: RunContext,
) -> ClipQuestionOutput | None:
    """Download + upload + ask. Returns None on ANY failure (never raises) —
    an individual clip's vision failure must never crash the whole turn."""
    from app.pipeline.agents.gemini_analyzer import gemini_upload_and_wait  # noqa: PLC0415
    from app.storage import download_to_file  # noqa: PLC0415

    if not clip.gcs_path or clip.kind != "video":
        return None
    tmp_path: str | None = None
    try:
        suffix = os.path.splitext(clip.gcs_path)[1] or ".mp4"
        fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        await asyncio.to_thread(download_to_file, clip.gcs_path, tmp_path)
        file_ref = await asyncio.to_thread(gemini_upload_and_wait, tmp_path)
        mime = getattr(file_ref, "mime_type", None) or "video/mp4"
        out = await asyncio.to_thread(
            question_agent.run,
            ClipQuestionInput(file_uri=file_ref.uri, file_mime=mime, question=candidate.question),
            ctx=run_context,
        )
        return out
    except Exception as exc:  # noqa: BLE001 — one clip's vision failure asks, never crashes
        log.warning(
            "clip_intent_vision_query_failed",
            media_id=candidate.media_id,
            intent_id=candidate.intent_id,
            error=str(exc),
        )
        return None
    finally:
        if tmp_path is not None:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _position_phrase(positions: list[int]) -> str:
    ordered = sorted(set(positions))
    if len(ordered) == 1:
        return f"clip {ordered[0]}"
    labels = [str(p) for p in ordered]
    return f"clips {', '.join(labels[:-1])} and {labels[-1]}"


def _build_question(work_items: list[_IntentWork], position_by_media: dict[str, int]) -> str:
    parts: list[str] = []
    for work in work_items:
        if work.intent_question:
            parts.append(work.intent_question)
            continue
        positions = [position_by_media[m] for m in work.failed_media_ids if m in position_by_media]
        if positions:
            parts.append(
                f"I couldn't tell the {work.intent.attribute} for {_position_phrase(positions)}"
            )
        elif not work.kept:
            label = work.intent.creator_text or work.intent.attribute
            parts.append(f'I couldn\'t find any clips for "{label}"')
    if not parts:
        return _GENERIC_QUESTION
    joined = "; ".join(parts)
    return f"{joined}. Could you clarify?"[:300]


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
            position=i.position,
        )
        for i in intents
    ]
    resolver_input = ClipRequestResolverInput(
        creator_request=creator_request or "",
        intents=resolver_intents,
        clips=resolver_clips,
    )
    return resolver_input, alias_to_media, aliases


# ── Main entry point ──────────────────────────────────────────────────────────


async def resolve_clip_intents_for_turn(
    *,
    intents: list[ClipIntent],
    creator_request: str,
    clips: list[IntentClip],
    run_context: Any,
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
        resolver_output: ClipRequestResolverOutput = await asyncio.wait_for(
            asyncio.to_thread(resolver_agent.run, resolver_input, ctx=ctx),
            timeout=_RESOLVER_DEADLINE_S,
        )
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never raise from a chat turn
        # Covers TerminalError (refusal/schema/transient-exhausted) and any
        # AI-cost-control TerminalError subclass (budget exhausted, policy
        # rejection) — none of those should ever surface as a 500 mid-chat.
        log.warning(
            "clip_intent_resolver_failed",
            error=str(exc),
            is_terminal_error=isinstance(exc, TerminalError),
        )
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
        )

    resolver_by_id = {r.intent_id: r for r in resolver_output.intents}
    work_by_id: dict[str, _IntentWork] = {i.intent_id: _IntentWork(intent=i) for i in intents}

    vision_candidates: list[_VisionCandidate] = []
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

        candidate_media = set(assignment_by_media) | set(vision_media)
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
            else:  # group / order / include: membership only, never printed
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

    # ── Capped, deadline-bounded vision re-query ─────────────────────────────
    cap = settings.clip_intents_max_vision_requeries
    to_call: list[_VisionCandidate] = []
    for candidate in vision_candidates:
        clip = clip_by_id.get(candidate.media_id)
        if clip is None:
            work_by_id[candidate.intent_id].failed_media_ids.add(candidate.media_id)
            continue
        question_norm = normalize_question(candidate.question)
        cached = _cached_answer(clip, question_norm)
        if cached is not None:
            _apply_vision_result(
                candidate,
                ClipQuestionOutput(
                    answer=str(cached.get("answer", "") or ""),
                    confidence=float(cached.get("confidence", 0.0) or 0.0),
                    evidence=str(cached.get("evidence", "") or ""),
                ),
                work_by_id=work_by_id,
                records_by_id=records_by_id,
                creator_request=creator_request,
            )
            continue
        if len(to_call) >= cap:
            # Over the per-turn budget — ask rather than burn an unbounded
            # number of File API uploads on one chat turn.
            work_by_id[candidate.intent_id].failed_media_ids.add(candidate.media_id)
            continue
        to_call.append(candidate)

    if to_call:
        question_agent = ClipQuestionAgent(default_client())
        tasks = [
            asyncio.ensure_future(
                _run_vision_candidate(
                    c, clip_by_id[c.media_id], question_agent=question_agent, run_context=ctx
                )
            )
            for c in to_call
        ]
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=settings.clip_intents_vision_deadline_s,
            )
        except TimeoutError:
            results = None
            for t in tasks:
                if not t.done():
                    t.cancel()

        if results is None:
            # Deadline hit before every call finished — every candidate we
            # attempted is unresolved this turn. No exception ever escapes.
            for candidate in to_call:
                work_by_id[candidate.intent_id].failed_media_ids.add(candidate.media_id)
        else:
            for candidate, result in zip(to_call, results, strict=True):
                output = result if isinstance(result, ClipQuestionOutput) else None
                if output is not None:
                    question_norm = normalize_question(candidate.question)
                    vision_answers.setdefault(candidate.media_id, {})[question_norm] = {
                        "answer": output.answer,
                        "confidence": output.confidence,
                        "evidence": output.evidence,
                    }
                _apply_vision_result(
                    candidate,
                    output,
                    work_by_id=work_by_id,
                    records_by_id=records_by_id,
                    creator_request=creator_request,
                )

    # ── Assemble the result ──────────────────────────────────────────────────
    resolved_intents: list[ResolvedClipIntent] = []
    unresolved_work: list[_IntentWork] = []
    for intent in intents:
        work = work_by_id[intent.intent_id]
        is_unresolved = bool(work.intent_question) or bool(work.failed_media_ids) or not work.kept
        if is_unresolved:
            unresolved_work.append(work)
            resolved_intents.append(
                ResolvedClipIntent(
                    intent_id=intent.intent_id,
                    op=intent.op,
                    attribute=intent.attribute,
                    creator_text=intent.creator_text,
                    position=intent.position,
                    status="needs_creator",
                    assignments=work.kept,
                    question=None,  # set on the turn-level question below
                )
            )
        else:
            resolved_intents.append(
                ResolvedClipIntent(
                    intent_id=intent.intent_id,
                    op=intent.op,
                    attribute=intent.attribute,
                    creator_text=intent.creator_text,
                    position=intent.position,
                    status="resolved",
                    assignments=work.kept,
                )
            )

    if not unresolved_work:
        return IntentResolution(
            intents=resolved_intents, question=None, vision_answers=vision_answers
        )

    turn_question = _build_question(unresolved_work, position_by_media)
    final_intents = [
        (i if i.status == "resolved" else i.model_copy(update={"question": turn_question}))
        for i in resolved_intents
    ]
    return IntentResolution(
        intents=final_intents, question=turn_question, vision_answers=vision_answers
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
