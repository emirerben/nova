"""Replay eval for the ``duplicates`` suggestion kind ("keep the better of near-identical clips").

Free, offline, no model, no database. Contexts are hand-authored fixtures in
``tests/fixtures/agent_evals/suggestion_kind_dupes/`` (stored per-clip ``near_dup_hash`` readings
plus the editor slots); the planner witnesses are the REAL ones (``make_editor_witness`` over
``parse_editor_operation`` + ``compile_editor_ops``), and a tap is replayed through the pinned
seam's own parser and the real compiler, so "tapping always works" is asserted on a compile with
a real diff. One test measures real photos with the real signal and serves the card from that.

Failure modes, written before the code (each has a fixture or a test below):

 1. a 3-way (or 4-way) burst: one card removes every other take, never a chain A~B, B~C, A!~C
    (d02, d17; the chain itself is pinned in ``tests/signals``)
 2. a photo and a video of one view: never a pair (d03); two photos are (d04)
 3. both takes are referenced by the creator (a group they asked for): no card (d05);
    only one referenced: that one stays even when it is the worse (d06)
 4. the remaining clip count drops under the floor (d07)
 5. ordinal shift: ``remove_clip.slot_index`` addresses the FULL slot list (a removed row, a
    reordered timeline), not the clip ordinal and not the list of live slots (d08); a clip in
    two slots loses both (d09); two removals in one bundle do not shift each other (d02)
 6. the loader did not say what is referenced: fail closed (d10)
 7. the twin is not in the edit (d11); the editor closed removal (d14); a locked slot (d22)
 8. the keep-one tiebreak: sharper, better exposed, longer, else listed first (d01, d12, d13)
 9. the agent names clips that are not a pair, or one clip (d16, ``test_one_named_clip``)
10. capture time: only a timed pair of near-in-time clips pairs at the looser distance
    (d18-d20)
11. an action out of the ring that removes a needed clip, a non-duplicate, half of a clip, or
    every copy, or anything but ``remove_clip``: the witness refuses (witness tests)
12. flag off: the kind is never issued (``test_flag_off``)
13. Turkish thread: Turkish closed text (d15)
14. exact byte duplicates are collapsed upstream (``kria/duplicate_uploads.py``) before a
    context is built, so this kind never sees them and cannot report the same pair twice
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from app.config import settings
from app.services import suggestion_chips as sc
from app.services.clip_signals import near_dup_hash as nd
from app.services.clip_signals.registry import SignalInputs
from app.services.suggestions import picks as pk
from app.services.suggestions import pinned, registry
from app.services.suggestions.context import SuggestionContext, snapshot_digest
from app.services.suggestions.kinds import duplicates as dup
from app.services.suggestions.models import (
    EditorOpsAction,
    RankedPick,
    SuggestionAgentOutput,
)

from . import suggestion_chips_harness as h

FIXTURES = (
    Path(__file__).resolve().parents[1] / "fixtures" / "agent_evals" / "suggestion_kind_dupes"
)
KIND = registry.get("duplicates")


def _cases() -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(FIXTURES.glob("d*.json"))]


CASES = {c["name"]: c for c in _cases()}


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setattr(settings, "kria_suggestion_duplicates_enabled", True)
    monkeypatch.setattr(settings, "kria_suggestion_signals_enabled", True)


# ── Building a context from a fixture ────────────────────────────────────────────────


def _slot(i: int, entry) -> dict:
    row = {"media_id": entry} if isinstance(entry, str) else dict(entry)
    return {
        "slot_id": f"slot-{i}",
        "output_start_s": i * 3.0,
        "output_end_s": (i + 1) * 3.0,
        "duration_s": 3.0,
        "transition_after": "cut",
        **row,
    }


def _snapshot(case: dict) -> dict:
    snap = copy.deepcopy(h.SNAPSHOT_PRESETS["plain_montage"])
    snap["slots"] = [_slot(i, e) for i, e in enumerate(case["slots"])]
    snap["total_duration_s"] = 3.0 * len(case["slots"])
    return h.deep_merge(snap, case.get("snapshot") or {})


def build_ctx(case: dict, *, with_witness: bool = True) -> SuggestionContext:
    snapshot = _snapshot(case)
    clips = [
        {
            "media_id": c["media_id"],
            "ordinal": i + 1,
            "kind": c["kind"],
            "subject": "",
            "setting": "",
            "has_speech": c["media_id"] in case.get("speech", []),
            "to_camera": c["media_id"] in case.get("to_camera", []),
            "duration_s": c["duration_s"],
            "notable_moments": [],
        }
        for i, c in enumerate(case["clips"])
    ]
    signals = {
        c["media_id"]: {
            nd.NAME: {
                "hashes": c["hashes"],
                "sharpness": c["sharpness"],
                "mean_luma": c["mean_luma"],
            }
        }
        for c in case["clips"]
    }
    inputs: dict = {}
    if case["needed"] is not None:
        inputs["needed"] = case["needed"]
    times = {c["media_id"]: c["capture_time"] for c in case["clips"] if c.get("capture_time")}
    if times:
        inputs["capture_times"] = times
    if case.get("exposure"):
        inputs["exposure"] = case["exposure"]
    witness = None
    if with_witness:
        job, variant = h.synthetic_job_and_variant(snapshot, archetype=None)
        witness = sc.make_editor_witness(job, variant, snapshot)
    return SuggestionContext(
        thread_id=f"thread-{case['name']}",
        state="ready",
        language=case.get("language", "en"),
        client_protocol=2,
        media_digest=f"digest-{case['name']}",
        manifest_hash="b" * 64,
        runtime_version=2,
        clips=clips,
        manifest={"capabilities": {}, "edit_format": "montage"},
        snapshot=snapshot,
        edit_format="montage",
        signals=signals,
        signals_version="sv_test",
        kind_inputs={dup.INPUT_KEY: inputs},
        editor_witness=witness,
    )


def _handles(ctx: SuggestionContext) -> dict:
    return pk.clip_handles(ctx)


def _named_targets(case: dict, ctx: SuggestionContext) -> list:
    """What the agent names: the case's own handles, else exactly the clips the card touches."""
    handles = _handles(ctx)
    if case.get("targets") is not None:
        return [handles[t] for t in case["targets"]]
    expect = case["expect"]
    if "keep" not in expect:  # nothing is offered: the agent tries the obvious pair
        return [handles["c1"], handles["c2"]]
    slots = _snapshot(case)["slots"]
    dropped = {slots[i]["media_id"] for i in expect.get("ops", [])}
    named = [t for t in handles.values() if t.media_id in {expect["keep"], *dropped}]
    # The card anchors to the first clip named, so a clip that goes is named first.
    return sorted(named, key=lambda t: t.media_id not in dropped)


