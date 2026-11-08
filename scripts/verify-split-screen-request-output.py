#!/usr/bin/env python3
"""Compile authored offline replay against already generated, fingerprinted originals.

Native preparation and consumption are separate serial commands. This bridge does
not call a provider, dispatch a job, probe media, or render any pixels.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = (
    ROOT
    / "src/apps/ios/Packages/KriaMediaEngine/Tests/KriaMediaEngineTests/Fixtures"
    / "split-screen-request-output.json"
)
sys.path.insert(0, str(ROOT / "src/apps/api"))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


@contextmanager
def replay_settings():
    from app.config import settings

    overrides = {
        "phone_rendering_enabled": True,
        "GENERATIVE_TIMELINE_EDITOR_ENABLED": True,
        "phone_editor_media_enabled": True,
        "phone_render_verified_features": [
            "basicComposition",
            "local1080Export",
            "audioMix",
            "variableSpeed",
            "stillImages",
            "visualVideos",
            "visualBlocks",
            "alphaOverlay",
            "splitScreen",
        ],
    }
    original = {key: getattr(settings, key) for key in overrides}
    try:
        for key, value in overrides.items():
            setattr(settings, key, value)
        yield overrides
    finally:
        for key, value in original.items():
            setattr(settings, key, value)


def read_sources(
    directory: Path, scenario: dict, scenario_hash: str
) -> tuple[dict, bytes]:
    receipt_bytes = (directory / "source-receipts.json").read_bytes()
    receipt = json.loads(receipt_bytes)
    if (
        receipt.get("schema_version") != 1
        or receipt.get("scenario_sha256") != scenario_hash
    ):
        raise ValueError("source receipts do not bind this exact scenario")
    rows = receipt["sources"]
    by_id = {row["media_id"]: row for row in rows}
    if len(rows) != len(by_id) or set(by_id) != {
        source["id"] for source in scenario["sources"]
    }:
        raise ValueError("source receipt identities do not match the reviewed scenario")
    for source in scenario["sources"]:
        row = by_id[source["id"]]
        relative = Path(row["path"])
        if relative.name != row["path"] or (directory / relative).is_symlink():
            raise ValueError("source receipts must name local original files")
        data = (directory / relative).read_bytes()
        if (digest(data), len(data)) != (row["sha256"], row["byte_count"]):
            raise ValueError("original bytes changed after native receipt generation")
        for field in ("kind", "width", "height"):
            if row[field] != source[field]:
                raise ValueError(
                    f"source {field} does not match the reviewed specification"
                )
        if source["kind"] == "video":
            if abs(row["duration_s"] - source["duration_s"]) > 1 / source["fps"]:
                raise ValueError(
                    "source duration does not match the reviewed specification"
                )
            if row["has_audio"] is not True:
                raise ValueError("generated video lacks its specified source audio")
    return by_id, receipt_bytes


def compile_case(case: dict, sources: dict, *, control: bool) -> dict:
    from app.agents._schemas.creator_agent import (
        CapabilityAvailability,
        CreatorMediaRef,
        ResolvedCreatorManifest,
    )
    from app.agents.main_creator import MainCreatorAgent, MainCreatorInput
    from app.kria.device_render import make_device_request
    from app.kria.media_sources import OriginalMediaDescriptor
    from app.pipeline.phone_initial_split_screen import compile_initial_split_screen
    from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
    from app.services.creator_capabilities import compile_strategy_to_plan
    from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD
    from app.services.phone_sources import (
        PHONE_SOURCES_FIELD,
        PhoneSourceBinding,
        PhoneVisualBinding,
    )

    manifest = ResolvedCreatorManifest(
        item_id="split-replay-item",
        edit_format="montage",
        render_program="guided",
        media=[
            CreatorMediaRef(media_id=key, kind=row["kind"])
            for key, row in sources.items()
        ],
        capabilities={
            key: CapabilityAvailability(available=True)
            for key in (
                "edit_format:montage",
                "draft_guided_proposal",
                "dispatch_render",
                "split_screen",
            )
        },
        context_hash=digest(case["request"].encode()),
        manifest_hash=digest(encoded(sources)),
    )
    agent_input = MainCreatorInput(
        user_message=case["request"],
        creator_request=case["request"],
        reply_language=case["language"],
        media_context=[
            {"media_id": key, "kind": row["kind"]} for key, row in sources.items()
        ],
        capability_manifest=manifest,
    )
    raw = copy.deepcopy(case["authored_response"])
    if control:
        panels = raw["action"]["strategy"]["split_screen"]["panels"]
        a, b = case["control_swap"]
        panels[a], panels[b] = panels[b], panels[a]
    parsed = MainCreatorAgent(None).parse(json.dumps(raw), agent_input)
    plan = compile_strategy_to_plan(manifest, parsed.action.strategy)
    bindings = []
    editor_sources = []
    for media_id, row in sources.items():
        if row["kind"] == "video":
            bindings.append(
                PhoneSourceBinding(
                    media_id=media_id,
                    proxy_path=f"users/split-replay/analysis-proxy-{media_id}.mp4",
                    generation="1",
                    original=OriginalMediaDescriptor(
                        **{
                            key: row[key]
                            for key in (
                                "sha256",
                                "byte_count",
                                "duration_s",
                                "width",
                                "height",
                                "has_audio",
                            )
                        }
                    ),
                )
            )
        else:
            visual = PhoneVisualBinding(
                media_id=media_id,
                gcs_path=f"users/split-replay/{media_id}.png",
                generation="1",
                sha256=row["sha256"],
                byte_count=row["byte_count"],
            )
            editor_sources.append(
                {
                    "media_id": media_id,
                    "lane": "asset",
                    "gcs_path": visual.gcs_path,
                    "generation": "1",
                    "kind": "image",
                    "status": "ready",
                    "source_index": sum(
                        row["kind"] == "video" for row in sources.values()
                    )
                    + len(editor_sources),
                    "visual_binding": visual.model_dump(mode="json"),
                }
            )
    ordinary = next(binding for binding in bindings if binding.media_id == "ordinary")
    variant = {
        "variant_id": "original_text",
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "editor_timeline_mode": "authored",
        "caption_cues": [],
        "text_elements": [],
        "text_elements_user_edited": True,
        EDITOR_SOURCES_FIELD: {"sources": editor_sources},
    }
    job = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000001231"),
        user_id=UUID("00000000-0000-0000-0000-000000000001"),
        assembly_plan={
            PHONE_SOURCES_FIELD: [
                binding.model_dump(mode="json") for binding in bindings
            ],
            "variants": [variant],
        },
        all_candidates={
            "clip_paths": [binding.proxy_path for binding in bindings],
            "creator_strategy": plan.strategy.model_dump(
                mode="json", exclude_none=True
            ),
        },
    )
    initial = make_device_request(
        job_id=job.id,
        variant_id="original_text",
        revision=1,
        recipe=compile_phone_subtitled_plan((ordinary,), caption_cues=[]),
    )
    result = compile_initial_split_screen(job, initial)
    recipe = result.recipe
    if abs(recipe.duration - case["expected_duration_s"]) > 1 / recipe.frame_rate:
        raise ValueError("compiled replay changed the ordinary output duration")
    return {
        "authored_response": raw,
        "parsed": parsed.model_dump(mode="json"),
        "policy": plan.model_dump(mode="json"),
        "initial_request": initial.model_dump(mode="json"),
        "request": result.model_dump(mode="json"),
        "recipe": recipe.model_dump(mode="json", exclude_none=True),
        "editable_timeline": job.assembly_plan["variants"][0]["user_timeline"],
    }


def compile_replay(directory: Path, scenario_path: Path = SCENARIO) -> dict:
    directory = directory.resolve()
    if (directory / "compiled-index.json").exists() or list(
        directory.glob("*-recipe.json")
    ):
        raise FileExistsError(
            "retain previous evidence; use a fresh artifact directory"
        )
    scenario_bytes = scenario_path.read_bytes()
    scenario_hash = digest(scenario_bytes)
    scenario = json.loads(scenario_bytes)
    sources, receipt_bytes = read_sources(directory, scenario, scenario_hash)
    outputs = {}
    cases = []
    with replay_settings() as settings_snapshot:
        for case in scenario["cases"]:
            for control in (False, True):
                stem = case["id"] + ("-control" if control else "-positive")
                compiled = compile_case(case, sources, control=control)
                recipe_bytes = encoded(compiled.pop("recipe"))
                outputs[f"{stem}-recipe.json"] = recipe_bytes
                outputs[f"{stem}-request.json"] = encoded(compiled)
                cases.append(
                    {
                        "id": case["id"],
                        "control": control,
                        "recipe_path": f"{stem}-recipe.json",
                        "recipe_sha256": digest(recipe_bytes),
                        "request_path": f"{stem}-request.json",
                    }
                )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True
    )
    index = {
        "schema_version": 1,
        "head_sha": head,
        "working_tree_dirty": bool(dirty),
        "scenario_sha256": scenario_hash,
        "source_receipts_sha256": digest(receipt_bytes),
        "evidence_level": scenario["evidence_level"],
        "excluded_boundaries": scenario["excluded_boundaries"],
        "local_test_settings": settings_snapshot,
        "cases": cases,
    }
    outputs["compiled-index.json"] = encoded(index)
    for name, data in outputs.items():
        with (directory / name).open("xb") as output:
            output.write(data)
    return index


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    arguments = parser.parse_args()
    result = compile_replay(arguments.artifacts)
    print(
        json.dumps(
            {"head_sha": result["head_sha"], "compiled_cases": len(result["cases"])}
        )
    )
