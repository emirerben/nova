# Çiğdem S2 "ss2b" fixes — failure modes (written before the code)

Run: 2026-11-08 20:51–20:58Z, thread `B562466E-1B84-449C-A1E8-82318300A301`, job
`f4f81c07-13ec-4944-b0d8-066151ae6737` (device render, client protocol 45). Prompt (Çiğdem's
"Yorumla" green-screen format, English stand-in take, 31.2 s raw):

> Yorumla episode 9: Kervan x Lumo Pamukkale show. Make the whole video like a green screen: me
> small at the bottom right in front, and behind me full screen whichever photo or video I'm
> talking about, changing every 3-5 seconds. Cut the pauses and the sentence I restarted.
> Captions small, white, lowercase, in the middle. No music.

## What the raw records say (not the check script's summary)

| Miss | Evidence | Decision point |
|---|---|---|
| "okay, we're recording." kept at 0.0–1.04 s | `silence_cut.removed` = 4 silences + 1 retake (16.4–20.14 s); the slate line is never a candidate | Speech cleanup has no category for a production slate: it cuts pauses, filler sounds and abandoned takes (`retake_detector` = a restart of the SAME content). `speech_cleanup_analysis.analyze_speech_cleanup`, `generative_build._silence_cut_retake_spans` |
| Instagram collab post (IMG_7127, "kervan × lumo … Shot on Lumo") on "feeds fashion" (~20 s) instead of "Lumo was the show's technology partner" (7.9 s raw) | `turns[0].plan…background_media.sequence` | Main Creator's `spoken_phrase` choice (LLM). The layout put every entry on its phrase correctly. |
| Backgrounds held 5–6.5 s against "every 3-5 seconds" while the receipt said "met" | windows 0–4 / 4–10 / 10–16 / 16–20 / 20–26.5 s; `cadence_s` absent from the strategy; r1 "5 photos or videos take turns behind you" | 5 photos for 26.5 s and the decided no-repeat rule (S57–S62, "a longer scene beats a repeat") make 3–5 s impossible. The bug is the silent "met": the planner drops the asked rhythm when every entry has a phrase, and the receipt never compares holds to it. |
| Caption line 68 px OCR (her reels 40–52 px) | `caption_look_receipt.size_px` 52 for "small" | `caption_look_ask.CAPTION_ASK_SIZE_PX["small"] = 52` (default 78). OCR line box ≈ 1.3 × font px. |
| (S4) backgrounds stop after ~30 s | — | Already fixed on main: `CHAT_EVIDENCE_SPEECH_BUDGET_CHARS` (a5353685f). No change here. |

Not misses (kit corrections, outside this repo): IMG_7125.MP4 is a runway clip on the Pamukkale
terraces, not a drone shot, so "Pamukkale" is the right word for it (answer-key keyword "drones"
was wrong); the kit's full-screen detector does not recognise a background behind a cut-out
speaker, so it scored 2/5 where frames show 4/5 on their words.

## A. Production slate at the start of a take ("okay, we're recording")

Fix: a deterministic, closed-list detector for the take's first utterance, fed into the same
retake-span path (same snapping, voice-guard exemption, receipts). Flag
`SPEECH_CLEANUP_SLATE_CUT_ENABLED`, default off.

1. Cuts a real opening that contains a slate word ("We're recording our first album today") →
   the WHOLE first utterance must equal a slate phrase after stripping leading/trailing fillers;
   a longer utterance never matches.
2. Cuts a real hook that is short and generic ("Ready?", "Here we go.", "Action!", "Let's go") →
   those are not on the list; only recording/rolling/countdown/mic-check talk is.
3. The transcript glues the slate to the first real sentence without a pause or full stop →
   the utterance ends only at sentence-final punctuation or a ≥ 0.25 s gap; no boundary within
   the first 6 words = no cut.
4. A slate in the middle of the take ("okay we're recording again") → only an utterance that
   starts at word 0 (after optional fillers) is considered.
5. The take is nothing but the slate, or almost → at least 3 words must remain after it.
6. Turkish casing/diacritics ("Tamam, KAYITTAYIZ.", "kayıt başladı") → Turkish-aware casefold
   (İ→i, I→ı) and punctuation strip before comparing; list carries EN + TR forms.
7. Slate longer than a slate (a mumbled 5 s preamble) → utterance must end within 3.0 s.
8. Same span also flagged by the LLM retake detector → spans merge in `_merge_removals`; no
   double count.
9. Voice guard drops the cut because the slate is voiced → passed as a retake span, which the
   guard exempts (`voice_guard_exempt_spans`).
10. Flag off → no span added; plans and receipts byte-identical.
11. Several talking clips → each clip's analysis sees its own word 0; a slate at the start of
    clip B is cut too (same rule per take).
12. Malformed / empty words → no span, never an exception.

## B. Photo on the wrong sentence (planner choice)

Fix: one rule in the Main Creator's background-sequence section: anchor each photo/video on the
sentence that is about what it shows (its people, product, place, names, on-screen text such as
a collab post's account names); never move one to a later or closing line to fill time — a
longer hold of the previous one is fine. Prompt fingerprint bumps automatically.

1. The rule makes the planner drop photos it can't place → the existing "leave out only what is
   never talked about" rule stays; nothing new is left out.
2. The rule fights the order rule ("list in the order spoken") → it only says which sentence,
   the order still follows the talk.
3. Free replay evals can't prove the semantic change (recorded responses) → proven only by a
   live run (paid, needs Yasin's OK) or the post-merge phone run; stated as pending.

## C. Asked rhythm vs real holds (honest receipt)

Fix: the planner keeps `cadence_s` when the creator names a rhythm even with spoken phrases; the
worker records the longest background window (output seconds) and the asked cadence on the lane
receipt; the finished-video receipt says, in EN/TR, when a photo stays clearly longer than asked
and why (too few photos, none repeats), status "partial".

1. No rhythm asked → no `cadence_s` → no sentence (default 4 s is not an ask).
2. A small overshoot from word alignment (4.6 s vs 4 s) → only flagged above cadence + 1 s
   ("3-5 s" = 4 → over 5 s).
3. The last window holds to the end after the final mention → counts (that is what the viewer
   sees).
4. Longest measured in source time across a speech cut → measured in OUTPUT (kept) time from the
   layout.
5. A render recorded before this change (no keys) → no claim either way.
6. A colour fill or a single photo → the existing "never changes" branches win first.
7. Draft (before render) → windows unknown; no claim.

## D. "Small" captions

Fix: `CAPTION_ASK_SIZE_PX["small"]` 52 → 42 (her reels: 40–52 px OCR line ≈ 31–40 px font; the
readable floor is 40 px). Default and "large" unchanged.

1. Below the readable floor → 42 ≥ 40.
2. A size test pinned to 52 → updated with the reason.
3. Older iPhone builds → they draw the px the server sends; no protocol change.

## Checks

- Unit tests per failure mode above (A, C), negative controls: a real hook containing
  "recording", a mid-take slate, a slate glued to the next sentence.
- Offline replay of this thread's words through the cleanup plan (slate cut at 0–1.23 s) and of
  its strategy through `lay_out` + receipt (rhythm sentence appears).
- `ruff`, focused pytest, `make verify-kria`, `bash scripts/preship-check.sh`.
- Pending, needs a phone run after merge (and Yasin's OK for any paid run): planner places the
  collab post on the partnership sentence; S4 Turkish 2-minute run.
