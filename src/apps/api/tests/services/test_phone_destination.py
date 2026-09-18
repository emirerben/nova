"""KRI-121 round 2: a pilot project holding only Visuals renders on the iPhone."""

import uuid
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services import phone_destination as destination
from app.services.creator_sessions import CREATOR_VISIBLE_ASSET_STATES
from app.services.phone_destination import (
    DEVICE_INTENT_KEY,
    has_device_intent,
    item_visuals_only_on_device,
    item_visuals_only_on_device_sync,
    visuals_only_on_device,
    with_device_intent,
)

USER = uuid.UUID("0b6b1c52-3f0e-4c7a-9d51-6f1e2a3b4c5d")
OTHER = uuid.UUID("9d8c7b6a-5f4e-4d3c-8b2a-1f0e9d8c7b6a")
STAMPED = {"media": [], DEVICE_INTENT_KEY: "device"}


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [USER])
    monkeypatch.setattr(
        settings, "phone_render_verified_features", ["audioMix", "stillImages", "visualVideos"]
    )


def decide(**changes):
    return visuals_only_on_device(
        **(
            {
                "user_id": USER,
                "edit_format": "montage",
                "has_voiceover": False,
                "has_clip_sources": False,
                "pool_kinds": ["image"],
                "thread_state": STAMPED,
            }
            | changes
        )
    )


@pytest.mark.parametrize("edit_format", ["montage", "day_vlog", "single_hero", None])
@pytest.mark.parametrize("pool_kinds", [["image"], ["video"], ["image", "video"]])
def test_a_stamped_pilot_project_with_only_drawable_visuals_renders_on_device(
    pilot, edit_format, pool_kinds
):
    assert decide(edit_format=edit_format, pool_kinds=pool_kinds)


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": OTHER},
        {"thread_state": {"media": []}},
        {"thread_state": None},
        {"thread_state": {DEVICE_INTENT_KEY: "cloud"}},
        {"has_clip_sources": True},
        {"has_voiceover": True},
        {"edit_format": "talking_head"},
        {"edit_format": "subtitled"},
        {"edit_format": "slides"},
        {"edit_format": "some_future_format"},
        {"pool_kinds": []},
    ],
    ids=[
        "outside_the_cohort",
        "web_created_thread",
        "no_thread",
        "other_intent",
        "footage_in_the_clip_lane",
        "voiceover",
        "talking_head",
        "subtitled",
        "slides",
        "unknown_format",
        "no_visuals",
    ],
)
def test_everything_else_keeps_todays_route(pilot, changes):
    assert not decide(**changes)


def test_kill_switch_and_unverified_kinds_keep_todays_route(pilot, monkeypatch):
    monkeypatch.setattr(settings, "phone_render_verified_features", ["stillImages"])
    assert decide(pool_kinds=["image", "video"])
    assert not decide(pool_kinds=["video"])
    monkeypatch.setattr(settings, "phone_render_verified_features", ["visualVideos"])
    assert not decide(pool_kinds=["image"])
    monkeypatch.setattr(settings, "phone_render_verified_features", ["audioMix"])
    assert not decide(pool_kinds=["image", "video"])
    monkeypatch.setattr(settings, "phone_render_verified_features", ["stillImages", "visualVideos"])
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    assert not decide()


@pytest.mark.parametrize(
    "native_client, user_id, stamped",
    [(True, USER, True), (False, USER, False), (True, OTHER, False), (False, OTHER, False)],
    ids=["native_pilot", "web_pilot", "native_outsider", "web_outsider"],
)
def test_only_the_native_app_on_a_pilot_account_stamps(pilot, native_client, user_id, stamped):
    state = {"media": [], "media_count": 0}
    result = with_device_intent(state, native_client=native_client, user_id=user_id)
    assert (result is not None) == stamped
    assert state == {"media": [], "media_count": 0}
    if stamped:
        assert result == state | {DEVICE_INTENT_KEY: "device"}
        assert has_device_intent(result)
        # Already stamped: nothing to write.
        assert with_device_intent(result, native_client=True, user_id=USER) is None


