# Leverkusen M1 run 2 #3: "Make it without these" kept upload order

Parent incident: KRI-1066 (Leverkusen M1 montage). This finding is unfiled (Linear free-plan
cap) and is referred to as "Leverkusen M1 run 2 #3".

## Incident

Thread 1a2189cc (2026-10-30, Fly eed794157, TestFlight 1819, protocol 25), phone Montage of
8 iPhone videos and 3 selected Visuals photos, every one dated. The request included "Put
everything in the order I filmed it, starting with the train ride" (brief r1, key
`capture_time`). Kria could not confirm that order (Leverkusen M1 run 2 #2) or the length
(#1), offered "Make it without these" (leaves out r1 and r5), and the creator took it.
Job 93f662a7 rendered `ordering_basis: "attachment"`:

IMG_1036, IMG_1027, IMG_0995 (photo), IMG_1037, IMG_1026, IMG_1019 (photo), IMG_1043,
IMG_1028, IMG_1038 (photo), **IMG_1002 (train ride, 10th of 11)**, IMG_1039.

The approval card said nothing about order.

## Expected order (answer key)

From the kit's answer key (capture times of the originals, local time) and the server's
stored capture times (UTC, one hour earlier), independent of any planner output:

| # | Item | Filmed (UTC) |
|---|---|---|
| 1 | IMG_0995 photo, train display | 17:34:31 |
| 2 | IMG_1002 video, train ride | 17:42:36 |
| 3 | IMG_1019 photo, view from the box | 18:52:22 |
| 4 | IMG_1026 video, warm-up | 19:16:50 |
| 5 | IMG_1027 video, crowd | 19:33:14 |
| 6 | IMG_1028 video, light show | 19:36:41 |
| 7 | IMG_1036 video, away fans | 19:49:47 |
| 8 | IMG_1037 video, goal 1 | 20:25:46 |
| 9 | IMG_1038 photo, line-up sheet | 20:33:20 |
| 10 | IMG_1039 video, goal 2 | 21:35:40 |
| 11 | IMG_1043 video, players thank the fans | 21:47:37 |

## Root cause

The simpler version supersedes every left-out requirement
(`kria_runtime` simpler-version branch, `BriefUpdate(operation="remove")`, KRI-698 #1639).
That was written so an approved simpler edit stops demanding what it left out (a dropped
title must not fail the render contract). The montage planner reads order only from LIVE
requirements (`unified_montage.brief_view`), so with r1 superseded `_order` returned
`list(clips), "attachment"`. The dated photos also lost their time seat (KRI-1069 seats
them only under an order ask). "Continue without a fixed order" (`OPT_UNORDERED`)
supersedes the ask the same way and had the same effect.

Measured offline with the worker's own `_plan_phone_unified_montage` on the approved job's
pinned brief, media snapshot, length plan and contract: as pinned, the plan reproduces the
shipped order exactly; the same inputs with r1 live give the order above.

## Fix and acceptance

`BriefView.kept_filming_order`: no order ask is live and the last order basis the brief
superseded was filming time. `plan_unified_montage` then plans exactly as under the ask
(dated photos at their own time), unless a hand reorder exists. Nothing promises it: no
receipt, no contract order (`order_required` stays false). `speech_decides_order` treats
that order as chosen, so a reply can't promise a spoken order the render doesn't play.

Acceptance (offline replay, `tests/kria/test_leverkusen_m1_simpler_keeps_filming_order.py`):

- The approved job, re-planned by the worker, plays the 11 items in the order above, and
  the phone recipe draws that order.
- No receipt for r1; the pinned contract carries no order.
- Unchanged: a hand reorder, a live upload-order ask, a dropped upload-order ask, a brief
  that never asked for filming order, undated footage (all keep upload order).
- Wrong-content control: the shipped upload order fails the expected order.

## Reach and remaining gaps

- Affected before the fix: any phone or cloud unified montage whose only order ask
  (filming order) was dropped by the simpler version or "Continue without a fixed order",
  with or without photos (clips only: the train ride played 7th of 8).
- Saved kit runs: only this run dropped an order ask; other simpler versions dropped text.
- Not changed: phone Talking cutaways and voiceover / narrated montages order from the
  pinned render contract, which carries no order once the ask is dropped, so they still
  use upload order there.
- Not changed: the simpler-version copy still says "Left out as agreed: Put everything in
  the order I filmed it" (copy decision: KRI-1056).
- Not verified: a native iPhone export of the fixed plan, and a re-send of the M1 chat
  after deploy.
