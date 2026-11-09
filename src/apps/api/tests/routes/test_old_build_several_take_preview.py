"""KRI-720 G4: an app build that cannot cut each take of a several-take Talking edit must not
preview it uncut.

The old editor preview (`NativePhoneTalkingSource`) cuts only the first take and plays later
takes whole, so captions/cards/sounds land on the wrong words. Every build since protocol 11
already falls back to the finished video when the variant's `phone_talking_multi_clip` receipt
has no `speaker_media_ids`; the server serves exactly that to a build below
`SPEAKER_REMOVED_PROTOCOL`, and only for a variant carrying a non-empty `speaker_removed` cut.
"""

import copy

import pytest

from app.routes import generative_jobs as gj
from app.services.client_protocol import (
    GENERATED_CARD_CONSENT_PROTOCOL,
    SPEAKER_REMOVED_PROTOCOL,
    clear_request_context,
    set_client_protocol,
)
from app.services.device_render import device_status
from tests.routes.test_phone_subtitled_editor_commit import (
    _several_take_job,
    save,
)

RECEIPT = {
    "version": 1,
    "speaker_media_ids": ["take1", "take2"],
    "cutaways": [],
    "unplaced": [],
    "clips": [],
    "speaker_removed": [
        {"media_id": "take2", "removed": [{"start_s": 3.0, "end_s": 4.0, "reason": "pause"}]}
    ],
}


@pytest.fixture(autouse=True)
def _no_leaked_request_context():
    clear_request_context()
    yield
    clear_request_context()


def _job(monkeypatch, receipt, *, flag=False):
    monkeypatch.setattr(gj.settings, "talking_multi_take_cleanup_enabled", flag)
    job, recipe = _several_take_job(monkeypatch, second_keep=[(0.0, 3.0), (4.0, 8.0)])
    job.assembly_plan["variants"][0]["phone_talking_multi_clip"] = copy.deepcopy(receipt)
    return job


def _served(job) -> dict:
    (variant,) = gj._variants_for_response(job)
    return variant


def _speakers(variant) -> list:
    return variant["phone_talking_multi_clip"]["speaker_media_ids"]


@pytest.mark.parametrize("protocol", [22, 45, GENERATED_CARD_CONSENT_PROTOCOL, None])
def test_an_old_or_undeclared_build_gets_the_finished_video_fallback(monkeypatch, protocol):
    job = _job(monkeypatch, RECEIPT)
    set_client_protocol(protocol)  # None = an HTTP caller that sends no header (web, or ancient)
    served = _served(job)
    # The condition `NativePhoneTalkingSource.sourceIndices` already falls back on in every build.
    assert _speakers(served) == []
    # Nothing else an old build reads is touched: the finished video, its poster, the cut itself.
    stored = job.assembly_plan["variants"][0]
    assert served["phone_talking_multi_clip"]["speaker_removed"] == RECEIPT["speaker_removed"]
    assert served["render_destination"] == "device"
    assert served["resolved_archetype"] == "subtitled"
    assert served["caption_cues"] == stored["caption_cues"]
    assert served["duration_s"] == stored["duration_s"]
    # The stored row is never rewritten by serving it.
    assert stored["phone_talking_multi_clip"] == RECEIPT


def test_a_build_that_reads_the_per_take_cut_gets_the_full_receipt(monkeypatch):
    job = _job(monkeypatch, RECEIPT)
    set_client_protocol(SPEAKER_REMOVED_PROTOCOL)
    assert _served(job)["phone_talking_multi_clip"] == RECEIPT
    set_client_protocol(SPEAKER_REMOVED_PROTOCOL + 3)
    assert _served(job)["phone_talking_multi_clip"] == RECEIPT


def test_a_single_take_cut_variant_is_unchanged_for_an_old_build(monkeypatch):
    # One take: no multi-clip receipt at all, the first take's cut is `silence_cut`.
    job = _job(monkeypatch, RECEIPT)
    variant = job.assembly_plan["variants"][0]
    variant.pop("phone_talking_multi_clip")
    variant["silence_cut"] = {"removed": [{"start_s": 4.0, "end_s": 5.0, "reason": "pause"}]}
    set_client_protocol(45)
    served = _served(job)
    assert "phone_talking_multi_clip" not in served
    assert served["silence_cut"] == variant["silence_cut"]


@pytest.mark.parametrize(
    "extra",
    [{}, {"speaker_removed": []}, {"speaker_removed": [{"media_id": "take2", "removed": []}]}],
)
def test_a_several_take_variant_with_no_extra_take_cut_is_unchanged(monkeypatch, extra):
    receipt = {k: v for k, v in RECEIPT.items() if k != "speaker_removed"} | extra
    job = _job(monkeypatch, receipt)
    set_client_protocol(45)
    assert _served(job)["phone_talking_multi_clip"] == receipt


def test_a_variant_rendered_with_cuts_stays_protected_when_the_flag_is_off(monkeypatch):
    job = _job(monkeypatch, RECEIPT, flag=False)
    set_client_protocol(45)
    assert _speakers(_served(job)) == []


def test_a_worker_reading_the_variant_is_not_given_the_old_build_view(monkeypatch):
    # Outside an HTTP request (a Celery task, the agent runtime) nobody previews anything.
    job = _job(monkeypatch, RECEIPT)
    assert _served(job)["phone_talking_multi_clip"] == RECEIPT


def test_a_malformed_receipt_is_left_alone(monkeypatch):
    # The original fixture's receipt is a bare `True`: serving it must not raise or change it.
    job = _job(monkeypatch, RECEIPT)
    job.assembly_plan["variants"][0]["phone_talking_multi_clip"] = True
    set_client_protocol(45)
    assert _served(job)["phone_talking_multi_clip"] is True


@pytest.mark.parametrize("protocol", [45, None])
def test_an_old_build_save_keeps_every_takes_cut(monkeypatch, protocol):
    job = _job(monkeypatch, RECEIPT)
    set_client_protocol(protocol)
    before = device_status(job, "subtitled").request.recipe
    save(job, sound_effects=[])
    after = device_status(job, "subtitled").request.recipe
    main = lambda recipe: next(t for t in recipe.tracks if t.id == "subtitled")  # noqa: E731
    take2 = lambda recipe: [  # noqa: E731
        c.model_dump_json() for c in main(recipe).clips if c.source_asset_id == "take2"
    ]
    assert len(take2(after)) == 2
    assert take2(after) == take2(before)
    assert main(after).model_dump_json() == main(before).model_dump_json()
    assert after.duration == pytest.approx(before.duration)
