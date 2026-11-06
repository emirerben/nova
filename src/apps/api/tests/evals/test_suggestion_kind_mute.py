"""``mute_clip`` suggestion kind: measured noise in, a proven BriefUpdate out, and what a tap
actually changes in a render.

Free, offline: the signal is the real ``audio_quality`` measurement over the committed calibration
clips (ffmpeg, no network, no model); the execution proofs run the real phone montage writer
(``tests/tasks/test_kri942_clip_sound_toggle.py``'s harness) and the real editor commit.

Ways this can fail, each pinned below (written before the kind):

1. A clip that is not measured noisy gets a card: unmeasured, silent, stale, bass-heavy music or
   clean speech read as wind (``test_the_signal_*``, ``test_only_a_measured_noisy_clip_*``).
2. The card mutes the clip's speech: a clip with speech or talking to camera, a photo.
3. The card does nothing or breaks the video: a clip that is not in the edit, a removed slot, a
   mute that leaves a silent video.
4. The card is offered where it cannot run: a speech/Talking format, a first-prompt thread, a guided
   phone edit with no footage-sound control (``audio_notes``).
5. The card undoes or repeats what the creator said (KRI-129): a live keep-sound ask, a second mute.
6. The agent re-points the action: a named clip that is not a candidate, no named clip, a media id
   the brief cannot scope.
7. A stale or tampered action is applied: the brief moved since the card was built, the update was
   edited to turn sound ON, a non-brief action.
8. **The tap does nothing.** A phone editor-ops re-render and a cloud render never read a new
   ``clip_sound`` fact; only a fresh phone montage plan does. ``test_execution_*`` pins both, and
   ties the dark ``PINNED_TAP_APPLIES_CLIP_SOUND`` constant to what the editor commit really does.
9. The kind ships on a measurement that is not good enough: ``test_the_calibration_gate_*``.
10. Flag off is not byte-identical: the kind is absent from ``kinds_for`` by default.
"""

from __future__ import annotations

import copy
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.kria.brief import BriefRequirement, BriefUpdate, CreativeBrief, apply_updates
from app.kria.brief_binding import BriefBinding
from app.kria.clip_sound_toggle_ask import SOUND_OFF
from app.kria.narrated_clip_sound_ask import CLIP_SOUND_FACT
from app.services.clip_signals import audio_quality
from app.services.clip_signals.registry import SignalInputs
from app.services.suggestions import picks, registry
from app.services.suggestions.context import NO_TARGET, SuggestionContext, Target, snapshot_digest
from app.services.suggestions.kinds import mute_clip
from app.services.suggestions.models import (
    BriefUpdateAction,
    EditorOpsAction,
    WitnessResult,
)

AUDIO = Path(__file__).resolve().parents[1] / "fixtures" / "clip_signals" / "audio"
needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")

WIND, CLEAN, MUSIC, QUIET = "m-wind", "m-clean", "m-music", "m-quiet"
NOISY_FILE = {"wind": "wind_00.m4a", "crowd": "crowd_00.m4a"}


def _measure(name: str) -> dict[str, Any]:
    """The real ``audio_quality`` record of a committed calibration clip."""
    record = audio_quality.compute(
        SignalInputs(media_id=name, kind="video", local_path=str(AUDIO / name))
    )
    assert record is not None, name
    return dict(record)


@pytest.fixture(scope="module")
def measured() -> dict[str, dict[str, Any]]:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg required")
    return {
        "wind": _measure("wind_00.m4a"),
        "crowd": _measure("crowd_00.m4a"),
        "clean": _measure("clean_00.m4a"),
        "music": _measure("music_00.m4a"),
        "silence": _measure("silence_00.m4a"),
    }


def _clip(media_id: str, ordinal: int, **extra: Any) -> dict[str, Any]:
    return {
        "media_id": media_id,
        "ordinal": ordinal,
        "kind": "video",
        "has_speech": False,
        "to_camera": False,
        "_empty": False,
        "_incomplete": False,
        "duration_s": 8.0,
        **extra,
    }


def _snapshot(media_ids: list[str], *, music: bool = False, **extra: Any) -> dict[str, Any]:
    return {
        "allowed_op_families": ["music", "clip"],
        "slots": [
            {"key": f"slot-{i}", "media_id": m, "removed": False} for i, m in enumerate(media_ids)
        ],
        "music": {"current_track_id": "t1" if music else None},
        **extra,
    }


