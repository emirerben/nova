#!/usr/bin/env python3
"""Write an offline KRI-524 device fixture from the captured creation reply.

This is deliberately not a hand-written recipe.  It replays the explicitly
labelled model-transport fixture in ``creation_composition.json`` through the
production creation composer, applies the same portable editor operation the
chat uses for "twice as fast", reloads the resulting server draft, and calls
the production phone compiler.  A metered live capture can be supplied with
``KRI_524_RETIME_RESPONSE``; its JSON replaces only the follow-up transport
fixture.  No model request is made by this script.
"""
# ruff: noqa: E402  # bootstrap app import path and test-only settings first

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src/apps/api"))
for name, value in {
    "STORAGE_BUCKET": "nova-test",
    "DATABASE_URL": "postgresql://postgres:postgres@localhost:5432/nova_kri524_test",
    "REDIS_URL": "redis://localhost:6379/0",
    "INTERNAL_API_KEY": "test",
}.items():
    os.environ.setdefault(name, value)

from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.config import settings
from app.kria.device_render import DeviceRenderStatus, make_device_request
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    compile_proposal_execution_plan,
)
from app.pipeline.phone_guided_plan import compile_phone_guided_plan
from app.schemas.edit_proposal import EditProposalSnapshot
from app.services.creation_text_composition import (
    CreationTextComposer,
    _lanes,
    compose_creation_text,
)
from app.services.cloud_render_contract import check_guided_plan_text
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    TextRequirement,
    verify_phone_recipe,
)
from app.services.kria_editor_ops import apply_text_lane_ops
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding

TITLE = "Join us for our favorite bakery and tea shop near the harbor"


def color_clip(path: Path, color: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=1080x1920:r=30:d=12",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=12",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )


