# Reviewed recent sessions in daily reports

## Intended flow

The reviewer opens a prepared batch of 15 recent eligible sessions. Each card shows
creator messages, a current finished export, and editable proposed requirements.
Each requirement receives met, partial, missed, or unclear; none defaults to met.
Save and next records the review only after server acknowledgement. A reload restores
saved work. Unsaved navigation asks before discarding changes. Missing video is not
success, and assistant proposals are not human judgements.

The daily workflow fetches a read-only report on main. It reports collection,
review, verified-output coverage, unknowns, evidence age, per-category missed
instructions, and ranked next actions. Offline replay remains a separate section.
The report includes controlled category descriptions and opaque case references;
no raw conversations, media URLs, storage paths, or account identities enter CI.

## Failures to establish before implementation

- Wrong or missing report credential; read-only report token cannot read captures,
  save reviews, or obtain media. Pull-request jobs never receive the token.
- Expired/revoked/ineligible/held-out/deleted sessions appear in neither playable
  exports nor report content. A bounded report explicitly says when truncated.
- A referenced job belongs to a different creator, is not the thread's current job,
  or contains a foreign object path: no playback or outcome evidence is accepted.
- A replaced object generation, changed source, superseded capture, concurrent
  review or changed current output rejects a stale save; existing review survives.
- A plan/ready status alone, an unplayed output, or omitted outcomes cannot count
  as actual-output verification. Unknown requirements stay in the denominator.
- Generation-pinned video loading fails or a video has no audible track: preserve
  a truthful unavailable/unclear state; never substitute a different export.
- Actual measured bytes must match the selected object identity; video/audio
  judgements remain human review, not metadata assertions.
- Save failure preserves the draft. Double-clicks cannot submit two reviews. A late
  response cannot overwrite a newer selected session. Refresh restores saved data.
- The report has zero reviewed sessions: show zero/unknown, not an empty-store
  success rate. Stale and historical evidence is labelled and never relabelled as
  current production quality.
- Private text in an expectation, rationale, storage path, or signed URL does not
  leak into the CI JSON/Markdown. The report ranks only supported category facts.
- Missing live report configuration, HTTP failure, malformed payload or expired
  evidence fails the main report step visibly while preserving offline results.

## Verification

Use real disposable PostgreSQL + HTTP integration journeys for ownership,
source/review concurrency, eligibility, outcome storage and report projection.
Use local fixture storage with real small MP4 bytes for generation replacement.
Use gstack browser for save/reload/next/error flows, with synthetic data only for
mutating tests. Inspect the actual 15 private exports and retain private evidence.
Verify a main workflow download after approved deployment/merge. No paid model
calls and no automatic promotion of captured sessions into committed fixtures.

## Creator-edit correction before adding the exclusion state

The creator confirmed some exports were edited manually after generation. For each
instruction, allow “Changed by me” instead of inferring an AI failure from the final
export. These outcomes must be counted separately, never ranked as misses or met
requirements, and must not require listening to make an audio correctness claim.
The remaining unaffected instructions can still be judged. Add this real HTTP
journey before introducing the new verdict.
