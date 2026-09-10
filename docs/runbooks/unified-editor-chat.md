# Unified creation and editor chat (KRI-22)

The `/plan` conversation is the only creator AI interface. A direct
`/plan/items/:id/edit?variant=...` link resolves its owning conversation through
`POST /creation-threads/for-editor`. Real embedded editor frames keep the existing
route. Old items receive an owner-checked association; a second request reuses it.

## Execution and persistence

The workspace sends typed `kria:editor-chat:v1` commands to its own editor frame.
Both ends check the same origin, window identity and connection. Commands bind to
an editor mount, item, variant, generation and draft revision. The live editor
builds the existing copilot snapshot, including unsaved changes and current
selection. It validates and applies through the same atomic operation applier and
history used by manual editing. A stale or disconnected editor never falls back
to a creation rerender. Retransmission of a command does not repeat execution.

Previewable operations stage a dirty, undoable draft. Save/export remains the
render boundary. Server-only layout/effect changes and reviewed speech changes
require confirmation in the main conversation. Layout/effect endpoints check the
confirmed job and baseline under the job lock. Generated assets retain the
existing cost confirmation, cancellation and insertion checks.

`POST /creation-threads/:id/editor-events` appends bounded browser-acknowledged
conversation messages. It accepts both creation runtimes, validates the owning
item/variant and deduplicates event IDs. It cannot approve, render, or write an
editor draft. History persistence retries never replay an editor command.
Runtime-v2's server draft executor also accepts draft-only tool groups; a render
approval is created only for a group that explicitly requests rendering.

## Timeline contract

- **Duration:** the existing virtual timeline and editor apply context own ordered
  entries, overlaps and output duration. The bridge does no timeline arithmetic.
- **Ripple:** existing operation appliers retain stable base-time lanes and their
  current insertion/retiming policies, including continuous music exclusion.
- **Scrubbing:** the editor's existing virtual-preview duration and inverse
  output-to-base mapping continue to clamp all playback and seeking.
- **Resizing:** existing operation validators own handle semantics, limits and
  rounding. Chat does not introduce alternative timing rules.
- **Undo:** one validated operation bundle records one existing editor history
  snapshot. The main-chat undo action is bound to that exact history revision.
- **Parity:** the existing atomic-applier, virtual-timeline and editor-commit
  fixtures remain the timing/render contract. The shared-chat tests additionally
  assert that local operations never dispatch a render before Save/export.

## Verification

Run the focused editor-chat, copilot/director and creation-workspace Jest tests;
the creation editor-conversation API tests; `make verify-kria`; and
`make verify-editor-timeline`. The real-Postgres runtime test covers both a
completed draft-only turn and an explicitly approved render. Browser verification
covers a manual change followed by consecutive chat edits, undo, explicit Save,
responsive Chat/Editor tabs and direct links. Hook tests cover confirmation/decline
and review/suggestion execution.

Deploy the API before the web build. Existing clients can omit the new optional
server-action generation preconditions. Keep existing editor capability flags;
this change does not require enabling runtime v2 or changing render flags.

### Acceptance matrix

| Capability or boundary | Regression evidence |
| --- | --- |
| Text content, style, position; captions; clip timing/order; music; SFX; overlays; transitions; motion; carousel | `src/apps/web/src/__tests__/edit-copilot/apply-ops.test.ts` and the remaining `edit-copilot/` suites exercise the existing atomic applier, unchanged by transport. |
| Questions, clarification, unsupported/no-effect responses, disabled capabilities and invalid mixed bundles | Copilot applier and hook suites reject partial/unsupported mutations. |
| Server layout/effect acknowledgement; reviewed speech; generated assets/cost confirmation | `useEditCopilot.test.tsx`, `useEditDirector.test.tsx`, `EditorShell-render-turn-reply.test.tsx`, bridge confirmation tests and the generation precondition API tests. |
| Manual edit → AI edit → second AI edit → undo → Save | `e2e/editor-chat.spec.ts` uses the real workspace and EditorShell with deterministic HTTP responses; verifies snapshots, no pre-Save render calls, and exactly one pinned Save. |
| Direct links and mobile Chat/Editor tabs | The second `editor-chat.spec.ts` case preserves item, variant and unsaved draft. |
| Duplicate delivery, project/variant/generation/revision changes, iframe reload | `src/__tests__/editor-chat/{bridge,workspace-bridge}.test.tsx`. |
| Receipt deduplication, offline retry, reload recovery and thread isolation | `src/__tests__/editor-chat/conversation.test.tsx` and `tests/routes/test_editor_conversation.py`. |
| Visual suggestion parity | The main chat uses the same matcher hook and undo-recorded acceptance callback. The editor's `poolOnly` presentation retains uploads without AI actions. |

Browser fixtures replace model and render HTTP responses; they do not claim a
paid live-model or production-render evaluation. No prompts changed in this work.
