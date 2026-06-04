"""Pure decision logic for the dev-loop Phase 2 quality gate.

The gate tick (scripts/cron/gate_runner.sh) runs the actual gate *commands*
(pytest, npm test, tsc, lint, make verify-overlays, plus advisory /qa + codex)
and shells out HERE for the decisions: which gates this diff requires, did the
blocking gates pass, and what the PR evidence body should say.

Dependency-free on purpose (stdlib only) — it unit-tests under bare python3
without the API venv, a DB, or the network. Every logged learning on this repo
is a pure-function-testability pitfall, so the render-path detector especially
gets real tests: a false negative there ships a clipped video (the #296 class).

State flow it serves:
    in_progress --(TASK COMPLETE)--> gating --[this logic]-->
        blocking gates green  -> open_pr -> awaiting_approval
        blocking gate failed  -> gate_failed -> queued (note)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Path markers meaning "this change can alter the burned video, so
# verify-overlays is REQUIRED." Default-ON: if any appears in a changed path,
# the render gate runs. Opt-out is explicit, never opt-in — a missed render
# change is the #296 "looks fine locally, clipped in prod" bug. Mirrors the
# single-pass / overlay / interstitial / transition surfaces in the pipeline.
RENDER_PATH_MARKERS = (
    "app/pipeline/",
    "single_pass",
    "interstitials",
    "transitions",
    "reframe",
    "text_overlay",
    "overlay",
    "agentic_timing",
    "orientation",
    "assets/fonts/",
)

# Paths whose change implies a HIGH-RISK class that must never auto-ship on a
# phone tap (Phase 4 enforcement; recorded now so the detector exists + is
# tested). Migrations, auth, and payment surfaces.
HIGH_RISK_MARKERS = (
    "migrations/versions/",
    "auth",
    "billing",
    "payment",
)

# Full unified diffs carry `+++ b/<path>` headers; `git diff --name-only`
# carries one bare path per line. Support both.
_DIFF_PATH_RE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def changed_paths(diff: str) -> list[str]:
    """Extract changed file paths from either `git diff --name-only` output
    (one path per line) or a full unified diff (``+++ b/<path>`` headers)."""
    if not diff or not diff.strip():
        return []
    paths = _DIFF_PATH_RE.findall(diff)
    if paths:
        return [p for p in paths if p != "/dev/null"]
    return [ln.strip() for ln in diff.splitlines() if ln.strip()]


def render_paths_touched(diff: str) -> bool:
    """True if the diff touches any render/overlay/pipeline path, meaning the
    burned-video output could change and `make verify-overlays` MUST run.

    Documented blind spot: a prompt-only change (prompts/*.txt) that alters
    overlay TEXT returns False — it touches no render path. Acceptable in Phase 2
    because the PR is human-merged; revisit when auto-ship lands."""
    return _any_marker(changed_paths(diff), RENDER_PATH_MARKERS)


def high_risk_paths_touched(diff: str) -> bool:
    """True if the diff touches a migration / auth / payment surface. Phase 2
    only records it in the gate_report; Phase 4 uses it to refuse a phone-ship."""
    return _any_marker(changed_paths(diff), HIGH_RISK_MARKERS)


def _any_marker(paths: list[str], markers: tuple[str, ...]) -> bool:
    for path in paths:
        low = path.lower()
        if any(marker in low for marker in markers):
            return True
    return False


@dataclass
class GateResult:
    """One gate's outcome. `blocking=False` means advisory (/qa, codex): its
    result is recorded and shown in the PR body but never blocks the PR."""

    name: str
    blocking: bool
    passed: bool
    detail: str = ""


@dataclass
class GateReport:
    results: list[GateResult] = field(default_factory=list)

    @property
    def blocking_failures(self) -> list[GateResult]:
        return [r for r in self.results if r.blocking and not r.passed]

    @property
    def advisory_failures(self) -> list[GateResult]:
        return [r for r in self.results if not r.blocking and not r.passed]

    @property
    def passed(self) -> bool:
        """A diff may open a PR iff every BLOCKING gate passed. Advisory gates
        never block — their failures are reported, not gated."""
        return not self.blocking_failures

    def to_dict(self) -> dict:
        """Serialized onto BuildTask.gate_report (JSONB)."""
        return {
            "passed": self.passed,
            "results": [
                {
                    "name": r.name,
                    "blocking": r.blocking,
                    "passed": r.passed,
                    "detail": r.detail,
                }
                for r in self.results
            ],
        }


def aggregate_gate_results(results: list[GateResult]) -> GateReport:
    """Collapse individual gate outcomes into one report. Pass iff no blocking
    gate failed; advisory failures are recorded only."""
    return GateReport(results=list(results))


def build_pr_body(
    *,
    task_id: str,
    task_title: str,
    report: GateReport,
    head_sha: str = "",
    base_sha: str = "",
) -> str:
    """Render the PR description: a scannable evidence table of every gate
    (blocking + advisory) plus task + commit context. The founder reads this at
    merge time, so advisory failures (e.g. a flaky /qa) are visible, not hidden."""
    lines = [
        f"## Autonomous build: {task_title}",
        "",
        f"- task: `{task_id}`",
    ]
    if head_sha:
        rebased = f" (rebased onto `{base_sha[:12]}`)" if base_sha else ""
        lines.append(f"- head: `{head_sha[:12]}`{rebased}")
    lines += ["", "### Gates", "", "| gate | kind | result |", "| --- | --- | --- |"]
    for r in report.results:
        kind = "blocking" if r.blocking else "advisory"
        if r.passed:
            mark = "pass"
        elif r.blocking:
            mark = "FAIL"
        else:
            mark = "fail (advisory)"
        detail = f" — {r.detail}" if r.detail else ""
        lines.append(f"| {r.name} | {kind} | {mark}{detail} |")
    lines += [
        "",
        (
            "All blocking gates green. Merge when ready."
            if report.passed
            else "Blocking gate(s) failed — this PR should not exist; investigate."
        ),
        "",
        "_Opened by the Nova autonomous dev-loop. You are the merge gate._",
    ]
    return "\n".join(lines)
