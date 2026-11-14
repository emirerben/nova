#!/usr/bin/env python3
"""Block the third fix PR on one KRI ticket unless a failure-mode list exists.

Rule: docs/runbooks/linear-workflow.md#fix-chains-the-second-fix-pr-rule (KRI-1383).
Shared by scripts/preship-check.sh and .github/workflows/fix-chain.yml.

Prior fixes are fix-prefixed PRs already on the main ref. A fix is charged to
the first KRI id in its squash subject, falling back to the first id in its PR
title or branch name (from --pr-index, a JSON list of
{number, title, headRefName}). Counting only the first id keeps a child fix that
names its parent second from inflating the parent's count.

Only first-parent history is read, so the branch commits of a merge-commit PR
never count as PRs of their own. With a PR index, a first-parent commit that has
no "(#N)" (a rebase-merged commit or a direct push) is not a PR and is ignored:
the index already holds every merged PR by its real title. Without an index
(offline local run) such commits are counted one by one, which can overcount a
multi-commit rebase merge.

Exit 0: allowed. Exit 1: blocked. Exit 2: usage error.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

THRESHOLD = 2
SKIP_MARKER = "[skip-fix-chain]"
TICKET_RE = re.compile(r"(?<![A-Za-z0-9])kri-(\d+)(?![A-Za-z0-9])", re.IGNORECASE)
FIX_RE = re.compile(r"^\s*(?:hot)?fix(?:\(|:|!|\s|$)", re.IGNORECASE)
PR_RE = re.compile(r"\(#(\d+)\)\s*$")
HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?\**Failure modes for (KRI-\d+)(?!\d)", re.IGNORECASE
)
ROW_RE = re.compile(
    r"^\s*[-*]\s+\S.*->.*\|\s*(?:covered by\s+(\S+)|uncovered - adding\s+(\S+))",
    re.IGNORECASE,
)
SKIP_RE = re.compile(r"^" + re.escape(SKIP_MARKER) + r"(?:\s+(.*))?$")
PLACEHOLDER_RE = re.compile(r"<[^<>\n]*>")
EMPTY_TOKENS = {
    "todo",
    "tbd",
    "none",
    "n/a",
    "na",
    "test",
    "tests",
    "xxx",
    "manual",
    "existing",
}
MIN_ROWS = 2
MIN_REASON_CHARS = 20
MIN_REASON_WORDS = 4


def first_ticket(text):
    match = TICKET_RE.search(text or "")
    return f"KRI-{int(match.group(1))}" if match else None


def is_fix(title):
    return bool(FIX_RE.match(title or ""))


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
    )


def prior_fixes(repo, main_ref, index, current_pr):
    """Map ticket -> set of PR keys that are merged fix PRs charged to it."""
    by_number = {int(entry["number"]): entry for entry in index}
    found = {}

    def charge(key, ticket):
        if ticket:
            found.setdefault(ticket, set()).add(key)

    for line in git(
        repo, "log", "--first-parent", main_ref, "--format=%H%x1f%s"
    ).splitlines():
        sha, _, subject = line.partition("\x1f")
        if not is_fix(subject):
            continue
        pr = PR_RE.search(subject)
        number = int(pr.group(1)) if pr else None
        if number is not None and number == current_pr:
            continue
        if number is None and index:
            continue
        key = f"pr-{number}" if number is not None else sha
        ticket = first_ticket(subject)
        if ticket is None and number in by_number:
            entry = by_number[number]
            ticket = first_ticket(entry.get("title")) or first_ticket(
                entry.get("headRefName")
            )
        charge(key, ticket)

    for number, entry in by_number.items():
        if number == current_pr or not is_fix(entry.get("title")):
            continue
        charge(
            f"pr-{number}",
            first_ticket(entry.get("title")) or first_ticket(entry.get("headRefName")),
        )
    return found


def valid_token(token):
    token = token.strip(".,;)")
    return len(token) >= 4 and token.lower() not in EMPTY_TOKENS


def failure_mode_problems(text, ticket):
    """Return None when text carries a usable block for ticket, else a reason."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    problem = f"no 'Failure modes for {ticket}' heading"
    for start, line in enumerate(lines):
        heading = HEADING_RE.match(line)
        if not heading or heading.group(1).upper() != ticket:
            continue
        rows, bad = 0, []
        for row in lines[start + 1 :]:
            if not row.strip():
                continue
            if not re.match(r"^\s*[-*]\s", row):
                break
            match = ROW_RE.match(row)
            token = (match.group(1) or match.group(2)) if match else None
            if (
                match
                and token
                and valid_token(token)
                and not PLACEHOLDER_RE.search(row)
            ):
                rows += 1
            else:
                bad.append(row.strip())
        if rows >= MIN_ROWS and not bad:
            return None
        problem = (
            f"block for {ticket} needs at least {MIN_ROWS} rows shaped "
            "'- <shape> -> <outcome> | covered by <test id>' or "
            "'... | uncovered - adding <test id>' with no placeholders"
        )
        if bad:
            problem += "; bad row: " + bad[0][:100]
    return problem


