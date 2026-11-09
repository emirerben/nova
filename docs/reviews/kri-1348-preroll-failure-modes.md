# KRI-1348 — off-script pre-roll at the start of a take: failure modes (written before the code)

Parent: KRI-1342 (Kria test "yorum3", 2026-11-09, thread `d9d7ad3d-dac3-40d0-9882-62f025c21bbc`, job
`7eba9903-2ceb-47ca-b1d8-f4922e6e95fa`, Fly `4f6ed3014`). The phone Talking video (Çiğdem "Yorumla"
format, take `IMG_7212`, 40.1 s raw, Speech cleanup consented) opens with "Okay, starting."
(0–1.38 s kept). Raw records: `silence_cut.removed` = 4 silences + 1 retake (16.08–22.78 s, the
"let me start that again" restart); the first keep segment is `[0.0, 1.385]`.

Two causes:

1. `SPEECH_CLEANUP_SLATE_CUT_ENABLED` (KRI-1326, PR #2230) is off on Fly.
2. Even on, `speech_slate.py` matches the whole first utterance against a closed list of
   phrases. "starting" is not on it, and neither is any phrasing nobody wrote down yet ("okay,
   are we good?", "testing, testing", "başlıyorum", "hadi başlayalım", "wait, the light… okay").

Earlier occurrences of the same class (all kept): Çiğdem S2 take `IMG_7231` "Okay, we're
recording." in Green 3 (2026-11-03, job `9bad2d75`), green4 (2026-11-04) and ss2b (2026-11-08,
job `f4f81c07`, KRI-1326). The kit scripts also mark S3 take A (`IMG_7244`) "Wait, the light…
okay." as CUT. No earlier incident cut a real first sentence; real-word cuts (KRI-970/978
provolone, KRI-1017 Plaza voice guard, KRI-236 edge trims) were mid-take pause/filler cuts, and
their guards are untouched here.

## The class and the rule

Pre-roll is talk ABOUT the recording, said before the content starts: recording/rolling,
starting/beginning, countdowns, mic/sound/camera/light checks, "is this on", "wait", "one
second", "let's go". It is never about the video's subject.

Instead of matching whole phrases, the detector classifies each WORD of the opening by role and
cuts the opening utterances only when every word in them has a production role:

| Role | Examples (EN / TR) | Effect |
|---|---|---|
| filler | okay, alright, so, um, well, yeah / tamam, evet, peki, şey, ee | allowed, never enough alone |
| glue | we, I, it, this, is, are, now, again, here, the, a; wait, ready, hold / biz, ben, bu, şimdi, mı; hazır, dur, bekle | allowed, never enough alone |
| production (strong) | recording, rolling, starting, start, begin, test, testing, check, mic, camera, sound, audio, light, focus, take / kayıt, kaydediyor, başlıyorum, başlıyoruz, çekiyor, çekim, ses, mikrofon, kamera, ışık, deneme, test | enough with a normal boundary |
| countdown | two or more number words (three two one / üç iki bir) | enough with a normal boundary |
| cue (weak) | go, let's, action, on ("is this on"), second, take / hadi, haydi, saniye, hazırız, mıyız | enough only with a clear fresh-start pause before the real talk |

New phrasings are new combinations of these roles and need no change; the table is data (word
classes), not sentences. A word the table does not know makes the utterance real speech.

## Failure modes and what prevents each

### False cuts of real speech (the expensive side: must never happen)

F1. **A real first sentence that opens with a filler** — "Okay, so here's the thing…", "Tamam,
bugün size…" → it contains words with no production role ("here's", "thing", "bugün") → kept.
The rule is "every word has a role", never "starts with a known word".

F2. **A real sentence that contains a production word** — "We're recording our first album
today", "Starting today I'm vegan", "Testing a new camera", "Kamera arkası…" → unknown content
words in the same utterance → kept.

F3. **A short real hook made only of cue words** — "Ready?", "Let's go!", "Here we go.",
"Action!", "Hadi!" said as a hook straight into the content → cue-only utterances need a clear
fresh start after them (a pause of at least 0.6 s by word gap or detected silence); a hook that
flows into the next line has none → kept. The KRI-1326 negative controls ("Ready? Apple did it
again…", "Let's go. Kervan turned ten…") stay green. Words addressed to the viewer ("you",
"siz", "mısınız") have no role at all, so "Are you ready?" / "Hazır mısınız?" stays even after a
dramatic pause.

F4. **Glued transcript** — "okay starting ten minutes after Lumo…" with no punctuation, gap or
silence: the opening utterance runs into the content and contains unknown words → kept. Same
boundary rule as KRI-1326 (punctuation, ≥ 0.2 s gap, or a silence starting inside the last word).

F5. **The video is about the production word** — a mic review opening "Mic check." then "This
mic costs…", a lighting tutorial opening "Light." → topic-able production nouns (mic, camera,
sound, audio, light, focus, test and their TR forms) count as production only when the rest of
the take never mentions the same thing; otherwise the opening is kept. Process words
(recording, starting, rolling, countdowns) are exempt from this check on purpose: yorum3 says
"let me start that again" later, and that must not save "Okay, starting.".

