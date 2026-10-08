"""KRI-546: the render-ready reply of a phone Montage judges order, duplicates and the ending.

Prod job a7d69a5b (stress kit M3 "Berlin student day", 2026-10-08): the iPhone render was in
filming order and ended on Elif's own spoken sentence, yet the reply said "Doğrulayamadım"
for the filming order, for "use one of two copies of the same video" and for "end on
Elif's sentence in her own voice". The plan record (`unified_montage`) had judged the
order, but it carries the approved generation while the published export carries the
phone's upload attempt id, so the review dropped it; and nothing could judge the other two.

The review now finds the record through the device record of the published attempt, reads
the closing line the plan held (`closing_speech`) against the finished timeline, and tells
duplicates apart by the original uploads' sha256. In that prod job the duplicate really
stayed in the edit: the reply must say so. No database: `_approved_generation_review` only
reads the thread id, the binding, the job's plan and the variant.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import PlanFacts, check_requirement
from app.kria.reply_language import reply_language_for
from app.services.device_render import DEVICE_RENDER_FIELD
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.tasks import kria_runtime

THREAD = uuid.UUID("7d7c5e25-997e-45f7-8ddf-b029501997e7")
GENERATION = "24277bbe8882400d9b437453c7f7f273"  # assembly_plan.creator_generation_id
ATTEMPT = "eaf31f41-612d-4b31-a240-151f531b44e2"  # the phone's published upload attempt
DEFAULT = "The confirmed edit is ready."


def _id(head: str) -> str:
    return f"analysis-proxy-ios-{head}.mp4"


def _sha(prefix: str) -> str:
    return prefix.ljust(64, "0")


# The finished edit, in screen order (capture time), as prod rendered it:
# (media id, original upload sha256, source start, source end).
F3A5 = _id("F3A5C884-3016-4293-B876-E0CC5FB50553")
COPY = _id("145EF73F-D98E-4378-97CE-0B0E6C3F8DE2")  # the same upload as F3A5, again
ELIF = _id("F0CECCF8-5871-4F5B-99AB-E90473FFD96F")
CUTS = [
    (_id("8ACB01BC-1229-420E-BF96-DFDF8C29663F"), _sha("85b7e1599ecd"), 2.2, 3.933),
    (F3A5, _sha("6e8c3888ca2a"), 1.9, 3.633),
    (COPY, _sha("6e8c3888ca2a"), 2.5, 4.233),
    (_id("AE0514AE-E875-4A95-BD35-D6E036F7D4C3"), _sha("329732d64313"), 0.0, 1.933),
    (_id("77640702-EAEA-4F32-BEC2-FCB396BFA3F2"), _sha("1e4e6b481165"), 0.0, 1.933),
    (_id("0E533600-D24E-408C-8FF5-4557D86D984D"), _sha("62239d3de980"), 2.5, 4.433),
    (_id("C21A8EE0-A8C7-4F3B-A52C-993347CA1CBD"), _sha("a1b5c83b8e24"), 0.0, 1.7),
    (_id("41C0D8B0-EB6E-4D2D-967C-60316DC215AA"), _sha("be9694b3cd36"), 0.75, 2.45),
    (_id("DC9A77D3-F410-4838-85F2-1378812FA210"), _sha("7936e271e012"), 0.0, 1.433),
    (_id("E526D364-2FEA-4982-9459-40819C77CE86"), _sha("b6a1fca167bd"), 3.334, 5.034),
    (_id("6FDF176A-7E76-4B6D-A487-96D7C6C25D92"), _sha("067d2adbc498"), 2.0, 3.7),
    (ELIF, _sha("16352219187b"), 0.0, 5.767),
]
TITLE = "Sabah, Üniversite, Öğle arası, Spor, Akşam"
R1 = "Videoları çektiğim saat sırasına göre diz, sabahtan geceye"
R3 = "Aynı videodan iki tane varsa birini kullan"
R4 = "En sonda Elif'in kameraya söylediği cümleyi kendi sesiyle kullan"


def _brief(**r4) -> CreativeBrief:
    """The prod brief, verbatim."""
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1", kind="order", scope="global", description=R1, facts={"key": "capture_time"}
            ),
            BriefRequirement(
                id="r2", kind="text", scope="global", description="Bölüm başlıkları", literal=TITLE
            ),
            BriefRequirement(id="r3", kind="select", scope="global", description=R3),
            BriefRequirement(
                id="r4",
                **{"kind": "audio", "scope": "clip:F0CECCF8", "description": R4, **r4},
            ),
            BriefRequirement(
                id="r5",
                kind="timing",
                scope="global",
                description="25 saniye olsun",
                facts={"duration_s": 25},
            ),
        ],
    )


def _stored(req_id: str, status: str, *, checked: bool, reason: str | None = None) -> dict:
    return {
        "requirement_id": req_id,
        "status": status,
        "verification": "checked" if checked else "unchecked",
        "stage": "checked" if checked else "understood",
        "reason": reason,
        "inferred": [],
        "inferred_labels": [],
        "target_media_ids": ["F0CECCF8"] if req_id == "r4" else [],
        "brief_version": 1,
        "generation_id": GENERATION,
    }


def _record(cuts=CUTS) -> dict:
    """`assembly_plan.unified_montage` as the phone montage worker stored it in prod."""
    return {
        "version": 1,
        "brief_version": 1,
        "generation_id": GENERATION,
        "clip_ids": [media_id for media_id, *_rest in cuts],
        "title": TITLE,
        "title_source": "brief",
        "duration_s": 24.998,
        "ordering_basis": "capture_time",
        "ordering_fallback_clip_ids": [],
        "closing_speech": {"media_id": ELIF, "source_start_s": 0.0, "source_end_s": 5.767},
        "intent_outcomes": [
            {
                "op": "order",
                "code": "landed",
                "name": "last: Elif'in kameraya söylediği cümleyi kendi sesiyle",
                "reason": None,
                "status": "met",
                "position": "last",
            }
        ],
        "requirement_receipts": [
            _stored("r1", "met", checked=True),
            _stored("r2", "met", checked=True),
            _stored(
                "r3", "partial", checked=False, reason="I can't verify this one automatically yet."
            ),
            _stored(
                "r4", "partial", checked=False, reason="I can't verify this one automatically yet."
            ),
            _stored("r5", "met", checked=True),
        ],
    }


def _variant(cuts=CUTS, **overrides) -> dict:
    timeline, at = [], 0.0
    for media_id, _sha256, start, end in cuts:
        timeline.append(
            {
                "kind": "video",
                "lane": "clip",
                "media_id": media_id,
                "source_start_s": start,
                "source_end_s": end,
                "output_start_s": round(at, 3),
                "output_end_s": round(at + end - start, 3),
            }
        )
        at += end - start
    variant = {
        "variant_id": "guided_story",
        "render_status": "ready",
        "render_destination": "device",
        "render_generation_id": ATTEMPT,
        "resolved_archetype": "guided_story",
        "edit_duration_s": 24.998,
        "duration_s": 26.598,
        "brand_tail_s": 1.6,
        "source_audio_preserved": True,
        "music_playback_mode": "reference_only",
        "text_elements": [
            {
                "id": "guided-title",
                "role": "generative_intro",
                "text": TITLE,
                "start_s": 0.0,
                "end_s": 2.2,
            }
        ],
        "story_timeline": timeline,
    }
    variant.update(overrides)
    return variant


def _job(*, record=None, sources=CUTS, device=True, requirement_generation=GENERATION) -> dict:
    plan = {
        "creator_generation_id": GENERATION,
        "unified_montage": record if record is not None else _record(),
        PHONE_SOURCES_FIELD: [
            {
                "media_id": media_id,
                "proxy_path": f"users/u/creation-threads/t/{media_id}",
                "generation": "1",
                "original": {"sha256": sha256},
            }
            for media_id, sha256, *_rest in sources
        ],
    }
    if device:
        plan[DEVICE_RENDER_FIELD] = {
            "guided_story": {
                "published_attempt": ATTEMPT,
                "base_generation": ATTEMPT,
                **(
                    {"requirement_generation": requirement_generation}
                    if requirement_generation
                    else {}
                ),
            }
        }
    return plan


def _review(plan: dict, variant: dict, brief: CreativeBrief | None = None, *, lang="tr", **result):
    binding = BriefBinding.create(THREAD, brief or _brief()).model_dump(mode="json")
    thread = SimpleNamespace(id=THREAD, creator_id=uuid.uuid4())
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan=plan, all_candidates={})
    execution = SimpleNamespace(result={"brief_binding": binding, **result})
    with reply_language_for(lang):
        text, payload = kria_runtime._approved_generation_review(
            None, thread, job, variant, execution, DEFAULT
        )
    return text, {row["requirement_id"]: row for row in payload}


def _line(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if prefix in line)


def _deduped() -> list:
    return [cut for cut in CUTS if cut[0] != COPY]


def test_prod_reply_judges_order_ending_and_the_duplicate_that_stayed():
    text, receipts = _review(_job(), _variant())

    assert "Doğrulayamadım" not in text
    assert _line(text, f"Yapıldı: {R1}")
    # Elif's clip ends the edit with its whole line (0.0-5.767 s) and the camera audio kept.
    assert _line(text, f"Yapıldı: {R4} (")
    # The duplicate really stayed in the edit (prod): an honest "couldn't", never "done".
    assert _line(text, f"Yapamadım: {R3} (").endswith("2. ve 3. klipler)")
    assert receipts["r1"]["status"] == "met"
    assert receipts["r3"]["status"] == "not_possible"
    assert receipts["r4"]["status"] == "met"
    assert receipts["r4"]["target_media_ids"] == ["F0CECCF8"]
    for req_id in ("r1", "r2", "r3", "r4", "r5"):
        assert receipts[req_id]["verification"] == "checked"
        assert receipts[req_id]["status"] == "met" or req_id == "r3"
        assert receipts[req_id]["brief_version"] == 1
        assert receipts[req_id]["generation_id"] == ATTEMPT
    # The receipts come back in the brief's order.
    assert list(receipts) == ["r1", "r2", "r3", "r4", "r5"]


def test_the_english_reply_names_the_repeated_clips():
    text, receipts = _review(_job(), _variant(), lang="en")

    assert "Couldn't verify" not in text
    assert _line(text, f"Couldn't: {R3} (").endswith(
        "(The same video is in the edit more than once: clips 2 and 3)"
    )
    assert "in its own sound" in receipts["r4"]["reason"]


def test_a_deduped_render_meets_the_duplicate_ask():
    """KRI-544 shape: one copy of the repeated upload is left out of the finished edit."""
    cuts = _deduped()
    text, receipts = _review(_job(record=_record(cuts)), _variant(cuts))

    assert receipts["r3"]["verification"] == "checked"
    assert receipts["r3"]["status"] == "met"
    assert "Doğrulayamadım" not in text
    assert "Yapamadım" not in text
    assert text.startswith(DEFAULT)


def test_a_partly_deduped_render_is_partial():
    third = (_id("99999999-0000-4000-8000-000000000000"), _sha("6e8c3888ca2a"), 0.0, 1.0)
    # Three copies of one file were uploaded; the edit left one out but kept two.
    sources = [*CUTS[:-1], third, CUTS[-1]]
    cuts = [cut for cut in sources if cut[0] != COPY]
    _text, receipts = _review(_job(record=_record(cuts), sources=sources), _variant(cuts))

    assert receipts["r3"]["status"] == "partial"
    assert receipts["r3"]["reason"].endswith("2. ve 11. klipler")


def test_the_same_clip_cut_twice_is_not_called_unique():
    cuts = [*_deduped()[:-1], _deduped()[1], CUTS[-1]]  # F3A5 again near the end
    _text, receipts = _review(_job(record=_record(cuts)), _variant(cuts))

    assert receipts["r3"]["status"] == "not_possible"
    assert receipts["r3"]["reason"].endswith("2. ve 11. klipler")


def test_fingerprints_fall_back_to_the_approved_media_snapshot():
    """The prod debug shape: the private source bindings are not there, the snapshot is."""
    plan = _job(sources=[])
    snapshot = {
        "clip_assignments": [
            {
                "media_id": media_id,
                "upload_contract": {"proxy": {"original": {"sha256": sha256}}},
            }
            for media_id, sha256, *_rest in CUTS
        ]
    }
    plan["creator_brief_binding"] = BriefBinding.create(
        THREAD, _brief(), media_snapshot=snapshot
    ).model_dump(mode="json")
    _text, receipts = _review(plan, _variant())

    assert receipts["r3"]["status"] == "not_possible"


def test_a_clip_without_a_fingerprint_leaves_the_duplicate_ask_unjudged():
    _text, receipts = _review(_job(sources=CUTS[:-1]), _variant())

    assert receipts["r3"]["verification"] == "unchecked"
    assert receipts["r4"]["status"] == "met"


@pytest.mark.parametrize(
    ("variant", "record", "reason"),
    [
        # The plan held no closing line (no speech found, or the camera audio was off).
        (_variant(), {**_record(), "closing_speech": None}, "doğrulayamadım"),
        # The finished closing cut is shorter than the line.
        (
            _variant([*CUTS[:-1], (ELIF, _sha("16352219187b"), 0.0, 2.3)]),
            None,
            "doğrulayamadım",
        ),
        # The render says the clips' own sound was dropped.
        (_variant(source_audio_preserved=False), None, "kendi sesi kapalı"),
        # Elif's clip is in the edit, but not last.
        (_variant([CUTS[-1], *CUTS[:-1]]), None, "sonda değil"),
    ],
)
def test_an_ending_the_render_does_not_show_is_partial(variant, record, reason):
    _text, receipts = _review(_job(record=record), variant)

    assert receipts["r4"]["verification"] == "checked"
    assert receipts["r4"]["status"] == "partial"
    assert reason in receipts["r4"]["reason"]


def test_an_ending_on_a_clip_left_out_of_the_edit_is_not_possible():
    cuts = CUTS[:-1]
    _text, receipts = _review(_job(record=_record(cuts)), _variant(cuts))

    assert receipts["r4"]["status"] == "not_possible"


def test_an_end_order_with_speech_needs_the_held_line_too():
    """The same ask filed as a `last` order: the seat alone is not "in her own voice"."""
    brief = _brief(kind="order", scope="global", facts={"last_clip": "Elif's sentence"})
    record = _record()
    record["requirement_receipts"][3] = _stored("r4", "met", checked=True)

    _text, receipts = _review(_job(record=record), _variant(), brief)
    assert receipts["r4"]["status"] == "met"
    assert "kendi sesiyle" in receipts["r4"]["reason"]

    _text, receipts = _review(_job(record={**record, "closing_speech": None}), _variant(), brief)
    assert receipts["r4"]["status"] == "partial"


def test_a_record_of_another_generation_is_never_read():
    """No device record for the published attempt (or an editor Save's own generation):
    the plan record may describe another cut, so it is not trusted, exactly as before."""
    for plan in (
        _job(device=False),
        _job(requirement_generation=None),
        _job(requirement_generation="an-editor-save-generation"),
    ):
        text, receipts = _review(plan, _variant())
        assert receipts["r1"]["verification"] == "unchecked"
        assert receipts["r3"]["verification"] == "unchecked"
        assert receipts["r4"]["verification"] == "unchecked"
        assert _line(text, f"Doğrulayamadım: {R1}")
        assert receipts["r2"]["status"] == "met"
        assert receipts["r5"]["status"] == "met"


def test_an_editor_turn_keeps_the_exact_generation_match():
    """An editor turn changed the cut the plan record describes: never read it there."""
    prep = {"generation": GENERATION, "render_destination": "device"}
    text, receipts = _review(_job(), _variant(), editor_prep=prep)

    for req_id in ("r1", "r3", "r4"):
        assert receipts[req_id]["verification"] == "unchecked"
    assert _line(text, f"Doğrulayamadım: {R1}")


def test_another_variants_published_attempt_is_not_this_ones():
    plan = _job()
    plan[DEVICE_RENDER_FIELD]["guided_story"]["published_attempt"] = "another-attempt"
    _text, receipts = _review(plan, _variant())

    assert receipts["r1"]["verification"] == "unchecked"


def test_a_published_voiceover_export_reads_its_narrated_record_too():
    """The same id gap hid KRI-533's narrated receipts on a published phone export."""
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="order",
                scope="global",
                description="End on the sunset valley",
                facts={"last_clip": "the sunset valley"},
            )
        ],
    )
    plan = _job()
    del plan["unified_montage"]
    plan["narrated_alignment"] = {
        "generation_id": GENERATION,
        "brief_version": 1,
        "requirement_receipts": [_stored("r1", "met", checked=True)],
    }
    text, receipts = _review(plan, _variant(resolved_archetype="narrated"), brief, lang="en")

    assert receipts["r1"]["status"] == "met"
    assert receipts["r1"]["verification"] == "checked"
    assert "Couldn't verify" not in text


def test_draft_facts_keep_today_answers_for_both_asks():
    """A draft (or any non-montage plan) never claims the duplicate or the ending."""
    brief = _brief()
    for req in brief.requirements[2:4]:
        receipt = check_requirement(req, PlanFacts())
        assert receipt.status == "partial"
        assert receipt.reason == "I can't verify this one automatically yet."


def test_the_variant_is_not_mutated():
    variant = _variant()
    before = copy.deepcopy(variant)
    _review(_job(), variant)
    assert variant == before
