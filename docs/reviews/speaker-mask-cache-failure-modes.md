# Speaker mask cache (Talk-to-camera Save) — failure modes (written before the code)

Problem: an editor Save of a Talking edit with a speaker background re-runs Vision person
segmentation on every frame, even when only text or captions changed (about 52 s for a 60 s video on
an iPhone 13 Pro, `speaker-background-failure-modes.md` S14). The matte's input is the speaker layer
alone, so a text-only edit hands Vision the same pixels it saw last time.

Phase 1 (this change) is the content path only: a disk cache of Vision's answer, keyed by the exact
pixels handed to Vision, behind a default-off switch. Not in phase 1: a trace fast path that skips
rendering the Vision input on a hit, and a pre-warm at Use take. The `mask-cache-design.md` review
document this was specified from was not available to the implementer; the rows below are rebuilt
from the task statement and extended.

## Decisions that remove whole classes of bugs

- **Key on the pixels, not on a name.** The key is the SHA-256 of the exact bytes handed to Vision,
  plus everything else that decides Vision's answer (request revision, quality, input dimensions and
  pixel format, input longest side, OS build, `hw.machine`). A source that changed, a clip with the
  same name, a new transform or a new speaker crop all change the pixels, so they cannot hit a stale
  entry. Nothing is looked up by clip id, asset id or time.
- **The cache sits below the matte.** It wraps the segmenter (`NativePersonSegmenting`), under
  `SpeakerMatte`'s coverage gate, 0.5 s hold, clip-key reset and trace. A hit hands the matte the same
  `CVPixelBuffer` bytes Vision would have, so every one of those behaves identically on a hit.
- **Off means the old path, not a disabled cache.** No store installed (the default) makes
  `SpeakerMatte` take the segmenter the factory returns, the same object as before. Nothing is read
  or written.
- **A miss returns Vision's own buffer.** Only the stored copy goes through compression; a miss
  never returns a round-tripped copy, so the first render of any edit is byte-identical to today.

## Failure modes

