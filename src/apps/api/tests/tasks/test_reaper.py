"""Unit tests for app.tasks.reaper.

Mocks `sync_session` (DB) and `celery_app.control.inspect()` (broker)
so the suite runs without Postgres/Redis.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.durable_attempt_cleanup import CleanupReconcileResult
from app.services.speech_cleanup_terminal import (
    close_required_speech_generation_uploads,
    reserve_required_speech_generation,
    stage_required_speech_generation,
)


@pytest.fixture(autouse=True)
def _stub_post_terminal_storage_cleanup(monkeypatch):
    reconcile = MagicMock(return_value=CleanupReconcileResult())
    monkeypatch.setattr(
        "app.tasks.reaper.reconcile_storage_attempt_cleanup",
        reconcile,
    )
    return reconcile


def _make_celery_with_inspect(active=None, reserved=None, raises=None):
    """Build a fake Celery app whose .control.inspect() returns the given dicts.

    Pass `active={"worker1": [{"args": ["job-id-1"]}]}` to simulate a live task.
    Pass `raises=Exception("boom")` to simulate broker failure.
    """
    app = MagicMock()
    inspector = MagicMock()
    if raises is not None:
        inspector.active.side_effect = raises
        inspector.reserved.side_effect = raises
    else:
        inspector.active.return_value = active or {}
        inspector.reserved.return_value = reserved or {}
    app.control.inspect.return_value = inspector
    return app


def _patch_sync_session(rowcount: int = 0, reaped_rows: list | None = None):
    """Returns a patch context for sync_session that yields a fake session.

    The first execute() call (the locking SELECT) returns a result whose
    fetchall() yields `reaped_rows` (default: `rowcount` empty-assembly-plan
    tuples so the variant-reconciliation loop is a no-op). Subsequent
    execute() calls model one successful fenced UPDATE per candidate.
    """
    if reaped_rows is None:
        import uuid as _uuid

        reaped_rows = [(_uuid.uuid4(), None, []) for _ in range(rowcount)]
    else:
        reaped_rows = [(*row, []) if len(row) == 2 else row for row in reaped_rows]

    session = MagicMock()

    # First execute: the FOR UPDATE SKIP LOCKED candidate SELECT.
    first_result = MagicMock()
    first_result.rowcount = len(reaped_rows)
    first_result.fetchall.return_value = reaped_rows

    # Subsequent executes: one fenced terminal UPDATE per selected job.
    subsequent_result = MagicMock()
    subsequent_result.rowcount = 1
    subsequent_result.fetchall.return_value = []

    # Return first_result on the first call, subsequent_result on later calls.
    session.execute.side_effect = [first_result] + [subsequent_result] * 20

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)

    return patch("app.tasks.reaper.sync_session", return_value=ctx), session


def _required_speech_plan(
    job_id: str,
    generation: str,
    *,
    close_uploads: bool = True,
) -> dict:
    plan: dict = {"variants": []}
    reserve_required_speech_generation(
        plan,
        job_id=job_id,
        pending_variant={
            "variant_id": "subtitled",
            "render_generation_id": generation,
            "render_status": "rendering",
            "ok": False,
            "video_path": (
                f"generative-jobs/{job_id}/render-generations/{generation}/provisional.mp4"
            ),
        },
        generation=generation,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=35),
    )
    stage_required_speech_generation(
        plan,
        generation=generation,
        result={
            "variant_id": "subtitled",
            "render_generation_id": generation,
            "render_status": "ready",
            "ok": True,
            "video_path": (f"generative-jobs/{job_id}/render-generations/{generation}/staged.mp4"),
            "_speech_cleanup_outcome_context": {
                "analysis_attempt_id": uuid.uuid4().hex,
                "analysis_view": "full_clip",
                "detector_version": "mixed-gap-v1",
                "source_tag": "0123456789abcdef",
                "selected_plan": "candidate",
                "candidate_status": "ready",
                "output_removal_count": 1,
                "output_removed_ms": 572,
            },
        },
    )
    if close_uploads:
        close_required_speech_generation_uploads(plan, generation=generation)
    return plan


def _assembly_plan_from_update_call(call) -> dict | None:
    params = call.args[0].compile().params
    return next(
        (value for value in params.values() if isinstance(value, dict) and "variants" in value),
        None,
    )


def _speech_cleanup_outcomes_from_update_call(call) -> list[dict]:
    params = call.args[0].compile().params
    traces = [
        value
        for value in params.values()
        if isinstance(value, list)
        and any(isinstance(event, dict) and event.get("stage") == "silence_cut" for event in value)
    ]
    if not traces:
        return []
    return [
        event["data"]
        for event in traces[0]
        if isinstance(event, dict) and event.get("event") == "speech_cleanup_render_outcome"
    ]


def _speech_cleanup_outcomes(trace: list[dict]) -> list[dict]:
    return [
        event["data"]
        for event in trace
        if isinstance(event, dict)
        and event.get("stage") == "silence_cut"
        and event.get("event") == "speech_cleanup_render_outcome"
    ]


class TestLiveJobIds:
    def test_returns_empty_set_when_no_active_or_reserved(self):
        from app.tasks.reaper import _live_job_ids

        app = _make_celery_with_inspect(active={}, reserved={})
        assert _live_job_ids(app) == set()

    def test_collects_first_arg_of_each_active_task(self):
        from app.tasks.reaper import _live_job_ids

        app = _make_celery_with_inspect(
            active={
                "celery@worker1": [{"args": ["job-aaa"]}, {"args": ["job-bbb"]}],
                "celery@worker2": [{"args": ["job-ccc"]}],
            }
        )
        assert _live_job_ids(app) == {"job-aaa", "job-bbb", "job-ccc"}

    def test_includes_reserved_tasks(self):
        from app.tasks.reaper import _live_job_ids

        app = _make_celery_with_inspect(
            active={"w1": [{"args": ["a"]}]},
            reserved={"w1": [{"args": ["b"]}]},
        )
        assert _live_job_ids(app) == {"a", "b"}

    def test_skips_tasks_with_no_args(self):
        from app.tasks.reaper import _live_job_ids

        app = _make_celery_with_inspect(
            active={
                "w1": [{"args": []}, {"args": ["only-this"]}, {}],
            }
        )
        assert _live_job_ids(app) == {"only-this"}

    def test_returns_none_on_inspect_failure(self):
        """Broker hiccup → None signals 'unknown — don't reap'."""
        from app.tasks.reaper import _live_job_ids

        app = _make_celery_with_inspect(raises=ConnectionError("redis down"))
        assert _live_job_ids(app) is None

    def test_active_returning_none_treated_as_empty(self):
        """celery_app.control.inspect().active() returns None when no workers report."""
        from app.tasks.reaper import _live_job_ids

        app = MagicMock()
        inspector = MagicMock()
        inspector.active.return_value = None
        inspector.reserved.return_value = None
        app.control.inspect.return_value = inspector
        assert _live_job_ids(app) == set()


