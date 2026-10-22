# KRI-807 current-main integration

Original source: `a85d9de13c9863acf0d98145e3c99f641ef581db` (#1742).
Fresh fetched main: `36031e054c1ffd2d6a3fdf3f5e492a4979154124`.
The original commits, product code and test assertions are preserved. Only an
append-only reliability-ledger conflict was resolved, retaining all current-main
IDs and adding KRI-807 once. This PR is independent of the pending Talking/prompt
stack; no unmerged sibling was imported here.

## Acceptance and review

An unresolved exclusion must offer the existing clip picker when there are at
least two clips and tappable candidates. Resolver suspects are suggestions only:
no footage is removed until the creator answers. Protected first/last/placed
clips stay protected. A prior accepted exclusion stays accepted, clean confident
matches do not gain a question, and a confirmed answer must reach the next turn.
One-clip, feature-off and planner-dropped exclusions retain the honest notice.

The existing resolver/card boundary is reused. Suspects remain transient and
cannot become persisted resolution facts by themselves. The source tests drive
the real parser, planner, resolver and picker builder while substituting external
model transport; vision calls are explicitly forbidden by the fixture. No prompt,
model, protocol, frontend layout or extra provider call is added.

## Executed verification

`bash scripts/check-api.sh test tests/kria/test_kri807_unmatched_exclusion_picker.py tests/kria/test_kri761_look_alike_exclusions.py tests/kria/test_kri667_clip_exclusions.py -q --tb=short`: **93 passed** on the integrated tree with isolated migrated
Postgres `nova_pr_1742_integration_test`.

These include unmatched/contested combinations, suggested versus actually
excluded clips, protected clips, answer/none-of-these recovery, carry-forward,
flag-off behavior, Turkish copy, and persistence of the creator's actual choice.
Source legacy notice assertions explicitly exercise the picker-off path; this
integration did not modify them.

Committed preship and exact-head hosted CI are required before merge. This is a
bounded recovery-choice fix, not proof that blur is detected correctly or the
complete Don Julio request produces the correct phone video. The original full
request/export remains KRI-802. No paid call, phone operation, rollout setting,
manual deployment or release was performed.
