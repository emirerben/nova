"""Native preview must resolve originals without mistaking analysis proxies for them."""

import copy
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.routes import generative_jobs as routes
from app.services.phone_editor_sources import EDITOR_SOURCES_FIELD, canonical_source
from app.services.phone_sources import PHONE_SOURCES_FIELD


def admitted_phone_job(monkeypatch, mode="guided"):
    from tests.routes.test_phone_guided_timeline_commit import _guided_phone_job

    job, variant, _ = _guided_phone_job(monkeypatch, count=1, clip_s=3)
    monkeypatch.setattr(routes.settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(routes.settings, "visual_blocks_enabled", True)
    monkeypatch.setattr(
        routes.settings,
        "phone_render_verified_features",
        [
            *routes.settings.phone_render_verified_features,
            "stillImages",
            "visualVideos",
            "visualBlocks",
            "alphaOverlay",
        ],
    )
    binding = job.assembly_plan[PHONE_SOURCES_FIELD][0]
    imports = {}
    sources = []
    for index in (1, 2):
        admitted_binding = {
            **copy.deepcopy(binding),
            "media_id": f"import-{index}",
            "proxy_path": f"users/{job.user_id}/analysis-proxy-import-{index}.mp4",
        }
        admitted_binding["original"]["sha256"] = str(index + 1) * 64
        source = canonical_source(
            {
                "media_id": admitted_binding["media_id"],
                "lane": "clip",
                "gcs_path": admitted_binding["proxy_path"],
                "generation": admitted_binding["generation"],
                "kind": "video",
                "duration_s": admitted_binding["original"]["duration_s"],
            }
        )
        sources.append(
            {**source, "source_index": index, "status": "ready", "source_binding": admitted_binding}
        )
        imports[str(uuid.uuid4())] = {
            "status": "ready",
            "source_id": source["media_id"],
            "source_index": index,
            "source": source,
        }
    variant[EDITOR_SOURCES_FIELD] = {"sources": sources, "imports": imports}
    if mode != "guided":
        variant["resolved_archetype"] = "narrated" if mode == "narrated" else "voiceover"
        job.all_candidates = {"clip_paths": [binding["proxy_path"]]}
        variant["ai_timeline"] = {
            "slots": [{"slot_id": "first", "clip_index": 0, "duration_s": 3, "in_s": 0}]
        }
        if mode == "authored":
            variant["editor_timeline_mode"] = "authored"
        if mode == "empty":
            from app.kria.draft_schemas import DraftSnapshotOut

            variant["editor_state"] = "empty"
            job._saved_editor_drafts = {
                "guided_story": DraftSnapshotOut(
                    draft_id=str(uuid.uuid4()),
                    item_id=str(uuid.uuid4()),
                    variant_key="guided_story",
                    draft_revision=1,
                    snapshot_hash="a" * 64,
                    etag="draft",
                    snapshot={},
                    can_undo=False,
                    created_at=datetime.now(UTC),
                )
            }
    return job, variant


@pytest.mark.parametrize("mode", ["guided", "montage", "narrated", "empty", "authored"])
def test_admission_indexes_resolve_in_real_device_timeline_for_every_editor_mode(monkeypatch, mode):
    """Boundary red control: admission-ready sources must exist in GET /timeline.

    Failures: registry stripped by public projection, missing generated-mode pool,
    lost original receipt, unstable imported indexes, or private registry leakage.
    """
    from app.routes.editor_sources import _response

    job, variant = admitted_phone_job(monkeypatch, mode)
    before = copy.deepcopy(job.assembly_plan)
    result = routes.TimelineResponse(
        **routes.dispatch_get_timeline(job, "guided_story", sign_url=lambda path, ttl: "signed")
    ).model_dump(mode="json")
    assert result["base_generation"] == "first"
    clips = {row["clip_index"]: row for row in result["clips"]}
    assert len(clips) == 3
    for key, record in variant[EDITOR_SOURCES_FIELD]["imports"].items():
        admission = _response(uuid.UUID(key), record, variant)
        clip = clips[admission.source_index]
        assert clip["media_id"] == admission.source.media_id
        assert clip["generation"] == admission.source.generation
        assert clip["native_source"]["media_id"] == admission.source.media_id
        assert clip["native_source"]["local_required"] is True
        assert clip["native_source"]["original"]["sha256"] == str(admission.source_index + 1) * 64
        assert clip["native_source"]["source_url"] is None
        assert clip["used"] is False
    assert EDITOR_SOURCES_FIELD not in str(result)
    assert "source_binding" not in str(result)
    assert job.assembly_plan == before


def test_admitted_guided_clip_save_and_reopen_retains_index_receipt_and_text(monkeypatch):
    job, variant = admitted_phone_job(monkeypatch)
    before_registry = copy.deepcopy(variant[EDITOR_SOURCES_FIELD])
    opened = routes.dispatch_get_timeline(job, "guided_story", sign_url=lambda path, ttl: "signed")
    imported = next(row for row in opened["clips"] if row.get("media_id") == "import-1")
    original_slot = opened["slots"][0]
    routes.prepare_editor_commit(
        job,
        "guided_story",
        routes.EditorCommitRequest(
            base_generation=opened["base_generation"],
            guided_revision_number=opened["revision_number"],
            timeline_slots=[
                routes.TimelineSlotEdit(
                    slot_id=original_slot["slot_id"], clip_index=0, in_s=0, duration_s=3
                ),
                routes.TimelineSlotEdit(
                    slot_id="placement-uuid",
                    clip_index=imported["clip_index"],
                    in_s=0,
                    duration_s=2,
                ),
            ],
        ),
    )
    # JSON save/reopen mirrors the Job JSONB boundary, including its private registry.
    job.assembly_plan = copy.deepcopy(job.assembly_plan)
    reopened = routes.dispatch_get_timeline(
        job, "guided_story", sign_url=lambda path, ttl: "signed"
    )
    assert reopened["slots"][-1]["clip_index"] == imported["clip_index"]
    assert reopened["clips"][imported["clip_index"]]["used"] is True
    assert reopened["clips"][imported["clip_index"]]["native_source"] == imported["native_source"]
    assert job.assembly_plan["variants"][0][EDITOR_SOURCES_FIELD] == before_registry
    assert job.assembly_plan["variants"][0]["text_elements"] == variant["text_elements"]


@pytest.mark.parametrize("privacy", ["masked", "unavailable"])
def test_private_admission_registry_cannot_bypass_public_media_mask(monkeypatch, privacy):
    from app.services.public_assembly_plan import PublicAssemblyPlanProjection

    job, variant = admitted_phone_job(monkeypatch)
    public_variant = {key: value for key, value in variant.items() if key != EDITOR_SOURCES_FIELD}
    public_variant["candidate_snapshot"] = {"clip_paths": []}
    monkeypatch.setattr(
        routes,
        "project_public_assembly_plan_with_metadata",
        lambda plan: PublicAssemblyPlanProjection(
            value={"variants": [public_variant]},
            masked_last_good_variant_ids=frozenset({"guided_story"})
            if privacy == "masked"
            else frozenset(),
            media_unavailable_variant_ids=frozenset({"guided_story"})
            if privacy == "unavailable"
            else frozenset(),
            active_speech_projection=False,
        ),
    )
    result = routes.dispatch_get_timeline(job, "guided_story", sign_url=lambda path, ttl: "signed")
    assert not any(row.get("media_id", "").startswith("import-") for row in result["clips"])
    assert EDITOR_SOURCES_FIELD not in str(result)


@pytest.mark.parametrize("mode", ["guided", "montage"])
def test_missing_admitted_original_receipt_never_downloads_analysis_proxy(monkeypatch, mode):
    job, variant = admitted_phone_job(monkeypatch, mode)
    variant[EDITOR_SOURCES_FIELD]["sources"][0].pop("source_binding")
    result = routes.dispatch_get_timeline(job, "guided_story", sign_url=lambda path, ttl: "signed")
    assert result["clips"][1]["native_source"] is None
    assert result["clips"][2]["native_source"]["media_id"] == "import-2"
    assert result["total_duration_s"] == 3


def test_cloud_original_uses_owned_timeline_source(monkeypatch):
    monkeypatch.setattr(routes, "signed_get_url", lambda path, ttl: f"https://storage.test/{path}")
    source = routes._native_timeline_source(
        SimpleNamespace(assembly_plan={}), "original.mov", "clip-0"
    )
    assert source == {
        "media_id": "clip-0",
        "source_url": "https://storage.test/original.mov",
        "local_required": False,
    }


def test_analysis_proxy_returns_only_verified_local_original_binding(monkeypatch):
    def forbidden_sign(*args):
        raise AssertionError("A native preview must never download an analysis proxy")

    monkeypatch.setattr(routes, "signed_get_url", forbidden_sign)
    path = "owner/analysis-proxy-123.mp4"
    original = {
        "sha256": "a" * 64,
        "byte_count": 1234,
        "duration_s": 2,
        "width": 1080,
        "height": 1920,
        "orientation_degrees": 0,
        "has_audio": True,
    }
    row = {
        "media_id": "analysis-proxy-123",
        "proxy_path": path,
        "generation": "1",
        "original": original,
    }
    job = SimpleNamespace(assembly_plan={PHONE_SOURCES_FIELD: [row]})
    source = routes._native_timeline_source(job, path, "clip-0")
    assert source == {
        "media_id": "analysis-proxy-123",
        # KRI-93 added `kind` to OriginalMediaDescriptor.model_dump()'s output;
        # `original` (the raw row input above) predates that field and defaults
        # it to "video" on validation, so the response carries it explicitly.
        "original": {**original, "kind": "video"},
        "local_required": True,
    }
    assert "source_url" not in source
    for records in [[], [row, row], [{**row, "original": {}}]]:
        job.assembly_plan[PHONE_SOURCES_FIELD] = records
        assert routes._native_timeline_source(job, path, "clip-0") is None


def test_native_effect_sources_are_bound_to_the_owned_variant(monkeypatch):
    monkeypatch.setattr(routes, "signed_get_url", lambda path, ttl: f"https://storage.test/{path}")
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "one",
                    "sound_effects": [
                        {"id": "first", "src_gcs_path": "sound-effects/pop.wav"},
                        {"id": "second", "src_gcs_path": "sound-effects/pop.wav"},
                        {"id": "invalid", "src_gcs_path": "private/not-a-sound.wav"},
                    ],
                },
                {
                    "variant_id": "two",
                    "sound_effects": [
                        {"id": "other", "src_gcs_path": "sound-effects/other.wav"},
                    ],
                },
            ]
        }
    )
    sources = routes._native_editor_assets(job, "one")
    assert [source["id"] for source in sources] == ["first", "second"]
    assert sources[0]["media_id"] == sources[1]["media_id"]
    assert all(source["kind"] == "sound_effect" for source in sources)
    assert routes._native_editor_assets(job, "missing") == []


