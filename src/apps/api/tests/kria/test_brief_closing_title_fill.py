"""Qendresa Q1 run 4 (prod 2026-11-17, thread 2b6bc731, held turn f0111e06): the ending held.

The request said `End with "comment REPO" and an arrow for the last 3 seconds.` The Main
Creator filed the brief row r6 (text/global, literal "comment REPO", facts {closing: "comment
REPO"}) but its strategy carried no `closing_title`. Approval readiness reads only the
strategy's texts, so the draft was held: "That exact text isn't in this draft." Run 3, same
prompt, got a `closing_title` from the model and was not held.

The server backstop (`planner._with_brief_closing_title`) fills `closing_title` from the
brief's own ending row when the route draws closing text, before the wording grounding and the
readiness check see the strategy.

Ways this fix could go wrong (written before the code):
1. The `closing` fact names a picture or a clip ("end on the toast photo"): words nobody asked
   for get drawn. Only a row whose `closing` fact IS its literal fills, never with
   `closing_media`.
2. The words are already in the plan (a pin, the title, a label) or on a colour closing card:
   the ending is drawn twice.
3. The route cannot draw closing text (a cloud Talking edit, the lane switched off): the fill
   turns today's hold into a different refusal. Only a phone Talking edit with the closing-title
   lane fills.
4. A row the creator left out ("Make it without these", superseded) comes back.
5. Two ending asks: the server picks one.
6. Turkish / German endings: only English "comment X" is the CTA look, so the arrow promise or
   the plain tag must not create a new hold.
7. Undrawable glyphs: left to the existing KRI-1024 receipt (not re-tested here).
8. The fill must reach the strategy readiness judges (so the draft is ready because the render
   will draw it), never be a checker that accepts text the render will not draw.
Kill switch: `BRIEF_CLOSING_TITLE_FILL_ENABLED=false` leaves the strategy byte-identical.

Real: `_with_brief_closing_title`, `assess_readiness` (policy, `plan_facts_from_strategy`,
`check_requirement`) and `resolve_creator_manifest`. No model call.
"""

from __future__ import annotations

import uuid

import pytest

from app.agents._schemas.creator_agent import CreativeStrategy
from app.config import settings
from app.kria import brief_checks as bc
from app.kria.approval_readiness import APPROVABLE, assess_readiness
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.planner import _with_brief_closing_title
from app.services import creator_capabilities as capabilities
from tests._prod_profile import PROD_VERIFIED_FEATURES, apply_prod_profile

TAKE = "analysis-proxy-ios-3D5CE019.mp4"
ENDING = "comment REPO"
TITLE = "5 repos / I'd build with / this weekend!!"
HELD_REASON = "That exact text isn't in this draft."
PROD_ON = (
    "creator_cta_ending_enabled",
    "freehand_drawing_enabled",
    "phone_subtitled_title_enabled",
    "phone_subtitled_closing_title_enabled",
    "phone_text_styling_enabled",
    "title_look_enabled",
)
VERIFIED = (*PROD_VERIFIED_FEATURES, "freehandDrawing")


def _r(rid, description, *, literal=None, scope="global", facts=None, status="open"):
    return BriefRequirement(
        id=rid,
        kind="text",
        scope=scope,
        description=description,
        literal=literal,
        facts=facts or {},
        status=status,
    )


TITLE_ROW = _r(
    "r3", "Start with a big title for the first 3 seconds.", literal=TITLE, scope="title"
)
# r6 exactly as run 4's brief model filed it (no `arrow` fact; the arrow is in the words).
R6 = _r(
    "r6",
    'End with "comment REPO" and an arrow for the last 3 seconds.',
    literal=ENDING,
    facts={"closing": ENDING},
)


def _strategy(**extra) -> CreativeStrategy:
    return CreativeStrategy.model_validate(
        {
            "pacing": "fast",
            "direction": "native",
            "rationale": "Around 38 seconds.",
            "edit_format": "subtitled",
            "caption_style": "karaoke",
            "audio_strategy": "original_audio",
            "render_program": "native",
            "story_structure": [],
            "target_duration_s": 38,
            "selected_media_ids": [TAKE],
            "video_reuse_policy": "once",
            "optional_treatments": [],
            "opening_title": TITLE,
            "opening_title_duration_s": 3.0,
            **extra,
        }
    )


def _manifest(*, phone: bool = True):
    return capabilities.resolve_creator_manifest(
        item_id="item-q1-run4",
        edit_format="subtitled",
        media=[{"media_id": TAKE, "kind": "video", "duration_s": 48.8}],
        phone_source_media_ids=[TAKE] if phone else [],
        phone_rendering_allowed=phone,
    )


@pytest.fixture(autouse=True)
def _prod(monkeypatch):
    apply_prod_profile(monkeypatch)
    for name in PROD_ON:
        monkeypatch.setattr(settings, name, True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(settings, "phone_subtitled_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(VERIFIED))
    monkeypatch.setattr(settings, "brief_closing_title_fill_enabled", True)


def _planned(brief: CreativeBrief, strategy: CreativeStrategy, manifest=None) -> CreativeStrategy:
    """The strategy the draft carries after the planner's backstop."""
    filled = _with_brief_closing_title(strategy, brief, manifest or _manifest())
    return filled if filled is not None else strategy


def _readiness(brief: CreativeBrief, strategy: CreativeStrategy, manifest=None) -> dict:
    return assess_readiness(
        brief,
        strategy.model_dump(mode="json", exclude_none=True),
        media={"clip_paths": [TAKE]},
        manifest=manifest or _manifest(),
        creator_id=uuid.UUID("00000000-0000-4000-8000-0000000011a4"),
        protected_regions=[],
        protected_marks=True,
    )


def _rows(report: dict) -> dict[str, dict]:
    return {row["requirement_id"]: row for row in report["requirements"]}


def _held(report: dict) -> dict[str, str]:
    return {
        rid: row["reason"]
        for rid, row in _rows(report).items()
        if row["disposition"] not in APPROVABLE
    }


def _drawn_count(strategy: CreativeStrategy, words: str) -> int:
    facts = bc.plan_facts_from_strategy(strategy.model_dump(mode="json", exclude_none=True))
    wanted = bc._fold(words)
    return sum(1 for text in facts.texts if bc._contains_text(text, wanted))


# ------------------------------------------------------------------ the incident


def test_control_without_the_fill_the_draft_is_held_as_prod_was(monkeypatch):
    monkeypatch.setattr(settings, "brief_closing_title_fill_enabled", False)
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6])
    report = _readiness(brief, _planned(brief, _strategy()))

    assert report["ready"] is False
    assert _held(report) == {"r6": HELD_REASON}


