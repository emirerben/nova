<!-- /autoplan restore point: /Users/emirerben/.gstack/projects/emirerben-nova/detached-main-autoplan-restore-20260906-105550.md -->
# Creator Memory: Paper Design and Implementation Plan

Status: Implemented in v0.67.0.0; staged rollout pending
Planned at: `41c876d03` (`origin/main`, detached worktree)
Intent: Give every creator transparent, reliable control over the direction Kria carries between projects, with an editable memory view and narrowly bounded automatic learning.

## Product promise

Kria should apply durable creator direction consistently across projects and show the creator why. A creator can say “always use Playfair Display,” “never add shadows,” “my videos are calm travel diaries,” or “keep hooks understated” once, then see and edit that direction under their profile. New projects and chat sessions receive the current direction automatically.

Project-specific instructions remain local to that project. Explicit durable language such as “always,” “never,” or “from now on” becomes active account memory immediately and produces a visible Undo receipt. Softer inferred patterns become suggestions and cannot affect another project until the creator accepts them. The system never learns from assistant text, filenames, model-generated media analysis, hidden metadata, or editor clicks in v1.

## Paper design

Use the ChatGPT Personalization pattern the user selected, but remove the artificial distinction between creator profile and memory. `/plan/profile` is one quiet, spacious **Personalization** document containing everything Kria knows and should remember about the creator’s videos.

The page begins with **Use personalization in new projects**, then reads as natural-language sections: **About your videos**, **Visual style**, **Storytelling and tone**, and **Things to avoid**. Existing persona fields supply the first section; creator-memory items supply and refine every section. The UI never exposes those storage boundaries. A bottom text field says **Tell Kria what to remember**.

The summary is derived deterministically from canonical memory items; it is never persisted as a generated blob. Every displayed statement retains its item id and has quiet `Edit` and overflow actions for **Stop using for future videos**, **View source** when available, and eligible **Undo**. Enforcement/scope appears as small secondary text only when useful; no colored badge grid or rule cards.

Suggestions appear as one plain text block: the proposed statement, “Not used until you accept it,” and text actions for **Why this suggestion**, **Remember**, and **Dismiss**. Conflicts or unsupported rules stay inline with the relevant statement and use direct copy plus **Review**, not a dashboard warning. Project confirmations show one subtle line, **Using your saved preferences · View**, which expands only on demand and contains the project-only override action.

Adding, editing, removing, or contradicting a memory happens through the text composer or statement action. Before mutation, a compact text review confirms the normalized instruction, future-video scope, and whether it is enforced or advisory; save errors preserve the draft. Success is one inline sentence with exact 10-minute Undo. The top menu contains only pause/resume personalization, clear remembered preferences, and privacy help.

On mobile the same single route and content hierarchy remain. The composer is in normal flow or sticky within its scroll region with safe-area/content padding, never absolutely over content or the software keyboard. Production uses the existing `LightShell`, editorial heading type, body type, real Switch/Input/Button/Dropdown primitives, 44px actions, live status, and focus return; the paper’s minimal visual switch is not an implementation primitive.

## Memory model and rules

Store account-memory enablement and revision on the durable `User` row, then add an item ledger. Do not couple account memory to the resettable one-to-one `Persona` row, create a second profile row, or persist an opaque summary blob:

- `User.creator_memory_enabled` and `User.creator_memory_revision`: account-level switch and monotonic optimistic-concurrency revision. `/personas/reset` deletes the `Persona` row, so account memory deliberately survives persona/onboarding reset.
- `CreatorMemoryItem`: stable id, user id, future-safe `scope_kind` (`account` in v1), category, normalized key, human-readable instruction, enforcement (`constraint`, `default`, or `advisory`), structured value when supported, source kind (`profile` or `creation_thread`), nullable source thread/event, confidence, state (`active`, `suggested`, `dismissed`, `superseded`, `forgotten`), `user_locked`, and timestamps.
- At most one active executable item per `(user_id, scope_kind, normalized_key)`. Free-form advisory instructions deduplicate by a normalized content hash. Historical rows remain for audit and Undo but are never injected.
- Existing `Persona.style`, user-wide feedback rollups, and workspace preference signals are inputs/compatibility projections behind one resolver, never co-equal memory authorities. There is no persisted generated memory summary in v1.

Precedence is explicit:

1. Safety/accessibility and renderer capability constraints always win and produce a visible conflict when creator direction cannot be honored.
2. A creator’s explicit instruction in the current project controls that project without changing global memory.
3. An explicit global constraint applies unless the creator explicitly overrides it for this project or revokes it globally; the conflict is shown, never silently resolved.
4. Global defaults apply only when the current project is silent.
5. Accepted advisory memory influences planning but cannot claim deterministic enforcement.
6. Existing `Persona.style` remains the compatibility fallback when active memory has no rule for that field.

Supported structured keys initially cover video/content types, tone, pacing, edit format mix, font family, text color, highlight color, text size, text position/alignment, font cycling, stroke width, and shadow enabled. Unknown but safe durable instructions remain prompt-only memory instead of being converted into executable settings.

## Automatic learning

After a creator-authored creation-thread message is durably committed, enqueue an idempotent background extraction task keyed by the thread event id. The extractor receives only the bounded user message plus the creator’s current active-memory snapshot. It returns typed operations: `activate_explicit`, `suggest`, `supersede`, `forget`, or `noop`.

The extractor must:

- accept only durable cross-project statements; “use Playfair in this video” stays project-local, while “always use Playfair” becomes active memory with an Undo receipt;
- classify explicit “always/never/from now on” language as active constraints/defaults, while softer recurring taste can create only an inert suggestion;
- never manufacture a preference from one-off project details, assistant messages, filenames, transcripts, or media analysis;
- treat all user text as untrusted data and emit a strict allowlisted schema;
- resolve contradictions by superseding the older automatic item, but never overwrite a locked user-authored item unless the message explicitly revokes it;
- fail open for the project: extraction failure never blocks chat, planning, or rendering.

Style-agent writes and explicit feedback notes use the same direction service. A typed style edit can activate a structured memory item; an unstructured feedback note can create only a suggestion unless it contains explicit durable language. Successful activation adds a `memory_updated` thread event with “Remembered for future videos” and Undo; suggestion creation stays quiet except in the Memory page.

## Planning and render enforcement

Add one canonical `CreatorDirectionSnapshot` service that loads active memory plus compatibility inputs, applies precedence, and produces:

- a bounded prompt block split into hard constraints, preferences, and creator context;
- typed visual overrides for the render pipeline;
- an owner-safe applied-rule receipt and private debug metadata containing item ids and the memory revision; no raw private text appears in logs, admin list views, or public responses.

Use that snapshot in:

- Main Creator Agent planning for every creation-thread project;
- content-plan generation and plan regeneration;
- persona/style agent context where existing values are discussed;
- generative render dispatch and re-render, with the memory revision and applied item ids persisted in the private assembly plan for reproducibility.

Extend the existing parity-safe `UserStyle`/overlay override path for `shadow_enabled`, then enforce structured memory after curated/derived defaults and before per-project explicit edits. Both Pillow and Skia renderers must honor the same field. Hard “never” constraints are validated after strategy compilation so an agent cannot reintroduce a forbidden treatment in prose or a style set.

Existing users keep their current persona and style. No executable backfill runs in v1: the snapshot service reads `Persona.style` as a lower-precedence compatibility fallback, and the Memory page does not relabel AI-derived TikTok/vision style as creator-authored memory. A later conversion requires golden-render parity for every job mode.

## API and UI contracts

Authenticated profile-memory endpoints:

- `GET /me/memory` returns enablement, revision, inert suggestions, owner-safe provenance, and a deterministic `personalization_sections` projection combining resolved persona compatibility context with active memory. Each statement has a stable statement id, optional memory-item id, section key, text, enforcement/scope/conflict/source status, and available actions; the response never persists generated summary prose.
- `POST /me/memory/items` creates a locked user-authored item.
- `PATCH /me/memory/items/{id}` edits/locks an item with `expected_revision`.
- `DELETE /me/memory/items/{id}` soft-forgets it with `expected_revision` and returns an Undo token.
- `POST /me/memory/items/{id}/restore` restores a forgotten item when it does not conflict with a newer active key.
- `POST /me/memory/items/{id}/accept` promotes a suggestion to a locked user-approved item.
- `DELETE /me/memory/items/{id}/suggestion` dismisses an inert suggestion.
- `PATCH /me/memory` toggles enablement with `expected_revision`.