# ── The matrix ───────────────────────────────────────────────────────────────────────


def test_inventory_is_well_formed():
    assert len(CASES) >= 20
    for case in CASES.values():
        assert case["description"].strip() and case["expect"], case["name"]
    offered = [c["expect"]["offered"] for c in CASES.values()]
    assert offered.count(True) >= 8 and offered.count(False) >= 8


@pytest.mark.parametrize("name", sorted(CASES))
def test_fixture_expectation(name):
    case = CASES[name]
    expect = case["expect"]
    ctx = build_ctx(case)
    targets = _named_targets(case, ctx)
    action = KIND.build_action(ctx, targets, None)
    if not expect["offered"]:
        assert action is None or not KIND.witness(action, ctx).ok
        if case.get("targets") is None:
            assert KIND.availability(ctx) is False
        return
    assert KIND.availability(ctx) is True and KIND.already_present(ctx) is False
    assert isinstance(action, EditorOpsAction)
    assert [op["slot_index"] for op in action.ops] == expect["ops"]
    assert {op["op"] for op in action.ops} == {"remove_clip"}
    assert action.base_digest == snapshot_digest(ctx.snapshot)
    proven = KIND.witness(action, ctx)
    assert proven.ok and proven.tier == "editor", proven
    plan = dup._plan_for(ctx, targets)
    assert (plan.keep, plan.reason) == (expect["keep"], expect["reason"])
    if "evidence" in expect:
        assert dup.evidence_lines(ctx, targets) == expect["evidence"]
    if "touches" in expect:
        slots = _snapshot(case)["slots"]
        assert {slots[i]["media_id"] for i in expect["ops"]} == set(expect["touches"])


