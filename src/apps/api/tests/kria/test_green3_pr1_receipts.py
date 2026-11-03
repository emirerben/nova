"""Green 3 (2026-11-03): what the chat says about captions, music, cleanup and the backgrounds.

Prod run: chat "Green 3", thread 5A650474-5103-4A82-BF2A-3F7C46F5BD56, plan item 57d612ee,
job 9bad2d75 (TestFlight 1876, Fly 72c060c0b). Turn 1 (event seq 7) held the draft:

    I can't make this yet:
    - Cut the pauses and the sentence I restarted.: If Clean up speech is offered when you
      approve, choose it to cut the long pauses; I'll cut the line you named ...
    - Captions small, white, lowercase, in the middle.: I can't confirm this draft does it yet.
    - No music.: A Talking video can't do this, so it stays as filmed

and "Make it without these" left out all three, though the approved render ran the cleanup
(3 pauses, 964 ms). The finished video's review (seq 18) said "8 photos or videos take turns
behind you" for 8 windows over 5 photos / videos.

Failure modes F1-F4, F6, F15, F16, N1-N5, C1-C3, K2-K4 of `.dev/failure-modes.md`. Built on
the KRI-1161 readiness harness (`test_brief_checks_speaker_background`), whose manifest and
strategy are a phone Talking draft like Green 3's. No model calls.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_phone_variant,
    renders_bed_mute,
    wants_bed_muted,
    wants_bed_under_voice,
)
from app.kria.portable_speaker_background import SPEAKER_FRAME_PRESETS
from app.schemas.phone_text_style import explicit_text_rule
from tests.kria.test_brief_checks_speaker_background import (
    CREATOR,
    OTHER,
    PHOTO,
    STRATEGY,
    TAKE,
    _draft_facts,
    _gate,  # noqa: F401 - autouse phone Talking gates
    _manifest,
    _open_windows,
    _variant,
)

GREEN3_PROMPT = (
    "Yorumla episode 9: Kervan x Lumo Pamukkale show. Make the whole video like a green screen: "
    "me small at the bottom right in front, and behind me full screen whichever photo or video "
    "I'm talking about, changing every 3-5 seconds. Cut the pauses and the sentence I restarted. "
    "Captions small, white, lowercase, in the middle. No music."
)


FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "green3_9bad2d75_caption_look_no_music.json"
    ).read_text(encoding="utf-8")
)
PROD_ROWS = {row["requirement_id"]: row for row in FIXTURE["turn1_readiness_rows"]}


def _r(rid: str, kind: str, description: str, facts: dict) -> BriefRequirement:
    return BriefRequirement(id=rid, kind=kind, scope="global", description=description, facts=facts)


# Turn-1 brief v1, exactly as stored on job 9bad2d75 (minus the later `superseded_by`).
R1 = _r(
    "r1",
    "style",
    "Make the whole video like a green screen: me small at the bottom right in front, and behind "
    "me full screen whichever photo or video I'm talking about, changing every 3-5 seconds",
    {"speaker_small": True, "background_changes": True, "speaker_background": True},
)
R2 = _r(
    "r2",
    "style",
    "Cut the pauses and the sentence I restarted.",
    {"cut_lines": ["So Lumo took its tech from cold hard, no again."], "speech_cleanup": True},
)
R3_PROD = _r(
    "r3",
    "style",
    "Captions small, white, lowercase, in the middle.",
    {"captions": "on", "position": "middle"},
)
R4_PROD = _r("r4", "audio", "No music.", {})
# The same two asks as the brief writer records them once taught (this PR's prompt).
LOOK = {
    "captions": "on",
    "caption_size": "small",
    "caption_color": "#FFFFFF",
    "caption_case": "lower",
    "caption_position": "middle",
}
R3 = _r("r3", "style", "Captions small, white, lowercase, in the middle.", LOOK)
R4 = _r("r4", "audio", "No music.", {"audio_intent": "no_music"})

GREEN3_BACKGROUND = {
    "media_id": PHOTO,
    "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}],
    "speaker_frame": "small_bottom_right",
}


@pytest.fixture(autouse=True)
def _phone_text_styling(monkeypatch: pytest.MonkeyPatch) -> None:
    """On in prod: job 9bad2d75 was stamped with `phone_text_style` (code default is off)."""
    from app.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)


def _ask():
    """The typed-look module, imported per test so origin/main shows each test's own failure."""
    from app.kria import caption_look_ask  # noqa: PLC0415

    return caption_look_ask


