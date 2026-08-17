from __future__ import annotations

import copy
import hashlib
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.pipeline.guided_story import (
    GuidedStoryError,
    _compile_execution_plan_version,
    _download_selected,
    _mix_pinned_music,
    _render_video_moment,
    _upload_verified_outputs,
    _verify_receipt,
    compile_execution_plan,
    validate_execution_plan,
    validate_proposal_timing,
    validate_ready_result,
    verify_guided_text_reburn,
)
from app.schemas.edit_proposal import (
    EditProposalSnapshot,
    MediaRef,
    StoryBeat,
    canonical_media_digest,
)


def _guided_snapshot(*, direction: str = "guided_story", catalog_extra: bool = False) -> dict:
    media = [
        MediaRef(
            lane="clip",
            media_id="coast-video",
            gcs_path="users/u/coast.mp4",
            generation="11",
            kind="video",
            duration_s=12,
            analysis={
                "subject": "coast",
                "description": "turquoise sea and a small boat",
                "best_moments": [{"start_s": 2, "end_s": 10, "description": "boat"}],
            },
        ),
        MediaRef(
            lane="asset",
            media_id="food-photo",
            gcs_path="users/u/food.jpg",
            generation="12",
            kind="image",
            analysis={"subject": "food", "description": "ice cream"},
        ),
        MediaRef(
            lane="asset",
            media_id="town-photo",
            gcs_path="users/u/town.jpg",
            generation="13",
            kind="image",
            analysis={"subject": "architecture", "description": "old town street"},
        ),
    ]
    if catalog_extra:
        media.append(
            MediaRef(
                lane="asset",
                media_id="unused-photo",
                gcs_path="users/u/unused.jpg",
                generation="14",
                kind="image",
            )
        )
    snapshot = EditProposalSnapshot(
        direction=direction,
        goal="Explain what stood out in Corfu",
        pace="balanced",
        duration_s=18,
        title="What Corfu felt like",
        media=media,
        story_beats=[
            StoryBeat(
                beat_id="food",
                topic="Food",
                thought="Small treats made the hot afternoons better.",
                media_ids=["food-photo"],
                layout="supporting_card",
                duration_s=4,
            ),
            StoryBeat(
                beat_id="town",
                topic="Architecture",
                thought="The old streets reward slow wandering.",
                media_ids=["town-photo"],
                duration_s=4,
            ),
            StoryBeat(
                beat_id="coast",
                topic="Coast",
                thought="The water changes the pace of the whole day.",
                media_ids=["coast-video"],
                duration_s=4,
            ),
        ],
    )
    return {
        "proposal_version": 7,
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


def test_proposal_timing_validator_rejects_unrenderable_revision() -> None:
    raw = _guided_snapshot(direction="text_explainer")
    snapshot = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    impossible = snapshot.model_copy(
        update={
            "duration_s": 10,
            "story_beats": [
                StoryBeat(
                    beat_id="first",
                    topic="First",
                    media_ids=["coast-video", "food-photo", "town-photo"],
                    duration_s=4,
                ),
                StoryBeat(
                    beat_id="second",
                    topic="Second",
                    media_ids=["coast-video", "food-photo", "town-photo"],
                    duration_s=4,
                ),
            ],
        }
    )
    with pytest.raises(GuidedStoryError, match="too short to show all approved media"):
        validate_proposal_timing(impossible)


def test_proposal_timing_validator_ignores_malformed_best_moments() -> None:
    raw = _guided_snapshot()
    snapshot = EditProposalSnapshot.model_validate(raw["approved_proposal"])
    snapshot.media[0].analysis["best_moments"] = ["not an object", None]

    validate_proposal_timing(snapshot)


def test_compiler_uses_only_beat_selected_media_and_hits_target_duration() -> None:
    raw = _guided_snapshot(catalog_extra=True)
    plan = compile_execution_plan(raw, track=None)

    assert plan["selected_media_ids"] == ["food-photo", "town-photo", "coast-video"]
    assert "unused-photo" not in {row["media_id"] for row in plan["story_timeline"]}
    assert plan["resolved_duration_s"] == 18
    assert plan["compiler_version"] == 2
    assert plan["proposal_version"] == 7
    assert [row["beat_id"] for row in plan["beat_windows"]] == ["food", "town", "coast"]
    assert {row["layout"] for row in plan["story_timeline"]} == {
        "fullscreen",
        "supporting_card",
    }
    # Title and first thought occupy different vertical positions, so both can
    # remain readable for the full first beat.  Delaying the thought until the
    # title ended reduced short montage labels to a single frame.
    first_thought = next(row for row in plan["text_elements"] if row["id"] == "guided-thought-food")
    assert first_thought["start_s"] == 0.0
    assert first_thought["end_s"] == plan["beat_windows"][0]["end_s"]


@pytest.mark.parametrize(
    ("direction", "minimum", "transition"),
    [
        ("guided_story", 1.4, "crossfade"),
        ("fast_montage", 0.8, "none"),
        ("text_explainer", 1.8, "crossfade"),
    ],
)
def test_direction_policy_is_persisted(direction: str, minimum: float, transition: str) -> None:
    raw = _guided_snapshot(direction=direction)
    plan = compile_execution_plan(raw, track=None)
    assert min(row["duration_s"] for row in plan["story_timeline"]) >= minimum
    assert plan["transition_policy"]["type"] == transition
    if direction == "text_explainer":
        assert max(row["size_px"] for row in plan["text_elements"][1:]) == 54


def test_execution_plan_is_fenced_to_approval_version_and_digest() -> None:
    raw = _guided_snapshot()
    plan = compile_execution_plan(
        raw,
        track={
            "track_id": "track-1",
            "title": "Dreamy",
            "audio_gcs_path": "music/dreamy.m4a",
            "generation": "123",
            "start_s": 12.5,
        },
    )
    assert validate_execution_plan(plan, raw) == plan

    changed = copy.deepcopy(raw)
    changed["proposal_version"] = 8
    with pytest.raises(GuidedStoryError, match="no longer matches approval") as exc:
        validate_execution_plan(plan, changed)
    assert exc.value.code == "guided_story_snapshot_invalid"


def test_execution_plan_accepts_v1_timing_across_compiler_upgrade() -> None:
    raw = _guided_snapshot()
    raw["approved_proposal"]["duration_s"] = 17
    raw["approved_proposal"]["story_beats"][0]["media_ids"] = [
        "food-photo",
        "town-photo",
    ]
    plan = _compile_execution_plan_version(raw, track=None, compiler_version=1)

    first_beat = [row for row in plan["story_timeline"] if row["beat_id"] == "food"]
    assert [row["duration_s"] for row in first_beat] == [2.953, 2.954]
    assert validate_execution_plan(plan, raw) == plan


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan["story_timeline"][0].update(gcs_path="users/other.jpg"),
        lambda plan: plan["story_timeline"][0].update(moment_id="changed-moment"),
        lambda plan: plan["story_timeline"][0].update(
            source_start_s=1.0,
            source_end_s=2.0,
            output_start_s=1.0,
            output_end_s=2.0,
            duration_s=1.0,
        ),
    ],
)
def test_execution_plan_v1_compatibility_still_rejects_semantic_drift(mutate) -> None:
    raw = _guided_snapshot()
    plan = _compile_execution_plan_version(raw, track=None, compiler_version=1)
    mutate(plan)

    with pytest.raises(GuidedStoryError) as exc:
        validate_execution_plan(plan, raw)

    assert exc.value.code == "guided_story_snapshot_invalid"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda plan: plan["beat_windows"].pop(),
        lambda plan: plan["story_timeline"][0].update(gcs_path="users/other.jpg"),
        lambda plan: plan["text_elements"][0].update(text="Rewritten after approval"),
    ],
)
def test_execution_plan_rejects_any_semantic_drift_from_approval(mutate) -> None:
    raw = _guided_snapshot()
    plan = compile_execution_plan(raw, track=None)
    mutate(plan)

    with pytest.raises(GuidedStoryError) as exc:
        validate_execution_plan(plan, raw)

    assert exc.value.code == "guided_story_snapshot_invalid"


