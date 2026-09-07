"""Execute the reaper's JSONB/jsonpath discovery predicates on real Postgres.

Why this module exists, separately from the mocked `test_reaper.py`:

PostgreSQL parses a jsonpath at EXECUTION time, not when the statement is
built or compiled. A malformed path is therefore invisible to import, to
ruff, and to every test that mocks `sync_session` — which is all of
`test_reaper.py`. Production shipped
`... && @.editor_render_attempt)` (a bare accessor where jsonpath demands a
predicate) and every worker logged, on `worker_ready`:

    ProgrammingError('(psycopg2.errors.SyntaxError) syntax error at or
    near ")" of jsonpath input')

`app/worker.py` catches that as non-fatal, so the whole stuck-variant
reconcile silently no-op'd: both jsonpath branches are OR'd into one
discovery query, so the malformed one took the valid one down with it.

These tests round-trip the real expression objects through Postgres, so a
malformed jsonpath fails the suite instead of being swallowed at runtime.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import cast, literal, select, text
from sqlalchemy.dialects.postgresql import JSONPATH
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.config import settings
from app.database import sync_session
from app.models import Job, User
from app.tasks.reaper import (
    _EDITOR_RENDER_ATTEMPT_JSONPATH,
    _RECONCILE_LOOKBACK_DAYS,
    _STUCK_VARIANT_JSONPATH,
    _editor_render_attempt_predicate,
    _reconcile_discovery_clauses,
    _repairable_state_predicate,
    _terminal_reconcile_state_predicate,
)

if not (make_url(settings.database_url).database or "").endswith("_test"):
    pytest.skip("requires a *_test database", allow_module_level=True)


@pytest.fixture
def db():
    """A rolled-back session, so seeded rows never leak across xdist workers."""

    try:
        session = sync_session()
        session.execute(text("select 1"))
    except OperationalError:
        pytest.skip("nova_test Postgres not reachable")
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def _seed(db, assembly_plan: dict | None, *, status: str = "variants_ready") -> uuid.UUID:
    """Insert one uncommitted Job (plus its owner) and return its id."""

    user_id = uuid.uuid4()
    db.add(User(id=user_id, email=f"{user_id}@test.local"))
    db.flush()
    job_id = uuid.uuid4()
    db.add(
        Job(
            id=job_id,
            user_id=user_id,
            status=status,
            raw_storage_path=f"test/{job_id}.mp4",
            assembly_plan=assembly_plan,
            updated_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    db.flush()
    return job_id


def _matching_ids(db, predicate, candidate_ids: list[uuid.UUID]) -> set[uuid.UUID]:
    """Run `predicate` on Postgres, scoped to this test's own rows."""

    rows = db.execute(select(Job.id).where(Job.id.in_(candidate_ids), predicate)).scalars()
    return set(rows)


def _run_jsonpath(db, path: str) -> None:
    """Run `assembly_plan @? <path>` — the exact shape the reaper emits."""

    db.execute(
        select(Job.id)
        .where(Job.assembly_plan.bool_op("@?")(cast(literal(path), JSONPATH)))
        .limit(1)
    ).all()


# --------------------------------------------------------------------------
# The predicates must PARSE. This is the regression guard for the prod bug.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,predicate_factory",
    [
        ("terminal_reconcile_state", _terminal_reconcile_state_predicate),
        ("editor_render_attempt", _editor_render_attempt_predicate),
        (
            "repairable_state",
            lambda: _repairable_state_predicate(
                datetime.now(UTC) - timedelta(minutes=60),
                datetime.now(UTC) - timedelta(seconds=180),
            ),
        ),
    ],
)
def test_reaper_predicate_executes_against_postgres(db, name, predicate_factory):
    """Every reaper discovery predicate must survive a real Postgres parse.

    `editor_render_attempt` is the one that shipped broken; `repairable_state`
    is the composed expression `reconcile_stuck_variants` actually runs, so
    this also pins the OR-composition that let one bad branch disable both.
    """

    db.execute(select(Job.id).where(predicate_factory()).limit(1)).all()


def test_bare_accessor_jsonpath_still_fails_on_postgres(db):
    """Prove the guard above is not vacuous.

    This is the exact expression production shipped. If Postgres ever started
    accepting a bare accessor as a `&&` operand, the test above would pass for
    the wrong reason and this one would tell us why.
    """

    _seed(db, {"variants": [{"render_status": "rendering"}]})
    broken = '$.variants[*] ? (@.render_status == "rendering" && @.editor_render_attempt)'
    with pytest.raises(ProgrammingError) as excinfo:
        _run_jsonpath(db, broken)
    # 42601 = syntax_error. Assert the structured code, not the wording:
    # the message text is Postgres' to change, the SQLSTATE is not.
    assert excinfo.value.orig.pgcode == "42601"
    assert "jsonpath input" in str(excinfo.value)
    db.rollback()


