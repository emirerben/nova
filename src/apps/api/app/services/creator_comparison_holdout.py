"""Registration for a sealed evaluator; no body export or development tuning API.

The authorized evaluator holds the private packet. Only source/config hashes and
aggregate counts are public. A registration is immutable and precedes live work.
"""

from __future__ import annotations

from app.services.request_following_cases import digest


def case_groups(case):
    if not case.get("creator_id") or not case.get("event_group_ids"):
        raise ValueError("creator and media-event groups are required")
    return {"creator:" + case["creator_id"], *("event:" + v for v in case["event_group_ids"])}


def register_holdout(packet, *, development_groups):
    if not packet.get("cases") or not packet.get("arms"):
        raise ValueError("nonempty frozen cases and arms required")
    if packet.get("sha256") != digest({k: v for k, v in packet.items() if k != "sha256"}):
        raise ValueError("registration packet hash mismatch")
    groups = set()
    for case in packet["cases"]:
        if case.get("split") != "holdout":
            raise ValueError("sealed registration requires holdout cases")
        groups.update(case_groups(case))
        if case.get("source_kind") != "synthetic_control":
            review = case.get("intent_review") or {}
            if (
                review.get("decision") != "approved"
                or not review.get("reviewed_by")
                or review.get("input_sha256") != case.get("input_sha256")
                or not review.get("expectations")
            ):
                raise ValueError("holdout requires source-bound human intent review")
    if groups & set(development_groups):
        raise ValueError("creator/media-event split overlap")
    return {
        "version": 1,
        "packet_sha256": packet["sha256"],
        "configuration_sha256": digest(packet["arms"]),
        "groups_sha256": digest(sorted(groups)),
        "development_groups_sha256": digest(sorted(development_groups)),
        "case_count": len(packet["cases"]),
        "repetitions": packet["repetitions"],
        "body_access": "sealed_evaluator_only",
        "quality_verdict": "unassessed",
    }


def validate_registration(packet, registration):
    if (
        registration.get("version") != 1
        or registration.get("packet_sha256") != packet.get("sha256")
        or packet.get("sha256") != digest({k: v for k, v in packet.items() if k != "sha256"})
        or registration.get("configuration_sha256") != digest(packet["arms"])
        or registration.get("groups_sha256")
        != digest(sorted(set().union(*(case_groups(c) for c in packet["cases"]))))
    ):
        raise ValueError("sealed registration mismatch")
