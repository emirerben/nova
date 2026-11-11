"""KRI-1332: a title linked to its clip follows that clip through chat edits, and "match the
sports with the videos again" puts it back instead of deleting it.

Thread 1894f8d0 (2026-11, phone montage, runtime v2): after the first render drew a title at
the start of each event, the creator asked for a 60 s video (``set_total_duration``, 50 clips
retimed), then 0.5 s for six clips (``patch_slots``). Every title stayed at its old second while
the clips under it moved. The creator asked "match the sports with the videos again" and the
planner deleted every title (``remove_texts`` ``group: free``): nothing marked them as bound to
a clip, so ``realign_labels`` had nothing to act on.

Replayed on the titles PR #2241 (KRI-1331) draws for that prod request: the fixture's timeline,
strategy and composer operations through ``compile_phone_guided_repaired``, so every title
carries the clip link the repair records (``source_params.source_asset_id`` /
``source_timeline_id``). The edits go through the copilot parser and ``compile_editor_ops`` on
the authored phone variant; each draft is projected with ``project_editor_draft``, the way the
next turn's base is built. Clip windows are recomputed here from the slot durations (every cut
in this montage is a plain cut), not read from the compiler.

Ways this could fail, written before the code:
  1. a linked title keeps its old seconds after ``set_total_duration`` while its clip moves;
  2. the same after a duration-only ``patch_slots`` that shrinks clips before and under it;
  3. a title that ran on past its first clip collapses to that clip, or runs past its event
     or past the end of the video once the clips shrink;
  4. a creator's own text with no clip link starts moving on a duration edit (independent text
     keeps its absolute time);
  5. several titles on one clip (prod's own resolution, KRI-1330): one follows, another stays;
  6. the planner still sees the titles as free text, so "match them with the videos" can only
     delete them and ``realign_labels`` finds nothing;
  7. ``realign_labels`` cannot read clip windows on an authored phone timeline (its slots carry
     no output times) and says everything already lines up;
  8. realign squeezes a title that sits correctly at its clip's start back into one clip, or
     moves a title onto the next event;
  9. legacy ``clip-label-*`` / ``montage-text-*`` rows change behaviour (pinned by
     ``test_kria_editor_guided_timeline.py``, ``test_kria_editor_realign_labels.py`` and the
     iPhone parity vectors in ``test_guided_label_rebase_vectors.py``).
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from typing import Any, get_args

import pytest

from app.agents.edit_copilot import _format_snapshot, _parse_op, _ParseState
from app.config import settings
from app.kria.recipes import MediaCapability
from app.services.kria_editor_ops import (
    build_editor_snapshot,
    compile_editor_ops,
    editor_payload_dump,
    merge_editor_draft,
    project_editor_draft,
)
from tests.tasks.test_kri1331_event_titles_once import (
    EVENTS,
    FIXTURE,
    TIMELINE,
    _composed,
    _composer_adds,
    _event_group_titles,
    _event_members,
    _planner_text,
    _render,
    _strategy,
)

FRAME = 1 / 30
SIXTY_SECONDS = {"op": "set_total_duration", "strategy": "proportional", "target_s": 60.0}
# The incident's second edit: 0.5 s for six clips, two of them the first football clips.
HALF_SECOND_CLIPS = {
    "op": "patch_slots",
    "patch": {"duration_s": 0.5},
    "selector": {"slot_indexes": [6, 7, 9, 10, 11, 14]},
}


@pytest.fixture(autouse=True)
def _verified(monkeypatch):
    monkeypatch.setattr(settings, "phone_font_qualification_strict", False)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(get_args(MediaCapability)))


def _incident_plan():
    """PR #2241's render of the prod request: the composer's titles moved to each event."""
    titles = _event_group_titles()
    operations = [
        {
            "op": "remove_texts",
            "selector": {"group": "free"},
            "target_ids": [row["id"] for row in titles],
            "expected_count": len(titles),
        },
        *_composer_adds(),
    ]
    return _render(_strategy(_event_members()), _composed(titles, operations)).plan


def _phone_variant(plan, *extra_texts: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """The rendered phone montage as the iPhone editor saves it (an authored timeline)."""
    seconds = {media["media_id"]: media["duration_s"] for media in FIXTURE["media"]}
    variant = {
        "variant_id": "original_text",
        "resolved_archetype": "guided_story",
        "render_destination": "device",
        "editor_timeline_mode": "authored",
        "render_generation_id": "first",
        "render_status": "ready",
        "text_mode": "agent_text",
        "user_timeline": {
            "slots": [
                {
                    "slot_id": moment.moment_id,
                    "clip_index": index,
                    "media_id": moment.media_id,
                    "media_kind": "video",
                    "in_s": moment.source_start_s,
                    "duration_s": moment.duration_s,
                    "source_duration_s": seconds[moment.media_id],
                    "removed": False,
                    "transition_after": "cut",
                }
                for index, moment in enumerate(plan.story_timeline)
            ]
        },
        "story_timeline": [moment.model_dump(mode="json") for moment in plan.story_timeline],
        "text_elements": [
            *(e.model_dump(mode="json", exclude_none=True) for e in plan.text_elements),
            *extra_texts,
        ],
    }
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={"variants": [variant]}, all_candidates={})
    return job, variant