def skip_reason(text):
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = SKIP_RE.match(line.strip())
        if not match:
            continue
        reason = (match.group(1) or "").strip()
        words = [w for w in reason.split() if w.lower() not in EMPTY_TOKENS]
        if (
            len(reason) >= MIN_REASON_CHARS
            and len(set(w.lower() for w in words)) >= MIN_REASON_WORDS
            and not PLACEHOLDER_RE.search(reason)
        ):
            return reason
    return None


def doc_texts(repo, files):
    root = Path(repo).resolve()
    for name in files:
        path = (root / name).resolve()
        if root not in path.parents or path.suffix != ".md":
            continue
        if not str(path.relative_to(root)).startswith(("docs/", "plans/")):
            continue
        if path.is_file():
            yield path.read_text(errors="replace")


def branch_fix_subjects(repo, main_ref, head):
    try:
        out = git(repo, "log", f"{main_ref}..{head}", "--format=%s")
    except subprocess.CalledProcessError:
        return []
    return out.splitlines()


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default=".")
    parser.add_argument("--main-ref", default="origin/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--branch", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--body-file")
    parser.add_argument("--pr-index")
    parser.add_argument("--pr-number", type=int)
    parser.add_argument("--changed-file", action="append", default=[])
    args = parser.parse_args(argv)

    body = (
        Path(args.body_file).read_text(errors="replace")
        if args.body_file and Path(args.body_file).is_file()
        else ""
    )
    index = json.loads(Path(args.pr_index).read_text()) if args.pr_index else []

    subjects = (
        [] if args.title else branch_fix_subjects(args.repo, args.main_ref, args.head)
    )
    current_is_fix = (
        is_fix(args.title) if args.title else any(is_fix(s) for s in subjects)
    )
    # Same rule as prior_fixes: one ticket per PR, so a child fix is never gated
    # by the parent it names second, and is later charged to the ticket it was gated on.
    ticket = (
        first_ticket(args.title)
        or first_ticket(args.branch)
        or next((t for t in (first_ticket(s) for s in subjects) if t), None)
    )
    tickets = [ticket] if ticket else []
    if not tickets:
        print("fix-chain: no KRI-<n> in title or branch - skipped")
        return 0
    if not current_is_fix:
        print(
            f"fix-chain: not a fix PR for {', '.join(tickets)} - no failure-mode list required"
        )
        return 0

    priors = prior_fixes(args.repo, args.main_ref, index, args.pr_number)
    status = 0
    for ticket in tickets:
        count = len(priors.get(ticket, ()))
        if count < THRESHOLD:
            print(
                f"fix-chain: {ticket} has {count} prior fix PR(s) on main - no fix chain"
            )
            continue
        label = f"{ticket} already has {count} prior fix PR(s) on main; this is fix #{count + 1}"
        problem = failure_mode_problems(body, ticket)
        if problem is not None:
            for text in doc_texts(args.repo, args.changed_file):
                if failure_mode_problems(text, ticket) is None:
                    problem = None
                    break
        if problem is None:
            print(f"fix-chain: {label}; failure-mode list present")
            continue
        reason = skip_reason(body)
        if reason:
            print(f"fix-chain: {label}; SKIPPED with reason: {reason}")
            continue
        status = 1
        print(f"fix-chain: BLOCKED - {label}.")
        print(f"  {problem}")
        print("  Add to the PR body (or a changed docs/ or plans/ .md file):")
        print(f"    Failure modes for {ticket} (after fix #{count})")
        print(
            "    - <shape / input / state> -> <expected outcome> | covered by <test id>"
        )
        print(
            "    - <shape / input / state> -> <expected outcome> | uncovered - adding <test id>"
        )
        print("  Or, if this is not a repeat of the same fault, add its own line:")
        print(
            f"    {SKIP_MARKER} <one-line reason, at least {MIN_REASON_CHARS} characters>"
        )
        print(
            "  Rule: docs/runbooks/linear-workflow.md#fix-chains-the-second-fix-pr-rule"
        )
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
