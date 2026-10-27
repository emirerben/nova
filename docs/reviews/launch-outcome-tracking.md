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

Logs are private under `.dev/launch-hardening/outcome-counts-*`. Request lineage,
assessment history and durable Photos acknowledgements remain implementation
work; these passing counts do not close KRI-728.