def _parse(job, variant, op: dict, utterance: str) -> tuple[dict | None, _ParseState]:
    state = _ParseState(0.9)
    state.utterance = utterance
    return _parse_op(op, build_editor_snapshot(job, variant), state), state


def _chat(job, saved, steps, *, preserve_text: bool = False) -> dict[str, Any]:
    """Each chat edit drafted on the last one, as the runtime builds the next turn's base."""
    draft: dict[str, Any] = {}
    base = saved
    for op, utterance in steps:
        parsed, state = _parse(job, base, op, utterance)
        assert parsed is not None, state.rejection_reasons
        compiled = compile_editor_ops(job, base, [parsed], preserve_text=preserve_text)
        draft = merge_editor_draft(draft, editor_payload_dump(compiled.payload))
        base = project_editor_draft(saved, draft, job)
    return base


def _clip_windows(variant) -> dict[str, tuple[float, float]]:
    slots = [s for s in variant["user_timeline"]["slots"] if not s.get("removed")]
    assert {s.get("transition_after") or "cut" for s in slots} == {"cut"}
    windows, cursor = {}, 0.0
    for slot in slots:
        windows[slot["slot_id"]] = (cursor, cursor + float(slot["duration_s"]))
        cursor += float(slot["duration_s"])
    return windows


def _texts(variant) -> dict[str, dict[str, Any]]:
    return {row["text"]: row for row in variant["text_elements"] if not row.get("removed")}


def _assert_on_its_event(variant, events: dict[str, range | list[int]]) -> None:
    """Each title starts on its event's first clip and stays within the event and the video."""
    windows = _clip_windows(variant)
    total = max(end for _, end in windows.values())
    rows = _texts(variant)
    for text, indexes in events.items():
        first = windows[TIMELINE[indexes[0]]["moment_id"]]
        run_end = windows[TIMELINE[indexes[-1]]["moment_id"]][1]
        row = rows[text]
        assert row["start_s"] == pytest.approx(first[0], abs=FRAME), (text, row, first)
        assert first[0] + 0.2 <= row["end_s"] <= min(run_end, total) + FRAME, (text, row, run_end)


def _ran_on(variant) -> set[str]:
    """The titles that run on past their first clip (a chapter title, not a one-clip label)."""
    windows = _clip_windows(variant)
    rows = _texts(variant)
    return {
        text
        for text, indexes in EVENTS.items()
        if rows[text]["end_s"] > windows[TIMELINE[indexes[0]]["moment_id"]][1] + FRAME
    }


def test_incident_titles_follow_their_clips_through_both_duration_edits():
    job, saved = _phone_variant(_incident_plan())
    ran_on = _ran_on(saved)
    assert ran_on, "the replay must include titles that run on past their first clip"
    _assert_on_its_event(saved, EVENTS)

    sixty = _chat(job, saved, [(SIXTY_SECONDS, "make it 60 seconds")])
    _assert_on_its_event(sixty, EVENTS)
    assert _ran_on(sixty) == ran_on

    shorter = _chat(
        job,
        saved,
        [(SIXTY_SECONDS, "make it 60 seconds"), (HALF_SECOND_CLIPS, "make them 0.5 seconds")],
    )
    windows = _clip_windows(shorter)
    football = windows[TIMELINE[EVENTS["Football"][0]]["moment_id"]]
    assert football[1] - football[0] == pytest.approx(0.5)
    _assert_on_its_event(shorter, EVENTS)
    assert _ran_on(shorter) == ran_on
    title = _texts(shorter)["Emir Olympics London Edition"]
    assert (title["start_s"], title["end_s"]) == (0.0, 2.2)


def test_a_creators_own_text_without_a_clip_link_keeps_its_seconds():
    note = {
        "id": "kria-creator-note",
        "text": "best weekend",
        "start_s": 30.0,
        "end_s": 31.5,
        "role": "generative_intro",
    }
    job, saved = _phone_variant(_incident_plan(), note)
    edited = _chat(job, saved, [(SIXTY_SECONDS, "make it 60 seconds")])
    row = _texts(edited)["best weekend"]
    assert (row["start_s"], row["end_s"]) == (30.0, 31.5)
    _assert_on_its_event(edited, EVENTS)


def test_a_linked_title_the_creator_moved_off_its_clip_keeps_its_seconds():
    """The link names the clip the title was put on; a title dragged elsewhere is the
    creator's own placement and must not be pulled back by a duration edit."""
    job, saved = _phone_variant(_incident_plan())
    row = next(r for r in saved["text_elements"] if r["text"] == "dodgeball")
    row["start_s"], row["end_s"] = 30.0, 31.5
    edited = _chat(job, saved, [(SIXTY_SECONDS, "make it 60 seconds")])
    moved = _texts(edited)["dodgeball"]
    assert (moved["start_s"], moved["end_s"]) == (30.0, 31.5)
    _assert_on_its_event(edited, {k: v for k, v in EVENTS.items() if k != "dodgeball"})