All routes are owner-scoped, reject unknown structured keys/values, sanitize text, cap active items and total text, and return stable error codes for stale revision, duplicate/conflict, limit reached, unsupported value, and missing ownership.

Editing a Persona-derived compatibility statement creates a locked memory item at higher precedence rather than mutating hidden generated persona data. The next resolver projection replaces the stale statement with the creator’s explicit correction wording. This gives the unified UI one predictable correction model while preserving the existing Persona as a lower-precedence onboarding input.

## Rollout, privacy, and operations

Gate learning/injection with backend `CREATOR_MEMORY_ENABLED` and the profile UI with `NEXT_PUBLIC_CREATOR_MEMORY_ENABLED`; deploy backend first. Flag off means no extraction, no injection, and old behavior remains unchanged. Existing memory remains stored and readable to the owner when learning is paused.

Add retention/deletion handling so account deletion cascades memory rows, deleted projects null their source link without preserving the project name, and admin/debug projections expose counts and ids but not full instruction text by default. Forget affects future snapshots only: queued/in-flight/completed job snapshots remain immutable for reproducibility and the UI states this plainly. Update account export and the privacy page to describe creator-authored cross-project memory, automatic extraction, model-provider processing, editing/forgetting, and the enablement control.

## Verification outline

- Model/migration tests for ownership, active-key uniqueness, revision conflicts, suggestion acceptance/dismissal, soft forget/restore, and cascade deletion.
- Extractor unit/eval fixtures for durable vs one-off directions, always/never constraints, contradictions, explicit revocation, locked-item protection, multilingual statements, injection-shaped text, and no-op messages.
- Route tests for auth, sanitization, bounds, stable errors, concurrent edits, and disabled-flag behavior.
- Planning tests proving a new thread and a regenerated content plan receive the same bounded snapshot while a disabled profile does not.
- Render tests proving font and `shadow_enabled=false` survive strategy/style selection, re-render, and both Pillow/Skia paths; extend renderer-parity guards and run `make verify-overlays` before merge.
- Frontend tests for navigation, loading/empty/error states, add/edit/forget/undo, stale revision recovery, enablement toggle, mobile actions, keyboard navigation, and screen-reader labels.
- End-to-end acceptance: state “Always use Playfair Display” and “Never add shadows” in Project A, create Project B without repeating them, verify the proposed plan names the preference and the rendered overlay uses Playfair with no shadow; then edit/forget each rule and verify Project C reflects the new state.

## Initial assumptions for review

- “Constantly learn” uses hybrid authority: explicit durable creator language activates automatically with visible Undo; softer inference becomes a suggestion and never affects output before acceptance.
- Memory is account-wide in v1; the schema carries a future-safe scope field, while project-local directions do not become global unless the creator expresses durable intent.
- User-authored/edited memory is protected from automatic overwrite.
- The first implementation delivers both paper-designed UI and end-to-end enforcement; a display-only memory page is not sufficient.
- Implementation starts only after this plan passes `/autoplan` and the user approves the final review gate.

## CEO review: strategy, scope, and product-system contract

### System audit and premise decision

The repo already contains most of the raw ingredients: `Persona.style` and its typed `UserStyle` schema, `/plan/style`, user-wide feedback rollups, explicit workspace preference signals, durable creation-thread events, and per-job style snapshots. They are fragmented, several are behind dark flags, and none gives the creator one inspectable account-level contract. The job is therefore not to invent memory from scratch; it is to turn existing signals plus explicit durable chat instructions into one explainable creator-direction control plane.

The product premise is confirmed: explicit durable language in ordinary chat may become active account memory automatically with a visible Undo receipt; softer inference is inert until accepted. V1 exposes one account-wide identity with project-specific overrides and stores `scope_kind=account` so future brand/channel scopes do not require a destructive migration. The user-facing frame remains “What Kria remembers,” while the internal promise is reliable creator control with visible reasons.

Landscape synthesis: creator tools such as Canva already treat fonts, colors, tone, templates, and guidelines as one brand foundation used by their AI. The tried-and-true pattern is explicit saved defaults; current AI assistants add inspect/forget controls. Kria’s differentiator is not a longer memory list but a direction receipt that proves which creator rules shaped a plan and render.

### Existing-code leverage map

| Sub-problem | Existing foundation | Decision |
|---|---|---|
| Durable account owner | `User` row | Add enablement/revision here so `/personas/reset` cannot erase account memory; no second profile table |
| Typed visual defaults | `UserStyle` and `/personas/style` | Keep as compatibility projection and route all writes through the direction service |
| Conversational style edits | `StyleIntentAgent` and `/plan/style` | Reuse typed edits; do not build a separate style agent |
| Explicit project preferences | `CreatorWorkspacePreferenceSignal` | Reuse idempotency and ownership patterns; promote only durable explicit language |
| Cross-project reactions/notes | `VideoFeedback` and `feedback_summary` | Treat as advisory input/suggestion evidence, never executable truth |
| Durable chat source | `CreationThreadEvent` | Key extraction idempotency and provenance to the committed user event |
| Planning context | `creator_context`, content-plan and intro-writer preference blocks | Replace scattered assembly with the bounded direction snapshot |
| Render hermeticity | `all_candidates` and per-variant `user_style_knobs` | Preserve immutable job-time snapshots and original-snapshot re-renders |
| Renderer support | Pillow and Skia already read `shadow_enabled` | Extend parity-safe schemas/types/tests before exposing the knob |

### Dream state and alternatives

```text
CURRENT                         THIS PLAN                         12-MONTH IDEAL
Persona/style/feedback          one account direction            account -> brand/channel
are separate and mostly  --->  resolver + visible ledger  --->  -> project -> render policy
hidden; users repeat rules      + hybrid learning + receipts     with capability-aware conflicts
```

| Approach | Completeness | Effort | Risk | Decision |
|---|---:|---:|---:|---|
| A. Explicit settings only: extend `/plan/style`, no chat learning | 6/10 | M | Low | Rejected: safe but misses the stated “say it once in normal chat” value |
| B. Hybrid direction ledger and single resolver | 10/10 | L | Medium | Selected: complete user value with bounded learning authority and one resolution boundary |
| C. Full scoped policy engine with brand/channel profiles in v1 | 10/10 | XL | High | Deferred: correct long-term shape, but brand onboarding is outside the confirmed first-release premise |

Selected scope additions are transactional extraction outbox, exact operation/Undo receipts, applied-rule receipts, versioned immutable snapshots, and sensitive-data redaction. Deferred: brand/channel profile UI, editor-click learning, retroactive re-render after Forget, and semantic deduplication of free-form rules. Skipped: generated summary as persistent truth and silent promotion of soft inference.

### Temporal implementation decisions

| Build window | Decision locked now |
|---|---|
| Foundation | One `CreatorDirectionService` owns all preference writes; `Persona.style` direct route mutations are refactored behind it |
| Core logic | Exact typed-key conflicts, explicit/project override semantics, suggestion acceptance, and compare-and-set Undo are defined before UI |
| Integration | Every planning/render mode mints one versioned snapshot before enqueue; old queued jobs remain hermetic |
| Polish/tests | Privacy redaction, feature-flag skew, deletion races, queue failures, and renderer parity are release gates rather than follow-ups |

### 1. Architecture review

The canonical boundary is a service/resolver pair, not a new co-equal database. `CreatorDirectionService` owns activation, suggestion, acceptance, dismissal, forget/restore, and project overrides; `CreatorDirectionResolver` is the only reader allowed to compile planning/render state. Existing `Persona.style`, feedback summaries, and workspace signals feed or project through that boundary.

```text
committed user event ──┬─ explicit durable rule ─▶ outbox ─▶ DirectionService ─▶ active ledger item
                       └─ soft pattern ───────────▶ outbox ─▶ DirectionService ─▶ inert suggestion
                                                                    │
Persona.style + feedback + project instruction ─────────────────────┤
                                                                    ▼
                                                     CreatorDirectionResolver
                                                                    │
                                      ┌─────────────────────────────┴─────────────────────────┐
                                      ▼                                                       ▼
                             bounded planning block                               typed render overrides
                                      └──────────────────▶ versioned immutable job snapshot ◀──┘
```

At 10x load the extractor queue and prompt cost fail first, so a deterministic durable-language prefilter, per-user quotas, bounded tokens, and a separate retryable outbox are required. At 100x, active-item/provenance queries require compound indexes and batched loading; snapshot construction must remain one bounded owner-scoped query. Rollback is feature-flag based and leaves additive schema/data intact.

