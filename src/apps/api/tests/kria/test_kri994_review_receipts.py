"""KRI-994: the finished video's review lists asks as "have a look" that the server can settle.

Prod thread 4C536AFE (Don Julio lunch M2, 2026-10-27, job 26556f5a). The brief held
r3 "Skip the clip where he cuts the cheese ...", r4 "Use my steak photo too" and r8
"Make the montage from the video clips only (no photos)". The creator tapped the cheese
clip out of the picker (`exclude_cheese_cut`, evidence "creator selected"), later wrote
"Forget the steak photo. Make the montage from the video clips only.", and the render
holds three video clips, none of them the cheese one. The review still read:

    Have a look at these in the video: ... Skip the clip where he cuts the cheese ...
    Use my steak photo too ... Make the montage from the video clips only (no photos)

Failure modes pinned here (the honest side of each passes today and must keep passing):

* a tapped exclusion whose clip the render still plays (a pin or the strategy's own placement
  keeps a clip an exclude names) is NOT met: the finished timeline is the proof, not the tap;
* no resolved exclude at all: nothing proves the ask;
* a photo that IS in the timeline never lets "no photos" read as met;
* a later ask that only partly overlaps an earlier one ("also use the steak photo, the rest
  video") must not withdraw it;
* asks no checker can judge (r2, r6) stay listed and never claim met.

Not pinned yet (fix design): a Turkish exclusion / withdrawal wording must not depend on an
English word list (KRI-667 ratchet).

The review is composed by ``_approved_generation_requirements_review``; this drives the real
function with the recorded brief, plan record, finished variant and strategy.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.services.device_render import DEVICE_RENDER_FIELD
from app.tasks import kria_runtime

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri994_don_julio_m2_review.json"
_THREAD = uuid.UUID("4c536afe-60fd-41a5-be8b-a8e1425fa8b3")
_CHEESE = "analysis-proxy-ios-98E88D32-BFD6-45DD-8FB4-99389BB0CEB0.mp4"
_NO_CHECKER = "I can't verify this one automatically yet."


def _load() -> dict:
    return json.loads(_FIXTURE.read_text())


def _review(
    data: dict | None = None,
    *,
    brief: CreativeBrief | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, dict[str, dict]]:
    """The real review text + receipts the chat would post for the recorded finished video."""
    data = data or _load()
    brief = brief or CreativeBrief.model_validate({**data["brief"]})
    variant = copy.deepcopy(data["variant"])
    record = copy.deepcopy(data["unified_montage"])
    generation = str(record["generation_id"])
    # A published phone export carries the phone's attempt id; the plan's generation is read
    # off its device record (`_plan_record_generations`).
    variant["render_generation_id"] = "29d59ee4-271a-4571-8167-00414f7b385b"
    job = SimpleNamespace(
        id=uuid.UUID("26556f5a-cf2a-4ec2-8e4c-321fc84f07f3"),
        assembly_plan={
            "unified_montage": record,
            "variants": [variant],
            DEVICE_RENDER_FIELD: {
                variant["variant_id"]: {
                    "published_attempt": variant["render_generation_id"],
                    "requirement_generation": generation,
                }
            },
        },
        all_candidates={"creator_strategy": copy.deepcopy(data["creator_strategy"])},
    )
    binding = BriefBinding.create(_THREAD, brief)
    execution = SimpleNamespace(
        result={"brief_binding": binding.model_dump(mode="json")}, target_draft_id=None
    )
    thread = SimpleNamespace(id=_THREAD)
    monkeypatch.setattr(kria_runtime, "_source_fingerprints", lambda _job: {})
    monkeypatch.setattr(kria_runtime, "_with_clip_sound_toggles", lambda _j, _v, facts, **_: facts)
    text, receipts = kria_runtime._approved_generation_requirements_review(
        None, thread, job, variant, execution, "The confirmed edit is ready."
    )
    return text, {row["requirement_id"]: row for row in receipts}


def _with_timeline_clip(data: dict, media_id: str) -> dict:
    """The recorded video with ``media_id`` added as one more cut (a clip that re-appears)."""
    data = copy.deepcopy(data)
    row = copy.deepcopy(data["variant"]["story_timeline"][0])
    row.update(media_id=media_id, beat_id="unified-cut-4", moment_id="unified-cut-4")
    data["variant"]["story_timeline"].append(row)
    data["unified_montage"]["clip_ids"].append(media_id)
    return data


def test_the_recorded_review_is_what_the_creator_saw(monkeypatch):
    """Control: today's composition reproduces the recorded review (the bug is real)."""
    data = _load()
    text, receipts = _review(data, monkeypatch=monkeypatch)

    assert text == data["recorded_review"]["content"]
    assert receipts["r3"]["verification"] == "unchecked"
    assert receipts["r4"]["verification"] == "unchecked"
    assert receipts["r8"]["verification"] == "unchecked"


