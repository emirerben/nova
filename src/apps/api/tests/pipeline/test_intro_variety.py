"""KRI-1614: the variety report catches an intro picker that has collapsed into a few looks.

Written before the report. Each test is a way the report could be wrong, in either direction:

* V1 one option dominates and the report still passes (`test_one_dominant_option_fails`).
* V2 every axis looks fine but the three top looks cover the videos (`test_a_few_looks_collapse`).
* V3 an issued option is never picked (`test_a_starved_option_fails`).
* V4 a forced axis fails the report: on the light plate only one ink reads
  (`test_the_ink_a_plate_forces_is_exempt_with_its_computation`), judged apart from the picker.
* V5 a stratum too small to judge passes silently (`test_a_stratum_too_small_to_judge_fails`).
* V6 the picker falls back to today's title most of the time (`test_mostly_declined_fails`).
* V7 the check is blind: a picker that ignores its seed, one that never picks an option, one that
  collapses in Turkish only, must each be caught (`test_every_control_is_caught`, real picker).
* V8 pooling hides a language (`test_a_collapse_in_one_language_is_named_in_that_language`).
* V9 a wobble in Skia measurements fails the gate, so nothing here asserts an exact count; a
  lucky small sample must not fail (`test_a_lucky_small_sample_does_not_fail_but_a_collapse_does`).

Skia is imported hard: this is the CI gate, and a silent skip would let it rot.
"""

from __future__ import annotations

import json
from itertools import cycle, islice
from pathlib import Path

import skia  # noqa: F401  - required, not optional: see the module docstring

from app.cli import intro_variety_report as rep


def _spread(language: str, n: int = 90, plate: str = "") -> list[rep.Look]:
    """Looks that walk through every issued option evenly (the opposite of a collapse)."""
    issued = rep.issued_options(language)
    pairs, ents, zones = (sorted(issued[a]) for a in ("font_pair", "entrance", "zone"))
    lens, poss = sorted(issued["accent_len"]), sorted(issued["accent_pos"])
    cols = [cycle(pairs), cycle(ents), cycle(zones), cycle(lens), cycle(poss)]
    looks = []
    for i in range(n):
        pair, ent, zone, length, pos = (next(c) for c in cols)
        # offset the cycles so axes are not locked together
        if i % 7 == 0:
            next(cols[1])
        looks.append(rep.Look(language, "t", plate, f"s{i}", pair, zone, None, ent, length, pos))
    return looks


def _both(n: int = 90) -> list[rep.Look]:
    return _spread("en", n) + _spread("tr", n)


def _judge(looks, **over):
    args = {"layer": "pool", "starvation": True, "with_ink": False}
    args.update(over)
    return rep.judge_looks(looks, **args)


def _replace(looks, **fields):
    return [rep.Look(**{**look.__dict__, **fields}) for look in looks]


def test_an_even_spread_passes():
    report = _judge(_both())
    assert report["failures"] == [], report["failures"]


def test_one_dominant_option_fails():
    # 80 percent of the English picks use one font pair
    en = [
        rep.Look(**{**look.__dict__, "font_pair": "young_serif"}) if i % 10 < 8 else look
        for i, look in enumerate(_spread("en"))
    ]
    failures = _judge(en + _spread("tr"))["failures"]
    assert [(f["stratum"], f["axis"], f["check"]) for f in failures] == [
        ("pool/en", "font_pair", "dominance")
    ]


def test_a_starved_option_fails():
    looks = [look for look in _both(300) if look.entrance != "pen"]
    failures = _judge(looks)["failures"]
    assert {(f["stratum"], f["axis"], f["check"]) for f in failures} >= {
        ("pool/en", "entrance", "starved"),
        ("pool/tr", "entrance", "starved"),
    }
    assert all("pen" in f["detail"] for f in failures if f["check"] == "starved")


def test_a_collapse_in_one_language_is_named_in_that_language():
    en = _spread("en")
    tr = _replace(
        _spread("tr"),
        font_pair="young_serif",
        entrance="fade",
        zone="above_head",
        accent_len=1,
        accent_pos="middle",
    )
    failures = _judge(en + tr)["failures"]
    assert failures
    assert {f["stratum"] for f in failures} == {"pool/tr"}