### 2. Error and rescue map

| Codepath | Failure | Rescue | User sees |
|---|---|---|---|
| Chat event + outbox transaction | DB conflict/rollback | Retry existing request-id path; neither event nor outbox commits partially | Existing chat retry state |
| Outbox dispatcher | Redis/Celery unavailable | Retain pending row, exponential retry, dead-letter after bounded attempts | “Pending” only when an explicit rule receipt is expected |
| Extractor | timeout, refusal, malformed/invalid schema | Specific model/runtime handling; no mutation; retry then dead-letter | Chat continues; Memory page may show retryable pending rule |
| Direction activation | duplicate delivery or stale revision | Event/operation idempotency and row lock/CAS | One receipt, no duplicate rule |
| Conflict resolver | incompatible active constraints | Persist conflict; do not guess | Applied-rules panel names conflict and choices |
| Undo | newer edit or expired/single-used receipt | CAS against resulting revision | “Can’t undo automatically; review the current rule” |
| Snapshot build | malformed compatibility style | Drop invalid compatibility input, retain active memory, structured warning | Project proceeds with supported rules |
| Render capability | requested field unsupported in mode | Capability-aware conflict/fallback; no silent claim of enforcement | Receipt marks rule conflicted/unsupported |
| Profile CRUD | stale revision, limit, invalid value | Stable typed 409/422 responses; preserve draft | Inline actionable error |
| Source deletion | provenance target disappears | `SET NULL`; generic source label | “Remembered from a past project” |

### 3. Security and privacy review

Memory text is sensitive creator-authored data and is untrusted prompt input. Source text is delimited as data, cleaned for control/role markers and hidden Unicode, length-limited, and never allowed to propose owner/scope identifiers; model output is validated against typed keys and allowlisted values after generation. Cross-user item access is prevented by owner-scoped queries, not by accepting a user id from the client.

Extractor and planner prompt blocks must be marked sensitive so full-input `AgentRun` tracing, admin job debug, logs, public job responses, and `assembly_plan` never expose raw memory text. The owner’s account export does include readable memory items and operation history; account deletion cascades items, receipts, and outbox rows, while project deletion only nulls source FKs and removes names. Privacy copy identifies automatic processing and model-provider use.

Threat tests cover prompt-injection text, fake system messages, oversized input/output, control characters, Unicode normalization, cross-tenant IDs, deletion during extraction, and a worker attempting to recreate state after account deletion. No new dependency or secret is required.

### 4. Data flow and interaction edge cases

```text
USER TEXT ─▶ durable-marker prefilter ─▶ typed extractor ─▶ validated operation ─▶ ledger ─▶ receipt
   │                │                        │                    │                │          │
 nil/empty      local-only=noop        timeout/refusal       bad key=noop     conflict     stale/hidden
 too long       soft=suggested         malformed=noop        wrong owner=deny CAS/retry    safe projection
```

Explicit activation is an immutable operation: operation id, prior item state, resulting revision, actor, source event, and Undo expiry/single-use state. Undo restores only that operation when the resulting revision still matches; double-click, refresh, offline retry, and contradiction-after-activation are idempotent or return a conflict. “Forget” is labeled “Stop using for future videos,” because historical job snapshots remain immutable.

Project override is three distinct choices: use a different default for this project, allow a constraint once, or change the global rule. Empty-state examples are visibly examples and require an explicit add action. Suggestions show redacted evidence metadata and an `advisory` status without exposing the raw past message.

### 5. Code quality and source-of-truth review

Direct `Persona.style` mutations in persona/style-agent/workspace routes must be refactored to the direction service; a repository guard test prevents new route/task writers from bypassing the service. The resolver stays a pure, versioned projection over bounded rows and compatibility input, while transactional mutation and operation receipts stay in the service. There is no persisted generated summary.

Every item exposes an enforcement status: `enforced`, `advisory`, `unsupported`, or `conflicted`. Exact Unicode-normalized text dedupe is used for free-form advisories; v1 does not attempt semantic synonym dedupe. Executable meanings always use typed keys, one active value per account/key, and explicit conflicts.

### 6. Test review

```text
NEW UX: add/edit/stop/undo; accept/dismiss suggestion; toggle; inspect applied rules; project override
NEW DATA: event -> outbox -> extraction -> item/operation -> snapshot -> plan/job receipt
NEW BRANCHES: explicit/local/soft/noop; constraint/default/advisory; active/suggested/conflicted/forgotten
ASYNC: outbox dispatch, extraction retry/dead-letter, deletion/flag races
ERRORS: stale CAS, duplicate delivery, invalid schema, model failure, capability conflict, source deletion
```

Unit tests pin normalization, precedence, conflict creation, immutable snapshot versions, exact Undo, sensitive projection, and structured `shadow_enabled` behavior. Integration tests cover event/outbox atomicity, task redelivery, owner-scoped routes, existing writer refactors, account export/delete, flag skew, and each job-mode snapshot mint. E2E proves Project A durable rules affect Project B, are explained, can be overridden once, then stopped for Project C.

The hostile tests send instruction-injection-shaped multilingual text, race activation/Undo/edit, delete the user while extraction runs, flip flags mid-task, and use two contradictory explicit rules. The chaos test holds Redis unavailable after the chat commit, then restores it and verifies exactly one activation and receipt. Prompt/schema changes require prompt-version bumps and live extractor evals; Skia/overlay changes require `make verify-overlays` plus representative local renders.

### 7. Performance review

The prefilter avoids an LLM call for nil, empty, clearly project-local, and ordinary conversational messages without durable markers; only ambiguous soft-pattern candidates enter the bounded extractor path. Apply per-user rate limits, token caps, queue-lag metrics, retry ceilings, and a dead-letter state that never blocks creation. Snapshot load uses indexes on `(user_id, scope_kind, state, normalized_key)` and `(user_id, state, updated_at)` with one batched provenance lookup.

Active item count and total injected characters are capped; the resolver records item count, build latency, and prompt-token contribution. No source thread/event relationship may be lazily traversed per row on the profile page. Rendering adds no model call and consumes the already persisted snapshot.

### 8. Observability and debuggability review

Structured logs carry creator-safe trace/operation/item/revision/job identifiers, never instruction text. Metrics cover extraction outcomes (`activated`, `suggested`, `noop`, `invalid`, `error`), outbox age/dead letters, activation-to-Undo rate, conflict/stale-revision rate, suggestion acceptance/dismissal, snapshot latency/count/tokens, applied receipt count, and renderer conformance failures by job mode.

Admin diagnostics expose the resolver version, memory revision, item ids, conflict ids, flag state, and mode-coverage result. Alerts fire for sustained outbox age/dead letters, invalid-output spikes, and mode-specific conformance failures. The runbook covers queue recovery, flag rollback, sensitive-trace verification, and stuck operation repair.

### 9. Deployment and rollback review

Deployment is additive and mixed-version safe: migrate first; deploy API/workers that tolerate absent fields while flags stay off; verify outbox and disabled responses; enable backend for an internal cohort; then enable the frontend and expand. The stable backend-off/web-on response prevents a broken settings page. Old workers must not receive the new task name until every worker image can consume it.

Rollback hides the web surface first, disables extraction/injection second, and retains rows/snapshots for recovery. Queued tasks re-check account deletion, profile enablement, and backend flag before mutation and become safe no-ops while disabled. Historical queued/in-flight jobs continue from their immutable snapshots.

### 10. Long-term trajectory review

Reversibility is 4/5: flags and additive schema make behavior easy to stop, while creator-authored rows are intentionally durable. The internal `scope_kind`, versioned resolver, operation ledger, and capability-aware conflicts form a safe path to brand/channel scopes without exposing premature onboarding complexity. The largest path-dependency risk is allowing direct `Persona.style` writes to survive; that must be eliminated as part of this feature.

The 12-month ideal is a scoped policy compiler, not an unbounded memory transcript. It resolves account, brand/channel, project, and render overrides into an explainable immutable receipt. Learning proposes direction; the creator retains final authority.

### 11. Design and UX review

```text
Account menu/footer -> Profile -> Memory
                               ├─ loading skeleton
                               ├─ empty examples -> Add instruction
                               ├─ active groups -> Edit / Stop for future / Undo
                               ├─ suggestions -> Why? / Accept / Dismiss
                               └─ errors/conflicts -> Preserve draft / Retry / Resolve

New project confirmation -> “Using N instructions” -> applied list -> one-project override
Chat explicit rule -> “Remembered for future videos” -> Undo
```

