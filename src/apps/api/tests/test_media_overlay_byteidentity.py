"""Byte-identity + kill-switch guards for media overlays (slice 1).

Guards:
- dispatch_set_media_overlays raises 404 when flag off.
- build_generative_job's all_candidates carries NO new key for this feature.
- _run_regenerate_variant with media_overlays_override=None does not reach the
  overlay branch (structural guard, no ffmpeg invocation).
- Storage prefix: overlay assets land under users/.../overlays/.
"""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


class TestKillSwitch:
    """dispatch_set_media_overlays must 404 when media_overlays_enabled is False."""

    def test_dispatch_raises_when_flag_off(self):
        from app.routes.generative_jobs import dispatch_set_media_overlays

        mock_job = MagicMock()
        mock_job.assembly_plan = {"variants": [{"variant_id": "v1", "render_status": "ready"}]}
        mock_job.id = "00000000-0000-0000-0000-000000000001"

        # Default settings has media_overlays_enabled=False — no patch needed.
        with pytest.raises(HTTPException) as exc_info:
            dispatch_set_media_overlays(mock_job, "v1", overlays_raw=[], user_id="u123")
        assert exc_info.value.status_code == 404


class TestStoragePrefix:
    def test_overlay_path_under_users_prefix(self):
        """presigned_put_url_for_media_overlay produces a users/ path."""
        mock_bucket = MagicMock()
        mock_blob = MagicMock()
        mock_blob.generate_signed_url.return_value = "https://signed/"
        mock_bucket.blob.return_value = mock_blob

        with patch("app.storage._get_client") as mock_client:
            mock_client.return_value.bucket.return_value = mock_bucket
            from app.storage import presigned_put_url_for_media_overlay

            _, gcs_path = presigned_put_url_for_media_overlay(
                user_id="u123",
                plan_item_id="item456",
                filename="card.png",
                content_type="image/png",
            )

        assert gcs_path.startswith("users/"), f"Expected users/ prefix, got: {gcs_path}"
        assert "overlays/" in gcs_path, f"Expected overlays/ in path: {gcs_path}"
        assert "u123" in gcs_path
        assert "item456" in gcs_path

    def test_cross_user_path_rejected_by_dispatch(self):
        """dispatch_set_media_overlays must reject paths belonging to another user."""
        from unittest.mock import MagicMock, patch

        from app.routes.generative_jobs import dispatch_set_media_overlays

        mock_job = MagicMock()
        mock_job.assembly_plan = {"variants": [{"variant_id": "v1", "render_status": "ready"}]}
        mock_job.id = "00000000-0000-0000-0000-000000000001"

        with patch("app.config.settings") as mock_settings:
            mock_settings.media_overlays_enabled = True
            # Overlay path under a DIFFERENT user's prefix.
            overlay = {
                "id": "card1",
                "kind": "image",
                "src_gcs_path": "users/OTHER_USER/plan/item/overlays/stolen.png",
                "position": "center",
                "scale": 0.35,
                "start_s": 0.0,
                "end_s": 3.0,
                "z": 0,
            }
            with pytest.raises(HTTPException) as exc_info:
                dispatch_set_media_overlays(
                    mock_job, "v1", overlays_raw=[overlay], user_id="REQUESTING_USER"
                )
            assert exc_info.value.status_code == 422


class TestVariantFinalizeKeys:
    """The variant finalize dict must include the new null-default keys."""

    def test_montage_finalize_has_media_overlay_keys(self):
        """Verify _render_generative_variant includes the new keys."""
        # Resolve relative to this test file's location.
        repo_root = pathlib.Path(__file__).parent.parent.parent.parent.parent
        src = (repo_root / "src/apps/api/app/tasks/generative_build.py").read_text()
        tree = ast.parse(src)

        # OV-2 (plan 010): _update_variant_entry MERGE patches (locals named
        # `patch`) deliberately omit media_overlays — the row-locked merge keeps
        # the DB's current cards, so naming the key would round-trip a stale
        # task-start snapshot (the save-during-render wipe). This guard applies
        # only to finalize/result dicts, which _finalize_job rebuilds through a
        # whitelist that strips unnamed keys.
        merge_patch_dicts: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Dict):
                targets = [node.target]
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == "patch" for t in targets):
                merge_patch_dicts.add(id(node.value))

        # Find all dict literals that contain "base_video_path" (the finalize dicts)
        # and check that "media_overlays" and "pre_media_overlay_video_path" are also present.
        found_base = 0
        found_overlay = 0
        found_pre_overlay = 0

        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict) or id(node) in merge_patch_dicts:
                continue
            keys = [k.value if isinstance(k, ast.Constant) else None for k in node.keys]
            if "base_video_path" in keys:
                found_base += 1
                if "media_overlays" in keys:
                    found_overlay += 1
                if "pre_media_overlay_video_path" in keys:
                    found_pre_overlay += 1

        assert found_base >= 1, "No finalize dict with base_video_path found"
        assert found_overlay >= found_base, (
            f"Not all finalize dicts ({found_base}) carry 'media_overlays' ({found_overlay})"
        )
        assert found_pre_overlay >= found_base, (
            f"Not all finalize dicts ({found_base}) carry"
            f" 'pre_media_overlay_video_path' ({found_pre_overlay})"
        )
