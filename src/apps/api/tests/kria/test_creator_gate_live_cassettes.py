"""KRI-470 / KRI-476: REAL model output -> clarification gate, chained end to end.

The cassettes under ``tests/fixtures/agent_evals/{main_creator,clip_intent_planner,
clip_request_resolver}/kri470_gate_*.json`` are live recordings from the owner-approved gate
experiment. The Main Creator ones were recorded twice on 2026-10-07: under prompt
``2026-10-06-v44`` (``kri470_gate_<case>_v44.json``, kept as the before-picture) and re-recorded
under ``2026-10-07-v45`` (``kri470_gate_<case>.json``, the primary cassettes). Each case here
feeds the recorded Main Creator ``raw_text`` through the same pair ``plan_live_turn`` runs:

    brief_updates -> ``apply_updates`` -> ``planner._plan_from_creator_output``
    -> media snapshot -> ``planner._gate_unresolved_choices``

and asserts whether the gate asks. This pins model-output -> gate ONLY. The eval replay of these
cassettes ignores the prompt text, so replay cannot prove a prompt change works; the v45
re-record is what showed it: a stated "60 second" now reaches the brief
(``test_stated_60s_reaches_the_brief_and_stays_silent``) where v44 lost it.

What the v45 recording ALSO showed (pinned, not hidden): the model chose ``archetype: day_vlog``
on 12-clip montages, and an archetype exempts the length question, so a brief that carries the
creator's 8 s no longer asks (``test_v45_8s_brief_is_exempted_by_the_models_own_archetype``).

Dependencies the cases make explicit:

* ``duration_vs_count`` needs (1) a ``timing`` requirement with ``duration_s`` in the live brief
  AND (2) a known clip set: an include intent that the clip-request resolver resolved, or a
  selection on the strategy. Without the resolved include intent the gate stays silent
  (``test_no_include_intent_keeps_duration_gate_silent``).
* ``order_basis`` fires when the brief asks for capture order and the clips carry no capture time.
* A strategy ``archetype`` or ``montage_audio`` exempts both questions (case d, v45 a).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents._schemas.creator_agent import (
    CapabilityAvailability,
    ResolvedCreatorManifest,
)
from app.agents.main_creator import MainCreatorAgent, MainCreatorInput, MainCreatorOutput
from app.kria import planner
from app.kria.brief import apply_updates, render_brief_request
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentClip, IntentResolution

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "agent_evals"


def _load(agent: str, case: str) -> dict[str, Any] | None:
    path = FIXTURES / agent / f"kri470_gate_{case}.json"
    return json.loads(path.read_text()) if path.exists() else None


def _parse(cassette: dict[str, Any], raw: dict[str, Any]) -> MainCreatorOutput:
    """The REAL agent parse (envelope repairs, brief_updates hoisting, strict schema)."""
    return MainCreatorAgent(model_client=None).parse(
        json.dumps(raw), MainCreatorInput.model_validate(cassette["input"])
    )


def _clip_ids(cassette: dict[str, Any]) -> list[str]:
    return [m["media_id"] for m in cassette["input"]["media_context"]]


def _snapshot_rows(media_context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for m in media_context:
        row: dict[str, Any] = {
            "media_id": m["media_id"],
            "kind": m["kind"],
            "duration_s": m["duration_s"],
            "has_audio": True,
        }
        for fact in m.get("facts") or []:
            if fact["kind"] == "capture_time":
                row["capture"] = {"capture_time": fact["value"]}
        rows.append(row)
    return rows


def _intent_clips(rows: list[dict[str, Any]]) -> list[IntentClip]:
    return [
        IntentClip(
            media_id=r["media_id"],
            kind="video",
            analysis=None,
            capture_time=(
                datetime.fromisoformat(r["capture"]["capture_time"].replace("Z", "+00:00"))
                if "capture" in r
                else None
            ),
        )
        for r in rows
    ]


def _recorded_intents(case: str, ids: list[str]) -> list[ResolvedClipIntent]:
    """The include/exclude intent the REAL clip-intent planner + resolver produced.

    The resolver addresses clips by positional alias (m001 = first clip); map them back."""
    planner_cassette = _load("clip_intent_planner", case)
    resolver_cassette = _load("clip_request_resolver", case)
    if not planner_cassette or not resolver_cassette:
        return []
    by_alias = {f"m{i + 1:03d}": media_id for i, media_id in enumerate(ids)}
    assignments = {
        intent["intent_id"]: intent["assignments"]
        for intent in json.loads(resolver_cassette["raw_text"])["intents"]
    }
    resolved = []
    for raw in json.loads(planner_cassette["raw_text"])["intents"]:
        if raw["op"] != "include":
            continue
        intent = ClipIntent(intent_id=raw["intent_id"], op="include", attribute=raw["attribute"])
        resolved.append(
            ResolvedClipIntent(
                **intent.model_dump(),
                assignments=[
                    ClipAssignment(
                        media_id=by_alias[a["media"]],
                        evidence=a["evidence"],
                        confidence=a["confidence"],
                    )
                    for a in assignments.get(raw["intent_id"], [])
                ],
            )
        )
    return resolved


def _include_all(ids: list[str]) -> ResolvedClipIntent:
    intent = ClipIntent(intent_id="inc", op="include", attribute="all clips")
    return ResolvedClipIntent(
        **intent.model_dump(),
        assignments=[ClipAssignment(media_id=i, evidence="x", confidence=0.9) for i in ids],
    )


async def _gate_turn(
    monkeypatch: pytest.MonkeyPatch,
    cassette: dict[str, Any],
    *,
    raw_output: dict[str, Any],
    media_context: list[dict[str, Any]] | None = None,
    resolved: list[ResolvedClipIntent],
):
    """One planner turn + the clarification gate, over a (possibly adjusted) model output."""
    inp = cassette["input"]
    request = inp["user_message"]
    output = _parse(cassette, raw_output)
    rows = _snapshot_rows(media_context or inp["media_context"])
    manifest = ResolvedCreatorManifest.model_validate(inp["capability_manifest"])
    assert manifest.capabilities["edit_format:montage"].available
    item_id = uuid.uuid4()
    manifest = manifest.model_copy(update={"item_id": str(item_id)})

    brief = apply_updates(None, output.brief_updates, source_turn_id=None)
    resolver = AsyncMock(
        return_value=PlannedIntentResolution(
            [ClipIntent(intent_id="g", op="include", attribute="x")] if resolved else [],
            IntentResolution(intents=resolved),
        )
    )
    monkeypatch.setattr(planner.settings, "clip_intents_enabled", True)
    monkeypatch.setattr(planner.settings, "kria_choice_questions_enabled", True)
    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolver)
    monkeypatch.setattr(planner, "_load_thread_events", AsyncMock(return_value=[]))
    monkeypatch.setattr(type(planner.settings), "creative_brief_for", lambda _s, _i: True)
    monkeypatch.setattr(type(planner.settings), "brief_binding_for", lambda _s, _i: True)
    monkeypatch.setattr(planner, "load_latest_brief", AsyncMock(return_value=None))
    planned = await planner._plan_from_creator_output(
        SimpleNamespace(),
        thread_id=uuid.uuid4(),
        item_id=item_id,
        creator_id=uuid.uuid4(),
        user_message=request,
        manifest=manifest,
        inputs=planner._CreatorInputs(
            agent_input=SimpleNamespace(),
            intent_clips=_intent_clips(rows),
            creator_request=request,
        ),
        output=output,
        brief_request=render_brief_request(brief, latest_message=request),
        wants_capture_order=planner._brief_wants_capture_order(brief),
    )
    assert planned.plan.mode == "act", planned.plan.response
    planned = replace(
        planned,
        media_snapshot={"clip_assignments": rows},
        brief_updates=tuple(output.brief_updates),
    )
    return await planner._gate_unresolved_choices(
        SimpleNamespace(), planned, thread_id=uuid.uuid4(), creator_id=uuid.uuid4()
    )


def _question(gated) -> dict[str, Any] | None:
    return gated.plan.choice_question


def _assert_silent(gated) -> None:
    assert _question(gated) is None, gated.plan.response
    assert gated.plan.mode == "act"


def _assert_asks(gated, conflict: str) -> dict[str, Any]:
    question = _question(gated)
    assert question is not None, "the gate stayed silent"
    assert question["conflict"] == conflict
    return question


def _raw(case: str) -> tuple[dict[str, Any], dict[str, Any]]:
    cassette = _load("main_creator", case)
    assert cassette is not None
    return cassette, json.loads(cassette["raw_text"])


def _timing_update(seconds: int) -> dict[str, Any]:
    return {
        "operation": "add",
        "kind": "timing",
        "scope": "global",
        "literal": None,
        "description": f"{seconds} second montage",
        "facts": {"duration_s": seconds},
    }


# ---------------------------------------------------------------------------------------------
# A: "8 second fast montage using all my clips"
# ---------------------------------------------------------------------------------------------


async def test_v44_stated_8s_with_all_12_clips_asks_duration_vs_count(monkeypatch):
    """Before-picture (v44): timing + select recorded, a plain montage (no archetype): asks."""
    cassette, raw = _raw("a_v44")
    kinds = {u["kind"] for u in raw["brief_updates"]}
    assert {"timing", "select"} <= kinds
    assert raw["action"]["strategy"].get("archetype") is None
    ids = _clip_ids(cassette)
    resolved = _recorded_intents("a", ids)
    assert len(resolved) == 1 and len(resolved[0].assignments) == 12

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=resolved)

    question = _assert_asks(gated, "duration_vs_count")
    assert {o["key"] for o in question["options"]} == {"extend", "fewer"}


async def test_no_include_intent_keeps_duration_gate_silent_when_a_select_is_live(monkeypatch):
    """DEPENDENCY: with a live `select` requirement the length-vs-count question needs a
    RESOLVED clip set. If the clip-intent resolver yields no include intent for "all my clips"
    the gate never guesses N and stays silent, even with the 8 s timing requirement."""
    cassette, raw = _raw("a_v44")

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=[])

    _assert_silent(gated)


async def test_v45_8s_brief_is_exempted_by_the_models_own_archetype(monkeypatch):
    """v45 keeps the stated 8 s (timing duration_s 8) but drops the `select` requirement and
    picks `archetype: day_vlog`. Every archetype is exempt from the length question, so the
    gate stays silent although 12 clips cannot fit 8 s. KNOWN LIMIT, pinned honestly: the
    model's own archetype choice can silence the gate for a plain 'N second montage'."""
    cassette, raw = _raw("a")
    assert [(u["kind"], u["facts"]) for u in raw["brief_updates"]] == [
        ("timing", {"duration_s": 8})
    ]
    assert raw["action"]["strategy"]["archetype"] == "day_vlog"

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=[])

    _assert_silent(gated)


