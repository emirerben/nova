"""KRI-767: a confirmed label the composer MOVES to another clip is not a copy change.

Prod job d4f1b2d8 (2026-10-19): the creator asked to move "Oblea de la casa" to the clip
where Caro holds the plate to the camera. The text composer answered with
``remove_texts`` on the old label + ``add_text`` with the same words on the new clip. The
copy guard looked rows up by the old source id, read the moved row as "", and refused the
whole render. Real copy changes (other words, removed or missing copy, a changed source
row, one row claimed twice) must still fail.
"""

from __future__ import annotations

import copy
import json
import unicodedata

import pytest

from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.pipeline.guided_story import compile_proposal_execution_plan, validate_execution_plan
from app.pipeline.unified_montage import BriefView, UnifiedClip, plan_unified_montage
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creation_text_composition import (
    CreationTextCompositionError,
    _check_copy,
    compose_creation_text,
    snapshot_copy_constraints,
)
from app.services.kria_editor_ops import apply_text_lane_ops

# Captured from prod job d4f1b2d8-7d67-41dc-80a9-acbdd6fee474 (`GET jobs/<id>/debug`, the
# `nova.edit.copilot` run, prompt v11, model gemini-3.1-pro-preview): only what the copy guard
# and the editor ops read. The creator's last message was "Oblea de la casa etiketini caronun
# tabagi kameraya cok yaklastirdigi klibe koy".
PROD_TEXT_BARS = [
    {
        "id": "guided-title",
        "text": "Kolombiya tatlısı, Ekvadorlu eller",
        "start_s": 0.0,
        "end_s": 2.2,
        "role": "generative_intro",
        "font_family": "Fraunces",
        "size_px": 76.0,
        "color": "#FFF8F0",
        "x_frac": 0.5,
        "y_frac": 0.16,
        "position": "custom",
        "alignment": "center",
        "effect": "static",
    },
    {
        "id": "clip-label-unified-cut-4",
        "text": "Oblea de la casa",
        "start_s": 4.639,
        "end_s": 7.012,
        "role": "generative_intro",
        "font_family": "Fraunces",
        "size_px": 58.0,
        "color": "#FFF8F0",
        "x_frac": 0.5,
        "y_frac": 0.78,
        "position": "custom",
        "alignment": "center",
        "effect": "static",
    },
    {
        "id": "clip-label-unified-cut-11",
        "text": "Atakan'ın ilk ısırığı",
        "start_s": 16.971,
        "end_s": 19.193,
        "role": "generative_intro",
        "font_family": "Fraunces",
        "size_px": 58.0,
        "color": "#FFF8F0",
        "x_frac": 0.5,
        "y_frac": 0.78,
        "position": "custom",
        "alignment": "center",
        "effect": "static",
    },
]
# slot_id, media uuid (media_id is analysis-proxy-ios-<uuid>.mp4), output window, source window
# fmt: off
PROD_SLOT_WINDOWS = (
    ("unified-cut-1", "6B1AD57F-00CB-4EFD-A78C-B472BE73698B", 0.0, 1.513, 2.693, 4.206),
    ("unified-cut-2", "8DEE4E71-E9CB-44D6-A412-3F55EBC10622", 1.513, 3.426, 12.694, 14.607),
    ("unified-cut-3", "655A640C-EB9F-456E-8ECD-08A2D46D9B3D", 3.426, 4.639, 0.0, 1.213),
    ("unified-cut-4", "62514CA5-8A6E-4AE6-AEA6-19E256CDB86B", 4.639, 7.012, 2.793, 5.166),
    ("unified-cut-5", "B9BB4944-9EB7-4062-A487-18CCAB0B9CF5", 7.012, 8.985, 0.0, 1.973),
    ("unified-cut-6", "9E2A6CEB-35A6-430D-8D49-6458F864AA08", 8.985, 10.198, 0.0, 1.213),
    ("unified-cut-7", "98A20E7E-33D7-4EED-88E0-391037736203", 10.198, 12.131, 0.0, 1.933),
    ("unified-cut-8", "93E6BD6F-4AA1-4511-A659-5CEED5A75AD0", 12.131, 13.744, 2.994, 4.607),
    ("unified-cut-9", "B2B00EA4-85CC-479B-B179-DDF596FD5725", 13.744, 15.757, 1.987, 4.0),
    ("unified-cut-10", "D7E0CB05-7CEE-4B06-9B46-10FC42D9355E", 15.757, 16.971, 0.893, 2.107),
    ("unified-cut-11", "59075062-BF98-4249-915E-783711E0FCE8", 16.971, 19.193, 4.014, 6.236),
    ("unified-cut-12", "2F878362-0B50-4488-AB9E-5FF5F2D650C7", 19.193, 20.003, 3.33, 4.14),
)
# fmt: on
PROD_COPY_CONSTRAINTS = {
    "guided-title": "Kolombiya tatlısı, Ekvadorlu eller",
    "clip-label-unified-cut-4": "Oblea de la casa",
    "clip-label-unified-cut-11": "Atakan'ın ilk ısırığı",
}
PROD_OPS = [
    {
        "op": "remove_texts",
        "selector": {"ids": ["clip-label-unified-cut-4"]},
        "target_ids": ["clip-label-unified-cut-4"],
        "expected_count": 1,
    },
    {
        "op": "add_text",
        "text": "Oblea de la casa",
        "end_s": 8.985,
        "clip_id": "analysis-proxy-ios-B9BB4944-9EB7-4062-A487-18CCAB0B9CF5.mp4",
        "start_s": 7.012,
        "style_from": "labels",
    },
]


