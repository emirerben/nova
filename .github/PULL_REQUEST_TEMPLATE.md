## What
<!-- One-line summary of this change -->

## Why
<!-- Context or reference to TASKS.md item -->

## Checklist
- [ ] TASKS.md updated (mark completed items, add follow-ups)
- [ ] No video files in diff (`git diff --stat` shows no .mp4/.mov/.mkv)
- [ ] .env.example updated if new env vars were added
- [ ] VIDEO_CONTEXT.md consulted for any video processing changes
- [ ] Applied release labels when needed: `release:major` or `release:minor`, plus one changelog category (`release:added`, `release:fixed`, `release:changed`, `release:deprecated`, `release:removed`, or `release:security`). No label means a patch-level `Changed` entry.
- [ ] If a Kria creation or edit journey is affected, the author linked the PR CI journey artifact for this HEAD (offline replay plus the iPhone unit leg), named the request and expected output, and recorded model/prompt, cost, and any gaps. PR CI no longer renders fixture journeys on the iPhone simulator: when this PR needs native render evidence, run CI manually on this branch (Actions → CI → Run workflow) and link that run; otherwise the nightly CI run is the first native render. See `docs/reviews/kri-559-journey-verification.md`.
- [ ] Reviewer checked the journey case matches this change, required stages passed on this HEAD, native render evidence comes from a manual CI run on this HEAD when the change needs it, and any live-model result is labelled separately from offline replay.
- [ ] For prompt, model, or provider changes, link the applicable free replay checks. Any separately approved live evaluation names its exact revision and settled cost; paid calls are not required by this checklist.

## Reliability gates (KRI-577; required when a Kria chat, render, receipt or device path changes)
<!-- Fill the five lines or write "n/a: <why>". See docs/runbooks/pr-acceptance-checklist.md -->
- Incident replay: <gate id(s) from `python3 scripts/reliability-gates.py select`, or the new fixture/test node that replays the original failure; it fails without the fix (red control) and passes with it>
- Neighbouring cases: <the sibling inputs/paths exercised (empty vs explicit selection, EN/TR, phone proxy vs full-res, draft vs render-ready, stale generation, cap boundary); which are still open>
- Platforms and evidence level: <offline replay | live model (cost) | native output (device/simulator export) | production (revision, UTC)>
- Model/prompt resource envelope: <n/a, or tokens / timeout / cost / call count before and after>
- Known environment failures vs regressions: <e.g. "9 DB-backed tests skipped locally; 0 regressions" — never "pre-existing" without the list>

## Request-to-output evidence (KRI-577 / KRI-559)
<!-- Required for affected runtime, fixture and evidence-policy changes. Docs-only
     changes are classified automatically. Read docs/runbooks/request-to-output-standard.md.
     Replace every empty field that applies. Pending checks need a reason and owner_issue;
     not_applicable checks need a concrete reason. A local_fix claim keeps broader gaps open.
     Passed checks need commands and immutable artifact references. Use safe repo-relative
     paths at this HEAD, or pinned GitHub blob/action artifact/run-attempt URLs.
     Passed incident/content checks also name expectation_source: a separate immutable
     source/acceptance artifact, not prose claiming the result is correct.
     Run: bash scripts/preship-check.sh --pr-body .dev/pr-body.md
     This declaration does not certify the contents of an export; reviewers inspect evidence. -->

```request-evidence
{
  "schema_version": 1,
  "head_sha": "",
  "claim": "local_fix",
  "issue": "",
  "original_request": "",
  "request_provenance": "",
  "request_artifact": "",
  "expected_outcome": "",
  "root_cause": "",
  "checks": [
    {"kind": "incident_replay", "status": "pending", "evidence_level": "pending", "reason": "", "owner_issue": ""},
    {"kind": "wrong_content_control", "status": "pending", "evidence_level": "pending", "reason": "", "owner_issue": ""},
    {"kind": "recovery", "status": "pending", "evidence_level": "pending", "reason": "", "owner_issue": ""},
    {"kind": "request_fulfillment", "status": "pending", "evidence_level": "pending", "reason": "", "owner_issue": ""},
    {"kind": "export_fidelity", "status": "pending", "evidence_level": "pending", "reason": "", "owner_issue": ""},
    {"kind": "rollout", "status": "pending", "evidence_level": "pending", "reason": "", "owner_issue": ""}
  ]
}
```

<!-- Do not edit VERSION, CHANGELOG.md, or root package version fields here.
     Post-merge automation publishes all release metadata after this PR lands. -->
