#!/usr/bin/env python3
"""Validate a scoped PR evidence receipt; never execute commands from PR text.

This checks completeness, immutable references, and honest claim boundaries. It
cannot determine whether a video actually depicts the requested action. Review
and the existing journey/renderer checks remain responsible for that judgment.
Run this file and its selector dependencies from the trusted baseline checkout;
--repo supplies candidate Git objects, never executable policy.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import NamedTuple
from urllib.parse import urlsplit

KINDS = {
    "incident_replay",
    "wrong_content_control",
    "recovery",
    "request_fulfillment",
    "export_fidelity",
    "rollout",
}
CONTENT_KINDS = {"incident_replay", "wrong_content_control", "request_fulfillment"}
LEVELS = {
    "unit",
    "integration",
    "offline_replay",
    "live_model",
    "human_review",
    "native_output",
    "production",
    "pending",
    "not_applicable",
}
FIELDS = {
    "schema_version",
    "claim",
    "issue",
    "original_request",
    "request_provenance",
    "expected_outcome",
    "root_cause",
    "checks",
}
IDENTITY_FIELDS = {"patch_id", "head_sha"}
ROW_FIELDS = {
    "kind",
    "status",
    "evidence_level",
    "command",
    "artifact",
    "expectation_source",
    "reason",
    "owner_issue",
}
POLICY_PATHS = {
    "AGENTS.md",
    "CLAUDE.md",
    "docs/runbooks/request-to-output-standard.md",
    "docs/runbooks/pr-acceptance-checklist.md",
    "docs/reviews/kri-559-journey-verification.md",
    "docs/runbooks/agent-navigation.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/workflows/pr-evidence.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/ci-changes.yml",
    "scripts/preship-check.sh",
    "scripts/ci/pr-evidence.py",
    "scripts/ci/test_pr_evidence.py",
}
ISSUE = re.compile(
    r"(?:KRI-[1-9]\d*|https://linear\.app/kria/issue/KRI-[1-9]\d*(?:/[a-zA-Z0-9-]+)?)\Z"
)
SHA = re.compile(r"[0-9a-f]{40}\Z")
PLACEHOLDER = re.compile(
    r"\b(?:todo|tbd|placeholder|replace[_ -]?(?:me|with|this)|fill[_ -]?(?:in|this|here))\b|<[^>]+>",
    re.I,
)
COMMENTS = re.compile(r"<!--[\s\S]*?-->")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def substantive(value, field, minimum=12):
    require(isinstance(value, str), f"{field}: must be text")
    visible = COMMENTS.sub("", value).strip()
    require(
        len(visible) >= minimum and not PLACEHOLDER.search(visible),
        f"{field}: replace placeholders/comments with a concrete explanation",
    )
    require(
        visible.lower()
        not in {"not applicable", "not available", "none", "n/a", "pending"},
        f"{field}: needs a concrete explanation",
    )
    return visible


def git(repo, *args):
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], stderr=subprocess.PIPE
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise ValueError(
            f"git inventory/object read failed ({args[0]}); verify --repo, --base and --head"
        ) from error


def commit(repo, ref):
    require(
        isinstance(ref, str) and ref and not ref.startswith("-"),
        "git commit reference is missing or invalid",
    )
    sha = (
        git(repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        .decode()
        .strip()
    )
    require(SHA.fullmatch(sha), "git returned an invalid commit SHA")
    return sha


class Scope(NamedTuple):
    repo: Path
    head: str
    ancestor: str
    repository: str | None


def merge_base(repo, base, head):
    ancestor = git(repo, "merge-base", base, head).decode().strip()
    require(SHA.fullmatch(ancestor), "git returned an invalid merge-base SHA")
    return ancestor


PINNED_DIFF = (
    "-c",
    "core.quotePath=true",
    "-c",
    "diff.suppressBlankEmpty=false",
    "diff",
    "--no-renames",
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-relative",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--inter-hunk-context=0",
    "--binary",
    "-U1",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


def pinned_git(repo, *args, stdin=None):
    """Run git with no user or system config so every machine hashes the same diff."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            check=True,
            env=env,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", b"") or b""
        raise ValueError(
            "git failed while fingerprinting the PR patch"
            + (f": {detail.decode(errors='replace').strip()}" if detail else "")
        ) from error


