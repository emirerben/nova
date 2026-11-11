# KRI-1321: Approve cancelled for builds older than split screens

Parent incident: KRI-1320 (Qendresa Q2 run). Prod thread `9db44dc2`, approvals `fe2d3639`
(2026-11-08 13:59Z) and `8ecd0c6b` (14:08Z), TestFlight 1926 = protocol 45.

## Acceptance

- An unchanged draft made for a protocol 45 build approves (HTTP 200). See
  `docs/reviews/kri-721-approval-readiness.md` for the readiness contract.
- A protocol 46 build still approves, and is the only build offered split screens.
- The draft turn (worker) and the approve re-check (request) agree on every capability for the
  same build.

## Root cause

`resolve_creator_manifest` offered `split_screen` through `split_screen_supported()`, which checks
the protocol only inside an HTTP request. The worker drafted with the capability, the request
re-checked without it for protocol 45, `capabilities_digest` differed, and
`_cancel_pending_approval` fired. The gate now reads the sending build's protocol
(`split_screen_planned_for`), like `background_focus_planned_for`.

## Evidence (offline replay, real Postgres, real HTTP approve; provider transport replayed)

Command, tested at the PR head:

```
cd src/apps/api && DATABASE_URL=postgresql://<user>@localhost:5432/<scratch>_test \
  .venv/bin/python -m pytest tests/kria/test_approval_readiness_postgres.py \
  -k split_screen_gate
```

- With the fix: protocols 45 and 46 both pass (HTTP 200, status `approved`).
- Fix stashed (red control): protocol 45 fails with the prod symptom, HTTP 409
  `approval_requirements_not_ready`; protocol 46 passes.
- Neighbours: `test_approval_readiness_postgres.py`, `test_split_screen.py`,
  `test_speaker_frame_override.py`, `test_creator_capabilities.py`,
  `test_background_focus_gates.py` (471 passed). Reliability gates mapped to the changed paths:
  1176 passed.

## Gaps

- Approvals already cancelled on thread `9db44dc2` stay cancelled; the creator asks Kria to
  prepare the edit again.
- No device or production check yet. Deploy order and the live re-check are in the PR.
- No cross-context parity test for every other capability gate. The other `*_client_supported`
  gates were not audited for the same worker-versus-request split.
