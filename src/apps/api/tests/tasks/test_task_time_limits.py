"""Cross-task invariant: every long-running render orchestrator's Celery
`time_limit` (and `soft_time_limit`) must stay strictly UNDER the broker's
`visibility_timeout` (worker.py).

Why this exists — prod incident 08532ba3 (2026-06-01). `orchestrate_generative_job`
ran with `time_limit=2000` while the worker's `broker_transport_options`
`visibility_timeout=1900`. With `task_acks_late=True`, a job still in-flight in the
1900-2000s window was redelivered to a SECOND worker while the first was still
running. Two concurrent runs each pre-tonemapped the same HDR clips into the
RAM-backed `/tmp` (tmpfs on Fly Firecracker) and the job died with
`[Errno 28] No space left on device`. #419's `_NO_RERUN_STATUSES` guard could not
help: during that window the first run's status is still `processing`/`rendering`
(not terminal), so nothing was no-op'd. The fix lowers the render orchestrators to
`soft=1740, hard=1800` so the SOFT limit fails the job terminal BEFORE the broker
redelivers — and this test locks the invariant so the next decorator that drifts
fails here instead of in prod.

Source-inspection by raw file read (NOT `inspect.getsource` on the imported
module) keeps this test free of celery/sqlalchemy/fastapi imports — it runs in
milliseconds and never pays the app-import cost. The decorator-extraction helpers
are adapted from `test_music_orchestrate.py` (the `tasks.`-prefix assumption is
relaxed here to match a task's exact full `name=`). Dedup opportunity: lift these
three helpers into `tests/tasks/conftest.py` and import from both modules — left
for a follow-up to keep this fix tightly scoped.
"""

import io
import os
import re
import tokenize


def _read_source(rel_path: str) -> str:
    """Read a source file under src/apps/api/ as a raw string."""
    api_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
    with open(os.path.join(api_dir, rel_path), encoding="utf-8") as f:
        return f.read()


def _extract_celery_task_decorator(source: str, task_name: str) -> str:
    """Return the `@celery_app.task(...)` block whose `name=` is exactly `task_name`.

    Prefix-agnostic variant of `test_music_orchestrate._extract_celery_task_decorator`:
    matches the full `name="<task_name>"` so it works for both the `tasks.`-prefixed
    names (auto-music) and the bare generative task names. Walks backward to the
    enclosing `@celery_app.task(` and forward via Python's tokenizer to the matching
    close paren — tokenizing (not naive paren counting) is essential because decorator
    kwarg comments contain unbalanced parens in prose.
    """
    name_idx = source.index(f'name="{task_name}"')
    decorator_idx = source.rfind("@celery_app.task(", 0, name_idx)
    assert decorator_idx != -1, f"decorator not found for {task_name}"

    slice_src = source[decorator_idx:]
    tokens = tokenize.generate_tokens(io.StringIO(slice_src).readline)
    depth = 0
    saw_open = False
    for tok in tokens:
        if tok.type != tokenize.OP:
            continue
        if tok.string == "(":
            depth += 1
            saw_open = True
        elif tok.string == ")":
            depth -= 1
            if saw_open and depth == 0:
                end_row, end_col = tok.end
                lines = slice_src.splitlines(keepends=True)
                offset = sum(len(line) for line in lines[: end_row - 1]) + end_col
                return source[decorator_idx : decorator_idx + offset]
    raise AssertionError(f"unbalanced parens in decorator for {task_name}")


def _extract_limit_kwarg(decorator_src: str, kwarg_name: str) -> int:
    m = re.search(rf"\b{kwarg_name}\s*=\s*(\d+)", decorator_src)
    assert m, f"{kwarg_name} kwarg not found in decorator:\n{decorator_src}"
    return int(m.group(1))


def _extract_visibility_timeout(worker_src: str) -> int:
    m = re.search(r'"visibility_timeout"\s*:\s*(\d+)', worker_src)
    assert m, "visibility_timeout not found in worker.py broker_transport_options"
    return int(m.group(1))


# (source_path, full task name) for every long-running render orchestrator that
# runs the heavy `_assemble_clips` → reframe → burn → mix pipeline and is therefore
# at risk of an acks_late redelivery double-running it. NOT included:
# batch_import_from_drive (time_limit=2400 > 1900) — it is download-bound, can
# legitimately run >31min, and needs its own raise-visibility-timeout-or-guard
# decision rather than a budget cut.
_RENDER_ORCHESTRATORS = [
    ("app/tasks/generative_build.py", "orchestrate_generative_job"),
    ("app/tasks/generative_build.py", "regenerate_generative_variant"),
    ("app/tasks/auto_music_orchestrate.py", "tasks.orchestrate_auto_music_job"),
    # Footage-pool ingest: downloads + Gemini-uploads + analyzes up to 40 clips,
    # so it's in the same acks_late redelivery double-run risk class.
    ("app/tasks/content_plan_build.py", "app.tasks.content_plan_build.match_pool_clips"),
    # Vision-based style ingest: downloads + Gemini-uploads + analyzes up to 30 TikTok
    # videos — same download+upload risk class as match_pool_clips.
    ("app/tasks/style_vision_build.py", "app.tasks.style_vision_build.analyze_tiktok_style"),
]


def test_render_orchestrator_time_limits_under_visibility_timeout() -> None:
    visibility_timeout = _extract_visibility_timeout(_read_source("app/worker.py"))

    for rel_path, task_name in _RENDER_ORCHESTRATORS:
        decorator = _extract_celery_task_decorator(_read_source(rel_path), task_name)
        hard = _extract_limit_kwarg(decorator, "time_limit")
        soft = _extract_limit_kwarg(decorator, "soft_time_limit")

        assert hard < visibility_timeout, (
            f"{task_name}.time_limit ({hard}) >= broker visibility_timeout "
            f"({visibility_timeout}). With task_acks_late=True the broker redelivers "
            "a still-running job to a second worker once it exceeds visibility_timeout "
            "→ concurrent double-run of the same render → tmpfs /tmp exhaustion "
            "(prod 08532ba3, 2026-06-01). Keep time_limit strictly below "
            "visibility_timeout (worker.py)."
        )
        assert soft < visibility_timeout, (
            f"{task_name}.soft_time_limit ({soft}) >= broker visibility_timeout "
            f"({visibility_timeout}). The soft limit must fire — marking the job "
            "terminal — BEFORE the broker redelivers, so #419's _NO_RERUN_STATUSES "
            "guard can no-op the redelivery instead of letting it double-run."
        )
        assert soft <= hard, (
            f"{task_name}.soft_time_limit ({soft}) > time_limit ({hard}); the soft "
            "grace period must precede the hard SIGKILL."
        )
