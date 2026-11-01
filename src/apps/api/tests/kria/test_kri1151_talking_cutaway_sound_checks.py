"""KRI-1151: the draft no longer calls "use their audio" unsupported on a Talking edit.

Replays the brief of prod thread 6fa36e51 (job 07efa44c, 2026-11-01; the fixture is the
captured brief, requirement ids and facts as the Main Creator wrote them):

    r1 [style/global]   Show the talks full screen when mentioned
    r2 [audio/global]   Use the audio of the talk clips when they are shown   (clip_sound: on)
    r3 [style/clip:134CDBD9...] Place the stadium full screen when I talk
    r4 [audio/clip:134CDBD9...] Keep my audio while the stadium is shown      (clip_sound: off)

The first draft answered "I can't make this yet" for r2 and r4 and held the draft back. r4 is
the classifier lane's (KRI-1151 only changes what a Talking edit can DO with an "on" ask); the
assertions here are about r2, plus the two ways it must not regress: the flag off keeps the old
answer, and an ask a cutaway cannot express still says why.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "kri1151_thread_6fa36e51.json"
THREAD = json.loads(_FIXTURE.read_text(encoding="utf-8"))
REQS = {r["id"]: BriefRequirement(**r) for r in THREAD["brief"]["requirements"]}
STADIUM = THREAD["stadium_media_id"]
UNSUPPORTED = "works on a montage made on your iPhone"
_VERIFIED = [
    "basicComposition",
    "local1080Export",
    "audioMix",
    "visualBlocks",
    "visualVideos",
    "alphaOverlay",
]


def _talking_draft(**overrides) -> PlanFacts:
    return PlanFacts(
        phone_talking=True,
        edit_format="subtitled",
        clip_ids=tuple(THREAD["clip_media_ids"]),
        **overrides,
    )


def _switch(monkeypatch: pytest.MonkeyPatch, *, cutaway_sound: bool, windows: bool = True) -> None:
    monkeypatch.setattr(settings, "phone_render_verified_features", _VERIFIED)
    monkeypatch.setattr(settings, "phone_talking_cutaway_sound_enabled", cutaway_sound)
    monkeypatch.setattr(settings, "phone_clip_sound_windows_enabled", windows)


def test_the_threads_request_text_is_what_the_fixture_replays():
    assert THREAD["creator_request"].startswith("Show the talks when I mention in full screen")
    assert REQS["r2"].facts == {"clip_sound": "on"}
    assert REQS["r4"].facts == {"clip_sound": "off"}


def test_flag_off_keeps_the_old_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, cutaway_sound=False)
    receipt = check_requirement(REQS["r2"], _talking_draft())
    assert receipt.status == "not_possible"
    assert UNSUPPORTED in receipt.reason


def test_flag_on_without_mute_windows_keeps_the_old_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, cutaway_sound=True, windows=False)
    receipt = check_requirement(REQS["r2"], _talking_draft())
    assert receipt.status == "not_possible"
    assert UNSUPPORTED in receipt.reason


def test_flag_on_r2_is_met_before_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, cutaway_sound=True)
    receipt = check_requirement(REQS["r2"], _talking_draft())
    assert receipt.status == "met"
    assert UNSUPPORTED not in (receipt.reason or "")


def test_an_ask_a_cutaway_cannot_express_still_says_why(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, cutaway_sound=True)
    seconds = REQS["r2"].model_copy(
        update={"facts": {"clip_sound": "on", "sound_from_s": 4, "sound_to_s": 6}}
    )
    receipt = check_requirement(seconds, _talking_draft())
    assert receipt.status == "not_possible"
    assert "whole cutaway clip" in receipt.reason


def test_a_named_clip_that_is_not_in_the_edit_is_said_so(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, cutaway_sound=True)
    gone = REQS["r2"].model_copy(update={"scope": "clip:no-such-clip.mp4"})
    receipt = check_requirement(gone, _talking_draft())
    assert receipt.status == "not_possible"
    assert "isn't in this edit" in receipt.reason


def test_the_finished_render_is_judged_from_the_workers_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, cutaway_sound=True)
    record = {
        "rows": [
            {
                "requirement_id": "r2",
                "state": "on",
                "status": "partial",
                "reason_code": "not_built",
                "on_windows": [],
                "off_windows": [],
            }
        ]
    }
    receipt = check_requirement(REQS["r2"], _talking_draft(clip_sound_toggles=record))
    assert receipt.status == "partial"
    record["rows"][0].update(status="met", reason_code=None, on_windows=[[3.0, 6.0]])
    assert check_requirement(REQS["r2"], _talking_draft(clip_sound_toggles=record)).status == "met"


def test_one_clip_has_nothing_to_cut_away_to_so_no_sound_is_promised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, cutaway_sound=True)
    alone = PlanFacts(phone_talking=True, edit_format="subtitled", clip_ids=(STADIUM,))
    receipt = check_requirement(REQS["r2"], alone)
    assert receipt.status == "not_possible"
    assert UNSUPPORTED in receipt.reason
