"""Tests for app.services.build_gate — the Phase 2 gate decision logic.

Pure stdlib functions, so these run under bare python3 with no DB or network.
The render-path detector is the highest-stakes piece (a false negative ships a
clipped video — the #296 class), so it gets the most cases.
"""

from __future__ import annotations

from app.services.build_gate import (
    GateResult,
    aggregate_gate_results,
    build_pr_body,
    changed_paths,
    high_risk_paths_touched,
    render_paths_touched,
)

_UNIFIED = """diff --git a/app/pipeline/reframe.py b/app/pipeline/reframe.py
index 111..222 100644
--- a/app/pipeline/reframe.py
+++ b/app/pipeline/reframe.py
@@ -1 +1 @@
-old
+new
"""

_NAME_ONLY = "src/apps/web/src/app/page.tsx\nsrc/apps/api/app/routes/music.py\n"


# ── changed_paths ────────────────────────────────────────────────────────────


def test_changed_paths_parses_unified_diff():
    assert changed_paths(_UNIFIED) == ["app/pipeline/reframe.py"]


def test_changed_paths_parses_name_only():
    assert changed_paths(_NAME_ONLY) == [
        "src/apps/web/src/app/page.tsx",
        "src/apps/api/app/routes/music.py",
    ]


def test_changed_paths_empty_and_blank():
    assert changed_paths("") == []
    assert changed_paths("   \n  ") == []


def test_changed_paths_drops_dev_null():
    diff = "--- a/old.py\n+++ /dev/null\n"
    assert "/dev/null" not in changed_paths(diff)


# ── render_paths_touched (the #296 guard) ────────────────────────────────────


def test_render_touched_pipeline_dir():
    assert render_paths_touched(_UNIFIED) is True


def test_render_touched_overlay_and_single_pass_and_fonts():
    for p in (
        "src/apps/api/app/pipeline/single_pass.py",
        "src/apps/api/app/pipeline/text_overlay_skia.py",
        "src/apps/api/app/pipeline/interstitials.py",
        "src/apps/api/app/pipeline/transitions.py",
        "assets/fonts/PlayfairDisplay-Bold.ttf",
    ):
        assert render_paths_touched(p + "\n") is True, p


def test_render_NOT_touched_pure_frontend():
    assert render_paths_touched("src/apps/web/src/app/music/page.tsx\n") is False


def test_render_blind_spot_prompt_only_is_false():
    # Documented blind spot: a prompt change that alters overlay TEXT touches no
    # render path. False is the EXPECTED (and documented) behavior in Phase 2.
    assert render_paths_touched("src/apps/api/prompts/template_text.txt\n") is False


def test_render_empty_diff_false():
    assert render_paths_touched("") is False


# ── high_risk_paths_touched ──────────────────────────────────────────────────


def test_high_risk_migration_and_auth():
    assert high_risk_paths_touched(
        "src/apps/api/app/migrations/versions/0047_x.py\n"
    ) is True
    assert high_risk_paths_touched("src/apps/web/src/lib/auth.ts\n") is True


def test_high_risk_plain_change_false():
    assert high_risk_paths_touched("src/apps/api/app/routes/music.py\n") is False


# ── aggregate_gate_results / GateReport.passed ───────────────────────────────


def test_all_blocking_pass_opens_pr():
    report = aggregate_gate_results(
        [
            GateResult("pytest", blocking=True, passed=True),
            GateResult("tsc", blocking=True, passed=True),
            GateResult("qa", blocking=False, passed=True),
        ]
    )
    assert report.passed is True
    assert report.blocking_failures == []


def test_one_blocking_fail_blocks():
    report = aggregate_gate_results(
        [
            GateResult("pytest", blocking=True, passed=True),
            GateResult("verify-overlays", blocking=True, passed=False, detail="clipped"),
        ]
    )
    assert report.passed is False
    assert [r.name for r in report.blocking_failures] == ["verify-overlays"]


def test_advisory_fail_does_NOT_block():
    # The /qa-advisory decision: a failing advisory gate still opens the PR.
    report = aggregate_gate_results(
        [
            GateResult("pytest", blocking=True, passed=True),
            GateResult("qa", blocking=False, passed=False, detail="1 console error"),
        ]
    )
    assert report.passed is True
    assert [r.name for r in report.advisory_failures] == ["qa"]


def test_report_to_dict_shape():
    d = aggregate_gate_results(
        [GateResult("pytest", blocking=True, passed=True, detail="412 passed")]
    ).to_dict()
    assert d["passed"] is True
    assert d["results"][0] == {
        "name": "pytest",
        "blocking": True,
        "passed": True,
        "detail": "412 passed",
    }


# ── build_pr_body ────────────────────────────────────────────────────────────


def test_pr_body_contains_task_gates_and_verdict():
    report = aggregate_gate_results(
        [
            GateResult("pytest", blocking=True, passed=True),
            GateResult("qa", blocking=False, passed=False, detail="1 console error"),
        ]
    )
    body = build_pr_body(
        task_id="abc-123",
        task_title="fix karaoke wrap",
        report=report,
        head_sha="deadbeefcafe0000",
        base_sha="00001111face2222",
    )
    assert "fix karaoke wrap" in body
    assert "abc-123" in body
    assert "deadbeefcafe" in body  # truncated head sha
    assert "rebased onto `00001111face`" in body
    assert "| pytest | blocking | pass |" in body
    assert "fail (advisory)" in body
    assert "All blocking gates green" in body


def test_pr_body_failed_verdict():
    report = aggregate_gate_results(
        [GateResult("tsc", blocking=True, passed=False, detail="type error")]
    )
    body = build_pr_body(task_id="x", task_title="t", report=report)
    assert "this PR should not exist" in body