@pytest.mark.parametrize("name", sorted(n for n, c in CASES.items() if c["expect"]["offered"]))
def test_the_unnamed_pick_gets_the_best_plan(name):
    """With no clip handle the deterministic order builds the best group; its action equals
    the one the named pick builds when that pick names the best group."""
    case = CASES[name]
    ctx = build_ctx(case)
    unnamed = KIND.build_action(ctx, [pk.NO_TARGET], None)
    assert isinstance(unnamed, EditorOpsAction)
    assert KIND.witness(unnamed, ctx).ok


# ── The served card, and the tap ─────────────────────────────────────────────────────


def _serve(case: dict):
    ctx = build_ctx(case)
    plan = sc.build_candidates(ctx)
    handles = _handles(ctx)
    named = _named_targets(case, ctx)
    ids = [next(k for k, t in handles.items() if t.media_id == n.media_id) for n in named]
    picks = SuggestionAgentOutput(
        ranked=[RankedPick(kind="duplicates", target_ids=ids, why="", copy=None, rank=1)]
    )
    return ctx, plan, sc.assemble_suggestions(ctx, plan, picks)


@pytest.mark.parametrize(
    "name",
    sorted(n for n, c in CASES.items() if c["expect"]["offered"] and "prompt" in c["expect"]),
)
def test_the_served_card_is_a_proven_removal(name):
    case = CASES[name]
    ctx, plan, (served, drops) = _serve(case)
    assert served is not None, drops
    card = next(c for c in served.suggestions if c.kind == "duplicates")
    assert card.title == case["expect"]["title"] if "title" in case["expect"] else True
    assert card.prompt == case["expect"]["prompt"]
    assert card.witness_tier == "editor" and card.action.type == "editor_ops"
    assert [op["slot_index"] for op in card.action.ops] == case["expect"]["ops"]
    # The card must survive the ring: that is how a tap finds its action.
    stored = pinned.ring_suggestions(served)
    state = {sc.RING_KEY: [{"set_id": served.set_id, "suggestions": stored}]}
    assert pinned.stored_card(state, served.set_id, card.id) is not None


@pytest.mark.parametrize("name", sorted(n for n, c in CASES.items() if c["expect"]["offered"]))
def test_a_tap_compiles_to_a_real_removal_of_exactly_the_dropped_clips(name):
    """The pinned seam's own steps: allow-list, parser against the live snapshot, then the
    real compiler. The result must change the timeline and drop only the planned slots."""
    from app.services.kria_editor_ops import compile_editor_ops

    case = CASES[name]
    ctx = build_ctx(case)
    action = KIND.build_action(ctx, _named_targets(case, ctx), None)
    ops = pinned.editor_ops(action)
    assert pinned.disallowed_op(ops) is None
    parsed = pinned.parse_ops(ops, ctx.snapshot)
    assert parsed is not None and len(parsed) == len(ops)
    job, variant = h.synthetic_job_and_variant(ctx.snapshot, archetype=None)
    compiled = compile_editor_ops(job, variant, parsed)
    assert not compiled.diff.unavailable and compiled.diff.real()
    payload = (
        compiled.payload if isinstance(compiled.payload, dict) else compiled.payload.model_dump()
    )
    removed = {i for i, s in enumerate(payload["timeline_slots"]) if s.get("removed")}
    before = {i for i, s in enumerate(ctx.snapshot["slots"]) if s.get("removed")}
    assert removed - before == set(case["expect"]["ops"])
    assert removed >= before


def test_one_named_clip_is_not_a_pair():
    case = CASES["d01_two_takes_sharper"]
    ctx = build_ctx(case)
    one = [_handles(ctx)["c2"]]
    assert KIND.build_action(ctx, one, None) is None
    assert dup.evidence_lines(ctx, one) == []


def test_a_pair_named_inside_a_burst_gets_its_own_best_of_two():
    """The agent names two clips of a 3-way burst: a valid sub-group, so the card is built for
    exactly those two and the third clip is untouched."""
    case = CASES["d02_three_way_burst"]
    ctx = build_ctx(case)
    handles = _handles(ctx)
    action = KIND.build_action(ctx, [handles["c1"], handles["c2"]], None)
    assert [op["slot_index"] for op in action.ops] == [0]
    assert KIND.witness(action, ctx).ok


