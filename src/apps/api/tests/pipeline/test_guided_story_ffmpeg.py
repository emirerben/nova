from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pillow_heif
import pytest
from PIL import Image, ImageDraw

from app.pipeline.canvas import Canvas
from app.pipeline.guided_story import (
    _render_moments,
    compile_execution_plan,
    render_execution_plan,
)
from app.pipeline.probe import probe_video
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    FastMontageCut,
    MediaRef,
    MixedMediaTimingProfile,
    StoryBeat,
    canonical_media_digest,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def test_concat_fallback_keeps_explicit_landscape_canvas(tmp_path: Path) -> None:
    from app.tasks.template_orchestrate import _concat_demuxer

    slots = [tmp_path / "slot_a.mp4", tmp_path / "slot_b.mp4"]
    for slot in slots:
        slot.write_bytes(b"x")
    failed_copy = MagicMock(returncode=1, stderr=b"copy failed")
    successful_encode = MagicMock(returncode=0, stderr=b"")

    with patch(
        "app.tasks.template_orchestrate.subprocess.run",
        side_effect=[failed_copy, successful_encode],
    ) as mock_run:
        _concat_demuxer(
            [str(slot) for slot in slots],
            str(tmp_path / "out.mp4"),
            str(tmp_path),
            expected_duration_s=4.0,
            canvas=Canvas(1920, 1080),
        )

    assert mock_run.call_count == 2
    fallback = mock_run.call_args_list[-1][0][0]
    assert fallback[fallback.index("-s") + 1] == "1920x1080"


