"""KRI-849: the creator's "if you can't tell what he says, ask me instead of guessing".

The Main Creator records that ask as a brief fact (never read off the words, KRI-667):

* ``facts["ask_if_unclear"]``: the creator's own words for the slot ("the third sauce he
  names"), on the captions requirement or on a row of its own. Advisory copy only: the server
  decides WHICH word is unclear by comparing two hearings of the speech
  (``app.pipeline.caption_unclear``), not by reading these words.
* ``facts["unclear_answer"]``: the creator's later answer in their own words, as the model
  records it: ``"omit"`` (leave the word out), or the exact words they typed.
* ``facts["unclear_answers"]``: the server's structured answers to the gate's unclear-word
  questions (``choice_questions.answered_brief``): ``[{start_s, end_s, action, text?}]``.

This module only reads those facts and runs the pre-approval comparison on the approved-media
snapshot row; the question lives in ``services/choice_questions`` and the render in
``tasks/generative_build``. No regex and no word tests on creator text.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.pipeline import caption_unclear as cu

ASK_FACT = "ask_if_unclear"
ANSWER_FACT = "unclear_answer"
ANSWERS_FACT = "unclear_answers"

# The model records "leave it out" as this value in ``unclear_answer`` (English, whatever the
# creator's language); any other text is the words the creator typed.
OMIT_VALUE = "omit"
_MAX_TYPED = 80


def _text(value: object) -> str:
    return " ".join(str(value).split()) if isinstance(value, str) else ""


def _facts(req: Any) -> Mapping[str, Any]:
    facts = getattr(req, "facts", None)
    return facts if isinstance(facts, Mapping) else {}


def asks_if_unclear(req: Any) -> bool:
    """True for a live requirement carrying the recorded ask."""
    return bool(getattr(req, "live", True)) and bool(_text(_facts(req).get(ASK_FACT)))


def ask_requirements(requirements: Iterable[Any]) -> list[Any]:
    return [req for req in requirements if asks_if_unclear(req)]


def ask_text(requirements: Iterable[Any]) -> str:
    """The creator's words for the slot, from the first requirement that carries them."""
    return next((_text(_facts(req).get(ASK_FACT)) for req in ask_requirements(requirements)), "")


def model_answer(requirements: Iterable[Any]) -> cu.UnclearAnswer | None:
    """The answer the model recorded from the creator's own words, if any: ``omit``, or the
    typed words (the last live requirement carrying one wins). Applies to every span unless a
    structured answer names the span."""
    found: cu.UnclearAnswer | None = None
    for req in ask_requirements(requirements):
        value = _text(_facts(req).get(ANSWER_FACT))
        if not value:
            continue
        if value.casefold() == OMIT_VALUE:
            found = cu.UnclearAnswer(cu.ACTION_OMIT)
        elif len(value) <= _MAX_TYPED:
            found = cu.UnclearAnswer(cu.ACTION_USE, text=value)
    return found


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def recorded_answers(requirements: Iterable[Any]) -> list[cu.UnclearAnswer]:
    """The structured answers ``answered_brief`` pinned on the ask requirements."""
    out: list[cu.UnclearAnswer] = []
    for req in ask_requirements(requirements):
        rows = _facts(req).get(ANSWERS_FACT)
        for row in rows if isinstance(rows, list) else ():
            if not isinstance(row, Mapping):
                continue
            action = row.get("action")
            start, end = _number(row.get("start_s")), _number(row.get("end_s"))
            if action not in (cu.ACTION_OMIT, cu.ACTION_USE) or start is None or end is None:
                continue
            out.append(cu.UnclearAnswer(action, start, end, _text(row.get("text"))))
    return out


# -- the pre-approval comparison ------------------------------------------------------------------


def _single_take(
    media_snapshot: Mapping[str, Any] | None, strategy: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    from app.kria.opening_cut_ask import _single_take as single  # noqa: PLC0415

    return single(media_snapshot, strategy)


def take_hearings(row: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], str]:
    """``(whisper sentence segments, Gemini transcript)`` of an approved-media snapshot row."""
    analysis = row.get("analysis") if isinstance(row, Mapping) else None
    understanding = analysis.get("understanding") if isinstance(analysis, Mapping) else None
    speech = understanding.get("speech") if isinstance(understanding, Mapping) else None
    speech = speech if isinstance(speech, Mapping) else {}
    segments = [s for s in speech.get("segments") or [] if isinstance(s, Mapping)]
    gemini = _text(speech.get("transcript")) or _text(
        analysis.get("transcript") if isinstance(analysis, Mapping) else None
    )
    return segments, gemini


def row_identity(row: Mapping[str, Any] | None) -> list[Any]:
    """What identifies the clip a question was asked about (not its analysis)."""
    keys = ("media_id", "gcs_path", "storage_generation", "gcs_generation", "generation")
    return [row.get(key) for key in keys] if isinstance(row, Mapping) else []


