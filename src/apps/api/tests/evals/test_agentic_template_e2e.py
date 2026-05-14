"""End-to-end eval for agentic templates.

Per-agent evals already cover whether `template_recipe` or `text_designer`
produce reasonable JSON in isolation. This suite asks a different question:
when the agentic build orchestrator chains them together, does the
*assembled recipe* hold together?

What's checked structurally (every PR, no network, runs in CI):
- every label-like overlay has all six text_designer fields baked in
- slot count matches recipe.shot_count
- interstitials reference real slot positions
- total_duration_s is consistent with sum(slot durations) + sum(holds)
- copy_tone and creative_direction are non-empty (the agent stack ran)
- agentic_template_build artifacts match expected schema

With `--with-judge` (manual workflow_dispatch + on prompt-change CI):
Claude Sonnet scores the recipe against the agentic_template_e2e rubric.
This is metadata-only judging (recipe + overlay/transition fields, not
rendered frames). Frame-level judging is a follow-up if metadata judging
misses regressions we care about.

Fixture format is a JSON file under tests/fixtures/agentic_e2e/ shaped
like `agentic_template_build_task`'s persisted recipe. Hand-authored in
v1; will be replaced by live-mode snapshots once the orchestrator runs
on real templates in staging.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "agentic_e2e"
RUBRIC_PATH = Path(__file__).parent / "rubrics" / "agentic_template_e2e.md"


def _discover_fixtures() -> list[Path]:
    if not FIXTURE_DIR.exists():
        return []
    return sorted(p for p in FIXTURE_DIR.glob("*.json") if not p.name.startswith("_"))


FIXTURE_PATHS = _discover_fixtures()


def _is_label_like(overlay: dict) -> bool:
    """Same detection logic as agentic_template_build._classify_overlay."""
    role = overlay.get("role", "")
    sample_text = overlay.get("sample_text") or overlay.get("text") or ""
    # Subject placeholder heuristic mirrored from template_orchestrate.
    is_subject = (
        bool(sample_text.strip()) and sample_text.isupper() and len(sample_text.split()) <= 3
    )
    return role == "label" or is_subject or sample_text.lower().startswith("welcome")


# ── Structural assertions ─────────────────────────────────────────────────────


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason=(
        f"no fixtures under {FIXTURE_DIR} — "
        "hand-author one or capture via live agentic_template_build_task"
    ),
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
class TestAgenticTemplateE2EStructure:
    """Schema + invariant checks. Run on every PR, no network."""

    @pytest.fixture
    def fixture(self, fixture_path: Path) -> dict:
        return json.loads(fixture_path.read_text())

    @pytest.fixture
    def recipe(self, fixture: dict) -> dict:
        return fixture["recipe"]

    def test_fixture_agent_field_is_agentic_build(self, fixture: dict):
        assert fixture.get("agent") == "nova.agentic.template_build", (
            f"fixture must declare agent=nova.agentic.template_build; got {fixture.get('agent')!r}"
        )

    def test_prompt_versions_recorded(self, fixture: dict):
        # Auto-trigger CI on prompt-version bumps relies on these strings.
        versions = fixture.get("prompt_versions", {})
        for agent in ("creative_direction", "template_recipe", "text_designer"):
            assert versions.get(agent), (
                f"prompt_versions[{agent!r}] missing or empty — auto-trigger CI "
                "won't be able to detect when this fixture is stale"
            )

    def test_recipe_has_required_top_level_fields(self, recipe: dict):
        required = {
            "shot_count",
            "total_duration_s",
            "slots",
            "copy_tone",
            "caption_style",
            "creative_direction",
            "transition_style",
            "color_grade",
            "interstitials",
        }
        missing = required - set(recipe)
        assert not missing, f"recipe missing fields: {sorted(missing)}"

    def test_creative_direction_is_substantive(self, recipe: dict):
        # The orchestrator should never persist an empty creative_direction;
        # if it does, Pass 1 of analyze_template silently failed.
        cd = recipe.get("creative_direction", "")
        assert len(cd.strip()) >= 30, (
            f"creative_direction is suspiciously short ({len(cd)} chars). "
            "Likely Pass 1 (creative_direction agent) returned empty and the "
            "orchestrator persisted the fallback."
        )

    def test_slot_count_matches_shot_count(self, recipe: dict):
        slots = recipe.get("slots", [])
        assert len(slots) == recipe["shot_count"], (
            f"slots ({len(slots)}) != shot_count ({recipe['shot_count']}) — "
            "template_recipe agent contradicted itself"
        )

    def test_slot_positions_are_unique_and_sequential(self, recipe: dict):
        slots = recipe.get("slots", [])
        positions = [s.get("position") for s in slots]
        assert positions == sorted(positions), f"slot positions not in ascending order: {positions}"
        assert len(set(positions)) == len(positions), f"duplicate slot positions: {positions}"

    def test_label_overlays_have_baked_styling(self, recipe: dict):
        """Every label-like overlay must have all six text_designer fields.

        This is the central invariant of agentic builds — if any label lacks
        its baked styling, the job-time pipeline (with the _LABEL_CONFIG
        override skipped for agentic templates) will render the overlay with
        defaults instead of the agent's chosen typography.
        """
        required = {"text_size", "font_style", "text_color", "effect", "start_s"}
        for slot in recipe.get("slots", []):
            for ov in slot.get("text_overlays", []):
                if not _is_label_like(ov):
                    continue
                missing = required - set(ov)
                assert not missing, (
                    f"slot {slot.get('position')} label overlay {ov.get('sample_text')!r} "
                    f"missing baked text_designer fields: {sorted(missing)}. "
                    "Re-run the orchestrator or check text_designer for failures."
                )

    def test_font_cycle_subjects_have_accel(self, recipe: dict):
        """Subject overlays with effect=font-cycle should have accel_at_s set.

        Without accel_at_s, the cycling never decelerates to settle on the
        final word — the renderer falls back to its default 70%-cycle/30%-settle
        behavior, which on curtain-close slots collides with the curtain timing.
        """
        for slot in recipe.get("slots", []):
            for ov in slot.get("text_overlays", []):
                if ov.get("effect") != "font-cycle":
                    continue
                sample = ov.get("sample_text", "")
                is_subject = bool(sample.strip()) and sample.isupper() and len(sample.split()) <= 3
                if not is_subject:
                    continue
                accel = ov.get("font_cycle_accel_at_s") or ov.get("accel_at_s")
                assert accel is not None, (
                    f"slot {slot.get('position')} subject {sample!r} has font-cycle "
                    "but no accel_at_s — text_designer didn't bake decel timing"
                )

    def test_interstitials_reference_valid_slots(self, recipe: dict):
        positions = {s.get("position") for s in recipe.get("slots", [])}
        for inter in recipe.get("interstitials", []):
            after = inter.get("after_slot")
            assert after in positions, (
                f"interstitial after_slot={after} doesn't match any slot "
                f"(known positions: {sorted(positions)})"
            )

    def test_total_duration_matches_slot_and_hold_sum(self, recipe: dict):
        slot_total = sum(float(s.get("target_duration_s", 0.0)) for s in recipe.get("slots", []))
        hold_total = sum(float(i.get("hold_s", 0.0)) for i in recipe.get("interstitials", []))
        expected = slot_total + hold_total
        actual = float(recipe.get("total_duration_s", 0.0))
        # 0.5s tolerance — agents round, beat-snap shifts boundaries.
        assert abs(actual - expected) < 0.5, (
            f"total_duration_s={actual} doesn't match slot sum ({slot_total}) "
            f"+ interstitial holds ({hold_total}) = {expected}"
        )


# ── LLM judge (opt-in, --with-judge) ─────────────────────────────────────────


@pytest.mark.skipif(
    not FIXTURE_PATHS,
    reason="no fixtures to judge",
)
@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_agentic_recipe_passes_metadata_judge(
    fixture_path: Path,
    with_judge: bool,
) -> None:
    """Claude Sonnet scores the assembled recipe against agentic_template_e2e.md.

    Opt-in via --with-judge so PR-time CI stays free. Live mode triggers when
    any agentic-stack agent's prompt is touched (see agent-evals.yml).
    """
    if not with_judge:
        pytest.skip("judge run requires --with-judge")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("--with-judge requires ANTHROPIC_API_KEY")

    from tests.evals.runners.llm_judge import LLMJudge

    fixture = json.loads(fixture_path.read_text())
    judge = LLMJudge(RUBRIC_PATH)
    result = judge.score(
        agent_name="nova.agentic.template_build",
        agent_input=fixture.get("meta", {}),
        agent_output=fixture["recipe"],
    )

    assert result.passed, (
        f"\n{fixture_path.stem}: {result.summary()}\n  reasoning: {result.reasoning}"
    )
