"""Guard: a phone editor action no group names has no answer to "can the agent do this?".

Added with 29 Ekim lane E (KRI-1039). Ways this can fail, and the assertion that catches each:
* a new public function on the editor session is not in any group (the drift this exists for);
* a group names a function that no longer exists (stale rows hide the first failure);
* an ``agent_ops`` group names an op the copilot does not have, or none;
* an ``agent_unsupported`` / ``not_an_edit`` group has no written reason;
* one function sits in two groups (the verdict would depend on dict order).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.agents.edit_copilot import _VALID_OPS
from app.kria.editor_capability_parity import PARITY_GROUPS, grouped_actions

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
        else:
            assert group.reason.strip(), f"{name} must say why the agent does not do this"


def test_an_action_is_in_one_group_only() -> None:
    grouped_actions()  # raises on a duplicate
