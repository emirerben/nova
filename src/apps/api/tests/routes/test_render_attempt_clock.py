"""Render-attempt clock: every re-render dispatch restarts the user's timer.

A re-render reuses the SAME Job row. `job_phases.mark_started` refuses to move
`started_at` once set (correct — it models worker pickup of one orchestrator
run), and `render_started_at` was written in exactly ONE place repo-wide (the
initial render loop in `generative_build.py`). So before this change every clock
in the progress band stayed pinned to the FIRST render: a re-render of a
5-minute edit displayed "40m 32s", the ETA floored to "less than a minute", the
stall copy fired instantly, and the per-variant tile read "Rendering · 40:32".

What is locked in here:

  1. `stamp_variant_attempt` overwrites a stale `render_started_at` and mutates
     THE DICT IT IS GIVEN — the copy-loop dispatchers depend on that.
  2. `mark_reattempt` moves `started_at`, leaves `finished_at` alone (it is
     exported as the plan item's ready date and nothing would write it back), and
     SKIPS the move while an orchestrator run is in flight.
  3. `_mark_variant_rendering` keeps its gen-id contract for its 4 callers.
  4. A persist-only save does NOT mark rendering and does NOT move the clock.
  5. Every function that stamps a variant also restarts the job clock, enforced
     structurally (AST) rather than by grepping one spelling of one line.
"""

from __future__ import annotations

import ast
import sys
import types
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.models import Job
from app.routes import generative_jobs as gj
from app.services.job_phases import mark_reattempt, stamp_variant_attempt

STALE = "2020-01-01T00:00:00Z"


def _job(**variant_overrides) -> Job:
    """Detached Job row with one editable variant. No DB session needed."""
    variant = {
        "variant_id": "original_text",
        "render_status": "ready",
        "render_started_at": STALE,
        "render_finished_at": "2020-01-01T00:05:00Z",
        "text_mode": "agent_text",
        "music_track_id": "track-1",
        "mix": 0.5,
    }
    variant.update(variant_overrides)
    job = Job(id=uuid.uuid4())
    job.assembly_plan = {"variants": [variant]}
    # The first render's clock: started 40 minutes ago, finished 35 minutes ago.
    job.started_at = datetime.now(UTC) - timedelta(minutes=40)
    job.finished_at = datetime.now(UTC) - timedelta(minutes=35)
    # No orchestrator run in flight — mark_finished cleared it.
    job.current_phase = None
    return job


# ── stamp_variant_attempt ─────────────────────────────────────────────────────


def test_stamp_overwrites_a_stale_render_started_at():
    variant = {"variant_id": "v", "render_status": "ready", "render_started_at": STALE}
    stamp_variant_attempt(variant)
    assert variant["render_status"] == "rendering"
    assert variant["render_started_at"] != STALE
    # Frozen wire format for this JSONB field: naive UTC + a literal Z.
    assert variant["render_started_at"].endswith("Z")
    assert "+00:00" not in variant["render_started_at"]
    parsed = datetime.fromisoformat(variant["render_started_at"].replace("Z", "+00:00"))
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 60


def test_stamp_does_not_write_render_enqueued_at():
    """`render_enqueued_at`'s only writer is `_mark_variant_rendering`, which owns
    the caption-reburn supersession token."""
    variant = {"variant_id": "v", "render_status": "ready"}
    stamp_variant_attempt(variant)
    assert "render_enqueued_at" not in variant


def test_stamp_mutates_the_dict_it_is_given_not_a_lookup():
    """REGRESSION. `persist_user_timeline` and `prepare_editor_commit` build a copy
    (`updated = dict(v)`) and then do `variants[i] = updated`. A helper that
    re-walked the job's variant list would mutate the ORIGINAL and have its write
    silently thrown away by that assignment — no error, no failing test."""
    original = {"variant_id": "v", "render_status": "ready", "render_started_at": STALE}
    copy = dict(original)
    stamp_variant_attempt(copy)
    assert copy["render_status"] == "rendering"
    assert copy["render_started_at"] != STALE
    assert original["render_status"] == "ready"
    assert original["render_started_at"] == STALE


# ── mark_reattempt ────────────────────────────────────────────────────────────


def test_reattempt_moves_started_at_to_now():
    job = _job()
    before = job.started_at
    assert mark_reattempt(job) is True
    assert job.started_at > before
    assert (datetime.now(UTC) - job.started_at).total_seconds() < 5


def test_reattempt_leaves_finished_at_alone():
    """REGRESSION. `plan_items.py` exports `current_job.finished_at` as the plan
    item's ready date, and no re-render task calls `mark_finished` — nulling it
    would erase that date permanently. Readers guard on
    `started_at > finished_at` instead."""
    job = _job()
    finished_before = job.finished_at
    mark_reattempt(job)
    assert job.finished_at == finished_before


