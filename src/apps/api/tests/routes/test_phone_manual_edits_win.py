"""KRI-736: the creator's own manual editor edits win over the approved text-role rules.

Job 5a8f5990, fifth distinct Save refusal: after the Save at 16:04 succeeded, the next one
(more manual edits) 422'd ``unsupported_phone_edit`` / "put confirmed text in the wrong
place" / ``opening_title``. The approved contract carried ``[opening X, any X, ...]`` and
the editor Save keeps every role a text was approved with, then the recompiled recipe is
verified against them, so a creator who MOVED, RE-TIMED or RE-WORDED the opening title was
refused. Founder decision: in the manual editor the creator wins. The specific role
(opening / closing / clip) degrades to ``any`` for that text -- the words themselves must
still be on screen -- while the automatic pipelines keep the strict verifier.

Drives the real route sequence (``plan_items.editor_commit``) on a phone guided job.
"""

from __future__ import annotations

import copy

import pytest

from app.routes import generative_jobs as gj
from app.services.authored_editor import prepare_authored_editor_commit
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    TextRequirement,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD, device_status
from app.services.editor_empty_drafts import stage_initial_authored_baseline
from tests.routes.test_phone_guided_clip_delete_order_contract import _order_required_job
from tests.routes.test_phone_guided_clip_delete_text_fit import _phone_retimed

VARIANT = "guided_story"
CLIP_S = 7.5
TITLE = "Weekend in Lisbon"


def _requirements(**overrides):
    base = {
        "opening": TextRequirement(role="opening", text=TITLE),
        "any_title": TextRequirement(role="any", text=TITLE),
        "any_label": TextRequirement(role="any", text="Place 2"),
    }
    base.update(overrides)
    return tuple(v for v in base.values() if v is not None)


def _save(
    monkeypatch,
    *,
    requirements,
    texts,
    deleted="m1",
    clip_s=CLIP_S,
    split=None,
    order_required=True,
    duration_s=None,
    contract_changes=None,
    muted=None,
):
    job, variant, revision = _order_required_job(monkeypatch, order_required=order_required)
    contract = job.assembly_plan[CONTRACT_FIELD]
    from app.services.creator_render_contract import CreatorRenderContract

    rebound = CreatorRenderContract.model_validate(contract).rebind(
        exact_texts=requirements,
        **({"duration_s": duration_s} if duration_s else {}),
        **(contract_changes or {}),
    )
    job.assembly_plan[CONTRACT_FIELD] = rebound.model_dump(mode="json")

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
            muted=muted,
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
    prep = prepare_authored_editor_commit(baseline, VARIANT, body, validation_arguments={})
    return baseline, prep, total


def _lane(variant, windows, total, *, title):
    lane = _phone_retimed(variant, windows, total)
    by_id = {t["id"]: t for t in lane}
    by_id["guided-title"].update(title)
    return lane


def _saved_texts(baseline):
    contract = baseline.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]
    return [(r["role"], r["text"], r["duration_s"]) for r in contract["exact_texts"]]


def _title_edit(**changes):
    return lambda variant, windows, total: _lane(variant, windows, total, title=changes)


def test_an_unmoved_opening_title_keeps_its_opening_rule(monkeypatch):
    baseline, _, _ = _save(
        monkeypatch, requirements=_requirements(), texts=_title_edit(start_s=0.0, end_s=2.2)
    )
    assert ("opening", TITLE, None) in _saved_texts(baseline)


@pytest.mark.parametrize(
    "title",
    [
        {"start_s": 3.0, "end_s": 6.0},  # moved later in the editor
        {"start_s": 1.0, "end_s": 2.2},  # nudged a little off frame zero
        {"start_s": 0.0, "end_s": 0.6, "text": TITLE},  # shortened below nothing: still opening
    ],
)
def test_a_moved_opening_title_saves_and_degrades_to_any(monkeypatch, title):
    baseline, _, _ = _save(monkeypatch, requirements=_requirements(), texts=_title_edit(**title))

    saved = _saved_texts(baseline)
    if title["start_s"] > 0.05:
        assert not any(role == "opening" for role, _, _ in saved)
    # Never to nothing: the words themselves are still a required, verified presence.
    assert ("any", TITLE, None) in saved
    recipe = device_status(baseline, VARIANT).request.recipe
    assert any(TITLE in "".join(run.text for run in layer.runs) for layer in recipe.text_layers)


