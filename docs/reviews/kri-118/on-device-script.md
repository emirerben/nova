# KRI-118 lane L7: on-device manual test script

Manual test script for a human to run on a **physical iPhone** enrolled in
the phone-rendering pilot. The automated coverage in this PR
(`src/apps/api/tests/tasks/test_kri118_type_matrix.py`,
`src/apps/api/tests/replay/test_prod_failure_replay.py`) exercises the
backend dispatch gate, the phone compilers, and `validate_phone_pilot_recipe`
offline — none of it proves the app actually renders on real hardware. This
script closes that gap. It was authored, not run, by this PR's agent (no
device access from this environment) — a human must execute it and fill in
the blank columns.

## Setup

1. **Build and install a real-account Debug build on the device**, following
   `docs/runbooks/ios-development.md`'s "Real-account build for a
   collaborator" section (the `Kria Live Development` scheme) — or, for a
   from-scratch physical-device build under your own Apple ID, the
   `Config/Local.xcconfig` workflow documented there (per-worktree,
   gitignored, team + bundle ID; never commit it). Confirm the resolved API
   base URL in `Info.plist` before installing: `localhost` in
   `Config/Local.xcconfig` addresses the phone itself, not your Mac, so pass
   `API_BASE_URL=https://nova-video.fly.dev` (prod) or a reachable LAN URL
   for a local backend, per the same runbook's `## Commands` /
   `API_BASE_URL` guidance.
2. **Confirm the account is phone-render-enrolled** (`phone_rendering_for`
   on the API; ask an engineer to check `PHONE_RENDER_USER_IDS` /
   `phone_rendering_enabled` for your test account if unsure).
3. **Confirm the flags this PR's rows depend on are live** for the test
   account: `NARRATED_SELF_NARRATION_ENABLED`, `SUBTITLED_ARCHETYPE_ENABLED`,
   `PHONE_NARRATION_RENDERING_ENABLED`, and whatever
   `PHONE_RENDER_VERIFIED_FEATURES` currently holds in the target
   environment (`fly secrets list -a nova-video` names the secret, not its
   value — ask whoever owns the pilot rollout to confirm the live list; see
   `PROD_VERIFIED_FEATURES`'s docstring in `test_kri118_type_matrix.py` for
   why this PR could not confirm it independently).
4. Auto-Lock **must be off** for the duration of the session (a locked
   screen suspends the app and can abort an in-progress render or hang a
   polling XCUITest, per the KRI-29 phone-render timing lessons).
5. Have footage ready ahead of time for each row below (record or AirDrop
   clips/photos onto the device before starting, so each row's timing
   reflects render/UX behavior, not capture time).

## Script

| # | Type | Footage | Prompt / action | Pass criteria | Job ID | Adjustments shown | Pass/Fail |
|---|------|---------|------------------|----------------|--------|--------------------|-----------|
| 1 | Montage | 8 clips (2 under 1s) | "fast cut to music" | Renders end to end; both short clips are visibly present in the output (not silently dropped) | | | |
| 2 | Montage → day vlog | 6 clips of a day | "my Saturday" | Item/summary copy says "day vlog" (or equivalent shape language); clip order in the output matches the order they were shot/attached | | | |
| 3 | Montage → single hero | 1 long clip + 3 short | "show off this shot" | The long (hero) clip opens the edit and visibly dominates screen time over the 3 short clips | | | |
| 4 | Guided story | 5 clips + 3 photos | "tell the story of…" | Both photos and videos actually appear in the rendered output, not just one kind | | | |
| 5 | Guided + voiceover | 4 clips + a recorded voiceover | (record voiceover, then Generate) | Cuts are audibly timed to the voiceover's pacing, not just laid end to end | | | |
| 6 | Talking to camera (subtitled) | 1 selfie clip | "subtitle it" | Captions appear on screen; no "clean up your speech" / speech-cleanup card is ever offered for this item | | | |
| 7 | Narrated | 3 clips + voiceover | (record voiceover, then Generate) | Voiceover audio is audible in the output; no music-bed failure or silent audio track | | | |
| 8 | Self-narration, 1 clip | Narrated-type card, 1 clip, no voiceover recorded | Generate without recording | Renders as a subtitled (captioned) edit off the clip's own speech, not stuck or silently downgraded to montage | | | |
| 9 | Self-narration, 2+ clips | Narrated-type card, 2 clips, no voiceover recorded | Generate without recording | A clear, early, typed refusal appears (suggesting "record a voiceover") — never a stuck spinner and never a generic 500/error screen | | | |
| 10 | Photo & video post | 5 photos + 1 video | Slides / post card | The slides/post card is visible and selectable on a phone account, and it renders | | | |

## Notes for the tester

- For rows 2, 3, and 10 specifically, this PR's automated coverage only
  proves the BACKEND accepts and compiles these shapes
  (`test_kri118_type_matrix.py`'s day_vlog/single_hero story-shape rows,
  `docs/reviews/kri-118/video-type-requirements.md`'s slides row) — it does
  not prove the iOS picker actually offers "day vlog" / "single hero" /
  "photo & post" as a distinct chat-pickable choice yet (lane L2, Main
  Creator, is still in flight as of this PR). If the picker doesn't yet
  surface a shape choice, phrase the prompt as free text (as written above)
  and confirm the BACKEND still applies the shape correctly, or mark that
  sub-row "N/A — L2 not yet live" rather than "Fail".
- Row 9's exact refusal copy is `phone_gate="self_narration_multi_clip"`
  server-side (`app/tasks/content_plan_build.py`); the human-facing message
  should reference a voiceover, not a generic clip/format error — flag it as
  a bug if it doesn't.
- If any row instead shows a raw/generic error (a bare "something went
  wrong", an unhandled crash, or a spinner that never resolves), that is
  itself a FAIL regardless of whether the render would have eventually
  succeeded — this PR's entire premise is that every refusal is typed and
  user-legible, never a silent dead end.
- Record the Job ID from the device's local job list (or `/admin/jobs` if
  you have admin access) for every row so a failure can be cross-referenced
  against `/admin/jobs/{id}/debug` after the session.
