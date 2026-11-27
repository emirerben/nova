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
    "control",
    [
        "two_inks",
        "title_on_face",
        "face_swapped_font",
        "ink_unreadable",
        "undeclared_overlap",
        "declared_tuck_not_drawn",
        "unissued_delay",
        "solid_line_waits",
        "card_over_title",
        "card_over_face",
        "card_bleeds_off_frame",
        "card_on_captions",
    ],
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


# --- KRI-1618: the late accent and the script tucked onto the serif -------------------------
#
# M1 the stagger holds back the solid rows or the accent outlives them; M3 a stack whose accent
# leads fails the opening check; M4 a hold too short for the delay; M7 the tuck with a pair that
# has no script face, or no script row above a serif row; M8 the tuck buries the serif (the
# geometry is the taste board's); M11 a scene without the moves changes.

TR_VOICES = ["solid"] * 6 + ["accent"] * 2
REF5_VOICES = ["accent", "accent", "solid", "solid", "solid"]
REF5_TITLES = {"en": "city guide: lisbon, day one", "tr": "şehir rehberi: lizbon, ilk gün"}


def _scene_rows(title, voices, lang, **over):
    from app.pipeline.canvas import PORTRAIT  # noqa: PLC0415

    scene = _compose(voices, sound="none", **over)
    out = mod.compile_intro_scene(
        scene, title, canvas=PORTRAIT, timeline_duration_s=30.0, language=lang
    )
    assert out.rows is not None, out.reason
    placed, receipt = mod.place_on_typical_face(out.rows, out.position, PORTRAIT)
    assert mod.placement_is_clean(receipt), receipt
    return placed


def _phone_layers(rows):
    from app.pipeline.phone_subtitled_plan import _compile_text_lane  # noqa: PLC0415

    return _compile_text_lane(rows, user_edited=False, timeline_duration_s=30.0)


@pytest.mark.parametrize(
    ("title", "voices", "lang"), [(TITLE_EN, EN_VOICES, "en"), (TITLE_TR, TR_VOICES, "tr")]
)
def test_a_late_accent_enters_after_the_solid_rows_and_leaves_with_them(title, voices, lang):
    rows = _scene_rows(title, voices, lang, stagger="accent_after", ink="cream")
    delay = mod.STAGGERS["accent_after"]
    layers = _phone_layers(rows)
    starts = {
        row["source_params"]["title_look"]["style"]: layer.start
        for row, layer in zip(rows, layers, strict=True)
    }
    assert starts == {"intro_solid": 0.0, "intro_accent": pytest.approx(delay)}
    assert len({layer.end for layer in layers}) == 1
    [match] = check.approval_matches(rows, title)
    assert match["start_s"] == 0.0  # the title is up from its first entrance (opening check)


def test_a_title_whose_accent_leads_still_opens_the_video():
    rows = _scene_rows(
        REF5_TITLES["en"],
        REF5_VOICES,
        "en",
        font_pair="script_serif",
        ink="white",
        stagger="accent_after",
    )
    assert rows[0]["start_s"] == pytest.approx(mod.STAGGERS["accent_after"])
    [match] = check.approval_matches(rows, REF5_TITLES["en"])
    assert match["start_s"] == 0.0


def test_a_hold_too_short_for_the_delay_is_declined_not_squeezed():
    scene = _compose(EN_VOICES, font_pair="serif_italic", sound="none", stagger="accent_after")
    short = _compile(scene, duration_s=mod.STAGGERS["accent_after"] + 0.05)
    assert (short.rows, short.reason) == (None, "hold_too_short")
    together = _compile(scene.model_copy(update={"stagger": "together"}), duration_s=0.65)
    assert together.reason == "ok"


@pytest.mark.parametrize(
    ("over", "voices", "reason"),
    [
        (
            {"font_pair": "grotesk_hand", "stack": "script_over_serif"},
            EN_VOICES,
            "option_not_issued",
        ),
        (
            {"font_pair": "script_serif", "stack": "script_over_serif"},
            EN_VOICES,
            "stack_needs_script_above_serif",
        ),
        (
            {"font_pair": "young_serif", "stagger": "accent_after"},
            ["solid"] * 6,
            "stagger_needs_both_voices",
        ),
    ],
    ids=["no-script-face", "script-below-serif", "nothing-to-stagger"],
)
def test_a_move_that_cannot_apply_is_refused(over, voices, reason):
    scene = _compose(voices, **over)
    assert mod.check_scene(scene, TITLE_EN, mod.issue_menu(TITLE_EN)) == reason
    assert _compile(scene).reason == reason


@pytest.mark.parametrize("lang", ["en", "tr"])
def test_the_tuck_sets_the_script_baseline_where_the_taste_board_does(lang):
    title = REF5_TITLES[lang]
    common = {"font_pair": "script_serif", "ink": "white"}
    tucked = _phone_layers(
        _scene_rows(title, REF5_VOICES, lang, stack="script_over_serif", **common)
    )
    plain = _phone_layers(_scene_rows(title, REF5_VOICES, lang, **common))
    serif = tucked[1].runs[0]
    font = skia.Font(
        skia.Typeface.MakeFromFile(
            str(Path(__file__).resolve().parents[2] / "assets/fonts/Newsreader-Medium.ttf")
        ),
        serif.font_size,
    )
    rise = serif.baseline_y - tucked[0].runs[0].baseline_y
    assert rise == pytest.approx(
        mod.STACKS["script_over_serif"].tuck_caps * font.getMetrics().fCapHeight, abs=2.0
    )
    plain_rise = plain[1].runs[0].baseline_y - plain[0].runs[0].baseline_y
    assert plain_rise > rise + 40  # without the declaration the rows keep the plain gap