def test_an_opening_title_held_shorter_than_the_approved_time_saves(monkeypatch):
    requirements = _requirements(
        opening=TextRequirement(role="opening", text=TITLE, duration_s=2.0)
    )
    baseline, _, _ = _save(
        monkeypatch, requirements=requirements, texts=_title_edit(start_s=0.0, end_s=1.0)
    )
    saved = _saved_texts(baseline)
    assert ("any", TITLE, None) in saved
    assert not any(role == "opening" for role, _, _ in saved)


def test_a_reworded_and_moved_opening_title_is_required_as_the_new_words(monkeypatch):
    baseline, _, _ = _save(
        monkeypatch,
        requirements=_requirements(),
        texts=_title_edit(start_s=2.0, end_s=5.0, text="Lisbon, finally"),
    )
    saved = _saved_texts(baseline)
    assert ("any", "Lisbon, finally", None) in saved
    assert not any(text == TITLE for _, text, _ in saved)


def test_a_closing_title_moved_off_the_last_frame_saves(monkeypatch):
    requirements = _requirements(closing=TextRequirement(role="closing", text="Place 3"))

    def texts(variant, windows, total):
        lane = _phone_retimed(variant, windows, total)
        by_id = {t["id"]: t for t in lane}
        by_id["clip-label-cut-3"].update(start_s=windows["cut-3"][0], end_s=total - 3.0)
        return lane

    baseline, _, _ = _save(monkeypatch, requirements=requirements, texts=texts)
    saved = _saved_texts(baseline)
    assert ("any", "Place 3", None) in saved
    assert not any(role == "closing" for role, _, _ in saved)


def test_a_text_the_creator_deleted_is_dropped_not_refused(monkeypatch):
    def texts(variant, windows, total):
        return [t for t in _phone_retimed(variant, windows, total) if t["id"] != "clip-label-cut-2"]

    baseline, _, _ = _save(monkeypatch, requirements=_requirements(), texts=texts)
    assert all(text != "Place 2" for _, text, _ in _saved_texts(baseline))


def test_words_that_are_not_on_screen_are_still_refused(monkeypatch):
    """Manual edits win over WHERE a text sits, never over the words being shown at all."""
    requirements = _requirements(
        any_label=TextRequirement(role="any", text="Place 2"),
    )
    from app.services import phone_editor

    real = phone_editor._text_requirements_for_save

    def smuggle_a_required_text(*args, **kwargs):
        return (*real(*args, **kwargs), TextRequirement(role="any", text="never shown"))

    monkeypatch.setattr(phone_editor, "_text_requirements_for_save", smuggle_a_required_text)
    with pytest.raises(Exception) as raised:
        _save(
            monkeypatch,
            requirements=requirements,
            texts=lambda v, w, t: _phone_retimed(v, w, t),
        )
    detail = getattr(raised.value, "detail", {})
    assert detail["code"] == "unsupported_phone_edit"
    assert detail["field_path"]
    assert detail["decline_reason"]


def test_the_strict_verifier_still_refuses_a_misplaced_text_for_automatic_callers(monkeypatch):
    """Only the manual Save relaxes a role: the shared verifier keeps refusing it."""
    from app.services.creator_render_contract import (
        CreatorRenderContract,
        TextPlacementError,
        verify_phone_recipe,
    )

    baseline, _, _ = _save(
        monkeypatch,
        requirements=_requirements(),
        texts=_title_edit(start_s=3.0, end_s=6.0),
    )
    recipe = device_status(baseline, VARIANT).request.recipe
    strict = CreatorRenderContract.model_validate(
        baseline.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]
    ).rebind(exact_texts=_requirements())
    with pytest.raises(TextPlacementError) as raised:
        verify_phone_recipe(strict, recipe, source_audio={})
    assert raised.value.field_path == "opening_title"