Hierarchy is one text-first Personalization document combining creator context and remembered direction. The surface follows the light editorial system, uses existing page shell/typography/buttons, avoids dashboard-like stat cards, and keeps optional explanation quiet. Mobile keeps 44px targets, safe-area composer spacing, and statement-local overflow actions; keyboard focus, live status, reduced motion, and screen-reader labels are release requirements.

The critical trust copy distinguishes active, suggested, advisory, unsupported, and conflicted states. “Applies to all future videos” is visible even before brand scopes exist. The applied-rules receipt is the product’s proof that memory works, not an implementation detail.

### Failure modes registry

| ID | Failure mode | Rescued? | Test | User-visible outcome | Logged safely? |
|---|---|---:|---:|---|---:|
| FM-01 | Outbox dispatch unavailable | Yes | Integration/chaos | Pending then remembered | Yes |
| FM-02 | Duplicate task delivery | Yes | Integration | One item/receipt | Yes |
| FM-03 | Undo races with newer edit | Yes | Route/concurrency | Conflict with review action | Yes |
| FM-04 | Local request misclassified global | Yes | Eval/E2E | Durable-marker gate plus Undo | Yes |
| FM-05 | Invalid/refused model output | Yes | Unit/eval | No memory mutation | Yes |
| FM-06 | Contradictory constraints | Yes | Resolver/E2E | Visible resolution choice | Yes |
| FM-07 | Flag flips mid-task | Yes | Integration | Safe no-op; chat unaffected | Yes |
| FM-08 | Source project deleted | Yes | Model/route | Generic past-project provenance | Yes |
| FM-09 | Text enters agent/admin logs | Yes | Privacy regression | No exposure | Redacted |
| FM-10 | Render mode bypasses snapshot | Yes | Mode matrix | Guard/test blocks rollout | Yes |
| FM-11 | Resolver schema changes | Yes | Snapshot compatibility | Historical job remains reproducible | Yes |
| FM-12 | Advisory appears enforced | Yes | UI/contract | Correct status label | Yes |
| FM-13 | Renderer lacks shadow parity | Yes | Renderer/local render | Feature remains unsupported | Yes |
| FM-14 | Extraction queue overload | Yes | Load/metrics | Creation stays responsive | Yes |
| FM-15 | Account deletion races extraction | Yes | Integration | No recreated/retained memory | Yes |

### NOT in scope

- Multiple brand/channel memory profiles: schema-ready, deferred until account-wide use validates the need.
- Learning from editor clicks or model/media observations: too ambiguous for v1 and contradicts the creator-authored trust boundary.
- Retroactive mutation or re-render of queued/completed outputs after Stop/Forget: historical snapshots stay reproducible.
- Semantic merging of free-form rules: exact normalization only; typed keys own executable conflicts.
- Persistent generated memory summaries: projections are derived and disposable.

### CEO implementation tasks

- [ ] **C1 (P1)** Define typed direction, scope, precedence, enforcement, conflict, snapshot, and Undo contracts before UI work.
- [ ] **C2 (P1)** Route every existing style/preference writer through `CreatorDirectionService` and add a bypass guard.
- [ ] **C3 (P1)** Add additive ledger, operation receipt, and extraction-outbox schema with owner/deletion/index constraints.
- [ ] **C4 (P1)** Implement bounded hybrid extraction, strict validation, idempotent dispatch, retries, and redaction.
- [ ] **C5 (P1)** Implement `CreatorDirectionResolver` and one versioned immutable snapshot mint point per job mode.
- [ ] **C6 (P1)** Prove `shadow_enabled` across schema, API, preview, Pillow, Skia, classic/music/generative/reburn paths.
- [ ] **C7 (P2)** Build the simple Personalization and text-summary surfaces, suggestions, exact Undo, inline conflicts, and subtle applied-preferences line.
- [ ] **C8 (P2)** Add export/delete/privacy/runbook/metrics/alerts and mixed-flag deployment checks.

CEO dual voice status: Luna raised 14 substantive concerns; all compatible safeguards were absorbed. The external Codex CLI voice was unavailable because the approval boundary blocked transmitting private repository context. No cross-model consensus is claimed.

## Design review: one unified Personalization document

The first design was rejected as too structured. A second two-state settings/summary design exposed another problem: “creator profile” and “what Kria remembers” were indistinguishable to the user. The user chose to merge them. The final paper direction is one clean, spacious, text-based Personalization document; it removes tabs, Manage hops, cards, badge-heavy rule rows, category dashboards, and a prominent receipt panel.

### Approved paper wireframe

| State | Artifact | Direction |
|---|---|---|
| Unified Personalization | `/Users/emirerben/.gstack/projects/emirerben-nova/designs/creator-memory-20260906/paper-wireframe.png` | One calm, narrow document combining creator context and remembered video direction |

### What already exists and must be reused

- `/plan/persona` and `PersonaEditor` provide the light shell, creator-profile content, loading/error handling, and settings-mode foundation; their data appears in Personalization without exposing a separate persona concept.
- `StyleAgentInterview` provides quiet inline pending/saved/retry language for memory updates; success is not toast-only.
- `Header`, the workspace account footer, and the mobile account sheet remain the entry points.
- Existing Button/Input/Dropdown/Switch primitives, Fraunces destination headings, Inter body text, zinc dividers, and lime enabled state remain the implementation system. The generic paper controls are visual shorthand only.

### Pass 1: information architecture — 6/10 to 10/10

`/plan/profile` is the only creator-facing destination and is labeled **Personalization**. Account menu, workspace footer, mobile account sheet, and applied-preference links all open it. The page does not mention “persona,” “profile memory,” or separate storage models.

```text
Account menu / workspace footer / mobile account sheet
                       │
                       ▼
              /plan/profile
              Personalization
              ├─ Use personalization in new projects
              ├─ About your videos
              ├─ Visual style
              ├─ Storytelling and tone
              ├─ Things to avoid
              ├─ Suggestion (only when present)
              └─ Tell Kria what to remember
```

One narrow reading measure, whitespace, and dividers replace nested settings navigation. Existing persona and memory sources resolve into the same derived statement list; the UI does not ask the creator to classify information.

### Pass 2: text-summary and item mapping — 4/10 to 10/10

The backend item ledger remains authoritative. `GET /me/memory` returns deterministic section metadata in the order `content`, `video_style`, `stories_pacing`, `avoid`, then `other`, matching **About your videos → Visual style → Storytelling and tone → Things to avoid**; each displayed statement retains `item_id`, display text, enforcement/scope, source status, conflict metadata, and available actions. The client groups statements into prose blocks but never persists or asks a model to generate a summary paragraph.

One visual sentence maps to one item even when adjacent sentences read as a paragraph. A quiet `Edit` and overflow trigger operates on that statement, not the whole category. The item menu is exactly **Edit**, **Stop using for future videos**, **View source** when available, and **Undo** only while eligible; a deleted source renders disabled **Source project deleted**.

### Pass 3: interaction-state coverage — 4/10 to 10/10

| Feature | Default/empty | Pending/success | Error/conflict |
|---|---|---|---|
| Personalization | Existing “About your videos” remains even with no learned preferences | Toggle saves inline | Failed toggle restores value and gives retry |
| Statement document | Empty learned sections are omitted; composer always remains | Updated statement receives focus | Load failure keeps heading/composer and offers retry |
| Composer/edit | “Tell Kria what to remember” with future-video example | Text review → Save → “Personalization updated · Undo” | Draft survives validation, duplicate, stale revision, or network error |
| Chat learning | No UI for local/no-op statements | Checking → remembered rule + Undo | “Couldn’t update memory. Nothing changed.” |
| Suggestion | Hidden when none; otherwise one prose block | Remember moves/focuses active statement | “Not used until you accept it”; safe retry/dismiss |
| Conflict/unsupported | Absent unless relevant | Resolution updates statement | Inline explanation + Review; never status hidden only in `•••` |
| Applied project line | “No saved preferences used” or “Personalization is paused” | “Using your personalization · View” | Expanded details name unsupported/conflicted rules and override action |

### Pass 4: simplicity and trust arc — 8/10 to 10/10

At five seconds, the page communicates what Kria knows, whether it will use it, and how to change it. The document reads like a short human summary, not a policy administration dashboard. Complexity appears only where it is real: a suggestion says it is inactive, a conflict appears next to the affected sentence, and a saved mutation gets a brief exact Undo.

