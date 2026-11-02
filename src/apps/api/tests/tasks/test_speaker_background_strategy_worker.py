"""KRI-1117 (chat): the phone Talking worker resolves the strategy's ``background_media``.

Same Cafe T2 harness as the lane-request tests (`test_speaker_background_worker.py`), with the
background named by the approved creator strategy instead of a hand-written lane request.
Failure modes pinned here: the strategy's background never reaches the recipe; the same photo is
ALSO placed as a mid-talk card (reservation); a closed phone gate leaves a different recipe than
a strategy that never asked; an id outside this item's ready pool is bound anyway; a hand-written
lane request is overridden by the agent.

KRI-1162 (commentary-style green screen) adds, each through the same Cafe T2 chain:
a sequence entry outside the ready pool is bound anyway (S35); an unheard phrase fails the job
or vanishes without a receipt (S38); the sequence stops at the end of a longer take or leaves a
shorter one dark (S39); a window photo is also dealt out as a card (S43); a speech cut leaves the
windows on the wrong sentence (S29); a closed windows gate changes more than the windows.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.services.client_protocol import clear_request_context
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    BITE,
    COTOPAXI,
    OBLEA,
    _assets_in_tracks,
    _cafe_job,
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _lane_request


def _job(monkeypatch, *, background, enabled=True, verified=True, **kwargs):
    clear_request_context()
    monkeypatch.setattr(settings, "speaker_background_enabled", enabled)
    kwargs.setdefault("beats", False)
    kwargs.setdefault("overlay_display", "fullscreen_images")
    job = _cafe_job(monkeypatch, **kwargs)
    features = [f for f in settings.phone_render_verified_features if f != "speakerBackground"]
    if verified:
        features.append("speakerBackground")
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", features)
    if background is not None:
        job.all_candidates["creator_strategy"]["background_media"] = background
    return job


def _card_media(job) -> set[str]:
    receipt = job.assembly_plan["variants"][0].get("phone_overlay_receipt") or {}
    return {entry["media_id"] for entry in receipt.get("placed", [])}


def test_the_strategys_background_reaches_the_recipe_and_the_receipts(monkeypatch):
    job = _job(monkeypatch, background={"media_id": f"asset-{COTOPAXI}"})
    gb._run_generative_job(str(job.id))

    layer = _recipe(job).speaker_background
    assert layer is not None and layer.asset_id == f"visual-{COTOPAXI}"
    receipt = job.assembly_plan["variants"][0]["phone_lane_receipt"]
    assert "background" in receipt["applied"]
    assert receipt["dropped"] == []


def test_a_video_background_keeps_the_start_the_strategy_named(monkeypatch):
    job = _job(monkeypatch, background={"media_id": f"asset-{BITE}", "source_start_s": 1.5})
    gb._run_generative_job(str(job.id))

    layer = _recipe(job).speaker_background
    assert (layer.media_kind, layer.source_start) == ("video", 1.5)


def test_the_background_photo_is_never_also_a_mid_talk_card(monkeypatch):
    baseline = _job(monkeypatch, background=None)
    gb._run_generative_job(str(baseline.id))
    assert COTOPAXI in _card_media(baseline), "the harness must offer the photo as a card first"

    job = _job(monkeypatch, background={"media_id": f"asset-{COTOPAXI}"})
    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_overlay_receipt"]
    offered = {entry["media_id"] for entry in receipt["placed"] + receipt["unplaced"]}
    assert COTOPAXI not in offered
    recipe = _recipe(job)
    cards = {
        clip.source_asset_id
        for track in recipe.tracks
        if track.id != "subtitled"
        for clip in track.clips
    }
    assert f"visual-{COTOPAXI}" not in cards
    assert recipe.speaker_background is not None
    # The other pool photos still take their cards.
    assert OBLEA in _card_media(job) or f"visual-{OBLEA}" in _assets_in_tracks(recipe)


@pytest.mark.parametrize("closed", ["flag", "token"])
def test_a_closed_gate_renders_exactly_what_a_strategy_that_never_asked_renders(
    monkeypatch, closed
):
    asked = _job(
        monkeypatch,
        background={"media_id": f"asset-{OBLEA}"},
        enabled=closed != "flag",
        verified=closed != "token",
    )
    gb._run_generative_job(str(asked.id))
    never = _job(monkeypatch, background=None, enabled=closed != "flag", verified=closed != "token")
    gb._run_generative_job(str(never.id))

    assert _recipe(asked).speaker_background is None
    assert _recipe(asked).model_dump_json() == _recipe(never).model_dump_json()
    assert _card_media(asked) == _card_media(never)


def test_an_id_outside_this_items_ready_pool_is_dropped_with_a_receipt(monkeypatch):
    foreign = "asset-00000000-0000-4000-8000-0000000000ff"
    job = _job(monkeypatch, background={"media_id": foreign})
    gb._run_generative_job(str(job.id))

    assert _recipe(job).speaker_background is None
    receipt = job.assembly_plan["variants"][0]["phone_lane_receipt"]
    assert {"lane": "background", "reason": "not_in_ready_pool"} in receipt["dropped"]
    assert job.status == "awaiting_device"


def test_a_hand_written_lane_request_background_wins_over_the_strategys(monkeypatch):
    from tests.tasks.test_speaker_background_worker import _background

    job = _job(
        monkeypatch,
        background={"media_id": f"asset-{OBLEA}"},
        lane_request={**_lane_request(), "background": _background(COTOPAXI, "image")},
    )
    gb._run_generative_job(str(job.id))

    layer = _recipe(job).speaker_background
    assert layer is not None and layer.asset_id == f"visual-{COTOPAXI}"
    dropped = job.assembly_plan["variants"][0]["phone_lane_receipt"]["dropped"]
    assert {"lane": "background", "reason": "lane_request_background_wins"} in dropped


# ------------------------------------------------------------------ KRI-1162: several backgrounds

TAKE_S = 24.6  # Caro talks 24.6 s; the cadence windows are laid out over exactly that.


def _windows_job(monkeypatch, sequence, *, windows_verified=True, **extra):
    background = {
        "media_id": f"asset-{COTOPAXI}",
        "sequence": [
            {"media_id": entry} if isinstance(entry, str) else entry for entry in sequence
        ],
        **extra,
    }
    job = _job(monkeypatch, background=background)
    features = [
        f for f in gb.settings.phone_render_verified_features if f != "speakerBackgroundWindows"
    ]
    if windows_verified:
        features.append("speakerBackgroundWindows")
    monkeypatch.setattr(gb.settings, "phone_render_verified_features", features)
    return job


def _windows(job):
    layer = _recipe(job).speaker_background
    return [(w.asset_id, round(w.start, 3), round(w.end, 3)) for w in layer.windows or []]


def _dropped(job):
    return job.assembly_plan["variants"][0]["phone_lane_receipt"]["dropped"]


def test_sequence_entries_outside_the_pool_are_dropped_with_receipts(monkeypatch):
    foreign = "asset-00000000-0000-4000-8000-0000000000ff"
    job = _windows_job(monkeypatch, [f"asset-{COTOPAXI}", foreign, f"asset-{OBLEA}"])
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    shown = {asset for asset, _s, _e in _windows(job)}
    assert shown == {f"visual-{COTOPAXI}", f"visual-{OBLEA}"}
    assert {"lane": "background", "window": 1, "reason": "not_in_ready_pool"} in _dropped(job)
    assert "background" in job.assembly_plan["variants"][0]["phone_lane_receipt"]["applied"]


def test_unheard_phrase_falls_back_to_cadence_with_a_note(monkeypatch):
    job = _windows_job(
        monkeypatch,
        [
            {"media_id": f"asset-{COTOPAXI}", "spoken_phrase": "unicorn parade"},
            {"media_id": f"asset-{OBLEA}"},
        ],
        cadence_s=6.0,
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    windows = _windows(job)
    # Laid out as unanchored: from the start, alternating, 6 s each.
    assert windows[0] == (f"visual-{COTOPAXI}", 0.0, 6.0)
    assert windows[1] == (f"visual-{OBLEA}", 6.0, 12.0)
    assert {"lane": "background", "window": 0, "reason": "phrase_not_heard"} in _dropped(job)


def test_an_anchored_phrase_starts_its_window_on_the_spoken_word(monkeypatch):
    job = _windows_job(
        monkeypatch,
        [
            {"media_id": f"asset-{OBLEA}", "spoken_phrase": "oblea de la casa", "hold_s": 5.0},
        ],
    )
    gb._run_generative_job(str(job.id))

    # "And this is the Oblea de la Casa." is 8 tokens spread over 2.2-5.2 s; "Oblea" is the 5th.
    ((asset, start, end),) = _windows(job)
    assert asset == f"visual-{OBLEA}"
    assert start == pytest.approx(2.2 + 4 * (3.0 / 8), abs=0.01)
    assert end - start == pytest.approx(5.0, abs=0.01)
    assert not [d for d in _dropped(job) if d.get("reason") == "phrase_not_heard"]


def test_sequence_cycles_to_the_take_end(monkeypatch):
    job = _windows_job(monkeypatch, [f"asset-{COTOPAXI}", f"asset-{OBLEA}"], cadence_s=4.0)
    gb._run_generative_job(str(job.id))

    windows = _windows(job)
    assert [asset for asset, _s, _e in windows] == [
        f"visual-{COTOPAXI}",
        f"visual-{OBLEA}",
    ] * 3
    assert windows[0][1] == 0.0
    assert windows[-1][2] == pytest.approx(TAKE_S, abs=1e-3)
    assert all(
        later[1] == pytest.approx(earlier[2], abs=1e-3)
        for earlier, later in zip(windows, windows[1:], strict=False)
    )


def test_windows_past_the_take_end_are_clipped(monkeypatch):
    job = _windows_job(
        monkeypatch,
        [{"media_id": f"asset-{OBLEA}", "spoken_phrase": "that's it", "hold_s": 30.0}],
    )
    gb._run_generative_job(str(job.id))

    ((asset, start, end),) = _windows(job)
    assert asset == f"visual-{OBLEA}"
    assert start == pytest.approx(23.7, abs=0.05)
    assert end == pytest.approx(TAKE_S, abs=1e-3)
    assert end <= _recipe(job).duration + 1e-6


def test_window_ids_are_reserved_from_cards(monkeypatch):
    baseline = _job(monkeypatch, background=None)
    gb._run_generative_job(str(baseline.id))
    assert {COTOPAXI, OBLEA} <= _card_media(baseline), "the harness must offer both as cards first"

    job = _windows_job(monkeypatch, [f"asset-{COTOPAXI}", f"asset-{OBLEA}"])
    gb._run_generative_job(str(job.id))

    receipt = job.assembly_plan["variants"][0]["phone_overlay_receipt"]
    offered = {entry["media_id"] for entry in receipt["placed"] + receipt["unplaced"]}
    assert not offered & {COTOPAXI, OBLEA}
    recipe = _recipe(job)
    in_cards = {
        clip.source_asset_id
        for track in recipe.tracks
        if track.id != "subtitled"
        for clip in track.clips
    }
    assert not in_cards & {f"visual-{COTOPAXI}", f"visual-{OBLEA}"}


def test_window_visuals_are_bound_like_the_fill(monkeypatch):
    # A window photo that is not the fill still needs its own pinned asset in the recipe (S35).
    job = _windows_job(monkeypatch, [f"asset-{OBLEA}"], speaker_frame="small_bottom_right")
    gb._run_generative_job(str(job.id))

    layer = _recipe(job).speaker_background
    assert layer.asset_id == f"visual-{COTOPAXI}"
    assert {w.asset_id for w in layer.windows} == {f"visual-{OBLEA}"}
    assert {asset.id for asset in _recipe(job).assets} >= {
        f"visual-{COTOPAXI}",
        f"visual-{OBLEA}",
    }
    assert (layer.speaker_frame.width_fraction, layer.speaker_frame.y_fraction) == (0.42, 0.8)


def test_a_closed_windows_gate_renders_exactly_the_single_background_ask(monkeypatch):
    asked = _windows_job(
        monkeypatch,
        [f"asset-{OBLEA}", f"asset-{COTOPAXI}"],
        windows_verified=False,
        speaker_frame="small_bottom_right",
    )
    gb._run_generative_job(str(asked.id))
    single = _job(monkeypatch, background={"media_id": f"asset-{COTOPAXI}"})
    gb._run_generative_job(str(single.id))

    layer = _recipe(asked).speaker_background
    assert layer is not None and layer.windows is None and layer.speaker_frame is None
    assert _recipe(asked).model_dump_json() == _recipe(single).model_dump_json()
    # Only the fill is reserved: the window photo may take its card as it would have.
    assert _card_media(asked) == _card_media(single)


def test_a_speech_cut_moves_the_windows_with_the_words_they_sit_on(monkeypatch):
    """S29: windows are laid out on the KEPT take, so the recipe's last window ends where the
    cut recipe ends, and every window keeps at least its minimum."""
    from tests.tasks.test_phone_subtitled_narrated_dispatch import (
        DURATION,
        _raw_preflight_snapshot,
        _setup_subtitled,
    )

    job, snapshot, _session, binding = _setup_subtitled(monkeypatch, duration_s=DURATION)
    snapshot["speech_cleanup_contract"] = "required_v1"
    snapshot["_speech_cleanup_internal"] = {
        "preflight_snapshot": _raw_preflight_snapshot(storage_path=binding.proxy_path)
    }
    import app.services.phone_overlay_grounding as ogm
    import app.services.phone_reaction_grounding as rgm
    import app.services.phone_visuals as phone_visuals_mod
    from tests.tasks.test_kri841_talking_ending_clip import _bind, _pool_rows
    from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats

    _enable_beats(monkeypatch, extra_features=("visualVideos", "authoredText"))
    monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", _bind)
    monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    monkeypatch.setattr(rgm, "_load_ready_pool_assets", lambda *a, **k: _pool_rows())
    clear_request_context()
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [
            *gb.settings.phone_render_verified_features,
            "speakerBackground",
            "speakerBackgroundWindows",
        ],
    )
    job.all_candidates["creator_strategy"] = {
        "edit_format": "subtitled",
        "audio_strategy": "original_audio",
        "background_media": {
            "media_id": f"asset-{COTOPAXI}",
            "sequence": [{"media_id": f"asset-{OBLEA}"}, {"media_id": f"asset-{COTOPAXI}"}],
            "cadence_s": 2.0,
        },
    }
    gb._run_phone_subtitled_job(str(job.id), snapshot, job.all_candidates, ownership_epoch=3)

    recipe = _recipe(job)
    windows = _windows(job)
    assert windows, "the sequence must survive the cut"
    assert recipe.duration == pytest.approx(DURATION - (0.54 + 1.9), abs=1e-3)
    assert windows[0][1] == pytest.approx(0.0, abs=1e-3)
    assert windows[-1][2] == pytest.approx(recipe.duration, abs=1e-3)
    assert all(end - start >= 0.5 - 1e-6 for _a, start, end in windows)
    assert all(
        later[1] >= earlier[2] - 1e-6 for earlier, later in zip(windows, windows[1:], strict=False)
    )


def test_a_window_across_a_removal_remaps_to_the_span_laid_out(monkeypatch):
    """The layout is in output time and written back in original time; the lane's own remap
    (`remap_range`) must give the span back exactly, including across the removal's edges."""
    from app.pipeline.silence_cut import CutPlan, Removal, remap_range
    from app.services import phone_background_sequence as seq

    keep = [(0.0, 5.0), (7.0, 12.0)]
    plan = CutPlan(keep_segments=keep, removed=[Removal(5.0, 7.0, "silence")], time_saved_s=2.0)
    layout = seq.lay_out(
        [seq.SequenceEntry(0), seq.SequenceEntry(1)],
        cadence_s=4.0,
        take_s=seq.kept_duration(keep, 12.0),
        words=None,
        keep_segments=keep,
    )
    assert [(w.index, w.start_s, w.end_s) for w in layout.windows] == [
        (0, 0.0, 4.0),
        (1, 4.0, 8.0),
        (0, 8.0, 10.0),
    ]
    for window in layout.windows:
        source_start = seq.to_source_time(window.start_s, keep)
        source_end = seq.to_source_time(window.end_s, keep, as_end=True)
        assert remap_range(source_start, source_end, plan) == pytest.approx(
            (window.start_s, window.end_s)
        )
