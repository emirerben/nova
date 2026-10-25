# Creator editorial taste calibration

The private CLI performs no provider calls or account mutations. The account API
and native preference/feedback screens connect confirmed examples to camera-roll
idea generation behind `CREATOR_EDITORIAL_TASTE_ENABLED=false` and the explicit
`CREATOR_EDITORIAL_TASTE_USER_IDS=[]` allowlist. An empty list admits nobody; a list
containing `"*"` admits every account (see [Rollout](#rollout-to-every-account)).
Independent creative-quality evaluation remains pending. See [the implementation sequence](../reviews/editorial-taste/implementation.md).

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
(e.g. `assistant_authored_calibration`, not a fabricated provider run) and one to
four `options`, each with `id`, `idea` and `needs_creator_input`. The source must be
an existing development example; the store pins its source hash.

After the creator answers, use `judge --id <uuid> --input <json>
--expected-revision <n>`. The judgment contains `selected` (one id, several tied ids,
or `[]` to reject the single proposal or choose neither in a comparison), `reason`, `reviewer` and the exact `creator_statement`.
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


## Account adapter

Migration 0124 adds project-cascading examples, versioned profile revisions and
judgments pinned to exact story revisions. The authenticated `/me/editorial-taste`
GET/PUT/DELETE routes manage confirmed projects/rules; POST `/feedback` records
`would_post`, `needs_work` or `not_for_me`, optional reasons, notes and a rewrite.
A judgment does not build a project or authorize rendering. Project notes remain
contextual; they never silently become standing rules.

PUT requires `expected_revision`, a stable `idempotency_key`, owned `project_ids`,
`rules`, optional `project_notes` (at most 1,500 characters per project),
`confirmed: true` and `enabled`. Project notes preserve full contextual feedback;
the complete generation context remains capped at 8,000 characters. Revision changes
include feedback. Clients must reload after conflict rather than automatically
confirming a newer profile. The service captures saved server metadata, not client
supplied post words. This is not pixel inspection or evidence of publication.

The actual camera-roll proposer retrieves at most three examples against the
query and outing descriptions. Confirmed rules and up to two recent judgments
enter its bounded sensitive input. The prompt version and context bind its cache.
Story history stores opaque version/source-hash receipts, not copied private words.
Preferences and Memory pause are rechecked under an owner lock after generation;
changed context returns 409 before an idea is offered.

Native collection options and story review offer Idea preferences when the
account endpoint is available. The creator chooses example posts and confirms
rules. Would you post this? records feedback separately from Build this project.
Account Memory pause suppresses context. Source deletion removes the corresponding
example words. Forget editorial taste deletes example/feedback bodies and scrubs
all historical rules, retaining opaque retry fences; delayed writes cannot revive
erased memory. Full account Memory clear also erases taste even if its rollout
flag is off. A retry of an older clear cannot erase new consent.

Local checks use synthetic HTTP/PostgreSQL journeys and the native simulator.
A private replay of the ten approved account captures checks the real API/context
builder at zero model cost. That replay is development evidence, not production
activation or a claim that new ideas match the creator's taste.

## Rollout to every account

`CREATOR_EDITORIAL_TASTE_USER_IDS='["*"]'` admits every account to the
`/me/editorial-taste` routes. `CREATOR_EDITORIAL_TASTE_ENABLED` stays the kill
switch: `"*"` with it `false` admits nobody, and `[]` still admits nobody.

Admission is not personalization. `generation_context` returns an empty string
until the creator has confirmed a profile (`confirmed: true`, `enabled: true` in
the latest revision), so no example or rule reaches a prompt for an account that
has not opted in. Camera-roll idea generation keeps its own gate,
`media_collection_proposals_for`; this flag does not widen it. Account Memory
pause still suppresses context.

```bash
# apply (api + worker must share the value)
fly secrets set -a nova-video CREATOR_EDITORIAL_TASTE_ENABLED=true CREATOR_EDITORIAL_TASTE_USER_IDS='["*"]'
fly apps restart nova-video
# rollback (kill switch; leaves the list untouched, saved profiles are kept)
fly secrets set -a nova-video CREATOR_EDITORIAL_TASTE_ENABLED=false
fly apps restart nova-video
```

Verify: `GET /me/editorial-taste` returns 200 for an arbitrary account, and 404
after the rollback. Journey: `tests/integration/test_editorial_taste.py::test_wildcard_admits_everyone_but_personalizes_only_after_confirmation`.

## Frozen comparison workflow

`scripts/editorial-taste-eval.py` is provider-free. It freezes a baseline and
personalized arm, future source inputs, source-group exclusions, A/B assignments,
outputs/failure slots and genuine creator judgments. It does not generate ideas,
inspect media, contact a provider, authenticate an operator or start training.

First reserve every unseen project with the corpus CLI's `holdout` command.
Prepare a private preregistration JSON containing: evaluation/owner UUIDs; purpose
(`synthetic_workflow_check`, `development_comparison` or
`independent_quality_evaluation`); the exact corpus HEAD SHA; baseline/personalized
model and prompt SHA descriptors; a source registry covering every learning
example with its pinned source SHA, complete original-content fingerprints and
outing-group IDs; and future cases containing reserved project/case UUIDs,
fingerprints/groups, a query and the exact supported `MomentCandidate` intervals.
Fingerprint/group sets must be disjoint across training and test outings and across
cases. Missing coverage, seen projects, duplicate IDs and non-finite/unsupported
intervals reject registration. Group coverage needs an owner-scoped source audit;
claiming `complete` in a hand-authored JSON file is not independent evidence.

Freeze `criteria` as `{"primary":"creator_preference","secondary":["would_post",
"unsupported_claims","footage_mismatch"]}`. Independent evaluation also needs an
approved profile and preregistered acceptance bars: minimum cases, minimum lower
bound of the 95% Wilson interval for personalized preference, minimum willingness
to post, and maximum problem rate. No bar can be chosen after seeing the results.
The Wilson interval describes the observed non-tied preferences; it does not repair
unrepresentative sampling or correlated outings. Small samples remain uncertain.

```bash
src/apps/api/.venv/bin/python scripts/editorial-taste-eval.py --store /private/path/comparison \
  preregister --input /private/path/pack.json --corpus /private/path/corpus
src/apps/api/.venv/bin/python scripts/editorial-taste-eval.py --store /private/path/comparison \
  output --case <uuid> --arm baseline --input /private/path/baseline.json
src/apps/api/.venv/bin/python scripts/editorial-taste-eval.py --store /private/path/comparison \
  output --case <uuid> --arm personalized --input /private/path/personalized.json
src/apps/api/.venv/bin/python scripts/editorial-taste-eval.py --store /private/path/comparison \
  cards --case <uuid>
src/apps/api/.venv/bin/python scripts/editorial-taste-eval.py --store /private/path/comparison \
  judge --case <uuid> --input /private/path/actual-creator-reply.json
src/apps/api/.venv/bin/python scripts/editorial-taste-eval.py --store /private/path/comparison \
  report --corpus /private/path/corpus
```

Each output is `{"status":"ready","output":<CameraRollStoriesOutput>}` or
`{"status":"failed","error_class":"ProviderTimeout"}`. Unknown/repeated source
IDs, more than 60 seconds or missing beat reasons reject it. An identical retry
adds no outcome; changed outcomes cannot replace a failure or a reviewed proposal.
Cards omit arm/model metadata and pin a checksum. Preserve the exact creator reply
with that checksum, selected labels (`[]` = neither, `["A","B"]` = tie), separate
A/B `would_post` booleans and `problems` lists, reason and reviewer attribution.
Empty/failed proposals cannot be selected or marked publishable. Never invent a
reply. The report counts every case, missing outputs, failures, pending judgments,
ties, neither and factuality separately; changing the corpus invalidates the frozen
comparison. Export fidelity and audience performance need their own evidence.

Fine-tuning readiness stays false: reviewed residual failures, an adequate dataset,
a human training decision and an approved priced plan remain necessary. No numeric
sample threshold automatically sends data or starts training. Four real CLI
journeys exercise this workflow with synthetic responses and synthetic judgments;
they prove the mechanics and make no creative-quality claim.

## Measuring feedback (read-only, zero model cost)

`scripts/editorial-taste-measure.py` reads one account's `CreatorIdeaFeedback` rows
and offered `MediaCollectionStory` rows through the app's DB session, in a
`READ ONLY` transaction, and reports:

- would_post rate by ISO week (UTC) with a 95% Wilson band, plus offered counts.
- repeat-rejection rate: among rejections (not `would_post`) with a rejection reason
  recorded after an earlier reasoned rejection, the share repeating an earlier reason.
  Each repeat is split by the story's offered-time `editorial_taste.feedback_ids`
  receipt: the earlier judgment was in the prompt, was not, or the story predates
  receipts. The receipt is fixed when the story is offered, so an earlier judgment
  made after that moment gets its own bucket (`earlier_judgment_after_offer`) and is
  not counted as the prompt dropping it. Only the two newest judgments reach a
  prompt, so "not in the prompt" is expected for older reasons.
- wrong_facts rate: rows with `invented_experience` or `footage_mismatch`.
- context diff for `--query`: `generation_context` with and without `reviewed_ideas`
  (sizes, hashes, verdicts/reasons and note lengths; no text).

```bash
src/apps/api/.venv/bin/python scripts/editorial-taste-measure.py \
  --owner <creator-uuid> --query 'food review' [--json] [--simulate-flag-on]
```

The whole report fails closed: if the taste flag is off for the owner, personalization
is paused, or the profile is not enabled, it reads no judgments and prints `blocked_by`
with the reason (`--simulate-flag-on` only overrides the flag). Output carries a hashed owner, run count, spread and a **Limiter** line (one account,
small samples, no control arm). It never prints idea words, notes, the query or the
raw owner id. A non-local `DATABASE_URL` is refused unless `--allow-prod` is passed;
that flag only permits the read-only session. `--simulate-flag-on` builds the context
as if `CREATOR_EDITORIAL_TASTE_ENABLED` covered the owner, in-process only. Observed
rates are descriptive of one account and cannot show that the flag improved taste.
Journey: `src/apps/api/tests/integration/test_editorial_taste_measure_cli.py`.