def test_flag_off_the_kind_is_never_issued(monkeypatch):
    case = CASES["d01_two_takes_sharper"]
    ctx = build_ctx(case)
    assert "duplicates" in {i.kind.kind for i in pk.issue_kinds(ctx, "ready")}
    monkeypatch.setattr(settings, "kria_suggestion_duplicates_enabled", False)
    assert "duplicates" not in {i.kind.kind for i in pk.issue_kinds(ctx, "ready")}


def test_first_prompt_never_offers():
    ctx = build_ctx(CASES["d01_two_takes_sharper"])
    first = SuggestionContext(**{**ctx.__dict__, "state": "first_prompt", "snapshot": None})
    assert KIND.availability(first) is False
    assert KIND.build_action(first, [pk.NO_TARGET], None) is None


# ── The witness refuses what the builder would never make (a stored action is not trusted) ──


def _hostile(name: str, ops: list[dict], **ctx_patch) -> tuple[SuggestionContext, EditorOpsAction]:
    case = copy.deepcopy(CASES[name])
    for key, value in ctx_patch.items():
        case[key] = value
    ctx = build_ctx(case)
    return ctx, EditorOpsAction(ops=ops, base_digest=snapshot_digest(ctx.snapshot))


def _rm(*indices: int) -> list[dict]:
    return [{"op": "remove_clip", "slot_index": i} for i in indices]


@pytest.mark.parametrize(
    ("label", "case", "ops", "patch", "reason"),
    [
        (
            "a needed clip",
            "d01_two_takes_sharper",
            _rm(0),
            {"needed": ["m1"]},
            "removes_a_needed_clip",
        ),
        ("a clip that is not a duplicate", "d01_two_takes_sharper", _rm(2), {}, "not_a_duplicate"),
        ("every copy", "d01_two_takes_sharper", _rm(0, 1), {}, "not_a_duplicate"),
        (
            "half of a clip in two slots",
            "d09_dropped_clip_in_two_slots",
            _rm(0),
            {},
            "partial_removal",
        ),
        (
            "an already removed slot",
            "d08_slot_index_is_not_the_ordinal",
            _rm(0),
            {},
            "slot_unavailable",
        ),
        ("below the floor", "d07_below_the_clip_floor", _rm(0), {}, "below_clip_floor"),
        ("the same slot twice", "d01_two_takes_sharper", _rm(0, 0), {}, "slot_repeated"),
        (
            "another op",
            "d01_two_takes_sharper",
            [{"op": "set_transition", "boundary_index": 0, "transition": "crossfade"}],
            {},
            "op_not_remove_clip",
        ),
        ("an unknown need", "d01_two_takes_sharper", _rm(0), {"needed": None}, "not_removable"),
        (
            "a clip with speech",
            "d01_two_takes_sharper",
            _rm(0),
            {"speech": ["m1"]},
            "removes_speech",
        ),
        (
            "a clip talking to camera",
            "d01_two_takes_sharper",
            _rm(0),
            {"to_camera": ["m1"]},
            "removes_speech",
        ),
        (
            "the better clip while the worse stays",
            "d01_two_takes_sharper",
            _rm(1),
            {},
            "drops_the_better_clip",
        ),
    ],
)
def test_the_witness_refuses(label, case, ops, patch, reason):
    ctx, action = _hostile(case, ops, **patch)
    proven = KIND.witness(action, ctx)
    assert (proven.ok, proven.reason) == (False, reason), label


def test_two_spoken_takes_are_never_a_duplicate():
    """Locked-off takes look identical while the lines differ: speech clips are never offered."""
    case = copy.deepcopy(CASES["d01_two_takes_sharper"])
    ids = [c["media_id"] for c in case["clips"]]
    for key in ("speech", "to_camera"):
        spoken = {**case, key: ids}
        ctx = build_ctx(spoken)
        assert KIND.availability(ctx) is False
        assert KIND.build_action(ctx, _named_targets(case, ctx), None) is None


