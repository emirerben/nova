# Published export evidence adapter

This isolated unit qualifies read-only extraction from an already published native MP4. It consumes the generation-bound publication byte proof (#1977 `publication_proof`) and immutable review-context types. It does not admit review work, call a provider, render a final video, mutate a user project or enable paid review.

The adapter validates owner/path, recipe identity, published attempt, exact storage generation, actual streamed bytes and SHA-256. Bounded JPEG frames carry timestamps and source hashes. A nonroot Linux child applies hard resource limits; the parent bounds output and wall time and cleans process groups. External MOV references are disabled. Unsupported platforms report unavailable before storage access. Frame evidence never claims audio inspection or model review.

Source has independent review. Two Darwin fail-closed checks passed. Actual nonroot Linux decoder qualification is pending the existing free-regression workflow, including pixel/timestamp/provenance, corrupt and replaced media, memory refusal, bounded stdout/stderr, file-size and process timeout controls. Previous Mac decoder results do not qualify the hardened implementation.

This branch isolates qualification from concurrent canonical admission recovery work. It retains the workflow's existing regression suites and uses replay/test credentials only. No shared-screen or simulator action is required.

## Boundaries

- **Where it runs.** `tasks.prepare_generation_review` downloads up to 256 MB into RAM-backed `/tmp` and runs ffmpeg, so it is routed by `KRIA_INTENT_REVIEW_EVIDENCE_QUEUE` (fly.toml pins `autoplace-jobs`, consumed by the 4 GB worker). It must never use `agent-control` or `maintenance`: the 1 GB `light` machine hosts Beat and turns. `tests/test_render_review_routes.py` pins this.
- **Decoder environment.** The ffmpeg/ffprobe child parses creator-uploaded bytes and receives an allowlisted environment (`PATH`, locale, library path, `TMPDIR`), never the worker's credentials. It is not network- or filesystem-confined; that needs a container boundary and is out of scope for rlimits.
- **Retention.** Decoded stills live in `result.evidence.frames` only for `FRAME_RETENTION` (24 hours) after the review row is created. `expire_review_frames` then drops the image bytes and keeps hashes, timestamps and provenance. Account deletion removes the rows by cascade.
- **Sweep.** Admission recovery and republishing run in their own single-flight, time-boxed beat task (`tasks.reconcile_generation_reviews`), never inside the 30 s `reconcile_kria_turns`.
