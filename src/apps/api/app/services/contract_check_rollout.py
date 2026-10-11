"""Explicit, fail-closed rollout state for creator contract checks.

The caller stamps a generation with these modes when it approves that
generation.  Workers only receive the stamp; this module deliberately does
not read settings or feature flags, so an in-flight job cannot change policy.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

RolloutMode = Literal["log_only", "enforce"]
CheckOutcome = Literal["passed", "declined"]

LOG_ONLY = "log_only"
ENFORCE = "enforce"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ContractCheckRolloutError(ValueError):
    """A check stamp or promotion proof cannot safely be used."""


@dataclass(frozen=True)
class GenerationCheckRollout:
    """Persisted rollout policy for one approved creator generation."""

    generation_id: str
    approved: bool
    modes: Mapping[str, RolloutMode]

    def mode_for(self, check_name: str) -> RolloutMode:
        if not self.approved:
            raise ContractCheckRolloutError("contract-check generation is not approved")
        mode = self.modes.get(check_name)
        if mode not in {LOG_ONLY, ENFORCE}:
            raise ContractCheckRolloutError(
                f"contract check {check_name!r} has no stamped rollout mode"
            )
        return mode

    def to_metadata(self) -> dict[str, Any]:
        """A JSON-safe generation stamp for the job's approved metadata."""
        if not self.generation_id:
            raise ContractCheckRolloutError("generation_id is required")
        return {
            "generation_id": self.generation_id,
            "approved": self.approved,
            "contract_check_modes": dict(self.modes),
        }

    @classmethod
    def from_metadata(cls, value: Mapping[str, Any]) -> GenerationCheckRollout:
        generation_id = value.get("generation_id")
        approved = value.get("approved")
        modes = value.get("contract_check_modes")
        if not isinstance(generation_id, str) or not generation_id:
            raise ContractCheckRolloutError("generation metadata lacks generation_id")
        if not isinstance(approved, bool):
            raise ContractCheckRolloutError("generation metadata lacks approved state")
        if not isinstance(modes, Mapping) or not modes:
            raise ContractCheckRolloutError("generation metadata lacks contract_check_modes")
        parsed: dict[str, RolloutMode] = {}
        for name, mode in modes.items():
            if not isinstance(name, str) or not name or mode not in {LOG_ONLY, ENFORCE}:
                raise ContractCheckRolloutError("generation metadata has an invalid check mode")
            parsed[name] = mode
        return cls(generation_id=generation_id, approved=approved, modes=parsed)


@dataclass(frozen=True)
class ContractCheckResult:
    name: str
    outcome: CheckOutcome
    mode: RolloutMode
    blocking: bool
    decline_message: str | None = None


def run_contract_check(
    name: str,
    verifier: Callable[[], object],
    *,
    rollout: GenerationCheckRollout,
    decline_exceptions: type[Exception] | tuple[type[Exception], ...],
) -> ContractCheckResult:
    """Run one verifier under its persisted generation policy.

    A verifier returns normally for success and raises its normal contract
    exception for a decline.  Only ``enforce`` makes that decline blocking;
    log-only still records the exact decline for replay and promotion.
    """
    mode = rollout.mode_for(name)
    try:
        verifier()
    except decline_exceptions as exc:
        return ContractCheckResult(
            name=name,
            outcome="declined",
            mode=mode,
            blocking=mode == ENFORCE,
            decline_message=format_decline_copy(name, str(exc)),
        )
    return ContractCheckResult(name=name, outcome="passed", mode=mode, blocking=False)


def format_decline_copy(check_name: str, detail: str | None = None) -> str:
    """Stable creator-facing wording for a requirement-specific decline."""
    label = check_name.replace("_", " ").strip()
    message = f"This edit cannot meet the approved {label} requirement."
    return f"{message} {detail}" if detail else message


@dataclass(frozen=True)
class PromotionReport:
    status: Literal["passed", "pending"]
    reasons: tuple[str, ...]
    base_sha: str | None
    candidate_sha: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "reasons": list(self.reasons),
            "base_sha": self.base_sha,
            "candidate_sha": self.candidate_sha,
        }