def test_the_event_check_refuses_a_title_on_the_wrong_clip():
    """Wrong-content control: the same words and length on the event's second clip, or on
    the next event's first clip, fail the check every other test relies on."""
    job, saved = _phone_variant(_incident_plan())
    edited = _chat(job, saved, [(SIXTY_SECONDS, "make it 60 seconds")])
    windows = _clip_windows(edited)
    for text, clip in (
        ("beach volleyball", EVENTS["beach volleyball"][1]),
        ("Football", EVENTS["dodgeball"][0]),
    ):
        wrong = copy.deepcopy(edited)
        row = _texts(wrong)[text]
        length = row["end_s"] - row["start_s"]
        row["start_s"] = windows[TIMELINE[clip]["moment_id"]][0]
        row["end_s"] = row["start_s"] + length
        with pytest.raises(AssertionError):
            _assert_on_its_event(wrong, EVENTS)


def test_several_titles_on_one_clip_all_follow_it():
    """Prod's own resolution (KRI-1330): Football, dodgeball and pub all on the clip at 34."""
    planner = [
        _planner_text("group-title-caption_beach_volleyball-1", "beach volleyball", range(17, 31)),
        _planner_text("clip-label-unified-cut-35", "Football", range(34, 35)),
    ]
    plan = _render(_strategy(), _composed(planner, FIXTURE["composer_operations"])).plan
    job, saved = _phone_variant(plan)
    events = {"Football": [34], "dodgeball": [34], "pub": [34], "beach volleyball": range(17, 31)}
    _assert_on_its_event(saved, events)

    edited = _chat(job, saved, [(SIXTY_SECONDS, "make it 60 seconds")])
    _assert_on_its_event(edited, events)


def _drifted():
    """The incident's state: both edits applied while every title kept its old seconds."""
    job, saved = _phone_variant(_incident_plan())
    drifted = _chat(
        job,
        saved,
        [(SIXTY_SECONDS, "make it 60 seconds"), (HALF_SECOND_CLIPS, "make them 0.5 seconds")],
        preserve_text=True,
    )
    windows = _clip_windows(drifted)
    for text, indexes in EVENTS.items():
        first = windows[TIMELINE[indexes[0]]["moment_id"]]
        assert _texts(drifted)[text]["start_s"] > first[1], text
    return job, drifted


def test_the_planner_sees_each_title_bound_to_its_first_clip():
    job, drifted = _drifted()
    lines = _format_snapshot(build_editor_snapshot(job, drifted)).splitlines()
    for text, indexes in EVENTS.items():
        [line] = [line for line in lines if f"text={text!r}" in line]
        assert "selector_group='labels'" in line, line
        assert f"label_for_clip={TIMELINE[indexes[0]]['media_id']!r}" in line, line


def test_match_the_sports_with_the_videos_again_reattaches_every_title():
    job, drifted = _drifted()
    ask = "match the sports with the videos again"

    removed, _state = _parse(
        job, drifted, {"op": "remove_texts", "selector": {"group": "free"}}, ask
    )
    assert removed is None

    parsed, state = _parse(job, drifted, {"op": "realign_labels"}, ask)
    assert parsed is not None, (state.no_effect_clarifications, state.selector_clarification)
    titles = {row["id"] for row in drifted["text_elements"] if row["text"] in EVENTS}
    assert set(parsed["target_ids"]) == titles

    compiled = compile_editor_ops(job, drifted, [parsed])
    realigned = project_editor_draft(drifted, editor_payload_dump(compiled.payload), job)
    _assert_on_its_event(realigned, EVENTS)
    assert {row["id"] for row in compiled.payload.text_elements} == {
        row["id"] for row in drifted["text_elements"]
    }


def test_realign_leaves_titles_that_already_sit_on_their_clips():
    job, saved = _phone_variant(_incident_plan())
    parsed, state = _parse(job, copy.deepcopy(saved), {"op": "realign_labels"}, "fix the titles")
    assert parsed is None
    assert any("already line up" in message for message in state.no_effect_clarifications)


def test_a_reorder_moves_a_title_with_its_clip_without_stretching_it(monkeypatch):
    """A guided story re-times its text on every timeline edit: moving a title's first clip
    to the front must not stretch the title back to the footage it ran on over."""
    from tests.services._guided_timeline_fixtures import arm_guided, guided_job, guided_revision

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    title = {
        "id": "kria-day-two",
        "text": "Day two",
        "start_s": 2.0,
        "end_s": 5.0,
        "role": "generative_intro",
        "source_params": {"source_asset_id": "m1", "source_timeline_id": "s2"},
    }
    job, variant = guided_job(revision, [title], job_id)
    arm_guided(monkeypatch, revision)

    compiled = compile_editor_ops(
        job, variant, [{"op": "reorder_clip", "from_index": 1, "to_index": 0}]
    )
    [row] = compiled.payload.text_elements
    assert row["start_s"] == pytest.approx(0.0, abs=FRAME)
    assert 2.0 - FRAME <= row["end_s"] <= 3.0 + FRAME
