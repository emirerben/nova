"""Load incident records and rebuild the approved contract the way dispatch does."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief_binding import BriefBinding
from app.services.creator_render_contract import CreatorRenderContract, build_render_contract
from tests.incidents.models import IncidentRecord

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "incidents"
API_ROOT = Path(__file__).resolve().parents[2]


def load_records() -> list[IncidentRecord]:
    return [
        IncidentRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(FIXTURE_DIR.glob("*.json"))
    ]


def media_snapshot(record: IncidentRecord) -> dict:
    """The approved-media snapshot as the phone persists it (capture time per clip)."""
    rows = []
    for media in record.inputs.media:
        row: dict = {
            "media_id": media.id,
            "kind": media.kind,
            "duration_s": media.duration_s,
            "has_audio": media.has_audio,
        }
        if media.capture_time is not None:
            row["capture"] = {"capture_time": media.capture_time.strftime("%Y-%m-%dT%H:%M:%SZ")}
        rows.append(row)
    return {"clip_assignments": rows}


def binding_for(record: IncidentRecord) -> BriefBinding:
    return BriefBinding.create(
        uuid.uuid5(uuid.NAMESPACE_URL, f"incident/{record.id}"),
        record.approved.brief,
        latest_message=record.approved.creator_request,
        media_snapshot=media_snapshot(record),
    )


def validate_against_current_schemas(record: IncidentRecord) -> None:
    """Fail loudly when a record no longer matches the real approval models."""
    if record.approved.strategy is not None:
        CreativeStrategy.model_validate(record.approved.strategy)
    binding_for(record)  # BriefBinding re-verifies its own digest


def build_contract(record: IncidentRecord) -> CreatorRenderContract | None:
    """Mirror ``content_plan_build`` dispatch: rebuild against the approved binding."""
    binding = binding_for(record)
    return build_render_contract(
        record.approved.strategy,
        generation_id=f"incident-{record.id}",
        brief=binding.resolve(),
        media_snapshot=binding.media_snapshot,
        has_voiceover=bool(record.inputs.voiceover_id),
    )


def capture_sorted_ids(record: IncidentRecord) -> list[str]:
    """Selected media in capture order (all media when the strategy selects none)."""
    selected = set((record.approved.strategy or {}).get("selected_media_ids") or ())
    rows = [m for m in record.inputs.media if m.capture_time and (not selected or m.id in selected)]
    return [m.id for m in sorted(rows, key=lambda m: m.capture_time)]
