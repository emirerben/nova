# Offline iPhone export benchmark

Run from a bootstrapped Nova checkout on a Mac with Xcode, XcodeGen, FFmpeg and an installed iPhone simulator. Supply its UDID and a new output directory. The seven synthetic cases use generated color fields, pictures and tones. The runner calls the phone recipe compilers and exports the results with the app's AVFoundation exporter. Inputs and outputs stay local.

```sh
bash scripts/kria/run-offline-export-bench.sh "$SIMULATOR_UDID" /private/tmp/nova-offline-exports
```

The runner uses the API virtualenv. Set `KRIA_BENCH_PYTHON` to reuse an installed API Python, `KRIA_BENCH_PACKAGE_CACHE` for an existing local Swift package checkout, and `KRIA_BENCH_BUILD_DIR` for a cached Xcode build. Use a dedicated simulator and build directory when running alongside other tests.

## Case inventory

| Fixture | Native behavior under test |
| --- | --- |
| narration | Voiceover over two montage clips |
| subtitled_sentence | Sentence captions, including the gap between cues |
| subtitled_word | Timed word highlights |
| subtitled_landscape_fit | Rotated landscape video, letterboxing and caption placement |
| subtitled_fullscreen_visuals | Still and video cutaways, edge coverage and retained speaker audio |
| talking_head | Speaker, muted cutaway and return to speaker |
| narrated | Two source clips under narration, slowed second clip, title and captions |

The app tests check the expected frames, audio and text windows relevant to each case. The independent FFmpeg score checks the exported canvas, AAC stream, duration and sampled colors. Its wrong-content control gives the sentence-caption case the word-caption movie. Both have the same duration and format. The source-color check must reject the substitution. This control measures a source mismatch on synthetic media.

Before rendering, the runner regenerates the five synthetic duration and word-retiming editor payloads and compares them byte for byte with the checked-in golden file. These five cases cover operation compilation.

The runner generates the media inputs twice. It compares source hashes, recipe hashes and sample expectations. The recipe comparison sorts the unordered capability set and preserves track and clip order. `scores.json` identifies the exported movies by SHA-256. The Xcode assertions and skips are in `native.xcresult`. The command succeeds only when the native test run and standalone scorer both succeed.

## Grader calibration

The fifteen editorial ratings stay empty until a reviewer or grader actually scores an export. To compare independently prepared reviews offline, give the scorer two CSVs. Each row has `video_sha256,dimension,rating`. Use the existing edit feedback dimensions and ratings. The hash must match a movie from this run. Each file may rate each dimension once per movie.

```sh
src/apps/api/.venv/bin/python scripts/kria/score_offline_exports.py \
  /private/tmp/nova-offline-exports/inputs /private/tmp/nova-offline-exports/repeated \
  /private/tmp/nova-offline-exports/comparison.json \
  --human-ratings human.csv --grader-ratings grader.csv
```

Each dimension reports exact matches among paired ratings. A disagreement about applicability counts as a mismatch. A pair where both reviewers marked the dimension absent is excluded. Overall gate calculations use decisive human verdicts. A mixed grader verdict abstains. Unreviewed exports contribute zero samples. Agreement on these fixtures measures the comparison procedure only. KRI-1427 requires the actual grader and Emir to review at least twenty decided edits.

KRI-1426 still requires 25 real incident footage sets, their original requests, a live planner run per case and scored finished edits. Plain montage, creator-song and chat-edit journeys have no rendered benchmark case in this suite. No case here measures request fulfillment, overclaims, or editorial improvement on real footage.