| # | Failure | Treatment | Pinned by |
|---|---|---|---|
| M1 | Stale mask from a different source with the same name or clip id | The key hashes the pixels; names are never part of it. The pack directory (sorted SHA-256 of the speaker sources) only groups entries for eviction. | `SpeakerMaskCacheTests.testDifferentPixelsNeverHitAnEarlierEntry` |
| M2 | Poisoning by a failed segmentation (nil buffer, thrown error, wrong pixel format, absurd size) | Only a non-nil `OneComponent8` buffer of sane size is stored, after Vision returned it. A throw or nil is passed up untouched so the matte's hold logic sees it as today, and nothing is written. | `testAFailedSegmentationIsNeverStored` |
| M3 | Concurrent export and preview writers to one pack | One file per key, written to a unique `.partial` name and renamed. A reader sees no file or a whole file, never a mix; two writers of one key write identical bytes (the key determines the answer), so the last rename is harmless. No shared in-memory index to corrupt. | `testConcurrentWritersOfOneKeyLeaveOneValidEntry` |
| M4 | Partial write when the app is killed or the disk fills | Rename-after-write leaves at most a `.partial`, which is never read and is swept at launch. A zero-length or truncated file that survives a power cut fails the header, length and 16-byte content digest checks, counts as a miss, is deleted and is rewritten. | `testACorruptOrTruncatedEntryIsAMissAndIsRepaired`, `testStalePartialFilesAreSweptAtLaunch` |
| M5 | Disk growth | Hard cap (default 512 MB) on the store. Entries are lzfse-compressed (a soft 8-bit mask is mostly flat). Eviction runs at launch and after every 16 MB written: stale-environment directories first, then least-recently-opened packs, never one opened within 60 s; a single pack still over the cap loses its oldest entries. A write queue longer than 128 drops writes instead of growing memory. | `testEvictionKeepsTheStoreUnderItsCapAndTheNewestPack`, `testOneOversizedPackLosesItsOldestEntries` |
| M6 | OS update changes Vision's output | The OS build, request revision and `hw.machine` are in the key and name the environment directory. Launch removes every other environment directory, so an update neither serves old masks nor leaves them on disk. | `testAnotherEnvironmentNeverHitsAndIsRemoved` |
| M7 | The 0.5 s held-mask path, coverage gate and clip reset change on a hit | The cache is below all of them and returns the identical buffer; the matte cannot tell a hit from a Vision call. A failed Vision call is not cached, so the hold path still runs on the next render of that frame exactly as before. | `testTheMatteSeesNoDifferenceBetweenColdAndWarm` (hold, coverage and trace counts equal on a cold and a warm run), `SpeakerMaskCacheParityTests.testCachedRenderMatchesUncachedFramesAndIsServedFromTheCache` (real Vision, real export) |
| M8 | Hit differs from Vision by compression or row padding | lzfse is lossless; the stored payload is the tightly packed mask and the rebuilt buffer has the same width, height and pixel format. The matte reads rows by `bytesPerRow`, so padding is irrelevant. Entry round-trip is compared byte for byte. | `testAHitReturnsTheBytesVisionAnswered` |
| M9 | Vision is not deterministic, so a cache hit can differ from a fresh run | Not assumed away. The cache's own property is exact and asserted: a warm render equals the cold render that filled the cache. Against no cache, the Mac parity test reports the uncached-vs-uncached noise floor and the cached-vs-uncached delta without asserting them, because Vision's run-to-run noise is not the cache's. The iPhone number is a device check. | `SpeakerMaskCacheParityTests.testCachedRenderMatchesUncachedFramesAndIsServedFromTheCache` (prints the `MASKCACHE mac noise floor`, `replay` and `parity` lines) |
| M10 | Cache hides a Vision regression or serves wrong masks after a code change | The key carries a format version; any change to the Vision input render (`inputMaxSide`, quality, pixel format) changes the key through the config. A change to how the input is rendered changes the pixels. Bumping `formatVersion` drops everything. | covered by M1 and M6 |
| M11 | Privacy: masks are derived from a person's video | Stored under `Caches` (not backed up, reclaimable by iOS), beside the proxies; removed by the same eviction, never uploaded. A mask is a silhouette of one frame, no pixels. | review |
| M12 | A second process or extension on the same root | Rename is atomic on one volume; eviction tolerates a vanished file; a read that loses the race is a miss. No lock files. | covered by M3 |
| M13 | Hashing every frame costs more than the cache saves | The Vision input must still be rendered and hashed on a hit (the phase-2 trace fast path removes that). Measured on the Mac against a miss and reported; phone cost is a device check. | `SpeakerMaskCacheParityTests.testPerFrameCostBreakdown`, and the speed line of `testCachedRenderMatchesUncachedFramesAndIsServedFromTheCache` |
| M14 | Flag on, store unwritable (read-only volume, full disk) | Creating the directory or writing fails closed: the segmenter wrapper is not installed for that render (no pack), or writes are dropped; Vision still answers. Never an export failure. | `testAnUnwritableRootFallsBackToVision` |
| M15 | Speaker sources with no known fingerprint (recipe without a manifest entry), or no store installed (the default), or a segmenter that is not Vision | No pack name or no store: `SpeakerMaskCaching.wrap` returns the same segmenter object as before, so nothing is read or written. Never a guessed name. | `testARecipeWithoutAFingerprintGetsNoCache`, `testOffAndUnknownFootageKeepTheOldSegmenter` |

## Switch and rollout

- Wiring: `SpeakerMaskCacheSwitch.installIfEnabled()` runs once in `KriaApp.init`. It installs
  `SpeakerMaskStore.installed` (under `Caches/KriaSpeakerMasks`) only when the user default
  `kria.speakerMaskCache` is true; the default is false. For a measurement, launch with
  `-kria.speakerMaskCache YES`. `SpeakerMaskCaching.wrap` is called where `Composition.swift` builds the
  `SpeakerMatte`, so every composition (player, exporter, image generator) shares one decorated segmenter.
- Off is the old path: no store means `wrap` returns the segmenter the factory made, the same object.
- The switch stays off until a phone has measured a text-only Save before and after, and the cache size
  per minute of footage (see the device checks in the change's hand-off).

## Follow-ups (not in this change)

- Trace fast path: record per-frame input digests in a trace so a text-only Save skips rendering
  the Vision input entirely.
- Pre-warm the pack at Use take, off the Save path.
- Delete a pack when its source leaves the project.
