"""Decide, ground and validate a spoken-excerpt montage plan (KRI-282).

Pure orchestration: the caller injects how to get word timings for a clip and
how to run the planner, so this module does no I/O of its own and is trivially
testable. ``app.tasks.generative_build._run_phone_speech_montage_job`` wires the
real ones.

Contract:

* ``not_requested`` -- the creator did not ask to use what they say (or nothing
  here applies). The caller renders the ordinary montage, byte-identically.
* ``ready`` -- at least one excerpt was grounded to real word timings; the
  grounded sections (and anything dropped, with reasons) come back.
* ``needs_creator`` -- the creator asked for something we cannot do without
  guessing. ``question`` is specific and actionable: it names the quote that
  could not be found, the clips that could be the speaker, or what is missing.
  It is never the generic clip-command failure.

The planner chooses excerpts by MEANING and names them by quoted phrase. This
module grounds each quote to word timings (``ground_excerpt``); a quote that
was not actually said is never rendered.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog

from app.agents.speech_excerpt_planner import (
    OtherClipView,
    SpeechClipView,
    SpeechExcerptPlannerInput,
)
from app.schemas.speech_montage import (
    MAX_EXCERPT_S,
    MontageSection,
    SpeechMontagePlan,
    SpeechSection,
)
from app.services.speech_segments import (
    MIN_SPEECH_WORDS,
    ground_excerpt,
    spoken_word_count,
    words_to_segments,
)

log = structlog.get_logger()

_MAX_SPEECH_CLIPS = 6
_MAX_OTHER_CLIPS = 60
_MIN_EXCERPT_S = 0.8
# A voice-pinned plan under this share of the target is reported, not passed silently.
_SHORT_RENDER_RATIO = 0.5

# Only used when NO clip has speech: with speech present the planner (open
# vocabulary) decides whether the creator asked for it. Without any speech
# clip we still want to answer a creator who asked, so a cheap multilingual
# hint decides whether the planner is worth a call at all.
_SPEECH_HINT = re.compile(
    r"(?i)\b(say|says|said|saying|speak|speaks|spoke|spoken|talk|talks|talking|voice|quote|"
    r"quotes|excerpt|excerpts|sentence|sentences|what i|play my|hear me|my (?:best )?lines?)\b"
    r"|söyl|konuş|sesim|cümle|diyor|dediğ"
)

NO_SPEECH_QUESTION = (
    "I couldn't hear anyone talking in your clips, so I can't play what you say. Add a clip "
    "where you talk to the camera, or tell me to make it without speech."
)


def mentions_speech(request: str) -> bool:
    return bool(_SPEECH_HINT.search(request or ""))


def speech_montage_possible(request: str, *, any_clip_has_speech: bool) -> bool:
    """The first-stage gate: could this request become a spoken-excerpt montage?

    Shared by `plan_speech_montage`, the worker (`run_phone_speech_montage_job`)
    and the output-shape offer (`render_shape.creation_offer`), which must not
    offer a shape the speech montage would silently ignore (it renders portrait).
    The planner (open vocabulary) makes the final call when a clip has speech, so
    this is deliberately the conservative superset.
    """
    return bool((request or "").strip()) and (any_clip_has_speech or mentions_speech(request))


@dataclass
class SpeechCandidate:
    """One clip as the speech-montage planner sees it."""

    media_id: str
    duration_s: float
    summary: str = ""
    kind: Literal["video", "image"] = "video"
    has_speech: bool = False
    to_camera: bool = False
    # Filled by the caller's ``load_words``; empty until then.
    words: list[dict[str, Any]] = field(default_factory=list)
    language: str = ""


@dataclass(frozen=True)
class GroundedSection:
    kind: Literal["speech", "montage"]
    media_id: str | None = None
    visual: Literal["speaker", "cutaways"] = "speaker"
    quote: str = ""
    source_start_s: float = 0.0
    source_end_s: float = 0.0
    duration_s: float = 0.0
    cut_s: float | None = None
    exact: bool = True

    def record(self) -> dict[str, Any]:
        row: dict[str, Any] = {"kind": self.kind}
        if self.kind == "speech":
            row.update(
                media_id=self.media_id,
                visual=self.visual,
                quote=self.quote,
                source_start_s=round(self.source_start_s, 3),
                source_end_s=round(self.source_end_s, 3),
                exact=self.exact,
            )
        else:
            row["duration_s"] = round(self.duration_s, 3)
            if self.cut_s:
                row["cut_s"] = self.cut_s
        return row


@dataclass
class SpeechMontageResolution:
    status: Literal["not_requested", "ready", "needs_creator"]
    sections: list[GroundedSection] = field(default_factory=list)
    question: str | None = None
    # Quotes that could not be grounded (kept for the receipt): {"quote", "reason"}.
    dropped: list[dict[str, str]] = field(default_factory=list)
    adjustments: list[str] = field(default_factory=list)

    def planned_record(self) -> dict[str, Any]:
        return {
            "sections": [s.record() for s in self.sections],
            "dropped": self.dropped,
            "adjustments": self.adjustments,
        }


PlannerRunner = Callable[[SpeechExcerptPlannerInput], SpeechMontagePlan]
WordLoader = Callable[[SpeechCandidate], tuple[list[dict[str, Any]], str]]


def _short(text: str, limit: int = 70) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _describe(candidate: SpeechCandidate, index: int) -> str:
    if candidate.summary:
        return _short(candidate.summary, 60)
    return f"clip {index}"


def _load_speech(
    claimed: Sequence[SpeechCandidate], load_words: WordLoader
) -> list[SpeechCandidate]:
    """Word timings for each clip that claims speech.

    A clip whose transcript is too thin (music, a few words) is not a speech source.
    """
    speech: list[SpeechCandidate] = []
    for candidate in claimed[: _MAX_SPEECH_CLIPS * 2]:
        try:
            words, language = load_words(candidate)
        except Exception as exc:  # noqa: BLE001 - one clip's transcription must not sink the edit
            _reraise_if_timeout(exc)
            log.warning("speech_montage_transcribe_failed", media_id=candidate.media_id)
            continue
        if spoken_word_count(words) < MIN_SPEECH_WORDS:
            continue
        candidate.words, candidate.language = words, language
        speech.append(candidate)
    return speech


def plan_speech_montage(
    *,
    creator_request: str,
    candidates: Sequence[SpeechCandidate],
    run_planner: PlannerRunner,
    load_words: WordLoader,
    target_duration_s: float | None = None,
    voice_media_ids: Collection[str] = (),
) -> SpeechMontageResolution:
    """See the module docstring. Never raises for planner/transcription trouble.

    ``voice_media_ids`` are the clips the creator's strategy already named as THE
    voice (``montage_audio.source_media_ids``). When one of them has usable speech
    it is the only speech source the planner sees: every other clip is plain
    footage, so no excerpt can come from a different speaker.
    """
    request = (creator_request or "").strip()
    videos = [c for c in candidates if c.kind == "video"]
    claimed = [c for c in videos if c.has_speech]
    if not speech_montage_possible(request, any_clip_has_speech=bool(claimed)):
        return SpeechMontageResolution("not_requested")

    voice_ids = set(voice_media_ids)
    speech: list[SpeechCandidate] = []
    pinned = [c for c in claimed if c.media_id in voice_ids]
    if pinned:
        speech = _load_speech(pinned, load_words)
    voice_pinned = bool(speech)
    if not speech:
        speech = _load_speech(claimed, load_words)
    speech.sort(key=lambda c: (not c.to_camera, -spoken_word_count(c.words)))
    speech = speech[:_MAX_SPEECH_CLIPS]
    speech_ids = {c.media_id for c in speech}
    others = [c for c in videos if c.media_id not in speech_ids]

    speech_refs = {f"c{i + 1}": c for i, c in enumerate(speech)}
    other_refs = {f"v{i + 1}": c for i, c in enumerate(others[:_MAX_OTHER_CLIPS])}
    planner_input = SpeechExcerptPlannerInput(
        creator_request=request,
        target_duration_s=target_duration_s,
        voice_clip_ref=next(iter(speech_refs), None) if voice_pinned else None,
        speech_clips=[
            SpeechClipView(
                ref=ref,
                duration_s=round(c.duration_s, 1),
                to_camera=c.to_camera,
                summary=_short(c.summary, 200),
                language=c.language,
                segments=[
                    {"s": round(seg.start_s, 1), "e": round(seg.end_s, 1), "text": seg.text}
                    for seg in words_to_segments(c.words)
                ][:40],
            )
            for ref, c in speech_refs.items()
        ],
        other_clips=[
            OtherClipView(
                ref=ref, duration_s=round(c.duration_s, 1), summary=_short(c.summary, 160)
            )
            for ref, c in other_refs.items()
        ],
    )

    try:
        plan = run_planner(planner_input)
    except Exception as exc:  # noqa: BLE001 - planner trouble is never a render failure by itself
        _reraise_if_timeout(exc)
        log.warning("speech_montage_planner_failed", error=type(exc).__name__)
        if mentions_speech(request):
            # The creator very likely asked for it; quietly rendering a plain montage
            # would be a silent override.
            return SpeechMontageResolution(
                "needs_creator",
                question=(
                    "I couldn't work out which of your lines to play just now. Tell me the "
                    "sentence you want to hear (or when it starts) and I'll build the edit "
                    "around it."
                ),
            )
        return SpeechMontageResolution("not_requested")

    if not plan.wants_speech_excerpts:
        return SpeechMontageResolution("not_requested")
    if plan.question:
        return SpeechMontageResolution("needs_creator", question=plan.question)
    if not speech:
        return SpeechMontageResolution("needs_creator", question=NO_SPEECH_QUESTION)
    resolution = _ground(plan, speech_refs, others)
    if voice_pinned and target_duration_s and resolution.status == "ready":
        total = sum(
            (s.source_end_s - s.source_start_s) if s.kind == "speech" else s.duration_s
            for s in resolution.sections
        )
        if total < _SHORT_RENDER_RATIO * target_duration_s:
            log.warning(
                "speech_montage_short_render",
                planned_s=round(total, 1),
                target_s=round(target_duration_s, 1),
            )
            resolution.adjustments.append(
                f"came out about {round(total)}s, shorter than the ~{round(target_duration_s)}s "
                "you asked for"
            )
    return resolution


def _reraise_if_timeout(exc: BaseException) -> None:
    try:
        from celery.exceptions import SoftTimeLimitExceeded  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    if isinstance(exc, SoftTimeLimitExceeded):
        raise exc


def _ground(
    plan: SpeechMontagePlan,
    speech_refs: dict[str, SpeechCandidate],
    others: list[SpeechCandidate],
) -> SpeechMontageResolution:
    sections: list[GroundedSection] = []
    dropped: list[dict[str, str]] = []
    adjustments: list[str] = []
    # Same quote twice from one clip resolves to its first and then its next utterance.
    last_end: dict[tuple[str, str], float] = {}
    for section in plan.sections:
        if isinstance(section, MontageSection):
            if not others:
                adjustments.append("skipped a fast-cut part because there is no other footage")
                continue
            sections.append(
                GroundedSection(kind="montage", duration_s=section.duration_s, cut_s=section.cut_s)
            )
            continue
        assert isinstance(section, SpeechSection)
        clip = speech_refs.get(section.clip_ref)
        if clip is None:
            dropped.append({"quote": section.quote, "reason": "unknown_clip"})
            continue
        key = (clip.media_id, section.quote.casefold())
        grounded = ground_excerpt(
            clip.words,
            section.quote,
            duration_s=clip.duration_s,
            min_after_s=last_end.get(key, 0.0),
        )
        if grounded is None:
            dropped.append({"quote": section.quote, "reason": "not_said"})
            continue
        length = grounded.end_s - grounded.start_s
        if length < _MIN_EXCERPT_S:
            dropped.append({"quote": section.quote, "reason": "too_short"})
            continue
        if length > MAX_EXCERPT_S:
            dropped.append({"quote": section.quote, "reason": "too_long"})
            continue
        last_end[key] = grounded.word_end_s
        visual = section.visual
        if visual == "cutaways" and not others:
            visual = "speaker"
            adjustments.append("showed you talking because there is no other footage to cut to")
        sections.append(
            GroundedSection(
                kind="speech",
                media_id=clip.media_id,
                visual=visual,
                quote=section.quote,
                source_start_s=grounded.start_s,
                source_end_s=grounded.end_s,
                exact=grounded.exact,
            )
        )

    if not any(s.kind == "speech" for s in sections):
        return SpeechMontageResolution(
            "needs_creator", question=_ungrounded_question(dropped, speech_refs), dropped=dropped
        )
    if dropped:
        names = ", ".join(f"“{_short(d['quote'], 40)}”" for d in dropped[:3])
        adjustments.append(f"left out lines I couldn't find in your clip: {names}")
    return SpeechMontageResolution(
        "ready", sections=sections, dropped=dropped, adjustments=adjustments
    )


def _ungrounded_question(
    dropped: list[dict[str, str]], speech_refs: dict[str, SpeechCandidate]
) -> str:
    first = dropped[0] if dropped else {"quote": "", "reason": "not_said"}
    quote = _short(first["quote"], 90)
    if first["reason"] == "too_long":
        return (
            f"The part starting “{quote}” runs too long to use as one excerpt. "
            "Which shorter sentence or two should I play?"
        )
    if first["reason"] == "too_short":
        return f"“{quote}” is too short to stand on its own. Which longer line should I use?"
    clips = list(speech_refs.values())
    where = (
        "in the clip where you talk"
        if len(clips) == 1
        else f"in any of the {len(clips)} clips where someone talks"
    )
    return (
        f"I couldn't find “{quote}” {where}. Tell me the line again in your own words, "
        "or which part of the talking clip to use."
    )


__all__ = [
    "NO_SPEECH_QUESTION",
    "GroundedSection",
    "SpeechCandidate",
    "SpeechMontageResolution",
    "mentions_speech",
    "plan_speech_montage",
]
