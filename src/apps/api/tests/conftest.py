"""Set required env vars before any app module is imported.

app/config.py instantiates Settings() at module load — required fields must
be present in the environment at collection time, not just at test runtime.
"""

import gc
import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

_PAID_KEY_PLACEHOLDERS = {"", "ci-noop", "test", "test-key", "dummy"}
if (
    os.environ.get("CI")
    and os.environ.get("GEMINI_API_KEY", "").strip() not in _PAID_KEY_PLACEHOLDERS
    and os.environ.get("NOVA_PAID_AI_WORKFLOW") != "1"
):
    pytest.exit(
        "Paid Gemini credentials are forbidden in routine CI. "
        "Use the named agent-evals live workflow.",
        returncode=2,
    )

# Per-test timing rows for CI; does nothing unless NOVA_CI_TIMES_DIR is set.
pytest_plugins = ("tests._ci_times",)

os.environ.setdefault("STORAGE_BUCKET", "nova-test")
os.environ.setdefault("STORAGE_PROVIDER", "gcs")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/nova_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("TOKEN_ENCRYPTION_KEY", "test-key-not-used-in-unit-tests")
os.environ.setdefault("WHISPER_BACKEND", "local")
os.environ.setdefault("WAITLIST_ADMIN_SECRET", "test-admin-secret")
os.environ.setdefault("ALLOWED_ORIGINS", '["http://localhost:3000"]')
# Strict plan-route auth fails closed when INTERNAL_API_KEY is unset, so the
# test env sets it explicitly — strict-path tests must pass the matching bearer
# to exercise the real check (rather than relying on a fail-open bypass).
os.environ.setdefault("INTERNAL_API_KEY", "test-internal-key")

# One database per xdist worker where the runner made them (KRI-1534 train; off by default).
# Here, before any app module reads DATABASE_URL.
from tests import _worker_database  # noqa: E402

_worker_database.select(os.environ)


@pytest.fixture
def prod_profile(monkeypatch):
    """Settings as production runs them (see tests/_prod_profile.py).

    Use in any test asserting that something renders on the iPhone in
    production: defaults-only tests passed twice while the real device failed.
    """

    from tests._prod_profile import apply_prod_profile  # noqa: PLC0415

    return apply_prod_profile(monkeypatch)


@pytest.fixture
def prod_profile_pre_cutover(monkeypatch):
    """`prod_profile` with the device-only cutover and brief binding held at code defaults."""

    from tests._prod_profile import PRE_CUTOVER_SETTINGS, apply_prod_profile  # noqa: PLC0415

    return apply_prod_profile(monkeypatch, hold_at_default=PRE_CUTOVER_SETTINGS)


@pytest.fixture(autouse=True)
def _fresh_async_engine_pool() -> None:
    """Start every test with an empty pool on the shared async engine.

    pytest-asyncio gives each test its own event loop, and an asyncpg connection is
    bound to the loop that opened it. A module that returns connections to
    ``app.database.engine``'s pool without disposing it leaves them for the next
    DB-backed async test on the same xdist worker, which then fails with "got Future
    attached to a different loop". Which module runs next depends on how CI shards
    the files, so the failure moves whenever a test file is added (#1539 hit it twice).
    Swapping the pool without closing it needs no I/O and no event loop.
    """

    database = sys.modules.get("app.database")
    if database is not None:
        database.engine.sync_engine.dispose(close=False)


@pytest.fixture
def release_abandoned_db_connections():
    """Close, after the test, the asyncpg connections the pool swap above only abandons.

    ``dispose(close=False)`` leaves them open until the garbage collector finalizes them,
    so a file whose tests each open several (the intent-review suites) piles up well past
    Postgres's default 100 connections across four xdist workers
    (``TooManyConnectionsError``). Opt in with ``pytestmark = pytest.mark.usefixtures(...)``;
    a collection per test is too slow to run for every DB-backed test.
    """

    yield
    gc.collect()


@pytest.fixture
def exclusive_intent_review_ledger():
    """Hold one cross-worker lock while a test owns the shared intent-review state.

    That state is every ``ai_cost_reservations`` row whose feature is ``intent_review`` and
    the whole ``creator_generation_reviews`` table. On one xdist database both are shared:

    - the ``paid`` fixture deletes every ``intent_review`` reservation, so an unlocked user
      removed a running review's reservation and the review ended ``unavailable`` before
      its provider call (``KeyError: 'raw_output'``);
    - the review sweeps (``reconcile_generation_reviews``, ``recover_intent_reviews``,
      ``post_unposted_outcomes``) act on every row in the table, so an unlocked sweep with
      the paid flag off stamped ``skipped:tier_off`` on another test's unposted outcome;
    - rows an earlier test left behind are still sweep candidates, so a later test's sweep
      posted or skipped them and its own counts included them.

    A session-level advisory lock on its own connection serializes every test that opts in
    (``pytestmark``, or through ``paid``). Every test that creates review rows or runs a
    review sweep must hold it; then any row present when the lock is taken is a leftover,
    and deleting them all lets each sweep see only the rows of the test that runs it.
    """

    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(db_url, pool_pre_ping=True)
    connection = None
    try:
        connection = engine.connect()
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": 2026110301})
    except OperationalError as exc:
        if connection is not None:
            connection.close()
        engine.dispose()
        pytest.skip(f"Postgres not reachable for the intent_review ledger lock: {exc!r}")
    try:
        with engine.begin() as purge:
            purge.execute(text("DELETE FROM creator_generation_reviews"))
        yield
    finally:
        try:
            connection.close()
        finally:
            engine.dispose()


