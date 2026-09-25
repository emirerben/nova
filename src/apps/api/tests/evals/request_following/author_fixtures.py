"""Generate the hand-authored request-following fixtures.

    cd src/apps/api && python -m tests.evals.request_following.author_fixtures

Four footage sets (food day, trip, sport, vlog) and twelve creator briefs over them, one per
row of the KRI-185 coverage table. Each brief carries:

* the creator's message(s) and the requirements distilled from them, and
* a hand-built `reference` outcome that satisfies every requirement.

These fixtures are NOT recordings of the current system. `Turn.recorded` is left empty, so
replay reports them as "authored, awaiting recordings" and keeps them out of the KPI. What
they do today: prove every checker is satisfiable (the reference must score all `met`) and
discriminating (an untouched attachment-order edit must not), and give P6b/live runs a fixed
target. The prod-capture fixture (`east_run`) is the only measured baseline in this wave.

The output is checked in; regenerate only when a brief or footage set changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .runner import FOOTAGE_DIR, THREAD_DIR

TZ = "+03:00"


def _clip(
    clip_id: str,
    duration_s: float,
    subject: str,
    when: str,
    place: str | None = None,
    route_rank: int | None = None,
) -> dict[str, Any]:
    facts = [{"kind": "capture_time", "value": f"{when}{TZ}", "provenance": "exif"}]
    if place:
        facts.append({"kind": "place", "value": place, "provenance": "geocode"})
    clip: dict[str, Any] = {
        "clip_id": clip_id,
        "duration_s": duration_s,
        "subject": subject,
        "facts": facts,
    }
    if route_rank is not None:
        clip["route_rank"] = route_rank
    return clip


def _footage(footage_id: str, description: str, clips: list[dict[str, Any]], order: list[str]):
    """`order` is the (deliberately non-chronological) attachment order."""
    by_id = {c["clip_id"]: c for c in clips}
    return {
        "footage_id": footage_id,
        "description": description,
        "clips": [by_id[c] for c in order],
    }


FOOD = _footage(
    "food_day",
    "A Sunday eating around Kadıköy, Istanbul. Eight clips attached out of filming order.",
    [
        _clip("F1", 3.0, "simit and tea breakfast", "2026-09-20T09:05:00", "Kadıköy, İstanbul"),
        _clip("F2", 2.5, "Turkish coffee being poured", "2026-09-20T10:40:00", "Kadıköy, İstanbul"),
        _clip("F3", 3.5, "market stall with spices", "2026-09-20T11:30:00", "Kadıköy, İstanbul"),
        _clip(
            "F4", 3.0, "lahmacun coming out of the oven", "2026-09-20T13:15:00", "Kadıköy, İstanbul"
        ),
        _clip("F5", 2.5, "baklava being plated", "2026-09-20T15:20:00", "Kadıköy, İstanbul"),
        _clip("F6", 2.0, "snack on the Kadıköy ferry", "2026-09-20T17:00:00", "Kadıköy, İstanbul"),
        _clip("F7", 3.5, "dinner table full of meze", "2026-09-20T20:10:00", "Moda, İstanbul"),
        _clip("F8", 3.0, "dondurma street vendor", "2026-09-20T21:30:00", "Moda, İstanbul"),
    ],
    ["F4", "F1", "F7", "F2", "F8", "F3", "F6", "F5"],
)

TRIP = _footage(
    "trip",
    "Two days in Cappadocia. Eight clips attached out of filming order; route_rank is the "
    "order the couple travelled.",
    [
        _clip(
            "T1", 4.0, "hot-air balloons at sunrise", "2026-08-14T05:50:00", "Göreme, Nevşehir", 1
        ),
        _clip(
            "T2", 3.0, "cave hotel breakfast terrace", "2026-08-14T08:30:00", "Göreme, Nevşehir", 2
        ),
        _clip(
            "T3", 3.5, "castle rock above the town", "2026-08-14T11:00:00", "Uçhisar, Nevşehir", 3
        ),
        _clip(
            "T4", 3.0, "walk through a rock valley", "2026-08-14T14:00:00", "Göreme, Nevşehir", 4
        ),
        _clip("T5", 3.5, "sunset over red cliffs", "2026-08-14T18:20:00", "Göreme, Nevşehir", 5),
        _clip("T6", 2.5, "pottery workshop", "2026-08-15T10:15:00", "Avanos, Nevşehir", 6),
        _clip(
            "T7", 3.0, "underground city corridor", "2026-08-15T13:00:00", "Kaymaklı, Nevşehir", 7
        ),
        _clip(
            "T8",
            3.0,
            "testi kebab being cracked open",
            "2026-08-15T19:30:00",
            "Göreme, Nevşehir",
            8,
        ),
    ],
    ["T7", "T2", "T5", "T1", "T8", "T4", "T3", "T6"],
)

SPORT = _footage(
    "sport",
    "An evening five-a-side match. Eight clips attached out of filming order.",
    [
        _clip("S1", 2.5, "team warming up", "2026-09-12T19:00:00"),
        _clip("S2", 2.0, "kickoff", "2026-09-12T19:20:00"),
        _clip("S3", 3.0, "first goal", "2026-09-12T19:32:00"),
        _clip("S4", 2.5, "goal celebration", "2026-09-12T19:33:00"),
        _clip("S5", 2.0, "keeper save", "2026-09-12T19:50:00"),
        _clip("S6", 1.5, "missed shot", "2026-09-12T20:05:00"),
        _clip("S7", 3.5, "winning goal", "2026-09-12T20:18:00"),
        _clip("S8", 2.5, "handshakes after the whistle", "2026-09-12T20:30:00"),
    ],
    ["S7", "S2", "S5", "S8", "S1", "S4", "S6", "S3"],
)

VLOG = _footage(
    "vlog",
    "A Saturday reset day. Eight clips attached out of filming order.",
    [
        _clip("V1", 2.0, "waking up", "2026-09-19T08:00:00"),
        _clip("V2", 3.0, "cleaning the flat", "2026-09-19T09:30:00"),
        _clip("V3", 2.0, "laundry", "2026-09-19T10:15:00"),
        _clip("V4", 3.5, "grocery run", "2026-09-19T12:00:00"),
        _clip("V5", 3.0, "gym session", "2026-09-19T16:00:00"),
        _clip("V6", 3.5, "cooking pasta", "2026-09-19T19:00:00"),
        _clip("V7", 2.5, "journaling", "2026-09-19T21:30:00"),
        _clip("V8", 3.0, "night walk", "2026-09-19T22:30:00"),
    ],
    ["V6", "V1", "V8", "V3", "V5", "V2", "V7", "V4"],
)

FOOTAGE = {f["footage_id"]: f for f in (FOOD, TRIP, SPORT, VLOG)}


# ── Plan construction ────────────────────────────────────────────────────────


def _dur(footage_id: str, clip_id: str) -> float:
    return next(c["duration_s"] for c in FOOTAGE[footage_id]["clips"] if c["clip_id"] == clip_id)


def _plan(
    footage_id: str,
    order: list[str],
    *,
    title: str | None = None,
    labels: dict[str, str] | None = None,
    cut_s: float | None = None,
    tail_s: float = 0.0,
) -> dict[str, Any]:
    """Clips laid out back to back. `cut_s` caps each clip; a label spans its clip."""
    labels = labels or {}
    clips: list[dict[str, Any]] = []
    texts: list[dict[str, Any]] = []
    cursor = 0.0
    for index, clip_id in enumerate(order):
        length = min(_dur(footage_id, clip_id), cut_s) if cut_s else _dur(footage_id, clip_id)
        clips.append(
            {"clip_id": clip_id, "start_s": round(cursor, 3), "end_s": round(cursor + length, 3)}
        )
        if clip_id in labels and not any(t["clip_id"] == clip_id for t in texts):
            texts.append(
                {
                    "id": f"label-{index}",
                    "role": "label",
                    "text": labels[clip_id],
                    "start_s": round(cursor, 3),
                    "end_s": round(cursor + length, 3),
                    "font_family": "Inter",
                    "clip_id": clip_id,
                }
            )
        cursor += length
    if title:
        texts.insert(
            0,
            {
                "id": "title",
                "role": "title",
                "text": title,
                "start_s": 0.0,
                "end_s": min(2.0, clips[0]["end_s"]),
                "font_family": "Inter",
            },
        )
    return {"clips": clips, "texts": texts, "total_duration_s": round(cursor + tail_s, 3)}


def _chrono(footage_id: str) -> list[str]:
    clips = FOOTAGE[footage_id]["clips"]
    when = {
        c["clip_id"]: next(f["value"] for f in c["facts"] if f["kind"] == "capture_time")
        for c in clips
    }
    return sorted(when, key=lambda c: when[c])


def _req(
    rid: str,
    request_type: str,
    kind: str,
    checker: str,
    params: dict[str, Any],
    source: str,
    *,
    scope: str = "global",
    turn: int = 0,
) -> dict[str, Any]:
    return {
        "id": rid,
        "kind": kind,
        "scope": scope,
        "request_type": request_type,
        "checker": checker,
        "params": params,
        "source": source,
        "introduced_in_turn": turn,
    }


def _turn(turn_id: str, message: str) -> dict[str, Any]:
    return {"turn_id": turn_id, "user_message": message, "engine": "recorded"}


def _thread(
    fixture_id: str,
    footage_id: str,
    description: str,
    turns: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    reference_plan: dict[str, Any],
    reply: str,
    plan_before: dict[str, Any] | None = None,
) -> dict[str, Any]:
    reference: dict[str, Any] = {"plan_after": reference_plan, "reply": reply}
    if plan_before:
        reference["plan_before"] = plan_before
    return {
        "fixture_id": fixture_id,
        "provenance": "authored",
        "footage": footage_id,
        "description": description,
        "turns": turns,
        "requirements": requirements,
        "reference": reference,
    }


# ── The twelve briefs ────────────────────────────────────────────────────────


def briefs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    # 1. Exact title (non-ASCII literal must survive untouched).
    literal = "Kadıköy'de Pazar"
    msg = f'Make a Sunday food-day video and call it exactly "{literal}".'
    out.append(
        _thread(
            "food_exact_title",
            "food_day",
            "Exact title with Turkish characters: NFC-preserved, never ASCII-folded.",
            [_turn("t0", msg)],
            [
                _req(
                    "title",
                    "title_exact",
                    "text",
                    "title_exact",
                    {"literal": literal},
                    msg,
                    scope="title",
                )
            ],
            _plan("food_day", _chrono("food_day"), title=literal),
            f'Titled it "{literal}" and cut the day in the order you ate it.',
        )
    )

    # 2. Described title + creator-supplied facts.
    msg = (
        "Two days in Cappadocia, cave hotel and balloons. "
        "Give it a title that says where and how long."
    )
    out.append(
        _thread(
            "trip_described_title_facts",
            "trip",
            "Described title that must carry the place and the duration the creator gave.",
            [_turn("t0", msg)],
            [
                _req(
                    "title-place",
                    "title_described",
                    "text",
                    "text_contains",
                    {"terms": ["Cappadocia"], "scope": "title"},
                    msg,
                    scope="title",
                ),
                _req(
                    "title-length",
                    "creator_facts",
                    "text",
                    "text_contains",
                    {"terms": ["2 days"], "scope": "title"},
                    msg,
                    scope="title",
                ),
            ],
            _plan("trip", _chrono("trip"), title="2 Days in Cappadocia"),
            'Titled it "2 Days in Cappadocia".',
        )
    )

    # 3. Exact per-clip labels.
    msg = (
        'Label the goals: "GOAL 1" on the first goal, "GOAL 2" on the winning one, '
        '"FULL TIME" on the handshakes.'
    )
    wanted = {"S3": "GOAL 1", "S7": "GOAL 2", "S8": "FULL TIME"}
    out.append(
        _thread(
            "sport_exact_labels",
            "sport",
            "Three literal per-clip labels, each on a named clip.",
            [_turn("t0", msg)],
            [
                _req(
                    "labels",
                    "label_exact",
                    "text",
                    "label_exact",
                    {"labels": wanted},
                    msg,
                    scope="per_clip",
                )
            ],
            _plan("sport", _chrono("sport"), labels=wanted),
            "Added GOAL 1, GOAL 2 and FULL TIME on those three clips.",
        )
    )

    # 4. Described per-clip labels naming the dish.
    msg = "Add a caption to every clip naming the dish or drink."
    names = {
        "F1": "Simit & tea",
        "F2": "Turkish coffee",
        "F3": "Spice market",
        "F4": "Fresh lahmacun",
        "F5": "Baklava",
        "F6": "Ferry snack",
        "F7": "Meze spread",
        "F8": "Dondurma",
    }
    out.append(
        _thread(
            "food_described_labels",
            "food_day",
            "Every clip captioned with what is on screen; captions must name the food.",
            [_turn("t0", msg)],
            [
                _req(
                    "labeled",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 1.0},
                    msg,
                    scope="per_clip",
                ),
                _req(
                    "names-the-dish",
                    "label_described",
                    "text",
                    "label_mentions",
                    {
                        "clips": {
                            "F1": ["simit", "tea"],
                            "F2": ["coffee"],
                            "F3": ["spice"],
                            "F4": ["lahmacun"],
                            "F5": ["baklava"],
                            "F6": ["ferry"],
                            "F7": ["meze"],
                            "F8": ["dondurma", "ice cream"],
                        }
                    },
                    msg,
                    scope="per_clip",
                ),
            ],
            _plan("food_day", _chrono("food_day"), labels=names),
            "Captioned all 8 clips with what is on screen.",
        )
    )

    # 5. Creator-supplied facts placed anywhere in the text.
    msg = "I'm Ece and this is day 3 of my Saturday reset. Put that in the edit."
    out.append(
        _thread(
            "vlog_creator_facts",
            "vlog",
            "Name and day count the creator supplied must appear on screen.",
            [_turn("t0", msg)],
            [
                _req(
                    "facts",
                    "creator_facts",
                    "text",
                    "text_contains",
                    {"terms": ["Ece", "day 3"], "scope": "any"},
                    msg,
                )
            ],
            _plan("vlog", _chrono("vlog"), title="Ece · Day 3 of my reset"),
            'Added "Ece · Day 3 of my reset" as the title.',
        )
    )

    # 6. Chronological order from capture time.
    msg = "Show everything in the order I filmed it."
    out.append(
        _thread(
            "trip_chronological",
            "trip",
            "Attachment order is scrambled; capture_time facts define the order filmed.",
            [_turn("t0", msg)],
            [
                _req(
                    "filmed-order",
                    "order_chronological",
                    "order",
                    "order_by_key",
                    {"key": "capture_time"},
                    msg,
                )
            ],
            _plan("trip", _chrono("trip"), title="Cappadocia"),
            "Ordered all 8 clips by when they were filmed.",
        )
    )

    # 7. Explicit order.
    msg = "Open on the first goal, then the celebration, and end on the handshakes."
    order = ["S3", "S4", "S1", "S2", "S5", "S6", "S7", "S8"]
    out.append(
        _thread(
            "sport_explicit_order",
            "sport",
            "Three named clips must appear in the stated sequence.",
            [_turn("t0", msg)],
            [
                _req(
                    "sequence",
                    "order_explicit",
                    "order",
                    "order_explicit",
                    {"sequence": ["S3", "S4", "S8"]},
                    msg,
                )
            ],
            _plan("sport", order, title="Five-a-side"),
            "Opened on the first goal, then the celebration, and closed on the handshakes.",
        )
    )

    # 8. Route order (P3 treats by_route as capture-time order for now).
    msg = "Follow our route, from the balloons in Göreme down to the underground city in Kaymaklı."
    out.append(
        _thread(
            "trip_route_order",
            "trip",
            "Clips follow the route travelled (route_rank), not attachment order.",
            [_turn("t0", msg)],
            [_req("route", "order_route", "order", "order_by_key", {"key": "route_rank"}, msg)],
            _plan(
                "trip", ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"], title="Cappadocia route"
            ),
            "Cut the clips along your route, balloons first and the underground city near the end.",
        )
    )

    # 9. Selection: exclude one clip, require another.
    msg = "Skip the laundry, but the gym has to be in."
    out.append(
        _thread(
            "vlog_selection",
            "vlog",
            "One clip excluded by name, one required by name.",
            [_turn("t0", msg)],
            [
                _req(
                    "skip-laundry",
                    "selection",
                    "select",
                    "select_exclude",
                    {"clip_ids": ["V3"]},
                    msg,
                ),
                _req(
                    "keep-gym", "selection", "select", "select_include", {"clip_ids": ["V5"]}, msg
                ),
            ],
            _plan("vlog", [c for c in _chrono("vlog") if c != "V3"], title="Saturday reset"),
            "Left out the laundry and kept the gym.",
        )
    )

    # 10. Duration + pacing (a montage may cut a clip twice to reach a length at fast pace).
    msg = "20 seconds, fast cuts."
    ten = ["S1", "S2", "S3", "S4", "S5", "S6", "S7", "S3", "S7", "S8"]
    ref = _plan("sport", ten, title="Five-a-side", cut_s=2.0)
    out.append(
        _thread(
            "sport_duration_pacing",
            "sport",
            "Target length with tight tolerance and a fast average cut.",
            [_turn("t0", msg)],
            [
                _req(
                    "twenty-seconds",
                    "duration",
                    "timing",
                    "duration_within",
                    {"target_s": 20.0, "tol_frac": 0.1},
                    msg,
                ),
                _req(
                    "fast-cuts",
                    "pacing",
                    "timing",
                    "pacing_max_avg_clip",
                    {"max_avg_clip_s": 2.0},
                    msg,
                ),
            ],
            ref,
            "Cut it to 20 seconds with 2-second shots.",
        )
    )

    # 11. Readability: every caption on screen long enough to read.
    msg = "Caption every clip, but keep each caption up long enough to read."
    long_names = {
        "F1": "Simit and tea to start",
        "F2": "Turkish coffee",
        "F3": "The spice market",
        "F4": "Fresh lahmacun",
        "F5": "Baklava",
        "F6": "Snack on the ferry",
        "F7": "Dinner, meze everywhere",
        "F8": "Dondurma to finish",
    }
    out.append(
        _thread(
            "food_readability",
            "food_day",
            "Captions must stay on screen for the reading-time rule (0.8s + 0.06s/char, 1.2-3.0s).",
            [_turn("t0", msg)],
            [
                _req(
                    "captioned",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 1.0},
                    msg,
                    scope="per_clip",
                ),
                _req("readable", "readability", "timing", "readability", {}, msg, scope="per_clip"),
            ],
            _plan("food_day", _chrono("food_day"), labels=long_names),
            "Captioned every clip, each held for its full shot.",
        )
    )

    # 12. Multi-turn: restructure after a render.
    m0 = "Make a Saturday reset vlog. Make sure the cooking and the night walk are in."
    m1 = "Shorter please, 15 seconds. Open with the gym, then the cooking."
    before = _plan("vlog", _chrono("vlog"), title="Saturday reset")
    after = _plan("vlog", ["V5", "V6", "V8", "V1", "V2"], title="Saturday reset", cut_s=3.0)
    out.append(
        _thread(
            "vlog_restructure",
            "vlog",
            "Two turns: a first cut, then a restructure of the rendered edit (shorter, reordered).",
            [_turn("t0", m0), _turn("t1", m1)],
            [
                _req(
                    "cooking",
                    "selection",
                    "select",
                    "select_include",
                    {"clip_ids": ["V6", "V8"]},
                    m0,
                ),
                _req("redo", "restructure", "select", "restructure_changed", {}, m1, turn=1),
                _req(
                    "fifteen",
                    "duration",
                    "timing",
                    "duration_within",
                    {"target_s": 15.0, "tol_frac": 0.1},
                    m1,
                    turn=1,
                ),
                _req(
                    "gym-then-cooking",
                    "order_explicit",
                    "order",
                    "order_explicit",
                    {"sequence": ["V5", "V6"]},
                    m1,
                    turn=1,
                ),
            ],
            after,
            "Cut it down to 15 seconds and opened with the gym, then the cooking.",
            plan_before=before,
        )
    )
    return out


def main() -> None:
    FOOTAGE_DIR.mkdir(parents=True, exist_ok=True)
    THREAD_DIR.mkdir(parents=True, exist_ok=True)
    for footage_id, footage in FOOTAGE.items():
        _write(FOOTAGE_DIR / f"{footage_id}.json", footage)
    for thread in briefs():
        _write(THREAD_DIR / f"{thread['fixture_id']}.json", thread)
    print(f"wrote {len(FOOTAGE)} footage sets and {len(briefs())} authored threads")


def _write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
