"""Safety and determinism tests for the local production-item clone tool."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[5] / "scripts" / "dev" / "clone_plan_item.py"


def _module():
    spec = importlib.util.spec_from_file_location("clone_plan_item", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_path_map_is_stable_and_media_id_based() -> None:
    module = _module()
    image_id = "11111111-1111-4111-8111-111111111111"
    video_id = "22222222-2222-4222-8222-222222222222"
    sources = [
        {"kind": "image", "media_id": image_id, "gcs_path": "users/u/a/IMG_1.HEIC"},
        {"kind": "video", "media_id": video_id, "gcs_path": "users/u/b/clip.mp4"},
    ]

    mapping = module.build_path_map(sources, job_id="job")

    assert mapping == {
        "users/u/a/IMG_1.HEIC": f"dev-qa/production-clones/job/sources/{image_id}.heic",
        "users/u/b/clip.mp4": f"dev-qa/production-clones/job/sources/{video_id}.mp4",
    }
    assert module.build_path_map(sources, job_id="job") == mapping


def test_path_map_rejects_duplicate_paths_and_unknown_kinds() -> None:
    module = _module()
    with pytest.raises(RuntimeError, match="duplicate"):
        module.build_path_map(
            [
                {
                    "kind": "image",
                    "media_id": "11111111-1111-4111-8111-111111111111",
                    "gcs_path": "users/u/a.mp4",
                },
                {
                    "kind": "video",
                    "media_id": "22222222-2222-4222-8222-222222222222",
                    "gcs_path": "users/u/a.mp4",
                },
            ]
        )
    with pytest.raises(RuntimeError, match="unsupported source kind"):
        module.build_path_map(
            [
                {
                    "kind": "audio",
                    "media_id": "11111111-1111-4111-8111-111111111111",
                    "gcs_path": "users/u/a",
                }
            ]
        )


def test_path_map_rejects_non_uuid_media_id() -> None:
    module = _module()
    with pytest.raises(RuntimeError, match="unsafe media_id"):
        module.build_path_map(
            [
                {
                    "kind": "image",
                    "media_id": "../../escape",
                    "gcs_path": "users/u/a.jpg",
                }
            ]
        )


def test_rewrite_paths_deep_copies_json() -> None:
    module = _module()
    value = {"nested": [{"gcs_path": "old/path.mp4"}], "url": "https://prod/video"}
    rewritten = module.rewrite_paths(value, {"old/path.mp4": "dev-qa/local.mp4"})

    assert rewritten == {"nested": [{"gcs_path": "dev-qa/local.mp4"}], "url": "https://prod/video"}
    assert value["nested"][0]["gcs_path"] == "old/path.mp4"


def test_strip_signed_storage_urls_removes_bearer_urls_only() -> None:
    module = _module()
    value = {
        "output_url": ("https://storage.googleapis.com/bucket/video.mp4?X-Goog-Signature=secret"),
        "product_url": "https://example.com/product",
    }

    assert module.strip_signed_storage_urls(value) == {
        "output_url": None,
        "product_url": "https://example.com/product",
    }


def test_rewrite_generations_follows_local_object_identity() -> None:
    module = _module()
    value = {
        "sources": [
            {"gcs_path": "old/path.mp4", "generation": "prod-generation"},
            {"gcs_path": "other/path.mp4", "generation": "untouched"},
        ]
    }
    assert module.rewrite_generations(value, {"old/path.mp4": "123456"}) == {
        "sources": [
            {"gcs_path": "old/path.mp4", "generation": "123456"},
            {"gcs_path": "other/path.mp4", "generation": "untouched"},
        ]
    }


def test_recompute_clone_integrity_updates_snapshot_revision_and_receipt() -> None:
    from app.schemas.edit_proposal import (
        EditProposalSnapshot,
        MediaRef,
        StoryBeat,
        canonical_media_digest,
    )
    from app.schemas.guided_edit_revision import guided_editor_state_hash

    module = _module()
    media_refs = [
        MediaRef(
            lane="clip",
            media_id="video-1",
            gcs_path="dev-qa/video-1.mp4",
            generation="local-generation",
            kind="video",
            duration_s=3.0,
        )
    ]
    snapshot = EditProposalSnapshot(
        duration_s=3,
        title="Local clone",
        media=media_refs,
        story_beats=[
            StoryBeat(
                beat_id="beat-1",
                topic="Local",
                thought="Use the mirrored source.",
                media_ids=["video-1"],
                duration_s=3,
            )
        ],
    )
    media = [row.model_dump(mode="json") for row in media_refs]
    revision_sources = [
        {
            key: row[key]
            for key in (
                "media_id",
                "lane",
                "gcs_path",
                "generation",
                "kind",
                "duration_s",
            )
        }
        for row in media
    ]
    expected_digest = canonical_media_digest(media_refs)
    old_digest = "a" * 64
    old_hash = "b" * 64
    assembly = {
        "guided_edit": {
            "proposal_version": 1,
            "media_digest": old_digest,
            "approved_proposal": snapshot.model_dump(mode="json"),
            "media_identities": media,
        },
        "guided_story_execution_plan": {"media_digest": old_digest},
        "variants": [
            {
                "variant_id": "guided_story",
                "media_digest": old_digest,
                "render_receipt": {
                    "media_digest": old_digest,
                    "revision_hash": old_hash,
                },
                "guided_edit_revision": {
                    "schema_version": 1,
                    "approval_proposal_version": 1,
                    "approval_media_digest": old_digest,
                    "revision_number": 1,
                    "state_hash": old_hash,
                    "sources": revision_sources,
                    "segments": [
                        {
                            "segment_id": "segment-1",
                            "media_id": "video-1",
                            "source_start_s": 0.0,
                            "source_end_s": 3.0,
                            "duration_s": 3.0,
                            "output_start_s": 0.0,
                            "output_end_s": 3.0,
                        }
                    ],
                },
            }
        ],
    }

    repaired = module.recompute_clone_integrity(assembly)
    variant = repaired["variants"][0]
    revision = variant["guided_edit_revision"]

    assert repaired["guided_edit"]["media_digest"] == expected_digest
    assert repaired["guided_story_execution_plan"]["media_digest"] == expected_digest
    assert variant["media_digest"] == expected_digest
    assert variant["render_receipt"]["media_digest"] == expected_digest
    assert revision["approval_media_digest"] == expected_digest
    assert revision["state_hash"] == guided_editor_state_hash(revision)
    assert variant["render_receipt"]["revision_hash"] == revision["state_hash"]


def test_prod_access_requires_explicit_opt_in() -> None:
    module = _module()
    with pytest.raises(RuntimeError, match="--allow-prod-read"):
        module._require_prod_read(argparse.Namespace(allow_prod_read=False))


def test_prod_get_refuses_to_send_token_to_another_origin() -> None:
    module = _module()
    with pytest.raises(ValueError, match="Nova production origin"):
        module._prod_get(
            "/admin/jobs/job",
            token="secret",
            base_url="https://attacker.example",
        )


def test_asset_path_scan_ignores_signed_urls() -> None:
    module = _module()
    assembly = {
        "variants": [
            {
                "video_path": "generative-jobs/job/video.mp4",
                "output_url": "https://storage.googleapis.com/bucket/video.mp4?signature=secret",
                "base_video_path": "generative-jobs/job/base.mp4",
            }
        ]
    }

    assert module._all_asset_paths(assembly) == {
        "generative-jobs/job/video.mp4",
        "generative-jobs/job/base.mp4",
    }


def test_mirror_media_merges_source_and_variant_asset_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from google.cloud import storage as gcs

    module = _module()
    mirrored: list[tuple[str, Path]] = []

    class FakeClient:
        pass

    monkeypatch.setattr(
        module,
        "_mirror_object",
        lambda _c, _b, path, destination: mirrored.append((path, destination)) or "1",
    )
    monkeypatch.setattr(module, "_make_image_preview", lambda *_args: None)
    monkeypatch.setattr(gcs, "Client", lambda **_kwargs: FakeClient())

    module.mirror_media(
        sources=[
            {
                "kind": "video",
                "media_id": "vid-1",
                "gcs_path": "users/u/source.mp4",
            }
        ],
        assembly={"variants": [{"video_path": "generative-jobs/job/output.mp4"}]},
        path_map={"users/u/source.mp4": "dev-qa/production-clones/job/sources/vid-1.mp4"},
        media_dir=tmp_path,
        source_bucket="source-bucket",
        job_id="job",
    )

    assert mirrored == [
        (
            "generative-jobs/job/output.mp4",
            tmp_path
            / "dev-qa/production-clones/job/assets"
            / (f"{module.hashlib.sha256(b'generative-jobs/job/output.mp4').hexdigest()[:24]}.mp4"),
        ),
        (
            "users/u/source.mp4",
            tmp_path / "dev-qa/production-clones/job/sources/vid-1.mp4",
        ),
    ]