def test_reattempt_skips_while_an_orchestrator_run_is_in_flight():
    """REGRESSION. `require_editable_variant` is PER-VARIANT and only the timeline
    routes carry a job-wide busy gate, so a user can edit variant A while B and C
    are still on the FIRST render. Moving the anchor there would make every later
    `record_phase` compute `t_offset_ms` against a new origin (a non-monotonic
    `phase_log`) and visibly reset the clock the user is watching for B and C."""
    job = _job()
    job.current_phase = "assemble"
    before = job.started_at
    assert mark_reattempt(job) is False
    assert job.started_at == before


def test_reattempt_is_timezone_aware():
    """`Job.started_at` is TIMESTAMPTZ; a naive datetime would raise on compare."""
    job = _job()
    mark_reattempt(job)
    assert job.started_at.tzinfo is not None


# ── _mark_variant_rendering (the 4 caption/bed-level callers) ─────────────────


def test_mark_variant_rendering_keeps_its_gen_id_contract():
    """REGRESSION. Its 4 callers pass the returned token to the task as
    `render_gen_id`; `_update_variant_entry` discards any worker write whose
    expected token differs from the stored one. Breaking this strands the variant
    in "rendering" forever."""
    job = _job()
    token = gj._mark_variant_rendering(job, "original_text")
    variant = job.assembly_plan["variants"][0]
    assert token
    assert variant["render_generation_id"] == token


def test_mark_variant_rendering_stamps_and_restarts_the_clock():
    job = _job()
    before = job.started_at
    gj._mark_variant_rendering(job, "original_text")
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "rendering"
    assert variant["render_started_at"] != STALE
    assert job.started_at > before


def test_mark_variant_rendering_still_writes_render_enqueued_at():
    job = _job()
    gj._mark_variant_rendering(job, "original_text")
    assert job.assembly_plan["variants"][0]["render_enqueued_at"]


def test_mark_variant_rendering_unknown_variant_leaves_the_variant_alone():
    job = _job()
    gj._mark_variant_rendering(job, "does-not-exist")
    assert job.assembly_plan["variants"][0]["render_status"] == "ready"


# ── the dispatchers ───────────────────────────────────────────────────────────


@pytest.fixture
def _no_celery(monkeypatch):
    """Swallow the `.delay()` / `.apply_async()` calls the dispatchers make.

    Injects a STUB module into `sys.modules` rather than importing the real
    `app.tasks.generative_build` and patching an attribute on it. That module is
    ~13k lines and drags the whole ML stack (torch / whisper / mediapipe) into the
    process; importing it here made the Skia render tests in `tests/pipeline/`
    blow their 30s pytest-timeout budget purely from the slower, fatter
    interpreter. These are route-layer dict-mutation tests — they have no business
    loading the renderer.

    Every dispatcher does a function-local `from app.tasks.generative_build import
    regenerate_generative_variant`, which honours this sys.modules entry. The
    `sent` list is asserted non-empty by each test so a bypassed stub fails loudly
    instead of silently reaching a real broker.
    """
    sent: list[tuple] = []

    class _Task:
        name = "regenerate_generative_variant"

        def delay(self, *a, **k):
            sent.append((a, k))

        def apply_async(self, *a, **k):
            sent.append((a, k))

    stub = types.ModuleType("app.tasks.generative_build")
    stub.regenerate_generative_variant = _Task()
    # dispatch_edit_variant's function-local import also pulls in the
    # tri-state CAROUSEL_MOMENT_UNSET sentinel (see generative_build.py) —
    # any distinct object works here since these tests never pass a
    # carousel_moment_override, so the sentinel value itself is never asserted.
    stub.CAROUSEL_MOMENT_UNSET = object()
    monkeypatch.setitem(sys.modules, "app.tasks.generative_build", stub)
    return sent


def _assert_attempt_restarted(job: Job, before, sent: list) -> None:
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "rendering"
    assert variant["render_started_at"] != STALE
    assert job.started_at > before
    assert sent, "dispatcher never enqueued — the stub was bypassed"


def test_dispatch_retext_restarts_the_clock(_no_celery):
    job = _job()
    before = job.started_at
    gj.dispatch_retext(job, "original_text", text="hello", remove=False)
    _assert_attempt_restarted(job, before, _no_celery)


def test_dispatch_change_style_restarts_the_clock(_no_celery):
    from app.pipeline.style_sets import style_set_ids

    job = _job()
    before = job.started_at
    gj.dispatch_change_style(
        job, "original_text", style_set_id=next(iter(style_set_ids(applies_to="generative")))
    )
    _assert_attempt_restarted(job, before, _no_celery)


