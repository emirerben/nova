"""KRI-736: Save after deleting a clip never fails because a text ends past the new timeline.

After the order-contract fix (#1648) the same Save still 422'd ``unsupported_phone_edit``
("text layer exceeds the timeline", job 5a8f5990). Two server gaps, both on the AUTHORED
compile that a clip delete converts a guided story into:

* the phone clamps every text to the new total, but ``build_intro_overlay`` rounds a text's
  end UP to 3 decimals while a frame-based cut total is not millisecond-exact (3 x 7.4333333
  = 22.2999999), so the layer ended ~0.1 ms past the clips and the recipe validator (strict
  ``>``) refused it;
* a text still on the OLD clock (an app build without GuidedLabelRebase, a stale draft) was
  silently dropped by ``render_presentation`` and the creator contract then 422'd.

Drives the exact route sequence (``plan_items.editor_commit``) on a phone guided job.
"""

from __future__ import annotations

import copy

import pytest

from app.routes import generative_jobs as gj
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.authored_text_fit import fit_text_to_timeline
from app.services.device_render import device_status
from app.services.editor_empty_drafts import stage_initial_authored_baseline
from tests.routes.test_phone_guided_clip_delete_order_contract import _order_required_job

VARIANT = "guided_story"
CLIP_S = 7.5


def _text(base: dict, text_id: str, start: float, end: float, text: str | None = None) -> dict:
    return {
        **copy.deepcopy(base),
        "id": text_id,
        "start_s": start,
        "end_s": end,
        "text": text or text_id,
    }


def _save(monkeypatch, *, deleted="m1", clip_s=CLIP_S, texts):
    """The `deletions` + `timeline_slots` + `text_elements` Save a clip delete sends.

    ``texts(variant, kept_starts, total)`` returns the text lane as the client would send
    it; ``kept_starts`` are the new clip windows.
    """
    job, variant, revision = _order_required_job(monkeypatch)
    rows = gj.editor_deletion_timeline(job, variant)
    sources = revision["sources"]
    target = next(r for r in rows if sources[r["clip_index"]]["media_id"] == deleted)
    slots = [
        gj.TimelineSlotEdit(
            slot_id=r["slot_id"],
            clip_index=r["clip_index"],
            in_s=r["in_s"],
            duration_s=clip_s,
            transition_after="cut",
            transition_duration_s=None,
        )
        for r in rows
    ]
    kept = [s for s in slots if s.slot_id != target["slot_id"]]
    windows, cursor = {}, 0.0
    for slot in kept:
        windows[slot.slot_id] = (cursor, cursor + clip_s)
        cursor += clip_s
    total = sum(s.duration_s for s in kept)
    baseline = stage_initial_authored_baseline(job, VARIANT, rows)
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=revision["revision_number"],
        deletions=[{"kind": "clip", "id": target["slot_id"]}],
        timeline_slots=slots,
        text_elements=texts(variant, windows, total),
    )
    body = gj.normalize_editor_deletion_request(baseline, VARIANT, body)
    gj.require_guided_story_editor_commit(baseline, VARIANT, body)
    prepare_authored_editor_commit(baseline, VARIANT, body, validation_arguments={})
    saved = baseline.assembly_plan["variants"][0]["text_elements"]
    recipe = device_status(baseline, VARIANT).request.recipe
    return saved, recipe, total


def _phone_retimed(variant, windows, total):
    """What the current phone sends: surviving labels follow their clips, the pin spans the cut."""
    base = variant["text_elements"][1]
    by_id = {t["id"]: t for t in variant["text_elements"]}
    survivors = {
        "clip-label-cut-0": windows["cut-0"],
        "clip-label-cut-2": windows["cut-2"],
        "clip-label-cut-3": windows["cut-3"],
    }
    lane = [by_id["guided-title"]]
    lane += [_text(base, i, *w, text=by_id[i]["text"]) for i, w in survivors.items()]
    lane.append(_text(base, "guided-pinned-0", 0.0, total, "Week 2"))
    lane.append(_text(base, "kria-chat-note", 1.0, total, "added in chat"))
    return lane