def test_r3_a_tapped_exclusion_the_timeline_honours_is_met(monkeypatch):
    """KRI-994 (1): the creator's picker tap left the cheese clip out and the render has none
    of its footage; the server holds both facts, so the ask is checked and met."""
    data = _load()
    assert data["picker_answer"]["answers"][0]["media_ids"] == [_CHEESE]
    assert _CHEESE not in [row["media_id"] for row in data["variant"]["story_timeline"]]

    text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["verification"] == "checked"
    assert receipts["r3"]["status"] == "met"
    assert "Done: Skip the clip where he cuts the cheese" in text
    assert "- Skip the clip where he cuts the cheese" not in text.split("Have a look")[-1]


def test_r3_stays_honest_when_the_excluded_clip_is_in_the_video(monkeypatch):
    """A pin / placement can keep a clip an exclude names (`clip_scope.excluded_clips`): the
    tap alone is never the proof, the finished timeline is."""
    data = _with_timeline_clip(_load(), _CHEESE)

    _text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["status"] != "met"


def test_r3_is_not_met_without_a_creator_exclusion(monkeypatch):
    """No resolved exclude at all (the planner never recorded one): nothing proves the ask."""
    data = _load()
    data["creator_strategy"]["resolved_clip_intents"] = [
        row for row in data["creator_strategy"]["resolved_clip_intents"] if row["op"] != "exclude"
    ]

    _text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r3"]["verification"] == "unchecked"
    assert receipts["r3"]["status"] != "met"


def test_r4_a_photo_ask_the_creator_withdrew_is_not_something_to_check(monkeypatch):
    """KRI-994 (2): "Forget the steak photo" (r8) withdrew r4, yet the review lists "Use my
    steak photo too" under "Have a look". r5 shows the ledger can supersede (by r7); the
    withdrawal of r4 never got there, so the server must not present r4 as live."""
    text, receipts = _review(monkeypatch=monkeypatch)

    assert "Use my steak photo too" not in text
    assert "r4" not in receipts or receipts["r4"]["status"] == "superseded"


def test_r8_clips_only_is_met_when_the_timeline_has_no_photo(monkeypatch):
    """KRI-994 (3): every cut in the finished timeline is a video, so "no photos" is proven."""
    data = _load()
    assert {row["kind"] for row in data["variant"]["story_timeline"]} == {"video"}
    assert not any(
        str(row["media_id"]).startswith("asset-") for row in data["variant"]["story_timeline"]
    )

    text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r8"]["verification"] == "checked"
    assert receipts["r8"]["status"] == "met"
    assert "Done: Make the montage from the video clips only" in text


def test_r8_is_not_met_when_a_photo_is_in_the_timeline(monkeypatch):
    data = _load()
    photo = "asset-cfb745e0-513a-4ade-b2c0-8b1b34e54585"
    data = _with_timeline_clip(data, photo)
    data["variant"]["story_timeline"][-1]["kind"] = "image"

    _text, receipts = _review(data, monkeypatch=monkeypatch)

    assert receipts["r8"]["status"] != "met"


def test_the_live_asks_the_server_cannot_settle_stay_listed_and_never_claim_met(monkeypatch):
    """r2 (only about the steak, KRI-993) and r6 (quick cuts) have no checker: they keep
    being listed, never `met` (the honesty rule behind KRI-700/KRI-558)."""
    text, receipts = _review(monkeypatch=monkeypatch)

    for req_id in ("r2", "r6"):
        assert receipts[req_id]["verification"] == "unchecked"
        assert receipts[req_id]["status"] != "met"
    assert "only about the steak" in text.split("Have a look")[-1]


def test_a_partly_overlapping_later_ask_does_not_withdraw_the_earlier_one(monkeypatch):
    """ "Also use the steak photo, but keep the rest video" is not "clips only": r4 stays."""
    data = _load()
    brief = CreativeBrief.model_validate(data["brief"])
    reqs = [
        req.model_copy(
            update={"description": "Use the steak photo too, the rest can stay video clips"}
        )
        if req.id == "r8"
        else req
        for req in brief.requirements
    ]

    text, receipts = _review(
        data, brief=CreativeBrief(version=brief.version, requirements=reqs), monkeypatch=monkeypatch
    )

    assert receipts["r4"]["status"] != "superseded"
    assert "Use my steak photo too" in text


def test_brief_requirement_model_is_the_shape_the_fixture_records():
    """The fixture's r5 is the superseded one the ticket points at (a `remove`)."""
    brief = CreativeBrief.model_validate(_load()["brief"])
    by_id = {req.id: req for req in brief.requirements}

    assert isinstance(by_id["r5"], BriefRequirement)
    assert by_id["r5"].status == "superseded"
    assert by_id["r4"].status == "open"
    assert by_id["r8"].facts == {}
