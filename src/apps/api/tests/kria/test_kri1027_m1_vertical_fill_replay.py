"""KRI-1027 replay: "make it all vertical" must not letterbox the sideways clips.

M1 "maximo 3" (2026-10-28): the creator wrote "Some of the clips are horizontal and some
vertical, make it all vertical"; the brief kept it as r2 (``style``, no facts). The thread's
``render_shape`` preselected the item's remembered "fit", the app echoed that default back
with the approval (`VideoShapePicker.wire(for:)` sends the server's default), the job carried
``creator_render_shape = {"landscape_fit": "fit"}`` and the phone letterboxed the three
sideways clips (casa rosada, the 1 s square, the bus selfie: 7.8 s of bars).

This drives the real boundaries with the run's own data (`tests/fixtures/
kri1027_m1_vertical_fill.json`): the thread projection the phone reads -> the approve
validation the phone's echo goes through -> the worker's bars/crop -> the phone compiler.
Only the database transport is replayed (one row read).
"""

from __future__ import annotations

import copy
import json
import pathlib
import types
import uuid

import pytest

from app.config import settings
from app.kria.api_schemas import ApprovalDecisionBody
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.runtime import _validated_render_shape
from app.pipeline.guided_story import GuidedStoryExecutionPlan
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.routes.creation_threads import _render_shape_projection
from app.services.phone_sources import PhoneSourceBinding
from app.tasks.generative_build import _creator_landscape_fit

FIXTURE = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[1] / "fixtures" / "kri1027_m1_vertical_fill.json"
    ).read_text()
)
SIDEWAYS = {
    media_id
    for media_id, original in FIXTURE["clips"].items()
    if original["width"] > original["height"]
}
_BAR_SCALE = 1080 / 1920 * 1080 / 1920  # contain / cover for 16:9 into 9:16


class _Rows:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def scalar_one_or_none(self):
        return self._snapshot


class _ReplayDB:
    """The one read `_render_shape_projection` makes: the pending approval's draft."""

    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def execute(self, _statement):
        return _Rows(self.snapshot)

    async def get(self, *_args):  # no current job on a first render
        return None


def _snapshot_with_r2(description: str | None, facts: dict | None = None) -> dict:
    """The approved draft with r2 reworded / re-recorded (None drops it), re-pinned."""
    snapshot = copy.deepcopy(FIXTURE["draft_snapshot"])
    binding = snapshot["brief_binding"]
    brief = copy.deepcopy(binding["brief"])
    requirements = [req for req in brief["requirements"] if req["id"] != "r2" or description]
    for req in requirements:
        if req["id"] == "r2":
            req["description"] = description
            req["facts"] = dict(facts or {})
    brief["requirements"] = requirements
    snapshot["brief_binding"] = BriefBinding.create(
        binding["thread_id"],
        CreativeBrief.model_validate(brief),
        full_creator_request=binding["creator_request"],
        media_snapshot=binding["media_snapshot"],
    ).model_dump(mode="json")
    return snapshot


# M1's r2 as the shared brief section teaches it from Main Creator v103 (Brief Extractor
# v42): the creator's words, plus the meaning the model records. Prod stored it on v100,
# where nothing taught the fact, as {"description": "make it all vertical", "facts": {}}.
M1_R2 = "make it all vertical"
M1_RECORDED = _snapshot_with_r2(M1_R2, {"landscape_fit": "fill"})


def _item():
    assignments = [
        {"upload_contract": {"proxy": {"original": original}}}
        for original in FIXTURE["clips"].values()
    ]
    return types.SimpleNamespace(
        landscape_fit="fit",  # PlanItem.landscape_fit server_default
        clip_assignments=assignments,
        current_job_id=None,
        voiceover_gcs_path=None,
    )


def _bindings() -> tuple[PhoneSourceBinding, ...]:
    plan = FIXTURE["guided_story_execution_plan"]
    by_media = {moment["media_id"]: moment for moment in plan["story_timeline"]}
    return tuple(
        PhoneSourceBinding(
            media_id=media_id,
            proxy_path=by_media[media_id]["gcs_path"],
            generation=by_media[media_id]["generation"],
            original=OriginalMediaDescriptor(
                **{
                    key: original[key]
                    for key in (
                        "sha256",
                        "byte_count",
                        "duration_s",
                        "width",
                        "height",
                        "has_audio",
                        "orientation_degrees",
                    )
                }
            ),
        )
        for media_id, original in FIXTURE["clips"].items()
    )


@pytest.fixture(autouse=True)
def _phone_creator(monkeypatch):
    monkeypatch.setattr(settings, "ios_native_device_only_enabled", False, raising=False)
    monkeypatch.setattr(type(settings), "phone_rendering_for", lambda _self, _creator: True)
    monkeypatch.setenv("LANDSCAPE_OUTPUT_ENABLED", "false")


async def _approve_like_the_phone(snapshot: dict) -> dict[str, str]:
    """Thread projection -> the app echoes its default -> approve validation -> stash."""
    item = _item()
    thread = types.SimpleNamespace(
        id=uuid.UUID(snapshot["brief_binding"]["thread_id"]),
        creator_id=uuid.uuid4(),
        runtime_version=2,
    )
    db = _ReplayDB(snapshot)
    offered = await _render_shape_projection(db, thread, item, None)
    assert offered is not None and offered["fit_choices"] == ["fit", "fill"]
    default = offered["default"]
    # `RenderShapeOffer.wire(for:)`: orientation only when offered, the fit row on portrait.
    body = ApprovalDecisionBody(
        expected_thread_revision=1,
        expected_draft_revision=1,
        expected_approval_fingerprint="0" * 64,
        output_orientation=(
            default["output_orientation"] if len(offered["orientations"]) > 1 else None
        ),
        landscape_fit=default["landscape_fit"],
    )
    return await _validated_render_shape(
        db,
        body=body,
        plan_item=item,
        strategy_payload=snapshot["strategy"],
        creator_id=thread.creator_id,
        creator_request=snapshot["intent"],
        # As `decide_approval` passes it: the offer the app echoed came from the same brief.
        brief_binding=snapshot.get("brief_binding"),
    )


