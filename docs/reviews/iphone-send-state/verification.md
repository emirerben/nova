# Send verification record

## Last complete local snapshot

Commit `32a2206b2f107c69db8f85f0209b3ac886ef4bbf` on base `de24c7f5947c00bc08731bcd04e858206f87eace` preserves the last fully tested source. It was committed before rebasing; its patch and per-file hashes remain in the task's local `.audit/last-passing` directory.

- Native unit gate executed 2,119 tests, skipped 26 and had zero failures.
- Documented selected native gate passed all 14 journeys, covering four smoke journeys and ten Send/draft journeys. Its coverage verifier passed with no reported retries/flakes.
- Backend `make verify-kria` passed 6,879 tests, skipped six and reported four expected failures, with zero unexpected failures. Contract/OpenAPI checks passed. The disposable UTF8 PostgreSQL/Redis services were separate from other owners and stopped afterward.
- Original delayed-preflight control failed the two expected bubble/composer assertions on unmodified app source. Four expanded status-clearance tests passed after the fix. The included fixture screenshots were inspected and show full bubble, pending status and Retry above the composer with keyboard open/closed. These screenshots are historical evidence from this snapshot, not current-head execution.
- Five combined tests with the exact PR1922 navigation patch passed before the final clearance-only change; temporary integration files were restored exactly.
- Exhaustive UI was interrupted in favor of the repository's documented selected PR gate. It is not a full exhaustive-UI pass. Physical phone and production were not run.

## Current-main reconciliation and retry correction

The unpublished branch rebased cleanly onto `fe63beb51`, including merged PR1922 navigation, PR1920 previews and PR1923 order handling. Independent review found one defect already in the earlier Send patch: Retry preserved the frozen structured request but its rejection handling used empty live card arguments. The fix at `5f96e87b9d9483cadd17c086f732e49d3553fa08` saves the answer kind/question ID in the receipt before persistence and restores card-specific rejection handling. Wire payload and client identity remain unchanged.

Independent static review found no remaining P1/P2 defect in that correction. A focused host executable compiled the current actual ChatDraftStore/receipt/context definitions extracted from the app file. Four scenarios passed for Codable/store recovery, identical request body/client ID, duplicate-attempt refusal and preservation of newer composer text. This is host unit evidence, not an iPhone UI run. Swift parsing and diff whitespace checks passed. All117 iOS verification-script/mapping checks passed again.

Both post-rebase native build attempts failed with ENOSPC before tests ran. The second attempted a new negative-control UI case for uncertain song-range submission followed by a structured422 rejection. That UI test has never executed locally. The new context-persistence XCTest has not executed in the iOS runner locally either. Earlier full counts do not certify this revision. Current-head CI must verify the integrated native code and regression tests.

No branch approval, merge, deployment, production flag/data change or paid provider call is authorized. A dedicated Linear ticket was not created because automatic approval review rejected that write; the parent instructed no retry and authorized the draft publication without new ticket creation. This does not waive the repository's PR-evidence validation.