class TestReapOrphans:
    def test_no_op_when_inspect_fails(self):
        """Inspection failure → no reap (safer to skip than false-positive)."""
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(raises=ConnectionError("redis down"))
        # sync_session should NOT even be called
        with patch("app.tasks.reaper.sync_session") as mock_session:
            assert reap_orphans(app) == 0
            mock_session.assert_not_called()

    def test_returns_rowcount_from_db(self):
        """Happy path: inspect returns nothing live, DB reaps 5 rows."""
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=5)
        with patch_ctx:
            assert reap_orphans(app) == 5
        # At least one UPDATE + one commit must have happened.
        assert session.execute.call_count >= 1
        session.commit.assert_called_once()

    def test_zero_rowcount_returns_zero(self):
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, _ = _patch_sync_session(rowcount=0)
        with patch_ctx:
            assert reap_orphans(app) == 0

    def test_excludes_live_jobs_from_update(self):
        """When live jobs exist, the WHERE clause must include NOT IN(live)."""
        import uuid as _uuid

        from app.tasks.reaper import reap_orphans

        live_uuid = str(_uuid.uuid4())
        app = _make_celery_with_inspect(
            active={
                "w1": [{"args": [live_uuid]}],
            }
        )
        patch_ctx, session = _patch_sync_session(rowcount=2)
        with patch_ctx:
            reap_orphans(app)
        # Inspect the SQL statement passed to execute() — verify NOT IN clause
        # exists and references the live job id (as a UUID parameter).
        stmt = session.execute.call_args_list[0][0][0]
        # Render with bind params visible (not literal — UUID type can't
        # always be literal-rendered across dialects).
        sql_str = str(stmt)
        assert "NOT IN" in sql_str.upper() or "not_in" in sql_str.lower()
        # And confirm the live UUID flows into the compiled params.
        compiled = stmt.compile()
        assert any(live_uuid in str(v) for v in compiled.params.values())

    def test_no_not_in_clause_when_live_set_empty(self):
        """Empty live set → SQL must NOT contain `NOT IN ()` (empty IN is invalid)."""
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=3)
        with patch_ctx:
            reap_orphans(app)
        stmt = session.execute.call_args_list[0][0][0]
        sql_str = str(stmt)
        # The two safety clauses (status IN, updated_at <) must always be present.
        assert "status" in sql_str
        assert "updated_at" in sql_str
        # No empty NOT IN — would be a SQL error.
        assert "NOT IN ()" not in sql_str.upper()
        assert "NOT IN" not in sql_str.upper()  # absent entirely when live={}

    def test_threshold_min_controls_cutoff(self):
        """Verify threshold_min flows into the updated_at< comparison."""
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=0)
        with patch_ctx:
            # Just smoke-test that a custom threshold doesn't crash.
            reap_orphans(app, threshold_min=5)
        # The cutoff string is dynamic (current time), so we can't pin an exact
        # value. But we can confirm the SQL was compiled and ran.
        session.execute.assert_called_once()

    def test_pre_computed_live_param_skips_internal_inspect(self):
        """`live=` param (sweep_stale_jobs consolidation) must bypass inspect()."""
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(raises=AssertionError("inspect() should not fire"))
        patch_ctx, session = _patch_sync_session(rowcount=0)
        with patch_ctx:
            assert reap_orphans(app, live=set()) == 0
        # Reaching this line without an exception proves inspect() was never
        # invoked when a pre-computed (empty) live set is supplied.
        session.execute.assert_called_once()

    def test_writes_processing_failed_with_unknown_failure_reason(self):
        """The reaped row gets the right marker fields."""
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=1)
        with patch_ctx:
            reap_orphans(app)
        stmt = session.execute.call_args[0][0]
        # Values land in compiled bind params, not the SQL text.
        params = stmt.compile().params
        values = {str(v) for v in params.values()}
        assert "processing_failed" in values
        assert "unknown" in values
        assert any("Resubmit" in str(v) for v in values)  # user-facing error_detail

    def test_reconciles_durable_storage_receipts_after_terminal_commit(
        self,
        _stub_post_terminal_storage_cleanup,
    ):
        import uuid as _uuid

        from app.tasks.reaper import reap_orphans

        job_id = _uuid.uuid4()
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(
            rowcount=1,
            reaped_rows=[(job_id, {})],
        )
        with patch_ctx:
            assert reap_orphans(app) == 1

        session.commit.assert_called_once()
        _stub_post_terminal_storage_cleanup.assert_called_once_with(
            job_id,
            source_limit=1,
            render_limit=1,
        )


