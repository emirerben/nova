"""Audit: literal text overlays in seed scripts must declare substitution intent.

If `_is_subject_placeholder(text)` is True for a literal overlay AND the
overlay does not explicitly opt out (`subject_substitute: False`) or in
(`subject_template` / `subject_part` / `subject_substitute: True`), the
heuristic in `template_orchestrate._resolve_overlay_text` will silently rewrite
the text to the user's subject or to `clip_meta.hook_text`. The test fails on
the PR when a new template forgets to declare intent.

REGRESSION: Rule of Thirds job a1091488-09f6-4ce0-b92e-b1cc52695c9c
(2026-05-13) shipped without the opt-out and rendered "pilot in cockpit"
instead of "The" and "Thirds". See plan
~/.claude/plans/the-rule-of-third-floating-thompson.md.

Allow-list: Brazil's "PERU" intentionally relies on the all-caps branch of
the heuristic to swap in the user's location at render time. Adding a new
entry requires a comment justifying the implicit-substitution choice.
"""
from __future__ import annotations

import importlib
import pathlib
from collections.abc import Iterable, Iterator

import pytest

from app.tasks.template_orchestrate import _is_subject_placeholder

# Repo path: tests/scripts/this_file.py -> src/apps/api/scripts
SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[2] / "scripts"

# (script_name, text) tuples for overlays that deliberately rely on the
# heuristic. Each entry needs a justification — the audit test exists to make
# adding a new entry a deliberate, reviewed decision.
GRANDFATHERED: set[tuple[str, str]] = {
    # Brazil renames the "PERU" label to the user's uploaded location via
    # the all-caps branch of _is_subject_placeholder. The template is built
    # around this behavior; flipping to explicit opt-in would require a
    # parallel migration of the cached recipe in production.
    ("seed_dimples_passport_brazil", "PERU"),
}


def _overlay_text(overlay: dict) -> str:
    """Return the candidate text the heuristic would see for this overlay."""
    return overlay.get("sample_text") or overlay.get("text") or ""


def _has_substitution_decision(overlay: dict) -> bool:
    """True if the overlay declares its substitution intent explicitly."""
    if overlay.get("subject_substitute") is False:
        return True  # explicit opt-out
    if overlay.get("subject_substitute") is True:
        return True  # explicit opt-in
    if overlay.get("subject_template"):
        return True  # explicit subject-interpolation template
    if overlay.get("subject_part"):
        return True  # explicit subject-slice
    return False


def _walk_overlays(source: dict | list) -> Iterator[dict]:
    """Yield every overlay dict from either a recipe-shaped dict or a
    flat list of overlay specs."""
    if isinstance(source, list):
        for entry in source:
            if isinstance(entry, dict):
                yield entry
        return
    if not isinstance(source, dict):
        return
    for slot in source.get("slots", []) or []:
        for overlay in slot.get("text_overlays", []) or []:
            if isinstance(overlay, dict):
                yield overlay


def _discover_seed_overlay_sources() -> Iterable[tuple[str, callable]]:
    """Auto-discover seed scripts that publish text overlays.

    Two shapes supported:
      - `build_recipe() -> dict` (slots/text_overlays nested) — used by
        seed_*.py scripts that materialize a full recipe.
      - module attribute `INTRO_OVERLAYS: list[dict]` — used by
        add_*_intro_overlays.py patch scripts.

    Returned tuple: (test id, callable that returns the overlay source).
    """
    items: list[tuple[str, callable]] = []
    for path in sorted(SCRIPTS_DIR.glob("seed_*.py")):
        mod_name = path.stem
        mod = importlib.import_module(f"scripts.{mod_name}")
        if hasattr(mod, "build_recipe"):
            items.append((mod_name, mod.build_recipe))
    for path in sorted(SCRIPTS_DIR.glob("add_*_intro_overlays.py")):
        mod_name = path.stem
        mod = importlib.import_module(f"scripts.{mod_name}")
        if hasattr(mod, "INTRO_OVERLAYS"):
            items.append((mod_name, lambda m=mod: m.INTRO_OVERLAYS))
    return items


_SEED_SOURCES = list(_discover_seed_overlay_sources())


@pytest.mark.parametrize(
    "script_name,get_source",
    _SEED_SOURCES,
    ids=[name for name, _ in _SEED_SOURCES],
)
def test_seed_overlays_declare_substitution_intent(script_name, get_source):
    """For every overlay whose text matches the placeholder heuristic, the
    overlay must declare intent (opt-out OR explicit opt-in) OR be on the
    grandfathered allow-list with a documented reason."""
    source = get_source()
    offenders: list[str] = []
    for overlay in _walk_overlays(source):
        text = _overlay_text(overlay)
        if not text:
            continue
        if not _is_subject_placeholder(text):
            continue
        if (script_name, text) in GRANDFATHERED:
            continue
        if _has_substitution_decision(overlay):
            continue
        offenders.append(text)
    assert not offenders, (
        f"{script_name}: overlays {offenders!r} match the placeholder "
        "heuristic but declare no substitution intent. Add "
        '`"subject_substitute": False` to lock the literal text, or '
        '`"subject_substitute": True` if the heuristic-driven substitution '
        "is intentional, or grandfather the overlay in GRANDFATHERED with a "
        "comment. See plan "
        "~/.claude/plans/the-rule-of-third-floating-thompson.md for context."
    )


def test_discovery_found_known_seed_scripts():
    """Meta-guard: if a refactor moves seed scripts out of the directory
    or renames them, this test fails before the audit silently passes by
    finding zero scripts to audit."""
    names = {name for name, _ in _SEED_SOURCES}
    expected = {
        "seed_rule_of_thirds",
        "seed_dimples_passport_brazil",
        "seed_how_do_you_enjoy_your_life",
        "add_waka_waka_intro_overlays",
    }
    missing = expected - names
    assert not missing, (
        f"audit discovery missed expected seed scripts: {missing}. "
        "Either the script was renamed/moved or build_recipe()/INTRO_OVERLAYS "
        "was removed. Update _discover_seed_overlay_sources or the expected set."
    )
