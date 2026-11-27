"""CLI: does the intro-scene picker still vary, or has it collapsed into a few looks? (KRI-1614)

A freer system tends to settle on three looks. This report runs the real picker
(`intro_scene_pick`) over many seeds, several hand-written titles per language (EN and TR) and the
two synthetic footage plates, and FAILS when one option dominates. Offline: no model, no network.

Two layers, because they answer different questions:

* ``pool``: the footage-free pick (`pick_candidates`, the first candidate of a job's stamp) over
  hundreds of seeds. Skia-free. Judges dominance and STARVATION (an option the server issues that
  is never picked).
* ``realized``: the scene the worker actually draws: the stamp fitted with the real
  `place_title_look` and inked on the take (`intro_ink_pass`), on a light and a dark plate. Needs
  Skia. Judges dominance, the top-3 combined looks and the decline rate. It runs few seeds, so it
  cannot judge starvation (a 3 percent option has no sample here); the pool layer does that.

Ways this could go wrong, and the guard for each (tests/pipeline/test_intro_variety.py):

* V1 one option dominates an axis (font pair, entrance, zone, accent length, accent position, ink).
  Rule: with ``k`` options issued in the stratum, the top option's share must stay at or under
  ``cap_for(k)`` = ``min(0.85, max(0.5, 2.5 / k))``: at most 2.5x its fair share, never more than
  half once five or more options exist. The share is judged by its one-sided 95 percent lower
  bound (`wilson_lower`), so a small realized sample's luck cannot fail a healthy picker while a
  picker that ignores its seed still does. The zone axis is designed to favour ``above_head`` (the
  references' usual spot; the pick moves it first 60 percent of the time, so about 73 percent of
  first picks), so its cap is ``ZONE_CAP``, a margin above that prior
  (``REALIZED_ZONE_CAP`` on the realized layer, where the placement also decides: see there).
* V2 the whole look collapses: the three most common (pair, zone, entrance, ink, accent shape)
  looks together cover more than a third of the videos in a stratum (``TOP3_CAP``).
* V3 an issued option is never picked (starvation), judged on the pool layer.
* V4 a forced axis is read as dominance: on the light plate only one ink reads, so ink there is
  not a choice. Which inks read is computed from the plate's contrast (4.5:1, the picker's own
  bar), independently of the picker, and the exemption is listed with that computation.
* V5 the stratum is too small to judge. A gate that judges nothing is blind: it fails.
* V6 the picker falls back to today's title rows too often (that IS a collapse to one look).
* V7 the check itself is blind. Controls that MUST fail: a picker that ignores the seed, a picker
  that never picks one entrance, a picker that mostly declines, and a picker that collapses in
  Turkish only (so a pooled EN+TR number cannot hide it). A control that passes exits 1.
* V8 a language is hidden by pooling. Every axis is judged per language; ink per language and plate.
* V10 a look the picker can pick draws wrong. The montage runs the render measurements and the
  approval check on its looks and lists any failure under ``montage.failed``. That is a finding
  for `make verify-intro-scenes` and the picker's owner, not a variety verdict, so it does not
  gate here.
* V9 a platform wobble fails the gate. Skia measurements differ a little between macOS and Linux,
  so nothing here asserts an exact count; caps leave margin over what today's picker shows.

Run:

    cd src/apps/api && python -m app.cli.intro_variety_report [--out .intro-variety]

Outputs ``report.json`` and ``montage.png`` (the first seeds of one title per language, drawn
through the real renderer and measured: read it). Exit 0 when the picker passes and every
control fails.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The CLI reads no secrets; importing the app config just needs these to exist.
os.environ.setdefault("STORAGE_BUCKET", "intro-variety")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

from app.pipeline import intro_scene as scene_mod  # noqa: E402
from app.pipeline import intro_scene_pick as pick_mod  # noqa: E402

LANGUAGES = ("en", "tr")
# Hand-written fixture titles, never shown on a screen at runtime. Different word counts so the
# accent splits and the faces that can draw them differ.
TITLES: dict[str, tuple[str, ...]] = {
    "en": (
        "the biggest design trend right now",
        "how i stopped overthinking",
        "five habits that changed my mornings",
        "why this matters",
        "what nobody tells you about moving abroad",
    ),
    "tr": (
        "bu yılın en büyük tasarım trendi şu an",
        "düşünmeyi nasıl bıraktım",
        "sabahlarımı değiştiren beş alışkanlık",
        "bu neden önemli",
        "yurt dışına taşınmak hakkında kimsenin söylemediği şey",
    ),
}
PLATES = ("light_wall", "dark_room")  # the synthetic footage of verify_intro_scenes
POOL_SEEDS = 200  # per title, footage-free
REALIZED_SEEDS = 12  # per title and plate, fitted and drawn
MONTAGE_SEEDS = 6  # per language, on the dark plate

MIN_SAMPLE = 30
ZONE_CAP = 0.85
# On the realized layer the engine's own placement also decides the zone: with a centred speaker
# most scenes only sit clear above the head (53 of 60 today in English), and a measurement that
# shifts a little between macOS and Linux moves a few picks there. With the Wilson bound a 60-pick
# stratum fails from 58 above the head; 0.95 failed only at 60 of 60 and let a collapse through
# (`realized_places_everything_above_the_head` is the control that holds this).
REALIZED_ZONE_CAP = 0.9
TOP3_CAP = 1 / 3
DECLINE_CAP = 0.25
MIN_INK_CONTRAST = 4.5  # the picker's own bar (`intro_scene_pick._MIN_INK_CONTRAST`)

AXES = ("font_pair", "entrance", "zone", "accent_len", "accent_pos")
LOOK_KEY = ("font_pair", "zone", "entrance", "ink", "accent_len", "accent_pos")


def cap_for(options: int, axis: str = "", *, realized: bool = False) -> float:
    """The most a single option may take of a stratum with ``options`` issued."""
    if axis == "zone":
        return REALIZED_ZONE_CAP if realized else ZONE_CAP
    return min(0.85, max(0.5, 2.5 / max(options, 1)))


@dataclass(frozen=True)
class Look:
    """What a job's pick realizes: option ids only, as the worker draws them."""

    language: str
    title: str
    plate: str
    seed: str
    font_pair: str
    zone: str
    ink: str | None  # None in the footage-free pool layer: the ink is chosen on the take
    entrance: str
    accent_len: int
    accent_pos: str


