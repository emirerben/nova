#!/usr/bin/env python3
"""Summarise a PR's red CI: what failed, flake vs real, and how to reproduce it.

Usage:
  python3 .claude/skills/ci-triage/scripts/triage.py            # PR of current branch
  python3 .claude/skills/ci-triage/scripts/triage.py 812        # PR number / URL / branch
  python3 .claude/skills/ci-triage/scripts/triage.py 812 --json

Stdlib only; needs an authenticated `gh`. Read-only: never re-runs, pushes or
edits anything. Guards: scripts/ci/test_ci_triage.py.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
OK = {"pass", "skipping"}

# Real infra trouble only. Timeouts are NOT here: a job hitting its limit is
# usually a slow test or a hang, which is a code problem.
FLAKE = re.compile(
    r"runner has received a shutdown signal|lost communication with the server"
    r"|No space left on device|HTTP 50[0234]\b|\b50[234] (Bad Gateway|Service Unavailable|Gateway Time-?out)"
    r"|rate limit exceeded|API rate limit|Could not resolve host|ECONNRESET|ETIMEDOUT"
    r"|Unable to boot (the )?(device|simulator)|CoreSimulatorService connection (became invalid|interrupted)"
    r"|Failed to download action|Received request to deprovision",
    re.I,
)
TIMEOUT = re.compile(
    r"exceeded the maximum execution time|has timed out|Timeout \(\d+(\.\d+)?s\)", re.I
)
SIGNAL = re.compile(
    r"FAILED |ERROR |Error:|error:|\berror\b:|AssertionError|Traceback|\*\* (TEST|BUILD) FAILED \*\*"
    r"|Test Case .* failed|XCTAssert|: error: |Process completed with exit code [1-9]|✗|FAIL\b",
)
STAMP = re.compile(r"^(?:[^\t]*\t[^\t]*\t)?\ufeff?\d{4}-\d\d-\d\dT[\d:.]+Z ?")
PYTEST_FAILED = re.compile(r"(?:FAILED|ERROR) (tests/\S+?\.py::\S+)")
API = "cd src/apps/api && "
PR_GATE = (
    "PR body / metadata gate, not code: read the job log and fix the PR description or "
    "metadata it names (docs/runbooks/pr-acceptance-checklist.md)"
)

# The log beats the check name: CI's `lint` gate also validates PR evidence.
LOG_RULES: list[tuple[str, str]] = [
    (
        r"PR request evidence|evidence receipt|head_sha must match|Lane C gate|local-test marker",
        PR_GATE,
    ),
    (
        r"would reformat|ruff (check|format)|\[\*\] \d+ fixable",
        API + "ruff check . && ruff format --check .",
    ),
]

# Gate jobs pass only when these upstream jobs pass (scripts/ci/select-tests.py gate).
GATES: dict[str, str] = {
    "lint": r"^lint-suites",
    "test-api": r"^api \(\d\)|^journey|creator-contract-replay|shared-motion-runtime",
    "build-and-test": r"^journey|ios-contracts|^ios-tests",
    "PR iPhone gate": r"^journey|ios-contracts|^ios-tests",
}

# (check-name regex, workflow-name regex, command). First match wins.
RULES: list[tuple[str, str, str]] = [
    (r"^lint", r"^CI$", API + "ruff check . && ruff format --check ."),
    (r"^api \((\d)\)", r"", API + "python3 ../../../scripts/ci/api-tests.py {0}"),
    (r"^test-api", r"^CI$", "make test-api"),
    (r"journey-mapping", r"", "python3 scripts/ci/select-tests.py validate-mapping"),
    (
        r"^journey",
        r"",
        "python3 scripts/ci/select-tests.py gate journey journey  # then the failing journey's command",
    ),
    (
        r"ios-contracts",
        r"",
        "python3 -m unittest discover -s scripts/ios/tests -v && ("
        + API
        + "python -m app.cli.kria_contracts --check)",
    ),
    (
        r"creator-contract-replay",
        r"",
        API
        + "python ../../../scripts/ci/replay-creator-contracts.py  # see ci.yml job for args",
    ),
    (
        r"shared-motion-runtime",
        r"",
        "cd src/apps/web && npx jest src/__tests__/lib/motion-runtime.test.ts --runInBand",
    ),
    (
        r"\bios\b|iOS|iPhone|build-and-test",
        r"^iOS$|^CI$",
        "make ios-verify  # unit only: KRIA_IOS_TEST_MODE=unit bash scripts/ios/verify.sh",
    ),
    (r"Structural evals", r"", API + "pytest tests/evals/ -v"),
    (r"", r"CLAUDE\.md size", "bash scripts/check_claude_md_size.sh"),
    (r"", r"Layer-2 cache", "bash src/apps/api/scripts/check_layer2_cache_bump.sh"),
    (r"", r"Codex agent routing", "python3 scripts/check_codex_agent_routing.py"),
    (
        r"verify-overlays",
        r"",
        "make verify-overlays  # then read .overlay-verify/report.json + montage.png",
    ),
    (
        r"Build prod image",
        r"",
        "docker build -f Dockerfile .  # check new COPY lines against .dockerignore",
    ),
    (
        r"Request-following|request-following",
        r"",
        API
        + "python scripts/request-following-regressions.py --output .request-following-report",
    ),
    (
        r"PR evidence|local-test marker|staging plan|release.metadata|publish",
        r"PR evidence|Require local test|Migration staging|Release metadata",
        PR_GATE,
    ),
]


def gh(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, timeout=timeout
    )


def job_ref(link: str) -> tuple[str | None, str | None]:
    m = re.search(r"/actions/runs/(\d+)/job/(\d+)", link or "")
    return (m.group(1), m.group(2)) if m else (None, None)


def clean(log: str) -> list[str]:
    return [STAMP.sub("", ln).rstrip() for ln in log.splitlines()]


def classify(bucket: str, log: str) -> str:
    if bucket == "cancel":
        return "cancelled"
    if FLAKE.search(log):
        return "infra-flake"
    if TIMEOUT.search(log):
        return "timeout"
    return "failure"


def workflow_file(workflow: str) -> str:
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        try:
            first = re.search(r"^name:\s*(.+)$", path.read_text(), re.M)
        except OSError:
            continue
        if first and first.group(1).strip().strip("'\"") == workflow:
            return str(path.relative_to(ROOT))
    return ".github/workflows/"


def upstream(name: str, failing: list[dict]) -> list[str]:
    """Other red checks that a gate job is only reporting."""
    pattern = GATES.get(name)
    if not pattern:
        return []
    return [
        f["name"]
        for f in failing
        if f["name"] != name and re.search(pattern, f["name"])
    ]


def reproduce(name: str, workflow: str, log: str) -> str:
    text = "\n".join(clean(log))
    for log_re, command in LOG_RULES:
        if re.search(log_re, text):
            return command
    failed = sorted(dict.fromkeys(PYTEST_FAILED.findall(text)))
    if failed and re.match(r"^(api \(|test-api|Structural evals|lint-suites)", name):
        return API + "pytest " + " ".join(shlex.quote(t) for t in failed[:20]) + " -v"
    for name_re, wf_re, command in RULES:
        if name_re and not re.search(name_re, name):
            continue
        if wf_re and not re.search(wf_re, workflow or ""):
            continue
        m = re.search(name_re, name) if name_re else None
        return command.format(*(m.groups() if m else ()))
    return f"No local mapping for this check; reproduce from its workflow file {workflow_file(workflow)}"


def excerpt(log: str, limit: int) -> list[str]:
    lines = [ln for ln in clean(log) if ln.strip()]
    hits = [
        i
        for i, ln in enumerate(lines)
        if SIGNAL.search(ln) or FLAKE.search(ln) or TIMEOUT.search(ln)
    ]
    if not hits:
        return lines[-limit:]
    keep: list[int] = []
    for i in hits:
        keep.extend(range(max(0, i - 3), min(len(lines), i + 4)))
    picked = [lines[i] for i in sorted(set(keep))]
    return picked[:limit] if len(picked) > limit else picked


BUCKET = {
    "success": "pass",
    "neutral": "pass",
    "skipped": "skipping",
    "failure": "fail",
    "timed_out": "fail",
    "action_required": "fail",
    "startup_failure": "fail",
    "cancelled": "cancel",
    "stale": "cancel",
}


def repo_slug() -> str:
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"], capture_output=True, text=True
    ).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(?:\.git)?/?$", url)
    if not m:
        raise SystemExit(f"triage: cannot read owner/repo from origin {url!r}")
    return m.group(1)


def api(path: str, *extra: str) -> object:
    done = gh(["api", path, *extra], timeout=60)
    if done.returncode != 0 and re.search(r"HTTP 5\d\d", done.stderr or ""):
        time.sleep(3)  # one retry for transient 5xx
        done = gh(["api", path, *extra], timeout=60)
    if done.returncode != 0:
        raise SystemExit(
            f"triage: gh api {path} failed: {(done.stderr or '').strip()[:300]}"
        )
    return json.loads(done.stdout)


def latest(checks: list[dict]) -> list[dict]:
    """Newest run per (check, workflow), like `gh pr checks`; drops superseded runs."""
    best: dict[tuple[str, str], dict] = {}
    for c in sorted(checks, key=lambda c: c.get("id") or 0):
        best[(c.get("name") or "", c.get("workflow") or "")] = c
    return list(best.values())


def checks_via_rest(pr: str | None) -> tuple[list[dict], dict]:
    """Fallback when GraphQL is unavailable (e.g. remote Claude Code sessions)."""
    slug = repo_slug()
    number = re.search(r"(\d+)\s*$", pr or "")
    if number:
        pull = api(f"repos/{slug}/pulls/{number.group(1)}")
    else:
        branch = (
            pr
            or subprocess.run(
                ["git", "branch", "--show-current"], capture_output=True, text=True
            ).stdout.strip()
        )
        owner = slug.split("/")[0]
        pulls = api(f"repos/{slug}/pulls?state=open&head={owner}:{branch}")
        if not pulls:
            raise SystemExit(f"triage: no open PR for branch {branch!r}")
        pull = pulls[0]
    sha = pull["head"]["sha"]
    runs = api(f"repos/{slug}/commits/{sha}/check-runs?per_page=100")["check_runs"]
    try:  # one call for every workflow name; cosmetic, so a 5xx must not abort
        listed = api(f"repos/{slug}/actions/runs?head_sha={sha}&per_page=100")[
            "workflow_runs"
        ]
        names = {str(r["id"]): r.get("name", "") for r in listed}
    except (SystemExit, KeyError, TypeError):
        names = {}
    checks = []
    for run in runs:
        run_id, _ = job_ref(run.get("html_url", ""))
        status = run.get("status")
        checks.append(
            {
                "id": run.get("id"),
                "name": run.get("name"),
                "bucket": "pending"
                if status != "completed"
                else BUCKET.get(run.get("conclusion") or "", "fail"),
                "link": run.get("html_url", ""),
                "workflow": names.get(run_id or "", ""),
            }
        )
    meta = {
        "number": pull["number"],
        "headRefOid": sha,
        "headRefName": pull["head"]["ref"],
        "url": pull["html_url"],
    }
    return latest(checks), meta


def triage(pr: str | None, limit: int) -> dict:
    fields = "name,bucket,link,workflow,state,description"
    done = gh(["pr", "checks", *([pr] if pr else []), "--json", fields], timeout=60)
    if done.stdout.strip().startswith("["):
        checks = json.loads(done.stdout)
        head = gh(
            [
                "pr",
                "view",
                *([pr] if pr else []),
                "--json",
                "number,headRefOid,headRefName,url",
            ],
            timeout=30,
        )
        meta = json.loads(head.stdout) if head.returncode == 0 else {}
    else:
        checks, meta = checks_via_rest(pr)
    pending = sorted(c["name"] for c in checks if c.get("bucket") == "pending")
    report = []
    for c in checks:
        bucket = c.get("bucket", "")
        if bucket in OK or bucket == "pending":
            continue
        run_id, job_id = job_ref(c.get("link", ""))
        log = ""
        if job_id:
            got = gh(
                ["run", "view", run_id, "--job", job_id, "--log-failed"], timeout=120
            )
            log = got.stdout or ""
            if not log.strip():
                # `gh run view` refuses while other jobs in the run are still going;
                # the per-job log endpoint works as soon as this job finished.
                got = gh(
                    ["api", f"repos/{repo_slug()}/actions/jobs/{job_id}/logs"],
                    timeout=120,
                )
                log = (
                    "\n".join((got.stdout or "").splitlines()[-400:])
                    if got.returncode == 0
                    else ""
                )
        report.append(
            {
                "name": c.get("name"),
                "workflow": c.get("workflow"),
                "bucket": bucket,
                "kind": classify(bucket, log),
                "link": c.get("link"),
                "run_id": run_id,
                "reproduce": reproduce(c.get("name", ""), c.get("workflow", ""), log)
                + (
                    ""
                    if log.strip()
                    else "  (GUESS from the check name: log unavailable, confirm via the link)"
                ),
                "excerpt": excerpt(log, limit)
                if log
                else ["(no log available; open the link)"],
            }
        )
    for f in report:
        ups = upstream(f["name"], report)
        if ups and f["reproduce"] != PR_GATE:
            f["kind"] = "gate"
            f["reproduce"] = (
                "Gate job: goes green once "
                + ", ".join(ups)
                + " pass. Fix those, not this."
            )
    return {"pr": meta, "failing": report, "pending": pending, "total": len(checks)}


def render(result: dict) -> str:
    pr = result["pr"]
    out = [
        f"# CI triage — PR #{pr.get('number', '?')} ({pr.get('headRefName', '?')} @ {str(pr.get('headRefOid', ''))[:9]})",
        f"{len(result['failing'])} red, {len(result['pending'])} pending, {result['total']} total checks.",
    ]
    if result["pending"]:
        out.append("Pending: " + ", ".join(result["pending"]))
    if not result["failing"]:
        out.append(
            "\nNothing red. "
            + ("Wait for pending checks." if result["pending"] else "CI is green.")
        )
    for f in result["failing"]:
        out += [
            f"\n## {f['name']}  [{f['kind']}]",
            f"workflow: {f['workflow']}  link: {f['link']}",
            f"reproduce: {f['reproduce']}",
            "```",
            *f["excerpt"],
            "```",
        ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "pr", nargs="?", help="PR number, URL or branch (default: current branch)"
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--lines", type=int, default=60, help="max log lines per failing check"
    )
    args = parser.parse_args(argv)
    result = triage(args.pr, args.lines)
    print(json.dumps(result, indent=2) if args.json else render(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