@pytest.mark.parametrize("clip_s", [7.5, 7.4333333, 7.4999999, 7.4916667])
def test_a_phone_retimed_lane_saves_when_the_cut_total_is_not_millisecond_exact(
    monkeypatch, clip_s
):
    saved, recipe, total = _save(monkeypatch, clip_s=clip_s, texts=_phone_retimed)

    assert {t["id"] for t in saved} >= {"clip-label-cut-3", "guided-pinned-0", "kria-chat-note"}
    assert recipe.text_layers, "the authored cut must still carry its text"
    assert max(layer.end for layer in recipe.text_layers) <= recipe.duration
    assert recipe.duration == pytest.approx(total)


def _old_clock(variant, windows, total):
    """An app build that does not re-time: every window is still on the 30 s pre-delete clock."""
    lane = copy.deepcopy(variant["text_elements"])
    lane.append(_text(lane[1], "guided-pinned-0", 0.0, 30.0, "Week 2"))
    return [
        t for t in lane if t["id"] != "clip-label-cut-1"
    ]  # the client dropped the deleted clip's label


def test_an_old_clock_lane_is_fitted_onto_the_surviving_footage(monkeypatch):
    saved, recipe, total = _save(monkeypatch, texts=_old_clock)

    assert total == pytest.approx(22.5)
    by_id = {t["id"]: t for t in saved}
    # Wholly past the new end, but its clip survived: it follows that clip (was 22.5-30).
    assert by_id["clip-label-cut-3"]["start_s"] == pytest.approx(15.0, abs=0.05)
    assert by_id["clip-label-cut-3"]["end_s"] == pytest.approx(22.5, abs=0.05)
    assert by_id["clip-label-cut-2"]["end_s"] == pytest.approx(15.0, abs=0.05)
    # The whole-video pin is clamped, never moved or dropped.
    assert (by_id["guided-pinned-0"]["start_s"], by_id["guided-pinned-0"]["end_s"]) == (0.0, 22.5)
    assert all(t["end_s"] <= total + 1e-6 for t in saved)
    assert max(layer.end for layer in recipe.text_layers) <= recipe.duration


ROWS = [
    {"slot_id": f"s{i}", "media_id": f"m{i}", "in_s": 0.0, "duration_s": 10.0, "clip_index": i}
    for i in range(3)
]


def _fit(elements, deleted="s1"):
    return fit_text_to_timeline(
        elements, old_rows=ROWS, new_rows=[r for r in ROWS if r["slot_id"] != deleted]
    )


def test_text_whose_footage_was_all_deleted_goes_with_it_but_other_text_is_kept():
    gone = {"id": "clip-label-s1", "start_s": 10.0, "end_s": 20.0, "text": "gone"}
    tail = {"id": "clip-label-s2", "start_s": 20.0, "end_s": 30.0, "text": "tail"}
    fitted = _fit([gone, tail])
    assert [t["id"] for t in fitted] == ["clip-label-s2"]
    assert (fitted[0]["start_s"], fitted[0]["end_s"]) == (10.0, 20.0)


def test_a_text_that_already_fits_is_returned_untouched():
    fits = {"id": "free", "start_s": 1.0, "end_s": 20.0, "text": "x"}
    assert _fit([fits])[0] is fits


def test_a_small_overshoot_is_clamped_not_reprojected():
    late = {"id": "free-text", "start_s": 2.0, "end_s": 20.05, "text": "x"}
    fitted = _fit([late])[0]
    assert (fitted["start_s"], fitted["end_s"]) == (2.0, 20.0)


@pytest.mark.parametrize(
    "element",
    [
        {
            "id": "caption-1",
            "start_s": 25.0,
            "end_s": 30.0,
            "text": "c",
            "source_params": {"source": "caption_cue"},
        },
        {"id": "lyric_3", "start_s": 25.0, "end_s": 30.0, "text": "l"},
        {"id": "n1", "start_s": 25.0, "end_s": 30.0, "text": "n", "role": "lyric_line"},
        {
            "id": "score",
            "start_s": 25.0,
            "end_s": 30.0,
            "text": "s",
            "source_params": {"narration_label_kind": "score"},
        },
    ],
)
def test_caption_lyric_and_narration_text_keep_their_own_clock(element):
    assert _fit([element]) == [element]
