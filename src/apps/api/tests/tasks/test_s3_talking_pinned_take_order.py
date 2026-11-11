"""S3 Yorum2 (prod thread cc0e3706, job 7416c94b): a several-take Talking edit plays its takes in
the order the approved render contract pins, not the order they were attached.

The creator attached take B, then A, then C and said "the 20-second home clip first, then the
7-second home clip, then the car clip last". The render contract now seats those takes
(`talking_clip_anchors`) and pins ``order_ids`` = A, B, C; the phone verifier refuses any other
picture order. The Talking writer read only upload order (`classify_talking_clips`), so a
pinned order that differed from it could only ever be refused.

Failure modes (written before the code):

P1  The takes play in upload order although the contract pins another: the render is refused
    at pin time ("couldn't keep the confirmed clip order").
P2  The takes are reordered but a later take's captions keep the old offsets (they are laid out
    over `extra_speaker_ids`), so its words show over another take.
P3  A contract that pins no order (or no contract at all) reorders anything: upload order stays.
P4  Full-screen photos pinned to words (KRI-1327) sit in the picture between the takes, and the
    verifier reads them as clips out of order, refusing the render.
"""

from __future__ import annotations

import pytest

import tests.tasks.test_s3_three_takes_fullscreen_photos as s3_photos
from app.services.creator_render_contract import CONTRACT_FIELD, CreatorRenderContract
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_talking_multi

# Attached B, A, C (the prod event order); the S3 takes' lengths.
ATTACHED = [("takeB", 6.967, 14), ("takeA", 20.1, 40), ("takeC", 7.134, 14)]
PINNED = ("takeA", "takeB", "takeC")


def _pin(job, **changes) -> None:
    contract = CreatorRenderContract(generation_id="generation").rebind(**changes)
    job.assembly_plan[CONTRACT_FIELD] = contract.model_dump(mode="json")


def _run(monkeypatch, **contract):
    job, _snapshot, _bindings = _setup_talking_multi(monkeypatch, clips=ATTACHED)
    if contract:
        _pin(job, **contract)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device", job.error_detail
    return device_status(job, "subtitled").request.recipe, job.assembly_plan["variants"][0]


def _main(recipe) -> list[tuple[str, float]]:
    track = next(t for t in recipe.tracks if t.id == "subtitled")
    return [(c.source_asset_id, round(float(c.timeline_start), 3)) for c in track.clips]


def test_p1_the_takes_play_in_the_pinned_order_not_the_attached_one(monkeypatch):
    recipe, variant = _run(
        monkeypatch, order_required=True, order_basis="capture_time", order_ids=PINNED
    )

    assert _main(recipe) == [("takeA", 0.0), ("takeB", 20.1), ("takeC", 27.067)]
    multi = variant[gb.PHONE_TALKING_MULTI_CLIP_FIELD]
    assert multi["speaker_media_ids"] == list(PINNED)


def test_p2_each_takes_captions_follow_it_to_its_new_place(monkeypatch):
    _recipe, variant = _run(
        monkeypatch, order_required=True, order_basis="capture_time", order_ids=PINNED
    )

    spans: dict[str, list[float]] = {}
    for cue in variant["caption_cues"]:
        for word in cue["words"]:
            take = word["text"].split("w", 1)[0]
            spans.setdefault(take, []).extend([word["start_s"], word["end_s"]])
    assert 0.0 <= min(spans["takeA"]) and max(spans["takeA"]) <= 20.1
    assert 20.1 <= min(spans["takeB"]) and max(spans["takeB"]) <= 27.067
    assert 27.067 <= min(spans["takeC"]) and max(spans["takeC"]) <= 34.201


@pytest.mark.parametrize(
    "contract", [{}, {"order_required": False}], ids=["no-contract", "no-order-pinned"]
)
def test_p3_without_a_pinned_order_the_attached_order_stays(monkeypatch, contract):
    recipe, _variant = _run(monkeypatch, **contract)

    assert [take for take, _at in _main(recipe)] == ["takeB", "takeA", "takeC"]


def test_p4_full_screen_photos_between_the_takes_never_break_the_pinned_order(monkeypatch):
    """The S3 render itself (takes attached A, B, C there): photos on "Nar", "Mira", "audience"
    and "café" across the three takes, with the creator's order pinned."""

    def _setup_with_order(monkeypatch_, *, clips, enabled=True):
        job, snapshot, bindings = _setup_talking_multi(monkeypatch_, clips=clips, enabled=enabled)
        _pin(job, order_required=True, order_basis="capture_time", order_ids=PINNED)
        return job, snapshot, bindings

    monkeypatch.setattr(s3_photos, "_setup_talking_multi", _setup_with_order)
    recipe, _variant = s3_photos._render(monkeypatch, enabled=True)

    assert any(s3_photos.NAR in key for key in s3_photos._fullscreen(recipe))
    assert [take for take, _at in _main(recipe)] == list(PINNED)
