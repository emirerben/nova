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


@pytest.mark.parametrize(
    "key", ["caption_size", "caption_position", "caption_color", "caption_case"]
)
@pytest.mark.parametrize("value", [[], {}, None, 3, ["small"], {"size": "small"}])
def test_a_non_text_typed_value_is_dropped_and_never_raises(key, value):
    """Emir's #2089 review: model output such as ``"caption_size": []`` raised TypeError (an
    unhashable value tested against a dict), which would fail every receipt of the turn."""
    req = _r("r3", "style", "Captions.", {"captions": "on", key: value})
    assert _ask().caption_look_ask(req) is None
    facts = dataclasses.replace(_draft_facts(_manifest()), caption_words_at_render=True)
    _one(req, facts)  # never raises


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


# Emir's #2089 review: a typed look shares its requirement with the captions' language.
TURKISH_ONLY = _r(
    "r3", "style", "Captions in Turkish.", {"captions": "on", "caption_language": "tr"}
)
TURKISH_LOOK = _r(
    "r3",
    "style",
    "Captions small, white, in Turkish.",
    {
        "captions": "on",
        "caption_language": "tr",
        "caption_size": "small",
        "caption_color": "#FFFFFF",
    },
)


def test_a_typed_look_never_hides_the_captions_language_on_a_draft():
    facts = dataclasses.replace(_draft_facts(_manifest()), caption_words_at_render=True)
    language = _one(TURKISH_ONLY, facts)
    both = _one(TURKISH_LOOK, facts)
    assert language.status != "met"  # the language is checked on the finished video
    # Was "met" with only the look line: the Turkish ask was never judged or said.
    assert (both.status, both.verification) == (language.status, language.verification)
    assert language.reason in both.reason


def test_a_typed_look_never_hides_a_word_by_word_miss():
    """The other half of the same review: sentence captions for a word-by-word ask are a miss."""
    ask = _r(
        "r3",
        "style",
        "Small captions, word by word.",
        {"captions": "word_by_word", "caption_size": "small"},
    )
    facts = dataclasses.replace(
        _draft_facts(_manifest(), caption_style="clean"), caption_words_at_render=True
    )
    receipt = _one(ask, facts)
    assert receipt.status == "partial", receipt
    assert receipt.reason == "Captions are on as full sentences, not word by word."


def test_readiness_treats_a_look_and_language_ask_like_the_language_ask():
    rows_language, _ = _readiness([TURKISH_ONLY], _manifest())
    rows_both, _ = _readiness([TURKISH_LOOK], _manifest())
    assert rows_both["r3"]["disposition"] == rows_language["r3"]["disposition"]
    assert rows_language["r3"]["reason"] in rows_both["r3"]["reason"]


def test_a_finished_video_in_the_wrong_language_is_not_met_for_its_look():
    variant = _drawn_variant(caption_language="en")
    receipt = _one(TURKISH_LOOK, plan_facts_from_phone_variant(variant))
    # The look is still drawn, but the captions are English, not Turkish.
    assert receipt.status != "met", receipt
    language = _one(TURKISH_ONLY, plan_facts_from_phone_variant(variant))
    assert language.status != "met"
    assert language.reason in receipt.reason


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
        caption_cues=[
            {"id": "c1", "text": "kervan celebrated its 10th year", "start_s": 0.0, "end_s": 2.0},
            {"id": "c2", "text": "with a sunrise fashion show", "start_s": 2.0, "end_s": 4.0},
        ],
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
            "position_x_frac": 0.5,
            "position_y_frac": 0.5,
            "over_face": False,
        },
    )
    variant.update(changes)
    return variant


def _cues_with(index: int, **extra) -> list[dict]:
    cues = _drawn_variant()["caption_cues"]
    cues[index] = {**cues[index], **extra}
    return cues