def _story_clips(landscape_fit: str):
    plan = GuidedStoryExecutionPlan.model_validate(FIXTURE["guided_story_execution_plan"])
    recipe = compile_phone_guided_plan(plan, _bindings(), landscape_fit=landscape_fit)
    return {
        clip.source_asset_id: clip
        for clip in next(t for t in recipe.tracks if t.id == "story").clips
    }


def test_the_replay_reproduces_the_letterboxed_render():
    """Sanity: the run's own shape compiles to the bars the render showed on exactly the
    three sideways clips (and nothing else)."""
    observed = FIXTURE["observed_on_prod"]["creator_render_shape"]
    assert observed == {"landscape_fit": "fit", "output_orientation": "portrait"}
    clips = _story_clips(_creator_landscape_fit({"creator_render_shape": observed}))
    assert len(SIDEWAYS) == 3
    for media_id, clip in clips.items():
        expected = pytest.approx(_BAR_SCALE) if media_id in SIDEWAYS else 1
        assert clip.transform.scale == expected


async def test_make_it_all_vertical_reaches_the_render_as_crop():
    # The phone echoes the preselected shape untouched: the plan must not call it a pick.
    stash = await _approve_like_the_phone(M1_RECORDED)
    assert stash == {"output_orientation": "portrait", "landscape_fit": "fill", "origin": "default"}
    clips = _story_clips(_creator_landscape_fit({"creator_render_shape": stash}))
    for media_id in SIDEWAYS:
        assert clips[media_id].transform.scale == 1, media_id  # no fit transform: no bars


async def test_keep_the_black_bars_still_letterboxes():
    snapshot = _snapshot_with_r2(
        "Keep the black bars on the horizontal clips", {"landscape_fit": "fit"}
    )
    stash = await _approve_like_the_phone(snapshot)
    assert stash == {"output_orientation": "portrait", "landscape_fit": "fit", "origin": "default"}
    clips = _story_clips(_creator_landscape_fit({"creator_render_shape": stash}))
    for media_id in SIDEWAYS:
        assert clips[media_id].transform.scale == pytest.approx(_BAR_SCALE), media_id


async def test_no_ask_keeps_todays_default():
    stash = await _approve_like_the_phone(_snapshot_with_r2(None))
    assert stash == {"output_orientation": "portrait", "landscape_fit": "fit", "origin": "default"}


async def test_r2_exactly_as_prod_stored_it_is_not_guessed_from_words():
    """KRI-667: no fact, and words the KRI-547 reader does not know -> nothing is read
    into them; the confirm screen keeps today's default (the re-send records the fact)."""
    assert FIXTURE["draft_snapshot"]["brief_binding"]["brief"]["requirements"][1] == {
        **FIXTURE["draft_snapshot"]["brief_binding"]["brief"]["requirements"][1],
        "id": "r2",
        "description": M1_R2,
        "facts": {},
    }
    stash = await _approve_like_the_phone(FIXTURE["draft_snapshot"])
    assert stash == {"output_orientation": "portrait", "landscape_fit": "fit", "origin": "default"}


async def test_the_kri547_vertical_words_need_no_fact():
    """The vertical / no-bars reader Talking already applies (KRI-547) also preselects crop."""
    stash = await _approve_like_the_phone(_snapshot_with_r2("make it vertical, no black bars"))
    assert stash == {"output_orientation": "portrait", "landscape_fit": "fill", "origin": "default"}


async def test_a_creator_who_flips_the_picker_to_bars_keeps_bars():
    """The chat ask only moves the default; an explicit pick on the confirm screen wins."""
    snapshot = M1_RECORDED
    item = _item()
    stash = await _validated_render_shape(
        _ReplayDB(snapshot),
        body=ApprovalDecisionBody(
            expected_thread_revision=1,
            expected_draft_revision=1,
            expected_approval_fingerprint="0" * 64,
            landscape_fit="fit",
        ),
        plan_item=item,
        strategy_payload=snapshot["strategy"],
        creator_id=uuid.uuid4(),
        creator_request=snapshot["intent"],
    )
    assert stash == {"output_orientation": "portrait", "landscape_fit": "fit", "origin": "default"}


async def test_a_talking_edit_keeps_its_preselection():
    """Talking frames its speaker from the KRI-547 words and falls back to the item's bars
    when no crop holds the face: the montage preselection never applies to it."""
    snapshot = copy.deepcopy(M1_RECORDED)
    snapshot["strategy"] = {**snapshot["strategy"], "edit_format": "subtitled"}
    item = _item()
    thread = types.SimpleNamespace(
        id=uuid.UUID(snapshot["brief_binding"]["thread_id"]),
        creator_id=uuid.uuid4(),
        runtime_version=2,
    )
    offered = await _render_shape_projection(_ReplayDB(snapshot), thread, item, None)
    assert offered is not None and offered["fit_choices"] == ["fit", "fill"]
    assert offered["default"]["landscape_fit"] == "fit"