@dataclass(frozen=True)
class UnclearFinding:
    """The pre-approval comparison for a draft whose brief carries the ask.

    ``state`` is ``none`` (no ask), else the comparison's: ``agree``, ``unclear`` (spans) or
    ``not_comparable``. ``row`` is the take it was run on.
    """

    state: str
    spans: tuple[cu.UnclearSpan, ...] = ()
    row: Mapping[str, Any] | None = None
    ask: str = ""


def detect(
    requirements: Sequence[Any],
    media_snapshot: Mapping[str, Any] | None,
    *,
    strategy: Mapping[str, Any] | None = None,
    spellings: Sequence[str] = (),
) -> UnclearFinding:
    """Compare the two hearings of the one take a Talking draft plays, inside the part of it the
    creator's start / end lines keep (the whole take when no window is known)."""
    asks = ask_requirements(requirements)
    if not asks:
        return UnclearFinding("none")
    ask = ask_text(asks)
    row = _single_take(media_snapshot, strategy)
    segments, gemini = take_hearings(row)
    if row is None or not segments or not gemini:
        return UnclearFinding(cu.STATE_NOT_COMPARABLE, (), row, ask)
    window: tuple[float | None, float | None] | None = None
    try:
        from app.kria.opening_cut_ask import estimate_take_window  # noqa: PLC0415

        estimate = estimate_take_window(requirements, media_snapshot, strategy=strategy)
        if estimate is not None and (estimate.start_s is not None or estimate.end_s is not None):
            window = (estimate.start_s, estimate.end_s)
    except Exception:  # noqa: BLE001 - an unknown window means the whole take, never a failure
        window = None
    result = cu.compare_segments(segments, gemini, spellings=spellings, window=window)
    return UnclearFinding(result.state, result.spans, row, ask)


def unsettled(
    spans: Sequence[cu.UnclearSpan], requirements: Sequence[Any]
) -> list[tuple[int, cu.UnclearSpan]]:
    """The spans no recorded answer (structured or typed) has settled yet, with their position
    among all spans."""
    if model_answer(requirements) is not None:
        return []
    answers = recorded_answers(requirements)
    open_spans: list[tuple[int, cu.UnclearSpan]] = []
    used: set[int] = set()
    for position, span in enumerate(spans):
        hit = next(
            (
                n
                for n, answer in enumerate(answers)
                if n not in used and cu.answer_gap(span, answer) is not None
            ),
            None,
        )
        if hit is None:
            open_spans.append((position, span))
        else:
            used.add(hit)
    return open_spans


def draft_state(
    requirements: Sequence[Any],
    media_snapshot: Mapping[str, Any] | None,
    *,
    strategy: Mapping[str, Any] | None = None,
    spellings: Sequence[str] = (),
) -> dict[str, Any] | None:
    """What a Talking draft will do about the unclear word, for the receipts (``PlanFacts``).

    ``None`` without the ask. Else ``{"state": "agree" | "not_comparable" | "applied",
    "spans": [{start_s, end_s, action, text?}]}``: every span is either answered (the creator's
    tap or typed words) or left out, so the draft never promises a word it does not hold.
    """
    finding = detect(requirements, media_snapshot, strategy=strategy, spellings=spellings)
    if finding.state == "none":
        return None
    if finding.state != cu.STATE_UNCLEAR:
        return {"state": finding.state, "spans": []}
    default = model_answer(requirements)
    decisions = cu.decide(
        finding.spans, recorded_answers(requirements), ask_active=True, default=default
    )
    return {
        "state": "applied",
        "spans": [
            {
                "start_s": round(span.start_s, 3),
                "end_s": round(span.end_s, 3),
                "action": "written" if action == cu.ACTION_USE else "omitted",
                "source": source,
                **({"text": text} if text else {}),
            }
            for span, action, text, source in decisions
        ],
    }


# -- the render --------------------------------------------------------------------------------

# A window estimated from sentence timings and the precise word span of the same stretch are one
# stretch when they are this close.
_SAME_STRETCH_S = 0.5


def _caption_window(map_window: Any, start_s: float, end_s: float) -> tuple[float, float] | None:
    return (start_s, end_s) if map_window is None else map_window(start_s, end_s)


