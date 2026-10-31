"""The planner's story directions on the phone montage route (prompt fidelity, text explainer).

The planner picks a direction: ``fast_montage`` (quick hard cuts), ``text_explainer`` (on-screen
text carries the story over footage) or ``guided_story`` (story beats). The unified phone
montage (`pipeline/unified_montage.py`) used to build only the first, whatever the planner chose.
This module builds the other two there, from the plan that route already made:

* The fast plan is the base. Its order, source windows and lengths stay (every creator ask the
  route reads is already folded into them), so no clip the creator selected is dropped and a
  stated length is not moved.
* The cuts become story beats: ``story_structure`` gives the chapters (each chapter's line is
  the beat's topic and its placeholder text), at most four clips to a beat, in the same order.
  The text step (`compose_creation_text`, the same research / animation path every montage
  uses) writes the captions that replace the placeholders; whatever it leaves stays.
* The guided compiler then holds every moment at least the direction's own minimum
  (`guided_story._DIRECTION_POLICY`), crossfades where the pace allows, and draws the beat text.

Owner rule: this never refuses a render and never raises. ``build_story_plan`` returns a reason
instead of a plan when a creator's ask only the fast-cut layout can express is live (text on
individual clips, a freeze, a speed change, a hidden part, a held photo, a spoken line kept
whole, ...), when the footage cannot give every clip the minimum within the length in force, or
when the story snapshot does not compile; the route then keeps the fast montage and says why
(``fallback_note``). Every function is pure. Kill switch: ``STORY_DIRECTIONS_ON_MONTAGE_ENABLED``
(`services.length_policy.story_direction`).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import structlog

from app.kria.reply_language import say
from app.schemas.edit_proposal import MAX_PROPOSAL_DURATION_S, EditProposalSnapshot, StoryBeat

if TYPE_CHECKING:
    from app.pipeline.unified_montage import BriefView, UnifiedMontagePlan

log = structlog.get_logger()

# `StoryBeat.media_ids` and `EditProposalSnapshot.story_beats` bounds.
MAX_MEDIA_PER_BEAT = 4
MAX_BEATS = 20
MAX_STORY_LINES = 8
# A placeholder caption is the planner's chapter line, kept readable on screen.
_SEED_CHARS = 100
# The ids of the placeholder caption rows this module seeds (the guided compiler names a beat's
# text ``guided-thought-<beat_id>``). The text step may replace them wherever it writes a
# caption (`researched_text`) and drops the ones it leaves (`creation_text_composition`).
STORY_BEAT_PREFIX = "story-beat-"
STORY_SEED_PREFIX = "guided-thought-" + STORY_BEAT_PREFIX

# Why the route kept the fast montage -> the creator-readable reason (en, tr).
_REASONS: dict[str, tuple[str, str]] = {
    "per_clip_text": (
        "you asked for text on individual clips, which only the fast-montage layout places",
        "tek tek kliplere yazı istedin, bunu sadece hızlı montaj düzeni yerleştirebiliyor",
    ),
    "freeze_speed": (
        "you asked for a freeze frame or a speed change, which only the fast-montage layout plays",
        "dondurulmuş kare ya da hız değişimi istedin, bunu sadece hızlı montaj düzeni oynatıyor",
    ),
    "hidden_parts": (
        "you asked me to keep a part of a clip out, which only the fast-montage layout can "
        "guarantee",
        "bir klibin bir bölümünü dışarıda tutmamı istedin, bunu sadece hızlı montaj düzeni "
        "garanti edebiliyor",
    ),
    "photo_timing": (
        "you set exact timing for photos or cuts, which only the fast-montage layout keeps",
        "fotoğraflar ya da kesimler için net süre verdin, bunu sadece hızlı montaj düzeni koruyor",
    ),
    "creator_song": (
        "your own song sets the cut points, which only the fast-montage layout follows",
        "kendi şarkın kesim noktalarını belirliyor, bunu sadece hızlı montaj düzeni izliyor",
    ),
    "spoken_windows": (
        "the clips' own spoken lines are kept whole, which only the fast-montage layout guarantees",
        "kliplerin kendi konuşmaları bütün tutuluyor, bunu sadece hızlı montaj düzeni garanti "
        "ediyor",
    ),
    "curved_text": (
        "you asked for curved text, which only the fast-montage layout draws",
        "kavisli yazı istedin, bunu sadece hızlı montaj düzeni çiziyor",
    ),
    "repeated_footage": (
        "the same clip plays more than once, which only the fast-montage layout allows",
        "aynı klip birden fazla kez oynuyor, buna sadece hızlı montaj düzeni izin veriyor",
    ),
    "named_moments": (
        "you named exact moments, which only the fast-montage layout plays as given",
        "belirli anları sen seçtin, bunları sadece hızlı montaj düzeni olduğu gibi oynatıyor",
    ),
    "holds_do_not_fit": (
        "the clips can't each be held long enough for it within the length you asked for",
        "klipler, istediğin süre içinde bunun için yeterince uzun tutulamıyor",
    ),
    "no_story_lines": (
        "the plan has no story outline to build chapters from",
        "bölümleri oluşturacak bir hikâye taslağı yok",
    ),
    "too_many_beats": (
        "there are too many clips to lay out as story chapters",
        "bölümlere ayırmak için çok fazla klip var",
    ),
    "story_not_compilable": (
        "the footage doesn't fit that layout",
        "görüntüler bu düzene uymuyor",
    ),
    "crossfade_unavailable": (
        "your iPhone build can't draw the crossfades that layout uses yet",
        "iPhone sürümün o düzenin kullandığı geçişleri henüz çizemiyor",
    ),
    "story_text_failed": (
        "the story text couldn't be placed on it",
        "hikâye yazısı buna yerleştirilemedi",
    ),
    "phone_rejected": (
        "your phone can't draw that layout for this footage",
        "telefonun bu görüntüler için o düzeni çizemiyor",
    ),
}

_DIRECTION_LABEL: dict[str, tuple[str, str]] = {
    "text_explainer": ("text explainer", "yazılı anlatım"),
    "guided_story": ("guided story", "yönlendirmeli hikâye"),
}


def says_fallback(reason: str | None, direction: str | None) -> bool:
    """Is ``reason`` worth a line to the creator for ``direction``?

    Only for a text explainer: that is a layout the creator's request asked for (explain,
    teach, "with text"). ``guided_story`` is the planner's default shape; the creator never
    asked for it, so the fast montage in its place needs no apology (the plan still records
    the reason)."""
    return bool(reason) and direction == "text_explainer"


def fallback_note(reason: str, direction: str, *, future: bool = False) -> str:
    """The creator-readable line saying why ``direction`` is cut as a fast montage.

    ``future`` is the approval card's wording ("will be cut"), else the finished video's. Read
    in the creator's reply language (call under `_worker_reply_language`)."""
    en, tr = _REASONS.get(reason, _REASONS["story_not_compilable"])
    label_en, label_tr = _DIRECTION_LABEL.get(direction, (direction, direction))
    if future:
        return say(
            en=f"I'll cut this as a fast montage instead of a {label_en}: {en}",
            tr=f"Bunu {label_tr} yerine hızlı montaj olarak keseceğim: {tr}",
        )
    return say(
        en=f"I cut this as a fast montage instead of a {label_en}: {en}.",
        tr=f"Bunu {label_tr} yerine hızlı montaj olarak kestim: {tr}.",
    )


