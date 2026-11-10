# KRI-1340: talking takes attached as Visuals

## What happened (raw data)

Prod thread `855274e8-a7b6-46d6-a07f-ab968d8516fe` ("Yorum1", 2026-11-08, item
`fb45417d-5164-4f60-9b31-efc384a766d4`), read with `scripts/admin.py --prod GET`:

- Format `talking_to_camera` (`subtitled`), picked before any media.
- Footage: one clip, `IMG_7244.MOV` (take A, 20.1 s), a phone analysis proxy bound to its
  device original (library asset `711d478f…`).
- Visuals: `IMG_7252.MOV` (take C, 7.15 s) and `IMG_7245.MOV` (take B, 6.99 s) as full
  pool uploads, both `ready`, plus 5 photos.
- Turn 1 reply: "I couldn't tell which clip "two at home" means … I held this first draft
  back", every one of the 9 asks unresolved. "café" is only spoken in take C, which no
  speech pass ever heard.

Emir (DM, 2026-11-09): it must not matter where the creator attached a talking video; if
they want it as a take, Kria uses it as a take.

The same three files as footage (Yorum2, `tests/fixtures/s3_yorum2_talking_order.json`)
carry `understanding.speech.to_camera: true` with their words: Gemini's clip analysis is
the same function for clips and Visuals (`autoplace.analyze_pool_video`).

## Design

One rule, one write, no phone-app change. Behind `VISUAL_TALKING_TAKES_ENABLED`
(default off; off = byte-identical).

1. **Rule** (`app/services/visual_talking_takes.py`, pure): a Visuals video is a talking
   take when its stored analysis says someone speaks to the camera
   (`understanding.speech.to_camera`), with at least as many words as the phone renderer
   needs to call a clip a speaker (6), in an item whose format is Talking (`subtitled`)
   with multi-clip Talking live, no recorded voiceover, and room under
   `TALKING_MULTI_CLIP_MAX_CLIPS`. No new model call.
