# KRI-342: deterministic auth boundaries and smaller CI package installs

This repair addresses CI preparation and one authentication contract test. It
does not change production authentication, creator journeys, prompts, media
selection, rendering, or the iOS client. Final hosted CI remains a separate
acceptance step; passing local tests is not a final-main success claim.

## Retained failure and acceptance

Main CI run `39866287748`, source
`2939a16bb83c4360ef521e69886e667d279e5667`, failed the API-0 case
`test_access_token_rejects_invalid_claim_boundaries[overrides4]` with
`DID NOT RAISE MobileAuthError`. Its offline job `120129457025` was cancelled at
the 30-minute job limit during APT setup, after two 600-second download
timeouts. No offline tests ran. A green source PR did not establish green main.

Acceptance: preserve every production token rule and original invalid-claim
assertion, make the boundary test independent of elapsed real time, keep valid
boundary claims accepted, reduce unnecessary package downloads, and preserve
installation failure/timeout behavior. Do not increase test or job deadlines.

## Auth: reproduced before repair

The original test makes `iat = construction_now + 61`. The real decoder accepts
`iat <= validation_now + 60`. A one-second advance changes that token from
invalid to valid. The test's clock assumption, rather than this policy, was wrong.

The unchanged original test and real signer/decoder were invoked with controlled
module-local clock reads, without sleeps, providers, or probabilistic retries:

| Construction / validation clock | Signed iat | Original rejection assertion |
| --- | --- | --- |
| 1792709228 / 1792709228 | 1792709289 | Pass |
| 1792709228 / 1792709229 | 1792709289 | Fail: DID NOT RAISE |

After repair both scenarios pass. The test now pins only `mobile_auth.time`,
restored by pytest's monkeypatch, and resolves the equal iat/exp case from that
same fixed clock. It does not replace global `time.time`, signing, or decoding.
All eleven original invalid cases and their error-code assertions remain.

New controls were written before the fixture repair: future iat +60 accepts,
+61 rejects, expiry now+1 accepts, and expiry now rejects. Expiry cases use
iat now-60 so the lifetime guard cannot mask the expiry guard.

In-memory copies of the real decoder established that the rejection assertion
detects a forced future-tolerance regression (60 to 61) both before and after
repair, one of one forced probes each. The new cases also detect tightening the
tolerance to 59 and changing expiry rejection from `<= now` to `< now`. These
are deterministic mutation controls, not statistical failure-rate estimates.

Production `app/services/mobile_auth.py` stays byte-identical, SHA256
`b824eabbfcd1118b35f4e2b13177c23ddbdf1508ca0f4992d541a5be2f37b28a`.

## APT: verified dependency evidence and limits

The retained log last lists the 27.4 MB `pocketsphinx-en-us` model. This does not
prove that package alone caused the mirror stall. The installer previously
included all recommended packages, increasing network work unrelated to tests.

A fresh Ubuntu 24.04 signed-index dependency comparison confirmed
`libpocketsphinx3` version `0.8.0+real5prealpha+1-15ubuntu5` **Recommends**
`pocketsphinx-en-us` (architecture all, 27,433,580 bytes). The default empty-image
plan had 303 new packages; `--no-install-recommends` had 213. Both kept explicit
FFmpeg, EGL/GL, Tesseract, Turkish OCR, and dependent English/orientation OCR.
The smaller plan omitted the speech model and optional Mesa video/Vulkan drivers.
No code references to pocketsphinx or FFmpeg `asr=` were found.

This comparison used ARM64 Ubuntu with real verified repository metadata, not
an exact x64 GitHub runner. Required hosted media/OCR gates must still execute.
The first probe failed repository signature validation on a full local Docker
disk and was stopped. A read-only container with bounded memory mounts for
APT lists/temp completed successfully; no signature/hash bypass, package
installation, disk cleanup, or host change was used.

The repair applies `--no-install-recommends` to both download and offline
installation, matching production's existing Dockerfile policy. Required
dependencies remain resolved by APT. Existing hard limits, four bounded attempts,
backoff, cached downloads, offline install, and propagated errors are unchanged.
This reduces unnecessary download exposure; it does not guarantee mirror uptime.

## Executed checks and remaining acceptance

- Before repair: original auth file 21 passed; original installer four passed.
- New four auth boundary cases passed before the fixture repair.
- After repair: auth file 25 passed; installer six passed. Installer expectations
  were first run against the original script and failed on the missing policy.
- Installer fault cases cover a killed update, download retry, update/download
  exhaustion without install, non-retried install failure, and missing arguments.
- Commands: `NOVA_EVAL_MODE=replay GEMINI_API_KEY=ci-noop OPENAI_API_KEY=ci-noop
  bash scripts/check-api.sh test tests/test_mobile_auth_contract.py -q`;
  `python3 scripts/ci/test_apt_install.py`; `bash -n scripts/ci/apt-install.sh`.
- Local Python is 3.14; hosted Python 3.11 is required before landing.
- `scripts/reliability-gates.py select --base origin/main` maps no incident gate
  to these paths. Preship, full exact-head PR CI and resulting main CI are
  required separately. No DB-backed local suite, native output, physical-phone
  acceptance, live model, paid evaluation, or manual release was claimed.

Broader KRI-342 load-sensitive tests and the original product incidents remain
outside this local fix. The original merge queue is complete; the twelve newer
PRs remain separate review handoffs.