# One call per job: ``(language, title, seed, plate)``; ``None`` = today's title rows.
Picker = Callable[[str, str, str, str], "Look | None"]


def accent_shape(voices: Iterable[str]) -> tuple[int, str]:
    flags = [v == "accent" for v in voices]
    length = sum(flags)
    start = flags.index(True) if length else 0
    position = "head" if start == 0 else "tail" if start + length == len(flags) else "middle"
    return length, position


# --- the footage-free pool pick -----------------------------------------------------------


def pool_picker(language: str, title: str, seed: str, plate: str = "") -> Look | None:
    """The first stamped candidate of ``seed`` (the one that wins nearly every render)."""
    candidates = pick_mod.pick_candidates(title, seed=seed)
    if not candidates:
        return None
    first = candidates[0]
    length, position = accent_shape(first["voices"])
    return Look(
        language=language,
        title=title,
        plate=plate,
        seed=seed,
        font_pair=first["font_pair"],
        zone=first["zones"][0],
        ink=None,
        entrance=first["entrance"],
        accent_len=length,
        accent_pos=position,
    )


# --- the realized pick (needs Skia) --------------------------------------------------------


class FitPicker:
    """The realized pick: the job's stamp fitted with the real placement, inked on the plate's
    take frames. The fit does not depend on the plate, so it is cached per (title, seed)."""

    def __init__(self) -> None:
        self._fits: dict[tuple[str, str, str], tuple[Any, Any, Any]] = {}

    def _fit(self, language: str, title: str, seed: str):
        key = (language, title, seed)
        if key not in self._fits:
            from app.cli import verify_intro_scenes as v  # noqa: PLC0415

            ask = {
                "version": pick_mod.INTRO_SCENE_VERSION,
                "seed": seed,
                "inks": pick_mod.ink_order(seed),
                "candidates": pick_mod.pick_candidates(title, seed=seed),
            }
            rows, receipt, info = pick_mod.fit_intro_scene(
                ask,
                title,
                canvas=v.PORTRAIT,
                timeline_duration_s=30.0,
                duration_s=None,
                language=pick_mod.language_of(title, language),
                place=lambda r, position: v._place(r, position),
            )
            self._fits[key] = (ask, rows, info) if rows is not None else (ask, None, info)
        return self._fits[key]

    def __call__(self, language: str, title: str, seed: str, plate: str) -> Look | None:
        from app.cli import verify_intro_scenes as v  # noqa: PLC0415

        ask, rows, info = self._fit(language, title, seed)
        if rows is None:
            return None
        rows = pick_mod.intro_ink_pass(rows, v._take_frames(plate), canvas=v.PORTRAIT)
        chosen = info["chosen"]
        candidate = ask["candidates"][chosen["candidate"]]
        ink = next(
            (n for n, hex_ in scene_mod.INKS.items() if hex_ == str(rows[0]["color"]).upper()),
            None,
        )
        length, position = accent_shape(candidate["voices"])
        return Look(
            language=language,
            title=title,
            plate=plate,
            seed=seed,
            font_pair=chosen["font_pair"],
            zone=chosen["zone"],
            ink=ink,
            entrance=candidate["entrance"],
            accent_len=length,
            accent_pos=position,
        )