async def test_v45_8s_brief_asks_once_the_archetype_exemption_is_removed(monkeypatch):
    """The same v45 recording with only the archetype cleared (a synthetic edit): the timing
    requirement the v45 prompt now guarantees is, on its own, enough for the gate to ask."""
    cassette, raw = _raw("a")
    raw = json.loads(json.dumps(raw))
    raw["action"]["strategy"]["archetype"] = None

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=[])

    question = _assert_asks(gated, "duration_vs_count")
    assert {o["key"] for o in question["options"]} == {"extend", "fewer"}


# ---------------------------------------------------------------------------------------------
# B: "60 second montage of all my clips" -- v44 LOST the stated length, v45 keeps it
# ---------------------------------------------------------------------------------------------


async def test_stated_60s_reaches_the_brief_and_stays_silent(monkeypatch):
    """The v45 prompt fix, as recorded live: the creator's 60 seconds is now a `timing`
    requirement (v44 returned an empty `brief_updates` for the same input, pinned below as the
    before-picture). 12 clips fit 60 s, so the gate must NOT ask."""
    cassette, raw = _raw("b")
    assert "60 second" in cassette["input"]["user_message"]
    assert [(u["kind"], u["facts"]) for u in raw["brief_updates"]] == [
        ("timing", {"duration_s": 60})
    ]
    _, v44 = _raw("b_v44")
    assert not v44["brief_updates"]
    ids = _clip_ids(cassette)

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=[_include_all(ids)])

    _assert_silent(gated)


