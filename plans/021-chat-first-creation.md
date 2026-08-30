# Kria Chat-First Creation

Status: approved for implementation

## Outcome

Make `/plan` the canonical, default-on product for signed-in creators: a durable
conversation with Kria that accepts footage, proposes creative direction, asks
for explicit confirmation, renders through Nova's existing pipeline, and keeps
the existing editor available beside the conversation.

The former plan home remains only as a kill-switch fallback. Legacy entry URLs
redirect to the equivalent `/plan` chat or gallery state.

## Architecture

```text
browser /plan
  |
  +-- creation thread API -------------------------------+
  |     durable events, revision fences, idempotency     |
  |                                                       v
  +-- direct owned upload --> PlanItem media --> CreatorAgentSession
                                                |
                                                +-- typed proposal
                                                +-- explicit confirmation
                                                v
                                             Job/render
                                                |
                         authoritative status/variants --+
                                                |
                                                v
                       existing EditorShell (?embedded=1)
```

`CreationThread` is the durable project and transcript projection. It may link
one `PlanItem`, the current `Job`, and one `CreatorAgentSession`.
`CreationThreadEvent` is append-only, owner scoped through its thread, strictly
ordered, revisioned, and idempotent by client event ID. Thread state never
contains external object paths or executable model output.

`PlanItem` remains authoritative for footage, intent, and editor identity.
`CreatorAgentSession` remains authoritative for typed creative planning and
confirmation. `Job` remains authoritative for rendering and variants.

## Product State Machine

```text
choose format -> attach media -> converse -> proposal -> confirm
      ^               |                         |          |
      |               +-- retry/remove          |          v
      +-- unavailable alternative               |       rendering
                                                 |          |
                                      adjust direction      +-- failed -> retry/adjust
                                                            +-- partial -> ready + retry variant
                                                            +-- ready -> play/download/editor
                                                                         |
                                                           converse -> confirm revision -> render
```

Messages received while a render is in flight are persisted as pending revision
intent. Once the exact generation is ready, the client submits one idempotent
prepare action. In-flight output is never silently mutated.

## Backend Contract

- Migration `0092` adds `creation_threads` and `creation_thread_events`, owner
  and revision constraints, append-only trigger, and a conservative backfill of
  unscheduled user-created drafts. Completed jobs are not rewritten.
- `POST /creation-threads` provisions the minimum Persona/ContentPlan when
  absent, a draft PlanItem, and the initial format artifact.
- `GET /creation-threads` returns project summaries. `GET
  /creation-threads/{id}` returns transcript, Creator Agent projection, and the
  authoritative re-signed Job/variant state.
- Revision-fenced message/action endpoints cover format, creative turns,
  confirmation, exact-generation revision preparation, retry, archive, and
  variant selection.
- Two-phase upload endpoints reserve signed owned targets and attach validated
  video, image, or voiceover media to the PlanItem.
- Server capabilities expose exactly Montage (`montage`, Classic default),
  Narrated (`narrated_planned`), and Talking to camera (`subtitled`).
- `CREATION_THREADS_ENABLED` defaults on. A disabled route returns 404.

## Frontend Contract

- `/plan` attempts chat-first capability before loading legacy persona/plan
  state. Only an API 404 selects the legacy fallback. Network and 5xx failures
  stay visible and recoverable.
- Desktop owns `h-dvh`: 260px project rail, full pre-render conversation, then
  a 420px chat rail with remaining width for the embedded editor. Only transcript
  and editor panes scroll.
- Mobile at 390x844 uses a compact project header, Projects sheet, Gallery,
  bottom-safe composer, horizontal format cards, and Chat/Editor tabs once ready.
- Both attachment affordances accept video, image, and audio. Narrated reuses the
  voice recorder. Counts and status hydrate from server state after refresh.
- The global Header is hidden only for canonical chat-first `/plan`.
- Embedded editor messages validate same-origin and the iframe window. Embedded
  mode forces the full overlay editor; direct editor breakpoints do not change.
