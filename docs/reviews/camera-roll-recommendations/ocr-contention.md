# Export OCR contention

CI on `7b1ba93cd` failed three actual-export controls because Tesseract exhausted
the five-second OCR subprocess deadline. Diagnostic output named the exact
Tesseract command and `TimeoutExpired`; these were missing measurements, not
wrong acceptance expectations. The iOS contract and physical-build CI gates passed.

Tesseract's OpenMP teams inherited the parent process's resource settings. Under
CPU quotas, multiple OCR processes competed with one another. The branch had
introduced a strict five-second OCR budget in `21a6ff242`; main used twenty seconds.
The fix caps each OCR child at one thread, preserving its algorithm, confidence
threshold, exact-copy comparisons and five-second deadline. Other subprocesses and
the parent environment retain their existing settings. Tesseract documents this
resource policy in its [official FAQ](https://tesseract-ocr.github.io/tessdoc/FAQ.html#can-i-increase-speed-of-ocr).

## Flake evidence

The forced test uses actual synthetic pixels and Tesseract processes, not mocked
measurements. In a read-only Debian container with two CPUs and 3 GiB RAM, thirty-two
concurrent reads inherited deliberately oversubscribed parent thread settings.
Exact frozen source from main and the pre-fix branch was compared with the fix:

| Code | Failed cohorts / 10 | Maximum cohort duration | OCR deadline |
| --- | ---: | ---: | ---: |
| Main at recorded base | 0 / 10 | 16.619 s | 20 s |
| Branch before fix | 10 / 10 | 6.417 s | 5 s |
| Branch with child thread cap | 0 / 10 | 2.159 s | 5 s |

The new integration case failed before the code change on Linux; after it, the
complete export/completion/publication matrix passed all 52 checks on both Linux
and macOS. Incorrect text, missing letters, extra punctuation, timing, source
windows/order, rate and audio retain their original negative controls. This is a
code resource-policy fix, with no timeout increase, retry or skip. It belongs in
this PR. Fresh full remote CI is still required; the stress experiment is not
proof that every hosted runner condition has been reproduced.

[Machine-readable report](ocr-contention-proof.json) pins source hashes, base
revision, workload, resource settings and measured results. Frozen main source was
loaded in an isolated probe module rather than modifying the shared main checkout.
The first Linux command/control experiments are kept alongside the task's local
logs; the final report above uses the real verifier code path.

Repeat the regression and correctness matrix from `src/apps/api`:

```sh
GEMINI_API_KEY=ci-noop pytest tests/integration/test_actual_export_verification.py \
  tests/integration/test_actual_export_completion.py \
  tests/routes/test_actual_export_publication.py -q
```

The regression is
`test_concurrent_text_evidence_survives_an_oversubscribed_parent`. CPU pressure is
controlled by the Linux container's `--cpus=2`; no paid provider or account media is
used. Local logs: `.dev/editorial-taste-ocr-runtime-red.log`,
`.dev/editorial-taste-ocr-linux-green.log`, `.dev/editorial-taste-ocr-mac-final.log`.
