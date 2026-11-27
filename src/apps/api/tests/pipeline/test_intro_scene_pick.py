"""KRI-1611: server-issued options and a seeded pick for a Talking edit's intro scene.

Written before the wiring was trusted. Each test is a way the pick could go wrong (the same
list is in the module docstring of `app/pipeline/intro_scene_pick.py`):

* P1: flag off / account not admitted / no stamp changes a row (`test_nothing_is_stamped_*`,
  `test_unstamped_job_draws_todays_rows`).
* P2: the scene overrides the creator or her own look (`test_scene_yields_to_*`).
* P3: a re-render picks differently (`test_the_same_job_is_pinned_*`).
* P4: the same look every video (`test_recent_looks_are_tried_last`).
* P5: a scene that does not fit this footage ships (`test_unclean_real_receipt_falls_back_*`,
  `test_a_fitted_scene_*`).
* P6: the split drops or reorders her words (`test_every_candidate_is_her_words_in_order`).
* P7: Turkish: faces that cannot draw it, caps (`test_turkish_*`).
* P8: more than one ink, or an ink lost on the footage (`test_ink_*`).
* P9: the approval check refuses a stack of her title (`test_approval_*`).
* Creator tier: her edit to a scene row stays hers (`test_a_creator_edit_*`).

All of it runs the real compiler, the real `place_title_look` and real Skia; only the speaker's
face is a fixed stand-in.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.pipeline import intro_scene as scene_mod
from app.pipeline import intro_scene_pick as pick
from app.pipeline import phone_subtitled_title as title_mod
from app.pipeline.canvas import PORTRAIT
from app.pipeline.phone_title_look import (
    TITLE_LOOK_ASK_FIELD,
    TITLE_LOOK_PARAM,
    intro_plain_text,
    place_title_look,
    stacked_title_matches,
)
from app.pipeline.render_geometry import ProtectedRegion
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, phone_text_policy_scope

EN = "the biggest design trend right now"
TR = "bu yılın en büyük tasarım trendi şu an"
USER = "6f1c0d52-4a4e-4f6e-9b7c-0d6a3c5f1a11"
POLICY = {
    "version": 1,
    "preferences": {"all_text": {}},
    "requested": {},
    "informative_text_designs": True,
    "informative_text_shapes": True,
}


class FakeJob:
    def __init__(self, title: str = EN, plan: dict | None = None, user_id: str = USER) -> None:
        self.user_id = user_id
        self.assembly_plan = {
            "creator_generation_id": "gen-1",
            PHONE_TEXT_STYLE_FIELD: dict(POLICY),
            **(plan or {}),
        }
        self.all_candidates = {"creator_strategy": {"opening_title": title}}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_enabled", True)
    monkeypatch.setattr(settings, "intro_scene_user_ids", [USER])
    monkeypatch.setattr(settings, "title_look_enabled", True)


def _stamped(title: str = EN, **kwargs: Any) -> dict:
    job = FakeJob(title, **kwargs)
    pick.stamp_seeded_intro_scene(job)
    ask = pick.pinned_intro_scene(job.assembly_plan)
    assert ask is not None
    return ask


# --- P1: nothing changes unless the stamp says so ------------------------------------------


def test_nothing_is_stamped_with_the_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_enabled", False)
    job = FakeJob()
    before = dict(job.assembly_plan)
    pick.stamp_seeded_intro_scene(job)
    assert job.assembly_plan == before


@pytest.mark.parametrize("ids", [[], ["someone-else"]], ids=["empty", "other-account"])
def test_nothing_is_stamped_for_an_account_not_admitted(monkeypatch, ids):
    monkeypatch.setattr(settings, "intro_scene_user_ids", ids)
    job = FakeJob()
    pick.stamp_seeded_intro_scene(job)
    assert pick.INTRO_SCENE_ASK_FIELD not in job.assembly_plan


def test_a_star_admits_every_account(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_user_ids", ["*"])
    job = FakeJob()
    pick.stamp_seeded_intro_scene(job)
    assert pick.INTRO_SCENE_ASK_FIELD in job.assembly_plan


def test_stamping_reads_no_prose(monkeypatch):
    """The stamp reads the job's pins (text policy, title-look ask), never the request words."""
    job = FakeJob()
    job.all_candidates["creator_request"] = "make the title bold serif in red"
    pick.stamp_seeded_intro_scene(job)
    assert pick.INTRO_SCENE_ASK_FIELD in job.assembly_plan