@pytest.fixture
def exclusive_slide_post_delivery():
    """Hold one cross-worker lock while a test owns the slide-post delivery state.

    That state is every ``slide_post_generation_receipts`` and
    ``slide_post_render_revisions`` row. On one xdist database it is shared, and the
    delivery sweeps (``reconcile_generations``, ``reconcile_revisions``) act on every
    pending row, so a sweep in one test settles the revision another test is about to
    observe (``assert 'skip' == 'published'``), or claims it before that test's
    kill-worker child can (KRI-1545).

    xdist's default ``load`` mode hands tests out in large consecutive chunks, so the
    tests of one file usually share a worker and never meet. Where a chunk ends moves
    with every test added before it: with the file split 35/13 across two workers these
    tests failed in 3 of 5 CI repeats (run 44651481740). A session-level advisory lock
    on its own connection does not depend on the scheduler, and Postgres releases it if
    the worker dies.
    """

    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(db_url, pool_pre_ping=True)
    # Collection already handles an intentionally absent test DB. Once these tests
    # are collected, failure to acquire their lock must fail rather than skip coverage.
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": 2026112001})
            yield
    finally:
        engine.dispose()


@pytest.fixture
def exclusive_notification_delivery():
    """Hold one cross-worker lock while a test owns the notification delivery state.

    That state is every ``notification_installations`` and ``notification_deliveries`` row.
    On one xdist database it is shared, in two ways:

    - an active device token belongs to one installation (the unique index on its
      fingerprint), and the tests register one fixed token, so two workers in this state at
      once refuse each other with 409. Main run 45430423656 failed 14 tests that way, seven
      on each of two workers inside three seconds;
    - ``discover_deliveries`` acts on every pending activity and installation, so a sweep in
      one test creates the delivery row another test is about to insert by hand
      (``uq_notification_delivery_pair``, hosted run 45469233667).

    Same class and same remedy as ``exclusive_slide_post_delivery`` above (KRI-1545). One
    database per worker would remove all of these locks.
    """

    if os.environ.get("EXP_NO_NOTIFICATION_LOCK") == "1":  # throwaway red control
        yield
        return
    db_url = os.environ["DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")
    engine = create_engine(db_url, pool_pre_ping=True)
    # Once these tests are collected, failure to acquire their lock must fail rather than skip.
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": 2026112401})
            yield
    finally:
        engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_client_request_context() -> None:
    """Start every test outside an HTTP request with no declared client protocol.

    `app.services.client_protocol` keeps the `X-Kria-Client-Protocol` gate in two
    ContextVars. Several route tests set it (`set_client_protocol(...)`) and never clear
    it, and the leaked "inside a request, protocol N" state then closes `require_client`
    gates such as `phone_voiceover_editor_lanes_supported` for whichever test the xdist
    worker runs next. Which test that is moves whenever a test file is added (#1834's
    `api (0)` shard hit `test_phone_voiceover_advertises_reaction_beats_and_keeps_them`).
    Clearing costs two ContextVar writes and needs no I/O.
    """

    from app.services.client_protocol import clear_request_context  # noqa: PLC0415

    clear_request_context()


@pytest.fixture(autouse=True)
def _caption_translation_never_reaches_a_provider(monkeypatch) -> None:
    """KRI-838: the caption translator is a paid OpenAI call that fails open.

    A render test that asks for captions in another language reaches it whenever the
    hinted text is not already in that language; without this, such a test would try to
    open a real connection. The blocked call raises, which is exactly the provider-down
    path (the original words come back, honestly labelled). Tests that want a
    translation stub ``_llm_translate_lines`` themselves; the module's own provider
    tests keep the real function from before this fixture runs.
    """

    from app.pipeline import caption_translate  # noqa: PLC0415

    def _blocked(*args, **kwargs):
        raise RuntimeError("caption translation provider call blocked in tests")

    monkeypatch.setattr(caption_translate, "_llm_translate_lines", _blocked)


@pytest.fixture(scope="module")
def _build_task_table_lock():
    """Serialize modules that destructively share the build_task table.

    API CI uses pytest-xdist workers against one ``nova_test`` database.  The
    build-task integration modules each TRUNCATE the same table, so without a
    cross-process lock one worker can erase another worker's committed fixture
    between its setup and assertion.  A session-level PostgreSQL advisory lock
    is independent of pytest's scheduling mode and is released automatically
    if a worker process dies.
    """

    db_url = os.environ["DATABASE_URL"].replace(
        "postgresql+asyncpg://",
        "postgresql://",
    )
    engine = create_engine(db_url, pool_pre_ping=True)
    connection = None
    lock_key = 2026082801
    try:
        connection = engine.connect()
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": lock_key})
    except OperationalError as exc:
        if connection is not None:
            connection.close()
        engine.dispose()
        pytest.skip(f"Postgres not reachable for build_task tests: {exc!r}")

    try:
        yield
    finally:
        try:
            try:
                unlocked = connection.scalar(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": lock_key},
                )
                if unlocked is not True:
                    raise RuntimeError("build_task advisory lock was not held at teardown")
            except BaseException:
                # Never return a session with an unproven session-level unlock
                # to this engine's pool. Invalidate its physical connection and
                # fail the test instead of deadlocking the next xdist module.
                connection.invalidate()
                raise
        finally:
            try:
                connection.close()
            finally:
                engine.dispose()


@pytest.fixture(autouse=True)
def _stub_db_has_no_stored_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """Planner unit tests pass stub dbs; they hold no draft (KRI-1365 reads the draft's sound)."""
    import sys

    planner = sys.modules.get("app.kria.planner")
    if planner is None:
        return
    real = planner._load_draft_strategy

    async def load(db, item_id):  # noqa: ANN001, ANN202
        try:
            return await real(db, item_id)
        except AttributeError:  # a stub db (no execute, or an execute result with no rows API)
            return None

    monkeypatch.setattr(planner, "_load_draft_strategy", load)
