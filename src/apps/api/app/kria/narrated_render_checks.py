"""Tahini run (job 1d062f21, 2026-11-17): two asks judged on a finished phone Voiceover edit.

The creator asked "Change the picture about once a second; reuse a clip if needed." (the brief
typed ``approximate_cut_duration_s: 1``) and named clips on words ("the fork bite on "grab a
fork""). The edit had 12 shots in 33.1 s and the fork clip played only at 4.2-6.0 s, yet the
review listed both under "Have a look at these in the video": a clip-scoped word cue fell to the
generic style check when the render had no pop-in receipt, and nothing read the pace at all.

Both are judged here from the render's own steps (``assembly_plan["narrated_alignment"]``, the
``narrated_cue_steps`` the review reads), never from a model's claim:

* `cut_pace_s`: the pace the brief typed, in seconds per shot (a structured fact, KRI-667: the
  server reads no new wording). `check_cut_pace` counts the shots, their average and the longest.
* a clip-scoped ask with named words (`clip_cue_triggers`): `check_clip_cue` finds the words in
  the voiceover under the steps and says whether the clip on screen as they start is the clip the
  ask is scoped to, with the times; a moment named inside that clip that plays late says so too.

Render-time only (``facts.rendered_variant`` with steps): a draft has no steps, so these return
None there and the draft's receipts, holds and promises are what they were.
``NARRATED_CUE_RECEIPTS_ENABLED`` off: every function here returns None / False.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING

from app.kria.reply_language import say

if TYPE_CHECKING:
    from app.kria.brief import BriefRequirement
    from app.kria.brief_checks import NarratedStepFact, PlanFacts
    from app.kria.contracts import RequirementReceipt

# The typed pace facts a brief row may carry: seconds per shot.
_PACE_FACT_KEYS = (
    "approximate_cut_duration_s",
    "cut_duration_s",
    "cut_s",
    "shot_duration_s",
    "seconds_per_cut",
    "seconds_per_shot",
)
# The montage cadence's own bounds (`MontageCadenceConstraint.cut_duration_s`).
MIN_PACE_S = 0.4
MAX_PACE_S = 3.0
# "About once a second" is met when the shots average at most this many paces and none is
# longer than `_longest_allowed`.
_PACE_AVERAGE_SLACK = 1.5


def _receipts_on() -> bool:
    from app.config import settings  # noqa: PLC0415

    return bool(settings.narrated_cue_receipts_enabled)


def cut_pace_s(req: BriefRequirement) -> float | None:
    """Seconds per shot a brief row typed (``approximate_cut_duration_s`` ...), clamped to
    0.4-3.0 s; None for a text row or a row with no such number."""
    if req.kind == "text":
        return None
    facts = req.facts if isinstance(req.facts, Mapping) else {}
    for key in _PACE_FACT_KEYS:
        value = facts.get(key)
        if isinstance(value, bool) or value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(seconds) and seconds > 0:
            return round(min(MAX_PACE_S, max(MIN_PACE_S, seconds)), 3)
    return None


def brief_cut_pace_s(requirements: Iterable[BriefRequirement]) -> float | None:
    """The typed pace of the first live row that has one."""
    for req in requirements:
        if getattr(req, "live", True):
            pace = cut_pace_s(req)
            if pace is not None:
                return pace
    return None


def clip_cue_triggers(req: BriefRequirement) -> tuple[str, list[str]] | None:
    """``(scoped clip, named words)`` for a non-text row scoped to one clip that names the words
    it plays on ("the fork bite on "grab a fork""); None otherwise."""
    from app.kria.brief_checks import _named_triggers  # noqa: PLC0415

    if req.kind == "text" or not req.scope.startswith("clip:"):
        return None
    clip = req.scope.split(":", 1)[1].strip()
    triggers = [t for t in _named_triggers(req) if str(t).strip()]
    return (clip, triggers) if clip and triggers else None


def judged_on_narrated_render(req: BriefRequirement) -> bool:
    """A typed pace is judged on the finished Voiceover edit (never on a draft)."""
    return _receipts_on() and cut_pace_s(req) is not None


def check_narrated_render_ask(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """The pace or clip-cue receipt of a finished Voiceover edit; None leaves ``req`` to the
    checks it always had."""
    if not (_receipts_on() and facts.rendered_variant and facts.narrated_cue_steps):
        return None
    if cut_pace_s(req) is not None:
        return check_cut_pace(req, facts)
    return check_clip_cue(req, facts)


def _shots(steps: Sequence[NarratedStepFact]) -> list[float]:
    """Seconds each shot is on screen: a step that shows the clip the step before showed is
    the same shot."""
    shots: list[float] = []
    previous: str | None = None
    for step in steps:
        length = max(0.0, step.end_s - step.start_s)
        if previous is not None and step.media_id == previous and shots:
            shots[-1] += length
        else:
            shots.append(length)
        previous = step.media_id
    return shots


def _longest_allowed(pace: float) -> float:
    return max(3.0 * pace, pace + 2.0)


def check_cut_pace(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """Shots, their average and the longest, against the typed pace."""
    from app.kria.brief_checks import _receipt  # noqa: PLC0415

    pace = cut_pace_s(req)
    shots = _shots(facts.narrated_cue_steps or ())
    if pace is None or not shots:
        return None
    total = sum(shots)
    average = total / len(shots)
    longest = max(shots)
    if average <= _PACE_AVERAGE_SLACK * pace + 1e-6 and longest <= _longest_allowed(pace) + 1e-6:
        reason = say(
            en=(
                "{shots} shots in {total:.1f} s, about one every {average:.1f} s; "
                "the longest is {longest:.1f} s"
            ),
            tr=(
                "{total:.1f} sn içinde {shots} çekim, ortalama {average:.1f} sn'de bir; "
                "en uzunu {longest:.1f} sn"
            ),
            shots=len(shots),
            total=total,
            average=average,
            longest=longest,
        )
        return _receipt(req, "met", reason, verification="checked", stage="checked")
    reason = say(
        en=(
            "{shots} shots in {total:.1f} s, about one every {average:.1f} s, not every "
            "{asked:.1f} s; the longest is {longest:.1f} s"
        ),
        tr=(
            "{total:.1f} sn içinde {shots} çekim, ortalama {average:.1f} sn'de bir, "
            "{asked:.1f} sn'de bir değil; en uzunu {longest:.1f} sn"
        ),
        shots=len(shots),
        total=total,
        average=average,
        asked=pace,
        longest=longest,
    )
    return _receipt(req, "partial", reason, verification="checked", stage="checked")


def _clip_matches(step: NarratedStepFact, clip: str) -> bool:
    return clip in {step.media_id, step.clip} - {""}


def _token_steps(steps: Sequence[NarratedStepFact]) -> tuple[list[str], list[int]]:
    from app.services.phone_reaction_grounding import fold_tokens  # noqa: PLC0415

    tokens: list[str] = []
    owners: list[int] = []
    for index, step in enumerate(steps):
        said = fold_tokens(step.text)
        tokens.extend(said)
        owners.extend([index] * len(said))
    return tokens, owners


def _spans(steps: Sequence[NarratedStepFact], clip: str) -> str:
    """Where ``clip`` plays, "4.2–6.0 s" each, merged where its steps touch."""
    spans: list[list[float]] = []
    for step in steps:
        if not _clip_matches(step, clip):
            continue
        if spans and abs(spans[-1][1] - step.start_s) < 1e-3:
            spans[-1][1] = step.end_s
        else:
            spans.append([step.start_s, step.end_s])
    return ", ".join(f"{start:.1f}–{end:.1f}" for start, end in spans)


def check_clip_cue(req: BriefRequirement, facts: PlanFacts) -> RequirementReceipt | None:
    """Is the clip ``req`` is scoped to on screen when its named words start?

    The words are found in the voiceover under the steps (the matcher's folding); the step that
    holds the first word is on screen as they are said. Met, or partial when the moment named in
    that clip plays late (`_named_moment_note`); "Couldn't" with both clips' times when another
    clip is on screen. None (the old check decides) when the words are not found in the steps or
    the clip is no step of the edit."""
    from app.kria.brief_checks import _named_moment_note, _names, _receipt  # noqa: PLC0415
    from app.services.phone_reaction_grounding import fold_tokens  # noqa: PLC0415

    asked = clip_cue_triggers(req)
    steps = list(facts.narrated_cue_steps or ())
    if asked is None or not steps:
        return None
    clip, triggers = asked
    if not any(_clip_matches(step, clip) for step in steps):
        return None
    tokens, owners = _token_steps(steps)
    verdicts: list[tuple[str, str]] = []
    for name in triggers:
        wanted = fold_tokens(name)
        found = [
            owners[i]
            for i in range(len(tokens) - len(wanted) + 1)
            if wanted and tokens[i : i + len(wanted)] == wanted
        ]
        if not found:
            return None
        on_it = [index for index in found if _clip_matches(steps[index], clip)]
        index = on_it[0] if on_it else found[0]
        step = steps[index]
        if on_it:
            late = _named_moment_note(req, step, name, index, len(steps))
            if late is not None:
                verdicts.append(("partial", late))
                continue
            verdicts.append(
                (
                    "met",
                    say(
                        en=(
                            "When you say {v1}, the clip you named is on screen "
                            "({step_start_s:.1f}–{step_end_s:.1f} s)"
                        ),
                        tr=(
                            "{v1} dediğinde söylediğin klip ekranda "
                            "({step_start_s:.1f}–{step_end_s:.1f} sn)"
                        ),
                        v1=_names([name]),
                        step_start_s=step.start_s,
                        step_end_s=step.end_s,
                    ),
                )
            )
            continue
        verdicts.append(
            (
                "not_possible",
                say(
                    en=(
                        "When you say {v1}, another clip is on screen "
                        "({step_start_s:.1f}–{step_end_s:.1f} s); the clip you named plays at "
                        "{shown_at} s"
                    ),
                    tr=(
                        "{v1} dediğinde ekranda başka bir klip var "
                        "({step_start_s:.1f}–{step_end_s:.1f} sn); söylediğin klip "
                        "{shown_at} sn arasında oynuyor"
                    ),
                    v1=_names([name]),
                    step_start_s=step.start_s,
                    step_end_s=step.end_s,
                    shown_at=_spans(steps, clip),
                ),
            )
        )
    rank = {"met": 0, "partial": 1, "not_possible": 2}
    status = max((status for status, _reason in verdicts), key=rank.__getitem__)
    reason = "; ".join(reason for _status, reason in verdicts)
    return _receipt(req, status, reason, verification="checked", stage="checked")
