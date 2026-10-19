# KRI-734 verification performance

## Acceptance before optimization

Benchmark the first simple-cut phone verifier on the production API machine
class (one shared CPU, 2 GiB, iad), in an isolated temporary Machine. Keep the
serving API/worker processes, database, approval state and rollout flags intact.
Record the production image revision and pending verifier source hashes.

Use the retained five-second native Gameday edit as the real-media case. Add
independently encoded, explicitly synthetic 5/30/60-second edits with changing
picture, different source audio, hard cuts and a static timed title. Fixture
creation and transfer are outside verification timing. Measure per-process CPU,
wall time and subprocess counts by decode/probe/OCR stage. Repeat matched runs
on the same hardware; distinguish cached local files from storage transfer and
from end-to-end user latency. A single-user isolated benchmark does not establish
production p95 or loaded-fleet performance.

Optimization must retain every sample time, picture threshold, OCR confidence,
text window/region, audio chunk/lag/gain threshold, duration check, source/attempt
fence and last-good publication rule. Do not shorten videos or reduce negative
controls to improve results. Unreadable/truncated output, a missing tool, timeout
or changed authority still blocks publication. Correct dark footage and title
boundaries remain paired with their wrong-output controls.

Before adopting a faster decoder, compare actual decoded pixels/PCM against the
baseline at its exact sample times, including fractional timestamps, source
windows, variable frame timing and display rotation. All existing actual-media
and completion negatives must continue to reject. Report partial/timeout results
honestly instead of treating incomplete checks as a fast successful verification.

## Failure behavior

A rejected or incomplete candidate is withheld from Ready/playback/Save/Share.
A replacement leaves the last accepted video, poster and URL intact. A first
export has no accepted video to keep and shows a failure with recovery instead.
The exact approved edit remains saved; an output-check retry needs the existing
approval/consent path, a fresh attempt and fresh measured evidence. Changed source
or approval authority requires a reviewed new edit. This slice offers recovery;
automatic bounded repair is the separate KRI-726 work.

## Results

Measured 2026-10-18 in iad using isolated Machines without services or jobs.
The deployed image was
`sha256:2064a66d7d16deb5c43c9b68e648ce9e1da7afb01fb6ecd9c94c20519d4c7a86`
(application revision `04d8c3606a42d44e88ab599b6fe1720246634691`). The pending
verifier was staged separately; the old implementation SHA-256 is
`39391cad7e656a9b9d4d908eebd3a8ddf51bad28894a78f85e2b1540bca39b78`.
The final verifier SHA-256 is
`92cfcffec10b07d60184fc456bbd3707d12dd2e26636db7f263e221b77bdced9`.
Tesseract English/Turkish packages from this PR were installed in the temporary
Machines: the deployed image does not yet contain them. Python 3.11.17,
FFmpeg 7.1.5 and Tesseract 5.5.0 were identical for both implementations.

### Production worker class: four performance CPUs, 8 GiB

Three runs per case, sequential optimized then baseline on the same final
otherwise idle Machine, with the same staged files. All 24 runs completed every
check (`checked`). Seconds below measure verification only; they exclude upload/download, queueing, module
imports and the phone export. This is a hardware comparison, **not** a change
to the production execution route, which still verifies in the API request.

| Edit | Before median (range), s | After median (range), s | Speedup | Picture samples retained |
| --- | --- | --- | --- | --- |
| Retained native 5 s + 1.6 s tail | 7.08 (6.94–7.17) | 5.78 (5.77–5.88) | 1.22× | 12 |
| Synthetic 5 s | 8.42 (8.41–8.47) | 4.65 (4.56–4.79) | 1.81× | 12 |
| Synthetic 30 s | 36.21 (36.01–36.74) | 10.06 (10.02–10.13) | 3.60× | 62 |
| Synthetic 60 s | 69.49 (68.87–69.65) | 17.49 (17.39–17.66) | 3.97× | 122 |

The 60-second case uses about 18.6 CPU-seconds instead of 158.7 (88% less).
Picture decoder processes, including OCR frame extraction, fall from 248 to 17;
the 122 source/output sample pairs, 60 audio checks and four text checks remain.
The native export retains exactly the same 13 measured check objects and file
checksum in the local before/after report. Longer fixtures are independently
encoded dynamic H.264/AAC test patterns with changing audio, two hard-cut source
windows and a timed title; they are not long native exports or original footage.
Their 1920×1080 canvas and default 250-frame GOP are recorded assumptions, not
claims about every phone encoder or 9:16 workload.

