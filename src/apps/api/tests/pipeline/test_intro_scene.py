"""KRI-1610: the IntroScene schema, its compiler to title rows, and the five teardowns.

Written before the module. Each test is a way the scene could go wrong:

* S1, S4: the schema holds no free text or number and a decline is explicit (see
  test_intro_scene_schema.py, which stays skia-free).
* S2: the split drops, adds or reorders a word. One voice per title word, in order; rows join
  back to the creator's exact words (`test_rows_are_her_words_in_order`).
* S3: an id the server never issued (a face that cannot draw Turkish, a pen entrance on a face
  that is not the pen's) is accepted (`test_menu_*`).
* S4: a decline is half-filled, or a compose is missing a choice (`test_decline_*`,
  `test_compose_*`).
* S5: a scene the stacked-title machinery would read differently (one run, a run under two
  characters, more than five runs) compiles anyway (`test_compile_declines_*`).
* S6: any failure breaks the render instead of falling back to today's title rows
  (`test_compile_fails_open_*`).
* S7: Turkish caps break the dotless / dotted i (`test_turkish_caps_*`).
* S8: more than one ink reaches a row (`test_one_ink_*`).
* S9: the teardown data drifts from the code: a scene that no longer validates, an "expressible"
  reference that no longer compiles, a non-expressible one with no named missing move
  (`test_teardowns_*`).
* S10: the render check passes something it should not: two inks, a title on the face, a face
  swapped for another (`test_render_check_*`; the negative controls MUST fail).
* S11: the issued menu lets through a scene the normal placement puts on the face or above the
  safe zone: a long accent run at 250px, a long caps row, a top-left row over the head. The
  compiler declines them (`test_compile_declines_*_that_cannot_sit_clear`) and the check sweeps
  every issued pair and zone over stress titles, so the claim is not 8 hand-picked fixtures
  (`test_issued_menu_sweep_*`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.pipeline import intro_scene as mod

PROMPTS = Path(__file__).resolve().parents[2] / "prompts"
TEARDOWNS = PROMPTS / "intro_teardowns.json"

TITLE_EN = "the biggest design trend right now"
TITLE_TR = "bu yılın en büyük tasarım trendi şu an"


def _compose(voices, **over: Any) -> mod.IntroScene:
    base: dict[str, Any] = {
        "decision": "compose",
        "voices": list(voices),
        "font_pair": "grotesk_hand",
        "ink": "oxblood",
        "zone": "above_head",
        "entrance": "fade",
        "sound": "accent-pop",
    }
    base.update(over)
    return mod.IntroScene.model_validate(base)


EN_VOICES = ["solid"] * 4 + ["accent"] * 2


# --- title words --------------------------------------------------------------------------


def test_title_words_keep_her_words_and_line_breaks():
    words = mod.title_words("5 PROJECTS / the AI builder\ncommunity")
    assert [w.text for w in words] == ["5", "PROJECTS", "the", "AI", "builder", "community"]
    assert [w.segment for w in words] == [0, 0, 1, 1, 1, 2]


def test_a_slash_inside_a_word_is_not_a_break():
    assert [w.segment for w in mod.title_words("24/7 and A/B")] == [0, 0, 0]


# --- S3: the issued menu ------------------------------------------------------------------


def test_menu_for_plain_english_offers_every_pair():
    assert set(mod.issue_menu(TITLE_EN).font_pairs) == set(mod.FONT_PAIRS)


def test_menu_only_issues_pairs_that_draw_the_title():
    menu = mod.issue_menu(TITLE_TR + " ğşıİçö")
    for pair_id in menu.font_pairs:
        for face in (mod.FONT_PAIRS[pair_id].solid, mod.FONT_PAIRS[pair_id].accent):
            assert mod.face_draws(face.family, TITLE_TR + " ğşıİçö")


def test_pen_entrance_only_with_the_pens_own_face():
    menu = mod.issue_menu(TITLE_EN)
    for pair_id in menu.font_pairs:
        pair = mod.FONT_PAIRS[pair_id]
        assert ("pen" in menu.entrances_for(pair_id)) == (pair.accent.family == "Patrick Hand")


def test_scene_with_a_pair_outside_the_menu_is_not_issued():
    menu = mod.IntroMenu(
        font_pairs=("serif_italic",),
        inks=tuple(mod.INKS),
        zones=tuple(mod.ZONES),
        entrances=tuple(mod.ENTRANCES),
        sounds=tuple(mod.SOUNDS),
    )
    assert mod.check_scene(_compose(EN_VOICES), TITLE_EN, menu) == "option_not_issued"


def test_pen_on_a_non_pen_pair_is_not_issued():
    scene = _compose(EN_VOICES, entrance="pen")
    assert mod.check_scene(scene, TITLE_EN, mod.issue_menu(TITLE_EN)) == "option_not_issued"


# --- S2: split completeness ---------------------------------------------------------------


@pytest.mark.parametrize("voices", [EN_VOICES[:-1], [*EN_VOICES, "solid"]])
def test_voices_must_be_one_per_word(voices):
    scene = _compose(voices)
    assert mod.check_scene(scene, TITLE_EN, mod.issue_menu(TITLE_EN)) == "voices_not_one_per_word"


def test_good_scene_passes_the_check():
    assert mod.check_scene(_compose(EN_VOICES), TITLE_EN, mod.issue_menu(TITLE_EN)) is None


# --- compile ------------------------------------------------------------------------------

skia = pytest.importorskip("skia")

from app.kria.recipes import Canvas  # noqa: E402
from app.pipeline import phone_subtitled_title as title_mod  # noqa: E402
from app.pipeline.phone_title_look import (  # noqa: E402
    is_title_look_row,
    split_title_lines,
    title_look_text,
)

CANVAS = Canvas(width=1080, height=1920)


def _compile(scene, title=TITLE_EN, **kwargs):
    kwargs.setdefault("canvas", CANVAS)
    kwargs.setdefault("timeline_duration_s", 30.0)
    return mod.compile_intro_scene(scene, title, **kwargs)


def test_rows_are_her_words_in_order():
    out = _compile(_compose(EN_VOICES))
    assert out.rows is not None, out.reason
    assert [r["text"] for r in out.rows] == ["the biggest design trend", "right now"]
    assert " ".join(r["text"] for r in out.rows).split() == TITLE_EN.split()


def test_rows_validate_as_text_elements_with_no_new_keys():
    from app.agents._schemas.text_element import TextElement

    out = _compile(_compose(EN_VOICES))
    for row in out.rows:
        TextElement.model_validate(row)
        assert is_title_look_row(row)
        assert row["source_params"]["source"] == title_mod.TALKING_TITLE_SOURCE


def test_rows_round_trip_through_the_stacked_title_reader():
    out = _compile(_compose(EN_VOICES))
    typed = title_look_text(out.rows)
    assert typed == out.typed_title == "the biggest design trend / right now"
    assert split_title_lines(typed) == [r["text"] for r in out.rows]


def test_caps_come_from_text_case_not_from_rewriting_words():
    out = _compile(_compose(EN_VOICES), title="the biggest design trend right now")
    assert out.rows[0]["text"] == "the biggest design trend"
    assert out.rows[0]["text_case"] == "upper"


def test_hard_breaks_the_creator_typed_split_rows_even_inside_one_voice():
    out = _compile(
        _compose(["solid"] * 6, font_pair="young_serif", ink="butter"),
        title="If Nobody Watches / Your Test Videos",
    )
    assert out.rows is not None, out.reason
    assert [r["text"] for r in out.rows] == ["If Nobody Watches", "Your Test Videos"]


def test_alternating_voices_make_alternating_rows():
    out = _compile(
        _compose(["accent", "accent", "solid", "solid"]), title="City guide: Lisbon, Day"
    )
    assert out.rows is not None, out.reason
    assert [r["font_family"] for r in out.rows] == ["Zeyada", "Hanken Grotesk"]


# --- S8: one ink --------------------------------------------------------------------------


@pytest.mark.parametrize("ink", sorted(mod.INKS))
def test_one_ink_reaches_every_row(ink):
    out = _compile(_compose(EN_VOICES, ink=ink))
    assert {r["color"] for r in out.rows} == {mod.INKS[ink]}


# --- S5: scenes the stack machinery would read differently --------------------------------


def test_compile_declines_a_single_run():
    out = _compile(_compose(["solid"] * 6))
    assert out.rows is None and out.reason == "single_run"


def test_compile_declines_a_one_character_run():
    out = _compile(_compose(["solid"] * 5 + ["accent"]), title="the biggest design trend now x")
    assert out.rows is None and out.reason == "run_too_short"


def test_compile_declines_a_number_only_run():
    out = _compile(_compose(["solid", "solid", "accent"]), title="top five 2026")
    assert out.rows is None and out.reason == "run_too_short"


def test_compile_declines_more_than_five_runs():
    voices = ["solid", "accent"] * 3
    out = _compile(_compose(voices), title="one two three four five six")
    assert out.rows is None and out.reason == "too_many_runs"


# --- S6: fail open ------------------------------------------------------------------------


def test_compile_fails_open_on_a_decline():
    scene = mod.IntroScene.model_validate({"decision": "decline", "decline_reason": "no_good_fit"})
    out = _compile(scene)
    assert out.rows is None and out.reason == "declined"


def test_compile_fails_open_on_a_malformed_scene():
    out = _compile({"decision": "compose", "ink": "nope"})
    assert out.rows is None and out.reason == "invalid_scene"


def test_compile_fails_open_on_an_empty_title():
    assert _compile(_compose(EN_VOICES), title="   ").rows is None


def test_compile_never_raises(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("renderer fell over")

    monkeypatch.setattr(mod, "_build_rows", boom)
    out = _compile(_compose(EN_VOICES))
    assert out.rows is None and out.reason == "compile_error"


# --- S7: Turkish --------------------------------------------------------------------------


def test_turkish_caps_keep_the_dotted_i():
    from app.pipeline.phone_text_style import displayed_text

    out = _compile(
        _compose(["solid"] * 6 + ["accent"] * 2, entrance="fade"), TITLE_TR, language="tr"
    )
    assert out.rows is not None, out.reason
    assert displayed_text(out.rows[0]) == "BU YILIN EN BÜYÜK TASARIM TRENDİ"


def test_every_issued_pair_draws_turkish_with_its_own_faces():
    # `drawable_family` would swap a face that cannot draw it and hide the change.
    menu = mod.issue_menu(TITLE_TR)
    assert menu.font_pairs, "no pair draws Turkish"
    for pair_id in menu.font_pairs:
        out = _compile(
            _compose(["solid"] * 6 + ["accent"] * 2, font_pair=pair_id, entrance="fade"),
            TITLE_TR,
            language="tr",
        )
        assert out.rows is not None, (pair_id, out.reason)
        wanted = mod.FONT_PAIRS[pair_id]
        assert [r["font_family"] for r in out.rows] == [wanted.solid.family, wanted.accent.family]


def test_a_face_that_cannot_draw_the_title_is_declined_not_swapped(monkeypatch):
    # Satisfy lacks Turkish capitals; `drawable_family` would swap in Pacifico unannounced.
    pair = mod.FontPair(mod.Face("Satisfy", 100), mod.Face("Satisfy", 100))
    monkeypatch.setitem(mod.FONT_PAIRS, "grotesk_hand", pair)
    assert "grotesk_hand" not in mod.issue_menu(TITLE_TR).font_pairs
    everything = mod.IntroMenu(
        font_pairs=tuple(mod.FONT_PAIRS),
        inks=tuple(mod.INKS),
        zones=tuple(mod.ZONES),
        entrances=tuple(mod.ENTRANCES),
        sounds=tuple(mod.SOUNDS),
    )
    out = _compile(
        _compose(["solid"] * 6 + ["accent"] * 2), TITLE_TR, language="tr", menu=everything
    )
    assert out.rows is None and out.reason == "face_cannot_draw_title"


# --- S9: the teardown data ----------------------------------------------------------------


def _teardowns() -> dict:
    return json.loads(TEARDOWNS.read_text())


def test_teardowns_file_has_a_version_and_the_five_seed_references():
    """The five references of KRI-1610 stay first; the intake script (KRI-1614) appends more."""
    data = _teardowns()
    assert data["version"]
    ids = [t["id"] for t in data["teardowns"]]
    assert ids == [f"ref{n}" for n in range(1, len(ids) + 1)] and len(ids) >= 5


def test_teardowns_carry_no_free_text_into_scenes():
    """A teardown's titles are fixtures for the render check; the scene itself is ids only."""
    for teardown in _teardowns()["teardowns"]:
        for example in (teardown.get("examples") or {}).values():
            assert set(example) == {"title", "scene"}
            mod.IntroScene.model_validate(example["scene"])


