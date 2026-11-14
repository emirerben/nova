#!/usr/bin/env python3
"""List open PRs whose Fix chain result is stale after a fix PR merged (KRI-1383).

Prints "<number> <head sha>" for each open fix PR charged to the same ticket as
the merged PR. fix-chain-recheck.yml reruns the Fix chain run for those heads: a
fix PR checked while its ticket had fewer than two merged fixes would otherwise
stay green after sibling fix PRs merged and slip through as the third.

Ticket rule is the one fix-chain-check.py uses: first KRI id in the squash
subject (merged side only), else the PR title, else the branch name.
"""

import argparse
import importlib.util
import json
from pathlib import Path
import sys

_spec = importlib.util.spec_from_file_location(
    "fix_chain_check", Path(__file__).with_name("fix-chain-check.py")
)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)


def stale_prs(merged, subject, open_prs):
    """merged: PRs the pushed commit closed ({number, title, head.ref} or headRefName)."""
    tickets = set()
    for pr in merged:
        title = pr.get("title") or ""
        branch = pr.get("headRefName") or (pr.get("head") or {}).get("ref") or ""
        if not (check.is_fix(subject) or check.is_fix(title)):
            continue
        ticket = (
            check.first_ticket(subject)
            or check.first_ticket(title)
            or check.first_ticket(branch)
        )
        if ticket:
            tickets.add(ticket)
    selected = []
    for pr in open_prs:
        title = pr.get("title") or ""
        if not check.is_fix(title):
            continue
        ticket = check.first_ticket(title) or check.first_ticket(pr.get("headRefName"))
        if ticket in tickets:
            selected.append((pr["number"], pr["headRefOid"]))
    return selected


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--merged", required=True)
    parser.add_argument("--subject", default="")
    parser.add_argument("--open", required=True)
    args = parser.parse_args(argv)
    merged = json.loads(Path(args.merged).read_text())
    open_prs = json.loads(Path(args.open).read_text())
    for number, head in stale_prs(merged, args.subject, open_prs):
        print(number, head)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
