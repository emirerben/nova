"""KRI-537/541: the render-ready reply judges the brief from the variant's evidence.

Prod thread 55a99da8 / job 890eca28: after a correct Voiceover render the reply still said
"Couldn't verify" for a "show my photo when I say X" ask and a "keep crowd noise quiet" ask,
because the review rebuilt facts from text lanes only. The phone narrated worker persists
`phone_beat_receipt` and `voiceover_bed_level` on the variant; `plan_facts_from_phone_variant`
reads them. KRI-541: the same reply then said "Couldn't verify" for "clean captions" and
"cut long pauses" although the render captioned and cut; the variant's `caption_cues`,
`voiceover_caption_style` and `silence_cut_outcome` now judge those. No database:
`_approved_generation_review` only reads the thread id, the binding and the variant.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.tasks import kria_runtime

GENERATION = "720266e0bc584a09af74bf6fbf167f99"
DEFAULT = "Your voiceover edit is ready."

_MEDAL = "show medal photo when voiceover says the medal"
_WATCH = "show watch photo when voiceover says four hours and twelve minutes"
_CROWD = "keep crowd noise quiet under the voiceover"


def _requirement(req_id: str, kind: str, description: str) -> BriefRequirement:
    return BriefRequirement(id=req_id, kind=kind, scope="global", description=description, facts={})


def _brief() -> CreativeBrief:
    """The prod marathon brief, verbatim."""
    return CreativeBrief(
        version=1,
        requirements=[
            _requirement("r1", "style", "clean captions"),
            _requirement(
                "r2", "audio", "cut long pauses and the restart of the kilometer thirty sentence"
            ),
            _requirement("r3", "audio", _CROWD),
            _requirement("r4", "timing", _MEDAL),
            _requirement("r5", "timing", _WATCH),
        ],
    )


_MEDAL_PLACED = {
    "at_s": 25.432,
    "end_s": 28.432,
    "beat_id": "medal",
    "trigger": "the medal",
    "visual_label": "340BCD6C-6C1B-4F78-A6D2-B68BF3B4255D-N3_04_visual.jpg",
}
_WATCH_PLACED = {
    "at_s": 23.262,
    "end_s": 25.432,
    "beat_id": "watch-time",
    "trigger": "four hours and twelve minutes",
    "visual_label": "02937924-17F3-4509-8DC0-BC7FEF699401-N3_05_visual.jpg",
}


def _variant(**overrides) -> dict:
    variant = {
        "variant_id": "v1",
        "render_generation_id": GENERATION,
        "resolved_archetype": "narrated",
        "caption_language": "en",
        "voiceover_bed_level": 0.25,
        "voiceover_caption_style": "sentence",
        "phone_beat_receipt": {
            "placed": [copy.deepcopy(_MEDAL_PLACED), copy.deepcopy(_WATCH_PLACED)],
            "closing": {"badge": "none", "status": "none"},
            "matcher": "phrase",
            "version": 1,
            "unplaced": [],
            "face_sampling": "skipped",
        },
        "text_elements": [],
        "text_overlays": [],
        # KRI-541: what the prod render recorded for the captions and cleanup asks.
        "render_destination": "device",
        "caption_cues": [
            {"text": f"line {i}", "start_s": float(i), "end_s": i + 0.9} for i in range(15)
        ],
        "silence_cut_outcome": "applied",
    }
    variant.update(overrides)
    return variant


def _review(variant: dict) -> tuple[str, dict[str, dict]]:
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    binding = BriefBinding.create(thread.id, _brief()).model_dump(mode="json")
    execution = SimpleNamespace(result={"brief_binding": binding})
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan={})
    text, payload = kria_runtime._approved_generation_review(
        None, thread, job, variant, execution, DEFAULT
    )
    return text, {row["requirement_id"]: row for row in payload}


def _line(text: str, prefix: str) -> str:
    return next(line for line in text.splitlines() if prefix in line)


def test_correct_render_judges_pop_in_and_crowd_noise_asks():
    text, receipts = _review(_variant())

    medal = _line(text, "Done: show medal photo when voiceover says the medal (")
    assert "25.4 s" in medal
    watch = _line(text, "Done: show watch photo when voiceover says four hours")
    assert "23.3 s" in watch
    crowd = _line(text, "Done: keep crowd noise quiet under the voiceover (")
    assert "25%" in crowd
    assert "Couldn't verify: show medal" not in text
    assert "Couldn't verify: keep crowd" not in text

    for req_id in ("r3", "r4", "r5"):
        receipt = receipts[req_id]
        assert receipt["verification"] == "checked"
        assert receipt["status"] == "met"
        assert receipt["brief_version"] == 1
        assert receipt["generation_id"] == GENERATION


def test_unheard_trigger_is_not_possible_and_the_reply_says_so():
    variant = _variant()
    variant["phone_beat_receipt"]["placed"] = [copy.deepcopy(_WATCH_PLACED)]
    variant["phone_beat_receipt"]["unplaced"] = [{"trigger": "the medal", "reason": "never_heard"}]

    text, receipts = _review(variant)

    # The only word this ask is about was never said, so nothing of it landed; the other
    # ask's beat is untouched.
    assert receipts["r4"]["verification"] == "checked"
    assert receipts["r4"]["status"] == "not_possible"
    assert "never heard" in receipts["r4"]["reason"]
    assert receipts["r5"]["status"] == "met"
    medal = _line(text, "Couldn't: show medal photo when voiceover says the medal (")
    assert "never heard the medal" in medal
    assert "Couldn't verify: show medal" not in text


def test_variant_without_beat_receipt_or_bed_level_keeps_today_behaviour():
    variant = _variant()
    del variant["phone_beat_receipt"]
    del variant["voiceover_bed_level"]
    del variant["resolved_archetype"]

    text, receipts = _review(variant)

    for req_id in ("r1", "r2", "r3", "r4", "r5"):
        assert receipts[req_id]["verification"] == "unchecked"
    assert "Have a look at these in the video:" in text
    for ask in ("clean captions", "show medal photo", "show watch photo", "keep crowd noise"):
        assert f"- {ask}" in text
    assert "verify" not in text.lower()


def test_a_narrated_variant_without_a_bed_level_still_knows_the_bed_is_under_the_voice():
    variant = _variant()
    del variant["phone_beat_receipt"]
    del variant["voiceover_bed_level"]

    text, receipts = _review(variant)

    assert receipts["r3"]["status"] == "met"
    assert "plays under your voice" in receipts["r3"]["reason"]
    assert receipts["r4"]["verification"] == "unchecked"
    assert "- show medal photo" in text
    assert "verify" not in text.lower()


def test_prod_render_reply_has_no_couldnt_verify_line():
    """KRI-541: prod job 890eca28 captioned the voiceover in sentences and cut 15 pauses."""
    text, receipts = _review(_variant())

    assert "Couldn't verify" not in text
    assert "I couldn't verify every requested change" not in text
    assert _line(text, "Done: clean captions")
    assert receipts["r1"]["verification"] == "checked"
    assert receipts["r1"]["status"] == "met"
    # The pauses were cut, the restarted kilometer-thirty sentence was not (the cut only
    # removes silences and filler sounds): an honest "Partly", never "Done".
    cleanup = _line(text, "Partly: cut long pauses and the restart of the kilometer thirty")
    assert "Speech cleanup cut the long pauses" in cleanup
    assert "trim that in the editor" in cleanup
    assert receipts["r2"]["verification"] == "checked"
    assert receipts["r2"]["status"] == "partial"
    for req_id in ("r1", "r2"):
        assert receipts[req_id]["brief_version"] == 1
        assert receipts[req_id]["generation_id"] == GENERATION


def test_a_render_without_cleanup_says_the_pauses_stay():
    variant = _variant()
    del variant["silence_cut_outcome"]

    text, receipts = _review(variant)

    assert receipts["r2"]["verification"] == "checked"
    assert receipts["r2"]["status"] == "partial"
    assert "didn't run on this video" in receipts["r2"]["reason"]
    assert "Couldn't verify: cut long pauses" not in text


def test_a_word_by_word_talking_render_judges_captions_too():
    variant = _variant(resolved_archetype="subtitled", voiceover_caption_style="word")
    del variant["voiceover_bed_level"]
    del variant["phone_beat_receipt"]

    text, receipts = _review(variant)

    assert receipts["r1"]["status"] == "met"
    assert receipts["r2"]["verification"] == "checked"
    assert "Couldn't verify: clean captions" not in text
    assert "Couldn't verify: cut long pauses" not in text
