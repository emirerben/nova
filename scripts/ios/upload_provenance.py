"""Resolve uploaded binary sources from release job evidence, never run headSha.

Read-only GitHub/git operations. Missing evidence fails closed. Legacy releases
are supported by checkout's exact commit output and Fastlane's transfer receipt.
"""

import json
import os
import re
import subprocess
import sys

ARCHIVE = "Archive signed Release IPA"
UPLOAD = "Upload and distribute to external TestFlight"
SHA = re.compile(r"[0-9a-f]{40}")
ANSI = re.compile(r"(?:\x1b|\^\[)\[[0-9;]*m")


class ProvenanceError(RuntimeError):
    pass


def command(*args):
    return subprocess.check_output(args, text=True)


def uploaded_source(attempt, log):
    steps = {step["name"]: step["conclusion"] for step in attempt["steps"]}
    if steps.get(UPLOAD) == "skipped":
        return None
    if UPLOAD not in steps:
        raise ProvenanceError(f"Run {attempt['id']}: upload step missing")
    if steps.get(ARCHIVE) != "success":
        raise ProvenanceError(f"Run {attempt['id']}: upload without a verified archive")
    checkout = set()
    transferred = False
    duplicate = False
    previous_step = previous_body = runner_step = ""
    for line in log.splitlines():
        fields = ANSI.sub("", line).split("\t", 2)
        if len(fields) != 3:
            continue
        job, step, timestamped = fields
        if job != "release":
            continue
        body = timestamped.split(" ", 1)[-1].strip()
        # gh occasionally loses step labels in older log archives. Recover only
        # exact runner command boundaries, never arbitrary text or workflow SHA.
        if body.startswith("##[group]Run "):
            runner_step = ""
            if re.fullmatch(r"##\[group\]Run actions/checkout@v[0-9]+", body):
                runner_step = body.removeprefix("##[group]")
            elif re.fullmatch(
                r"##\[group\]Run BUNDLE_GEMFILE=fastlane/Gemfile bundle _[0-9.]+_ exec fastlane ios testflight",
                body,
            ):
                runner_step = UPLOAD
        if step == "UNKNOWN STEP":
            step = runner_step
        if (
            step.startswith("Run actions/checkout@")
            and step == previous_step
            and previous_body.endswith("git log -1 --format=%H")
            and SHA.fullmatch(body)
        ):
            checkout.add(body)
        if step == UPLOAD:
            transferred |= bool(
                re.fullmatch(
                    r"\[\d{2}:\d{2}:\d{2}\]: Successfully uploaded the new binary to App Store Connect",
                    body,
                )
            )
            duplicate |= bool(
                re.fullmatch(
                    r"\[\d{2}:\d{2}:\d{2}\]: TestFlight already has build \d+ for [^;]+; skipping duplicate upload \d+\.",
                    body,
                )
            )
        previous_step, previous_body = step, body
    if duplicate and not transferred and steps[UPLOAD] == "success":
        return None
    if not transferred or duplicate or len(checkout) != 1:
        raise ProvenanceError(
            f"Run {attempt['id']}: missing or ambiguous upload/source evidence"
        )
    return checkout.pop()


def sources(attempts, read_log):
    result = []
    for attempt in sorted(attempts, key=lambda a: a["completedAt"], reverse=True):
        steps = {step["name"]: step["conclusion"] for step in attempt["steps"]}
        if steps.get(UPLOAD) == "skipped":
            continue
        source = uploaded_source(attempt, read_log(attempt["id"]))
        if source:
            result.append(source)
    if not result:
        raise ProvenanceError(
            "No verified upload in retained history; reconcile release provenance before retrying"
        )
    return result


def latest_source(attempts, read_log):
    return sources(attempts, read_log)[0]


def ancestor(a, b, repo):
    result = subprocess.run(
        ["git", "-C", repo, "merge-base", "--is-ancestor", a, b], capture_output=True
    )
    if result.returncode not in (0, 1):
        raise ProvenanceError("Cannot verify commit ancestry")
    return result.returncode == 0


