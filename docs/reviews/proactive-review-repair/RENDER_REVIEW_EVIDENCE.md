# Published export evidence adapter

`services/render_review_evidence.py` reads one already published native MP4 and returns bounded JPEG frames with byte-exact provenance. `tasks.prepare_generation_review` calls it for every admitted generation review. The adapter itself calls no provider, renders nothing and mutates no project. It consumes the generation-bound publication byte proof (#1977 `publication_proof`) and the frozen review-context types.

The adapter validates owner/path, recipe identity, published attempt, exact storage generation, actual streamed bytes and SHA-256. Bounded JPEG frames carry timestamps and source hashes. A frame the decoder cannot produce, such as a time past the last video frame when the audio track outlasts the video, is dropped; evidence is `unavailable` only when no frame decodes. A nonroot Linux child applies hard resource limits; the parent bounds output and wall time and cleans process groups. External MOV references are disabled. Unsupported platforms report unavailable before storage access. Frame evidence never claims audio inspection or model review.

## Decoder qualification

The decoder controls (pixel, timestamp and provenance, corrupt and replaced media, memory refusal, bounded stdout/stderr, file-size and process timeout) need a nonroot Linux process. They run in the free-regression workflow (`scripts/request-following-regressions.py` lists `tests/services/test_render_review_evidence.py`) with replay and test credentials only. On macOS the same tests fail closed with `resource_limits_unavailable`, so a Mac result never qualifies the decoder.

Frame sampling (render-review-frames-v2): up to six frames. Up to four are anchored on the middle of on-screen text spans taken from the frozen recipe (wording the creator asked for first, then titles, then other text) because uniform midpoints miss text shown for a couple of seconds in a long video. Uniform midpoints fill the remainder and skip any within 0.5 s of an anchor. `FrameEvidence.anchor_ref` names the text layer. See `docs/runbooks/kria-intent-review.md`.

## Boundaries

- **Where it runs.** `tasks.prepare_generation_review` downloads up to 256 MB into RAM-backed `/tmp` and runs ffmpeg, so it is routed by `KRIA_INTENT_REVIEW_EVIDENCE_QUEUE` (fly.toml pins `autoplace-jobs`, consumed by the 4 GB worker). It must never use `agent-control` or `maintenance`: the 1 GB `light` machine hosts Beat and turns. `tests/test_render_review_routes.py` pins this.
- **Decoder environment.** The ffmpeg/ffprobe child parses creator-uploaded bytes and receives an allowlisted environment (`PATH`, locale, library path, `TMPDIR`), never the worker's credentials. It is not network- or filesystem-confined; that needs a container boundary and is out of scope for rlimits.
- **Retention.** Decoded stills live in `result.evidence.frames` only for `FRAME_RETENTION` (24 hours) after the review row is created. `expire_review_frames` then drops the image bytes and keeps hashes, timestamps and provenance. Account deletion removes the rows by cascade.
- **Sweep.** Admission recovery and republishing run in their own single-flight, time-boxed beat task (`tasks.reconcile_generation_reviews`), never inside the 30 s `reconcile_kria_turns`.
