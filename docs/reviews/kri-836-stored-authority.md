# KRI-836: preserve newer stored slide instructions

Original PR #1751 at9eedf8975c4caa85ec22fe53b419e1314f7dcdc0 and fresh main12d4e766231f8326e96eb6bf7336a956c43b0167 are retained by normal merges. The author's checkout was clean and its remote unchanged before this isolated correction. No phone, source media, production data, paid/model call or deployment changed.

## Failure boundary and correction

KRI-812 repairs saving after an asset is added/deleted. Its anchoring check did not run when a submitted binding matched the same asset set. Consequently an older device could Save or start another chat edit with an older request and discard newer already saved instructions. A re-digested same-owner/thread/item binding could also change the request, pinned brief or frozen answers. This was an existing same-owner consistency defect, not a cross-user issue.

The stored-authority selection now runs for both unchanged and changed media, through both PUT and chat-edit. An identical binding preserves its exact digest. A request extension with the same approval keeps earlier instructions; an older ancestor keeps the newer stored request. Divergent requests, pinned briefs and frozen answers are rejected specifically. Media re-snapshotting still requires a real add/delete and owned ready rows; replacing bytes under the same asset ID is still rejected. Existing expected-version conflicts, legacy omitted bindings and flag-off behavior are retained.

The failure list was recorded in .dev/stored-authority-failure-modes.md before tests/code and in F23–F25 of the real-route test module before changing production code. No original test function or assertion was removed.

## Executed evidence

Before the source correction, ten new real PostgreSQL route cases produced eight assertion failures and two valid-extension passes: both Save/chat lost newer stored instructions, and all six divergent request/brief/answers cases were accepted instead of rejected. No model/provider was called.

After the correction:

- All19 cases in tests/routes/test_slide_post_binding_save_postgres.py passed, including all nine original deletion/addition/reorder/ownership/source-byte/409/legacy/flag controls.
- The combined slide native routes, chat service, framing, sizing and free recorded copilot evals passed:418 cases. Command:

```sh
REDIS_URL=redis://127.0.0.1:16389/0 NOVA_EVAL_MODE=replay bash scripts/check-api.sh test tests/routes/test_slide_post_binding_save_postgres.py tests/services/test_slide_post_chat_edit.py tests/routes/test_slide_post_native_routes.py tests/pipeline/test_slide_post_framing.py tests/pipeline/test_slide_post_sizes.py tests/evals/test_edit_copilot_evals.py -q
```

The stale-device cases first persist newer instructions through real chat-edit and PUT, then send the older binding with the same assets. PUT still rejects the stale version with409, accepts the explicit rebase and preserves the newer digest. Chat preserves the newer request before appending the next message. Tests read saved bindings back from the database and assert request/brief/answers, rather than only accepting HTTP200.

Logs: /private/tmp/nova-kri836-same-assets-red.log, /private/tmp/nova-kri836-binding-green.log and /private/tmp/nova-kri836-slide-neighbors.log. Scratch database: nova_kri_836_stored_authority_test. One initial neighbor command named nonexistent module paths and collected no tests; the corrected existing-module command above is the418-case result. Ruff checks and formatting pass.

## Claim limits

This proves the local server authority correction and synthetic route recovery. The original five-slide horse incident has not been replayed with its retained real media here. The existing KRI-812 report's generate assertions record dispatch of saved slide identities; they do not supply a rendered image/video artifact. Exact original four-slide Save/reopen/export acceptance and actual export fidelity remain KRI-812. No full request fulfillment, physical TestFlight or rollout claim is made. Current source-head hosted checks and preship remain required before merge.