def prod_slots() -> list[dict]:
    return [
        {
            "slot_id": slot_id,
            "media_id": f"analysis-proxy-ios-{media}.mp4",
            "clip_index": index,
            "duration_s": round(end - start, 3),
            "output_start_s": start,
            "output_end_s": end,
            "source_start_s": source_start,
            "source_end_s": source_end,
        }
        for index, (slot_id, media, start, end, source_start, source_end) in enumerate(
            PROD_SLOT_WINDOWS
        )
    ]


OBLEA = "Oblea de la casa"
SOURCE_ID = "clip-label-unified-cut-4"


def row(row_id: str, text: str, start_s: float = 0.0, **extra) -> dict:
    return {"id": row_id, "text": text, "start_s": start_s, "end_s": start_s + 1.0, **extra}


# --- the real prod job ---------------------------------------------------------------


def test_real_kri767_move_passes_the_copy_guard():
    """The exact ops + constraints the composer produced in prod, replayed offline."""
    state = apply_text_lane_ops(PROD_TEXT_BARS, prod_slots(), PROD_OPS)
    # The premise of the incident: the source id is gone and the words live on a new row.
    assert SOURCE_ID not in {r["id"] for r in state.text}
    moved = [r for r in state.text if r["text"] == OBLEA]
    assert len(moved) == 1 and moved[0]["id"].startswith("clip-label-media-")
    assert (moved[0]["start_s"], moved[0]["end_s"]) == (7.012, 8.985)

    _check_copy(state.text, PROD_COPY_CONSTRAINTS)


def test_real_kri767_move_to_other_words_still_fails():
    ops = copy.deepcopy(PROD_OPS)
    ops[1]["text"] = "Oblea de la casa!"
    state = apply_text_lane_ops(PROD_TEXT_BARS, prod_slots(), ops)
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _check_copy(state.text, PROD_COPY_CONSTRAINTS)


# --- _check_copy: what counts as a move ---------------------------------------------


def test_unchanged_copy_by_id_still_passes():
    _check_copy([row(SOURCE_ID, OBLEA)], {SOURCE_ID: OBLEA})


def test_same_words_on_a_new_row_is_a_move():
    _check_copy([row("clip-label-media-x.mp4", OBLEA, 7.0)], {SOURCE_ID: OBLEA})