def test_compiler_fails_instead_of_dropping_media_when_duration_is_too_short() -> None:
    raw = _guided_snapshot(direction="text_explainer")
    proposal = raw["approved_proposal"]
    proposal["story_beats"][0]["media_ids"] = ["food-photo", "town-photo", "coast-video"]
    proposal["story_beats"][0]["duration_s"] = 1
    proposal["story_beats"][1]["duration_s"] = 12
    proposal["story_beats"] = proposal["story_beats"][:2]
    proposal["duration_s"] = 10

    with pytest.raises(GuidedStoryError, match="too short") as exc:
        compile_execution_plan(raw, track=None)
    assert exc.value.code == "guided_story_duration_impossible"


def test_compiler_gives_short_video_its_available_time_and_redistributes_beat() -> None:
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = 1.966667
    coast["analysis"]["duration_s"] = 1.966667
    coast["analysis"]["best_moments"] = []
    proposal["duration_s"] = 12
    proposal["story_beats"] = [
        {
            "beat_id": "old-town",
            "topic": "Old Town",
            "thought": "The old streets reward slow wandering.",
            "media_ids": ["food-photo", "town-photo", "coast-video"],
            "layout": "fullscreen",
            "duration_s": 10,
        },
        {
            "beat_id": "closing",
            "topic": "Closing",
            "thought": "One last look before leaving.",
            "media_ids": ["food-photo"],
            "layout": "fullscreen",
            "duration_s": 2,
        },
    ]

    plan = compile_execution_plan(raw, track=None)

    moments = plan["story_timeline"]
    video = next(row for row in moments if row["media_id"] == "coast-video")
    assert moments.index(video) < len(moments) - 1
    assert video["source_end_s"] <= 1.967
    assert video["duration_s"] >= 1.4
    assert plan["beat_windows"][0]["resolved_duration_s"] == 10
    assert plan["resolved_duration_s"] == 12


