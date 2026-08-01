# Kria legal pages — compliance notes & pre-publish checklist

This is the internal companion to the two public pages at `/terms` and
`/privacy` (`src/apps/web/src/app/terms/page.tsx`,
`src/apps/web/src/app/privacy/page.tsx`). It carries the parts of the
`terms-of-service` and `privacy-policy` skill outputs that don't belong on a
public page: the compliance checklist, the jurisdiction notes, and the gap
list this PR intentionally leaves open. Placeholders that both pages import
live in `src/apps/web/src/lib/legal.ts` — that file is the single source of
truth for the legal entity name, address, governing law, and contact
addresses; update it, not the page components, when those facts change.

**Disclaimer** (repeated on both public pages): this documentation and the
pages it describes were drafted with AI assistance for planning purposes.
None of it is legal advice. A qualified attorney must review both documents
before they are relied upon in production — see the checklist below.

## Why this exists now

Kria (`usekria.com`) had no published Terms of Service or Privacy Policy.
That's a real gap, not a formality:

- TikTok's Content Posting API audit requires published terms + a privacy
  policy before the app can request broader publishing access
  (`docs/runbooks/tiktok-direct-publishing.md`).
- The product transmits users' raw video and audio — their own face and
  voice — to Google and OpenAI, asks for employer/school/location/travel
  history during onboarding, and by default scrapes and AI-analyzes a user's
  public TikTok profile. That's a meaningfully higher bar than a typical
  SaaS landing page.
- There was no account-deletion or data-export endpoint before this PR's
  Lane B, which both GDPR (Art. 15/17) and CCPA require in some form.

## Jurisdictions covered

GDPR (EU) + UK GDPR + CCPA/CPRA (California). Chosen because `usekria.com`
is publicly reachable worldwide and the product actively targets creators,
so restricting scope to "US only" would leave real exposure. Turkey/KVKK
was considered (the product has Turkish-language support) but left out of
v1 — revisit if a meaningful share of users are Turkey-based.

## Legal-entity posture

Kria currently operates as a **sole proprietorship** — no corporate entity
has been formed. That is reflected directly in the Terms (§12–13): without a
corporate veil, the liability cap and indemnification clauses are the only
thing standing between a claim and personal assets. Two areas raise the
stakes on this:

1. **The music library.** It's copyrighted and curated; §8 of the Terms is
   deliberately conservative and flagged `[⚠️ NEEDS YOUR INPUT]` pending a
   real answer about what license(s) are held for which tracks and
   platforms.
2. **Third parties in user footage.** Users upload real video of real
   people who never signed anything. §5 of the Terms puts the consent
   burden on the uploader and disclaims pre-screening, but that clause is
   only as good as its indemnification backstop (§14) — which is only as
   good as the entity behind it.

**Recommendation, not blocking this PR:** incorporate (Delaware LLC/C-Corp
is the standard move, ~1 week) before introducing any paid tier or scaling
user acquisition materially past today's volume.

## What was verified against the codebase (not assumed)

Full sourcing is in the approved plan at
`/Users/emirerben/.claude/plans/run-npx-skills-use-floating-church.md`. Key
facts baked into the copy:

- Google OAuth scope is exactly `openid email profile` — no Drive, no
  YouTube upload scope requested at sign-in.
- `User` model stores only `id, email, name, auth_provider,
  onboarding_status, created_at` — no avatar, no Google subject ID, no
  OAuth token on the user row itself.
- Sub-processors receiving user content: Google (Gemini File API — full
  raw video), OpenAI (Whisper transcription + GPT-4o caption correction),
  Google Cloud Storage, Fly.io, Vercel, Resend (waitlist email only), TikTok
  (opt-in). Anthropic and Langfuse are wired in but **default off**.
- Zero analytics, zero ad tracking, zero Sentry — verified by grep across
  `src/apps/web/src/**` and `layout.tsx`. This is a genuine differentiator
  and the policy says so affirmatively rather than hedging.
- Only cookies set are NextAuth defaults (session, CSRF, callback-url,
  transient OAuth state/PKCE) — no consent banner required.
