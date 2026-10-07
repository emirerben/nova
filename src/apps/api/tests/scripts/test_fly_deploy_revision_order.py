"""Deploy-order contract tests using real commit graphs and the Fly CLI boundary.

Failure scenarios: stale/unrelated code overwrites production; a missing label
or failed Git fetch authorizes a deploy; an owned guard bypasses revalidation;
production advances after preflight; an expired/foreign lease permits mutation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.scripts import test_video_poster_backfill_workflow as launcher


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.PIPE, text=True
    ).strip()


@pytest.fixture
def graph(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "git"
    repo.mkdir()
    _git(repo, "-c", "init.templateDir=", "init", "-b", "main")
    _git(repo, "config", "user.name", "Deploy contract test")
    _git(repo, "config", "user.email", "deploy-test@example.invalid")
    revisions = {}
    for name in ("old", "current", "docs"):
        filename = "README.md" if name == "docs" else "revision.txt"
        (repo / filename).write_text(name)
        _git(repo, "add", filename)
        _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", name)
        revisions[name] = _git(repo, "rev-parse", "HEAD")
    remote = tmp_path / "origin.git"
    _git(repo, "clone", "--bare", str(repo), str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "fetch", "origin")
    _git(repo, "checkout", "--detach", revisions["current"])
    return repo, revisions


def _check(tmp_path: Path, graph, *, live: str, candidate: str | None = None, **kwargs):
    repo, revisions = graph
    candidate = candidate or revisions["current"]
    guard = launcher._deploy_guard(revision=candidate)
    return launcher._run(
        tmp_path / "cli",
        [launcher._image(labels={launcher.REVISION_LABEL: live})],
        expected_sha=candidate,
        git_repo=repo,
        machine_sequence=[launcher._inventory(guard)],
        args=["--check-deploy-revision"],
        **kwargs,
    )


@pytest.mark.parametrize("live_name", ["old", "current"])
def test_same_or_newer_revision_accepts_docs_only_main_advance(tmp_path, graph, live_name):
    result = _check(tmp_path, graph, live=graph[1][live_name])
    assert result.returncode == 0, result.stderr
    assert "Validated deploy revision order" in result.stdout
    assert not (tmp_path / "cli" / "create.args").exists()
    assert not (tmp_path / "cli" / "destroy.args").exists()


def test_stale_acquisition_is_rejected_before_any_fly_mutation(tmp_path, graph):
    repo, revisions = graph
    _git(repo, "checkout", "--detach", revisions["old"])
    result = launcher._run(
        tmp_path / "cli",
        [launcher._image(labels={launcher.REVISION_LABEL: revisions["current"]})],
        expected_sha=revisions["old"],
        git_repo=repo,
        machine_sequence=[launcher._inventory(launcher._deploy_guard(revision=revisions["old"]))],
        args=["--acquire-deploy-guard"],
    )
    assert result.returncode != 0
    assert revisions["old"] in result.stderr and revisions["current"] in result.stderr
    assert not (tmp_path / "cli" / "create.args").exists()
    assert not (tmp_path / "cli" / "destroy.args").exists()
    assert not (tmp_path / "cli" / "start.args").exists()


def test_owned_guard_does_not_allow_older_revision(tmp_path, graph):
    result = _check(tmp_path, graph, live=graph[1]["docs"])
    assert result.returncode != 0
    assert "not an ancestor of expected deploy" in result.stderr


def test_new_revert_commit_is_allowed(tmp_path, graph):
    repo, revisions = graph
    _git(repo, "checkout", "main")
    _git(repo, "-c", "core.hooksPath=/dev/null", "revert", "--no-edit", revisions["current"])
    reverted = _git(repo, "rev-parse", "HEAD")
    _git(repo, "push", "origin", "main")
    assert (repo / "revision.txt").read_text() == "old"
    result = _check(tmp_path, graph, live=revisions["current"], candidate=reverted)
    assert result.returncode == 0, result.stderr


def test_owned_guard_reuse_rechecks_production_after_initial_proof(tmp_path, graph):
    repo, revisions = graph
    new_digest = f"sha256:{'d' * 64}"
    guard = launcher._deploy_guard(revision=revisions["current"])
    old_inventory = launcher._inventory(guard)
    result = launcher._run(
        tmp_path / "cli",
        [
            launcher._image(labels={launcher.REVISION_LABEL: revisions["old"]}),
            launcher._image(digest=new_digest, labels={launcher.REVISION_LABEL: revisions["docs"]}),
        ],
        expected_sha=revisions["current"],
        git_repo=repo,
        machine_sequence=[
            old_inventory,
            old_inventory,
            old_inventory,
            launcher._inventory(guard, digest=new_digest),
        ],
        args=["--acquire-deploy-guard"],
    )
    assert result.returncode != 0
    assert revisions["docs"] in result.stderr
    assert not (tmp_path / "cli" / "create.args").exists()
    assert not (tmp_path / "cli" / "destroy.args").exists()


def test_unrelated_live_revision_is_rejected(tmp_path, graph):
    repo, revisions = graph
    _git(repo, "checkout", "--orphan", "unrelated")
    (repo / "revision.txt").write_text("unrelated")
    _git(repo, "add", "revision.txt")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "unrelated")
    unrelated = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "--detach", revisions["current"])
    result = _check(tmp_path, graph, live=unrelated)
    assert result.returncode != 0
    assert unrelated in result.stderr


def test_checkout_mismatch_and_unmerged_candidate_are_rejected(tmp_path, graph):
    repo, revisions = graph
    mismatch = _check(
        tmp_path / "mismatch", graph, live=revisions["old"], candidate=revisions["old"]
    )
    assert mismatch.returncode != 0
    assert "does not match EXPECTED_SHA" in mismatch.stderr
    _git(repo, "checkout", "-b", "unmerged")
    (repo / "revision.txt").write_text("unmerged feature")
    _git(repo, "add", "revision.txt")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "unmerged")
    candidate = _git(repo, "rev-parse", "HEAD")
    unmerged = _check(tmp_path / "unmerged", graph, live=revisions["old"], candidate=candidate)
    assert unmerged.returncode != 0
    assert "not an ancestor of origin/main" in unmerged.stderr


def test_unknown_history_and_failed_fetch_cannot_authorize_deploy(tmp_path, graph):
    unknown = _check(tmp_path / "unknown", graph, live="c" * 40)
    assert unknown.returncode != 0
    assert "refusing deploy" in unknown.stderr
    _git(graph[0], "remote", "set-url", "origin", str(tmp_path / "missing-origin.git"))
    failed_fetch = _check(tmp_path / "fetch", graph, live=graph[1]["old"])
    assert failed_fetch.returncode != 0
    assert "Could not fetch origin/main" in failed_fetch.stderr


@pytest.mark.parametrize("labels", [{}, {launcher.REVISION_LABEL: "invalid"}, "not-json"])
def test_missing_or_malformed_revision_label_fails_closed(tmp_path, graph, labels):
    repo, revisions = graph
    result = launcher._run(
        tmp_path / "cli",
        [launcher._image(labels=labels)],
        expected_sha=revisions["current"],
        git_repo=repo,
        machine_sequence=[
            launcher._inventory(launcher._deploy_guard(revision=revisions["current"]))
        ],
        args=["--check-deploy-revision"],
    )
    assert result.returncode != 0
    assert "Could not resolve one deployed image revision label" in result.stderr


def test_conflicting_labels_and_failed_image_read_fail_closed(tmp_path, graph):
    repo, revisions = graph
    images = [
        launcher._image(labels={launcher.REVISION_LABEL: revisions[name]})
        for name in ("old", "current")
    ]
    guard = launcher._deploy_guard(revision=revisions["current"])
    conflict = launcher._run(
        tmp_path / "conflict",
        images,
        expected_sha=revisions["current"],
        git_repo=repo,
        machine_sequence=[launcher._inventory(guard)],
        args=["--check-deploy-revision"],
    )
    assert conflict.returncode != 0
    assert "Could not resolve one deployed image revision label" in conflict.stderr
    failed_read = _check(tmp_path / "failed-read", graph, live=revisions["old"], image_exit=1)
    assert failed_read.returncode != 0
    assert "Could not read Fly production image metadata" in failed_read.stderr


@pytest.mark.parametrize(
    "guard_args",
    [{"owner": "999:1"}, {"created_epoch": launcher.NOW_EPOCH - launcher.DEPLOY_LEASE_S - 1}],
)
def test_foreign_or_expired_guard_blocks_deploy(tmp_path, graph, guard_args):
    repo, revisions = graph
    guard = launcher._deploy_guard(revision=revisions["current"], **guard_args)
    result = launcher._run(
        tmp_path / "cli",
        [launcher._image(labels={launcher.REVISION_LABEL: revisions["old"]})],
        expected_sha=revisions["current"],
        git_repo=repo,
        machine_sequence=[launcher._inventory(guard)],
        args=["--check-deploy-revision"],
    )
    assert result.returncode != 0
    assert "unexpired lease" in result.stderr


@pytest.mark.parametrize("remaining, allowed", [(2159, False), (2160, True)])
def test_initial_deploy_requires_lease_for_entire_timeout(tmp_path, graph, remaining, allowed):
    repo, revisions = graph
    guard = launcher._deploy_guard(
        revision=revisions["current"],
        created_epoch=launcher.NOW_EPOCH - launcher.DEPLOY_LEASE_S + remaining,
    )
    result = launcher._run(
        tmp_path / "cli",
        [launcher._image(labels={launcher.REVISION_LABEL: revisions["old"]})],
        expected_sha=revisions["current"],
        git_repo=repo,
        machine_sequence=[launcher._inventory(guard)],
        args=["--check-deploy-revision"],
        extra_env={"DEPLOY_GUARD_MIN_REMAINING_S": "2160"},
    )
    assert (result.returncode == 0) == allowed, result.stderr
    if not allowed:
        assert "lease" in result.stderr


def test_production_advancing_after_acquisition_is_rechecked(tmp_path, graph):
    repo, revisions = graph
    new_digest = f"sha256:{'d' * 64}"
    guard = launcher._deploy_guard(revision=revisions["current"])
    old_inventory = launcher._inventory()
    result = launcher._run(
        tmp_path / "cli",
        [
            launcher._image(labels={launcher.REVISION_LABEL: revisions["old"]}),
            launcher._image(digest=new_digest, labels={launcher.REVISION_LABEL: revisions["docs"]}),
        ],
        expected_sha=revisions["current"],
        git_repo=repo,
        machine_sequence=[
            old_inventory,
            old_inventory,
            old_inventory,
            old_inventory,
            launcher._inventory(guard),
            launcher._inventory(guard, digest=new_digest),
        ],
        args=["--acquire-deploy-guard"],
    )
    assert result.returncode != 0
    assert revisions["docs"] in result.stderr
    assert "CALL" in (tmp_path / "cli" / "create.args").read_text()
    assert not (tmp_path / "cli" / "destroy.args").exists()


@pytest.mark.parametrize("change", ["production", "owner"])
def test_retry_with_real_checker_rejects_changed_production_or_guard(
    tmp_path, graph, monkeypatch, change
):
    repo, revisions = graph
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "run-video-poster-backfill.sh").write_text(launcher.SCRIPT.read_text())
    monkeypatch.setattr(launcher, "SCRIPT", launcher.REPO_ROOT / "scripts/fly-deploy-with-retry.sh")
    deploy_stub = r"""