def patch_id(repo, ancestor, head):
    """Fingerprint of what the PR changes, independent of the commits carrying it.

    Two properties are chosen deliberately.
    - --verbatim keeps whitespace, so re-indenting a line (which moves code in or
      out of an if/for block in Python) changes the id. Plain --stable ignores it.
    - -U1 keeps one line of context on each side of a hunk, so moving an added
      line to a different place changes the id. -U0 hid that. The cost is that a
      base edit directly adjacent to the PR's hunks also changes the id; the
      failure is closed and the author recomputes with --patch-id.
    Line numbers stay out of the hash, so a rebase over distant edits keeps it.
    --binary makes a changed video/image alter it. Diffing from the merge-base keeps
    merge commits that only bring in the base branch out of it. The diff settings
    and an empty git config are pinned so a local diff.algorithm or color.ui cannot
    produce an id CI would not.
    """
    diff = pinned_git(repo, *PINNED_DIFF, ancestor, head, "--")
    output = pinned_git(repo, "patch-id", "--verbatim", stdin=diff)
    fields = output.split()
    require(
        len(fields) >= 1 and re.fullmatch(rb"[0-9a-f]{40}", fields[0]),
        "PR has no patch to fingerprint (empty diff against the base branch)",
    )
    return fields[0].decode()


def is_ancestor(repo, candidate, head):
    try:
        return (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "merge-base",
                    "--is-ancestor",
                    candidate,
                    head,
                ],
                capture_output=True,
            ).returncode
            == 0
        )
    except OSError as error:
        raise ValueError("git ancestry check failed") from error


def changed_paths(repo, base, head, working):
    ancestor = merge_base(repo, base, head)
    raw = git(repo, "diff", "--no-renames", "--name-only", "-z", ancestor, head, "--")
    if working:
        require(
            commit(repo, "HEAD") == head,
            "--working requires --head to be the current checkout HEAD",
        )
        raw += git(repo, "diff", "--no-renames", "--name-only", "-z", head, "--")
        raw += git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    paths = sorted({p.decode("utf-8") for p in raw.split(b"\0") if p})
    return paths


