from types import SimpleNamespace

from app.services.creator_direction import CreatorDirectionSnapshot
from app.services.creator_direction_snapshot import (
    SNAPSHOT_KEY,
    apply_direction_overrides,
    attach_private_snapshot,
    attach_snapshot,
    current_typed_overrides,
    ensure_job_snapshot,
    ensure_job_snapshot_async,
    renderer_policy_scope,
    resolve_snapshot_for_dispatch,
    serialize_private_snapshot,
    serialize_snapshot,
)
from app.services.public_assembly_plan import project_public_assembly_plan
from app.tasks.content_plan_build import _plan_direction_prompt


def _resolved(*, shadow: bool = False) -> CreatorDirectionSnapshot:
    return CreatorDirectionSnapshot(
        enabled=True,
        revision=4,
        items=(
            {
                "id": "memory-1",
                "instruction": "Use the same look every time",
                "structured_value": {"shadow_enabled": shadow},
            },
        ),
    )


def test_snapshot_serialization_is_redacted_and_versioned():
    result = serialize_snapshot(_resolved(shadow=False), source="test")

    assert result["schema"] == "CreatorDirectionSnapshotV1"
    assert result["memory_revision"] == 4
    assert result["applied_item_ids"] == ["memory-1"]
    assert result["typed_overrides"] == {"shadow_enabled": False}
    assert "Use the same look" not in str(result)


def test_attach_snapshot_reuses_immutable_generation_snapshot():
    first = attach_snapshot({}, _resolved(), source="dispatch")
    changed = attach_snapshot(
        first,
        CreatorDirectionSnapshot(enabled=True, revision=99, items=()),
        source="retry",
    )

    assert changed[SNAPSHOT_KEY] == first[SNAPSHOT_KEY]
    assert changed[SNAPSHOT_KEY]["memory_revision"] == 4


def test_shadow_policy_is_applied_before_either_renderer_payload():
    overlays = [{"text": "Hello", "shadow_enabled": True}, {"text": "World"}]

    result = apply_direction_overrides(overlays, typed_overrides={"shadow_enabled": False})

    assert result == [
        {"text": "Hello", "shadow_enabled": False},
        {"text": "World", "shadow_enabled": False},
    ]
    assert overlays[0]["shadow_enabled"] is True


def test_font_policy_disables_font_cycling_before_renderer_payload():
    overlays = [{"text": "Hello", "font_family": "Other", "cycle_fonts": ["A", "B"]}]

    result = apply_direction_overrides(overlays, typed_overrides={"font_family": "Inter"})

    assert result == [
        {
            "text": "Hello",
            "font_family": "Inter",
            "font_cycling": False,
            "cycle_fonts": [],
        }
    ]
    assert overlays[0]["font_family"] == "Other"


def test_renderer_policy_scope_does_not_leak_between_jobs():
    overlays = [{"text": "Hello", "font_family": "Other", "shadow_enabled": True}]

    with renderer_policy_scope():
        assert (
            apply_direction_overrides(
                overlays, typed_overrides={"font_family": "Inter", "shadow_enabled": False}
            )[0]["font_family"]
            == "Inter"
        )

    assert current_typed_overrides() == {}
    assert apply_direction_overrides(overlays) == overlays


def test_snapshot_is_private_from_job_assembly_projection():
    snapshot = serialize_snapshot(_resolved(), source="test")

    assert "_creator_direction_snapshot_v1" not in project_public_assembly_plan(
        {"_creator_direction_snapshot_v1": snapshot, "output_url": None}
    )


def test_private_snapshot_keeps_prompt_but_public_job_projection_does_not():
    snapshot = serialize_private_snapshot(_resolved(), source="thread")

    assert snapshot["prompt_block"] == "- Use the same look every time"
    assert "prompt_block" not in project_public_assembly_plan(
        {"_creator_direction_snapshot_v1": snapshot}
    )


def test_private_snapshot_upgrades_public_dispatch_snapshot_for_worker_prompt():
    public = serialize_snapshot(_resolved(), source="dispatch")

    private = attach_private_snapshot(
        {SNAPSHOT_KEY: public},
        _resolved(),
        source="dispatch",
        generation_id="generation-1",
    )

    assert private["prompt_block"] == "- Use the same look every time"
    assert private["generation_id"] == "generation-1"
    assert private["typed_overrides"] == {"shadow_enabled": False}


async def test_async_job_dispatch_pins_private_prompt_for_direct_generators(monkeypatch):
    import app.services.creator_direction_snapshot as snapshot_service

    async def _resolve(_db, _user_id, *, thread_id=None):
        return _resolved()

    monkeypatch.setattr(snapshot_service, "resolve_snapshot", _resolve)
    job = SimpleNamespace(user_id="user-1", assembly_plan={})

    result = await ensure_job_snapshot_async(object(), job, source="generative_dispatch")

    assert result["prompt_block"] == "- Use the same look every time"
    assert job.assembly_plan[SNAPSHOT_KEY] == result


def test_content_plan_worker_reads_pinned_prompt_only():
    class Plan:
        creator_direction_snapshot = {"prompt_block": "- Use the pinned direction"}

    assert _plan_direction_prompt(Plan()) == "- Use the pinned direction"


def test_legacy_job_snapshot_fails_open_when_memory_storage_is_unavailable():
    class UnavailableDatabase:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("mixed-version schema")

    job = SimpleNamespace(user_id="legacy-user", assembly_plan={})

    snapshot = ensure_job_snapshot(UnavailableDatabase(), job, source="legacy_worker")

    assert snapshot["enabled"] is False
    assert snapshot["typed_overrides"] == {}
    assert job.assembly_plan[SNAPSHOT_KEY] == snapshot


def test_inherited_private_snapshot_wins_over_live_memory_resolution():
    class UnavailableDatabase:
        def get(self, *_args, **_kwargs):
            raise AssertionError("inherited snapshots must not resolve live memory")

    inherited = serialize_private_snapshot(_resolved(shadow=True), source="plan_dispatch")
    job = SimpleNamespace(user_id="user-b", assembly_plan={})

    result = ensure_job_snapshot(
        UnavailableDatabase(),
        job,
        source="content_plan_dispatch",
        inherited_snapshot=inherited,
    )

    assert result == inherited
    assert job.assembly_plan[SNAPSHOT_KEY] == inherited


def test_renderer_policy_scope_resets_between_sequential_jobs():
    first = serialize_private_snapshot(_resolved(shadow=False), source="first")
    second = serialize_private_snapshot(_resolved(shadow=True), source="second")

    with renderer_policy_scope():
        bind_job = SimpleNamespace(user_id="first", assembly_plan={SNAPSHOT_KEY: first})
        ensure_job_snapshot(object(), bind_job, source="first")
        assert current_typed_overrides() == {"shadow_enabled": False}
    assert current_typed_overrides() == {}

    with renderer_policy_scope():
        bind_job = SimpleNamespace(user_id="second", assembly_plan={SNAPSHOT_KEY: second})
        ensure_job_snapshot(object(), bind_job, source="second")
        assert current_typed_overrides() == {"shadow_enabled": True}
    assert current_typed_overrides() == {}


async def test_non_async_dispatch_database_uses_an_empty_snapshot():
    snapshot = await resolve_snapshot_for_dispatch(object(), "legacy-user")

    assert snapshot.enabled is False
    assert snapshot.items == ()
