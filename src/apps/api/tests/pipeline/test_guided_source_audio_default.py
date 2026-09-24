"""KRI-184: a guided montage keeps the clips' own sound unless the creator says otherwise.

Prod job a5d1ef9d: bare "Suggest an edit." -> `audio_strategy=licensed_music` ->
`montage_audio=None`; the matched song is reference-only (never mixed in), so the
render was silent (source muted, no music) with a burst of sound at each cut.
"""

from __future__ import annotations

import subprocess

import pytest

from app.pipeline import guided_story
from app.pipeline.guided_story import (
    compile_execution_plan,
    plan_preserves_source_audio,
    song_reference_variant_fields,
    validate_execution_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from tests.pipeline.test_guided_story import _guided_snapshot
from tests.pipeline.test_phone_guided_plan import fixture


@pytest.mark.parametrize(
    ("plan", "expected"),
    [
        ({"compiler_version": 8, "montage_audio": None, "narration": None}, True),
        ({"compiler_version": 6, "montage_audio": None}, True),
        ({"compiler_version": 8, "montage_audio": {"preserve_source_audio": True}}, True),
        # An explicit "mute the originals" is still honoured.
        ({"compiler_version": 8, "montage_audio": {"preserve_source_audio": False}}, False),
        # A recorded voiceover hard-replaces footage audio.
        ({"compiler_version": 8, "montage_audio": None, "narration": {"any": "thing"}}, False),
        # Pre-v6 plans mix their song instead of keeping the footage sound.
        ({"compiler_version": 5, "montage_audio": None}, False),
    ],
)
def test_plan_preserves_source_audio_truth_table(plan: dict, expected: bool) -> None:
    assert plan_preserves_source_audio(plan) is expected


def test_no_explicit_choice_keeps_source_audio_and_the_stored_plan_still_validates() -> None:
    raw = _guided_snapshot()
    assert raw["approved_proposal"].get("montage_audio") is None

    plan = compile_execution_plan(raw, track=None)

    assert plan["montage_audio"] is None
    # Interpretation changed, not the compiled plan: no "changed after approval".
    assert validate_execution_plan(plan, raw) == plan
    fields = song_reference_variant_fields(plan)
    assert fields["music_playback_mode"] == "reference_only"
    assert fields["source_audio_preserved"] is True


def test_phone_recipe_for_a_reference_only_plan_keeps_original_volume() -> None:
    plan, bindings = fixture()
    plan = plan.model_copy(update={"compiler_version": 8, "editor_audio_level": 1.0})
    assert plan.montage_audio is None

    recipe = compile_phone_guided_plan(plan, bindings)

    assert recipe.audio.original_volume == 1.0


def test_phone_recipe_honours_an_explicit_mute() -> None:
    plan, bindings = fixture()
    plan = plan.model_copy(
        update={"compiler_version": 8, "montage_audio": {"preserve_source_audio": False}}
    )

    assert compile_phone_guided_plan(plan, bindings).audio.original_volume == 0


def test_cloud_source_audio_mux_runs_by_default_and_fades_every_cut(monkeypatch, tmp_path) -> None:
    plan = {
        "compiler_version": 8,
        "montage_audio": None,
        "narration": None,
        "resolved_duration_s": 4.0,
        "editor_audio_level": 1.0,
        "story_timeline": [
            {
                "kind": "video",
                "media_id": "a",
                "source_start_s": 0.0,
                "source_end_s": 2.0,
                "output_start_s": 0.0,
            },
            {
                "kind": "video",
                "media_id": "b",
                "source_start_s": 1.0,
                "source_end_s": 1.02,  # shorter than 4 fade windows: fade is clamped
                "output_start_s": 2.0,
            },
        ],
    }
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="0\n", stderr="")

    monkeypatch.setattr(guided_story.subprocess, "run", fake_run)

    out = guided_story._mux_guided_source_audio(
        "assembled.mp4",
        plan,
        {"a": "a.mp4", "b": "b.mp4"},
        str(tmp_path / "out.mp4"),
    )

    assert out == str(tmp_path / "out.mp4")
    graph = next(c[c.index("-filter_complex") + 1] for c in commands if "-filter_complex" in c)
    assert graph.count("afade=t=in") == 2
    assert graph.count("afade=t=out") == 2
    assert "afade=t=in:d=0.01" in graph  # full 10 ms edge on the 2 s clip
    assert "afade=t=in:d=0.005" in graph  # clamped to a quarter of the 20 ms clip


def test_cloud_source_audio_mux_is_skipped_for_an_explicit_mute() -> None:
    plan = {"compiler_version": 8, "montage_audio": {"preserve_source_audio": False}}

    assert guided_story._mux_guided_source_audio("assembled.mp4", plan, {}, "out.mp4") == (
        "assembled.mp4"
    )
