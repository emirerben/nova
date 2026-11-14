# KRI-1392: the model chooses edit or rebuild

## Request and acceptance

A creator with a rendered video who asks for a change gets the change made in place. A full
re-plan (new strategy, approval and render) happens only when a model reads the message as a
request for a new version, or when the creator accepts our offer of one. No word list decides
it, so the rule is the same in every language.

Accepted when:

- "Make the font different", "make the text faster", "make the video shorter" and "revert"
  stay in-place edits on every path, in English and Turkish, and German or Spanish asks follow
  the same rule.
- "Start over", "redo the whole thing" and "make me a new version" reach the re-planner, in one
  turn where a model reads them and after one offer where none can.
- A draft the editor cannot open never becomes a re-plan, whatever the wording (KRI-237).
- Half of a compound ask is never applied when the requirement extraction failed (KRI-524).

## Where the cues decided before

| Path | Decided by | Effect of the cue |
| --- | --- | --- |
| Brief flag off, editor target opens | The copilot | None. The copilot already read every turn. It can only edit, ask or refuse, and its refusal offered `reply "redo"`. That reply went back to the copilot, which refused again. |
| Editor target cannot be opened (guarded miss) | `_fast_path_eligible` | A cue word re-planned. Any other ask got the recovery reply. |
| Brief flag on, extract first | The extractor's `request_scope` | None on this path. |
| Brief flag on, extraction failed | `_fast_path_eligible`, `_is_text_edit_ask`, `_is_single_ask` | Only a short, single, text-worded ask could be served by the copilot. |
| Failed render, correction | `_fast_path_eligible`, `_is_text_edit_ask` | A cue word skipped the correction and re-planned. A text word chose a different correction rule. |
| Brief flag on, no extraction verdict | `wants_full_replan` in `route_requirements` | Reached only where the route is forced to `replan` anyway (an open wording question, a clip-picker answer). |

## Failure modes, written before the code

| Id | Failure mode | Where it is checked |
| --- | --- | --- |
| F1 | A rebuild ask is read as an edit once its cue is gone | Live extractor check below; brief off the copilot refuses it and offers the rebuild (east_run `t2-recreate`, recorded) |
| F2 | "Start over" or "redo the whole thing" on a rendered video goes nowhere | `test_replying_with_the_offered_word_replans_without_the_copilot`, the `offer` path of the replay |
| F3 | A mixed ask (edit plus rebuild) applies the edit and drops the rebuild | Extractor reads `mixed` as `rebuild`; brief off the copilot lists the rebuild part unmet |
| F4 | A long or multi-sentence message gets half applied when extraction failed | `_sentence_count` guard, `test_compound_request_does_not_report_editor_success_when_extraction_fails` |
| F5 | A German or Spanish creator gets neither path | `de-*` and `es-*` rows of the replay and the extractor check; `nochmal` accepted |
| F6 | Brief flag off and on disagree | Both run in the replay and the tests |
| F7 | The extraction-failure path serves something unsafe | Serves only in-place text ops, one sentence, nothing unmet |
| F8 | A failed-render correction with a cue word re-plans, or a rebuild verdict gets the "can't correct" refusal | Two new cases in `test_failed_draft_correction_postgres.py` |
| F9 | "redo" after "undo that" is taken as a rebuild | The offer word counts only right after our offer (`test_the_word_without_an_offer_is_the_copilots_to_read`) |
| F10 | An unopenable draft re-plans, or loops without a way out | `test_an_unopenable_draft_never_replans_and_offers_the_way_out` |

## The rule now

| Path | Who decides |
| --- | --- |
| Brief flag on, extract first | The extractor's `request_scope` (`edit`, `rebuild`, `clarify`). Unchanged. |
| Brief flag off, editor target opens | The copilot: ops, a question, or a refusal that carries the fresh-version offer. |
| The previous reply offered a fresh version and the creator replies with its word | The creator. `_accepts_redo_offer` matches the whole message against the word our own offer quotes, in English, Turkish or German, and only when the latest reply carried that offer. It re-plans and skips the copilot and the extractor. |
| Editor target cannot be opened | Never a re-plan. Render in flight, stale editor state and unknown render outcome keep their replies. Every other miss, and the failed-correction refusal, now ends with the fresh-version offer. |
| Extraction failed | The copilot's own result. It is served when the message is one sentence, every op is an in-place text op, and the copilot names no unmet part. |
| Failed render | A correction when the render left a variant or an editor draft (KRI-1103 rule, now for every ask). The extractor's `rebuild` verdict re-plans instead of the refusal. |

## Deleted, and what stayed