# --- P2: she and her own look win ------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "kwargs"),
    [
        ("typed_stack", {"title": "5 PROJECTS / the AI builder community"}),
        ("legacy_generation", {"plan": {"variants": [{"id": "v"}]}}),
        ("device_render", {"plan": {"_device_render_v1": True}}),
        ("title_look_ask", {"plan": {TITLE_LOOK_ASK_FIELD: {"version": 1}}}),
        (
            "requested_title_rule",
            {
                "plan": {
                    PHONE_TEXT_STYLE_FIELD: {**POLICY, "requested": {"title": {"color": "#fff"}}}
                }
            },
        ),
        (
            "requested_title_font",
            {
                "plan": {
                    PHONE_TEXT_STYLE_FIELD: {
                        **POLICY,
                        "requested": {"title": {"font_family": "Anton"}},
                    }
                }
            },
        ),
        ("no_informative_stamp", {"plan": {PHONE_TEXT_STYLE_FIELD: {"version": 1}}}),
        (
            "build_without_designs",
            {"plan": {PHONE_TEXT_STYLE_FIELD: {**POLICY, "informative_text_shapes": False}}},
        ),
        ("one_word_title", {"title": "hello"}),
        ("empty_title", {"title": "  "}),
    ],
)
def test_scene_yields_to(label, kwargs):
    job = FakeJob(**kwargs)
    before = dict(job.assembly_plan)
    pick.stamp_seeded_intro_scene(job)
    assert job.assembly_plan == before, label


def test_scene_needs_the_title_look_switch(monkeypatch):
    monkeypatch.setattr(settings, "title_look_enabled", False)
    job = FakeJob()
    pick.stamp_seeded_intro_scene(job)
    assert pick.INTRO_SCENE_ASK_FIELD not in job.assembly_plan


def test_automatic_font_is_not_an_ask():
    ask = _stamped(
        plan={
            PHONE_TEXT_STYLE_FIELD: {
                **POLICY,
                "preferences": {"all_text": {"automatic_font": True}},
            }
        }
    )
    assert ask["candidates"]


# --- P3 / P4 / P6 / P7: the pick -------------------------------------------------------------


def test_the_same_job_is_pinned_the_same_way():
    assert _stamped() == _stamped()


def test_a_second_generation_can_look_different():
    looks = set()
    for generation in range(24):
        ask = _stamped(plan={"creator_generation_id": f"gen-{generation}"})
        looks.add((ask["candidates"][0]["font_pair"], tuple(ask["candidates"][0]["voices"])))
    assert len(looks) >= 4


def test_recent_looks_are_tried_last():
    base = pick.pick_candidates(EN, seed="s", recent=())
    first = base[0]["font_pair"]
    again = pick.pick_candidates(EN, seed="s", recent=(first,))
    assert again[0]["font_pair"] != first
    assert first in {c["font_pair"] for c in again}  # still offered when nothing else is left


def test_a_job_stamps_the_looks_it_excluded():
    class Session:
        def execute(self, _query):
            return SimpleNamespace(scalars=lambda: iter(["grotesk_hand", None, "serif_italic"]))

    job = FakeJob()
    pick.stamp_seeded_intro_scene(job, session=Session())
    ask = pick.pinned_intro_scene(job.assembly_plan)
    assert ask["excluded"] == ["grotesk_hand", "serif_italic"]
    assert ask["candidates"][0]["font_pair"] not in {"grotesk_hand", "serif_italic"}