class TestThresholdConstant:
    """The 60-min threshold is load-bearing for the no-false-positive guarantee."""

    def test_threshold_is_2x_hard_time_limit(self):
        """Threshold (min) must be ≥ 2× orchestrate_template_job hard time_limit
        (1800s = 30min) so a legitimately slow finisher always wins the race."""
        from app.tasks.reaper import THRESHOLD_MIN

        assert THRESHOLD_MIN >= 60, (
            f"THRESHOLD_MIN={THRESHOLD_MIN} too low — must be 2× the multi-clip "
            f"hard time_limit (1800s/60min) to avoid reaping legit slow jobs."
        )


@pytest.mark.parametrize(
    "status,should_reap",
    [
        ("processing", True),
        # Worker-owned mid-pipeline statuses the newer (music/generative)
        # orchestrators flip to once a task is actively executing. A SIGKILL
        # mid-flight strands them exactly like `processing` — they must reap.
        # (prod job 5ae0142f stuck "rendering" forever before this was added.)
        ("matching", True),
        ("rendering", True),
        ("posting", True),
        # template_ready is the SUCCESS terminal state for template jobs —
        # set at the finalize step after assemble + audio mix + upload. The
        # reaper must NOT touch it; doing so would flip every completed job
        # to processing_failed after the 60-minute threshold (prod regression
        # observed on job e3804f62).
        ("template_ready", False),
        ("music_ready", False),
        ("variants_ready", False),
        ("variants_ready_partial", False),
        ("variants_failed", False),
        ("clips_ready", False),
        ("clips_ready_partial", False),
        ("completed", False),
        ("cancelled", False),
        ("processing_failed", False),
        # queued is deliberately NOT reapable: a job still in the broker queue
        # (not yet prefetched) is invisible to inspect(), so reaping it would
        # false-positive legit work waiting behind a deep backlog.
        ("queued", False),
    ],
)
def test_non_terminal_statuses_constant_includes_correct_set(status, should_reap):
    """Sanity-pin the status filter so a future schema change is caught."""
    from app.tasks.reaper import _NON_TERMINAL_STATUSES

    assert (status in _NON_TERMINAL_STATUSES) is should_reap


def test_template_ready_jobs_are_not_reaped():
    """Regression: a stale `template_ready` row must NOT be reaped.

    Prod incident: job e3804f62 finished successfully at 21:28 (status set
    to `template_ready` by the finalize step). At 22:31 the sweeper saw it
    as stale + unowned and flipped it to `processing_failed` with
    error_detail "Worker died with no recovery; reaped on worker startup."
    The user then opened the job and saw it as failed even though it had
    succeeded an hour earlier.

    The fix is to keep `template_ready` out of `_NON_TERMINAL_STATUSES`.
    This test pins the invariant.
    """
    from app.tasks.reaper import _NON_TERMINAL_STATUSES

    assert "template_ready" not in _NON_TERMINAL_STATUSES, (
        "template_ready is the success terminal state — reaping it flips "
        "every completed template job to processing_failed after 60 minutes."
    )


