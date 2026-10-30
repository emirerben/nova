# KRI-1075: renaming the chat while Kria plans threw the finished plan away

Parent run: Leverkusen kit M1 run 1 (iPhone chat 2026-10-30, thread `5dda77f9`, turn
`a8da158a`, Fly `1deead562`, TestFlight 1801, protocol 24). Sibling: KRI-1076.

## Original request (verbatim)

> Montage of my night at the BayArena: my friend Taha invited me to his VIP box for Bayer
> Leverkusen vs 1. FC Nürnberg in the DFB-Pokal. Put everything in the order I filmed it,
> starting with the train ride. Make it all vertical, no black bars. Skip the photo of the
> empty train seat, and use only one of the two photos of the view from our box. Keep the
> sound only on the train clip with the announcement "Nächster Halt: Leverkusen Mitte" and
> mute everything else. Title: "Pokalnacht in Leverkusen 🔴⚫". Make it 25 seconds. Suggest a
> song but don't add it.

While Kria planned the reply, the creator renamed the chat from the chat list twice.

## Facts the expectation rests on (thread events + prod log, UTC)

| rev | time | event |
| --- | --- | --- |
| 12 | 14:50:24 | `user_message` (turn `a8da158a`) |
| 13 | 14:50:25 | `thread_title_generated` "Pokalnacht in Leverkusen 🔴⚫" (`title_generated_revision` 13) |
| 14 | 14:50:38 | `thread_renamed` "Pokalnacht in Leverkuseb" |
| 15 | 14:50:42 | `thread_renamed` "Pokalnacht in Leverkusen" |
| — | 14:51:07 | `nova.creator.main` done (43 s, $0.150); 14:51:15 `run_kria_turn` → `requeued` |
| — | 14:51:57 | `nova.creator.main` again (42 s, $0.094) |
| 16 | 14:52:06 | `assistant_response` (the clip picker) |

The worker's commit fence `conversation_revision_matches_sync` tolerated the automatic title,
duplicate copies (KRI-552) and queued follow-ups; a `thread_renamed` in the gap counted as a
conversation change, so the turn went back to `pending` and planned again.

A chat name is display-only. No planner input reads `CreationThread.title`: its readers are
the thread/job list responses and the finished-edit taste profile. A requested video title
comes from the creator's message ("Title: …"), not the chat name.

## Observable acceptance

1. One or several renames while a turn plans do not requeue it: one planning pass, the first
   plan is committed, and the reply arrives about one plan sooner (~50 s here).
2. The creator's last chosen name is the chat's name after the commit.
3. The iPhone's next message, sent with the revision it then sees, is accepted.
4. Any other change in the same gap (a clip added) still requeues the turn.

## Wrong results (must be refused)

- A rename masks a real change: a clip added beside the renames is committed against the old
  plan (control: `test_a_clip_added_alongside_the_renames_still_replans`).

## Case matrix

| Case | Covered by |
| --- | --- |
| Prod order: auto title, typo rename, fixed rename | `test_two_renames_while_kria_plans_keep_the_first_plan` |
| Renames plus a clip added | `test_a_clip_added_alongside_the_renames_still_replans` |
| Duplicate copy / queued follow-up (KRI-552) | `tests/kria/test_kri552_duplicate_send_postgres.py` (unchanged, passing) |
| All three worker commit paths | one shared fence function; the response path is replayed end to end |
| EN/TR | Not affected: no copy or parsing changed. |

## Remaining gaps

- Client-side revision checks (`conversation_revision_matches`, async) still count a rename
  as a change: an approval sent with a revision read before a rename on another device gets
  a 409 and the app refreshes. Not seen in this run (the creator's picker answer at 14:54:01
  went through); not changed here.
- Not verified on the iPhone yet: needs a deploy, then a rename while Kria plans.