def test_a_move_tolerates_spacing_and_unicode_form_like_every_other_copy_check():
    cafe = "Café de Merope"
    decomposed = unicodedata.normalize("NFD", cafe)
    _check_copy([row("clip-label-media-x.mp4", f"  {decomposed}\n")], {SOURCE_ID: cafe})


def test_two_labels_swapped_between_clips_are_both_moves():
    constraints = {"clip-label-unified-cut-2": "Alpha", "clip-label-unified-cut-5": "Beta"}
    rows = [row("clip-label-media-b.mp4", "Beta", 1.0), row("clip-label-media-a.mp4", "Alpha", 4.0)]
    _check_copy(rows, constraints)


def test_one_label_moved_while_its_neighbour_stays_put():
    constraints = {"clip-label-unified-cut-2": "Alpha", "clip-label-unified-cut-5": "Beta"}
    rows = [
        row("clip-label-unified-cut-2", "Alpha", 1.0),
        row("clip-label-media-b.mp4", "Beta", 7.0),
    ]
    _check_copy(rows, constraints)


# --- _check_copy: real copy changes still fail --------------------------------------


@pytest.mark.parametrize(
    ("label", "rows"),
    [
        ("removed with no replacement", []),
        ("moved row has different words", [row("clip-label-media-x.mp4", "Oblea")]),
        ("moved row differs only by punctuation", [row("clip-label-media-x.mp4", f"{OBLEA}.")]),
        (
            "a removed row never counts as the moved one",
            [row("clip-label-media-x.mp4", OBLEA, removed=True)],
        ),
        (
            "a caption-lane row never counts as the moved one",
            [row("narration-caption-1", OBLEA)],
        ),
        (
            "source row still there but reworded, even with the words elsewhere",
            [row(SOURCE_ID, "Oblea"), row("clip-label-media-x.mp4", OBLEA, 7.0)],
        ),
        (
            "source row still there but emptied, even with the words elsewhere",
            [row(SOURCE_ID, ""), row("clip-label-media-x.mp4", OBLEA, 7.0)],
        ),
    ],
)
def test_real_copy_changes_still_fail(label, rows):
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _check_copy(rows, {SOURCE_ID: OBLEA})


def test_empty_confirmed_copy_still_fails():
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _check_copy([row("clip-label-media-x.mp4", "")], {SOURCE_ID: ""})


def test_two_confirmed_labels_with_the_same_words_need_two_rows():
    constraints = {"clip-label-unified-cut-2": "Same", "clip-label-unified-cut-5": "Same"}
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _check_copy([row("clip-label-media-x.mp4", "Same")], constraints)
    _check_copy(
        [row("clip-label-media-x.mp4", "Same"), row("clip-label-media-y.mp4", "Same", 4.0)],
        constraints,
    )


def test_a_row_owned_by_another_confirmed_label_cannot_be_claimed_twice():
    constraints = {"clip-label-unified-cut-2": "Same", "clip-label-unified-cut-5": "Same"}
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _check_copy([row("clip-label-unified-cut-5", "Same", 4.0)], constraints)


def test_a_chunked_sequence_is_owned_by_its_source_and_never_a_moved_label():
    chunks = [
        row(
            "title::sequence-0",
            "Join us",
            0.0,
            source_params={"sequence_source_id": "guided-title"},
        ),
        row(
            "title::sequence-1", "today", 1.0, source_params={"sequence_source_id": "guided-title"}
        ),
    ]
    _check_copy(chunks, {"guided-title": "Join us today"})
    # The chunks belong to the title: they cannot also stand in for a missing label.
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _check_copy(chunks, {"guided-title": "Join us today", SOURCE_ID: "Join us"})


# --- compose → persist → reload → approval-time compile (offline model replay) ------


TITLE = "Kolombiya tatlisi, Ekvadorlu eller"
ATAKAN = "Atakan'in ilk isirigi"


