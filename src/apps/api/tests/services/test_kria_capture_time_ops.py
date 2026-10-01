"""KRI-219: order by filming time + filming-hour labels as EDITOR ops.

The user asked (after a render) to "order the videos by the time they were filmed
and add the hour to each". That must be editor ops, built only from the capture
times the server put on each slot, never from model text.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.kria.brief import (
    BriefRequirement,
    plan_shape_from_editor_snapshot,
    route_requirements,
)
from app.services.clip_facts import display_timezone, format_capture_hour, ordered_capture_media
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.services._guided_timeline_fixtures import (
    arm_guided,
    guided_bars,
    guided_job,
    guided_revision,
)


def _timed(place: str) -> list[dict]:
    return [
        {"kind": "capture_time", "value": "2026-09-20T12:00:00Z", "provenance": "exif"},
        {"kind": "place", "value": place, "provenance": "geocode"},
    ]


def _facts(iso: str | None, place: str | None = None) -> list[dict]:
    rows = []
    if iso:
        rows.append({"kind": "capture_time", "value": iso, "provenance": "exif"})
    if place:
        rows.append({"kind": "place", "value": place, "provenance": "geocode"})
    return rows


# m0 filmed 3rd, m1 filmed 1st, m2 has no filming time, m3 filmed 2nd.
FACTS = {
    "m0": _facts("2026-09-20T14:32:00Z", "Antalya Sahili, Antalya, Türkiye"),
    "m1": _facts("2026-09-20T09:05:00Z", "Meltem, Antalya, Türkiye"),
    "m2": _facts(None, "Konyaaltı, Antalya, Türkiye"),
    "m3": _facts("2026-09-20T11:47:00Z", "D400, Antalya, Türkiye"),
}


@pytest.fixture
def guided(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True, raising=False)
    job_id = str(uuid.uuid4())
    revision = guided_revision(job_id)
    job, variant = guided_job(revision, guided_bars(revision), job_id)
    arm_guided(monkeypatch, revision)
    return job, variant, revision


def _snapshot(job, variant, facts=None) -> dict:
    return build_editor_snapshot(job, variant, clip_context={"facts": facts or FACTS})


def _parse(snapshot: dict, ops: list[dict], utterance: str = "do it"):
    raw = json.dumps(
        {
            "intent": "edit",
            "ops": ops,
            "confidence": 0.9,
            "reply": "Done.",
            "suggestions": [],
            "needs_clarification": False,
        }
    )
    return EditCopilotAgent(ModelClient()).parse(
        raw, EditCopilotInput(utterance=utterance, prior_turns=[], variant_snapshot=snapshot)
    )


# ── reorder_clips_by ─────────────────────────────────────────────────────────


def test_reorder_by_capture_time_sorts_timed_clips_and_pins_untimed(guided) -> None:
    job, variant, _rev = guided
    out = _parse(_snapshot(job, variant), [{"op": "reorder_clips_by", "criterion": "capture_time"}])
    assert out.outcome == "proposed"
    assert out.ops[0]["permutation"] == [1, 3, 2, 0]  # m2 stays in slot 2
    assert "No filming time for clip 3" in out.reply
    compiled = compile_editor_ops(job, variant, out.ops)
    assert compiled.changes[0] == "Order clips by filming time"
    assert [row["slot_id"] for row in _slot_rows(compiled)] == ["s2", "s4", "s3", "s1"]


def _slot_rows(compiled):
    return [
        row.model_dump() if hasattr(row, "model_dump") else row
        for row in compiled.payload.timeline_slots
    ]


def test_reorder_newest_first(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _snapshot(job, variant),
        [{"op": "reorder_clips_by", "criterion": "capture_time", "direction": "desc"}],
    )
    assert out.ops[0]["permutation"] == [0, 3, 2, 1]
    assert out.ops[0]["direction"] == "desc"


def test_reorder_labels_follow_their_clip(guided) -> None:
    job, variant, _rev = guided
    out = _parse(_snapshot(job, variant), [{"op": "reorder_clips_by", "criterion": "capture_time"}])
    compiled = compile_editor_ops(job, variant, out.ops)
    bars = {row["id"]: row for row in compiled.payload.text_elements}
    # m1 is now first (0-2 s); m0 (labelled "Label m0") moved to the last slot.
    assert (bars["clip-label-media-m1"]["start_s"], bars["clip-label-media-m1"]["end_s"]) == (0, 2)
    assert (bars["clip-label-media-m0"]["start_s"], bars["clip-label-media-m0"]["end_s"]) == (6, 8)


def test_reorder_refuses_honestly_without_two_timed_clips(guided) -> None:
    job, variant, _rev = guided
    facts = {"m0": FACTS["m0"], "m1": _facts(None), "m2": _facts(None), "m3": _facts(None)}
    out = _parse(
        _snapshot(job, variant, facts),
        [{"op": "reorder_clips_by", "criterion": "capture_time"}],
    )
    assert out.ops == []
    assert "couldn't order them by filming time" in out.reply
    assert "Done." not in out.reply


def test_reorder_already_in_order_is_a_noop_reply(guided) -> None:
    job, variant, _rev = guided
    facts = {f"m{i}": _facts(f"2026-09-20T0{i}:00:00Z") for i in range(4)}
    out = _parse(
        _snapshot(job, variant, facts), [{"op": "reorder_clips_by", "criterion": "capture_time"}]
    )
    assert out.ops == []
    assert "already in chronological order" in out.reply


def test_reorder_rejects_unknown_criterion_and_model_permutation(guided) -> None:
    job, variant, _rev = guided
    snap = _snapshot(job, variant)
    assert _parse(snap, [{"op": "reorder_clips_by", "criterion": "vibes"}]).ops == []
    out = _parse(
        snap,
        [{"op": "reorder_clips_by", "criterion": "capture_time", "permutation": [3, 2, 1, 0]}],
    )
    assert out.ops[0]["permutation"] == [1, 3, 2, 0]  # never the model's


def test_reorder_needs_a_v2_snapshot(guided) -> None:
    job, variant, _rev = guided
    snap = _snapshot(job, variant)
    snap.pop("editor_ops_version")
    assert _parse(snap, [{"op": "reorder_clips_by", "criterion": "capture_time"}]).ops == []


def test_stale_permutation_is_refused_at_compile(guided) -> None:
    from app.services.kria_editor_ops import KriaEditorOpError

    job, variant, _rev = guided
    bad = {
        "op": "reorder_clips_by",
        "criterion": "capture_time",
        "permutation": [1, 0],
        "media_ids": ["m1", "m0"],
    }
    with pytest.raises(KriaEditorOpError):
        compile_editor_ops(job, variant, [bad])


def test_ordered_capture_media_descending_keeps_pins_and_ties() -> None:
    from datetime import UTC, datetime

    t = datetime(2026, 9, 20, 9, tzinfo=UTC)
    result = ordered_capture_media(["a", "b", "c", "d"], {"a": t, "b": t, "d": t}, descending=True)
    assert result.ordered_ids == ["a", "b", "c", "d"]  # ties keep attachment order, c pinned


# ── label_each_clip from capture_time ────────────────────────────────────────


def test_label_from_capture_time_appends_the_hour_in_local_time(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _snapshot(job, variant),
        [
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
                "labels": [{"media_id": "m0", "text": "INVENTED"}],
            }
        ],
    )
    assert out.outcome == "proposed"
    labels = {row["media_id"]: row["text"] for row in out.ops[0]["labels"]}
    # Europe/Istanbul is UTC+3 (read off the Turkish place): 14:32Z -> 17:32.
    assert labels == {
        "m0": "Label m0 · 17:32",
        "m1": "Label m1 · 12:05",
        "m3": "Label m3 · 14:47",
    }
    assert "INVENTED" not in json.dumps(out.ops)
    assert "No filming time for clip 3" in out.reply
    assert "Europe/Istanbul" in out.reply
    compiled = compile_editor_ops(job, variant, out.ops)
    texts = {row["id"]: row["text"] for row in compiled.payload.text_elements}
    assert texts["clip-label-media-m0"] == "Label m0 · 17:32"
    assert compiled.changes == ["Add filming time to 3 clips"]


def test_label_from_capture_time_is_utc_when_no_zone_is_known(guided) -> None:
    job, variant, _rev = guided
    facts = {k: _facts(f"2026-09-20T1{i}:30:00Z") for i, k in enumerate(["m0", "m1", "m2", "m3"])}
    out = _parse(
        _snapshot(job, variant, facts),
        [
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
            }
        ],
    )
    assert out.ops[0]["timezone"] == "UTC"
    assert "shown in UTC" in out.reply
    assert {r["text"] for r in out.ops[0]["labels"]} == {
        "Label m0 \u00b7 10:30",
        "Label m1 \u00b7 11:30",
        "Label m2 \u00b7 12:30",
        "Label m3 \u00b7 13:30",
    }


def test_replace_mode_swaps_an_unedited_label_for_the_hour_but_keeps_edited_ones(guided) -> None:
    job, variant, _rev = guided
    snap = _snapshot(job, variant)
    for bar in snap["text_bars"]:
        if bar.get("clip_id") in {"m0", "m1"}:
            bar["edited"] = bar["clip_id"] == "m1"
    out = _parse(snap, [{"op": "label_each_clip", "source": "facts", "label_from": "capture_time"}])
    labels = {r["media_id"]: r["text"] for r in out.ops[0]["labels"]}
    assert labels["m0"] == "17:32"
    assert "m1" not in labels


def test_label_from_capture_time_honours_a_named_zone(guided) -> None:
    job, variant, _rev = guided
    facts = {k: _facts("2026-09-20T12:00:00Z") for k in ["m0", "m1", "m2", "m3"]}
    out = _parse(
        _snapshot(job, variant, facts),
        [
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
                "timezone": "America/New_York",
            }
        ],
    )
    assert out.ops[0]["labels"][0]["text"].endswith("08:00")
    bogus = _parse(
        _snapshot(job, variant, facts),
        [
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
                "timezone": "Mars/Base",
            }
        ],
    )
    assert bogus.ops[0]["timezone"] == "UTC"


def test_hour_already_shown_is_not_appended_twice(guided) -> None:
    job, variant, _rev = guided
    for bar in variant["text_elements"]:
        if bar["id"] == "clip-label-media-m0":
            bar["text"] = "Antalya Sahili · 17:32"
    out = _parse(
        _snapshot(job, variant),
        [
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
            }
        ],
    )
    assert {r["media_id"] for r in out.ops[0]["labels"]} == {"m1", "m3"}


def test_no_capture_times_means_no_labels_and_an_honest_reply(guided) -> None:
    job, variant, _rev = guided
    facts = {k: _facts(None, "Somewhere, Testland") for k in ["m0", "m1", "m2", "m3"]}
    out = _parse(
        _snapshot(job, variant, facts),
        [{"op": "label_each_clip", "source": "facts", "label_from": "capture_time"}],
    )
    assert out.ops == []
    assert "No filming time for clips 1, 2, 3, 4" in out.reply


def test_default_label_each_clip_is_unchanged_and_rejects_bad_params(guided) -> None:
    job, variant, _rev = guided
    snap = _snapshot(job, variant)
    bad = _parse(snap, [{"op": "label_each_clip", "source": "facts", "label_from": "vibes"}])
    assert bad.ops == []
    default = _parse(snap, [{"op": "label_each_clip", "source": "facts"}])
    assert all("label_from" not in op for op in default.ops)


def test_reorder_then_label_in_one_bundle_keeps_labels_on_their_clips(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _snapshot(job, variant),
        [
            {"op": "reorder_clips_by", "criterion": "capture_time"},
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
            },
        ],
    )
    assert [op["op"] for op in out.ops] == ["reorder_clips_by", "label_each_clip"]
    compiled = compile_editor_ops(job, variant, out.ops)
    bars = {row["id"]: row for row in compiled.payload.text_elements}
    assert bars["clip-label-media-m1"]["text"] == "Label m1 · 12:05"
    assert bars["clip-label-media-m1"]["start_s"] == 0  # m1 is first now
    assert bars["clip-label-media-m0"]["text"].endswith("17:32")
    assert bars["clip-label-media-m0"]["start_s"] == 6


# ── zone + format helpers ────────────────────────────────────────────────────


def test_display_timezone_precedence() -> None:
    place = [_timed("Kadıköy, İstanbul, Türkiye")]
    assert display_timezone(place) == ("Europe/Istanbul", "place")
    assert display_timezone([_timed("Meltem, Muratpasa, Turquia")]) == (
        "Europe/Istanbul",
        "place",
    )
    assert display_timezone(place, "Asia/Tokyo") == ("Asia/Tokyo", "creator")
    assert display_timezone([_timed("Somewhere, Testland")]) == ("UTC", "utc")
    assert display_timezone([], "nope") == ("UTC", "utc")
    from datetime import UTC, datetime

    assert (
        format_capture_hour(datetime(2026, 1, 1, 21, 5, tzinfo=UTC), "Europe/Istanbul") == "00:05"
    )


# ── routing ──────────────────────────────────────────────────────────────────


def test_time_ask_routes_to_editor_ops_not_a_replan(guided) -> None:
    job, variant, _rev = guided
    shape = plan_shape_from_editor_snapshot(_snapshot(job, variant))
    assert shape.can_edit_timeline and shape.can_order_by_capture_time
    order = BriefRequirement(
        id="r1",
        kind="order",
        scope="global",
        description="by filming time",
        facts={"key": "capture_time"},
    )
    hour = BriefRequirement(
        id="r2",
        kind="text",
        scope="per_clip",
        description="the hour it was filmed",
        facts={"key": "capture_time"},
    )
    message = "Order the videos based on the time they were filmed. Add the hour to each video"
    assert route_requirements([order, hour], shape, message=message) == "editor_ops"
    assert route_requirements([order], shape, message="order by time filmed") == "editor_ops"


def test_semantic_selection_and_place_labels_with_order_still_replan(guided) -> None:
    job, variant, _rev = guided
    shape = plan_shape_from_editor_snapshot(_snapshot(job, variant))
    order = BriefRequirement(
        id="r1", kind="order", scope="global", description="x", facts={"key": "capture_time"}
    )
    select = BriefRequirement(
        id="r2", kind="select", scope="global", description="only the funniest clips"
    )
    place = BriefRequirement(
        id="r3", kind="text", scope="per_clip", description="the landmark on each clip"
    )
    assert route_requirements([order, select], shape, message="only funniest") == "replan"
    # place labels + order is still the planner's job unless the label is the hour
    assert route_requirements([order, place], shape, message="landmarks, in order") == "replan"


def test_no_capture_facts_keeps_the_planner_route(guided) -> None:
    job, variant, _rev = guided
    shape = plan_shape_from_editor_snapshot(build_editor_snapshot(job, variant))
    assert not shape.can_order_by_capture_time
    order = BriefRequirement(
        id="r1", kind="order", scope="global", description="x", facts={"key": "capture_time"}
    )
    assert route_requirements([order], shape, message="order by time filmed") == "replan"


# ── re-plan path honesty (KRI-129: no silent place-for-time substitution) ────


def _montage(brief_reqs, capture: dict[str, str | None], places: dict[str, str] | None = None):
    from datetime import datetime

    from app.kria.brief import CreativeBrief
    from app.pipeline.unified_montage import UnifiedClip, brief_view, plan_unified_montage

    clips = []
    for i, (media_id, iso) in enumerate(capture.items()):
        place = (places or {}).get(media_id, f"Place {i}, Antalya, Türkiye")
        facts = [{"kind": "place", "value": place, "provenance": "geocode"}]
        when = None
        if iso:
            facts.append({"kind": "capture_time", "value": iso, "provenance": "exif"})
            when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        clips.append(
            UnifiedClip(
                media_id=media_id,
                proxy_path=f"users/u/{media_id}.mp4",
                generation="1",
                duration_s=4.0,
                width=1080,
                height=1920,
                facts=tuple(facts),
                capture_time=when,
            )
        )
    brief = CreativeBrief(version=1, requirements=brief_reqs)
    return plan_unified_montage(clips, brief_view(brief))


HOUR_REQS = [
    BriefRequirement(
        id="r1", kind="order", scope="global", description="x", facts={"key": "capture_time"}
    ),
    BriefRequirement(id="r2", kind="text", scope="per_clip", description="the hour of each clip"),
]


def test_montage_prints_the_hour_never_the_place() -> None:
    plan = _montage(
        HOUR_REQS,
        {"a": "2026-09-20T14:32:00Z", "b": "2026-09-20T14:32:00Z", "c": "2026-09-20T09:05:00Z"},
    )
    labels = {row["media_id"]: row for row in plan.record()["labels"]}
    assert [labels[c]["text"] for c in plan.clip_ids] == ["12:05", "17:32", "17:32"]
    assert {row["fact_kind"] for row in labels.values()} == {"capture_time"}  # same hour twice kept
    assert plan.record()["label_timezone"] == "Europe/Istanbul"


def test_montage_untimed_clip_is_left_unlabelled_with_an_honest_reason() -> None:
    plan = _montage(
        HOUR_REQS, {"a": "2026-09-20T14:32:00Z", "b": None, "c": "2026-09-20T09:05:00Z"}
    )
    record = plan.record()
    assert "b" not in {row["media_id"] for row in record["labels"]}
    assert record["dropped_label_reasons"] == {"b": "no_capture_time"}


def test_receipt_flags_place_names_standing_in_for_the_hour() -> None:
    from app.kria.brief_checks import check_requirement, plan_facts_from_unified_montage

    place_reqs = [
        BriefRequirement(
            id="r2", kind="text", scope="per_clip", description="the landmark on each clip"
        )
    ]
    plan = _montage(place_reqs, {"a": "2026-09-20T14:32:00Z", "b": "2026-09-20T09:05:00Z"})
    facts = plan_facts_from_unified_montage(plan.record())
    time_req = HOUR_REQS[1]
    receipt = check_requirement(time_req, facts)
    assert receipt.status == "not_possible"
    assert "place names" in (receipt.reason or "")


def test_receipt_partial_when_some_clips_have_no_time_and_when_utc() -> None:
    from app.kria.brief_checks import check_requirement, plan_facts_from_unified_montage

    plan = _montage(
        HOUR_REQS, {"a": "2026-09-20T14:32:00Z", "b": None, "c": "2026-09-20T09:05:00Z"}
    )
    receipt = check_requirement(HOUR_REQS[1], plan_facts_from_unified_montage(plan.record()))
    assert receipt.status == "partial" and "2 of 3" in (receipt.reason or "")
    full = _montage(HOUR_REQS, {"a": "2026-09-20T14:32:00Z", "c": "2026-09-20T09:05:00Z"})
    assert (
        check_requirement(HOUR_REQS[1], plan_facts_from_unified_montage(full.record())).status
        == "met"
    )


def test_mixed_countries_resolve_to_utc_whatever_the_order() -> None:
    uk = _timed("Wandsworth, London, United Kingdom")
    tr = _timed("Ulus, Be\u015fikta\u015f, T\u00fcrkiye")
    assert display_timezone([uk, tr]) == ("UTC", "utc")
    assert display_timezone([tr, uk]) == ("UTC", "utc")
    assert display_timezone([tr, _timed("Somewhere, Testland")]) == ("UTC", "utc")
    assert display_timezone([tr, tr]) == ("Europe/Istanbul", "place")
    # a clip with no place, or with no capture time, does not vote
    noplace = [{"kind": "capture_time", "value": "2026-09-20T12:00:00Z", "provenance": "exif"}]
    assert display_timezone([tr, noplace]) == ("Europe/Istanbul", "place")
    assert display_timezone([noplace]) == ("UTC", "utc")
    untimed = [{"kind": "place", "value": "London, United Kingdom"}]
    assert display_timezone([tr, untimed]) == ("Europe/Istanbul", "place")


def test_editor_ops_and_replan_print_identical_hours_for_the_same_clips(guided) -> None:
    places = {
        "m0": "Wandsworth, London, United Kingdom",
        "m1": "Ulus, Be\u015fikta\u015f, T\u00fcrkiye",
        "m2": "Ann Arbor, United States",
        "m3": "Lisboa, Portugal",
    }
    times = {
        "m0": "2026-06-07T15:41:55Z",
        "m1": "2024-07-11T13:36:13Z",
        "m2": "2026-05-03T05:56:36Z",
        "m3": "2026-06-18T09:17:19Z",
    }
    facts = {
        m: [
            {"kind": "capture_time", "value": times[m], "provenance": "exif"},
            {"kind": "place", "value": places[m], "provenance": "geocode"},
        ]
        for m in places
    }
    job, variant, _rev = guided
    out = _parse(
        _snapshot(job, variant, facts),
        [
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
            }
        ],
    )
    editor_hours = {}
    for row in out.ops[0]["labels"]:
        editor_hours[row["media_id"]] = row["text"].split(" \u00b7 ")[-1]
    replan = _montage(HOUR_REQS[1:], dict(times), places)
    plan_hours = {r["media_id"]: r["text"] for r in replan.record()["labels"]}
    assert editor_hours == plan_hours
    assert replan.record()["label_timezone"] == "UTC" == out.ops[0]["timezone"]
    assert "UTC" in out.reply


def test_replan_receipt_states_the_zone_in_the_reply() -> None:
    from app.kria.brief import CreativeBrief
    from app.kria.brief_checks import (
        build_receipts,
        plan_facts_from_unified_montage,
        reply_from_receipts,
    )

    plan = _montage(HOUR_REQS, {"a": "2026-09-20T14:32:00Z", "c": "2026-09-20T09:05:00Z"})
    brief = CreativeBrief(version=1, requirements=HOUR_REQS[1:])
    receipts = build_receipts(brief.requirements, plan_facts_from_unified_montage(plan.record()))
    assert "Europe/Istanbul" in reply_from_receipts(brief, receipts)


def test_noop_reorder_reply_says_only_already_ordered(guided) -> None:
    job, variant, _rev = guided
    facts = {f"m{i}": _facts(f"2026-09-20T0{i}:00:00Z") for i in range(4)}
    out = _parse(
        _snapshot(job, variant, facts),
        [
            {"op": "reorder_clips_by", "criterion": "capture_time"},
            {
                "op": "label_each_clip",
                "source": "facts",
                "label_from": "capture_time",
                "mode": "append",
            },
        ],
    )
    assert [op["op"] for op in out.ops] == ["label_each_clip"]
    assert "reordered" not in out.reply.lower() and "Done." not in out.reply
    assert "already in chronological order" in out.reply
    assert out.reply.startswith("Added the filming hour to")


# ── hour-only format, in-place routing, format honesty (KRI-219 wedding thread) ──


def _label_op(snapshot, **extra):
    return _parse(
        snapshot,
        [{"op": "label_each_clip", "source": "facts", "label_from": "capture_time", **extra}],
    )


def test_hour_only_replace_uses_facts_and_the_one_zone(guided) -> None:
    job, variant, _rev = guided
    snap = _snapshot(job, variant)
    out = _label_op(snap, mode="replace", time_format="hour")
    # model-typed / creator-changed bars that are ONLY a time may be redone
    labels = {r["media_id"]: r["text"] for r in out.ops[0]["labels"]} if out.ops else {}
    assert labels == {} or all(t.isdigit() and len(t) == 2 for t in labels.values())
    for bar in snap["text_bars"]:
        if bar.get("clip_id"):
            bar["text"], bar["edited"] = "08:00", True  # a model-typed hour
    out = _label_op(snap, mode="replace", time_format="hour")
    labels = {r["media_id"]: r["text"] for r in out.ops[0]["labels"]}
    # Istanbul (all clips Turkish): 14:32Z -> 17, 09:05Z -> 12, 11:47Z -> 14
    assert labels == {"m0": "17", "m1": "12", "m3": "14"}
    assert out.ops[0]["time_format"] == "hour"
    assert "Europe/Istanbul" in out.reply
    assert _label_op(snap, time_format="minutes").ops == []


def test_default_time_format_is_unchanged(guided) -> None:
    job, variant, _rev = guided
    out = _label_op(_snapshot(job, variant), mode="append")
    assert out.ops[0]["labels"][0]["text"].endswith("17:32")


def test_hand_worded_labels_are_still_kept_by_hour_replace(guided) -> None:
    job, variant, _rev = guided
    snap = _snapshot(job, variant)
    for bar in snap["text_bars"]:
        if bar.get("clip_id") == "m0":
            bar["text"], bar["edited"] = "Ahmet arrives", True
        elif bar.get("clip_id"):
            bar["edited"] = False
    out = _label_op(snap, mode="replace", time_format="hour")
    assert {r["media_id"] for r in out.ops[0]["labels"]} == {"m1", "m3"}


def _shape(job, variant):
    return plan_shape_from_editor_snapshot(_snapshot(job, variant))


def test_wedding_thread_requirement_sets_route_to_the_editor(guided) -> None:
    job, variant, _rev = guided
    shape = _shape(job, variant)
    style = BriefRequirement(
        id="r6",
        kind="style",
        scope="per_clip",
        description="Move the timestamps to the top left, make them smaller.",
    )
    text = BriefRequirement(
        id="r7",
        kind="text",
        scope="per_clip",
        description="Remove everything after the hours, don’t include minutes",
    )
    msg = (
        "Move the timestamps to the top left, make them smaller. Remove everything after the hours"
    )
    assert route_requirements([style, text], shape, message=msg) == "editor_ops"
    assert route_requirements([text], shape, message="just the hour") == "editor_ops"
    title = BriefRequirement(id="r8", kind="text", scope="title", literal="Ahmet")
    assert route_requirements([title, style], shape, message="title + style") == "editor_ops"
    audio = BriefRequirement(id="r9", kind="audio", scope="global", description="louder music")
    assert route_requirements([style, audio], shape, message="x") == "replan"
    select = BriefRequirement(id="r10", kind="select", scope="global", description="best 3")
    assert route_requirements([style, select], shape, message="x") == "replan"


def test_replan_hour_only_prints_bare_hours_and_receipt_verifies_format() -> None:
    from app.kria.brief_checks import check_requirement, plan_facts_from_unified_montage

    only = BriefRequirement(
        id="r7", kind="text", scope="per_clip", description="just the hour, don't include minutes"
    )
    plan = _montage([only], {"a": "2026-09-20T14:32:00Z", "c": "2026-09-20T09:05:00Z"})
    labels = [r["text"] for r in plan.record()["labels"]]
    assert all(t.isdigit() for t in labels)
    assert check_requirement(only, plan_facts_from_unified_montage(plan.record())).status == "met"
    # labels that still carry minutes can never read "met" for an hour-only ask
    record = plan.record()
    for row in record["labels"]:
        row["text"] = "14:32"
    receipt = check_requirement(only, plan_facts_from_unified_montage(record))
    assert receipt.status == "partial" and "editor" in (receipt.reason or "")


def test_zone_note_survives_the_canned_success_reply(guided) -> None:
    from app.kria.planner import _phone_editor_reply
    from app.routes._copilot import _honest_outcome

    job, variant, _rev = guided
    out = EditCopilotAgent(ModelClient()).parse(
        json.dumps(
            {
                "intent": "edit",
                "ops": [
                    {
                        "op": "label_each_clip",
                        "source": "facts",
                        "label_from": "capture_time",
                        "mode": "append",
                        "time_format": "hour",
                    }
                ],
                "confidence": 0.9,
                "reply": "Done, I updated the timestamps.",
                "suggestions": [],
                "needs_clarification": False,
            }
        ),
        EditCopilotInput(utterance="x", prior_turns=[], variant_snapshot=_snapshot(job, variant)),
    )
    _outcome, reply = _honest_outcome(out, out.ops)
    assert "Europe/Istanbul" in reply and "No filming time for clip 3" in reply
    phone = _phone_editor_reply(reply)
    assert phone.startswith("Updated your edit") and "Europe/Istanbul" in phone


# ── descriptive captions are never answered with place names (KRI-219) ───────


@pytest.mark.parametrize(
    "utterance",
    [
        "Add a text to each clip explaining the part of the wedding (could be airport pickup)",
        "write what is happening in each clip",
        "her klibe düğünün hangi bölümü olduğunu yaz",
        "Add a label to each video with the same style as the title about what it is",
    ],
)
def test_descriptive_caption_ask_is_a_clarification_not_place_names(guided, utterance) -> None:
    job, variant, _rev = guided
    out = EditCopilotAgent(ModelClient()).parse(
        json.dumps(
            {
                "intent": "edit",
                "ops": [{"op": "label_each_clip", "source": "facts"}],
                "confidence": 0.9,
                "reply": "Done, labelled every clip.",
                "suggestions": [],
                "needs_clarification": False,
            }
        ),
        EditCopilotInput(
            utterance=utterance, prior_turns=[], variant_snapshot=_snapshot(job, variant)
        ),
    )
    assert out.ops == [] and out.outcome == "clarification"
    assert "Tell me what each clip is" in out.reply and "Done" not in out.reply


def test_place_label_asks_are_not_caught_by_the_guard(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _snapshot(job, variant),
        [{"op": "label_each_clip", "source": "facts"}],
        utterance="label each clip with its place name",
    )
    assert out.ops or out.rejection_reasons  # reached the normal label path
    assert "Tell me what each clip is" not in out.reply


# ── stored clip understanding reaches the copilot (KRI-219) ──────────────────

SEEN = {
    "m0": "Guests hugging at an airport arrivals hall; airport terminal",
    "m1": "People dancing in a decorated hall; wedding hall; dancing",
    "m3": "Family walking along a seaside promenade",
}
ASK_DESCRIBE = "Add a text to each clip explaining the part of the wedding (airport pickup etc)"


def _seen_snapshot(job, variant, seen=SEEN):
    return build_editor_snapshot(job, variant, clip_context={"facts": FACTS, "seen": seen})


def _add(text, start, end, **extra):
    return {"op": "add_text", "text": text, "start_s": start, "end_s": end, **extra}


def test_snapshot_exposes_a_vision_description_per_slot_only_when_stored(guided) -> None:
    job, variant, _rev = guided
    slots = {s["media_id"]: s for s in _seen_snapshot(job, variant)["slots"]}
    assert slots["m0"]["seen"] == {"text": SEEN["m0"], "provenance": "vision"}
    assert "seen" not in slots["m2"]
    assert all("seen" not in s for s in _snapshot(job, variant)["slots"])


def test_seen_text_comes_from_the_shared_projection_and_is_bounded() -> None:
    from app.services.kria_editor_ops import _seen_text

    analysis = {
        "understanding": {
            "kind": "video",
            "summary": "A" * 400,
            "setting": "hall",
            "notable_moments": [{"start_s": 0, "end_s": 1, "description": "toast"}],
        }
    }
    text = _seen_text(analysis)
    assert 0 < len(text) <= 220
    assert _seen_text({"clip_facts": []}) == "" and _seen_text(None) == ""


def test_prompt_shows_seen_next_to_the_slot(guided) -> None:
    from app.agents.edit_copilot import _format_snapshot

    job, variant, _rev = guided
    assert "seen='Guests hugging" in _format_snapshot(_seen_snapshot(job, variant))


def test_descriptive_captions_are_kept_only_for_clips_that_were_seen(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _seen_snapshot(job, variant),
        [_add("Airport pickup", 0, 2), _add("Dancing", 2, 4), _add("Something", 4, 6)],
        utterance=ASK_DESCRIBE,
    )
    # Slots: m0 (0-2) seen, m1 (2-4) seen, m2 (4-6) NOT seen -> invented caption dropped.
    assert [op["text"] for op in out.ops] == ["Airport pickup", "Dancing"]
    assert "I wrote these from what I saw" in out.reply
    assert "clip 1: Airport pickup" in out.reply and "I left out clip 3" in out.reply
    assert "I didn't caption clip 4" in out.reply


def test_all_unseen_captions_are_dropped_with_an_honest_reply(guided) -> None:
    job, variant, _rev = guided
    out = _parse(_snapshot(job, variant), [_add("Airport pickup", 0, 2)], utterance=ASK_DESCRIBE)
    assert out.ops == [] and "can't tell what those clips show" in out.reply


def test_label_each_clip_is_still_refused_for_descriptions_even_with_seen(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _seen_snapshot(job, variant),
        [{"op": "label_each_clip", "source": "facts"}],
        utterance=ASK_DESCRIBE,
    )
    assert out.ops == [] and "write a short caption for each clip" in out.reply


def test_other_asks_are_untouched_by_the_caption_pass(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _seen_snapshot(job, variant), [_add("See you soon", 6, 8)], utterance="add an end text"
    )
    assert [op["text"] for op in out.ops] == ["See you soon"]
    assert "I wrote these" not in out.reply


# ── captions must not parrot the creator's framing onto footage that doesn't show it ──


def test_a_caption_repeating_the_creators_event_words_without_support_is_dropped(guided) -> None:
    job, variant, _rev = guided
    seen = {
        "m0": "Playing football on an outdoor turf pitch at night",
        "m1": "Running up stairs and celebrating in a rustic bar",
        "m3": "Guests hugging at an airport arrivals hall",
    }
    out = _parse(
        _seen_snapshot(job, variant, seen),
        [
            _add("Pre-wedding soccer", 0, 2),
            _add("Running up stairs", 2, 4),
            _add("Airport pickup", 6, 8),
        ],
        utterance=ASK_DESCRIBE,
    )
    # "wedding" is the creator's word and appears in neither football nor stairs seen text.
    assert [op["text"] for op in out.ops] == ["Running up stairs", "Airport pickup"]
    assert "Clip 1 doesn't look like" in out.reply and "wedding" in out.reply
    assert "clip 2: Running up stairs" in out.reply


def test_event_words_are_kept_when_the_footage_supports_them(guided) -> None:
    job, variant, _rev = guided
    seen = {"m0": "Bride getting ready for the wedding with friends"}
    out = _parse(
        _seen_snapshot(job, variant, seen),
        [_add("Wedding preparations", 0, 2)],
        utterance=ASK_DESCRIBE,
    )
    assert [op["text"] for op in out.ops] == ["Wedding preparations"]


# ── 2026-10-01 follow-ups: restyle just-added captions, enrich failures, no false replan note ──

CAPTION_BARS = [
    {
        "id": "guided-title",
        "text": "5 am in my room",
        "start_s": 0.0,
        "end_s": 1.7,
        "role": "generative_intro",
        "font_family": "Instrument Serif",
        "size_px": 78,
        "color": "#FFF8F0",
        "position": "custom",
        "x_frac": 0.78,
        "y_frac": 0.16,
        "alignment": "center",
        "effect": "static",
    },
    *[
        {
            "id": f"kria-cap{i}",
            "text": t,
            "start_s": float(2 * i),
            "end_s": float(2 * i + 2),
            "role": "generative_intro",
            "font_family": "Instrument Serif",
            "size_px": 78,
            "color": "#FFF8F0",
            "position": "custom",
            "x_frac": 0.5,
            "y_frac": 0.66,
            "alignment": "center",
            "effect": "static",
        }
        for i, t in enumerate(["Window reflection", "Computer monitors"])
    ],
]
RESTYLE_ASK = (
    "Update them to show on the bottom left or on empty spots. Make them a bit smaller, "
    "all non capital, and remove the effect"
)


def _caption_snapshot(job, variant):
    snap = _seen_snapshot(job, variant)
    snap["text_bars"] = [dict(b) for b in CAPTION_BARS]
    snap["text_appearance_version"] = 1
    snap["text_appearance"] = {
        "version": 1,
        "caption_cues_editable": False,
        "targets": [
            {
                "id": b["id"],
                "kind": "text",
                "supported_fields": ["stroke_width", "shadow_enabled", "font_family"],
                "values": {
                    "stroke_width": 0.0,
                    "shadow_enabled": True,
                    "font_family": "Instrument Serif",
                },
                "identity": f"id{b['id']}",
                **({"group": "title"} if b["id"] == "guided-title" else {}),
            }
            for b in CAPTION_BARS
        ],
    }
    return snap


def test_restyling_chat_added_captions_with_everyday_words_is_accepted(guided) -> None:
    job, variant, _rev = guided
    out = _parse(
        _caption_snapshot(job, variant),
        [
            {
                "op": "patch_text",
                "selector": {"group": "labels"},
                "patch": {
                    "position": "custom",
                    "alignment": "left",
                    "x_frac": 0.08,
                    "y_frac": 0.86,
                    "size_scale": 0.8,
                    "text_case": "lowercase",
                },
            },
            {
                "op": "patch_text_appearance",
                "selector": {"scope": "editable_text", "quantifier": "all", "group": "labels"},
                "patch": {"stroke_width": 0, "shadow_enabled": False},
                "text_appearance_version": 1,
            },
        ],
        utterance=RESTYLE_ASK,
    )
    assert [op["op"] for op in out.ops] == ["patch_text", "patch_text_appearance"]
    assert out.ops[0]["patch"]["text_case"] == "lower"
    assert out.ops[0]["target_ids"] == ["kria-cap0", "kria-cap1"]  # never the title
    assert out.ops[1]["target_ids"] == ["kria-cap0", "kria-cap1"]
    assert "captions you added in chat" in out.reply


def test_a_refused_value_is_named_in_plain_words(guided) -> None:
    from app.routes._copilot import _honest_outcome

    job, variant, _rev = guided
    out = _parse(
        _caption_snapshot(job, variant),
        [
            {
                "op": "patch_text",
                "selector": {"group": "all"},
                "patch": {"effect": "glow-in-the-dark"},
            }
        ],
        utterance="remove the effect",
    )
    assert out.ops == [] and out.outcome == "failed"
    _outcome, reply = _honest_outcome(out, out.ops)
    assert "couldn't set the effect to 'glow-in-the-dark'" in reply and "typewriter" in reply


def test_per_clip_text_is_expressible_in_place_when_clips_were_seen(guided) -> None:
    from app.kria.brief import BriefRequirement, plan_shape_from_editor_snapshot, route_requirements

    job, variant, _rev = guided
    snap = _seen_snapshot(job, variant)
    snap.pop("label_facts", None)
    snap["text_bars"] = [dict(CAPTION_BARS[0])]  # no clip-label lane at all
    req = BriefRequirement(
        id="r6",
        kind="text",
        scope="per_clip",
        description="a label about what the video is, matching the title style",
    )
    assert route_requirements([req], plan_shape_from_editor_snapshot(snap)) == "editor_ops"
    for slot in snap["slots"]:
        slot.pop("seen", None)
    assert route_requirements([req], plan_shape_from_editor_snapshot(snap)) == "replan"
