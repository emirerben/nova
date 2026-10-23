# Camera-roll integration with current main — 23 October 2026

Scope: KRI-731 / PR #1747, a local capacity and multiplicity fix. The reviewed
source was ec0c5ed844e169708750eadac2c60eaaa3aee9d0. Integrate main
bf6c20c807040638782310cdbdd06d8133a4c32e additively; retain both parents.
No new product behavior, provider calls, account settings or deployment.

## Failure cases recorded before resolving the merge

- F1: choosing one workflow conflict side silently removes the other named native
  checks. Preserve both complete steps and their exact-test result verification.
- F2: a stale generated API contract loses either the 500-member/partial-consent
  request or KRI-822's explicit original-attachment request. Regeneration must
  match the combined API, not a selected conflict side.
- F3: Services loses either the partial-analysis overload or verified-original
  upload method. Preserve both and main's client protocol 14 unchanged.
- F4: test selection loses the new partial-consent/three-idea cases or the saved
  original/relink cases. Check declarations, workflow commands and result guards.
- F5: new original admission accidentally promotes proxies, changes owner checks,
  reanalyzes originals, or changes collection draft approval. Replay collection,
  original upgrade and camera-roll execution boundaries on the combined source.

Before integration the exact source head had 25 successful CI checks and one
explicitly skipped paid eval, including successful iPhone validation, ios-contracts
and journey. These results do not cover this new integration head. CI must run
again after publication; no web-editor work or local browser tests are in scope.

## Remaining acceptance

The saved live examples still have generic/ad-like wording and overlapping
premises. Three valid source-referencing candidates do not establish publishable
ideas, source-picture semantics, physical-device 500-original ingest, or a new
end-to-end approval/export/Save/reopen result. Those remain KRI-731 acceptance;
this integration must not relabel them as passed. No new paid preview is allowed.

## Combined-source checks

- `bash scripts/check-api.sh test tests/evals/test_camera_roll_agents.py tests/integration/test_media_collections.py tests/integration/test_camera_roll_execution.py tests/integration/test_media_library_original_upgrade.py -q` — 62 passed, 24 deprecation warnings. Uses this worktree's isolated PostgreSQL scratch database; provider transport is replayed, no external model calls.
- `python3 -m unittest discover -s scripts/ios/tests -q` — 117 passed, including workflow/test-selection orchestration. No physical phone is used.
- From `src/apps/api`, `.venv/bin/python -m app.cli.kria_contracts --check` with test configuration — tools, mobile and OpenAPI contracts match without regeneration.
- Static combined-source check: both complete named-native steps match their respective parent verbatim; all four exact UI cases remain in the workflow run and result verifier, native group and journey manifest. Services keeps both methods and protocol 14; generated schema retains both explicit consent booleans and maxItems 500.
- `git diff --check` — passed. The only manual merge resolution is retaining both native CI steps. Services/OpenAPI/manifests merged additively without manual changes. KRI-822 original admission remains unchanged from main. #1767 is test-only and introduces no camera-roll contract change.

No new model cost, media upload, phone operation, product prompt change or web-editor work. Fresh exact-head CI/native results remain required after publication; the earlier green ec0c5ed run is historical evidence only.