def error_fields(exc: BaseException) -> dict[str, str]:
    """What a story-direction fallback may log about ``exc``: its type, and a guided-story code.

    Never the message: a validation error's text carries the creator's story lines and captions.
    """
    from app.pipeline.guided_story import GuidedStoryError  # noqa: PLC0415

    fields = {"error_type": type(exc).__name__}
    if isinstance(exc, GuidedStoryError):
        fields["error_code"] = exc.code
    return fields


def story_lines(strategy: Mapping[str, Any]) -> list[str]:
    """The planner's chapter outline: non-empty, de-duplicated, whitespace-normalised."""
    raw = strategy.get("story_structure")
    if not isinstance(raw, (list, tuple)):
        return []
    lines: list[str] = []
    for value in raw:
        text = " ".join(str(value or "").split())
        if text and text not in lines:
            lines.append(text)
    return lines[:MAX_STORY_LINES]


def story_blocker(strategy: Mapping[str, Any], view: BriefView) -> str | None:
    """A reason the creator's ASKS keep this montage a fast montage, else None.

    Pure over the strategy and the brief view, so approval-time planning (which holds clips
    longer only when a story will be built) and the render agree. Each reason is an ask only
    the fast-cut layout can express; a story layout would silently lose it.
    """
    from app.pipeline.hidden_parts import hidden_parts_from_strategy  # noqa: PLC0415

    if (
        view.wants_per_clip_text
        or view.clip_literals
        or strategy.get("shot_labels")
        or any(
            isinstance(row, Mapping) and row.get("op") in {"caption", "label"}
            for row in strategy.get("resolved_clip_intents") or []
        )
    ):
        return "per_clip_text"
    if view.freezes or view.speeds:
        return "freeze_speed"
    if hidden_parts_from_strategy(strategy):
        return "hidden_parts"
    if (
        view.hold_requirements
        or strategy.get("mixed_media_timing")
        or strategy.get("montage_cadence")
        or strategy.get("video_reuse_policy") == "allow_repeat"
    ):
        return "photo_timing"
    if view.curved_title or view.curved_labels or view.curved_texts:
        return "curved_text"
    return None


