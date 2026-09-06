"""Regression locks for chat-first media parity with PlanItem uploads.

The chat workspace is only a different entry point into the existing PlanItem
pipeline. These tests intentionally pin the shared primary-footage contract so
the chat route cannot silently reintroduce a smaller private limit.
"""

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.routes.creation_threads import (
    AttachBody,
    MediaInput,
    UploadBody,
    UploadFile,
    _media_capabilities,
)
from app.routes.plan_items import _MAX_BYTES_PER_FILE, _MAX_CLIPS_PER_ITEM
from app.services.plan_item_media import (
    PROTECTED_PLAN_ITEM_MEDIA_FIELDS,
    mutate_plan_item_media,
)


def _video_upload(index: int, *, size: int = 1) -> UploadFile:
    return UploadFile(
        filename=f"clip-{index}.mp4",
        content_type="video/mp4",
        file_size_bytes=size,
        client_upload_id=f"clip-{index}",
    )


def _video_media(index: int) -> MediaInput:
    return MediaInput(
        media_id=f"clip-{index}",
        kind="video",
        filename=f"clip-{index}.mp4",
        content_type="video/mp4",
    )


def test_chat_upload_reservation_uses_the_plan_item_clip_count_ceiling() -> None:
    """The chat reservation batch must accept the same 50 primary clips."""

    assert _MAX_CLIPS_PER_ITEM == 50
    body = UploadBody(files=[_video_upload(index) for index in range(_MAX_CLIPS_PER_ITEM)])
    assert len(body.files) == 50
    with pytest.raises(ValidationError):
        UploadBody(files=[_video_upload(index) for index in range(_MAX_CLIPS_PER_ITEM + 1)])


def test_chat_upload_reservation_uses_the_plan_item_file_size_ceiling() -> None:
    """A valid PlanItem-sized source must not be rejected by chat validation."""

    assert _MAX_BYTES_PER_FILE == 4 * 1024 * 1024 * 1024
    upload = _video_upload(0, size=_MAX_BYTES_PER_FILE)
    assert upload.file_size_bytes == _MAX_BYTES_PER_FILE
    with pytest.raises(ValidationError):
        _video_upload(0, size=_MAX_BYTES_PER_FILE + 1)


def test_chat_media_attachment_batch_matches_primary_clip_ceiling() -> None:
    """Attaching reserved footage cannot impose a second 20-file ceiling."""

    body = AttachBody(
        media=[_video_media(index) for index in range(_MAX_CLIPS_PER_ITEM)],
        client_event_id="attach-50",
        expected_revision=0,
    )
    assert len(body.media) == _MAX_CLIPS_PER_ITEM
    with pytest.raises(ValidationError):
        AttachBody(
            media=[_video_media(index) for index in range(_MAX_CLIPS_PER_ITEM + 1)],
            client_event_id="attach-51",
            expected_revision=0,
        )


def test_chat_media_capabilities_expose_the_existing_plan_item_pools() -> None:
    """Chat clients receive the PlanItem clip, Visuals, and voiceover rules."""

    item = SimpleNamespace(edit_format="montage", voiceover_gcs_path=None)
    capabilities = _media_capabilities(item=item, clip_count=4, visual_count=7)

    assert capabilities["clips"] == {
        "current": 4,
        "max": 50,
        "server_max": 50,
        "max_file_bytes": 4 * 1024 * 1024 * 1024,
        "content_types": ["video/mp4", "video/quicktime"],
        "format": "montage",
    }
    assert capabilities["visuals"]["current"] == 7
    assert capabilities["visuals"]["max"] == 100
    assert capabilities["visuals"]["max_file_bytes"] == {
        "image": 25 * 1024 * 1024,
        "video": 512 * 1024 * 1024,
    }
    assert capabilities["voiceover"]["max"] == 1


def test_visual_media_is_exposed_as_a_separate_plan_item_pool() -> None:
    """Chat advertises Visuals separately; the pool owns its upload path/rows."""

    item = SimpleNamespace(edit_format="montage", voiceover_gcs_path=None)
    capabilities = _media_capabilities(item=item, clip_count=0, visual_count=0)
    assert capabilities["visuals"]["content_types"]
    assert capabilities["visuals"]["max"] == 100


def _attribute_targets(node: ast.AST) -> list[ast.Attribute]:
    if isinstance(node, ast.Attribute):
        return [node]
    if isinstance(node, (ast.Tuple, ast.List)):
        return [target for value in node.elts for target in _attribute_targets(value)]
    return []


def test_protected_plan_item_media_fields_have_one_writer() -> None:
    """Only the locked facade may replace narration/source identity fields."""

    app_root = Path(__file__).resolve().parents[2] / "app"
    owner = (app_root / "services" / "plan_item_media.py").resolve()
    offenders: list[str] = []
    protected_assignment = re.compile(
        rf"\.(?:{'|'.join(sorted(PROTECTED_PLAN_ITEM_MEDIA_FIELDS))})\s*=(?!=)"
    )
    for path in sorted(app_root.rglob("*.py")):
        source = path.read_text()
        if protected_assignment.search(source) is None:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            targets: list[ast.Attribute] = []
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                raw_targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                targets = [
                    target
                    for raw_target in raw_targets
                    for target in _attribute_targets(raw_target)
                ]
            elif isinstance(node, ast.AugAssign):
                targets = _attribute_targets(node.target)
            for target in targets:
                if target.attr in PROTECTED_PLAN_ITEM_MEDIA_FIELDS and path.resolve() != owner:
                    offenders.append(f"{path.relative_to(app_root)}:{target.lineno}:{target.attr}")

    assert offenders == [], "protected PlanItem media writes outside facade:\n" + "\n".join(
        offenders
    )