def _one(req, facts):
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def test_the_inline_green3_data_is_the_prod_record():
    assert GREEN3_PROMPT == FIXTURE["request"]
    stored = {row["id"]: row for row in FIXTURE["brief_v1_requirements"]}
    for req in (R1, R2, R3_PROD, R4_PROD):
        row = stored[req.id]
        assert (req.kind, req.description, req.facts) == (
            row["kind"],
            row["description"],
            row["facts"],
        )


# ------------------------------------------------------------------- 1. the caption parser


def test_green3_prompt_parses_to_the_captions_ask_not_the_speakers_corner():
    # origin/main: ('caption', {'position': 'bottom_right'}) -- the speaker's corner.
    assert explicit_text_rule(GREEN3_PROMPT) == (
        "caption",
        {"position": "middle", "color": "#FFFFFF"},
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Unchanged: a place or colour the text sentence names.
        ("Put the title at the top left", ("title", {"position": "top_left"})),
        ("Always put titles at the top left", ("title", {"position": "top_left"})),
        ("White captions please", ("caption", {"color": "#FFFFFF"})),
        ("Captions should be yellow", ("caption", {"color": "#FFFF00"})),
        # The speaker, a photo or a card is never the text's position.
        ("Put me small at the bottom right", None),
        (
            "Me at the bottom right, captions at the bottom left",
            ("caption", {"position": "bottom_left"}),
        ),
        (
            "Put the logo at the bottom right and the captions in the middle",
            ("caption", {"position": "middle"}),
        ),
        ("At the bottom right, add captions", ("caption", {"position": "bottom_right"})),
        # The creator's first person about the captions, a sentence later (code review).
        (
            "Captions in white. I'd like them in the middle",
            ("caption", {"position": "middle", "color": "#FFFFFF"}),
        ),
        (
            "I'm small at the bottom right. Captions in the middle",
            ("caption", {"position": "middle"}),
        ),
        # A colour about something else is not the text's colour (code review: was None).
        ("Make the title yellow and the background red", ("title", {"color": "#FFFF00"})),
        ("Captions in the middle on a white background", ("caption", {"position": "middle"})),
        # Turkish: "subtitles small, white, in the middle; I'm small at the bottom left".
        (
            "Altyazılar küçük, beyaz, ortada. Ben sol altta küçük olayım.",
            ("caption", {"position": "middle", "color": "#FFFFFF"}),
        ),
    ],
)
def test_caption_parser_neighbours(text, expected):
    assert explicit_text_rule(text) == expected


# ------------------------------------------------------------- 2. the typed look on the brief


def test_the_typed_look_is_read_from_the_brief_facts():
    ask = _ask().caption_look_ask(R3)
    assert ask is not None
    assert ask.as_dict() == {
        "size": "small",
        "position": "middle",
        "color": "#FFFFFF",
        "case": "lower",
    }
    assert _ask().CAPTION_ASK_SIZE_PX["small"] < 78  # smaller than the default caption
    # Green 3's stored r3 had only an untaught `position`: no typed look, no word reading.
    assert _ask().caption_look_ask(R3_PROD) is None


@pytest.mark.parametrize(
    "facts",
    [
        {"captions": "on", "caption_size": "tiny-ish"},
        {"captions": "on", "caption_color": "#FFF"},
        {"captions": "on", "caption_color": "sky"},
        {"captions": "on", "caption_case": "sentence"},
        {"captions": "on", "caption_position": "sideways"},
    ],
)
def test_an_invalid_typed_value_is_dropped_not_applied(facts):
    assert _ask().caption_look_ask(_r("r3", "style", "Captions.", facts)) is None


def test_a_colour_word_becomes_its_hex():
    ask = _ask().caption_look_ask(_r("r3", "style", "Captions.", {"caption_color": "white"}))
    assert ask is not None and ask.color == "#FFFFFF"