def story_media_blocker(strategy: Mapping[str, Any], media: Mapping[str, Any] | None) -> str | None:
    """A reason the APPROVED length plan keeps this montage a fast montage, else None.

    Read before approval from the plan the draft pins (``media["length_plan"]``): a window held
    shorter than the direction's minimum (a stated length the holds do not fit; the plan keeps
    the plain windows then) means the render will cut a fast montage. Unknown (no plan) is None.
    """
    from app.services.length_policy import story_hold_floor_s  # noqa: PLC0415

    floor = story_hold_floor_s(strategy)
    plan = (media or {}).get("length_plan")
    if floor is None or not isinstance(plan, Mapping) or plan.get("window_eligible") is not True:
        return None
    sources = [row for row in plan.get("sources") or [] if isinstance(row, Mapping)]
    capacity = {str(row.get("media_id")): row.get("duration_s") for row in sources}
    stills = {str(row.get("media_id")) for row in sources if row.get("kind") == "image"}
    for window in plan.get("windows") or []:
        # A photo keeps its own hold; the story floor is a video-clip rule.
        if not isinstance(window, Mapping) or str(window.get("media_id")) in stills:
            continue
        held = float(window.get("source_end_s", 0)) - float(window.get("source_start_s", 0))
        source = capacity.get(str(window.get("media_id")))
        limit = min(floor, float(source)) if isinstance(source, (int, float)) else floor
        if held < limit - 0.05:
            return "holds_do_not_fit"
    return None


def crossfade_blocker(strategy: Mapping[str, Any], direction: str) -> str | None:
    """``crossfade_unavailable`` when the direction's boundary is a crossfade this deployment's
    phones are not verified to draw, else None (`build_story_plan` applies the same rule)."""
    from app.config import settings  # noqa: PLC0415
    from app.pipeline.guided_story import guided_transition_params  # noqa: PLC0415
    from app.services.length_policy import stated_pace  # noqa: PLC0415

    transition, _seconds = guided_transition_params(
        direction, stated_pace(strategy) or "balanced", None
    )
    if transition == "crossfade" and "crossfade" not in settings.phone_render_verified_features:
        return "crossfade_unavailable"
    return None


