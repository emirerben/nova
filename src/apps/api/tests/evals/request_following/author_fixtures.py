"""Generate the hand-authored request-following fixtures.

    cd src/apps/api && python -m tests.evals.request_following.author_fixtures

Five synthetic footage sets (food day, trip, sport, vlog, harbor run) and 29 creator briefs
over them: twelve wave-1 briefs (one per row of the KRI-185 coverage table) plus the P6b
briefs for the receipt, dedupe, per-clip label, correction and bulk-font behaviours. The
prod capture `east_run` is the 30th thread and is not authored here. Each brief carries:

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
    landmark: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """`landmark` is (inferred guess, ground truth). The guess is what a landmark_guess run
    produced for the clip (synthetic here); the truth is only ever read by the wrong-landmark
    metric, never shown to a planner."""
    facts = [{"kind": "capture_time", "value": f"{when}{TZ}", "provenance": "exif"}]
    if place:
        facts.append({"kind": "place", "value": place, "provenance": "geocode"})
    if landmark:
        facts.append(
            {"kind": "landmark", "value": landmark[0], "provenance": "inferred", "confidence": 0.6}
        )
    clip: dict[str, Any] = {
        "clip_id": clip_id,
        "duration_s": duration_s,
        "subject": subject,
        "facts": facts,
    }
    if route_rank is not None:
        clip["route_rank"] = route_rank
    if landmark:
        clip["true_landmark"] = landmark[1]
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
            "T3",
            3.5,
            "castle rock above the town",
            "2026-08-14T11:00:00",
            "Uçhisar, Nevşehir",
            3,
            ("Uçhisar Castle", "Uçhisar Castle"),
        ),
        _clip(
            "T4",
            3.0,
            "walk through a rock valley",
            "2026-08-14T14:00:00",
            "Göreme, Nevşehir",
            4,
            ("Love Valley", "Love Valley"),
        ),
        _clip(
            "T5",
            3.5,
            "sunset over red cliffs",
            "2026-08-14T18:20:00",
            "Göreme, Nevşehir",
            5,
            ("Red Valley", "Rose Valley"),
        ),
        _clip("T6", 2.5, "pottery workshop", "2026-08-15T10:15:00", "Avanos, Nevşehir", 6),
        _clip(
            "T7",
            3.0,
            "underground city corridor",
            "2026-08-15T13:00:00",
            "Kaymaklı, Nevşehir",
            7,
            ("Kaymaklı Underground City", "Kaymaklı Underground City"),
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

# An invented coastal run (no real place names). Filmed Old Mill Pier -> Kestrel Point; the
# creator's stated route runs the other way, so `route_rank` (their route) is the reverse of
# filming order. Inferred landmark guesses carry ground truth: one of seven is wrong.
HARBOR = _footage(
    "harbor_run",
    "A 14k coastal run around the invented town of Brightwater. Eight clips attached out of "
    "filming order; route_rank follows the route the creator STATES (Kestrel Point to Old Mill "
    "Pier), which is the reverse of the order they filmed in.",
    [
        _clip(
            "H1",
            3.0,
            "wooden pier at dawn",
            "2026-09-13T06:30:00",
            "Old Mill, Brightwater",
            8,
            ("Old Mill Pier", "Old Mill Pier"),
        ),
        _clip(
            "H2",
            2.5,
            "fish stalls opening",
            "2026-09-13T06:48:00",
            "Old Mill, Brightwater",
            7,
            ("Saltwood Fish Market", "Saltwood Fish Market"),
        ),
        _clip(
            "H3",
            3.5,
            "lighthouse on the breakwater",
            "2026-09-13T07:05:00",
            "Tern Bay, Brightwater",
            6,
            ("Tern Lighthouse", "Tern Lighthouse"),
        ),
        _clip(
            "H4",
            2.5,
            "runner passing the lighthouse base",
            "2026-09-13T07:12:00",
            "Tern Bay, Brightwater",
            5,
            ("Tern Lighthouse", "Tern Lighthouse"),
        ),
        _clip(
            "H5",
            3.0,
            "stone fort on the hill",
            "2026-09-13T07:40:00",
            "Halden Hill, Brightwater",
            4,
            ("Halden Watchtower", "Fort Halden"),
        ),
        _clip("H6", 2.0, "cliff path", "2026-09-13T08:05:00", "Halden Hill, Brightwater", 3),
        _clip(
            "H7",
            3.0,
            "white beach cove",
            "2026-09-13T08:30:00",
            "Kestrel Cove, Brightwater",
            2,
            ("Kestrel Beach", "Kestrel Beach"),
        ),
        _clip(
            "H8",
            3.5,
            "finish arch at the point",
            "2026-09-13T08:52:00",
            "Kestrel Point, Brightwater",
            1,
            ("Kestrel Point Finish", "Kestrel Point"),
        ),
    ],
    ["H5", "H1", "H8", "H3", "H6", "H2", "H7", "H4"],
)

FOOTAGE = {f["footage_id"]: f for f in (FOOD, TRIP, SPORT, VLOG, HARBOR)}


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
    font: str = "Inter",
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
                    "font_family": font,
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
                "font_family": font,
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
    return wave1_briefs() + p6b_briefs()


def wave1_briefs() -> list[dict[str, Any]]:
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


# ── P6b briefs: receipts, dedupe, per-clip labels, corrections, bulk fonts ───

HARBOR_ROUTE = "I ran from Kestrel Point to Old Mill Pier this morning. Show the run in that order."
REVERSED_REPLY = (
    "Your clips were filmed starting at Old Mill Pier and ending at Kestrel Point, the reverse "
    "of the route you gave (Kestrel Point → Old Mill Pier). I kept filming order; tell me if "
    "you want your route order instead."
)


def _labels_by_facts(footage_id: str, kind: str) -> dict[str, str]:
    """clip_id -> fact value for clips that have one, in filming order, dropping a label
    that repeats the previous kept one (the KRI-210 dedupe)."""
    by_id = {c["clip_id"]: c for c in FOOTAGE[footage_id]["clips"]}
    out: dict[str, str] = {}
    previous = None
    for clip_id in _chrono(footage_id):
        fact = next((f for f in by_id[clip_id]["facts"] if f["kind"] == kind), None)
        if fact is None or fact["value"].casefold() == previous:
            continue
        previous = fact["value"].casefold()
        out[clip_id] = fact["value"]
    return out


def p6b_briefs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    harbor = _chrono("harbor_run")

    # 13. Reversed route: the AI keeps filming order and SAYS so (KRI-208, no silent override).
    out.append(
        _thread(
            "harbor_route_reversed",
            "harbor_run",
            "The stated route contradicts filming order: keep filming order, name both "
            "endpoints, ask which to follow.",
            [_turn("t0", HARBOR_ROUTE)],
            [
                _req(
                    "keeps-filming-order",
                    "order_route",
                    "order",
                    "order_by_key",
                    {"key": "capture_time"},
                    HARBOR_ROUTE,
                ),
                _req(
                    "receipt-names-the-reversal",
                    "order_route",
                    "order",
                    "reply_states",
                    {
                        "all_of": [
                            "filmed starting at Old Mill Pier",
                            "reverse",
                            "tell me if you want your route order",
                        ]
                    },
                    HARBOR_ROUTE,
                ),
            ],
            _plan("harbor_run", harbor, title="Brightwater run"),
            REVERSED_REPLY,
        )
    )

    # 14. Reversed route, creator answers "follow my route" (multi-turn).
    m1 = "Yes, use my route order."
    out.append(
        _thread(
            "harbor_route_reversed_then_route_order",
            "harbor_run",
            "After the reversal receipt the creator picks their own route: the edit is "
            "re-ordered along route_rank.",
            [_turn("t0", HARBOR_ROUTE), _turn("t1", m1)],
            [
                _req(
                    "route-order",
                    "order_route",
                    "order",
                    "order_by_key",
                    {"key": "route_rank"},
                    m1,
                    turn=1,
                ),
                _req("reordered", "restructure", "select", "restructure_changed", {}, m1, turn=1),
                _req(
                    "says-route-order",
                    "order_route",
                    "order",
                    "reply_states",
                    {"all_of": ["route order"]},
                    m1,
                    turn=1,
                ),
            ],
            _plan("harbor_run", list(reversed(harbor)), title="Brightwater run"),
            "Re-ordered all 8 clips in your route order, Kestrel Point first.",
            plan_before=_plan("harbor_run", harbor, title="Brightwater run"),
        )
    )

    # 15. Control: route agrees with filming order, so nothing may claim a reversal.
    msg = "I ran from Old Mill Pier out to Kestrel Point. Show the run in that order."
    out.append(
        _thread(
            "harbor_route_matches_filming",
            "harbor_run",
            "Control: stated route equals filming order; no reversal may be claimed.",
            [_turn("t0", msg)],
            [
                _req(
                    "filming-order",
                    "order_route",
                    "order",
                    "order_by_key",
                    {"key": "capture_time"},
                    msg,
                ),
                _req(
                    "no-false-reversal",
                    "order_route",
                    "order",
                    "reply_states",
                    {"none_of": ["reverse", "opposite", "contradict"]},
                    msg,
                ),
            ],
            _plan("harbor_run", harbor, title="Brightwater run"),
            "Cut the run from Old Mill Pier to Kestrel Point, the order you filmed it.",
        )
    )

    # 16. Consecutive labels dedupe (KRI-210): a repeat is dropped and the reply says so.
    msg = "Label every clip with the landmark it shows."
    labels = _labels_by_facts("harbor_run", "landmark")
    out.append(
        _thread(
            "harbor_label_dedupe",
            "harbor_run",
            "Two neighbouring clips show the same lighthouse: the repeat is dropped, an "
            "ungrounded clip stays unlabelled, and the reply counts both.",
            [_turn("t0", msg)],
            [
                _req(
                    "labelled",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 0.75},
                    msg,
                    scope="per_clip",
                ),
                _req(
                    "no-repeat",
                    "label_described",
                    "text",
                    "label_no_consecutive_repeat",
                    {},
                    msg,
                    scope="per_clip",
                ),
                _req(
                    "says-what-it-left-off",
                    "label_described",
                    "text",
                    "reply_states",
                    {"all_of": ["repeated the label", "left (it|them) off"]},
                    msg,
                ),
            ],
            _plan("harbor_run", harbor, labels=labels),
            "Labelled 6 of 8 clips. One repeated the label before it and one had no landmark "
            "I could confirm, so I left them off.",
        )
    )

    # 17. Title written from the brief (KRI-210b): the title carries the brief's facts and
    # the receipt says where it came from.
    msg = (
        "Make a video of my 14k harbour run from Kestrel Point to Old Mill Pier "
        "and give it a title."
    )
    title = "14k harbour run: Kestrel Point to Old Mill Pier"
    out.append(
        _thread(
            "harbor_title_from_brief",
            "harbor_run",
            "No exact title asked for: a title written from the brief counts, and the reply "
            "says it came from the creator's own words.",
            [_turn("t0", msg)],
            [
                _req(
                    "title-has-the-run",
                    "title_described",
                    "text",
                    "text_contains",
                    {"terms": ["14k"], "scope": "title"},
                    msg,
                    scope="title",
                ),
                _req(
                    "title-has-the-route",
                    "creator_facts",
                    "text",
                    "text_contains",
                    {"terms": ["Kestrel Point", "Old Mill Pier"], "scope": "title"},
                    msg,
                    scope="title",
                ),
                _req(
                    "receipt-names-the-source",
                    "title_described",
                    "text",
                    "reply_states",
                    {"all_of": ["from (what you|your)"]},
                    msg,
                    scope="title",
                ),
            ],
            _plan("harbor_run", harbor, title=title),
            f'Titled it "{title}" from what you told me.',
        )
    )

    # 18. A landmark the AI guessed wrong, corrected in chat (one label only).
    m0 = "Label every clip with the landmark it shows."
    m1 = "Clip 5 isn't Halden Watchtower, it's Fort Halden."
    before = _plan("harbor_run", harbor, labels=labels)
    fixed = {**labels, "H5": "Fort Halden"}
    out.append(
        _thread(
            "harbor_landmark_correction",
            "harbor_run",
            "One guessed landmark is wrong; the creator corrects clip 5 and only that label "
            "may change.",
            [_turn("t0", m0), _turn("t1", m1)],
            [
                _req(
                    "labelled",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 0.75},
                    m0,
                    scope="per_clip",
                ),
                _req(
                    "fix-clip-5",
                    "label_exact",
                    "text",
                    "label_single_change",
                    {"clip_id": "H5", "text": "Fort Halden"},
                    m1,
                    scope="clip:H5",
                    turn=1,
                ),
                _req(
                    "reply-confirms",
                    "label_exact",
                    "text",
                    "reply_states",
                    {"all_of": ["Fort Halden"]},
                    m1,
                    scope="clip:H5",
                    turn=1,
                ),
            ],
            _plan("harbor_run", harbor, labels=fixed),
            "Changed clip 5's label to Fort Halden. The other labels are as they were.",
            plan_before=before,
        )
    )

    # 19. Label each clip from facts on a rendered edit (L2 label_each_clip), trip footage.
    m0 = "Two days in Cappadocia. Show it in the order I filmed it."
    m1 = "Label each clip with where it was filmed."
    place_labels = {cid: v.split(",")[0] for cid, v in _labels_by_facts("trip", "place").items()}
    out.append(
        _thread(
            "trip_label_each_clip_from_facts",
            "trip",
            "Two turns: filming order, then per-clip place labels from the clips' own facts; "
            "back-to-back repeats are dropped and reported.",
            [_turn("t0", m0), _turn("t1", m1)],
            [
                _req(
                    "filmed-order",
                    "order_chronological",
                    "order",
                    "order_by_key",
                    {"key": "capture_time"},
                    m0,
                ),
                _req(
                    "labelled",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 0.75},
                    m1,
                    scope="per_clip",
                    turn=1,
                ),
                _req(
                    "names-the-place",
                    "label_described",
                    "text",
                    "label_mentions",
                    {"clips": {c: [v] for c, v in place_labels.items()}},
                    m1,
                    scope="per_clip",
                    turn=1,
                ),
                _req(
                    "no-repeat",
                    "label_described",
                    "text",
                    "label_no_consecutive_repeat",
                    {},
                    m1,
                    scope="per_clip",
                    turn=1,
                ),
                _req(
                    "says-what-it-left-off",
                    "label_described",
                    "text",
                    "reply_states",
                    {"all_of": ["repeated the label", "left (it|them) off"]},
                    m1,
                    turn=1,
                ),
            ],
            _plan("trip", _chrono("trip"), title="Cappadocia", labels=place_labels),
            "Labelled 6 of 8 clips with where they were filmed. Two repeated the label before "
            "them, so I left them off.",
            plan_before=_plan("trip", _chrono("trip"), title="Cappadocia"),
        )
    )

    # 20/21. Label language follows the creator's language and is never mixed.
    msg = "Her klibe gördüğü yerin adını yaz."
    tr_labels = {
        "T3": "Uçhisar Kalesi",
        "T4": "Aşk Vadisi",
        "T5": "Gül Vadisi",
        "T7": "Kaymaklı Yeraltı Şehri",
    }
    out.append(
        _thread(
            "trip_landmark_labels_turkish",
            "trip",
            "Turkish brief: landmark names come back in Turkish, with no English words mixed in.",
            [_turn("t0", msg)],
            [
                _req(
                    "turkish-names",
                    "label_described",
                    "text",
                    "label_mentions",
                    {"clips": {c: [v] for c, v in tr_labels.items()}},
                    msg,
                    scope="per_clip",
                ),
                _req(
                    "no-english-mix",
                    "label_described",
                    "text",
                    "text_avoids",
                    {"terms": ["Castle", "Valley", "Underground"], "scope": "labels"},
                    msg,
                    scope="per_clip",
                ),
            ],
            _plan("trip", _chrono("trip"), labels=tr_labels),
            "Her klibe yerini yazdım.",
        )
    )
    msg = "Label the landmarks in each clip."
    en_labels = {
        "T3": "Uçhisar Castle",
        "T4": "Love Valley",
        "T5": "Rose Valley",
        "T7": "Kaymaklı Underground City",
    }
    out.append(
        _thread(
            "trip_landmark_labels_english",
            "trip",
            "English brief: names come back in English, no Turkish generic words mixed in.",
            [_turn("t0", msg)],
            [
                _req(
                    "english-names",
                    "label_described",
                    "text",
                    "label_mentions",
                    {"clips": {c: [v] for c, v in en_labels.items()}},
                    msg,
                    scope="per_clip",
                ),
                _req(
                    "no-turkish-mix",
                    "label_described",
                    "text",
                    "text_avoids",
                    {"terms": ["Kalesi", "Vadisi", "Yeraltı"], "scope": "labels"},
                    msg,
                    scope="per_clip",
                ),
            ],
            _plan("trip", _chrono("trip"), labels=en_labels),
            "Labelled the four landmarks.",
        )
    )

    # 22. No location facts: never invent a label, say so.
    msg = "Label each clip with where it was filmed."
    out.append(
        _thread(
            "sport_labels_without_place_facts",
            "sport",
            "These clips carry no place or landmark: inventing labels would be a fabrication.",
            [_turn("t0", msg)],
            [
                _req(
                    "no-invented-labels",
                    "label_described",
                    "text",
                    "labels_none",
                    {},
                    msg,
                    scope="per_clip",
                ),
                _req(
                    "says-why",
                    "label_described",
                    "text",
                    "reply_states",
                    {"all_of": ["couldn'?t", "where"]},
                    msg,
                ),
            ],
            _plan("sport", _chrono("sport"), title="Five-a-side"),
            "I couldn't tell where these were filmed (the clips carry no location), so I "
            "didn't add place labels.",
        )
    )

    # 23. A default title must be disclosed as a default.
    msg = "Give it a title."
    out.append(
        _thread(
            "vlog_default_title_disclosed",
            "vlog",
            "Nothing to title from: the reply must not pass a placeholder off as the creator's.",
            [_turn("t0", msg)],
            [
                _req(
                    "receipt-names-the-source",
                    "title_described",
                    "text",
                    "reply_states",
                    {
                        "all_of": ["default title"],
                        "none_of": ["from (what you|your)"],
                    },
                    msg,
                    scope="title",
                )
            ],
            _plan("vlog", _chrono("vlog"), title="My day"),
            "I used a plain default title because none was given.",
        )
    )

    # 24. Bulk font change is ONE edit that reaches every text lane and re-plans nothing.
    m0 = "Make a Saturday reset vlog with a caption on every clip."
    m1 = "Change all fonts to Montserrat."
    vlog_labels = {c["clip_id"]: c["subject"].capitalize() for c in FOOTAGE["vlog"]["clips"]}
    base = _plan("vlog", _chrono("vlog"), title="Saturday reset", labels=vlog_labels)
    out.append(
        _thread(
            "vlog_change_all_fonts",
            "vlog",
            '"Change all fonts" on a rendered edit: every one of the 9 text lanes, in one '
            "edit, without re-planning the clips.",
            [_turn("t0", m0), _turn("t1", m1)],
            [
                _req(
                    "captioned",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 1.0},
                    m0,
                    scope="per_clip",
                ),
                _req(
                    "all-montserrat",
                    "style",
                    "style",
                    "font_all_equal",
                    {"font": "Montserrat"},
                    m1,
                    turn=1,
                ),
                _req("no-replan", "style", "style", "clips_unchanged", {}, m1, turn=1),
                _req(
                    "reply-not-a-replan",
                    "style",
                    "style",
                    "reply_states",
                    {"all_of": ["Montserrat"], "none_of": ["re-?plan", "regenerat"]},
                    m1,
                    turn=1,
                ),
            ],
            _plan(
                "vlog",
                _chrono("vlog"),
                title="Saturday reset",
                labels=vlog_labels,
                font="Montserrat",
            ),
            "Set all 9 text elements to Montserrat.",
            plan_before=base,
        )
    )

    # 25. Fonts changed for everything, then a standing ban (3 turns, state persists).
    m0 = "Caption every clip with what it is."
    m1 = "Change all fonts to Playfair Display."
    m2 = "And never use Inter again."
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
            "food_fonts_then_forbid",
            "food_day",
            "Three turns: captions, a bulk font change, then a standing font ban that the "
            "final edit must still honour.",
            [_turn("t0", m0), _turn("t1", m1), _turn("t2", m2)],
            [
                _req(
                    "captioned",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 1.0},
                    m0,
                    scope="per_clip",
                ),
                _req(
                    "all-playfair",
                    "style",
                    "style",
                    "font_all_equal",
                    {"font": "Playfair Display"},
                    m1,
                    turn=1,
                ),
                _req(
                    "no-inter", "style", "style", "font_forbidden", {"fonts": ["Inter"]}, m2, turn=2
                ),
            ],
            _plan("food_day", _chrono("food_day"), labels=names, font="Playfair Display"),
            "Done, and Inter is off the list.",
        )
    )

    # 26. Restyle only the titles; labels keep their font.
    m0 = (
        'Label the goals: "GOAL 1" on the first goal and "FULL TIME" on the handshakes, '
        "and add a title."
    )
    m1 = "Set the title in Playfair Display but leave the goal labels as they are."
    wanted = {"S3": "GOAL 1", "S8": "FULL TIME"}
    base = _plan("sport", _chrono("sport"), title="Five-a-side", labels=wanted)
    after = _plan("sport", _chrono("sport"), title="Five-a-side", labels=wanted)
    after["texts"][0]["font_family"] = "Playfair Display"
    out.append(
        _thread(
            "sport_title_font_only",
            "sport",
            "A scoped style change: the title moves, the labels and the clips do not.",
            [_turn("t0", m0), _turn("t1", m1)],
            [
                _req(
                    "labels",
                    "label_exact",
                    "text",
                    "label_exact",
                    {"labels": wanted},
                    m0,
                    scope="per_clip",
                ),
                _req(
                    "title-font",
                    "style",
                    "style",
                    "font_all_equal",
                    {"font": "Playfair Display", "roles": ["title"]},
                    m1,
                    scope="title",
                    turn=1,
                ),
                _req(
                    "labels-keep-font",
                    "style",
                    "style",
                    "font_all_equal",
                    {"font": "Inter", "roles": ["label"]},
                    m1,
                    scope="per_clip",
                    turn=1,
                ),
                _req("no-replan", "style", "style", "clips_unchanged", {}, m1, turn=1),
            ],
            after,
            "Set the title in Playfair Display. The labels are unchanged.",
            plan_before=base,
        )
    )

    # 27. Exclusion carries into a later duration request.
    m0 = "Cut the missed shot."
    m1 = "Make it 12 seconds."
    out.append(
        _thread(
            "sport_exclude_then_duration",
            "sport",
            "A standing exclusion must survive a later length request.",
            [_turn("t0", m0), _turn("t1", m1)],
            [
                _req(
                    "no-missed-shot",
                    "selection",
                    "select",
                    "select_exclude",
                    {"clip_ids": ["S6"]},
                    m0,
                ),
                _req(
                    "twelve-seconds",
                    "duration",
                    "timing",
                    "duration_within",
                    {"target_s": 12.0, "tol_frac": 0.1},
                    m1,
                    turn=1,
                ),
            ],
            _plan(
                "sport",
                [c for c in _chrono("sport") if c != "S6"],
                title="Five-a-side",
                cut_s=1.7,
            ),
            "Cut it to 12 seconds, still without the missed shot.",
        )
    )

    # 28. Exact title survives a later labelling turn.
    m0 = 'Call it exactly "Ece\'nin Cumartesisi".'
    m1 = "Now caption every clip with what I'm doing."
    verbs = {
        "V1": "Waking up",
        "V2": "Cleaning the flat",
        "V3": "Laundry",
        "V4": "Grocery run",
        "V5": "Gym session",
        "V6": "Cooking pasta",
        "V7": "Journaling",
        "V8": "Night walk",
    }
    out.append(
        _thread(
            "vlog_exact_title_then_labels",
            "vlog",
            "The exact title from turn one must still be there after a caption edit.",
            [_turn("t0", m0), _turn("t1", m1)],
            [
                _req(
                    "title",
                    "title_exact",
                    "text",
                    "title_exact",
                    {"literal": "Ece'nin Cumartesisi"},
                    m0,
                    scope="title",
                ),
                _req(
                    "captioned",
                    "label_described",
                    "text",
                    "label_coverage",
                    {"min_frac": 1.0},
                    m1,
                    scope="per_clip",
                    turn=1,
                ),
                _req(
                    "names-the-activity",
                    "label_described",
                    "text",
                    "label_mentions",
                    {
                        "clips": {
                            "V1": ["waking"],
                            "V2": ["clean"],
                            "V4": ["grocery"],
                            "V5": ["gym"],
                            "V6": ["cook"],
                            "V8": ["walk"],
                        }
                    },
                    m1,
                    scope="per_clip",
                    turn=1,
                ),
            ],
            _plan("vlog", _chrono("vlog"), title="Ece'nin Cumartesisi", labels=verbs),
            "Kept your title and captioned all 8 clips.",
        )
    )

    # 29. Selection + duration + pacing together on the harbor footage.
    msg = "Skip the cliff path, keep it around 16 seconds, fast cuts."
    out.append(
        _thread(
            "harbor_selection_duration_pacing",
            "harbor_run",
            "Three constraints in one sentence: one clip out, a target length, a fast pace.",
            [_turn("t0", msg)],
            [
                _req(
                    "skip-cliff", "selection", "select", "select_exclude", {"clip_ids": ["H6"]}, msg
                ),
                _req(
                    "sixteen-seconds",
                    "duration",
                    "timing",
                    "duration_within",
                    {"target_s": 16.0, "tol_frac": 0.1},
                    msg,
                ),
                _req(
                    "fast",
                    "pacing",
                    "timing",
                    "pacing_max_avg_clip",
                    {"max_avg_clip_s": 2.4},
                    msg,
                ),
            ],
            _plan(
                "harbor_run",
                [c for c in harbor if c != "H6"],
                title="Brightwater run",
                cut_s=2.3,
            ),
            "Left out the cliff path and cut it to about 16 seconds with quick shots.",
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