# --------------------------------------------------------------------------
# ...and they must select the right rows. A syntactically valid jsonpath that
# matches nothing would reap nothing, which is the same silent no-op.
# --------------------------------------------------------------------------


def test_stuck_variant_jsonpath_matches_rendering_and_pending_only(db):
    rendering = _seed(db, {"variants": [{"render_status": "rendering"}]})
    pending = _seed(db, {"variants": [{"render_status": "pending"}]})
    second_variant = _seed(
        db,
        {"variants": [{"render_status": "ready"}, {"render_status": "pending"}]},
    )
    ready = _seed(db, {"variants": [{"render_status": "ready"}]})
    no_variants = _seed(db, {"other": "state"})
    null_plan = _seed(db, None)
    candidates = [rendering, pending, second_variant, ready, no_variants, null_plan]

    matched = _matching_ids(db, _terminal_reconcile_state_predicate(), candidates)

    assert matched == {rendering, pending, second_variant}


def test_required_speech_state_predicate_matches_each_private_key(db):
    """The sibling branch of the same OR — no jsonpath, but same query."""

    keys = (
        "required_speech_generation_locks",
        "staged_render_results",
        "working_render_variants",
        "terminal_pending",
    )
    by_key = {key: _seed(db, {"_speech_cleanup_internal": {key: {}}}) for key in keys}
    unrelated_key = _seed(db, {"_speech_cleanup_internal": {"detector_version": "v1"}})
    # jsonb_typeof guard: a non-object private capsule must not match.
    scalar_private = _seed(db, {"_speech_cleanup_internal": "not-an-object"})
    candidates = [*by_key.values(), unrelated_key, scalar_private]

    matched = _matching_ids(db, _terminal_reconcile_state_predicate(), candidates)

    assert matched == set(by_key.values())


def test_editor_render_attempt_jsonpath_requires_rendering_plus_lease(db):
    stuck_save = _seed(
        db,
        {
            "variants": [
                {
                    "render_status": "rendering",
                    "editor_render_attempt": {"task_id": "t", "generation_id": "g"},
                }
            ]
        },
    )
    rendering_no_lease = _seed(db, {"variants": [{"render_status": "rendering"}]})
    lease_but_done = _seed(
        db,
        {"variants": [{"render_status": "ready", "editor_render_attempt": {"task_id": "t"}}]},
    )
    candidates = [stuck_save, rendering_no_lease, lease_but_done]

    matched = _matching_ids(db, _editor_render_attempt_predicate(), candidates)

    assert matched == {stuck_save}


def _age(db, job_id: uuid.UUID, delta: timedelta) -> None:
    """Backdate a seeded job's `updated_at` by `delta`."""

    db.execute(
        Job.__table__.update().where(Job.id == job_id).values(updated_at=datetime.now(UTC) - delta)
    )
    db.flush()


def test_repairable_state_predicate_honors_each_branch_cutoff(db):
    """Each branch must fire on ITS OWN cutoff, and only its own.

    The two rows are deliberately NOT interchangeable. A row satisfying both
    state predicates cannot tell the branches apart: with only that row,
    swapping the cutoffs between branches - or wiring the editor predicate into
    both legs - still passes. The lease-less row is what discriminates them.
    """

    editor_only = _seed(
        db,
        {
            "variants": [
                {
                    "render_status": "rendering",
                    "editor_render_attempt": {"task_id": "t", "generation_id": "g"},
                }
            ]
        },
    )
    generic_only = _seed(db, {"variants": [{"render_status": "pending"}]})
    _age(db, editor_only, timedelta(minutes=5))
    _age(db, generic_only, timedelta(minutes=5))
    candidates = [editor_only, generic_only]

    now = datetime.now(UTC)
    generic_cutoff = now - timedelta(minutes=60)
    editor_cutoff = now - timedelta(seconds=180)

    # 5 minutes old: past the 180s editor cutoff, short of the 60min generic one.
    assert _matching_ids(
        db, _repairable_state_predicate(generic_cutoff, editor_cutoff), candidates
    ) == {editor_only}

    # Both rows aged past 60min: the generic branch picks up the lease-less row.
    _age(db, editor_only, timedelta(minutes=61))
    _age(db, generic_only, timedelta(minutes=61))
    assert _matching_ids(
        db, _repairable_state_predicate(generic_cutoff, editor_cutoff), candidates
    ) == {editor_only, generic_only}

    # Editor branch disabled by an older cutoff, rows young again -> neither fires.
    _age(db, editor_only, timedelta(minutes=5))
    _age(db, generic_only, timedelta(minutes=5))
    assert (
        _matching_ids(db, _repairable_state_predicate(generic_cutoff, generic_cutoff), candidates)
        == set()
    )