def _voiceover_item() -> SimpleNamespace:
    return SimpleNamespace(
        clip_gcs_paths=[],
        clip_assignments=[],
        voiceover_gcs_path="users/u/item/voice.m4a",
        voiceover_generation="voice-generation-1",
        voiceover_duration_s=18.0,
        edit_format="narrated_planned",
        audio_mode="voiceover",
        speech_cleanup_enabled=True,
        speech_cleanup_notice={"state": "accepted"},
        edit_proposal=None,
    )


def _current_analysis(item: SimpleNamespace) -> SimpleNamespace:
    from app.services.plan_item_media import resolve_item_narration

    resolution = resolve_item_narration(item, detector_policy="detector-policy-v1")
    assert resolution.source is not None
    return SimpleNamespace(
        source_policy_fingerprint=resolution.source.source_policy_fingerprint,
        status="ready",
        decision="clean",
        superseded_at=None,
    )


def _registered_video(media_id: str = "visual-video") -> dict[str, object]:
    return {
        "media_id": media_id,
        "gcs_path": f"users/u/item/{media_id}.mp4",
        "storage_generation": f"{media_id}-generation",
        "duration_s": 8.0,
        "has_audio": False,
        "manifest_identity": media_id,
    }


def test_audio_only_analysis_survives_later_visual_video_attachment() -> None:
    """An unchanged active voiceover keeps its evidence and accepted choice."""

    item = _voiceover_item()
    analysis = _current_analysis(item)

    result = mutate_plan_item_media(
        item,
        detector_policy="detector-policy-v1",
        clip_assignments=[_registered_video()],
        current_analysis=analysis,
    )

    assert result.source_changed is False
    assert result.schedule is False
    assert analysis.superseded_at is None
    assert analysis.decision == "clean"
    assert item.speech_cleanup_enabled is True
    assert item.speech_cleanup_notice == {"state": "accepted"}
    assert item.clip_gcs_paths == ["users/u/item/visual-video.mp4"]


def test_reordering_visual_only_footage_preserves_active_voiceover_choice() -> None:
    item = _voiceover_item()
    first, second = _registered_video("visual-a"), _registered_video("visual-b")
    item.clip_assignments = [first, second]
    item.clip_gcs_paths = [str(first["gcs_path"]), str(second["gcs_path"])]
    analysis = _current_analysis(item)

    result = mutate_plan_item_media(
        item,
        detector_policy="detector-policy-v1",
        clip_assignments=[second, first],
        current_analysis=analysis,
    )

    assert result.source_changed is False
    assert result.schedule is False
    assert analysis.superseded_at is None
    assert item.speech_cleanup_enabled is True


@pytest.mark.parametrize(
    "mutation",
    [
        {"voiceover_generation": "voice-generation-2"},
        {"voiceover_gcs_path": "users/u/item/replacement.m4a"},
        {"voiceover_duration_s": 16.0},
        {"edit_format": "narrated_ready"},
        {"audio_mode": "kria"},
    ],
)
def test_active_source_or_policy_change_invalidates_before_reschedule(
    mutation: dict[str, object],
) -> None:
    item = _voiceover_item()
    item.clip_assignments = [_registered_video("foreground")]
    item.clip_gcs_paths = ["users/u/item/foreground.mp4"]
    # Give the embedded fallback audio so changing audio_mode resolves a new
    # source rather than merely making narration unavailable.
    item.clip_assignments[0]["has_audio"] = True
    analysis = _current_analysis(item)
    settled_at = datetime.now(UTC)

    result = mutate_plan_item_media(
        item,
        detector_policy="detector-policy-v1",
        current_analysis=analysis,
        now=settled_at,
        **mutation,
    )

    assert result.source_changed is True
    assert analysis.superseded_at == settled_at
    assert item.speech_cleanup_enabled is False
    assert item.speech_cleanup_notice is None
    assert result.schedule is (result.current.source is not None)


def test_chat_and_editor_entrypoints_use_locked_media_mutation_boundary() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    chat_source = (app_root / "routes" / "creation_threads.py").read_text()
    editor_source = (app_root / "routes" / "plan_items.py").read_text()

    assert "mutate_plan_item_media(" in chat_source
    assert "mutate_plan_item_media(" in editor_source
    assert "with_for_update=True" in chat_source
    assert "with_for_update=True" in editor_source


def test_chat_preflight_publication_is_strictly_post_commit() -> None:
    app_root = Path(__file__).resolve().parents[2] / "app"
    tree = ast.parse((app_root / "routes" / "creation_threads.py").read_text())
    attach = next(
        node
        for node in tree.body
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == "attach_media"
    )
    commit_lines = [
        node.lineno
        for node in ast.walk(attach)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]
    publish_lines = [
        node.lineno
        for node in ast.walk(attach)
        if isinstance(node, ast.Name) and node.id == "publish_preflight_after_commit"
    ]

    assert commit_lines
    assert publish_lines
    assert max(commit_lines) < min(publish_lines)