def test_stamp_tolerates_a_thread_without_state(pilot):
    assert with_device_intent(None, native_client=True, user_id=USER) == {
        DEVICE_INTENT_KEY: "device"
    }


def item(**changes):
    return SimpleNamespace(
        **(
            {
                "id": uuid.UUID("7c1d2e3f-4a5b-4c6d-8e7f-9a0b1c2d3e4f"),
                "edit_format": "montage",
                "audio_mode": "kria",
                "voiceover_gcs_path": None,
                "clip_gcs_paths": [],
                "clip_assignments": [],
            }
            | changes
        )
    )


class Reads:
    """Answer the loader's reads in order and keep the statements it sent."""

    def __init__(self, *results):
        self.results = list(results)
        self.statements = []

    def execute(self, statement):
        self.statements.append(str(statement))
        rows = self.results.pop(0)
        return SimpleNamespace(scalars=lambda: iter(rows))


class AsyncReads(Reads):
    async def execute(self, statement):  # type: ignore[override]
        return super().execute(statement)


def test_sync_loader_reads_the_thread_stamp_then_the_registered_visuals(pilot):
    db = Reads([{"media": []}, STAMPED], ["image"])
    assert item_visuals_only_on_device_sync(db, item(), USER)
    threads, pool = db.statements
    assert "creation_threads" in threads and "plan_item_assets" in pool
    # Callers may hold the item lock; the documented order is thread first, so
    # this must stay a plain read.
    assert "FOR UPDATE" not in threads.upper()


@pytest.mark.asyncio
async def test_async_loader_matches_the_sync_loader(pilot):
    assert await item_visuals_only_on_device(AsyncReads([STAMPED], ["video"]), item(), USER)
    assert not await item_visuals_only_on_device(AsyncReads([STAMPED], []), item(), USER)
    assert not await item_visuals_only_on_device(AsyncReads([{"media": []}]), item(), USER)


@pytest.mark.asyncio
async def test_a_locked_threads_own_state_replaces_the_thread_read(pilot):
    db = AsyncReads(["image"])
    assert await item_visuals_only_on_device(db, item(), USER, thread_state=STAMPED)
    assert len(db.statements) == 1
    assert not await item_visuals_only_on_device(
        AsyncReads(), item(), USER, thread_state={"media": []}
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"clip_gcs_paths": ["users/u/plan/i/clip.mp4"]},
        {"clip_assignments": [{"media_id": "clip-1", "gcs_path": "users/u/plan/i/clip.mp4"}]},
        {"audio_mode": "voiceover", "voiceover_gcs_path": "users/u/plan/i/voice.m4a"},
    ],
    ids=["clip_paths", "clip_assignments", "voiceover"],
)
def test_items_the_rule_can_never_accept_cost_no_query(pilot, changes):
    assert not item_visuals_only_on_device_sync(Reads(), item(**changes), USER)


def test_flags_off_costs_no_query(monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", False)
    assert not item_visuals_only_on_device_sync(Reads(), item(), USER)
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [USER])
    monkeypatch.setattr(settings, "phone_render_verified_features", ["audioMix"])
    assert not item_visuals_only_on_device_sync(Reads(), item(), USER)


def test_a_retained_voiceover_take_outside_voiceover_mode_does_not_block(pilot):
    retained = item(audio_mode="kria", voiceover_gcs_path="users/u/plan/i/voice.m4a")
    assert item_visuals_only_on_device_sync(Reads([STAMPED], ["image"]), retained, USER)


def test_registered_visuals_match_the_manifests_membership():
    # The manifest lists a Visual from registration on. Deciding on a narrower
    # set would flip the destination (and the manifest hash) mid-analysis.
    assert destination._REGISTERED_POOL_STATES == CREATOR_VISIBLE_ASSET_STATES