@pytest.mark.parametrize(
    ("direction", "media_ids", "video_duration_s", "transition_type"),
    [
        ("guided_story", ["food-photo", "coast-video"], 1.4, "crossfade"),
        ("fast_montage", ["coast-video", "food-photo"], 0.8, "none"),
    ],
)
def test_compiler_accepts_video_at_minimum_without_transition_overlap(
    direction: str,
    media_ids: list[str],
    video_duration_s: float,
    transition_type: str,
) -> None:
    raw = _guided_snapshot(direction=direction)
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = video_duration_s
    coast["analysis"]["duration_s"] = video_duration_s
    coast["analysis"]["best_moments"] = []
    proposal["duration_s"] = 10
    proposal["story_beats"] = [
        {
            "beat_id": "boundary",
            "topic": "Boundary",
            "thought": "Every approved source remains visible.",
            "media_ids": media_ids,
            "layout": "fullscreen",
            "duration_s": 10,
        }
    ]

    plan = compile_execution_plan(raw, track=None)

    video = next(row for row in plan["story_timeline"] if row["media_id"] == "coast-video")
    assert video["duration_s"] == pytest.approx(video_duration_s, abs=0.001)
    assert video["source_end_s"] <= video_duration_s + 0.001
    assert plan["transition_policy"]["type"] == transition_type
    assert plan["resolved_duration_s"] == 10


def test_compiler_rejects_selected_video_without_duration() -> None:
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = None

    with pytest.raises(GuidedStoryError, match="no usable duration") as exc:
        compile_execution_plan(raw, track=None)

    assert exc.value.code == "guided_story_duration_impossible"


