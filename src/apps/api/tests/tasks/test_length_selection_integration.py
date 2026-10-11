"""Duration selection at the worker boundary, including authored timeline edits."""

from types import SimpleNamespace

import pytest

from app.pipeline.generative_decision import GenerativeAssemblyStepDecision
from app.tasks import generative_build as gb
from tests.tasks.test_user_song_montage_job import harness as _song_harness


@pytest.fixture
def song_harness(monkeypatch):
    return _song_harness.__wrapped__(monkeypatch)


def test_best_parts_projects_pydantic_decisions_without_losing_step_fields():
    step = GenerativeAssemblyStepDecision(
        clip_id="a",
        in_s=0,
        out_s=2,
        target_duration_s=2,
        source_path="/a",
        transition_in="cut",
        slot_extra={"position": 1},
    )
    rewritten, receipt, conflicts = gb._best_parts_decision_windows(
        [step],
        clip_metas=[
            SimpleNamespace(
                clip_id="a",
                best_moments=[{"start_s": 3, "end_s": 7, "energy": 9}],
            )
        ],
        clip_id_to_local={"a": "/a"},
        probe_map={"/a": SimpleNamespace(duration_s=10)},
    )
    assert not conflicts
    assert len(receipt) == 1
    selected = rewritten[0]
    assert (selected.in_s, selected.out_s, selected.target_duration_s) == (3, 7, 4)
    assert selected.exact_window
    assert selected.slot_extra == {"position": 1}
    assert selected.source_path == "/a"
    assert (step.in_s, step.out_s) == (0, 2)


@pytest.mark.parametrize("has_analysis", [False, True])
def test_authored_timeline_windows_survive_decision_selection(monkeypatch, has_analysis):
    import app.pipeline.agents.gemini_analyzer as analyzer

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *a, **kw: None)
    monkeypatch.setattr(gb, "_build_no_music_recipe", lambda *a, **kw: {"slots": []})
    monkeypatch.setattr(
        analyzer,
        "build_recipe",
        lambda _: SimpleNamespace(beat_timestamps_s=[], color_grade="none"),
    )
    metas = (
        [
            SimpleNamespace(
                clip_id="a",
                best_moments=[{"start_s": 0, "end_s": 8, "energy": 9}],
            )
        ]
        if has_analysis
        else []
    )
    decision = gb._decide_generative_variant(
        job_id="test",
        rank=1,
        spec={"variant_id": "clean", "text_mode": "none", "track": None},
        clip_metas=metas,
        clip_id_to_local={"a": "/a"},
        clip_id_to_gcs={},
        probe_map={"/a": SimpleNamespace(duration_s=10)},
        available_footage_s=10,
        agent_text=None,
        agent_form={},
        variant_dir="/tmp",
        assembly_steps_override=[
            SimpleNamespace(
                clip_id="a",
                slot={"target_duration_s": 2, "exact_window": True},
                moment={"start_s": 4, "end_s": 6},
            )
        ],
    )
    step = decision.assembly_steps[0]
    assert (step.in_s, step.out_s, step.target_duration_s) == (4, 6, 2)
    assert step.exact_window
    assert decision.extras["best_parts"] == []


def test_song_worker_reaches_background_planner_with_supported_arguments(song_harness):
    job, snapshot, _ = song_harness(sync="background")
    planned = gb._run_phone_unified_montage_job(
        str(job.id),
        snapshot,
        job.all_candidates,
        ownership_epoch=3,
    )
    assert planned is not None
    assert planned["unified_montage"]["user_song"]
