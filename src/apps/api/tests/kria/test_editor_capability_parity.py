"""Guard: a phone editor action no group names has no answer to "can the agent do this?".

Added with 29 Ekim lane E (KRI-1039). Ways this can fail, and the assertion that catches each:
* a new public function on the editor session is not in any group (the drift this exists for);
* a group names a function that no longer exists (stale rows hide the first failure);
* an ``agent_ops`` group names an op the copilot does not have, or none;
* an ``agent_ops`` group names a field none of its ops accepts (the op exists, the edit does not);
* an ``agent_unsupported`` / ``not_an_edit`` group has no written reason;
* one function sits in two groups (the verdict would depend on dict order).

KRI-1388 added the phone proof, because seven groups passed every check above and still could not
run from iPhone chat. Ways that can fail, each an assertion below:
* an op of an ``agent_ops`` group has no phone probe, so nothing proves it (the claim check);
* the parser drops it on a phone snapshot: closed family, empty pool, missing state;
* the compiler refuses it on the variant the snapshot came from: no handler, a lane the device
  never has (a base video), a field the handler ignores;
* a field the group names is never sent by a passing probe, so the op runs but the edit does not;
* an ``agent_unsupported`` group starts compiling on a phone and the table understates it.
Probes run under the production settings profile, with the capability map a phone advertises
when the lane gate is open (``author_action_fixtures.PHONE_CAPS``).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents.edit_copilot import (
    _CAPTION_META_FIELD_GATES,
    _CAPTION_META_FIELDS,
    _OP_FIELDS,
    _VALID_OPS,
)
from app.agents.editor_ops_v2.timeline import _PATCH_KEYS as _PATCH_SLOTS_KEYS
from app.kria.editor_capability_parity import PARITY_GROUPS, grouped_actions
from app.services.kria_editor_ops import COPILOT_EDITOR_CAPABILITIES
from tests.evals.request_following.author_action_fixtures import (
    PHONE_OP_PROBES,
    SCENARIOS,
    Scenario,
    build,
)

# The fields inside an op's `patch` that the copilot parser keeps.
_PATCH_FIELDS = {"set_caption_meta": _CAPTION_META_FIELDS, "patch_slots": _PATCH_SLOTS_KEYS}
# Patch fields the parser keeps only where the snapshot carries an editor capability.
_GATED_FIELDS = {"set_caption_meta": _CAPTION_META_FIELD_GATES}

FEATURES = Path(__file__).resolve().parents[4] / "apps" / "ios" / "Kria" / "Features"
# The session file and every file that extends it. An extension file is read from its
# `extension NativeEditorSession` line, so protocol requirements above it are not counted.
SWIFT_FILES = (
    "NativeEditorSession.swift",
    "NativeEditorRetiming.swift",
    "NativeTextEditing.swift",
)
_SESSION_DECL = re.compile(r"^extension NativeEditorSession\b", re.M)
_FUNC = re.compile(
    r"^    (?:@discardableResult )?(?:@MainActor )?(?:public |internal )?(?:nonisolated )?"
    r"func ([A-Za-z0-9_]+)",
    re.MULTILINE,
)
_PRIVATE_FUNC = re.compile(
    r"^    (?:private|fileprivate) [^\n]*?func ([A-Za-z0-9_]+)", re.MULTILINE
)


def _session_source() -> str:
    sources = []
    for name in SWIFT_FILES:
        path = FEATURES / name
        if not path.exists():
            pytest.skip("iOS sources are not in this checkout")
        source = path.read_text()
        if name != SWIFT_FILES[0]:
            match = _SESSION_DECL.search(source)
            assert match, f"{name} no longer extends NativeEditorSession"
            source = source[match.start() :]
        sources.append(source)
    return "\n".join(sources)


def _public_actions() -> set[str]:
    source = _session_source()
    private = set(_PRIVATE_FUNC.findall(source))
    declared = set(_FUNC.findall(source))
    # An overload is public when any of its declarations is.
    return {name for name in declared if name not in private or _has_public_overload(source, name)}


def _has_public_overload(source: str, name: str) -> bool:
    lines = [ln for ln in source.splitlines() if re.search(rf"\bfunc {re.escape(name)}\b", ln)]
    return any(not re.match(r"^\s+(?:private|fileprivate)\b", ln) for ln in lines)


def test_ios_editor_actions_have_agent_ops() -> None:
    mapped = set(grouped_actions())
    actions = _public_actions()
    missing = sorted(actions - mapped)
    assert not missing, (
        "The editor session (or an extension of it) gained public functions with no parity group: "
        f"{missing}. Add each to a group in app/kria/editor_capability_parity.py: the agent op(s) "
        "that do it, an agent_unsupported reason, or not_an_edit."
    )
    stale = sorted(mapped - actions)
    assert not stale, f"groups name functions that are gone from the editor session: {stale}"


def test_every_file_extending_the_session_is_scanned() -> None:
    if not FEATURES.exists():
        pytest.skip("iOS sources are not in this checkout")
    extenders = {
        path.name for path in FEATURES.glob("*.swift") if _SESSION_DECL.search(path.read_text())
    }
    assert extenders <= set(SWIFT_FILES), (
        f"{sorted(extenders - set(SWIFT_FILES))} extend NativeEditorSession: add them to "
        "SWIFT_FILES so their public functions need a capability-parity group"
    )


def test_every_group_states_its_answer() -> None:
    for name, group in PARITY_GROUPS.items():
        assert group.kind in {"agent_ops", "agent_unsupported", "not_an_edit"}, name
        assert group.actions, name
        if group.kind == "agent_ops":
            assert group.ops, f"{name} says the agent can do it but names no op"
            unknown = sorted(set(group.ops) - set(_VALID_OPS))
            assert not unknown, f"{name} names ops the copilot does not have: {unknown}"
            for field in group.fields:
                accepting = [
                    op
                    for op in group.ops
                    if field in _OP_FIELDS.get(op, ()) or field in _PATCH_FIELDS.get(op, ())
                ]
                assert accepting, (
                    f"{name} says {group.ops} set {field!r}, but none of them accepts it: "
                    "add the field to the op's parser or move the actions to agent_unsupported"
                )
                for op in accepting:
                    gate = _GATED_FIELDS.get(op, {}).get(field)
                    assert gate is None or gate.capability in COPILOT_EDITOR_CAPABILITIES, (
                        f"{op}.{field} needs editor capability {gate.capability!r}, which the "
                        "copilot snapshot never carries, so the agent can never set it"
                    )
        else:
            assert group.reason.strip(), f"{name} must say why the agent does not do this"


def test_an_action_is_in_one_group_only() -> None:
    grouped_actions()  # raises on a duplicate


PROBES: list[Scenario] = [*SCENARIOS, *PHONE_OP_PROBES]
CLAIMS = sorted(
    (name, op)
    for name, group in PARITY_GROUPS.items()
    if group.kind == "agent_ops"
    for op in group.ops
)


def _probes(group: str, op: str) -> list[Scenario]:
    return [p for p in PROBES if p.group == group and p.reference[0]["op"] == op]


def _sends(op: dict, field: str) -> bool:
    patch = op.get("patch")
    return field in op or (isinstance(patch, dict) and field in patch)


def test_every_probe_names_a_parity_group() -> None:
    assert not sorted({p.group for p in PROBES} - set(PARITY_GROUPS))


@pytest.mark.parametrize(("group", "op"), CLAIMS, ids=[f"{g}.{o}" for g, o in CLAIMS])
def test_every_agent_op_runs_from_iphone_chat(group: str, op: str, prod_profile) -> None:
    probes = _probes(group, op)
    assert probes, (
        f"{group} says the agent does {op} but no phone probe proves it: add one to "
        "PHONE_OP_PROBES in tests/evals/request_following/author_action_fixtures.py"
    )
    fields = [
        field
        for field in PARITY_GROUPS[group].fields
        if field in _OP_FIELDS.get(op, ()) or field in _PATCH_FIELDS.get(op, ())
    ]
    unsent = [f for f in fields if not any(_sends(p.reference[0], f) for p in probes)]
    assert not unsent, f"{group}.{op}: no phone probe sends {unsent}"
    for probe in probes:
        built = build(probe)
        verdict = built["probe"]
        assert verdict["parsed_ops"] == [op] and not verdict["parse_reasons"], (
            f"{group}.{op} is dropped by the parser on a phone snapshot "
            f"(families {verdict['families']}): {verdict['parse_reasons']}"
        )
        kept = [f for f in fields if _sends(probe.reference[0], f) and _sends(built["ops"][0], f)]
        sent = [f for f in fields if _sends(probe.reference[0], f)]
        assert kept == sent, f"{group}.{op}: the parser dropped {sorted(set(sent) - set(kept))}"
        assert verdict["compiles"] is True, (
            f"{group}.{op} parses on a phone but does not compile: {verdict.get('compile_error')}"
        )
        compiled = built["compiled"]
        assert compiled.before or not compiled.diff.empty(), f"{group}.{op} compiles to no change"


UNSUPPORTED_PROBES = [
    p for p in PROBES if p.group in PARITY_GROUPS and PARITY_GROUPS[p.group].kind != "agent_ops"
]


@pytest.mark.parametrize("probe", UNSUPPORTED_PROBES, ids=lambda p: p.fixture_id)
def test_an_unsupported_group_is_refused_on_a_phone(probe: Scenario, prod_profile) -> None:
    verdict = build(probe)["probe"]
    assert not (verdict["parsed_ops"] and verdict.get("compiles") is True), (
        f"{probe.reference[0]['op']} now runs from iPhone chat: move {probe.group} to agent_ops "
        "in app/kria/editor_capability_parity.py"
    )