def test_a_stratum_too_small_to_judge_fails():
    failures = _judge(_spread("en", 10) + _spread("tr", 10))["failures"]
    assert {f["check"] for f in failures} == {"sample_too_small"}


def test_a_few_looks_collapse():
    """Every axis is spread, but only three whole looks ever appear."""
    base = _spread("en", 3, plate="dark_room")
    looks = list(islice(cycle(base), 90))
    inked = [rep.Look(**{**look.__dict__, "ink": "white"}) for look in looks]
    report = rep.judge_looks(inked, layer="realized", starvation=False, with_ink=True)
    assert any(f["check"] == "collapse" for f in report["failures"]), report["failures"]


def test_the_ink_a_plate_forces_is_exempt_with_its_computation():
    # Computed from the plate's contrast, apart from the picker.
    assert set(rep.viable_inks("light_wall")) == {"oxblood"}
    assert "oxblood" not in rep.viable_inks("dark_room")
    assert len(rep.viable_inks("dark_room")) >= 2
    light = [
        rep.Look(**{**look.__dict__, "plate": "light_wall", "ink": "oxblood"})
        for look in _spread("en", 60)
    ]
    report = rep.judge_looks(light, layer="realized", starvation=False, with_ink=True)
    exempt = [e for e in report["exemptions"] if e["axis"] == "ink"]
    assert exempt and "oxblood" in exempt[0]["reason"] and "7." in exempt[0]["reason"]
    assert not [
        f
        for f in report["failures"]
        if f["axis"] == "ink" and f["stratum"] == "realized/en/light_wall"
    ]


def test_one_ink_on_a_plate_that_allows_several_is_dominance():
    dark = [
        rep.Look(**{**look.__dict__, "plate": "dark_room", "ink": "white"})
        for look in _spread("en", 60)
    ]
    report = rep.judge_looks(dark, layer="realized", starvation=False, with_ink=True)
    assert any(f["axis"] == "ink" and f["check"] == "dominance" for f in report["failures"])


def test_mostly_declined_fails():
    report = rep.judge_picker(
        lambda language, title, seed, plate: None if rep.hash_bucket(seed) < 6 else _one(language),
        layer="pool",
        seeds=40,
        plates=("",),
    )
    assert any(f["check"] == "declined_too_often" for f in report["failures"])


def _one(language: str) -> rep.Look:
    return _spread(language, 1)[0]


def test_a_lucky_small_sample_does_not_fail_but_a_collapse_does():
    # 53 of 60 on one option (88 percent): inside the noise of a 73 percent prior.
    assert rep.wilson_lower(53, 60) < rep.cap_for(3, "zone")
    # Every pick the same: out of the noise at the same size.
    assert rep.wilson_lower(60, 60) > rep.cap_for(3)
    assert rep.wilson_lower(0, 0) == 0.0


def test_the_cap_gets_looser_with_fewer_options_but_never_past_the_ceiling():
    caps = [rep.cap_for(k) for k in range(1, 9)]
    assert caps == sorted(caps, reverse=True)
    assert max(caps) <= 0.85 and min(caps) >= 0.5
    assert rep.cap_for(6) == 0.5
    assert (
        rep.cap_for(3, "zone")
        == rep.ZONE_CAP
        < rep.REALIZED_ZONE_CAP
        == rep.cap_for(3, "zone", realized=True)
    )


def test_the_report_runs_on_todays_picker_and_every_control_fails(tmp_path: Path):
    """The real end to end check CI runs: the real picker, the real placement and Skia, EN and TR,
    a light and a dark plate. No exact counts: only verdicts."""
    report = rep.run(out_dir=str(tmp_path))
    assert report["overall"] == "PASS", report["failed"] or report["blind_controls"]
    assert {layer["layer"] for layer in report["layers"]} == {"pool", "realized"}
    assert report["blind_controls"] == []
    assert len(report["controls"]) >= 6 and all(c["caught"] for c in report["controls"])
    saved = json.loads((tmp_path / "report.json").read_text())
    assert saved["overall"] == "PASS" and (tmp_path / "montage.png").stat().st_size > 0
    realized = next(layer for layer in report["layers"] if layer["layer"] == "realized")
    assert {e["stratum"] for e in realized["exemptions"]} == {
        "realized/en/light_wall",
        "realized/tr/light_wall",
    }
