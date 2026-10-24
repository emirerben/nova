"""Prepared-execution coverage: one row per render adapter (KRI-725).

``AdapterDeclaration`` already forces every confirmed-contract requirement to be
consumed or declined per adapter. This matrix does the same for approval identity:
for every adapter a creator approval can reach, which snapshot field or Job pin
carries each approval-critical input, which function fences the worker start and
the publication, and what happens to every approved decision key.

It is accounting, not proof. The E2E evidence lives in
``tests/kria/test_kri725_cloud_prepared_postgres.py`` (cloud) and
``tests/kria/test_prepared_execution_postgres.py`` (device). A ``gap:`` row names
the Linear ticket that owns the missing capability, so unmet demand stays visible
instead of being narrowed away.

A new adapter, or a decision key with no row, fails at import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from app.services.cloud_render_contract import CLOUD_ADAPTER_DECLARATIONS
from app.services.creator_render_contract import ADAPTER_DECLARATIONS

# Every key ``prepared_execution.build_decisions`` can record.
DECISION_KEYS: tuple[str, ...] = (
    "text",
    "length",
    "order",
    "audio",
    "direction",
    "editor",
    "shape",
    "speech_cleanup",
)

_GAP = re.compile(r"^gap:KRI-\d+$")  # kri667: validates our own coverage-state literals
# A refused creator ask is unmet demand: it needs a gap:KRI-n owner, never a fixed state.
_FIXED = frozenset({"executed", "not_applicable"})

ApprovalPath = Literal["prepared", "not_via_approval"]


@dataclass(frozen=True)
class Coverage:
    adapter: str
    approval_path: ApprovalPath
    reason: str
    # Approval-critical input -> where the approved value is carried to the renderer.
    pinned_inputs: dict[str, str]
    worker_fence: str
    publication_fence: str
    decisions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        missing = set(DECISION_KEYS) - set(self.decisions)
        extra = set(self.decisions) - set(DECISION_KEYS)
        if missing or extra:
            raise ValueError(
                f"{self.adapter}: decisions must account for exactly {DECISION_KEYS} "
                f"(missing {sorted(missing)}, unknown {sorted(extra)})"
            )
        for key, value in self.decisions.items():
            if value not in _FIXED and not _GAP.match(value):
                raise ValueError(f"{self.adapter}.{key}: {value!r} is not a coverage state")
        if self.approval_path == "prepared" and not (self.worker_fence and self.publication_fence):
            raise ValueError(f"{self.adapter}: a prepared adapter needs both fences")


_SOURCES = "snapshot.sources (path, generation, lineage, trim window)"
_CONTRACT = "Job assembly_plan creator_render_contract (digest on the stamp)"
_BRIEF = "snapshot.binding_digest / brief_version (pinned_brief_for_job)"
_LENGTH = "Job creator_brief_binding.media_snapshot.length_plan (_approved_length_plan_envelope)"
_DEVICE_WORKER = "device_render.pin_device_request (recipe copies {id, attempt_id})"
_DEVICE_PUBLISH = "device_render.verify_device_record_contract"
_CLOUD_WORKER = "generative_build._enter_prepared_attempt (prepared_execution.verify_job_stamp)"
_CLOUD_PUBLISH = "generative_build._reject_unverified_cloud_variant / _update_variant_entry"

_PHONE_DECISIONS = {
    "text": "executed",
    "length": "executed",
    "order": "executed",
    "audio": "executed",
    "direction": "executed",
    "editor": "executed",
    "shape": "executed",
    "speech_cleanup": "executed",
}


def _phone(adapter: str, *, speech_cleanup: str = "executed") -> Coverage:
    return Coverage(
        adapter=adapter,
        approval_path="prepared",
        reason="KRI-722: the device recipe is pinned under the approved snapshot and attempt.",
        pinned_inputs={
            "sources": _SOURCES,
            "contract": _CONTRACT,
            "brief": _BRIEF,
            "length": _LENGTH,
            "moments": "Job length_plan v2 envelope (KRI-723) / occurrence pins (KRI-724)",
        },
        worker_fence=_DEVICE_WORKER,
        publication_fence=_DEVICE_PUBLISH,
        decisions={**_PHONE_DECISIONS, "speech_cleanup": speech_cleanup},
    )


PREPARED_COVERAGE: dict[str, Coverage] = {
    row.adapter: row
    for row in (
        _phone("phone_speech_montage"),
        _phone("phone_voice_behind_footage"),
        _phone("phone_guided_unified_montage", speech_cleanup="not_applicable"),
        _phone("phone_voiceover_montage"),
        _phone("phone_subtitled"),
        _phone("phone_narrated"),
        Coverage(
            adapter="cloud_guided_story",
            approval_path="prepared",
            reason=(
                "KRI-725: dispatch binds the recompiled story to the snapshot's "
                "compiled_refs.prepared_collection_story; the worker reads only the Job pin."
            ),
            pinned_inputs={
                "sources": _SOURCES,
                "contract": _CONTRACT,
                "brief": _BRIEF,
                "story": "snapshot.compiled_refs.prepared_collection_story -> "
                "Job guided_story_execution_plan",
            },
            worker_fence=_CLOUD_WORKER,
            publication_fence=_CLOUD_PUBLISH,
            decisions={
                "text": "executed",
                "length": "executed",
                "order": "executed",
                # Camera-audio-only requests (audio_source_ids) stay declined at preflight.
                "audio": "gap:KRI-725",
                "direction": "executed",
                "editor": "executed",
                "shape": "executed",
                "speech_cleanup": "executed",
            },
        ),
        Coverage(
            adapter="cloud_classic",
            approval_path="prepared",
            reason=(
                "KRI-725: approval inputs were already Job-pinned at dispatch; the shared "
                "cloud worker and publication fences now bind them to the attempt."
            ),
            pinned_inputs={
                "sources": _SOURCES,
                "contract": _CONTRACT,
                "brief": _BRIEF,
                "length": _LENGTH,
            },
            worker_fence=_CLOUD_WORKER,
            publication_fence=_CLOUD_PUBLISH,
            decisions={
                # Exact on-screen text asks are declined at preflight_cloud_contract.
                "text": "gap:KRI-725",
                "length": "executed",
                # order_required + KRI-723 moments[] receipts are not produced in cloud.
                "order": "gap:KRI-725",
                # original_audio / audio_source_ids are declined at preflight.
                "audio": "gap:KRI-725",
                "direction": "executed",
                "editor": "executed",
                "shape": "executed",
                "speech_cleanup": "executed",
            },
        ),
        Coverage(
            adapter="cloud_slides",
            approval_path="prepared",
            reason=(
                "KRI-725: slides/stickers are snapshot sources, the draft digest is "
                "compiled_refs.slide_post, and the Job carries the approved draft. Kria's "
                "strategy policy does not offer slides yet, so production slide posts "
                "still dispatch unstamped from the slides route."
            ),
            pinned_inputs={
                "sources": "snapshot.sources slide:i / sticker:i:id (asset id, generation)",
                "slide_post": "snapshot.compiled_refs.slide_post -> Job approved_slide_post",
                "contract": _CONTRACT,
                "brief": _BRIEF,
            },
            worker_fence=_CLOUD_WORKER,
            publication_fence=_CLOUD_PUBLISH,
            decisions={
                "text": "executed",
                "length": "not_applicable",
                "order": "executed",
                "audio": "not_applicable",
                "direction": "executed",
                "editor": "not_applicable",
                "shape": "executed",
                "speech_cleanup": "not_applicable",
            },
        ),
    )
}

_ADAPTERS = set(ADAPTER_DECLARATIONS) | set(CLOUD_ADAPTER_DECLARATIONS)
if set(PREPARED_COVERAGE) != _ADAPTERS:
    raise ValueError(
        "Every render adapter needs a prepared-coverage row: "
        f"missing {sorted(_ADAPTERS - set(PREPARED_COVERAGE))}, "
        f"unknown {sorted(set(PREPARED_COVERAGE) - _ADAPTERS)}"
    )


def coverage_table() -> str:
    """The Markdown table ``docs/pipelines/creator-render-contract.md`` carries."""
    header = ["adapter", *DECISION_KEYS]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join("---" for _ in header) + "|",
    ]
    for adapter in sorted(PREPARED_COVERAGE):
        row = PREPARED_COVERAGE[adapter]
        lines.append(
            "| " + " | ".join([adapter, *(row.decisions[k] for k in DECISION_KEYS)]) + " |"
        )
    return "\n".join(lines)
