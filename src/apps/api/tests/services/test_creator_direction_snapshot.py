from types import SimpleNamespace

import pytest

from app.models import CreatorMemoryItem, Persona, ProjectDirectionOverride
from app.services.creator_direction import CreatorDirectionResolver, CreatorDirectionSnapshot
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
    resolve_snapshot_sync,
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


def _rich_resolved() -> CreatorDirectionSnapshot:
    return CreatorDirectionSnapshot(
        enabled=True,
        revision=7,
        items=(
            {
                "id": "memory-raw",
                "instruction": "Private active direction text",
                "normalized_key": "text_color",
                "enforcement": "constraint",
                "structured_value": {"text_color": "#112233"},
                "source_kind": "creation_thread",
            },
        ),
        overrides=(
            {
                "id": "override-1",
                "instruction": "Project-only font direction",
                "normalized_key": "font_family",
                "enforcement": "default",
                "structured_value": {"font_family": "Montserrat"},
                "conflict_id": "conflict-1",
            },
        ),
        compatibility_items=(
            {
                "id": "compatibility-style-font",
                "instruction": "Private compatibility style text",
                "normalized_key": "font_family",
                "enforcement": "default",
                "structured_value": {"font_family": "Inter"},
                "source_kind": "persona_style",
            },
            {
                "id": "compatibility-style-highlight",
                "instruction": "Existing style preference: highlight_color=#ffffff",
                "normalized_key": "highlight_color",
                "enforcement": "default",
                "structured_value": {"highlight_color": "#ffffff"},
                "source_kind": "persona_style",
            },
        ),
    )


class _AwaitableResult:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows
        self._scalar = scalar

    def __await__(self):
        async def _return_self():
            return self

        return _return_self().__await__()

    @property
    def scalars(self):
        return _ScalarRows(self._rows)

    def scalar_one_or_none(self):
        return self._scalar


class _AwaitableValue:
    def __init__(self, value):
        self.value = value

    def __await__(self):
        async def _return_value():
            return self.value

        return _return_value().__await__()

    def __getattr__(self, name):
        return getattr(self.value, name)


class _ScalarRows:
    def __init__(self, rows):
        self.rows = list(rows or ())

    def __call__(self):
        return self

    def __iter__(self):
        return iter(self.rows)

    def all(self):
        return self.rows


class _SeededDirectionDB:
    def __init__(self):
        self.user = SimpleNamespace(
            creator_memory_enabled=True,
            creator_memory_revision=12,
        )
        self.items = [
            SimpleNamespace(
                id="memory-1",
                category="video_style",
                normalized_key="text_color",
                instruction="Use the seeded active color",
                enforcement="constraint",
                structured_value={"text_color": "#112233"},
                source_kind="creation_thread",
                source_thread_id="thread-1",
                state="active",
                user_locked=True,
                confidence=1.0,
                updated_at="2026-01-01T00:00:00+00:00",
            )
        ]
        self.persona = SimpleNamespace(
            style={
                "style_set_id": "default",
                "knobs": {
                    "font_family": "Inter",
                    "highlight_color": "#ffffff",
                },
            }
        )
        self.overrides = [
            SimpleNamespace(
                id="override-1",
                normalized_key="font_family",
                instruction="Use Montserrat for this project",
                structured_value={"font_family": "Montserrat"},
            )
        ]

    def get(self, _model, _user_id):
        return _AwaitableValue(self.user)

    def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is CreatorMemoryItem:
            return _AwaitableResult(rows=self.items)
        if entity is Persona:
            return _AwaitableResult(scalar=self.persona)
        if entity is ProjectDirectionOverride:
            return _AwaitableResult(rows=self.overrides)
        raise AssertionError(f"unexpected resolver entity: {entity!r}")


@pytest.fixture
def seeded_direction_db():
    return _SeededDirectionDB()


@pytest.mark.asyncio
async def test_sync_and_async_resolvers_have_identical_seeded_projection(seeded_direction_db):
    thread_id = "thread-1"

    async_snapshot = await CreatorDirectionResolver().snapshot(
        seeded_direction_db,
        "user-1",
        thread_id=thread_id,
    )
    sync_snapshot = resolve_snapshot_sync(
        seeded_direction_db,
        "user-1",
        thread_id=thread_id,
    )

    assert async_snapshot.enabled == sync_snapshot.enabled
    assert async_snapshot.revision == sync_snapshot.revision
    assert async_snapshot.compatibility_items == sync_snapshot.compatibility_items
    assert async_snapshot.overrides == sync_snapshot.overrides
    assert async_snapshot.context_sections == sync_snapshot.context_sections
    assert async_snapshot.prompt_block == sync_snapshot.prompt_block
    assert async_snapshot.typed_overrides == sync_snapshot.typed_overrides
    assert async_snapshot.overrides[0]["enforcement"] == "default"
    project_result = next(
        row
        for row in serialize_snapshot(async_snapshot, source="test")["capability_results"]
        if row["scope"] == "project"
    )
    assert project_result["status"] == "enforced"
    assert project_result["enforcement"] == "default"


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


def test_snapshot_v1_persists_receipts_but_private_only_keeps_context_text():
    snapshot = _rich_resolved()

    public = serialize_snapshot(snapshot, source="dispatch")
    private = serialize_private_snapshot(snapshot, source="dispatch")

    assert public["compatibility_item_ids"] == [
        "compatibility-style-font",
        "compatibility-style-highlight",
    ]
    assert public["project_override_values"] == {"font_family": "Montserrat"}
    assert public["conflict_ids"] == ["conflict-1"]
    assert {row["scope"] for row in public["capability_results"]} == {
        "account",
        "compatibility",
        "project",
    }
    assert public["context_sections"]["creator_context"]["item_ids"] == [
        "compatibility-style-highlight"
    ]
    assert "Private active direction text" not in str(public)
    assert "Private compatibility style text" not in str(public)
    assert "Private active direction text" in private["prompt_block"]
    assert "highlight_color=#ffffff" in str(private["context_sections"])
    assert private["context_sections"]["creator_context"] == [
        "Existing style preference: highlight_color=#ffffff"
    ]


def test_private_snapshot_reuse_preserves_v1_receipts_and_redaction():
    first = attach_private_snapshot({}, _rich_resolved(), source="dispatch")
    changed = attach_private_snapshot(
        {SNAPSHOT_KEY: first},
        CreatorDirectionSnapshot(enabled=True, revision=99, items=()),
        source="retry",
    )

    assert changed == first
    assert changed["memory_revision"] == 7
    assert changed["compatibility_input_version"] == "persona-style-v1"
    assert "Private active direction text" not in str(
        project_public_assembly_plan({SNAPSHOT_KEY: changed})
    )


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