def compare_source(head, source, main, repo="."):
    if not SHA.fullmatch(source) or not SHA.fullmatch(head):
        raise ProvenanceError("Source and candidate must be full commit SHAs")
    if not ancestor(source, main, repo) or not ancestor(head, main, repo):
        raise ProvenanceError("Candidate or uploaded source is not on current main")
    if ancestor(head, source, repo):
        return "superseded"
    if ancestor(source, head, repo):
        return "base"
    raise ProvenanceError("Uploaded source and candidate have divergent ancestry")


def history_attempts(runs, current_id):
    result = []
    for run in runs:
        last = int(run["attempt"])
        if last < 1:
            raise ProvenanceError("Invalid release attempt number")
        if str(run["databaseId"]) == current_id or run["status"] in (
            "queued",
            "pending",
            "waiting",
            "requested",
        ):
            last -= 1
        elif run["status"] != "completed":
            raise ProvenanceError(
                "Another release is active; cannot verify serialized history"
            )
        result.extend((run["databaseId"], attempt) for attempt in range(1, last + 1))
    return result


def never_executed(job):
    """Actions records no runner allocation and no executed steps.

    Require every raw assignment field: gh run view omits these fields, so its
    empty steps/cancelled summary alone is insufficient. Missing or conflicting
    metadata remains unknown. The incident's platform acquisition-failure
    annotation independently confirms this no-runner state.
    """
    return (
        job.get("status") == "completed"
        and job.get("conclusion") == "cancelled"
        and job.get("runner_id") == 0
        and job.get("runner_name") == ""
        and job.get("runner_group_id") == 0
        and job.get("runner_group_name") == ""
        and job.get("steps") == []
    )


def main():
    # Include failed/cancelled workflows: transfer may have succeeded before a
    # distribution failure. Never infer an upload from workflow/step success.
    runs = json.loads(
        command(
            "gh",
            "run",
            "list",
            "--workflow",
            "testflight.yml",
            "--branch",
            "main",
            "--limit",
            "100",
            "--json",
            "databaseId,status,attempt",
        )
    )
    attempts = []
    for run_id, attempt_number in history_attempts(runs, os.environ["GITHUB_RUN_ID"]):
        jobs = json.loads(
            command(
                "gh",
                "run",
                "view",
                str(run_id),
                "--attempt",
                str(attempt_number),
                "--json",
                "jobs",
            )
        )["jobs"]
        releases = [job for job in jobs if job["name"] == "release"]
        if len(releases) != 1:
            raise ProvenanceError(
                f"Run {run_id} attempt {attempt_number}: missing or ambiguous release job"
            )
        job = releases[0]
        if job["conclusion"] == "skipped":
            continue
        if job["conclusion"] == "cancelled" and job["steps"] == []:
            # Query exact raw Actions metadata using existing actions:read.
            # Never infer no-upload from cancellation or absent logs alone.
            repo = os.environ["GITHUB_REPOSITORY"]
            job_id = str(job["databaseId"])
            detail = json.loads(
                command("gh", "api", f"repos/{repo}/actions/jobs/{job_id}")
            )
            if never_executed(detail):
                continue
        attempts.append(
            dict(
                id=(run_id, attempt_number),
                completedAt=job["completedAt"],
                steps=job["steps"],
            )
        )
    uploaded = sources(
        attempts,
        lambda key: command(
            "gh", "run", "view", str(key[0]), "--attempt", str(key[1]), "--log"
        ),
    )
    head = os.environ["HEAD_SHA"]
    base = None
    superseded = False
    for source in uploaded:
        relation = compare_source(head, source, "FETCH_HEAD")
        superseded |= relation == "superseded"
        if base is None or ancestor(base, source, "."):
            base = source
    # Both values are validated commit/boolean data, suitable for shell read.
    print(f"{base} {str(superseded).lower()}")


if __name__ == "__main__":
    try:
        main()
    except (
        ProvenanceError,
        subprocess.CalledProcessError,
        KeyError,
        ValueError,
    ) as exc:
        print(
            f"::error::TestFlight provenance verification failed: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