The composer remains the primary interaction: creators can write “Always use Playfair Display,” “Forget that I use shadows,” or “Use a calmer pace.” A compact text-only review asks “Remember this for future videos?” and shows the normalized statement plus **Applies to all future videos · Enforced/Advisory**, followed by Save and Keep editing.

### Pass 5: ChatGPT-reference fidelity without imitation — 8/10 to 10/10

Keep the reference’s useful structure: a single enablement row, prose summary with updated metadata, a small overflow menu, and one bottom correction composer. Kria improves on the reference by merging overlapping profile/memory concepts. Preserve Kria’s own type, color, copy, and existing shell rather than copying ChatGPT chrome. No decorative gradients, icon circles, statistic cards, colored status pills, or chat bubbles are added.

The menu contains only **Pause/Turn on personalization**, **Clear remembered preferences**, and **Learn about personalization**. Clearing removes memory-led preferences but keeps explicit creator background/goals unless the creator edits those statements; the confirmation names that distinction. Primary per-item actions remain beside their statement.

### Pass 6: responsive and accessibility — 4/10 to 10/10

Production uses semantic headings and a list of addressable statements, real Switch/Input/Button/Dropdown primitives, visible labels, 44px targets, and text labels inside menus. Status updates use polite live regions, actionable errors use alerts, and focus returns to the updated statement after save/accept/Undo. Edit/overflow actions remain keyboard reachable without relying on hover or color.

The composer is normal-flow on short content or sticky inside the page scroll region with bottom content padding equal to its height plus `safe-area-inset-bottom`; it is never absolutely positioned over content or the iOS keyboard. At 375×667 and 200% zoom, prose wraps without horizontal scroll and the composer submit has an accessible name.

### Pass 7: decisions locked

- The product has one Personalization page; creator profile and memory are internal sources, never separate user concepts.
- The visual model is headings, prose, whitespace, and dividers. Status is quiet secondary text and appears only when it changes meaning.
- Each sentence maps to a canonical item id; no generated summary blob and no ambiguous paragraph-level mutation.
- Edit/forget/source/eligible Undo stay statement-local; pause/bulk stop/privacy stay in the top menu.
- Suggestions remain plain text and explicitly inactive, with Why/Remember/Dismiss.
- Conflicts remain inline with Review and the already specified resolution choices.
- The bottom **Tell Kria what to remember** composer remains primary but uses a small confirmation step before global mutation.
- Applied project direction is a subtle expandable text line, not a card or badge collection.

### Design implementation tasks

- [ ] **D1 (P1)** Build one `/plan/profile` Personalization document using existing profile and UI primitives.
- [ ] **D2 (P1)** Add deterministic section/item mapping, statement-local edit/stop/source/Undo, simple composer review, and inline suggestion/conflict states.
- [ ] **D3 (P1)** Add the subtle applied-preferences line and project-only override details to confirmation artifacts.
- [ ] **D4 (P2)** Add Personalization navigation to Header, workspace footer, and mobile account sheet with route/focus restoration.
- [ ] **D5 (P2)** Verify 375×667, 200% zoom, long/translatable prose, sticky/keyboard/safe-area behavior, live regions, menus, and reduced motion.

Design re-review status: the user chose one unified Personalization document after identifying the creator-profile/memory overlap. Luna’s statement-level interaction/accessibility safeguards remain absorbed without restoring the rejected dashboard/list feel. Codex remains unavailable at the privacy boundary. The final paper wireframe is the visual reference and the statement-level rules above are the implementation contract.

## Engineering review: executable architecture and delivery plan

Engineering assessment: this is a creator-direction control-plane migration, not a profile-page feature. Current code commits durable `CreationThreadEvent` rows, directly mutates and reads `Persona.style`, carries user-wide `preference_summary`, and snapshots mutable render state across several JSONB paths. Implementation must establish one write boundary and one versioned read projection before exposing the Memory UI.

### Canonical ownership and state transitions

`CreatorDirectionService` is the only module allowed to mutate account direction. Its commands are `activate_explicit`, `create_suggestion`, `accept_suggestion`, `dismiss_suggestion`, `edit`, `stop_using`, `restore`, and `apply_project_override`; all take an authenticated owner, idempotency key, expected account-memory revision, actor/source metadata, and a typed payload. Existing persona-style and workspace-preference routes call this service rather than assigning `Persona.style` directly.

`CreatorDirectionResolver` is pure and side-effect free. It loads one bounded owner-scoped state set, applies the documented precedence and mode capabilities, and returns a versioned snapshot. Existing readers receive a compatibility adapter during migration, but no route/task may rebuild direction ad hoc. A repository guard test searches production route/task code for direct `Persona.style` assignment and for render/planning reads that bypass the resolver.

Item transitions are finite and validated: `suggested -> active|dismissed`; `active -> superseded|forgotten`; `forgotten -> active` only when no newer active key conflicts. An automatic operation may supersede only another automatic unlocked item. User-authored or accepted items are locked; a contradictory automatic proposal becomes a conflict/suggestion until the creator resolves it.

### Additive database contract

- `User`: add `creator_memory_enabled BOOLEAN NOT NULL DEFAULT true` and `creator_memory_revision BIGINT NOT NULL DEFAULT 0 CHECK (creator_memory_revision >= 0)`. Persona reset changes onboarding/persona/style compatibility state but preserves these account controls and all ledger rows.
- `CreatorMemoryItem`: UUID id; owner FK with cascade; `scope_kind` checked to `account` in v1; category; nullable normalized executable key; nullable exact normalized-content hash; bounded instruction; nullable typed JSON value; enforcement; source kind; nullable thread/event FKs with `ON DELETE SET NULL`; confidence; lifecycle state; `user_locked`; timestamps.
- `CreatorMemoryOperation`: UUID id; owner FK with cascade; unique `(user_id, idempotency_key)`; operation kind; nullable item FK; bounded prior/result payload; resulting revision; actor kind; nullable source event; `undo_expires_at`; nullable `undone_at`; timestamp. The opaque operation id is the Undo token; it is owner-scoped, single-use, expires after 10 minutes, and compare-and-sets the recorded resulting revision.
- `CreatorMemoryOutbox`: UUID id; owner FK with cascade; unique source event; bounded source-message payload encrypted/handled like existing creator text; status `pending|leased|succeeded|dead`; attempt count; `available_at`; nullable `lease_until`; safe last error code; extractor/payload version; timestamps. Success distinguishes `noop` from mutation in a result code.
- `ProjectDirectionOverride`: UUID id; owner and project/thread FKs with cascade; normalized key; override kind `different_default|allow_constraint_once`; typed value; lifecycle/revision/timestamps; unique active key per project. Changing a global rule continues through the account Memory service rather than this table.

PostgreSQL owns correctness with partial unique indexes: one active executable `(user_id, scope_kind, normalized_key)`, one active/suggested exact advisory content hash, and one outbox row per source event. Add profile indexes on `(user_id, state, updated_at DESC)` and resolver lookup on `(user_id, scope_kind, state, normalized_key)`. Migration tests run against PostgreSQL because SQLite cannot prove partial-index behavior.

### Transactional extraction and worker contract

Every eligible creator message appends its user event and outbox row in the same transaction. The eligibility helper is shared across normal and early-return branches: only substantive creator-authored project messages qualify; duplicate client event ids, control/status actions, assistant/system events, empty messages, and explicitly local-only text do not create another outbox row. The outbox persists the bounded source payload so project/event deletion cannot make a leased task dereference missing text.

A light-queue periodic claimer leases due rows with `SKIP LOCKED`, dispatches a versioned extraction task, and reclaims expired leases. The task has bounded tokens/time, exponential retries with jitter, a fixed attempt ceiling, and a dead-letter result; task limits remain below the broker visibility timeout. It rechecks the global flag, profile toggle, user existence/deletion state, source ownership, payload version, and item revision before mutation. Disabled/paused/deleted becomes a successful safe no-op rather than retry churn.

The deterministic prefilter handles explicit durable markers and obvious local/no-op text; the model is reserved for ambiguous classification and soft suggestions. Extractor output cannot choose user, scope, source, or arbitrary keys. Validation performs Unicode normalization, control/role-marker rejection, length/value bounds, allowlisted typed keys, and exact-text dedupe. Prompt and output tracing uses a sensitive projection so raw instructions never enter logs, `AgentRun.input_json`, public job JSON, or admin detail payloads.

