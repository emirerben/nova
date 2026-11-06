"""Qendresa item 15: prepare the before/after device cases from REAL stand-in footage.

Not a regression test: a case builder for `scripts/ios/qendresa-card-layout-e2e.py`. Skipped
unless `KRIA_CARD_E2E_OUT` (output dir) and `KRIA_CARD_E2E_FOOT` (footage dir with `take.mp4`,
`words.json`, `faces.json`, `visuals/visuals.json` + files) are set.

It drives the REAL worker (`_run_generative_job` -> `_run_phone_subtitled_job` -> free heuristic
matcher -> grounding -> compiler -> recipe validation) twice on the same footage:

- `q1_before`: `talking_card_layout_enabled` off, `cardLayout` not verified (today's cards);
- `q1_after`: flag on, `cardLayout` verified, protocol 40.

Everything else is identical (item 12 caption shape on with her Q1 captions, video overlays on,
no opening title, no Gemini key). Only transport is replaced: Whisper words (from the footage's
whisper.cpp JSON), probe, DB, the Visuals pool, the Visuals binding (REAL sha256 / byte count so
the device's fingerprint check passes) and face sampling (Apple Vision boxes from `faces.json`).

Each case dir gets `recipe.json`, `assets.json` (manifest asset id -> real file) and `cards.json`
(every overlay-track card plus the worker's overlay receipt).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

import app.services.phone_overlay_grounding as ogm
import app.services.phone_visuals as phone_visuals_mod
from app.config import settings
from app.kria.brief import CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.render_geometry import NormalizedBox, ProtectedRegion, _face_protection_box
from app.pipeline.transcribe import Word
from app.services import phone_text_styling as styling
from app.services.client_protocol import clear_request_context
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding, PhoneVisualBinding
from app.services.phone_text_styling import PHONE_TEXT_STYLE_FIELD, stamp_phone_text_style
from app.tasks import generative_build as gb
from tests.tasks.test_kri841_talking_ending_clip import (
    _offline,  # noqa: F401 - autouse fixture the harness needs
    _recipe,
)
from tests.tasks.test_phone_subtitled_narrated_dispatch import _enable_beats, _setup_subtitled
from tests.tasks.test_qendresa_caption_shape_worker import Q1_CAPTIONS, Q1_PROMPT

OUT = os.environ.get("KRIA_CARD_E2E_OUT")
FOOT = os.environ.get("KRIA_CARD_E2E_FOOT")
CANVAS_RATIO = 1080.0 / 1920.0
PROTOCOL = 40
OVERLAY_TRACK = "subtitled-overlays"
API = Path(__file__).resolve().parents[2]
_NS = uuid.UUID("00000000-0000-4000-8000-000000000115")


def _probe(path: Path) -> dict:
    out = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    video = next(s for s in out["streams"] if s["codec_type"] == "video")
    return {
        "duration": float(out["format"]["duration"]),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": any(s["codec_type"] == "audio" for s in out["streams"]),
    }


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_words(path: Path) -> list[Word]:
    """whisper.cpp `-ml 1 -sow` JSON -> `Word`s: no-space tokens continue the previous word."""
    merged: list[list] = []  # [text, start_s, end_s]
    for token in json.loads(path.read_text())["transcription"]:
        raw = token["text"]
        text = raw.strip()
        if not text or (text.startswith("[") and text.endswith("]")):
            continue
        start, end = token["offsets"]["from"] / 1000, token["offsets"]["to"] / 1000
        if merged and not raw.startswith(" "):
            merged[-1][0] += text
            merged[-1][2] = max(merged[-1][2], end)
        else:
            merged.append([text, start, end])
    return [Word(text, start, max(end, start + 0.01), 1.0) for text, start, end in merged]


@pytest.mark.skipif(not (OUT and FOOT), reason="set KRIA_CARD_E2E_OUT and KRIA_CARD_E2E_FOOT")
def test_prepare_device_cases(monkeypatch):
    foot = Path(FOOT)
    out = Path(OUT)
    take = (foot / "take.mp4").resolve()
    rows_in = json.loads((foot / "visuals/visuals.json").read_text())
    samples = json.loads((foot / "faces.json").read_text())
    info = _probe(take)
    assert (info["width"], info["height"]) == (1080, 1920), info
    words = load_words(foot / "words.json")
    assert words

    # ---- the Visuals pool, with REAL file fingerprints
    files: dict[str, Path] = {}  # pool media id -> real file
    pool: list[dict] = []
    visual_bindings: dict[str, PhoneVisualBinding] = {}
    aspects: dict[str, float] = {}
    keys: dict[str, str] = {}
    for row in rows_in:
        media_id = str(uuid.uuid5(_NS, row["key"]))
        path = (foot / "visuals" / row["file"]).resolve()
        assert path.is_file() and not path.is_symlink(), path
        files[media_id] = path
        keys[media_id] = row["key"]
        aspects[media_id] = row["width"] / row["height"]
        analysis: dict = {
            "subject": row["subject"],
            "on_screen_text": row["on_screen_text"],
            "width": row["width"],
            "height": row["height"],
        }
        if row.get("best_moments"):
            analysis["best_moments"] = row["best_moments"]
        ext = path.suffix.lstrip(".")
        gcs_path = f"users/u1/plan/item1/pool/{media_id}.{ext}"
        pool.append(
            {
                "id": media_id,
                "gcs_path": gcs_path,
                "gcs_generation": "7",
                "kind": row["kind"],
                "source_filename": row["file"],
                "duration_s": row.get("duration_s"),
                "aspect": aspects[media_id],
                "user_context": "",
                "analysis": analysis,
                "capture": None,
            }
        )
        extra: dict = {}
        if row["kind"] == "video":
            vinfo = _probe(path)
            extra = {
                "duration_s": vinfo["duration"],
                "width": vinfo["width"],
                "height": vinfo["height"],
                "orientation_degrees": 0,
            }
        visual_bindings[media_id] = PhoneVisualBinding(
            media_id=media_id,
            gcs_path=gcs_path,
            generation="7",
            sha256=_sha(path),
            byte_count=path.stat().st_size,
            kind=row["kind"],
            **extra,
        )

    def bind(_open, *, job_id, pins):  # noqa: ARG001
        return tuple(visual_bindings[media_id] for media_id in pins)

    # ---- face sampling: nearest Apple Vision sample (searching outwards for a non-null one)
    times = [s["t"] for s in samples]

    def face_at(at: float):
        order = sorted(range(len(samples)), key=lambda i: abs(times[i] - at))
        for i in order:
            if samples[i]["face"] is not None:
                x, y, w, h = samples[i]["face"]
                return _face_protection_box(NormalizedBox(x, y, x + w, y + h))
        return None

    def faces(_clip_path, anchors, **_kwargs):
        regions = []
        for at in anchors:
            box = face_at(at)
            if box is not None:
                regions.append(ProtectedRegion(max(0.0, at - 0.5), at + 0.5, box, kind="face"))
        return regions, {"attempted": len(anchors), "detected": len(regions)}

    real_binding = PhoneSourceBinding(
        media_id="c0",
        proxy_path="user/analysis-proxy-c0.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256=_sha(take),
            byte_count=take.stat().st_size,
            duration_s=info["duration"],
            width=info["width"],
            height=info["height"],
            orientation_degrees=0,
            has_audio=info["has_audio"],
        ),
    )

    def build_job(*, after: bool):
        clear_request_context()
        job, snapshot, _session, _binding = _setup_subtitled(
            monkeypatch, transcript_words=words, duration_s=info["duration"]
        )
        row = [real_binding.model_dump(mode="json")]
        job.assembly_plan[PHONE_SOURCES_FIELD] = row
        snapshot[PHONE_SOURCES_FIELD] = row
        features = ["visualVideos", "authoredText", "captionShape"] + (
            ["cardLayout"] if after else []
        )
        _enable_beats(monkeypatch, extra_features=tuple(features))
        monkeypatch.setattr(gb.settings, "phone_subtitled_video_overlays_enabled", True)
        monkeypatch.setattr(settings, "talking_card_layout_enabled", after)
        monkeypatch.setattr(settings, "phone_caption_shape_enabled", True)
        monkeypatch.setattr(settings, "phone_text_styling_enabled", True)
        monkeypatch.setattr(settings, "phone_subtitled_title_enabled", False)
        monkeypatch.setattr(gb.settings, "subtitled_caption_correction_enabled", False)
        monkeypatch.setattr(phone_visuals_mod, "bind_phone_visual_assets", bind)
        monkeypatch.setattr(ogm, "_load_ready_pool_assets", lambda *a, **k: [dict(r) for r in pool])
        monkeypatch.setattr(ogm, "sample_face_regions", faces)

        def environment(canvas):  # no proxy on disk: captions sit at her asked spot
            styling._POLICY.get()["_face_boxes"] = []

        monkeypatch.setattr(styling, "_ensure_generation_environment", environment)
        job.all_candidates["creator_request"] = Q1_PROMPT
        job.all_candidates["voiceover_caption_style"] = "word"
        job.all_candidates["creator_strategy"] = {
            "edit_format": "subtitled",
            "audio_strategy": "original_audio",
        }
        job.all_candidates["creator_client_protocol"] = PROTOCOL
        brief = CreativeBrief(version=1, requirements=[Q1_CAPTIONS])
        job.assembly_plan["creator_brief_binding"] = BriefBinding.create(
            uuid.uuid4(), brief
        ).model_dump(mode="json")
        stamp_phone_text_style(job)
        job.assembly_plan[PHONE_TEXT_STYLE_FIELD]["selection_calls"] = 2  # no font model call
        return job

    for name, after in (("q1_before", False), ("q1_after", True)):
        job = build_job(after=after)
        gb._run_generative_job(str(job.id))
        assert job.status == "awaiting_device", (name, job.status)
        variant = job.assembly_plan["variants"][0]
        recipe = _recipe(job)
        validate_phone_pilot_recipe(recipe, allow_editor_media=True)  # as the worker does

        directory = out / "cases" / name
        import shutil

        shutil.rmtree(directory, ignore_errors=True)  # a stale movie is never this run's evidence
        directory.mkdir(parents=True)
        (directory / "recipe.json").write_text(recipe.model_dump_json())

        assets: dict[str, str] = {}
        for asset in recipe.asset_manifest.assets:
            if asset.kind == "original":
                assets[asset.id] = str(take)
            elif asset.id.startswith("visual-"):
                assets[asset.id] = str(files[asset.id.removeprefix("visual-")])
        for asset in [*recipe.assets, *recipe.asset_manifest.assets]:
            if asset.id.startswith("font-"):
                font = API / "assets/fonts" / asset.id.removeprefix("font-")
                assert font.is_file() and not font.is_symlink(), font
                assets[asset.id] = str(font)
        missing = [a.id for a in recipe.asset_manifest.assets if a.id not in assets]
        assert not missing, f"assets.json cannot map {missing}"
        (directory / "assets.json").write_text(json.dumps(assets, indent=2))

        track = next((t for t in recipe.tracks if t.id == OVERLAY_TRACK), None)
        cards = []
        for clip in sorted(
            track.clips if track else [], key=lambda c: c.visual_placement.window_start
        ):
            place = clip.visual_placement
            media_id = clip.source_asset_id.removeprefix("visual-")
            width = place.width_fraction
            cards.append(
                {
                    "asset": clip.source_asset_id,
                    "media_key": keys[media_id],
                    "kind": "video"
                    if media_id in visual_bindings and visual_bindings[media_id].kind == "video"
                    else "image",
                    "window_start": place.window_start,
                    "window_end": place.window_end,
                    "x": place.x_fraction,
                    "y": place.y_fraction,
                    "width": width,
                    "height": width * CANVAS_RATIO / aspects[media_id],
                    "source_start": clip.source_start,
                }
            )
        (directory / "cards.json").write_text(
            json.dumps(
                {"cards": cards, "receipt": variant.get("phone_overlay_receipt")},
                indent=2,
                default=str,
            )
        )
        assert cards, f"{name}: no overlay cards"
