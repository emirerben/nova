"""Guards for the suggestion-blocks scaffold (exempt from the no-tests-after-code rule: each
pins an invariant the project names, none restates a computation).

Failure modes these catch, written before the scaffold:
 1. a registered kind with no witness / no declared surface ships a card nothing checked
 2. a kind module nobody registered (or a registered stub that starts offering)
 3. the engine's kind order moving, which re-ranks ties
 4. an import cycle between the engine, the registry and the kind modules
 5. a kind flag that is a typo (silently False) or that defaults on
 6. the model returning anything but a pick: an op, an unissued kind, an unissued target
 7. a stored signal outliving the thresholds it was measured under
 8. flag-off wire bytes drifting from the KRI-756 contract
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services import clip_signals
from app.services import suggestion_chips as sc
from app.services.clip_signals import registry as signal_registry
from app.services.suggestions import models as m
from app.services.suggestions import registry
from app.services.suggestions.builders import editor_ops_witness, no_action
from app.services.suggestions.context import snapshot_digest
from tests.evals import suggestion_chips_harness as h

KINDS_DIR = Path(registry.__file__).parent / "kinds"
STUBS = ("mute_clip", "filler_words", "duplicates", "generated_overlay")
LEGACY_ORDER = [
    "default_edit",
    "hook",
    "opener_clip",
    "captions",
    "sfx",
    "music",
    "transition",
    "title_animation",
    "pinned_text",
]


# ── registry: witness + surface, discovery, order ────────────────────────────────────


def test_every_registered_kind_has_a_witness_and_a_declared_surface():
    assert registry.problems() == []


def test_the_registry_guard_is_not_vacuous():
    real = registry.get("captions")
    assert registry.problems([dataclasses.replace(real, witness=None)]) == ["captions: no witness"]
    assert registry.problems([dataclasses.replace(real, surfaces=frozenset())]) == [
        "captions: no surface declared"
    ]
    assert registry.problems([dataclasses.replace(real, surfaces=frozenset({"tv"}))]) == [
        "captions: unknown surface ['tv']"
    ]
    assert registry.problems([dataclasses.replace(real, required_signals=("nope",))]) == [
        "captions: required signal not registered ['nope']"
    ]


def test_every_kind_module_is_in_the_explicit_list():
    from app.services.suggestions.kinds import KIND_MODULES

    on_disk = {p.stem for p in KINDS_DIR.glob("*.py") if p.stem != "__init__"}
    listed = {module.__name__.rsplit(".", 1)[1] for module in KIND_MODULES}
    assert on_disk == listed, "a kind module that is not in kinds/__init__.py is never registered"


def test_the_engine_gate_keeps_its_original_order():
    assert [str(k) for k in sc.CHIP_KIND_GATE] == LEGACY_ORDER
    assert set(sc.CHIP_KIND_GATE) == set(sc.ChipKind)
    assert [k.kind for k in registry.all_kinds()[: len(LEGACY_ORDER)]] == LEGACY_ORDER
    new = [k for k in registry.all_kinds() if k.kind not in LEGACY_ORDER]
    assert [k.kind for k in new] == list(STUBS)
    assert all(k.legacy is None for k in new), "a new kind must not join the legacy chip engine"


@pytest.mark.parametrize("name", STUBS)
def test_a_stub_kind_never_offers_and_never_builds_an_action(name):
    """Holds only while the kind is a stub; its lane replaces `no_action` and owns its own
    tests from then on."""
    kind = registry.get(name)
    if kind.build_action is not no_action:
        pytest.skip(f"{name} is implemented")
    for case in h.all_cases():
        ctx = h.build_context(case)
        assert kind.availability(ctx) is False, case.name
        assert kind.build_action(ctx, [], None) is None
    assert (
        kind.witness(m.ItemSettingAction(field="speech_cleanup_enabled", value=True), ctx).ok
        is False
    )


@pytest.mark.parametrize(
    "module",
    [
        "app.services.suggestions.kinds.hook",
        "app.services.suggestions.kinds",
        "app.services.suggestions.registry",
        "app.services.suggestions.models",
        "app.services.clip_signals",
        "app.services.suggestion_chips",
    ],
)
def test_each_entry_point_imports_first_without_a_cycle(module):
    env = {**os.environ, "STORAGE_BUCKET": "x", "DATABASE_URL": "postgresql://x/y"}
    done = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-800:]


def test_registry_kinds_match_what_the_chip_engine_actually_offers():
    """Ported kinds with a v2 action: the registry builds the op the engine's dry run proved,
    and the registry witness reaches the same edit targets. Over every ready replay fixture."""
    checked = 0
    for case in h.all_cases():
        ctx = h.build_context(case)
        if ctx.state != "ready":
            continue
        plan = sc.build_candidates(ctx)
        for cand in plan.levers:
            kind = registry.get(str(cand.kind))
            if kind.witness is not editor_ops_witness:
                continue
            action = kind.build_action(ctx, [cand.target], None)
            assert isinstance(action, m.EditorOpsAction), (case.name, cand.kind)
            proven = kind.witness(action, ctx)
            assert proven.ok and proven.touched == cand.touched, (case.name, cand.kind)
            moved = sc.SuggestionContext(**{**ctx.__dict__, "snapshot": {**ctx.snapshot, "x": 1}})
            assert kind.witness(action, moved).reason == "stale_base"
            checked += 1
    assert checked >= 8, "the fixtures stopped exercising the ported kinds"


# ── flags ────────────────────────────────────────────────────────────────────────────


def test_every_kind_flag_is_a_real_setting_that_defaults_off():
    for kind in registry.all_kinds():
        if kind.flag is None:
            continue
        assert kind.flag in Settings.model_fields, f"{kind.kind}: {kind.flag} is not a setting"
        assert Settings.model_fields[kind.flag].default is False
    for name in (
        "kria_suggestion_pinned_taps_enabled",
        "kria_suggestion_signals_enabled",
        "kria_suggestion_editor_triggers_enabled",
        "kria_suggestion_generated_overlay_enabled",
    ):
        assert Settings.model_fields[name].default is False, name
    assert Settings.model_fields["ai_openai_suggestions_budget_usd"].default == 0


def test_the_suggester_model_default_is_a_model_the_text_client_accepts():
    from app.agents._text_providers import TEXT_MODELS

    assert Settings.model_fields["kria_suggester_model"].default in TEXT_MODELS


def test_a_kind_that_reads_signals_needs_the_signals_flag():
    off = type("S", (), {"kria_suggestion_mute_clip_enabled": True})()
    on = type(
        "S",
        (),
        {"kria_suggestion_mute_clip_enabled": True, "kria_suggestion_signals_enabled": True},
    )()
    assert registry.enabled(registry.get("mute_clip"), settings=off) is False
    assert registry.enabled(registry.get("mute_clip"), settings=on) is True


# ── what the model may return ────────────────────────────────────────────────────────


def _strict_openai_problems(schema, path="$"):
    """The rules OpenAI strict mode enforces: closed objects, every property required."""
    out = []
    if schema.get("type") == "object":
        if schema.get("additionalProperties") is not False:
            out.append(f"{path}: additionalProperties must be false")
        if sorted(schema.get("required", [])) != sorted(schema.get("properties", {})):
            out.append(f"{path}: every property must be required")
    for key, child in (schema.get("properties") or {}).items():
        out += _strict_openai_problems(child, f"{path}.{key}")
    if isinstance(schema.get("items"), dict):
        out += _strict_openai_problems(schema["items"], f"{path}[]")
    return out


def test_the_agent_schema_is_strict_and_closed_over_what_was_issued():
    schema = m.agent_response_schema(["captions", "sfx"], ["c1", "c2"])
    assert _strict_openai_problems(schema) == []
    pick = schema["properties"]["ranked"]["items"]["properties"]
    assert pick["kind"]["enum"] == ["captions", "sfx"]
    assert pick["target_ids"]["items"]["enum"] == [m.WHOLE_VIDEO_HANDLE, "c1", "c2"]
    assert "ops" not in pick, "the agent has no field for an op"
    with pytest.raises(ValueError):
        m.agent_response_schema([], ["c1"])


def _pick(**over):
    base = {
        "kind": "captions",
        "target_ids": ["c1"],
        "why": "Speech in c1",
        "copy": None,
        "rank": 1,
    }
    return {**base, **over}


def _parse(ranked):
    return m.parse_agent_output(
        json.dumps({"ranked": ranked}), kinds=["captions", "sfx"], target_ids=["c1", "c2"]
    )


def test_a_well_formed_answer_parses_and_copy_may_be_text_or_null():
    out = _parse([_pick(), _pick(kind="sfx", target_ids=[], copy="Wind in c1", rank=2)])
    assert [p.kind for p in out.ranked] == ["captions", "sfx"]
    assert out.ranked[1].copy_text == "Wind in c1"


@pytest.mark.parametrize(
    "ranked, reason",
    [
        ([{**_pick(), "ops": [{"op": "set_mix"}]}], "schema"),
        ([_pick(kind="hook")], "unknown_kind"),
        ([_pick(target_ids=["c9"])], "unknown_target"),
        ([_pick(), _pick(kind="sfx")], "schema"),  # duplicate rank
        ([_pick(), _pick()], "schema"),  # same (kind, targets) twice
        ([_pick(rank=6)], "schema"),
        (
            [_pick(rank=i) | {"kind": "captions", "target_ids": [f"c{i}"]} for i in range(1, 7)],
            "schema",
        ),
        ([{k: v for k, v in _pick().items() if k != "why"}], "schema"),
    ],
)
def test_anything_but_an_issued_pick_voids_the_answer(ranked, reason):
    with pytest.raises(m.InvalidAgentOutput) as caught:
        _parse(ranked)
    assert caught.value.reason == reason


def test_a_model_answer_that_is_not_json_voids_the_answer():
    with pytest.raises(m.InvalidAgentOutput):
        m.parse_agent_output("not json", kinds=["captions"], target_ids=[])


# ── the Action union stays closed and server-side ────────────────────────────────────


def _suggestion(**over):
    base = {
        "id": "ss_1",
        "kind": "captions",
        "title": "Add captions",
        "prompt": "Add captions",
        "rank": 1,
        "action": {"type": "editor_ops", "ops": [{"op": "set_caption_meta"}], "base_digest": "d"},
    }
    return {**base, **over}


@pytest.mark.parametrize(
    "bad",
    [
        {"action": {"type": "shell", "cmd": "rm"}},
        {"action": {"type": "item_setting", "field": "title", "value": True}},
        {"action": {"type": "editor_ops", "ops": [], "base_digest": "d"}},
        {"action": {"type": "editor_ops", "ops": [{"op": "x"}], "base_digest": ""}},
        {
            "action": {
                "type": "brief_update",
                "updates": [{"operation": "explode"}],
                "expected_version": 1,
            }
        },
        {"action": {"type": "copy_decision", "role": "caption", "text": "hi"}},
        {"action": {"type": "async_generate", "spec": {}, "cost_usd": 50}},
        {"title": "x" * 41},
        {"evidence": ["a", "b", "c", "d"]},
    ],
)
def test_a_suggestion_cannot_carry_an_unvetted_action_or_oversize_text(bad):
    with pytest.raises(ValidationError):
        m.Suggestion.model_validate(_suggestion(**bad))


def test_the_action_never_reaches_the_wire_and_cost_does():
    paid = m.Suggestion.model_validate(
        _suggestion(
            kind="generated_overlay",
            action={"type": "async_generate", "spec": {"prompt": "x"}, "cost_usd": 0.04},
        )
    )
    wire = paid.to_wire().model_dump()
    assert wire["cost_usd"] == 0.04 and wire["action_type"] == "async_generate"
    assert not {"action", "witness_tier", "ops", "spec"} & set(wire)


def test_a_set_has_contiguous_ranks_and_unique_ids():
    digest = m.SuggestionDigest()
    first = m.Suggestion.model_validate(_suggestion())
    with pytest.raises(ValidationError):
        m.SuggestionSet(
            set_id="sg_1",
            digest=digest,
            state="ready",
            suggestions=[first.model_copy(update={"rank": 2})],
        )
    with pytest.raises(ValidationError):
        m.SuggestionSet(set_id="sg_1", digest=digest, state="ready", suggestions=[first, first])


# ── signals ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_signal():
    spec = signal_registry.register(
        signal_registry.SignalSpec(
            name="zz_temp", version=1, kinds=frozenset({"video"}), compute=lambda i: {"n": 1}
        )
    )
    yield spec
    signal_registry._REGISTRY.pop("zz_temp")


def test_each_signal_owns_its_key_and_nothing_else_moves(temp_signal):
    analysis = {"understanding": {"subject": "dog"}, "exposure": {"mean": 0.4}}
    merged = clip_signals.merge_signals(analysis, {"zz_temp": {"n": 1}})
    assert merged["understanding"] == analysis["understanding"]
    assert merged["exposure"] == analysis["exposure"]
    again = clip_signals.merge_signals(merged, {"zz_temp": {"n": 2}})
    assert again["signals"]["zz_temp"] == {"v": 1, "data": {"n": 2}}
    assert analysis == {"understanding": {"subject": "dog"}, "exposure": {"mean": 0.4}}
    with pytest.raises(KeyError):
        clip_signals.merge_signals(analysis, {"unregistered": {}})


def test_a_signal_measured_under_old_thresholds_is_treated_as_missing(temp_signal):
    merged = clip_signals.merge_signals({}, {"zz_temp": {"n": 1}})
    assert clip_signals.read_signals(merged) == {"zz_temp": {"n": 1}}
    missing = clip_signals.missing_signals(merged, "video")
    assert "zz_temp" not in missing and {"audio_quality", "near_dup_hash"} <= set(missing)
    before = clip_signals.signals_version()
    signal_registry._REGISTRY["zz_temp"] = dataclasses.replace(temp_signal, version=2)
    assert clip_signals.read_signals(merged) == {}
    assert "zz_temp" in clip_signals.missing_signals(merged, "video")
    assert clip_signals.signals_version() != before, "a bump must invalidate served cards"


def test_one_failing_signal_never_takes_the_others_down(temp_signal):
    boom = signal_registry.register(
        signal_registry.SignalSpec(
            name="zz_boom", version=1, kinds=frozenset({"video"}), compute=lambda i: 1 / 0
        )
    )
    try:
        out = clip_signals.compute_signals(clip_signals.SignalInputs(media_id="m", kind="video"))
    finally:
        signal_registry._REGISTRY.pop(boom.name)
    assert out["zz_temp"] == {"n": 1} and "zz_boom" not in out


def test_signal_stubs_are_registered_but_not_implemented():
    assert {s.name for s in clip_signals.specs() if not s.implemented} >= {
        "audio_quality",
        "near_dup_hash",
    }
    out = clip_signals.compute_signals(clip_signals.SignalInputs(media_id="m", kind="video"))
    assert not {"audio_quality", "near_dup_hash"} & set(out)


def test_backfill_selects_by_signals_alone_never_by_understanding_state():
    from app.cli import backfill_clip_signals
    from app.services.clip_signals.backfill import rows_needing_signals

    understood = {"understanding": {"subject": "dog"}, "understanding_attempts": 1}
    every_signal = {name: {"x": 1} for name in clip_signals.names()}
    done = clip_signals.merge_signals(understood, every_signal)
    rows = [
        {"media_id": "a", "gcs_path": "p/a", "kind": "video", "analysis": understood},
        {"media_id": "b", "gcs_path": "p/b", "kind": "video", "analysis": done},
        {"media_id": "c", "gcs_path": "p/c", "kind": "image", "analysis": None},
        {"media_id": "d", "gcs_path": "", "kind": "video", "analysis": understood},
        {"gcs_path": "p/e", "kind": "video"},
        {"media_id": "f", "gcs_path": "p/f", "kind": "audio"},
        "not a row",
    ]
    pending = {p.media_id: p.missing for p in rows_needing_signals(rows)}
    assert pending == {
        "a": tuple(clip_signals.missing_signals(understood, "video")),
        "c": tuple(clip_signals.missing_signals(None, "image")),
    }
    assert pending["a"], "an understood clip is still pending until it has signals"
    assert backfill_clip_signals.main(["--write"]) == 2, "no fenced store is wired: refuse"


# ── wire: flag off is byte-identical to KRI-756 ──────────────────────────────────────

# `unavailable_set().model_dump_json()` captured from the tree before the v2 fields existed.
_KRI756_UNAVAILABLE_JSON = (
    '{"schema_version":1,"set_id":"","state":"none","status":"unavailable","understanding":"empty",'
    '"retry_after_s":null,"wording":"fallback","anchor":{"media_digest":"","manifest_hash":null,'
    '"render_generation_id":null,"variant_id":null},"chips":[]}'
)


def test_a_set_without_suggestions_serializes_exactly_as_kri756_did():
    assert sc.unavailable_set().model_dump_json() == _KRI756_UNAVAILABLE_JSON
    assert "suggestions" not in sc.SuggestionSetOut().model_dump()
    assert "digest" not in sc.SuggestionSetOut().model_dump(mode="json")


def test_a_set_with_suggestions_adds_only_the_new_keys():
    paid = m.Suggestion.model_validate(_suggestion())
    out = sc.SuggestionSetOut(
        suggestions=[paid.to_wire()], digest=m.SuggestionDigest(state="ready")
    )
    dumped = json.loads(out.model_dump_json())
    legacy = json.loads(_KRI756_UNAVAILABLE_JSON)
    legacy["status"] = "unavailable"
    assert {k: v for k, v in dumped.items() if k not in ("suggestions", "digest")} == legacy
    assert dumped["suggestions"][0]["id"] == "ss_1" and dumped["digest"]["state"] == "ready"


def test_snapshot_digest_moves_with_any_snapshot_change_and_not_with_key_order():
    assert snapshot_digest(None) is None
    assert snapshot_digest({"a": 1, "b": 2}) == snapshot_digest({"b": 2, "a": 1})
    assert snapshot_digest({"a": 1}) != snapshot_digest({"a": 2})