def test_dispatch_set_intro_size_restarts_the_clock(_no_celery):
    job = _job()
    before = job.started_at
    gj.dispatch_set_intro_size(job, "original_text", text_size_px=90)
    _assert_attempt_restarted(job, before, _no_celery)


def test_dispatch_set_intro_timing_restarts_the_clock(_no_celery):
    job = _job()
    before = job.started_at
    gj.dispatch_set_intro_timing(job, "original_text", start_s=0.0, end_s=2.0)
    _assert_attempt_restarted(job, before, _no_celery)


def test_dispatch_edit_variant_restarts_the_clock(_no_celery):
    """The combined edit endpoint — what the pocket editor's Save hits."""
    job = _job()
    before = job.started_at
    gj.dispatch_edit_variant(
        job,
        "original_text",
        text="new hook",
        remove_text=False,
        style_set_id=None,
        text_size_px=None,
    )
    _assert_attempt_restarted(job, before, _no_celery)


def test_dispatch_set_mix_restarts_the_clock(_no_celery):
    """REGRESSION (review finding). This dispatcher wrote NO render_status at all,
    so a mix/background-sound Save left both clocks reading the first render's
    timestamps and never closed the 409 re-entrancy gate."""
    job = _job()
    before = job.started_at
    gj.dispatch_set_mix(job, "original_text", mix=0.8)
    _assert_attempt_restarted(job, before, _no_celery)


# ── the copy-loop dispatchers (the silent-no-op trap) ─────────────────────────


