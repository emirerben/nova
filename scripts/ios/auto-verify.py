#!/usr/bin/env python3
"""Queue offline iPhone verification locally, without waiting or changing CI."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

import feature_map

CONTROL = ".cursor/skills/verify-kria-ios/control-kria-ios"
TERMINAL = {"passed", "passed_with_retries", "failed", "interrupted", "unverified"}
HOOKS = (
    "applypatch-msg",
    "pre-applypatch",
    "post-applypatch",
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-rebase",
    "post-checkout",
    "post-merge",
    "pre-push",
    "post-rewrite",
    "pre-auto-gc",
    "push-to-checkout",
    "reference-transaction",
    "pre-merge-commit",
)


def git(repo, *args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args],
        text=True,
        stderr=subprocess.PIPE,
    ).strip()


def common_dir(repo):
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def state_root(repo):
    override = os.environ.get("KRIA_VERIFY_AUTO_ROOT")
    return (
        Path(override).resolve()
        if override
        else common_dir(repo).parent / "test-results/verify-kria-ios/auto"
    )


def save(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


@contextmanager
def lock(root, name):
    root.mkdir(parents=True, exist_ok=True)
    with (root / name).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def disabled(repo):
    if os.environ.get("CI") or os.environ.get("KRIA_VERIFY_AUTO_DISABLED") == "1":
        return True
    try:
        return git(repo, "config", "--get", "kria.verifyIos") == "false"
    except subprocess.CalledProcessError:
        return False


def install(repo):
    if disabled(repo):
        return
    common = common_dir(repo)
    destination = common / "kria-verification-hooks"
    with lock(common, "kria-verification-install.lock"):
        configured = True
        try:
            previous = git(repo, "config", "--get", "core.hooksPath")
        except subprocess.CalledProcessError:
            configured = False
            previous = str(common / "hooks")
        if Path(previous) != destination:
            destination.mkdir(exist_ok=True)
            save(
                destination / "previous.json",
                {"path": previous, "configured": configured},
            )
        dispatcher = destination / "dispatch.py"
        temporary = destination / "dispatch.tmp"
        shutil.copy2(repo / "scripts/hooks/kria-verification-dispatch.py", temporary)
        temporary.replace(dispatcher)
        previous_path = Path(
            json.loads((destination / "previous.json").read_text())["path"]
        )
        existing = {
            p.name for p in previous_path.glob("*") if not p.name.endswith(".sample")
        }
        for event in set(HOOKS) | existing:
            wrapper = destination / event
            temporary = destination / (event + ".tmp")
            temporary.write_text(
                f'#!/bin/sh\nexec python3 {shlex.quote(str(dispatcher))} {shlex.quote(event)} "$@"\n'
            )
            temporary.chmod(0o755)
            temporary.replace(wrapper)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "config",
                "--local",
                "core.hooksPath",
                str(destination),
            ],
            check=True,
        )
    print(
        "[Kria verification] local pull/push hooks installed (existing hooks preserved)."
    )


def uninstall(repo):
    common = common_dir(repo)
    destination = common / "kria-verification-hooks"
    if git(repo, "config", "--get", "core.hooksPath") != str(destination):
        raise ValueError("Hook configuration changed; refusing to replace it")
    config = json.loads((destination / "previous.json").read_text())
    args = (
        ["core.hooksPath", config["path"]]
        if config["configured"]
        else ["--unset", "core.hooksPath"]
    )
    subprocess.run(["git", "-C", str(repo), "config", "--local", *args], check=True)
    print("[Kria verification] restored previous Git hooks; evidence retained.")


def wake(repo, root):
    with (root / "worker.log").open("a") as output:
        subprocess.Popen(
            [
                sys.executable,
                str(repo / "scripts/ios/auto-verify.py"),
                "worker",
                "--root",
                str(root),
            ],
            cwd=repo,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def enqueue(repo, reason, base=None, head=None):
    if disabled(repo):
        return
    pushed_head = head is not None
    head = git(repo, "rev-parse", (head or "HEAD") + "^{commit}")
    root = state_root(repo)
    with lock(root, "queue.lock"):
        seen_file = root / "seen.json"
        seen = json.loads(seen_file.read_text()) if seen_file.exists() else {}
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if pushed_head:
            branch = "push:" + head
        previous = seen.get(branch)
        if base is None:
            if reason == "post-merge":
                base = git(repo, "rev-parse", "ORIG_HEAD")
            elif branch in {"main", "dev"}:
                base = previous
            else:
                try:
                    base = git(repo, "merge-base", "origin/main", head)
                except subprocess.CalledProcessError:
                    base = previous
        if base is None:
            try:
                base = git(repo, "rev-parse", head + "^")
            except subprocess.CalledProcessError:
                pass
        paths = (
            git(repo, "diff", "--no-renames", "--name-only", base, head).splitlines()
            if base
            else git(
                repo, "diff-tree", "--root", "--no-commit-id", "--name-only", "-r", head
            ).splitlines()
        )
        data = json.loads(git(repo, "show", f"{head}:{feature_map.MAP}"))
        features = feature_map.features_for_paths(paths, data["features"])
        seen[branch] = head
        save(seen_file, seen)
        if not features:
            if any(
                json.loads(file.read_text())["status"] in {"queued", "running"}
                for file in (root / "requests").glob("*.json")
            ):
                wake(repo, root)
            return
        tests = feature_map.tests_for_features(features, data["features"])
        policy = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[
            :12
        ]
        requests = root / "requests"
        requests.mkdir(exist_ok=True)
        # Reuse broader coverage too, including an already reported failure.
        rows = sorted(
            (json.loads(file.read_text()) for file in requests.glob("*.json")),
            key=lambda row: row["created_at"],
            reverse=True,
        )
        for row in rows:
            if (
                row["head"] == head
                and row["policy"] == policy
                and set(features) <= set(row["features"])
            ):
                print(
                    f"[Kria verification] {head[:8]} already {row['status']}; {row['evidence']}"
                )
                if row["status"] in {"queued", "running"}:
                    wake(repo, root)
                return
        suffix = hashlib.sha256(",".join(features).encode()).hexdigest()[:8]
        key = f"{head[:12]}-{policy}-{suffix}"
        evidence = root / "runs" / key
        evidence.mkdir(parents=True)
        row = {
            "key": key,
            "head": head,
            "base": base,
            "policy": policy,
            "features": features,
            "tests": tests,
            "reason": reason,
            "common": str(common_dir(repo)),
            "status": "queued",
            "created_at": timestamp(),
            "evidence": str(evidence),
        }
        save(requests / f"{key}.json", row)
        wake(repo, root)
    print(
        f"[Kria verification] queued {head[:8]} ({', '.join(features)}) in background; {evidence}"
    )


def notify(row):
    message = f"{row['head'][:8]}: {row['status']}. Evidence: {row['evidence']}"
    print("[Kria verification] " + message, flush=True)
    if (
        sys.platform == "darwin"
        and shutil.which("osascript")
        and os.environ.get("KRIA_VERIFY_AUTO_NOTIFICATIONS") != "0"
    ):
        try:
            subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'display notification {json.dumps(message)} with title "Kria iPhone verification"',
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        except subprocess.SubprocessError:
            pass


def control(snapshot, row, command, output, request):
    args = [str(snapshot / CONTROL), command, "--run", row["key"]]
    if command == "drive":
        args += ["--feature", ",".join(row["features"]), *row["tests"]]
    output.write(json.dumps({"argv": args, "cwd": str(snapshot)}) + "\n")
    output.flush()
    process = subprocess.Popen(
        args,
        cwd=snapshot,
        stdout=output,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    row["active_child"] = process.pid
    save(request, {**row, "status": "running"})
    try:
        status = process.wait(timeout=2100)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
        row.pop("active_child", None)
        save(request, {**row, "status": "running"})
    if status:
        raise RuntimeError(f"{command} exited {status}; see automation.log")


def execute(root, file):
    row = json.loads(file.read_text())
    snapshot = root / "worktrees" / row["key"]
    snapshot.parent.mkdir(exist_ok=True)
    row.update(
        status="running",
        snapshot=str(snapshot),
        started_at=timestamp(),
        worker_pid=os.getpid(),
    )
    save(file, row)
    evidence = Path(row["evidence"])
    env = dict(os.environ, KRIA_VERIFY_AUTO_DISABLED="1", KRIA_VERIFY_AUTO_SNAPSHOT="1")
    git_args = ["git", "--git-dir", row["common"], "worktree"]
    created = False
    with (evidence / "automation.log").open("a") as output:
        try:
            subprocess.run(
                [*git_args, "add", "--detach", str(snapshot), row["head"]],
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                check=True,
            )
            created = True
            if (
                feature_map.tests_for_features(
                    row["features"], feature_map.load_map(snapshot)
                )
                != row["tests"]
            ):
                raise ValueError(
                    "Snapshot journey selection does not match queued commit"
                )
            for command in ("launch", "doctor", "drive"):
                control(snapshot, row, command, output, file)
            proof = json.loads(
                (
                    snapshot
                    / "test-results/verify-kria-ios"
                    / row["key"]
                    / "proof.json"
                ).read_text()
            )
            if proof["head"] != row["head"] or proof["status"] not in {
                "passed",
                "passed_with_retries",
            }:
                raise ValueError(
                    "Proof is missing, failed or belongs to another commit"
                )
            row["status"] = proof["status"]
        except (Exception, KeyboardInterrupt) as error:
            row.update(status="failed", error=str(error))
        finally:
            source = snapshot / "test-results/verify-kria-ios" / row["key"]
            try:
                if (source / "run.json").exists():
                    control(snapshot, row, "cleanup", output, file)
                    if not json.loads((source / "run.json").read_text()).get("cleaned"):
                        raise ValueError("Owned simulator cleanup was not confirmed")
                    shutil.copytree(source, evidence, dirs_exist_ok=True)
                if created and snapshot.exists():
                    subprocess.run(
                        [*git_args, "remove", "--force", str(snapshot)],
                        env=env,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        check=True,
                    )
            except Exception as error:
                row.update(status="failed", cleanup_error=str(error))
                if source.exists():
                    shutil.copytree(source, evidence, dirs_exist_ok=True)
            row["finished_at"] = timestamp()
            save(evidence / "automation.json", row)
            save(file, row)
    notify(row)


def recover(file):
    row = json.loads(file.read_text())
    snapshot = Path(row["snapshot"])
    evidence = Path(row["evidence"])
    source = snapshot / "test-results/verify-kria-ios" / row["key"]
    with (evidence / "automation.log").open("a") as output:
        try:
            pid = row.get("active_child")
            if pid:
                try:
                    actual = subprocess.check_output(
                        ["ps", "-p", str(pid), "-o", "command="], text=True
                    ).strip()
                    if (
                        os.getpgid(pid) == pid
                        and str(snapshot / CONTROL) in actual
                        and row["key"] in actual
                    ):
                        os.killpg(pid, signal.SIGTERM)
                        for _ in range(20):
                            try:
                                os.kill(pid, 0)
                            except ProcessLookupError:
                                break
                            time.sleep(0.1)
                        else:
                            os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, subprocess.CalledProcessError):
                    pass
                row.pop("active_child", None)
            if (source / "run.json").exists():
                control(snapshot, row, "cleanup", output, file)
                if not json.loads((source / "run.json").read_text()).get("cleaned"):
                    raise ValueError("Owned simulator cleanup was not confirmed")
                shutil.copytree(source, evidence, dirs_exist_ok=True)
            if snapshot.exists():
                subprocess.run(
                    [
                        "git",
                        "--git-dir",
                        row["common"],
                        "worktree",
                        "remove",
                        "--force",
                        str(snapshot),
                    ],
                    env=dict(os.environ, KRIA_VERIFY_AUTO_SNAPSHOT="1"),
                    check=True,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                )
            row.update(
                status="interrupted",
                error="Previous worker stopped before recording completion",
            )
        except Exception as error:
            row.update(status="failed", cleanup_error=str(error))
        save(file, row)
        save(evidence / "automation.json", row)
    notify(row)


def retry(repo, key):
    root = state_root(repo)
    if not key or any(c not in "0123456789abcdef-r" for c in key):
        raise ValueError("Invalid request key; copy it from status --json")
    with lock(root, "queue.lock"):
        row = json.loads((root / "requests" / f"{key}.json").read_text())
        if row["status"] not in TERMINAL:
            raise ValueError("That request is still queued or running")
        row["key"] = key.split("-r")[0] + "-r" + uuid.uuid4().hex[:6]
        row.update(
            status="queued",
            created_at=timestamp(),
            reason="explicit retry",
            evidence=str(root / "runs" / row["key"]),
        )
        for field in (
            "error",
            "cleanup_error",
            "started_at",
            "finished_at",
            "snapshot",
            "worker_pid",
            "active_child",
        ):
            row.pop(field, None)
        Path(row["evidence"]).mkdir()
        save(root / "requests" / f"{row['key']}.json", row)
        wake(repo, root)
    print(f"[Kria verification] retry queued; {row['evidence']}")


def worker(root):
    def interrupted(*_):
        raise KeyboardInterrupt("Verification worker interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    with lock(root, "worker.lock"):
        # Owning this lock proves no other worker is using a running snapshot.
        for file in (root / "requests").glob("*.json"):
            if json.loads(file.read_text())["status"] == "running":
                recover(file)
        while True:
            with lock(root, "queue.lock"):
                queued = [
                    file
                    for file in sorted((root / "requests").glob("*.json"))
                    if json.loads(file.read_text())["status"] == "queued"
                ]
                if not queued:
                    return
            execute(root, queued[0])


def status(root, as_json=False):
    rows = sorted(
        (json.loads(file.read_text()) for file in (root / "requests").glob("*.json")),
        key=lambda row: row["created_at"],
        reverse=True,
    )
    if as_json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows[:5]:
            print(
                f"[Kria verification] {row['head'][:8]} {row['status']}: {row['evidence']}"
            )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("install", "uninstall"):
        commands.add_parser(name)
    enqueue_parser = commands.add_parser("enqueue")
    enqueue_parser.add_argument("--reason", required=True)
    enqueue_parser.add_argument("--base")
    enqueue_parser.add_argument("--head")
    retry_parser = commands.add_parser("retry")
    retry_parser.add_argument("key")
    worker_parser = commands.add_parser("worker")
    worker_parser.add_argument("--root", required=True, type=Path)
    status_parser = commands.add_parser("status")
    status_parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    repo = Path.cwd()
    try:
        if args.command == "install":
            install(repo)
        elif args.command == "uninstall":
            uninstall(repo)
        elif args.command == "enqueue":
            enqueue(repo, args.reason, args.base, args.head)
        elif args.command == "retry":
            retry(repo, args.key)
        elif args.command == "worker":
            worker(args.root)
        else:
            status(state_root(repo), args.json)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"[Kria verification] unverified: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