def _brief(*extra: BriefRequirement, version: int = 4) -> CreativeBrief:
    return CreativeBrief(version=version, requirements=list(extra))


def _ctx(measured: dict[str, dict[str, Any]], **over: Any) -> SuggestionContext:
    """A ready montage of four clips: wind (c1), clean speech-free (c2), bass music (c3), a quiet
    one (c4). Only c1 is measured noisy."""
    clips = [_clip(WIND, 1), _clip(CLEAN, 2), _clip(MUSIC, 3), _clip(QUIET, 4)]
    signals = {
        WIND: {"audio_quality": measured["wind"]},
        CLEAN: {"audio_quality": measured["clean"]},
        MUSIC: {"audio_quality": measured["music"]},
        QUIET: {"audio_quality": measured["silence"]},
    }
    base: dict[str, Any] = {
        "thread_id": "t",
        "state": "ready",
        "language": "en",
        "edit_format": "montage",
        "clips": clips,
        "signals": signals,
        "snapshot": _snapshot([WIND, CLEAN, MUSIC, QUIET]),
        "kind_inputs": {mute_clip.INPUT_KEY: mute_clip.loader_input(_brief())},
    }
    base.update(over)
    return SuggestionContext(**base)


@pytest.fixture()
def proven(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the tap's route applies the fact, to test the kind's own rules."""
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)


def _open_ordinals(ctx: SuggestionContext) -> list[int | None]:
    return [t.clip_ordinal for t in mute_clip.candidate_targets(ctx)]


def _off(media_id: str, rid: str = "r1", **facts: Any) -> BriefRequirement:
    return BriefRequirement(
        id=rid,
        kind="audio",
        scope=f"clip:{media_id}",
        description="mute",
        facts={CLIP_SOUND_FACT: "off", **facts},
    )


# -- 1. the signal it reads --------------------------------------------------------------


@needs_ffmpeg
def test_the_signal_separates_noise_from_what_must_not_be_muted(measured) -> None:
    # The contract this kind leans on, from the real measurement of the real clips.
    assert measured["wind"]["noisy"] is True
    assert measured["crowd"]["noisy"] is True
    assert measured["clean"]["noisy"] is False
    assert measured["music"]["noisy"] is False  # bass-heavy music is not wind
    assert measured["silence"]["silent"] is True and measured["silence"]["noisy"] is False


@needs_ffmpeg
def test_only_a_measured_noisy_clip_is_offered(measured, proven) -> None:
    assert _open_ordinals(_ctx(measured)) == [1]
    crowd_ctx = _ctx(
        measured, signals={**_ctx(measured).signals, CLEAN: {"audio_quality": measured["crowd"]}}
    )
    assert sorted(o for o in _open_ordinals(crowd_ctx) if o) == [1, 2]


@needs_ffmpeg
@pytest.mark.parametrize(
    "record",
    [
        None,
        {"measured": False, "reason": "no_usable_audio"},
        {"noisy": True, "silent": True},
        {"noisy": "yes"},
        {"noisy": False, "wind_score": 0.99},
    ],
)
def test_an_unmeasured_or_unconvincing_record_offers_nothing(measured, proven, record) -> None:
    signals = {WIND: {"audio_quality": record} if record is not None else {}}
    assert _open_ordinals(_ctx(measured, signals=signals)) == []


# -- 2. speech is the point --------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize("flag", ["has_speech", "to_camera"])
def test_a_clip_kept_for_its_speech_is_never_muted(measured, proven, flag) -> None:
    clips = [_clip(WIND, 1, **{flag: True}), _clip(CLEAN, 2)]
    ctx = _ctx(measured, clips=clips, snapshot=_snapshot([WIND, CLEAN]))
    assert _open_ordinals(ctx) == []
    assert not mute_clip.KIND.availability(ctx)


@needs_ffmpeg
@pytest.mark.parametrize("flag", ["_empty", "_incomplete"])
def test_a_clip_without_a_complete_understanding_is_never_muted(measured, proven, flag) -> None:
    # An unanalysed clip reads has_speech=False; a voice in wind is still "noisy".
    clips = [_clip(WIND, 1, **{flag: True}), _clip(CLEAN, 2)]
    ctx = _ctx(measured, clips=clips, snapshot=_snapshot([WIND, CLEAN]))
    assert _open_ordinals(ctx) == []
    assert not mute_clip.KIND.availability(ctx)


@needs_ffmpeg
def test_a_photo_is_never_muted(measured, proven) -> None:
    ctx = _ctx(measured, clips=[_clip(WIND, 1, kind="image"), _clip(CLEAN, 2)])
    assert _open_ordinals(ctx) == []


# -- 3. the mute must matter and must not silence the video -------------------------------


@needs_ffmpeg
def test_a_clip_outside_the_edit_is_not_offered(measured, proven) -> None:
    out = _ctx(measured, snapshot=_snapshot([CLEAN, MUSIC]))
    assert _open_ordinals(out) == []
    removed = _snapshot([WIND, CLEAN])
    removed["slots"][0]["removed"] = True
    assert _open_ordinals(_ctx(measured, snapshot=removed)) == []


@needs_ffmpeg
def test_a_mute_that_leaves_a_silent_video_is_refused(measured, proven) -> None:
    only = _ctx(measured, clips=[_clip(WIND, 1)], snapshot=_snapshot([WIND]))
    assert _open_ordinals(only) == []
    with_music = _ctx(measured, clips=[_clip(WIND, 1)], snapshot=_snapshot([WIND], music=True))
    assert _open_ordinals(with_music) == [1]


# -- 4. where it can run -----------------------------------------------------------------


@needs_ffmpeg
@pytest.mark.parametrize(
    "over",
    [
        {"state": "first_prompt"},
        {"state": "none"},
        {"edit_format": "subtitled"},
        {"edit_format": "talking_head"},
        {"edit_format": None},
        {"snapshot": None},
    ],
)
def test_it_is_offered_only_on_a_ready_montage(measured, proven, over) -> None:
    assert not mute_clip.KIND.availability(_ctx(measured, **over))


@needs_ffmpeg
def test_a_guided_phone_edit_without_footage_sound_control_is_refused(
    measured, proven, monkeypatch
) -> None:
    """The REAL editor snapshot of a guided phone montage carries the "no control for the
    footage's own sound" note; the kind must read it, so a rewording there fails here."""
    from app.services.kria_editor_ops import build_editor_snapshot
    from tests.routes._editor_save_matrix import guided_phone_job

    job, variant, _revision = guided_phone_job(monkeypatch)
    real = build_editor_snapshot(job, variant)
    assert any("footage's own sound" in n for n in real["audio_notes"])
    ids = [s["media_id"] for s in real["slots"]]
    ctx = _ctx(
        measured,
        clips=[_clip(ids[0], 1), _clip(ids[1], 2)],
        signals={ids[0]: {"audio_quality": measured["wind"]}},
        snapshot={**real, "music": {"current_track_id": "t1"}},
    )
    assert _open_ordinals(ctx) == []
    free = {k: v for k, v in real.items() if k != "audio_notes"}
    assert _open_ordinals(
        _ctx(
            measured,
            clips=[_clip(ids[0], 1), _clip(ids[1], 2)],
            signals={ids[0]: {"audio_quality": measured["wind"]}},
            snapshot={**free, "music": {"current_track_id": "t1"}},
        )
    ) == [1]


# -- 5. the creator's request wins -------------------------------------------------------


def _with_brief(measured, *reqs: BriefRequirement) -> SuggestionContext:
    return _ctx(measured, kind_inputs={mute_clip.INPUT_KEY: mute_clip.loader_input(_brief(*reqs))})


@needs_ffmpeg
def test_a_second_mute_of_the_same_clip_is_already_present(measured, proven) -> None:
    ctx = _with_brief(measured, _off(WIND))
    assert mute_clip.KIND.availability(ctx) and mute_clip.KIND.already_present(ctx)
    assert _open_ordinals(ctx) == []


@needs_ffmpeg
@pytest.mark.parametrize(
    "req",
    [
        BriefRequirement(id="r1", kind="audio", scope="global", facts={CLIP_SOUND_FACT: "on"}),
        BriefRequirement(
            id="r1", kind="audio", scope=f"clip:{WIND}", facts={CLIP_SOUND_FACT: "on"}
        ),
        BriefRequirement(
            id="r1",
            kind="audio",
            scope="global",
            facts={CLIP_SOUND_FACT: "off", "sound_clip": "last"},
        ),
        BriefRequirement(
            id="r1",
            kind="audio",
            scope="global",
            facts={CLIP_SOUND_FACT: "off", "sound_from_s": 1, "sound_to_s": 3},
        ),
    ],
)
def test_a_live_sound_ask_that_could_cover_the_clip_blocks_the_card(measured, proven, req) -> None:
    assert _open_ordinals(_with_brief(measured, req)) == []


@needs_ffmpeg
def test_an_ask_about_another_clip_does_not_block(measured, proven) -> None:
    assert _open_ordinals(_with_brief(measured, _off(CLEAN))) == [1]


@needs_ffmpeg
def test_a_removed_ask_no_longer_blocks(measured, proven) -> None:
    gone = _off(WIND).model_copy(update={"status": "superseded"})
    assert _open_ordinals(_with_brief(measured, gone)) == [1]


@needs_ffmpeg
@pytest.mark.parametrize(
    "inputs",
    [
        None,
        {},
        {"brief_version": True},
        {"brief_version": -1},
        {"brief_version": 2, "requirements": [{"id": 1}]},
    ],
)
def test_without_a_readable_brief_the_kind_fails_closed(measured, proven, inputs) -> None:
    kind_inputs = {} if inputs is None else {mute_clip.INPUT_KEY: inputs}
    ctx = _ctx(measured, kind_inputs=kind_inputs)
    assert not mute_clip.KIND.availability(ctx)
    assert (
        mute_clip.KIND.build_action(ctx, [Target(key="c", media_id=WIND, clip_ordinal=1)], None)
        is None
    )


# -- 6. one change per card, the agent cannot re-point it ---------------------------------


def _target(media_id: str, ordinal: int) -> Target:
    return Target(key=f"clip:{media_id}", media_id=media_id, clip_ordinal=ordinal)


@needs_ffmpeg
def test_the_action_is_one_typed_clip_sound_off_update_bound_to_the_brief_version(
    measured, proven
) -> None:
    ctx = _ctx(measured)
    action = mute_clip.KIND.build_action(ctx, [_target(WIND, 1)], None)
    assert isinstance(action, BriefUpdateAction)
    assert action.expected_version == 4
    (update,) = action.updates
    assert update["operation"] == "add" and update["kind"] == "audio"
    assert update["scope"] == f"clip:{WIND}"
    assert update["facts"] == {"clip_sound": "off"}
    # The description is the card's own closed sentence, never footage-derived text.
    assert update["description"] == "Mute the sound of clip 1"
    tr = _ctx(measured, language="tr")
    assert mute_clip.KIND.build_action(tr, [_target(WIND, 1)], None).updates[0]["description"] == (
        "1. klibin sesini kapat"
    )


@needs_ffmpeg
@pytest.mark.parametrize(
    "targets",
    [
        [],
        [NO_TARGET],
        [_target(CLEAN, 2)],  # a clip the signal never flagged
        [_target("m-ghost", 9)],  # a handle for a clip that does not exist
    ],
)
def test_a_target_that_is_not_a_candidate_builds_nothing(measured, proven, targets) -> None:
    assert mute_clip.KIND.build_action(_ctx(measured), targets, None) is None


@needs_ffmpeg
def test_a_media_id_the_brief_cannot_scope_builds_nothing(measured, proven) -> None:
    bad = "m wind!"
    ctx = _ctx(
        measured,
        clips=[_clip(bad, 1), _clip(CLEAN, 2)],
        signals={bad: {"audio_quality": measured["wind"]}},
        snapshot=_snapshot([bad, CLEAN]),
    )
    assert mute_clip.KIND.build_action(ctx, [_target(bad, 1)], None) is None


@needs_ffmpeg
def test_several_noisy_clips_give_one_card_for_the_noisiest(measured, proven) -> None:
    ctx = _ctx(
        measured,
        signals={
            WIND: {"audio_quality": {**measured["wind"], "wind_score": 0.5}},
            CLEAN: {"audio_quality": {**measured["wind"], "wind_score": 0.9}},
        },
    )
    assert _open_ordinals(ctx) == [2, 1]
    # One change per card: a pick naming both builds nothing (its title would name two clips).
    assert mute_clip.KIND.build_action(ctx, [_target(WIND, 1), _target(CLEAN, 2)], None) is None
    action = mute_clip.KIND.build_action(ctx, [_target(CLEAN, 2)], None)
    assert action.updates[0]["scope"] == f"clip:{CLEAN}"


# -- 7. the witness ----------------------------------------------------------------------


def _built(measured) -> tuple[SuggestionContext, BriefUpdateAction]:
    ctx = _ctx(measured)
    action = mute_clip.KIND.build_action(ctx, [_target(WIND, 1)], None)
    assert isinstance(action, BriefUpdateAction)
    return ctx, action


@needs_ffmpeg
def test_the_witness_proves_the_built_action_and_names_what_it_touches(measured, proven) -> None:
    ctx, action = _built(measured)
    proof = mute_clip.KIND.witness(action, ctx)
    assert proof == WitnessResult(ok=True, tier="brief", touched=frozenset({f"clip_sound:{WIND}"}))


@needs_ffmpeg
def test_the_witness_refuses_a_stale_brief(measured, proven) -> None:
    _ctx0, action = _built(measured)
    moved = _ctx(
        measured, kind_inputs={mute_clip.INPUT_KEY: mute_clip.loader_input(_brief(version=5))}
    )
    assert mute_clip.KIND.witness(action, moved).reason == "stale_brief"


@needs_ffmpeg
def test_the_witness_refuses_a_clip_that_stopped_qualifying(measured, proven) -> None:
    _ctx0, action = _built(measured)
    quiet = _ctx(measured, signals={WIND: {"audio_quality": measured["clean"]}})
    assert mute_clip.KIND.witness(action, quiet).reason == "not_a_candidate"


@needs_ffmpeg
def test_the_witness_refuses_a_tampered_update(measured, proven) -> None:
    ctx, action = _built(measured)
    turned_on = BriefUpdateAction.model_construct(
        type="brief_update",
        expected_version=4,
        updates=[{**action.updates[0], "facts": {"clip_sound": "on"}}],
    )
    assert mute_clip.KIND.witness(turned_on, ctx).reason == "not_a_clip_mute"
    whole = BriefUpdateAction.model_construct(
        type="brief_update",
        expected_version=4,
        updates=[{**action.updates[0], "scope": "global"}],
    )
    assert mute_clip.KIND.witness(whole, ctx).reason == "not_a_clip_mute"
    two = BriefUpdateAction.model_construct(
        type="brief_update", expected_version=4, updates=[action.updates[0]] * 2
    )
    assert mute_clip.KIND.witness(two, ctx).reason == "not_one_update"


@needs_ffmpeg
def test_the_witness_refuses_another_action_type(measured, proven) -> None:
    ctx, _action = _built(measured)
    ops = EditorOpsAction(
        ops=[{"op": "set_mix", "music_level": 0.2}],
        base_digest=snapshot_digest(ctx.snapshot) or "x",
    )
    assert mute_clip.KIND.witness(ops, ctx).reason == "not_brief_update"


# -- G10-style: through the real card builder, whatever the agent names -------------------


def _copy_for(kind: str, targets, _copy):
    from app.services.suggestion_chip_copy import render_suggestion_copy

    ordinals = [t.clip_ordinal for t in targets if t.clip_ordinal is not None]
    rendered = render_suggestion_copy(kind, "en", ordinals)
    return None if rendered is None else (*rendered, [f"Clip {n}" for n in ordinals[:2]])


@needs_ffmpeg
def test_a_card_reaches_the_phone_only_for_a_proven_clip(measured, proven, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.config.settings.kria_suggestion_mute_clip_enabled", True, raising=False
    )
    monkeypatch.setattr("app.config.settings.kria_suggestion_signals_enabled", True, raising=False)
    ctx = _ctx(measured)
    issued = picks.issue_kinds(ctx, "ready")
    assert [i.kind.kind for i in issued] == ["mute_clip"]
    for named, expect in [
        (("c1",), 1),
        (("c2",), 0),
        (("c9",), 0),
        ((), 0),
        (("c3", "c1"), 0),
        (("c1", "c1"), 1),
    ]:
        cards, drops = picks.build_suggestions(
            ctx,
            issued,
            [picks.Pick(kind="mute_clip", target_ids=named)],
            base_digest="d",
            copy_for=_copy_for,
        )
        assert len(cards) == expect, (named, drops)
        if cards:
            (card,) = cards
            assert card.action.type == "brief_update" and card.witness_tier == "brief"
            assert card.prompt == "Mute the sound of clip 1" and card.title == "Mute clip 1"
            assert card.anchor.clip_ordinal == 1


# -- 10. the flag ------------------------------------------------------------------------


def test_the_kind_is_off_by_default_and_registered_correctly() -> None:
    kind = registry.get("mute_clip")
    assert kind is mute_clip.KIND
    assert registry.problems([kind]) == []
    assert kind.surfaces == frozenset({"phone"}) and kind.min_protocol is None
    assert kind.flag == "kria_suggestion_mute_clip_enabled"
    assert all(k.kind != "mute_clip" for k in registry.kinds_for("ready", "phone"))
    assert mute_clip.PINNED_TAP_APPLIES_CLIP_SOUND is False


def test_the_local_fact_names_are_the_briefs() -> None:
    from app.kria.clip_sound_toggle_ask import SOUND_OFF as real_off

    assert mute_clip.CLIP_SOUND_FACT == CLIP_SOUND_FACT
    assert mute_clip.SOUND_OFF == real_off == SOUND_OFF


@needs_ffmpeg
def test_as_shipped_nothing_is_offered_even_for_a_noisy_clip(measured) -> None:
    ctx = _ctx(measured)
    assert not mute_clip.KIND.availability(ctx)
    assert mute_clip.candidate_targets(ctx) == []


# -- 9. the calibration gate -------------------------------------------------------------


@needs_ffmpeg
def test_the_calibration_gate_the_kind_depends_on_holds_on_the_committed_clips() -> None:
    """precision >= 0.9 of ``noisy`` against the labeled clips (wind/crowd are the positives).

    The fit set was seen during fitting, so this guards a regression only; the held-out number
    (0.94, 80 clips, synthetic) is in tests/fixtures/clip_signals/audio/CALIBRATION.md and
    ``scripts/clip_signal_calibration.py report``. Neither is real phone audio, which is why the
    flag stays off until real clips pass ``report --real-dir``.
    """
    import json

    labels = json.loads((AUDIO / "labels.json").read_text())
    tp = fp = 0
    for name, row in labels.items():
        if _measure(name).get("noisy") is True:
            if row["label"] in {"wind", "crowd"}:
                tp += 1
            else:
                fp += 1
    assert tp >= 8
    assert tp / (tp + fp) >= 0.9, (tp, fp)


# -- 8. what a tap really changes in a render --------------------------------------------


def _incident_ctx(measured, kri942) -> tuple[SuggestionContext, str]:
    """The KRI-942 Don Julio edit (real clips, real approved plan) with the butcher's clip noisy."""
    butcher = kri942.BUTCHER
    media = [m["media_id"] for m in kri942.DATA["execution_plan"]["story_timeline"]]
    ordered = list(dict.fromkeys(media))
    clips = [_clip(m, i + 1) for i, m in enumerate(ordered)]
    ctx = SuggestionContext(
        thread_id="t",
        state="ready",
        language="en",
        edit_format="montage",
        clips=clips,
        signals={butcher: {"audio_quality": measured["wind"]}},
        snapshot=_snapshot(media),
        kind_inputs={
            mute_clip.INPUT_KEY: mute_clip.loader_input(
                CreativeBrief.model_validate(
                    {
                        "version": kri942.DATA["brief"]["version"],
                        "requirements": [
                            {**row, "facts": {}} if row["id"] == "r7" else row
                            for row in kri942.DATA["brief"]["requirements"]
                        ],
                    }
                )
            )
        },
    )
    return ctx, butcher


@needs_ffmpeg
def test_execution_a_phone_montage_replan_applies_the_clip_sound_fact(
    measured, proven, monkeypatch
) -> None:
    """The brief the tap writes, run through the real phone montage writer: the noisy clip's
    cuts go silent and every other clip keeps its sound. This is the route that WORKS."""
    from tests.tasks import test_kri942_clip_sound_toggle as k

    ctx, butcher = _incident_ctx(measured, k)
    action = mute_clip.KIND.build_action(ctx, [_target(butcher, 3)], None)
    assert action is not None and mute_clip.KIND.witness(action, ctx).ok

    before = _brief_of(ctx)
    after = apply_updates(
        before, [BriefUpdate.model_validate(action.updates[0])], source_turn_id=None
    )

    base = k._volumes(k._run(monkeypatch, brief=before))
    muted = k._volumes(k._run(monkeypatch, brief=after))
    butcher_cuts = {
        m["moment_id"]
        for m in k.DATA["execution_plan"]["story_timeline"]
        if m["media_id"] == butcher
    }
    assert butcher_cuts and set(base.values()) == {1.0}  # the control: no ask, every clip audible
    assert {cut: muted[cut] for cut in butcher_cuts} == dict.fromkeys(butcher_cuts, 0.0)
    assert {cut: v for cut, v in muted.items() if cut not in butcher_cuts} == {
        cut: 1.0 for cut in muted if cut not in butcher_cuts
    }


def _brief_of(ctx: SuggestionContext) -> CreativeBrief:
    return mute_clip._brief(ctx)


@needs_ffmpeg
def test_execution_an_editor_commit_does_not_apply_the_clip_sound_fact(
    measured, monkeypatch
) -> None:
    """The route a pinned tap takes: the editor commit with the new brief bound to it. The
    recipe is recompiled from the saved plan, so the noisy clip keeps its sound and the receipt
    re-judges the ask as NOT met. That is why ``PINNED_TAP_APPLIES_CLIP_SOUND`` is False."""
    shipped = mute_clip.PINNED_TAP_APPLIES_CLIP_SOUND
    monkeypatch.setattr(mute_clip, "PINNED_TAP_APPLIES_CLIP_SOUND", True)  # to build the card
    from app.routes import generative_jobs as gj
    from app.services.device_render import device_status
    from app.services.kria_editor_ops import compile_editor_ops
    from app.services.phone_clip_sound_toggle import rendered_toggle_receipt
    from tests.routes._editor_save_matrix import guided_phone_job
    from tests.routes.test_phone_guided_timeline_commit import _shorten_ops

    job, variant, revision = guided_phone_job(monkeypatch)
    ids = [s["media_id"] for s in variant_slots(job)]
    noisy = ids[1]
    ctx = _ctx(
        measured,
        clips=[_clip(m, i + 1) for i, m in enumerate(ids)],
        signals={noisy: {"audio_quality": measured["wind"]}},
        snapshot=_snapshot(ids, music=True),
    )
    action = mute_clip.KIND.build_action(ctx, [_target(noisy, 2)], None)
    assert action is not None and mute_clip.KIND.witness(action, ctx).ok
    brief = apply_updates(
        _brief_of(ctx), [BriefUpdate.model_validate(action.updates[0])], source_turn_id=None
    )
    binding = BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, "mute-clip-eval"),
        brief,
        latest_message="Mute the sound of clip 2",
        media_snapshot={"clip_assignments": [{"media_id": m, "kind": "video"} for m in ids]},
    ).model_dump(mode="json")

    payload = compile_editor_ops(job, variant, _shorten_ops(21)).payload
    payload.guided_revision_number = revision["revision_number"]
    gj.prepare_editor_commit(
        job, "guided_story", payload, creator_brief_binding=copy.deepcopy(binding)
    )

    recipe = device_status(job, "guided_story").request.recipe
    story = next(t for t in recipe.tracks if t.kind == "video")
    gain = recipe.audio.original_volume
    volumes = {c.id: round(c.volume * gain, 3) for c in story.clips}
    assert set(volumes.values()) == {1.0}, volumes  # nothing was muted
    assert not recipe.audio.mute_windows
    judged = rendered_toggle_receipt(
        {**job.assembly_plan, "creator_brief_binding": binding},
        job.assembly_plan["variants"][0],
        recipe,
    )
    assert judged is not None and all(r["status"] != "met" for r in judged["rows"]), judged
    # The dark constant must say exactly this; flipping it needs a route that re-plans.
    assert shipped is False


def variant_slots(job) -> list[dict]:
    from app.services.kria_editor_ops import build_editor_snapshot

    variant = job.assembly_plan["variants"][0]
    return build_editor_snapshot(job, variant)["slots"]
