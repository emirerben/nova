# Independent source review and exact retained-input evidence

## Independent review and exact retained-input replay

Candidate6296b33b exports the exact retained unnormalized synthetic Talking original with its complete revision1 recipe, font and all nine captions. The25.788333s output fully decodes; its opening frame covers the21.029ms empty video head, sampled source pictures match their presentation timestamps, and speech has zero measured whole-track delay. All nine caption midpoint samples were visually inspected. This is local macOS engine proof; physical TestFlight and app Save/reopen/retry/relink remain open. Prior restricted-context export failures and the corrected raw-seek measurement are retained in the private replay bundle. No pre-fix exact-asset causal comparison ran.

Root source review found no actionable defect in this bounded head-gap admission change. Clips are built in timeline order and tracks reused only after their prior slot ends; using the actual inserted track end preserves the held-head clock. Deleted originals retain the real asset-ID error. Current-main12d integration is clean, package bytes equal the reviewed candidate, and prospective treecfb9a4dc0fddb8e257b12af9b70291cb20b776fb matches the isolated validation tree. Current exact-head hosted checks are still required.

Local evidence: /Users/emirerben/.codex/visualizations/2026/10/21/01a15ecf-f035-7970-b4d4-96ad1e34ddd7/troya-stress-test/kri811-handoff/replay-6296b33b/approved-context-replay/README.md


## Retained QA owner report

The following records the existing Troya QA owner's completed local check, independently read by root. Root inspected all nine caption contact-sheet samples; root did not repeat the export, use the phone, or execute a paid/model call. The author's prior321 engine checks/6 explicit existing skips are component evidence; hosted checks must pass on the final source head.

# KRI-811 exact original + full recipe: local engine export passed

**Candidate `6296b33b1f825a21ab775a43c3db3d39065ad027` exported the exact retained unnormalized synthetic Talking original with its complete captured revision-1 recipe.** This was one separately approved full-export process after the exact first-caption prerequisite painted in the same approved execution context.

The video-offset incident now has concrete local-engine fulfillment evidence. This is **macOS engine proof, not physical TestFlight or app retry/reopen/relink acceptance**, and does not authorize merge or deployment.

## Request and source fidelity

- Exact original SHA-256: `b4d825ce0b381f2a66385815d77d51322de3f63e3b2d3871532840234e644ef2`;6,568,984bytes. No normalized/control/proxy substitution.
- Exact font SHA-256: `3133e71e9ef5de172c374f43daa0c10a5609b99f2fb78a03ff0622ca58b7c36f`.
- Captured native recipe digest: `430fe45a0118deb8b126f65981626f21217924d56912e16ada32ae8b3172aa17`;revision1.
- Recipe file SHA-256: `c1ca20ac1e1542361520bdb1f2b3108893eb6643215be7138d0c8f614dd680e5`. All9caption layers, source timing and audio settings retained.
- All290 tracked package/engine/test/resource files in the export snapshot were byte-checked against the pinned candidate. Export used the **uninstrumented engine** and existing ModelExport runner; runner adaptation is only the `animatedText` capability declaration. The runner uses branding `.none` for diagnostic export.

## Actual output and verification

- `export.mp4`: H.264/AAC,1080×1920,30fps; **25.788333seconds**, versus25.788requested. Full file decodes.
- First output frame begins at0; next at0.033333s. Its source picture matches the first real original frame, filling the original's **21.029ms** empty video head. At30fps there is no separate encoded21msframe; the0..33.333msfirst frame covers the gap.
- Source pictures at0,5,12,21seconds match timestamp-aware reference frames. Central-region RGB mean errors are2.365,2.099,1.940,2.041, all below the declared15threshold.
- Speech waveform correlation: **0.9743**, measured whole-audio delay **0seconds** at16kHzsample resolution. Three speech-section correlations are0.9996/0.9902/0.9825; measured section delays0/0/−0.0000625seconds. No21msslide was observed.
- Source audio is mono; output is stereo. Each output channel independently correlates0.9743with the source at0delay; gain≈0.9734, peak0.36214versus source0.36228. The earlier mono-downmix gain included stereo folding and is not evidence of added volume.
- All9cue midpoint samples were visually inspected: wording is present/readable, with expected highlight state. `caption-contact.jpg` retains the samples. No independent ASR/OCR/lip-sync or complete word-timing judge ran.
- Output SHA-256: `cbf524af703246feb87d1b298fbfdd874410286fac95c26e073b80c395901139`;22,920,142bytes.

## Measurement correction retained

The first raw-seek comparison at12s exceeded MAE15because FFmpeg seeking picked the next source frame at12.021029s. The original's frame actually visible at12s is11.987695s. Comparing that presentation-clock frame yields MAE1.940with the same threshold. `verification-initial-raw-seek.json` preserves the original result; `presentation-clock-verification.json` records the correction. Media, recipe and threshold were unchanged. The opening-frame metadata read interval was enlarged from0.12to0.5s to include reordered decoded frames; no export was repeated for verification.

## Execution context and limits

Both actions went through separate normal tool approval review. The earlier restricted-process failures remain untouched in sibling directories. The same caption succeeds and exact export completes in the approved context; the precise OS mechanism is not isolated. No software fallback, graphics-option change or style simplification was used for this successful export.

No physical phone, simulator, original app/project/draft, source implementation, PR, merge/deploy, security setting, model/paid call or network media sharing changed. Candidate checkout remained clean at the pinned head. No pre-fix exact-asset comparison was run, so this is current-candidate fulfillment evidence rather than isolated causal proof of the diff.

Use `summary.json`, `verification.json`, export state/results, preflight source hashes, ffprobe metadata, verification scripts and contact sheets for review. Physical-device/app-state checks remain separate acceptance work owned by the reviewer/source owner.