def test_compiler_rejects_video_too_short_after_transition_overlap() -> None:
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    coast = next(row for row in proposal["media"] if row["media_id"] == "coast-video")
    coast["duration_s"] = 1.45
    proposal["duration_s"] = 10
    proposal["story_beats"] = [
        {
            "beat_id": "short-video",
            "topic": "Short video",
            "thought": "A quick glimpse.",
            "media_ids": ["coast-video"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
        {
            "beat_id": "closing",
            "topic": "Closing",
            "thought": "One last look.",
            "media_ids": ["food-photo"],
            "layout": "fullscreen",
            "duration_s": 5,
        },
    ]

    with pytest.raises(GuidedStoryError, match="too short to show clearly") as exc:
        compile_execution_plan(raw, track=None)

    assert exc.value.code == "guided_story_duration_impossible"


def test_compiler_rejects_all_video_beat_without_enough_total_footage() -> None:
    raw = _guided_snapshot()
    proposal = raw["approved_proposal"]
    for row in proposal["media"]:
        row["kind"] = "video"
        row["duration_s"] = 2.0
    for row in raw["media_identities"]:
        row["kind"] = "video"
    media = [MediaRef.model_validate(row) for row in proposal["media"]]
    raw["media_digest"] = canonical_media_digest(media)
    proposal["duration_s"] = 10
    proposal["story_beats"] = [
        {
            "beat_id": "all-video",
            "topic": "All video",
            "thought": "Every approved clip should remain visible.",
            "media_ids": ["food-photo", "town-photo", "coast-video"],
            "layout": "fullscreen",
            "duration_s": 10,
        }
    ]

    with pytest.raises(GuidedStoryError, match="longer than its approved videos") as exc:
        compile_execution_plan(raw, track=None)

    assert exc.value.code == "guided_story_duration_impossible"


def test_missing_selected_source_has_stable_failure_code(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.storage.download_generation_to_file",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("generation missing")),
    )
    plan = {
        "selected_media_ids": ["food-photo"],
        "story_timeline": [
            {
                "media_id": "food-photo",
                "gcs_path": "users/u/food.jpg",
                "generation": "12",
                "kind": "image",
            }
        ],
    }

    with pytest.raises(GuidedStoryError) as exc:
        _download_selected(plan, str(tmp_path))

    assert exc.value.code == "guided_story_media_missing"


def test_selected_source_with_wrong_format_is_rejected(tmp_path, monkeypatch) -> None:
    def invalid_image(_path: str, local: str, *, generation: str) -> None:
        assert generation == "12"
        with open(local, "wb") as handle:
            handle.write(b"not an image")

    monkeypatch.setattr("app.storage.download_generation_to_file", invalid_image)
    plan = {
        "selected_media_ids": ["food-photo"],
        "story_timeline": [
            {
                "media_id": "food-photo",
                "gcs_path": "users/u/food.jpg",
                "generation": "12",
                "kind": "image",
            }
        ],
    }

    with pytest.raises(GuidedStoryError) as exc:
        _download_selected(plan, str(tmp_path))

    assert exc.value.code == "guided_story_media_replaced"


def test_selected_heic_is_normalized_without_changing_source_receipt(tmp_path, monkeypatch) -> None:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    source = tmp_path / "uploaded.heic"
    try:
        Image.new("RGB", (80, 120), (24, 120, 180)).save(source, format="HEIF")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"local pillow-heif cannot encode HEIF: {exc}")
    source_bytes = source.read_bytes()

    def download(_path: str, local: str, *, generation: str) -> None:
        assert generation == "12"
        shutil.copy2(source, local)

    monkeypatch.setattr("app.storage.download_generation_to_file", download)
    plan = {
        "selected_media_ids": ["corfu-photo"],
        "story_timeline": [
            {
                "media_id": "corfu-photo",
                "gcs_path": "users/u/corfu.HEIC",
                "generation": "12",
                "kind": "image",
            }
        ],
    }

    local_by_id, receipts = _download_selected(plan, str(tmp_path))

    render_source = Path(local_by_id["corfu-photo"])
    assert render_source.suffix == ".jpg"
    with Image.open(render_source) as image:
        assert image.format == "JPEG"
        assert image.size == (80, 120)
    assert receipts[0]["bytes"] == len(source_bytes)
    assert receipts[0]["sha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_selected_transparent_image_preserves_alpha_and_source_receipt(
    tmp_path, monkeypatch
) -> None:
    from PIL import Image

    source = tmp_path / "uploaded.webp"
    image = Image.new("RGBA", (80, 120), (24, 120, 180, 255))
    image.putpixel((0, 0), (24, 120, 180, 0))
    image.save(source, format="WEBP", lossless=True)
    source_bytes = source.read_bytes()

    def download(_path: str, local: str, *, generation: str) -> None:
        assert generation == "13"
        shutil.copy2(source, local)

    monkeypatch.setattr("app.storage.download_generation_to_file", download)
    plan = {
        "selected_media_ids": ["transparent-card"],
        "story_timeline": [
            {
                "media_id": "transparent-card",
                "gcs_path": "users/u/card.webp",
                "generation": "13",
                "kind": "image",
            }
        ],
    }

    local_by_id, receipts = _download_selected(plan, str(tmp_path))

    render_source = Path(local_by_id["transparent-card"])
    assert render_source.suffix == ".png"
    with Image.open(render_source) as normalized:
        assert normalized.format == "PNG"
        assert normalized.getchannel("A").getextrema() == (0, 255)
    assert receipts[0]["bytes"] == len(source_bytes)
    assert receipts[0]["sha256"] == hashlib.sha256(source_bytes).hexdigest()


def test_video_window_beyond_downloaded_duration_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.guided_story.probe_video",
        lambda _path: SimpleNamespace(duration_s=1.0),
    )

    with pytest.raises(GuidedStoryError) as exc:
        _render_video_moment(
            "source.mp4",
            "output.mp4",
            start_s=0.0,
            end_s=2.0,
            layout="fullscreen",
        )

    assert exc.value.code == "guided_story_duration_impossible"