### Immutable snapshot and integration matrix

`CreatorDirectionSnapshotV1` contains `snapshot_id`, `resolver_version`, `memory_revision`, applied item ids, resolved typed values, bounded hard/preference/context prompt sections, enforcement/capability results, conflict ids, project-override values, compatibility-input version, and creation time. Readable raw instruction text remains in the owner ledger/export; public/debug projections receive statuses and ids only.

Persist the snapshot in the owning creation/job record through one `mint_creator_direction_snapshot()` helper. Treat it as immutable once a generation is queued. A user-initiated project override or explicit “refresh with current memory” creates a new generation/snapshot; ordinary retry and fast reburn reuse the generation’s original snapshot.

| Mode | Mint boundary | Persistence | Retry/re-render rule |
|---|---|---|---|
| Creator Agent/chat | initial session/turn that creates project output | creator session/plan private state | later planning regeneration mints a new snapshot only when explicitly requested |
| Content plan | plan generation/regeneration dispatch | plan generation record/private snapshot | retry reuses; explicit regenerate may mint current memory |
| Generative job | initial dispatch before enqueue | private `all_candidates`/job snapshot reference | worker and normal retry reuse original |
| Classic template | template-job creation before enqueue | private job snapshot reference | orchestration and retry reuse original |
| Music/auto-music | music-job creation before enqueue | private job snapshot reference | orchestration and retry reuse original |
| Editor reburn/retime/text edit | new render-generation boundary | variant generation/private snapshot reference | pure reburn reuses; project-direction change mints a new generation |
| Legacy job without snapshot | compatibility adapter at first safe read | stamped once before mutation/enqueue | never silently use live current memory after stamping |

Every row receives a focused guard test. Capability evaluation marks an item `enforced`, `advisory`, `unsupported`, or `conflicted`; receipts never claim deterministic enforcement for prompt-only advice. `shadow_enabled` remains unavailable in the UI until backend schemas, frontend types, preview, Pillow, Skia, classic, music, generative, reburn, and overlay-verification tests all pass.

### Exact API behavior

- Responses use bounded collections rather than pagination: maximum 100 live items and 20 suggestions per owner; history stays out of `GET /me/memory` and is available only in the bounded account export. Instruction text is capped at 500 Unicode scalar values and total injected text at 4,000 characters after resolution.
- Every mutation accepts `Idempotency-Key` plus `expected_revision`. A replay returns the original operation and resulting representation; a reused key with different input returns `409 idempotency_mismatch`.
- Stable errors are `409 stale_revision`, `409 memory_conflict`, `409 undo_no_longer_applicable`, `410 undo_expired`, `422 unsupported_key`, `422 unsupported_value`, `422 instruction_too_long`, `429 memory_limit_reached`, and owner-safe `404 memory_item_not_found`. Each includes `code`, plain `message`, affected field/item when safe, current revision where useful, and a concrete recovery action.
- Item edit replaces the complete normalized instruction/typed value after the server review step; it is not a JSON merge. Accept locks the suggestion and atomically resolves or returns the current conflicting item. Stop/restore are idempotent under their operation key.
- `POST /creation-threads/{thread_id}/direction-overrides` creates/updates an owner-scoped project override; `DELETE` removes it. The response includes the newly minted effective project receipt without changing account memory.
- When the backend flag is off, reads return `200` with `feature_available=false`, saved rows only to the owner, and no applied state; mutations return stable `503 creator_memory_disabled` with retry guidance. Backend authority wins over a stale enabled frontend.

### Privacy, export, deletion, and operability

Account export adds current readable memory plus a capped recent operation history and documents that older operation history is omitted while current rules remain complete. Account deletion explicitly removes/locks outbox leases before memory/persona/user deletion, then cascades items/operations/outbox/overrides; workers check user existence immediately before commit and cannot recreate an owner. Project deletion cascades project overrides and nulls memory source FKs, leaving only source kind and timestamp.

Metrics contain no instruction content: extraction outcomes, oldest pending age, dead letters, retry attempts, activation-to-Undo, stale revisions, conflicts, suggestion acceptance/dismissal, snapshot latency/item/token count, mode/capability conformance, and flag/profile states. A safe trace chain connects event id, outbox id, operation id, item revision, snapshot id, job id, and generation id. The runbook covers lease repair, dead-letter replay, flag rollback, privacy verification, and mode-conformance failures.

### Engineering failure registry additions

| ID | Failure | Required rescue |
|---|---|---|
| FM-16 | Eligible event commits without outbox in an early branch | Shared transaction helper and branch-exclusion tests |
| FM-17 | Mutation commits but response is lost | Idempotency key returns original operation/receipt |
| FM-18 | Two active values race for one key | Partial unique index, locked profile revision, deterministic 409 |
| FM-19 | Suggestion acceptance races with new active item | Atomic lock/transition or stable conflict response |
| FM-20 | Mixed workers receive incompatible task payload | Versioned payload and safe unknown-version dead letter/no-op policy |
| FM-21 | Re-render reads live memory | Generation snapshot test at every entry point |
| FM-22 | Existing style route bypasses ledger | Service adapter plus repository guard test |
| FM-23 | Project override exists only in UI state | Owner/project table, route, and snapshot integration |
| FM-24 | Export grows without bound | Complete current state plus capped operation history |
| FM-25 | Account deletion races extraction | Lease invalidation, FK cascade, pre-commit owner check |
| FM-26 | SQLite hides partial-index error | PostgreSQL migration/integration test |
| FM-27 | Raw text leaks through agent tracing | Sensitive-projection regression test on every trace sink |
| FM-28 | Resolver silently drops unsupported value | Persist/result status and visible receipt |
| FM-29 | Account toggle and global flag disagree | Global backend flag authoritative; task checks both |
| FM-30 | Source event disappears before extraction | Bounded outbox payload; source link remains optional |

### Dependency-complete implementation waves

1. **Contract and schema:** freeze enums, limits, errors, state machine, snapshot/payload versions, idempotency and Undo semantics; add additive migration, models, indexes, and PostgreSQL tests.
2. **Direction core:** implement service, normalization, resolver, conflict/capability projection, and operation receipts; refactor direct persona/workspace writers and readers behind adapters; add bypass guards.
3. **Reliable learning:** insert outbox with eligible events; add claimer/task routing, prefilter, extractor/evals, retries/dead letters, flag/deletion checks, and sensitive tracing.
4. **Snapshot integration:** wire Creator Agent, content plans, generative, template, music, legacy, retry, and editor-generation paths; add project overrides and mode matrix tests.
5. **Privacy and operations:** complete export/delete/privacy policy, metrics/alerts/runbook, mixed-version and rollback tests. This wave must finish before any production cohort is enabled.
6. **User surface:** build the unified Personalization document and subtle receipts against stable contracts; add navigation, exact states, accessibility/mobile tests, and Project A/B/C browser E2E.
7. **Visual enforcement gate:** finish `shadow_enabled` parity, `make verify-overlays`, representative local renders, frontend lint/typecheck/tests, backend tests/lint, and `scripts/preship-check.sh`; deploy migration/code with flags off, then backend cohort, then frontend.

Engineering dual voice status: Luna identified 15 additional failure modes and concrete contract gaps; all are resolved above. The external Codex voice remained unavailable because private repository context could not cross the approval boundary. No cross-model consensus is claimed and no engineering decisions remain open.

## Developer-experience review: implement, verify, and operate without guesswork

DX classification: internal API/service and asynchronous control plane, reviewed in DX POLISH mode. The primary developer is a Nova engineer fluent in FastAPI, Next.js, Celery, PostgreSQL, and FFmpeg but new to creator-direction semantics. Their first success must be provider-free: activate one rule, resolve an immutable snapshot, inspect the applied receipt, replay the outbox idempotently, and Undo the exact operation in one local session.

### Developer perspective

I open the plan and understand the product, but the first implementation path crosses four new tables, several creation-message branches, two flags, the Next proxy, asynchronous extraction, and every render mode. Without one smoke test or a key registry, I have to infer where a new setting belongs and can easily add a route that the browser cannot call. The current `/api/me/[...path]` proxy exports GET/POST/DELETE but not PATCH, and its 5xx safety boundary would flatten the planned `creator_memory_disabled` response. If extraction dead-letters, metrics tell me something failed but there is no safe inspect/retry command. If I attach account controls to `Persona`, `/personas/reset` deletes them. The polished path gives me one architecture page, one provider-free smoke command with expected assertions, typed client/errors, safe outbox repair through `scripts/admin.py`, and an extension checklist. I can prove the control plane before touching an LLM, browser, or production credential, then use existing dev-login only for optional end-to-end verification.