async def test_30_clips_in_15s_synthetic_brief_asks(monkeypatch):
    """What a lost/limited timing requirement would have hidden: 30 clips in 15 s must ask
    (a plain montage: the recorded archetype exemption is cleared, see case A)."""
    cassette, raw = _raw("a")
    media = [
        {
            "media_id": f"clip-{i}",
            "kind": "video",
            "duration_s": 6.0,
            "facts": [{"kind": "capture_time", "value": f"2026-09-20T07:{i:02d}:00Z"}],
        }
        for i in range(1, 31)
    ]
    raw = json.loads(json.dumps(raw))
    raw["brief_updates"] = [_timing_update(15)]
    raw["action"]["strategy"]["target_duration_s"] = 15
    raw["action"]["strategy"]["archetype"] = None
    ids = [m["media_id"] for m in media]

    gated = await _gate_turn(
        monkeypatch,
        cassette,
        raw_output=raw,
        media_context=media,
        resolved=[_include_all(ids)],
    )

    question = _assert_asks(gated, "duration_vs_count")
    assert {o["key"] for o in question["options"]} == {"extend", "fewer"}


@pytest.mark.parametrize(("case", "seconds"), [("a", 8), ("b", 60), ("e", 20)])
def test_v45_model_emits_a_timing_requirement_for_every_stated_length(case, seconds):
    _, raw = _raw(case)
    timings = [u["facts"] for u in raw["brief_updates"] if u["kind"] == "timing"]
    assert timings == [{"duration_s": seconds}]


