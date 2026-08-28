"""Contract tests for the production video-poster rollout launcher."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
SCRIPT = REPO_ROOT / "scripts" / "run-video-poster-backfill.sh"
BACKFILL_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "video-poster-backfill.yml"
DEPLOY_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "fly-deploy.yml"
REVISION_LABEL = "org.opencontainers.image.revision"
EXPECTED_SHA = "a" * 40
DIGEST = f"sha256:{'b' * 64}"
MACHINE_ID = "abc123def45678"
FOREIGN_MACHINE_ID = "def456abc12345"
GUARD_NAME = "nova-video-production-mutation-guard"
NOW_EPOCH = 2_000_000_000
BACKFILL_LEASE_S = 18_900
DEPLOY_LEASE_S = 2_700
BACKFILL_COMMAND = (
    "cd /app && python -m scripts.backfill_video_posters "
    "--exclude-synthetic --strict --batch-size 25 && python -m "
    "scripts.backfill_video_posters --dry-run --exclude-synthetic --strict --batch-size 25"
)

_STUB_FLYCTL = r"""#!/usr/bin/env bash
set -euo pipefail

if [[ "$1 $2" == "machine list" ]]; then
  count=0
  [[ ! -f "$STUB_LIST_COUNT" ]] || count="$(cat "$STUB_LIST_COUNT")"
  printf '%s' "$((count + 1))" > "$STUB_LIST_COUNT"
  jq -c --argjson index "$count" '.[$index] // .[-1]' <<<"$STUB_MACHINE_SEQUENCE"
  exit 0
fi

if [[ "$1 $2" == "image show" ]]; then
  printf '%s\n' "$@" >> "$STUB_IMAGE_ARGS"
  if [[ "${STUB_IMAGE_EXIT:-0}" != "0" ]]; then
    exit "$STUB_IMAGE_EXIT"
  fi
  printf '%s' "$STUB_IMAGE_JSON"
  exit 0
fi

if [[ "$1 $2" == "machine create" ]]; then
  printf 'CALL\n' >> "$STUB_CREATE_ARGS"
  printf '%s\n' "$@" >> "$STUB_CREATE_ARGS"
  if [[ "${STUB_CREATE_EXIT:-0}" != "0" ]]; then
    exit "$STUB_CREATE_EXIT"
  fi
  printf 'Machine created successfully\n Machine ID: %s\n' "$STUB_MACHINE_ID"
  exit 0
fi

if [[ "$1 $2" == "machine start" ]]; then
  printf 'CALL\n' >> "$STUB_START_ARGS"
  printf '%s\n' "$@" >> "$STUB_START_ARGS"
  exit "${STUB_START_EXIT:-0}"
fi

if [[ "$1 $2" == "machine destroy" ]]; then
  printf '%s\n' "$@" >> "$STUB_DESTROY_ARGS"
  exit "${STUB_DESTROY_EXIT:-0}"
fi

if [[ "$1" == "logs" ]]; then
  printf '%s\n' "$@" > "$STUB_LOG_ARGS"
  printf 'Backfill complete: generated=1\n'
  exit "${STUB_LOG_EXIT:-0}"
fi

echo "unexpected flyctl invocation: $*" >&2
exit 97
"""

_STUB_CURL = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "$STUB_CURL_ARGS"
exit "${STUB_CURL_EXIT:-0}"
"""


def _image(
    *,
    digest: str = DIGEST,
    labels: dict[str, str] | str | None = None,
    tag: str = "deployment-test",
) -> dict:
    if labels is None:
        labels = {REVISION_LABEL: EXPECTED_SHA}
    return {
        "Digest": digest,
        "Registry": "registry.fly.io",
        "Repository": "nova-video",
        "Tag": tag,
        "Labels": labels if isinstance(labels, str) else json.dumps(labels),
    }


def _machine(
    state: str,
    *,
    machine_id: str = MACHINE_ID,
    name: str = GUARD_NAME,
    revision: str = EXPECTED_SHA,
    digest: str = DIGEST,
    owner: str = "12345:1",
    created_epoch: int = NOW_EPOCH,
    exit_timestamp: int = 2,
    exit_code: int | None = 0,
    guest_exit_code: int = 0,
    signal: int = 0,
    guest_signal: int = 0,
    oom_killed: bool = False,
    requested_stop: bool = False,
    restarting: bool = False,
    include_start_event: bool = True,
    incomplete_config: bool = False,
    operation_metadata: bool = True,
) -> dict:
    events: list[dict] = []
    if include_start_event:
        events.append({"type": "start", "timestamp": 1, "status": "started"})
    if state == "stopped" and exit_code is not None:
        exit_event: dict[str, int | bool] = {"oom_killed": oom_killed}
        # fly-go's pinned model uses `omitempty`: a successful integer zero is
        # absent in `flyctl machine list --json`.
        if exit_code != 0:
            exit_event["exit_code"] = exit_code
        if guest_exit_code != 0:
            exit_event["guest_exit_code"] = guest_exit_code
        if signal != 0:
            exit_event["signal"] = signal
        if guest_signal != 0:
            exit_event["guest_signal"] = guest_signal
        if requested_stop:
            exit_event["requested_stop"] = True
        if restarting:
            exit_event["restarting"] = True
        events.append(
            {
                "type": "exit",
                "timestamp": exit_timestamp,
                "request": {"exit_event": exit_event},
            }
        )
    metadata = {
        "nova_revision": revision,
        "nova_image_digest": digest,
        "nova_guard_owner": owner,
        "nova_guard_created_epoch": str(created_epoch),
        "nova_guard_deadline_epoch": str(created_epoch + BACKFILL_LEASE_S),
    }
    if operation_metadata:
        metadata["nova_operation"] = "video-poster-backfill"
    config = {
        "metadata": metadata,
        "restart": {"policy": "no"},
        "guest": {"cpu_kind": "shared", "cpus": 4, "memory_mb": 8192},
        "init": {
            "cmd": [
                "/usr/bin/timeout",
                "--signal=TERM",
                "--kill-after=300s",
                "18000s",
                "/bin/bash",
                "-lc",
                BACKFILL_COMMAND,
            ]
        },
    }
    machine = {
        "id": machine_id,
        "name": name,
        "state": state,
        "image_ref": {"digest": digest},
        "events": events,
    }
    machine["incomplete_config" if incomplete_config else "config"] = config
    return machine


