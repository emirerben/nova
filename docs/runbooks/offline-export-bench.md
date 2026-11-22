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

## Public-footage interim baseline

Three authored requests in English, Turkish and German exercise the plain phone montage planner and exporter on six archival films. The fourth export deliberately reverses the two shots in the English request. The fixture includes the request, typed order, expected shot names, original Wikimedia Commons links, rights labels and source hashes. Each input is the first two seconds of a film, padded to portrait with its full picture visible.

From a bootstrapped checkout, pass a dash to download the six pinned originals into the new output directory. For offline runs, replace the dash with a directory of cached originals named `<source key>.ogv`. The runner checks every hash before using its source.

```sh
bash scripts/kria/run-archival-bench.sh "$SIMULATOR_UDID" - /private/tmp/nova-archive-run
```

The command saves the compiled plan, device recipe, native MP4, sampled source matches, run time and provider cost. It compares three frames in each requested two-second interval to all six originals. A passing sample needs both the expected nearest source and a mean absolute brightness error of at most 15 on a 0–255 scale. The reversed control must retain its duration and match the wrong source in all six samples. To rescore existing exports, run `python3 scripts/kria/archival_bench.py score "$RAW_DIRECTORY" "$OUTPUT_DIRECTORY"`.

The planner receives authored order indices. This baseline measures shot order and duration from the typed brief through native export. Free-text interpretation, editorial ratings and performance on historical creator incidents remain unmeasured.

The [recorded interim score sheet](../reviews/kri-1426/archival-baseline.json) covers Nova main at `781bd6701f` with the benchmark harness added. The run downloaded all six originals and took 52 seconds with cached build products and cost USD 0 in model calls. All 24 requested samples matched. All six samples from the reversed control matched the opposite shot, and both versions lasted four seconds.

## Compare independently rated edits

Use an explicit list of evaluation-cleared final MP4s. The pack may contain any number of distinct exports. Prepare it once with the API Python and a new local directory.

```sh
src/apps/api/.venv/bin/python scripts/kria/calibrate_export_pack.py prepare review-pack evaluation-exports/*.mp4
```

The local inventory maps each filename to its SHA-256. Give the human reviewer and grader separate copies of their blank CSV. Each rates the same exports independently on the fifteen edit-feedback dimensions. Leave unreviewed cells empty. Put the completed `human.csv` and `grader.csv` back in the pack and run:

```sh
src/apps/api/.venv/bin/python scripts/kria/calibrate_export_pack.py compare review-pack > agreement.json
```

The command rechecks every file hash and reports the number of paired ratings per dimension, the weakest measured dimensions, and the existing overall agreement gate. It makes no model calls. A successful command means the comparison ran. Read `agreement_gate_passed` for its verdict. A pack filled with synthetic labels tests the calculation only.
