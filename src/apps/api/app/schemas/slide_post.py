"""Pydantic schema for `PlanItem.slide_post` — the reviewable mixed-media
"slide post" draft (an ordered sequence of images and videos, e.g. a TikTok
photo-mode post or an Instagram carousel).

Deliberately a SEPARATE column and a separate, lighter-weight envelope from
`app/schemas/edit_proposal.py`, even though both are "draft + approval state
+ staleness" documents (plans/024 eng-review). Reusing `edit_proposal` would
entangle a slide draft with `guided_edit_applicable`'s media-sync/staleness
machinery, which exists specifically for the guided-story renderer — a slide
post must never be able to trip that path. What IS borrowed from
`edit_proposal` is the *shape* of the verbs: a monotonic `version` on the
draft, a `rendered_version` marker recording which version the current
render was built from (the `approved`/staleness signal), and a fail-closed
`parse_slide_post` matching `parse_edit_proposal`.

A slide never carries a raw GCS path — it references a `PlanItemAsset.id`.
The asset row (already promoted, already analyzed) is the source of truth
for the actual media; see `app/pipeline/slide_post/build.py` for how a slide
is resolved to bytes at render time.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, model_validator

from app.pipeline.slide_post.profiles import PlatformProfile

# Hard outer bound independent of platform profile — the tightest profile
# (tiktok_photo, 35) is looser than this only by convention; this cap exists
# so a malformed/oversized draft can never reach the profile validator or the
# renderer. Profile-specific limits (e.g. instagram_carousel's 20) are
# enforced by `app.pipeline.slide_post.profiles.validate`, not here.
MAX_SLIDES = 35


class SlideRef(BaseModel):
    """One item in the ordered sequence. Points at pooled media, never copies it."""

    # Client-stable id (not the asset id) so the frontend can key a reorder
    # without re-fetching — minted once when the slide is added to the draft.
    id: str = Field(min_length=1, max_length=64)
    asset_id: uuid.UUID
    kind: str = Field(pattern="^(image|video)$")
    # Per-slide accessibility / caption text. Optional — the composer agent
    # fills it; the user may edit or clear it.
    alt: str | None = Field(default=None, max_length=500)


class SlidePostDraft(BaseModel):
    """The persisted draft document (`PlanItem.slide_post`)."""

    schema_version: int = 1
    # Bumped on every edit (reorder, add, remove, cover/caption/profile
    # change). Compared against `rendered_version` to decide staleness —
    # mirrors `voiceover_script.version` vs `voiceover_script_recorded_version`.
    version: int = Field(ge=1, default=1)
    platform_profile: PlatformProfile
    slides: list[SlideRef] = Field(default_factory=list, max_length=MAX_SLIDES)
    cover_index: int = Field(ge=0, default=0)
    caption: str = Field(default="", max_length=2200)  # Instagram's own cap
    # The `version` the currently-rendered `slides` variant was built from.
    # None before the first successful render. draft is stale (needs a
    # rebuild) whenever `version != rendered_version`.
    rendered_version: int | None = None
    # False for an untouched AI (composer) draft; True the moment the user
    # changes anything. Display-only — never gates rendering.
    user_edited: bool = False

    @model_validator(mode="after")
    def _validate_cover_and_ids(self) -> SlidePostDraft:
        if self.slides:
            if self.cover_index >= len(self.slides):
                raise ValueError("cover_index out of range")
            ids = [s.id for s in self.slides]
            if len(set(ids)) != len(ids):
                raise ValueError("slide ids must be unique within a draft")
        elif self.cover_index != 0:
            raise ValueError("cover_index must be 0 when there are no slides")
        return self


def parse_slide_post(value: object) -> SlidePostDraft | None:
    """Fail closed for legacy/corrupt JSONB instead of breaking item reads."""

    if not isinstance(value, dict):
        return None
    try:
        return SlidePostDraft.model_validate(value)
    except Exception:  # noqa: BLE001 - corrupted JSONB is treated as no draft
        return None


def slide_post_needs_render(draft: SlidePostDraft) -> bool:
    """True when the draft has changed since the last successful render."""

    return draft.rendered_version != draft.version


def mark_slide_post_rendered(draft: SlidePostDraft) -> SlidePostDraft:
    """Stamp the draft as rendered at its current version (post-render write)."""

    return draft.model_copy(update={"rendered_version": draft.version})


def bump_slide_post_version(draft: SlidePostDraft, **updates: object) -> SlidePostDraft:
    """Apply `updates` and mark the draft user-edited at the next version.

    Single mutation point for every draft edit (reorder/add/remove/cover/
    caption/profile) so `version` and `user_edited` can never drift from an
    actual change, mirroring how `plan_clips.set_item_clips` is the single
    writer for `clip_gcs_paths`.
    """

    return draft.model_copy(update={**updates, "version": draft.version + 1, "user_edited": True})
