"""Cloud narrated storyboard bars reach the editor with their cloud look.

A cloud ``narrated`` (recorded voiceover) render with the storyboard on saves
its authored bars (opening title, ``PLAYER n``, transcript scores) in
``text_elements`` with ``text_elements_materialized_from ==
"narrated_storyboard"``, as bare presets ("top"/"bottom", a size class, no
face). Its captions ride the separate ``caption_cues`` lane (``text_mode`` is
"none", so nothing projects into ``text_elements``).

The iOS editor fills a missing ``y_frac``/``font_family`` with 0.5 / Fraunces,
so it previewed the title mid-frame and its Save wrote Fraunces into the next
burn. The read path now hands the editor the look the cloud burns.
"""

from __future__ import annotations

import copy
import types
import uuid

import pytest

import app.routes.generative_jobs as gj
from app.agents._schemas.text_element import (
    CAPTION_CUE_SOURCE,
    is_narrated_storyboard_element,
    merge_projected_text_elements_for_variant,
)
from app.pipeline.transcribe import Transcript, Word
from app.tasks import generative_build as gb
from tests.routes.test_editor_commit import _arm, _commit_req

_CUES = [
    {"text": "Intro to the final", "start_s": 0.0, "end_s": 1.0},
    {"text": "score six four", "start_s": 1.0, "end_s": 1.8},
]
_TITLE, _PLAYER, _SCORE = "Match Day", "PLAYER 1", "six four"


def _storyboard_rows() -> list[dict]:
    transcript = Transcript(
        words=[
            Word("Intro", 0.0, 0.4, 1.0),
            Word("score", 1.0, 1.2, 1.0),
            Word("six", 1.2, 1.5, 1.0),
            Word("four", 1.5, 1.8, 1.0),
        ],
        language="en",
    )
    return gb._narrated_storyboard_text_elements(
        transcript=transcript,
        step_timings=[types.SimpleNamespace(step_id="step_0", start_s=0.0, end_s=2.0)],
        clip_assignments=[types.SimpleNamespace(step_id="step_0", clip_path="/tmp/a.mp4")],
        clip_id_by_path={"/tmp/a.mp4": "clip_0"},
        creator_request="Add intro text, player names, and scores",
        explicit_opening_title=_TITLE,
        storyboard={"overlays": []},
    )


def _storyboard_variant(**extra) -> dict:
    """The variant row `_render_narrated_variant` persists with the storyboard on
    (field for field what prod job 1f51a6e2 carries)."""
    return {
        "variant_id": "narrated",
        "resolved_archetype": "narrated",
        "text_mode": "none",
        "render_status": "ready",
        "render_finished_at": "2026-07-01T00:00:00Z",
        "duration_s": 2.0,
        "video_path": "generative-jobs/job/variant_1_narrated.mp4",
        "base_video_path": "generative-jobs/job/variant_1_narrated_base.mp4",
        "caption_cues": copy.deepcopy(_CUES),
        "captions_enabled": True,
        "voiceover_caption_style": "sentence",
        "text_elements": _storyboard_rows(),
        "text_elements_user_edited": False,
        "text_elements_materialized_from": "narrated_storyboard",
        "context_label_text_elements": None,
        "lyric_overlay_snapshot": None,
        "mix": None,
        **extra,
    }


def _job(variant: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        id=uuid.uuid4(),
        mode="content_plan",
        status="variants_ready",
        all_candidates={"clip_paths": []},
        assembly_plan={"variants": [variant]},
    )


def _texts(rows: list[dict] | None) -> list[str]:
    return [row["text"] for row in rows or []]


def _by_text(rows: list[dict] | None) -> dict[str, dict]:
    return {row["text"]: row for row in rows or []}


def _look(row: dict) -> tuple:
    return (
        row.get("position"),
        row.get("x_frac"),
        row.get("y_frac"),
        row.get("size_px"),
        row.get("font_family"),
    )


_TITLE_LOOK = ("custom", 0.5, 0.15, 120.0, "Playfair Display")
_PLAYER_LOOK = ("custom", 0.5, 0.85, 36.0, "Playfair Display")
_SCORE_LOOK = ("custom", 0.5, 0.15, 120.0, "Playfair Display")