def test_motion_resources_are_signed_from_the_owned_variant(monkeypatch):
    monkeypatch.setattr(routes, "signed_get_url", lambda path, ttl: f"https://storage.test/{path}")
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "motion",
                    "motion_scenes": [
                        {
                            "params": {
                                "assets": [
                                    {"asset_id": "image-a", "gcs_path": "users/owner/image.png"},
                                    {"asset_id": "bad", "gcs_path": "private/key"},
                                ]
                            }
                        }
                    ],
                }
            ]
        }
    )
    assets = routes._native_editor_assets(job, "motion")
    assert [asset["id"] for asset in assets] == ["image-a"]
    assert assets[0]["kind"] == "motion_scene"
    assert routes._native_editor_assets(job, "other") == []


def test_visual_sources_are_scoped_to_the_owned_variant_and_valid_paths(monkeypatch):
    monkeypatch.setattr(routes, "signed_get_url", lambda path, ttl: f"https://storage.test/{path}")
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "visual",
                    "visual_blocks": [
                        {
                            "id": "montage",
                            "kind": "montage",
                            "shots": [
                                {"id": "one", "src_gcs_path": "users/owner/one.png"},
                                {"id": "bad", "src_gcs_path": "private/key"},
                            ],
                        },
                        {
                            "id": "card",
                            "kind": "text_card",
                            "background": {
                                "type": "asset",
                                "shot": {
                                    "id": "background",
                                    "src_gcs_path": "users/owner/two.png",
                                },
                            },
                        },
                        {"id": "media", "kind": "media", "src_gcs_path": "users/owner/three.png"},
                    ],
                }
            ]
        }
    )
    assets = routes._native_editor_assets(job, "visual")
    assert [asset["id"] for asset in assets] == ["montage:one", "card:background", "media:media"]
    assert all(asset["kind"] == "visual_block" for asset in assets)
    assert routes._native_editor_assets(job, "other") == []


