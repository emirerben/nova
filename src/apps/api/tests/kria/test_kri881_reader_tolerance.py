"""KRI-881 rolling deploy: this code reads the approval binding KRI-881 (#1807) writes.

KRI-881 pins the sourced wording the creator saw before approval to the approved brief
binding (``BriefBinding.prepared_research``, in the digest when present). During a Fly
rolling deploy, api and worker machines on this code and on KRI-881 run side by side, so
a draft written by a KRI-881 worker can be approved, dispatched or rendered here.

The fixture was written by KRI-881's own writer (``BriefBinding.create(...,
prepared_research=...)`` and ``drafts.canonical_snapshot`` at the head it names), never
by this code. Failure modes, each pinned below:

1. ``extra="forbid"`` rejects the field: the draft, the approval and every worker read of
   ``creator_brief_binding`` fail validation.
2. The field is accepted but not in the digest the same way: "Brief binding digest
   mismatch" (tamper detection fires on an honest binding).
3. The field is accepted but dropped on re-save: the job's binding no longer carries what
   the creator approved, and a KRI-881 render finds no pinned research.
4. The draft's canonical snapshot hashes differently here, so the approval's
   draft-snapshot fence calls the draft changed.
5. A tampered or stripped ``prepared_research`` still verifies.
6. A drafted-on-KRI-881, approved-here chat does not reach the worker with the field intact.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.kria.brief_binding import BriefBinding
from app.kria.drafts import KriaDraftDocument, canonical_snapshot

FIXTURE = json.loads(
    (
        Path(__file__).parents[1] / "fixtures" / "kri881" / "prepared_research_binding.json"
    ).read_text()
)


# == 1, 2, 3: the binding validates, verifies and round-trips byte for byte ==================


def test_a_kri881_binding_validates_and_verifies_here() -> None:
    binding = BriefBinding.model_validate(FIXTURE["binding"])
    assert binding.digest == FIXTURE["binding"]["digest"]
    assert binding.prepared_research == FIXTURE["prepared_research"]
    assert binding.resolve(FIXTURE["binding"]["thread_id"]) is not None


def test_a_kri881_binding_round_trips_without_losing_research() -> None:
    binding = BriefBinding.model_validate(FIXTURE["binding"])
    dumped = binding.model_dump(mode="json")
    assert dumped == FIXTURE["binding"]
    assert BriefBinding.model_validate(json.loads(json.dumps(dumped))).digest == binding.digest


def test_this_writer_mints_the_same_digest_as_kri881() -> None:
    """A machine on this code minting the binding from the same inputs agrees with KRI-881."""
    raw = FIXTURE["binding"]
    rebuilt = BriefBinding.model_validate(raw)
    assert (
        BriefBinding._digest(
            rebuilt.thread_id,
            rebuilt.state,
            rebuilt.brief,
            rebuilt.creator_request,
            rebuilt.media_snapshot,
            rebuilt.choice_answers,
            rebuilt.protected_regions,
            FIXTURE["prepared_research"],
        )
        == raw["digest"]
    )


def test_a_binding_without_research_keeps_its_old_shape_and_digest() -> None:
    raw = {k: v for k, v in FIXTURE["binding"].items() if k not in {"prepared_research"}}
    legacy = BriefBinding.create(
        raw["thread_id"],
        BriefBinding.model_validate(FIXTURE["binding"]).brief,
        latest_message="Include sources for the historical facts",
        media_snapshot=raw["media_snapshot"],
    )
    assert "prepared_research" not in legacy.model_dump(mode="json")


# == 4: the draft snapshot hashes the same here ==============================================


def test_a_kri881_draft_snapshot_hashes_identically_here() -> None:
    document = KriaDraftDocument.model_validate(FIXTURE["draft_snapshot"])
    snapshot, snapshot_hash = canonical_snapshot(document)
    assert snapshot == FIXTURE["draft_snapshot"]
    assert snapshot_hash == FIXTURE["draft_snapshot_hash"]


# == 5: tamper detection still covers the research ===========================================


def test_tampered_research_is_refused() -> None:
    raw = json.loads(json.dumps(FIXTURE["binding"]))
    raw["prepared_research"]["texts"][0]["text"] = "Built in 1066."
    with pytest.raises(ValueError, match="digest mismatch"):
        BriefBinding.model_validate(raw)


def test_stripped_research_is_refused() -> None:
    raw = {k: v for k, v in FIXTURE["binding"].items() if k != "prepared_research"}
    with pytest.raises(ValueError, match="digest mismatch"):
        BriefBinding.model_validate(raw)
