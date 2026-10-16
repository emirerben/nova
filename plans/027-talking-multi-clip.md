# 027 — Talking to camera with several videos (speaker + cutaways)

Linear: [KRI-719](https://linear.app/kria/issue/KRI-719). Status: **Phases 1-2 built (server + iPhone render + iOS app, flag off)**; phase 3 open. Pause/filler removal for several videos: [KRI-720](https://linear.app/kria/issue/KRI-720). Written 2026-10-16 against `origin/main` 31af74ef7.

## Phase 1 as built (2026-10-16)

Changed from the plan after Yasin's note that a Talking edit can hold several talking videos ("2 talking, 2 normal"):

- Kria sorts clips into **speakers** (speech) and **footage** automatically. Speakers play back to back on the main track, each with its own audio and captions; footage covers them as muted full-screen cutaways.
- Speech cleanup is refused for every multi-clip Talking edit in v1 (KRI-720 adds it). With 2+ speakers, cards/sounds/beats and face fill are skipped.
- No creator speaker pin yet (comes with the iOS work in phase 2).
- Details: `docs/runbooks/phone-rendering.md` "Several videos on a phone Talking edit".

## Phase 2 as built (2026-10-16, iOS)

- Offline fallback cap for Talking is 10; the server's `max_clips` always wins (`CreationFormat.maximumClipCount(for:serverLimits:)`), so a server with the flag off still caps Talking at 1 and the single-pick picker auto-close keeps working there.
- Copy follows the cap: with more than one clip allowed, Talking reads "the video of you talking, plus any clips to show while you talk"; "up to 1 clips" plural fixed.
- Teleprompter take is refused only when the edit is full (it already was cap-based); the refusal names the cap.
- Live editor preview plays every speaker from the variant's `phone_talking_multi_clip.speaker_media_ids`, in order, as locked composite clips (first speaker carries any cut, the rest play whole). A speaker missing from the source pool falls back to the finished MP4.
- Live preview draws the receipt's `cutaways` windows muted and full frame over the speakers (`NativePhoneTalkingSource.cutaways` → `NativeEditorRenderCompiler.compile(talkingCutaways:)`). Read from the receipt on the client rather than projected as `media_overlays` rows: rows would show up as editable cards and an older build's Save would send them back as new full-screen overlays, which `lanes_from_editor_sections` refuses. A cutaway clip missing from the pool falls back to the finished MP4.
- **Not done:** no "Make speaker" pin (needs a server `set_speaker` action; scope with Yasin). Cutaways are not shown on the editor timeline and can't be moved.
- Tests: `CreationUITests.testTalkingToCameraPicksThreeVideos` (3 videos on Talking), `testLibraryPickerReturnsToChatByDoneAndBySinglePick` now runs against a cap-1 server (`KRIA_CHAT_TALKING_MAX_CLIPS=1`).


A creator on **Talking to camera** (`edit_format = "subtitled"`) can pick several videos. One is the **speaker** (main track: its own audio, captions, title, cleanup). The rest play **on top, full screen and muted**, at moments Kria picks, while the speaker's voice and captions keep running.

Today the format is one clip end to end (scoped that way on purpose in [subtitled-talking-head-style.md](subtitled-talking-head-style.md), "single captioned clip, not speaker + B-roll"). This plan reverses that scope call, so it needs Emir's OK.

## What already exists (reuse, don't rebuild)

| Piece | Where | Reuse |
|---|---|---|
| Speaker + muted full-frame cutaways on the iPhone | `compile_phone_subtitled_plan(cutaways=…)`, `_compile_cutaway_track` (`pipeline/phone_subtitled_plan.py:956`), track `talking-head-cutaways`; device test `DeviceMontageRenderE2ETests.testTalkingHeadCutawayRendersOnTheIPhone` | Yes: the device renderer already draws it. |
| Full-screen Visuals scheduling on Talking (KRI-297/661) | `services/phone_overlay_grounding._ground_fullscreen_sequence` (hook 2.5 s, ≤4 s windows, word-snapped starts, capture order, `no_room_in_timeline` receipts); survives speech cleanup via `remap_lanes_for_cut` | Yes: use its scheduler for cutaway timing. |
| Speaker pick by speech | `_pick_speech_spine` (`generative_build.py` ~20419, speech coverage ≥ 0.15); cloud `talking_head_assembler.select_spine` | Yes. |
| Speaker pin field | `pinned_spine_media_id` (`services/active_narration_source.py:94,319`) | Yes: the creator's choice rides this. |
| Per-media `role` in chat attachments | `CreationAttachedMedia.role` (iOS `Core/CreationFlow.swift:299`) | Yes: `role: "speaker"`. |

The KRI-136 `talking_head` path (self-narrated Voiceover items, `PHONE_TALKING_HEAD_RENDERING_ENABLED`, off) is **not** reused as a route: it forbids speech cleanup, uses a cruder evenly-spaced 1.5 s/3 s scheduler with no word snapping, ignores face/text layout, and fails the job on any cutaway error. The cloud `talking_head` renderer has no captions at all.

## Design decisions (recommended)

1. **Stay `subtitled`.** A declared Talking item with 2+ clips keeps `resolved_archetype = "subtitled"`; extra clips become a new cutaway *lane*. No new edit format, no `talking_head` routing. Why: keeps captions, cleanup, title, face-fill, cards and sounds exactly as they are.
2. **Who is the speaker.** Order of precedence: (a) creator's pick (`pinned_spine_media_id`), (b) a teleprompter take ("Record with script") is auto-pinned, (c) highest speech coverage via `_pick_speech_spine`. If no clip has speech, refuse with the existing KRI-653 silent-take copy.
3. **When cutaways play.** Kria picks, using the full-screen Visuals scheduler on the **cut** timeline (after cleanup): first 2.5 s stays on the speaker (hook + title), windows ≤ 4 s, starts snapped to spoken words, upload/capture order. Cutaway clips and full-screen Visuals share **one** slot list so they never overlap. Clips that don't fit are reported, never silently dropped.
4. **Speech cleanup works.** Because windows are scheduled after the CutPlan, the multi-clip cleanup refusals (`content_plan_build.py:1015`, worker `:6807`) go away for this path.
5. **Cap.** 10 clips total for Talking (1 speaker + 9 cutaways), not 50. A 60 s take fits roughly 10–12 windows anyway.
6. **One flag**, `TALKING_MULTI_CLIP_ENABLED` (default off), server only. The app reads the cap from capabilities, so no iOS release gate beyond shipping the UI. Turn on only after phone **and** cloud both ship (avoids one format behaving two ways depending on render destination).
7. **First version: Kria places the cutaways.** Creator instructions like "show the market clip when I say 'market'" (KRI-511 territory) come later.

## Work, in order

### Phase 1 — server, phone render (largest piece)

- `routes/creation_threads.py`: `_format_clip_limit("subtitled")` returns 10 when the flag is on (feeds capabilities `max_clips`, `media.clips.max`, upload reservation `:5744`, attach `:6147`). Add `role: "speaker"` to the attached-media projection and a `set_speaker` thread action that writes `pinned_spine_media_id`.
- Speaker resolution for declared subtitled: new helper beside `_resolve_archetype` (`generative_build.py:20247`) returning the speaker clip id by the precedence above; record it on the variant receipt.
- `active_narration_source.py:340`: declared subtitled with >1 embedded-audio clip resolves to the speaker instead of `ambiguous_embedded_source` (so preflight + cleanup run on the speaker only).
- Dispatch gate `tasks/content_plan_build.py:2331`: allow 2–10 clips when flagged; fix the 300 s duration check at `:2361` to read the **speaker**, not `phone_sources[0]`.
- Manifest `services/creator_capabilities.py:651`: `phone_format:subtitled` available for 2+ clips when flagged; keep refusing recorded voiceover.
- Worker `_run_phone_subtitled_job`: lift the guard at `:6797` for flagged declared subtitled; bind the speaker as the single main binding, the rest via `bind_phone_sources` as cutaway bindings.
- New lane in the KRI-174 lane system: `footage_cutaways`. Schedule with the shared full-screen scheduler (cut timeline, slots shared with full-screen Visuals), compile through `_compile_cutaway_track` with `source_start_s` support (today always 0; pick the clip's most active stretch later, start at 0 for v1). Lane failure drops the lane with a receipt instead of failing the job.
- Face/text: add cutaway windows to the grounding `occupied` list (`:7493`) so cards don't stack on them; title stays in the speaker-only hook window.
- Receipts + chat copy: "I used 4 of your 6 extra clips; the take is too short for the rest." English + Turkish (`tasks/kria_runtime.py` copy tables). Update `PHONE_GATE_MESSAGES`.
- Runtime v2: `kria/brief_checks._check_whole_take` (`:2763`) must stop reporting "uses one of your clips, not every take" when the extra clips went to cutaways.

### Phase 2 — iOS

- Picker: no Talking-specific cap in code once the server raises `max_clips`; change the offline fallback (`ChatWorkspaceView.swift:2492`) to 10 and its comment. The KRI-175 auto-close stops firing on its own (limit > 1); the user taps Done.
- Speaker choice: in the attached clip list (`CreationAttachments.swift:538`), a "Speaker" badge on the chosen clip and "Make speaker" on the others (sends `set_speaker`). Teleprompter take shows as speaker automatically. Teleprompter admission (`:337`) only blocks when a speaker take already exists, not when any clip exists.
- Copy: `FootageStage` (`CanonicalChatComponents.swift:461`) → "Pick the video of you talking, plus any clips to show while you talk." Fix "up to 1 clips" plural (`:477`). Format-switch error stays, with the new number.
- Editor preview: `NativePhoneTalkingSource.sourceIndex` (`NativeEditorSources.swift:194`) uses the speaker index instead of requiring `pool.clips.count == 1` (today 2+ clips loses the live preview and falls back to the MP4). Cutaways are drawn from the variant receipt as preview-only clips (see phase 2 notes above). Moving/deleting cutaways in the editor is a follow-up.
- Fixtures: `ChatUITestTransport.swift:153` → 10.

### Phase 3 — cloud render

- `_render_subtitled_variant` (`generative_build.py:27266`) today keeps the first clip and logs `subtitled_extra_clips_ignored`. Use the resolved speaker, then overlay the scheduled cutaways (muted, cover-fill) on the caption-free base before captions burn, reusing `assemble_talking_head`'s ffmpeg overlay pattern. Final encode stays `preset="fast"` (encoder policy).

### Phase 4 — later (separate tickets)

- Creator-directed cutaways ("show X when I say Y").
- Edit cutaway timing in the iPhone editor.
- Web plan-item page (`src/apps/web/src/app/plan/items/[id]/page.tsx:2115`, hard-coded `maxClips={1}`).

## Ways this can fail (write these down before coding, per repo testing rules)

1. Wrong clip picked as speaker (two clips with speech, e.g. a street interview). → Creator override; receipt names the chosen speaker in chat.
2. Speaker audio drops or a cutaway's audio leaks. → Cutaways compile at volume 0; device E2E asserts speaker audio continuous.
3. Cleanup cuts shift cutaways off their words. → Schedule after the CutPlan; test with cleanup on.
4. Cutaway overlaps a full-screen Visual or a card. → Shared slot list + `occupied`; compiler already rejects overlapping windows.
5. A cutaway clip shorter than its window freezes. → Clamp to clip duration (existing `_compile_cutaway_track` clamp).
6. Title or captions land on a face inside the cutaway. → Captions stay in the caption band; title only in the speaker-only hook window.
7. Too many clips for a short take. → Receipt "used N of M", never silent.
8. Format switch Montage → Talking with 12 clips. → Existing `clip_cap_exceeded` / iOS format-switch refusal with the new number.
9. Old app build (cap 1 fallback) talking to a flagged server. → Fine: the old build reads `max_clips` from the server; only the offline fallback is stale.
10. Duration gate reads the wrong clip and refuses a valid 4-minute speaker. → Fix `phone_sources[0]`.

## Verification (E2E first)

- Server: flip `test_multi_clip_declared_subtitled_still_fails_closed`, `test_subtitled_*_enforces_one_clip_limit`, `test_chat_format_clip_limits_match_plan_item_setup`, `test_dispatch_subtitled_two_clips_refuses_with_clip_count_reason` to the flagged behavior, keep flag-off byte-identical.
- iPhone render: new `DeviceMontageRenderE2ETests` case, declared Talking with 3 clips + cleanup on: speaker audio continuous, 2 muted cutaways on spoken words, captions continue over them. Artifact: the rendered MP4 + XCTest result bundle.
- iOS flow: XCUITest picks 3 videos on Talking, marks the 2nd as speaker, sends.
- Cloud: `make local-render MODE=generative CLIPS="speaker.mp4 a.mp4 b.mp4"` with a Talking item; attach the MP4.
- Real-device check on Yasin's iPhone before turning the flag on.

## Rollout

1. Merge phases 1–3 dark (flag off, byte-identical).
2. `fly secrets set TALKING_MULTI_CLIP_ENABLED=true --app nova-video` + restart api and worker.
3. Rollback: set it back to `false` + restart. Items already holding 2+ clips then render the speaker only (existing `subtitled_extra_clips_ignored` behavior), never fail.

## Open questions for Emir

- OK to reverse the original "single captioned clip" scope?
- 10-clip cap right?
- Phone-only first (scope the cap per item render destination) or wait for cloud too? Plan recommends waiting.