def test_splitting_a_clip_keeps_a_required_order_satisfiable(monkeypatch):
    """A cut of one clip into two adjacent pieces is still the approved clip order."""
    job, variant, revision = _order_required_job(monkeypatch)
    rows = gj.editor_deletion_timeline(job, variant)
    first = rows[0]
    half = first["duration_s"] / 2
    slots = [
        gj.TimelineSlotEdit(
            slot_id=r["slot_id"],
            clip_index=r["clip_index"],
            in_s=r["in_s"],
            duration_s=half if r is first else r["duration_s"],
            transition_after="cut",
            transition_duration_s=None,
        )
        for r in rows
    ]
    slots.insert(
        1,
        gj.TimelineSlotEdit(
            slot_id=None,
            clip_index=first["clip_index"],
            in_s=first["in_s"] + half,
            duration_s=half,
            transition_after="cut",
            transition_duration_s=None,
        ),
    )
    baseline = stage_initial_authored_baseline(job, VARIANT, rows)
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=revision["revision_number"],
        timeline_slots=slots,
    )
    prep = prepare_authored_editor_commit(baseline, VARIANT, body, validation_arguments={})
    assert prep["authored_timeline"] is True
    clips = device_status(baseline, VARIANT).request.recipe.tracks[0].clips
    assert len(clips) == len(rows) + 1
    # Five pieces of four sources: the required order is the four SOURCES, in order.
    saved = baseline.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]
    assert saved["order_ids"] == ["m0", "m1", "m2", "m3"]


def test_a_manual_trim_moves_the_length_target_with_it(monkeypatch):
    baseline, _, total = _save(
        monkeypatch,
        requirements=_requirements(),
        texts=_title_edit(start_s=0.0, end_s=2.2),
        clip_s=3.0,
        duration_s=30.0,
    )
    contract = baseline.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]
    assert contract["duration_s"] == pytest.approx(total)


def _guided_save(
    monkeypatch,
    *,
    contract_changes,
    muted=None,
    title=None,
    requirements=None,
    labels=None,
    with_timeline=True,
):
    """A manual Save on the (non-authored) guided-story editor, via the real commit path."""
    from app.services.creator_render_contract import CreatorRenderContract

    job, variant, revision = _order_required_job(monkeypatch, order_required=False)
    contract = CreatorRenderContract.model_validate(job.assembly_plan[CONTRACT_FIELD]).rebind(
        exact_texts=requirements or _requirements(), **contract_changes
    )
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")
    rows = gj.editor_deletion_timeline(job, variant)
    lane = copy.deepcopy(variant["text_elements"])
    if title:
        next(t for t in lane if t["id"] == "guided-title").update(title)
    for text_id, window in (labels or {}).items():
        next(t for t in lane if t["id"] == text_id).update(window)
    body = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        editor_state_version=1,
        guided_revision_number=revision["revision_number"],
        timeline_slots=(
            [
                gj.TimelineSlotEdit(
                    slot_id=r["slot_id"],
                    clip_index=r["clip_index"],
                    in_s=r["in_s"],
                    duration_s=r["duration_s"],
                    transition_after="cut",
                    transition_duration_s=None,
                    muted=muted,
                )
                for r in rows
            ]
            if with_timeline
            else None
        ),
        text_elements=lane,
    )
    prep = gj.prepare_editor_commit(job, VARIANT, body)
    return job, prep


def test_a_moved_opening_title_saves_on_the_guided_editor_too(monkeypatch):
    job, _ = _guided_save(monkeypatch, contract_changes={}, title={"start_s": 3.0, "end_s": 5.0})
    saved = _saved_texts(job)
    assert ("any", TITLE, None) in saved
    assert not any(role == "opening" for role, _, _ in saved)


def test_a_shot_label_moved_out_of_its_shot_saves_and_stays_required(monkeypatch):
    requirements = _requirements(
        any_label=TextRequirement(role="clip", text="Place 2", media_id="m2", shot_index=2)
    )
    job, _ = _guided_save(
        monkeypatch,
        contract_changes={},
        requirements=requirements,
        labels={"clip-label-cut-2": {"start_s": 1.0, "end_s": 3.0}},
        with_timeline=False,
    )
    saved = _saved_texts(job)
    assert ("any", "Place 2", None) in saved
    assert not any(role == "clip" for role, _, _ in saved)


def test_muting_every_clip_clears_a_required_camera_sound(monkeypatch):
    job, prep = _guided_save(
        monkeypatch, contract_changes={"original_audio": "require"}, muted=True
    )
    assert job.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]["original_audio"] is None
    assert {"requirement": "original_audio"} in prep["requirements_relaxed"]


def test_unmuting_camera_sound_clears_a_forbidden_camera_sound(monkeypatch):
    job, _ = _guided_save(monkeypatch, contract_changes={"original_audio": "forbid"}, muted=False)
    assert job.assembly_plan[CONTRACT_REVISIONS_FIELD][VARIANT]["original_audio"] is None
