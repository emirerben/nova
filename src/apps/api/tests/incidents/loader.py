"""Load incident records and rebuild the approved contract the way dispatch does."""

from __future__ import annotations

import re
import shlex
import uuid
from pathlib import Path

from app.agents._schemas.creator_agent import CreativeStrategy
from app.kria.brief_binding import BriefBinding
from app.services.creator_render_contract import CreatorRenderContract, build_render_contract
from tests.incidents.models import IncidentRecord

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "incidents"
API_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = API_ROOT.parents[2]


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
    # Building the binding runs BriefBinding's own validators (state/brief agreement).
    binding_for(record)


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


def route_job(record: IncidentRecord, platform: str) -> tuple[dict, dict]:
    """The persisted job shape (assembly_plan, all_candidates) a stamped dispatch would read.

    Mirrors what approval persists: the contract, the approved strategy, the binding's
    media snapshot (with per-clip speech facts), the plan's edit format and the media
    present (recording / song). Request text is never put here -- the point of PR-D is that
    nothing downstream reads it.
    """
    from app.services.creator_render_contract import (
        CONTRACT_FIELD,
        PLAN_AUTHORITY_FIELD,
        REQUIREMENT_VERSION_FIELD,
    )
    from app.services.phone_sources import PHONE_SOURCES_FIELD

    contract = build_contract(record)
    assert contract is not None, "a route expectation needs a built contract"
    binding = binding_for(record)
    rows = []
    for row, media in zip(
        binding.media_snapshot["clip_assignments"], record.inputs.media, strict=True
    ):
        row = dict(row)
        if media.speech is not None:
            row["analysis"] = {
                "understanding": {
                    "speech": {
                        "has_speech": media.speech.has_speech,
                        "to_camera": bool(media.speech.to_camera),
                        "transcript": "spoken" if media.speech.has_speech else "",
                    }
                }
            }
        rows.append(row)
    strategy = record.approved.strategy
    assembly: dict = {
        CONTRACT_FIELD: contract.model_dump(mode="json"),
        "creator_generation_id": contract.generation_id,
        "creator_brief_binding": {
            **binding.model_dump(mode="json"),
            "media_snapshot": {"clip_assignments": rows},
        },
    }
    if platform == "phone":
        assembly[PHONE_SOURCES_FIELD] = []
    if record.inputs.cloud_adapter == "cloud_guided_story" or record.inputs.guided_plan is not None:
        assembly["guided_edit"] = {"approved_proposal": {}}
    candidates: dict = {
        REQUIREMENT_VERSION_FIELD: 1,
        PLAN_AUTHORITY_FIELD: 1,
        "edit_format": (strategy or {}).get("edit_format", "montage"),
        "clip_paths": [media.id for media in record.inputs.media],
    }
    if strategy is not None:
        candidates["creator_strategy"] = strategy
    if record.inputs.voiceover_id:
        candidates["voiceover_gcs_path"] = f"voiceover/{record.inputs.voiceover_id}"
    if record.inputs.song is not None:
        candidates["user_song"] = {
            "gcs_path": "song/incident",
            "generation": 1,
            "duration_s": record.inputs.song.duration_s,
            "sync": "lipsync" if record.inputs.song.mode == "lipsync" else "background",
        }
    return assembly, candidates


def capture_sorted_ids(record: IncidentRecord) -> list[str]:
    """Selected media in capture order (all media when the strategy selects none)."""
    selected = set((record.approved.strategy or {}).get("selected_media_ids") or ())
    rows = [m for m in record.inputs.media if m.capture_time and (not selected or m.id in selected)]
    return [m.id for m in sorted(rows, key=lambda m: m.capture_time)]


def unresolved_reference(command: str) -> str | None:
    """Why ``command`` does not resolve to something real in the repo, or None if it does.

    Used for ``repro`` commands and for post-fix observation ``proof``: free text is not
    proof. Recognised: a pytest node id (file and ``::test`` must exist, ``-k`` must match a
    record), ``make kria-replay FIXTURE=x``, any other ``make <target>`` defined in the
    Makefile, ``swift test --filter X`` (X must appear in a Swift source), or a command
    naming an existing ``.py`` / ``.sh`` / ``.swift`` file.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unparseable command"
    if not tokens:
        return "empty command"
    if tokens[0] == "pytest" and len(tokens) > 1:
        file, _, node = tokens[1].partition("::")
        path = API_ROOT / file
        if not path.is_file():
            return f"missing test file {file}"
        if node:
            name = node.split("[")[0].split("::")[-1]
            if not re.search(rf"def {re.escape(name)}\b", path.read_text(encoding="utf-8")):
                return f"{file} has no test {name}"
        if "-k" in tokens:
            expr = tokens[tokens.index("-k") + 1]
            if not any(expr in record.id for record in load_records()):
                return f"-k {expr} matches no record id"
        return None
    if tokens[0] == "make" and len(tokens) > 1:
        if tokens[1] == "kria-replay":
            fixture = next((t.split("=", 1)[1] for t in tokens if t.startswith("FIXTURE=")), "")
            found = list((API_ROOT / "tests").rglob(f"{fixture}.json")) if fixture else []
            return None if found else f"no replay fixture {fixture!r}"
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        ok = re.search(rf"^{re.escape(tokens[1])}\s*:", makefile, re.MULTILINE)
        return None if ok else f"Makefile has no target {tokens[1]}"
    if tokens[:2] == ["swift", "test"] and "--filter" in tokens:
        name = tokens[tokens.index("--filter") + 1].split("/")[0]
        swift = (REPO_ROOT / "src/apps/ios").rglob("*.swift")
        found = any(name in path.read_text(encoding="utf-8", errors="ignore") for path in swift)
        return None if found else f"no Swift source mentions {name}"
    for token in tokens:
        if token.endswith((".py", ".sh", ".swift")) and any(
            (root / token).is_file() for root in (REPO_ROOT, API_ROOT)
        ):
            return None
    return "does not name a pytest id, make target, swift filter or existing script"
