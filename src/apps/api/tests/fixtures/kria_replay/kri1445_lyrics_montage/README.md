# kri1445_lyrics_montage

Prod thread 6db7ecd4 (2026-11-14). One 14 s clip, a montage, and the creator's pasted lyrics
(3 lines) as on-screen text. Two jobs ended `processing_failed` / `creation_text_failed` in the
`assembly` stage, before any phone render.

    make kria-replay FIXTURE=kri1445_lyrics_montage

The scorecard is `scripts/kria/score_kri1445_lyrics_montage.py`. It runs the real phone worker
(`_run_phone_unified_montage_job` to `compose_creation_text` to `_run_phone_guided_job` and the
phone compile) offline on the recorded, scrubbed job. Only the text composer's model call is
replaced, by a cassette. The stored strategy is not handed to the worker as it is: its pins and
clip intents, and the resolved clip intents, go through the real `adapt_creator_action` first,
the planning boundary every new request's strategy crosses.

## What the plan held

The recorded approved strategy carried the lyric twice (what the replay hands the planning
boundary; see `adapt_creator_action` for what comes out). `clip_intents[0]` is a `caption` op with the
whole lyric as one block, and `pinned_texts` holds the same lyric as 3 lines at `bottom_center`.
The real worker turns that into four text bars (a clip label with the whole lyric, then the 3
lines) and protects all four as confirmed copy. The brief has `r1` ("find the lyrics and add
it as text", no literal) next to `r2` (the pasted lyric as its literal).

## Cassettes

| Name | Source | What the composer returns |
| --- | --- | --- |
| `A_clarification` | recorded outcome, authored output | a question about which layout to keep (job 1) |
| `B_removed_bar` | hand-authored | remove bar 0, then re-time bars 0-3 (job 2's raise site) |
| `C_remove_block` | probe | remove the one-block bar only |
| `D_no_op` | probe | `describe`, no ops |

The composer's stored input and output are redacted (keys and sha256 only), so every cassette
is authored. A is authored from the recorded error tail and reproduces that tail exactly. B
reaches the recorded error and cause class through real code, but the recorded ops were never
stored and may have differed. C and D are not from the incident.

## Checks

1. `each_lyric_line_once`: each line is drawn once in the compiled recipe's `text_layers`
   (NFC and diacritic folded; a layer drawing the whole lyric counts once per line in it).
2. `job_not_text_failed`: no `CreationTextCompositionError` / `creation_text_failed`.
3. `clarification_becomes_question`: a composer question is saved through
   `_save_request_recovery` as the creator's question and the turn does not end as a failure.
   The read-back in `tasks/kria_runtime.py` needs a thread and a database and is not run here.
4. `dropped_line_rejected`: negative control on the counter in check 1.
5. `composer_not_called`: the lyric is already drawn and every ask is met, so the text composer
   is never called and no cassette is consumed.

The scorecard exits 1 until every check is VERIFIED.

## Scrubbing

The lyric and the song title are invented (3 Turkish lines of the same shape, diacritics
included). Thread, user, media and storage ids, the source filename and the media hashes are
removed. The media analysis is trimmed to subject, description and moments.
