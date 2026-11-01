"""The phone montage's clip-sound lane: build the creator's on/off asks, then judge them (KRI-942).

`_run_phone_guided_job` (every phone montage that renders through the guided compiler, the
unified montage included) calls `plan_toggle_lane` after its first compile, like the timed-cards
lane: the asks are resolved on that recipe's clock (`app.pipeline.clip_sound_toggle`), written
onto the plan's moments (`source_audio_muted` for a clip silent end to end,
`source_audio_mute_spans` for part of one), and the plan is compiled again. The lane offers its
candidates from the most to the least faithful; the worker pins the first that compiles, passes
the pilot check and keeps the pinned contract's camera-audio promise, so a sound ask never costs
the creator their video:

1. the asks as resolved (part-of-a-clip spans only when
   `phone_rollout.phone_clip_sound_windows_supported()`; otherwise those clips keep their sound);
2. without the part-of-a-clip spans;
3. the base alone (the strategy's named sources), when it has one;
4. nothing: the plan as it was.

The receipt record (``variant["clip_sound_toggles"]``) is `judge_recipe` over the pinned recipe
plus the reasons a candidate step down implies. `rendered_toggle_receipt` re-judges a finished
render against whatever recipe is pinned then (an editor Save included), so the brief receipt
never trusts this lane's own claim.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from app.kria.clip_sound_toggle_ask import (
    SOUND_OFF,
    SoundTarget,
    SoundToggleAsk,
    brief_sound_toggle_asks,
)
from app.pipeline.clip_sound_toggle import (
    CONTRACT_CONFLICT,
    PARTIAL_UNSUPPORTED,
    RENDER_REFUSED,
    SoundPlan,
    _media_match,
    judge_recipe,
    resolve_sound_toggles,
    toggle_clips_from_recipe,
)

TOGGLE_RECEIPT_FIELD = "clip_sound_toggles"
STORY_TRACK_ID = "story"


@dataclass(frozen=True)
class ToggleCandidate:
    raw_plan: dict[str, Any]
    build: SoundPlan  # what this candidate plays
    reasons: dict[str, str]  # why an ask is not (fully) built by it
    windows: bool


@dataclass(frozen=True)
class ToggleLane:
    asks: tuple[SoundToggleAsk, ...]
    intent: SoundPlan  # what the creator asked for: every candidate is judged against it
    candidates: tuple[ToggleCandidate, ...]


def _binding_brief(snapshot: Mapping[str, Any]) -> tuple[Any, dict]:
    """``(brief, media_snapshot)`` pinned on the job, or ``(None, {})`` (logged, never fatal)."""
    from app.kria.brief_binding import BriefBinding  # noqa: PLC0415

    raw = snapshot.get("creator_brief_binding")
    if not raw:
        return None, {}
    try:
        binding = BriefBinding.model_validate(raw)
        return binding.resolve(), dict(binding.media_snapshot or {})
    except Exception:  # noqa: BLE001 - an unreadable binding is "no ask"
        import structlog  # noqa: PLC0415

        structlog.get_logger().warning("clip_sound_toggle.binding_unreadable")
        return None, {}


def _ground(media_snapshot: Mapping[str, Any], bindings: Sequence[Any]):  # noqa: ANN202
    from app.pipeline.phone_clip_sound import ground_trigger_line  # noqa: PLC0415

    durations = {b.media_id: b.original.duration_s for b in bindings}

    def ground(lines: tuple[str, ...], media_id: str):  # noqa: ANN202
        return ground_trigger_line(
            lines, media_id, media_snapshot, duration_s=durations.get(media_id)
        )

    return ground


def _with_kept_lines(
    asks: Sequence[SoundToggleAsk], media_snapshot: Mapping[str, Any]
) -> tuple[SoundToggleAsk, ...]:
    """``asks`` with each clip kept audible for a named line carrying that sentence (KRI-1028).

    The approved length plan's ``kept_lines`` (`kept_sound_lines.ground_kept_lines`) say where
    the sentence is; the part that keeps the clip is then judged on holding it, not only on
    playing the clip's sound.
    """
    plan = media_snapshot.get("length_plan") if isinstance(media_snapshot, Mapping) else None
    rows = [
        row
        for row in (plan.get("kept_lines") if isinstance(plan, Mapping) else None) or []
        if isinstance(row, Mapping)
        and isinstance(row.get("start_s"), (int, float))
        and isinstance(row.get("end_s"), (int, float))
    ]
    if not rows:
        return tuple(asks)

    def kept(ask: SoundToggleAsk, part: SoundTarget) -> SoundTarget:
        if part.media_ref is None or part.lines or part.has_window:
            return part
        row = next(
            (
                row
                for row in rows
                if row.get("requirement_id") == ask.requirement_id
                and _media_match(part.media_ref, [str(row.get("media_id"))]) is not None
            ),
            None,
        )
        if row is None:
            return part
        return replace(
            part,
            kept_line=str(row.get("line") or "") or None,
            kept_span=(float(row["start_s"]), float(row["end_s"])),
        )

    return tuple(
        replace(ask, exceptions=tuple(kept(ask, part) for part in ask.exceptions))
        if ask.state == SOUND_OFF
        else replace(ask, target=kept(ask, ask.target))
        for ask in asks
    )


def _camera_assets(bindings: Sequence[Any]) -> tuple[set[str], set[str]]:
    """``(every footage asset id, the ones whose file has no sound)``."""
    cameras = {b.render_asset().id for b in bindings}
    silent = {b.render_asset().id for b in bindings if b.original.has_audio is False}
    return cameras, silent


def _base(strategy: object, plan: Any, media_ids: set[str]) -> tuple[bool, frozenset[str] | None]:
    """The sound every clip starts with: the plan's own choice, narrowed to the strategy's
    named sources (`montage_audio.source_media_ids`) when it names any clip in this edit."""
    from app.pipeline.guided_story import plan_preserves_source_audio  # noqa: PLC0415

    base_on = plan_preserves_source_audio(plan)
    audio = strategy.get("montage_audio") if isinstance(strategy, Mapping) else None
    if not base_on or not isinstance(audio, Mapping) or not audio.get("preserve_source_audio"):
        return base_on, None
    named = frozenset(str(m) for m in audio.get("source_media_ids") or [] if m) & media_ids
    return base_on, named or None


def apply_sound_plan(raw_plan: Mapping[str, Any], sound: SoundPlan) -> dict[str, Any]:
    """``raw_plan`` with each moment's sound as ``sound`` decides (a fresh copy)."""
    from app.pipeline.clip_sound_toggle import PARTIAL  # noqa: PLC0415

    out = copy.deepcopy(dict(raw_plan))
    known = {clip.clip_id for clip in sound.clips}
    for moment in out.get("story_timeline") or []:
        moment_id = moment.get("moment_id")
        moment.pop("source_audio_mute_spans", None)
        if moment_id not in known:
            continue
        kind, _spans = sound.decision(moment_id)
        if kind == "off":
            moment["source_audio_muted"] = True
        elif kind == PARTIAL:
            moment["source_audio_mute_spans"] = [
                list(s) for s in sound.mute_spans_source(moment_id)
            ]
    from app.pipeline.guided_story import plan_preserves_source_audio  # noqa: PLC0415

    keeps = plan_preserves_source_audio(out)
    if sound.any_audible and not keeps and out.get("user_song") is None:
        out["montage_audio"] = {**(out.get("montage_audio") or {}), "preserve_source_audio": True}
    elif not sound.any_audible and keeps:
        out["montage_audio"] = {**(out.get("montage_audio") or {}), "preserve_source_audio": False}
    return out


