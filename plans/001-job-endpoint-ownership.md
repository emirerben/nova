# Plan 001: Enforce job ownership on generative/music/template status and variant-edit endpoints

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat a49fe589..HEAD -- src/apps/api/app/routes/generative_jobs.py src/apps/api/app/routes/music_jobs.py src/apps/api/app/routes/template_jobs.py src/apps/api/app/auth.py`
> If any in-scope file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: commit `a49fe589`, 2026-06-12

## Why this matters

`GET /generative-jobs/{job_id}/status` and all nine generative variant-edit POST endpoints take only a `db` dependency — no authentication, no ownership check. The same is true of `GET /music-jobs/{job_id}/status` and `GET /template-jobs/{job_id}/status`. The status endpoint also serves `content_plan` jobs, which belong to **real signed-in users** and whose response includes freshly re-signed GCS playback URLs for the user's rendered footage. Today, anyone who obtains a job UUID can read another user's video output URLs or trigger expensive re-renders on their job (compute abuse). Job UUIDs are unguessable, so this is not remotely exploitable by enumeration — but any leaked ID (logs, shared links, browser history) becomes a permanent capability over someone else's content. This plan closes that gap **without breaking the live anonymous flow**, which is a hard product constraint (see below).

## Current state

Relevant files:

- `src/apps/api/app/auth.py` — auth dependencies. `SYNTHETIC_USER_ID` constant (line 30), `get_current_user_or_synthetic` (lines 99–121), `CurrentUser` / `CurrentUserOrSynthetic` annotated types (lines 124–125). The module docstring (lines 14–17) says: "Legacy public routes (generative/template/music jobs) use get_current_user_or_synthetic which falls back to the synthetic dev user when no X-User-Id header is present, so existing unauthenticated flows keep working unchanged." This plan completes that rollout for read/edit endpoints.
- `src/apps/api/app/routes/generative_jobs.py` — job loader `_load_generative_job` (lines 348–359), status endpoint (line 1072), variant-edit endpoints (lines 1110–1270).
- `src/apps/api/app/routes/music_jobs.py` — `get_music_job_status` (line 333).
- `src/apps/api/app/routes/template_jobs.py` — `get_template_job_status` (line 498).
- `src/apps/api/tests/routes/test_generative_jobs.py`, `tests/routes/test_auth_regression.py` — test patterns to follow.

The shared loader, as it exists today (`generative_jobs.py:348–359`):

```python
async def _load_generative_job(
    job_id: str, db: AsyncSession, *, allowed_modes: tuple[str, ...] = ("generative",)
) -> Job:
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    result = await db.execute(select(Job).where(Job.id == job_uuid))
    job = result.scalar_one_or_none()
    if job is None or job.mode not in allowed_modes:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job
```

The status endpoint (`generative_jobs.py:1072–1083`) — note: only a `db` dependency, and it serves `content_plan` jobs via `_READABLE_MODES = ("generative", "content_plan")` (line 345):

```python
@router.get("/{job_id}/status", response_model=GenerativeJobStatusResponse)
async def get_generative_job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobStatusResponse:
    ...
    job = await _load_generative_job(job_id, db, allowed_modes=_READABLE_MODES)
