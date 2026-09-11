"""Native preview must resolve originals without mistaking analysis proxies for them."""

from types import SimpleNamespace

from app.routes import generative_jobs as routes
from app.services.phone_sources import PHONE_SOURCES_FIELD


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
        "original": original,
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
