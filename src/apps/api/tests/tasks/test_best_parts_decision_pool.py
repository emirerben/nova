"""The decision builder selects resolved footage before matcher degradation."""

from types import SimpleNamespace

from app.tasks import generative_build as gb


def test_decision_keeps_full_resolved_pool_when_matcher_would_repeat_analyzed_clip(
    monkeypatch, tmp_path
):
    import app.pipeline.agents.gemini_analyzer as analyzer
    import app.pipeline.template_matcher as matcher

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gb,
        "_build_no_music_recipe",
        lambda *_args, **_kwargs: {"slots": [{"target_duration_s": 5, "position": 0}]},
    )
    monkeypatch.setattr(
        analyzer,
        "build_recipe",
        lambda _: SimpleNamespace(beat_timestamps_s=[], color_grade="none"),
    )
    matcher_called = []
    monkeypatch.setattr(
        matcher,
        "match",
        lambda *args, **kwargs: matcher_called.append(True),
    )

    decision = gb._decide_generative_variant(
        job_id="pool",
        rank=1,
        spec={"variant_id": "original", "text_mode": "none", "track": None},
        # b is intentionally missing analysis. It remains selected as a
        # disclosed fallback rather than disappearing after greedy matching.
        clip_metas=[
            SimpleNamespace(clip_id="a", best_moments=[{"start_s": 0, "end_s": 5, "energy": 9}])
        ],
        clip_id_to_local={"a": "/a", "b": "/b"},
        clip_id_to_gcs={},
        probe_map={"/a": SimpleNamespace(duration_s=5), "/b": SimpleNamespace(duration_s=5)},
        available_footage_s=10,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
        narrative_order=["a", "b"],
        creator_video_reuse_policy="once",
    )

    assert not matcher_called
    assert [step.clip_id for step in decision.assembly_steps] == ["a", "b"]
    assert [(step.in_s, step.out_s) for step in decision.assembly_steps] == [(0, 5), (0, 5)]
    assert [item["reason"] for item in decision.extras["best_parts"]] == [
        "best_moment",
        "analysis_fallback",
    ]


def test_voiceover_proven_target_is_the_actual_exact_window_budget(monkeypatch, tmp_path):
    import app.pipeline.agents.gemini_analyzer as analyzer
    import app.storage as storage
    import app.tasks.template_orchestrate as orchestrate

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *args, **kwargs: None)
    monkeypatch.setattr(storage, "download_to_file", lambda *args, **kwargs: None)
    monkeypatch.setattr(orchestrate, "_probe_duration", lambda _: 30)
    monkeypatch.setattr(
        gb,
        "_build_no_music_recipe",
        lambda *_args, **_kwargs: {"slots": [{"target_duration_s": 12}]},
    )
    monkeypatch.setattr(
        analyzer,
        "build_recipe",
        lambda _: SimpleNamespace(beat_timestamps_s=[], color_grade="none"),
    )

    decision = gb._decide_generative_variant(
        job_id="voice",
        rank=1,
        spec={
            "variant_id": "voiceover_music",
            "text_mode": "none",
            "track": None,
            "voiceover_gcs_path": "voice.mp3",
        },
        clip_metas=[
            SimpleNamespace(clip_id="a", best_moments=[]),
            SimpleNamespace(clip_id="b", best_moments=[]),
        ],
        clip_id_to_local={"a": "/a", "b": "/b"},
        clip_id_to_gcs={},
        probe_map={"/a": SimpleNamespace(duration_s=6), "/b": SimpleNamespace(duration_s=6)},
        available_footage_s=12,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
        creator_target_duration_s=12,
    )

    assert decision.extras["voiceover_target_s"] == 12
    assert decision.duration_s == 12
    assert all(step.exact_window for step in decision.assembly_steps)


def test_decision_discloses_three_second_expansion_of_a_short_best_moment(monkeypatch, tmp_path):
    import app.pipeline.agents.gemini_analyzer as analyzer

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *args, **kwargs: None)
    monkeypatch.setattr(gb, "_build_no_music_recipe", lambda *_args, **_kwargs: {"slots": [{}]})
    monkeypatch.setattr(
        analyzer,
        "build_recipe",
        lambda _: SimpleNamespace(beat_timestamps_s=[], color_grade="none"),
    )
    decision = gb._decide_generative_variant(
        job_id="minimum",
        rank=1,
        spec={"variant_id": "original", "text_mode": "none", "track": None},
        clip_metas=[
            SimpleNamespace(clip_id="a", best_moments=[{"start_s": 0, "end_s": 2, "energy": 9}])
        ],
        clip_id_to_local={"a": "/a"},
        clip_id_to_gcs={},
        probe_map={"/a": SimpleNamespace(duration_s=5)},
        available_footage_s=5,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
    )

    assert (decision.assembly_steps[0].in_s, decision.assembly_steps[0].out_s) == (0, 3)
    assert decision.extras["best_parts"][0]["reason"] == "best_moment_extended_for_minimum"


