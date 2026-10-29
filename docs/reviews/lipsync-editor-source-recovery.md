# Saved guided-story source preview recovery

Parent incident: KRI-934. KRI-507 supplies the neighboring constraint: text-only changes on a song/lip-sync creation must preserve its guided path and timing; this change does not route edits through a different renderer or allow locked clip edits.

## Observed incident and claim

TestFlight 1789 (binary source a9aed88eda7202eb81f92fd5fc85d2965b0b35df) restored original-project playback and stopped the plan/status oscillation on the user's iPhone. Its editor still showed only the outro and a permanent missingVideoTrack fallback. The original media/document was left unchanged.

This follow-up corrects two independently reproduced read-projection defects. After explicit user approval, one targeted production read in a forced read-only transaction confirmed that the original saved version-2 draft omits timeline_slots and matches the render generation. Replaying that exact draft against the existing diagnostic projection produced zero cuts with the baseline wrapper and four cuts with the correction; the explicit-clear control kept zero. The draft and job inputs remained unchanged. This confirms the original read-projection defect, but does not establish that the original iPhone editor is repaired. Its actual source resolution, song fidelity and reopen remain rollout checks.

## Read ownership

A saved guided-story draft may own text without owning clip cuts. An omitted timeline_slots section inherits the rendered source cut; an explicit empty list remains a creator clear. The API previously converted both cases into an empty cut. The native full-snapshot shortcut also discarded the rendered cut when its saved section was absent. Finally, the native client only requested guided cuts when clip editing was enabled, although locked cuts still need source preview.

The API now replaces rendered slots only when the saved section exists. The native client fetches the guided read projection independently of editing capability, with its existing generation agreement/retry/conflict checks. Native decoding derives an in-memory timeline only for an absent section and matching saved/render generation. Saved lanes, intentional emptiness, capabilities, immutable snapshot and hash remain authoritative. No stored draft or media changes.

## Case matrix and evidence

| Case | Expectation | Evidence |
| --- | --- | --- |
| Saved text draft omits timeline | Preserve rendered cuts and saved text | API red replay failed before correction; native staging red replay decoded zero instead of three clips |
| Explicit empty timeline | Keep zero clips | API and native negative controls pass |
| Guided timeline locked | Read cuts while retaining edit prohibition | Transport red replay failed before correction; fixed read passes |
| Render generation changes between reads | Re-read or conflict; never mix generations | Existing native contract tests pass |
| Real source preview from sparse saved draft | Compile actual bound media and saved title, without finished-video fallback | AVFoundation acceptance passes; generated composition frame inspected |
| Exact original saved draft | Preserve four rendered cuts; an explicit clear stays empty | Approved read-only capture and offline baseline/fixed replay pass |
| Original iPhone project with song | True source preview, playback and stable reopen, unchanged timing | Pending follow-up rollout |

Checks from the task worktree:

- API empty-editor projection and native-source suites: 31 passed; scoped Ruff passed.
- KriaTests and ChatDraftStagingTests: 130 executed, one standard Keychain skip, no failures.
- NativeEditorSessionTests/testSavedGuidedTextDraftBuildsRealSourcePreviewUnlessCutsWereExplicitlyCleared: one passed, no skips. Uses the real importer, checksum-bound local source store, decoder, session, resolver, compiler and AVFoundation composition; provider transport alone is replayed. The explicit-clear control refuses source resurrection.
- git diff --check passed.

Local artifacts are retained in .dev/sparse-draft-{before,after}.log, .dev/sparse-native-{before,partial,contracts}.xcresult and .dev/sparse-native-preview-run.xcresult. Its retained screenshot shows the bundled Alberobello source picture under the saved title. These fixture artifacts establish native composition, not original song/lip-sync fidelity or physical-device delivery.

The targeted saved-draft read was explicitly approved by the user after automatic approval review had rejected the initial attempt. It checked transaction_read_only=on, selected only the exact draft/thread, and rolled back. The private capture and replay remain outside Git. Admin job diagnostics omit private source bindings, so this capture does not independently prove source-pool availability on the iPhone.

No production database write, source replacement, saved edit, regeneration, deletion, deployment or release is part of this follow-up verification. The original incident remains open until the actual TestFlight client previews and reopens the original project correctly.
