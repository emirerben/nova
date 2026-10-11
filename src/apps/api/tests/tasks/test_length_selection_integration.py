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


@pytest.mark.parametrize("eligible", [False, True])
def test_approved_envelope_does_not_replace_an_authored_timeline(monkeypatch, eligible):
    import app.pipeline.agents.gemini_analyzer as analyzer

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *a, **kw: None)
    monkeypatch.setattr(gb, "_build_no_music_recipe", lambda *a, **kw: {"slots": []})
    monkeypatch.setattr(
        analyzer,
        "build_recipe",
        lambda _: SimpleNamespace(beat_timestamps_s=[], color_grade="none"),
    )
    monkeypatch.setattr(
        gb,
        "_approved_best_parts_decisions",
        lambda *_args, **_kwargs: pytest.fail("non-window plan must not override the timeline"),
    )

    decision = gb._decide_generative_variant(
        job_id="test",
        rank=1,
        spec={"variant_id": "clean", "text_mode": "none", "track": None},
        clip_metas=[],
        clip_id_to_local={"a": "/a"},
        clip_id_to_gcs={"a": "source.mp4"},
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
        approved_length_plan={"window_eligible": eligible, "windows": []},
    )

    step = decision.assembly_steps[0]
    assert (step.in_s, step.out_s, step.target_duration_s) == (4, 6, 2)


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


def test_render_wrapper_does_not_forward_selection_only_envelope_to_processing(
    monkeypatch: pytest.MonkeyPatch,
):
    seen = {}
    decision = SimpleNamespace(base={}, variant_t0=0.0)
    envelope = {"window_eligible": True}

    monkeypatch.setattr(gb, "_decide_generative_variant", lambda **_kwargs: decision)

    def process(_decision, **kwargs):
        seen.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(gb, "_process_generative_variant", process)

    assert gb._render_generative_variant(
        job_id="job-1",
        spec={"variant_id": "clean"},
        approved_length_plan=envelope,
        approved_runtime_generations={"source.mp4": "7"},
    ) == {"ok": True}
    assert seen["job_id"] == "job-1"
    assert "approved_length_plan" not in seen
    assert "approved_runtime_generations" not in seen


# Value: protects=validated hero dominance and creator timing through real decision matching;
# fails_when=a late best-parts rewrite replaces validated slots or an envelope ignores hero policy;
# why_new=uses real recipe/matcher plus both approved and legacy decision boundaries;
# seam=none; no_claim=does not measure a rendered video or live analysis quality.
@pytest.mark.parametrize("approved", [False, True])
def test_single_hero_exact_selection_preserves_composition(monkeypatch, tmp_path, approved):
    from app.agents._schemas.edit_format import SINGLE_HERO_RENDERER_VERSION
    from app.pipeline.agents.gemini_analyzer import ClipMeta
    from app.services.length_planning import build_length_plan

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *a, **kw: None)
    metas = [
        ClipMeta("hero", "", "", 9, [{"start_s": 4, "end_s": 5, "energy": 9}]),
        ClipMeta("support", "", "", 5, [{"start_s": 1, "end_s": 11, "energy": 5}]),
    ]
    envelope = (
        build_length_plan(
            {"edit_format": "single_hero", "audio_strategy": "original_audio"},
            {
                "clip_assignments": [
                    {
                        "media_id": meta.clip_id,
                        "gcs_path": meta.clip_id,
                        "generation": "1",
                        "duration_s": 20,
                        "analysis": {
                            "hook_score": meta.hook_score,
                            "best_moments": meta.best_moments,
                        },
                    }
                    for meta in metas
                ]
            },
            creator_target_s=20,
        )
        if approved
        else None
    )
    decision = gb._decide_generative_variant(
        job_id="hero-length",
        rank=1,
        spec={
            "variant_id": "hero",
            "text_mode": "none",
            "track": None,
            "archetype": "single_hero",
            "single_hero_renderer_version": SINGLE_HERO_RENDERER_VERSION,
        },
        clip_metas=metas,
        clip_id_to_local={"hero": "hero", "support": "support"},
        clip_id_to_gcs={"hero": "hero", "support": "support"},
        probe_map={name: SimpleNamespace(duration_s=20) for name in ("hero", "support")},
        available_footage_s=40,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
        creator_target_duration_s=20,
        strict_single_hero=True,
        approved_length_plan=envelope,
        approved_runtime_generations={"hero": "1", "support": "1"},
    )
    assert decision.duration_s == pytest.approx(20)
    assert [step.clip_id for step in decision.assembly_steps] == ["hero", "support"]
    assert decision.assembly_steps[0].target_duration_s / decision.duration_s >= 0.60
    if approved:
        assert decision.extras["best_parts"] == envelope["windows"]
    else:
        assert decision.extras["best_parts"] == []