### Nine-stage developer journey

| Stage | Developer does | Before | Planned path/status |
|---|---|---|---|
| 1. Discover | Opens plan and pipeline docs | Architecture is spread across plan/code | `docs/pipelines/creator-memory.md` links model, service, resolver, queue, snapshots, and render matrix |
| 2. Set up | Runs existing local stack | Generic setup works but feature fixtures are unclear | Existing `scripts/dev-auto.sh`; no new dependency or provider key for focused tests |
| 3. Migrate | Applies additive schema | Partial indexes and mixed versions are easy to miss | Runbook gives upgrade/check/rollback commands and PostgreSQL-only assertions |
| 4. Hello world | Runs one deterministic flow | No named test or expected result | One backend smoke test proves activate → resolve → receipt → replay → Undo in under five minutes |
| 5. Integrate UI | Calls owner APIs through Next | PATCH absent; 5xx code flattened | `/api/me/memory`, PATCH export, typed `memory-api.ts`, safe disabled-code passthrough |
| 6. Add a key | Extends a visual/content rule | Allowlist ownership is ambiguous | One registry/checklist covers schema, resolver, planner, UI, capabilities, renderers, and tests |
| 7. Debug async | Investigates pending/dead extraction | Metrics/logs only; SQL temptation | Safe admin health/retry endpoints and `scripts/admin.py` commands expose ids/status/codes only |
| 8. Deploy/upgrade | Ships migration, workers, flags, web | Sequence described but not executable | Exact mixed-worker, backend-first enablement, frontend-first rollback, and flag-skew checks |
| 9. Measure/extend | Reviews internal cohort | Runtime metrics omit developer friction | Track smoke duration, mode conformance, stale-error recovery, and post-cohort DX review |

### Time to first verified memory flow

Current plan-only TTHW is estimated above 10–15 minutes because the developer must discover proxy, queue, snapshot, and test boundaries. Target is under five minutes from an already set-up worktree and under three minutes of focused test runtime:

```bash
cd src/apps/api
pytest tests/services/test_creator_direction_smoke.py::test_activate_resolve_and_undo -q

cd ../web
npm test -- --runInBand src/__tests__/plan/CreatorMemoryPage.test.tsx
```

The backend fixture is deterministic and provider-free. Its assertion names/report must make five facts obvious: one active item, one V1 snapshot, the matching applied item id, duplicate delivery creating no second row, and exact Undo succeeding. Optional browser verification uses the documented `ALLOW_DEV_LOGIN=true` flow and is not part of hello world.

### Pass 1: getting started — 4/10 to 9/10

The existing repo has a strong one-command local stack, but this feature has no golden path through its many boundaries. Add the named smoke test, deterministic fixtures, expected assertions, and time budget to the runbook; do not require Gemini/OpenAI, Redis timing, or a browser for the first proof. A score of 9 acknowledges normal repository setup remains a prerequisite while the feature-specific path is one copy-paste command.

### Pass 2: API/service interface — 7/10 to 9/10

Owner-scoped nouns and explicit mutation routes are consistent, and idempotency plus expected revision put callers in the pit of success. Finish the interface by exporting PATCH from `src/apps/web/src/app/api/me/[...path]/route.ts`, adding a typed `src/apps/web/src/lib/memory-api.ts`, and freezing complete request/response models rather than ad hoc dictionaries. The simple client methods must be production-safe while project override and recovery complexity remain explicit methods.

### Pass 3: errors and debugging — 5/10 to 9/10

Every API error uses one `MemoryApiError` envelope: nested backend `detail` with `code`, human-safe `message`, `retryable`, `request_id`, `recovery_action`, and only the safe contextual fields required by that code. The Next proxy must explicitly pass through the sanitized `creator_memory_disabled` 503 code/message and preserve the generic boundary for every other unapproved 5xx. Three pinned paths are stale edit → retain draft/reload current rule; stale frontend flag → stable paused UI; dead letter → safe admin inspect/retry without SQL or instruction text.

Example stale response:

```json
{
  "detail": {
    "code": "stale_revision",
    "message": "This memory changed in another tab.",
    "retryable": false,
    "request_id": "request-id",
    "current_revision": 12,
    "expected_revision": 11,
    "item_id": "memory-item-id",
    "recovery_action": "reload_memory"
  }
}
```

### Pass 4: documentation and learning — 4/10 to 9/10

Ship `docs/pipelines/creator-memory.md` for invariants/data flow/snapshot modes, `docs/runbooks/creator-memory.md` for local proof/queue repair/rollout/rollback/privacy checks, and a concise API/key-extension section in the pipeline doc. Every command includes expected output and uses current repository tooling such as `scripts/admin.py`; docs land in the same change as the feature. Link the plan and relevant existing creator/generative/template pipeline docs rather than duplicating their internals.

### Pass 5: upgrade and migration — 7/10 to 9/10

The additive/flagged rollout is credible, but developers need exact order and mixed-version checks. The runbook freezes: migrate; deploy tolerant API/workers with flags off; verify schema/outbox/smoke; ensure all workers know the payload; enable backend internal cohort; enable web; then expand. Rollback hides web first and disables backend extraction/injection second, leaving rows and historical snapshots intact; downgrade never drops creator data during emergency rollback.

### Pass 6: developer environment and tooling — 6/10 to 9/10

Reuse pytest, Jest, dev-login, hot reload, `make verify-overlays`, local-render, ruff, TypeScript, and preship rather than adding a harness. Feature fixtures isolate time/model/queue behavior and expose deterministic service/task entry points; PostgreSQL integration remains mandatory for partial-index tests. Focused commands precede the full gates so an engineer gets feedback in seconds and then proves mode/render parity before merge.

### Pass 7: ecosystem and extension boundary — 5/10 to 8/10

This is an internal product, so community/install/pricing dimensions are not applicable; extensibility is the real ecosystem question. Create one typed-key registry that owns normalized key, value schema, prompt mapping, capability/mode status, UI label/editor, and sensitive projection. Its contribution checklist requires planner, preview, Pillow, Skia, classic, music, generative, reburn, receipt, and verification coverage where applicable, preventing a future key from becoming prompt-only by accident.

### Pass 8: measurement and feedback — 5/10 to 9/10

Runtime metrics already cover queue and resolver health; add developer measures for smoke-test duration, migration success, snapshot mode-conformance, proxy error-code conformance, and stale-revision recovery. After the first internal cohort, run the documented smoke flow from a clean worktree and a live `/devex-review`, recording actual TTHW and any repair requiring raw SQL or private-text inspection as a failure. This closes the loop between the planned under-five-minute path and operational reality.

### Safe operator interface

Add `GET /admin/creator-memory/health` and `POST /admin/creator-memory/outbox/{id}/retry`. Health returns aggregate counts, oldest age, payload versions, and safe error codes; retry returns ids/status only, is idempotent, rechecks flags/owner existence, and never returns source payload or instruction text.

```bash
python scripts/admin.py GET /admin/creator-memory/health
python scripts/admin.py POST admin/creator-memory/outbox/<id>/retry --yes
```

### DX scorecard and checklist

| Dimension | Before | After |
|---|---:|---:|
| Getting started | 4 | 9 |
| API/service interface | 7 | 9 |
| Errors/debugging | 5 | 9 |
| Documentation/learning | 4 | 9 |
| Upgrade/migration | 7 | 9 |
| Dev environment/tooling | 6 | 9 |
| Extension boundary | 5 | 8 |
| Measurement/feedback | 5 | 9 |

Overall: 5.4/10 to 8.9/10. TTHW: >10–15 minutes to <5 minutes from a configured worktree.

- [ ] **DX1 (P1)** Move memory toggle/revision to `User` and pin persona-reset survival.
- [ ] **DX2 (P1)** Add `/api/me` PATCH, typed memory client, exact models, and safe disabled-code passthrough.
- [ ] **DX3 (P1)** Add provider-free backend smoke and focused frontend contract test with documented expected assertions.
- [ ] **DX4 (P1)** Add pipeline/runbook/key-registry documentation with exact local, recovery, migration, rollout, and rollback commands.
- [ ] **DX5 (P1)** Add safe admin health/dead-letter retry operations through `scripts/admin.py`.
- [ ] **DX6 (P2)** Add developer conformance measures and repeat the smoke/live DX audit after the internal cohort.

