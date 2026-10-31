# KRI-1076: "skip the photo of the empty train seat" found no photo

Parent run: Leverkusen kit M1 run 1 (iPhone chat 2026-10-30, thread `5dda77f9`, turn
`a8da158a`, Fly `1deead562`, TestFlight 1801, protocol 24). Sibling: KRI-1075 (the rename
requeue that made this turn plan twice).

## Original request (verbatim)

> Montage of my night at the BayArena: my friend Taha invited me to his VIP box for Bayer
> Leverkusen vs 1. FC Nürnberg in the DFB-Pokal. Put everything in the order I filmed it,
> starting with the train ride. Make it all vertical, no black bars. Skip the photo of the
> empty train seat, and use only one of the two photos of the view from our box. Keep the
> sound only on the train clip with the announcement "Nächster Halt: Leverkusen Mitte" and
> mute everything else. Title: "Pokalnacht in Leverkusen 🔴⚫". Make it 25 seconds. Suggest a
> song but don't add it.

Media: 8 iPhone videos and 5 Visuals photos. Exactly one photo shows a seat (IMG_1001).

## Facts the expectation rests on

- IMG_1001's stored analysis (ready, attempt 1): subject "Train seat and window view",
  description "A photo taken from inside a train or bus, showing the back of a dark grey seat
  with a light grey folded tray table, and a window reflecting the interior." No other photo
  or video record mentions a seat.
- The turn's saved plan (read-only admin GET of `/admin/creation-threads/5dda77f9…/turns`)
  lists four planner intents: `order_filmed` (order), `exclude_empty_seat_photo` (exclude),
  `select_one_view_photo` (include), `include_train_clip_sound` (include). Attributes and
  `source_kind` are not kept in that record.
- Both planning passes logged `kria_clip_exclusions_contested asks=1 pinned=0 suspected=0
  unmatched=1`, after two concurrent resolver calls (13 clips: two batches). The picker row
  offered 12 clips (7 videos + 5 photos; the train video is placed by the strategy) with
  `suggested_media_ids: []`. The creator tapped IMG_1001.
- No `agent_run` row exists for `nova.plan.clip_intent_planner` or
  `nova.plan.clip_request_resolver`: both ran with `plan_item_id=None`,
  `creator_agent_session_id=None` (prod log), so `persist_agent_run` skipped them.

## What is and is not known

Suspects are the resolver's below-bar matches and vision-check clips. `suspected=0` means
the resolver returned neither for the skip in either batch (or returned an assignment that
`_invented_selection` dropped). Which one, and why, needs the resolver's raw answer, which was
never stored. The same calls did match two other asks: the picker-answer turn carried them
(`clip_intent_matches_carried carried=2`, 14:54:30Z). Don Julio M1 (KRI-807) had the
same shape: a "skip the … of the empty table" ask with no confident match.

Telling a "photo of" ask apart is the planner model's job (`ClipIntent.source_kind`). The
readers landed in #1948 (KRI-1052); the producer (prompt + inventory key) is KRI-1052's next
increment. Server code does not read the creator's words for it (DECISIONS 2026-10-21).

## Observable acceptance (this PR)

1. A chat turn that resolves clip intents writes `agent_run` rows for the clip-intent planner
   and every resolver batch under the plan item, and the resolver row's input shows the clip
   records it read (here IMG_1001's "Train seat and window view") and the skip it was asked.
2. A skip the planner marks photo-only offers only photos in its picker and never pre-ticks a
   video; a video-only skip offers only videos; the picker's clip cap counts only that kind.
3. A skip with no source kind offers and pre-ticks exactly what it did before.

## Wrong results (must be refused)

- A photo-only skip whose resolver suspects the train video and the seat photo pre-ticks the
  video (the control in `test_a_photo_skip_offers_only_photos_and_never_ticks_a_video`).
- An unmarked skip drops a video from its picker.

## Case matrix

| Case | Covered by |
| --- | --- |
| Prod turn: 13 clips, two resolver batches, no suspect | `test_the_missed_skip_is_saved_with_what_the_resolver_read` |
| Planner marks the skip photo-only, same turn | `test_a_photo_only_skip_from_the_planner_offers_only_photos` |
| Photo-only skip, a video and a photo suspected | `test_a_photo_skip_offers_only_photos_and_never_ticks_a_video` |
| Video-only skip | `test_a_video_skip_offers_only_videos` |
| No source kind (every turn today) | `test_a_skip_without_a_kind_offers_every_clip_as_before` |
| Photos after a full picker of videos | `test_photos_after_a_full_picker_of_videos_are_still_offered` |
| EN/TR | Not affected: no copy or parsing changed; the picker text is the existing `say()` copy. |

## Remaining gaps

- Why the resolver missed IMG_1001 is unproven. The next miss is readable from `agent_run`.
  Re-running the resolver on this turn would need the 8 prod video records (not captured)
  and a paid model call; not run.
- The planner does not yet mark "photo of" asks photo-only (KRI-1052 producer), so the
  picker still offers videos for this request until that lands.
- The fixture's 8 video records are one-line stand-ins from the kit answer key; the 5 photo
  records and all media ids are the prod ones.