- `/plan/new`, `/create`, `/create/manual`, `/library`, and `/generative` redirect
  to `/plan` chat/gallery state. Persisted backend contracts remain supported.
- `NEXT_PUBLIC_CHAT_FIRST_CREATION_ENABLED` defaults on.

## Failure Handling

Every recoverable state has a visible next action: per-file retry/removal,
voice recording or format change, stale-revision reload preserving draft text,
offline/reconnecting queue, format alternatives, partial-result playback plus
failed-variant retry, and terminal render retry or direction adjustment.

Ownership, upload object paths, expected revisions, confirmation manifests,
render generations, and idempotency keys are server checked. A model-authored
message cannot execute a render or storage mutation directly.

## Test Plan

Backend behavior tests cover ownership, path rejection, capabilities, revision
conflicts, idempotent retries, upload attachment, Creator Agent binding,
confirmation dispatch, Job reconciliation, partial/failed renders, queued
revision intent, archive, and backfill.

Frontend unit/integration tests cover transcript projection, artifact actions,
offline/stale draft preservation, refreshed media counts, projects/gallery,
redirects, bounded layout, collapse, mobile sheet/tabs, iframe messaging,
keyboard/focus behavior, reduced motion, and 200% zoom.

Playwright and local browser QA cover 1440x900, 1280x720, and 390x844. A real
local Montage acceptance run uses repository clips from creation through render,
playback, download, embedded editor, and one confirmation-gated revision.

Focused suites run first, then API Ruff/format/full pytest, web lint/TypeScript/
full Jest/Playwright, and `bash scripts/preship-check.sh`.

## Deployment and Rollback

Deploy database/API first, then web. Defaults are on in source. Before launch,
verify no production override keeps either flag off. Roll back web with the
frontend flag; roll back API with the backend flag after the web fallback is
live. Existing PlanItems, Jobs, editor links, and completed Gallery entries stay
valid in either mode.

## What Already Exists

- Creator Agent sessions, typed strategies, manifests, confirmation, exact
  generation, and ownership fences are reused rather than rebuilt.
- Direct owned uploads, PlanItem media, generative Jobs, variant re-signing,
  render dispatch, Gallery data, voice recording, and EditorShell remain the
  production implementations.
- Existing plan home is retained only as the rollback renderer.

## NOT in Scope

- Rewriting EditorShell or the render pipeline.
- Removing persisted legacy backend models or old render contracts.
- Changing agent prompts; if implementation proves a prompt change necessary,
  its version bump and live eval become blocking work.
- A second render state machine, executable chat operations, or storage paths in
  thread JSON.
- Production deployment before the autoship pre-merge approval gate.

## Work Lanes

| Lane | Modules | Depends on |
|---|---|---|
| Backend | API models, migration, routes/services, route tests | existing Creator Agent/render contracts |
| Frontend | `/plan`, creation API client, shell, redirects, component tests | response schema; mockable in parallel |
| QA/docs | dev QA fixtures, Playwright, DESIGN/runbook | approved Paper states; core selectors stabilize before final pass |
| Integration | schema reconciliation, full gates, localhost | all three lanes |

The first three lanes run in parallel with non-overlapping ownership. Integration
is sequential.

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | not run | Approved user plan is the product decision |
| Codex Review | `/codex review` | Independent second opinion | 0 | pending diff | Runs before ship |
| Eng Review | `/plan-eng-review` | Architecture & tests | 1 | clear | Existing contracts reused; ownership, deploy skew, pending intent, and rollback specified |
| Design Review | `/plan-design-review` | UI/UX gaps | 1 | clear | Paper desktop, mobile, and recovery states are acceptance criteria |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | not run | Local setup and complete gates specified |

**VERDICT:** ENG + DESIGN CLEARED — ready to implement

NO UNRESOLVED DECISIONS