def test_every_teardown_example_is_a_valid_issued_scene():
    for teardown in _teardowns()["teardowns"]:
        for language, example in (teardown.get("examples") or {}).items():
            scene = mod.IntroScene.model_validate(example["scene"])
            menu = mod.issue_menu(example["title"])
            assert mod.check_scene(scene, example["title"], menu) is None, (
                teardown["id"],
                language,
            )


def test_expressible_teardowns_compile_in_both_languages():
    for teardown in _teardowns()["teardowns"]:
        if teardown["expressible"] == "no":
            continue
        assert set(teardown["examples"]) == {"en", "tr"}, teardown["id"]
        for language, example in teardown["examples"].items():
            out = _compile(
                mod.IntroScene.model_validate(example["scene"]),
                example["title"],
                language=language,
            )
            assert out.rows is not None, (teardown["id"], language, out.reason)


def test_inexpressible_pieces_are_named_with_the_missing_move():
    for teardown in _teardowns()["teardowns"]:
        if teardown["expressible"] == "yes":
            assert teardown["missing_moves"] == []
        else:
            assert teardown["missing_moves"], teardown["id"]
            for move in teardown["missing_moves"]:
                assert set(move) == {"piece", "move"} and move["piece"] and move["move"]


def test_engine_limits_name_the_missing_move():
    limits = _teardowns()["engine_limits"]
    assert limits
    assert all(set(limit) == {"piece", "move"} and limit["move"] for limit in limits)


