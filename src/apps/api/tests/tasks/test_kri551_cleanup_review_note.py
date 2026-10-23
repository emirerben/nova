"""KRI-551: a finished Voiceover render says what speech cleanup cut and what it left.

Prod thread ddc6e71b / job 8a0ced82 (stress kit N3, 2026-10-08): the creator asked to cut
"the part where I restart the kilometer thirty sentence". Consented (`required_v1`) cleanup
only ever cuts long pauses and filler sounds, so "At kilometer 30... sorry." stayed in the
video, and the render-ready reply said nothing: that turn had lost its brief binding
(KRI-552), so no requirement receipt could report the limit. The reply now carries a note
read from the variant's own cut record, independent of any brief.

How this could fail (written before the code):
- the note is skipped when the brief binding is lost (the prod case) -> reply stays silent;
- the note over-claims, e.g. says the restart was cut, or says "retakes" were handled;
- it states a count the cut record does not hold (a fabricated number);
- it prints the limit twice next to a receipt that already says it;
- it says the limit where a retake detector really ran and cut a restart;
- Turkish chats get English copy;
- a montage / cloud template variant (no speech spine) picks the note up.

No database: `_approved_generation_review` only reads the thread id, the binding and the variant.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import check_requirement, plan_facts_from_phone_variant
from app.kria.reply_language import bind_reply_language, release_reply_language
from app.tasks import kria_runtime

GENERATION = "d9ff5ff3a2184bfda7719201a4767d7c"
DEFAULT = "Your voiceover edit is ready."

# The words the creator still hears in the finished N3 video (job 8a0ced82 caption cues).
# "At kilometer 30... sorry." is the restarted sentence that stayed in.
_N3_CAPTION_LINES = (
    "I ran my first marathon last Sunday.",
    "Honestly, I almost didn't start.",
    "The night before, I slept maybe three hours.",
    "The first 10 kilometers felt easy.",
    "Too easy.",
    "At kilometer 30...",
    "sorry.",
    "At kilometer 32, my legs just...",
    "stopped.",
    "I walked for a bit.",
    "A random woman handed me a gel and said, you've got this.",
    "I finished in 4 hours and 12 minutes.",
    "And when they put the medal around my neck, I cried.",
    "Like, properly cried.",
    "If you're thinking about signing up for one, do it.",
)

# `variants[0].silence_cut.removed` exactly as prod stored it: 14 silences, 1 filler, no retake.
_N3_REMOVED = [
    {"start_s": 2.685, "end_s": 4.248, "reason": "silence"},
    {"start_s": 6.265, "end_s": 7.175, "reason": "silence"},
    {"start_s": 9.84, "end_s": 10.415, "reason": "silence"},
    {"start_s": 12.585, "end_s": 13.095, "reason": "silence"},
    {"start_s": 13.905, "end_s": 14.575, "reason": "silence"},
    {"start_s": 16.0, "end_s": 16.94, "reason": "filler_lexical"},
    {"start_s": 17.525, "end_s": 19.575, "reason": "silence"},
    {"start_s": 23.885, "end_s": 26.655, "reason": "silence"},
    {"start_s": 32.245, "end_s": 32.895, "reason": "silence"},
    {"start_s": 35.465, "end_s": 36.095, "reason": "silence"},
    {"start_s": 38.005, "end_s": 38.375, "reason": "silence"},
    {"start_s": 39.185, "end_s": 40.335, "reason": "silence"},
    {"start_s": 42.585, "end_s": 43.055, "reason": "silence"},
    {"start_s": 45.405, "end_s": 45.915, "reason": "silence"},
    {"start_s": 46.88, "end_s": 47.42, "reason": "silence"},
]

_RESTART_LIMIT_EN = "a sentence you restarted stays in"
_RESTART_LIMIT_TR = "baştan başladığın bir cümle videoda kalır"
# KRI-846 + KRI-551: a named line is never sent to the editor, and a generic cut record (even one
# with a retake) cannot prove it was removed, so it "may still be" in the video.
_NAMED_CUT_LIMIT_EN = (
    "I can't cut out one exact line yet or confirm it's gone, so it may still be in your video"
)


def _n3_variant(**overrides) -> dict:
    variant = {
        "variant_id": "original_text",
        "render_generation_id": GENERATION,
        "resolved_archetype": "narrated",
        "render_destination": "device",
        "caption_language": "en",
        "voiceover_caption_style": "sentence",
        "caption_cues": [
            {"text": text, "start_s": float(i), "end_s": i + 0.9}
            for i, text in enumerate(_N3_CAPTION_LINES)
        ],
        "silence_cut_outcome": "applied",
        "silence_cut": {
            "removed": [dict(row) for row in _N3_REMOVED],
            "version": 2,
            "time_saved_s": 14.308,
            "original_duration_s": 47.42,
        },
    }
    variant.update(overrides)
    return variant


def _marathon_brief() -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="audio",
                scope="global",
                description="cut long pauses and the restart of the kilometer thirty sentence",
                facts={},
            )
        ],
    )


def _review(variant: dict, *, brief: CreativeBrief | None = None, plan=None, language="en"):
    """The finished-render reply. `brief=None` is the prod binding: `state: "none"`."""
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    binding = BriefBinding.create(thread.id, brief).model_dump(mode="json")
    execution = SimpleNamespace(result={"brief_binding": binding})
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan=dict(plan or {}))
    token = bind_reply_language(language)
    try:
        text, _ = kria_runtime._approved_generation_review(
            None, thread, job, variant, execution, DEFAULT
        )
    finally:
        release_reply_language(token)
    return text


def test_the_n3_restart_is_still_in_the_video_and_the_reply_says_so():
    """The prod case: no brief binding, 14 silences + 1 filler cut, the restart left."""
    variant = _n3_variant()
    assert any(cue["text"] == "At kilometer 30..." for cue in variant["caption_cues"])
    assert not any(row["reason"] == "retake" for row in variant["silence_cut"]["removed"])

    text = _review(variant, plan={"speech_cleanup_contract": "required_v1"})

    assert text.startswith(DEFAULT)
    assert "Speech cleanup cut 14 long pauses and 1 filler sound (14.3 s)." in text
    assert _RESTART_LIMIT_EN in text
    assert "editor" not in text  # KRI-846: no editor action cuts a spoken line
    # Never claims the restart (or any retake) was handled.
    assert "retakes were" not in text
    assert "removed the restart" not in text.lower()


def test_the_n3_reply_speaks_turkish_in_a_turkish_chat():
    text = _review(
        _n3_variant(caption_language="tr"),
        plan={"speech_cleanup_contract": "required_v1"},
        language="tr",
    )

    assert "Konuşma temizliği şunları kesti: 14 uzun duraklama, 1 dolgu sesi (14,3 sn)." in text
    assert _RESTART_LIMIT_TR in text
    assert "editör" not in text  # KRI-846
    assert _RESTART_LIMIT_EN not in text


def test_a_bound_brief_that_already_reports_the_restart_limit_is_not_told_twice():
    text = _review(_n3_variant(), brief=_marathon_brief())

    assert text.count("can't cut out one exact line yet") == 1
    assert _RESTART_LIMIT_EN not in text
    # The counts are new information, so they stay.
    assert "Speech cleanup cut 14 long pauses and 1 filler sound" in text


def test_a_bound_n3_brief_with_a_recorded_retake_reports_it_but_keeps_the_line_partial():
    variant = _n3_variant()
    variant["silence_cut"]["removed"].append({"start_s": 14.06, "end_s": 20.15, "reason": "retake"})

    receipt = check_requirement(
        _marathon_brief().requirements[0], plan_facts_from_phone_variant(variant)
    )
    text = _review(variant, brief=_marathon_brief())

    assert receipt.status == "partial"
    assert _NAMED_CUT_LIMIT_EN in (receipt.reason or "")
    assert "1 restarted sentence" in text
    assert "isn't cut automatically yet" not in text
    assert _RESTART_LIMIT_EN not in text
    # A retake count does not prove this brief's kilometer-thirty line was the one removed.
    assert _NAMED_CUT_LIMIT_EN in text
    assert "Speech cleanup cut 14 long pauses, 1 filler sound and 1 restarted sentence" in text


def test_an_unrelated_recorded_retake_never_marks_a_requested_line_done():
    req = BriefRequirement(
        id="r-unrelated",
        kind="audio",
        scope="global",
        description="cut the part where I say 'I almost didn't start'",
        facts={},
    )
    variant = _n3_variant()
    variant["silence_cut"]["removed"].append({"start_s": 14.06, "end_s": 20.15, "reason": "retake"})

    receipt = check_requirement(req, plan_facts_from_phone_variant(variant))

    assert receipt.status == "partial"
    assert receipt.reason == (f"Speech cleanup cut the long pauses; {_NAMED_CUT_LIMIT_EN}")


def test_counts_come_only_from_the_cut_record():
    variant = _n3_variant()
    variant["silence_cut"]["removed"] = [
        {"start_s": 1.0, "end_s": 2.0, "reason": "silence"},
        {"start_s": 5.0, "end_s": 5.5, "reason": "filler_acoustic"},
        {"start_s": 8.0, "end_s": 9.0, "reason": "filler_lexical"},
    ]
    variant["silence_cut"]["time_saved_s"] = 2.5

    text = _review(variant, plan={"speech_cleanup_contract": "required_v1"})

    assert "Speech cleanup cut 1 long pause and 2 filler sounds (2.5 s)." in text


def test_a_cut_record_with_no_rows_claims_no_numbers():
    variant = _n3_variant()
    del variant["silence_cut"]

    text = _review(variant, plan={"speech_cleanup_contract": "required_v1"})

    assert "Speech cleanup cut the long pauses." in text
    assert _RESTART_LIMIT_EN in text
    assert not any(char.isdigit() for char in text.removeprefix(DEFAULT))


def test_a_run_that_found_nothing_still_says_restarts_stay():
    variant = _n3_variant(silence_cut_outcome="no_change")
    del variant["silence_cut"]

    text = _review(variant, plan={"speech_cleanup_contract": "required_v1"})

    assert "Speech cleanup found no long pauses to cut." in text
    assert _RESTART_LIMIT_EN in text


def test_a_restart_the_detector_really_cut_is_reported_not_denied(monkeypatch):
    """Legacy auto-cleanup with the retake lever on can cut a restart: never say it can't."""
    monkeypatch.setattr(kria_runtime.settings, "retake_cut_enabled", True)
    variant = _n3_variant()
    variant["silence_cut"]["removed"].append({"start_s": 10.4, "end_s": 12.2, "reason": "retake"})

    text = _review(variant, plan={})

    assert "1 restarted sentence" in text
    assert _RESTART_LIMIT_EN not in text


