# KRI-1235: render promises held the phone Talking draft (failure modes and expectations)

Parent incident: KRI-1234 (Qendresa Q1 "Talking + screenshot cards", TestFlight 1910, Fly
v2122, 2026-11-06 20:49Z, thread `b931bc4e-cfe8-466c-ae78-fbe492d635ad`). The failure modes
were listed in the test's docstring before the fix; prod rows are from read-only admin GETs.

## What prod did

Her first message (11 requirements, brief v1) got "I can't make this yet:" with
"Make it without these / Change my request". Approval readiness (event seq 7,
`brief_coverage.approval_readiness`) said:

| Row | Ask | Disposition | Reason |
| --- | --- | --- | --- |
| r1 | Cut out all my pauses and mistakes so it's tight | needs_choice | If Clean up speech is offered when you approve, choose it and every pause is cut down to about 0.3 s; Clean up speech also cuts a sentence you restarted if it finds one, and I'll tell you after |
| r2 | Captions word by word, middle, white, two words | executable | (none) |
| r3 | Big opening title | executable | (none) |
| r4 | Matching screenshot / recording, not over my face | unsupported | This draft has no pop-ins timed to your words. |
| r5-r9 | "1. Spec-kit" ... "5. Claude Code Game Studios" | needs_choice | I'll show it the moment you say that word in your take; if I don't hear the word, it won't appear |
| r10 | End with "comment REPO" and an arrow | needs_choice | I'll draw a curly arrow next to your ending text |
| r11 | Only my voice, no music | executable | No music, as asked. |

Turn 2 ("Make it without these") held again on r10 alone. Turn 3 rendered without r1, r4-r10.
Every `needs_choice` reason above is a plan the phone Talking render carries out, not a
shortfall. Readiness approved only the five older speech-cleanup sentences
(`_cleanup_deferred`).

## Expected after the fix

- r1, r5-r10: approvable (`unverified`, named for the creator to check after the render).
- r2, r3, r11: unchanged (`executable`).
- r4: the only row that may hold (KRI-1237 owns it).
- The hold reply, and the "Make it without these" leaves-out list (both read the same rows),
  name no promise.
- The same in a Turkish turn (receipts carry the Turkish twin).

## Ways the fix can fail

1. A reason a checker emits as a render promise is missing from the list (the prod bug).
2. Prefix or substring matching lets a shortfall through ("<promise>; ... so it may still be
   in your video", "<promise> and more").
3. A refusal that shares words with a promise is approved: only the long pauses cut, cleanup
   unavailable, a restart that stays in, a named line that may stay, an arrow the creator must
   draw, a text that may appear at someone else's voice, no word to wait for.
4. A `not_possible` receipt worded like a promise is approved.
5. A Turkish turn stores the Turkish twin, so an English-only list re-blocks Turkish creators.
6. Promises that contain "; " themselves (spoken text, list labels) break a naive split.
7. A pinned spoken-text promise has a dynamic tail; a fixed list cannot match it, and a prefix
   match would approve a different word or time.
8. The hold reply or the leaves-out list still names promises.
9. A joined cleanup reason longer than the receipt's 300-character cap is cut and re-blocks.
10. Repeated readiness assessments disagree. HTTP approval is separate evidence, not proved
    by calling the same assessment twice.
11. A future feature adds a new draft plan reason and silently re-blocks its own draft.

## Not covered

- Quoted start / end line plans (`_TALKING_WINDOW_PLAN`, KRI-790) and the KRI-724 source cut and
  noise filter plans still hold on the readiness path; each is listed with its reason in the
  guard test's `NOT_RENDER_PROMISES`.
- The receipts screen's "Partly done" ticks before anything is made.
- No live model call, device run or render: offline replay only.

## Local review correction: bounded display is not promise identity

Acceptance recorded before implementation: the real checker/receipt/readiness chain must
approve an EN/TR pinned promise whose human-readable reason exceeds 300 characters, while
still rejecting a different trigger suffix, a different selected time, the wrong requirement,
a joined shortfall hidden beyond that display limit, and a `not_possible` receipt. The public
receipt stays at most 300 characters and its schema does not gain approval evidence.

The 207-character mission trigger from review produces a 305-character English promise.
Longer trigger pairs can share all 300 visible characters, hiding both their differing suffixes
and their selected times. Comparing truncated expectations or accepting a prefix is unsafe.
Reason composition must preserve the complete explanation, not a stale promise marker.

Evidence scope: real offline receipt construction and readiness assessment. Repeated `_assess`
calls establish deterministic assessment only; they do not exercise the HTTP approval recheck.
HTTP approval, native export, and live Q1 request fulfillment remain unverified under KRI-1235
and parent KRI-1234. No paid call, real device, publication, merge, or deployment is included.

### Local verification of the review correction

The correction retains complete localized reason evidence in a Pydantic private attribute.
The public reason remains capped, and serialization/schema omit that private evidence.
Current receipt composition/replacement paths preserve the full reason and refresh the
projection together. Readiness requires the matching requirement ID and exact complete text;
it does not compare a truncated expected sentence or accept a promise prefix.

- Final regression file on original PR head `4fdc15d153bad11e41bfb271d129f300b4c16693`:
  **6 failed, 50 passed**. The valid 207/290-character triggers reach the actual receipt
  boundary. This red control used an archived original tree, not another owner's checkout.
- `bash scripts/check-api.sh test tests/kria/test_kri1235_render_promises_approvable.py -q`:
  **56 passed** after correction.
- The focused file plus the neighboring editor, Talking, draft deferral, pause/restart,
  unclear-word, speaker opening, ending and list-label suites: **550 passed**.
- `DATABASE_URL="$(cat .dev/test-db-url)" python3 scripts/reliability-gates.py run --changed`:
  **863 passed, zero skips/errors**, using this worktree's own migrated test database.
  Existing HTTP approval tests are included; no new long-trigger HTTP scenario is claimed.
- Scoped Ruff lint/format, `git diff --check`, and the instruction-size guard pass.

No main integration or dependency/native fix is included. Production, native output,
paid-provider behavior, and the exact long-trigger HTTP journey remain unverified.
