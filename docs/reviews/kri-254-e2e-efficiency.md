# KRI-254: Tester Army evaluation and E2E efficiency

Evaluated 2026-10-02 against Nova `4b5e4a19d` and the public documentation for
`e2e` 0.15.1 / `@e2e-dev/web` 0.11.1.

## Decision

Keep Playwright as the required web regression runner. Improve its CI environment
and evidence first. Tester Army is promising for authoring new exploratory flows,
but replacing existing tests would not address the measured delay.

No test cases are removed, no AI credentials are added, and no production code
changes. The existing API/Jest shards, iOS checks, guards and evals remain intact.

## What Tester Army adds

The [framework](https://e2e.tester.army/docs) mixes natural-language agent actions
with deterministic assertions and supports web and mobile engines. It can reduce
the effort of scripting UI interactions. Its web engine uses Playwright underneath.

The [replay cache](https://e2e.tester.army/docs/cache) avoids model calls for
previously verified `agent.act` steps. It does not cache arbitrary test execution:
`agent.assert`, `agent.waitFor` and `agent.extract` still call a model. Cache misses,
handoffs and retries can also run live. Even strict-cache mode is not a blanket
zero-model-call guarantee. Nova's current Playwright tests already perform their
actions without model calls, so this cache has nothing to eliminate in those tests.

The [migration guide](https://e2e.tester.army/docs/migrate/playwright) allows the
runners to coexist. Adoption still requires deliberate compatibility work:
[e2e](https://github.com/tester-army/e2e/blob/main/packages/e2e/package.json) and
[its web engine](https://github.com/tester-army/e2e/blob/main/packages/web/package.json)
require Node >=22.12.0, and the web engine requires Playwright >=1.63.0. Nova CI uses
Node 20 and its npm lock resolves Playwright 1.61.1. The vendor versions and these
requirements are a dated observation, not a promise about later releases.

Agent testing also introduces live-model latency and output variability. The
[security model](https://e2e.tester.army/docs/security) documents credential
redaction limits and notes that shared replay recordings should be reviewed as
test code. Those tradeoffs are reasonable for an optional experiment; they do not
justify a wholesale replacement of the existing required checks.

## Nova baseline

The web suite contains 80 source tests, expanded into 146 cases across three
mobile viewports plus the desktop-editor and chat-first-creation projects. It
uses local `dev-qa` fixtures and mocked service responses. It is useful UI
regression coverage, not proof of the full API/queue/render/storage journey.
The [KRI-198 audit](kri-198-test-prune.md) already identified that distinction.

| Successful GitHub run | Browser + OS setup | Test step | Result |
| --- | ---: | ---: | --- |
| [36893539864](https://github.com/emirerben/nova/actions/runs/36893539864) | 333s | 316s | 146 passed; job ~11m13s |
| [36887057025](https://github.com/emirerben/nova/actions/runs/36887057025) | 47s | 317s | 146 passed; job ~6m39s |

The slow run's setup was predominantly apt/font/Mesa work: package installation
continued until 16:44:31 UTC; Chromium downloads started at 16:44:33 and finished
at 16:44:42. Caching browser downloads alone would save only about nine seconds
in that sample. The test execution times were almost identical across the runs.
Two samples describe the observed problem, not a statistically reliable average.

There was also dependency drift. E2E used `pnpm install --no-frozen-lockfile` and
installed Playwright 1.63.0, whereas normal CI's `npm ci` honored the checked-in
1.61.1 lock. Successful runs did not upload their Playwright reports, making
comparisons and retry investigation unnecessarily difficult.

## Implemented CI changes

- Run in the official `mcr.microsoft.com/playwright:v1.61.1-noble` image, which
  includes browsers, Linux libraries and fonts. Remove per-run apt/browser
  installation. [Playwright documents this environment](https://playwright.dev/docs/docker).
- Use Node 20, npm's download cache and `npm ci --no-audit --no-fund`, matching
  normal CI. Check browser executable availability before tests so an incompatible
  lock/image update fails early rather than silently downloading another version.
- Preserve every project and retry setting; explicitly use two workers as in the
  baseline GitHub runs. Cancel superseded PR runs and cap the job at 20 minutes.
- Upload HTML, machine-readable JSON and failure artifacts on both successful
  and failed non-cancelled runs, retained for seven days. GitHub already records
  setup-step durations; JSON adds test counts, durations and retry outcomes.
- Run this workflow when its own definition or the shared motion runtime changes,
  as well as on web changes.

The image pull still costs time. This change removes variable apt work, but its
net runtime benefit must be assessed from actual CI runs including container
initialization. It does not claim to make the test cases themselves faster.

## Verification and operating procedure

Local verification on 2026-10-02: locked npm install succeeded; all 146 cases
passed in 91.2 seconds with two workers, zero skipped, zero unexpected and zero
flaky results. This macOS/Node 25 run verifies suite compatibility, not a CI
speedup. `actionlint` 1.7.12, the documentation size guard, `git diff --check` and
the repository preship gate passed. Linux/Node 20 container timing comes from
the PR's Mobile E2E run, including image initialization.

Run the full suite from `src/apps/web` with the locked dependencies:

```sh
npm ci --no-audit --no-fund
npm exec -- playwright install chromium
CI=1 PLAYWRIGHT_JSON_OUTPUT_NAME=test-results/e2e-results.json \
  npm exec -- playwright test --workers=2 --reporter=html,list,json
```

On a worktree created by `new-session.sh`, first replace only the worktree's
`node_modules` symlink with its own install; do not run `npm ci` through a shared
dependency link. The CI container needs neither browser nor OS installation.

Compare GitHub's container-initialization, npm-install and test-step durations,
plus the JSON report's test count and flaky/unexpected outcomes, to the baseline.
Check that all 146 cases still execute and reports upload. A reduced count or
green result obtained by filtering/retrying away failures is not an improvement.

When upgrading Playwright in `package-lock.json`, update the container tag in
`.github/workflows/e2e-mobile.yml` in the same PR and run the full E2E job. Rollback
is a revert of the workflow change; no app deployment or data migration is needed.

## When to revisit an AI pilot

Use a separate, optional lane for one new fixture-backed user journey that is
expensive to script. Keep deterministic checks for saved state and error outcomes.
Measure authoring/repair time, cold and replay latency, model tokens/cost, replay
hit rate and false passes against the equivalent Playwright flow. Use synthetic
data, no production credentials, explicit spending limits and disabled telemetry.
Only promote it after repeat runs show a benefit without lost failure detection.

That experiment addresses authoring effort and exploratory coverage. It does not
replace the missing real backend journey, rendering parity checks, API tests or
native iOS assertions merely by using an agent to click the UI.