def test_run4_ending_is_filled_drawn_once_and_the_draft_is_ready():
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6])
    planned = _planned(brief, _strategy())
    report = _readiness(brief, planned)

    assert planned.closing_title == ENDING
    assert _drawn_count(planned, ENDING) == 1
    assert report["ready"] is True
    assert _held(report) == {}
    # The arrow is promised because the phone draws the CTA ending with it.
    assert _rows(report)["r6"]["reason"] == bc._CLOSING_ARROW_PLAN


def test_run3_brief_with_an_arrow_fact_fills_the_same_way():
    r6 = R6.model_copy(update={"facts": {"arrow": True, "closing": ENDING}})
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, r6])
    planned = _planned(brief, _strategy())

    assert planned.closing_title == ENDING
    assert _readiness(brief, planned)["ready"] is True


def test_a_planner_closing_title_is_kept_as_written():
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6])
    strategy = _strategy(closing_title=ENDING)

    assert _with_brief_closing_title(strategy, brief, _manifest()) is None


# ------------------------------------------------------------------ Turkish / German endings


@pytest.mark.parametrize(
    ("description", "words"),
    [
        ("Sona 'REPO yaz' yaz.", "REPO yaz"),
        ("Ende mit 'kommentiere REPO'.", "kommentiere REPO"),
    ],
)
def test_turkish_and_german_endings_are_drawn_without_a_new_hold(description, words):
    row = _r("r6", description, literal=words, facts={"closing": words})
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, row])
    planned = _planned(brief, _strategy())
    report = _readiness(brief, planned)

    assert planned.closing_title == words
    assert _drawn_count(planned, words) == 1
    assert _held(report) == {}
    assert _rows(report)["r6"]["disposition"] in APPROVABLE


# ------------------------------------------------------------------ negative controls


def test_an_ending_that_names_a_picture_fills_no_text():
    row = _r("r6", "End on the toast photo.", facts={"closing": "the toast photo"})
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, row])

    assert _with_brief_closing_title(_strategy(), brief, _manifest()) is None


def test_an_ending_text_with_a_word_that_differs_from_its_closing_fact_fills_no_text():
    row = _r("r6", "End on the toast photo.", literal="MY PICK", facts={"closing": "toast photo"})
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, row])

    assert _with_brief_closing_title(_strategy(), brief, _manifest()) is None


def test_words_already_pinned_are_not_drawn_twice():
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6])
    strategy = _strategy(pinned_texts=[{"text": ENDING, "corner": "bottom_center"}])

    assert _with_brief_closing_title(strategy, brief, _manifest()) is None


def test_words_already_in_the_title_are_not_drawn_twice():
    brief = CreativeBrief(version=1, requirements=[R6])
    strategy = _strategy(opening_title=f"Then {ENDING}")

    assert _with_brief_closing_title(strategy, brief, _manifest()) is None


def test_a_colour_closing_card_keeps_its_own_words():
    row = R6.model_copy(update={"facts": {"closing": ENDING, "background_color": "#000000"}})
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, row])

    assert _with_brief_closing_title(_strategy(), brief, _manifest()) is None


def test_two_ending_asks_fill_nothing():
    other = _r(
        "r8",
        'End with "follow for more".',
        literal="follow for more",
        facts={"closing": "follow for more"},
    )
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6, other])

    assert _with_brief_closing_title(_strategy(), brief, _manifest()) is None


def test_an_ending_left_out_is_not_brought_back():
    left_out = R6.model_copy(
        update={"status": "superseded", "facts": {**R6.facts, "superseded_by": "server"}}
    )
    brief = CreativeBrief(version=2, requirements=[TITLE_ROW, left_out])

    assert _with_brief_closing_title(_strategy(), brief, _manifest()) is None


def test_a_route_that_cannot_draw_closing_text_is_unchanged(monkeypatch):
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6])

    # A cloud Talking edit (no phone render) has no closing-text lane.
    assert _with_brief_closing_title(_strategy(), brief, _manifest(phone=False)) is None
    # Nor does a phone Talking edit with the lane switched off.
    monkeypatch.setattr(settings, "phone_subtitled_closing_title_enabled", False)
    assert _with_brief_closing_title(_strategy(), brief, _manifest()) is None


def test_a_montage_is_not_touched():
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6])
    strategy = _strategy(edit_format="montage")

    assert _with_brief_closing_title(strategy, brief, _manifest()) is None


def test_switch_off_is_byte_identical(monkeypatch):
    monkeypatch.setattr(settings, "brief_closing_title_fill_enabled", False)
    brief = CreativeBrief(version=1, requirements=[TITLE_ROW, R6])
    strategy = _strategy()
    before = strategy.model_dump_json()

    assert _with_brief_closing_title(strategy, brief, _manifest()) is None
    assert strategy.model_dump_json() == before