def story_floor_for(strategy: Mapping[str, Any], view: BriefView) -> float | None:
    """The minimum hold a story direction gives each clip where a story will be built, else None.

    One rule for approval-time planning and the render: a story direction, nothing the creator
    asked that only the fast-cut layout carries, and a phone that draws its crossfade. A plan
    that will be cut as a fast montage anyway is not given longer holds.
    """
    from app.services.length_policy import story_direction, story_hold_floor_s  # noqa: PLC0415

    direction = story_direction(strategy)
    if (
        direction is None
        or story_blocker(strategy, view) is not None
        or crossfade_blocker(strategy, direction) is not None
    ):
        return None
    return story_hold_floor_s(strategy)


def _plan_blocker(
    plan: UnifiedMontagePlan,
    approved_length_plan: Mapping[str, Any] | None,
    *,
    song: bool,
) -> str | None:
    """A reason the PLAN the route already made keeps this montage a fast montage."""
    snapshot = plan.snapshot
    if song or snapshot.user_song is not None:
        return "creator_song"
    if snapshot.clip_labels:
        return "per_clip_text"
    if plan.freeze_frames or plan.clip_speeds:
        return "freeze_speed"
    if plan.hidden_parts:
        return "hidden_parts"
    if plan.asset_holds or snapshot.creator_still_holds or snapshot.mixed_media_timing:
        return "photo_timing"
    if snapshot.title_bend is not None or any(row.get("bend") for row in plan.text_shapes):
        return "curved_text"
    if (
        plan.spoken_roles
        or plan.opening_speech
        or plan.closing_quiet
        or plan.closing_speech
        or plan.named_lines
    ):
        return "spoken_windows"
    if isinstance(approved_length_plan, Mapping) and approved_length_plan.get("moments"):
        return "named_moments"
    return None


def _beat_sizes(clips: int, beats: int) -> list[int]:
    base, extra = divmod(clips, beats)
    return [base + (1 if index < extra else 0) for index in range(beats)]


