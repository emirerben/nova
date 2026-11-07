# talking_keep_whole replay fixture (reconstructed, KRI-971)

Acceptance source for the keep-whole card fix. Written from the Linear ticket KRI-971, not from
the code under test.

Creator request (Don Julio lunch T1, thread 867D2F93, one 8.2 s Talking take):
"Use the whole clip as it is, don't trim anything."

The brief recorded it as requirement r1: `kind: style`, `scope: global`,
`facts: {"keep_whole_take": true}`.

Acceptance:

1. The draft's receipt for r1 is checked and met (before the fix: "I can't verify this one
   automatically yet.", because `_wants_whole_take` only accepted `kind == "timing"`).
2. The approval card's primary action does not cut the take: the thread read carries
   `speech_cleanup.recommended_choice == "keep_original"`, and the iOS card leads with
   "Keep original speech and create" under "You asked to keep the whole clip".
3. Older app builds ignore the new field and keep today's buttons.

    make kria-replay FIXTURE=talking_keep_whole      # check 3.r1
    cd src/apps/api && .venv/bin/python -m pytest tests/routes/test_creation_thread_cleanup_requested.py

Recorded: the creator sentence and brief r1 as quoted in the ticket. Reconstructed: the clip
analysis (transcript, segments, 8.2 s), the strategy (subtitled, original audio), and the
approval card, which is assembled offline from the real pure functions the draft turn calls.
The prod evidence folder was not available when this was written.
