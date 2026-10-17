# Learning-loop acceptance and failure cases

Before implementation, verify the CLI end to end on a temporary repository and
private evidence directory. No model calls or production writes.

- Import the same capture twice: stable identity, no duplicate candidates and no
  overwrite of reviewed expectations. A changed capture invalidates review.
- Preserve ordered turns, media context, editor changes, decisions and operations;
  missing final output is unknown, not a pass. Paginated capture cannot silently
  truncate; API failures remain explicit.
- Unreviewed, rejected, stale, ambiguous or holdout proposals cannot be promoted.
  Expected outcomes require human attribution and creator-source pointers.
- Promotion validates the existing IncidentRecord and current contract schemas,
  writes into the existing corpus, refuses overwrite, and preserves provenance.
- Whole conversations and every derived variant share a split. Holdout reports
  expose counts/gaps without exporting their content into development fixtures.
- Coverage joins incidents, gate ledger and journeys; a PR link is a reference,
  never proof. Understanding, persistence and render evidence remain independent.
- Review binds a digest of the entire proposal. Editing expectations, sources,
  variants or the incident after review blocks promotion.
- No secrets, signed URLs or raw media go into committed examples. Captures stay
  outside the repository with owner-only permissions. Redaction is a review step,
  not an automated claim that arbitrary creator prose is safe to publish.
