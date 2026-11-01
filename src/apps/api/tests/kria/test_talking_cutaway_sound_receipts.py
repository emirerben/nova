"""KRI-1150: "keep my audio under the cutaway" is already what a Talking edit does.

Thread 6fa36e51 (2026-11-01): "Show the talks when I mention in full screen and use their
audio. Place the stadium full screen when I talk but keep my audio". The brief recorded r4
as ``clip_sound: off`` on the stadium clip. `_check_sound_toggle` refused every on/off ask on
a Talking edit ("works on a montage made on your iPhone, not in this edit"), so r4 was listed
as unmet and the first draft was held back, although the phone Talking writer plays a footage
clip muted over the speaker's voice. r2 (the talk clips play their own audio) is a different
ask and stays unmet.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    check_requirement,
    needs_creator_choice,
    plan_facts_from_phone_variant,
)

THREAD = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri1150_talking_keep_audio_thread.json").read_text()
)
STADIUM = THREAD["stadium_media_id"]
SPEAKER = THREAD["clip_assignments"][0]["media_id"]
PODIUM = THREAD["clip_assignments"][1]["media_id"]
NOT_ON_TALKING = "works on a montage made on your iPhone"


def _requirements() -> list[BriefRequirement]:
    return [BriefRequirement(**row) for row in THREAD["requirements"]]


def _req(req_id: str) -> BriefRequirement:
    return next(r for r in _requirements() if r.id == req_id)


def _draft(clips: list[dict] | None = None) -> PlanFacts:
    assignments = copy.deepcopy(clips if clips is not None else THREAD["clip_assignments"])
    return PlanFacts(
        edit_format="subtitled",
        audio_strategy="original_audio",
        phone_talking=True,
        media_snapshot={"clip_assignments": assignments},
        clip_ids=tuple(c["media_id"] for c in assignments),
    )


def _rendered(*, speakers: list[str], footage: list[str]) -> PlanFacts:
    variant = {
        "variant_id": "v1",
        "render_destination": "device",
        "resolved_archetype": "subtitled",
        "text_elements": [],
        "phone_talking_multi_clip": {
            "version": 1,
            "speaker_media_ids": speakers,
            "cutaways": [],
            "unplaced": [],
            "clips": [{"clip_id": m, "role": "speaker"} for m in speakers]
            + [{"clip_id": m, "role": "footage"} for m in footage],
        },
    }
    return plan_facts_from_phone_variant(variant)


def test_the_thread_request_keeps_the_stadium_muted_ask_off_the_unmet_list() -> None:
    receipts = {r.requirement_id: r for r in build_receipts(_requirements(), _draft())}

    assert receipts["r4"].status == "met"
    assert not needs_creator_choice(receipts["r4"])


def test_the_talk_clips_own_sound_stays_honestly_unmet() -> None:
    receipt = check_requirement(_req("r2"), _draft())

    assert receipt.status == "not_possible"
    assert NOT_ON_TALKING in receipt.reason


def test_a_clip_that_may_speak_is_not_promised_muted() -> None:
    clips = copy.deepcopy(THREAD["clip_assignments"])
    clips[-1]["analysis"]["understanding"]["speech"]["has_speech"] = True

    receipt = check_requirement(_req("r4"), _draft(clips))

    assert receipt.status == "not_possible"
    assert NOT_ON_TALKING in receipt.reason


def test_muting_the_whole_video_is_not_what_a_talking_edit_does() -> None:
    req = BriefRequirement(
        id="r9",
        kind="audio",
        scope="global",
        description="Mute everything",
        facts={"clip_sound": "off"},
    )

    receipt = check_requirement(req, _draft())

    assert receipt.status == "not_possible"


def test_turning_a_cutaway_sound_on_is_not_what_a_talking_edit_does() -> None:
    req = BriefRequirement(
        id="r9",
        kind="audio",
        scope=f"clip:{STADIUM}",
        description="Play the stadium sound",
        facts={"clip_sound": "on"},
    )

    assert check_requirement(req, _draft()).status == "not_possible"


def test_the_finished_video_judges_the_clip_by_the_role_the_writer_gave_it() -> None:
    muted = _rendered(speakers=[SPEAKER, PODIUM], footage=[STADIUM])
    spoke = _rendered(speakers=[SPEAKER, STADIUM], footage=[PODIUM])

    assert check_requirement(_req("r4"), muted).status == "met"
    refused = check_requirement(_req("r4"), spoke)
    assert refused.status == "not_possible"
    assert NOT_ON_TALKING in refused.reason


@pytest.mark.parametrize(
    ("language", "reason"),
    [
        ("en", "That clip plays without its own sound, so your voice keeps playing over it"),
        ("tr", "O klip kendi sesi olmadan oynuyor, sesin üzerinde çalmaya devam ediyor"),
    ],
)
def test_the_receipt_says_the_voice_keeps_playing(language: str, reason: str) -> None:
    from app.kria.reply_language import reply_language_for

    with reply_language_for(language):
        assert check_requirement(_req("r4"), _draft()).reason == reason