def test_rendering_jobs_are_reaped():
    """Regression: a stale `rendering` row with no live worker must be reaped.

    Prod incident (job 5ae0142f): a generative edit got through clip
    metadata → song match and flipped to status `rendering`, then the worker
    machine was SIGKILL'd by a deploy mid-render. The hard kill skipped the
    task's try/except → _fail_job, so the row sat at `rendering`,
    assembly_plan=None, error_detail=None forever and the page showed
    "Rendering your edits…" indefinitely.

    `rendering` (and the sibling worker-owned statuses `matching`/`posting`,
    set by auto_music_orchestrate.py + generative_build.py) was not in
    `_NON_TERMINAL_STATUSES`, so the reaper — whose entire reason for
    existing is to clear exactly this perpetual-loading state — never swept
    it. This test pins the fix.
    """
    from app.tasks.reaper import _NON_TERMINAL_STATUSES

    for status in ("rendering", "matching", "posting"):
        assert status in _NON_TERMINAL_STATUSES, (
            f"{status} is a worker-owned non-terminal status — a job killed "
            f"mid-{status} stays stuck forever unless the reaper sweeps it."
        )


def test_reaper_sweeps_a_stale_rendering_job():
    """End-to-end: rendering job, no live worker → reap UPDATE filters on it.

    Confirms `rendering` actually flows into the compiled status-IN filter,
    not just the constant (catches a future refactor that builds the WHERE
    clause from a different source than `_NON_TERMINAL_STATUSES`).
    """
    from app.tasks.reaper import reap_orphans

    app = _make_celery_with_inspect(active={}, reserved={})
    patch_ctx, session = _patch_sync_session(rowcount=1)
    with patch_ctx:
        assert reap_orphans(app) == 1
    stmt = session.execute.call_args[0][0]
    # The status IN(...) binds as a single expanding-list param, so look for
    # `rendering` as a member of any bound param value (not a standalone key).
    bound = stmt.compile().params.values()
    assert any("rendering" in v for v in bound if isinstance(v, (list, tuple))), (
        "rendering must appear in the status-IN filter"
    )


def test_reaper_reconciles_frozen_rendering_variants():
    """Regression: reaped job with a frozen 'rendering' variant must have that
    variant flipped to 'failed' in assembly_plan.

    Prod incident (job df883a50): worker died mid-render_variants; reaper
    flipped job-level status to processing_failed but left original_text at
    render_status='rendering' in assembly_plan['variants'].  Frontend
    anyRendering check kept polling every 2s forever and re-signed GCS URLs
    caused the ready videos to reload on every poll (the "glitch").

    Fix: reap_orphans() reconciles frozen variants in the same transaction.
    """
    import uuid as _uuid

    from app.tasks.reaper import reap_orphans

    frozen_job_id = _uuid.uuid4()
    assembly_plan_with_stuck_variant = {
        "variants": [
            {"variant_id": "song_lyrics", "render_status": "ready", "video_path": "gcs/..."},
            {"variant_id": "song_text", "render_status": "ready", "video_path": "gcs/..."},
            {"variant_id": "original_text", "render_status": "rendering", "video_path": None},
        ]
    }

    app = _make_celery_with_inspect(active={}, reserved={})
    patch_ctx, session = _patch_sync_session(
        rowcount=1,
        reaped_rows=[(frozen_job_id, assembly_plan_with_stuck_variant)],
    )
    with patch_ctx:
        count = reap_orphans(app)

    assert count == 1

    # The second execute() call is the single terminal UPDATE carrying the
    # reconciled assembly plan.
    assert session.execute.call_count >= 2, "Expected SELECT + terminal UPDATE"
    # Find the terminal UPDATE after the locking SELECT.
    recon_stmt = session.execute.call_args_list[1][0][0]
    params = recon_stmt.compile().params
    # The updated assembly_plan should have the stuck variant flipped to "failed".
    new_ap = next(
        (v for v in params.values() if isinstance(v, dict) and "variants" in v),
        None,
    )
    assert new_ap is not None, "Variant-reconciliation UPDATE must carry new assembly_plan"
    new_statuses = {v["variant_id"]: v["render_status"] for v in new_ap["variants"]}
    assert new_statuses["original_text"] == "failed", (
        "Frozen 'rendering' variant must be flipped to 'failed' by the reaper"
    )
    assert new_statuses["song_lyrics"] == "ready", "Ready variants must not be touched"
    assert new_statuses["song_text"] == "ready", "Ready variants must not be touched"


