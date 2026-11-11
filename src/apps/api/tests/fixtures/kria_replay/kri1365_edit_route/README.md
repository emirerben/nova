# KRI-1365 follow-up edits on a ready video

`make kria-replay FIXTURE=kri1365_edit_route` runs `scripts/kria/score_kri1365_edit_route.py`.

The fixture holds no creator media, signed URLs or creator words. The 15 clip durations and the
38.1 s ready montage come from prod thread 147eb928. A public Atatürk line stands in for the
approved Turkish title and its English translation.

Ways this can fail, each covered by a check:

- An approved title still counts as a pending wording discussion, so every follow-up is a full
  re-plan with a new approval (checks 1-3).
- After a re-plan render fails with no video, the follow-up targets the failed job and re-plans
  instead of editing the video the creator still has (check 4).
- The edit path admits an op that changes the approved words (check 7), or refuses a timing-only
  draft because a wording decision exists at all (check 5).
- The ops compile but the video or the words are not shorter, or a word is lost (check 5).
- A wording question that is still open skips Main Creator (check 6).
