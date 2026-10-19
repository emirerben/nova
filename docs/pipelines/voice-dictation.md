# Voice dictation (KRI-742)

Creators talk to Kria instead of typing. A mic button sits in every chat composer on iOS: the creation
chat, the editor copilot, the slide-post AI sheet and the Review sheet.

## How it works

1. **Live words.** `DictationController` (`src/apps/ios/Kria/Core/DictationController.swift`) taps the
   microphone with `AVAudioEngine` and feeds Apple's on-device `SFSpeechRecognizer`; partial text is
   written into the composer as the creator speaks. The same buffers are written to an AAC file.
2. **Final text.** On stop the file is posted to `POST /dictation/transcribe`
   (`src/apps/api/app/routes/dictation.py`). Whisper (`transcribe_whisper`) returns text that handles
   mixed Turkish/English; its `prompt` carries the creator's own clip/song/project names
   (`context_terms`). That text replaces the live words.
3. **Never lose words.** A failed or slow server pass keeps Apple's live words. A typed prefix is always
   kept; dictated words are appended to it.

## Invariants

- **Silence never becomes text** (KRI-653 class). The route returns `{"text": "", "reason": "no_speech"}`
  when every segment has `no_speech_prob >= 0.6`, or when Whisper only echoes the vocabulary prompt.
- **Audio is a throwaway.** It lives in a request-scoped temp file; nothing is stored or logged beyond
  `dictation_transcribed` / `dictation_no_speech` (user id, duration, character count).
- **Audio session hygiene.** Capture claims the shared session through `AudioSessionArbiter`
  (`claimCapture("dictation")`) and posts `.kriaAudioCaptureWillStart`, like the voiceover recorder.
- Limits: 3 MB / 120 s upload, 30 requests/minute per user, 12 s Whisper timeout, 110 s client cap.

## Flag

`VOICE_DICTATION_ENABLED` (default `true`, kill switch only). Off: the route 404s and `/capabilities`
no longer advertises `voice_dictation`, so the app hides the mic. The capability also needs
`OPENAI_API_KEY` (or the local Whisper backend). Apply: `fly secrets set VOICE_DICTATION_ENABLED=false -a nova-video` (API) + restart.
No protocol bump: older builds simply never show the mic.

## Tests and evidence

- API: `src/apps/api/tests/routes/test_dictation.py` (flag, limits, silence, timeout, temp-file cleanup,
  vocabulary prompt).
- iOS unit: `DictationControllerTests` (state machine with fake engine/transcriber).
- iOS UI: `CreationUITests/testDictationShowsLiveWordsThenServerTextAndIsSendable` (scripted engine via
  `KRIA_VOICE_DICTATION=1`; a simulator has no usable microphone).
- Accuracy: `src/apps/api/scripts/dictation_eval.py` scores real phone recordings (WER for Apple-only vs
  Whisper, names/numbers hit rate). Pass bar: shipped WER <= 10% and below Apple-only, names + numbers
  right in >= 90% of samples.
- Usage: compare turns per user for accounts with `dictation_transcribed` logs against the rest.

Follow-up: web composer dictation (KRI-743) reuses this route.