def test_reaper_terminalizes_required_speech_before_generic_variant_repair() -> None:
    """A staged/provisional required result can never become ready by path presence."""

    from app.tasks.reaper import reap_orphans

    job_id = uuid.uuid4()
    generation = uuid.uuid4().hex
    plan = _required_speech_plan(str(job_id), generation)
    app = _make_celery_with_inspect(active={}, reserved={})
    patch_ctx, session = _patch_sync_session(
        rowcount=1,
        reaped_rows=[(job_id, plan)],
    )

    with patch_ctx:
        assert reap_orphans(app) == 1

    updated = _assembly_plan_from_update_call(session.execute.call_args_list[1])
    assert updated is not None
    variant = updated["variants"][0]
    assert variant["render_status"] == "failed"
    assert variant["ok"] is False
    assert "video_path" not in variant
    internal = updated["_speech_cleanup_internal"]
    assert "required_speech_generation_locks" not in internal
    assert "staged_render_results" not in internal
    assert internal["render_generation_cleanup_pending"][0]["upload_state"] == "closed"
    outcomes = _speech_cleanup_outcomes_from_update_call(session.execute.call_args_list[1])
    assert len(outcomes) == 1
    assert outcomes[0]["outcome"] == "failed_owned"
    assert outcomes[0]["render_generation_id"] == generation
    assert outcomes[0]["failure_class"] == "WorkerDied"


def test_reaper_blocked_required_speech_leaves_job_untouched(
    _stub_post_terminal_storage_cleanup,
) -> None:
    """Missing ownership proof must block the whole orphan transition."""

    from app.services.durable_attempt_cleanup import (
        RENDER_GENERATION_CLEANUP_FIELD,
        CleanupReceiptLocator,
        remove_cleanup_receipt,
    )
    from app.tasks.reaper import reap_orphans

    job_id = uuid.uuid4()
    generation = uuid.uuid4().hex
    plan = _required_speech_plan(str(job_id), generation)
    assert remove_cleanup_receipt(
        plan,
        CleanupReceiptLocator(
            field=RENDER_GENERATION_CLEANUP_FIELD,
            receipt_id=generation,
        ),
    )
    original = deepcopy(plan)
    app = _make_celery_with_inspect(active={}, reserved={})
    patch_ctx, session = _patch_sync_session(reaped_rows=[(job_id, plan)])

    with patch_ctx:
        assert reap_orphans(app) == 0

    assert plan == original
    assert session.execute.call_count == 1  # locking SELECT only; no terminal UPDATE
    session.commit.assert_called_once()
    _stub_post_terminal_storage_cleanup.assert_not_called()


def test_reaper_defers_required_speech_while_upload_lease_is_fresh(
    _stub_post_terminal_storage_cleanup,
) -> None:
    from app.tasks.reaper import reap_orphans

    job_id = uuid.uuid4()
    generation = uuid.uuid4().hex
    plan = _required_speech_plan(str(job_id), generation, close_uploads=False)
    original = deepcopy(plan)
    app = _make_celery_with_inspect(active={}, reserved={})
    patch_ctx, session = _patch_sync_session(reaped_rows=[(job_id, plan)])

    with patch_ctx:
        assert reap_orphans(app) == 0

    assert plan == original
    assert session.execute.call_count == 1
    _stub_post_terminal_storage_cleanup.assert_not_called()


def test_reaper_no_variant_reconciliation_when_no_rows():
    """When the reaper reaped zero rows, no variant reconciliation runs."""
    from app.tasks.reaper import reap_orphans

    app = _make_celery_with_inspect(active={}, reserved={})
    patch_ctx, session = _patch_sync_session(rowcount=0)
    with patch_ctx:
        assert reap_orphans(app) == 0
    # Only the locking SELECT when no candidates were found.
    assert session.execute.call_count == 1


def test_reaper_no_variant_reconciliation_when_assembly_plan_is_none():
    """Jobs without assembly_plan (not yet at render step) skip variant pass."""
    import uuid as _uuid

    from app.tasks.reaper import reap_orphans

    app = _make_celery_with_inspect(active={}, reserved={})
    patch_ctx, session = _patch_sync_session(
        rowcount=1,
        reaped_rows=[(_uuid.uuid4(), None)],  # assembly_plan=None
    )
    with patch_ctx:
        count = reap_orphans(app)

    assert count == 1
    # SELECT + terminal UPDATE. No separate variant reconciliation write is needed.
    assert session.execute.call_count == 2


# ---------------------------------------------------------------------------
# reconcile_stuck_variants (W6 — frozen-spinner watchdog)
# ---------------------------------------------------------------------------


def _patch_sync_session_for_reconcile(candidate_rows):
    """Model ID discovery, one locked re-read, and an update per candidate."""
    normalized_rows = [(*row, []) if len(row) == 2 else row for row in candidate_rows]
    locked_rows = iter(normalized_rows)
    session = MagicMock()

    def execute(statement):
        sql = str(statement)
        result = MagicMock()
        if getattr(statement, "is_update", False):
            result.rowcount = 1
        elif "FOR UPDATE" in sql:
            result.fetchone.return_value = next(locked_rows, None)
        else:
            result.scalars.return_value.all.return_value = [row[0] for row in normalized_rows]
        return result

    session.execute.side_effect = execute
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return patch("app.tasks.reaper.sync_session", return_value=ctx), session