def _seed(line: str) -> str:
    """The chapter line as placeholder caption text, cut at a word boundary when long."""
    if len(line) <= _SEED_CHARS:
        return line
    cut = line[:_SEED_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return cut or line[:_SEED_CHARS]


def story_beats(
    media_ids: list[str], durations: list[float], lines: list[str]
) -> tuple[list[StoryBeat] | None, str | None]:
    """Story beats over ``media_ids`` in order, or ``(None, reason)``.

    Enough beats to hold four clips each and at least one per outline line; the clips are
    shared out evenly in order and the outline is spread over the beats in order (a line a
    beat or two long when there are more beats than lines). Each beat carries its line as
    placeholder text (`STORY_SEED_PREFIX` ids), so every beat is a window the text step can
    caption and the plan always has text; the captions it writes replace the placeholders.
    """
    if len(set(media_ids)) != len(media_ids):
        return None, "repeated_footage"
    if not lines:
        return None, "no_story_lines"
    count = max(-(-len(media_ids) // MAX_MEDIA_PER_BEAT), min(len(lines), len(media_ids)))
    if count > MAX_BEATS:
        return None, "too_many_beats"
    beats: list[StoryBeat] = []
    cursor = 0
    for index, size in enumerate(_beat_sizes(len(media_ids), count)):
        line = lines[index * len(lines) // count]
        seconds = sum(durations[cursor : cursor + size])
        beats.append(
            StoryBeat(
                beat_id=f"{STORY_BEAT_PREFIX}{index + 1}",
                topic=line[:80],
                thought=_seed(line),
                thought_source="ai_draft",
                media_ids=media_ids[cursor : cursor + size],
                layout="fullscreen",
                duration_s=max(1.0, min(MAX_PROPOSAL_DURATION_S, seconds)),
            )
        )
        cursor += size
    return beats, None


# A moment that crossfades into the next one is rendered its overlap longer than it is held
# (`guided_story` adds the transition to the moment's source window); the planned window is
# widened by this much, where the clip has it, so the compiler still finds room in it.
_WINDOW_SLACK_S = 0.4


def _planned_window_first(
    ref: dict[str, Any], start_s: float, end_s: float, source_s: float | None
) -> None:
    """Put the plan's own window first among the clip's best moments, in place.

    The guided compiler places a moment on the first best moment with room for it
    (`guided_story._source_window`); the plan already chose the strongest window per clip
    (approved length plan or best parts), so the story shows the same footage.
    """
    if source_s:
        # Slack after the window where the clip has it; the rest from just before it.
        wanted_s = end_s - start_s + _WINDOW_SLACK_S
        end_s = min(end_s + _WINDOW_SLACK_S, source_s)
        start_s = max(0.0, min(start_s, end_s - wanted_s))
    analysis = dict(ref.get("analysis") or {})
    moments = [m for m in analysis.get("best_moments") or [] if isinstance(m, dict)]
    inside = next(
        (
            m
            for m in moments
            if m.get("start_s", 0) <= start_s + 1e-6 and m.get("end_s", 0) >= end_s - 1e-6
        ),
        None,
    )
    pinned = {
        "start_s": start_s,
        "end_s": end_s,
        "energy": (inside or {}).get("energy", 5.0),
        "description": (inside or {}).get("description", ""),
    }
    analysis["best_moments"] = [pinned, *moments]
    ref["analysis"] = analysis


def build_story_plan(
    fast: UnifiedMontagePlan,
    *,
    strategy: Mapping[str, Any],
    view: BriefView,
    direction: str,
    stated_pace: str | None,
    approved_length_plan: Mapping[str, Any] | None,
    song: bool,
) -> tuple[UnifiedMontagePlan | None, str | None]:
    """``(story plan, None)``, or ``(None, reason)`` to keep ``fast``. Never raises."""
    try:
        return _build_story_plan(
            fast,
            strategy=strategy,
            view=view,
            direction=direction,
            stated_pace=stated_pace,
            approved_length_plan=approved_length_plan,
            song=song,
        )
    except Exception as exc:  # noqa: BLE001 - a direction is never a reason to fail a render
        log.warning("story_direction_build_failed", direction=direction, **error_fields(exc))
        return None, "story_not_compilable"


def _build_story_plan(
    fast: UnifiedMontagePlan,
    *,
    strategy: Mapping[str, Any],
    view: BriefView,
    direction: str,
    stated_pace: str | None,
    approved_length_plan: Mapping[str, Any] | None,
    song: bool,
) -> tuple[UnifiedMontagePlan | None, str | None]:
    from app.pipeline.guided_story import (  # noqa: PLC0415
        GuidedStoryError,
        compile_proposal_execution_plan,
    )

    blocker = story_blocker(strategy, view) or _plan_blocker(fast, approved_length_plan, song=song)
    if blocker is not None:
        return None, blocker
    cuts = list(fast.snapshot.fast_cuts or [])
    beats, reason = story_beats(
        [cut.media_id for cut in cuts],
        [cut.output_duration_s for cut in cuts],
        story_lines(strategy),
    )
    if beats is None:
        return None, reason
    data = fast.snapshot.model_dump(mode="json")
    data.pop("fast_cuts", None)
    data.pop("clip_labels", None)
    window_of = {cut.media_id: cut for cut in cuts}
    for ref in data["media"]:
        cut = window_of.get(ref["media_id"])
        if cut is not None and ref.get("kind") == "video":
            _planned_window_first(ref, cut.source_start_s, cut.source_end_s, ref.get("duration_s"))
    data["direction"] = direction
    data["pace"] = stated_pace or "balanced"
    # The direction's own boundary is a crossfade; a phone that cannot draw one gets the fast
    # montage (hard cuts), never a render that asks it for something it lacks.
    if (unavailable := crossfade_blocker({"pacing": data["pace"]}, direction)) is not None:
        return None, unavailable
    data["story_beats"] = [beat.model_dump(mode="json") for beat in beats]
    story = EditProposalSnapshot.model_validate(data)
    try:
        compile_proposal_execution_plan(story)
    except GuidedStoryError as exc:
        return None, (
            "holds_do_not_fit"
            if exc.code == "guided_story_duration_impossible"
            else "story_not_compilable"
        )
    return (
        replace(
            fast,
            snapshot=story,
            requested_direction=None,
            story_beats=len(beats),
            story_fallback=fast,
        ),
        None,
    )