@pytest.mark.parametrize("case", ["c_dated", "c_undated", "d"])
def test_v45_model_invents_no_length_when_the_creator_stated_none(case):
    _, raw = _raw(case)
    brief_updates = raw.get("brief_updates") or raw["action"].get("brief_updates") or []
    assert all(u["kind"] != "timing" for u in brief_updates)


# ---------------------------------------------------------------------------------------------
# C: "chronological order" -- the order requirement is emitted reliably
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["c_dated", "c_undated"])
def test_model_reliably_emits_the_order_requirement(case):
    _, raw = _raw(case)
    assert [(u["kind"], u["facts"]) for u in raw["brief_updates"]] == [
        ("order", {"key": "capture_time"})
    ]


async def test_chronological_order_with_capture_times_is_silent(monkeypatch):
    cassette, raw = _raw("c_dated")

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=[])

    _assert_silent(gated)


async def test_chronological_order_without_capture_times_asks_order_basis(monkeypatch):
    cassette, raw = _raw("c_undated")
    assert all(not m.get("facts") for m in cassette["input"]["media_context"])

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=[])

    _assert_asks(gated, "order_basis")


# ---------------------------------------------------------------------------------------------
# D: talk-to-camera voice behind a montage of the remaining clips (montage_audio)
# ---------------------------------------------------------------------------------------------


async def test_montage_audio_plan_reaches_approval_without_a_question(monkeypatch):
    """Montage-audio plans are exempt from the gate (a known limit: a conflict inside one stays
    silent)."""
    cassette, raw = _raw("d")
    ids = _clip_ids(cassette)
    # The recorded planner/resolver outputs come from the v44 run (v45 dropped the `select`
    # requirement, so prod would not have asked the resolver); they still describe the include.
    resolved = _recorded_intents("d", ids)
    assert len(resolved) == 1 and len(resolved[0].assignments) == 11
    assert "clip-1" not in {a.media_id for a in resolved[0].assignments}

    gated = await _gate_turn(monkeypatch, cassette, raw_output=raw, resolved=resolved)

    _assert_silent(gated)