class _FakeDb:
    """Minimal AsyncSession stand-in for `persist_user_timeline`."""

    def __init__(self, job: Job) -> None:
        self._job = job
        self.commits = 0

    async def get(self, _model, _pk, **_kw):
        return self._job

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_persist_user_timeline_stamp_survives_the_copy():
    """REGRESSION. Builds `updated = dict(v)` then assigns `variants[i] = updated`.
    A stamp written to the original `v` would be discarded here with no error."""
    job = _job()
    before = job.started_at
    db = _FakeDb(job)
    await gj.persist_user_timeline(
        db,
        str(job.id),
        "original_text",
        [{"slot_id": "s1", "duration_s": 2.0}],
        render_gen_id=uuid.uuid4().hex,
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "rendering"
    assert variant["render_started_at"] != STALE
    assert job.started_at > before


@pytest.mark.asyncio
async def test_persist_only_timeline_save_does_not_touch_the_clock():
    """REGRESSION. The rendering mark is inside `if render_gen_id is not None:`
    because the same function serves a persist-only save. Marking unconditionally
    would 409-lock the variant against further edits."""
    job = _job()
    before = job.started_at
    db = _FakeDb(job)
    await gj.persist_user_timeline(
        db,
        str(job.id),
        "original_text",
        [{"slot_id": "s1", "duration_s": 2.0}],
        render_gen_id=None,
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "ready"
    assert variant["render_started_at"] == STALE
    assert job.started_at == before


def test_prepare_editor_commit_stamp_survives_the_copy():
    """REGRESSION. The PRIMARY Save path (item page + pocket editor) also builds
    `updated = dict(v)` and then assigns `variants[i] = updated`. A stamp written
    to the original `v` is silently discarded — no error, no failing test, and the
    user's timer keeps counting from the first render.

    The AST guard cannot see this: `prepare_editor_commit` calls BOTH helpers, so
    it stays paired even when the stamp lands on the wrong dict.
    """
    job = _job()
    before = job.started_at
    gj.prepare_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(
            mix=gj.EditorCommitMix(music_level=0.8),
            base_generation="2020-01-01T00:05:00Z",
        ),
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["render_status"] == "rendering"
    assert variant["render_started_at"] != STALE
    assert job.started_at > before


def test_prepare_editor_commit_title_only_leaves_the_clock_alone():
    """A title-only commit has no render section (`new_gen is None`), kicks no
    render, and must therefore not stamp the variant or move the clock — doing so
    would 409-lock the variant and restart a timer for work that never runs."""
    job = _job()
    before = job.started_at
    prep = gj.prepare_editor_commit(
        job,
        "original_text",
        gj.EditorCommitRequest(title="A new title", base_generation="2020-01-01T00:05:00Z"),
    )
    variant = job.assembly_plan["variants"][0]
    assert prep["has_render_section"] is False
    assert variant["render_status"] == "ready"
    assert variant["render_started_at"] == STALE
    assert job.started_at == before


# ── the render / persist-only fork inside ONE dispatcher ──────────────────────


def test_dispatch_set_text_elements_render_restarts_the_clock(_no_celery):
    job = _job(base_video_path="generative-jobs/x/base.mp4")
    before = job.started_at
    gj.dispatch_set_text_elements(
        job,
        "original_text",
        elements=[{"id": "e1", "text": "hi", "start_s": 0.0, "end_s": 1.0}],
        render=True,
    )
    _assert_attempt_restarted(job, before, _no_celery)


def test_dispatch_set_text_elements_persist_only_leaves_the_clock_alone(_no_celery):
    """REGRESSION. Both the stamp and `mark_reattempt` sit behind `if render:` in
    the SAME function, so the AST pairing guard passes either way — only this test
    pins that a persist-only save (render=False) enqueues nothing, does not
    409-lock the variant, and does not restart the user's timer.
    """
    job = _job()
    before = job.started_at
    gj.dispatch_set_text_elements(
        job,
        "original_text",
        elements=[{"id": "e1", "text": "hi", "start_s": 0.0, "end_s": 1.0}],
        render=False,
    )
    variant = job.assembly_plan["variants"][0]
    assert variant["text_elements"], "the elements must still persist"
    assert variant["render_status"] == "ready"
    assert variant["render_started_at"] == STALE
    assert job.started_at == before
    assert not _no_celery, "a persist-only save must not enqueue a render"


def test_reattempt_moves_the_clock_on_a_job_stand_in_without_current_phase():
    """`mark_reattempt` reads `current_phase` through `getattr(..., None)` because
    dispatchers are also called with lightweight stand-ins (the SimpleNamespace
    jobs in `test_editor_commit.py`). A missing attribute means "no run in
    flight" — the clock moves rather than raising AttributeError mid-Save."""
    stand_in = types.SimpleNamespace(started_at=datetime.now(UTC) - timedelta(minutes=40))
    assert mark_reattempt(stand_in) is True
    assert (datetime.now(UTC) - stand_in.started_at).total_seconds() < 5


# ── structural guard: stamping and restarting must stay paired ────────────────


def _functions_calling(tree: ast.Module, name: str) -> set[str]:
    out: set[str] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                if call.func.id == name:
                    out.add(fn.name)
    return out


ATTEMPT_DISPATCH_MODULES = [
    "src/apps/api/app/routes/generative_jobs.py",
    "src/apps/api/app/tasks/autoplace.py",
]


def _repo_root() -> Path:
    # .../src/apps/api/tests/routes/<this file> -> parents: routes, tests, api,
    # apps, src, <repo root>.
    root = Path(__file__).resolve().parents[5]
    assert (root / "src/apps/api/app").is_dir(), f"repo root misresolved: {root}"
    return root


@pytest.mark.parametrize("rel_path", ATTEMPT_DISPATCH_MODULES)
def test_every_stamping_function_also_restarts_the_job_clock(rel_path):
    """The original bug existed because 14 hand-rolled `render_status = "rendering"`
    blocks had no choke point, so the timestamp was forgotten in 10 of them.

    Structural (AST) rather than a source grep: the previous version of this guard
    matched one exact spelling of one line, so `v.update({"render_status": ...})`,
    a module constant, a multi-line dict literal, or the same write in ANOTHER
    module all slipped through — and `tasks/autoplace.py` did exactly that.
    """
    tree = ast.parse((_repo_root() / rel_path).read_text())
    stampers = _functions_calling(tree, "stamp_variant_attempt")
    resetters = _functions_calling(tree, "mark_reattempt")
    missing = sorted(stampers - resetters)
    assert not missing, (
        f"{rel_path}: {missing} stamp a variant attempt but never restart the job "
        "clock — the user's timer would keep counting from the first render."
    )


def test_no_module_marks_a_variant_rendering_outside_the_helper():
    """Any NEW write of render_status="rendering" in a DISPATCH module must go
    through `stamp_variant_attempt`, or the clock silently stops restarting.

    Scoped to the route/task dispatch modules: `tasks/generative_build.py` is the
    worker and legitimately sets in-flight statuses of its own, and the reaper
    rewrites stranded rows.
    """
    offenders: list[str] = []
    for rel_path in ATTEMPT_DISPATCH_MODULES:
        tree = ast.parse((_repo_root() / rel_path).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "render_status"
                    and isinstance(node.value, ast.Constant)
                    and node.value.value == "rendering"
                ):
                    offenders.append(f"{rel_path}:{node.lineno}")
    assert not offenders, (
        "render_status set to 'rendering' outside stamp_variant_attempt at "
        f"{offenders} — use the helper so the attempt clock restarts with it."
    )