def test_a_failed_history_lookup_only_means_no_exclusion():
    class Session:
        def execute(self, _query):
            raise RuntimeError("db down")

    job = FakeJob()
    pick.stamp_seeded_intro_scene(job, session=Session())
    assert pick.INTRO_SCENE_ASK_FIELD in job.assembly_plan


@pytest.mark.parametrize("title", [EN, TR], ids=["en", "tr"])
def test_every_candidate_is_her_words_in_order(title):
    ask = _stamped(title)
    words = scene_mod.title_words(title)
    menu = scene_mod.issue_menu(title)
    assert 1 <= len(ask["candidates"]) <= pick.MAX_CANDIDATES
    for candidate in ask["candidates"]:
        assert len(candidate["voices"]) == len(words)  # one voice per word, nothing added
        assert candidate["font_pair"] in menu.font_pairs
        assert candidate["entrance"] in menu.entrances_for(candidate["font_pair"])
        assert candidate["voices"].count("accent") <= 3
        assert set(candidate["zones"]) == set(scene_mod.ZONES)
    assert set(ask["inks"]) == set(scene_mod.INKS)


def test_the_stamp_holds_no_text_of_hers_or_the_models():
    """No field reaches the screen as text: only ids and voices are pinned."""
    ask = _stamped(TR)
    flat = repr(ask)
    assert "tasarım" not in flat and "trendi" not in flat


def test_turkish_pairs_can_draw_dotted_and_dotless_letters():
    menu = scene_mod.issue_menu(TR)
    assert menu.font_pairs  # the issued menu keeps only faces that draw this title
    for candidate in _stamped(TR)["candidates"]:
        pair = scene_mod.FONT_PAIRS[candidate["font_pair"]]
        assert scene_mod.face_draws(pair.solid.family, TR)
        assert scene_mod.face_draws(pair.accent.family, TR)


def test_language_comes_from_the_title_or_the_spoken_language():
    assert pick.language_of(TR) == "tr"
    assert pick.language_of(EN) is None
    assert pick.language_of(EN, "tr-TR") == "tr"
    assert pick.language_of(EN, "en") is None


# --- P5: the fit ------------------------------------------------------------------------------

FACE = scene_mod.TYPICAL_FACE


def _place_with(face=FACE):
    def sample(_path, anchors, **_kwargs):
        return [ProtectedRegion(0.0, 1e6, face, kind="face")], {
            "attempted": len(anchors),
            "decoded": 1,
            "detected": 1,
        }

    def place(rows, position):
        return place_title_look(
            rows,
            clip_path="take.mp4",
            keep_segments=[(0.0, 1e6)],
            display_width=PORTRAIT.width,
            display_height=PORTRAIT.height,
            canvas=PORTRAIT,
            sample=sample,
            position=position,
            avoid=[scene_mod.TYPICAL_CAPTIONS],
        )

    return place


def _fit(ask, title, place, language=None):
    return pick.fit_intro_scene(
        ask,
        title,
        canvas=PORTRAIT,
        timeline_duration_s=30.0,
        duration_s=None,
        language=language,
        place=place,
    )


@pytest.mark.parametrize(("title", "language"), [(EN, None), (TR, "tr")], ids=["en", "tr"])
def test_a_fitted_scene_is_her_words_in_one_ink_with_a_clean_receipt(title, language):
    rows, receipt, info = _fit(_stamped(title), title, _place_with(), language)
    assert rows is not None and receipt is not None, info
    assert scene_mod.placement_is_clean(receipt)
    assert info["reason"] == "ok"
    # her exact words, in order, as several rows (a lone row is the plain title path)
    assert len(rows) >= 2
    assert " ".join(row["text"] for row in rows) == title
    assert intro_plain_text(rows) == title
    assert len({row["color"] for row in rows}) == 1
    assert all(row["source_params"]["source"] == title_mod.TALKING_TITLE_SOURCE for row in rows)
    if language == "tr":
        assert all(row.get("text_language") == "tr" for row in rows)
    for row in rows:
        assert row["source_params"][TITLE_LOOK_PARAM]["style"].startswith("intro_")