2. **Write** (`app/tasks/visual_talking_takes.py`): when a Visuals video's analysis turns
   `ready` (`autoplace.analyze_pool_asset`), the server does what the phone does when it
   attaches footage: it hashes the pool bytes at their pinned generation, makes a 320-wide
   analysis proxy, uploads it under the thread, and appends a normal clip row (proxy
   receipt + original descriptor + the Visual's analysis) through
   `mutate_plan_item_media`. The row also carries `pool_source` (asset id, path,
   generation). Every reader of `PlanItem.clip_assignments` (planner, render contract,
   clip names/order, readiness, cleanup, captions, photo-on-word grounding, the phone job)
   now sees the take as footage, with no per-reader change.
3. **Phone render**: `PhoneSourceBinding.pool_source` makes `render_asset()` a
   `VisualRenderAsset` (pinned pool generation + the same SHA-256), so the phone downloads
   the take through the existing Visuals grant instead of looking for a camera-roll
   original it never bound. The KRI-841 ending clip already plays a pool video with sound
   on the main track. Editor Save maps speakers by render-asset id.
4. **Visuals tray unchanged** for the creator. The server hides an adopted Visual from
   the planner's Visuals (manifest + snapshot) so it is never also used as a cutaway.
   Deleting the Visual drops its adopted take.

## Ways this can go wrong (written before the code)

| # | Case | What must happen |
|---|---|---|
| 1 | B-roll video with someone else talking to camera (friend, interview) | Rule promotes it (to_camera). Accepted for v1: only in Talking items, where a to-camera speaker is the format's subject. Logged with the asset id. |
| 2 | Video with music or background chatter, no one talking to camera | `to_camera` false or < 6 words: stays a Visual. Control test. |
| 3 | Same take attached as footage AND as a Visual | Same SHA-256 as an existing clip's original: not adopted (would play twice). |
| 4 | Photos | Never adopted (kind image). |
| 5 | Creator really wants that talking video as a cutaway/overlay | v1 adopts it; the phone renderer's own speaker/cutaway split and the mention-demotion (`demote_speakers_to_cutaways`) still apply to it like any take. Follow-up if seen. |
| 6 | Phone missing the original | Not needed: the take renders from pool bytes by grant; SHA re-checked on device. |
| 7 | Visual still analysing when the creator sends the message | Not adopted yet; the turn plans it as a pending Visual (today's behaviour). Adopted when analysis lands; the next turn sees the take. |
| 8 | Adoption after a draft, before Approve | Adding a clip changes the media identity: the pending approval is retired like any attach (`retire_pending_approvals_for_source_change`). |
| 9 | Proxy and original durations differ > 0.1 s | Not adopted (the phone binding's receipt check would refuse the job). |
| 10 | Item not Talking, voiceover recorded, multi-clip off, cap reached, switch off | Not adopted. |
| 11 | Visual deleted after adoption | Its take row is dropped in the same transaction. |
| 12 | Task redelivered / runs twice | Idempotent: an existing row with the same `pool_source.asset_id` stops it. |
| 13 | Proxy upload races another writer | Upload uses `if-generation-match: 0`; the row is written under the item lock and rechecks every condition. |

## Proof (2026-11-09, commit on branch `codex/kri-1340-visual-talking-takes`)

| Check | Result |
|---|---|
| Yorum1 setup on real Postgres, real adoption task (ffmpeg proxy, local storage), the takes' own stored analysis: `tests/tasks/test_kri1340_visual_takes_postgres.py` | B and C adopted; b-roll (`not_to_camera`) and photo (`not_video`) stay Visuals; `bind_phone_sources` binds all three, B/C from the pool with the pool bytes' SHA; Yorum1's words ("two at home" / "one in the car") seat A → B → C with no unresolved problem; readiness has no "couldn't tell which clip"; take C's words ("cafes") are in the edit's footage; a rerun adds nothing; switch off / not Talking / a copy of take A change nothing. |
| Phone Talking job + recipe compiler with S3's own word timings, B and C pool-sourced: `tests/tasks/test_kri1340_visual_takes_phone_render.py` | B/C are pinned `visual` assets (pool generation + SHA), A stays an `original`; takes play once each, A → B → C (B at 20.1 s, C at 27.07 s); the café photo lands 2.6 s into take C, donut in B, Nar in A, the calendar decoy never; export needs only A's original; the editor streams B/C (`local_required: false`) and keeps A local; a footage binding serializes exactly as before. |
| Counted group in the Talking contract: `tests/kria/test_s3_yorum2_talking_clip_order.py` | "two at home" / "one in the car" seats A, B, C; "the home clip" and "three at home" (count mismatch) are still asked by name. |
| Delete: `tests/routes/test_plan_item_assets.py::test_delete_drops_the_talking_take_adopted_from_this_visual` | Deleting the Visual removes its take; footage stays. |
| Real S3 files through `_prepare` (IMG_7244/7245/7252.MOV, local storage, not committed) | Proxies 320×568 at 30 fps, durations within 0.016 s of the originals (20.108/6.989/7.152 s, the prod pool durations). IMG_7244's pool SHA equals the phone's own original SHA (`d6788e0a…`), so the copy guard holds on real data. |
| Related suites (`tests/kria tests/services tests/pipeline` + phone Talking task tests), `-n 6` | 24,345 passed. The 25 failures + 2 errors also fail on clean `origin/main` on this Mac (render-review evidence needs Linux resource limits, face sampler cv2, Apple-revocation DB name, real-storage review admission, `test_s3_caption_middle_off_face`). |

No phone-app change: the device already downloads `visual` assets by grant and plays a main-track
pool video with sound (KRI-841 ending clip); the editor already plays URL-backed speakers
(`NativeEditorSourcesTests.testPhoneTalkingEditWithSeveralVideosPlaysEverySpeakerInOrder`), and the
relink prompt lists only `local_required` sources.

Not done here: a simulator UI test that attaches takes the Yorum1 way needs the Visuals analysis
(Gemini) and a chat turn, both paid; the real-iPhone run below covers the same path.

Pending: one real chat on Yasin's dev build with the switch on (paid, ~$0.50–2): finished video
has all 3 takes in order, ~36 s.