@pytest.mark.parametrize(
    "lease",
    [None, "a-string", False, {}],
    ids=["json-null", "scalar-string", "json-false", "empty-object"],
)
def test_editor_jsonpath_exists_is_wider_than_the_python_dict_check(db, lease):
    """`exists (...)` matches any value, including ones Python then ignores.

    Deliberate asymmetry: discovery is a cheap prefilter, and
    `reconcile_stuck_variants` re-checks `isinstance(attempt, dict)` under the
    row lock, so an over-included row is loaded and left untouched. Narrowing
    the jsonpath instead would risk under-inclusion, which loses repairs
    silently. This pins the intent so the widening is not "fixed" by accident.
    """

    job_id = _seed(
        db,
        {"variants": [{"render_status": "rendering", "editor_render_attempt": lease}]},
    )

    assert _matching_ids(db, _editor_render_attempt_predicate(), [job_id]) == {job_id}


def test_discovery_clauses_execute_and_require_repairable_state(db):
    """The real discovery WHERE must run, and must actually filter on state.

    Executing the assembled clause list is what stops a future edit from
    dropping `repairable_state_predicate` out of the query unnoticed.
    """

    now = datetime.now(UTC)
    lookback = now - timedelta(days=_RECONCILE_LOOKBACK_DAYS)
    predicate = _repairable_state_predicate(
        now - timedelta(minutes=60), now - timedelta(seconds=180)
    )

    stuck = _seed(db, {"variants": [{"render_status": "rendering"}]})
    healthy = _seed(db, {"variants": [{"render_status": "ready"}]})
    still_working = _seed(db, {"variants": [{"render_status": "rendering"}]}, status="rendering")
    cancelled = _seed(db, {"variants": [{"render_status": "rendering"}]}, status="cancelled")
    for job_id in (stuck, healthy, still_working, cancelled):
        _age(db, job_id, timedelta(minutes=61))
    ancient = _seed(db, {"variants": [{"render_status": "rendering"}]})
    _age(db, ancient, timedelta(days=_RECONCILE_LOOKBACK_DAYS + 1))
    candidates = [stuck, healthy, still_working, cancelled, ancient]

    clauses = _reconcile_discovery_clauses(predicate, lookback, [])
    matched = set(db.execute(select(Job.id).where(Job.id.in_(candidates), *clauses)).scalars())
    assert matched == {stuck}

    # The live-job exclusion is the one conditional clause in the builder.
    clauses = _reconcile_discovery_clauses(predicate, lookback, [stuck])
    matched = set(db.execute(select(Job.id).where(Job.id.in_(candidates), *clauses)).scalars())
    assert matched == set()


def test_jsonpath_constants_are_bound_not_spliced():
    """The path must travel as a parameter, not as raw SQL text.

    `literal_column` splicing is what forced this module to hand-manage the
    quoting and the `::jsonpath` cast inside a Python string literal.
    """

    compiled = _editor_render_attempt_predicate().compile()
    assert _EDITOR_RENDER_ATTEMPT_JSONPATH in compiled.params.values()
    assert "::jsonpath" not in str(compiled)

    compiled = _terminal_reconcile_state_predicate().compile()
    assert _STUCK_VARIANT_JSONPATH in compiled.params.values()


def test_reconcile_sweep_index_predicate_matches_the_reaper_jsonpath():
    """Migration 0096's index predicate must be byte-identical to the constant.

    A partial index only serves a query whose predicate the planner can prove
    implies the index's. Drifting the jsonpath by one character silently drops
    the sweep back to a seq scan of `jobs` on every worker start.
    """

    import importlib  # noqa: PLC0415

    migration = importlib.import_module(
        "app.migrations.versions.0096_stuck_variant_reconcile_index"
    )

    assert f"'{_STUCK_VARIANT_JSONPATH}'::jsonpath" in migration._PREDICATE