def test_guided_image_native_source_survives_response_without_using_browser_derivative(monkeypatch):
    revision = {
        "sources": [
            {
                "media_id": "photo",
                "gcs_path": "users/owner/original.heic",
                "kind": "image",
                "generation": "7",
            }
        ],
        "segments": [
            {
                "segment_id": "shot",
                "media_id": "photo",
                "source_start_s": 0,
                "duration_s": 2,
                "output_start_s": 0,
                "output_end_s": 2,
            }
        ],
        "revision_number": 1,
        "state_hash": "hash",
    }
    monkeypatch.setattr(routes, "_guided_v2_revision", lambda *args: revision)
    monkeypatch.setattr(routes, "signed_get_url", lambda path, ttl: f"https://storage.test/{path}")
    job = SimpleNamespace(assembly_plan={})
    timeline = routes._guided_v2_timeline_projection(
        job,
        {"render_generation_id": "generation"},
        image_preview_paths={"users/owner/original.heic": "preview/browser.jpg"},
    )
    public = routes.project_public_assembly_plan(timeline)
    response = routes.TimelineResponse(**public)
    assert response.clips[0].signed_url == "https://storage.test/preview/browser.jpg"
    assert (
        response.clips[0].native_source.source_url
        == "https://storage.test/users/owner/original.heic"
    )
    assert response.clips[0].native_source.media_id == "photo"
    assert response.base_generation == "generation"
    assert response.slots[0].clip_index == 0


