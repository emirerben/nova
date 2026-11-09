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

## Proof

- Offline replay of the Yorum1 shape (take A footage, takes B and C Visuals) must give the
  Yorum2 result: order A→B→C seated, no "couldn't tell which clip" problem, "café"
  heard by the photo-on-word grounding.
- Controls: Yorum2 (three footage takes) unchanged; a no-one-talking Visual video stays a
  Visual; switch off is byte-identical.
- Device: one real chat on Yasin's dev build (paid, ask first).