def _style_with(**extra) -> dict:
    return {**_drawn_variant()["caption_editor_style"], **extra}


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
        # Emir's second #2089 review: the block dragged sideways (alignment unchanged) ...
        {"caption_editor_style": _style_with(position_x_frac=0.7)},
        # ... one caption moved or resized in the editor (per-line overrides the render uses) ...
        {"caption_editor_style": _style_with(line_overrides={"c2": {"position_y_frac": 0.7}})},
        {"caption_editor_style": _style_with(line_overrides={"c2": {"position_x_frac": 0.3}})},
        {"caption_editor_style": _style_with(line_overrides={"c2": {"size_px": 78}})},
        # ... or one cue's own size / colour.
        {"caption_cues": _cues_with(1, size_px=78)},
        {"caption_cues": _cues_with(1, text_color="#FFFF00")},
    ],
)
def test_a_finished_video_that_lost_the_look_is_not_met(changes):
    receipt = _one(R3, plan_facts_from_phone_variant(_drawn_variant(**changes)))
    assert receipt.status == "partial", receipt
    assert receipt.reason == "The captions no longer look the way you asked."


def test_an_override_that_keeps_the_drawn_look_is_still_met():
    style = _style_with(line_overrides={"c2": {"size_px": 52, "position_y_frac": 0.5}})
    variant = _drawn_variant(caption_editor_style=style, caption_cues=_cues_with(0, size_px=52))
    receipt = _one(R3, plan_facts_from_phone_variant(variant))
    assert receipt.status == "met", receipt


def test_a_receipt_without_the_drawn_x_is_unknown_not_met():
    """Where the drawn spot is not recorded, nothing is claimed (the usual "can't check")."""
    receipt_without_x = {**_drawn_variant()["caption_look_receipt"]}
    receipt_without_x.pop("position_x_frac")
    receipt = _one(
        R3, plan_facts_from_phone_variant(_drawn_variant(caption_look_receipt=receipt_without_x))
    )
    assert receipt.status != "met", receipt


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


# ------------------------------------------------ KRI-1472: "Only my voice." (Q1 run 3)
#
# Prod 2026-11-14 (thread 05aa3a87, job 3ef3cbe5): "Only my voice, no music." was filed as two
# rows: "No music." (typed `no_music`, met) and "Only my voice." as an `audio` row whose only
# fact is `voice_only: true`. No checker owned that row, so the Talking catch-all refused it
# ("A Talking video can't do this, so it stays as filmed"), the draft was held and "Make it
# without these" listed it as left out, though a Talking edit with no music IS only her voice.
#
# Ways the fix can go wrong, written before the code:
#  V1 the prod row is still refused on a phone Talking draft;   V2 or in Turkish;
#  V3 a cloud Talking draft differs;       V4 a plan with music is called met;
#  V5 a finished video with music is called met;   V6 the switch off is not today's answer;
#  V7 another sound ask a Talking edit can't do ("louder voice", a non-true value, one clip's
#     row) stops being refused;            V8 another typed sound intent on the row is overruled;
#  V9 only one spelling of the fact is read;       V10 it is read as a mute of the footage;
#  V11 a montage's answer changes;         V12 the reply renames the ask "No music";
#  V13 a montage's music fallback (the planner's `asks_no_music`) changes.

TALKING_CANT = "A Talking video can't do this, so it stays as filmed"
VOICE_ONLY_MET = "No music, as asked."
R7_RUN3 = _r("r7", "audio", "Only my voice.", {"voice_only": True})


def _voice_switch(monkeypatch, value: bool) -> None:
    from app.config import settings  # noqa: PLC0415

    monkeypatch.setattr(settings, "voice_only_ask_enabled", value)


def test_v1_run3_only_my_voice_is_done_on_a_phone_talking_draft(monkeypatch):
    _voice_switch(monkeypatch, True)
    receipt = _one(R7_RUN3, _draft_facts(_manifest()))
    assert (receipt.status, receipt.verification) == ("met", "checked")
    assert receipt.reason == VOICE_ONLY_MET


def test_v2_in_turkish(monkeypatch):
    from app.kria.reply_language import reply_language_for  # noqa: PLC0415

    _voice_switch(monkeypatch, True)
    with reply_language_for("tr"):
        receipt = _one(R7_RUN3, _draft_facts(_manifest()))
    assert (receipt.status, receipt.reason) == ("met", "İstediğin gibi müzik yok.")


