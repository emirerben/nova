"""KRI-1391: the copilot is shown spoken words and song beats, and edits against them.

The two replay threads carry the snapshot the REAL `build_editor_snapshot` produced for
`author_kri1391.author_variant`. This file holds them to that builder and to the run that
compiles the recorded model output; it does not restate the builder's arithmetic.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from app.agents.edit_copilot import _format_snapshot
from app.services.kria_editor_ops import timeline_signature

from .author_kri1391 import author_snapshot, author_variant
from .runner import THREAD_DIR, load_fixture, run_thread

THREADS = ["v2_speech_cut_at_word", "v2_title_on_first_beat"]


def _cassette(fixture_id: str) -> dict:
    return load_fixture(THREAD_DIR / f"{fixture_id}.json").turns[-1].kria["cassette"]


@pytest.mark.parametrize("fixture_id", THREADS)
def test_fixture_snapshot_is_what_the_builder_emits(fixture_id):
    shown = _cassette(fixture_id)["agent_input"]["variant_snapshot"]
    assert shown == author_snapshot(), (
        "regenerate: python -m tests.evals.request_following.author_kri1391"
    )


@pytest.mark.parametrize("fixture_id", THREADS)
def test_replay_compiles_the_recorded_edit_and_meets_the_request(fixture_id):
    result = run_thread(load_fixture(THREAD_DIR / f"{fixture_id}.json"))
    assert [score.status for score in result.scores] == ["met"]
    assert result.evidence and result.evidence.execution_path == "compile_only"


@pytest.mark.parametrize("fixture_id", THREADS)
def test_recorded_edit_copies_its_times_from_the_marks_the_model_was_shown(fixture_id):
    cassette = _cassette(fixture_id)
    shown = cassette["agent_input"]["variant_snapshot"]
    allowed = set(shown["beat_marks"]) | {w["end_s"] for w in shown["speech"]["words"]}
    allowed |= {w["start_s"] for w in shown["speech"]["words"]}
    for op in cassette["model_output"]["ops"]:
        assert op.get("start_s") in allowed


def test_the_prompt_lists_the_marks_and_numbers_each_caption_cue():
    prompt = _format_snapshot(author_snapshot())
    assert "MUSIC BEAT MARKS" in prompt and "0.480, 0.970" in prompt
    assert "'over'@1.30-1.70" in prompt and '(after "over")' in prompt
    assert "\n0. id='cue-1'" in prompt and "\n1. id='cue-2'" in prompt


def test_marks_are_withheld_once_a_draft_re_cuts_the_timeline():
    snapshot = author_snapshot(timeline_dirty=True)
    assert "speech" not in snapshot and "beat_marks" not in snapshot


def test_a_trim_changes_the_timeline_signature_so_the_planner_marks_it_dirty():
    saved = author_variant()
    job = SimpleNamespace(assembly_plan={"variants": [saved]}, all_candidates={})
    trimmed = copy.deepcopy(saved)
    trimmed["ai_timeline"]["slots"][0].update(in_s=1.7, duration_s=4.3)
    assert timeline_signature(job, saved) == timeline_signature(job, copy.deepcopy(saved))
    assert timeline_signature(job, saved) != timeline_signature(job, trimmed)