def test_the_limit_is_not_stated_where_a_retake_detector_could_have_run(monkeypatch):
    monkeypatch.setattr(kria_runtime.settings, "retake_cut_enabled", True)

    text = _review(_n3_variant(), plan={})

    assert "Speech cleanup cut 14 long pauses and 1 filler sound" in text
    assert _RESTART_LIMIT_EN not in text


@pytest.mark.parametrize(
    "variant",
    [
        {"resolved_archetype": "montage", "silence_cut_outcome": "applied"},
        {"variant_id": "song_text", "silence_cut_outcome": "applied"},
        {"resolved_archetype": "narrated"},
        {"resolved_archetype": "narrated", "silence_cut_outcome": "not_run"},
    ],
)
def test_a_render_with_no_speech_cleanup_gets_no_note(variant):
    full = {"variant_id": "v1", "render_generation_id": GENERATION, **variant}

    assert _review(full, plan={"speech_cleanup_contract": "required_v1"}) == DEFAULT


def test_the_talking_whole_take_receipt_does_not_promise_retake_cuts():
    """`speech_cleanup_enabled` with 'keep my whole take': pauses and fillers, not retakes."""
    from app.kria.brief_checks import PlanFacts, check_requirement

    req = BriefRequirement(
        id="r1",
        kind="timing",
        scope="global",
        description="keep my whole take",
        facts={"keep_whole_take": True},
    )
    facts = PlanFacts(
        edit_format="subtitled",
        video_clip_count=1,
        speech_cleanup_enabled=True,
    )
    for language, forbidden in (("en", "retake"), ("tr", "tekrar çekim")):
        token = bind_reply_language(language)
        try:
            receipt = check_requirement(req, facts)
        finally:
            release_reply_language(token)
        assert receipt.reason, language
        assert forbidden not in receipt.reason.lower(), receipt.reason