def policy():
    path = Path(__file__).with_name("select-tests.py")
    spec = importlib.util.spec_from_file_location("trusted_evidence_selector", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except (OSError, ValueError, KeyError, ImportError) as error:
        raise ValueError(
            "trusted baseline suite/journey policy could not be loaded"
        ) from error
    return module


def requirements(repo, base, head, working):
    selector = policy()
    paths = changed_paths(repo, base, head, working)
    affected = [
        path
        for path in paths
        if path in POLICY_PATHS
        or selector.journey_affected(path)
        or selector.affected(path)
        or path.startswith("src/apps/web/")
    ]
    return {"required": bool(affected), "affected_paths": affected}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(path.read_text(), object_pairs_hook=unique_object)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read JSON file {path.name}: {error}") from error


def event_context(path):
    event = read_json(path)
    require(
        isinstance(event, dict) and isinstance(event.get("pull_request"), dict),
        "event must identify a pull_request",
    )
    pr = event["pull_request"]
    try:
        repo = event["repository"]["full_name"]
        require(
            isinstance(repo, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo),
            "event repository identity is invalid",
        )
        require(
            pr["base"]["repo"]["full_name"] == repo,
            "event base repository does not match event repository",
        )
        require(
            re.fullmatch(
                r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", pr["head"]["repo"]["full_name"]
            ),
            "event head repository identity is invalid",
        )
        require(
            SHA.fullmatch(pr["base"]["sha"]) and SHA.fullmatch(pr["head"]["sha"]),
            "event base/head SHA is invalid",
        )
    except (KeyError, TypeError) as error:
        raise ValueError(
            "event lacks valid base/head repository and SHA identity"
        ) from error
    return pr, repo


def extract(body):
    require(isinstance(body, str), "PR body is missing")
    visible = COMMENTS.sub("", body.replace("\r\n", "\n"))
    openings = re.findall(r"^```request-evidence[^\n]*$", visible, re.M)
    blocks = re.findall(
        r"^```request-evidence[ \t]*\n(.*?)^```[ \t]*$", visible, re.M | re.S
    )
    require(
        len(openings) == len(blocks) == 1,
        "include exactly one visible fenced request-evidence JSON block",
    )
    try:
        value = json.loads(blocks[0], object_pairs_hook=unique_object)
    except json.JSONDecodeError as error:
        raise ValueError(f"request-evidence is malformed JSON: {error.msg}") from error
    require(isinstance(value, dict), "request-evidence must be a JSON object")
    return value


def artifact(value, scope):
    repo, head, repository = scope.repo, scope.head, scope.repository
    value = substantive(value, "artifact", minimum=3)
    if "://" in value:
        url = urlsplit(value)
        require(
            url.scheme == "https"
            and url.netloc == "github.com"
            and not url.query
            and not url.fragment,
            "artifact URL must be an immutable GitHub URL without credentials/query/fragment",
        )
        parts = url.path.strip("/").split("/")
        require(len(parts) >= 4 and all(parts), "artifact URL is incomplete")
        require(
            not repository or "/".join(parts[:2]) == repository,
            "artifact repository differs from PR base repository",
        )
        tail = "/".join(parts[2:])
        pinned_blob = re.fullmatch(r"blob/([0-9a-f]{40})/(.+)", tail)
        pinned_commit = re.fullmatch(r"commit/([0-9a-f]{40})", tail)
        if pinned_blob or pinned_commit:
            match = pinned_blob or pinned_commit
            sha = match[1]
            if pinned_blob:
                require(
                    is_ancestor(repo, sha, head),
                    "artifact blob commit must be the PR head or one of its ancestors; "
                    "after a rebase use a repo-relative path",
                )
                path = pinned_blob[2]
                require(
                    git(
                        repo,
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        f"{sha}:{path}",
                    )
                    == git(
                        repo,
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        f"{head}:{path}",
                    ),
                    "artifact blob differs between its pinned commit and the PR head",
                )
                return artifact(path, scope)
            own = git(repo, "rev-list", f"{scope.ancestor}..{head}").decode().split()
            require(
                sha in own,
                "artifact commit must be one of this PR's own commits; after a rebase use a repo-relative path",
            )
            return f"commit:{sha}"
        require(
            re.fullmatch(
                r"actions/runs/[1-9]\d*/(?:attempts/[1-9]\d*|artifacts/[1-9]\d*)", tail
            ),
            "artifact URL needs a full head commit/blob, run attempt, or numbered Actions artifact",
        )
        return value
    path = PurePosixPath(value)
    require(
        not path.is_absolute()
        and "\\" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
        and not value.startswith("-"),
        "artifact must be a safe repository-relative path",
    )
    # ls-tree gives the Git object mode; never follow worktree symlinks.
    tree = git(repo, "ls-tree", "-z", head, "--", value)
    entries = [item for item in tree.split(b"\0") if item]
    require(len(entries) == 1, f"artifact missing from declared head: {value}")
    metadata, actual_path = entries[0].split(b"\t", 1)
    mode, kind, oid = metadata.split()
    require(
        actual_path.decode() == value
        and mode in {b"100644", b"100755"}
        and kind == b"blob",
        f"artifact must be a regular Git file, not a directory/symlink/submodule: {value}",
    )
    require(
        int(git(repo, "cat-file", "-s", oid.decode()).strip()) > 0,
        f"artifact is empty: {value}",
    )

    return f"git:{head}:{value}"


def validate(value, scope, patch):
    head = scope.head
    identity = IDENTITY_FIELDS & set(value)
    require(
        len(identity) == 1
        and FIELDS | identity <= set(value) <= FIELDS | identity | {"request_artifact"},
        f"request-evidence requires fields {', '.join(sorted(FIELDS | {'patch_id'}))}",
    )
    require(
        type(value["schema_version"]) is int and value["schema_version"] == 1,
        "schema_version must be 1",
    )
    if "patch_id" in value:
        require(
            value["patch_id"] == patch,
            "patch_id does not match this PR's patch: the code changed after the "
            "evidence was recorded, or the evidence belongs to another change. "
            "Recompute with: python3 scripts/ci/pr-evidence.py --patch-id --base origin/main --head HEAD",
        )
    else:
        # Receipts written before patch_id existed; drop once no open PR carries one.
        require(
            value["head_sha"] == head,
            "head_sha must match the exact current PR head SHA (or switch to patch_id)",
        )
    require(
        value["claim"] in {"local_fix", "request_fulfillment"},
        "claim must be local_fix or request_fulfillment",
    )
    require(
        isinstance(value["issue"], str) and ISSUE.fullmatch(value["issue"]),
        "issue must name a KRI ticket or its Linear issue URL",
    )
    for field in (
        "original_request",
        "request_provenance",
        "expected_outcome",
        "root_cause",
    ):
        substantive(value[field], field)
    checks = value["checks"]
    require(
        isinstance(checks, list) and all(isinstance(row, dict) for row in checks),
        "checks must be an array of objects",
    )
    require(
        len(checks) == len(KINDS) and {row.get("kind") for row in checks} == KINDS,
        "checks require exactly one of each kind: " + ", ".join(sorted(KINDS)),
    )
    for row in checks:
        kind = row["kind"]
        require(set(row) <= ROW_FIELDS, f"{kind}: unknown check fields")
        require(
            row.get("status") in {"passed", "pending", "not_applicable"},
            f"{kind}: invalid status",
        )
        require(row.get("evidence_level") in LEVELS, f"{kind}: invalid evidence_level")
        status = row["status"]
        if status == "passed":
            require(
                row["evidence_level"] not in {"pending", "not_applicable"},
                f"{kind}: passed requires an executed evidence_level",
            )
            substantive(row.get("command"), f"{kind}.command", minimum=3)
            actual = artifact(row.get("artifact"), scope)
            if kind in CONTENT_KINDS:
                try:
                    expected = artifact(row.get("expectation_source"), scope)
                except ValueError as error:
                    raise ValueError(f"{kind}.expectation_source: {error}") from error
                if kind in {"wrong_content_control", "request_fulfillment"}:
                    require(
                        expected != actual,
                        f"{kind}.expectation_source must reference an artifact distinct from the generated result",
                    )
            if kind == "rollout":
                require(
                    row["evidence_level"] == "production",
                    "rollout passed requires production evidence_level",
                )
        else:
            require(
                row["evidence_level"] == status,
                f"{kind}: evidence_level must match {status} status",
            )
            substantive(row.get("reason"), f"{kind}.reason")
            if status == "pending":
                require(
                    isinstance(row.get("owner_issue"), str)
                    and ISSUE.fullmatch(row["owner_issue"]),
                    f"{kind}.owner_issue must name the ticket owning the remaining gap",
                )
    require(
        any(row["status"] == "passed" and row["kind"] != "rollout" for row in checks),
        "local_fix requires at least one passed implementation check with executed evidence",
    )
    by_kind = {row["kind"]: row for row in checks}
    if value.get("request_artifact"):
        artifact(value["request_artifact"], scope)
    require(
        not any(
            row["status"] == "passed" and row["evidence_level"] == "production"
            for row in checks
        )
        or by_kind["rollout"]["status"] == "passed",
        "production evidence requires passed production rollout evidence",
    )
    if value["claim"] == "request_fulfillment":
        require(
            value.get("request_artifact"),
            "request_fulfillment requires request_artifact identifying the original/redacted/synthetic request fixture",
        )
        artifact(value["request_artifact"], scope)
        require(
            all(
                row["status"] == "passed" for row in checks if row["kind"] != "rollout"
            ),
            "request_fulfillment claim requires passed incident replay, wrong-content control, recovery, fulfillment, and export fidelity",
        )
        fulfillment = by_kind["request_fulfillment"]
        require(
            fulfillment["evidence_level"] in {"native_output", "production"},
            "request_fulfillment requires native_output or production evidence",
        )
        require(
            by_kind["export_fidelity"]["evidence_level"]
            in {"native_output", "production"},
            "request_fulfillment export_fidelity requires native_output or production evidence",
        )
        rollout = by_kind["rollout"]
        require(
            rollout["status"] in {"passed", "not_applicable"},
            "request_fulfillment cannot leave rollout pending; use an explicit local-only limit or production proof",
        )
    return value["claim"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--body-file", type=Path)
    parser.add_argument("--event", type=Path)
    parser.add_argument("--working", action="store_true")
    parser.add_argument(
        "--patch-id",
        action="store_true",
        help="print the patch_id for the receipt; no PR body needed",
    )
    parser.add_argument(
        "--requirements",
        action="store_true",
        help="print scope JSON; no PR body needed",
    )
    args = parser.parse_args()
    pr, repository = event_context(args.event) if args.event else (None, None)
    base = commit(args.repo, args.base or (pr and pr["base"]["sha"]))
    head = commit(args.repo, args.head or (pr and pr["head"]["sha"]))
    if pr:
        require(
            base == pr["base"]["sha"] and head == pr["head"]["sha"],
            "event base/head SHA does not match supplied --base/--head",
        )
    ancestor = merge_base(args.repo, base, head)
    if args.patch_id:
        print(patch_id(args.repo, ancestor, head))
        return
    scope = requirements(args.repo, base, head, args.working)
    if args.requirements:
        print(json.dumps(scope))
        return
    if args.working:
        require(
            not requirements(args.repo, head, head, True)["required"],
            "commit affected working changes before validating head_sha; --requirements can still report uncommitted scope",
        )
    if not scope["required"]:
        print(
            "PR request evidence not required: no affected runtime/test/policy paths."
        )
        return
    try:
        body = args.body_file.read_text() if args.body_file else pr and pr.get("body")
    except OSError as error:
        raise ValueError(
            "affected paths require a PR body: supply --body-file (local default .dev/pr-body.md) or --event"
        ) from error
    if pr and args.body_file:
        require(body == pr.get("body"), "body-file differs from event PR body")
    claim = validate(
        extract(body),
        Scope(args.repo, head, ancestor, repository),
        patch_id(args.repo, ancestor, head),
    )
    print(
        f"PR request evidence accepted for {claim}. This validates the receipt and references; it does not verify content, remote artifact availability, or deployment."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, UnicodeError, TypeError, KeyError) as error:
        print(f"PR request evidence: {error}", file=sys.stderr)
        sys.exit(1)