def _deploy_guard(
    *,
    machine_id: str = MACHINE_ID,
    owner: str = "12345:1",
    revision: str = EXPECTED_SHA,
    digest: str = DIGEST,
    created_epoch: int = NOW_EPOCH,
    state: str = "created",
) -> dict:
    return {
        "id": machine_id,
        "name": GUARD_NAME,
        "state": state,
        "image_ref": {"digest": digest},
        "events": [],
        "config": {
            "metadata": {
                "nova_operation": "fly-deploy-guard",
                "nova_guard_owner": owner,
                "nova_guard_created_epoch": str(created_epoch),
                "nova_guard_deadline_epoch": str(created_epoch + DEPLOY_LEASE_S),
                "nova_revision": revision,
                "nova_image_digest": digest,
            },
            "restart": {"policy": "no"},
            "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 256},
            "init": {"cmd": ["/bin/false"]},
        },
    }


def _managed_machine(
    process_group: str,
    *,
    digest: str = DIGEST,
    state: str = "started",
    machine_id: str | None = None,
    autostop: bool | str = False,
    requested_stop: bool = False,
    oom_killed: bool = False,
) -> dict:
    config: dict = {"metadata": {"fly_process_group": process_group}}
    if autostop:
        config["services"] = [{"autostop": autostop}]
    events: list[dict] = []
    if requested_stop or oom_killed:
        events.append(
            {
                "type": "exit",
                "timestamp": 2,
                "request": {
                    "exit_event": {
                        "requested_stop": requested_stop,
                        "oom_killed": oom_killed,
                    }
                },
            }
        )
    return {
        "id": machine_id or f"feed{process_group.encode().hex()[:8]}",
        "name": f"nova-{process_group}",
        "state": state,
        "image_ref": {"digest": digest},
        "events": events,
        "config": config,
    }


def _inventory(*guards: dict, digest: str = DIGEST, state: str = "started") -> list[dict]:
    return [
        _managed_machine("api", digest=digest, state=state, autostop=True),
        _managed_machine("worker", digest=digest, state=state),
        _managed_machine("light", digest=digest, state=state),
        _managed_machine("autoplace", digest=digest, state=state),
        *guards,
    ]


def _default_sequence() -> list[list[dict]]:
    created = _machine("created", include_start_event=False)
    stopped = _machine("stopped")
    return [
        _inventory(),
        _inventory(),
        _inventory(),
        _inventory(created),
        _inventory(created),
        _inventory(stopped),
    ]


