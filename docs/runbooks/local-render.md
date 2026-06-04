# Local-render parity — runbook

`dev-auto.sh` is for fast iteration but does NOT match prod output: the API + worker run
on the Mac host with brew ffmpeg, host fonts, and a host Python. Brew ffmpeg ≠ Debian
apt ffmpeg, and host fontconfig will resolve ASS subtitle fallbacks differently. The
plain `docker-compose.yml` doesn't fix this either — it builds `src/apps/api/Dockerfile`
+ `Dockerfile.worker`, both of which are missing `fonts-dejavu-core`, `libheif1`, and
the prod root `Dockerfile`'s explicit torch+torchvision CPU-only install.

## Usage

```bash
cp .env.local-render.example .env.local-render   # fill in GCS + AI keys
make local-render CLIP=/path/to/clip.mp4 TEMPLATE=<uuid> [MODE=template|music] [INPUTS='{"location":"Tokyo"}']

# generative edits (no template; song auto-matched; renders all 3 variants):
make local-render MODE=generative CLIPS="a.mp4 b.mp4 c.mp4"
```

Stop with `make local-render-down`; tail with `make local-render-logs`.

The `nova-render` compose project name lets this stack coexist with `make dev` and
`dev-auto.sh` on the same machine.

## Residual divergence sources

Even Docker can't eliminate:

- **LLM nondeterminism** — Gemini/OpenAI calls vary call-to-call; mitigated by
  `template_cache` (key includes `prompt_version` + `TEXT_OVERLAY_VERSION_V2`) so
  re-runs of the same `(template_id, source_video)` hit cache. First-run drift is
  intrinsic — render twice to separate cache from jitter.
- **DB seed drift** — local Postgres has whatever `seed_*.py` last ran; seed the same
  template version locally before comparing to a prod render.
- **Signed-URL host + expiry** — same bucket, different host metadata: MP4 bytes are
  identical, URL strings differ.
- **Feature flags** — `text_overlay_v2_enabled`, `ORIENTATION_NORMALIZE_ENABLED`,
  `SINGLE_PASS_ENCODE_ENABLED`, `TEXT_RENDERER_SKIA_ENABLED`. `.env.local-render.example`
  lists Fly defaults; the driver prints active container values before submitting. Differ
  from `fly secrets list` → render won't match.

## Cache busting

BuildKit layer cache invalidates automatically when `pyproject.toml` or the `Dockerfile`
changes. To force a from-scratch rebuild (e.g. apt-package divergence suspected):
`docker-compose -f docker-compose.local-render.yml build --no-cache`.
