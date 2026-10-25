# Engine-noise reduction on phone Talking edits — model, rollout, listening review

The lane (PR #1760, 20b59cef3) was merged dark: `PHONE_ENGINE_NOISE_REDUCTION_ENABLED`
defaults `false`, the worker needs a local ARNNDN model whose SHA-256 matches
`PHONE_ENGINE_NOISE_MODEL_SHA256`, and the phone recipe needs `processedAudio` +
`audioMix` verified. This PR supplies the model; this page is the operator runbook.

## The model

- File: `src/apps/api/assets/models/rnnoise-lq-2018-08-31.rnnn` (290 KB, plain text
  "rnnoise-nu model file version 1"). Source: `leavened-quisling-2018-08-31/lq.rnnn` in
  github.com/GregorR/rnnoise-models — "voice" signal (speech plus laughter and other
  human sounds) against general noise; the repository README states the model files
  are not subject to copyright.
- Pinned SHA-256: `1957528b752799fddf06270bc5469af7cf54c3badc358544ae2abed730943ff9`
  (`app/pipeline/engine_noise_model.BUNDLED_MODEL_SHA256`).
- In the production image it lands at `/app/assets/models/rnnoise-lq-2018-08-31.rnnn`
  (Dockerfile `WORKDIR /app` + `COPY src/apps/api/assets ./assets`).
- Guard: `tests/pipeline/test_engine_noise_model_asset.py` (file present, digest matches,
  FFmpeg `arnndn` loads it, the three settings + two features are the whole gate).
- Alternative if the listening review prefers it: `marathon-prescription-2018-08-29/mp.rnnn`
  (general signal, general noise). Swap the file, re-pin the digest, re-run the guard.

## Failure modes (written before the asset landed)

| # | Mode | Guard / mitigation |
|---|---|---|
| 1 | Model missing from the image | present in `assets/models`, copied by the Dockerfile; test 1 |
| 2 | Digest in the secret ≠ file | the worker refuses ("noise model is unavailable or changed"), audio stays original, receipt `not_applied`; test 1 pins the digest, the commands below carry it |
| 3 | FFmpeg in the image lacks `arnndn` | built into FFmpeg since 4.3; prod runs 7.x; test 3 loads the model |
| 4 | Flag on, token missing | `engine_noise_reduction_available()` is false ⇒ "isn't available for this edit"; test 4 |
| 5 | Secrets set before the image deploys | fail-open (mode 2); set them after the deploy anyway |
| 6 | Speech damaged, lip-sync drift, quiet reply lost | the lane measures filter latency and realigns; the listening review below is the only check of naturalness |
| 7 | Wind / traffic / crowd expectations | not covered by a stationary-noise model; the receipt says "reduced, not isolated" |

## Rollout

1. Merge this PR; wait for **Fly Deploy** of that commit (`fly status --app nova-video`).
2. Read the current verified-features list and set the secrets. Each `fly secrets set`
   restarts api + worker. The second command is generated from the 2026-10-25 live list
   plus the two required features — regenerate it if the list changed:
   `python scripts/admin.py --prod GET admin/kria/phone-render-config | python3 -c 'import json,sys; print(json.dumps(json.load(sys.stdin)["verified_features"]))' | (cd src/apps/api && python -m app.pipeline.engine_noise_model)`

```bash
fly secrets set -a nova-video PHONE_ENGINE_NOISE_REDUCTION_ENABLED=true PHONE_ENGINE_NOISE_MODEL_PATH=/app/assets/models/rnnoise-lq-2018-08-31.rnnn PHONE_ENGINE_NOISE_MODEL_SHA256=1957528b752799fddf06270bc5469af7cf54c3badc358544ae2abed730943ff9
```

```bash
fly secrets set -a nova-video PHONE_RENDER_VERIFIED_FEATURES='["alphaOverlay","animatedText","audioDucking","audioMix","authoredText","basicComposition","cameraEffects","captions","carouselEffects","clipTransitions","crossfade","customEffects","editorMedia","freehandDrawing","freezeFrame","goldenHourLook","hdr","hevcDecode","local1080Export","mediaCards","motionPresets","motionScenes","musicBed","narrationAudio","positionedText","processedAudio","semanticCamera","singleWordText","slidePosts","soundEffects","sourceCrop","stillImages","textBend","variableSpeed","visualBlocks","visualVideos"]'
```

3. Confirm: `python scripts/admin.py --prod GET admin/kria/phone-render-config` lists
   `processedAudio`.
4. No new app build is needed: TestFlight build 1736 (2026-10-25) declares client protocol 20
   and the lane requires 18 (`processed_audio` decoding shipped with #1760). Older builds
   get a 426 update-required for this feature only.
5. On the iPhone, in a NEW chat (old briefs carry no `noise_reduction: "engine"` fact), send a
   single-source Talking take recorded next to an engine and ask to "reduce the engine noise".

## Listening review (required before telling creators about it)

Compare the exported audio with the original on the same take:

- Engine band quieter: `ffmpeg -i <file> -af "bandpass=f=120:w=200,volumedetect" -f null -`
  mean volume must drop by several dB between original and export.
- Speech intact: `ffmpeg -i <file> -af "highpass=f=300,lowpass=f=3400,volumedetect" -f null -`
  mean volume within ~1 dB of the original.
- Listen: the quiet reply is still audible, no robotic artefacts, lips stay in sync.
- Duration unchanged (`ffprobe -show_entries format=duration`).

Rollback: `fly secrets set -a nova-video PHONE_ENGINE_NOISE_REDUCTION_ENABLED=false` (the
token can stay).