def test_a_fit_is_stable_across_renders():
    ask = _stamped()
    first = _fit(ask, EN, _place_with())
    second = _fit(ask, EN, _place_with())
    assert first[0] == second[0]


def test_unclean_real_receipt_falls_back_to_todays_title():
    """Every candidate compiles on the typical speaker, but the REAL placement says no."""
    calls = []

    def place(rows, position):
        calls.append(position)
        return rows, {"status": "best_effort", "face_collision": True}

    rows, receipt, info = _fit(_stamped(), EN, place)
    assert rows is None and receipt is None
    assert calls  # it did try the real placement
    assert len(calls) <= pick.MAX_ATTEMPTS
    assert all(t["reason"] == "placement_unclean" for t in info["tried"] if "reason" in t)


def test_a_face_filling_the_frame_leaves_nothing_clean():
    from app.pipeline.render_geometry import NormalizedBox

    rows, _receipt, info = _fit(_stamped(), EN, _place_with(NormalizedBox(0.02, 0.02, 0.98, 0.98)))
    assert rows is None
    assert info["reason"] in {"no_candidate_fits", "attempts_exhausted"}


@pytest.mark.parametrize(
    "ask",
    [{}, {"inks": ["oxblood"], "candidates": [{"voices": ["solid"], "zones": ["above_head"]}]}],
    ids=["empty", "malformed"],
)
def test_a_bad_stamp_never_raises(ask):
    rows, receipt, info = _fit(ask, EN, _place_with())
    assert rows is None and receipt is None
    assert "reason" in info


def test_a_title_that_changed_after_the_stamp_is_declined():
    ask = _stamped(EN)
    rows, _receipt, _info = _fit(
        ask, "a different title with more words than before", _place_with()
    )
    assert rows is None


def test_pinned_scene_ignores_an_unknown_version():
    assert pick.pinned_intro_scene({pick.INTRO_SCENE_ASK_FIELD: {"version": 9}}) is None
    assert pick.pinned_intro_scene(None) is None


# --- P8: one ink ------------------------------------------------------------------------------


def _frames(luma: int):
    from app.pipeline import caption_legibility as legibility

    return [legibility.GreyFrame(bytes([luma]) * (270 * 480), 270, 480, 0.0, 30.0)]


def _scene_rows():
    rows, _receipt, info = _fit(_stamped(), EN, _place_with())
    assert rows is not None, info
    return rows


def test_ink_is_light_on_a_dark_take_and_dark_on_a_light_one():
    rows = _scene_rows()
    dark = pick.intro_ink_pass(rows, _frames(20), canvas=PORTRAIT)
    light = pick.intro_ink_pass(rows, _frames(235), canvas=PORTRAIT)
    for out in (dark, light):
        assert len({row["color"] for row in out}) == 1
    from app.pipeline import caption_legibility as legibility

    def reads(out, luma):
        return legibility.contrast_ratio(
            legibility.relative_luminance(out[0]["color"]),
            legibility.relative_luminance(f"#{luma:02X}{luma:02X}{luma:02X}"),
        )

    assert reads(dark, 20) >= 4.5
    assert reads(light, 235) >= 4.5
    assert dark[0]["color"] != light[0]["color"]


def _dark_first_rows():
    ask = _stamped()
    ask = {**ask, "inks": ["oxblood", *[i for i in ask["inks"] if i != "oxblood"]]}
    rows, _receipt, info = _fit(ask, EN, _place_with())
    assert rows is not None, info
    assert rows[0]["color"] == scene_mod.INKS["oxblood"]
    return rows


