# Template Fixtures

Snapshots of production template configurations. Used for:
- **Recovery** — restore a template if its DB row is corrupted or accidentally archived.
- **Reproducibility** — bring a template into a fresh environment (staging, new dev DB).
- **History** — track meaningful template config changes alongside code.

Each `*.json` file contains:
- `_meta` — export timestamp, source environment
- `template` — metadata (name, description, source URL, GCS paths, clip constraints)
- `recipe` — the active recipe at export time (slots, text overlays, timings, interstitials)

## Format

Snapshot only. The source video file (`gcs_path`) and any music tracks must already exist in GCS for the template to render. These fixtures do **not** include binary media.

## Restoring a fixture

There is no automated import yet. To restore manually:

1. Read the fixture JSON.
2. `POST /admin/templates` with the `template` block (or recreate via admin UI from the source URL).
3. `PUT /admin/templates/{id}/recipe` with the `recipe` block.

A future PR may add a CLI: `python -m app.scripts.import_template src/apps/api/templates/just-fine.json`.

### Templates that exist in prod but are unpublished

If `template.id` already resolves at `GET /admin/templates/{id}` but does not appear in the public `/templates` list, the row is missing `published_at`. To surface it:

```bash
curl -X PATCH "https://nova-video.fly.dev/admin/templates/{id}" \
  -H "X-Admin-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"publish": true}'
```

### Music-track fixtures

`love-from-moon.json` is shaped for a templated **music track**, not a regular template. It records the recipe + asset requirements. Seeding it into a fresh environment runs the existing CLI:

```bash
API_URL=https://nova-video.fly.dev ADMIN_TOKEN=$TOKEN \
  python3 scripts/seed_love_from_moon.py
```

The script requires the audio + thumbnail files at `/tmp/tiktok-konna/` (override via `AUDIO_PATH` / `THUMB_PATH`).

## When to update a fixture

Re-export when a meaningful, deliberate config change lands (slot timings rebalanced, overlays restructured, music swapped). Skip noise (per-test edits, experimental tweaks).

To re-export from production:

```bash
TOKEN=...  # admin token
TPL_ID=010403eb-6c6d-408b-af4d-e68500e3353c
curl -s "https://nova-video.fly.dev/admin/templates/$TPL_ID" -H "X-Admin-Token: $TOKEN" > /tmp/tpl.json
curl -s "https://nova-video.fly.dev/admin/templates/$TPL_ID/recipe" -H "X-Admin-Token: $TOKEN" > /tmp/rec.json
# Then merge into the fixture format (see existing files for shape).
```
