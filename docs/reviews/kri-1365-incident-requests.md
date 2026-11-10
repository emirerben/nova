# KRI-1365: shorten / speed-up follow-ups on a ready montage

Redacted request record for the 2026-11-10 incident. The creator had a ready 38 s montage
(15 silent clips, two word-by-word text sequences at different places, approved wording) and
sent these chat turns. Creator wording is replaced by a stand-in in fixtures.

| Turn | Message | Expected outcome |
| --- | --- | --- |
| 1 | "Make the text faster" | The ready video's text sequences are retimed in place (editor ops, no new approval, no re-plan). Every approved word is kept. |
| 2 | "Make the video shorter" | The ready video is cut proportionally (about 30%) in place. |
| 3 | "Make the video shorter, showing each text shorter" | Clips and both text sequences shrink together; the pair still starts and ends together. |
| 4 | "Revert to the old version. Make the video shorter, showing each text shorter" after a failed render | The earlier ready video is the edit target. |

Further expectations:

- Footage whose clips all have `has_audio=false` never gets a draft that requires camera audio,
  and an approved draft is never rejected at render time for it (PR 2).
- Approved word-by-word text is paced and split by the server; a pair shown together ends
  together, and a text that cannot be read in the video is reported, not flickered (PR 3).
- Changing an approved word through an editor op is refused.
