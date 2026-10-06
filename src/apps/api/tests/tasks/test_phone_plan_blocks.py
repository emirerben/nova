"""KRI-443 follow-up: phone narrated / voiceover-montage / subtitled writers feed the
live plan blocks once their device request is pinned (the guided and unified-montage
phone paths already do, through `_guided_execution_plan`).

Without this the thread kept only the seven `waiting` blocks for the whole device
render ("Planning - 0 of 7 decided").
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.kria import plan_blocks
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks import test_phone_montage_dispatch as montage
from tests.tasks import test_phone_subtitled_narrated_dispatch as phone


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr(plan_blocks, "emit_plan_blocks", mock)
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    return mock


def _only_emit(sent: MagicMock, job) -> dict[str, dict]:
    assert sent.call_count == 1
    job_id, blocks = sent.call_args.args
    assert job_id == str(job.id)
    assert [b["section_id"] for b in blocks] == list(plan_blocks.SECTION_ORDER)
    assert {b["state"] for b in blocks} == {"decided"}
    return {b["section_id"]: b for b in blocks}


def test_narrated_emits_seven_decided_blocks(monkeypatch, sent) -> None:
    job, *_ = phone._setup_narrated(
        monkeypatch, extra_candidates={"creator_strategy": {"opening_title": "Cacio e pepe"}}
    )
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    by_id = _only_emit(sent, job)
    assert by_id["title"]["summary"] == "Cacio e pepe"
    assert by_id["clips"]["summary"] == "3 clips · 12s"
    assert by_id["captions"]["summary"].endswith(("caption", "captions"))
    assert by_id["music"]["summary"] == "Your voiceover"
    for section in ("sfx", "overlays", "look"):
        assert by_id[section]["skipped"] is True


def test_narrated_without_a_title_skips_only_the_title(monkeypatch, sent) -> None:
    job, *_ = phone._setup_narrated(monkeypatch)
    gb._run_generative_job(str(job.id))

    by_id = _only_emit(sent, job)
    assert by_id["title"]["skipped"] is True
    assert by_id["clips"]["skipped"] is False


def test_subtitled_emits_seven_decided_blocks(monkeypatch, sent) -> None:
    job, *_ = phone._setup_subtitled(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    by_id = _only_emit(sent, job)
    assert by_id["clips"]["summary"].startswith("1 clip")
    assert by_id["captions"]["skipped"] is False
    assert by_id["music"]["skipped"] is True


def test_voiceover_montage_emits_seven_decided_blocks(monkeypatch, sent) -> None:
    job, *_ = montage.setup(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    by_id = _only_emit(sent, job)
    assert by_id["clips"]["summary"].startswith("2 clips")
    assert by_id["music"]["summary"] == "Your voiceover"
    assert by_id["title"]["skipped"] is True


def test_voiceover_montage_with_a_matched_track_names_it(monkeypatch, sent) -> None:
    job, snapshot, candidates, _cloud = montage._voiceover_with_track_setup(
        monkeypatch,
        verified_features=[
            "basicComposition",
            "local1080Export",
            "crossfade",
            "audioMix",
            "narrationAudio",
            "musicBed",
        ],
    )
    gb._run_phone_voiceover_montage_job(str(job.id), snapshot, candidates, ownership_epoch=3)

    by_id = _only_emit(sent, job)
    assert by_id["music"]["summary"] != "Your voiceover"
    assert by_id["music"]["detail"] == "Under your voiceover"


@pytest.mark.parametrize("path", ["narrated", "subtitled", "montage"])
def test_flag_off_emits_nothing(monkeypatch, path) -> None:
    sent = MagicMock()
    monkeypatch.setattr(plan_blocks, "emit_plan_blocks", sent)
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    job = {
        "narrated": lambda: phone._setup_narrated(monkeypatch)[0],
        "subtitled": lambda: phone._setup_subtitled(monkeypatch)[0],
        "montage": lambda: montage.setup(monkeypatch)[0],
    }[path]()
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"
    sent.assert_not_called()


def test_a_feed_failure_never_fails_the_render(monkeypatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    monkeypatch.setattr(plan_blocks, "_emit", MagicMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr(
        plan_blocks, "blocks_from_phone_recipe", MagicMock(side_effect=RuntimeError("boom"))
    )
    job, *_ = phone._setup_narrated(monkeypatch)
    gb._run_generative_job(str(job.id))

    assert job.status == "awaiting_device"


def test_redelivery_does_not_emit_again(monkeypatch, sent) -> None:
    job, *_ = phone._setup_subtitled(monkeypatch)
    gb._run_generative_job(str(job.id))
    assert sent.call_count == 1
    first_request = device_status(job, "subtitled").request

    gb._run_phone_subtitled_job(
        str(job.id), copy.deepcopy(job.assembly_plan), job.all_candidates, ownership_epoch=3
    )
    assert device_status(job, "subtitled").request == first_request
    assert sent.call_count == 1