Deleted from `app/kria/planner.py`: `_TEXT_EDIT_ASK`, `_TEXT_EDIT_ASK_TR`, `_REPLAN_CUES`,
`_REPLAN_CUES_TR`, `_COMPOUND_CUES`, `_FAST_PATH_MAX_CHARS`, `_is_text_edit_ask`,
`_is_single_ask`, `_fast_path_eligible`, `_editor_state_stale`. Deleted from
`app/kria/brief.py`: `_REDO_PATTERNS`, `_TR_REDO_PATTERNS`, `_TR_REDO_AGAIN`,
`_TR_ELEMENT_STEMS`, `_NEGATED_AGAIN`, `_asks_turkish_redo`, `wants_full_replan`.

KRI-667 ratchet (`tests/kria/test_kri667_no_new_intent_regexes.py`, `re.*` calls):

| Module | Before | After |
| --- | ---: | ---: |
| `kria/planner.py` | 12 | 6 |
| `kria/brief.py` | 27 | 17 |
| `agents/edit_copilot.py` | 26 | 26 |

Membership baselines are unchanged (planner 1).

Kept, with the recorded reason:

- **One sentence, on the extraction-failure path only.** Recorded East Run turn `t1-brief`
  (`tests/fixtures/request_following/threads/east_run.json`) got five title and label ops from
  the copilot with no unmet part named, while the per-clip labels and the time to read them were
  not done. The copilot's unmet list misses parts of multi-sentence briefs, so serving it would
  apply half of a compound ask (KRI-524). The guard counts end punctuation and line breaks. It
  is no word list and reads the same in every language.
- **`_asks_move` and `_asks_remove`.** They do not choose edit or rebuild. They decide whether an
  `order` or `select` requirement on an edit turn is a positional move the editor can do or a
  semantic selection the planner must make. No recording shows the extractor writing positional
  facts for "remove clip 4", so removing them would send explicit moves to the re-planner. The
  parity map row D16 now names only these, with a new EN/TR case (D54).

`test_compose_unmet_flag_off_is_byte_identical` no longer pins `_fast_path_eligible("make the
font different") is False`. That pin was the bug.

## Evidence

### Routing replay

`scripts/kria/score_kri1392_routing.py` drives the real `plan_live_turn` with an in-memory
database and stub models. The copilot answers with each turn's recorded output, parsed by the
real `EditCopilotAgent`. The corpus is every recorded request-following turn (98) plus 26
authored messages in EN, TR, DE and ES. The same command ran on the base (`b77926af3`, a
detached checkout) and on this branch. Outputs:
[before](routing-cues-replay-before.json), [after](routing-cues-replay-after.json).

```bash
cd src/apps/api
.venv/bin/python ../../../scripts/kria/score_kri1392_routing.py --out /tmp/after.json
.venv/bin/python ../../../scripts/kria/score_kri1392_routing.py --compare \
  ../../../docs/reviews/kri-1384/routing-cues-replay-before.json /tmp/after.json
```

140 decisions differ over 124 messages. 107 are the same recovery reply with the offer sentence
added (unopenable draft, ordinary edits). The other 33 change routing.

Unopenable draft, brief flag off (`missing`), 17 changed from `main_creator` to recovery with
the offer:

| Messages | Before | After | Judgement |
| --- | --- | --- | --- |
| "make the font different", "give the title a different colour", "yazı tipini farklı yap", "Cut the part where I say 'um let me start over'." (authored and recorded `v2_speech_cut_at_word`), a 15-sentence title edit | Re-planned on a cue word or length | Recovery, offer | Right. Edits must not become re-plans because the draft would not open (KRI-237). |
| "start over", "redo the whole thing", "make me a new version", "Create the video again based on my prompt" (authored and recorded `east_run/t2-recreate`), "give me a completely different vibe", "baştan yap", "yeni bir versiyon istiyorum", the mixed ask, a bare "redo" / "yeniden yap" with no offer before it | Re-planned on a cue word | Recovery, offer | Right, one turn slower. No model reads this path with the brief flag off. Replying with the offer word re-plans. With the brief flag on, the extractor reads these before the target is loaded, so nothing changes there. |

German and Spanish rebuild asks were a recovery with no way out before. They now end with the
offer.

Offer accepted, brief flag off (`offer`), 3 changed from the copilot's repeated refusal to
`main_creator`: "redo", "yeniden yap", "nochmal". Right. This was the KRI-473 dead end. Every
other message after an offer still goes to the copilot.

Extraction failed (`degrade`), 13 changed from the honest recovery to serving the copilot's
in-place edit:

| Turn | Recorded ops | Baseline status of what it asked | Judgement |
| --- | --- | --- | --- |
| `action_text_look/t0` "Make the title bigger and yellow." | `patch_text` | met | Right. Both parts in one op; the joiner list blocked it. |
| `east_run/t6-no-fraunces` | `patch_text_style` | met | Right |
| `food_exact_title/t0` | `reorder_clips_by`, `add_text` | title met | Right |
| `food_readability/t0` | 8 × `add_text` | captioned, readable met | Right |
| `harbor_landmark_correction/t1` "Clip 5 isn't Halden Watchtower, it's Fort Halden." | `edit_text` | fix met | Right. No text word, so it was refused. |
| `harbor_title_from_brief/t0` | `reorder_clips_by`, `add_text` | title has the run, lacks the route | Right route, partial content (the copilot's title, same on the ready path) |
| `sport_exact_labels/t0` | 3 × `add_text` | met | Right |
| `sport_title_font_only/t0` | 4 × `add_text` | labels met | Right |
| `trip_chronological/t0` | `reorder_clips_by` | met | Right |
| `trip_landmark_labels_turkish/t0` "Her klibe gördüğü yerin adını yaz." | `label_each_clip` | Turkish names unmet | Right route, wrong content. The labels are not in Turkish, the same output as the ready path. |
| `trip_route_order/t0` | `reorder_clips_by` | met | Right |
| `vlog_change_all_fonts/t1` "Change all fonts to Montserrat." | `patch_text` | met | Right. "fonts" missed the singular `font` cue. |
| `vlog_exact_title_then_labels/t0` | `add_text` | met | Right |

"Baseline status" is the thread's final status for the requirements that turn introduced.

Without the sentence guard the same run served 8 more multi-sentence turns. Five had a
requirement the ops did not do and the copilot did not name: `east_run/t1-brief` and
`t5-brief-again` (per-clip labels, route order, time to read), `harbor_route_reversed` (the
filming order), `trip_described_title_facts` (the length in the title) and
`vlog_creator_facts` (partial). The guard keeps them on the honest recovery. It also keeps
three that would have been fine (`harbor_route_matches_filming`,
`trip_label_each_clip_from_facts/t0`, `vlog_restructure/t0`). That is the cost of the guard.

Unchanged: all 91 recorded turns on the `ready` path (brief flag off, target opens).

### Live extractor check

Where the cue used to decide a failed-render correction, the extractor's `request_scope` now
does. The same command records it for the authored corpus (`--extractor live`) as a follow-up
on a rendered video: [routing-cues-extractor.json](routing-cues-extractor.json).

- 23 of 23 matched the expected scope: 13 edits (including "make the font different", its TR
  and DE forms, "make the text faster", "make the video shorter", "revert", "undo that" and the
  quoted "start over") and 10 rebuilds (including the mixed ask, DE and ES).
- Model `gemini-3.6-flash`, prompt `be-1c1185bddc2d`, latency p50 6.05 s, max 11.91 s.
- Development ledger, `--usage-purpose live_eval --test-run-id rf-kri1392-scope-20261114-a
  --max-cost-usd 2 --approve-reservation`. Priced first at $0.93 of reservations
  (`--extractor price`), settled **$0.2891**. No judge, no copilot call.

The copilot prompt is unchanged, so its recordings stand and no copilot turn was re-recorded.

### KRI-1365 asks and revert

`scripts/kria/score_kri1365_edit_route.py` (real `plan_live_turn` and `_load_editor_target`
over the prod thread) is 7 of 7 VERIFIED on the base and on this branch: "Make the text
faster", "Make the video shorter", the per-text variant and "Revert to the old version. ..."
edit in place, and the wrong-content control is refused. `tests/kria/test_chat_undo_postgres.py`
(the `undo_last_edit` path, including the honest refusal when undo is not offered) passes.

### Request-following re-score

`python -m tests.evals.request_following.report --phase baseline` regenerates a file identical
to main's `request-following-baseline-scores.md`. Requirements met **74%** (90/122, partial 7,
unmet 25), overclaims **14**, copilot turn time **p50 10.9 s, p95 28.2 s** over 86 calls. The
KRI-1389 baseline was 71% (82/115), 14, p50 10.9 s, p95 37.7 s over 79 calls; the gap is
#2300, #2311 and #2316. This change does not move it, because that harness measures the copilot
after routing and this change touches only the routing in front of it.

### Tests and negative controls

- `tests/kria/test_kri1392_rebuild_offer.py` drives `plan_live_turn`. Run against the base, 8 of
  its 9 planner cases fail (the remaining one, "redo" with no offer, is right on both).
- The two new `test_failed_draft_correction_postgres.py` cases fail on the base: "Make the font
  different" re-planned through Main Creator, and a rebuild verdict reached the copilot.

## Not measured

- No device or rendered output. The change is planner routing; replies are server copy that
  already exists in `tr.json` and `de.json`.
- The brief-flag-off rebuild path still needs one more turn. A copilot `rebuild` intent would
  remove it, at the cost of a copilot prompt change and a new risk of edits read as rebuilds.
  Not done here.
- A one-sentence compound ask whose dropped part the copilot does not name can still be served
  on the extraction-failure path. No recorded turn shows it.
- A polite form of the offer word ("yeniden yap lütfen") is not the offer word. With the brief
  flag on the extractor reads it; with it off the copilot refuses again and repeats the offer.
