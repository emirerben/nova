"""KRI-1613: an intro scene's own sound, from the scene to the lines that declare it.

The real compiler makes the lines; `title_family.intro_sound_cues` is the one reader the worker
and every editor Save use, so these drive it with compiled lines, never hand-built ones. The
expected times come from the requirement (the accent waits `STAGGERS["accent_after"]`, 0.6 s, and
the sound lands on the first frame of the entrance), not from a run of the code.

Failure modes (intro_scene M12-M18) covered here:
* a cue with nothing to play, or with no line to sit on, is accepted;
* an accent-only cue misses the late accent, or sounds on the solid lines too;
* lines that enter together get one sound each (stacked, louder);
* a retime or a short window leaves the cue where the line was;
* a whole-title rewrite or a removed line keeps its sound;
* a silent scene's rows or dumps change.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from pydantic import ValidationError

from app.pipeline import intro_scene as mod
from app.pipeline.title_family import (
    INTRO_SOUND_ID_PREFIX,
    intro_sound_cues,
    retime_title_rows,
)

TITLE = "the biggest design trend right now"
VOICES = ["solid"] * 4 + ["accent"] * 2
LATE = mod.STAGGERS["accent_after"]


def _scene(**over: Any) -> mod.IntroScene:
    return mod.IntroScene.model_validate(
        {
            "decision": "compose",
            "voices": VOICES,
            "font_pair": "grotesk_hand",
            "ink": "oxblood",
            "zone": "above_head",
            "entrance": "pop",
            "sound": "accent-pop",
            **over,
        }
    )


def _check(**over: Any) -> str | None:
    return mod.check_scene(_scene(**over), TITLE, mod.issue_menu(TITLE))


def test_a_cue_needs_a_sound_and_a_line_to_sit_on() -> None:
    assert _check(sound="none", sound_cue="accent") == "sound_cue_needs_sound"
    ref4 = "If Nobody Watches / Your Test Videos"  # two solid lines, no accent
    scene = _scene(voices=["solid"] * 6, font_pair="young_serif", sound_cue="accent")
    assert mod.check_scene(scene, ref4, mod.issue_menu(ref4)) == "sound_cue_has_no_line"
    each = _scene(voices=["solid"] * 6, font_pair="young_serif", sound_cue="each")
    assert mod.check_scene(each, ref4, mod.issue_menu(ref4)) is None
    assert _check(sound_cue="accent") is None


def test_a_cue_or_sound_the_menu_did_not_issue_is_refused() -> None:
    silent_menu = dataclasses.replace(mod.issue_menu(TITLE), sound_cues=("none",))
    assert mod.check_scene(_scene(sound_cue="each"), TITLE, silent_menu) == "option_not_issued"
    with pytest.raises(ValidationError):
        _scene(sound="boom", sound_cue="each")
    with pytest.raises(ValidationError):
        _scene(sound_cue="loud")


def test_a_decline_carries_no_cue() -> None:
    with pytest.raises(ValidationError):
        mod.IntroScene(decision="decline", decline_reason="no_good_fit", sound_cue="accent")


def test_a_silent_scene_dumps_and_compiles_as_before() -> None:
    plain = _scene(sound="none")
    assert "sound_cue" not in plain.model_dump()
    assert plain.moves() == {}
    slug_only = _scene()  # a slug without a cue: every scene before KRI-1613
    assert slug_only.model_dump()["sound"] == "accent-pop"
    assert "sound_cue" not in slug_only.model_dump() and slug_only.moves() == {}


skia = pytest.importorskip("skia")

from app.pipeline.canvas import PORTRAIT  # noqa: E402
from app.pipeline.phone_title_look import collapse_title_look  # noqa: E402


def _rows(**over: Any) -> list[dict[str, Any]]:
    out = mod.compile_intro_scene(
        _scene(**over), TITLE, canvas=PORTRAIT, timeline_duration_s=30.0, language="en"
    )
    assert out.rows is not None, out.reason
    return out.rows


def _accent_id(rows: list[dict[str, Any]]) -> str:
    [row] = [r for r in rows if r["source_params"]["title_look"]["style"] == "intro_accent"]
    return row["id"]


def test_a_silent_scene_declares_nothing_and_its_rows_are_unchanged() -> None:
    silent = _rows(sound="none")
    assert _rows() == silent  # the slug alone changes no row
    assert intro_sound_cues(silent) == []
    assert all("sound" not in r["source_params"]["title_look"] for r in silent)


def test_the_accent_cue_lands_on_the_late_accent_entrance() -> None:
    rows = _rows(stagger="accent_after", sound_cue="accent")
    [cue] = intro_sound_cues(rows)
    assert cue.at_s == pytest.approx(LATE)
    assert cue.sound == "accent-pop"
    assert cue.id == f"{INTRO_SOUND_ID_PREFIX}{_accent_id(rows)}"


def test_one_per_entrance_sounds_each_entrance_once() -> None:
    late = _rows(stagger="accent_after", sound_cue="each")
    assert [cue.at_s for cue in intro_sound_cues(late)] == pytest.approx([0.0, LATE])
    together = _rows(sound_cue="each")  # every line enters on one frame: one entrance
    [cue] = intro_sound_cues(together)
    assert cue.at_s == 0.0 and cue.id == f"{INTRO_SOUND_ID_PREFIX}{together[0]['id']}"


def test_a_retime_moves_the_cue_and_a_short_window_merges_the_entrances() -> None:
    rows = _rows(stagger="accent_after", sound_cue="each")
    moved = retime_title_rows(rows, 2.0, 6.0)
    assert [cue.at_s for cue in intro_sound_cues(moved)] == pytest.approx([2.0, 2.0 + LATE])
    short = retime_title_rows(rows, 2.0, 2.0 + LATE)  # too short to wait: shown together
    [cue] = intro_sound_cues(short)
    assert cue.at_s == pytest.approx(2.0) and cue.id == f"{INTRO_SOUND_ID_PREFIX}{rows[0]['id']}"


def test_a_whole_title_rewrite_or_a_removed_line_takes_its_sound_away() -> None:
    rows = _rows(stagger="accent_after", sound_cue="accent")
    first, _rest = collapse_title_look(rows, rows)
    assert intro_sound_cues([first]) == []
    accent = _accent_id(rows)
    gone = [{**r, "removed": True} if r["id"] == accent else r for r in rows]
    assert intro_sound_cues(gone) == []