def test_snapshot_identity_set_must_match_approved_catalog() -> None:
    raw = _guided_snapshot()
    raw["media_identities"].pop()
    with pytest.raises(GuidedStoryError, match="identities") as exc:
        compile_execution_plan(raw, track=None)
    assert exc.value.code == "guided_story_snapshot_invalid"


def test_partial_output_upload_is_compensated(tmp_path, monkeypatch) -> None:
    from app import storage

    clean = tmp_path / "clean.mp4"
    final = tmp_path / "final.mp4"
    clean.write_bytes(b"clean")
    final.write_bytes(b"final")
    uploaded: list[str] = []
    deleted: list[str] = []

    def upload(_local: str, key: str) -> str:
        uploaded.append(key)
        if key.endswith("final.mp4"):
            raise RuntimeError("provider failed after create")
        return f"https://example.test/{key}"

    monkeypatch.setattr(storage, "upload_public_read", upload)
    monkeypatch.setattr(storage, "delete_object_best_effort", deleted.append)

    with pytest.raises(RuntimeError, match="provider failed"):
        _upload_verified_outputs(
            str(clean),
            str(final),
            base_key="jobs/base.mp4",
            output_key="jobs/final.mp4",
        )

    assert uploaded == ["jobs/base.mp4", "jobs/final.mp4"]
    assert deleted == ["jobs/base.mp4", "jobs/final.mp4"]


def test_deleted_pinned_music_generation_has_stable_failure_code(monkeypatch) -> None:
    from app.tasks import template_orchestrate

    monkeypatch.setattr(
        template_orchestrate,
        "_mix_template_audio",
        lambda *_a, **_kw: (_ for _ in ()).throw(FileNotFoundError("generation gone")),
    )
    music = {
        "track_id": "track-1",
        "title": "Corfu Drift",
        "audio_gcs_path": "music/corfu.m4a",
        "generation": "123",
        "start_s": 0.0,
    }
    track = SimpleNamespace(
        id="track-1",
        audio_gcs_path="music/corfu.m4a",
        generation="123",
    )

    with pytest.raises(GuidedStoryError) as exc:
        _mix_pinned_music("story.mp4", "base.mp4", "/tmp", music, track)

    assert exc.value.code == "guided_story_music_missing"


