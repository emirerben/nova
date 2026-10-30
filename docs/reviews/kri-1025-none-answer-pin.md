# KRI-1025: "None of these" on the clip picker no longer dead-ends the chat

Parent incident: KRI-1023 (Plaza de Mayo kit, M1 "maximo 3", 2026-10-28).

## Original request and what happened

Prompt (thread `A228E85B`, plan item `08b8b60a`, Fly v1904 = `dde04eb74`, TestFlight 1784,
protocol 23): "Montage of our afternoon at Plaza de Mayo, in the order we filmed it. … End on
the Obelisco. About 20 seconds. …" The Obelisco was never filmed (the kit's trap).

1. Turn 1: Kria asked with the clip picker "I couldn't tell which of your clips show "the
   Obelisco". Tap the clips that do, or tell me there aren't any." (`order:the-obelisco`,
   intent `order_last_obelisco`). Correct.
2. Turn 2: the creator answered None (`clip_selection.none_keys: ["order:the-obelisco"]`).
   Kria replied `recovery`, no draft, no question, no next action: "I couldn't tell which
   clip "the Obelisco" means, so I can't confirm the order."
3. Only the typed "There is no obelisco in the clips. End on the cathedral" moved on.

## Root cause (traced)

- `fold_clip_selections` records the None; `apply_creator_selections` drops
  `order_last_obelisco` from the clip intents ("empty-by-creator, never asked again").
- Brief r1 keeps `facts: {key: capture_time, last_clip: "the Obelisco"}`: a picker tap does
  not rewrite the brief, and nothing else recorded the answer.
- The draft backstop (`kria_runtime._unresolved_choice_plan`) builds the render contract;
  `stated_anchors(brief)` still names a last clip that no sequence row seats, so
  `_pin_not_found("the Obelisco")` becomes `contract.unresolved[0]` and the reply.
- Same class: creator text whose described clip was answered None. A per-clip shot text
  hits `text_clip_not_found`; a global one stays pinned as exact text on an edit that prints
  it nowhere (refused after the render).

## Acceptance criteria

- A None (or skip) for a first/last pin gives a draft, not a refusal. The draft says plainly
  that no clip shows it, what the video starts/ends on instead, and how to change it.
- `_pin_not_found` / `text_clip_not_found` never fire for a pin or text the creator answered
  None to. The review row never claims "ending on the Obelisco".
- Approval and dispatch read the same decision (no refusal after approval).
- Only the exact words answered None are voided: a typed new ending, a newer tap, another
  pin already placed there, or a different landmark keeps today's behaviour.
- A pin the creator never answered still names the clip, now with a way forward.
- EN and TR copy. KRI-697 (answer identity) and KRI-694 (answer language) unchanged.

## Fix

- `app/services/clip_none_answers.py`: reads the thread's picker answers (same events and
  rows as `fold_clip_selections`). A brief anchor whose newest answer for the same words is
  None/skip, with no other resolved pin on that spot, becomes a server-owned `choice_answer`
  (`pin_absent`). A brief text whose label/caption row was answered None, and that nothing
  else prints, becomes `text_absent`. Each comes with a notice in the chat's language.
- `choice_questions.answered_brief` applies them to the pinned brief copy: the anchor fact
  is removed and the description re-worded ("in the order you filmed them"); a pin-only order
  or a left-out text is superseded. Order-independent and idempotent.
- `planner._gate_unresolved_choices` records them next to the other choice answers, adds
  the notices to the summary and the policy notices, and words them after the creator's
  other answers (upload order → "the last clip you added").
- `_unresolved_choice_plan`: a contract line that names a clip now carries its next step.

## Case matrix (all offline, `tests/kria/test_kri1025_none_answer_pin.py`)

| Case | Covered by |
| --- | --- |
| Incident: last pin + filming order, recorded events/brief/strategy | F1 |
| First pin | F2 |
| Pin-only order ("End on X", no basis) | F3 |
| Two pins, one answered None | F4 |
| Typed new ending; ending not yet placed | F5, F5b |
| Spot already placed by another pin | F6 |
| Newer tap for the same words | F7 |
| Wording drift vs a different landmark | F8 |
| Label text, per-clip and global scope | F9 |
| Turkish chat | F11 |
| No answer at all: byte-identical | F13 |
| Never-answered pin: named with a next step | F14 |
| Skipped picker | F15 |
| Idempotent pinned copy | F16 |
| No filming times + "use the order you added" | F17 |

Negative control: F8 ("the Obelisk", "the Cabildo") and F5 (brief now says "the cathedral")
use the same None answer and must NOT void anything; a word-matching bug would fail them.

## Not covered / remaining gaps

- No iPhone re-send yet: after deploy, re-send M1 and answer None on the Obelisco row.
  Expected: a draft whose summary says "None of your clips show "the Obelisco", so the video
  ends on the last clip you filmed. Tell me which clip to end on if you'd like a different
  one." The other M1 findings (KRI-1024, 1026–1032) are separate tickets under KRI-1023.
- Group / then-rows ("start with X, then Y") answered None are already dropped without a
  contract refusal; their receipt does not mention the missing group. Not changed here.
- When the brief's anchor words and the picker row's words differ beyond case, articles
  and punctuation, nothing is voided and the old named refusal (now with a next step) stays.