@pytest.mark.parametrize("frames", [[], None], ids=["no-frames", "no-reading"])
def test_an_unmeasured_take_never_keeps_a_dark_first_ink(frames, monkeypatch):
    """Nothing was sampled (proxy download failed, budget spent) or nothing read: the pinned
    first ink is not known to read, so the scene takes the first LIGHT ink, as today's white."""
    from app.pipeline import caption_legibility as legibility

    if frames is None:
        monkeypatch.setattr(legibility, "worst_contrast", lambda *a, **k: None)
        frames = _frames(20)
    out = pick.intro_ink_pass(_dark_first_rows(), frames, canvas=PORTRAIT)
    colors = {row["color"] for row in out}
    assert len(colors) == 1  # still one ink for the whole scene
    assert legibility.relative_luminance(colors.pop()) >= 0.5


def test_ink_never_touches_other_rows_or_hers():
    rows = _scene_rows()
    other = {"id": "caption-1", "text": "x", "color": "#123456", "start_s": 0, "end_s": 1}
    out = pick.intro_ink_pass([*rows, other], _frames(20), canvas=PORTRAIT)
    assert out[-1] == other
    assert pick.intro_ink_pass([other], [], canvas=PORTRAIT) == [other]  # no scene: untouched


def test_a_creator_edit_to_a_scene_row_stays_hers():
    """The scene is ordinary rows: her edit marks the field hers, and a later ink pass (or a
    re-style) leaves it."""
    from app.pipeline.informative_text import CREATOR_FIELDS_KEY, record_creator_edits

    rows = _scene_rows()
    edited = [dict(row, source_params=dict(row["source_params"])) for row in rows]
    edited[0]["color"] = "#FF00AA"
    record_creator_edits(rows, edited)
    assert edited[0]["source_params"][CREATOR_FIELDS_KEY] == ["color"]
    assert CREATOR_FIELDS_KEY not in edited[1]["source_params"]
    out = pick.intro_ink_pass(edited, _frames(20), canvas=PORTRAIT)
    assert out[0]["color"] == "#FF00AA"
    assert out[1]["color"] != "#FF00AA"


# --- P9: the approval check -------------------------------------------------------------------


def _evidence(rows):
    """The rendered text layers as `verify_phone_recipe` sees them: no element ids, `text-N`."""
    return [
        {
            "element_id": f"text-{index}",
            "text": row["text"],
            "start_s": row["start_s"],
            "end_s": row["end_s"],
            "anchor_y": row["y_frac"],
        }
        for index, row in enumerate(rows)
    ]


def test_approval_accepts_her_slashless_title_drawn_as_a_scene():
    rows = _scene_rows()
    [match] = stacked_title_matches(_evidence(rows), EN, tolerance_s=0.05)
    assert match["text"] == EN and len(match["stacked_lines"]) == len(rows)


@pytest.mark.parametrize(
    "change",
    [
        lambda ev: [{**ev[0], "text": ev[0]["text"] + "s"}, *ev[1:]],  # a changed word
        lambda ev: [
            {**ev[0], "anchor_y": ev[1]["anchor_y"]},
            {**ev[1], "anchor_y": ev[0]["anchor_y"]},
            *ev[2:],
        ],  # first two lines swap places
        lambda ev: [{**ev[0], "start_s": 2.0}, *ev[1:]],  # not shown together
        lambda ev: [{**ev[0], "anchor_y": ev[1]["anchor_y"]}, *ev[1:]],  # not top to bottom
        lambda ev: ev[:1],  # a line missing
    ],
    ids=["word", "order", "timing", "same-y", "missing"],
)
def test_approval_negative_controls(change):
    rows = _scene_rows()
    result = stacked_title_matches(change(_evidence(rows)), EN, tolerance_s=0.05)
    assert result == []