def test_only_live_requirements_count():
    brief = CreativeBrief(
        version=2, requirements=[R1, R3.model_copy(update={"status": "superseded"})]
    )
    assert _ask().brief_caption_look(brief) is None
    assert _ask().brief_caption_look(CreativeBrief(version=1, requirements=[R1, R3])) is not None


def test_the_corner_speaker_overlap_is_geometry_not_words():
    assert _ask().caption_over_speaker("bottom_right", "small_bottom_right")
    assert _ask().caption_over_speaker("bottom", "small_bottom_right")
    assert not _ask().caption_over_speaker("middle", "small_bottom_right")
    assert not _ask().caption_over_speaker("top", "small_bottom_right")
    assert not _ask().caption_over_speaker("bottom_right", None)


# ---------------------------------------------------- 3. the draft's receipts (turn 1, seq 7)


def test_green3_draft_says_the_caption_look_is_done_on_the_phone_writer(monkeypatch):
    _open_windows(monkeypatch)
    facts = dataclasses.replace(
        _draft_facts(_manifest(PHOTO, OTHER), background_media=GREEN3_BACKGROUND),
        caption_words_at_render=True,
    )
    receipt = _one(R3, facts)
    # Prod: partial / unchecked "I can't check the captions on this draft yet."
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == "Captions are small, white, lowercase and in the middle."


def test_a_draft_ask_on_the_corner_speaker_is_done_and_said(monkeypatch):
    _open_windows(monkeypatch)
    facts = dataclasses.replace(
        _draft_facts(_manifest(PHOTO, OTHER), background_media=GREEN3_BACKGROUND),
        caption_words_at_render=True,
    )
    bottom = _r("r3", "style", "Captions at the bottom.", {"caption_position": "bottom"})
    receipt = _one(bottom, facts)
    assert receipt.status == "met"
    assert receipt.reason.startswith("Captions are at the bottom")
    assert "over you in the corner" in receipt.reason


def test_with_phone_text_styling_off_the_draft_claims_nothing(monkeypatch):
    """The writer stamps no policy with the flag off, so no look is drawn and none is claimed."""
    from app.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "phone_text_styling_enabled", False)
    facts = dataclasses.replace(_draft_facts(_manifest()), caption_words_at_render=True)
    receipt = _one(R3, facts)
    assert receipt.status != "met"


def test_a_draft_with_captions_off_claims_no_look():
    facts = dataclasses.replace(
        _draft_facts(_manifest(), caption_style="none"), caption_words_at_render=True
    )
    assert facts.caption_style == "none"
    assert _one(R3, facts).status != "met"


def test_a_route_that_does_not_draw_the_look_still_says_it_cannot_check():
    """F16: only the phone Talking writer applies the typed look."""
    facts = dataclasses.replace(_draft_facts(_manifest()), caption_words_at_render=False)
    receipt = _one(R3, facts)
    assert receipt.status == "partial"
    assert receipt.verification == "unchecked"


def test_green3_no_music_is_done_on_a_phone_talking_draft():
    receipt = _one(R4, _draft_facts(_manifest()))
    # Prod: not_possible "A Talking video can't do this, so it stays as filmed".
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == "No music, as asked."


def test_no_music_is_done_on_a_cloud_talking_draft():
    facts = dataclasses.replace(
        _draft_facts(_manifest(edit_format="talking_head"), edit_format="talking_head"),
        phone_talking=False,
    )
    assert _one(R4, facts).status == "met"


@pytest.mark.parametrize("audio", ["licensed_music", "user_song"])
def test_no_music_is_never_met_on_a_plan_with_music(audio):
    receipt = _one(R4, _draft_facts(_manifest(), audio_strategy=audio))
    assert receipt.status != "met"


def test_no_music_is_not_a_mute_of_the_footage_sound():
    assert not wants_bed_muted(R4)
    assert not wants_bed_under_voice(R4)
    assert not renders_bed_mute(R4)


def test_an_untyped_no_music_keeps_todays_answer():
    """N4: briefs written before the typed fact are never re-read by their words."""
    receipt = _one(R4_PROD, _draft_facts(_manifest()))
    assert receipt.status == "not_possible"


