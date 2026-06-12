# HANDOFF.md

_This file is the shared brain between architect and builder sessions. The builder writes raw results here after each work block. The architect reads and judges it at the start of each session._

## Last completed slice

None — loop bootstrap (first architect session, 2026-06-12). No builder work has run yet.

## Key decisions made

- **Diagnosis before capability (architect, 2026-06-12).** Goal is editorial-grade text overlays (stacked multi-block text, mixed fonts/sizes, text color-change effect — three TikTok references on file) AND closing the "great locally, basic in prod" gap. Slice order frozen: 1) make degradation observable + convict the mechanism on real prod jobs, 2) fix convicted mechanisms, 3) build the editorial/color-change capabilities. Rationale: PR #498 proved the dominant failure class is input-dependent silent degradation (signal-free Turkish hook flattened the editorial cluster to plain lines, invisibly); building new capability first would flatten in prod the same way.
- **Ranked divergence mechanisms (evidence in architect session #1):** (1) input-dependent silent degradation — no pipeline events for layout selection / role derivation / shrink; (2) silent font fallback — `_typeface_for_overlay` (`text_overlay_skia.py:187-218`) and Pillow `ImageFont.load_default()` fall back with zero signal; (3) environment divergence per `docs/runbooks/local-render.md` incl. feature-flag drift between Fly secrets and local env; (4) process gap — `make verify-overlays` is manual-only, CI never renders through the prod image. Encoder drift ruled unlikely (locked by `tests/test_encoder_policy.py`).

## Visual targets (reference analysis — architect, 2026-06-12)

The three TikTok references are captured locally at `.sources/tiktok-refs/` (gitignored — never commit; re-fetch via yt-dlp/gallery-dl if lost). This section is the durable spec-input distilled from them; Slice 3 is judged against it.

**Ref 1 — @denocampo_/photo/7352488708433612037 (`ref1-fonts-carousel/`, 20 slides).** A "my favorite fonts" carousel (all free, dafont.com). Font list, in slide order: Birds of Paradise (script), Apple Garamond, Forward Serif, Dream Orphans, CODIGRA, Roseblue, Motena (Golden), Recoleta, Favorite Notification, Amelina Script, Summer Dreams, Creato, Coolvetica, Couture, TTPhobos, MADESOULMAZE, Marola, Helvetica Neue. Shared aesthetic: warm cream/off-white text (NOT pure white) over moody warm photos; soft glow; **roman+italic mixed within one two-line lockup** (line 2 italic and right-shifted); layered-repetition effects (same word ×3 in different weights/offsets — Creato, MADESOULMAZE, Couture). Matches the existing taste memory: editorial serifs, sans reads cheap.

**Ref 2 — @mafeanzures/video/7610489921840614686 (`ref2-kinetic-frames/`, 1fps, 78s).** Word-synced kinetic captions: ONE word/short phrase on screen at a time, each phrase at a different position AND scale, and — the key capability — **each phrase can use a DIFFERENT font** (heavy sans for "that"/"really"/"little", ornate script for "Magical Night", condensed serif for "automatically cool", bold-sans-with-small-subline for "my fonts / which"). It's literally a video about her edit fonts (shows dafont.com). Target capability: per-phrase font/scale/position variation across a timed caption sequence — not one global caption style.

**Ref 3 — @salvadorlerma/video/7646572293430217998 (`ref3-colorsweep-frames/`, 2fps, 26s; Peanuts edit).** Lyric typography with three distinct capabilities: (1) **progressive color sweep** — letters transition white→blue (or navy→accent) sweeping across the word as the lyric is sung (see `ref3_036.png` "it's someone that you", `ref3_048.png` "your / best friend"); (2) **mixed-font lockup within one lyric line** — plain sans "your" + large serif-italic "best friend"; (3) **scene-adaptive palette** — navy text on the bright-yellow scene, white on dark scenes. This is the "test color change effect" Emir called out.

**Capability gaps these imply (vs. current engine, from architect session #1 exploration):** one overlay = one font + one base color (no per-word font/color mix in the Skia path — span support exists only in Pillow); no time-varying color (feasible: Skia already renders per-frame PNG sequences, so per-frame paint is the lever); no scene-adaptive text palette; word-cluster intro covers multi-block size hierarchy but only for the intro. Fonts: several reference fonts (Garamond-class, Recoleta-class soft serif) have close bundled analogs (EB Garamond, Fraunces, Playfair, Bodoni Moda, Cormorant); true script (Amelina/Birds of Paradise class) is thin in the bundle (Great Vibes, Pacifico) — Slice 3 should decide additions consciously (font-registry coupling: registry-embeddings.npz + sync:fonts mirror + 2 count tests).

## Open disagreements

None yet. The builder must raise disagreements in PHASE 0; each gets an ACCEPT/REJECT/MODIFY verdict here next architect session.

## Acceptance criteria for last slice

N/A — no completed slice to judge.

## Last slice results (raw)

### Test output

| Command | Exit | Raw result |
|---|---:|---|
| `pytest tests/pipeline/test_intro_cluster.py tests/pipeline/test_text_overlay_skia.py tests/pipeline/test_overlay_verify.py tests/pipeline/test_generative_overlays.py tests/tasks/test_generative_build.py` | 0 | `267 passed, 1 warning in 20.94s` |
| `pytest` | 0 | `5012 passed, 58 skipped, 168 warnings in 212.31s` |
| `ruff check .` | 0 | `All checks passed!` |
| `ruff format --check` (9 touched Python files) | 0 | `9 files already formatted` |
| `git diff --check` | 0 | no output |
| `make verify-overlays` | 2 | `PASS=18 WARN=0 FAIL=1 SKIPPED=0` |

### Overlay verification report

| Field | Value |
|---|---:|
| fixture overlay instances | 19 |
| `resolved_typeface` present | 19 |
| fallback count | 1 |
| pre-existing fixture instances passing | 18 |
| failing fixture | `unknown_font_family.json` |
| requested family | `Definitely Not A Real Nova Font` |
| resolved family | `Playfair Display` |
| fallback | `true` |

### Flag parity

| Flag | Fly production | `.env.local-render` | dev `.env` | Effective default |
|---|---|---|---|---|
| `TEXT_RENDERER_SKIA_ENABLED` | unset | `true` | unset | `true` |
| `TEXT_OVERLAY_V2_ENABLED` / `text_overlay_v2_enabled` | unset | `false` | unset | `false` |
| `SINGLE_PASS_ENCODE_ENABLED` | unset | `false` | unset | `false` |
| `ORIENTATION_NORMALIZE_ENABLED` | unset | `true` | unset | `true` |

### Production evidence jobs

| Prod job | Prod intro text | Local rerender job | `song_text` / `original_text` layout event | Font fallback | Cluster role events | Cluster shrink events | Mechanism verdict |
|---|---|---|---|---:|---:|---:|---|
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | `i need more information to write this hook` | `f1bd388b-0e2d-4e0c-a7ba-74f3551934aa` | `requested_layout=linear`, `selected_layout=linear`, `reason=explicit_linear` | `false` | 0 | 0 | `other: agent explicitly requested linear` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | `the question that made everyone stop laughing` | `4a71dfe5-685b-4447-8131-5093f4e8ad0e` | `requested_layout=linear`, `selected_layout=linear`, `reason=explicit_linear` | `false` | 0 | 0 | `other: agent explicitly requested linear` |

### Frame stills

| Prod job | Frame | Path |
|---|---|---|
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | prod `song_text`, 1.0s | `/private/tmp/nova-overlay-evidence/22c0bc36-0ef2-447c-b589-388cfabb5c34/stills/prod-song_text-1.0s.png` |
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | rerender `song_text`, 1.0s | `/private/tmp/nova-overlay-evidence/22c0bc36-0ef2-447c-b589-388cfabb5c34/stills/rerender-song_text-1.0s.png` |
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | rerender `original_text`, 1.0s | `/private/tmp/nova-overlay-evidence/22c0bc36-0ef2-447c-b589-388cfabb5c34/stills/rerender-original_text-1.0s.png` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | prod `song_text`, 1.0s | `/private/tmp/nova-overlay-evidence/568ced7b-f0ca-49c5-8360-5387bbbbc493/stills/prod-song_text-1.0s.png` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | rerender `song_text`, 1.0s | `/private/tmp/nova-overlay-evidence/568ced7b-f0ca-49c5-8360-5387bbbbc493/stills/rerender-song_text-1.0s.png` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | rerender `original_text`, 1.0s | `/private/tmp/nova-overlay-evidence/568ced7b-f0ca-49c5-8360-5387bbbbc493/stills/rerender-original_text-1.0s.png` |

### Per-variant event dumps

Prod job `22c0bc36-0ef2-447c-b589-388cfabb5c34`, local rerender `f1bd388b-0e2d-4e0c-a7ba-74f3551934aa`:

```json
{
  "song_lyrics": {
    "intro_layout_selected": [],
    "font_resolved": [
      {"overlay_index": 0, "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 1, "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 2, "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 3, "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 4, "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false}
    ]
  },
  "song_text": {
    "intro_layout_selected": [{"text": "the sound that started it all", "requested_layout": "linear", "selected_layout": "linear", "reason": "explicit_linear", "word_count": 6, "has_word_roles": false, "fallback": false}],
    "font_resolved": [
      {"overlay_index": 0, "effect": "fade-in", "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 1, "effect": "static", "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false}
    ]
  },
  "original_text": {
    "intro_layout_selected": [{"text": "the sound that started it all", "requested_layout": "linear", "selected_layout": "linear", "reason": "explicit_linear", "word_count": 6, "has_word_roles": false, "fallback": false}],
    "font_resolved": [
      {"overlay_index": 0, "effect": "fade-in", "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 1, "effect": "static", "requested_font_family": "Playfair Display", "resolved_typeface": {"name": "Playfair Display", "file": "PlayfairDisplay-Bold.ttf", "source": "font_family"}, "fallback": false}
    ]
  }
}
```

Prod job `568ced7b-f0ca-49c5-8360-5387bbbbc493`, local rerender `4a71dfe5-685b-4447-8131-5093f4e8ad0e`:

```json
{
  "song_lyrics": {
    "intro_layout_selected": [],
    "font_resolved": [
      {"overlay_index": 0, "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 1, "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 2, "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 3, "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 4, "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false}
    ]
  },
  "song_text": {
    "intro_layout_selected": [{"text": "the kind of laugh that leaves you breathless", "requested_layout": "linear", "selected_layout": "linear", "reason": "explicit_linear", "word_count": 8, "has_word_roles": false, "fallback": false}],
    "font_resolved": [
      {"overlay_index": 0, "effect": "fade-in", "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 1, "effect": "static", "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false}
    ]
  },
  "original_text": {
    "intro_layout_selected": [{"text": "the kind of laugh that leaves you breathless", "requested_layout": "linear", "selected_layout": "linear", "reason": "explicit_linear", "word_count": 8, "has_word_roles": false, "fallback": false}],
    "font_resolved": [
      {"overlay_index": 0, "effect": "fade-in", "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false},
      {"overlay_index": 1, "effect": "static", "requested_font_family": "Playfair Display Regular", "resolved_typeface": {"name": "Playfair Display Regular", "file": "PlayfairDisplay-Regular.ttf", "source": "font_family"}, "fallback": false}
    ]
  }
}
```

Raw authenticated debug payloads:

| Job | Path |
|---|---|
| `f1bd388b-0e2d-4e0c-a7ba-74f3551934aa` | `/private/tmp/nova-overlay-evidence/22c0bc36-0ef2-447c-b589-388cfabb5c34/rerender-debug.json` |
| `4a71dfe5-685b-4447-8131-5093f4e8ad0e` | `/private/tmp/nova-overlay-evidence/568ced7b-f0ca-49c5-8360-5387bbbbc493/rerender-debug.json` |

### Local debug-view evidence

Job `63e0cc36-f01f-442c-8b89-977ceac38daa`, `GET /admin/jobs/63e0cc36-f01f-442c-8b89-977ceac38daa/debug`:

```json
[
  {"event": "font_resolved", "data": {"text": "On top Feel the shot, body rock, rock it, don't stop", "effect": "lyric-line", "overlay_index": 0, "requested_font_family": "Syne", "requested_font_style": "sans", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}},
  {"event": "font_resolved", "data": {"text": "Round and round, up and down, around the clock", "effect": "lyric-line", "overlay_index": 1, "requested_font_family": "Syne", "requested_font_style": "sans", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}},
  {"event": "font_resolved", "data": {"text": "Monday, Tuesday, Wednesday and Thursday (do it)", "effect": "lyric-line", "overlay_index": 2, "requested_font_family": "Syne", "requested_font_style": "sans", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}},
  {"event": "font_resolved", "data": {"text": "Friday, Saturday, Saturday to Sunday (do it)", "effect": "lyric-line", "overlay_index": 3, "requested_font_family": "Syne", "requested_font_style": "sans", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}},
  {"event": "intro_layout_selected", "data": {"text": "pov: you’re a bubble floating over a synthwave city", "requested_layout": "linear", "selected_layout": "linear", "reason": "explicit_linear", "word_count": 9, "has_word_roles": false, "fallback": false}},
  {"event": "font_resolved", "data": {"text": "pov: you’re a bubble floating over a synthwave city", "effect": "fade-in", "overlay_index": 0, "requested_font_family": "Syne", "requested_font_style": "display", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}},
  {"event": "font_resolved", "data": {"text": "pov: you’re a bubble floating over a synthwave city", "effect": "static", "overlay_index": 1, "requested_font_family": "Syne", "requested_font_style": "display", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}},
  {"event": "intro_layout_selected", "data": {"text": "pov: you’re a bubble floating over a synthwave city", "requested_layout": "linear", "selected_layout": "linear", "reason": "explicit_linear", "word_count": 9, "has_word_roles": false, "fallback": false}},
  {"event": "font_resolved", "data": {"text": "pov: you’re a bubble floating over a synthwave city", "effect": "fade-in", "overlay_index": 0, "requested_font_family": "Syne", "requested_font_style": "display", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}},
  {"event": "font_resolved", "data": {"text": "pov: you’re a bubble floating over a synthwave city", "effect": "static", "overlay_index": 1, "requested_font_family": "Syne", "requested_font_style": "display", "resolved_typeface": {"name": "Syne", "file": "Syne-ExtraBold.ttf", "source": "font_family"}, "fallback": false, "level": "info"}}
]
```

| Artifact | Path |
|---|---|
| authenticated debug JSON | `/private/tmp/local-debug-63e0cc36-f01f-442c-8b89-977ceac38daa.json` |
| `song_text` still, 1.0s | `/private/tmp/nova-overlay-synthetic/stills/local-song_text-1.0s.png` |

## Slice 2 results (raw)

### Test output

| Command | Exit | Raw result |
|---|---:|---|
| `cd src/apps/api && pytest -p no:cacheprovider tests/pipeline/test_intro_cluster.py -q` | 0 | `25 passed in 0.12s` |
| `cd src/apps/api && pytest -p no:cacheprovider tests/ -k "overlay_format or intro_writer or overlay_verify"` | 0 | `86 passed, 4989 deselected in 5.66s` |
| `cd src/apps/api && pytest -p no:cacheprovider tests/ -k "overlay_format or intro_writer or overlay_verify or intro_cluster" -q` | 0 | `111 passed, 4965 deselected in 5.24s` |
| `cd src/apps/api && pytest -p no:cacheprovider tests/` | 0 | `5017 passed, 58 skipped, 168 warnings in 214.53s` |
| `cd src/apps/api && pytest -p no:cacheprovider tests/` | 1 | `11 failed, 5007 passed, 58 skipped, 166 warnings in 428.56s` |
| `cd src/apps/api && pytest -p no:cacheprovider <9 redis/celery failed tests> -q` | 0 | `9 passed, 2 warnings in 1.99s` |
| `cd src/apps/api && ruff check --no-cache .` | 0 | `All checks passed!` |
| `cd src/apps/api && ruff format --check --no-cache <13 touched Python files>` | 0 | `13 files already formatted` |
| `cd src/apps/api && ruff check --no-cache app/pipeline/intro_cluster.py tests/pipeline/test_intro_cluster.py` | 0 | `All checks passed!` |
| `cd src/apps/api && ruff format --check --no-cache app/pipeline/intro_cluster.py tests/pipeline/test_intro_cluster.py` | 0 | `2 files already formatted` |
| `make verify-overlays` | 0 | `overlay-verify: PASS (PASS=19 WARN=0 FAIL=0 SKIPPED=0)` |
| `NOVA_EVAL_MODE=live pytest -p no:cacheprovider tests/evals/test_overlay_format_matcher_evals.py -v --eval-mode=live --with-judge --allow-cost` | 0 | `3 passed in 24.79s` |
| `NOVA_EVAL_MODE=live pytest -p no:cacheprovider tests/evals/test_intro_writer_evals.py -v --eval-mode=live --with-judge --allow-cost` | 0 | `10 passed in 82.37s` |

### Overlay verify expected-fail

| Field | Value |
|---|---|
| report | `.overlay-verify/report.json` |
| overall | `PASS` |
| counts | `PASS=19 FAIL=0 WARN=0 SKIPPED=0` |
| expected-fail slot/overlay | `7/0` |
| text | `font fallback must be loud` |
| requested font | `Definitely Not A Real Nova Font` |
| resolved font | `Playfair Display` |
| fallback | `true` |
| expected_failure | `{"verdict":"FAIL","reason_contains":"fallback"}` |
| expectation_matched | `true` |

### Replay measurement

| Field | Value |
|---|---|
| rows | `15` |
| selection | `first 15 eligible 3-6 word prod matcher rows from 20-row run` |
| source | `/private/tmp/nova-slice2-evidence/replay-results.json` |
| artifact | `/private/tmp/nova-slice2-evidence/replay-results-eligible15.json` |
| cluster_after | `5` |
| cluster_after_rate | `0.3333` |
| cluster_words_all_3_to_6 | `true` |

| Prod job | Hook words | Before layout | After layout | Layout source | After effect | Matched examples | Cluster word floor |
|---|---:|---|---|---|---|---|---|
| `a2a1d3c5-3aee-45ee-b347-2abb6bee39e7` | 3 | `linear` | `cluster` | `model` | `fade-in` | `energetic-payoff-cluster-01`, `fitness-grind-scaleup-01` | `true` |
| `f891019a-74c0-4886-ae56-1b8308a2b63f` | 3 | `null` | `linear` | `model` | `fade-in` | `milestone-emotional-fadein-01` | `true` |
| `49249ac1-abfb-4239-941d-c457828c2480` | 6 | `null` | `linear` | `model` | `karaoke-line` | `tutorial-tip-karaoke-01`, `transformation-before-after-karaoke-01` | `true` |
| `454d0f1d-e640-44a2-b7e6-dba67ce6bf71` | 3 | `null` | `cluster` | `model` | `fade-in` | `energetic-payoff-cluster-01`, `people-social-cluster-01` | `true` |
| `41a5fcdb-103f-4214-b5e0-76032f243591` | 6 | `null` | `linear` | `model` | `scale-up` | `energetic-payoff-cluster-01`, `fitness-grind-scaleup-01` | `true` |
| `11432066-5d2b-4c0b-a431-65d1e84dcb7a` | 3 | `null` | `linear` | `model` | `karaoke-line` | `pov-surprise-karaoke-01`, `pov-social-karaoke-01` | `true` |
| `ed671211-6fb9-46af-be53-796b72620f72` | 3 | `null` | `linear` | `model` | `scale-up` | `fitness-grind-scaleup-01`, `energetic-payoff-cluster-01` | `true` |
| `73495ae2-f353-4cd2-b978-71ac257653c0` | 6 | `null` | `linear` | `model` | `scale-up` | `adventure-humor-scaleup-02`, `fitness-grind-scaleup-01`, `energetic-payoff-cluster-01` | `true` |
| `9df59b4c-8ae5-4127-a02c-b0894a8568d3` | 3 | `null` | `cluster` | `model` | `fade-in` | `energetic-payoff-cluster-01`, `people-social-cluster-01` | `true` |
| `6f8eaa78-e504-447d-aa7f-23463325adcd` | 4 | `null` | `cluster` | `model` | `fade-in` | `energetic-payoff-cluster-01`, `people-social-cluster-01` | `true` |
| `011ab4d0-6f81-47ec-9549-6dcf9e49f514` | 3 | `null` | `linear` | `model` | `scale-up` | `pov-social-karaoke-01`, `energetic-payoff-cluster-01` | `true` |
| `fd8a6a0d-9e0b-46ab-b710-16907c7d944a` | 5 | `null` | `linear` | `model` | `scale-up` | `fitness-grind-scaleup-01` | `true` |
| `f02e2b90-d98b-4f64-8fc5-0851d9b221a2` | 5 | `null` | `linear` | `model` | `scale-up` | `fitness-grind-scaleup-01` | `true` |
| `901fe271-dc8b-46ae-80dc-c5182298658d` | 6 | `null` | `linear` | `model` | `scale-up` | `fitness-grind-scaleup-01`, `energetic-payoff-cluster-01` | `true` |
| `448c7e05-6ed9-430e-bd68-84be1434b729` | 3 | `null` | `cluster` | `model` | `fade-in` | `energetic-payoff-cluster-01`, `people-social-cluster-01` | `true` |

### Local-render proof

| Prod job | Local job | Status | Rendered outputs | Log |
|---|---|---|---|---|
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | `1fb75578-bb3e-433e-a61c-d79409a84f39` | `variants_ready` | `.local-render/1fb75578-bb3e-433e-a61c-d79409a84f39-song_lyrics.mp4`, `.local-render/1fb75578-bb3e-433e-a61c-d79409a84f39-song_text.mp4`, `.local-render/1fb75578-bb3e-433e-a61c-d79409a84f39-original_text.mp4` | `/private/tmp/nova-slice2-evidence/local-render-logs/22c0bc36.log` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | `b8b365c3-5c29-48c9-aaaf-319234c64bc8` | `variants_ready` | `.local-render/b8b365c3-5c29-48c9-aaaf-319234c64bc8-song_lyrics.mp4`, `.local-render/b8b365c3-5c29-48c9-aaaf-319234c64bc8-song_text.mp4`, `.local-render/b8b365c3-5c29-48c9-aaaf-319234c64bc8-original_text.mp4` | `/private/tmp/nova-slice2-evidence/local-render-logs/568ced7b.log` |
| `dfb9713d-b867-499b-b7f5-d0dc2eec85f5` | `f73dc75a-f678-4152-beb5-3f95285e4b98` | `variants_ready` | `.local-render/f73dc75a-f678-4152-beb5-3f95285e4b98-original_text.mp4` | `/private/tmp/nova-slice2-evidence/local-render-logs/dfb9713d.log` |
| `dfb9713d-b867-499b-b7f5-d0dc2eec85f5` | `7e7ac3bb-5dca-4a87-b58e-797bead1c0bd` | `variants_ready` | `.local-render/7e7ac3bb-5dca-4a87-b58e-797bead1c0bd-original_text.mp4` | `stdout` |

| Prod job | Local debug JSON | Intro stills |
|---|---|---|
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | `/private/tmp/nova-slice2-evidence/local-debug/1fb75578-bb3e-433e-a61c-d79409a84f39-debug.json` | `/private/tmp/nova-slice2-evidence/stills/22c0bc36/song_text_t1.jpg`, `/private/tmp/nova-slice2-evidence/stills/22c0bc36/song_lyrics_t1.jpg`, `/private/tmp/nova-slice2-evidence/stills/22c0bc36/original_text_t1.jpg` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | `/private/tmp/nova-slice2-evidence/local-debug/b8b365c3-5c29-48c9-aaaf-319234c64bc8-debug.json` | `/private/tmp/nova-slice2-evidence/stills/568ced7b/song_text_t1.jpg`, `/private/tmp/nova-slice2-evidence/stills/568ced7b/song_lyrics_t1.jpg`, `/private/tmp/nova-slice2-evidence/stills/568ced7b/original_text_t1.jpg` |
| `dfb9713d-b867-499b-b7f5-d0dc2eec85f5` | `/private/tmp/nova-slice2-evidence/local-debug/f73dc75a-f678-4152-beb5-3f95285e4b98-debug.json` | `/private/tmp/nova-slice2-evidence/stills/dfb9713d/original_text_t1.jpg` |
| `dfb9713d-b867-499b-b7f5-d0dc2eec85f5` | `/private/tmp/nova-slice2-evidence/local-debug/7e7ac3bb-5dca-4a87-b58e-797bead1c0bd-pipeline_trace.json` | `/private/tmp/nova-slice2-evidence/stills/dfb9713d-spacing-fix/original_text_t1.jpg`, `/private/tmp/nova-slice2-evidence/stills/dfb9713d-spacing-fix/forced_cluster_this_bridge_sunset_clean_t15.jpg` |

### Local-render layout events

| Prod job | Local job | Text | Requested layout | Selected layout | Layout source | Reason | Word count | Has word roles |
|---|---|---|---|---|---|---|---:|---|
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | `1fb75578-bb3e-433e-a61c-d79409a84f39` | `the bell sounds slower than the glyph. why?` | `linear` | `linear` | `model` | `explicit_linear` | 8 | `false` |
| `22c0bc36-0ef2-447c-b589-388cfabb5c34` | `1fb75578-bb3e-433e-a61c-d79409a84f39` | `the bell sounds slower than the glyph. why?` | `linear` | `linear` | `model` | `explicit_linear` | 8 | `false` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | `b8b365c3-5c29-48c9-aaaf-319234c64bc8` | `when he asked 'why put all my thoughts on my heart?'` | `linear` | `linear` | `model` | `explicit_linear` | 11 | `false` |
| `568ced7b-f0ca-49c5-8360-5387bbbbc493` | `b8b365c3-5c29-48c9-aaaf-319234c64bc8` | `when he asked 'why put all my thoughts on my heart?'` | `linear` | `linear` | `model` | `explicit_linear` | 11 | `false` |
| `dfb9713d-b867-499b-b7f5-d0dc2eec85f5` | `f73dc75a-f678-4152-beb5-3f95285e4b98` | `this bridge sunset` | `cluster` | `cluster` | `model` | `agent_pick` | 3 | `true` |
| `dfb9713d-b867-499b-b7f5-d0dc2eec85f5` | `7e7ac3bb-5dca-4a87-b58e-797bead1c0bd` | `pov: this sunset over the bridge` | `linear` | `linear` | `model` | `explicit_linear` | 6 | `false` |

### Scenic cluster rendered-size measurement

| Field | Value |
|---|---|
| source image | `/private/tmp/nova-slice2-evidence/stills/dfb9713d/original_text_t1.jpg` |
| measurement artifact | `/private/tmp/nova-slice2-evidence/scenic-cluster-size-measurement.json` |
| crop_xyxy | `[150, 650, 950, 1180]` |
| threshold | `gray>230, close kernel 35x18` |
| boxes | `[{"x":227,"y":680,"w":675,"h":169,"area":56482},{"x":303,"y":902,"w":574,"h":107,"area":44616}]` |
| distinct_box_sizes | `[[574,107],[675,169]]` |
| distinct_height_count | `2` |

### Scenic cluster spacing measurement

| Field | Value |
|---|---|
| text | `this bridge sunset` |
| roles | `["connector","hero","hero"]` |
| measurement artifact | `/private/tmp/nova-slice2-evidence/scenic-cluster-spacing-y-tight-measurement.json` |
| clean forced-cluster still | `/private/tmp/nova-slice2-evidence/stills/dfb9713d-spacing-fix/forced_cluster_this_bridge_sunset_y_tight_clean_t15.jpg` |
| live rerender still | `/private/tmp/nova-slice2-evidence/stills/dfb9713d-spacing-fix/original_text_t1.jpg` |
| live rerender video | `.local-render/7e7ac3bb-5dca-4a87-b58e-797bead1c0bd-original_text.mp4` |
| collisions | `0` |
| this_bridge_gap_x_frac | `0.025` |
| this_bridge_overlap_y_frac | `0.037491` |
| this_bridge_center_y_delta_frac | `0.012` |
| bridge_sunset_overlap_x_frac | `0.404994` |
| bridge_sunset_gap_y_frac | `0.008665` |

## Next slice spec

### Slice 1 — "Make prod text-overlay degradation observable and loud" (FROZEN 2026-06-12)

#### What to build

One PR. Backend-only instrumentation + verification hardening:

1. **Pipeline trace events at every text-degradation decision point**, visible in `/admin/jobs/{id}`:
   - `intro_layout_selected` — chosen layout (editorial cluster vs linear/flat), and why (agent pick, signal-free demotion, fallback).
   - `cluster_roles_derived` — per-word roles + which contrast guarantees from #498 fired.
   - `cluster_shrink_applied` — shrink factor when the cluster-atomic shrink runs.
   - `font_resolved` — per overlay: requested `font_family` vs actually-resolved typeface; emitted at warning level when they differ (fallback fired).
   - Emit via `record_pipeline_event` (`app/agents/_runtime` / pipeline trace layer); must no-op gracefully when called outside a `pipeline_trace_for` context (overlay_verify CLI, unit tests).
2. **Loud font fallback in `overlay_verify`:** extend `app/pipeline/overlay_verify.py` + `report.json` to record `resolved_typeface` per overlay and FAIL when a requested font fell back. Add a negative fixture requesting an unknown font that must fail. Add a positive assertion to the editorial fixtures: when an editorial cluster is requested, the rendered cluster has ≥2 distinct text sizes (the #498 invariant, checked at the rendered-output layer — test rendered output, not scheduler integers).
3. **Prod evidence run (the "understand the source" deliverable):** pick 2–3 recent real prod generative/plan jobs whose output looks "basic text lines" (pull candidates via `python scripts/admin.py --prod GET /admin/generative`). For each: re-render through `make local-render` (prod image), capture the new decision events + first-frame stills of intro text (prod output vs re-render), and write a mechanism-conviction table (job → which mechanism fired: input-driven fallback / font fallback / flag drift / other) into "Last slice results" above — raw data only.
4. **Flag parity table:** record the values of `TEXT_RENDERER_SKIA_ENABLED`, `text_overlay_v2_enabled`, `SINGLE_PASS_ENCODE_ENABLED`, `ORIENTATION_NORMALIZE_ENABLED` in prod (Fly) vs `.env.local-render` vs dev `.env` into the same results section.

#### Hard acceptance criteria (frozen before work starts)

1. `cd src/apps/api && pytest tests/pipeline/test_intro_cluster.py tests/pipeline/test_text_overlay_skia.py tests/pipeline/test_overlay_verify.py` passes with no skips, including NEW tests: (a) unknown `font_family` emits a fallback `font_resolved` event; (b) `intro_layout_selected` + `cluster_shrink_applied` events are emitted with documented payload shapes; (c) event emission outside a trace context does not raise.
2. `make verify-overlays` exits non-zero on the new unknown-font fixture and `report.json` contains `resolved_typeface` for every overlay in every fixture; all pre-existing fixtures still PASS.
3. "Last slice results" contains, for ≥2 real prod jobs: job ID, per-variant `intro_layout_selected`/`font_resolved` event dumps, the flag-parity table, frame-still file paths, and a one-line mechanism verdict per job (table form, no narrative).
4. A locally-run generative job shows the new events in the `/admin/jobs/{id}` debug view (paste the event-list JSON from the debug endpoint into "Last slice results" as evidence).
5. Full existing suites green: `pytest` (api), `ruff check`, frontend untouched.

#### Explicit out-of-scope (builder must NOT touch)

- NO new layout primitives: no per-word multi-color, no time-varying/color-change text, no multi-font-per-overlay. That is Slice 3.
- NO prompt file edits under `src/apps/api/prompts/` (would trigger prompt_version bumps + live evals).
- NO changes to `intro_cluster.py` geometry/role rules beyond event emission — #498's logic is freshly verified; do not "improve" it.
- NO encoder/preset changes; NO CI workflow changes (making verify-overlays a CI gate is a later slice); NO frontend changes; NO DB migrations.

#### Reality checks (verify against the repo before writing code)

- Confirm `record_pipeline_event` call contract and that renderer-level code (`text_overlay_skia.py`, `intro_cluster.py`) executes inside the orchestrator's `pipeline_trace_for(job_id)` context (mandatory contract in CLAUDE.md) — if any render path runs in a subprocess, events from it will silently drop; design around that.
- Check `.github/workflows/layer2-cache-guard.yml` path triggers — if touched files match, bump `TEXT_OVERLAY_VERSION_V2` in `template_cache.py` or use the documented escape hatch consciously.
- `overlay_verify` runs via CLI inside the prod image with no job/DB — the `resolved_typeface` reporting must not depend on the trace layer.
- Renderer-parity invariant: any new burn-dict field must be honored by BOTH renderers (`test_both_renderers_honor_text_anchor_left` pattern) — instrumentation-only fields should bypass the burn dict where possible.
- Work from a fresh worktree (`nova-fresh` / `scripts/new-session.sh`), never the shared checkout.

#### Slice preview (not frozen — orientation only)

- **Slice 2:** fix whatever mechanisms the Slice 1 evidence convicts (font registry/image gap, flag drift, further input-robustness), with before/after prod re-renders as proof.
- **Slice 3:** editorial capability build toward the 3 TikTok references — multi-block layouts beyond the intro, per-word highlight colors in the Skia path (span support exists only in Pillow today), and the text color-change effect (feasible via the Skia per-frame PNG sequence machinery — time-varying paint per frame).