class TestFinalizeStuckVariant:
    def test_stuck_with_video_becomes_ready(self):
        from app.tasks.reaper import _finalize_stuck_variant

        out = _finalize_stuck_variant(
            {"variant_id": "song_text", "render_status": "rendering", "video_path": "g/x.mp4"}
        )
        assert out["render_status"] == "ready"
        assert out["ok"] is True

    def test_stuck_without_video_becomes_failed(self):
        from app.tasks.reaper import _finalize_stuck_variant

        out = _finalize_stuck_variant({"variant_id": "song_lyrics", "render_status": "pending"})
        assert out["render_status"] == "failed"
        assert out["ok"] is False
        assert out["error"]

    def test_terminal_variant_unchanged(self):
        from app.tasks.reaper import _finalize_stuck_variant

        v = {"variant_id": "x", "render_status": "ready"}
        assert _finalize_stuck_variant(v) is v


class TestReconcileStuckVariants:
    def test_no_op_when_inspect_fails(self):
        from app.tasks.reaper import reconcile_stuck_variants

        app = _make_celery_with_inspect(raises=ConnectionError("redis down"))
        with patch("app.tasks.reaper.sync_session") as mock_session:
            assert reconcile_stuck_variants(app) == 0
            mock_session.assert_not_called()

    def test_pre_computed_live_param_skips_internal_inspect(self):
        """`live=` param (sweep_stale_jobs consolidation) must bypass inspect()."""
        from app.tasks.reaper import reconcile_stuck_variants

        # inspect() raises if called at all — proves the function never
        # touches the broker when a pre-computed `live` set is provided.
        app = _make_celery_with_inspect(raises=AssertionError("inspect() should not fire"))
        patch_ctx, session = _patch_sync_session_for_reconcile([])
        with patch_ctx:
            assert reconcile_stuck_variants(app, live=set()) == 0
        # Reaching this line without an exception proves inspect() was never
        # invoked; the SELECT still ran (empty candidate set → 0 reconciled).
        assert session.execute.call_count == 1

    def test_flips_stuck_variant_and_commits(self):
        import uuid as _uuid

        from app.tasks.reaper import reconcile_stuck_variants

        app = _make_celery_with_inspect(active={}, reserved={})
        plan = {
            "variants": [
                {"variant_id": "song_text", "render_status": "rendering", "video_path": "g/x.mp4"},
                {"variant_id": "original_text", "render_status": "ready"},
            ]
        }
        patch_ctx, session = _patch_sync_session_for_reconcile([(_uuid.uuid4(), plan)])
        with patch_ctx:
            assert reconcile_stuck_variants(app) == 1
        # ID discovery + locked revalidation + one UPDATE.
        assert session.execute.call_count == 3
        session.commit.assert_called_once()

        select_sql = str(session.execute.call_args_list[0].args[0])
        lock_sql = str(session.execute.call_args_list[1].args[0])
        update_sql = str(session.execute.call_args_list[2].args[0])
        assert "jobs.status !=" in select_sql
        assert "FOR UPDATE" not in select_sql
        assert "@?" in select_sql
        assert "LIMIT" in select_sql
        assert "FOR UPDATE" in lock_sql
        assert "@?" in lock_sql
        assert "jobs.status !=" in update_sql

    def test_required_speech_generation_is_failed_before_path_based_promotion(self):
        from app.tasks.reaper import reconcile_stuck_variants

        job_id = uuid.uuid4()
        generation = uuid.uuid4().hex
        plan = _required_speech_plan(str(job_id), generation)
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session_for_reconcile([(job_id, plan)])

        with patch_ctx:
            assert reconcile_stuck_variants(app) == 1

        updated = _assembly_plan_from_update_call(session.execute.call_args_list[2])
        assert updated is not None
        variant = updated["variants"][0]
        assert variant["render_status"] == "failed"
        assert variant["ok"] is False
        assert "video_path" not in variant
        internal = updated["_speech_cleanup_internal"]
        assert "required_speech_generation_locks" not in internal
        assert "staged_render_results" not in internal
        assert internal["render_generation_cleanup_pending"][0]["upload_state"] == "closed"
        outcomes = _speech_cleanup_outcomes_from_update_call(session.execute.call_args_list[2])
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "failed_owned"
        assert outcomes[0]["render_generation_id"] == generation
        assert outcomes[0]["failure_class"] == "WorkerDied"

    def test_blocked_required_speech_recovery_never_falls_through_to_generic_promotion(self):
        from app.services.durable_attempt_cleanup import (
            RENDER_GENERATION_CLEANUP_FIELD,
            CleanupReceiptLocator,
            remove_cleanup_receipt,
        )
        from app.tasks.reaper import reconcile_stuck_variants

        job_id = uuid.uuid4()
        generation = uuid.uuid4().hex
        plan = _required_speech_plan(str(job_id), generation)
        assert remove_cleanup_receipt(
            plan,
            CleanupReceiptLocator(
                field=RENDER_GENERATION_CLEANUP_FIELD,
                receipt_id=generation,
            ),
        )
        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session_for_reconcile([(job_id, plan)])

        with patch_ctx:
            assert reconcile_stuck_variants(app) == 0

        assert session.execute.call_count == 2
        session.commit.assert_called_once()

    def test_skips_live_re_render(self):
        import uuid as _uuid

        from app.tasks.reaper import reconcile_stuck_variants

        jid = _uuid.uuid4()
        app = _make_celery_with_inspect(active={"w1": [{"args": [str(jid)]}]})
        plan = {"variants": [{"variant_id": "x", "render_status": "rendering"}]}
        patch_ctx, session = _patch_sync_session_for_reconcile([(jid, plan)])
        with patch_ctx:
            assert reconcile_stuck_variants(app) == 0
        # Discovery + locked revalidation only — the live job is never updated.
        assert session.execute.call_count == 2
        session.commit.assert_called_once()

    def test_non_job_live_task_ids_do_not_enter_uuid_discovery_bind(self):
        """Other Celery task args must not make the Job UUID query fail."""

        from app.tasks.reaper import reconcile_stuck_variants

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session_for_reconcile([])
        with patch_ctx:
            assert (
                reconcile_stuck_variants(
                    app,
                    live={"track:not-a-job", "not-a-uuid"},
                )
                == 0
            )

        discovery = session.execute.call_args_list[0].args[0]
        assert "jobs.id NOT IN" not in str(discovery)

    def test_no_update_when_all_variants_terminal(self):
        import uuid as _uuid

        from app.tasks.reaper import reconcile_stuck_variants

        app = _make_celery_with_inspect(active={}, reserved={})
        plan = {
            "variants": [
                {"variant_id": "x", "render_status": "ready"},
                {"variant_id": "y", "render_status": "failed"},
            ]
        }
        patch_ctx, session = _patch_sync_session_for_reconcile([(_uuid.uuid4(), plan)])
        with patch_ctx:
            assert reconcile_stuck_variants(app) == 0
        assert session.execute.call_count == 2  # discovery + locked revalidation
        session.commit.assert_called_once()

    def test_commits_each_candidate_before_locking_the_next(self):
        import uuid as _uuid

        from app.tasks.reaper import reconcile_stuck_variants

        app = _make_celery_with_inspect(active={}, reserved={})
        rows = [
            (
                _uuid.uuid4(),
                {"variants": [{"render_status": "rendering", "video_path": "g/a.mp4"}]},
            ),
            (
                _uuid.uuid4(),
                {"variants": [{"render_status": "pending", "video_path": "g/b.mp4"}]},
            ),
        ]
        patch_ctx, session = _patch_sync_session_for_reconcile(rows)

        with patch_ctx:
            assert reconcile_stuck_variants(app, batch_limit=2) == 2

        assert session.commit.call_count == 2
        statements = [str(call.args[0]) for call in session.execute.call_args_list]
        assert sum("FOR UPDATE" in statement for statement in statements) == 2
        assert sum(statement.lstrip().startswith("UPDATE") for statement in statements) == 2


