# Kept-line montage: closing text and a second free-standing text

Follow-up to KRI-939 (PR #1839). That PR made the spoken-excerpt phone route
(`run_phone_speech_montage_job` → `compile_phone_speech_montage_plan`) draw the
confirmed title and clip labels. It deliberately left two contract texts undrawn,
so `verify_phone_recipe` still refused them by name:

- a `closing` text (strategy `closing_title`: "end on the text …"), and
- a second free-standing `any` text (a whole-video brief literal that is not the
  title, a label, a corner pin or a chapter list).

## Real run

No production job refused on this route for either text. Of the 35
`phone_plan_unsupported` jobs in the last 200 failures (prod admin read,
2026-10-26), the only `speech_montage` one is Don Julio walk-in M1
(`1cb33402`, title only, fixed by #1839). The replay below therefore extends
M1's real contract (opening + duplicate `any` title, two clip labels) with a
closing text and a second line, built from the shapes the contract builder
emits (`creator_render_contract.py`: `closing_title` → `closing`; a global brief
literal → `any`).

## Where the words are dropped

1. Brief extraction / strategy: both texts survive (they are in the pinned
   contract, which is why the refusal can name them).
2. Strategy → job: `phone_speech_montage_job._confirmed_text` picks only the
   `opening` (else first `any`) text and `clip` texts. **Closing and extra `any`
   texts are dropped here.**
3. Compiler: `compile_phone_speech_montage_plan` has no input for them, so the
   recipe never carries a layer with those words.
4. `rendered_evidence` → `exact_texts`: correctly finds no matching layer and
   refuses. The check is right; nothing upstream drew the words.

## Ways the fix can fail (written before the code)

1. Closing text missing, or ending before the last frame (the check wants
   `end_s >= duration - 1 frame`).
2. Closing text on a short edit: its hold runs past the start of the timeline or
   past the end, or swallows the whole edit.
3. Closing text shorter than a confirmed hold (`duration_s`).
4. Second line merged into the title layer (one layer reading "Title Line"), or
   drawn as a duplicate of the title / a label / a corner pin / a chapter list
   the edit already shows.
5. Second line overlapping the title (same place, same time) or the closing text.
6. Second line in its own window but no room on a short edit: dropped silently
   instead of stacked clear of the title.
7. Closing or second line on top of a clip label on the same cut (same y band).
8. Text over a face / in the caption band: this route has no face tracking and
   draws no captions; the closing text goes in the lower label band, not the
   middle of the frame where a speaker's face sits.
9. Turkish (or any non-ASCII) words drift: dotted/dotless i, combining marks,
   emoji, re-casing.
10. Editor re-save drops the text: the variant row must carry every drawn row
    (the editor preview compiles text from the row, never the recipe), read-only.
11. Chat receipt claims "couldn't add" for words that are on screen: the record's
    text facts must list what was drawn.
12. Flag-off / legacy path changes: an uncontracted job, or a contract with no
    closing / extra text, must compile byte-identically to before.
13. A label for a clip the edit never shows must still refuse (unchanged).