F6. **A countdown that is the hook** — "Five. Five things nobody tells you…" → a countdown needs
at least two number words; a single number has no production role.

F7. **Number words that are not a countdown** — Turkish "bir" is also "a/one": "Bir şey
söyleyeceğim" → "şey" is a filler but "söyleyeceğim" is unknown → kept.

F8. **Prefix collisions in Turkish stems** — "çekici" (attractive) vs "çekiyor", "sesli" vs
"ses", "başlangıç" → stems are matched with a minimum length and the whole-utterance rule
bounds the damage: a collision only matters if every other word in the utterance is also known.
Unit tests pin "Çekici bir kampanya." and "Sesli düşünüyorum." as kept.

F9. **Greetings and sign-ons** — "Hi guys!", "Selam!", "Hey, I'm Defne" are the creator's real
intro → greetings have no role → kept.

F10. **A slate in the middle of the take** — "…okay, we're recording again" → only utterances
that start at word 0 are read; the scan stops at the first real utterance.

F11. **The take is only pre-roll, or almost** — at least 3 words must remain after the cut.

F12. **A long mumbled preamble** — the pre-roll must end within 8 s and 14 words in total
(several short utterances: "Is this on? … Okay. Three, two, one."; the creator may sit silent
for 2–3 s after pressing record), each utterance at most 6 words and 3 s; otherwise nothing is
cut.

F13. **Words whisper made up over silence or noise at the start** ("Thank you.", "you", "Altyazı
M.K.") → not production words → kept as before (not this fix's job; never a false cut).

Added after the synthetic-opening replay (macOS `say` openings in front of real takes, transcribed
by whisper.cpp), before the corpus verdict:

F14. **Whisper splits the opening at every comma pause** ("Three," / "two," / "one." each its
own utterance; "Is this on?" / "Okay." with the pause after "Okay.") → the opening is read as
one running stretch: numbers add up across utterances, and a cue is settled by the pause right
before the real talk (a silence that runs up to the first real word, since whisper stretches the
stamps over it), not by the pause after the cue itself.

F15. **A repeated check word looks like the topic** ("Testing, testing, one, two.") → the topic
check (F5) compares against the words AFTER the whole pre-roll, not after each utterance.

F16. **Audience question with a real pause after it** ("Hazır mısınız? [0.9 s] Fenerbahçe…") was
cut by the first draft → the viewer-address rule in F3.

Added in self-review of the draft (before the corpus verdict):

F17. **A production word used as a noun** — "That's a record.", "It's a start.", "It's a roll."
(sushi), "Bu bir kayıt." are all role words → an article (a, an, the, bir) right before a
production word makes the utterance real. (Topic nouns are exempt: "Wait, the light… okay.")

F18. **Hooks and apologies that look like cues** — "Wait." / "Ready?" / "Dur!" open real hooks as
often as pre-roll, and "Sorry." / "I'm sorry." can open an apology video → "wait", "ready",
"hold", "hazır", "dur", "bekle" are glue (allowed, never an anchor) and "sorry"/"pardon" have no
role. "Wait, the light… okay." is still cut through "light"; "Hazır mıyız?" (are WE ready) still
counts through the first-person "mıyız".

### Missed pre-roll (the cheap side: acceptable, but measured)

M1. A pre-roll word not in the table → kept (as today). Logged in the corpus replay so the table
can grow from evidence.

M2. Cue-only pre-roll without a clear pause ("let's go" straight into content) → kept by F3.

M3. Pre-roll glued to content in the transcript → kept by F4.

### Plumbing

P1. **Flag off** → no span; plans, receipts and fingerprints byte-identical. The same flag
(`SPEECH_CLEANUP_SLATE_CUT_ENABLED`) is reused: it was never turned on anywhere, so widening the
rule behind it changes nothing live, and the rollout stays one switch.

P2. **Consent fence** → still a retake-kind span, still requires both retake switches
(`RETAKE_CUT_ENABLED`, `SPEECH_CLEANUP_RETAKES_ENABLED`), cut only on a consented cleanup.

P3. **Overlap with the LLM retake detector** → spans merge in the planner, as for KRI-1326.

P4. **Voice guard** → the span is passed as a retake span, which the guard exempts.

P5. **Malformed words** (empty text, missing times, end < start) → no span, never an exception.

P6. **Multi-clip takes** → each clip's own word 0 (same as KRI-1326).

P7. **Both callers** (preflight analysis and the render-time cut in `generative_build`) call the
same pure function with the same inputs; no caller change is needed.

## Checks

- Unit tests per failure mode, EN and TR, including every KRI-1326 test unchanged.
- Corpus replay (free: whisper.cpp large-v3-turbo stamps + the repo's own silence detector) over
  every speech take in the `~/Downloads/kria-*-test` kits, old rule vs new rule: every take with
  marked pre-roll is cut, no take without pre-roll is cut, and on cut takes the first kept word
  is the answer key's first real word.
- Negative control: the same takes with the pre-roll removed from the word list (the real first
  sentence becomes word 0) → nothing cut.
- `test_silence_cut*.py`, `test_speech_voice_guard*.py`, speech cleanup analysis tests, ruff.