def test_approval_still_refuses_a_plain_title_drawn_as_unrelated_rows():
    ev = [
        {"element_id": "text-0", "text": "hello", "start_s": 0, "end_s": 3, "anchor_y": 0.1},
        {"element_id": "text-1", "text": "world", "start_s": 0, "end_s": 3, "anchor_y": 0.2},
    ]
    assert stacked_title_matches(ev, "hello there world", tolerance_s=0.05) == []
    assert stacked_title_matches(ev, "hello world", tolerance_s=0.05) != []  # her words, stacked


def test_a_typed_stack_is_unchanged():
    lines = [
        {"element_id": "text-0", "text": "5 PROJECTS", "start_s": 0, "end_s": 3, "anchor_y": 0.1},
        {
            "element_id": "text-1",
            "text": "the AI community",
            "start_s": 0,
            "end_s": 3,
            "anchor_y": 0.2,
        },
    ]
    assert stacked_title_matches(lines, "5 PROJECTS / the AI community", tolerance_s=0.05)
    assert stacked_title_matches(lines, "5 PROJECTS / other", tolerance_s=0.05) == []


# --- the real job path: flag off / unstamped is byte-identical --------------------------------


def _title_rows(plan, *, title=EN, policy=None, **kwargs):
    from app.tasks.generative_build import _phone_talking_title_rows

    def sample(_path, anchors, **_kw):
        return [ProtectedRegion(0.0, 1e6, FACE, kind="face")], {"decoded": len(anchors)}

    original = title_mod.sample_face_regions
    title_mod.sample_face_regions = sample
    try:
        with phone_text_policy_scope(policy or dict(POLICY)):
            return _phone_talking_title_rows(
                title,
                duration_s=None,
                cues=[],
                binding=SimpleNamespace(
                    original=SimpleNamespace(width=720, height=1280, orientation_degrees=0)
                ),
                clip_path="take.mp4",
                keep_segments=[(0.0, 30.0)],
                landscape_fit="fill",
                canvas=PORTRAIT,
                title_look_plan=plan,
                **kwargs,
            )
    finally:
        title_mod.sample_face_regions = original


def test_unstamped_job_draws_todays_rows():
    legacy = _title_rows(None)
    assert _title_rows({}) == legacy
    assert _title_rows({"unrelated": 1}) == legacy
    assert all(
        "intro_" not in str((row.get("source_params") or {}).get(TITLE_LOOK_PARAM))
        for row in legacy[0]
    )


def test_stamped_job_draws_the_scene_and_yields_when_asked():
    job = FakeJob()
    pick.stamp_seeded_intro_scene(job)
    legacy, _ = _title_rows(None)
    rows, receipt = _title_rows(job.assembly_plan)
    assert rows != legacy and len(rows) >= 2
    assert " ".join(row["text"] for row in rows) == EN
    assert scene_mod.placement_is_clean(receipt) and receipt["intro_scene"]
    # a logo asked above the title, a pinned rule, or the look switch off: today's rows
    assert (
        _title_rows(job.assembly_plan, reserve_logo=True)[0]
        == _title_rows(None, reserve_logo=True)[0]
    )
    ruled = {**POLICY, "requested": {"title": {"font_family": "Anton"}}}
    assert _title_rows(job.assembly_plan, policy=ruled)[0] == legacy


def test_flag_flip_after_dispatch_does_not_restyle(monkeypatch):
    job = FakeJob()
    pick.stamp_seeded_intro_scene(job)
    monkeypatch.setattr(settings, "intro_scene_enabled", False)
    rows, _receipt = _title_rows(job.assembly_plan)
    assert len(rows) >= 2  # the stamp, not the flag, decides