class TestReconcileCancelledRequiredSpeech:
    @staticmethod
    def _session_for(job):
        session = MagicMock()
        session.get.return_value = job
        context = MagicMock()
        context.__enter__.return_value = session
        context.__exit__.return_value = False
        return patch("app.tasks.reaper.sync_session", return_value=context), session

    def test_fresh_writing_owner_is_retained_byte_for_byte(self):
        from app.tasks.reaper import reconcile_cancelled_required_speech_job

        job_id = uuid.uuid4()
        generation = uuid.uuid4().hex
        plan = _required_speech_plan(str(job_id), generation, close_uploads=False)
        original = deepcopy(plan)
        job = SimpleNamespace(
            id=job_id,
            status="cancelled",
            assembly_plan=plan,
            pipeline_trace=[],
        )
        patch_ctx, session = self._session_for(job)

        with patch_ctx:
            decision = reconcile_cancelled_required_speech_job(job_id)

        assert decision.status == "deferred"
        assert decision.reason == "generation_uploads_still_active"
        assert job.status == "cancelled"
        assert job.assembly_plan == original
        assert job.pipeline_trace == []
        session.commit.assert_not_called()

    @pytest.mark.parametrize("upload_proof", ["closed", "expired"])
    def test_safe_owner_terminalizes_without_changing_cancelled_status(self, upload_proof):
        from app.tasks.reaper import reconcile_cancelled_required_speech_job

        job_id = uuid.uuid4()
        generation = uuid.uuid4().hex
        plan = _required_speech_plan(
            str(job_id),
            generation,
            close_uploads=upload_proof == "closed",
        )
        if upload_proof == "expired":
            receipt = plan["_speech_cleanup_internal"]["render_generation_cleanup_pending"][0]
            receipt["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        job = SimpleNamespace(
            id=job_id,
            status="cancelled",
            assembly_plan=plan,
            pipeline_trace=[],
        )
        patch_ctx, session = self._session_for(job)

        with patch_ctx:
            decision = reconcile_cancelled_required_speech_job(job_id)

        assert decision.status == "terminalized"
        assert job.status == "cancelled"
        assert job.assembly_plan["variants"][0]["render_status"] == "failed"
        internal = job.assembly_plan["_speech_cleanup_internal"]
        assert "required_speech_generation_locks" not in internal
        assert "staged_render_results" not in internal
        assert internal["render_generation_cleanup_pending"][0]["upload_state"] == "closed"
        outcomes = _speech_cleanup_outcomes(job.pipeline_trace)
        assert len(outcomes) == 1
        assert outcomes[0]["outcome"] == "cancelled_owned"
        assert outcomes[0]["render_generation_id"] == generation
        assert outcomes[0]["failure_phase"] is None
        assert outcomes[0]["failure_class"] is None
        session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Render-worker autostop interaction (Fly cost-cut plan, PR2)