def test_native_overlay_alpha_and_invalid_paths(monkeypatch):
    monkeypatch.setattr(routes, "signed_get_url", lambda path, ttl: f"https://storage.test/{path}")
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "one",
                    "media_overlays": [
                        {"id": "image", "kind": "image", "src_gcs_path": "users/owner/photo.png"},
                        {"id": "video", "kind": "video", "src_gcs_path": "users/owner/video.mp4"},
                        {"id": "invalid", "kind": "image", "src_gcs_path": "private/photo.png"},
                    ],
                }
            ]
        }
    )
    for enabled in (False, True):
        monkeypatch.setattr(routes.settings, "media_overlay_alpha_enabled", enabled)
        assets = routes._native_editor_assets(job, "one")
        assert [asset["id"] for asset in assets] == ["image", "video"]
        assert [asset["preserve_alpha"] for asset in assets] == [enabled, False]


def test_native_signing_failure_is_local_to_the_unavailable_resource(monkeypatch):
    def sign(path, ttl):
        if "missing" in path:
            raise ValueError("unavailable")
        return f"https://storage.test/{path}"

    monkeypatch.setattr(routes, "signed_get_url", sign)
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "one",
                    "sound_effects": [
                        {"id": "missing", "src_gcs_path": "sound-effects/missing.wav"},
                        {"id": "ready", "src_gcs_path": "sound-effects/ready.wav"},
                    ],
                }
            ]
        }
    )
    assert routes._native_timeline_source(job, "missing.mp4", "clip-0") is None
    assert [asset["id"] for asset in routes._native_editor_assets(job, "one")] == ["ready"]


def test_malformed_persisted_asset_collections_do_not_break_timeline(monkeypatch):
    monkeypatch.setattr(routes, "signed_get_url", lambda path, ttl: f"https://storage.test/{path}")
    for key in ("sound_effects", "media_overlays", "motion_scenes", "visual_blocks"):
        for malformed in (True, 1, "invalid", {}):
            job = SimpleNamespace(
                assembly_plan={"variants": [{"variant_id": "one", key: malformed}]}
            )
            assert routes._native_editor_assets(job, "one") == []
    job = SimpleNamespace(
        assembly_plan={
            "variants": [
                {
                    "variant_id": "one",
                    "motion_scenes": [{"params": {"assets": True}}],
                    "visual_blocks": [{"id": "montage", "kind": "montage", "shots": 1}],
                }
            ]
        }
    )
    assert routes._native_editor_assets(job, "one") == []


def test_source_signatures_are_reused_only_within_one_request(monkeypatch):
    calls = []

    def sign(path, ttl):
        calls.append((path, ttl))
        return f"https://storage.test/{path}?signature={len(calls)}"

    monkeypatch.setattr(routes, "signed_get_url", sign)
    signer = routes._timeline_url_signer()
    job = SimpleNamespace(assembly_plan={})
    browser_url = signer("source.mp4", routes.PLAYBACK_URL_TTL_MIN)
    for index in range(100):
        assert (
            routes._native_timeline_source(job, "source.mp4", str(index), sign_url=signer)[
                "source_url"
            ]
            == browser_url
        )
    assert len(calls) == 1
    next_request = routes._timeline_url_signer()
    assert next_request("source.mp4", routes.PLAYBACK_URL_TTL_MIN) != browser_url
    assert len(calls) == 2


def test_malformed_native_paths_and_phone_bindings_are_unavailable(monkeypatch):
    def forbidden_sign(*args):
        raise AssertionError("Invalid sources must never be signed")

    monkeypatch.setattr(routes, "signed_get_url", forbidden_sign)
    job = SimpleNamespace(assembly_plan={})
    for path in (None, 1, True, [], {}, "", "   "):
        assert routes._native_timeline_source(job, path, "clip-0") is None
    for bindings in (1, True, "invalid", {"proxy_path": "owner/analysis-proxy-123.mp4"}):
        job.assembly_plan = {PHONE_SOURCES_FIELD: bindings}
        assert routes._native_timeline_source(job, "owner/analysis-proxy-123.mp4", "clip-0") is None
