# yorum3 (KRI-1342 / KRI-1346 / KRI-1347)

Prod Kria thread "yorum3" (2026-11-09, Fly `4f6ed3014`): a Çiğdem-style Yorumla talking video
on the iPhone (phone Talking route). Built with `scripts/kria/build_route_fixture.py` from
read-only admin GETs (thread events, turns, job debug) and scrubbed the same way (ids, paths,
URLs, hashes, file names, library asset ids). Added by hand from the same capture:
`prod_outcome.render.phone_lane_receipt` / `phone_beat_receipt` (beat ids, times, display mode,
trigger words), and the hold and review reply texts.

The bound brief is the approved one: r5 (the red closing screen) is `superseded` because the
creator chose "Make it without these". `scripts/kria/check_reply_consistency.py` reopens it to
replay the held first draft.

Used only by `scripts/kria/check_reply_consistency.py`; it is not a route-grader fixture (no
`checks` or `replay_inputs`), so it lives outside `tests/fixtures/kria_replay/`.
