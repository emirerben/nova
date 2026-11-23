"""KRI-1585: informative text roles and style provenance, behind INFORMATIVE_TEXT_DESIGNS_ENABLED.

The flag is read once, when the job's phone text policy is stamped (``POLICY_KEY``). Every later
pass branches on that stamp, so flipping the flag never restyles a job already made.

A style field on a text row has one of three owners, strongest first:

1. ``creator_style_fields``: the creator set it (editor save, chat edit, an ask in words).
2. ``design_style_fields``: a design or named look the creator chose wrote it.
3. Neither: saved preferences, this turn's request and the resolver's automatic picks decide.

A design never writes over a creator field, and the resolver keeps both recorded tiers.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

POLICY_KEY = "informative_text_designs"
# KRI-1581: the job's approving app build draws the designs (`text_designs_planned_for`).
SHAPES_KEY = "informative_text_shapes"
CREATOR_FIELDS_KEY = "creator_style_fields"
DESIGN_FIELDS_KEY = "design_style_fields"
DESIGN_ID_KEY = "text_design"
# Fields a chat ``add_text`` filled from its own defaults, not from her words.
DEFAULT_FIELDS_KEY = "default_style_fields"
HOOK_ROLE = "hook"
RESEARCH_ROLE = "research"

# The fields a creator can set on a text row that a later style pass could otherwise change.
STYLE_FIELDS = (
    "alignment",
    "color",
    "font_family",
    "glow_strength",
    "letter_spacing",
    "line_spacing",
    "max_width_frac",
    "position",
    "scrim",
    "shadow_blur",
    "shadow_color",
    "shadow_enabled",
    "shadow_offset_y",
    "shadow_opacity",
    "size_px",
    "stroke_color",
    "stroke_width",
    "text_case",
    "x_frac",
    "y_frac",
)

_ACTIVE: ContextVar[bool] = ContextVar("informative_text_designs", default=False)
_SHAPES: ContextVar[bool] = ContextVar("informative_text_shapes", default=False)


def policy_enabled(policy: object) -> bool:
    return isinstance(policy, dict) and policy.get(POLICY_KEY) is True


def shapes_enabled(policy: object) -> bool:
    return policy_enabled(policy) and isinstance(policy, dict) and policy.get(SHAPES_KEY) is True


def stamp_policy(policy: dict, *, client_protocol: int | None = None) -> dict:
    """Add the stamp to a policy being created now, only when the flag is on.

    ``client_protocol`` is the approving app build. The designs themselves (KRI-1581) are
    stamped only for a build that draws them; an older one keeps plain informative rows.
    """
    from app.config import settings

    if settings.informative_text_designs_enabled:
        policy[POLICY_KEY] = True
        from app.services.phone_rollout import text_designs_planned_for  # noqa: PLC0415

        if text_designs_planned_for(client_protocol):
            policy[SHAPES_KEY] = True
    return policy


@contextmanager
def informative_text_scope(enabled: bool, *, shapes: bool = False) -> Iterator[None]:
    token = _ACTIVE.set(bool(enabled))
    shapes_token = _SHAPES.set(bool(enabled) and bool(shapes))
    try:
        yield
    finally:
        _SHAPES.reset(shapes_token)
        _ACTIVE.reset(token)


def informative_text_active() -> bool:
    return _ACTIVE.get()


def informative_shapes_active() -> bool:
    return _SHAPES.get()


def informative_role(row: dict) -> str | None:
    """``hook`` or ``research`` for an informative row while the stamp is active, else None."""
    if not _ACTIVE.get():
        return None
    source = row.get("source_params") or {}
    if source.get("phone_text_role") == HOOK_ROLE:
        return HOOK_ROLE
    if isinstance(source.get("research"), dict):
        return RESEARCH_ROLE
    return None


def creator_fields(row: dict) -> set[str]:
    return set((row.get("source_params") or {}).get(CREATOR_FIELDS_KEY) or [])


def authored_fields(row: dict) -> set[str]:
    """Fields a style pass keeps as they are: the creator's and the chosen design's."""
    source = row.get("source_params") or {}
    return set(source.get(CREATOR_FIELDS_KEY) or []) | set(source.get(DESIGN_FIELDS_KEY) or [])


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) < 1e-9
    return a == b


def mark_default_fields(row: dict, fields: Iterable[str], policy: object) -> None:
    """On a stamped job, note which of a new row's style fields are only defaults, so the Save
    that commits it does not make them hers (KRI-1584: the Playfair default stays a default)."""
    defaults = sorted(set(fields) & set(STYLE_FIELDS))
    if policy_enabled(policy) and defaults:
        row["source_params"] = {**(row.get("source_params") or {}), DEFAULT_FIELDS_KEY: defaults}


def record_creator_edits(previous: Iterable[dict], rows: list[dict]) -> list[dict]:
    """Mark the style fields each saved row changed against the row it replaces.

    A row with no previous row of the same id is new: every style field it carries is the
    creator's. A field the creator set on an earlier save stays hers, even when the client sent
    the row back without its ``source_params``. Rows are changed in place and returned.
    """
    before = {row.get("id"): row for row in previous if isinstance(row, dict) and row.get("id")}
    for row in rows:
        if not isinstance(row, dict):
            continue
        prior = before.get(row.get("id"))
        if prior is None:
            defaults = set((row.get("source_params") or {}).get(DEFAULT_FIELDS_KEY) or [])
            changed = {
                field
                for field in STYLE_FIELDS
                if row.get(field) is not None and field not in defaults
            }
        else:
            changed = {
                field for field in STYLE_FIELDS if not _same(row.get(field), prior.get(field))
            }
        source = dict(row.get("source_params") or {})
        kept = set(source.get(CREATOR_FIELDS_KEY) or [])
        if prior is not None:
            kept |= set((prior.get("source_params") or {}).get(CREATOR_FIELDS_KEY) or [])
        if not kept | changed or sorted(kept | changed) == source.get(CREATOR_FIELDS_KEY):
            continue
        source[CREATOR_FIELDS_KEY] = sorted(kept | changed)
        row["source_params"] = source
    return rows


def apply_design_values(row: dict, values: dict[str, Any], design_id: str) -> dict:
    """Write a chosen design's values onto ``row``, never over a field the creator set."""
    source = dict(row.get("source_params") or {})
    creator = set(source.get(CREATOR_FIELDS_KEY) or [])
    written = set()
    for field, value in values.items():
        if field not in STYLE_FIELDS:
            raise ValueError(f"{field} is not a text style field")
        if field in creator:
            continue
        row[field] = value
        written.add(field)
    source[DESIGN_FIELDS_KEY] = sorted(written)
    source[DESIGN_ID_KEY] = design_id
    row["source_params"] = source
    return row