# ------------------------------------------------------- 4. readiness and "Leaves out" (C1-C3)


def _readiness(requirements, manifest, **strategy):
    brief = CreativeBrief(version=1, requirements=requirements)
    report = assess_readiness(
        brief,
        {**STRATEGY, **strategy},
        media={"clip_paths": [TAKE]},
        manifest=manifest,
        creator_id=CREATOR,
    )
    return {row["requirement_id"]: row for row in report["requirements"]}, report


def test_green3_turn1_holds_only_the_restarted_sentence_with_the_typed_brief(monkeypatch):
    _open_windows(monkeypatch)
    rows, report = _readiness(
        [R1, R2, R3, R4], _manifest(PHOTO, OTHER), background_media=GREEN3_BACKGROUND
    )
    # Prod: r2 needs_choice, r3 evidence_missing, r4 unsupported -> "Make it without these".
    # Backgrounds, captions and no music are approvable now. r2 also names the restarted
    # sentence, which KRI-1176 (#2090) honestly says may stay in while the retake path is off,
    # so r2 alone is held; the pauses-only ask below pins KRI-1178 on the cleanup choice.
    assert {rid: row["disposition"] in APPROVABLE for rid, row in rows.items()} == {
        "r1": True,
        "r2": False,
        "r3": True,
        "r4": True,
    }, rows
    assert rows["r2"]["disposition"] == "needs_choice"
    assert report["ready"] is False
    # The cleanup line is still what the creator reads first about r2, then the honest part.
    assert rows["r2"]["reason"].startswith("If Clean up speech is offered when you approve")
    assert rows["r2"]["reason"].endswith("so it may still be in your video")


def test_green3_prod_brief_replays_prod_but_drops_the_named_line_promise(monkeypatch):
    _open_windows(monkeypatch)
    rows, report = _readiness(
        [R1, R2, R3_PROD, R4_PROD], _manifest(PHOTO, OTHER), background_media=GREEN3_BACKGROUND
    )
    # The replay says exactly what prod said (event seq 7) about captions and music ...
    for rid in ("r3", "r4"):
        assert rows[rid]["reason"] == PROD_ROWS[rid]["reason"], rid
    # ... and keeps prod's cleanup sentence for r2 without its false "I'll cut the line you
    # named" (KRI-1176, #2090). r2 stays held for that line, as in prod.
    prod_cleanup, _ = PROD_ROWS["r2"]["reason"].split("; ", 1)
    assert rows["r2"]["reason"].startswith(prod_cleanup + "; "), rows["r2"]
    assert "I'll cut the line you named" in PROD_ROWS["r2"]["reason"]
    assert "I'll cut the line you named" not in rows["r2"]["reason"]
    assert rows["r2"]["disposition"] == PROD_ROWS["r2"]["disposition"] == "needs_choice"
    # The untyped asks keep today's verdicts (no word reading).
    assert rows["r3"]["disposition"] == PROD_ROWS["r3"]["disposition"] == "evidence_missing"
    assert rows["r4"]["disposition"] == PROD_ROWS["r4"]["disposition"] == "unsupported"
    assert report["ready"] is False


def test_a_pauses_only_cleanup_ask_is_approvable():
    """C1 without the restart words: the Clean up speech choice alone is never "left out".

    The Green 3 r2 cases above also name the restarted sentence; KRI-1176 (PR 3) words that part
    "stays in" while the retake path is off, which rightly holds it. This one pins KRI-1178 on
    the cleanup choice alone."""
    ask = _r("r2", "style", "Cut the long pauses.", {"speech_cleanup": True})
    rows, report = _readiness([ask], _manifest())
    assert rows["r2"]["disposition"] == "unverified", rows["r2"]
    assert rows["r2"]["reason"].startswith("If Clean up speech is offered when you approve")
    assert report["ready"] is True


def test_a_line_the_render_cannot_find_still_holds_the_draft():
    """C2: with no recorded line to cut, "the sentence I restarted" may stay in the video."""
    ask = _r(
        "r2", "style", "Cut the pauses and the sentence I restarted.", {"speech_cleanup": True}
    )
    rows, _report = _readiness([ask], _manifest())
    assert rows["r2"]["disposition"] == "needs_choice", rows["r2"]
    assert "it may still be in your video" in rows["r2"]["reason"]