def test_every_teardown_says_what_footage_it_suits_and_why_it_works():
    for teardown in _teardowns()["teardowns"]:
        assert teardown["footage_plate"] in {"light_wall", "dark_room"}
        assert teardown["footage_fit"].strip()
        assert teardown["why_it_works"].strip()


def test_sounds_are_in_the_seed_sfx_catalogue():
    from scripts.sfx_library.catalog import LIBRARY

    known = {effect.slug for effect in LIBRARY}
    assert {s for s in mod.SOUNDS if s != "none"} <= known


# --- S11: scenes that compile but cannot sit clear ----------------------------------------


def _compile_clear(title, voices, lang, pair, zone, ink="oxblood"):
    from app.pipeline.canvas import PORTRAIT  # noqa: PLC0415

    scene = _compose(voices, font_pair=pair, zone=zone, ink=ink, sound="none")
    return mod.compile_intro_scene(
        scene, title, canvas=PORTRAIT, timeline_duration_s=30.0, language=lang
    )


def test_compile_declines_a_long_loop_accent_that_cannot_sit_clear():
    out = _compile_clear(
        "how am i / really loving this",
        ["solid"] * 3 + ["accent"] * 3,
        "en",
        "grotesk_loop",
        "above_head",
    )
    assert out.rows is None
    assert out.reason == "placement_unclean"