def _verified_receipt(plan: dict) -> dict:
    beat_ids = [row["beat_id"] for row in plan["beat_windows"]]
    moment_ids = [row["moment_id"] for row in plan["story_timeline"]]
    text_ids = [row["id"] for row in plan["text_elements"]]
    return {
        "schema_version": 1,
        "verified": True,
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "expected_beat_ids": beat_ids,
        "actual_beat_ids": beat_ids,
        "expected_moment_ids": moment_ids,
        "actual_moment_ids": moment_ids,
        "expected_media_ids": plan["selected_media_ids"],
        "actual_media_ids": plan["selected_media_ids"],
        "expected_text_ids": text_ids,
        "actual_text_ids": text_ids,
        "media_count": len(plan["selected_media_ids"]),
        "image_count": 2,
        "video_count": 1,
        "expected_duration_s": plan["resolved_duration_s"],
        "actual_duration_s": plan["resolved_duration_s"],
        "music_applied": False,
        "music": None,
        "output": {
            "width": 1080,
            "height": 1920,
            "video_codec": "h264",
            "audio_codec": "aac",
            "sha256": "a" * 64,
        },
        "base_storage": {
            "path": "generative-jobs/job-1/base_guided.mp4",
            "generation": "base-gen",
            "size": 100,
            "md5_hash": None,
        },
        "output_storage": {
            "path": "generative-jobs/job-1/final_guided.mp4",
            "generation": "output-gen",
            "size": 101,
            "md5_hash": None,
        },
        "media_stages": [
            {
                "media_id": media_id,
                "gcs_path": next(
                    row["gcs_path"] for row in plan["story_timeline"] if row["media_id"] == media_id
                ),
                "generation": next(
                    row["generation"]
                    for row in plan["story_timeline"]
                    if row["media_id"] == media_id
                ),
                "kind": next(
                    row["kind"] for row in plan["story_timeline"] if row["media_id"] == media_id
                ),
            }
            for media_id in plan["selected_media_ids"]
        ],
        "moment_stages": [
            {
                "moment_id": row["moment_id"],
                "beat_id": row["beat_id"],
                "media_id": row["media_id"],
                "generation": row["generation"],
                "kind": row["kind"],
                "layout": row["layout"],
                "image_motion": row["image_motion"],
            }
            for row in plan["story_timeline"]
        ],
        "text_stages": [{"element_id": element_id, "visible": True} for element_id in text_ids],
    }


def _ready_result(plan: dict) -> dict:
    return {
        "variant_id": "guided_story",
        "resolved_archetype": "guided_story",
        "render_status": "ready",
        "ok": True,
        "proposal_version": plan["proposal_version"],
        "media_digest": plan["media_digest"],
        "story_timeline": plan["story_timeline"],
        "text_elements": plan["text_elements"],
        "base_video_path": "generative-jobs/job-1/base_guided.mp4",
        "video_path": "generative-jobs/job-1/final_guided.mp4",
        "render_receipt": _verified_receipt(plan),
    }


def test_ready_result_requires_canonical_plan_and_complete_stage_receipts(monkeypatch) -> None:
    plan = compile_execution_plan(_guided_snapshot(), track=None)
    result = _ready_result(plan)

    assert validate_ready_result(plan, result, job_id="job-1", verify_storage=False) == result

    corrupt = copy.deepcopy(result)
    corrupt["render_receipt"]["actual_moment_ids"].pop()
    with pytest.raises(GuidedStoryError) as exc:
        validate_ready_result(plan, corrupt, job_id="job-1", verify_storage=False)
    assert exc.value.code == "guided_story_receipt_mismatch"

    for section, field, value in (
        (None, "expected_duration_s", 99.0),
        ("media_stages", "generation", "replaced-source"),
        ("moment_stages", "layout", "fullscreen"),
    ):
        corrupt = copy.deepcopy(result)
        target = (
            corrupt["render_receipt"] if section is None else corrupt["render_receipt"][section][0]
        )
        target[field] = value
        with pytest.raises(GuidedStoryError) as exc:
            validate_ready_result(plan, corrupt, job_id="job-1", verify_storage=False)
        assert exc.value.code == "guided_story_receipt_mismatch"

    from app import storage

    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(
            path=path,
            generation="base-gen" if "base_" in path else "output-gen",
            etag=None,
            size=100 if "base_" in path else 101,
            content_type="video/mp4",
            md5_hash=None,
        ),
    )
    assert validate_ready_result(plan, result, job_id="job-1", verify_storage=True) == result
    monkeypatch.setattr(
        storage,
        "object_metadata",
        lambda path: storage.ObjectMetadata(
            path=path,
            generation="replacement",
            etag=None,
            size=100,
            content_type="video/mp4",
            md5_hash=None,
        ),
    )
    with pytest.raises(GuidedStoryError) as exc:
        validate_ready_result(plan, result, job_id="job-1", verify_storage=True)
    assert exc.value.code == "guided_story_receipt_mismatch"

    corrupt = copy.deepcopy(result)
    corrupt["render_receipt"]["media_stages"].pop()
    with pytest.raises(GuidedStoryError) as exc:
        validate_ready_result(plan, corrupt, job_id="job-1", verify_storage=False)
    assert exc.value.code == "guided_story_receipt_mismatch"


