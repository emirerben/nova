# KRI-1348 — pre-roll cut: offline replay evidence

Evidence level: **offline replay** (free: whisper.cpp large-v3-turbo q5_0 word stamps, the repo's own
`detect_silences_with_status(min_silence_s=0.1, ambient_adaptive=True)`, no paid model, no device,
no deployment). Old = `speech_slate.py` on `origin/main` (ff332979f, KRI-1326 closed phrase list);
new = this branch. Failure modes: `kri-1348-preroll-failure-modes.md`. Expected openings:
`kri-1348-preroll-answer-key.json`. Media, transcripts and per-take JSON stay outside git.

## 1. Kit corpus (every speech take in `~/Downloads/kria-*-test`)

240 audio files with speech from 12 kits (boat 21, cabin 20, Çiğdem 31, Ecuador 19, football 3,
Don Julio 14 + 10 + 8, Leverkusen 20, Qendresa 41, stress 52, airdrop 1), EN / TR / ES / DE.
Rendered outputs and test mocks excluded; byte-identical files deduped; some takes still appear
as several sidecar copies (`.MOV` upload, `.mp4`, `.mp3`, `.wav`), counted per file below.

Truth: the only answer keys that mark pre-roll are the Çiğdem scripts (`SCRIPT.txt` CUT lines):
S1 `IMG_7212` "Okay... starting.", S2 `IMG_7231` "Okay, we're recording.", S3 take A `IMG_7244`
"Wait, the light... okay." (13 files incl. copies and the S1 raw-take control). Every other
file's opening was also read by hand: none holds unmarked pre-roll. The nearest case, Don Julio
julio3 "Just a sec. Yes, I'm ready. What is this, sir?", is a conversation with the waiter and its
answer key keeps it.

| | files | old rule cuts | new rule cuts |
|---|---|---|---|
| Opening marked CUT in the script | 13 | 3 (S2 copies only) | **13** |
| No pre-roll | 227 | 0 | **0** |

On every cut file the first kept word is the script's first real word ("Ten minutes after Lumo…",
"Kervan/Curvan celebrated…", "Why do brands do food marketing?").

Full chain (the REAL `analyze_speech_cleanup`, switch off vs on, retake switches on, no LLM retake
detector, filler re-timing off because the aligner model is not installed locally, real 16 kHz
audio so the voice guard runs): exactly those 13 plans change, each gains one `retake` removal
from 0 s (0.82–2.44 s), the first kept word is the first real word, and no word anywhere is
partly cut. The other 227 plans are identical.

## 2. Production transcripts (whisper-1, as prod stamped them)

| Take | Opening | Old | New |
|---|---|---|---|
| yorum3, job 7eba9903 (KRI-1342), `overlay_transcript` | "Okay, starting." | kept | cut 0–1.26 s, keeps "Ten minutes after…" |
| Green 3, job 9bad2d75 (`tests/fixtures/kri1176_green3_restart.json`) | "Okay, we're recording." | cut | cut 0–1.22 s, keeps "Curvan celebrated…" |

## 3. Negative control

Each of the 15 cut takes above with its pre-roll words removed (the real first sentence becomes
word 0, times shifted): 15 / 15 cut nothing.

## 4. Synthetic openings (unfamiliar wording, EN and TR)

macOS `say` (Samantha EN, Yelda TR) speaks an opening, then a pause (cut cases) or almost none
(hook cases), in front of a real take that starts on its real first sentence (Çiğdem S1 from
"Ten minutes…", S4 Turkish). Transcribed by whisper.cpp like the corpus.

| Should be cut | Old | New |
|---|---|---|
| Okay... starting. · Is this on? Okay. · Three, two, one. · Testing, testing, one, two. · Alright, let's get started. · Wait, the light... okay. · Are we recording? Okay. · Let's go. [1 s] | 2 / 8 | **8 / 8** |
| Tamam... başlıyorum. · Tamam, kayıttayız. · Hadi başlayalım. · Üç, iki, bir. | 1 / 4 | **4 / 4** |
| Çekiyor mu? Tamam. · Kayıt başladı mı? Evet. · Ses kontrol, bir, iki. | whisper heard none of them (auto and `-l tr`): nothing to cut; unit tests cover the words | |

| Must be kept (real first sentence / hook) | Old | New |
|---|---|---|
| Okay, so here's the thing. · Ready? Here's what happened. · Let's go! (no pause) · Starting today, I'm changing everything. · Tamam, bugün size bir şey anlatacağım. · Hazır mısınız? · Selam arkadaşlar! | 7 / 7 kept | **7 / 7 kept** |

The first draft cut "Hazır mısınız?" (an audience question followed by a real 0.9 s pause) and
missed the comma-split countdown and "Testing, testing"; those became F14–F16 and were fixed before
the corpus verdict.

Re-run 2026-11-10 after the F19 review fix (a glue-only opening line ends the scan): every
number above is unchanged.

## 5. Checks

- `tests/pipeline/test_speech_slate.py`: 77 passed (23 KRI-1326 unchanged + 54 new, one per
  failure mode, EN and TR, incl. the F19 review controls); `test_speech_slate_cleanup_e2e.py`: 8 passed.
- Speech cleanup suites (`test_silence_cut*`, `test_speech_cleanup*`, voice guard e2e, retake
  detector, Green 3 retake replay, worker contract, config): 784 passed.
- `make kria-replay FIXTURE=nermin-matcha-update`: passes.

## Not covered

- No device or production run: the switch is off on Fly. Rollout steps:
  `docs/runbooks/chat-speech-cleanup-rollout.md` ("Off-script opening").
- Words the table does not know stay (recall gap by design), e.g. "Kayıt aldı mı?".
- The brief is not consulted (the take itself is the topic source; the brief is not an input of
  the preflight analysis).
- whisper.cpp stands in for prod whisper-1; the two prod transcripts above are the only whisper-1
  evidence.
