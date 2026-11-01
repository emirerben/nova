"""KRI-1102 follow-up: a creator label on a Visuals photo is judged on that photo.

Leverkusen M2 run 3 (prod job 3d28fa04): "Label the selfie 'Taha 🤝 me'" became brief
requirement ``r2`` scoped ``clip:asset-71843110-…``. The unified montage put the label on
the selfie, but its plan record names Visuals by the bare pool id (``71843110-…``), so the
plan-time receipt said "I can't verify text for that target clip in this draft." The chain
here is the worker's, offline: brief -> ``plan_unified_montage`` -> ``plan.record()`` ->
``plan_facts_from_unified_montage`` -> ``build_receipts``.

Ways this can fail (written before the fix):
1. The label is on the named photo, but the brief's ``asset-<uuid>`` never matches the
   record's bare ``<uuid>``, so the receipt says it can't check (the bug).
2. Over-matching: a label on a DIFFERENT photo (or the same words anywhere) makes the named
   photo's ask read "met".
3. The named photo is not in the edit at all, yet the receipt claims "met" or says the
   photo got no text (it can't know; the honest answer stays "can't verify").
4. The right photo carries other words, yet the receipt reads "met".
5. The receipt's ``target_media_ids`` loses the brief's ``asset-<uuid>`` id that the app
   uses to point at the photo.
6. A record that already names the photo ``asset-<uuid>`` stops matching.
7. A footage clip's ``clip:<id>`` scope changes behavior.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    _CANT_CHECK_TARGET_CLIP_TEXT,
    build_receipts,
    plan_facts_from_unified_montage,
)
from app.pipeline.unified_montage import brief_view, plan_unified_montage
from tests.pipeline.test_unified_montage import clip, visual

SELFIE, SCOREBOARD = visual(1), visual(2)
CLIPS = [clip(0, duration=6.0), clip(1, duration=6.0)]
LITERAL = "Taha 🤝 me"


def _label_req(scope: str, literal: str = LITERAL) -> BriefRequirement:
    return BriefRequirement(
        id="r2",
        kind="text",
        scope=scope,
        literal=literal,
        description=f"Label the selfie '{literal}'",
    )


def _planned_record(on: str) -> dict:
    """The worker's plan record with the creator's label asked for on ``on``."""
    brief = CreativeBrief(version=1, requirements=[_label_req(f"clip:{on}")])
    plan = plan_unified_montage(CLIPS, brief_view(brief), visuals=[SELFIE, SCOREBOARD])
    return plan.record()


def _receipt(record: dict, req: BriefRequirement):
    """The receipt the worker stores for a bound (approved-brief) job, as prod wrote r2."""
    facts = plan_facts_from_unified_montage(record)
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def test_the_plan_record_names_the_photo_by_its_bare_pool_id():
    """The premise: the label sits on the selfie, named without the ``asset-`` prefix."""
    record = _planned_record(SELFIE.manifest_id)
    assert SELFIE.media_id in record["clip_ids"]
    assert SELFIE.manifest_id not in record["clip_ids"]
    assert [(row["media_id"], row["text"]) for row in record["labels"]] == [
        (SELFIE.media_id, LITERAL)
    ]


def test_a_label_on_the_named_photo_is_met():
    """Failure 1 + 5: judged on the photo, and the receipt still points at ``asset-<uuid>``."""
    record = _planned_record(SELFIE.manifest_id)
    receipt = _receipt(record, _label_req(f"clip:{SELFIE.manifest_id}"))
    assert receipt.status == "met", receipt.reason
    assert receipt.verification == "checked"
    assert receipt.reason is None
    assert receipt.target_media_ids == [SELFIE.manifest_id]


def test_a_label_on_another_photo_does_not_satisfy_the_named_one():
    """Failure 2 (wrong-photo control): the words are in the edit, on the other photo."""
    record = _planned_record(SCOREBOARD.manifest_id)
    assert [row["media_id"] for row in record["labels"]] == [SCOREBOARD.media_id]
    receipt = _receipt(record, _label_req(f"clip:{SELFIE.manifest_id}"))
    assert receipt.status == "not_possible"
    assert receipt.reason == "That clip didn't get its own text in this draft."


def test_a_photo_left_out_of_the_edit_stays_unverifiable():
    """Failure 3: a photo the plan does not play is never judged met or textless."""
    record = _planned_record(SELFIE.manifest_id)
    absent = visual(9)
    receipt = _receipt(record, _label_req(f"clip:{absent.manifest_id}"))
    assert receipt.status == "partial"
    assert receipt.reason == _CANT_CHECK_TARGET_CLIP_TEXT


def test_other_words_on_the_named_photo_are_not_met():
    """Failure 4: right photo, different words."""
    record = _planned_record(SELFIE.manifest_id)
    receipt = _receipt(record, _label_req(f"clip:{SELFIE.manifest_id}", literal="Final whistle"))
    assert receipt.status == "partial"
    assert receipt.reason == "That clip's text isn't the exact text you gave."


@pytest.mark.parametrize("spelling", ["bare", "manifest"])
def test_either_record_spelling_of_the_photo_matches(spelling):
    """Failure 6: a record naming the photo ``asset-<uuid>`` keeps matching exactly."""
    record = _planned_record(SELFIE.manifest_id)
    if spelling == "manifest":
        rename = {SELFIE.media_id: SELFIE.manifest_id}
        record["clip_ids"] = [rename.get(c, c) for c in record["clip_ids"]]
        record["labels"] = [
            {**row, "media_id": rename.get(row["media_id"], row["media_id"])}
            for row in record["labels"]
        ]
    receipt = _receipt(record, _label_req(f"clip:{SELFIE.manifest_id}"))
    assert receipt.status == "met", receipt.reason


def test_a_footage_clip_label_is_unchanged():
    """Failure 7: footage ids carry no prefix; a label on clip 1 is judged as before."""
    record = _planned_record(CLIPS[1].media_id)
    assert _receipt(record, _label_req(f"clip:{CLIPS[1].media_id}")).status == "met"
    off = _receipt(record, _label_req(f"clip:{CLIPS[0].media_id}"))
    assert off.status == "not_possible"


def test_leverkusen_m2_run3_record_reads_met():
    """The prod record (job 3d28fa04, ids and label as stored) replayed offline."""
    selfie = "71843110-fb7d-4124-90a2-99861d248a2a"
    record = {
        "version": 1,
        "brief_version": 6,
        "clip_ids": [
            selfie,
            "223f68d8-edf8-48c4-a92a-80913b1d0409",
            "analysis-proxy-ios-DABF1B6F-03AC-4937-A1D3-C556EBCC21CD.mp4",
            "analysis-proxy-ios-49F3233F-92C5-4EC1-A239-AA4CC837F688.mp4",
        ],
        "labels": [
            {
                "text": LITERAL,
                "inferred": False,
                "media_id": selfie,
                "fact_kind": None,
                "provenance": "creator",
            }
        ],
        "visual_ids": [selfie, "223f68d8-edf8-48c4-a92a-80913b1d0409"],
        "label_scope_clip_ids": [selfie],
        "dropped_label_reasons": {},
    }
    receipt = _receipt(record, _label_req(f"clip:asset-{selfie}"))
    assert receipt.status == "met", receipt.reason
    assert receipt.target_media_ids == [f"asset-{selfie}"]