def _montage_plan():
    clips = [
        UnifiedClip(
            media_id=f"c{i}",
            proxy_path=f"users/test/c{i}.mp4",
            generation="1",
            duration_s=12,
            width=1080,
            height=1920,
        )
        for i in range(12)
    ]
    return plan_unified_montage(
        clips,
        BriefView(
            title_literal=TITLE,
            wants_per_clip_text=True,
            clip_literals={"c3": OBLEA, "c10": ATAKAN},
            target_duration_s=20,
        ),
        strategy={"opening_title": TITLE, "font_family": "Inter", "text_color": "#FFFFFF"},
        font_covers=lambda *_: True,
    )


def _compose(monkeypatch, plan, response):
    seen = []

    def run(self, input, *, ctx=None):
        seen.append(input)
        assert isinstance(input, EditCopilotInput)
        return self.parse(json.dumps(response), input)

    monkeypatch.setattr(EditCopilotAgent, "run", run)
    constraints = snapshot_copy_constraints(plan.snapshot)
    result = compose_creation_text(
        plan.snapshot,
        creator_request=f"Move the {OBLEA} label to the clip where the plate comes to the camera.",
        copy_constraints=constraints,
    )
    assert len(seen) == 1
    return result, constraints


def _move_response(plan, *, text=OBLEA):
    """The same op shapes the prod composer returned: remove the label, add it on another clip."""
    base = compile_proposal_execution_plan(plan.snapshot)
    label = next(r for r in base["text_elements"] if r["text"] == OBLEA)
    labelled = {r["id"] for r in base["text_elements"] if r["id"].startswith("clip-label-")}
    target = next(
        slot
        for slot in base["story_timeline"]
        if f"clip-label-{slot['moment_id']}" not in labelled
        and slot["output_start_s"] >= label["end_s"]
    )
    return (
        {
            "intent": "edit",
            "confidence": 0.95,
            "reply": "Moved the label.",
            "ops": [
                {"op": "remove_texts", "selector": {"ids": [label["id"]]}},
                {
                    "op": "add_text",
                    "text": text,
                    "start_s": target["output_start_s"],
                    "end_s": target["output_end_s"],
                    "clip_id": target["media_id"],
                    "style_from": "labels",
                },
            ],
        },
        label,
        target,
    )


def test_moved_label_is_composed_persisted_and_recompiled_at_approval(monkeypatch):
    plan = _montage_plan()
    original = compile_proposal_execution_plan(plan.snapshot)
    response, label, target = _move_response(plan)
    assert label["id"] in snapshot_copy_constraints(plan.snapshot)  # the guard watches it

    snapshot, constraints = _compose(monkeypatch, plan, response)

    restored = EditProposalSnapshot.model_validate_json(snapshot.model_dump_json())
    result = compile_proposal_execution_plan(restored)  # replays the pinned program
    moved = [r for r in result["text_elements"] if r["text"] == OBLEA]
    assert len(moved) == 1
    assert (moved[0]["start_s"], moved[0]["end_s"]) == (
        target["output_start_s"],
        target["output_end_s"],
    )
    assert label["id"] not in {r["id"] for r in result["text_elements"]}
    assert [r["text"] for r in result["text_elements"] if r["text"] == TITLE] == [TITLE]
    assert result["story_timeline"] == original["story_timeline"]
    plan.snapshot = restored
    assert validate_execution_plan(result, plan.guided_edit())
    # The approval-time guard (a pinned program, no model) agrees with compose time.
    assert compose_creation_text(restored, creator_request="again", copy_constraints=constraints)


def test_composer_that_rewords_the_moved_label_still_fails_the_render(monkeypatch):
    plan = _montage_plan()
    response, _, _ = _move_response(plan, text="Oblea de la casa (nueva)")
    with pytest.raises(CreationTextCompositionError, match="changed confirmed copy"):
        _compose(monkeypatch, plan, response)
