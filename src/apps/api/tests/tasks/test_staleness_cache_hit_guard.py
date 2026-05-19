"""Source-inspection tests pinning the cache-hit guard around
`recipe_cached_versions` writes in both the manual and agentic build tasks.

The shipped /admin STALE badge (PR #250) writes the live
`AgentSpec.prompt_version` map onto the template row whenever
`recipe_cached` is rewritten. On a Redis cache hit, the recipe is reused
verbatim from a prior run — its underlying agent runs did NOT execute this
invocation, so the prompts that produced it may already be stale.

Without the guard, a reanalyze that hits cache would falsely clear the
STALE badge by stamping current prompt versions onto a recipe that those
prompts never produced. Operators would see "fresh" and trust output that
is in fact pre-fix.

These tests run as text-grep contracts on the task source files because:
- Full task integration tests would need GCS, Gemini, and 5+ agents wired.
- The behavior is structural ("the assignment is inside an `if agents_ran:`
  block, not unconditional") and a text-pattern check is the cheapest way
  to lock that invariant against future refactors.
"""

from __future__ import annotations

import inspect

from app.tasks import agentic_template_build, template_orchestrate


def _normalize_whitespace(src: str) -> str:
    """Collapse runs of whitespace so token-order checks ignore formatting drift."""
    return " ".join(src.split())


def test_agentic_versions_write_is_gated_by_agents_ran():
    """On Redis cache hit, `agents_ran` must stay False so the versions snapshot
    is not overwritten with current AgentSpec values."""
    src = inspect.getsource(agentic_template_build)

    assert "agents_ran = False" in src, (
        "agentic_template_build must initialize `agents_ran = False` before the "
        "cache check so cache-hit paths leave the snapshot alone."
    )
    assert "agents_ran = True" in src, (
        "agentic_template_build must flip `agents_ran = True` inside the "
        "cache-miss branch — otherwise no path ever refreshes the snapshot."
    )

    # The version write must be inside an `if agents_ran:` block. We look for
    # the conditional + the version assignment in close proximity so the test
    # is robust to surrounding code shifts.
    normalized = _normalize_whitespace(src)
    assert "if agents_ran:" in normalized, (
        "agentic_template_build must guard the recipe_cached_versions write "
        "with `if agents_ran:` so cache-hit paths skip it."
    )
    guard_idx = normalized.find("if agents_ran:")
    version_idx = normalized.find("template.recipe_cached_versions = capture_recipe_versions")
    assert version_idx > guard_idx, (
        "recipe_cached_versions assignment must follow the `if agents_ran:` guard, not precede it."
    )


def test_agents_ran_flip_is_inside_cache_miss_branch():
    """`agents_ran = True` must follow the `if recipe is None:` branch (the
    cache-miss path) so it only flips when agents actually ran. A misplaced
    flip outside that branch would re-introduce the false-clear bug."""
    src = inspect.getsource(agentic_template_build)
    # Find the cache-miss branch and the agents_ran flip; the flip must come
    # after the branch opener.
    miss_idx = src.find("if recipe is None:")
    flip_idx = src.find("agents_ran = True")
    assert miss_idx >= 0, "cache-miss branch (`if recipe is None:`) not found"
    assert flip_idx > miss_idx, (
        "`agents_ran = True` must live inside the cache-miss branch — "
        "found it before or outside `if recipe is None:`."
    )


def test_manual_versions_write_is_gated_by_agents_ran():
    """Same guard, manual analyze_template_task path."""
    src = inspect.getsource(template_orchestrate)

    assert "agents_ran = False" in src, (
        "template_orchestrate must initialize `agents_ran = False` before the cache check."
    )
    assert "agents_ran = True" in src, (
        "template_orchestrate must flip `agents_ran = True` inside the cache-miss branch."
    )

    normalized = _normalize_whitespace(src)
    assert "if agents_ran:" in normalized, (
        "template_orchestrate must guard the recipe_cached_versions write with `if agents_ran:`."
    )

    # Locate the manual-path version write specifically (the audio_only branch
    # writes `recipe_cached_versions = {}` and is intentionally unguarded).
    target = "template.recipe_cached_versions = capture_recipe_versions"
    guard_idx = normalized.rfind("if agents_ran:")  # the manual-path one
    version_idx = normalized.find(target)
    assert version_idx > guard_idx, (
        "manual-path recipe_cached_versions assignment must follow the `if agents_ran:` guard."
    )


def test_manual_agents_ran_flip_is_inside_cache_miss_branch():
    src = inspect.getsource(template_orchestrate)
    miss_idx = src.find("if recipe is None:")
    flip_idx = src.find("agents_ran = True")
    assert miss_idx >= 0, "manual-path cache-miss branch not found"
    assert flip_idx > miss_idx, (
        "`agents_ran = True` must live inside the cache-miss branch in template_orchestrate."
    )
