# talking_keep_whole replay fixture (reconstructed, KRI-971)

Acceptance source for the keep-whole card fix. Written from the Linear ticket KRI-971, not from
the code under test.

Creator request (Don Julio lunch T1, thread 867D2F93, one 8.2 s Talking take):
"Use the whole clip as it is, don't trim anything."

The brief recorded it as requirement r1: `kind: style`, `scope: global`,
`facts: {"keep_whole_take": true}`.

Acceptance, and what proves each part:

1. The draft's receipt for r1 is checked and met (before the fix: "I can't verify this one
   automatically yet.", because `_wants_whole_take` only accepted `kind == "timing"`). Proven by
   this replay: check `3.r1` is red on `origin/main`'s `brief_checks.py` and green here. The
   replay grades only that receipt and the route table. It does not grade the approval card.
2. The approval card's primary action does not cut the take: the thread read carries
   `speech_cleanup.recommended_choice == "keep_original"`, and the iOS card leads with
   "Keep original speech and create" under "You asked to keep the whole clip". Proven by
   `tests/routes/test_creation_thread_cleanup_requested.py` (the real `_response`) and
   `SpeechCleanupOfferTests`, not by this replay.
3. Older app builds ignore the new field and keep today's buttons ("Clean up speech and create"
   first). A known limit: the fix reaches a creator only on a build that carries the iOS change.

    make kria-replay FIXTURE=talking_keep_whole      # check 3.r1; exits 0 with a valid scorecard
    cd src/apps/api && .venv/bin/python -m pytest tests/routes/test_creation_thread_cleanup_requested.py tests/kria/test_keep_whole_take_any_kind.py

`replay_inputs.text_elements` is `[]` on purpose: the ask has no title, and the Talking replay
calls the render entry with the writer's text rows, which here are none.

Recorded: the creator sentence and brief r1 as quoted in the ticket. Reconstructed: the clip
analysis (transcript, segments, 8.2 s), the strategy (subtitled, original audio), and the
approval card, which is assembled offline from the real pure functions the draft turn calls.
The prod evidence folder was not available when this was written.