# ── Read path ────────────────────────────────────────────────────────────────


def test_storyboard_bars_reach_the_editor_with_the_cloud_look() -> None:
    merged = merge_projected_text_elements_for_variant(_storyboard_variant())

    assert _texts(merged) == [_TITLE, _PLAYER, _SCORE]
    rows = _by_text(merged)
    assert _look(rows[_TITLE]) == _TITLE_LOOK
    assert _look(rows[_PLAYER]) == _PLAYER_LOOK
    assert _look(rows[_SCORE]) == _SCORE_LOOK
    assert (rows[_TITLE]["role"], rows[_TITLE]["effect"]) == ("generative_intro", "fade-in")
    assert rows[_SCORE]["effect"] == "pop-in"
    # Same ids and provenance as the stored rows, so a Save replaces them in place.
    assert [row["id"] for row in merged] == [row["id"] for row in _storyboard_rows()]
    assert all(is_narrated_storyboard_element(row) for row in merged)
    # Captions stay in their own lane, never mirrored into text.
    assert not any(
        (row.get("source_params") or {}).get("source") == CAPTION_CUE_SOURCE for row in merged
    )


def test_status_route_serves_the_resolved_bars_without_rewriting_the_row(monkeypatch) -> None:
    _arm(monkeypatch)
    job = _job(_storyboard_variant())

    [variant] = gj._variants_for_response(job)

    assert _texts(variant["text_elements"]) == [_TITLE, _PLAYER, _SCORE]
    assert _look(variant["text_elements"][0]) == _TITLE_LOOK
    assert variant["caption_cues"] == _CUES
    assert variant["editor_capabilities"]["text_elements"] is True
    assert job.assembly_plan["variants"][0]["text_elements"] == _storyboard_rows()


def test_storyboard_bars_stay_beside_a_projection_without_duplicates() -> None:
    """A row that does project text (here caption mirrors) keeps its saved bars."""
    variant = _storyboard_variant(text_mode="agent_text")
    merged = merge_projected_text_elements_for_variant(variant)
    assert _texts(merged) == [_TITLE, _PLAYER, _SCORE, *(cue["text"] for cue in _CUES)]

    mirror = merged[3]
    variant["text_elements"] = [*variant["text_elements"], mirror]
    assert _texts(merge_projected_text_elements_for_variant(variant)) == _texts(merged)


def test_ios_saved_row_resolves_to_where_the_cloud_burned_it() -> None:
    """Rows an iOS Save already wrote: named position + injected y 0.5 + Fraunces.

    The cloud ignored that y_frac (named position) and burned Fraunces, so the
    editor must show exactly that: y 0.15, Fraunces.
    """
    rows = _storyboard_rows()
    for row in rows:
        row.update({"x_frac": 0.5, "y_frac": 0.5, "font_family": "Fraunces"})
    variant = _storyboard_variant(text_elements=rows, text_elements_user_edited=True)

    merged = _by_text(merge_projected_text_elements_for_variant(variant))

    assert _look(merged[_TITLE]) == ("custom", 0.5, 0.15, 120.0, "Fraunces")
    assert _look(merged[_PLAYER]) == ("custom", 0.5, 0.85, 36.0, "Fraunces")


# ── Save round trip ──────────────────────────────────────────────────────────


def _save(monkeypatch, job, text_elements: list[dict]) -> dict:
    variant = job.assembly_plan["variants"][0]
    return gj.prepare_editor_commit(
        job,
        "narrated",
        _commit_req(
            base_generation=gj.variant_render_baseline(variant),
            text_elements=text_elements,
        ),
    )