def test_a_length_the_cleanup_ask_also_names_still_holds_the_draft():
    """C2: "cut the pauses and keep it under 10 s" on a 24.6 s take is short of the length."""
    ask = _r(
        "r2",
        "style",
        "Cut the long pauses and keep it under 10 seconds.",
        {"speech_cleanup": True, "duration_s": 10},
    )
    rows, _report = _readiness([ask], _manifest())
    assert rows["r2"]["disposition"] == "needs_choice", rows["r2"]


# ----------------------------------------------------------- 5. how many photos (K1-K4)


def test_a_draft_sequence_naming_a_photo_twice_counts_it_once(monkeypatch):
    _open_windows(monkeypatch)
    background = {
        "media_id": PHOTO,
        "sequence": [{"media_id": PHOTO}, {"media_id": OTHER}, {"media_id": PHOTO}],
        "speaker_frame": "small_bottom_right",
    }
    facts = _draft_facts(_manifest(PHOTO, OTHER), background_media=background)
    receipt = _one(R1, facts)
    assert receipt.status == "met"
    assert receipt.reason.startswith("2 photos or videos take turns behind you")


def test_green3_finished_video_counts_distinct_photos_not_windows():
    facts = plan_facts_from_phone_variant(
        _variant(
            phone_lane_receipt={
                "applied": ["background"],
                "dropped": [],
                "background": {"windows": 8, "media": 5, "speaker_frame": "small_bottom_right"},
            }
        )
    )
    receipt = _one(R1, facts)
    # Prod: "8 photos or videos take turns behind you".
    assert receipt.reason == (
        "5 photos or videos take turns behind you; you're small at the bottom right."
    )


def _drawn_variant(**changes):
    """A finished Green 3 variant as the writer leaves it with the typed look drawn."""
    variant = _variant(
        caption_cues=[{"text": "kervan celebrated its 10th year", "start_s": 0.0, "end_s": 2.0}],
        caption_size_px=52,
        caption_text_color="#FFFFFF",
        caption_editor_style={
            "alignment": "center",
            "position_x_frac": 0.5,
            "position_y_frac": 0.5,
        },
        caption_look_receipt={
            **LOOK_DICT,
            "size_px": 52,
            "position_y_frac": 0.5,
            "over_face": False,
        },
    )
    variant.update(changes)
    return variant


LOOK_DICT = {"size": "small", "position": "middle", "color": "#FFFFFF", "case": "lower"}


def test_a_finished_video_carrying_the_look_is_met():
    receipt = _one(R3, plan_facts_from_phone_variant(_drawn_variant()))
    assert receipt.status == "met", receipt


@pytest.mark.parametrize(
    "changes",
    [
        # A KRI-827 mark (or an editor drag) moved the block off the asked spot.
        {"caption_editor_style": {"alignment": "center", "position_y_frac": 0.7}},
        {"caption_text_color": "#FFFF00"},
        {"caption_size_px": 78},
        {"caption_cues": [{"text": "Kervan celebrated", "start_s": 0.0, "end_s": 2.0}]},
    ],
)
def test_a_finished_video_that_lost_the_look_is_not_met(changes):
    receipt = _one(R3, plan_facts_from_phone_variant(_drawn_variant(**changes)))
    assert receipt.status == "partial", receipt
    assert receipt.reason == "The captions no longer look the way you asked."


def test_an_editor_saved_background_counts_distinct_media():
    facts = plan_facts_from_phone_variant(
        _variant(
            speaker_background={
                "media_id": PHOTO,
                "speaker_frame": dict(SPEAKER_FRAME_PRESETS["small_bottom_right"]),
                "windows": [
                    {"media_id": PHOTO, "start_s": 0, "end_s": 4},
                    {"media_id": OTHER, "start_s": 4, "end_s": 8},
                    {"media_id": PHOTO, "start_s": 8, "end_s": 12},
                    {"media_id": OTHER, "start_s": 12, "end_s": 16},
                ],
            }
        )
    )
    receipt = _one(R1, facts)
    assert receipt.reason.startswith("2 photos or videos take turns behind you")