@pytest.mark.parametrize("pair", ["grotesk_hand", "script_serif", "young_serif"])
def test_compile_declines_a_top_left_row_that_covers_the_face_box(pair):
    out = _compile_clear(TITLE_TR, ["solid"] * 6 + ["accent"] * 2, "tr", pair, "top_left")
    assert out.rows is None
    assert out.reason == "ink_off_zone"


@pytest.mark.parametrize("pair", ["grotesk_hand", "grotesk_pen"])
def test_compile_declines_a_long_caps_row_that_rises_above_the_safe_zone(pair):
    out = _compile_clear(
        "bir tasarımcı olarak bir günde ne yiyorum / dürüstçe",
        ["solid"] * 7 + ["accent"],
        "tr",
        pair,
        "above_head",
        ink="cream",
    )
    assert out.rows is None
    assert out.reason == "ink_off_zone"


def test_a_scene_that_sits_clear_still_compiles():
    out = _compile_clear(TITLE_EN, EN_VOICES, "en", "serif_italic", "above_head")
    assert out.reason == "ok"


def test_ink_box_problems_name_each_way_a_row_is_off():
    face, captions = mod.TYPICAL_FACE, mod.TYPICAL_CAPTIONS
    clear = mod.NormalizedBox(0.2, 0.1, 0.8, 0.2)
    assert mod.ink_box_problems(clear, face=face, captions=captions) == []
    assert mod.ink_box_problems(
        mod.NormalizedBox(0.2, 0.054, 0.8, 0.2), face=face, captions=captions
    )
    assert mod.ink_box_problems(mod.NormalizedBox(0.3, 0.3, 0.7, 0.4), face=face, captions=captions)
    assert mod.ink_box_problems(
        mod.NormalizedBox(0.2, 0.82, 0.8, 0.86), face=face, captions=captions
    )