def _merge_windows(windows: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for lo, hi in sorted(windows):
        if merged and lo <= merged[-1][1] + _SAME_STRETCH_S:
            merged[-1][1] = max(merged[-1][1], hi)
        else:
            merged.append([lo, hi])
    return [(lo, hi) for lo, hi in merged]


def apply_to_cues(
    cues: Sequence[dict[str, Any]],
    raw_binding: Any,
    *,
    spellings: Sequence[str] = (),
    map_window: Any = None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The phone Talking writer's step: leave the unclear word out of the caption ``cues`` (or
    write the creator's own words there), per the approved brief pinned on the job.

    While the ask is active the windows acted on are the union of

    * (a) every window the creator answered by tap (``facts.unclear_answers``),
    * (b) the approval-time spans, recomputed from the PINNED snapshot row (Gemini's transcript
      against the whisper sentences the creator was asked about: deterministic, no render pass),
    * (c) the spans of the render pass itself, when it can be compared with Gemini's hearing.

    A precise render span absorbs the estimated windows next to it. Each window takes the
    creator's answer (their tap by window, else the typed ``unclear_answer``); a window nobody
    answered is left out: the captions never print a guess, and an answer is honoured whether or
    not the render pass is comparable (whisper-1 is re-run at render once its 24 h cache expires).
    ``map_window(start_s, end_s)`` moves a source-time window into the caption timeline (an
    opening cut shifts it); ``None`` = unmoved, and a window it drops is outside the video.

    Returns ``(cues, receipt)``; ``receipt`` is ``None`` without the ask (the variant is then
    byte-identical to before this feature), else ``{"state", "spans"}`` for the finished-video
    line, never holding a heard word.
    """
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415

    if not raw_binding:
        return list(cues), None
    binding = BriefBinding.model_validate(raw_binding)
    brief = binding.resolve()
    live = brief.live() if brief is not None else []
    if not ask_requirements(live):
        return list(cues), None
    words = [word for cue in cues for word in (cue.get("words") or []) if isinstance(word, Mapping)]
    _segments, gemini = take_hearings(_single_take(binding.media_snapshot, None))
    render = cu.compare_words(words, gemini, spellings=spellings)

    answers: list[cu.UnclearAnswer] = []
    estimated: list[tuple[float, float]] = []
    for answer in recorded_answers(live):
        window = _caption_window(map_window, answer.start_s or 0.0, answer.end_s or 0.0)
        if window is not None:
            answers.append(cu.UnclearAnswer(answer.action, window[0], window[1], answer.text))
            estimated.append(window)
    approval = detect(live, binding.media_snapshot, spellings=spellings)
    for span in approval.spans:
        window = _caption_window(map_window, span.start_s, span.end_s)
        if window is not None:
            estimated.append(window)

    precise = list(render.spans) if render.state == cu.STATE_UNCLEAR else []
    targets: list[cu.UnclearSpan] = list(precise)
    for lo, hi in _merge_windows(estimated):
        if not any(
            lo <= span.end_s + _SAME_STRETCH_S and hi >= span.start_s - _SAME_STRETCH_S
            for span in precise
        ):
            targets.append(cu.UnclearSpan(lo, hi, "", "", -1, -1, key=(), precise=False))
    targets.sort(key=lambda span: span.start_s)
    if not targets:
        return list(cues), {"state": render.state, "spans": []}
    out, report = cu.apply_unclear_answers(
        cues, targets, answers, ask_active=True, default=model_answer(live)
    )
    return out, {"state": "applied", "spans": report.rows()}


def fallback_omit(
    cues: Sequence[dict[str, Any]], raw_binding: Any, *, map_window: Any = None
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """What the writer falls back to when :func:`apply_to_cues` raised: drop the caption words
    inside the windows the pinned binding names, by the simplest means (the binding's own JSON,
    no comparison, no answers applied), and report ``state: "error"`` so the finished-video line
    says the unclear word could not be checked. ``None`` when the binding holds no active ask;
    ``error`` with no spans when no window could be read."""
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415

    try:
        pinned = BriefBinding.model_validate(raw_binding)  # the same trust check as everywhere
        data = pinned.model_dump(mode="json")
        requirements = [
            req
            for req in (data.get("brief") or {}).get("requirements") or []
            if req.get("status") != "superseded"
        ]
        asks = [req for req in requirements if _text((req.get("facts") or {}).get(ASK_FACT))]
    except Exception:  # noqa: BLE001 - an unreadable binding names no ask
        return list(cues), None
    if not asks:
        return list(cues), None
    windows: list[tuple[float, float]] = []
    try:
        for req in asks:
            for row in (req.get("facts") or {}).get(ANSWERS_FACT) or []:
                start, end = _number(row.get("start_s")), _number(row.get("end_s"))
                if start is not None and end is not None:
                    windows.append((start, end))
    except Exception:  # noqa: BLE001 - best effort
        pass
    try:
        brief = pinned.resolve()
        found = detect(brief.live() if brief is not None else [], data.get("media_snapshot"))
        windows.extend((span.start_s, span.end_s) for span in found.spans)
    except Exception:  # noqa: BLE001 - best effort
        pass
    mapped = [m for lo, hi in windows if (m := _caption_window(map_window, lo, hi)) is not None]
    merged = _merge_windows(mapped)
    out = cu.drop_windows(cues, merged) if merged else list(cues)
    rows = [
        {"start_s": round(lo, 3), "end_s": round(hi, 3), "action": "omitted", "source": "fallback"}
        for lo, hi in merged
    ]
    return out, {"state": "error", "spans": rows}


__all__ = [
    "ANSWERS_FACT",
    "ANSWER_FACT",
    "ASK_FACT",
    "OMIT_VALUE",
    "UnclearFinding",
    "apply_to_cues",
    "ask_requirements",
    "ask_text",
    "asks_if_unclear",
    "detect",
    "draft_state",
    "fallback_omit",
    "model_answer",
    "recorded_answers",
    "row_identity",
    "take_hearings",
    "unsettled",
]