#
# The render worker now spends most of its life stopped. A stopped worker
# is INVISIBLE to inspect() — it isn't connected to the broker at all — which
# looks identical to a broker hiccup from the reaper's point of view: both
# produce an empty/failed live-job read. The two scenarios below must be
# tested SEPARATELY (not assumed to be covered by each other), because they
# take different code paths: a broker hiccup makes `_live_job_ids` return
# None (reap_orphans no-ops entirely, already covered by
# TestReapOrphans.test_no_op_when_inspect_fails above); a genuinely-stopped-
# but-otherwise-healthy worker makes inspect() SUCCEED with an empty result
# (live=set()), so reap_orphans proceeds to build and run the UPDATE — and
# the only thing standing between that UPDATE and a false-positive reap of a
# legitimately queued job is `_NON_TERMINAL_STATUSES` excluding "queued".
# That exclusion predates this plan (for a different reason — see the
# constant's docstring), but this plan is what makes the "worker stopped for
# minutes at a time" condition a routine, not an anomaly, so the invariant
# needs its own explicit, permanent test.
# ---------------------------------------------------------------------------


class TestQueuedJobSafeUnderStoppedRenderWorker:
    def test_broker_hiccup_reap_orphans_is_a_full_no_op(self):
        """Scenario 1: inspect() itself fails (broker unreachable).

        `_live_job_ids` returns None → reap_orphans returns 0 without ever
        building a SQL statement. Restated here (not just relying on
        TestReapOrphans.test_no_op_when_inspect_fails) so this whole class
        reads as the complete pair of scenarios for the autostop feature.
        """
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(raises=ConnectionError("redis down"))
        with patch("app.tasks.reaper.sync_session") as mock_session:
            assert reap_orphans(app) == 0
            mock_session.assert_not_called()

    def test_worker_genuinely_stopped_queued_status_excluded_from_sql(self):
        """Scenario 2: inspect() SUCCEEDS but reports nothing live — because
        the render worker machine is stopped (autostop), not because the
        broker is down. This is the worse case: reap_orphans does NOT
        short-circuit here (live=set() is a normal, valid result), so it
        proceeds to build and run the UPDATE. A queued job must still never
        match it.

        Asserts at the compiled-SQL level, not just against the
        `_NON_TERMINAL_STATUSES` constant — this is what actually runs
        against Postgres, and a future refactor that builds the WHERE
        clause from a different source than the constant would still be
        caught here.
        """
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=0)
        with patch_ctx:
            reap_orphans(app)

        stmt = session.execute.call_args[0][0]
        bound = stmt.compile().params.values()
        status_lists = [v for v in bound if isinstance(v, list | tuple)]
        assert status_lists, "expected a status IN(...) bound parameter list"
        assert not any("queued" in status_list for status_list in status_lists), (
            "A job at status=queued must never appear in the reap UPDATE's "
            "status filter, even when the render worker is genuinely stopped "
            "(inspect() succeeds with an empty live set) rather than "
            "unreachable — this is the scenario render-worker autostop makes "
            "routine, not exceptional."
        )

    def test_worker_genuinely_stopped_does_not_short_circuit_like_a_hiccup(self):
        """Confirms the two scenarios are NOT the same code path: an empty-
        but-successful inspect() result must still reach the DB (unlike a
        real inspect() failure, which must not). If a future change made
        "worker stopped" also short-circuit like a hiccup, this test would
        catch it — and that would be a regression in the OPPOSITE direction
        (an orphan from an actually-crashed worker would never get reaped).
        """
        from app.tasks.reaper import reap_orphans

        app = _make_celery_with_inspect(active={}, reserved={})
        patch_ctx, session = _patch_sync_session(rowcount=0)
        with patch_ctx:
            reap_orphans(app)
        session.execute.assert_called_once()