def test_scene_survives_the_clear_of_captions_replacement():
    """`_title_clear_of_captions` places the title again with `replace`; the stamp serves both."""
    job = FakeJob()
    pick.stamp_seeded_intro_scene(job)
    first, _ = _title_rows(job.assembly_plan)
    again, _ = _title_rows(job.assembly_plan, look_caption_avoid=lambda _window: [])
    assert [row["text"] for row in first] == [row["text"] for row in again]


# --- downstream readers of the title -----------------------------------------------------------


def test_the_plan_facts_read_her_slashless_title_from_a_scene():
    """The editor-payload check (`brief_checks`) sees her title as she wrote it, not only the
    " / " form of a stack she never typed."""
    from app.kria.brief_checks import plan_facts_from_editor_payload

    rows = _scene_rows()
    facts = plan_facts_from_editor_payload({"text_elements": rows})
    assert EN in facts.texts  # as she wrote it
    assert any(" / " in text for text in facts.texts)  # the stack form still reads


def test_the_recent_looks_query_builds_against_postgres():
    """The history lookup is a real SQLAlchemy statement over the Job JSONB column."""
    from sqlalchemy.dialects import postgresql

    seen = []

    class Session:
        def execute(self, statement):
            seen.append(str(statement.compile(dialect=postgresql.dialect())))
            return SimpleNamespace(scalars=lambda: iter(["serif_italic"]))

    assert pick.recent_intro_looks(Session(), USER) == ("serif_italic",)
    assert "jsonb_extract_path_text(jobs.assembly_plan" in seen[0]
    assert "ORDER BY jobs.created_at DESC" in seen[0] and "intro_scene_ask" not in seen[0]  # bound


# --- KRI-1633: saved preferences never block the offer; only her look ask for this title ----------
#
# F2 saved preferences block the offer (the bug).   F3 her explicit title-look ask is ignored.
# F4 a font she excluded is offered.                F5 old stamps re-render differently / new
# jobs get a silent scene.

_SAVED_PREFERENCES = {
    "preferences": {
        "title": {
            "position": "top_left",
            "favorite_fonts": ["Anton"],
            "excluded_fonts": ["Zeyada"],
            "font_family": "Anton",
            "color": "#ffffff",
            "stroke_width": 4,
            "shadow_enabled": True,
        },
        "all_text": {"font_family": "Anton", "automatic_font": True},
    },
    "requested": {
        "all_text": {"font_family": "Anton", "color": "#000000"},  # the strategy writer's knobs
        "title": {"position": "top", "favorite_fonts": ["Anton"], "automatic_font": False},
    },
}


def _legacy_has_text_ask(policy) -> bool:
    """KRI-1611's rule, kept here only as the negative control (F2)."""
    for source in (policy.get("requested"), policy.get("preferences")):
        if not isinstance(source, dict):
            continue
        for role in ("all_text", "title"):
            rule = source.get(role)
            if isinstance(rule, dict) and any(k != "automatic_font" for k in rule):
                return True
    return False


def test_saved_preferences_do_not_block_the_offer_but_the_old_rule_did():
    policy = {**POLICY, **_SAVED_PREFERENCES}
    assert _legacy_has_text_ask(policy) is True  # negative control: the old rule blocked
    assert pick.has_text_ask(policy) is False


@pytest.mark.parametrize("key", pick.TITLE_LOOK_ASK_KEYS)
def test_her_look_ask_for_this_title_blocks(key):
    value = {"font_family": "Anton", "color": "#fff", "stroke_width": 3, "shadow_enabled": False}
    assert pick.has_text_ask({"requested": {"title": {key: value[key]}}}) is True


@pytest.mark.parametrize(
    "policy",
    [
        None,
        {},
        {"requested": {"title": {"position": "top_left"}}},
        {"requested": {"all_text": {"font_family": "Anton"}}},
        {"preferences": {"title": {"font_family": "Anton", "color": "#fff"}}},
        {"requested": {"title": {"excluded_fonts": ["Anton"]}}},
        {"requested": {"hook": {"font_family": "Anton"}}},
    ],
)
def test_nothing_but_a_title_look_ask_counts(policy):
    assert pick.has_text_ask(policy) is False