def test_save_with_an_added_bar_keeps_the_storyboard_and_its_look(monkeypatch) -> None:
    _arm(monkeypatch)
    job = _job(_storyboard_variant())
    [served] = gj._variants_for_response(job)
    added = {
        "id": "user-bar",
        "text": "What a finish",
        "start_s": 1.0,
        "end_s": 2.0,
        "role": "generative_intro",
        "position": "custom",
        "x_frac": 0.5,
        "y_frac": 0.5,
        "size_px": 72,
        "font_family": "Inter",
    }

    prep = _save(monkeypatch, job, [*served["text_elements"], added])

    assert prep["sections"]["text_elements"] is True
    stored = job.assembly_plan["variants"][0]
    assert stored["text_elements_user_edited"] is True
    assert stored["caption_cues"] == _CUES
    stored_rows = _by_text([row for row in stored["text_elements"] if not row.get("removed")])
    assert set(stored_rows) == {_TITLE, _PLAYER, _SCORE, "What a finish"}
    assert _look(stored_rows[_TITLE]) == _TITLE_LOOK
    [reloaded] = gj._variants_for_response(job)
    assert _texts(reloaded["text_elements"]) == [_TITLE, _PLAYER, _SCORE, "What a finish"]
    assert _look(reloaded["text_elements"][0]) == _TITLE_LOOK


def test_deleting_a_storyboard_bar_sticks(monkeypatch) -> None:
    _arm(monkeypatch)
    job = _job(_storyboard_variant())
    [served] = gj._variants_for_response(job)

    _save(monkeypatch, job, [row for row in served["text_elements"] if row["text"] != _PLAYER])

    [reloaded] = gj._variants_for_response(job)
    assert _texts(reloaded["text_elements"]) == [_TITLE, _SCORE]


def test_editing_the_title_persists_the_edit(monkeypatch) -> None:
    _arm(monkeypatch)
    job = _job(_storyboard_variant())
    [served] = gj._variants_for_response(job)
    rows = copy.deepcopy(served["text_elements"])
    rows[0].update({"text": "Final Day", "y_frac": 0.3})

    _save(monkeypatch, job, rows)

    [reloaded] = gj._variants_for_response(job)
    title = reloaded["text_elements"][0]
    assert (title["text"], title["y_frac"], title["font_family"]) == (
        "Final Day",
        0.3,
        "Playfair Display",
    )


# ── Reburn ───────────────────────────────────────────────────────────────────


def _compose(monkeypatch, variant: dict) -> tuple[list[dict], list[dict]]:
    """Run `_compose_subtitled_final`; return (Skia text overlays, caption cues burned)."""
    from app.pipeline import text_overlay_skia

    skia_overlays: list[dict] = []
    burned_cues: list[dict] = []

    def _fake_skia(base, overlays, out, tmpdir, **_kwargs):
        skia_overlays.extend(overlays)

    def _fake_captions(captions_input, final_path, variant, tmpdir, **_kwargs):
        burned_cues.extend(variant.get("caption_cues") or [])

    monkeypatch.setattr(text_overlay_skia, "burn_text_overlays_skia", _fake_skia)
    monkeypatch.setattr(gb, "_burn_persisted_captions_onto_base", _fake_captions)
    gb._compose_subtitled_final(
        "/tmp/base.mp4",
        variant,
        "/tmp",
        job_id="job",
        variant_id="narrated",
        upload_key_base="generative-jobs/job/variant_1_narrated_cap",
    )
    return skia_overlays, burned_cues


@pytest.mark.parametrize("saved_from_editor", [False, True])
def test_reburn_burns_the_storyboard_under_the_captions(monkeypatch, saved_from_editor) -> None:
    _arm(monkeypatch)
    job = _job(_storyboard_variant())
    if saved_from_editor:
        [served] = gj._variants_for_response(job)
        _save(monkeypatch, job, served["text_elements"])
    variant = job.assembly_plan["variants"][0]

    assert gb._should_compose_subtitled_final(variant)
    overlays, cues = _compose(monkeypatch, variant)

    by_text = {overlay["text"]: overlay for overlay in overlays}
    assert set(by_text) == {_TITLE, _PLAYER, _SCORE}
    assert cues == _CUES
    title = by_text[_TITLE]
    if saved_from_editor:
        assert (title["position_y_frac"], title["text_size_px"]) == (0.15, 120)
        assert title["font_family"] == "Playfair Display"
    else:
        assert (title["position"], title["text_size"]) == ("top", "large")
