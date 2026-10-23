# Camera-roll named native CI correction

The green PR CI run39756148645 checked source08c5448e. Its diagnostics artifact12422564572 records only four smoke UI scenarios; it did not execute the two camera-roll UI cases declared in the selected journey. Six camera-roll model tests passed, and25 existing API integration tests passed in the actual-main validation copy. These are separate evidence levels.

CI now runs both declared camera-roll UI cases explicitly on the same compiled checkout and simulator, retains their xcresult and JSON, and requires each exact test to pass. Missing, skipped or failed results fail the existing journey verifier. The smoke and unit stages remain intact. A new current-head run must pass before merge; this correction does not relabel the earlier smoke run.

This verifies fixture-based partial-index consent and independent idea screens. It does not establish better generated wording, the original creative-quality request, physical phone upload/export, or Save/reopen acceptance. The offline wording review still identifies brochure language, overlapping premises and unverified claims.
