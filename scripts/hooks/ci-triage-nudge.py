#!/usr/bin/env python3
"""Tell Claude to run the ci-triage skill when CI is red, without being asked.

Two entry points, both fail-open (any error, missing `gh`, bad input -> exit 0,
no output), so this can never block or slow a session:

  PostToolUse (Bash, default): after a `gh pr checks|view|status` or
  `gh run watch|view|list` call whose output shows a failed, cancelled or
  timed-out check, emit `additionalContext` telling Claude to invoke
  ci-triage. Nudges once per session per distinct red state, so polling with
  `gh run watch` does not repeat it.

  SessionStart (`--session-start`): if the current branch has an open PR whose
  head is red, print a one-line note (SessionStart stdout becomes context).

Guards: scripts/ci/test_ci_triage.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

CI_READ = re.compile(r"\bgh\s+(pr\s+(checks|view|status)|run\s+(watch|view|list))\b")
SELF = re.compile(r"ci-triage|triage\.py")
RED = [
    re.compile(r"\t(fail|cancel)\t"),
    re.compile(r'"bucket"\s*:\s*"(fail|cancel)"'),
    re.compile(
        r'"(conclusion|state)"\s*:\s*"(failure|timed_out|cancelled|startup_failure|error)"',
        re.I,
    ),
    re.compile(r"completed with '(failure|cancelled|timed_out|startup_failure)'"),
    re.compile(r"Some checks were not successful"),
    re.compile(r"^X\s+\S", re.M),
]
RED_ROLLUP = {
    "FAILURE",
    "TIMED_OUT",
    "CANCELLED",
    "STARTUP_FAILURE",
    "ERROR",
    "ACTION_REQUIRED",
}

NUDGE = (
    "CI is red on this PR. Invoke the `ci-triage` skill now (Skill tool) before "
    "pushing more fixes, re-running jobs or merging: it separates infra flakes "
    "from real failures and gives the local command that reproduces each one. "
    "Do not ask the user first."
)


def output_text(response) -> str:
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = [response.get(k) for k in ("stdout", "stderr", "output", "content")]
        text = "\n".join(p for p in parts if isinstance(p, str))
        return text or json.dumps(response)
    return json.dumps(response) if response is not None else ""


def red_lines(text: str) -> list[str]:
    hits = [m.group(0) for pattern in RED for m in pattern.finditer(text)]
    if not hits:
        return []
    # Fingerprint on the lines that carry the failure, not on timings.
    keep = [ln for ln in text.splitlines() if any(p.search(ln) for p in RED)]
    return [re.sub(r"\d+m?\d*s\b", "", ln).strip() for ln in keep] or hits


def already_nudged(session: str, lines: list[str]) -> bool:
    digest = hashlib.sha1("\n".join(sorted(lines)).encode()).hexdigest()[:16]
    safe = re.sub(r"[^A-Za-z0-9_-]", "", session)[:64] or "nosession"
    marker = Path(tempfile.gettempdir()) / "nova-ci-triage-nudge" / f"{safe}-{digest}"
    if marker.exists():
        return True
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    return False


def post_tool(event: dict) -> int:
    command = (event.get("tool_input") or {}).get("command") or ""
    if not CI_READ.search(command) or SELF.search(command):
        return 0
    lines = red_lines(output_text(event.get("tool_response")))
    if not lines or already_nudged(str(event.get("session_id") or ""), lines):
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": NUDGE,
                }
            }
        )
    )
    return 0


def triage_module():
    import importlib.util

    root = Path(__file__).resolve().parents[2]
    path = root / ".claude/skills/ci-triage/scripts/triage.py"
    spec = importlib.util.spec_from_file_location("ci_triage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def session_start() -> int:
    if not shutil.which("gh"):
        return 0
    branch = subprocess.run(
        ["git", "branch", "--show-current"], capture_output=True, text=True, timeout=5
    ).stdout.strip()
    if not branch or branch in ("main", "dev"):
        return 0
    done = subprocess.run(
        ["gh", "pr", "view", "--json", "number,state,statusCheckRollup"],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if done.returncode == 0:
        pr = json.loads(done.stdout)
        if pr.get("state") != "OPEN":
            return 0
        red = sorted(
            {
                c.get("name") or c.get("context") or "?"
                for c in pr.get("statusCheckRollup") or []
                if (c.get("conclusion") or c.get("state") or "").upper() in RED_ROLLUP
            }
        )
    else:
        # GraphQL unavailable (remote sessions): reuse the skill's REST path.
        checks, meta = triage_module().checks_via_rest(None)
        pr = {"number": meta.get("number")}
        red = sorted(
            {c["name"] for c in checks if c.get("bucket") in ("fail", "cancel")}
        )
    if red:
        shown = ", ".join(red[:6]) + (
            f" (+{len(red) - 6} more)" if len(red) > 6 else ""
        )
        print(f"PR #{pr.get('number')} on '{branch}' has red checks: {shown}. {NUDGE}")
    return 0


def main(argv: list[str]) -> int:
    try:
        if "--session-start" in argv:
            return session_start()
        return post_tool(json.load(sys.stdin))
    except (
        BaseException
    ):  # fail-open, incl. the helper's SystemExit: never block a session
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
