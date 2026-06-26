# Plan 004: Add CI dependency caching and a one-command `make verify`

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, update the status row for this plan
> in `plans/README.md` — unless a reviewer dispatched you and told you they
> maintain the index.
>
> **Drift check (run first)**:
> `git diff --stat a49fe589..HEAD -- .github/workflows/ci.yml Makefile`
> If either file changed since this plan was written, compare the
> "Current state" excerpts against the live code before proceeding; on a
> mismatch, treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: commit `a49fe589`, 2026-06-12

## Why this matters

`.github/workflows/ci.yml` has zero dependency caching: all three jobs (`lint`, `test-web`, `test-api`) cold-install Node and/or Python dependencies on every PR run (`pnpm install --no-frozen-lockfile`, `pip install -e ".[dev]"`). That's minutes of redundant install time per job per push on a repo that ships many small PRs per day. Separately, there is no single local command that runs the full CI-equivalent gate — `make lint`, `make test`, and the web typecheck are separate invocations, and `npx tsc --noEmit` isn't in any Make target at all, so type errors surface only in Vercel builds. A `make verify` gives agents and humans one command to know the tree is green before pushing.

## Current state

`.github/workflows/ci.yml` (52 lines of interest):

- Lines 12–20 (`lint` job): `actions/setup-node@v4` (node 20) → `pnpm/action-setup@v3` (v9) → `pnpm install --no-frozen-lockfile` in `src/apps/web`.
- Lines 21–26 (`lint` job): `actions/setup-python@v5` (3.11) → `pip install -e ".[dev]"` in `src/apps/api`. No `cache:` key anywhere.
- Lines 33–42 (`test-web`): same node/pnpm cold install.
- Lines 84–89 (`test-api`): same python cold install; also apt-installs ffmpeg/libegl1/libgl1 (leave that alone — apt caching is not worth the complexity).
- **Quirk to know**: the web app has `src/apps/web/package-lock.json` (npm) but CI installs with **pnpm and no pnpm lockfile** (`--no-frozen-lockfile`). This means CI installs are not lockfile-reproducible. Fixing that mismatch is OUT of scope (it's a package-manager policy decision); it only affects the cache key choice below — key on `package.json`, since there is no pnpm lockfile to key on.

`Makefile`:

- Line 1: `.PHONY: dev dev-web dev-api api-install-dev test test-api test-quality build lint \` (continuation list).
- `test:` runs `(cd src/apps/web && pnpm test)` then API pytest (via `$(API_LOCAL_PYTHON)`).
- `lint:` runs `(cd src/apps/web && pnpm lint)` then `(cd src/apps/api && ruff check .)`.
- There is no `verify` target and no target running `tsc --noEmit`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Validate workflow syntax | `gh workflow list` after push, or locally: `python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` | parses, no error |
| Local verify run | `make verify` | exit 0 |
| Watch a CI run | `gh run watch` (or `gh pr checks <pr>`) | all jobs green |

## Scope

**In scope** (the only files you should modify):
- `.github/workflows/ci.yml`
- `Makefile`

**Out of scope** (do NOT touch, even though they look related):
- Switching CI from pnpm to npm (or committing a pnpm-lock.yaml) — package-manager policy decision for the maintainer; see Maintenance notes.
- Docker layer caching for `docker-build.yml` / local-render images.
- Splitting or parallelizing the test jobs, changing pytest flags, apt-package caching.
- Pre-commit hooks (audited and deliberately rejected — CI + agent hooks cover linting).
- The other 10 workflow files in `.github/workflows/`.

## Git workflow

- Worktree first: `bash scripts/new-session.sh ci-caching && cd ../nova-ci-caching`.
- Conventional commits, e.g. `chore(ci): cache pip + pnpm store; add make verify`.
- Do NOT push or open a PR unless the operator instructed it (CI verification requires a PR — coordinate with the operator).

## Steps

### Step 1: Cache pip in both Python setup steps

In `ci.yml`, extend both `actions/setup-python@v5` steps (`lint` job and `test-api` job):

```yaml
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
          cache-dependency-path: src/apps/api/pyproject.toml
```

**Verify**: YAML still parses (`python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"` → no error).

### Step 2: Cache the pnpm store in both Node jobs

`actions/setup-node`'s `cache: pnpm` requires a pnpm lockfile, which this repo doesn't have — so cache the pnpm store explicitly. In both the `lint` and `test-web` jobs, AFTER `pnpm/action-setup` and BEFORE the install step, add:

```yaml
      - name: Get pnpm store directory
        id: pnpm-store
        run: echo "path=$(pnpm store path)" >> "$GITHUB_OUTPUT"
      - uses: actions/cache@v4
        with:
          path: ${{ steps.pnpm-store.outputs.path }}
          key: pnpm-store-${{ runner.os }}-${{ hashFiles('src/apps/web/package.json') }}
          restore-keys: pnpm-store-${{ runner.os }}-
```

Note: `pnpm store path` needs pnpm on PATH, so `pnpm/action-setup` must run first. In the `lint` job at `a49fe589`, `pnpm/action-setup` (line 15) already follows `setup-node` (line 12) — keep that order and insert the cache steps after `pnpm/action-setup`.

**Verify**: YAML parses; step order in both jobs is setup-node → pnpm/action-setup → store-path → cache → install.

### Step 3: Add `make verify`

In `Makefile`: add `verify` to the `.PHONY` list (line 1) and add the target (match the existing parenthesized-subshell style):

```make
# ── Verify (one-command local gate: lint + typecheck + all tests) ─────────────

verify: lint
	(cd src/apps/web && npx tsc --noEmit)
	$(MAKE) test
```

This chains: `make lint` (web eslint + ruff) → web typecheck → `make test` (web Jest + API pytest). Keep it sequential and fail-fast (Make's default).

**Verify**: `make verify` from repo root → runs all four gates, exits 0 on a clean tree. (Requires local dev setup — Postgres/Redis for pytest per `docker-compose up -d redis db`, and the API venv via `make api-install-dev` which the `test` target already triggers.)

### Step 4: Prove the cache works in CI

Push the branch and open a draft PR (with operator approval). Let CI run twice (push a trivial amendment for run #2).

**Verify**: in run #2's logs — `test-api`'s setup-python step prints a pip cache restore (e.g. "Cache restored from key: ..."), and the pnpm cache step prints `Cache restored from key: pnpm-store-...`. Install steps in run #2 should be visibly faster than run #1. Record both runs' total durations in your report.

## Test plan

No unit tests — verification is operational: YAML parse check, a successful `make verify` locally, and the two-run CI cache-hit demonstration in Step 4.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `grep -c "cache: pip" .github/workflows/ci.yml` → 2
- [ ] `grep -c "actions/cache@v4" .github/workflows/ci.yml` → 2
- [ ] `grep -n "^verify:" Makefile` → 1 hit; `verify` present in `.PHONY`
- [ ] `make verify` exits 0 locally
- [ ] Second CI run shows cache restore lines in both a Node job and the `test-api` job
- [ ] `git status` shows only `ci.yml` + `Makefile` modified
- [ ] `plans/README.md` status row updated

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts don't match the live files (drift since `a49fe589`).
- `pnpm store path` fails in CI (pnpm version/action incompatibility) — report rather than swapping the pnpm action version, which affects all jobs.
- `make verify` exposes a pre-existing red test or type error on a clean tree — that's a real finding to report, not something to fix inside this plan.
- Cache restore makes `pip install -e ".[dev]"` fail (corrupt/poisoned cache edge case) — remove the cache key suffix to bust it once, and if it recurs, report.

## Maintenance notes

- **Known inconsistency, deliberately not fixed here**: the web app is npm-locked locally (`package-lock.json`, CLAUDE.md uses `npm` commands) but CI and the Makefile use pnpm with `--no-frozen-lockfile` — CI installs are not reproducible builds. The maintainer should eventually pick one manager and commit its lockfile to CI; when that happens, replace the explicit store cache with `setup-node`'s native `cache:` keyed on the lockfile.
- The pnpm-store cache key uses `package.json` (no lockfile exists) — it over-invalidates on any package.json edit and under-invalidates on transitive updates; acceptable until the lockfile decision lands.
- If a future PR adds a new heavy CI job, copy the same caching pattern rather than inventing a new one.