if [[ "$1" == "deploy" ]]; then
  echo CALL >> "$STUB_DEPLOY_ARGS"
  echo 'Machine abc123 has state: destroyed'
  echo 'release_command failed running on machine abc123 with exit code 143'
  exit 1
fi
"""
    monkeypatch.setattr(
        launcher,
        "_STUB_FLYCTL",
        launcher._STUB_FLYCTL.replace('if [[ "$1 $2"', deploy_stub + 'if [[ "$1 $2"', 1),
    )
    guard = launcher._deploy_guard(revision=revisions["current"])
    new_digest = f"sha256:{'d' * 64}"
    changed_guard = launcher._deploy_guard(revision=revisions["current"], owner="999:1")
    changed_inventory = (
        launcher._inventory(guard, digest=new_digest)
        if change == "production"
        else launcher._inventory(changed_guard)
    )
    deploy_args = tmp_path / "deploy.args"
    result = launcher._run(
        tmp_path / "cli",
        [
            launcher._image(labels={launcher.REVISION_LABEL: revisions["old"]}),
            launcher._image(digest=new_digest, labels={launcher.REVISION_LABEL: revisions["docs"]}),
        ],
        expected_sha=revisions["current"],
        git_repo=repo,
        machine_sequence=[
            launcher._inventory(guard),
            launcher._inventory(guard),
            changed_inventory,
        ],
        args=["--remote-only"],
        extra_env={"STUB_DEPLOY_ARGS": str(deploy_args), "FLY_DEPLOY_RETRY_DELAY_SECONDS": "0"},
    )
    assert result.returncode != 0
    assert deploy_args.read_text().splitlines() == ["CALL"]
    assert "refusing deploy" in result.stderr
    if change == "production":
        assert revisions["docs"] in result.stderr
    else:
        assert "not currently owned" in result.stderr
