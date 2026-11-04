# Item 23: a long creation prompt (~17 asks) is planned in one go

Related: KRI-1129. Flag: `BRIEF_LONG_REQUEST_ENABLED` (default off). Prompts: Main Creator
`2026-11-05-v131`, brief extractor `2026-11-05-v66` (both change only with the flag on).

## The request

The Q2 test-kit prompt (a screen walkthrough of five GitHub repos: opening title, logo, screen
recordings full screen, the speaker cut out small in a corner, readable zoom, the right recording
per repo, five numbered labels, word-by-word captions in a grey box, tight pauses, a closing
"comment REPO" with an arrow, only her voice) states about 17 separate asks in one first message.
Expected outcome: every ask lands in the Creative Brief from that one message, in one planning
call, with nothing merged away and no "couldn't plan" reply.

## Why the cap was 16 (git history)

| Change | What it did | Reason recorded |
|---|---|---|
| KRI-188 (#1221, 2026-09-24) | `MAX_UPDATES_PER_TURN = 8`, extra entries silently truncated | bound one planner response |
| KRI-422 (#1380, 2026-10-04) | 8 -> 16; prompt teaches "at most 16 objects" | two titles + six shot texts filled 8 and a duration ask was cut; 16 is ~2x the largest real batch (9) |
| KRI-459 (#1409, 2026-10-06) | truncate -> refuse the whole batch; the 40-requirement ledger cap removed | "This bounds a single planner response, not the ledger. A larger model batch is rejected as a whole so the caller can recover without losing its tail." |

So the cap is a guard against a runaway batch from one model response, sized from observed demand.

- **Not a token budget.** Each entry may carry a 200-char literal, a 300-char description and
  2,000 bytes of facts, so 16 entries could already be ~40k chars. Truncated output has its own
  handling (KRI-542 retries a truncated Gemini 3 call at low thinking).
- **Model reliability** enters through the prompt. The model is told the number. On a 17-ask
  prompt it either merges asks to fit (a silent loss) or writes 17 and is refused.

What happened to this prompt before the change, on a first turn:
1. `MainCreatorAgent.parse` refused the batch ("brief_updates exceeds 16 entries").
2. The agent retried twice with that feedback: 3 paid calls in all.
3. The turn ended in request recovery (`creator_planning_failed`), and none of the 17 asks were
   kept.

## The choice

With the flag on:

- **A batch of 16 or fewer** is accepted exactly as today, whatever its size. Flag on never
  refuses what flag off accepts.
- **A batch of 17 to 32** is accepted only if its own compact JSON totals 9,000 chars or less
  (`MAX_BRIEF_REQUEST_CHARS`, the size of one planning batch of the brief context).
- **Above 32 entries, or above the budget,** the batch is refused whole, as today. The refusal
  tells the retry to "shorten descriptions and facts, keep every ask".
- **Both prompts** (Main Creator and brief extractor) say "at most 32 objects" instead of 16.
  Nothing else in either prompt changes.

The new worst case (9,000 chars) is below today's 16-entry worst case, so the runaway guard is
tighter in bytes while looser in count. Her 17 asks are 2,753 chars.

**Not chosen: "accept the batch in two ordered halves."** There are two ways to do it, and neither
helps:

- **Split one response on the server.** This is the same as a larger cap, with no added
  protection.
- **Make two model calls.** This doubles the paid calls, and the plan is chosen before the model
  sees every ask. Two halves can also disagree; the planner already refuses disagreeing batches
  with `planning_batches_disagree`.

## Ways it could fail (written before the code), and what pins each

| # | Failure | Pinned by |
|---|---|---|
| 1 | Flag on but the prompt still says 16, so the model keeps merging asks | `test_flag_on_changes_only_the_cap_in_the_main_creator_prompt`, `test_brief_extractor_prompt_flag_off_is_byte_identical_and_on_changes_only_the_cap`, replay "prompt cap 32" |
| 2 | Flag off changes a byte (prompt, retry feedback, recovery turn) | `test_flag_off_prompt_sections_are_byte_identical`, `test_flag_off_refuses_17_entries_with_todays_message`, `test_flag_off_her_prompt_recovers_exactly_as_recorded` (golden recorded on 37f470c7f) |
| 3 | A higher count removes the runaway guard | `test_flag_on_refuses_33_entries_whole`, `test_flag_on_refuses_17_entries_over_the_character_budget_whole`, `test_flag_on_budget_boundary_is_exactly_9000_characters` |
| 4 | The budget refuses a batch of 16 or fewer that is accepted today | `test_flag_on_never_refuses_what_flag_off_accepts` |
| 5 | Partial acceptance (the tail dropped) | the refusal tests above (all or nothing) |
| 6 | Asks collapsed or reordered by planner dedupe or refiling | `test_flag_on_her_prompt_plans_all_17_asks_in_one_call` (17 entries, in order, by kind/scope/literal/description) |
| 7 | Downstream budgets (`CREATOR_REQUEST_MAX_CHARS` 12k, `brief_context` 3 x 9k) | the same test checks the effective request size (2,581 chars) |
| 8 | An oversized answer is retried blind and drops asks | `test_flag_on_oversized_first_answer_is_retried_and_keeps_all_17_asks` (2 calls, the retry prompt carries the instruction, 17/17 kept) |
| 9 | Follow-up turns (brief extractor) still squeezed to 16 | extractor prompt test + `test_flag_on_brief_extractor_accepts_17_entries` |
| 10 | Output tokens | Main Creator 16,384 including thinking; the extra is at most ~2.5k tokens. Extractor 4,096: a truncated call is retried at low thinking (KRI-542). Not pinned by a test. |
| 11 | The flag is flipped mid-turn | worst case is today's behaviour (the prompt says 16 and the parse allows 32, or the reverse). Not pinned. |

## Before / after (offline replay, no model called)

`make kria-replay FIXTURE=q2-long-prompt` runs the real `plan_live_turn` and the real
`MainCreatorAgent`. Only the model client is replaced, by a cassette that answers with an
authored 17-entry Main Creator response for the prompt
(`src/apps/api/tests/fixtures/kria_replay/q2-long-prompt/`):

```
Q2 long prompt: 17 separate asks in one first message
flag off: 3 model call(s), recovery (creator_planning_failed), asks kept 0/17, prompt cap 16
  flag off equals golden: yes
flag on: 1 model call(s), act/action, asks kept 17/17, prompt cap 32
RESULT: PASS
```

The answer is authored, not recorded from a live model. It shows that the server keeps a
17-entry answer. It does not show what the live model writes for this prompt.

## Not verified here

One live Main Creator turn would confirm it (v131, flag on, ~$0.10-0.50; not run, no paid calls
in this change):
- **Input:** the exact Q2 prompt, with a manifest of one talking take, five screen recordings
  and one PNG logo.
- **Check:** count the `brief_updates` it files (expect 17-19, none merged) and confirm the turn
  is not a recovery.