def test_real_ffmpeg_guided_v2_renders_image_and_video_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the story-native moment renderers with persisted look state."""
    from app.pipeline import guided_story

    image = tmp_path / "image.jpg"
    video = tmp_path / "video.mp4"
    _image(image, (190, 105, 55), "IMAGE LOOK")
    _video(video, size="360x640")

    canvas = Canvas(320, 180)
    monkeypatch.setattr(guided_story, "LANDSCAPE", canvas)
    plan = {
        "output_orientation": "landscape",
        "story_timeline": [
            {
                "moment_id": "segment-image",
                "beat_id": "beat-image",
                "media_id": "image",
                "generation": "image-generation",
                "kind": "image",
                "duration_s": 1.0,
                "layout": "fullscreen",
                "image_motion": None,
                "look_preset": "olive_film",
                "look_adjustments": {
                    "intensity": 0.7,
                    "warmth": 0.3,
                    "contrast": -0.2,
                    "grain": 0.1,
                    "vignette": 0.15,
                },
            },
            {
                "moment_id": "segment-video",
                "beat_id": "beat-video",
                "media_id": "video",
                "generation": "video-generation",
                "kind": "video",
                "source_start_s": 0.0,
                "source_end_s": 1.0,
                "duration_s": 1.0,
                "layout": "fullscreen",
                "image_motion": None,
                "look_preset": "smoky_split_tone",
                "look_adjustments": {
                    "intensity": 0.6,
                    "warmth": -0.2,
                    "contrast": 0.25,
                    "grain": 0.2,
                    "vignette": 0.3,
                },
            },
        ],
    }

    outputs, receipts = _render_moments(
        plan,
        {"image": str(image), "video": str(video)},
        str(tmp_path),
    )

    assert len(outputs) == 2
    assert [receipt["media_id"] for receipt in receipts] == ["image", "video"]
    assert all(receipt["codec"] == "h264" for receipt in receipts)
    for output in outputs:
        probe = probe_video(output)
        assert (probe.width, probe.height, probe.codec) == (320, 180, "h264")
        assert probe.duration_s == pytest.approx(1.0, abs=0.15)


def test_real_ffmpeg_many_quick_photos_keep_exact_frame_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import guided_story

    raw = _snapshot()
    snapshot = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    snapshot.direction = "fast_montage"
    snapshot.pace = "fast"
    snapshot.duration_s = 15
    snapshot.mixed_media_timing = MixedMediaTimingProfile(
        image_hold="very_fast",
        video_hold="longer",
        boundary_style="cut",
    )
    snapshot.output_orientation = "portrait"
    photo_ids = ["food", "town", "food-detail", "dessert", "street"] + [
        f"photo-extra-{index}" for index in range(15)
    ]
    snapshot.media.extend(
        MediaRef(
            lane="asset",
            media_id=media_id,
            gcs_path=f"users/test/{media_id}.jpg",
            generation=f"extra-{index}",
            kind="image",
            aspect=1.7778,
        )
        for index, media_id in enumerate(photo_ids[5:])
    )
    snapshot.fast_cuts = [
        FastMontageCut(
            cut_id=f"photo-{index}",
            media_id=media_id,
            source_start_s=0.0,
            source_end_s=0.65,
            output_duration_s=0.65,
            role="hook" if index == 0 else "build",
        )
        for index, media_id in enumerate(photo_ids)
    ] + [
        FastMontageCut(
            cut_id="video-payoff",
            media_id="coast",
            source_start_s=0.0,
            source_end_s=2.0,
            output_duration_s=2.0,
            role="payoff",
        )
    ]
    raw["approved_proposal"] = snapshot.model_dump(mode="json")
    raw["media_digest"] = canonical_media_digest(snapshot.media)
    raw["media_identities"] = [
        {
            "lane": ref.lane,
            "media_id": ref.media_id,
            "gcs_path": ref.gcs_path,
            "generation": ref.generation,
            "kind": ref.kind,
        }
        for ref in snapshot.media
    ]
    plan = compile_execution_plan(raw, track=None)

    image = tmp_path / "photo.jpg"
    video = tmp_path / "video.mp4"
    _image(image, (220, 120, 40), "QUICK PHOTO")
    _video(video, size="640x360")
    canvas = Canvas(180, 320)
    monkeypatch.setattr(guided_story, "PORTRAIT", canvas)

    outputs, receipts = _render_moments(
        plan,
        {**{media_id: str(image) for media_id in photo_ids}, "coast": str(video)},
        str(tmp_path),
    )

    assert len(outputs) == 21
    assert sum(receipt["output_duration_s"] for receipt in receipts) == pytest.approx(
        15.0, abs=0.01
    )
    assert all(
        0.5 <= receipt["output_duration_s"] <= 0.8
        for receipt in receipts
        if receipt["kind"] == "image"
    )


def _image(path: Path, color: tuple[int, int, int], label: str) -> None:
    image = Image.new("RGB", (640, 360), color)
    ImageDraw.Draw(image).text((40, 40), label, fill="white")
    if path.suffix.lower() in {".heic", ".heif"}:
        pillow_heif.register_heif_opener()
        try:
            image.save(path, format="HEIF")
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"local pillow-heif cannot encode HEIF: {exc}")
    else:
        image.save(path)


def _transparent_image(path: Path) -> None:
    image = Image.new("RGBA", (640, 360), (50, 180, 120, 255))
    ImageDraw.Draw(image).rectangle((0, 0, 120, 120), fill=(50, 180, 120, 0))
    image.save(path, format="WEBP", lossless=True)


def _video(path: Path, *, size: str = "360x640", duration_s: float = 5.5) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            str(duration_s),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


def _snapshot(
    *,
    analyzed_aspect: float | None = None,
    creator_pinned_portrait: bool = False,
) -> dict:
    media = [
        MediaRef(
            lane="asset",
            media_id="food",
            gcs_path="users/test/food.heic",
            generation="1",
            kind="image",
            aspect=analyzed_aspect,
        ),
        MediaRef(
            lane="asset",
            media_id="town",
            gcs_path="users/test/town.jpg",
            generation="2",
            kind="image",
            aspect=analyzed_aspect,
        ),
        MediaRef(
            lane="asset",
            media_id="food-detail",
            gcs_path="users/test/food-detail.webp",
            generation="4",
            kind="image",
            aspect=analyzed_aspect,
        ),
        MediaRef(
            lane="asset",
            media_id="dessert",
            gcs_path="users/test/dessert.jpg",
            generation="5",
            kind="image",
            aspect=analyzed_aspect,
        ),
        MediaRef(
            lane="asset",
            media_id="street",
            gcs_path="users/test/street.jpg",
            generation="6",
            kind="image",
            aspect=analyzed_aspect,
        ),
        MediaRef(
            lane="clip",
            media_id="coast",
            gcs_path="users/test/coast.mp4",
            generation="3",
            kind="video",
            duration_s=5.5,
            aspect=analyzed_aspect,
        ),
        MediaRef(
            lane="asset",
            media_id="swim",
            gcs_path="users/test/swim.mp4",
            generation="7",
            kind="video",
            duration_s=5.5,
            aspect=analyzed_aspect,
        ),
    ]
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="A small Corfu travel story",
        pace="balanced",
        duration_s=9 if creator_pinned_portrait else 15,
        title="Corfu in small moments",
        media=media,
        mixed_media_timing=(
            MixedMediaTimingProfile(
                image_hold="very_fast",
                video_hold="longer",
                boundary_style="cut",
            )
            if creator_pinned_portrait
            else None
        ),
        output_orientation="portrait" if creator_pinned_portrait else None,
        story_beats=[
            StoryBeat(
                beat_id="food",
                topic="Food",
                thought="A cold treat between long walks.",
                media_ids=["food", "food-detail", "dessert"],
                layout="supporting_card",
                duration_s=4,
            ),
            StoryBeat(
                beat_id="town",
                topic="Town",
                thought="Warm walls and narrow streets.",
                media_ids=["town", "street"],
                duration_s=4,
            ),
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="Then everything opens onto the water.",
                media_ids=["coast", "swim"],
                duration_s=4,
            ),
        ],
    )
    return {
        "proposal_version": 4,
        "media_digest": canonical_media_digest(media),
        "approved_proposal": snapshot.model_dump(mode="json"),
        "media_identities": [
            {
                "lane": ref.lane,
                "media_id": ref.media_id,
                "gcs_path": ref.gcs_path,
                "generation": ref.generation,
                "kind": ref.kind,
            }
            for ref in media
        ],
    }


@pytest.mark.parametrize(
    ("with_music", "orientation", "creator_pinned_portrait"),
    [
        (False, "portrait", False),
        (True, "portrait", False),
        (False, "landscape", False),
        (False, "portrait", True),
        (True, "portrait", True),
    ],
)
def test_real_ffmpeg_mixed_story_has_text_audio_and_exact_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    with_music: bool,
    orientation: str,
    creator_pinned_portrait: bool,
) -> None:
    from app import storage
    from app.pipeline import guided_story

    food = tmp_path / "food.heic"
    food_detail = tmp_path / "food-detail.webp"
    dessert = tmp_path / "dessert.jpg"
    town = tmp_path / "town.jpg"
    street = tmp_path / "street.jpg"
    coast = tmp_path / "coast.mp4"
    swim = tmp_path / "swim.mp4"
    _image(food, (220, 120, 40), "FOOD")
    _transparent_image(food_detail)
    _image(dessert, (200, 90, 120), "DESSERT")
    _image(town, (170, 130, 80), "TOWN")
    _image(street, (140, 110, 75), "STREET")
    _video(
        coast,
        size=("640x360" if orientation == "landscape" or creator_pinned_portrait else "360x640"),
    )
    shutil.copy2(coast, swim)
    sources = {
        "users/test/food.heic": food,
        "users/test/food-detail.webp": food_detail,
        "users/test/dessert.jpg": dessert,
        "users/test/town.jpg": town,
        "users/test/street.jpg": street,
        "users/test/coast.mp4": coast,
        "users/test/swim.mp4": swim,
    }
    uploads = tmp_path / "uploads"
    uploads.mkdir()

    def download(object_path: str, local_path: str, *, generation: str) -> None:
        assert generation in {"1", "2", "3", "4", "5", "6", "7"}
        shutil.copy2(sources[object_path], local_path)

    def upload(local_path: str, object_path: str, content_type: str = "video/mp4") -> str:
        del content_type
        shutil.copy2(local_path, uploads / Path(object_path).name)
        return f"https://example.test/{object_path}"

    canvas = Canvas(640, 360) if orientation == "landscape" else Canvas(360, 640)
    monkeypatch.setattr(guided_story, orientation.upper(), canvas)
    monkeypatch.setattr(guided_story.settings, "output_width", canvas.width)
    monkeypatch.setattr(guided_story.settings, "output_height", canvas.height)
    monkeypatch.setattr(storage, "download_generation_to_file", download)
    monkeypatch.setattr(storage, "upload_public_read", upload)
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda object_path: storage.ObjectMetadata(
            path=object_path,
            generation=f"stored-{Path(object_path).name}",
            etag=None,
            size=(uploads / Path(object_path).name).stat().st_size,
            content_type="video/mp4",
            md5_hash=None,
        ),
    )

    track = None
    track_payload = None
    if with_music:
        audio = tmp_path / "music.m4a"
        subprocess.run(
            [
                "ffmpeg",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=330:sample_rate=44100",
                "-t",
                "8",
                "-c:a",
                "aac",
                "-y",
                str(audio),
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        from app import storage

        monkeypatch.setattr(
            storage,
            "download_generation_to_file",
            lambda object_path, local_path, *, generation: (
                shutil.copy2(audio, local_path)
                if object_path == "music/a.m4a"
                else download(object_path, local_path, generation=generation)
            ),
        )
        track = SimpleNamespace(
            id="track-1",
            title="Corfu Drift",
            audio_gcs_path="music/a.m4a",
            generation="123",
        )
        track_payload = {
            "track_id": "track-1",
            "title": "Corfu Drift",
            "audio_gcs_path": "music/a.m4a",
            "generation": "123",
            "start_s": 0.0,
        }

    plan = compile_execution_plan(
        _snapshot(
            analyzed_aspect=(
                1.7778 if orientation == "landscape" or creator_pinned_portrait else None
            ),
            creator_pinned_portrait=creator_pinned_portrait,
        ),
        track=track_payload,
    )
    assert plan["output_orientation"] == orientation
    assert plan["transition_policy"]["type"] == ("none" if creator_pinned_portrait else "crossfade")
    if creator_pinned_portrait:
        assert plan["mixed_media_timing"] == {
            "image_hold": "very_fast",
            "video_hold": "longer",
            "boundary_style": "cut",
        }
        assert all(
            0.5 <= moment["duration_s"] <= 0.8
            for moment in plan["story_timeline"]
            if moment["kind"] == "image"
        )
        assert all(
            1.5 <= moment["duration_s"] <= 3.0
            for moment in plan["story_timeline"]
            if moment["kind"] == "video"
        )
    assert all(element["stroke_width"] == 0 for element in plan["text_elements"])
    if not with_music and orientation == "portrait":
        image_moment = next(row for row in plan["story_timeline"] if row["kind"] == "image")
        image_moment["look_preset"] = "olive_film"
        image_moment["look_adjustments"] = {
            "intensity": 0.7,
            "warmth": 0.3,
            "contrast": -0.2,
            "grain": 0.1,
            "vignette": 0.15,
        }
        video_moment = next(row for row in plan["story_timeline"] if row["kind"] == "video")
        video_moment["look_preset"] = "smoky_split_tone"
        video_moment["look_adjustments"] = {
            "intensity": 0.6,
            "warmth": -0.2,
            "contrast": 0.25,
            "grain": 0.2,
            "vignette": 0.3,
        }
    for element in plan["text_elements"]:
        element["size_px"] = 28 if element["id"] == "guided-title" else 20
    result = render_execution_plan(
        plan,
        job_id="real-guided-test",
        tmpdir=str(tmp_path),
        track=track,
    )

    receipt = result["render_receipt"]
    assert result["ok"] is True
    assert receipt["verified"] is True
    assert receipt["actual_beat_ids"] == ["food", "town", "coast"]
    assert receipt["actual_media_ids"] == [
        "food",
        "food-detail",
        "dessert",
        "town",
        "street",
        "coast",
        "swim",
    ]
    assert receipt["image_count"] == 5
    assert receipt["video_count"] == 2
    assert receipt["output"]["audio_codec"] == "aac"
    assert receipt["music_applied"] is with_music
    assert set(receipt["actual_text_ids"]) == {
        "guided-title",
        "guided-thought-food",
        "guided-thought-town",
        "guided-thought-coast",
    }
    final = next(uploads.glob("variant_1_guided_story_*.mp4"))
    probe = probe_video(str(final))
    assert (probe.width, probe.height, probe.codec, probe.has_audio) == (
        canvas.width,
        canvas.height,
        "h264",
        True,
    )
    assert receipt["actual_duration_s"] == pytest.approx(plan["resolved_duration_s"], abs=0.15)
    assert probe.duration_s == pytest.approx(plan["resolved_duration_s"], abs=0.15)
    base = next(uploads.glob("base_1_guided_story_*.mp4"))
    base_probe = probe_video(str(base))
    assert (base_probe.width, base_probe.height) == (canvas.width, canvas.height)