def test_v3_done_on_a_cloud_talking_draft_too(monkeypatch):
    _voice_switch(monkeypatch, True)
    facts = dataclasses.replace(
        _draft_facts(_manifest(edit_format="talking_head"), edit_format="talking_head"),
        phone_talking=False,
    )
    assert _one(R7_RUN3, facts).status == "met"


@pytest.mark.parametrize("audio", ["licensed_music", "user_song"])
def test_v4_never_met_on_a_plan_with_music(monkeypatch, audio):
    _voice_switch(monkeypatch, True)
    receipt = _one(R7_RUN3, _draft_facts(_manifest(), audio_strategy=audio))
    assert (receipt.status, receipt.reason) == ("partial", "This video has music.")


def test_v5_never_met_on_a_finished_video_with_music(monkeypatch):
    _voice_switch(monkeypatch, True)
    facts = dataclasses.replace(plan_facts_from_phone_variant(_variant()), music_in_render=True)
    receipt = _one(R7_RUN3, facts)
    assert (receipt.status, receipt.reason) == ("partial", "This video has music.")
    assert _one(R7_RUN3, plan_facts_from_phone_variant(_variant())).status == "met"


def test_v6_switch_off_is_todays_refusal(monkeypatch):
    _voice_switch(monkeypatch, False)
    receipt = _one(R7_RUN3, _draft_facts(_manifest()))
    assert (receipt.status, receipt.verification) == ("not_possible", "checked")
    assert receipt.reason == TALKING_CANT


@pytest.mark.parametrize(
    "req",
    [
        _r("a", "audio", "Louder voice.", {}),
        _r("b", "audio", "Louder voice.", {"louder_voice": True}),
        _r("c", "audio", "Only my voice.", {"voice_only": False}),
        _r("d", "audio", "Only my voice.", {"voice_only": "yes"}),
        BriefRequirement(
            id="e",
            kind="audio",
            scope=f"clip:{TAKE}",
            description="Only my voice on this clip.",
            facts={"voice_only": True},
        ),
    ],
    ids=["no-fact", "other-fact", "false", "not-a-bool", "one-clip"],
)
def test_v7_other_sound_asks_on_talking_are_answered_as_before(monkeypatch, req):
    _voice_switch(monkeypatch, True)
    on = _one(req, _draft_facts(_manifest()))
    _voice_switch(monkeypatch, False)
    off = _one(req, _draft_facts(_manifest()))
    assert on == off and on.reason != VOICE_ONLY_MET


@pytest.mark.parametrize("intent", ["source_muted", "source_unchanged", "no_music"])
def test_v8_another_typed_sound_intent_on_the_row_keeps_its_own_answer(monkeypatch, intent):
    req = _r("r7", "audio", "Only my voice.", {"voice_only": True, "audio_intent": intent})
    _voice_switch(monkeypatch, True)
    on = _one(req, _draft_facts(_manifest()))
    _voice_switch(monkeypatch, False)
    assert on == _one(req, _draft_facts(_manifest()))


@pytest.mark.parametrize(
    "facts",
    [{"only_voice": True}, {"only_my_voice": True}, {"audio_intent": "voice_only"}],
)
def test_v9_the_other_spellings_of_the_fact_are_read(monkeypatch, facts):
    _voice_switch(monkeypatch, True)
    receipt = _one(_r("r7", "audio", "Nur meine Stimme.", facts), _draft_facts(_manifest()))
    assert (receipt.status, receipt.reason) == ("met", VOICE_ONLY_MET)


def test_v10_it_is_not_a_mute_of_the_footage_sound(monkeypatch):
    _voice_switch(monkeypatch, True)
    assert not wants_bed_muted(R7_RUN3)
    assert not wants_bed_under_voice(R7_RUN3)
    assert not renders_bed_mute(R7_RUN3)