def test_excluded_fonts_are_read_from_every_source_and_never_block():
    policy = {
        "preferences": {"title": {"excluded_fonts": ["Zeyada"]}},
        "requested": {"all_text": {"excluded_fonts": ["Great Vibes"]}},
    }
    assert pick.excluded_font_families(policy) == {"zeyada", "great vibes"}
    assert pick.excluded_font_families(None) == frozenset()
    picked = pick.pick_candidates(EN, seed="s", excluded_fonts=pick.excluded_font_families(policy))
    assert picked
    for candidate in picked:
        faces = scene_mod.FONT_PAIRS[candidate["font_pair"]]
        assert faces.solid.family.casefold() not in {"zeyada", "great vibes"}
        assert faces.accent.family.casefold() not in {"zeyada", "great vibes"}
    assert {c["font_pair"] for c in picked}.isdisjoint({"grotesk_hand", "script_serif"})


def test_the_automatic_pick_is_retired_with_the_model_switch_off(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_model_enabled", False)
    job = FakeJob()
    before = dict(job.assembly_plan)
    pick.stamp_intro_scene(job)
    assert job.assembly_plan == before
    assert pick.INTRO_SCENE_ASK_FIELD not in job.assembly_plan


def test_the_model_arm_still_stamps_when_its_switch_is_on(monkeypatch):
    monkeypatch.setattr(settings, "intro_scene_model_enabled", True)
    job = FakeJob()
    pick.stamp_intro_scene(job)
    assert pick.model_requested(pick.pinned_intro_scene(job.assembly_plan) or {})


def test_a_kri1611_stamped_job_renders_byte_identical_rows():
    """F5: a job stamped under the old rule draws the same rows after the rule changed. The golden
    was produced by origin/main's code (KRI-1611 rule) on this exact stamp and footage."""
    import json
    from pathlib import Path

    golden = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "kri1633_old_stamp_rows.json").read_text()
    )
    rows, receipt = _title_rows(golden["plan"])
    assert rows == golden["rows"]
    assert {k: receipt[k] for k in golden["receipt"]} == golden["receipt"]


# --- KRI-1618: the late accent and the tuck in the seeded pick --------------------------------

_MOVE_TITLES = (EN, "city guide: lisbon, day one", "şehir rehberi: lizbon, ilk gün")


def test_the_moves_never_shift_a_seeds_other_choices(monkeypatch):
    with_moves = {
        (title, seed): pick.pick_candidates(title, seed=seed)
        for title in _MOVE_TITLES
        for seed in ("a", "b", "c", "d")
    }
    monkeypatch.setattr(pick, "_moves", lambda *_args: {})
    for (title, seed), picked in with_moves.items():
        stripped = [{k: v for k, v in c.items() if k not in ("stagger", "stack")} for c in picked]
        assert stripped == pick.pick_candidates(title, seed=seed), (title, seed)


def test_the_pick_offers_each_move_only_where_it_applies():
    seen: set[tuple[str, str]] = set()
    for title in _MOVE_TITLES:
        menu = scene_mod.issue_menu(title)
        for seed in map(str, range(40)):
            for candidate in pick.pick_candidates(title, seed=seed):
                scene = scene_mod.IntroScene(
                    decision="compose",
                    voices=tuple(candidate["voices"]),
                    font_pair=candidate["font_pair"],
                    ink="white",
                    zone=candidate["zones"][0],
                    entrance=candidate["entrance"],
                    sound="none",
                    stagger=candidate.get("stagger", "together"),
                    stack=candidate.get("stack", "gap"),
                )
                assert scene_mod.check_scene(scene, title, menu) is None, candidate
                seen.update(scene.moves().items())
    assert seen == {("stagger", "accent_after"), ("stack", "script_over_serif")}
