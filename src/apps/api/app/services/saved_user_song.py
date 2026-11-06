"""Read-only projection of durable guided song edits, including failed renders."""

from app.schemas.guided_edit_revision import normalize_guided_editor_revision
from app.schemas.saved_user_song import SavedUserSong


def saved_user_song_for_variant(job, visible_variant: dict) -> dict | None:
    assembly = job.assembly_plan or {}
    raw = next(
        (
            v
            for v in assembly.get("variants", [])
            if isinstance(v, dict) and v.get("variant_id") == visible_variant.get("variant_id")
        ),
        None,
    )
    if (
        not raw
        or raw.get("resolved_archetype") != "guided_story"
        or raw.get("render_destination") != "device"
    ):
        return None
    generation = raw.get("render_generation_id") or raw.get("render_finished_at")
    visible_generation = visible_variant.get("render_generation_id") or visible_variant.get(
        "render_finished_at"
    )
    if not generation or generation != visible_generation:
        return None
    persisted = raw.get("guided_edit_revision")
    approval = assembly.get("guided_edit")
    if (
        not isinstance(persisted, dict)
        or not isinstance(approval, dict)
        or not isinstance(persisted.get("user_song"), dict)
    ):
        return None
    try:
        revision = normalize_guided_editor_revision(
            persisted,
            expected_approval_version=int(approval.get("proposal_version") or 0),
            expected_media_digest=str(approval.get("media_digest") or ""),
        )
        return SavedUserSong(
            job_id=job.id,
            variant_id=raw["variant_id"],
            generation=generation,
            revision_number=revision["revision_number"],
            state=revision["user_song"],
        ).model_dump(mode="json")
    except (ValueError, TypeError, KeyError):
        return None
