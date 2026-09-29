"""Editor ops v2, audio lane (KRI-219). Stub: the lane PR fills this in.

Do not import app.agents.edit_copilot / app.services.kria_editor_ops at module
top level (circular); import lazily inside functions.
"""

from __future__ import annotations

from app.agents.editor_ops_v2 import OpSpec

SPECS: list[OpSpec] = []


def register_handlers() -> None:
    """Call `kria_editor_ops.register_handler(name, fn)` per op (lazy import)."""
