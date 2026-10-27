# Request outcome tracking

Scope is KRI-728 and the existing request-following collection/report. Do not
create another analytics pipeline. This is instrumentation, separate from
request fulfillment and native export verification.

## Acceptance and data shape

Preserve an original request, each explicit follow-up or recovery, all planning
and rendering attempts, the generation-bound artifact and human assessment.
Ready jobs, successful file exports, Photos-save callbacks and fulfillment are
different facts. A successful revision cannot rewrite the original failure.
Legacy records with no trustworthy request link remain unknown.

The proposed additive turn metadata links opaque request, parent-turn and
intent-revision identifiers. It lives on the existing committed user event and
does not change conversation semantics. Client replay keeps the first link.
Foreign, missing or legacy parents cannot acquire a known original request.
Existing render-retry execution receipts remain the attempt authority.

Human assessments retain immutable artifact identity, request/revision basis,
per-requirement verdicts and assessment history. Duplicate callbacks or repeated
identical assessments cannot create another success. A Photos acknowledgement
must follow a successful native save, survive an offline retry and bind to the
actual artifact. Old generations cannot replace the current artifact.

Reports must identify their selected population, denominator, date window,
client/protocol/cohort, latency and known/unknown cost. No creator messages,
transcripts, filenames, URLs or media belong in aggregate output. Missing human
inspection, unlinked legacy attempts and unknown provider outcomes remain unknown.

Failure controls include pre-job planning failure followed by a ready job,
ready-but-partial output, original failure followed by an accepted alternative,
repeated and delayed acknowledgements, stale artifacts, incomplete captures,
consent withdrawal and no human review. Native fixtures do not prove real saves.

## Completed counting boundary

The actual Cunda failure used `processing_failed`, which the report did not
count. The dryer case completed its runtime turn with a structured planning
failure response, so a failed-turn-only query missed it. A ready job elsewhere
in the same captured session also hid pre-job failures.

Four new HTTP/Postgres checks fail before the correction. The report now counts
both render-failure states and deduplicates structured pre-job failures by turn
identifier. The operator queue uses the same projection. A later ready job does
not erase that failure or establish fulfillment. The full collector/report and
actual-export-publication matrix passes 36 checks. The collection test follows
its bounded scan and response pages rather than assuming a single batch covers
the scratch database. No production record was changed by these checks.

Logs are private under `.dev/launch-hardening/outcome-counts-*`.

## Request and revision identity

Optional typed metadata now lives on the existing user event. The original
request, parent turn and intent revision use opaque identifiers. A retry keeps
the revision; a revision or explicitly accepted recovery starts another one.
Missing, foreign and legacy parents remain unknown. Transport replay retains
the first committed link and cannot append another request event.

The iPhone sends the metadata only when the server advertises support. Structured
answers continue the request. Only the matching offered simpler-version choice
is classified as recovery. Client build, version, protocol and the existing Fly
deployment revision accompany the link. Existing request digests remain stable.

The seven real Postgres lineage journeys pass alongside the runtime neighbors. The native
context, structured-answer and suggestion suites pass 46 tests with no skips.
The chat fixture also passes after the typed submission change. Result bundles
are private under `.dev/outcome-ios/`.

## Historical assessments and report

Human reviews specify whether they judged the original request, a revision or a
recovery. A known basis requires a server-owned turn and render execution for
the exact current owned artifact. Legacy reviews remain unknown. Compact
assessment history retains artifact identity, expectation digest and fixed
verdicts without copying notes or creator text. Identical assessments deduplicate.
Superseding a capture preserves prior assessments, and revoked eligibility hides
the entire history.

The existing daily report now groups linked original requests and distinguishes
original fulfillment, revision fulfillment and recovery success. It reports
request and attempt denominators, first-attempt unknowns, pre-job failures,
historically failed jobs, correction effort, observed latency and deduplicated
settled costs. It preserves unknown costs, versions and cohorts. An incomplete
capture or multiple uninspected export attempts cannot establish first-attempt
fulfillment. The report describes a selected eligible sample, not all customers.

Six HTTP/Postgres acceptance cases fail against the parent PR. Two further
controls reproduced older captures overwriting new attempt counts and a ready
retry hiding an earlier failure on the same job. The integration matrix covers
these controls, consent withdrawal, duplicate reviews, legacy unknowns and actual
export publication. The final request/report/publication and plan-review matrix passes 92 checks after refreshing main. Another 56
completion and actual-export verification checks passed before the final report
correction. That correction changes only report projection, and the final matrix
was rerun afterwards. Logs are private under `.dev/launch-hardening/outcome-*`.

Actual exports continue to use the existing generation-bound device completion
and verification receipts. Their repeated, delayed and rejected callbacks retain
the existing publication checks. Photos-library save callbacks, result acceptance
and abandonment remain explicitly unknown. They are never inferred from an
approval, ready job, Save tap, share sheet or missing feedback. No additional
Photos telemetry pipeline is introduced by this measurement change.

The final review also reproduced two false-success paths. Recovery success now
requires the matching server-stored simpler-version question and accepted
selection, rather than a client label alone. Rejecting an intent review keeps
its history but excludes its fulfillment claim. A future protocol number cannot
block the requested action through analytics validation. The legacy request-id
guard now pins the actual parent digest instead of reconstructing it from a
newer schema. Those controls and neighboring plan-review checks pass together.

A legacy thread cannot create a known original merely by restarting client
metadata. Server-marked duplicate messages do not count as correction effort.
Both controls failed before correction and pass in the final 92-check matrix
alongside the latest lip-sync and Cunda main-branch journeys.

Final collector review reproduced a missing-job completeness error introduced
while adding cost capture. With fewer than 1,001 agent runs, an unowned job was
excluded correctly but its missing evidence was not marked. A real
collector/HTTP/Postgres replay showed that this allowed an incomplete capture
to acquire a fulfilled first-attempt assessment. Job completeness is now checked
independently of cost truncation. The replay verifies the assessment is refused,
first-attempt fulfillment stays unknown, and foreign media and cost stay absent.
