# Chat-first creation cleanup audit

The rollback window is closed. Chat-first creation is the sole signed-in
`/plan` product experience.

## Removed

- The former plan home, persona onboarding funnel, footage fork, and standalone
  payoff UI.
- The frontend build flag, backend feature switch, account allowlist, and the
  deploy-skew fallback into the former experience.
- Tests and UI helpers that existed only for those retired surfaces.

## Retained compatibility

- `src/apps/web/src/app/plan/items/[id]` remains the canonical persisted-item
  release desk and editor surface used by chat projects and Gallery.
- PlanItem, generative-job, Creator Agent, persona, content-plan, upload, and
  manual-draft backend contracts remain because persisted projects and the
  current rendering/editor pipeline depend on them.
- `/plan/new`, `/create`, `/create/manual`, `/library`, and `/generative` remain
  unconditional redirects so bookmarks enter the canonical chat or Gallery.
- Gallery keeps using the shared `LibraryTile` and owner-scoped playback APIs.

Rollback is a deployment revert. New work must not recreate a second `/plan`
router or account-scoped creation experience.
