# Creator editorial taste calibration

This is the private calibration boundary, not an active production learning
feature. It performs no provider calls, account mutations or fine-tuning uploads.
The actual generation adapter, in-app feedback, independent evaluation and rollout
remain subsequent work. See [the implementation sequence](../reviews/editorial-taste/implementation.md).

Use a private directory **outside every Git checkout**, with permission 700:

```bash
python3 scripts/editorial-taste.py --store /private/tmp/creator-taste \
  import --input /private/path/capture.json --owner <creator-uuid> \
  --reviewer '<actual creator>' --approval '<their actual statement>'
python3 scripts/editorial-taste.py --store /private/tmp/creator-taste \
  propose-profile --rules /private/path/rules.json --expected-revision 1
python3 scripts/editorial-taste.py --store /private/tmp/creator-taste \
  approve-profile --expected-revision 2 --profile-sha256 <returned-hash> \
  --reviewer '<actual creator>' --approval '<their actual statement>'
python3 scripts/editorial-taste.py --store /private/tmp/creator-taste \
  context --owner <creator-uuid> --query 'food review' --kind slides
python3 scripts/editorial-taste.py --store /private/tmp/creator-taste report
```

The operator records genuine human decisions; reviewer names do not authenticate
identity. Never manufacture approval, publication, media inspection or performance.
The creator's approval of a project does not prove that every historical message
was human-authored or that the saved metadata exactly matches the exported pixels.

The capture contains `owner_id`, a timezone-aware `captured_at`, `examples` and
`exposed_project_ids`. Each example has `project_id`, `owner_id`, `title`, `kind`
(`video`/`slides`), `words`, `creator_requests`, `source_descriptions`, optional
`transcript` and bounded `artifact` identity metadata. No media or signed URLs.
Examples are admitted as positive only because the import records explicit
creator confirmation. Ownership fields must first be checked against trusted
account records; this file tool cannot revalidate production ownership or consent.

Source changes refuse re-import rather than silently invalidating human reviews.
Unchanged retries add no vote. New examples return the proposed profile to review.
Checksummed immutable revisions detect accidental modification; they are not
cryptographic proof of authorship. There is one owner per store. Writes use a file
lock and atomic HEAD replacement; back up private revisions in approved private
storage. `/private/tmp` is temporary. Erase copies when the creator revokes use.

Record a development calibration with `calibration --input <json>
--expected-revision <n>`. Fields: `id`, `owner_id`, `source_project_id`, `provenance`
(e.g. `assistant_authored_calibration`, not a fabricated provider run) and two to
four `options`, each with `id`, `idea` and `needs_creator_input`. The source must be
an existing development example; the store pins its source hash.

After the creator answers, use `judge --id <uuid> --input <json>
--expected-revision <n>`. The judgment contains `selected` (one id, several tied ids,
or `[]` for neither), `reason`, `reviewer` and the exact `creator_statement`.
Unreviewed comparisons do not enter context. Reviewed choices remain contextual
examples, not automatically approved universal rules or render approval.

`holdout --project <unexposed-project-uuid>` reserves an id without its body. Seen
project ids cannot become unseen holdout, and holdout bodies cannot enter this
corpus. **This is a project-id guard only**: the eventual evaluation must also
exclude copied footage across projects using canonical media identities. No
independent holdout evaluation has run. The first ten are all development data.

The bounded context uses a transparent lexical baseline over at most three
positive examples, confirmed profile rules and up to two reviewed calibrations.
It labels examples as evidence of taste, never as facts about the current footage.
Inspect retrieval choices and measure them before choosing a more expensive
retrieval or model approach. Do not interpret structural checks as creative wins.

External TikTok research is a separate reference pool. Preserve post ids, original
capture dates, raw metric provenance, missing captions, promotion markers and the
extent of visual inspection. Archived view counts do not establish causality,
current counts or the creator's personal taste. Missing captions do not mean a
video has no text. PR prose is not a substitute for the source post.

Verification (real file/CLI journey, synthetic content, no network):

```bash
src/apps/api/.venv/bin/python -m pytest \
  src/apps/api/tests/journeys/test_editorial_taste_cli.py --noconftest -q
```

Provider-specific training export and fine-tuning are intentionally absent at this
stage. Readiness remains false until an adequate reviewed dataset and independent
evaluation justify that step. Inspect current provider support and obtain a priced
training budget before uploading a dataset or starting a paid training job.
