"""KRI-1340 (Çiğdem S3 Yorum1): takes B and C were attached through Visuals. Once the server
adopts them as takes (`app.tasks.visual_talking_takes`), the phone Talking job must render them
exactly like footage takes, from the Visuals pool.

Failure modes (written before the code, `docs/reviews/kri-1340-visual-talking-takes.md`):

V1  The phone looks for a camera-roll original it never bound for B / C ("Find your originals")
    instead of downloading the Visuals bytes: every take adopted from Visuals must be a pinned
    `visual` asset (pool generation + SHA), never an `original`.
V2  An adopted take plays out of order, twice, or not at all.
V3  A photo named in an adopted take ("café", take C) is placed in take A's time or dropped.
V4  The export check demands an approved original for an adopted take.
V5  Footage takes change at all (take A stays an `original`, byte-identical binding JSON).

The real phone Talking job + recipe compiler, with the S3 render's own word timings
(`test_s3_three_takes_fullscreen_photos`); only storage, ingest and Whisper are stubbed.
"""

from __future__ import annotations

import pytest

from app.kria.render_assets import OriginalRenderAsset, VisualRenderAsset
from app.services.device_render import approved_export_sources, device_status
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding
from tests.tasks import test_s3_three_takes_fullscreen_photos as s3
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_talking_multi

POOL = {
    "takeB": ("f7df51e4-f9c7-4d37-bf56-a815e10277ac", "1762638262708063"),  # IMG_7245
    "takeC": ("fc73d969-9e69-4b00-b8e8-d3356c2ad13d", "1762638262185412"),  # IMG_7252
}


def _setup_with_visual_takes(monkeypatch, **kwargs):
    job, snapshot, bindings = _setup_talking_multi(monkeypatch, **kwargs)
    rows = []
    for row in snapshot[PHONE_SOURCES_FIELD]:
        if row["media_id"] in POOL:
            asset_id, generation = POOL[row["media_id"]]
            row = {
                **row,
                "pool_source": {
                    "asset_id": asset_id,
                    "gcs_path": f"users/u/plan/i/pool/{asset_id}.mov",
                    "generation": generation,
                },
            }
        rows.append(row)
    snapshot[PHONE_SOURCES_FIELD] = rows
    job.assembly_plan[PHONE_SOURCES_FIELD] = rows
    return job, snapshot, bindings


@pytest.fixture
def rendered(monkeypatch):
    monkeypatch.setattr(s3, "_setup_talking_multi", _setup_with_visual_takes)
    recipe, variant = s3._render(monkeypatch, enabled=True)
    return recipe, variant


def test_v1_adopted_takes_are_pinned_visuals_and_footage_stays_an_original(rendered):
    recipe, _variant = rendered
    assets = {asset.id: asset for asset in recipe.asset_manifest.assets}

    kinds = {asset_id: type(asset) for asset_id, asset in assets.items()}
    assert kinds["takeA"] is OriginalRenderAsset
    for media_id, (asset_id, generation) in POOL.items():
        asset = assets[media_id]
        assert isinstance(asset, VisualRenderAsset), (media_id, kinds)
        assert asset.visual_id == asset_id
        assert asset.generation == generation
        assert asset.media_kind == "video"
        assert asset.fingerprint.sha256 == "a" * 64


def test_v2_the_three_takes_play_once_each_in_order(rendered):
    recipe, _variant = rendered
    main = next(t for t in recipe.tracks if t.id == "subtitled")
    played = []
    for clip in sorted(main.clips, key=lambda c: c.timeline_start):
        if clip.source_asset_id not in played:
            played.append(clip.source_asset_id)
    assert played == ["takeA", "takeB", "takeC"]
    starts = {
        c.source_asset_id: float(c.timeline_start) for c in main.clips if c.id.startswith("speaker")
    }
    assert starts["takeB"] == pytest.approx(20.1, abs=0.05)
    assert starts["takeC"] == pytest.approx(20.1 + 6.967, abs=0.05)


def test_v3_the_cafe_photo_lands_on_its_word_inside_take_c(rendered):
    recipe, _variant = rendered
    shown = s3._fullscreen(recipe)

    def at(media_id):
        hits = [t for key, t in shown.items() if media_id in key]
        assert hits, f"{media_id} not shown; shown={shown}"
        return hits[0]

    assert at(s3.CAFE) == pytest.approx(27.067 + 2.6, abs=0.6)
    assert at(s3.DONUT) == pytest.approx(20.1 + 5.8, abs=0.6)
    assert at(s3.NAR) == pytest.approx(11.5, abs=0.6)
    assert not any(s3.CALENDAR in key for key in shown)


def test_v4_export_needs_only_the_footage_original(monkeypatch):
    jobs = []

    def _keep(monkeypatch_, **kwargs):
        out = _setup_with_visual_takes(monkeypatch_, **kwargs)
        jobs.append(out[0])
        return out

    monkeypatch.setattr(s3, "_setup_talking_multi", _keep)
    s3._render(monkeypatch, enabled=True)
    job = jobs[0]
    request = device_status(job, "subtitled").request

    sources = approved_export_sources(job, request)

    originals = {
        a.media_id
        for a in request.recipe.asset_manifest.assets
        if isinstance(a, OriginalRenderAsset)
    }
    assert originals == {"takeA"}
    assert set(sources) >= originals


def test_v5_a_footage_binding_serializes_exactly_as_before():
    binding = PhoneSourceBinding.model_validate(
        {
            "media_id": "takeA",
            "proxy_path": "u/analysis-proxy-takeA.mp4",
            "generation": "1",
            "original": {
                "sha256": "a" * 64,
                "byte_count": 1000,
                "duration_s": 20.1,
                "width": 1080,
                "height": 1920,
                "has_audio": True,
            },
        }
    )
    assert "pool_source" not in binding.model_dump(mode="json")
    assert isinstance(binding.render_asset(), OriginalRenderAsset)


def test_v6_the_phone_editor_streams_an_adopted_take_and_keeps_footage_local(monkeypatch):
    """The editor's source pool (`_native_timeline_source`): a footage take still needs this
    iPhone's original; a take adopted from Visuals plays its pinned pool bytes."""
    from app import storage
    from app.routes import generative_jobs

    jobs = []

    def _keep(monkeypatch_, **kwargs):
        out = _setup_with_visual_takes(monkeypatch_, **kwargs)
        jobs.append(out[0])
        return out

    monkeypatch.setattr(s3, "_setup_talking_multi", _keep)
    s3._render(monkeypatch, enabled=True)
    job = jobs[0]
    signed = []
    monkeypatch.setattr(
        storage,
        "signed_get_url_for_generation",
        lambda path, *, generation, **_k: (
            signed.append((path, generation)) or f"https://signed.test/{path}?g={generation}"
        ),
    )
    rows = {row["media_id"]: row for row in job.assembly_plan[PHONE_SOURCES_FIELD]}

    footage = generative_jobs._native_timeline_source(job, rows["takeA"]["proxy_path"], "clip-0")
    take_c = generative_jobs._native_timeline_source(job, rows["takeC"]["proxy_path"], "clip-2")

    assert footage["local_required"] is True and "source_url" not in footage
    asset_id, generation = POOL["takeC"]
    assert take_c == {
        "media_id": "takeC",
        "source_url": f"https://signed.test/users/u/plan/i/pool/{asset_id}.mov?g={generation}",
        "local_required": False,
    }
    assert signed == [(f"users/u/plan/i/pool/{asset_id}.mov", generation)]
