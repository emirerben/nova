"""Unit test for persona threading in generate_plan_item_videos (mock DB).

Locks that the per-item plan task loads the creator's persona + the item's
theme/idea and forwards them to the shared build_generative_job — the data path
that makes content-plan hooks persona-coherent (intro_writer threading).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.agents._schemas.content_plan import PlanItemSpec
from app.models import ContentPlan, PlanItem
from app.models import Persona as PersonaRow
from app.tasks.content_plan_build import (
    generate_content_plan,
    generate_plan_item_videos,
    regenerate_content_plan,
)


def _session_with(item, plan, persona_row) -> MagicMock:
    session = MagicMock()

    def _get(model, _pk):
        return {PlanItem: item, ContentPlan: plan, PersonaRow: persona_row}.get(model)

    session.get = MagicMock(side_effect=_get)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_persona_forwarded_to_build_generative_job() -> None:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "first 5am workout"
    item.idea = "film the dark early start"

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {
        "tone": "no-excuses gym motivation",
        "content_pillars": ["morning routines", "discipline"],
    }

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, persona_row)
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    mock_build.assert_called_once()
    kwargs = mock_build.call_args.kwargs
    assert kwargs["mode"] == "content_plan"
    assert kwargs["persona_tone"] == "no-excuses gym motivation"
    assert kwargs["persona_pillars"] == ["morning routines", "discipline"]
    assert kwargs["item_theme"] == "first 5am workout"
    assert kwargs["item_idea"] == "film the dark early start"


def test_missing_persona_falls_back_to_empty() -> None:
    # A plan item whose persona row is gone must NOT block the render — the task
    # passes empty persona fields and the builder omits the key downstream.
    item = MagicMock()
    item.id = uuid.uuid4()
    item.content_plan_id = uuid.uuid4()
    item.clip_gcs_paths = ["users/u/plan/i/a.mp4"]
    item.theme = "first 5am workout"
    item.idea = ""

    plan = MagicMock()
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()

    job = MagicMock()
    job.id = uuid.uuid4()

    ctx = _session_with(item, plan, None)  # persona row missing
    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.services.generative_jobs.build_generative_job", return_value=job) as mock_build,
        patch("app.services.job_dispatch.enqueue_orchestrator_sync"),
    ):
        generate_plan_item_videos.run(str(item.id))

    kwargs = mock_build.call_args.kwargs
    assert kwargs["persona_tone"] == ""
    assert kwargs["persona_pillars"] == []
    assert kwargs["item_theme"] == "first 5am workout"


# ---- post-generation near-duplicate dedup (_dedup_and_replace) --------------

from app.agents._schemas.content_plan import ContentPlanInput, ContentPlanOutput  # noqa: E402
from app.agents._schemas.persona import Persona  # noqa: E402
from app.tasks.content_plan_build import _dedup_and_replace  # noqa: E402


class _FakeAgent:
    """Stands in for ContentPlanGeneratorAgent: records calls, returns a canned
    regen output (or raises) so the dedup orchestration is testable with no LLM."""

    def __init__(self, regen=None, raises=False) -> None:  # noqa: ANN001
        self.calls = 0
        self._regen = regen
        self._raises = raises

    def run(self, agent_input, ctx):  # noqa: ANN001, ARG002
        self.calls += 1
        if self._raises:
            raise RuntimeError("regen boom")
        return self._regen


def _spec(day: int, idea: str, **kw) -> PlanItemSpec:  # noqa: ANN003
    return PlanItemSpec(day_index=day, theme=kw.pop("theme", "pillar"), idea=idea, **kw)


def _plan_input() -> ContentPlanInput:
    return ContentPlanInput(
        persona=Persona(
            summary="s",
            content_pillars=["a"],
            tone="warm",
            audience="x",
            posting_cadence="4/wk",
            sample_topics=["y"],
        ),
        horizon_days=3,
    )


def test_dedup_skips_regen_when_no_duplicates() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "tour of my favorite coffee shops"),
            _spec(2, "best hiking trails near me"),
            _spec(3, "how I meal prep for the week"),
        ]
    )
    agent = _FakeAgent(raises=True)  # would blow up if regen were attempted
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")
    assert agent.calls == 0  # no extra LLM call when the plan is already varied
    assert result is output


def test_dedup_replaces_duplicate_with_distinct_regen_idea() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "5am gym workout motivation routine"),
            _spec(2, "my favorite weekend brunch spots downtown"),
            _spec(3, "early morning gym workout motivation routine"),  # dup of day 1
        ]
    )
    regen = ContentPlanOutput(
        items=[
            _spec(
                7,
                "a guide to local hiking trails",
                theme="outdoors",
                rationale="save-worthy",
                edit_format="single_hero",
            ),
        ]
    )
    agent = _FakeAgent(regen=regen)
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")

    assert agent.calls == 1
    assert [it.day_index for it in result.items] == [1, 2, 3]  # full length, day kept
    day3 = next(it for it in result.items if it.day_index == 3)
    assert day3.idea == "a guide to local hiking trails"
    assert day3.edit_format == "single_hero"  # content fields carried from candidate
    assert day3.rationale == "save-worthy"


# ── T5 provenance: add_ideas_to_plan sets source_idea_seed_id + flips status ──


from app.tasks.content_plan_build import add_ideas_to_plan  # noqa: E402


def _make_add_ideas_sessions(plan, persona_row, new_specs):
    """Build the two sync_session context managers used by add_ideas_to_plan.

    Session 1: returns plan + persona_row (setup read).
    Session 2: returns a fresh plan mock + persona_row (persist write).
    """
    added_items = []

    # --- session 1 (read) ---
    session1 = MagicMock()
    plan1 = MagicMock()
    plan1.persona_id = plan.persona_id
    plan1.id = plan.id
    plan1.horizon_days = plan.horizon_days
    plan1.events = {}
    plan1.items = []
    plan1.plan_status = "ready"

    def _get1(model, _pk):
        return {ContentPlan: plan1, PersonaRow: persona_row}.get(model)

    session1.get = MagicMock(side_effect=_get1)
    ctx1 = MagicMock()
    ctx1.__enter__ = MagicMock(return_value=session1)
    ctx1.__exit__ = MagicMock(return_value=False)

    # --- session 2 (write) ---
    session2 = MagicMock()
    plan2 = MagicMock()
    plan2.persona_id = plan.persona_id
    plan2.id = plan.id
    plan2.plan_status = "ready"

    persona_row2 = MagicMock()
    persona_row2.idea_seeds = list(persona_row.idea_seeds)

    def _get2(model, _pk):
        return {ContentPlan: plan2, PersonaRow: persona_row2}.get(model)

    session2.get = MagicMock(side_effect=_get2)

    def _add(item):
        added_items.append(item)

    session2.add = MagicMock(side_effect=_add)
    ctx2 = MagicMock()
    ctx2.__enter__ = MagicMock(return_value=session2)
    ctx2.__exit__ = MagicMock(return_value=False)

    return [ctx1, ctx2], added_items, persona_row2


def test_add_ideas_sets_source_idea_seed_id() -> None:
    """add_ideas_to_plan writes source_idea_seed_id on the new PlanItem."""
    persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {
        "summary": "s",
        "content_pillars": ["sport"],
        "tone": "cool",
        "audience": "gen z",
        "posting_cadence": "daily",
        "sample_topics": ["football"],
    }
    persona_row.idea_seeds = [
        {"id": "seed_fb", "text": "Fenerbahce game", "status": "pending"},
    ]
    persona_row.style = None

    plan = MagicMock()
    plan.persona_id = persona_id
    plan.id = uuid.uuid4()
    plan.horizon_days = 30
    plan.events = {}
    plan.items = []

    # One spec closely matching the seed
    new_spec = PlanItemSpec(
        day_index=1,
        theme="Football",
        idea="Fenerbahce match day atmosphere",
        filming_guide=[],
    )
    agent_output = ContentPlanOutput(items=[new_spec])

    ctxs, added_items, persona_row2 = _make_add_ideas_sessions(plan, persona_row, [new_spec])
    ctx_iter = iter(ctxs)

    with (
        patch(
            "app.tasks.content_plan_build.sync_session",
            side_effect=lambda: next(ctx_iter),
        ),
        patch(
            "app.tasks.content_plan_build.ContentPlanGeneratorAgent",
        ) as mock_agent_cls,
    ):
        mock_agent_cls.return_value.run.return_value = agent_output
        add_ideas_to_plan.run(str(uuid.uuid4()))

    assert len(added_items) == 1
    assert added_items[0].source_idea_seed_id == "seed_fb"


def test_add_ideas_flips_seed_status_to_in_plan() -> None:
    """Matched seeds are flipped to in_plan so IdeasCard shows ✓ and re-submission is idempotent."""
    persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {
        "summary": "s",
        "content_pillars": ["running"],
        "tone": "energetic",
        "audience": "runners",
        "posting_cadence": "daily",
        "sample_topics": ["marathon"],
    }
    persona_row.idea_seeds = [
        {"id": "seed_run", "text": "morning run in London", "status": "pending"},
    ]
    persona_row.style = None

    plan = MagicMock()
    plan.persona_id = persona_id
    plan.id = uuid.uuid4()
    plan.horizon_days = 30
    plan.events = {}
    plan.items = []

    new_spec = PlanItemSpec(
        day_index=2,
        theme="Morning fitness",
        idea="early morning run along the Thames",
        filming_guide=[],
    )
    agent_output = ContentPlanOutput(items=[new_spec])

    ctxs, added_items, persona_row2 = _make_add_ideas_sessions(plan, persona_row, [new_spec])
    ctx_iter = iter(ctxs)

    with (
        patch(
            "app.tasks.content_plan_build.sync_session",
            side_effect=lambda: next(ctx_iter),
        ),
        patch(
            "app.tasks.content_plan_build.ContentPlanGeneratorAgent",
        ) as mock_agent_cls,
    ):
        mock_agent_cls.return_value.run.return_value = agent_output
        add_ideas_to_plan.run(str(uuid.uuid4()))

    # persona_row2.idea_seeds must have been reassigned with status flipped.
    updated_seeds = persona_row2.idea_seeds
    assert any(s.get("id") == "seed_run" and s.get("status") == "in_plan" for s in updated_seeds), (
        f"Seed not flipped to in_plan; got: {updated_seeds}"
    )


def test_add_ideas_unmatched_seed_stays_pending() -> None:
    """A seed that the model ignored stays pending (no false positive flip)."""
    persona_id = uuid.uuid4()

    persona_row = MagicMock()
    persona_row.persona = {
        "summary": "s",
        "content_pillars": ["cooking"],
        "tone": "casual",
        "audience": "foodies",
        "posting_cadence": "daily",
        "sample_topics": ["recipes"],
    }
    persona_row.idea_seeds = [
        {"id": "seed_run", "text": "morning run in London", "status": "pending"},
        {"id": "seed_food", "text": "pasta carbonara recipe", "status": "pending"},
    ]
    persona_row.style = None

    plan = MagicMock()
    plan.persona_id = persona_id
    plan.id = uuid.uuid4()
    plan.horizon_days = 30
    plan.events = {}
    plan.items = []

    # Only one seed in pending (status != in_plan) but we filter to both above.
    # The model only deepens "morning run" here; "pasta" gets no matching spec.
    new_spec = PlanItemSpec(
        day_index=3,
        theme="Morning run",
        idea="early morning run jog in London parks",
        filming_guide=[],
    )
    agent_output = ContentPlanOutput(items=[new_spec])

    ctxs, added_items, persona_row2 = _make_add_ideas_sessions(plan, persona_row, [new_spec])
    ctx_iter = iter(ctxs)

    with (
        patch(
            "app.tasks.content_plan_build.sync_session",
            side_effect=lambda: next(ctx_iter),
        ),
        patch(
            "app.tasks.content_plan_build.ContentPlanGeneratorAgent",
        ) as mock_agent_cls,
    ):
        mock_agent_cls.return_value.run.return_value = agent_output
        add_ideas_to_plan.run(str(uuid.uuid4()))

    updated = {s["id"]: s["status"] for s in persona_row2.idea_seeds if isinstance(s, dict)}
    assert updated.get("seed_run") == "in_plan"
    assert updated.get("seed_food") == "pending"


def test_dedup_keeps_original_when_regen_fails() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "5am gym workout motivation routine"),
            _spec(2, "early morning gym workout motivation routine"),  # dup
        ]
    )
    agent = _FakeAgent(raises=True)
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")
    assert result is output  # best-effort: a failed regen never degrades the plan


def test_dedup_keeps_original_slot_when_regen_has_no_distinct_idea() -> None:
    output = ContentPlanOutput(
        items=[
            _spec(1, "5am gym workout motivation routine"),
            _spec(2, "early morning gym workout motivation routine"),  # dup of day 1
        ]
    )
    # Regen only offers another near-dup → nothing distinct to swap in.
    regen = ContentPlanOutput(items=[_spec(9, "5am gym workout motivation session")])
    agent = _FakeAgent(regen=regen)
    result = _dedup_and_replace(agent, _plan_input(), output, "pid")
    assert [it.idea for it in result.items] == [
        "5am gym workout motivation routine",
        "early morning gym workout motivation routine",
    ]


# ── regenerate_content_plan: the "their say" invariant ────────────────────────


def _plan_item(day: int, *, user_edited: bool, current_job_id: uuid.UUID | None) -> MagicMock:
    it = MagicMock()
    it.day_index = day
    it.user_edited = user_edited  # explicit — a bare MagicMock attr is truthy
    it.current_job_id = current_job_id
    it.theme = f"old theme {day}"
    it.idea = f"old idea {day}"
    return it


def _valid_persona() -> dict:
    return {
        "summary": "you film calm morning routines",
        "content_pillars": ["mornings", "discipline"],
        "tone": "warm and steady",
        "audience": "people who want a calmer start",
        "posting_cadence": "3-4 posts/week",
        "sample_topics": ["sunrise walk"],
    }


def test_regenerate_preserves_user_edited_and_in_flight_items() -> None:
    """The load-bearing invariant: regenerate replaces ONLY a day that is neither
    hand-edited nor already rendering. Day 1 (user_edited) and day 3 (current_job)
    are kept verbatim; only day 2 is deleted and re-inserted from fresh AI output."""
    user_id = uuid.uuid4()
    edited = _plan_item(1, user_edited=True, current_job_id=None)
    regenerable = _plan_item(2, user_edited=False, current_job_id=None)
    in_flight = _plan_item(3, user_edited=False, current_job_id=uuid.uuid4())

    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.user_id = user_id
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = [edited, regenerable, in_flight]

    persona_row = MagicMock()
    persona_row.persona = _valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk: {ContentPlan: plan, PersonaRow: persona_row}.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    # Fresh AI output proposes all three days; only the regenerable one may land.
    output = MagicMock()
    output.items = [
        PlanItemSpec(day_index=1, theme="NEW 1", idea="new idea 1"),
        PlanItemSpec(day_index=2, theme="NEW 2", idea="new idea 2"),
        PlanItemSpec(day_index=3, theme="NEW 3", idea="new idea 3"),
    ]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.services.feedback_summary.rollup_user_feedback",
            return_value="liked: 3, disliked: 1",
        ),
    ):
        regenerate_content_plan.run(str(plan.id))

    # The feedback summary was persisted on the plan.
    assert plan.preference_summary == "liked: 3, disliked: 1"
    # ONLY the regenerable day-2 item was deleted (protected days untouched).
    deleted = [c.args[0] for c in session.delete.call_args_list]
    assert deleted == [regenerable]
    # ONLY a single new item was added, for day 2, from the fresh AI output.
    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 1
    assert added[0].day_index == 2
    assert added[0].theme == "NEW 2"


# ── filming_guide persistence in both copy blocks ─────────────────────────────


def _spec_with_guide() -> PlanItemSpec:
    """A PlanItemSpec with a non-empty filming_guide for persistence assertions."""
    from app.agents._schemas.content_plan import ShotSpec  # noqa: PLC0415

    return PlanItemSpec(
        day_index=1,
        theme="morning routine",
        idea="film the 5am gym start",
        filming_guide=[
            ShotSpec(what="creator lacing shoes", how="close-up", duration_s=5),
        ],
    )


def test_generate_persists_filming_guide() -> None:
    """generate_content_plan must pass filming_guide into the PlanItem row.

    Locks the persistence copy block so a missed filming_guide=[...] line is
    caught before reaching prod (where it would silently store [] on every item).
    """
    plan_id = uuid.uuid4()
    plan = MagicMock()
    plan.id = plan_id
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = []
    plan.plan_status = "generating"

    persona_row = MagicMock()
    persona_row.persona = _valid_persona()

    user = MagicMock()
    user.onboarding_status = "plan_ready"

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk: {
            ContentPlan: plan,
            PersonaRow: persona_row,
            __import__("app.models", fromlist=["User"]).User: user,
        }.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    spec = _spec_with_guide()
    output = MagicMock()
    output.items = [spec]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch("app.tasks.content_plan_build._dedup_and_replace", return_value=output),
    ):
        from app.tasks.content_plan_build import generate_content_plan  # noqa: PLC0415

        generate_content_plan.run(str(plan_id))

    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 1
    persisted_guide = added[0].filming_guide
    assert isinstance(persisted_guide, list)
    assert len(persisted_guide) == 1
    assert persisted_guide[0]["what"] == "creator lacing shoes"
    assert persisted_guide[0]["duration_s"] == 5


def test_regenerate_persists_filming_guide() -> None:
    """regenerate_content_plan must pass filming_guide into the PlanItem row.

    This is the second copy block — both must be updated or regenerated plans
    silently lose their filming guides.
    """
    plan_id = uuid.uuid4()
    regenerable = _plan_item(2, user_edited=False, current_job_id=None)

    plan = MagicMock()
    plan.id = plan_id
    plan.user_id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = [regenerable]

    persona_row = MagicMock()
    persona_row.persona = _valid_persona()

    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk: {ContentPlan: plan, PersonaRow: persona_row}.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    spec = _spec_with_guide()
    output = MagicMock()
    output.items = [spec]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.services.feedback_summary.rollup_user_feedback",
            return_value="",
        ),
    ):
        regenerate_content_plan.run(str(plan_id))

    added = [c.args[0] for c in session.add.call_args_list]
    assert len(added) == 1
    persisted_guide = added[0].filming_guide
    assert isinstance(persisted_guide, list)
    assert len(persisted_guide) == 1
    assert persisted_guide[0]["what"] == "creator lacing shoes"


# ── posts_per_week flows from persona JSONB into ContentPlanInput ─────────────


def test_persona_posts_per_week_reaches_plan_input() -> None:
    """Persona JSONB with posts_per_week=3 must produce a Persona with posts_per_week=3
    inside ContentPlanInput, which the plan generator then uses to derive the cap.

    This is the task-boundary test: `Persona(**persona_row.persona)` in
    generate_content_plan / regenerate_content_plan must forward the new key
    without any extra code, because Persona validates via **kwargs.
    """
    from app.agents._schemas.content_plan import ContentPlanInput  # noqa: PLC0415
    from app.agents._schemas.persona import Persona  # noqa: PLC0415

    persona_dict = {
        "summary": "you film calm morning routines",
        "content_pillars": ["mornings", "discipline"],
        "tone": "warm and steady",
        "audience": "people who want a calmer start",
        "posting_cadence": "3-4 posts/week",
        "posts_per_week": 3,
        "sample_topics": ["sunrise walk"],
    }
    # Simulate what generate_content_plan / regenerate_content_plan does.
    persona = Persona(**persona_dict)
    assert persona.posts_per_week == 3

    plan_input = ContentPlanInput(persona=persona, horizon_days=30)
    assert plan_input.persona.posts_per_week == 3


# ── M1 idea_seeds plumbing: persona.idea_seeds[].text → ContentPlanInput ─────


def _gen_session(plan, persona_row) -> MagicMock:
    """sync_session context mock for generate_content_plan."""
    session = MagicMock()
    session.get = MagicMock(
        side_effect=lambda model, _pk: {ContentPlan: plan, PersonaRow: persona_row}.get(model)
    )
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def test_idea_seeds_reach_content_plan_input() -> None:
    """persona_row.idea_seeds[].text must flow into ContentPlanInput.user_idea_seeds.

    Guards the silent-default regression: if the extraction is dropped or the
    field name drifts, user ideas are silently ignored and every plan falls back
    to the market IDEA_BANK. Blank seeds must be filtered out.
    """
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.plan_status = "generating"

    persona_row = MagicMock()
    persona_row.persona = {
        "summary": "tech founder sharing behind-the-scenes",
        "content_pillars": ["building", "founder life"],
        "tone": "honest and direct",
        "audience": "indie hackers",
        "posting_cadence": "3 posts/week",
    }
    persona_row.idea_seeds = [
        {"id": "a1", "text": "behind-the-scenes of my launch week", "status": "pending"},
        {"id": "a2", "text": "", "status": "pending"},  # blank — must be filtered
        {"id": "a3", "text": "day-in-the-life: moving apartments", "status": "pending"},
    ]
    persona_row.tiktok_profile = None
    persona_row.style = None

    captured_inputs: list[ContentPlanInput] = []
    output = MagicMock()
    output.items = []
    agent = MagicMock()

    def _run(agent_input, ctx):  # noqa: ANN001, ARG001
        captured_inputs.append(agent_input)
        return output

    agent.run = MagicMock(side_effect=_run)

    ctx = _gen_session(plan, persona_row)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch("app.tasks.content_plan_build._dedup_and_replace", return_value=output),
    ):
        generate_content_plan.run(str(plan.id))

    assert len(captured_inputs) == 1
    seeds = captured_inputs[0].user_idea_seeds
    assert "behind-the-scenes of my launch week" in seeds
    assert "day-in-the-life: moving apartments" in seeds
    # blank seed must be filtered
    assert "" not in seeds
    assert len(seeds) == 2


# ── reroll_plan_item task tests ──────────────────────────────────────────────


from app.tasks.content_plan_build import reroll_plan_item  # noqa: E402


def _reroll_session(item, plan, persona_row=None) -> MagicMock:
    """sync_session context mock that routes get() by model class."""
    session = MagicMock()

    def _get(model, _pk):  # noqa: ANN001
        return {PlanItem: item, ContentPlan: plan, PersonaRow: persona_row}.get(model)

    session.get = MagicMock(side_effect=_get)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def _idea_item(day: int = 3, idea: str = "film the 5am start") -> MagicMock:
    it = MagicMock()
    it.id = uuid.uuid4()
    it.content_plan_id = uuid.uuid4()
    it.day_index = day
    it.theme = "morning routine"
    it.idea = idea
    it.filming_suggestion = None
    it.rationale = None
    it.filming_guide = []
    it.item_status = "rerolling"
    it.user_edited = False
    return it


def _plan_with_items(items) -> MagicMock:
    plan = MagicMock()
    plan.id = uuid.uuid4()
    plan.persona_id = uuid.uuid4()
    plan.events = None
    plan.horizon_days = 30
    plan.items = items
    return plan


def _persona_row() -> MagicMock:
    row = MagicMock()
    row.persona = _valid_persona()
    return row


def test_reroll_patches_item_fields_preserving_day_index() -> None:
    """Fresh spec is applied to the item in-place; day_index is untouched."""
    item = _idea_item(day=7, idea="old idea")
    sibling = MagicMock()
    sibling.idea = "sibling idea"
    plan = _plan_with_items([item, sibling])

    fresh_spec = PlanItemSpec(day_index=14, theme="new theme", idea="brand new idea")
    output = MagicMock()
    output.items = [fresh_spec]
    agent = MagicMock()
    agent.run = MagicMock(return_value=output)

    ctx = _reroll_session(item, plan, _persona_row())

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch(
            "app.tasks.content_plan_build.choose_replacements",
            return_value=[fresh_spec],
        ),
    ):
        reroll_plan_item.run(str(item.id))

    # Fields patched
    assert item.theme == "new theme"
    assert item.idea == "brand new idea"
    assert item.item_status == "idea"
    assert item.user_edited is False
    # day_index must be preserved (we never write it)
    assert item.day_index == 7


def test_reroll_resets_to_idea_on_failure() -> None:
    """Agent throws; item_status must be reset to 'idea' (best-effort)."""
    item = _idea_item()
    plan = _plan_with_items([item])

    ctx = _reroll_session(item, plan, _persona_row())

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch(
            "app.tasks.content_plan_build.ContentPlanGeneratorAgent",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(Exception),  # retry re-raises
    ):
        reroll_plan_item.run(str(item.id))

    assert item.item_status == "idea"


def test_reroll_exclude_list_is_all_plan_ideas() -> None:
    """ContentPlanInput.exclude_ideas must include all plan item ideas."""
    item = _idea_item(idea="film the dark start")
    other = MagicMock()
    other.idea = "cook a quick meal"
    plan = _plan_with_items([item, other])

    captured_inputs: list[ContentPlanInput] = []

    output = MagicMock()
    output.items = []
    agent = MagicMock()

    def _run(agent_input, ctx):  # noqa: ANN001, ARG001
        captured_inputs.append(agent_input)
        return output

    agent.run = MagicMock(side_effect=_run)

    ctx = _reroll_session(item, plan, _persona_row())

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.tasks.content_plan_build.default_client"),
        patch("app.tasks.content_plan_build.ContentPlanGeneratorAgent", return_value=agent),
        patch("app.tasks.content_plan_build.choose_replacements", return_value=[]),
    ):
        reroll_plan_item.run(str(item.id))

    assert len(captured_inputs) == 1
    excluded = captured_inputs[0].exclude_ideas
    assert "film the dark start" in excluded
    assert "cook a quick meal" in excluded


# ── Narrative clip order (filming-guide alignment) ────────────────────────────


def _narrative_item(guide: list[dict], assignments: list[dict]) -> MagicMock:
    item = MagicMock()
    item.id = uuid.uuid4()
    item.filming_guide = guide
    item.clip_assignments = assignments
    return item


def test_narrative_order_derives_guide_order_not_attach_order() -> None:
    """clip_assignments arrive in client attach order; the guide's shot
    sequence must win."""
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [
        {"shot_id": "s1", "what": "opening", "duration_s": 4},
        {"shot_id": "s2", "what": "middle", "duration_s": 5},
        {"shot_id": "s3", "what": "ending", "duration_s": 3},
    ]
    # Attached scrambled: s3's clip first, pool clip, s1's, s2's.
    assignments = [
        {"gcs_path": "u/c3.mp4", "shot_id": "s3"},
        {"gcs_path": "u/pool.mp4", "shot_id": None},
        {"gcs_path": "u/c1.mp4", "shot_id": "s1"},
        {"gcs_path": "u/c2.mp4", "shot_id": "s2"},
    ]
    # set_item_clips puts slot clips first (attach order), pool after:
    clip_paths = ["u/c3.mp4", "u/c1.mp4", "u/c2.mp4", "u/pool.mp4"]
    item = _narrative_item(guide, assignments)

    ordered, count = _narrative_clip_order(item, clip_paths)

    assert ordered == ["u/c1.mp4", "u/c2.mp4", "u/c3.mp4", "u/pool.mp4"]
    assert count == 3


def test_narrative_order_stale_shot_id_becomes_pool() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [{"shot_id": "s1", "what": "opening", "duration_s": 4}]
    assignments = [
        {"gcs_path": "u/stale.mp4", "shot_id": "s-removed-by-reroll"},
        {"gcs_path": "u/c1.mp4", "shot_id": "s1"},
    ]
    clip_paths = ["u/stale.mp4", "u/c1.mp4"]
    item = _narrative_item(guide, assignments)

    ordered, count = _narrative_clip_order(item, clip_paths)

    assert ordered == ["u/c1.mp4", "u/stale.mp4"]
    assert count == 1


def test_narrative_order_no_guide_is_noop() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    item = _narrative_item([], [{"gcs_path": "u/a.mp4", "shot_id": None}])
    ordered, count = _narrative_clip_order(item, ["u/a.mp4"])

    assert ordered == ["u/a.mp4"]
    assert count == 0


def test_narrative_order_no_shot_assignments_is_noop() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [{"shot_id": "s1", "what": "opening", "duration_s": 4}]
    item = _narrative_item(guide, [{"gcs_path": "u/a.mp4", "shot_id": None}])
    ordered, count = _narrative_clip_order(item, ["u/a.mp4"])

    assert ordered == ["u/a.mp4"]
    assert count == 0


def test_narrative_order_assignment_path_not_in_clip_paths_ignored() -> None:
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [{"shot_id": "s1", "what": "opening", "duration_s": 4}]
    item = _narrative_item(guide, [{"gcs_path": "u/ghost.mp4", "shot_id": "s1"}])
    ordered, count = _narrative_clip_order(item, ["u/real.mp4"])

    assert ordered == ["u/real.mp4"]
    assert count == 0


def test_narrative_order_multi_clip_per_shot() -> None:
    """Multiple clips assigned to the same shot all appear in guide order, then pool."""
    from app.tasks.content_plan_build import _narrative_clip_order

    guide = [
        {"shot_id": "s1", "what": "opening", "duration_s": 4},
        {"shot_id": "s2", "what": "closing", "duration_s": 5},
    ]
    # s1 has two clips attached in reverse order; s2 has one; one pool clip.
    assignments = [
        {"gcs_path": "u/c1b.mp4", "shot_id": "s1"},
        {"gcs_path": "u/c2.mp4", "shot_id": "s2"},
        {"gcs_path": "u/c1a.mp4", "shot_id": "s1"},
        {"gcs_path": "u/pool.mp4", "shot_id": None},
    ]
    clip_paths = ["u/c1b.mp4", "u/c2.mp4", "u/c1a.mp4", "u/pool.mp4"]
    item = _narrative_item(guide, assignments)

    ordered, count = _narrative_clip_order(item, clip_paths)

    # Guide order: s1 clips (both) before s2 clip; pool is tail.
    assert ordered[:2] == ["u/c1b.mp4", "u/c1a.mp4"]  # both s1 clips in attach order
    assert ordered[2] == "u/c2.mp4"                    # s2 clip
    assert ordered[3] == "u/pool.mp4"                  # pool last
    assert count == 3  # 3 clips placed (the 2 for s1 + 1 for s2)


# ── Footage pool ────────────────────────────────────────────────────────────────


def test_pool_match_limit_within_matcher_schema():
    """_POOL_MATCH_LIMIT must validate against ClipPlanMatcherInput's schema bound.

    Regression: the pool shipped with limit 8 against a le=7 field — every pool
    match failed at pydantic validation before the matcher ever ran (dogfood,
    2026-06-11)."""
    from app.agents.clip_plan_matcher import ClipPlanMatcherInput, ClipSummary, PlanItemSummary
    from app.tasks.content_plan_build import _POOL_MATCH_LIMIT

    inp = ClipPlanMatcherInput(
        clips=[
            ClipSummary(
                clip_gcs_path="users/u/plan-pool/p/a.mp4",
                hook_text="",
                hook_score=5.0,
                detected_subject="street scene",
                transcript_excerpt="",
            )
        ],
        items=[PlanItemSummary(item_id="i1", theme="t", idea="i", filming_suggestion="")],
        max_assignments=_POOL_MATCH_LIMIT,
    )
    assert inp.max_assignments == _POOL_MATCH_LIMIT


# ── T15: generate_ideas_into_plan updates bare ideas in-place ─────────────────


def test_generate_ideas_updates_in_place() -> None:
    """generate_ideas_into_plan expands bare ideas in-place (update, not append).

    Verifies:
    - session.add is never called (no new rows created)
    - each bare idea item gets theme, day_index, filming_guide written back
    - plan_status is set to "ready"
    """
    from app.agents.idea_expander import FilmingShot, IdeaExpanderOutput  # noqa: PLC0415
    from app.tasks.content_plan_build import generate_ideas_into_plan  # noqa: PLC0415

    plan_id = str(uuid.uuid4())
    item1_id = uuid.uuid4()
    item2_id = uuid.uuid4()

    bare1 = MagicMock()
    bare1.id = item1_id
    bare1.idea = "sunrise hike"
    bare1.day_index = None
    bare1.position = 1

    bare2 = MagicMock()
    bare2.id = item2_id
    bare2.idea = "coffee productivity"
    bare2.day_index = None
    bare2.position = 2

    plan = MagicMock()
    plan.id = uuid.UUID(plan_id)
    plan.persona_id = uuid.uuid4()
    plan.horizon_days = 30
    plan.plan_status = "generating"
    plan.items = [bare1, bare2]

    persona_row = MagicMock()
    persona_row.persona = {
        "summary": "creator",
        "content_pillars": ["outdoor"],
        "tone": "warm",
        "audience": "hikers",
        "posting_cadence": "3/wk",
        "sample_topics": ["trails"],
    }

    expand1 = IdeaExpanderOutput(
        theme="Golden Hour Hike",
        filming_suggestion="Film at dawn on a local trail",
        filming_guide=[FilmingShot(what="trail entrance", how="wide angle", duration_s=4)],
        rationale="sunrise hooks viewers",
    )
    expand2 = IdeaExpanderOutput(
        theme="Focused Work Session",
        filming_suggestion="Film your desk setup with natural light",
        filming_guide=[],
        rationale="relatable productivity content",
    )

    session = MagicMock()
    session.get = MagicMock(side_effect=lambda model, pk: {
        ContentPlan: plan,
        PersonaRow: persona_row,
        PlanItem: bare1 if pk == item1_id else bare2,
    }.get(model))
    session.add = MagicMock()
    session.commit = MagicMock()

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("app.tasks.content_plan_build.sync_session", return_value=ctx),
        patch("app.agents.idea_expander.IdeaExpanderAgent") as mock_agent_cls,
        patch("app.services.pipeline_trace.pipeline_trace_for") as mock_trace,
        patch("sqlalchemy.orm.attributes.flag_modified"),
    ):
        mock_trace_ctx = MagicMock()
        mock_trace_ctx.__enter__ = MagicMock(return_value=None)
        mock_trace_ctx.__exit__ = MagicMock(return_value=False)
        mock_trace.return_value = mock_trace_ctx

        mock_agent = MagicMock()
        mock_agent.run.side_effect = [expand1, expand2]
        mock_agent_cls.return_value = mock_agent

        generate_ideas_into_plan.run(plan_id)

    # No new rows — update only.
    session.add.assert_not_called()
    # Each bare idea item got its theme and day_index written back.
    assert bare1.theme == "Golden Hour Hike"
    assert bare1.day_index == 1
    assert bare2.theme == "Focused Work Session"
    assert bare2.day_index == 2
    # Plan status was reset to ready.
    assert plan.plan_status == "ready"
