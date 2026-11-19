# KRI-1445: failure modes after the second fix

KRI-1445 covers the founder's lyrics montages on 2026-11-14 and 2026-11-16 (prod threads aa36d140, 6db7ecd4 and 59f3707e). One 14 s clip, Turkish lyrics pasted into an English request, and no video. The fixes merged so far are #2339 (length), #2340 (consent reply keeps the caption), #2341 (lyrics drawn once), #2342 (simpler-version card), #2343 (pasted lyrics language, superseded here) and #2369 (pre-approval re-check). #2368 is the third fix PR. It moves the chat language from word scoring to the model that reads the message.

Failure modes for KRI-1445 (after fix #2)
- First message is an English request around Turkish lyrics (thread 59f3707e) -> reply in English and `en` stored | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_the_incident_first_message_gets_an_english_reply
- A genuinely Turkish sentence in an English chat -> chat switches to Turkish; later "Yes"/"Yap" keep it | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_a_turkish_sentence_switches_and_short_replies_keep_it
- A reply too short to read ("Do it", "Yap") -> the stored language is kept, never flipped | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_a_short_reply_the_model_cannot_read_keeps_the_chat
- The model returns an unsupported code ("xx", or "de" while German replies are off) -> the stored language is kept | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_an_unsupported_code_keeps_the_stored_language
- The Main Creator call fails or times out -> the stored language is kept and the recovery reply uses it | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_a_failed_model_call_keeps_the_stored_language
- A server refusal written after the model call (the readiness hold) -> it is in the model's language | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_the_readiness_hold_is_written_in_the_models_language
- A late deferred brief extraction -> it never overwrites a newer turn's language | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_deferred_extraction_stores_only_for_the_latest_message
- Kill switch off (`KRIA_REPLY_LANGUAGE_ENABLED=false`) -> no prompt line, nothing stored, English copy | covered by tests/kria/test_model_owned_reply_language_postgres.py::test_kill_switch_off_stores_nothing_and_ignores_the_model
- A model-chosen length a hair over the clips (14.005 s on 14.03 s) -> no refusal | covered by tests/kria/test_kri_tadic_length_tolerance.py::test_the_incidents_model_chosen_length_passes
- A stated length the clips cannot reach -> it still asks | covered by tests/kria/test_kri_tadic_length_tolerance.py::test_a_stated_length_the_clips_cant_reach_still_says_so
- "Should I make a simpler version?" from readiness -> card offered, and "Yes" drafts it | covered by tests/kria/test_tadic_readiness_simpler_card_postgres.py::test_yes_after_the_readiness_refusal_drafts_the_simpler_version
- A length refusal beside a separately held text ask -> no card, both blockers named | covered by tests/kria/test_tadic_readiness_simpler_card_postgres.py::test_a_mixed_hold_names_both_blockers_and_offers_no_card
- Pre-approval re-check rewrites a field no ask is about -> the rewrite is adopted, drafted and named in `recheck.changed_fields` | covered by tests/kria/test_readiness_recheck_fixed_point_postgres.py::test_the_incident_drafts_the_renderers_normalization_and_names_it
- Pre-approval re-check rewrites an asked-for field -> the simpler-version card, and "Evet" drafts it | covered by tests/kria/test_readiness_recheck_fixed_point_postgres.py::test_a_rewrite_of_an_asked_field_asks_with_the_card_and_evet_drafts_it
- A rewrite found at approval time -> approval is still cancelled | covered by tests/kria/test_readiness_recheck_fixed_point_postgres.py::test_approval_still_cancels_on_a_rewrite_it_finds
- "Yap" after the lyrics were pasted in a separate message -> the caption intent is kept | covered by tests/agents/test_clip_intent_planner_consent_reply.py::test_the_request_as_quote_keeps_the_verse_pasted_after_it
- A quote that no creator message holds -> it is still dropped | covered by tests/agents/test_clip_intent_planner_consent_reply.py::test_a_quote_no_creator_message_holds_is_still_dropped
- Lyrics as a caption plus the same words split into pins -> drawn once | covered by tests/kria/test_single_lyrics_text.py::test_lyrics_split_into_pins_and_a_caption_become_one_text

Known limits outside this list:
- The field the pre-approval re-check rewrote in thread 59f3707e is still unknown. #2369 records it in `brief_coverage.approval_readiness.recheck.changed_fields` the next time it happens.
- The live language check ran on gemini-3.1-pro-preview ($0.3005, `docs/reviews/kri-1445/model-owned-language-live.md`). Prod's Main Creator runs gpt-6.1-sol, so the founder's next real chat is the first check of this behaviour on the production model.
- Copy written before any model reads the message (instant status replies, "Add at least one clip", the Steer-now stop line) uses the stored language.
