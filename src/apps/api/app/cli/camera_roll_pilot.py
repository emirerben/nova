"""Offline preregistered pilot reporting. No providers, outreach or flag changes."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class PilotProtocol(_Strict):
    registered_at: AwareDatetime
    creator_count: int = Field(ge=10)
    collections_per_creator: int = Field(ge=2)
    selection_floor: float = Field(ge=0.5, le=1)
    export_floor: float = Field(ge=0.8, le=1)
    max_corrective_turns: int = Field(ge=0, le=2)
    max_latency_s: float = Field(gt=0)
    max_settled_spend_usd: float = Field(gt=0, le=2)
    reliability_evidence: str | None = Field(default=None, min_length=1, max_length=500)
    native_output_evidence: str | None = Field(default=None, min_length=1, max_length=500)
    app_revision: str = Field(min_length=1, max_length=160)
    server_revision: str = Field(min_length=1, max_length=160)
    capability_cohort: str = Field(min_length=1, max_length=160)


class PilotSession(_Strict):
    session_key: str = Field(min_length=1, max_length=160)
    creator_key: str = Field(min_length=1, max_length=160)
    collection_key: str = Field(min_length=1, max_length=160)
    started_at: AwareDatetime
    internal_stress_test: bool = False
    consented: bool
    invited: bool
    eligible: bool
    exposed: bool
    selected: bool
    approved: bool
    verified_export: bool
    human_fulfillment: Literal["met", "missed", "unknown"] = "unknown"
    export_artifact_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    approved_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    export_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    review_evidence: str | None = Field(default=None, min_length=1, max_length=500)
    corrective_turns: int = Field(ge=0)
    latency_s: float | None = Field(default=None, ge=0)
    model_calls: int | None = Field(default=None, ge=0)
    settled_spend_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def valid_journey(self):
        if self.exposed and not (self.consented and self.eligible):
            raise ValueError("Exposure requires consent and eligibility")
        if self.selected and not self.exposed:
            raise ValueError("Selection requires exposure")
        if self.approved and not (self.selected and self.approved_snapshot_sha256):
            raise ValueError("Approval requires a selected, pinned draft")
        if self.verified_export and not (
            self.approved
            and self.export_artifact_sha256
            and self.review_evidence
            and self.approved_snapshot_sha256 == self.export_snapshot_sha256
        ):
            raise ValueError(
                "Verified export requires approval, actual artifact and matching snapshot review"
            )
        if self.human_fulfillment != "unknown" and not self.review_evidence:
            raise ValueError("Fulfillment requires independent review evidence")
        return self


def report(protocol: PilotProtocol, sessions: list[PilotSession]) -> dict:
    if len(sessions) > 10_000:
        raise ValueError("Pilot ledger exceeds 10000 sessions")
    if len({row.session_key for row in sessions}) != len(sessions):
        raise ValueError("Duplicate session keys")
    pilot = [row for row in sessions if not row.internal_stress_test]
    if any(row.started_at < protocol.registered_at for row in pilot):
        raise ValueError("Protocol must be registered before pilot sessions start")
    counts = {
        "invited_creators": len({row.creator_key for row in pilot if row.invited}),
        "consented_creators": len({row.creator_key for row in pilot if row.consented}),
        "eligible_sessions": sum(row.consented and row.eligible for row in pilot),
        "exposed": sum(row.exposed for row in pilot),
        "selected": sum(row.selected for row in pilot),
        "approved": sum(row.approved for row in pilot),
        "verified_exports": sum(row.verified_export for row in pilot),
        "no_selection": sum(row.exposed and not row.selected for row in pilot),
        "selected_without_export": sum(row.selected and not row.verified_export for row in pilot),
        "missed_instructions": sum(row.human_fulfillment == "missed" for row in pilot),
        "unknown_fulfillment": sum(
            row.selected and row.human_fulfillment == "unknown" for row in pilot
        ),
        "corrective_turns": sum(row.corrective_turns for row in pilot),
    }
    fulfilled = sum(
        row.verified_export
        and row.human_fulfillment == "met"
        and row.corrective_turns <= protocol.max_corrective_turns
        for row in pilot
    )
    selection_rate = (
        counts["selected"] / counts["eligible_sessions"] if counts["eligible_sessions"] else None
    )
    export_rate = fulfilled / counts["selected"] if counts["selected"] else None
    collections = defaultdict(set)
    for row in pilot:
        if row.consented and row.eligible and row.exposed:
            collections[row.creator_key].add(row.collection_key)
    sample_met = (
        sum(len(values) >= protocol.collections_per_creator for values in collections.values())
        >= protocol.creator_count
    )
    measured = [row for row in pilot if row.consented and row.eligible]
    resource_evidence = bool(measured) and all(
        row.latency_s is not None
        and row.model_calls is not None
        and row.settled_spend_usd is not None
        for row in measured
    )
    resource_caps = resource_evidence and all(
        row.latency_s <= protocol.max_latency_s
        and row.settled_spend_usd <= protocol.max_settled_spend_usd
        for row in measured
    )
    bars = {
        "sample": sample_met,
        "selection": selection_rate is not None and selection_rate >= protocol.selection_floor,
        "fulfilled_exports": export_rate is not None and export_rate >= protocol.export_floor,
        "resource_evidence": resource_evidence,
        "resource_caps": resource_caps,
        "reliability_evidence": bool(protocol.reliability_evidence),
        "native_output_evidence": bool(protocol.native_output_evidence),
    }
    return {
        "schema_version": 1,
        "protocol_sha256": hashlib.sha256(protocol.model_dump_json().encode()).hexdigest(),
        "registered_at": protocol.registered_at.isoformat(),
        "app_revision": protocol.app_revision,
        "server_revision": protocol.server_revision,
        "capability_cohort": protocol.capability_cohort,
        "counts": counts,
        "internal_stress_sessions": len(sessions) - len(pilot),
        "selection_rate": selection_rate,
        "fulfilled_export_rate": export_rate,
        "known_settled_spend_usd": sum(row.settled_spend_usd or 0 for row in measured),
        "unknown_spend_sessions": sum(row.settled_spend_usd is None for row in measured),
        "known_model_calls": sum(row.model_calls or 0 for row in measured),
        "failed_bars": [name for name, passed in bars.items() if not passed],
        "decision": "eligible_for_review" if all(bars.values()) else "hold",
        "evidence_limit": (
            "Private artifact references require human review; "
            "this report changes no rollout settings."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--sessions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.resolve() in {args.protocol.resolve(), args.sessions.resolve()}:
            raise ValueError("Output must not replace the frozen protocol or session ledger")
        protocol = PilotProtocol.model_validate_json(args.protocol.read_text())
        raw = json.loads(args.sessions.read_text())
        if not isinstance(raw, list) or len(raw) > 10_000:
            raise ValueError("Expected at most 10000 session rows")
        result = report(protocol, [PilotSession.model_validate(row) for row in raw])
        with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, delete=False) as file:
            temporary = Path(file.name)
            try:
                file.write(json.dumps(result, indent=2) + "\n")
                file.flush()
                temporary.replace(args.output)
            finally:
                temporary.unlink(missing_ok=True)
    except (ValueError, OSError, ValidationError) as exc:
        # Pydantic error strings can echo private input. Report only the error type.
        parser.exit(2, f"Invalid pilot input ({type(exc).__name__}); no report written.\n")


if __name__ == "__main__":
    main()