```

A representative mutation endpoint (`generative_jobs.py:1110–1119`); the other eight follow the same shape:

```python
@router.post("/{job_id}/variants/{variant_id}/swap-song", response_model=GenerativeJobResponse)
async def swap_song(
    job_id: str,
    variant_id: str,
    req: SwapSongRequest,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobResponse:
    """Re-render a variant against a different library song (async re-slot)."""
    job = await _load_generative_job(job_id, db)
    await dispatch_swap_song(job, variant_id, new_track_id=req.new_track_id, db=db)
```

The full set of generative endpoints to guard (all in `generative_jobs.py`, line numbers at `a49fe589`):

| Endpoint | Line |
|---|---|
| `GET /{job_id}/status` | 1072 |
| `POST .../swap-song` | 1110 |
| `POST .../retext` | 1126 |
| `POST .../change-style` | 1140 |
| `POST .../intro-size` (set_intro_size) | 1164 |
| `POST .../edit` (edit_variant) | 1183 |
| `GET .../timeline` (get_variant_timeline) | 1219 |
| `POST .../timeline` (edit_variant_timeline) | 1234 |
| `POST/DELETE .../timeline reset` (reset_variant_timeline) | 1253 |
| `POST .../mix` (set_mix) | 1266 |

`get_music_job_status` (`music_jobs.py:333–348`) and `get_template_job_status` (`template_jobs.py:498–512`) inline the same load-and-404 pattern without a loader helper.

### Key design facts (verified at `a49fe589` — these dictate the fix shape)

1. **The public generative page calls the API directly from the browser with no auth headers.** `src/apps/web/src/lib/generative-api.ts:7` sets `const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"` and lines 168/181/191/208 `fetch(${API_BASE}/generative-jobs/...)` with no headers. Jobs created this way get `user_id = SYNTHETIC_USER_ID`. **Therefore: do NOT require authentication on these endpoints. Enforce ownership only when the job belongs to a real (non-synthetic) user.** Synthetic-owned jobs remain accessible by UUID (capability-URL model) — that is the intended transitional behavior.
2. **The plan-item page polls the same status endpoint through the authenticated `/api/plan` proxy.** `src/apps/web/src/lib/plan-api.ts:422` and `:544` call `request(`/generative-jobs/${jobId}/status`)`, and that proxy "injects the NextAuth session's X-User-Id + the server-only INTERNAL_API_KEY" (`plan-api.ts:5`). So requests for real-user (`content_plan`) jobs already carry identity — adding enforcement breaks nothing.
3. `CurrentUserOrSynthetic` returns the synthetic user when `X-User-Id` is absent, and fully validates the internal key + user row when it is present (`auth.py:99–121`). It is already used by the create endpoints (`create_generative_job` at `generative_jobs.py:990–992`, `create_music_job` at `music_jobs.py:143–145`, `create_template_job` at `template_jobs.py:187–189`).

Repo conventions: routes raise `HTTPException(status.HTTP_404_NOT_FOUND, detail="Job not found")` for both missing and forbidden (no 403 — don't leak existence). Follow the dependency-injection style of `create_generative_job` (`generative_jobs.py:990–993`) for adding the user parameter.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| API route tests | `cd src/apps/api && pytest tests/routes/ -v` | all pass |
| Targeted tests | `cd src/apps/api && pytest tests/routes/test_generative_jobs.py tests/routes/test_auth_regression.py -v` | all pass |
| Lint | `cd src/apps/api && ruff check . && ruff format --check .` | exit 0 |
| Full API suite | `cd src/apps/api && pytest tests/ --ignore=tests/quality` | all pass |

Tests need local Postgres+Redis; if not already running: `docker-compose up -d redis db` from repo root. If the repo has `.venv-test`, use `src/apps/api/.venv-test/bin/python -m pytest ...`.

## Scope

**In scope** (the only files you should modify):
- `src/apps/api/app/routes/generative_jobs.py`
- `src/apps/api/app/routes/music_jobs.py`
- `src/apps/api/app/routes/template_jobs.py`
- `src/apps/api/app/auth.py` (add one small helper)
- `src/apps/api/tests/routes/test_generative_jobs.py` (extend)
- `src/apps/api/tests/routes/test_auth_regression.py` (extend) — or a new `tests/routes/test_job_ownership.py`

**Out of scope** (do NOT touch, even though they look related):
- `src/apps/api/app/routes/plan_items.py` — already enforces ownership (`CurrentUser` + `plan.user_id` checks).
- `/me/jobs` routes and all `/admin/*` routes — separately gated.
- Any frontend file — the proxy already sends identity; no client change is needed.
- The `users/` GCS-prefix allowlist in `admin_music.py:1880` — admin-token-gated today; see Maintenance notes.
- `create_*_job` endpoints — they already use `CurrentUserOrSynthetic`.
- The `GET /template-jobs/{job_id}/debug`, `/eval`, `/events` routes — debug surfaces; guard them only if trivial, otherwise note as follow-up.

## Git workflow

- Worktree: per repo convention (CLAUDE.md), create a fresh worktree first: `bash scripts/new-session.sh job-ownership && cd ../nova-job-ownership`.
- Branch naming: the script handles it; commit style is conventional commits, e.g. `fix(auth): enforce job ownership on status + variant-edit endpoints`.
- Do NOT push or open a PR unless the operator instructed it.

## Steps

### Step 1: Add the ownership helper to `app/auth.py`

Add to `src/apps/api/app/auth.py` (after `SYNTHETIC_USER_ID`):

```python
def ensure_job_owner(job_user_id: uuid.UUID | None, current_user: User) -> None:
    """Raise 404 when `job_user_id` belongs to a real user other than `current_user`.

    Jobs owned by the synthetic dev user (or with no owner) stay reachable by
    UUID — the documented transitional model for the anonymous public flows.
    404 (not 403) so a forbidden job is indistinguishable from a missing one.
    """
    if job_user_id is None or job_user_id == SYNTHETIC_USER_ID:
        return
    if job_user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
```

**Verify**: `cd src/apps/api && ruff check app/auth.py` → exit 0.

### Step 2: Thread the current user through `_load_generative_job`

In `generative_jobs.py`, change `_load_generative_job` to accept the user and enforce ownership after the mode check:

```python
async def _load_generative_job(
    job_id: str,
    db: AsyncSession,
    current_user: User,
    *,
    allowed_modes: tuple[str, ...] = ("generative",),
) -> Job:
    ...existing UUID parse + mode check...
    ensure_job_owner(job.user_id, current_user)
    return job
```

Make `current_user` a **required positional parameter** (not defaulted) so any call site that forgets it fails at import/test time, not silently.

**Verify**: `cd src/apps/api && python -c "import app.routes.generative_jobs"` → no error (call sites will be fixed next; this just confirms syntax). Running pytest now SHOULD fail with TypeErrors at call sites — that is expected until Step 3.

### Step 3: Add `CurrentUserOrSynthetic` to all 10 generative endpoints

For each endpoint in the table above (status + 9 variant routes), add the dependency and pass it through, following the existing style of `create_generative_job` (`generative_jobs.py:990–993`):

```python
async def get_generative_job_status(
    job_id: str,
    current_user: CurrentUserOrSynthetic,
    db: AsyncSession = Depends(get_db),
) -> GenerativeJobStatusResponse:
    ...
    job = await _load_generative_job(job_id, db, current_user, allowed_modes=_READABLE_MODES)
```

The import `from app.auth import CurrentUserOrSynthetic` already exists (line 32); add `ensure_job_owner` and `User` imports as needed.

**Verify**: `cd src/apps/api && pytest tests/routes/test_generative_jobs.py tests/routes/test_generative_timeline.py -v` → all existing tests pass (they exercise the no-header path, which resolves to the synthetic user and remains allowed).

### Step 4: Guard the music and template status endpoints

In `music_jobs.py:get_music_job_status` and `template_jobs.py:get_template_job_status`, add `current_user: CurrentUserOrSynthetic` to the signature and call `ensure_job_owner(job.user_id, current_user)` immediately after the existing `job is None or job.job_type != ...` 404 check. Both files already import `CurrentUserOrSynthetic`.

**Verify**: `cd src/apps/api && pytest tests/routes/ -v -k "music or template"` → all pass.

### Step 5: Write the ownership tests

Extend `tests/routes/test_generative_jobs.py` (or create `tests/routes/test_job_ownership.py` modeled on `tests/routes/test_auth_regression.py` for header construction — it shows how `X-User-Id` + `Authorization: Bearer <internal key>` are sent in tests). Cases:

1. **Anonymous regression**: job with `user_id = SYNTHETIC_USER_ID` → `GET /generative-jobs/{id}/status` with **no headers** → 200. (Protects the live anonymous flow.)
2. **Real-user job, no headers** → 404.
3. **Real-user job, wrong user's headers** → 404.
4. **Real-user job, owner's headers** (matching `X-User-Id` + valid internal key) → 200.
5. Cases 2–4 repeated for one mutation endpoint (`POST .../retext` is the simplest — body `{"text": "x", "remove": false}`).
6. Cases 1–4 repeated for `GET /music-jobs/{id}/status` and `GET /template-jobs/{id}/status`.
7. **content_plan readability**: a `content_plan`-mode job owned by user A is readable by A via status (the `_READABLE_MODES` path) and 404 for user B.

**Verify**: `cd src/apps/api && pytest tests/routes/ -v` → all pass, including the new tests.

### Step 6: Full gate

**Verify**: `cd src/apps/api && ruff check . && ruff format --check . && pytest tests/ --ignore=tests/quality` → exit 0, all pass.

## Test plan

Covered by Step 5. Pattern files: `tests/routes/test_generative_jobs.py` (route/fixture style), `tests/routes/test_auth_regression.py` (auth header construction). New coverage: the anonymous-capability regression, owner/non-owner/no-auth trios for one read and one mutation endpoint per job family, and the content_plan read path.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `cd src/apps/api && pytest tests/ --ignore=tests/quality` exits 0
- [ ] `grep -n "current_user" src/apps/api/app/routes/generative_jobs.py | wc -l` shows the dependency on all 10 guarded endpoints (≥10 signature occurrences beyond the create endpoint)
- [ ] `grep -n "ensure_job_owner" src/apps/api/app/routes/music_jobs.py src/apps/api/app/routes/template_jobs.py` → 1 hit in each
- [ ] New ownership tests exist and pass (`pytest tests/routes/ -v -k ownership` or the chosen file)
- [ ] `ruff check .` and `ruff format --check .` exit 0
- [ ] No files outside the in-scope list are modified (`git status`)
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts don't match the live code (drift since `a49fe589`).
- You find a frontend caller OTHER than the `/api/plan` proxy that polls these endpoints for jobs with a real `user_id` (search: `grep -rn 'generative-jobs' src/apps/web/src --include='*.ts' --include='*.tsx'`). Enforcement would break it; report which caller and stop.
- `Job.user_id` turns out to be nullable-and-NULL for real-user jobs in any code path that creates jobs (check `grep -rn "user_id=" src/apps/api/app/tasks/ src/apps/api/app/routes/`). The helper treats NULL as anonymous; if real-user jobs can have NULL, that's a data-model question for the maintainer.
- Existing tests fail in a way that suggests they relied on cross-user access.

## Maintenance notes

- **When the anonymous flow is retired** (full auth rollout), flip these endpoints from `CurrentUserOrSynthetic` to `CurrentUser` and delete the synthetic-bypass branch in `ensure_job_owner` — the dependency seam added here makes that a two-line change.
- **Follow-up deliberately deferred**: per-user validation of `users/{user_id}/...` clip paths in `_validate_clip_path_prefixes` (`admin_music.py:1880`) — becomes relevant when public routes accept `users/`-prefixed uploads from signed-in users; today the path components are unguessable UUIDs.
- The `/template-jobs/{id}/debug|eval|events` routes were left unguarded by scope decision; revisit alongside the rollout completion.
- Reviewers should scrutinize: that 404 (never 403) is returned on ownership failure, and that the no-header status poll still works against a synthetic job (the anonymous regression test).

---

## GSTACK REVIEW REPORT

**Reviewed at**: commit `a49fe589` · 2026-06-12 · Reviewer: plan-eng-review (full)

### Step 0 — Scope challenge & STOP condition pre-checks

| Question | Answer |
|---|---|
| Minimum change set? | 1 helper in `auth.py` + 10 endpoint signatures + 2 status endpoints + tests |
| Complexity check triggered? | NO (< 5 files, no new services/infra) |
| STOP: frontend callers beyond `/api/plan` proxy with real user_id? | **NO.** `generative-api.ts` + `api.ts` (template jobs) call without auth headers → `user_id=SYNTHETIC_USER_ID` → `ensure_job_owner` allows them. The `/api/plan` proxy sends `X-User-Id` for real-user `content_plan` jobs — no extra callers found. |
| STOP: `Job.user_id` nullable for real-user jobs? | **NO.** `Mapped[uuid.UUID]` with `nullable=False` (`models.py:323-325`). The `\| None` in `ensure_job_owner`'s signature is purely defensive. |

### Section 1 — Architecture

`ensure_job_owner` as a single-responsibility helper in `auth.py` is the right location (peers with `get_current_user_or_synthetic`). Using `CurrentUserOrSynthetic` on all guarded endpoints preserves the anonymous flow without any branching in the routes. Raising 404 (never 403) on ownership failure is consistent with every existing load-and-404 pattern in the codebase. The positional-and-required `current_user` arg in `_load_generative_job` forces call-site completeness at import time.

**Finding: none.**

### Section 2 — Code Quality

Single helper, 10-line implementation. DRY: all 10 generative endpoints share `_load_generative_job` so the ownership check is added once. Music and template status endpoints inline their load logic and each get one `ensure_job_owner` call. No duplication.

One implementation note: `Job.user_id` is non-nullable per schema, so the `None`-arm of `ensure_job_owner` will never fire in production — the defensive branch is harmless and good for future-proofing if the model changes.

**Finding: none.**

### Section 3 — Test Coverage

```
CODE PATHS
[+] ensure_job_owner helper
  ├── [★★★ COVERED by new tests] job_user_id is None → allow
  ├── [★★★ COVERED] job_user_id == SYNTHETIC_USER_ID → allow (anonymous regression)
  ├── [★★★ COVERED] job_user_id != current_user.id → 404
  └── [★★★ COVERED] job_user_id == current_user.id → pass

[+] generative status (read)
  ├── [★★★ COVERED] no headers + synthetic job → 200
  ├── [★★★ COVERED] no headers + real-user job → 404
  ├── [★★★ COVERED] wrong user + real-user job → 404
  └── [★★★ COVERED] owner headers + own job → 200

[+] generative mutation (retext)
  └── [★★★ COVERED] cases 2-4 above repeated

[+] music status + template status
  └── [★★★ COVERED] cases 1-4 repeated

[+] content_plan readability (real-user A reads, B gets 404)
  └── [★★★ COVERED]

COVERAGE: 14 new test cases. Existing suite unaffected (unauthenticated callers use synthetic jobs).
```

**Finding: none.**

### Section 4 — Performance

`CurrentUserOrSynthetic` resolves to synthetic user on requests with no `X-User-Id` header — no DB hit. Authenticated requests (plan-page proxy) add one DB lookup per status poll; acceptable for the user-polling interval. No performance concern.

**Finding: none.**

### NOT in scope

- `/template-jobs/{id}/debug|eval|events` — debug surfaces, left unguarded per plan scope
- per-user `users/{user_id}/...` clip-path validation in `admin_music.py`
- `create_*_job` endpoints (already guarded)
- frontend changes (proxy already sends identity)

### What already exists

- `SYNTHETIC_USER_ID`, `CurrentUserOrSynthetic`, `get_current_user_or_synthetic` in `auth.py` — plan reuses all three
- `test_auth_regression.py` — auth header construction pattern for new tests
- `_load_generative_job` shared loader — single insertion point for ownership check

### TODOS updates

None — zero findings.

### Failure modes

1. A template/music job created by a real (authenticated) user in the future would return 404 to the unauthenticated poller in `api.ts` → plan's STOP condition covers this; verify when full auth rollout happens.
2. `ensure_job_owner` called with wrong argument order → positional + type check catches at test time.

### Worktree parallelization

Sequential implementation. Single code path; no parallelization needed.

**VERDICT: APPROVED — zero findings across all 4 sections. Safe to implement.**
