# KRI-153: native editor save reliability

## Evidence

The connected iPhone's bounded diagnostics recorded repeated HTTP 422 failures
on September 22, 2026. The likely affected edit retained its original published
recipe. The original request body was not retained, so the exact text change
that triggered the incident cannot be established.

An offline replay of the edit accepted an ordinary title change with the current
guided revision. Highlight backgrounds and explicit animation phases reproduced
the rejection: `validate_phone_pilot_recipe` rejected those fields unconditionally,
even when the account's verified feature list included `authoredText`. The native
editor exposes these controls and the native renderer implements them.

## Corrections

- Require verified `authoredText` for these fields, including when a caller has
  mutated a recipe without recalculating its required capabilities. Other pilot
  restrictions and layout safety limits remain enforced. Rejected saves leave
  the persisted recipe unchanged.
- Decode editor-commit 422 errors into safe, actionable messages and record only
  bounded status/code diagnostics. Keep the local edits available for correction.
- Retain acknowledged sections until rendering succeeds so a failed cloud render
  can be resubmitted. Device `needs_attention` uses the existing server retry
  endpoint, including when a local MP4 already exists.
- Treat authoritative server failure/publication as stronger than an old local
  observer. Fence delayed retry completions against newer requests.
- Expose `published_generation` only for a published device record. Device upload
  completion uses an upload-attempt ID, whereas editor commit returns a different
  generation. The editor accepts that publication only when both the published
  generation and expected device request identity match. Preserve follow-up edits
  while advancing their document, clean baseline, and undo/redo generations.

## Regression coverage

`test_phone_editor_commit.py` exercises Highlight, every exposed entrance/exit/loop
phase, and animation-speed endpoints in legacy and guided-v2 modes, plus rejected
save atomicity. `test_device_render.py` and route tests pin the published-generation
contract and its absence after retry. Native session tests cover stale generations,
publication identity, follow-up saves, failed-render resubmission, local-output
retry, observer precedence, and stale retry completion. Error-decoding tests cover
known codes, malformed details, and raw-message suppression.

This change does not enable any new rollout flag. Backend deployment fixes the
authored-text rejection for already-qualified accounts; publication/retry/error
improvements also require the updated iOS app. Deploy the API before distributing
that app. Older clients ignore the optional status field.