def plan_toggle_lane(
    snapshot: Mapping[str, Any],
    *,
    raw_plan: Mapping[str, Any],
    plan: Any,
    recipe: Any,
    bindings: Sequence[Any],
    strategy: object,
    windows_supported: bool,
) -> ToggleLane | None:
    """The lane's candidates for this compiled recipe, or None when nothing would change.

    None (the job pins exactly what it would have before): no on/off ask in the approved brief
    and no named sources in the strategy, or a soundtrack that replaces the camera sound (the
    creator's song, a recorded voiceover); the receipt judges those asks from the recipe.
    """
    if plan.user_song is not None or plan.narration is not None:
        return None
    brief, media_snapshot = _binding_brief(snapshot)
    asks = _with_kept_lines(brief_sound_toggle_asks(brief), media_snapshot)
    asset_to_media = {b.render_asset().id: b.media_id for b in bindings}
    clips = toggle_clips_from_recipe(
        recipe, video_track_id=STORY_TRACK_ID, asset_to_media=asset_to_media
    )
    if not clips:
        return None
    base_on, base_ids = _base(strategy, plan, {c.media_id for c in clips})
    if not asks and base_ids is None:
        return None
    already_muted = frozenset(
        m.moment_id for m in plan.story_timeline if getattr(m, "source_audio_muted", None)
    )
    ground = _ground(media_snapshot, bindings)

    def resolve(with_asks: Sequence[SoundToggleAsk]) -> SoundPlan:
        return resolve_sound_toggles(
            with_asks,
            clips,
            duration_s=float(recipe.duration),
            ground=ground,
            base_on_media_ids=base_ids,
            base_on=base_on,
            muted_clip_ids=already_muted,
        )

    full = resolve(asks)
    every = {ask.requirement_id: RENDER_REFUSED for ask in asks}
    candidates: list[ToggleCandidate] = []
    partial = full.partial_clip_ids
    if partial and windows_supported:
        candidates.append(ToggleCandidate(apply_sound_plan(raw_plan, full), full, {}, True))
    whole = full.without_partial()
    whole_reasons = {
        req: PARTIAL_UNSUPPORTED if not windows_supported else RENDER_REFUSED
        for req in full.owners_of(partial)
    }
    candidates.append(
        ToggleCandidate(apply_sound_plan(raw_plan, whole), whole, whole_reasons, False)
    )
    if base_ids is not None and asks:
        alone = resolve(())
        candidates.append(ToggleCandidate(apply_sound_plan(raw_plan, alone), alone, every, False))
    candidates.append(ToggleCandidate(copy.deepcopy(dict(raw_plan)), full, every, False))
    return ToggleLane(asks=asks, intent=full, candidates=tuple(candidates))


