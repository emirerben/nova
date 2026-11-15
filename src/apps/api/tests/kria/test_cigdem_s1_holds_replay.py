"""Çiğdem S1 "Cigdem 1" (prod 2026-11-15, thread 51646943, job 4bb9842c): the two false holds.

One talking take (IMG_7212, 40.1 s) and five photos, server e74148c6c, client protocol 51. The
request: "Yorumla episode 8: Vela's reply to Lumo. Use my talking video and cut the dead air and
the sentence I restarted. When I mention the launch, Vela's 2 posts and the billboards, show that
picture full screen while my voice keeps going. Captions small, white, lowercase, in the middle
of the screen, never on my face. A small "Yorumla" label in the top left for the whole video. End
on a red screen with yellow text "follow for more brand and marketing content". No music."

What the creator got: the first draft HELD three times before anything was made.

- r1 "Use my talking video." -> "A Talking video can't do this, so it stays as filmed" (KRI-1488);
- r3 (triggers "launch", "Vela's 2 posts", "billboards") -> "No pop-in for launch, Vela's 2
  posts." (KRI-1489), though the planner put all four pictures on "launched", "five years",
  "hinge" and "billboards";
- r7, the red end card -> "Ending on a full-screen colour ... isn't available" (honest).

Her first reply ("show each one full screen when I say its word", brief v2) got the same two
holds; only naming the spoken words (brief v3) cleared r3, and r1 needed "Make it without these".

Real: the brief rows exactly as `creative_brief_versions` holds them (v1, v2), the strategy the
planner approved (turn f8f579dc's `draft.apply_strategy`, the same four beats) and
`assess_readiness` under production's switches. No model call; the held drafts' own strategies
were never stored, and every planner run in the thread put the beats on the same four words.

Both fixes read typed fields and counts only, never the creator's words (KRI-667).

Failure modes written before the fix (each has a test below):
 1. a `select` row WITH a typed fact keeps its own checker (no blanket "have a look");
 2. the untyped `select` row is never claimed "Done" (nothing proves it), only no longer "can't";
 3. Turkish and English untyped rows read the same (no words are read);
 4. a word the creator QUOTED that no pop-in answers still holds;
 5. spare pop-ins must match the described moments EXACTLY ("2 posts" = 2): one too few (a
    missed moment) or one too many (a pop-in on a word nobody asked for) still holds;
 6. a pop-in a name claims (this ask's, or another ask's in the brief) is never spare;
 7. with both switches off the draft is held exactly as prod held it (the control).
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.services import creator_capabilities as capabilities
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile

TURN_1 = "cc9df523-d762-4997-a191-60237646acb0"
TURN_2 = "521b55b0-958e-433f-8d2e-5c1801cc29c3"
TAKE = "analysis-proxy-ios-F85BA960-DDF4-4334-8AE5-2CB94F898726.mp4"
TAKE_S = 40.121678
LAUNCH = "09a70d6a-d113-4073-804a-9926c0f286f1"
POST_1 = "90f20e68-2569-49ab-91ed-6de743e6ca8d"
POST_2 = "87c62e76-481f-4c3a-99b6-720cd02311c2"
BILLBOARDS = "b728367b-3ce3-4ab4-82bb-bf7a597c29cc"
LATTE = "0a6e2c68-d764-4fe3-b2d9-c67e03f53b75"  # the decoy
PHOTOS = [LAUNCH, POST_1, POST_2, BILLBOARDS, LATTE]
CREATOR = uuid.UUID("7b56dd6f-6b9a-4634-89c8-ce91bb50a50d")

END_CARD = "Ending on a full-screen colour with your text isn't available on your iPhone yet"
PROD_HOLDS = {
    "r1": "A Talking video can't do this, so it stays as filmed",
    "r3": "No pop-in for launch, Vela's 2 posts.",
    "r7": END_CARD,
}
SWITCHES = ("untyped_select_unjudged_enabled", "described_popin_names_enabled")
# Switches production has on for this path (all default off in code); the same list as the
# Qendresa Q1 run 3 replay, which was read off prod the day before this run.
PROD_ON = (
    "tight_pause_cut_enabled",
    "title_look_enabled",
    "phone_caption_shape_enabled",
    "spoken_list_labels_enabled",
    "talking_card_layout_enabled",
    "creator_cta_ending_enabled",
    "freehand_drawing_enabled",
    "phone_text_styling_enabled",
    "phone_subtitled_title_enabled",
    "phone_subtitled_closing_title_enabled",
    "phone_subtitled_spoken_text_enabled",
    "phone_subtitled_video_overlays_enabled",
    "speech_cleanup_retakes_enabled",
    # Prod draws the whole-video "Yorumla" corner label (job 4bb9842c's `corner_text_placement`).
    "phone_talking_corner_text_enabled",
)


def _r(rid, kind, description, *, literal=None, facts=None, turn=TURN_1, status="open"):
    return BriefRequirement(
        id=rid,
        kind=kind,
        scope="global",
        description=description,
        literal=literal,
        facts=facts or {},
        status=status,
        source_turn_id=turn,
    )


# Brief v1, row for row as prod stored it.
R1 = _r("r1", "select", "Use my talking video.")
R2 = _r(
    "r2",
    "style",
    "Cut the dead air and the sentence I restarted.",
    facts={"cut_restarts": True, "speech_cleanup": True},
)
R3_FACTS = {"triggers": ["launch", "Vela's 2 posts", "billboards"], "display_mode": "fullscreen"}
R3 = _r(
    "r3",
    "style",
    "When I mention the launch, Vela's 2 posts and the billboards, show that picture full "
    "screen while my voice keeps going.",
    facts=R3_FACTS,
)
R4 = _r(
    "r4",
    "style",
    "Captions small, white, lowercase, in the middle of the screen.",
    facts={
        "captions": "on",
        "caption_case": "lower",
        "caption_size": "small",
        "caption_color": "#FFFFFF",
        "caption_position": "middle",
    },
)
R5 = _r("r5", "style", "Captions never on my face.", facts={"keep_text_off": [{"kind": "face"}]})
R6 = _r(
    "r6",
    "text",
    'A small "Yorumla" label in the top left for the whole video.',
    literal="Yorumla",
    facts={"size": "small", "position": "top_left"},
)
R7 = _r(
    "r7",
    "text",
    'End on a red screen with yellow text "follow for more brand and marketing content".',
    literal="follow for more brand and marketing content",
    facts={
        "closing": "follow for more brand and marketing content",
        "text_color": "#FFD24A",
        "background_color": "red",
    },
)
R8 = _r("r8", "audio", "No music.", facts={"audio_intent": "no_music"})
BRIEF_V1 = CreativeBrief(version=1, requirements=[R1, R2, R3, R4, R5, R6, R7, R8])

# Brief v2: her reply "Keep the pictures: show each one full screen when I say its word. Skip the
# red end card." rewrote r3 (same triggers) and the creator superseded r7.
R3_V2 = _r(
    "r3",
    "style",
    "Keep the launch picture, Vela's two posts and the billboard picture: show each one full "
    "screen when I say its word, while my voice keeps going.",
    facts=R3_FACTS,
    turn=TURN_2,
)
R7_V2 = R7.model_copy(
    update={"status": "superseded", "facts": {**R7.facts, "superseded_by": "creator"}}
)
BRIEF_V2 = CreativeBrief(version=2, requirements=[R1, R2, R3_V2, R4, R5, R6, R7_V2, R8])


def _beat(beat_id: str, trigger: str, visual: str) -> dict:
    return {
        "beat_id": beat_id,
        "trigger": trigger,
        "visual_id": f"asset-{visual}",
        "occurrence": "first",
        "visual_role": "photo",
        "display_mode": "fullscreen",
    }


BEATS = [
    _beat("launch", "launched", LAUNCH),
    _beat("first-post", "five years", POST_1),
    _beat("second-post", "hinge", POST_2),
    _beat("billboards", "billboards", BILLBOARDS),
]
# The strategy the planner approved (turn f8f579dc), with the take's proxy as prod named it.
STRATEGY = {
    "pacing": "balanced",
    "direction": "native",
    "rationale": "A 30-second target; word-triggered full-screen pictures over her voice.",
    "intro_hook": "Begin directly with the contrast between Lumo's launch and Vela's response.",
    "edit_format": "subtitled",
    "pinned_texts": [{"text": "Yorumla", "corner": "top_left"}],
    "caption_style": "clean",
    "audio_strategy": "original_audio",
    "reaction_beats": BEATS,
    "render_program": "native",
    "story_structure": [],
    "target_duration_s": 30,
    "selected_media_ids": [TAKE],
    "video_reuse_policy": "once",
    "optional_treatments": [],
}


def _manifest():
    media = [{"media_id": TAKE, "kind": "video", "duration_s": TAKE_S}]
    media += [{"media_id": f"asset-{p}", "kind": "image", "label": f"{p}.jpg"} for p in PHOTOS]
    return capabilities.resolve_creator_manifest(
        item_id="f0fcb94e-7556-44db-8b0f-d7dff0c0588e",
        edit_format="subtitled",
        media=media,
        phone_source_media_ids=[TAKE],
        phone_rendering_allowed=True,
    )


def _readiness(monkeypatch, brief: CreativeBrief, *, fixes: bool = True, strategy=None) -> dict:
    apply_prod_profile(monkeypatch)
    for name in PROD_ON:
        monkeypatch.setattr(settings, name, True)
    for name in SWITCHES:
        monkeypatch.setattr(settings, name, fixes)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    return assess_readiness(
        brief,
        strategy or STRATEGY,
        media={"clip_paths": [TAKE]},
        manifest=_manifest(),
        creator_id=CREATOR,
        protected_regions=[],
        protected_marks=True,
    )


def _held(report: dict) -> dict[str, str]:
    return {
        row["requirement_id"]: row["reason"]
        for row in report["requirements"]
        if row["disposition"] not in APPROVABLE
    }


def _row(report: dict, rid: str) -> dict:
    return next(row for row in report["requirements"] if row["requirement_id"] == rid)


# ------------------------------------------------------------------ the incident


def test_control_switches_off_hold_the_first_draft_exactly_as_prod_did(monkeypatch):
    assert _held(_readiness(monkeypatch, BRIEF_V1, fixes=False)) == PROD_HOLDS


def test_first_draft_holds_only_the_honest_end_card(monkeypatch):
    report = _readiness(monkeypatch, BRIEF_V1)
    assert _held(report) == {"r7": END_CARD}
    assert _row(report, "r1")["disposition"] in APPROVABLE
    assert _row(report, "r3")["disposition"] in APPROVABLE


def test_her_first_reply_is_ready_with_nothing_held(monkeypatch):
    # Brief v2 dropped the end card; "when I say its word" names no word, so the described
    # moments stay described.
    assert _held(_readiness(monkeypatch, BRIEF_V2)) == {}


# ------------------------------------------------------------------ failure modes


def test_a_typed_selection_keeps_its_own_checker(monkeypatch):
    only_video = _r("r1", "select", "Use my video clips only.", facts={"only_media_kind": "video"})
    report = _readiness(monkeypatch, CreativeBrief(version=1, requirements=[only_video, R8]))
    assert _row(report, "r1")["reason"] != _row(_readiness(monkeypatch, BRIEF_V1), "r1")["reason"]


@pytest.mark.parametrize(
    "description", ["Use my talking video.", "Konuşma videomu kullan.", "Use my second clip."]
)
def test_an_untyped_selection_is_have_a_look_never_done(monkeypatch, description):
    brief = CreativeBrief(version=1, requirements=[_r("r1", "select", description), R8])
    report = _readiness(monkeypatch, brief)
    row = _row(report, "r1")
    assert row["disposition"] in APPROVABLE
    assert row["disposition"] != "executable"  # nothing proves it: never "Done"
    assert row["reason"] != PROD_HOLDS["r1"]


def test_a_quoted_word_with_no_pop_in_still_holds(monkeypatch):
    nike = _r(
        "r3",
        "style",
        'Show the billboard photo full screen when I say "Nike".',
        facts={"triggers": ["Nike"], "display_mode": "fullscreen"},
    )
    report = _readiness(monkeypatch, CreativeBrief(version=1, requirements=[nike, R8]))
    assert _held(report) == {"r3": "No pop-in for Nike."}


def test_fewer_spare_pop_ins_than_missing_names_still_hold(monkeypatch):
    one = {**STRATEGY, "reaction_beats": [b for b in BEATS if b["beat_id"] in {"billboards"}]}
    two = {
        **STRATEGY,
        "reaction_beats": [b for b in BEATS if b["beat_id"] in {"billboards", "launch"}],
    }
    brief = CreativeBrief(version=1, requirements=[R3, R8])
    assert _held(_readiness(monkeypatch, brief, strategy=one)) == {
        "r3": "No pop-in for launch, Vela's 2 posts."
    }
    # "launched" is one spare pop-in for two unmatched names: still held.
    assert _held(_readiness(monkeypatch, brief, strategy=two)) == {
        "r3": "No pop-in for launch, Vela's 2 posts."
    }


def test_a_pop_in_a_name_claims_is_never_spare(monkeypatch):
    # Both pop-ins sit on words this ask names ("billboards" twice over): none is spare.
    claimed = {
        **STRATEGY,
        "reaction_beats": [
            _beat("billboards", "billboards", BILLBOARDS),
            _beat("billboards-2", "the billboards", POST_1),
        ],
    }
    brief = CreativeBrief(version=1, requirements=[R3, R8])
    assert _held(_readiness(monkeypatch, brief, strategy=claimed)) == {
        "r3": "No pop-in for launch, Vela's 2 posts."
    }


def test_one_spare_pop_in_too_many_still_holds(monkeypatch):
    # A fifth pop-in on a word nobody named (the decoy latte on "coffee"): 4 spare for 3 moments.
    extra = {**STRATEGY, "reaction_beats": [*BEATS, _beat("latte", "coffee", LATTE)]}
    brief = CreativeBrief(version=1, requirements=[R3, R8])
    assert _held(_readiness(monkeypatch, brief, strategy=extra)) == {
        "r3": "No pop-in for launch, Vela's 2 posts."
    }


def test_a_pop_in_another_ask_names_is_never_spare(monkeypatch):
    # "Show the latte when I say coffee" claims the fifth pop-in: 3 spare for 3 moments again.
    coffee = _r(
        "r9",
        "style",
        "Show the latte photo when I say coffee.",
        facts={"triggers": ["coffee"], "display_mode": "fullscreen"},
    )
    extra = {**STRATEGY, "reaction_beats": [*BEATS, _beat("latte", "coffee", LATTE)]}
    brief = CreativeBrief(version=1, requirements=[R3, R8, coffee])
    assert _held(_readiness(monkeypatch, brief, strategy=extra)) == {}