def test_a_scene_without_the_moves_compiles_to_todays_rows():
    explicit = _compile(_compose(EN_VOICES, stagger="together", stack="gap"))
    default = _compile(_compose(EN_VOICES))
    assert explicit.rows == default.rows
    for row in default.rows:
        assert row["start_s"] == 0.0
        assert set(row["source_params"]["title_look"]) == {"line", "lines", "style"}


def test_render_check_draws_each_move_in_both_languages(report):  # KRI-1618 M8-M10
    moves = [c for c in report["cases"] if str(c.get("name")).startswith("KRI-1618")]
    moves = [c for c in moves if not c["control"]]
    assert {(c["id"], c["language"]) for c in moves} == {
        (case_id, language)
        for base in ("late-accent", "script-tuck")
        for case_id in (base, f"{base}@0.3s")
        for language in ("en", "tr")
    }
    assert all(c["verdict"] == "PASS" for c in moves), [c["reasons"] for c in moves]


@pytest.mark.parametrize("face_top", [0.18, 0.2])
def test_a_squeezed_title_keeps_the_tuck(face_top):
    """A tall face leaves the band above it too short: the title squeezes (ink to ink, smaller)
    and the tuck still sits on the board's geometry at that size."""
    from app.pipeline.canvas import PORTRAIT
    from app.pipeline.phone_title_look import place_title_look
    from app.pipeline.render_geometry import ProtectedRegion

    face = mod.NormalizedBox(0.2, face_top, 0.8, 0.85)

    def sample(_path, anchors, **_kwargs):
        return [ProtectedRegion(0.0, 1e6, face, kind="face")], {"attempted": len(anchors)}

    scene = _compose(
        REF5_VOICES, font_pair="script_serif", ink="white", sound="none", stack="script_over_serif"
    )
    out = mod.compile_intro_scene(
        scene, REF5_TITLES["en"], canvas=PORTRAIT, timeline_duration_s=30.0, language="en"
    )
    placed, receipt = place_title_look(
        out.rows,
        clip_path="take.mp4",
        keep_segments=[(0.0, 1e6)],
        display_width=1080,
        display_height=1920,
        canvas=PORTRAIT,
        sample=sample,
    )
    assert receipt["status"] == "squeezed" and receipt["scale"] < 1.0, receipt
    script, serif = (layer.runs[0] for layer in _phone_layers(placed))
    font = skia.Font(
        skia.Typeface.MakeFromFile(str(PROMPTS.parent / "assets/fonts/Newsreader-Medium.ttf")),
        serif.font_size,
    )
    rise = serif.baseline_y - script.baseline_y
    caps = mod.STACKS["script_over_serif"].tuck_caps
    assert rise == pytest.approx(caps * font.getMetrics().fCapHeight, abs=2.0)


# --- KRI-1615: her photo cards laid out with the title ----------------------------------------


def test_render_check_draws_both_arrangements_in_both_languages(report):  # M22-M29
    cards = [c for c in report["cases"] if c.get("name") == "KRI-1615 cards" and not c["control"]]
    assert {(c["id"], c["language"]) for c in cards} == {
        (case_id, language)
        for case_id in ("ref3+around_speaker", "ref5+grid_2x2")
        for language in ("en", "tr")
    }
    assert all(c["verdict"] == "PASS" for c in cards), [c["reasons"] for c in cards]
    assert {len(c["cards"]) for c in cards} == {2, 4}


def test_render_check_draws_pop_cards_over_a_close_speaker(report):  # M47-M56
    pop = [c for c in report["cases"] if c.get("name") == "KRI-1615 pop cards"]
    cases = [c for c in pop if not c["control"]]
    assert {(c["id"], c["language"]) for c in cases} == {
        (f"{case_id}+pop", language)
        for case_id in ("ref3+around_speaker", "ref5+grid_2x2")
        for language in ("en", "tr")
    }
    assert all(c["verdict"] == "PASS" for c in cases), [c["reasons"] for c in cases]
    controls = {c["control"]: c["verdict"] for c in pop if c["control"]}
    assert controls == {
        "pop_card_over_title": "FAIL",
        "pop_card_on_captions": "FAIL",
        "pop_card_bleeds_off_frame": "FAIL",
        "pop_card_held_2s": "FAIL",
    }
    wiring = {w["name"]: w["ok"] for w in report["wiring"]}
    assert wiring["a_close_speaker_leaves_clean_cards_no_room"]
    assert wiring["cards_never_pop_over_the_stand_in_speaker"]


def test_render_check_wires_the_card_declines(report):
    wiring = {w["name"]: w for w in report["wiring"]}
    for name in (
        "cards_decline_on_a_close_speaker",
        "no_grid_from_three_photos",
        "no_arrangement_is_issued_without_her_photos",
    ):
        assert wiring[name]["ok"], wiring[name]


def test_props_sweep_renders_clean_or_declines():
    sweep = check.sweep_props(thorough=False)
    assert sweep["compiled"] > 50, sweep  # not vacuous: most shapes fit the roomy speaker
    assert sweep["declined"], sweep  # and the tight corner declines, not renders
    assert sweep["failed"] == [], sweep["failed"][:3]