def _run(
    tmp_path: Path,
    images: list[dict] | None = None,
    *,
    expected_sha: str = EXPECTED_SHA,
    machine_sequence: list[list[dict]] | None = None,
    create_exit: int = 0,
    start_exit: int = 0,
    destroy_exit: int = 0,
    curl_exit: int = 0,
    log_exit: int = 0,
    image_exit: int = 0,
    raw_image_json: str | None = None,
    args: list[str] | None = None,
    retry_failed_machine_id: str = "",
    acknowledge_failed_backfill_machine_id: str = "",
    verified_deploy_digest: str = "",
    now_epoch: int = NOW_EPOCH,
    github_event_name: str = "workflow_dispatch",
    github_ref: str = "refs/heads/main",
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    flyctl = bin_dir / "flyctl"
    flyctl.write_text(_STUB_FLYCTL)
    flyctl.chmod(flyctl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    curl = bin_dir / "curl"
    curl.write_text(_STUB_CURL)
    curl.chmod(curl.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    date = bin_dir / "date"
    date.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' '{now_epoch}'\n")
    date.chmod(date.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    paths = {
        "STUB_LIST_COUNT": tmp_path / "list.count",
        "STUB_CREATE_ARGS": tmp_path / "create.args",
        "STUB_IMAGE_ARGS": tmp_path / "image.args",
        "STUB_START_ARGS": tmp_path / "start.args",
        "STUB_DESTROY_ARGS": tmp_path / "destroy.args",
        "STUB_LOG_ARGS": tmp_path / "logs.args",
        "STUB_CURL_ARGS": tmp_path / "curl.args",
    }
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
            "EXPECTED_SHA": expected_sha,
            "RETRY_FAILED_MACHINE_ID": retry_failed_machine_id,
            "ACKNOWLEDGE_FAILED_BACKFILL_MACHINE_ID": acknowledge_failed_backfill_machine_id,
            "VERIFIED_DEPLOY_DIGEST": verified_deploy_digest,
            "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_EVENT_NAME": github_event_name,
            "GITHUB_REF": github_ref,
            "POSTER_BACKFILL_POLL_INTERVAL_S": "0",
            "POSTER_BACKFILL_MAX_WAIT_S": "5",
            "FLY_PRODUCTION_SETTLE_ATTEMPTS": "3",
            "STUB_IMAGE_JSON": raw_image_json or json.dumps(images or []),
            "STUB_IMAGE_EXIT": str(image_exit),
            "STUB_MACHINE_SEQUENCE": json.dumps(
                _default_sequence() if machine_sequence is None else machine_sequence
            ),
            "STUB_MACHINE_ID": MACHINE_ID,
            "STUB_CREATE_EXIT": str(create_exit),
            "STUB_START_EXIT": str(start_exit),
            "STUB_DESTROY_EXIT": str(destroy_exit),
            "STUB_CURL_EXIT": str(curl_exit),
            "STUB_LOG_EXIT": str(log_exit),
            **{key: str(value) for key, value in paths.items()},
        }
    )
    return subprocess.run(
        ["bash", str(SCRIPT), *(args or [])],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_launcher_creates_verifies_starts_and_reconciles_exact_digest(tmp_path: Path) -> None:
    result = _run(tmp_path, [_image(), _image()])

    assert result.returncode == 0, result.stderr
    create_args = (tmp_path / "create.args").read_text().splitlines()
    assert create_args[1:4] == ["machine", "create", "registry.fly.io/nova-video:deployment-test"]
    assert "--restart" in create_args
    assert create_args[create_args.index("--restart") + 1] == "no"
    assert create_args[create_args.index("--vm-cpus") + 1] == "4"
    assert create_args[create_args.index("--vm-memory") + 1] == "8192"
    metadata_values = [
        create_args[index + 1] for index, value in enumerate(create_args) if value == "--metadata"
    ]
    assert f"nova_image_digest={DIGEST}" in metadata_values
    assert "nova_operation=video-poster-backfill" in metadata_values
    assert "nova_guard_owner=12345:1" in metadata_values
    assert create_args[create_args.index("--name") + 1] == GUARD_NAME
    assert not any(value.startswith("fly_process_group=") for value in metadata_values)
    assert "--detach" not in create_args
    assert "--rm" not in create_args
    delimiter = create_args.index("--")
    assert create_args[delimiter + 1 :] == [
        "/usr/bin/timeout",
        "--signal=TERM",
        "--kill-after=300s",
        "18000s",
        "/bin/bash",
        "-lc",
        BACKFILL_COMMAND,
    ]
    assert BACKFILL_COMMAND.count("--exclude-synthetic --strict --batch-size 25") == 2
    assert "--dry-run --exclude-synthetic --strict" in BACKFILL_COMMAND
    assert (tmp_path / "start.args").read_text().splitlines()[-1] == MACHINE_ID
    assert (tmp_path / "destroy.args").read_text().splitlines()[-1] == MACHINE_ID
    assert "--force" not in (tmp_path / "destroy.args").read_text()
    assert "Backfill complete" in result.stdout


def test_launcher_accepts_autostopped_managed_machines_on_the_exact_digest(
    tmp_path: Path,
) -> None:
    created = _machine("created", include_start_event=False)
    stopped = _machine("stopped", signal=-1, guest_signal=-1)

    def healthy_inventory(*guards: dict) -> list[dict]:
        return [
            _managed_machine("api", autostop=True),
            _managed_machine(
                "api",
                state="stopped",
                machine_id="feedapi0002",
                autostop=True,
            ),
            _managed_machine("worker", state="stopped", requested_stop=True),
            _managed_machine("light"),
            _managed_machine("autoplace"),
            *guards,
        ]

    result = _run(
        tmp_path,
        [_image(), _image()],
        machine_sequence=[
            healthy_inventory(),
            healthy_inventory(),
            healthy_inventory(),
            healthy_inventory(created),
            healthy_inventory(created),
            healthy_inventory(stopped),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "start.args").read_text().splitlines()[-1] == MACHINE_ID
    assert (tmp_path / "destroy.args").read_text().splitlines()[-1] == MACHINE_ID


def test_launcher_rejects_managed_machine_that_never_settles(tmp_path: Path) -> None:
    transitional = _inventory(state="starting")
    result = _run(
        tmp_path,
        [_image()],
        machine_sequence=[transitional, transitional, transitional],
    )

    assert result.returncode == 1
    assert "did not reach the required stable process topology" in result.stderr
    assert not (tmp_path / "create.args").exists()


@pytest.mark.parametrize(
    "failure",
    [
        "all-api-stopped",
        "api-stopped-without-autostop",
        "light-stopped",
        "autoplace-stopped",
        "worker-crashed",
    ],
)
def test_launcher_rejects_stopped_machine_without_documented_lifecycle(
    tmp_path: Path,
    failure: str,
) -> None:
    machines = [
        _managed_machine("api", autostop=True),
        _managed_machine("worker"),
        _managed_machine("light"),
        _managed_machine("autoplace"),
    ]
    if failure == "all-api-stopped":
        machines[0] = _managed_machine("api", state="stopped", autostop=True)
    elif failure == "api-stopped-without-autostop":
        machines.append(_managed_machine("api", state="stopped", machine_id="feedapi0002"))
    elif failure == "light-stopped":
        machines[2] = _managed_machine("light", state="stopped")
    elif failure == "autoplace-stopped":
        machines[3] = _managed_machine("autoplace", state="stopped")
    else:
        machines[1] = _managed_machine("worker", state="stopped")

    result = _run(
        tmp_path,
        [_image()],
        machine_sequence=[machines, machines, machines, machines, machines],
    )

    assert result.returncode == 1
    assert "do not satisfy the required stable process topology" in result.stderr
    assert not (tmp_path / "create.args").exists()


def test_launcher_rejects_missing_required_process_group(tmp_path: Path) -> None:
    missing_light = [
        _managed_machine("api", autostop=True),
        _managed_machine("worker"),
        _managed_machine("autoplace"),
    ]
    result = _run(
        tmp_path,
        [_image()],
        machine_sequence=[missing_light, missing_light, missing_light],
    )

    assert result.returncode == 1
    assert "missing a required api, worker, light, or autoplace" in result.stderr
    assert not (tmp_path / "create.args").exists()


def test_launcher_bounded_wait_accepts_managed_lifecycle_settle(tmp_path: Path) -> None:
    created = _machine("created", include_start_event=False)
    stopped = _machine("stopped", signal=-1, guest_signal=-1)
    transitional = _inventory(state="stopping")
    result = _run(
        tmp_path,
        [_image(), _image()],
        machine_sequence=[
            _inventory(),
            _inventory(),
            transitional,
            _inventory(),
            _inventory(created),
            _inventory(created),
            _inventory(stopped),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert "Waiting for managed production Machines to settle" in result.stdout
    assert (tmp_path / "start.args").read_text().splitlines()[-1] == MACHINE_ID


def test_reconcile_waits_for_active_machine_then_validates_receipt(tmp_path: Path) -> None:
    active = _machine("started")
    stopped = _machine("stopped")
    result = _run(
        tmp_path,
        args=["--reconcile-only"],
        machine_sequence=[[active], [active], [active], [stopped]],
    )

    assert result.returncode == 0, result.stderr
    assert "Waiting for poster backfill Machine" in result.stdout
    assert (tmp_path / "destroy.args").exists()
    assert not (tmp_path / "create.args").exists()


def test_reconcile_resumes_created_guard_then_validates_it_without_force(tmp_path: Path) -> None:
    created = _machine("created", include_start_event=False)
    stopped = _machine("stopped")
    result = _run(
        tmp_path,
        [_image()],
        args=["--reconcile-only"],
        machine_sequence=[
            _inventory(created),
            _inventory(created),
            _inventory(created),
            _inventory(created),
            _inventory(stopped),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "start.args").read_text().splitlines()[-1] == MACHINE_ID
    destroyed = (tmp_path / "destroy.args").read_text()
    assert MACHINE_ID in destroyed
    assert "--force" not in destroyed


def test_normal_run_reuses_clean_same_revision_receipt_without_new_machine(
    tmp_path: Path,
) -> None:
    stopped = _machine("stopped")

    result = _run(
        tmp_path,
        machine_sequence=[[stopped], [stopped], [stopped]],
    )

    assert result.returncode == 0, result.stderr
    assert f"already completed and verified revision {EXPECTED_SHA}" in result.stdout
    assert (tmp_path / "destroy.args").read_text().splitlines()[-1] == MACHINE_ID
    assert not (tmp_path / "image.args").exists()
    assert not (tmp_path / "create.args").exists()
    assert not (tmp_path / "start.args").exists()


def test_normal_run_creates_new_guard_after_clean_different_revision_receipt(
    tmp_path: Path,
) -> None:
    prior_id = "deadbeef123456"
    prior_revision = "c" * 40
    prior_stopped = _machine(
        "stopped",
        machine_id=prior_id,
        name="poster-backfill-11111-1",
        revision=prior_revision,
    )
    created = _machine("created", include_start_event=False)
    stopped = _machine("stopped")

    result = _run(
        tmp_path,
        [_image(), _image()],
        machine_sequence=[
            [prior_stopped],
            [prior_stopped],
            _inventory(),
            _inventory(),
            _inventory(created),
            _inventory(created),
            _inventory(stopped),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "image.args").exists()
    assert (tmp_path / "create.args").exists()
    assert (tmp_path / "start.args").read_text().splitlines()[-1] == MACHINE_ID
    destroyed = (tmp_path / "destroy.args").read_text().splitlines()
    assert destroyed.count(prior_id) == 1
    assert destroyed.count(MACHINE_ID) == 1
    create_args = (tmp_path / "create.args").read_text().splitlines()
    metadata_values = [
        create_args[index + 1] for index, value in enumerate(create_args) if value == "--metadata"
    ]
    assert f"nova_revision={EXPECTED_SHA}" in metadata_values
    assert f"nova_image_digest={DIGEST}" in metadata_values


def test_failed_or_missing_exit_receipt_is_retained_and_blocks(tmp_path: Path) -> None:
    for suffix, stopped in (
        ("nonzero", _machine("stopped", exit_code=23)),
        ("guest-nonzero", _machine("stopped", guest_exit_code=24)),
        ("signal", _machine("stopped", signal=15)),
        ("guest-signal", _machine("stopped", guest_signal=9)),
        ("oom", _machine("stopped", oom_killed=True)),
        ("requested-stop", _machine("stopped", requested_stop=True)),
        ("restarting", _machine("stopped", restarting=True)),
        ("missing", _machine("stopped", exit_code=None)),
    ):
        case_dir = tmp_path / suffix
        case_dir.mkdir()
        result = _run(
            case_dir,
            args=["--reconcile-only"],
            machine_sequence=[[stopped], [stopped], [stopped]],
        )
        assert result.returncode == 1
        assert "retained" in result.stderr
        assert not (case_dir / "destroy.args").exists()


def test_clean_exit_accepts_fly_no_signal_negative_one_sentinel(tmp_path: Path) -> None:
    stopped = _machine("stopped", signal=-1, guest_signal=-1)
    result = _run(
        tmp_path,
        args=["--reconcile-only"],
        machine_sequence=[[stopped], [stopped], [stopped]],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "destroy.args").read_text().splitlines()[-1] == MACHINE_ID


def test_incomplete_config_metadata_is_discovered_and_reconciled(tmp_path: Path) -> None:
    stopped = _machine("stopped", incomplete_config=True)
    result = _run(
        tmp_path,
        args=["--reconcile-only"],
        machine_sequence=[[stopped], [stopped], [stopped]],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "destroy.args").read_text().splitlines()[-1] == MACHINE_ID


def test_every_machine_state_requires_the_exact_bounded_contract(tmp_path: Path) -> None:
    def wrong_name(machine: dict) -> None:
        machine["name"] = "unrelated-clean-machine"

    def wrong_digest(machine: dict) -> None:
        machine["image_ref"]["digest"] = "registry.fly.io/nova-video:latest"

    def wrong_command(machine: dict) -> None:
        machine["config"]["init"]["cmd"][-1] = "echo not-the-backfill"

    def wrong_guest(machine: dict) -> None:
        machine["config"]["guest"]["memory_mb"] = 512

    def exposed_service(machine: dict) -> None:
        machine["config"]["services"] = [{"protocol": "tcp", "internal_port": 8000}]

    def injected_env(machine: dict) -> None:
        machine["config"]["env"] = {"DATABASE_URL": "postgres://elsewhere"}

    def attached_mount(machine: dict) -> None:
        machine["config"]["mounts"] = [{"path": "/data", "volume": "vol_other"}]

    def injected_file(machine: dict) -> None:
        machine["config"]["files"] = [
            {"guest_path": "/app/scripts/backfill_video_posters.py", "raw_value": "ZXhpdCgwKQ=="}
        ]

    def mismatched_digest_claim(machine: dict) -> None:
        machine["config"]["metadata"]["nova_image_digest"] = f"sha256:{'c' * 64}"

    mutations = {
        "name": wrong_name,
        "digest": wrong_digest,
        "command": wrong_command,
        "guest": wrong_guest,
        "service": exposed_service,
        "env": injected_env,
        "mount": attached_mount,
        "file": injected_file,
        "digest-claim": mismatched_digest_claim,
    }
    for state in ("started", "stopped"):
        for label, mutate in mutations.items():
            case_dir = tmp_path / state / label
            case_dir.mkdir(parents=True)
            machine = _machine(state)
            mutate(machine)

            result = _run(
                case_dir,
                args=["--reconcile-only"],
                machine_sequence=[[machine]],
            )

            assert result.returncode == 1
            assert (
                "violates its bounded execution contract" in result.stderr
                or "name/metadata is unverifiable" in result.stderr
            )
            assert not (case_dir / "start.args").exists()
            assert not (case_dir / "destroy.args").exists()


def test_duplicate_machine_id_is_ambiguous_and_never_mutated(tmp_path: Path) -> None:
    duplicate = _machine("stopped")
    result = _run(
        tmp_path,
        args=["--reconcile-only"],
        machine_sequence=[[duplicate, duplicate]],
    )

    assert result.returncode == 1
    assert "resolves to more than one Machine" in result.stderr
    assert not (tmp_path / "start.args").exists()
    assert not (tmp_path / "destroy.args").exists()


def test_reserved_name_without_operation_metadata_blocks_without_mutation(
    tmp_path: Path,
) -> None:
    unverified = _machine("started", operation_metadata=False)
    result = _run(
        tmp_path,
        args=["--reconcile-only"],
        machine_sequence=[[unverified]],
    )

    assert result.returncode == 1
    assert "name/metadata is unverifiable" in result.stderr
    assert not (tmp_path / "start.args").exists()
    assert not (tmp_path / "destroy.args").exists()


def test_lost_create_response_resolves_stable_name_and_start_failure_retains_guard(
    tmp_path: Path,
) -> None:
    create_response_lost = _run(tmp_path / "create", [_image()], create_exit=23)
    assert create_response_lost.returncode == 0, create_response_lost.stderr
    assert (tmp_path / "create" / "start.args").exists()

    start_dir = tmp_path / "start"
    start_failed = _run(start_dir, [_image()], start_exit=24)
    assert start_failed.returncode == 1
    assert (start_dir / "create.args").exists()
    assert (start_dir / "start.args").exists()
    assert not (start_dir / "destroy.args").exists()


def test_launcher_refuses_mixed_or_malformed_production_image(tmp_path: Path) -> None:
    other_digest = f"sha256:{'c' * 64}"
    mixed_inventory = [
        _managed_machine("api"),
        _managed_machine("worker", digest=other_digest),
    ]
    mixed = _run(
        tmp_path / "mixed",
        [_image(), _image(digest=other_digest)],
        machine_sequence=[mixed_inventory, mixed_inventory, mixed_inventory],
    )
    assert mixed.returncode == 1
    assert "do not expose one immutable image digest" in mixed.stderr

    malformed = _run(tmp_path / "digest", [_image(digest="sha256:not-a-real-digest")])
    assert malformed.returncode == 1
    assert "valid deployed registry, repository, and digest" in malformed.stderr


def test_launcher_fails_closed_on_bad_image_metadata(tmp_path: Path) -> None:
    empty = _run(tmp_path / "empty", [])
    assert empty.returncode == 1
    assert "valid deployed registry" in empty.stderr

    malformed = _run(tmp_path / "malformed", [], raw_image_json="not-json")
    assert malformed.returncode == 1
    assert malformed.stderr

    unreadable = _run(tmp_path / "unreadable", [], image_exit=42)
    assert unreadable.returncode == 1
    assert "Could not read Fly production image metadata" in unreadable.stderr


def test_launcher_requires_one_nonempty_tag_for_the_production_digest(tmp_path: Path) -> None:
    missing = _run(tmp_path / "missing", [_image(tag="")])
    assert missing.returncode == 1
    assert "one deployed image tag" in missing.stderr

    ambiguous = _run(
        tmp_path / "ambiguous",
        [_image(tag="deployment-one"), _image(tag="deployment-two")],
    )
    assert ambiguous.returncode == 1
    assert "one deployed image tag" in ambiguous.stderr


def test_launcher_refuses_bad_revision_or_sha(tmp_path: Path) -> None:
    missing = _run(tmp_path / "missing", [_image(labels={})])
    assert missing.returncode == 1
    assert "revision label" in missing.stderr

    mismatch = _run(tmp_path / "mismatch", [_image()], expected_sha="d" * 40)
    assert mismatch.returncode == 1
    assert "does not match expected deploy" in mismatch.stderr

    short = _run(tmp_path / "short", [_image()], expected_sha="abc123")
    assert short.returncode == 1
    assert "40-character" in short.stderr


def test_delayed_create_conflict_relists_and_reconciles_single_stable_guard(
    tmp_path: Path,
) -> None:
    foreign_active = _machine("started", owner="99999:1")
    foreign_stopped = _machine("stopped", owner="99999:1")
    result = _run(
        tmp_path,
        [_image()],
        create_exit=23,
        machine_sequence=[
            _inventory(),
            _inventory(),
            _inventory(),
            _inventory(),
            _inventory(foreign_active),
            _inventory(foreign_active),
            _inventory(foreign_stopped),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "create.args").read_text().count("CALL") == 1
    assert "Waiting for poster backfill Machine" in result.stdout
    assert not (tmp_path / "start.args").exists()
    assert (tmp_path / "destroy.args").read_text().splitlines()[-1] == MACHINE_ID


def test_backfill_reproves_managed_digest_after_winning_name_before_start(
    tmp_path: Path,
) -> None:
    changed_digest = f"sha256:{'c' * 64}"
    created = _machine("created", include_start_event=False)
    changed_inventory = _inventory(created, digest=changed_digest)
    result = _run(
        tmp_path,
        [_image(), _image(digest=changed_digest)],
        machine_sequence=[
            _inventory(),
            _inventory(),
            _inventory(),
            _inventory(created),
            changed_inventory,
        ],
    )

    assert result.returncode == 1
    assert "changed after backfill guard acquisition" in result.stderr
    assert not (tmp_path / "start.args").exists()
    assert not (tmp_path / "destroy.args").exists()


def test_deploy_guard_acquire_is_created_only_and_exact_release_is_forced(
    tmp_path: Path,
) -> None:
    guard = _deploy_guard()
    acquire_dir = tmp_path / "acquire"
    acquired = _run(
        acquire_dir,
        [_image(labels={})],
        args=["--acquire-deploy-guard"],
        machine_sequence=[
            _inventory(),
            _inventory(),
            _inventory(),
            _inventory(guard),
        ],
    )

    assert acquired.returncode == 0, acquired.stderr
    create_args = (acquire_dir / "create.args").read_text().splitlines()
    assert create_args[create_args.index("--name") + 1] == GUARD_NAME
    assert create_args[create_args.index("--vm-cpus") + 1] == "1"
    assert create_args[create_args.index("--vm-memory") + 1] == "256"
    assert create_args[create_args.index("--") + 1 :] == ["/bin/false"]
    metadata = [
        create_args[index + 1] for index, value in enumerate(create_args) if value == "--metadata"
    ]
    assert "nova_operation=fly-deploy-guard" in metadata
    assert "nova_guard_owner=12345:1" in metadata
    assert not any(value.startswith("fly_process_group=") for value in metadata)
    assert not (acquire_dir / "start.args").exists()
    assert not (acquire_dir / "destroy.args").exists()

    release_dir = tmp_path / "release"
    released = _run(
        release_dir,
        [_image()],
        args=["--release-deploy-guard"],
        verified_deploy_digest=DIGEST,
        machine_sequence=[_inventory(guard), _inventory(guard)],
    )
    assert released.returncode == 0, released.stderr
    destroy_args = (release_dir / "destroy.args").read_text()
    assert MACHINE_ID in destroy_args
    assert "--force" in destroy_args
    assert not (release_dir / "start.args").exists()


def test_deploy_guard_lost_create_response_resolves_owned_stable_name(
    tmp_path: Path,
) -> None:
    guard = _deploy_guard()
    result = _run(
        tmp_path,
        [_image(labels={})],
        args=["--acquire-deploy-guard"],
        create_exit=52,
        machine_sequence=[
            _inventory(),
            _inventory(),
            _inventory(),
            _inventory(guard),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert "Acquired created-only deploy guard" in result.stdout
    assert not (tmp_path / "start.args").exists()


def test_foreign_unexpired_deploy_guard_blocks_without_mutation(tmp_path: Path) -> None:
    guard = _deploy_guard(machine_id=FOREIGN_MACHINE_ID, owner="99999:1")
    result = _run(
        tmp_path,
        args=["--acquire-deploy-guard"],
        machine_sequence=[_inventory(guard), _inventory(guard)],
    )

    assert result.returncode == 1
    assert "has not passed its validated deadline plus grace" in result.stderr
    assert not (tmp_path / "create.args").exists()
    assert not (tmp_path / "start.args").exists()
    assert not (tmp_path / "destroy.args").exists()


def test_expired_exact_deploy_guard_reclaims_only_after_consistent_fleet_proof(
    tmp_path: Path,
) -> None:
    old_digest = f"sha256:{'d' * 64}"
    expired = _deploy_guard(
        machine_id=FOREIGN_MACHINE_ID,
        owner="99999:1",
        digest=old_digest,
        created_epoch=NOW_EPOCH - 4_000,
    )
    owned = _deploy_guard()
    result = _run(
        tmp_path,
        [_image(), _image(digest=old_digest, labels={})],
        args=["--acquire-deploy-guard"],
        machine_sequence=[
            _inventory(expired),
            _inventory(expired),
            _inventory(expired),
            _inventory(),
            _inventory(owned),
        ],
    )

    assert result.returncode == 0, result.stderr
    destroy_args = (tmp_path / "destroy.args").read_text()
    assert FOREIGN_MACHINE_ID in destroy_args
    assert "--force" in destroy_args
    assert (tmp_path / "create.args").read_text().count("CALL") == 1
    assert not (tmp_path / "start.args").exists()


def test_expired_deploy_guard_is_retained_when_managed_fleet_is_mixed(
    tmp_path: Path,
) -> None:
    old_digest = f"sha256:{'d' * 64}"
    other_digest = f"sha256:{'c' * 64}"
    expired = _deploy_guard(
        machine_id=FOREIGN_MACHINE_ID,
        owner="99999:1",
        digest=old_digest,
        created_epoch=NOW_EPOCH - 4_000,
    )
    mixed = [
        _managed_machine("api"),
        _managed_machine("worker", digest=other_digest),
        expired,
    ]
    result = _run(
        tmp_path,
        [_image(), _image(digest=other_digest), _image(digest=old_digest)],
        args=["--acquire-deploy-guard"],
        machine_sequence=[mixed, mixed, mixed],
    )

    assert result.returncode == 1
    assert "do not expose one immutable image digest" in result.stderr
    assert not (tmp_path / "destroy.args").exists()
    assert not (tmp_path / "create.args").exists()


def test_deploy_guard_rejects_foreign_owner_or_non_created_state(tmp_path: Path) -> None:
    foreign = _deploy_guard(owner="99999:1")
    release = _run(
        tmp_path / "owner",
        args=["--release-deploy-guard"],
        machine_sequence=[_inventory(foreign)],
    )
    assert release.returncode == 1
    assert "not owned by this workflow" in release.stderr
    assert not (tmp_path / "owner" / "destroy.args").exists()

    started = _deploy_guard(state="started")
    invalid = _run(
        tmp_path / "state",
        args=["--acquire-deploy-guard"],
        machine_sequence=[_inventory(started), _inventory(started)],
    )
    assert invalid.returncode == 1
    assert "created-only contract" in invalid.stderr
    assert not (tmp_path / "state" / "destroy.args").exists()


def test_deploy_guard_release_requires_matching_verified_production_digest(
    tmp_path: Path,
) -> None:
    guard = _deploy_guard()
    missing = _run(
        tmp_path / "missing",
        args=["--release-deploy-guard"],
        machine_sequence=[_inventory(guard)],
    )
    assert missing.returncode == 1
    assert "VERIFIED_DEPLOY_DIGEST" in missing.stderr
    assert not (tmp_path / "missing" / "destroy.args").exists()

    wrong_digest = f"sha256:{'c' * 64}"
    mismatch = _run(
        tmp_path / "mismatch",
        [_image()],
        args=["--release-deploy-guard"],
        verified_deploy_digest=wrong_digest,
        machine_sequence=[_inventory(guard), _inventory(guard)],
    )
    assert mismatch.returncode == 1
    assert "no longer matches" in mismatch.stderr
    assert not (tmp_path / "mismatch" / "destroy.args").exists()


def test_manual_main_fix_deploy_can_release_exact_failed_backfill_then_acquire(
    tmp_path: Path,
) -> None:
    new_sha = "c" * 40
    failed = _machine("stopped", exit_code=23)
    deploy_guard = _deploy_guard(revision=new_sha)
    result = _run(
        tmp_path,
        [_image()],
        expected_sha=new_sha,
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=[
            _inventory(failed),
            _inventory(failed),
            _inventory(failed),
            _inventory(failed),
            _inventory(),
            _inventory(),
            _inventory(deploy_guard),
        ],
    )

    assert result.returncode == 0, result.stderr
    assert "Explicitly acknowledging incomplete poster repair" in result.stdout
    assert "Removed acknowledged failed poster backfill guard" in result.stdout
    assert "Acquired created-only deploy guard" in result.stdout
    destroy_args = (tmp_path / "destroy.args").read_text().splitlines()
    assert destroy_args[-1] == MACHINE_ID
    assert "--force" not in destroy_args
    assert (tmp_path / "create.args").read_text().count("CALL") == 1
    assert MACHINE_ID in (tmp_path / "logs.args").read_text()
    assert "https://nova-video.fly.dev/health" in (tmp_path / "curl.args").read_text()


@pytest.mark.parametrize(
    ("event_name", "ref", "message"),
    [
        ("push", "refs/heads/main", "manual main-branch deploy"),
        ("workflow_dispatch", "refs/heads/feature", "manual main-branch deploy"),
    ],
)
def test_failed_backfill_release_requires_manual_main_dispatch(
    tmp_path: Path,
    event_name: str,
    ref: str,
    message: str,
) -> None:
    result = _run(
        tmp_path,
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        github_event_name=event_name,
        github_ref=ref,
    )

    assert result.returncode == 1
    assert message in result.stderr
    assert not (tmp_path / "destroy.args").exists()
    assert not (tmp_path / "create.args").exists()


@pytest.mark.parametrize(
    "guard",
    [
        _machine("started"),
        _machine("starting"),
        _machine("stopping"),
        _machine("created", include_start_event=False),
    ],
    ids=["started", "starting", "stopping", "created"],
)
def test_failed_backfill_release_rejects_every_active_state(
    tmp_path: Path,
    guard: dict,
) -> None:
    result = _run(
        tmp_path,
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=[_inventory(guard), _inventory(guard), _inventory(guard)],
    )

    assert result.returncode == 1
    assert "not stopped" in result.stderr
    assert not (tmp_path / "destroy.args").exists()
    assert not (tmp_path / "start.args").exists()


def test_failed_backfill_release_rejects_wrong_id_clean_exit_and_missing_receipt(
    tmp_path: Path,
) -> None:
    failed = _machine("stopped", exit_code=23)
    wrong = _run(
        tmp_path / "wrong",
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=FOREIGN_MACHINE_ID,
        machine_sequence=[_inventory(failed), _inventory(failed), _inventory(failed)],
    )
    assert wrong.returncode == 1
    assert "does not exactly match" in wrong.stderr

    clean = _machine("stopped")
    clean_result = _run(
        tmp_path / "clean",
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=[_inventory(clean), _inventory(clean), _inventory(clean)],
    )
    assert clean_result.returncode == 1
    assert "exited cleanly" in clean_result.stderr

    missing = _machine("stopped", exit_code=None)
    missing_result = _run(
        tmp_path / "missing",
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=[_inventory(missing), _inventory(missing), _inventory(missing)],
    )
    assert missing_result.returncode == 1
    assert "without an exit receipt" in missing_result.stderr
    for name in ("wrong", "clean", "missing"):
        assert not (tmp_path / name / "destroy.args").exists()
        assert not (tmp_path / name / "create.args").exists()


def test_failed_backfill_release_requires_preserved_logs_and_well_formed_receipt(
    tmp_path: Path,
) -> None:
    failed = _machine("stopped", exit_code=23)
    logs_failed = _run(
        tmp_path / "logs",
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=[_inventory(failed)] * 3,
        log_exit=1,
    )
    assert logs_failed.returncode == 1
    assert "logs could not be preserved" in logs_failed.stderr
    assert not (tmp_path / "logs" / "destroy.args").exists()
    assert not (tmp_path / "logs" / "create.args").exists()

    malformed_receipts: list[dict] = []
    invalid_code = json.loads(json.dumps(failed))
    invalid_code["events"][-1]["request"]["exit_event"]["exit_code"] = "not-an-integer"
    malformed_receipts.append(invalid_code)
    invalid_timestamp = json.loads(json.dumps(failed))
    invalid_timestamp["events"][-1]["timestamp"] = ""
    malformed_receipts.append(invalid_timestamp)
    invalid_event = json.loads(json.dumps(failed))
    invalid_event["events"][-1]["request"]["exit_event"] = "not-an-object"
    malformed_receipts.append(invalid_event)
    for index, malformed in enumerate(malformed_receipts):
        case_dir = tmp_path / f"malformed-{index}"
        malformed_result = _run(
            case_dir,
            args=["--acquire-deploy-guard"],
            acknowledge_failed_backfill_machine_id=MACHINE_ID,
            machine_sequence=[_inventory(malformed)] * 3,
        )
        assert malformed_result.returncode == 1
        assert "malformed exit receipt" in malformed_result.stderr
        assert not (case_dir / "destroy.args").exists()
        assert not (case_dir / "create.args").exists()


def test_failed_backfill_release_never_acknowledges_legacy_guard(tmp_path: Path) -> None:
    legacy = _machine(
        "stopped",
        name="poster-backfill-12345-1",
        exit_code=23,
    )
    result = _run(
        tmp_path,
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=[_inventory(legacy), _inventory(legacy)],
    )

    assert result.returncode == 1
    assert "does not exactly match the stable Machine ID" in result.stderr
    assert not (tmp_path / "logs.args").exists()
    assert not (tmp_path / "destroy.args").exists()
    assert not (tmp_path / "create.args").exists()


def test_failed_backfill_release_retains_guard_on_health_destroy_or_absence_failure(
    tmp_path: Path,
) -> None:
    failed = _machine("stopped", exit_code=23)
    prefix = [_inventory(failed)] * 4

    unhealthy = _run(
        tmp_path / "health",
        [_image()],
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=prefix,
        curl_exit=22,
    )
    assert unhealthy.returncode == 1
    assert "Production health failed" in unhealthy.stderr
    assert not (tmp_path / "health" / "destroy.args").exists()

    destroy_failed = _run(
        tmp_path / "destroy",
        [_image()],
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=prefix,
        destroy_exit=1,
    )
    assert destroy_failed.returncode == 1
    assert "could not be destroyed" in destroy_failed.stderr
    assert not (tmp_path / "destroy" / "create.args").exists()

    still_present = _run(
        tmp_path / "present",
        [_image()],
        args=["--acquire-deploy-guard"],
        acknowledge_failed_backfill_machine_id=MACHINE_ID,
        machine_sequence=[*prefix, _inventory(failed)],
    )
    assert still_present.returncode == 1
    assert "still present after destroy" in still_present.stderr
    assert not (tmp_path / "present" / "create.args").exists()


def test_failed_backfill_requires_exact_id_and_sha_ack_then_retries_once(
    tmp_path: Path,
) -> None:
    failed = _machine("stopped", exit_code=23)
    no_ack = _run(
        tmp_path / "none",
        args=[],
        machine_sequence=[[failed], [failed], [failed]],
    )
    assert no_ack.returncode == 1
    assert "explicit retry acknowledgement is required" in no_ack.stderr
    assert not (tmp_path / "none" / "start.args").exists()

    wrong_id = _run(
        tmp_path / "wrong-id",
        args=[],
        retry_failed_machine_id=FOREIGN_MACHINE_ID,
        machine_sequence=[[failed], [failed], [failed]],
    )
    assert wrong_id.returncode == 1
    assert "does not exactly match" in wrong_id.stderr
    assert not (tmp_path / "wrong-id" / "start.args").exists()

    wrong_sha = _run(
        tmp_path / "wrong-sha",
        expected_sha="c" * 40,
        retry_failed_machine_id=MACHINE_ID,
        machine_sequence=[[failed], [failed], [failed]],
    )
    assert wrong_sha.returncode == 1
    assert "does not exactly match" in wrong_sha.stderr
    assert not (tmp_path / "wrong-sha" / "start.args").exists()

    active = _machine("started")
    succeeded = _machine("stopped", exit_timestamp=3)
    exact = _run(
        tmp_path / "exact",
        [_image()],
        retry_failed_machine_id=MACHINE_ID,
        machine_sequence=[
            _inventory(failed),
            _inventory(failed),
            _inventory(failed),
            _inventory(failed),
            _inventory(failed),
            _inventory(active),
            _inventory(succeeded),
        ],
    )
    assert exact.returncode == 0, exact.stderr
    assert "Waiting for acknowledged poster backfill retry" in exact.stdout
    assert (tmp_path / "exact" / "start.args").read_text().count("CALL") == 1
    assert "--force" not in (tmp_path / "exact" / "destroy.args").read_text()
    assert not (tmp_path / "exact" / "create.args").exists()


def test_workflows_pin_revision_keep_historical_lock_and_gate_deploy() -> None:
    backfill = BACKFILL_WORKFLOW.read_text()
    deploy = DEPLOY_WORKFLOW.read_text()

    assert "group: fly-deploy-main" in backfill
    assert "group: fly-deploy-main" in deploy
    assert "fly-production-mutation" not in backfill
    assert "fly-production-mutation" not in deploy
    assert "cancel-in-progress: false" in backfill
    assert "cancel-in-progress: false" in deploy
    assert "if: github.ref == 'refs/heads/main'" in backfill
    assert "if: github.ref == 'refs/heads/main'" in deploy
    assert "ref: ${{ inputs.expected_sha }}" in backfill
    assert "Prove the requested revision is live" in backfill
    assert backfill.index("Prove the requested revision is live") < backfill.index(
        "Check out the exact deployed revision"
    )
    assert "bash scripts/run-video-poster-backfill.sh" in backfill
    assert "retry_failed_machine_id:" in backfill
    assert "RETRY_FAILED_MACHINE_ID: ${{ inputs.retry_failed_machine_id }}" in backfill
    assert 'has("fly_process_group")' in backfill
    assert "select(.Digest == $digest)" in backfill
    assert "https://nova-video.fly.dev/health" in backfill
    assert "run-video-poster-backfill.sh --reconcile-only" not in deploy
    assert "needs: reconcile" not in deploy
    assert "timeout-minutes: 360" in deploy
    assert "run-video-poster-backfill.sh --acquire-deploy-guard" in deploy
    assert "run-video-poster-backfill.sh --release-deploy-guard" in deploy
    assert "acknowledge_failed_backfill_machine_id:" in deploy
    assert (
        "ACKNOWLEDGE_FAILED_BACKFILL_MACHINE_ID: "
        "${{ inputs.acknowledge_failed_backfill_machine_id }}" in deploy
    )
    assert "GITHUB_EVENT_NAME: ${{ github.event_name }}" in deploy
    assert "GITHUB_REF: ${{ github.ref }}" in deploy
    assert deploy.index("Acquire durable Fly mutation guard") < deploy.index("      - name: Deploy")
    assert deploy.index("Verify exact deployed image") < deploy.index(
        "Release durable Fly mutation guard"
    )
    assert "if: always()" in deploy
    assert "nova-fly-deploy-verified" in deploy
    assert 'VERIFIED_DEPLOY_DIGEST="$verified_digest"' in deploy
    assert deploy.index('>"$RUNNER_TEMP/nova-fly-deploy-verified"') < deploy.index(
        "run-video-poster-backfill.sh --release-deploy-guard"
    )
    assert "timeout --signal=TERM --kill-after=60s 2100s" in deploy
    assert 'has("fly_process_group")' in deploy
    assert 'init.cmd == ["/bin/false"]' in deploy
    assert "org.opencontainers.image.revision=${GITHUB_SHA}" in deploy
    for workflow in (backfill, deploy):
        assert "managed_fleet_has_required_processes" in workflow
        assert "managed_fleet_has_transitional_states" in workflow
        assert "managed_fleet_is_operationally_stable" in workflow
        assert '.fly_process_group) == "light"' in workflow
        assert '.fly_process_group) == "autoplace"' in workflow
        assert "$last_exit.requested_stop == true" in workflow
        assert '.autostop == true or .autostop == "stop"' in workflow
        assert "do not satisfy the required stable process topology" in workflow
        assert "did not reach the required stable process topology" in workflow
        assert "actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09" in workflow
        assert (
            "superfly/flyctl-actions/setup-flyctl@ed8efb33836e8b2096c7fd3ba1c8afe303ebbff1"
        ) in workflow
        assert "version: 0.4.94" in workflow