- `tiktok_deep_analysis_enabled` defaults **true**; `style_vision_build`
  (downloads a user's TikTok videos to Google for vision analysis) is
  shipped but defaults **false**. The Privacy Policy discloses both, one as
  current behavior and one as a disclosed-but-inactive feature.

## Retention — what the policy promises vs. what infra enforces today

The Privacy Policy (§8) states specific retention windows. As of this PR:

| Category | Policy says | Enforced by |
|---|---|---|
| Anonymous/session uploads | 24h | `infra/gcs-lifecycle.json` — already live |
| Voiceover recordings, music renders | 24h | `infra/gcs-lifecycle.json` — already live |
| Speech transcripts (`transcript-cache/`) | 24h | **This PR** — added to `infra/gcs-lifecycle.json` (was previously unbounded; the cache is content-hash-keyed with no link back to a user, so account deletion can't find and purge it — see below) |
| Uploaded footage / rendered output (`users/…`, `generative-jobs/…`) | kept until you delete | Correct as-is — never auto-swept; `DELETE /me/account` (this PR) is now the removal path |
| Internal AI processing logs tied to a job | 30 days | Already enforced (`agent_run_retention_days`) |

**Corrected from an earlier draft of this document:** initial research flagged
`agent_run` rows with `job_id IS NULL` as an unenforced retention gap
containing questionnaire/persona text. Direct code reading
(`app/agents/_persistence.py::persist_agent_run`) shows this is not the case —
the persistence layer's own owner-routing (`_parse_owner`) explicitly refuses
to write a row when `job_id`, `template_id`, AND `music_track_id` all fail to
resolve (`if job_uuid is None and template_uuid is None and track_uuid is
None: return`), and `persona_build.py` / `content_plan_build.py` call their
off-job agents with exactly that shape (`RunContext(job_id=None)`). So those
calls produce **no row at all** — not an unswept one. Every `job_id IS NULL`
row that actually exists in the table has `template_id` or `music_track_id`
set, i.e. it's the template/track admin-debug data `cleanup_agent_runs`'s own
docstring says to keep forever. No code change was needed here; this file is
corrected instead so it doesn't send a future reader chasing a fix for a gap
that doesn't exist.

## Backend endpoints this policy depends on (Lane B)

The Privacy Policy declares a right to delete and a right to export. Before
this PR, neither had a real endpoint — `POST /personas/reset` deletes
persona/plan data but explicitly *keeps* rendered videos and does not touch
GCS objects; `DELETE /tiktok/connection` redacts TikTok-derived data only.
This PR adds:

- `DELETE /me/account` (as `POST /me/account/delete-request` +
  `POST /me/account/delete-confirm`) — full erasure: DB rows deleted in
  FK-safe order (mirrors `/personas/reset`'s established pattern, extended
  for `Job`/`OAuthToken`/`TikTokPublication`, none of which cascade from
  `users.id` at the DB level — see the docstring on
  `confirm_account_deletion` for the exact sequence and why), then GCS
  objects under `users/{user_id}/` and `generative-jobs/{job_id}/` are swept
  asynchronously (`tasks.purge_user_storage`). Two-step confirm — a Fernet
  token of the caller's own id, emailed as a code, verified + TTL-checked on
  confirm — so a stray call can't delete an account outright. (`AgentRun`
  rows tied to a job cascade-delete automatically via `ondelete=CASCADE`
  when the job is deleted — no separate handling needed; see the corrected
  retention note above for why `job_id IS NULL` rows were never a concern
  here.)
- `GET /me/export` — JSON bundle of account, persona, plans, transcripts,
  feedback, TikTok publications, plus a re-signed link to each job's source
  media (capped at 100 jobs — see the endpoint's docstring). **Deviates from
  the original plan's "async job, email a download link" pattern**: this is
  metadata plus a bounded set of links, not a multi-gigabyte archive, so a
  direct synchronous authenticated response is simpler and equally correct.
  Does not yet re-derive every render pipeline's rendered-*output* URL
  contract across all five job modes (generative/template/music/auto_music/
  content_plan) — only the original uploaded source is re-signed. Rendered
  outputs stay reachable via the existing library UI while the account is
  active; closing that gap is a reasonable follow-up if this endpoint sees
  real use.

See `src/apps/api/app/routes/me.py` for the implementation and
`src/apps/api/app/tasks/` for the async workers. **Both new task modules
must appear in the `include` list in `app/worker.py`** or Celery silently
discards them with no error at queue time — this is a known repo trap
(`[[celery-worker-include-coupling]]` in project memory).

## Pre-publish checklist

- [ ] Fill every `[YOUR …]` and `[⚠️ …]` token in
      `src/apps/web/src/lib/legal.ts` (legal entity name, address,
      governing law).
- [ ] **Create `privacy@usekria.com` and `legal@usekria.com` as monitored
      inboxes.** `hello@usekria.com` is already a live Resend sender; these
      two are not yet provisioned to receive mail. A policy naming an
      unmonitored address is worse than naming none.
- [ ] Answer the music-licensing question so Terms §8 can move from
      placeholder to specific language about what's actually cleared.
- [ ] Decide on GOVERNING_LAW / entity formation (see "Legal-entity
      posture" above).
- [ ] Attorney review of both documents — required by both skills' own
      disclaimers, and not pro forma given the liability-cap-at-$100 +
      third-party-faces-sent-to-Google fact pattern here.
- [ ] Apply the `infra/gcs-lifecycle.json` change by hand:
      `gsutil lifecycle set infra/gcs-lifecycle.json gs://$STORAGE_BUCKET`
      — editing the JSON alone changes nothing in prod
      (`[[gcs-lifecycle-drifts-from-repo]]`).
- [ ] After publish: submit `/terms` and `/privacy` URLs to TikTok's
      Content Posting audit.

## Open follow-ups (deliberately out of scope for this PR)

- No corporate entity formed — sole-proprietor exposure as described above.
- No consent checkbox gate at sign-in — the clickwrap notice under the
  Google sign-in button (added in `Header.tsx`) is the cheapest available
  enforceability win; a full "I agree" checkbox is a larger UX change,
  deferred.
- No cookie-consent banner — not required today (strictly-necessary cookies
  only), but must be revisited the moment any analytics or marketing
  cookie is added.
- `user_id → content_hash` index for transcript-cache is not built; instead
  we shortened the cache TTL to 24h so orphaned entries age out on their
  own. Revisit if a longer transcript cache TTL becomes worth the
  engineering to do it properly.
- `GET /me/export` re-signs each job's original uploaded source but not its
  rendered output (the URL contract differs across all five job modes — see
  "Backend endpoints" above). No frontend UI calls the new account-deletion
  or export endpoints yet — this PR ships the backend contract the Privacy
  Policy relies on; a settings-page UI to drive it is a natural next PR.
- No dedicated Fernet key for account-deletion codes — reuses
  `TOKEN_ENCRYPTION_KEY` (already-required infra for OAuth token encryption,
  see `services/token_crypto.py`) rather than provisioning a second secret.
  Fine in practice: a decrypted code is only ever compared to the caller's
  own `user.id`, so cross-purpose token confusion fails closed.
