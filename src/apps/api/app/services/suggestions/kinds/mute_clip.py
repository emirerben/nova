"""``mute_clip``: mute one clip whose own sound is wind or crowd noise.

The action is a typed ``BriefUpdate`` (``clip_sound: off`` scoped ``clip:<media_id>``, the shape
``kria/clip_sound_toggle_ask.py`` reads); there is no new editor op and no protocol bump.

**Execution is not proven, so the kind never offers** (``PINNED_TAP_APPLIES_CLIP_SOUND``).
A clip-sound fact only changes a render where the phone montage writer re-plans from the brief
(``_run_phone_guided_job`` -> ``plan_toggle_lane``). A pinned tap runs the editor-ops route:
``prepare_editor_commit`` recompiles the saved plan and only re-judges the receipt, so the clip
keeps its sound; cloud renders never read the fact (``brief_checks`` says it works on a montage
made on the iPhone). ``tests/evals/test_suggestion_kind_mute.py`` pins both halves: the re-plan
applies the fact, the editor commit does not. Flip the constant only with a route that re-plans.

Offered for a clip when all of these hold (each is a way a "mute" card goes wrong):

* the ``audio_quality`` signal measured it ``noisy`` (never ``silent``, never unmeasured);
* it is a video with no speech and no talking to camera: muting it would silence the point;
* it is in the current edit, and muting it leaves something audible (a music track or another
  clip);
* a montage edit whose footage sound the creator can control (``audio_notes`` says otherwise for
  guided phone edits, which are exactly where this would run);
* no live brief ask already turns that clip's sound on or off (KRI-129: the creator's request wins,
  and a second mute is not a new change).

Brief inputs come from the loader: ``kind_inputs["mute_clip"] = loader_input(brief)``. Without
them no action can bind to a brief version, so the kind fails closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.services.suggestion_chip_copy import render_suggestion_copy
from app.services.suggestions.builders import PHONE
from app.services.suggestions.context import SuggestionContext, Target, slots, video_clips
from app.services.suggestions.models import (
    Action,
    BriefUpdateAction,
    SuggestionKind,
    WitnessResult,
)

if TYPE_CHECKING:
    from app.kria.brief import CreativeBrief
    from app.kria.clip_sound_toggle_ask import SoundToggleAsk

# The brief and sound-ask modules pull the ORM; this package stays import-light, so they load
# inside the functions that read them (as `models.BriefUpdateAction` does).
CLIP_SOUND_FACT = "clip_sound"
SOUND_OFF = "off"

KIND_NAME = "mute_clip"
INPUT_KEY = "mute_clip"
SIGNAL = "audio_quality"

# See the module docstring: proven False by the editor-commit fixture, not assumed.
PINNED_TAP_APPLIES_CLIP_SOUND = False

# The sentence `kria_editor_ops._audio_notes` adds for a guided phone edit; the fixture builds a
# real snapshot so a rewording there fails a test here instead of silently opening the kind.
_NO_FOOTAGE_SOUND_CONTROL = "footage's own sound"


@dataclass(frozen=True)
class Candidate:
    media_id: str
    ordinal: int
    # The stronger of the two noise heads, only to pick the worst clip when several qualify.
    score: float


@dataclass(frozen=True)
class Scan:
    noisy: tuple[Candidate, ...]  # measured noisy clips that are in the edit
    open: tuple[Candidate, ...]  # the ones a card may still mute, noisiest first


def loader_input(brief: CreativeBrief | None) -> dict[str, Any]:
    """What the serve loader puts under ``kind_inputs["mute_clip"]``."""
    return {
        "brief_version": brief.version if brief is not None else 0,
        "requirements": [r.model_dump(mode="json") for r in brief.live()] if brief else [],
    }


def _brief(ctx: SuggestionContext) -> CreativeBrief | None:
    from app.kria.brief import BriefRequirement, CreativeBrief  # noqa: PLC0415

    raw = ctx.kind_inputs.get(INPUT_KEY)
    version = raw.get("brief_version") if isinstance(raw, Mapping) else None
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        return None
    try:
        return CreativeBrief(
            version=version,
            requirements=[
                BriefRequirement.model_validate(r) for r in raw.get("requirements") or []
            ],
        )
    except (ValidationError, TypeError, ValueError):
        return None


def _footage_sound_controllable(ctx: SuggestionContext) -> bool:
    notes = (ctx.snapshot or {}).get("audio_notes")
    return not any(isinstance(n, str) and _NO_FOOTAGE_SOUND_CONTROL in n for n in notes or [])


def _touches(ask: SoundToggleAsk, media_id: str, edit_media: Sequence[str]) -> bool:
    """Could ``ask`` (on or off) cover this clip? Only an ask that names a different clip, and
    nothing else, is known to leave it alone; a position, a window or a line is not resolvable
    here, so it counts."""
    from app.pipeline.clip_sound_toggle import _media_match  # noqa: PLC0415

    target = ask.target
    if target.clip_position or target.has_window or target.lines or ask.exceptions:
        return True
    if target.media_ref is None:
        return True
    return _media_match(target.media_ref, edit_media) in (media_id, None)


def _signal(ctx: SuggestionContext, media_id: str) -> Mapping[str, Any] | None:
    row = ctx.signals.get(media_id)
    data = row.get(SIGNAL) if isinstance(row, Mapping) else None
    return data if isinstance(data, Mapping) else None


def _measured_noisy(data: Mapping[str, Any] | None) -> float | None:
    """The noise score of a clip the signal measured noisy, else None."""
    if data is None or data.get("measured") is False:
        return None
    if data.get("noisy") is not True or data.get("silent") is True:
        return None
    scores = [
        float(data[k])
        for k in ("wind_score", "crowd_score")
        if isinstance(data.get(k), (int, float)) and not isinstance(data.get(k), bool)
    ]
    return max(scores) if scores else 0.0


def _edit_media(ctx: SuggestionContext) -> list[str]:
    return [str(s["media_id"]) for s in slots(ctx) if isinstance(s.get("media_id"), str)]


def _scan(ctx: SuggestionContext) -> Scan:
    if (
        not PINNED_TAP_APPLIES_CLIP_SOUND
        or ctx.state != "ready"
        or ctx.edit_format != "montage"
        or not _footage_sound_controllable(ctx)
    ):
        return Scan((), ())
    brief = _brief(ctx)
    if brief is None:
        return Scan((), ())
    from app.kria.clip_sound_toggle_ask import sound_toggle_asks  # noqa: PLC0415

    asks = sound_toggle_asks(brief.live())
    edit_media = _edit_media(ctx)
    music = (ctx.snapshot or {}).get("music")
    has_music = isinstance(music, Mapping) and music.get("current_track_id") is not None
    noisy: list[Candidate] = []
    for clip in video_clips(ctx):
        media_id, ordinal = clip.get("media_id"), clip.get("ordinal")
        if (
            not isinstance(media_id, str)
            or not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or media_id not in edit_media
            or clip.get("has_speech")
            or clip.get("to_camera")
        ):
            continue
        score = _measured_noisy(_signal(ctx, media_id))
        if score is not None:
            noisy.append(Candidate(media_id, ordinal, score))
    open_: list[Candidate] = []
    for cand in noisy:
        if any(_touches(a, cand.media_id, edit_media) for a in asks):
            continue
        if not (has_music or any(m != cand.media_id for m in edit_media)):
            continue  # the card would leave a silent video
        open_.append(cand)
    open_.sort(key=lambda c: (-c.score, c.ordinal))
    return Scan(tuple(noisy), tuple(open_))


def candidate_targets(ctx: SuggestionContext) -> list[Target]:
    """The clips a card may name, noisiest first (the deterministic fallback has no agent to
    name one)."""
    return [
        Target(key=f"clip:{c.media_id}", media_id=c.media_id, clip_ordinal=c.ordinal)
        for c in _scan(ctx).open
    ]


def _available(ctx: SuggestionContext) -> bool:
    return bool(_scan(ctx).noisy)


def _already_present(ctx: SuggestionContext) -> bool:
    scan = _scan(ctx)
    return bool(scan.noisy) and not scan.open


def _build(ctx: SuggestionContext, targets: Sequence[Target], copy: str | None) -> Action | None:
    """One clip per card: exactly one named target, and it must still be a candidate. The card's
    title, prompt and anchor are built from the targets the pick named, so a pick naming several
    clips (or one that is not a candidate: a speaking clip, a clip not in the edit) builds nothing
    rather than a card that says one thing and mutes another."""
    from app.kria.brief import BriefUpdate  # noqa: PLC0415

    brief = _brief(ctx)
    named = {t.media_id for t in targets if t.media_id}
    if brief is None or len(named) != 1:
        return None
    cand = next((c for c in _scan(ctx).open if c.media_id in named), None)
    if cand is None:
        return None
    rendered = render_suggestion_copy(KIND_NAME, ctx.language, [cand.ordinal])
    if rendered is None:
        return None
    try:
        update = BriefUpdate(
            operation="add",
            kind="audio",
            scope=f"clip:{cand.media_id}",
            description=rendered[1],
            facts={CLIP_SOUND_FACT: SOUND_OFF},
        )
        return BriefUpdateAction(
            updates=[update.model_dump(mode="json", exclude_none=True)],
            expected_version=brief.version,
        )
    except (ValidationError, ValueError):
        return None  # e.g. a media id outside the brief's scope grammar


def _witness(action: Action, ctx: SuggestionContext) -> WitnessResult:
    """Prove the update through the brief engine and the sound-ask reader, against the live
    candidates: it must apply to the current brief and read back as exactly one "off" ask for a
    clip a card may still mute."""

    from app.kria.brief import BriefUpdate, BriefUpdateBatchError, apply_updates  # noqa: PLC0415
    from app.kria.clip_sound_toggle_ask import requirement_sound_toggle  # noqa: PLC0415
    from app.pipeline.clip_sound_toggle import _media_match  # noqa: PLC0415

    def refuse(reason: str) -> WitnessResult:
        return WitnessResult(ok=False, tier="brief", reason=reason)

    if not isinstance(action, BriefUpdateAction):
        return refuse("not_brief_update")
    brief = _brief(ctx)
    if brief is None or action.expected_version != brief.version:
        return refuse("stale_brief")
    if len(action.updates) != 1:
        return refuse("not_one_update")
    update = BriefUpdate.model_validate(action.updates[0])
    scope = update.scope or ""
    if (
        update.operation != "add"
        or update.kind != "audio"
        or update.facts != {CLIP_SOUND_FACT: SOUND_OFF}
        or not scope.startswith("clip:")
    ):
        return refuse("not_a_clip_mute")
    media_id = scope.removeprefix("clip:")
    if media_id not in {c.media_id for c in _scan(ctx).open}:
        return refuse("not_a_candidate")
    try:
        merged = apply_updates(brief, [update], source_turn_id=None)
    except (BriefUpdateBatchError, ValidationError, ValueError):
        return refuse("update_refused")
    ask = requirement_sound_toggle(merged.requirements[-1])
    if (
        ask is None
        or ask.state != SOUND_OFF
        or ask.exceptions
        or ask.target.media_ref is None
        or _media_match(ask.target.media_ref, _edit_media(ctx)) != media_id
    ):
        return refuse("not_read_back")
    return WitnessResult(ok=True, tier="brief", touched=frozenset({f"clip_sound:{media_id}"}))


KIND = SuggestionKind(
    kind=KIND_NAME,
    states=frozenset({"ready"}),
    required_signals=(SIGNAL,),
    availability=_available,
    already_present=_already_present,
    build_action=_build,
    witness=_witness,
    surfaces=PHONE,
    flag="kria_suggestion_mute_clip_enabled",
)

__all__ = [
    "KIND",
    "PINNED_TAP_APPLIES_CLIP_SOUND",
    "Candidate",
    "Scan",
    "candidate_targets",
    "loader_input",
]