@pytest.mark.parametrize(
    ("receipt", "clean"),
    [
        ({"status": "preset"}, True),
        ({"status": "squeezed", "face_collision": False}, True),
        ({"status": "best_effort"}, False),
        ({"status": "error"}, False),
        ({"status": "shrunk", "face_collision": True}, False),
        ({"status": "shrunk", "text_overlap": True}, False),
        ({}, False),
    ],
)
def test_placement_is_clean_reads_the_receipt(receipt, clean):
    assert mod.placement_is_clean(receipt) is clean


# --- S10: the render check ----------------------------------------------------------------

from app.cli import verify_intro_scenes as check  # noqa: E402


@pytest.fixture(scope="module")
def report(tmp_path_factory):
    out = tmp_path_factory.mktemp("intro-verify")
    return {**check.run(out_dir=str(out), sweep=False), "out": out}


def test_render_check_passes_every_expressible_reference_in_both_languages(report):
    rendered = [c for c in report["cases"] if not c["control"] and c["rendered"]]
    assert {c["language"] for c in rendered} == {"en", "tr"}
    assert rendered
    for case in rendered:
        assert case["verdict"] == "PASS", (case["id"], case["language"], case["reasons"])


def test_render_check_lists_what_it_could_not_render(report):
    skipped = [c for c in report["cases"] if not c["control"] and not c["rendered"]]
    assert {c["id"] for c in skipped} == {"ref2"}


@pytest.mark.parametrize(
    "control", ["two_inks", "title_on_face", "face_swapped_font", "ink_unreadable"]
)
def test_render_check_negative_controls_must_fail(report, control):
    case = next(c for c in report["cases"] if c["control"] == control)
    assert case["verdict"] == "FAIL", case


def test_issued_menu_sweep_renders_clean_or_declines():
    sweep = check.sweep_issued_menu(thorough=False)
    assert sweep["compiled"] > 50, sweep  # not vacuous: most of the menu does compile
    assert sweep["declined"], sweep  # and the risky corner is declined, not rendered
    assert sweep["failed"] == [], sweep["failed"][:3]


def test_render_check_writes_the_montage_and_report(report):
    assert (report["out"] / report["montage"]).stat().st_size > 10_000
    assert (report["out"] / report["report"]).exists()
