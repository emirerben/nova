# KRI-1558: speaker layout per section — failure modes

Written before the implementation (CLAUDE.md testing rule). The carrier under test is the ticket's
recommended one: the existing per-clip `speaker_frame_override`, with the speaker take split into
back-to-back pieces of the same source at each section boundary (nothing removed between them).

Status: **confirmed** = reproduced by a render; **fixed** = the render passes after an engine change on
this branch; **checked** = the render passes on unchanged main; **open** = not built or not tested yet.

| # | Failure | Status | Evidence / guard |
|---|---|---|---|
| 1 | The voice dips at a split with nothing removed. `AudioCutHandles.plan` only treats a source *jump* as a cut, so a continuous join got the plain 25 ms declick on both sides. | confirmed → fixed | `SpeakerSectionSplitSeamTests.testTheVoiceKeepsItsLevelThroughASplitWithNothingRemoved`: on main the level fell to **0.11** of the unsplit take for 5 ms at the join (export and preview). With a continuous join playing with no fade: **1.00** export, **0.99** preview. |
| 2 | A hard join that is not sample-continuous clicks. | fixed (checked with #1) | Same test: largest sample-to-sample step at the join 0.0291 (export) / 0.0288 (preview) vs 0.0291 in the unsplit take. |
| 3 | The layout switches a frame early or late, or a frame belongs to neither layout. | checked | `testTheLayoutChangesOnTheBoundaryFrameWithNothingInBetween`: frames 25–35, preview and export, switch exactly on frame 30. Passes on main. |
| 4 | The speaker's cut-out restarts at the split, so one missed mask right after it shows the uncut frame (S7 resets on any clip change; the per-clip path also used a separate panel matte per clip). | confirmed → fixed | `testABadMaskRightAfterTheSplitHoldsTheLastGoodCutOut`: on main the split showed the uncut frame where one clip held the last good mask. Fixed by keying the matte by shot (`ShotContinuity`), and drawing a lone per-clip speaker through the composition's own matte. A real cut (source jump) still starts clean. |
| 5 | An app build without #1/#4 receives a split recipe and dips / flashes. | open | Needs a capability token + protocol bump (`scripts/bump-protocol.py`) so the server only splits for builds that have the fix, and keeps one layout otherwise. The ticket's "no protocol bump" assumption does not hold for this route. |
| 6 | Captions jump or restart at the split. | open | Captions are their own text lane on timeline time, so splitting the speaker clip should not touch them. To prove in an end-to-end Talking render with captions across a boundary. |
| 7 | A layout drifts off its section after a speech cut. | open | Pieces are clips, so a cleanup cut inside a piece shortens that piece only. To prove once the planner writes layouts. |
| 8 | "Put me back full screen" is half-applied because one of the three places that size the speaker is missed: layer `speaker_frame`, per-clip `speaker_frame_override`, `full_screen_windows`. `clear_speaker_frame` answers "You're already full screen" when the corner comes from a per-clip frame. | open | Code: `agents/editor_ops_v2/background.py` `_op_clear_speaker_frame`. |
| 9 | Per-clip `full` keeps the picture behind the cut-out speaker; it is not "speaker full screen in her own room". That is `full_screen_windows` (max 4 spans) or leaving the piece out of `subject_clip_ids`. | open | The layout set must map "speaker full screen" to the right carrier, or "no picture behind me after the intro" still shows a picture. |
| 10 | Captions or the ending text land on the speaker's face in a corner section (KRI-1482/1483). | open | Text placement reads one fixed geometry today. |
| 11 | No section list to hang layouts on: KRI-1556 (passages) is in Backlog and nothing on main defines passages. | open — decision | Asked before building the planner part. |
| 12 | Pictures put in "at one pace" (`set_background_sequence`) do not line up with what is said, so a layout per picture window would not be a layout per section. | open | Same dependency as #11. |
| 13 | A control that swaps two sections' layouts still passes the section check. | open | Mutation control to add with the planner part. |
| 14 | An older edit (one take clip) reopened in the editor loses its layout or gets split unasked. | open | Splitting must only happen when layouts differ; one layout keeps one clip. |