def sound_contract_conflict(contract: Any, recipe: Any, bindings: Sequence[Any]) -> bool:
    """True when ``recipe``'s audible camera sources break the pinned contract's promise
    (`original_audio` require/forbid, `audio_source_ids`), the check the pin runs."""
    if contract is None:
        return False
    from app.services.creator_render_contract import (  # noqa: PLC0415
        AudioChoiceError,
        verify_phone_recipe,
    )

    source_audio = {b.media_id: b.original.has_audio is not False for b in bindings}
    try:
        verify_phone_recipe(contract, recipe, source_audio=source_audio)
    except AudioChoiceError:
        return True
    except Exception:  # noqa: BLE001 - any other refusal is the pin's to report, not ours
        return False
    return False


def toggle_record(
    lane: ToggleLane,
    candidate: ToggleCandidate,
    recipe: Any,
    bindings: Sequence[Any],
    *,
    extra_reasons: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The variant's receipt record: the creator's asks judged against the pinned recipe."""
    cameras, silent = _camera_assets(bindings)
    reasons = {**candidate.reasons, **(extra_reasons or {})}
    record = judge_recipe(
        lane.intent, recipe, camera_assets=cameras, silent_assets=silent, reasons=reasons
    )
    record["windows"] = candidate.windows
    if candidate.build.degraded_clip_ids:
        record["degraded_clip_ids"] = sorted(candidate.build.degraded_clip_ids)
    return record


def conflict_reasons(lane: ToggleLane) -> dict[str, str]:
    return {ask.requirement_id: CONTRACT_CONFLICT for ask in lane.asks}


def rendered_toggle_receipt(
    assembly: Mapping[str, Any],
    variant: Mapping[str, Any],
    recipe: Any,
    *,
    brief_binding: Any = None,
) -> dict[str, Any] | None:
    """The on/off asks judged against the recipe pinned for ``variant`` now, or None.

    Re-resolved on that recipe's own clips (an editor Save that moved or dropped a mute is
    judged as it is), with the worker's recorded reasons for what it could not build.
    ``brief_binding`` is the approved binding the review reads (default: the job's own).
    """
    from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding  # noqa: PLC0415

    brief, media_snapshot = _binding_brief(
        {"creator_brief_binding": brief_binding} if brief_binding is not None else assembly
    )
    asks = _with_kept_lines(brief_sound_toggle_asks(brief), media_snapshot)
    if not asks or recipe is None:
        return None
    try:
        bindings = tuple(
            PhoneSourceBinding.model_validate(row)
            for row in assembly.get(PHONE_SOURCES_FIELD) or []
        )
    except Exception:  # noqa: BLE001 - no trustworthy sources, nothing to judge
        return None
    if not bindings:
        return None
    from app.pipeline.phone_talking_cutaway_sound import rendered_cutaway_receipt  # noqa: PLC0415

    talking = rendered_cutaway_receipt(
        asks,
        recipe,
        silent_media_ids=[b.media_id for b in bindings if b.original.has_audio is False],
    )
    if talking is not None:
        return talking
    asset_to_media = {b.render_asset().id: b.media_id for b in bindings}
    track_id = next(
        (t.id for t in recipe.tracks if t.kind == "video" and t.id == STORY_TRACK_ID),
        next((t.id for t in recipe.tracks if t.kind == "video"), None),
    )
    if track_id is None:
        return None
    clips = toggle_clips_from_recipe(recipe, video_track_id=track_id, asset_to_media=asset_to_media)
    if not clips:
        return None
    plan = resolve_sound_toggles(
        asks,
        clips,
        duration_s=float(recipe.duration),
        ground=_ground(media_snapshot, bindings),
    )
    recorded = variant.get(TOGGLE_RECEIPT_FIELD)
    reasons = {
        str(row.get("requirement_id")): str(row.get("reason_code"))
        for row in (recorded.get("rows") if isinstance(recorded, Mapping) else None) or []
        if isinstance(row, Mapping) and row.get("reason_code")
    }
    cameras, silent = _camera_assets(bindings)
    return judge_recipe(plan, recipe, camera_assets=cameras, silent_assets=silent, reasons=reasons)


__all__ = [
    "STORY_TRACK_ID",
    "TOGGLE_RECEIPT_FIELD",
    "ToggleCandidate",
    "ToggleLane",
    "apply_sound_plan",
    "conflict_reasons",
    "plan_toggle_lane",
    "rendered_toggle_receipt",
    "sound_contract_conflict",
    "toggle_record",
]