def test_natural_minimum_is_three_seconds_total_not_per_clip(monkeypatch, tmp_path):
    import app.pipeline.agents.gemini_analyzer as analyzer

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *args, **kwargs: None)
    monkeypatch.setattr(gb, "_build_no_music_recipe", lambda *_args, **_kwargs: {"slots": [{}]})
    monkeypatch.setattr(
        analyzer,
        "build_recipe",
        lambda _: SimpleNamespace(beat_timestamps_s=[], color_grade="none"),
    )
    decision = gb._decide_generative_variant(
        job_id="minimum-total",
        rank=1,
        spec={"variant_id": "original", "text_mode": "none", "track": None},
        clip_metas=[
            SimpleNamespace(clip_id="a", best_moments=[{"start_s": 1, "end_s": 1.2, "energy": 9}]),
            SimpleNamespace(clip_id="b", best_moments=[{"start_s": 1, "end_s": 1.2, "energy": 8}]),
        ],
        clip_id_to_local={"a": "/a", "b": "/b"},
        clip_id_to_gcs={},
        probe_map={"/a": SimpleNamespace(duration_s=5), "/b": SimpleNamespace(duration_s=5)},
        available_footage_s=10,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
    )

    assert decision.duration_s == 3
    assert [step.target_duration_s for step in decision.assembly_steps] == [1.5, 1.5]


def test_natural_pool_below_three_seconds_returns_typed_conflict():
    decisions, receipt, conflicts = gb._best_parts_source_decisions(
        ["a"],
        clip_metas=[SimpleNamespace(clip_id="a", best_moments=[])],
        clip_id_to_local={"a": "/a"},
        probe_map={"/a": SimpleNamespace(duration_s=2)},
        slots=[{}],
        target_duration_s=None,
    )

    assert decisions == []
    assert receipt[0]["media_id"] == "a"
    assert conflicts[0]["code"] == "snapshot_min_duration"


def test_approved_envelope_maps_frozen_media_windows_by_runtime_gcs_path():
    envelope = {
        "version": 1,
        "sources": [
            {
                "media_id": "approved-a",
                "gcs_path": "gs://a",
                "storage_generation": "7",
                "duration_s": 10,
            }
        ],
        "windows": [
            {
                "media_id": "approved-a",
                "source_start_s": 2,
                "source_end_s": 6,
                "score": 4,
                "reason": "best_moment",
            }
        ],
    }
    decisions, receipt = gb._approved_best_parts_decisions(
        envelope,
        clip_id_to_gcs={"clip_0": "gs://a"},
        clip_id_to_local={"clip_0": "/a"},
        probe_map={"/a": SimpleNamespace(duration_s=10)},
        runtime_generations={"gs://a": "7"},
    )

    assert [(step.clip_id, step.in_s, step.out_s) for step in decisions] == [("clip_0", 2, 6)]
    assert receipt[0]["media_id"] == "approved-a"


def test_silent_footage_uses_only_the_proven_creator_target(monkeypatch, tmp_path):
    import app.pipeline.agents.gemini_analyzer as analyzer

    monkeypatch.setattr(gb, "_record_render_subphase", lambda *args, **kwargs: None)
    monkeypatch.setattr(gb, "_build_no_music_recipe", lambda *_args, **_kwargs: {"slots": [{}]})
    monkeypatch.setattr(
        analyzer,
        "build_recipe",
        lambda _: SimpleNamespace(beat_timestamps_s=[], color_grade="none"),
    )
    kwargs = dict(
        job_id="silent-target",
        rank=1,
        spec={
            "variant_id": "original",
            "text_mode": "none",
            "track": None,
            "target_duration_s": 12,
        },
        clip_metas=[
            SimpleNamespace(clip_id="a", best_moments=[]),
            SimpleNamespace(clip_id="b", best_moments=[]),
        ],
        clip_id_to_local={"a": "/a", "b": "/b"},
        clip_id_to_gcs={},
        probe_map={"/a": SimpleNamespace(duration_s=15), "/b": SimpleNamespace(duration_s=15)},
        available_footage_s=30,
        agent_text=None,
        agent_form={},
        variant_dir=str(tmp_path),
    )

    proven = gb._decide_generative_variant(**kwargs, creator_target_duration_s=12)
    raw_marker = gb._decide_generative_variant(**kwargs)

    assert proven.duration_s == 12
    assert raw_marker.duration_s == 30
