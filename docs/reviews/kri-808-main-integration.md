# KRI-808 source review and current-main integration

Original author source 212e7bda82997e4c844deb20feec99865152449b is preserved by normal merge from fresh origin/main 7eaf217cb15bc8ebd8d2fea552bbcfe3ee7b0fea. No production behavior, assertion or rollout default was changed during this refresh.

## Independently checked source-language evidence

The ticket's English expectations for M1_09 and M1_08 conflict with the source audio. The incident plan item's existing debug/proposal records identify the exact uploaded analysis proxies. Read-only configured storage access retrieved their recorded object generations, not replacement uploads:

| Clip | Object generation | Bytes | SHA256 |
|---|---|---|---|
| M1_09 | 1792610494082781 | 288263 | ec4337ff09e3b6b2fcb6c0e91ff2928b9178339c5bdf5e3522ce1b353b892e59 |
| M1_08 | 1792610494585198 | 302795 | 35e34f4b5b1b3d66d999a6459c32c92a17722287adf9cbbd7692d321b0fe68cc |

These are 2.5s/2.7s incident analysis proxies with AAC audio, rather than high-resolution phone originals. The iOS proxy generator exports the source audio without transcription/translation; its contract checks duration and audio-presence parity. FFmpeg extracted the full audio without trimming or speed changes for local playback. Storage paths, signed URLs, raw admin prompts and media were not added to git.

This assistant session cannot process audio input. Emir directly played the two extracted tracks and confirmed: “m1_09 is gracias, spanish. the other one is also spanish”. This supports the literal Gracias and Spanish for M1_09, and Spanish language for M1_08. The exact full M1_08 wording remains unconfirmed. Other fixture words remain recorded-provider hypotheses, not newly human-transcribed truth. File ownership was not inferred; nobody was contacted.

## Review and limits

The first Whisper pass is compared to an existing independent Gemini transcript. A clear mismatch can cause at most two candidate-language retries, booked to the creator's optional-background spend. Agreement costs no extra call. A retry must improve token agreement and reach the half-token threshold. Failures are contained; no confirmed retry means no suspect timed words. Loop/rate filters also clean stored transcript text on read. Kill switch retains the original first-pass path.

This is a heuristic partial repair, not proof of speech recognition accuracy. Two models can agree on wrong words; 50% overlap can admit partial mistakes. The retry language stored in speech.language is the checked decoder hint: e.g. the fixture can preserve Gracias under an English hint. It does not prove the clip's actual language is English. No claim that this fixes all language metadata is made. Existing stored segment rows are not automatically re-analyzed, and non-Latin quoted-phrase grounding remains a known gap.

The original free whisper.cpp medium retries stand in for whisper-1 transport. They are not live whisper-1 output. No new model call, upload, sharing, deployment or phone operation was performed. The source fixture's English translation is not used to override actual Spanish speech. Full Don Julio request fulfillment and actual native export remain open under KRI-802.

## Verification

Initial focused recorded-provider and quality/segment checks: 50 passed, one explicitly skipped local-medium kill-switch case. The full task/persistence and neighboring render-path results, preship and exact-head hosted CI must be recorded separately before merge.