def test_v11_a_montage_answers_as_before(monkeypatch):
    facts = dataclasses.replace(
        _draft_facts(_manifest(edit_format="montage"), edit_format="montage"), phone_talking=False
    )
    _voice_switch(monkeypatch, True)
    on = _one(R7_RUN3, facts)
    _voice_switch(monkeypatch, False)
    assert on == _one(R7_RUN3, facts) and on.status != "met"


def test_v12_the_reply_keeps_her_words_for_the_ask(monkeypatch):
    from app.kria.brief_checks import ask_text  # noqa: PLC0415

    _voice_switch(monkeypatch, True)
    assert ask_text(R7_RUN3) == "Only my voice."


def test_v13_a_voice_only_row_on_a_montage_keeps_todays_music_fallback(monkeypatch):
    """Review: the fix is Talking-only, and `asks_no_music` cannot tell the format. So the
    planner's KRI-1365 fallback to the draft's music (`creator_asked_no_music`) reads a
    `voice_only` row exactly as main does, on a montage as everywhere."""
    from app.kria.draft_sound_carry import asks_no_music  # noqa: PLC0415

    brief = CreativeBrief(version=1, requirements=[R7_RUN3])
    _voice_switch(monkeypatch, True)
    assert not asks_no_music(brief)
    assert asks_no_music(CreativeBrief(version=1, requirements=[R7_RUN3, R4]))  # typed no_music


# ------------------------- KRI-1472, Q2 run 3: "Only my voice" restated with no typed fact
#
# Prod 2026-11-15 (thread F04383A7, job 77c63496): "Only my voice." was filed as r13 "Only my
# voice: no music." (typed `no_music`, met) and r14 "Only my voice: keep the speaker audible
# and mute the screen recordings." with NO fact at all. r14 held the draft ("A Talking video
# can't do this") and was listed as left out, although the recordings behind a speaker never
# play sound. The reading is structural only: an untyped whole-video audio row from the SAME
# turn that shares its lead clause (the words before the first colon) with a sibling carrying
# the typed no-music / voice-only fact is that sibling's ask, on a Talking edit.
#
# Ways it can go wrong, written before the code:
#  W1 the Q2 pair still holds;          W2 a typed `voice_only` sibling is not accepted;
#  W3 a lone untyped audio row, one from another turn, one with another lead clause, with no
#     colon, with a fact of its own, scoped to a clip, or "make my voice louder" is waved
#     through;                          W4 music in the plan is called done;
#  W5 the switch off is not today's refusal;        W6 a montage's answers change;
#  W7 it reads English words (a Turkish pair must work the same).

Q2_TURN = "9bb944c3-4564-4a89-b5b2-a62ebdcf3cc7"
OTHER_TURN = "089730e8-90b5-46ee-8fba-1825c77202cf"


def _audio(rid, description, facts=None, *, turn=Q2_TURN, scope="global"):
    return BriefRequirement(
        id=rid,
        kind="audio",
        scope=scope,
        description=description,
        facts=facts or {},
        source_turn_id=turn,
    )


R13_Q2 = _audio("r13", "Only my voice: no music.", {"audio_intent": "no_music"})
R14_Q2 = _audio("r14", "Only my voice: keep the speaker audible and mute the screen recordings.")


def _pair(sibling, row, facts=None):
    receipts = build_receipts(
        [sibling, row], facts or _draft_facts(_manifest()), include_unchecked=True
    )
    return {r.requirement_id: r for r in receipts}[row.id]


def test_w1_q2_the_restated_only_my_voice_row_is_done_with_its_typed_sibling(monkeypatch):
    _voice_switch(monkeypatch, True)
    receipt = _pair(R13_Q2, R14_Q2)
    assert (receipt.status, receipt.verification, receipt.reason) == (
        "met",
        "checked",
        "No music, as asked.",
    )


def test_w2_a_typed_voice_only_sibling_counts_too(monkeypatch):
    _voice_switch(monkeypatch, True)
    sibling = _audio("r13", "Only my voice: nothing but me.", {"voice_only": True})
    assert _pair(sibling, R14_Q2).status == "met"


