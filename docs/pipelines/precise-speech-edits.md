# Precise speech edits and experimental engine-noise reduction

## Root cause and scope

The phone Talking path previously grounded its opening cut in word timestamps,
but did not resolve an approved request to remove one occurrence of a repeated
phrase into that same cut plan. Lowering original-audio volume also lowers the
voice: it cannot independently reduce an engine mixed into the recording.

The implementation covers single-source phone Talking edits. It transcribes the
full source when an explicit speech-removal request needs it, resolves the
requested occurrence against original word timestamps, and combines that removal
with the existing keep plan. Captions and video use the same resulting timeline.
Editor saves retain the cut. Native editor previews use the finished export for
applied explicit cuts or noise processing, because the live source-preview compiler
does not yet hydrate the pinned audio fades and processed track. Very short kept-side fades prevent native audio
crossfades from borrowing the removed word back into the result.

Matching ignores case and punctuation but preserves accents and numeric wording.
Repeated phrases without an occurrence, overlapping speech, invalid timestamps,
uncertain transcripts, and unsupported multi-source requests are not guessed.
On-screen text removal does not authorize deletion of spoken audio. Receipts make
failed or unsupported requests visible to brief checks rather than claiming success.

## Request contract

The server acts only on structured brief facts (KRI-667 language policy: no
regex reading of the creator's words). Main Creator v76 / Brief Extractor v21
record them:

- `{"speech_remove":{"quote":"Neredesin","occurrence":2}}` on the named-cut row
  (beside its KRI-792 `cut_lines`) when the creator says which time a quoted
  phrase is said. The quote and a 1-based occurrence are both required; a
  malformed fact authorizes nothing. Kinds `timing`, `select` or `audio`; title,
  per-clip and text rows never authorize a source cut.
- `{"noise_reduction":"engine"}` on an `audio` requirement for an engine/motor
  noise-reduction ask.

A brief stored before these facts existed (for example prod T1's r2/r5, v1 rows
with descriptions only) authorizes no source cut or filter; it keeps the KRI-705
"A Talking video can't do this" receipt. Re-send the request after deploy. The
draft-time receipts ("Will locate the requested occurrence ...", the noise plan
and the noise limits) are format limits, so they never hold a first draft behind
"a simpler version?" (guard: the `brief-facts` case of
`test_kri705_phone_talking_t1_brief_names_all_seven_asks_and_promises_no_cut`).

Prompt for the planner:

> Analyze the entire source speech before choosing an edit. For “remove the second
> Neredesin,” identify the second exact spoken occurrence in source order, retain
> the first occurrence and nearby reply, and remove only its verified word span.
> Keep video, audio, captions, and later editor saves on one timeline. Do not invent
> timestamps or substitute title deletion for speech deletion. If the occurrence
> or boundaries cannot be verified, report that limitation. Treat engine-noise
> reduction as a separate audio operation and preserve the voice; never satisfy it
> by lowering the volume of the entire recording.

## Experimental audio processing

`PHONE_ENGINE_NOISE_REDUCTION_ENABLED` defaults to false. Enabling requires a local
ARNNDN model path and its SHA-256 via `PHONE_ENGINE_NOISE_MODEL_PATH` and
`PHONE_ENGINE_NOISE_MODEL_SHA256`, plus verified `processedAudio` and `audioMix`
capabilities. No model is downloaded at runtime. Basic `afftdn` is reference-only,
not a fallback for creator engine-noise requests.

The worker processes source analysis audio. It measures filter latency with
impulses, pads and trims to compensate, and rejects unsupported source offsets or
changed timing. An immutable private receipt binds the resulting audio to the
job, variant, render generation, source generation, storage generation and hash.
The phone receives an opaque asset identity, not a storage path. Processed audio
follows the exact source windows and fades of the original video. Protocol 15 is
required for recipes carrying processed audio. Editor saves restore an existing
processed track and refuse a missing receipt instead of silently restoring noise.

This is noise suppression, not guaranteed engine/speech separation. The supplied
boat sample demonstrated negligible benefit from afftdn. ARNNDN reduced background
energy but also changed speech energy; that is not evidence of intelligibility.
The aligned derivative retains the original 28-second duration. Keep the flag off
until listening review confirms the quiet reply, speech naturalness, and lip sync.
The downloaded experimental model and private media are not repository artifacts.

## Verification

Regression coverage includes the saved boat transcript (second occurrence
9.22–9.70 seconds), preserved first occurrence and quiet reply, ambiguous requests,
distinct Turkish words, caption timing, worker publication, optional-effect
failure, editor saves, and kept-side audio guards. Native crossfade tests pass.
Existing render-asset and creator-contract suites pass.

Timeline unit gate: 14 suites / 209 tests passed. The browser portion could not
complete because the local build ran out of disk space; this is not a full browser
gate pass. No production deployment or live model evaluation was performed.
