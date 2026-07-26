# Give Every Customer Route Correct Metadata Ownership

Status: Implemented 2026-07-26 — validation passed

Audited against: `b1790a47`
Revalidated against: `ca307fc5`

## Evidence chain

- Surface: `/`, `/plan/**`, `/library`, `/generative`,
  `/template-jobs/[id]`.
- Problem: `app/layout.tsx` supplies one marketing metadata object to every
  route. Private workspace routes inherit the landing title and social URL,
  while the landing has no canonical or social image.
- Design evidence: `fixing-metadata` requires one deterministic owner per page,
  canonical/`og:url` agreement, noindex for private/transient pages, and
  absolute Open Graph/Twitter images for shareable pages.
- Owner: `src/apps/web/src/app/layout.tsx`, plus route-family layouts added next
  to the existing route entrypoints.
- Approval: the user explicitly approved this root change and its indexing
  boundary before implementation.

## Design decision

Keep the landing and public generative entry indexable and canonical. Mark
authenticated workspaces and user-specific/transient render routes
`noindex, nofollow`. Give each route family a stable title and literal
description. Add one stable 1200×630 Kria social image referenced by absolute
Open Graph and Twitter URLs.

| Route family | Title | Indexing | Canonical |
| --- | --- | --- | --- |
| `/` | `Kria — Your AI content agent` | index, follow | `/` |
| `/generative` | `Make Your Edit — Kria` | index, follow | `/generative` |
| `/plan` | `Your Plan — Kria` | noindex, nofollow | omitted |
| `/plan/persona` | `Your Persona — Kria` | noindex, nofollow | omitted |
| `/plan/style` | `Your Style — Kria` | noindex, nofollow | omitted |
| `/plan/items/[id]` | `Your Video — Kria` | noindex, nofollow | omitted |
| `/plan/items/[id]/edit` | `Video Editor — Kria` | noindex, nofollow | omitted |
| `/plan/items/[id]/transcript` | `Script & Record — Kria` | noindex, nofollow | omitted |
| `/library` | `Your Videos — Kria` | noindex, nofollow | omitted |
| `/template-jobs/[id]` | `Render Status — Kria` | noindex, nofollow | omitted |

Descriptions remain static and never include job, user, plan, or template data.

## Reuse and preserved behavior

- Reuse `BRAND_NAME` and `CANONICAL_WEB_ORIGIN` from `brand.ts`.
- Reuse Next.js Metadata API, existing Kria lime/cream/black colors,
  Fraunces/Inter fonts, and the three-frame fan mark geometry.
- Preserve every route's render tree, authentication, navigation, API calls,
  favicon handling, viewport behavior, and component contracts.
- Add no runtime dependency, public API, database schema, backend behavior, or
  design-system migration.

## Implementation

1. Centralize deterministic public/private metadata composition in
   `src/apps/web/src/lib/site-metadata.ts`.
2. Keep the root layout limited to global metadata, let the landing page own
   its public metadata, and add a 1200×630 versioned
   `/opengraph-image/v1` route handler. An ordinary route is required here:
   Next.js file-based Open Graph images are inherited after metadata merging
   and would re-add share tags to private routes. The versioned URL prevents a
   later redesign from being trapped behind immutable social-image caches.
3. Add the narrowest route-family metadata owners needed by client pages.
4. Add contract tests for absolute public URLs and private-route inheritance
   suppression.

## Validation

- Targeted Jest metadata tests: 18 passed.
- Full frontend Jest suite: 1,886 passed.
- `npm run lint`: passed with pre-existing warnings only.
- `npx tsc --noEmit --incremental false`: passed.
- `npm run build`: passed.
- Rendered `/opengraph-image/v1` at 1200×630 and inspected it at full
  resolution and thumbnail scale.
- Inspected production-build metadata output for `/generative`, `/plan`,
  `/architecture`, `/admin/jobs`, `/dev-qa/clips`, and the not-found tree.
  Public metadata appeared only on its intended owners; excluded trees had no
  landing canonical or social tags.

## Stop conditions

- Stop if `/generative` is not intended to be publicly indexable.
- Stop if production's canonical host differs from
  `CANONICAL_WEB_ORIGIN`.
- Stop if a route needs user-derived metadata or a new sharing behavior.

## Engineering review

- Scope is larger than eight files because Next.js client pages require
  server-owned segment layouts for metadata. This is framework-required
  ownership, not a component rewrite.
- No database, API, auth, migration, concurrency, or rollback risk is introduced.
- Rollback is a single frontend commit; removing the route metadata owners
  restores prior inheritance behavior.
- `DESIGN.md` remains authoritative and unchanged.