def validate_promotion(
    evidence: Mapping[str, Any], *, now_epoch_s: int | None = None
) -> PromotionReport:
    """Return a stable passed/pending report; missing proof never passes."""
    import time

    reasons: list[str] = []
    base_sha = evidence.get("base_sha") if isinstance(evidence.get("base_sha"), str) else None
    candidate_sha = (
        evidence.get("candidate_sha") if isinstance(evidence.get("candidate_sha"), str) else None
    )
    if evidence.get("schema_version") != 1:
        reasons.append("unsupported schema_version")
    if not base_sha or not _SHA_RE.fullmatch(base_sha):
        reasons.append("base_sha must be a 40-character lowercase hex SHA")
    if not candidate_sha or not _SHA_RE.fullmatch(candidate_sha):
        reasons.append("candidate_sha must be a 40-character lowercase hex SHA")
    replay = evidence.get("replay")
    if (
        not isinstance(replay, Mapping)
        or replay.get("complete") is not True
        or replay.get("successful") is not True
    ):
        reasons.append("missing complete successful replay")
    elif (
        replay.get("base_sha") != base_sha
        or replay.get("candidate_sha") != candidate_sha
        or isinstance(replay.get("available_count"), bool)
        or not isinstance(replay.get("available_count"), int)
        or replay.get("available_count") <= 0
        or isinstance(replay.get("input_count"), bool)
        or replay.get("input_count") != replay.get("available_count")
        or isinstance(replay.get("replayed_count"), bool)
        or replay.get("replayed_count") != min(100, replay.get("available_count", -1))
    ):
        reasons.append("replay is not base/candidate bound or does not cover its bounded inputs")
    if evidence.get("newly_blocking_declines") != 0:
        reasons.append("candidate newly blocks a successful captured job")
    affected = evidence.get("affected_supported_paths")
    valid_affected_paths = (
        isinstance(affected, list)
        and bool(affected)
        and all(isinstance(path, str) and path.strip() for path in affected)
        and len(set(affected)) == len(affected)
    )
    if not valid_affected_paths:
        reasons.append("affected supported paths must be unique nonempty strings")
    deployment_started = evidence.get("deployment_started_epoch_s")
    clean_since = evidence.get("clean_since_epoch_s")
    now = int(time.time()) if now_epoch_s is None else now_epoch_s
    valid_deployment = (
        not isinstance(deployment_started, bool)
        and isinstance(deployment_started, int)
        and deployment_started >= 0
    )
    valid_clean_since = (
        not isinstance(clean_since, bool) and isinstance(clean_since, int) and clean_since >= 0
    )
    if not valid_clean_since:
        reasons.append("missing clean-hour evidence")
    elif now - clean_since < 24 * 60 * 60:
        reasons.append("fewer than 24 clean hours")
    elif not valid_deployment or clean_since < deployment_started:
        reasons.append("clean window predates candidate deployment")
    observations = evidence.get("clean_observations")
    if not isinstance(observations, list) or not observations:
        reasons.append("missing clean observations")
    else:
        intervals: list[tuple[int, int]] = []
        total_observations = 0
        for observation in observations:
            if (
                not isinstance(observation, Mapping)
                or observation.get("check_mode") != LOG_ONLY
                or observation.get("stream_complete") is not True
            ):
                reasons.append("clean observations are not log_only contract checks")
                break
            interval_start = observation.get("interval_start_epoch_s")
            interval_end = observation.get("interval_end_epoch_s")
            count = observation.get("check_observations")
            if (
                isinstance(interval_start, bool)
                or not isinstance(interval_start, int)
                or isinstance(interval_end, bool)
                or not isinstance(interval_end, int)
                or interval_start < 0
                or interval_end <= interval_start
                or interval_end > now
                or (valid_deployment and interval_start < deployment_started)
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                reasons.append("clean observations lack valid contiguous interval check counts")
                break
            intervals.append((interval_start, interval_end))
            total_observations += count
            declines = observation.get("check_declines")
            if isinstance(declines, bool) or not isinstance(declines, int) or declines < 0:
                reasons.append("clean observations lack valid contract-check decline counts")
                break
            if declines != 0:
                reasons.append("clean window contains contract-check declines")
                break
            if (
                observation.get("api_revision") != candidate_sha
                or observation.get("worker_revision") != candidate_sha
            ):
                reasons.append("clean observations do not match candidate revisions")
                break
        if intervals and valid_clean_since:
            intervals.sort()
            contiguous = intervals[0][0] == clean_since and all(
                previous[1] == current[0] for previous, current in zip(intervals, intervals[1:])
            )
            if (
                not contiguous
                or intervals[-1][1] < clean_since + 24 * 60 * 60
                or total_observations <= 0
            ):
                reasons.append("clean observations do not cover one continuous 24-hour stream")
    canary_evidence = evidence.get("canary_evidence")
    if not isinstance(canary_evidence, list) or not canary_evidence:
        reasons.append("missing measured canary evidence")
    else:
        by_path = {
            item.get("path"): item
            for item in canary_evidence
            if isinstance(item, Mapping) and isinstance(item.get("path"), str)
        }
        for path in affected if valid_affected_paths else ():
            item = by_path.get(path)
            if (
                not isinstance(item, Mapping)
                or item.get("measured_export_success") is not True
                or item.get("api_revision") != candidate_sha
                or item.get("worker_revision") != candidate_sha
                or not isinstance(item.get("export_duration_s"), (int, float))
                or isinstance(item.get("export_duration_s"), bool)
                or not math.isfinite(item.get("export_duration_s"))
                or item.get("export_duration_s") <= 0
                or isinstance(item.get("frame_count"), bool)
                or not isinstance(item.get("frame_count"), int)
                or item.get("frame_count") <= 0
                or not isinstance(item.get("command"), str)
                or not item.get("command").strip()
                or not isinstance(item.get("app_build"), str)
                or not item.get("app_build").strip()
                or not isinstance(item.get("source_fixture_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", item.get("source_fixture_sha256"))
                or not isinstance(item.get("source_provenance"), Mapping)
                or not item.get("source_provenance")
                or isinstance(item.get("timestamp_epoch_s"), bool)
                or not isinstance(item.get("timestamp_epoch_s"), int)
                or not valid_deployment
                or item.get("timestamp_epoch_s") < deployment_started
                or item.get("timestamp_epoch_s") > now
            ):
                reasons.append("canary lacks measured export success at candidate revisions")
                break
    return PromotionReport(
        status="passed" if not reasons else "pending",
        reasons=tuple(reasons),
        base_sha=base_sha,
        candidate_sha=candidate_sha,
    )