def test_guided_text_reburn_rejects_text_outside_final_timeline(tmp_path) -> None:
    plan = compile_execution_plan(_guided_snapshot(), track=None)
    outside = [dict(plan["text_elements"][0], start_s=20.0, end_s=21.0)]

    with pytest.raises(GuidedStoryError) as exc:
        verify_guided_text_reburn(
            _verified_receipt(plan),
            outside,
            [{"element_id": outside[0]["id"], "visible": True}],
            str(tmp_path / "final.mp4"),
            str(tmp_path / "base.mp4"),
        )

    assert exc.value.code == "guided_story_text_missing"


def test_guided_text_reburn_rejects_invisible_required_element(tmp_path, monkeypatch) -> None:
    from app.pipeline import guided_story

    plan = compile_execution_plan(_guided_snapshot(), track=None)
    final = tmp_path / "final.mp4"
    base = tmp_path / "base.mp4"
    final.write_bytes(b"final")
    base.write_bytes(b"base")
    monkeypatch.setattr(
        guided_story,
        "probe_video",
        lambda _path: SimpleNamespace(
            duration_s=18.0,
            width=1080,
            height=1920,
            codec="h264",
        ),
    )
    monkeypatch.setattr(guided_story, "_audio_codec", lambda _path: "aac")

    with pytest.raises(GuidedStoryError) as exc:
        verify_guided_text_reburn(
            _verified_receipt(plan),
            [plan["text_elements"][0]],
            [{"element_id": "guided-title", "visible": False}],
            str(final),
            str(base),
        )

    assert exc.value.code == "guided_story_text_missing"


@pytest.mark.parametrize("drop", ["supporting_card", "media", "text"])
def test_receipt_fault_injection_never_publishes_a_missing_required_stage(
    drop: str, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.pipeline import guided_story

    plan = compile_execution_plan(_guided_snapshot(), track=None)
    media_receipts = [{"media_id": media_id} for media_id in plan["selected_media_ids"]]
    moment_receipts = [
        {
            "moment_id": row["moment_id"],
            "beat_id": row["beat_id"],
            "media_id": row["media_id"],
            "kind": row["kind"],
            "layout": row["layout"],
        }
        for row in plan["story_timeline"]
    ]
    text_receipts = [{"element_id": row["id"], "visible": True} for row in plan["text_elements"]]
    if drop == "supporting_card":
        moment_receipts = [row for row in moment_receipts if row["layout"] != "supporting_card"]
    elif drop == "media":
        media_receipts.pop()
    else:
        text_receipts.pop()

    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    monkeypatch.setattr(
        guided_story,
        "probe_video",
        lambda _path: SimpleNamespace(
            duration_s=18,
            width=guided_story.settings.output_width,
            height=guided_story.settings.output_height,
            codec="h264",
            has_audio=True,
        ),
    )
    monkeypatch.setattr(guided_story, "_audio_codec", lambda _path: "aac")
    monkeypatch.setattr(guided_story, "_sha256", lambda _path: "hash")

    with pytest.raises(GuidedStoryError) as exc:
        _verify_receipt(
            plan,
            media_receipts,
            moment_receipts,
            text_receipts,
            str(final),
            music_applied=False,
        )
    assert exc.value.code in {"guided_story_text_missing", "guided_story_receipt_mismatch"}
