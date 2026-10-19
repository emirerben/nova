# Background verification and bounded recovery

The approval authorizes one fixed edit and, when explicitly accepted, up to two
corrective exports of that same edit. Existing approvals grant zero repairs.
Verification runs on the existing performance render worker, never the API or
the maintenance worker. A durable per-export outbox survives broker failure.

## Timeline decisions

- Duration owner: the approved recipe, including its existing brand-tail policy.
- Ripple: none; recovery cannot move a clip or another timeline lane.
- Scrub bounds: the approved recipe's duration, unchanged.
- Resize: none; canvas and source transforms remain approved values.
- Undo: no new edit operation; the last accepted artifact remains available.
- Preview/render parity: publication requires actual output checks against the
  same recipe/source receipts; a repaired export receives a new attempt receipt.

## Failure oracles, written before implementation

1. Completion returns `checking` without downloading/decoding media. No URL,
   poster or ready state is published until the worker passes every check.
2. Broker failure, a crashed worker, duplicate completion and duplicate delivery
   cannot lose a pending check, run concurrent claims, or publish twice. Claims
   expire only after the worker hard deadline. A bounded reconciler redelivers.
3. Recipe, approval, source, owner, upload generation or claim changes during
   decoding reject publication. Rejected candidates cannot be resurrected by
   completion replay. Last-good output survives all failure paths.
4. The phone hides unchecked candidates and resumes checking after interruption;
   older clients cannot accidentally expose an unchecked candidate.
5. Consent is recorded on the exact original approval execution, with a maximum
   of two repairs and a finite expiry. Legacy, denied, expired, changed and
   unknown-outcome actions receive no automatic render authority.
6. A corrective attempt changes execution state only: recipe digest, clips,
   timing, text, audio and source scope stay identical. An idempotent atomic
   budget cannot be spent twice by duplicate reports.
7. Measured failures name the check and corrective action. Identical failures
   without new evidence stop; unsupported/authority/infrastructure failures do
   not trigger speculative rerenders. No model calls or extra creative work.
8. The real iPhone flow must show approval permission, checking, a bounded
   corrective attempt and verified publication; a negative control must stop
   safely and preserve the accepted artifact. Local/synthetic evidence must be
   distinguished from production and physical-device evidence.

## Delivery order

1. Durable background checking, task leases/reconciliation, native waiting and
   publication fences; focused actual-media and database concurrency checks.
2. Explicit original-approval repair permission and deterministic correction,
   immutable edit fences, two-attempt budget, honest progress/failure messages.
3. Isolated iPhone QA build using retained account footage; repeatable evidence
   bundle; relevant reliability, API, schema and native checks.

The broader KRI-723 preparation migration and other render adapters remain open.

## First deterministic repair

The initial repair action normalizes an affected original into a fresh local
H.264/AAC decoding copy using the existing native source-conversion path. It
then reruns the unchanged recipe. This differs from plain retry, which feeds
the original container/codec back into the same composition. The original's
fingerprint is checked before conversion. The decoding copy is disposable and
cannot replace an original or become source authority. All output checks run
again; conversion success is never proof of a corrected edit.

Only complete measured picture failures (optionally accompanied by audio
failures on those same clips) qualify. Text-only, partial, authority and
infrastructure failures stop. A source already normalized in this consent
window cannot be tried again without new approval. A second attempt is useful
only if verification identifies a different affected source, within the same
two-attempt and 30-minute grant. No model calls are permitted.
