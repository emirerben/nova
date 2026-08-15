from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pillow_heif
import pytest
from PIL import Image, ImageDraw

from app.pipeline.canvas import Canvas
from app.pipeline.guided_story import (
    compile_execution_plan,
    render_execution_plan,
)
from app.pipeline.probe import probe_video
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
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


def _video(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=360x640:rate=30",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "5.5",
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


def _snapshot() -> dict:
    media = [
        MediaRef(
            lane="asset",
            media_id="food",
            gcs_path="users/test/food.heic",
            generation="1",
            kind="image",
        ),
        MediaRef(
            lane="asset",
            media_id="town",
            gcs_path="users/test/town.jpg",
            generation="2",
            kind="image",
        ),
        MediaRef(
            lane="asset",
            media_id="food-detail",
            gcs_path="users/test/food-detail.webp",
            generation="4",
            kind="image",
        ),
        MediaRef(
            lane="asset",
            media_id="dessert",
            gcs_path="users/test/dessert.jpg",
            generation="5",
            kind="image",
        ),
        MediaRef(
            lane="asset",
            media_id="street",
            gcs_path="users/test/street.jpg",
            generation="6",
            kind="image",
        ),
        MediaRef(
            lane="clip",
            media_id="coast",
            gcs_path="users/test/coast.mp4",
            generation="3",
            kind="video",
            duration_s=5.5,
        ),
        MediaRef(
            lane="asset",
            media_id="swim",
            gcs_path="users/test/swim.mp4",
            generation="7",
            kind="video",
            duration_s=5.5,
        ),
    ]
    snapshot = EditProposalSnapshot(
        direction="guided_story",
        goal="A small Corfu travel story",
        pace="balanced",
        duration_s=15,
        title="Corfu in small moments",
        media=media,
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


@pytest.mark.parametrize("with_music", [False, True])
def test_real_ffmpeg_mixed_story_has_text_audio_and_exact_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_music: bool
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
    _video(coast)
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

    canvas = Canvas(360, 640)
    monkeypatch.setattr(guided_story, "PORTRAIT", canvas)
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

    plan = compile_execution_plan(_snapshot(), track=track_payload)
    for element in plan["text_elements"]:
        element["size_px"] = 28 if element["id"] == "guided-title" else 20
        element["stroke_width"] = 2
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
    assert (probe.width, probe.height, probe.codec, probe.has_audio) == (360, 640, "h264", True)
    assert probe.duration_s == pytest.approx(15, abs=0.3)