### DX dual-voice status

| Dimension | Luna | Codex | Result |
|---|---|---|---|
| Getting started under five minutes | Gap found and fixed | Unavailable | Single-voice finding retained |
| API naming/proxy completeness | Gap found and fixed | Unavailable | Single-voice finding retained |
| Actionable errors | Gap found and fixed | Unavailable | Single-voice finding retained |
| Findable, complete docs | Gap found and fixed | Unavailable | Single-voice finding retained |
| Safe upgrade path | Gap found and fixed | Unavailable | Single-voice finding retained |
| Friction-free dev environment | Gap found and fixed | Unavailable | Single-voice finding retained |

Codex could not run because the repository privacy boundary blocked transmitting private context. Luna found ten concrete DX contract gaps; all were verified or absorbed. There are no DX taste disagreements or scope expansions: the fixes make the selected system implementable and operable.

## Cross-phase synthesis

Five concerns repeated independently across product, design, engineering, and DX and therefore act as release-level invariants:

1. **One authority:** every legacy and new preference writer/reader routes through the direction service/resolver; no second truth hides in `Persona.style`, feedback summaries, or UI state.
2. **Trust is visible:** active vs suggested, enforced vs advisory/unsupported/conflicted, source/scope, exact Undo, and applied project receipts are user-facing product behavior.
3. **Every output path counts:** classic, music, generative, Creator Agent, content plans, legacy, retry, and reburn use explicit immutable snapshot boundaries; generative-only support does not satisfy the feature.
4. **Private and reliable by construction:** creator text is untrusted and sensitive; transactional outbox, idempotency, redacted traces, owner-scoped routes, deletion races, and safe operator recovery are inseparable from “constant learning.”
5. **Account lifetime differs from persona lifetime:** memory control/revision belongs on `User`; onboarding/persona reset and project deletion must not erase account direction.

### Review voice matrices

| CEO dimension | Luna | Codex | Final disposition |
|---|---|---|---|
| Product promise/control framing | Concern | Unavailable | Reframed as explainable creator direction |
| Learning authority | Concern | Unavailable | Hybrid premise confirmed by user |
| Account vs brand scope | Concern | Unavailable | Account v1 confirmed; schema future-safe |
| Canonical source of truth | Critical | Unavailable | One service/resolver, legacy adapters and bypass guard |
| Reliability/Undo | Critical | Unavailable | Outbox plus exact 10-minute operation receipt |
| Privacy/render coverage | Critical | Unavailable | Redaction and all-mode snapshot gates |

| Design dimension | Luna | Codex | Final disposition |
|---|---|---|---|
| Information hierarchy | User-corrected | Unavailable | One Personalization document; no creator-profile/memory distinction |
| State coverage | Gap fixed | Unavailable | Empty, review, pending, error, conflict, Undo without dashboard chrome |
| Journey/trust | Gap fixed | Unavailable | Chat receipt and subtle applied-preferences line |
| Specificity/AI slop | User correction | Unavailable | Headings, prose, whitespace, dividers; no tabs/Manage hops/cards/badge grid |
| Design-system reuse | Gap fixed | Unavailable | Existing shell, editor, primitives, and Kria typography |
| Responsive/accessibility | Gap fixed | Unavailable | Semantic statements, live regions, focus, safe-area composer |
| Ambiguous interaction choices | Gap fixed | Unavailable | Statement actions, top menu, conflict, suggestion, scope semantics |

| Engineering dimension | Luna | Codex | Final disposition |
|---|---|---|---|
| Schema and ownership | Critical | Unavailable | Checked additive tables, partial indexes, User revision |
| Transaction/queue boundary | Critical | Unavailable | Event+outbox atomicity, leases, retries, dead letters |
| API concurrency/idempotency | Gap | Unavailable | Idempotency key plus revision CAS and stable errors |
| Snapshot/re-render coverage | Critical | Unavailable | Per-generation V1 snapshot and mode matrix |
| Privacy/deletion/export | Critical | Unavailable | Sensitive projections and lifecycle race tests |
| Deployment/operability | Gap | Unavailable | Mixed-version sequence, safe flags, metrics/runbook |

Codex was probed for each review phase but could not receive private repository context through the approval boundary. All matrices are therefore transparent single-Luna review records, not cross-model consensus claims.

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | CEO | Explicit durable language activates; soft inference only suggests | User decision | Authority/trust | Delivers “say it once” without silent soft inference | All-manual; all-automatic |
| 2 | CEO | Account-wide identity in v1 with future-safe scope field | User decision | Simplicity/reversibility | Matches current product while preserving brand/channel evolution | Brand scopes now; unscoped schema |
| 3 | CEO | Model feature as creator direction, not a transcript summary | Auto-decided | User outcome | Rules and enforcement matter more than prose recollection | Generated summary as truth |
| 4 | CEO/Eng | One write service and one pure resolver | Auto-decided | Single source of truth | Prevents receipts/snapshots disagreeing with legacy style state | Parallel authorities |
| 5 | Eng/DX | Put enablement/revision on `User`, not resettable `Persona` | Auto-decided | Data lifetime correctness | `/personas/reset` deletes Persona but account memory must survive | Preserve hidden Persona fields through reset |
| 6 | CEO/Eng | Commit eligible user event and extraction outbox atomically | Auto-decided | Reliability | A committed durable statement cannot be silently lost on queue failure | Best-effort `.delay()` after commit |
| 7 | CEO/Design | Undo is exact, single-use, CAS-protected, and valid for 10 minutes | Auto-decided | Predictability | Reverses the operation without overwriting newer edits | Generic restore; indefinite Undo |
| 8 | CEO/Eng | Persist project defaults and one-time exemptions as typed overrides | Auto-decided | Scope clarity | A project exception cannot live only in browser state | Implicit prompt text override |
| 9 | CEO/Eng | Mint versioned immutable direction per generation | Auto-decided | Reproducibility | Queued/retried renders must not drift when memory changes | Live memory at worker/reburn time |
| 10 | CEO/Design | Label every result enforced, advisory, unsupported, or conflicted | Auto-decided | Honest feedback | Prompt guidance must not masquerade as deterministic render enforcement | Single “active” label |
| 11 | CEO | Do not backfill AI-derived style into user memory | Auto-decided | Consent | Existing derived evidence lacks creator-authored authority | Automatic migration/backfill |
| 12 | Design | Merge creator profile and memory into one `/plan/profile` Personalization document | User decision | Conceptual simplicity | Both concepts tell Kria how to make the creator’s videos; storage boundaries stay internal | Separate profile/memory tabs or Manage destination |
| 13 | Design | Derive addressable prose statements from persona compatibility plus memory items | Auto-decided | Simplicity with control | Natural text stays editable without a generated-summary source of truth | Badge-heavy ledger; persisted AI summary |
| 14 | Design | Free-form updates require compact scope/enforcement review; suggestions never apply until accepted | Auto-decided | Explicit action | One text field remains simple without creating hidden global state | Raw immediate mutation; automatic suggestion activation |
| 15 | CEO/Eng | Raw memory text stays out of AgentRun, admin/job/public projections | Auto-decided | Privacy | Private direction remains owner-readable without entering operational surfaces | Log everything for debugging |
| 16 | Eng | Use bounded live-item collections and complete-current/capped-history export | Auto-decided | Bounded cost | Keeps profile/export predictable without losing current truth | Unbounded operation export |
| 17 | Eng/DX | Expose safe admin health and idempotent dead-letter retry | Auto-decided | Repairability | Operators should not need SQL or private payload access | Metrics-only recovery |
| 18 | DX | Provide a provider-free activate→resolve→replay→Undo smoke flow | Auto-decided | Fast feedback | Proves the architecture in under five minutes | LLM/browser-dependent first test |
| 19 | Eng/DX | Centralize typed keys and capability coverage in one registry/checklist | Auto-decided | Extension safety | New visual rules must cover UI, planner, modes, and renderers coherently | Distributed allowlists |
| 20 | CEO/Eng | Backend-first enablement and frontend-first rollback; retain additive data | Auto-decided | Reversibility | Prevents stale web/worker breakage and avoids destructive emergency rollback | Simultaneous flag flip/schema rollback |

No deferred item is required for v1 correctness. Future brand/channel profiles, editor-click learning, semantic free-form dedupe, retroactive re-renders, and persistent generated summaries remain deliberately outside this implementation and are recorded under NOT in scope rather than as hidden follow-up work.