def fingerprint(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def captured_case() -> dict:
    data = json.loads(
        (
            REPO
            / "src/apps/api/tests/fixtures/prompt_coverage/creation_composition.json"
        ).read_text()
    )
    return next(case for case in data["cases"] if case["id"] == "words")


def compose_from_capture(case: dict):
    """Replay only the saved transport bytes; the compiler remains production."""
    original = CreationTextComposer.__dict__.get("run")

    def run(self, input, *, ctx=None):
        return self.parse(case["model_response"], input)

    CreationTextComposer.run = run
    try:
        return compose_creation_text(
            EditProposalSnapshot.model_validate(case["snapshot"]),
            creator_request=case["request"],
        )
    finally:
        if original is None:
            del CreationTextComposer.run
        else:
            CreationTextComposer.run = original


def retime(plan: dict, followup: dict) -> tuple[dict, str]:
    texts, slots = _lanes(plan)
    response_path = os.environ.get("KRI_524_RETIME_RESPONSE_PATH")
    if response_path:
        raw = json.loads(Path(response_path).read_text())
        provenance = "metered_live_capture"
    elif os.environ.get("KRI_524_RETIME_RESPONSE"):
        raw = json.loads(os.environ["KRI_524_RETIME_RESPONSE"])
        provenance = "metered_live_capture"
    else:
        raw = json.loads(followup["model_response"])
        provenance = "replay_of_authored_synthetic_live_capture"
    parsed = EditCopilotAgent(None).parse(
        json.dumps(raw),
        EditCopilotInput(
            utterance="Make those words twice as fast; keep everything else.",
            variant_snapshot={
                "editor_ops_version": 2,
                "allowed_op_families": ["text"],
                "text_bars": texts,
                "slots": slots,
                "total_duration_s": 20,
            },
        ),
    )
    state = apply_text_lane_ops(texts, copy.deepcopy(slots), parsed.ops)
    plan = copy.deepcopy(plan)
    plan["text_elements"] = state.text
    return plan, provenance


def editor_fixture(after: dict) -> dict:
    """Project the persisted server draft into the DEBUG host's EditorDraft shell."""

    def fixture_uuid(number: int) -> str:
        return f"00000000-0000-4000-8000-{number:012d}"

    return {
        "projectID": fixture_uuid(524),
        "revision": 1,
        "etag": "kri-524-captured",
        "captions": {"enabled": False, "style": "sentence"},
        "music": None,
        "clips": [
            {
                "id": fixture_uuid(100 + index),
                "asset_id": fixture_uuid(200 + index),
                "clip_index": index,
                "start": row["output_start_s"],
                "end": row["output_end_s"],
                "trim_in": row["source_start_s"],
                "trim_out": row["source_end_s"],
                "source_duration": 12,
                "muted": False,
                "slot_id": row["moment_id"],
            }
            for index, row in enumerate(after["story_timeline"])
        ],
        "text": [
            {
                "id": fixture_uuid(300 + index),
                "content": row["text"],
                "position": [row.get("x_frac", 0.5), row.get("y_frac", 0.5)],
                "style": row.get("font_family", "Inter"),
                "canonicalID": row["id"],
            }
            for index, row in enumerate(after["text_elements"])
        ],
        "serverSnapshot": {
            "schema_version": 2,
            "kind": "editor",
            "editor_capabilities": {"text_elements": True, "timeline": True},
            "editor_payload": {
                "base_generation": "kri-524-captured-server-draft",
                "sections": {
                    "timeline_slots": [
                        {
                            "slot_id": row["moment_id"],
                            "clip_index": index,
                            "in_s": row["source_start_s"],
                            "duration_s": row["duration_s"],
                            "source_duration_s": 12,
                            "removed": False,
                        }
                        for index, row in enumerate(after["story_timeline"])
                    ],
                    "text_elements": after["text_elements"],
                },
            },
        },
    }


def main() -> None:
    out = Path(
        sys.argv[1]
        if len(sys.argv) > 1
        else "/private/tmp/nova-kri-524-state/work/kri-524-creation"
    )
    out.mkdir(parents=True, exist_ok=True)
    stages: dict[str, str] = {}
    capture = captured_case()
    composed = compose_from_capture(capture)
    before = compile_proposal_execution_plan(composed)
    stages["creation_composition"] = "passed"
    after, retime_provenance = retime(before, capture["followup"])
    stages["same_chat_retime"] = "passed"
    # Production holds the approved wording constant while composition changes
    # its representation. Exercise those gates before handing a fixture to iOS.
    contract = CreatorRenderContract(
        generation_id="kri-524-captured-server-draft",
        original_audio="require",
        exact_texts=(
            TextRequirement(role="opening", text=TITLE),
            TextRequirement(role="any", text=TITLE),
        ),
        order_required=True,
        order_ids=("c0", "c1"),
    ).rebind()
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    for candidate in (before, after):
        check_guided_plan_text(assembly, candidates=None, plan=candidate)
    stages["guided_validation"] = "passed"
    # Save and reopen the approved draft before the phone compiler sees it.
    # This is a filesystem persistence boundary, not a database write.
    saved_plan = out / "saved-guided-plan.json"
    saved_plan.write_text(json.dumps(after, indent=2) + "\n")
    plan = GuidedStoryExecutionPlan.model_validate_json(saved_plan.read_text())
    stages["persistence_reload"] = "passed"
    for moment in plan.story_timeline:
        moment.gcs_path = f"users/test/analysis-proxy-{moment.media_id}.mp4"
    files = {"c0": out / "creation-c0.mp4", "c1": out / "creation-c1.mp4"}
    color_clip(files["c0"], "red")
    color_clip(files["c1"], "blue")
    bindings = []
    for media_id, path in files.items():
        sha, size = fingerprint(path)
        bindings.append(
            PhoneSourceBinding(
                media_id=media_id,
                proxy_path=f"users/test/analysis-proxy-{media_id}.mp4",
                generation="1",
                original=OriginalMediaDescriptor(
                    sha256=sha,
                    byte_count=size,
                    duration_s=12,
                    width=1080,
                    height=1920,
                    has_audio=True,
                ),
            )
        )
    recipe = compile_phone_guided_plan(plan, tuple(bindings))
    stages["phone_compilation"] = "passed"
    settings.phone_render_verified_features = sorted(recipe.required_capabilities)
    validate_phone_pilot_recipe(recipe)
    verify_phone_recipe(contract, recipe, source_audio={"c0": True, "c1": True})
    stages["phone_validation"] = "passed"
    request = make_device_request(
        job_id=uuid.uuid4(),
        variant_id="creation_words_retimed",
        revision=1,
        recipe=recipe,
    )
    status = DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(
        mode="json"
    )
    (out / "status-creation-words-retimed.json").write_text(
        json.dumps(status, indent=2)
    )
    words = [row for row in after["text_elements"] if "::sequence-" in row["id"]]
    if [row["text"] for row in words] != TITLE.split():
        raise ValueError(
            "captured creation compiler did not preserve the requested words"
        )
    (out / "server-draft-before-retime.json").write_text(json.dumps(before, indent=2))
    (out / "server-draft-after-retime.json").write_text(json.dumps(after, indent=2))
    (out / "editor-draft.json").write_text(json.dumps(editor_fixture(after), indent=2))
    (out / "e2e.json").write_text(
        json.dumps(
            {
                "verified_features": sorted(recipe.required_capabilities),
                "cases": {
                    "creation_words_retimed": {
                        "status_file": "status-creation-words-retimed.json",
                        "duration_s": recipe.duration,
                        "required_capabilities": sorted(recipe.required_capabilities),
                        "drop_capability": "positionedText",
                        "clips": [
                            {"media_id": "c0", "file": "creation-c0.mp4"},
                            {"media_id": "c1", "file": "creation-c1.mp4"},
                        ],
                        "samples": [
                            {
                                "name": "opening",
                                "t": 1,
                                "x": 80,
                                "y": 1800,
                                "rgb": [255, 0, 0],
                            },
                            {
                                "name": "second_clip",
                                "t": 11,
                                "x": 80,
                                "y": 1800,
                                "rgb": [0, 0, 255],
                            },
                        ],
                        "audio_samples": [
                            {
                                "name": "first_clip_original_audio",
                                "t": 1,
                                "speaker_hz": 440,
                                "muted_hz": 660,
                            },
                            {
                                "name": "second_clip_original_audio",
                                "t": 11,
                                "speaker_hz": 440,
                                "muted_hz": 660,
                            },
                        ],
                        "caption_samples": [
                            {
                                "name": "word_fade_start",
                                "t": 1 / 30,
                                "region": [0, 0, 1080, 700],
                                "expect_text": False,
                            },
                            {
                                "name": "word_peak",
                                "t": 0.2,
                                "region": [0, 0, 1080, 700],
                                "expect_text": True,
                            },
                            {
                                "name": "word_fade_exit",
                                "t": 0.4,
                                "region": [0, 0, 1080, 700],
                                "expect_text": False,
                            },
                            {
                                "name": "sequence_finished",
                                "t": 5.2,
                                "region": [0, 0, 1080, 700],
                                "expect_text": False,
                            },
                        ],
                        "expected_words": TITLE.split(),
                        "expected_word_end_s": 5.0,
                        "model_transport_provenance": {
                            "creation": "authored_model_transport_fixture",
                            "retime": retime_provenance,
                        },
                    }
                },
            },
            indent=2,
        )
    )
    source_fixture = (
        REPO / "src/apps/api/tests/fixtures/prompt_coverage/creation_composition.json"
    )
    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    fixture_provenance = json.loads(source_fixture.read_text())["provenance"]
    proof = {
        "schema_version": 1,
        "head_sha": head_sha,
        "fixture_provenance": {
            "creation": "authored_synthetic_live_capture_replayed_offline",
            "retime": retime_provenance,
            "raw_user_media": False,
        },
        "model_prompt_configuration": {
            "creation_model": capture["model"],
            "creation_prompt_version": fixture_provenance["prompt_version"],
            "retime_model": capture["followup"]["model"],
            "retime_prompt_version": capture["followup"]["prompt_version"],
        },
        "evidence_level": "offline_replay_and_production_validation",
        "settled_spend_usd": 0,
        "stages": stages,
        "stage_details": {
            "persistence_reload": "Saved and reopened GuidedStoryExecutionPlan JSON on disk; no database write",
            "guided_validation": "check_guided_plan_text on both creation and retimed plans",
            "phone_validation": "validate_phone_pilot_recipe and verify_phone_recipe",
        },
        "source_fixture_sha256": fingerprint(source_fixture)[0],
        "input_hashes": {
            name: fingerprint(out / name)[0]
            for name in (
                "e2e.json",
                "status-creation-words-retimed.json",
                "editor-draft.json",
                "saved-guided-plan.json",
            )
        },
    }
    (out / "journey-input-proof.json").write_text(json.dumps(proof, indent=2) + "\n")
    print(
        f"Wrote {out}; compiled {len(words)} retimed word layers from captured server draft."
    )


if __name__ == "__main__":
    main()