@pytest.mark.parametrize(
    ("sibling", "row"),
    [
        (_r("x", "style", "Captions on.", {"captions": "on"}), R14_Q2),
        (R13_Q2, _audio("r14", R14_Q2.description, turn=OTHER_TURN)),
        (R13_Q2, _audio("r14", R14_Q2.description, turn=None)),
        (R13_Q2, _audio("r14", "Sound: mute the screen recordings.")),
        (R13_Q2, _audio("r14", "Only my voice, and mute the screen recordings.")),
        (_audio("r13", "Only my voice, no music.", {"audio_intent": "no_music"}), R14_Q2),
        (R13_Q2, _audio("r14", "Make my voice louder.")),
        (R13_Q2, _audio("r14", "Only my voice: make it louder.", {"voice_gain_db": 6})),
        (R13_Q2, _audio("r14", R14_Q2.description, scope=f"clip:{TAKE}")),
        (_audio("r13", "Only my voice: the same.", {}), R14_Q2),
    ],
    ids=[
        "lone",
        "another-turn",
        "no-turn",
        "another-clause",
        "no-colon",
        "sibling-no-colon",
        "louder",
        "own-fact",
        "one-clip",
        "untyped-sibling",
    ],
)
def test_w3_every_other_untyped_sound_ask_keeps_todays_refusal(monkeypatch, sibling, row):
    _voice_switch(monkeypatch, True)
    receipt = _pair(sibling, row)
    assert (receipt.status, receipt.reason) == ("not_possible", TALKING_CANT)


@pytest.mark.parametrize("audio", ["licensed_music", "user_song"])
def test_w4_music_in_the_plan_is_said_never_done(monkeypatch, audio):
    _voice_switch(monkeypatch, True)
    receipt = _pair(R13_Q2, R14_Q2, _draft_facts(_manifest(), audio_strategy=audio))
    assert (receipt.status, receipt.reason) == ("partial", "This video has music.")


def test_w5_switch_off_is_todays_refusal(monkeypatch):
    _voice_switch(monkeypatch, False)
    receipt = _pair(R13_Q2, R14_Q2)
    assert (receipt.status, receipt.reason) == ("not_possible", TALKING_CANT)


def test_w6_a_montage_answers_the_pair_as_before(monkeypatch):
    facts = dataclasses.replace(
        _draft_facts(_manifest(edit_format="montage"), edit_format="montage"), phone_talking=False
    )
    _voice_switch(monkeypatch, True)
    on = _pair(R13_Q2, R14_Q2, facts)
    _voice_switch(monkeypatch, False)
    assert on == _pair(R13_Q2, R14_Q2, facts) and on.status != "met"


def test_w7_no_words_are_read_a_turkish_pair_works_the_same(monkeypatch):
    _voice_switch(monkeypatch, True)
    sibling = _audio("r13", "Sadece sesim: müzik olmasın.", {"audio_intent": "no_music"})
    row = _audio("r14", "Sadece sesim: ekran kayıtlarının sesi kapalı olsun.")
    assert _pair(sibling, row).status == "met"


# KRI-1472 review: a row that shares the lead clause but asks for ANOTHER sound action is not
# a restatement ("Music: no music." + "Music: add a whoosh on each cut." -> the whoosh is not
# "Done"). The structure (same turn, same lead clause, no facts) is necessary, not enough: the
# row must also not be read as a sound action by the brief's existing sound readers.
@pytest.mark.parametrize(
    ("sibling", "row"),
    [
        (
            _audio("r13", "Music: no music.", {"audio_intent": "no_music"}),
            _audio("r14", "Music: add a whoosh on each cut."),
        ),
        (
            _audio("r13", "Voice: no music.", {"audio_intent": "no_music"}),
            _audio("r14", "Voice: louder."),
        ),
    ],
    ids=["whoosh", "louder"],
)
def test_w8_another_sound_action_under_the_same_lead_clause_is_not_done(monkeypatch, sibling, row):
    _voice_switch(monkeypatch, True)
    receipt = _pair(sibling, row)
    assert receipt.status != "met", receipt
    assert receipt.reason != "No music, as asked."