# --- what the server issues, computed apart from the picker -------------------------------


def _contrast(hex_a: str, rgb_b: tuple[int, int, int]) -> float:
    from app.cli.verify_intro_scenes import _luminance, _rgb  # noqa: PLC0415

    la, lb = _luminance(_rgb(hex_a)), _luminance(rgb_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def viable_inks(plate: str) -> dict[str, float]:
    """The inks that read (4.5:1) on the plate's wall, with the measured ratio. Computed from the
    plate's pixels and the ink hexes, not from the picker."""
    from app.cli.verify_intro_scenes import PLATES as PLATE_COLOURS  # noqa: PLC0415

    wall = PLATE_COLOURS[plate][0]
    ratios = {name: round(_contrast(hex_, wall), 2) for name, hex_ in scene_mod.INKS.items()}
    return {name: ratio for name, ratio in ratios.items() if ratio >= MIN_INK_CONTRAST}


def issued_options(language: str) -> dict[str, set[Any]]:
    """The options the server issues for this language's fixture titles, unioned."""
    out: dict[str, set[Any]] = {axis: set() for axis in AXES}
    for title in TITLES[language]:
        menu = scene_mod.issue_menu(title)
        out["font_pair"].update(menu.font_pairs)
        out["zone"].update(menu.zones)
        for pair in menu.font_pairs:
            out["entrance"].update(menu.entrances_for(pair))
        for voices in pick_mod.accent_splits(title):
            length, position = accent_shape(voices)
            out["accent_len"].add(length)
            out["accent_pos"].add(position)
    return out


# --- judging -----------------------------------------------------------------------------


def wilson_lower(hits: int, n: int, z: float = 1.645) -> float:
    """The lower end of the one-sided 95 percent interval of a share (Wilson). A dominance check
    fails only when the true share is confidently over the cap, so a small realized sample's luck
    cannot fail a healthy picker while a collapse (every pick the same) still does."""
    if n == 0:
        return 0.0
    p = hits / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return (centre - margin) / (1 + z * z / n)


def _shares(values: Iterable[Any]) -> tuple[Counter, int]:
    counts = Counter(values)
    return counts, sum(counts.values())


def judge_axis(
    looks: list[Look],
    axis: str,
    issued: set[Any],
    *,
    stratum: str,
    starvation: bool,
    realized: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """(summary, failures, exemptions) of one axis in one stratum."""
    counts, n = _shares(getattr(look, axis) for look in looks)
    summary: dict[str, Any] = {
        "stratum": stratum,
        "axis": axis,
        "n": n,
        "issued": sorted(map(str, issued)),
        "counts": {str(k): v for k, v in sorted(counts.items(), key=lambda kv: -kv[1])},
    }
    failures: list[dict[str, Any]] = []
    exemptions: list[dict[str, Any]] = []
    if n < MIN_SAMPLE:
        failures.append(
            {
                "stratum": stratum,
                "axis": axis,
                "check": "sample_too_small",
                "detail": f"{n} picks, need {MIN_SAMPLE} to judge",
            }
        )
        return summary, failures, exemptions
    if len(issued) < 2:
        exemptions.append(
            {
                "stratum": stratum,
                "axis": axis,
                "reason": f"only {sorted(map(str, issued))} is issued here: not a choice",
            }
        )
        return summary, failures, exemptions
    top, top_n = counts.most_common(1)[0]
    share = top_n / n
    bound = wilson_lower(top_n, n)
    cap = cap_for(len(issued), axis, realized=realized)
    summary.update(
        top=str(top), top_share=round(share, 3), top_share_lower=round(bound, 3), cap=round(cap, 3)
    )
    if bound > cap:
        failures.append(
            {
                "stratum": stratum,
                "axis": axis,
                "check": "dominance",
                "detail": f"{top!r} takes {share:.0%} of {n} picks (at least {bound:.0%} at 95 "
                f"percent confidence), cap {cap:.0%} for {len(issued)} issued option(s)",
            }
        )
    if starvation:
        never = sorted(str(o) for o in issued if counts.get(o, 0) == 0)
        if never:
            failures.append(
                {
                    "stratum": stratum,
                    "axis": axis,
                    "check": "starved",
                    "detail": f"issued but never picked in {n} picks: {never}",
                }
            )
    return summary, failures, exemptions


def judge_looks(
    looks: list[Look], *, layer: str, starvation: bool, with_ink: bool
) -> dict[str, Any]:
    """Judge one layer's looks. Non-ink axes per language, counting each (title, seed) once (the
    fit does not depend on the plate, so a second plate would double the sample, not add to it);
    ink per language and plate."""
    summaries: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    exemptions: list[dict[str, Any]] = []
    declined = 0
    for language in LANGUAGES:
        mine = [look for look in looks if look.language == language]
        issued = issued_options(language)
        once = [look for look in mine if look.plate == PLATES[0]] if with_ink else mine
        for axis in AXES:
            s, f, e = judge_axis(
                once,
                axis,
                issued[axis],
                stratum=f"{layer}/{language}",
                starvation=starvation,
                realized=with_ink,
            )
            summaries.append(s)
            failures += f
            exemptions += e
        if not with_ink:
            continue
        for plate in PLATES:
            here = [look for look in mine if look.plate == plate]
            readable = viable_inks(plate)
            s, f, e = judge_axis(
                here, "ink", set(readable), stratum=f"{layer}/{language}/{plate}", starvation=False
            )
            s["contrast"] = readable
            if e:
                e[0]["reason"] += f" (4.5:1 against the wall: {readable})"
            summaries.append(s)
            failures += f
            exemptions += e
            top3, n3 = _shares(tuple(getattr(look, k) for k in LOOK_KEY) for look in here)
            if n3 >= MIN_SAMPLE:
                share = sum(c for _, c in top3.most_common(3)) / n3
                summaries.append(
                    {
                        "stratum": f"{layer}/{language}/{plate}",
                        "axis": "top3_looks",
                        "n": n3,
                        "distinct_looks": len(top3),
                        "top3_share": round(share, 3),
                        "cap": round(TOP3_CAP, 3),
                    }
                )
                if share > TOP3_CAP:
                    failures.append(
                        {
                            "stratum": f"{layer}/{language}/{plate}",
                            "axis": "top3_looks",
                            "check": "collapse",
                            "detail": f"the 3 most common looks cover {share:.0%} of {n3} "
                            f"(cap {TOP3_CAP:.0%}); {len(top3)} distinct",
                        }
                    )
    return {
        "layer": layer,
        "ok": not failures,
        "failures": failures,
        "exemptions": exemptions,
        "summaries": summaries,
        "declined": declined,
    }


def collect(
    picker: Picker, *, seeds: int, plates: Iterable[str], tag: str = "v"
) -> tuple[list[Look], dict[str, tuple[int, int]]]:
    """Run ``picker`` over every language x title x plate x seed. Also returns each stratum's
    (declined, total) so a mostly-declining picker is caught (V6)."""
    looks: list[Look] = []
    declines: dict[str, tuple[int, int]] = {}
    for language in LANGUAGES:
        declined = total = 0
        for title in TITLES[language]:
            for plate in plates:
                for index in range(seeds):
                    total += 1
                    look = picker(language, title, f"{tag}-{index}", plate)
                    if look is None:
                        declined += 1
                    else:
                        looks.append(look)
        declines[language] = (declined, total)
    return looks, declines


def judge_picker(
    picker: Picker, *, layer: str, seeds: int, plates: Iterable[str]
) -> dict[str, Any]:
    pool = layer == "pool"
    looks, declines = collect(picker, seeds=seeds, plates=plates)
    report = judge_looks(looks, layer=layer, starvation=pool, with_ink=not pool)
    for language, (declined, total) in declines.items():
        share = declined / total if total else 1.0
        report["declined"] += declined
        if share > DECLINE_CAP:
            report["failures"].append(
                {
                    "stratum": f"{layer}/{language}",
                    "axis": "decline",
                    "check": "declined_too_often",
                    "detail": f"{declined} of {total} fall back to today's title "
                    f"({share:.0%}, cap {DECLINE_CAP:.0%})",
                }
            )
    report["ok"] = not report["failures"]
    report["decline_rates"] = {
        language: {"declined": d, "total": t} for language, (d, t) in declines.items()
    }
    return report


# --- negative controls ---------------------------------------------------------------------

CONSTANT_SEED = "same-for-every-job"


def _ignores_seed(picker: Picker) -> Picker:
    return lambda language, title, seed, plate: picker(language, title, CONSTANT_SEED, plate)


def _never_picks(picker: Picker, *, entrance: str) -> Picker:
    def wrapped(language: str, title: str, seed: str, plate: str) -> Look | None:
        for attempt in range(50):
            look = picker(language, title, f"{seed}/{attempt}" if attempt else seed, plate)
            if look is None or look.entrance != entrance:
                return look
        return None

    return wrapped


def _mostly_declines(picker: Picker) -> Picker:
    def wrapped(language: str, title: str, seed: str, plate: str) -> Look | None:
        return None if hash_bucket(seed) < 6 else picker(language, title, seed, plate)

    return wrapped


def hash_bucket(seed: str) -> int:
    """A stable 0-9 bucket of a seed (no Python hash randomization)."""
    return sum(seed.encode()) % 10


def _places_everything_above_the_head(picker: Picker) -> Picker:
    """The zone collapse the pool layer cannot see: the picker is fine, the placement is not.
    English only, so the verdict has to stay in its language."""

    def wrapped(language: str, title: str, seed: str, plate: str) -> Look | None:
        look = picker(language, title, seed, plate)
        if look is None or language != "en":
            return look
        return dataclasses.replace(look, zone="above_head")

    return wrapped


def _collapses_in_turkish(picker: Picker) -> Picker:
    def wrapped(language: str, title: str, seed: str, plate: str) -> Look | None:
        return picker(language, title, CONSTANT_SEED if language == "tr" else seed, plate)

    return wrapped


def controls(pool: Picker, realized: Picker | None) -> list[dict[str, Any]]:
    """Each control MUST fail; `expect` names the check that has to catch it."""
    cases: list[tuple[str, str, str, Picker, str, int, tuple[str, ...]]] = [
        ("pool_ignores_seed", "dominance", "", _ignores_seed(pool), "pool", 40, ("",)),
        (
            "pool_never_picks_one_entrance",
            "starved",
            "",
            _never_picks(pool, entrance="pen"),
            "pool",
            POOL_SEEDS,
            ("",),
        ),
        (
            "pool_mostly_declines",
            "declined_too_often",
            "",
            _mostly_declines(pool),
            "pool",
            40,
            ("",),
        ),
        (
            "pool_collapses_in_turkish",
            "dominance",
            "pool/tr",
            _collapses_in_turkish(pool),
            "pool",
            40,
            ("",),
        ),
    ]
    if realized is not None:
        cases += [
            (
                "realized_ignores_seed",
                "collapse",
                "",
                _ignores_seed(realized),
                "realized",
                REALIZED_SEEDS,
                PLATES,
            ),
            (
                "realized_places_everything_above_the_head",
                "dominance:zone",
                "realized/en",
                _places_everything_above_the_head(realized),
                "realized",
                REALIZED_SEEDS,
                PLATES,
            ),
            (
                "realized_collapses_in_turkish",
                "dominance",
                "realized/tr",
                _collapses_in_turkish(realized),
                "realized",
                REALIZED_SEEDS,
                PLATES,
            ),
        ]
    out = []
    for name, expect, stratum, picker, layer, seeds, plates in cases:
        report = judge_picker(picker, layer=layer, seeds=seeds, plates=plates)
        check, _, axis = expect.partition(":")
        hit = [
            f
            for f in report["failures"]
            if f["check"] == check and f["stratum"].startswith(stratum) and axis in ("", f["axis"])
        ]
        other_language = [
            f
            for f in report["failures"]
            if stratum and not f["stratum"].startswith(stratum) and f["check"] != "sample_too_small"
        ]
        out.append(
            {
                "name": name,
                "expect": expect,
                "stratum": stratum or "any",
                "caught": bool(hit),
                # a language-only collapse must not smear into the other language
                "stayed_in_its_language": not other_language,
                "failures": [
                    f"{f['stratum']} {f['axis']} {f['check']}" for f in report["failures"]
                ][:8],
            }
        )
    return out


# --- the montage -------------------------------------------------------------------------


def build_montage(out_dir: str) -> dict[str, Any]:
    """Draw the first seeds of one title per language through the real renderer and measure
    them (the same checks as `make verify-intro-scenes`). Read the picture: it is the content
    check that the variety is real."""
    from app.cli import verify_intro_scenes as v  # noqa: PLC0415

    cases = []
    for language in LANGUAGES:
        title = TITLES[language][0]
        for index in range(MONTAGE_SEEDS):
            cases.append(v._pick_case(title, language, f"v-{index}", "dark_room"))
    v.build_montage(cases, os.path.join(out_dir, "montage.png"))
    return {
        "cases": len(cases),
        "failed": [
            f"{c['id']}/{c['language']}: {'; '.join(c['reasons'])}"
            for c in cases
            if c["verdict"] != "PASS"
        ],
        "looks": [c.get("pick") for c in cases],
    }


# --- run ---------------------------------------------------------------------------------


def run(*, out_dir: str, realized: bool = True) -> dict[str, Any]:
    os.makedirs(out_dir, exist_ok=True)
    fit = FitPicker() if realized else None
    layers = [judge_picker(pool_picker, layer="pool", seeds=POOL_SEEDS, plates=("",))]
    if fit is not None:
        layers.append(judge_picker(fit, layer="realized", seeds=REALIZED_SEEDS, plates=PLATES))
    ctl = controls(pool_picker, fit)
    blind = [c["name"] for c in ctl if not c["caught"] or not c["stayed_in_its_language"]]
    montage = build_montage(out_dir) if realized else None
    failed = [f for layer in layers for f in layer["failures"]]
    overall_ok = not failed and not blind
    report = {
        "overall": "PASS" if overall_ok else "FAIL",
        "thresholds": {
            "cap": "min(0.85, max(0.5, 2.5 / issued_options))",
            "zone_cap": ZONE_CAP,
            "top3_cap": round(TOP3_CAP, 3),
            "decline_cap": DECLINE_CAP,
            "min_sample": MIN_SAMPLE,
            "min_ink_contrast": MIN_INK_CONTRAST,
        },
        "fixtures": {
            "titles": {k: list(v) for k, v in TITLES.items()},
            "plates": list(PLATES),
            "pool_seeds_per_title": POOL_SEEDS,
            "realized_seeds_per_title_plate": REALIZED_SEEDS,
        },
        "layers": layers,
        "failed": [f"{f['stratum']} {f['axis']} {f['check']}: {f['detail']}" for f in failed],
        "controls": ctl,
        "blind_controls": blind,
        "montage": montage,
        "exempt_sound": "the seeded pick never chooses a sound (only the model arm may, "
        "KRI-1613), so it is not an axis",
    }
    Path(out_dir, "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intro_variety_report", description=__doc__)
    parser.add_argument("--out", default=".intro-variety", help="output dir")
    parser.add_argument(
        "--pool-only", action="store_true", help="skip the Skia layer (footage-free pick only)"
    )
    args = parser.parse_args(argv)
    report = run(out_dir=os.path.abspath(args.out), realized=not args.pool_only)
    print(f"\nintro-variety: {report['overall']}")
    for layer in report["layers"]:
        label = "PASS" if layer["ok"] else "FAIL"
        print(f"  [{label}] {layer['layer']}: {len(layer['failures'])} failure(s)")
        for item in layer["exemptions"]:
            print(f"    exempt {item['stratum']} {item['axis']}: {item['reason']}")
    for line in report["failed"]:
        print(f"    FAIL {line}")
    for control in report["controls"]:
        ok = control["caught"] and control["stayed_in_its_language"]
        print(
            f"  [{'CONTROL-FAILED' if ok else 'CONTROL-PASSED'}] {control['name']} "
            f"(expects {control['expect']}): {', '.join(control['failures'][:2])}"
        )
    if report["montage"]:
        print(
            f"  montage: {len(report['montage']['looks'])} looks drawn, "
            f"{len(report['montage']['failed'])} failed render checks"
        )
    print(f"  report:  {os.path.join(args.out, 'report.json')}")
    return 0 if report["overall"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