def _route_verdict(case: str, platform: str):
    from app.services.render_route import resolve_route, route_inputs_from_job
    from tests.incidents.loader import route_job
    from tests.incidents.models import IncidentRecord

    cassette, raw = _raw(case)
    output = _parse(cassette, raw)
    brief = apply_updates(None, output.brief_updates, source_turn_id=None)
    media = []
    for m in cassette["input"]["media_context"]:
        row: dict[str, Any] = {
            "id": m["media_id"],
            "kind": "video",
            "duration_s": m["duration_s"],
            "has_audio": True,
        }
        for fact in m.get("facts") or []:
            if fact["kind"] == "capture_time":
                row["capture_time"] = fact["value"]
        speech = (m.get("analysis_only_not_copy") or {}).get("speech")
        row["speech"] = {"has_speech": True, "to_camera": True} if speech else {"has_speech": False}
        media.append(row)
    record = IncidentRecord.model_validate(
        {
            "id": f"kri470-cassette-{case.replace('_', '-')}",
            "incident": {"ticket": "KRI-469", "summary": "live-recorded plan, shadow route check"},
            "kind": "routing",
            "approved": {
                "strategy": output.action.strategy.model_dump(mode="json", exclude_none=True),
                "brief": brief.model_dump(mode="json"),
                "creator_request": cassette["input"]["user_message"],
            },
            "inputs": {"media": media},
            "expect": {"route": {"platform": platform, "route": "x"}},
            "repro": {"command": "pytest x", "status": "pending", "owner": "KRI-470"},
        }
    )
    assembly, candidates = route_job(record, platform)
    inputs = route_inputs_from_job(assembly, candidates, platform=platform)
    assert inputs is not None
    return resolve_route(inputs)


def test_montage_audio_route_shape_is_the_documented_kri469_gap():
    """KRI-469 SHAPE, pinned on purpose: the same approved plan resolves to a decline on cloud
    (`montage_audio.source_media_ids[]` is not a cloud-contract field) and to the speech montage
    on the phone. The PR-H slice changes this; when it does, update THIS test with it."""
    cloud = _route_verdict("d", "cloud")
    phone = _route_verdict("d", "phone")

    assert "capability_unavailable" in repr(cloud)
    assert "montage_audio.source_media_ids[]" in repr(cloud)
    assert "speech_montage" in repr(phone)


# ---------------------------------------------------------------------------------------------
# E: "20 second montage of my 6 best clips" -- timing recorded, the 6 fit
# ---------------------------------------------------------------------------------------------


async def test_20s_with_6_selected_clips_is_silent(monkeypatch):
    cassette, raw = _raw("e")
    assert [u["facts"] for u in raw["brief_updates"] if u["kind"] == "timing"] == [
        {"duration_s": 20}
    ]
    selected = raw["action"]["strategy"]["selected_media_ids"]
    assert len(selected) == 6

    gated = await _gate_turn(
        monkeypatch, cassette, raw_output=raw, resolved=_recorded_intents("e", selected)
    )

    _assert_silent(gated)


def test_main_creator_manifest_has_the_runtime_format_capability():
    """The cassettes carry `edit_format:montage` the way `creator_capabilities` adds it at
    runtime, so the strategy-policy check runs unmodified."""
    for case in ("a", "b", "c_dated", "c_undated", "d", "e", "a_v44", "b_v44", "d_v44", "e_v44"):
        cassette = _load("main_creator", case)
        assert cassette is not None
        manifest = ResolvedCreatorManifest.model_validate(cassette["input"]["capability_manifest"])
        assert manifest.capabilities["edit_format:montage"] == CapabilityAvailability(
            available=True
        )