Raw redacted reports: [worker baseline](worker-baseline.json) and
[worker optimized](worker-optimized.json). They record each run's status, CPU and
stage times, subprocess counts, sample counts, tool versions and source hash.

### Production API class: one shared CPU, 2 GiB

Initial baseline medians were 8.53 s (native 5 s), 12.73 s (synthetic 5 s),
97.67 s (30 s) and 101.76 s (60 s). **All six longer runs were partial**, so
their elapsed times are censored failures, not completed verification timings.
The 90-second budget is checked between steps; a subprocess can run up to
20 seconds, so it is not a hard end-to-end deadline. Transfers are outside it.

Sustained measurements showed severe wall-time variability despite much lower
CPU work. An intermediate optimized native case took 5.55/5.65/34.38 s. A later
three-run [intermediate stress run](api-intermediate-stress.json) took
77.78/90.80/92.17 s for the native edit (two partial), and every 30/60-second
case remained partial. It preceded the final generic numeric-type rounding fix.
Final-source sanity checks, one run each, took **90.72 s** (native 5 s) and
**99.04 s** (synthetic 60 s), **both partial**. CPU work was 6.82/8.57 seconds,
respectively; wall time is not equivalent to CPU consumed.
That gap is consistent with shared-CPU scheduling limits; scheduler/steal counters
were not captured during those runs, so the exact cause is not independently proven.
Short early baseline bursts and later sustained runs do not establish a fair
speedup or a production p95. [Initial API baseline](api-baseline.json),
[final-source API outcomes](api-optimized.json).

**Rollout blocker:** do not enable the flag on the current shared-CPU API path.
Before enrollment, use a separately reviewed bounded background execution path
with dependable CPU capacity, then measure storage transfer, queue latency and
a fresh protocol-12 device canary. No worker routing, VM size, fleet setting or
rollout flag was changed here. Temporary benchmark compute is removed after
capturing evidence.

## What changed, with checks preserved

* Decode nearby picture samples together, in batches of at most 17 spanning at
  most eight seconds. This avoids seeking from a keyframe and launching FFmpeg
  separately for every sample. Raw output is bounded to 64 MiB per subprocess.
* Read integer stream timestamps and preserve FFmpeg's original stream-time-base
  rounding. Sparse frames, rotation, nonzero timestamps, later source windows and
  coarse/fine time bases have exact pixel/PCM parity controls. The original
  production float-offset normalization and the helper's scalar rounding remain
  intact. Decoder uncertainty
  falls back to the original independent seek path, within the existing budget.
* Decode each title region in one batch. Convert to RGB before the crop, keeping
  exactly the original Pillow crop pixels even at odd crop coordinates. Both raw
  and paint-color-isolated OCR views still run at every original timestamp.
* Bound frame decoder/filter threads; emit small raw sampled frames instead of
  PNG round trips. Audio decoding, sample schedule, comparisons, thresholds,
  publication fences and failure behavior are unchanged.

The first batch experiment incorrectly rounded fractional seeks upward; the
prewritten exact-pixel oracle caught it at 0.333334 s. The final implementation
uses nearest stream tick, including tie behavior. A subsequent fine-clock test
also caught converting a numpy scalar before rounding in the batch helper at
0.1000005 s. The helper now retains the original scalar rounding, and the
production caller retains its existing float-offset normalization before adding
source/timeline starts. Single-threading alone slowed
the local multicore case, so that experiment was not accepted as the solution.

## Reproduce without publication

Stage private fixture files and a JSON list whose cases contain `name`, `kind`,
`status` (saved request JSON path), `video`, `sources` (asset ID to local path),
and optional `brand_tail`. The operator supplies trusted private inputs; this
tool cannot authenticate receipt provenance or certify a live deployment.

```sh
cd src/apps/api
python -m app.cli.benchmark_device_export \
  --implementation app/services/actual_export_verification.py \
  --fixtures /private/tmp/benchmark-cases.json \
  --report /private/tmp/benchmark-report.json --repeat 3
```

Exit zero means the profiling run completed. It does **not** mean every case was
`checked`; inspect each status. Use `verify_device_export` for a single candidate
quality gate. No benchmark calls a model or publishes media.

Production FFmpeg parity also passed all six timing fixtures: 78 requested sampled
pictures, 78 exact RGB title crops and 78 exact PCM windows, plus later-window
checks, with fallback explicitly forbidden. [Parity results](production-decoder-parity.json).
The [fixture manifest](benchmark-fixtures.json) records checksums and sizes of the
private input files; no footage, paths, account identifiers or request JSON is
committed. Synthetic fixture encoding and native capture remain separate inputs.
