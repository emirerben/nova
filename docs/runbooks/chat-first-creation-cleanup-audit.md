# Chat-first creation cleanup audit

This audit records compatibility code that should remain during the chat-first
rollout and candidates for a later removal pass. It intentionally does not delete
legacy code: persisted plans, editor links, and deploy-skew rollback need those
contracts to stay available.

## Keep for compatibility

- `src/apps/web/src/app/plan/items/[id]/page.tsx` still owns the canonical item
  release desk and must remain reachable from Gallery and the embedded editor.
- `src/apps/web/src/app/plan/items/[id]/components/SetupPicker.tsx` contains
  shared media metadata and existing item compatibility, even though the new
  `/plan` entry surface exposes only the three Paper formats.
- The API's plan-item, generative-job, and creator-agent routes remain the
  authoritative render/editor contracts. Creation threads should call their
  controller/service rather than maintain a second render implementation.
- `/plan/new`, `/create`, `/create/manual`, `/library`, and `/generative` should
  remain redirect stubs until the chat-first client has shipped through the
  rollback window.

## Candidates after the rollback window

These are safe candidates for a separately reviewed cleanup change once metrics
show that the kill switch is no longer needed:

1. The former `/plan/new` chooser and its dedicated tests were removed in this
   rollout. Keep the unconditional redirect until old links have aged out.
2. The `/create` and `/create/manual` UI implementations and their
   `NEXT_PUBLIC_CREATION_HUB_ENABLED` / `NEXT_PUBLIC_MANUAL_EDITOR_ENABLED`
   gates were removed in this rollout. Keep their redirect stubs, plus backend
   upload/manual-draft contracts, until persisted data and old bookmarks no
   longer require compatibility.
3. Remove `WorkspaceHome`'s legacy create block and old `/plan` loading path once
   the chat-first flag has a measured rollback-free period. Keep Gallery's job
   tile primitives, which are reused by the chat workspace.
4. Re-audit `Header` route classification after the chat workspace is the only
   `/plan` implementation; its hidden-header exception should remain scoped to
   the canonical chat workspace rather than every plan-item route.
5. Remove the old interview-only copy from design documentation only where it
   describes creation; onboarding and editor copilot are intentionally separate
   surfaces with different interaction contracts.

## Flags to track

The removed `NEXT_PUBLIC_CREATION_HUB_ENABLED` and
`NEXT_PUBLIC_MANUAL_EDITOR_ENABLED` flags have no live code references; the
compatibility routes are unconditional redirects. The creation entry flag is
`NEXT_PUBLIC_CHAT_FIRST_CREATION_ENABLED`; its API counterpart is
`CREATION_THREADS_ENABLED`. Do not remove the new pair until the legacy
rollback path has been retired in a separately reviewed change.
