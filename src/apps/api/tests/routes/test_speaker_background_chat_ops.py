"""KRI-1117 (chat half): "put this photo behind me" through the copilot, end to end.

The whole path with nothing stubbed but the model's JSON, on the same phone Talking harness the
editor Save tests use: chat base -> snapshot -> parser -> compile -> the real editor Save -> the
recompiled phone recipe. Failure modes pinned here:

 1. an asset the item's pool never listed (another item's, made up, missing) reaches the Save;
 2. a cloud variant or a closed phone gate is offered the op, or refused without a reason (S18);
 3. a kill-switched server changes the snapshot the model sees, or teaches the op;
 4. "remove the background" is lost on the way (a null lane dropped by the draft's None filter),
    or is accepted when there is nothing to remove;
 5. turn 2 indexes the turn-1 draft wrongly (the background it just set is not "current");
 6. a refused Save speaks in the generic sentence instead of "Couldn't ..." with the reason, in
    either reply language.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.agents.edit_copilot import _family_allowed, _parse_op, _ParseState, _with_v2_fragments
from app.config import settings
from app.routes import generative_jobs as gj
from app.services.kria_editor_ops import (
    KriaEditorOpError,
    build_editor_snapshot,
    compile_editor_ops,
    editor_payload_dump,
    merge_editor_draft,
    project_editor_draft,
)
from tests.routes.test_phone_subtitled_editor_commit import device_status
from tests.routes.test_speaker_background_editor import (
    PHOTO,
    VIDEO,
    _background,
    background_job,
)

# The item's ready pool as the planner lists it (`planner._background_pool_rows`).
POOL = [
    {"id": PHOTO.media_id, "kind": "image", "label": "beach.jpg"},
    {"id": VIDEO.media_id, "kind": "video", "label": "waves.mp4", "duration_s": 9.0},
]
FOREIGN = "00000000-0000-4000-8000-0000000000ff"

# sha256 of the snapshot JSON of `background_job` with the kill switch OFF, computed on
# codex/sb-server before the background lane existed (sort_keys, default=str).
PRE_LANE_SNAPSHOT_SHA256 = "059c34bc8909f7cbde98b19dfed2ca85ed2dfa5fd774e68a3d29ac21c78a7e14"


def _job(monkeypatch, *, background=None):
    job = background_job(monkeypatch, background=background, pinned=(PHOTO, VIDEO))
    job.assembly_plan["variants"][0]["output_url"] = "https://storage.example/out.mp4"
    job.all_candidates = {}
    return job


def _base(job, payload=None):
    return project_editor_draft(job.assembly_plan["variants"][0], payload or {}, job)


def _snapshot(job, base=None, pool=POOL):
    return build_editor_snapshot(
        job, base or _base(job), clip_context={"background_pool": pool} if pool else {}
    )


def _parse(snapshot, *raw_ops):
    state = _ParseState(0.9)
    return [_parse_op(raw, snapshot, state) for raw in raw_ops], state


def _commit(job, compiled):
    variant = job.assembly_plan["variants"][0]
    payload = compiled.payload.model_copy(
        update={"base_generation": gj.variant_render_baseline(variant)}
    )
    return gj.prepare_editor_commit(job, "subtitled", payload, user_id="owner", plan_item_id="item")


def _recipe(job):
    return device_status(job, "subtitled").request.recipe


# ------------------------------------------------------------------ the happy path


def test_chat_puts_a_pool_photo_behind_the_speaker_and_saves(monkeypatch):
    job = _job(monkeypatch)
    base = _base(job)
    snapshot = _snapshot(job, base)
    assert "background" in snapshot["allowed_op_families"]
    assert snapshot["background"] == {"available": True, "current": None, "pool": POOL}

    (op,), _state = _parse(snapshot, {"op": "set_background", "asset_id": PHOTO.media_id})
    assert op == {
        "op": "set_background",
        "asset_id": PHOTO.media_id,
        "kind": "image",
        "source_start_s": 0.0,
    }
    compiled = compile_editor_ops(job, base, [op])
    assert compiled.changes == ["Put the photo behind you"]
    assert compiled.payload.speaker_background.model_dump() == {
        "media_id": PHOTO.media_id,
        "kind": "image",
        "source_start_s": 0.0,
    }
    assert "speaker_background" in compiled.payload.model_fields_set

    prep = _commit(job, compiled)
    assert prep["sections"]["speaker_background"] is True
    layer = _recipe(job).speaker_background
    assert layer is not None and layer.asset_id == f"visual-{PHOTO.media_id}"


def test_chat_can_pick_a_video_with_a_start_and_replace_a_photo(monkeypatch):
    job = _job(monkeypatch, background=_background(PHOTO))
    base = _base(job)
    snapshot = _snapshot(job, base)
    assert snapshot["background"]["current"] == {
        "media_id": PHOTO.media_id,
        "kind": "image",
        "source_start_s": 0.0,
    }
    (op,), _ = _parse(
        snapshot, {"op": "set_background", "asset_id": VIDEO.media_id, "source_start_s": 3}
    )
    compiled = compile_editor_ops(job, base, [op])
    assert compiled.changes == ["Put the video behind you"]
    _commit(job, compiled)
    layer = _recipe(job).speaker_background
    assert (layer.asset_id, layer.media_kind, layer.source_start) == (
        f"visual-{VIDEO.media_id}",
        "video",
        3.0,
    )


def test_a_photo_ignores_a_start_and_a_video_start_past_its_end_is_refused(monkeypatch):
    job = _job(monkeypatch)
    snapshot = _snapshot(job)
    (photo,), _ = _parse(
        snapshot, {"op": "set_background", "asset_id": PHOTO.media_id, "source_start_s": 4}
    )
    assert photo["source_start_s"] == 0.0
    (late,), state = _parse(
        snapshot, {"op": "set_background", "asset_id": VIDEO.media_id, "source_start_s": 9.5}
    )
    assert late is None
    assert state.rejection_reasons[-1]["reason"] == "invalid_value"


# ---------------------------------------------------------- 1. only this item's pool


@pytest.mark.parametrize("asset_id", [FOREIGN, "beach.jpg", "", 7, None])
def test_an_asset_the_pool_never_listed_is_refused_at_the_parser(monkeypatch, asset_id):
    job = _job(monkeypatch)
    (op,), state = _parse(_snapshot(job), {"op": "set_background", "asset_id": asset_id})
    assert op is None
    assert state.rejection_reasons[-1]["reason"] in {"invalid_value", "missing_required"}


def test_an_empty_pool_fails_closed(monkeypatch):
    job = _job(monkeypatch)
    snapshot = _snapshot(job, pool=[])
    assert snapshot["background"]["pool"] == []
    (op,), state = _parse(snapshot, {"op": "set_background", "asset_id": PHOTO.media_id})
    assert op is None and state.rejection_reasons[-1]["reason"] == "invalid_value"


def test_the_background_pool_never_opens_add_overlay(monkeypatch):
    job = _job(monkeypatch)
    snapshot = _snapshot(job)
    assert snapshot["overlays"]["asset_pool"] == []
    (op,), _state = _parse(
        snapshot, {"op": "add_overlay", "asset_id": PHOTO.media_id, "start_s": 1, "end_s": 3}
    )
    assert op is None


# ------------------------------------------------- 2. unavailable is honest (S18)


def test_a_cloud_variant_says_why_and_refuses_the_op(monkeypatch):
    job = _job(monkeypatch)
    job.assembly_plan["variants"][0]["render_destination"] = "cloud"
    snapshot = _snapshot(job)
    assert "background" not in snapshot["allowed_op_families"]
    assert snapshot["background"] == {
        "available": False,
        "reason": (
            "this edit is rendered in the cloud, so a photo or video can't go behind you here"
        ),
    }
    (op,), state = _parse(snapshot, {"op": "set_background", "asset_id": PHOTO.media_id})
    assert op is None
    assert state.rejection_reasons[-1]["reason"] == "capability_unavailable"


@pytest.mark.parametrize(
    ("close", "needle"),
    [
        ("token", "aren't ready on this iPhone app"),
        ("authored", "timeline was customised"),
    ],
)
def test_a_closed_phone_gate_or_an_authored_timeline_says_why(monkeypatch, close, needle):
    job = _job(monkeypatch)
    if close == "token":
        monkeypatch.setattr(
            settings,
            "phone_render_verified_features",
            [f for f in settings.phone_render_verified_features if f != "speakerBackground"],
        )
    else:
        job.assembly_plan["variants"][0]["editor_timeline_mode"] = "authored"
    snapshot = _snapshot(job)
    assert "background" not in snapshot["allowed_op_families"]
    assert snapshot["background"]["available"] is False
    assert needle in snapshot["background"]["reason"]
    (op,), state = _parse(snapshot, {"op": "set_background", "asset_id": PHOTO.media_id})
    assert op is None and state.rejection_reasons[-1]["reason"] == "capability_unavailable"


def test_a_server_that_has_not_turned_backgrounds_on_says_nothing(monkeypatch):
    job = _job(monkeypatch)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    snapshot = _snapshot(job)
    assert "background" not in snapshot
    assert "background" not in snapshot["allowed_op_families"]


def test_a_montage_variant_is_never_told_about_backgrounds(monkeypatch):
    job = _job(monkeypatch)
    job.assembly_plan["variants"][0]["resolved_archetype"] = "guided_story"
    assert "background" not in _snapshot(job)


# ------------------------------------------------------ 3. flag off is byte-identical


def test_flag_off_is_byte_identical_to_legacy(monkeypatch):
    job = _job(monkeypatch)
    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    snapshot = _snapshot(job)
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
    assert digest == PRE_LANE_SNAPSHOT_SHA256
    # The pool the planner would list changes nothing either.
    assert snapshot == _snapshot(job, pool=None)


def test_the_prompt_teaches_backgrounds_only_when_the_family_is_offered(monkeypatch):
    job = _job(monkeypatch)
    open_prompt = _with_v2_fragments("PROMPT", _snapshot(job))
    assert "BACKGROUND BEHIND THE SPEAKER" in open_prompt
    assert '"op":"set_background"' in open_prompt

    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    closed = _with_v2_fragments("PROMPT", _snapshot(job))
    assert "set_background" not in closed and "BACKGROUND" not in closed
    job.assembly_plan["variants"][0]["render_destination"] = "cloud"
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    assert "set_background" not in _with_v2_fragments("PROMPT", _snapshot(job))


def test_the_web_drawer_can_never_see_the_new_ops():
    drawer = {"allowed_op_families": ["background"], "background": {"pool": POOL}}
    assert not _family_allowed("set_background", drawer)
    assert not _family_allowed("remove_background", drawer)


# --------------------------------------------------------------- 4. remove_background


def test_chat_removes_the_background_and_the_clear_survives_the_draft(monkeypatch):
    job = _job(monkeypatch, background=_background(PHOTO))
    base = _base(job)
    (op,), _ = _parse(_snapshot(job, base), {"op": "remove_background"})
    compiled = compile_editor_ops(job, base, [op])
    assert compiled.changes == ["Removed the background behind you"]
    assert "speaker_background" in compiled.payload.model_fields_set
    assert compiled.payload.speaker_background is None

    # The draft dict the approval stores: a plain None-drop would erase the clear.
    assert "speaker_background" not in compiled.payload.model_dump(exclude_none=True)
    dumped = editor_payload_dump(compiled.payload)
    assert dumped["speaker_background"] is None
    merged = merge_editor_draft({"base_generation": "x"}, dumped)
    revalidated = gj.EditorCommitRequest.model_validate(merged)
    assert "speaker_background" in revalidated.model_fields_set

    _commit(job, compiled)
    assert _recipe(job).speaker_background is None


def test_removing_when_there_is_nothing_to_remove_is_a_plain_refusal(monkeypatch):
    job = _job(monkeypatch)
    (op,), state = _parse(_snapshot(job), {"op": "remove_background"})
    assert op is None
    assert state.rejection_reasons[-1]["reason"] == "invalid_value"


def test_an_untouched_lane_is_absent_not_a_clear(monkeypatch):
    job = _job(monkeypatch, background=_background(PHOTO))
    base = _base(job)
    snapshot = _snapshot(job, base)
    (op,), _ = _parse(snapshot, {"op": "set_caption_meta", "patch": {"enabled": False}})
    assert op is not None
    compiled = compile_editor_ops(job, base, [op])
    assert "speaker_background" not in compiled.payload.model_fields_set
    assert "speaker_background" not in editor_payload_dump(compiled.payload)


# --------------------------------------------------------- 5. turn 2 sees turn 1's draft


def test_turn_two_indexes_the_background_turn_one_drafted(monkeypatch):
    job = _job(monkeypatch)
    base = _base(job)
    (op,), _ = _parse(_snapshot(job, base), {"op": "set_background", "asset_id": PHOTO.media_id})
    first = editor_payload_dump(compile_editor_ops(job, base, [op]).payload)

    second_base = _base(job, first)
    snapshot = _snapshot(job, second_base)
    assert snapshot["background"]["current"]["media_id"] == PHOTO.media_id
    # Setting the same one again changes nothing, and says so.
    (again,), _ = _parse(snapshot, {"op": "set_background", "asset_id": PHOTO.media_id})
    with pytest.raises(KriaEditorOpError, match="already behind you"):
        compile_editor_ops(job, second_base, [again])

    (remove,), _ = _parse(snapshot, {"op": "remove_background"})
    cleared = editor_payload_dump(compile_editor_ops(job, second_base, [remove]).payload)
    third = _snapshot(job, _base(job, merge_editor_draft(first, cleared)))
    assert third["background"]["current"] is None


# ------------------------------------------------ 6. a refused Save is honest


def test_every_refusal_code_the_save_can_raise_has_creator_words_in_both_languages():
    from app.services.phone_speaker_background import BACKGROUND_REFUSALS

    root = Path(gj.__file__).resolve().parents[1]
    raised: set[str] = set()
    for relative in ("routes/generative_jobs.py", "services/phone_speaker_background.py"):
        raised |= set(
            re.findall(
                r'(?:"code": |_refuse\()"(speaker_background_[a-z_]+)"',
                (root / relative).read_text(),
            )
        )
    assert raised, "no refusal codes found: the scan is broken"
    assert raised <= set(BACKGROUND_REFUSALS), sorted(raised - set(BACKGROUND_REFUSALS))
    for en, tr in BACKGROUND_REFUSALS.values():
        assert en.startswith("Couldn't") and tr


def test_the_v1_chat_reply_for_a_refused_background_says_couldnt_with_the_reason():
    from app.services import creation_editor_actions as actions

    reply = actions._rejection_reply(
        HTTPException(422, detail={"code": "speaker_background_media_not_in_pool"})
    )
    assert reply.startswith("Couldn't use that photo or video: it isn't in this video's Visuals")
    assert reply.endswith("Nothing was changed.")
    unknown = actions._rejection_reply(HTTPException(422, detail={"code": "something_else"}))
    assert not unknown.startswith("Couldn't use that photo")


def test_the_v2_approval_reply_for_a_refused_background_says_why():
    from app.tasks.kria_runtime import _device_edit_refusal_copy

    text = _device_edit_refusal_copy("speaker_background_disabled")
    assert text == (
        "Couldn't put a photo or video behind you: it isn't available right now, "
        "so I left the video as it was."
    )


def test_the_chat_op_names_read_as_plain_phrases_in_a_not_done_line():
    from app.services import creation_editor_actions as actions

    lines = actions._not_done_lines(
        [{"op": "set_background", "reason": "capability_unavailable"}], []
    )
    assert lines == [
        "Not done: put that behind you (that isn't something I can change in this edit yet)."
    ]


# ------------------------------------- which chat Saves pin a pool visual first (S19)


def _set_payload(**fields):
    return gj.EditorCommitRequest(base_generation="first", **fields)


def test_only_a_save_that_sets_a_background_on_a_variant_that_can_carry_one_is_pinned(monkeypatch):
    from app.services.phone_speaker_background import background_pin_request

    job = _job(monkeypatch)
    sets = _set_payload(speaker_background={"media_id": PHOTO.media_id, "kind": "image"})
    assert background_pin_request(job, "subtitled", sets) == (PHOTO.media_id, "image")
    assert background_pin_request(job, "subtitled", _set_payload(speaker_background=None)) is None
    assert background_pin_request(job, "subtitled", _set_payload()) is None

    monkeypatch.setattr(settings, "speaker_background_enabled", False)
    assert background_pin_request(job, "subtitled", sets) is None
    monkeypatch.setattr(settings, "speaker_background_enabled", True)
    job.assembly_plan["variants"][0]["render_destination"] = "cloud"
    assert background_pin_request(job, "subtitled", sets) is None
