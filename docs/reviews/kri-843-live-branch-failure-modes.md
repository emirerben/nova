# KRI-843 follow-up: mixed-language captions on the live Talking branch

Written before the code (project testing rule: list the failure modes, then the tests, then
the implementation). Scope: the two gaps PR #1782 (c6d77e7e9) left open.

1. The phone Talking writer's `else:` branch (`_run_phone_subtitled_job`, cleanup declined
   AND no quoted start/end line) transcribes live with one auto-language pass plus the EN/TR
   cross-check and never runs the per-stretch merge. A mixed Turkish/English/Spanish take on
   that path gets single-language captions.
2. `_redo_for_misheard_language` (`speech_cleanup_preflight.py`) redoes a settled preflight
   only when the cross-check returns a single language; a mixed reference infers "" so a
   preflight that ran before the Gemini reference existed is never redone.
3. Mixed-language captions must not DEPEND on the preflight (`speech_cleanup_preflight_mode`
   defaults `off`, rollout 0).

## How it can fail

| ID | Failure mode | Test |
| --- | --- | --- |
| L1 | A single-language take (reference names one language) pays extra whisper passes, or its calls / `caption_language_crosscheck` payload change at all. | `test_l1_a_single_language_take_keeps_todays_live_path` |
| L2 | `SPEECH_CLEANUP_MIXED_LANGUAGE_ENABLED=false` is not byte-identical to today's live branch (calls, event, captions). | `test_l2_kill_switch_off_is_todays_live_path` |
| L3 | The merge replaces correct first-pass words with a worse pass (the Turkish translation of "What's your name?" beats the English; "Gracias." over 7 s beats Lucas). | `test_l3_l4_l11_t1_live_captions_follow_each_stretch` |
| L4 | Stretches the reference does not confirm (Turkish chatter 16.5-25.7 s, the murmur) are left in the first-pass language and NOT replaced by another pass's hallucination, nor dropped. | same |
| L5 | KRI-838: the creator's requested caption language no longer overrides. The spoken pass must still be the merged one (beats / `raw_words` / spoken-text asks), the hinted caption pass still runs and `_ensure_requested_caption_language` still judges its text. | `test_l5_requested_caption_language_still_overrides_the_merged_pass` |
| L6 | Language word-lists misfire on names (Lucas, Don Julio, salsa criolla, chimichurri) and read an English reference as mixed. | `test_l6_names_in_an_english_reference_are_not_a_second_language` |
| L7 | Redo loops: the scheduler redoes a settled mixed-reference run more than once, redoes a decided run, or redoes a run that already had a reference. | `tests/services/test_kri843_mixed_reference_redo.py` |
| L8 | Cost cap: more than `MAX_EXTRA_PASSES` extra passes on the live branch, or an extra pass when the first pass heard no words. | `test_l8_extra_passes_are_capped_on_the_live_branch`, `test_l8_no_words_no_extra_pass` |
| L9 | An extra pass that raises a transcription/provider error fails the render, or a programming error is swallowed. | `test_l9_a_failed_extra_pass_is_skipped_on_the_live_branch`, engine test `test_f7_*` |
| L10 | The live branch leaves no evidence: the `caption_language_crosscheck` event must carry the same mixed receipt the preflight writes (`mixed`, `reference_languages`, `tried`, `heard`, `failed`, `segments`). | `test_l3_l4_l11_t1_live_captions_follow_each_stretch` |
| L11 | Preflight dependency: with `speech_cleanup_preflight_mode == "off"` and no snapshot on the job, mixed captions do not happen. | same (asserts the mode and the absence of a snapshot) |
| L12 | Worker late reference (`_run_engine`): a mixed reference landing mid-run is not re-run, while a single-language contradiction is. | `test_l12_a_mixed_reference_landing_mid_run_reruns_the_engine` |
| L13 | Merged words out of order or past the clip end break `build_plain_cues` or the device recipe. | `test_l3_l4_l11_...` asserts cue times are sorted and inside `[0, duration]` |
| L14 | Multi-clip Talking (KRI-719 extra speakers) and the explicit word-removal path (`explicit_source_transcript`) are NOT routed through the merge. | Out of scope; noted in the runbook. |

## Design

- One shared helper, `merge_mixed_language_passes` in `app/pipeline/speech_cleanup_analysis.py`,
  used by the preflight engine (`_crosschecked_transcript`) and the phone live branch. Same
  flag, same `MAX_EXTRA_PASSES`, same receipt keys. `mixed_reference_languages(reference)`
  is the single predicate (flag + two or more languages); `language_redo_reason` is what the
  scheduler and the worker's late-reference path ask ("mixed" | the EN/TR language | None).
- The phone branch passes `transcribe_whisper_cached` with the beats vocabulary prompt (the
  same prompt the first pass used), `duration_s=probe.duration_s`, `covers_s=None` (the plan
  item's Gemini clip analysis listened to the whole clip; only the preflight worker's own
  30 s Gemini sample is capped).
- Bounded redo: `_redo_for_misheard_language` already skips any run whose diagnostics carry a
  `language_crosscheck`; the redone run has the reference, so the engine always writes one
  (mixed or EN/TR). Once per snapshot by construction.