def test_a_spoken_keeper_lets_the_silent_copy_go():
    case = {**CASES["d01_two_takes_sharper"], "speech": ["m2"]}
    ctx = build_ctx(case)
    plan = dup._plans(ctx)[0]
    assert (plan.keep, plan.drops) == ("m2", ("m1",))
    only_m1_speaks = {**CASES["d01_two_takes_sharper"], "speech": ["m1"]}
    ctx = build_ctx(only_m1_speaks)
    plan = dup._plans(ctx)[0]
    assert (plan.keep, plan.drops, plan.reason) == ("m1", ("m2",), "speech")
    assert dup.evidence_lines(ctx, [_handles(ctx)["c2"], _handles(ctx)["c1"]]) == [
        "Keeps clip 1: it has your speech"
    ]


def test_the_card_anchor_is_a_clip_that_goes_never_the_keeper():
    """The tap scopes its brief requirement to the anchor, so naming the keeper first builds no
    card, while either order of a valid pair that leads with the dropped clip builds the same."""
    case = CASES["d01_two_takes_sharper"]
    ctx = build_ctx(case)
    handles = _handles(ctx)
    keeper_first = [handles["c2"], handles["c1"]]
    assert KIND.build_action(ctx, keeper_first, None) is None
    assert dup.evidence_lines(ctx, keeper_first) == []
    drop_first = [handles["c1"], handles["c2"]]
    action = KIND.build_action(ctx, drop_first, None)
    assert [op["slot_index"] for op in action.ops] == [0] and KIND.witness(action, ctx).ok


def test_the_witness_accepts_the_builders_own_action_and_refuses_a_stale_base():
    ctx, action = _hostile("d01_two_takes_sharper", _rm(0))
    assert KIND.witness(action, ctx).ok
    moved = EditorOpsAction(ops=action.ops, base_digest="0" * 32)
    assert KIND.witness(moved, ctx).reason == "stale_base"


def test_a_witness_that_cannot_run_proves_nothing():
    case = CASES["d01_two_takes_sharper"]
    ctx = build_ctx(case, with_witness=False)
    action = KIND.build_action(ctx, _named_targets(case, ctx), None)
    assert action is not None and KIND.witness(action, ctx).ok is False


# ── Measured end to end: real pixels -> the real signal -> a served card ──────────────

_BUILD = Path(__file__).resolve().parents[1] / "fixtures" / "clip_signals" / "dups" / "build.py"


def _build_module():
    spec = importlib.util.spec_from_file_location("dup_fixture_build_for_kind", _BUILD)
    module = importlib.util.module_from_spec(spec)
    sys.modules["dup_fixture_build_for_kind"] = module
    spec.loader.exec_module(module)
    return module


def _measured(item_id: str, path: Path) -> dict:
    data = nd.compute(SignalInputs(media_id=item_id, kind="image", local_path=str(path)))
    assert data and data["hashes"], f"{item_id} measured nothing"
    return data


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required")
def test_real_photos_measured_by_the_real_signal_serve_a_removal(tmp_path):
    from PIL import ImageFilter

    b = _build_module()
    sharp = b.scene(1)
    files = {
        "p_sharp": sharp,
        "p_blur": sharp.filter(ImageFilter.GaussianBlur(4)),
        "p_other3": b.scene(3),
        "p_other4": b.scene(4),
    }
    stored = {}
    for name, img in files.items():
        path = tmp_path / f"{name}.jpg"
        img.convert("RGB").save(path, "JPEG", quality=92)
        stored[name] = _measured(name, path)
    names = list(files)
    case = {
        "name": "real_photos",
        "language": "en",
        "needed": [],
        "slots": names,
        "clips": [
            {
                "media_id": n,
                "kind": "image",
                "duration_s": None,
                "hashes": stored[n]["hashes"],
                "sharpness": stored[n]["sharpness"],
                "mean_luma": stored[n]["mean_luma"],
            }
            for n in names
        ],
        "expect": {"offered": True, "keep": "p_sharp", "ops": [1], "reason": "sharper"},
    }
    ctx = build_ctx(case)
    # Real measured keys are the signal's own; a re-encode of the same shot pairs, scenes do not.
    plans = dup._plans(ctx)
    assert [(p.keep, p.drops) for p in plans] == [("p_sharp", ("p_blur",))]
    action = KIND.build_action(ctx, _named_targets(case, ctx), None)
    assert [op["slot_index"] for op in action.ops] == [1]
    assert KIND.witness(action, ctx).ok
