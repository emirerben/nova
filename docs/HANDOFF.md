# HANDOFF.md

_This file is the shared brain between architect and builder sessions. The builder writes raw results here after each work block. The architect reads and judges it at the start of each session._

## Last completed slice

**Slice 2 — "Fire the editorial cluster in prod + never render refusal text"** shipped as PR #507 (`951ad5e6`, 2026-06-12). `overlay_format_matcher` now picks `cluster` on hook-shape grounds (broadened `match_overlay_format.txt` + rebalanced `overlay_examples.json`); `layout_source` distinguishes a real model pick from a coerced default; `intro_writer.parse()` raises `RefusalError` on refusal/meta text and the orchestrator (`_run_text_agents` → `_fallback_intro_text`) renders a deterministic clip-metadata fallback hook (`"watch {core} unfold"`); and the `verify-overlays` gate now has expected-fail semantics (exits 0). Judged **PARTIAL** by architect session #3 (2026-06-13) — see "Acceptance criteria for last slice".

**Slice 1 — "Make prod text-overlay degradation observable and loud"** shipped earlier as PR #505 (`6718297b`, 2026-06-12); judged **PASS 5/5** by architect session #2. (Slice 1 raw results below under "Slice 1 results (raw)".)

## Key decisions made

- **Diagnosis before capability (architect, 2026-06-12).** Goal is editorial-grade text overlays (stacked multi-block text, mixed fonts/sizes, text color-change effect — three TikTok references on file) AND closing the "great locally, basic in prod" gap. Slice order frozen: 1) make degradation observable + convict the mechanism on real prod jobs, 2) fix convicted mechanisms, 3) build the editorial/color-change capabilities. Rationale: PR #498 proved the dominant failure class is input-dependent silent degradation (signal-free Turkish hook flattened the editorial cluster to plain lines, invisibly); building new capability first would flatten in prod the same way.
- **Ranked divergence mechanisms (evidence in architect session #1):** (1) input-dependent silent degradation — no pipeline events for layout selection / role derivation / shrink; (2) silent font fallback — `_typeface_for_overlay` (`text_overlay_skia.py:187-218`) and Pillow `ImageFont.load_default()` fall back with zero signal; (3) environment divergence per `docs/runbooks/local-render.md` incl. feature-flag drift between Fly secrets and local env; (4) process gap — `make verify-overlays` is manual-only, CI never renders through the prod image. Encoder drift ruled unlikely (locked by `tests/test_encoder_policy.py`).
- **Slice 1 verdict on those mechanisms (architect session #2, 2026-06-12):** font fallback **exonerated** (0/19 prod overlays fell back); flag drift **exonerated** (parity table consistent across prod / local-render / dev). **Convicted: upstream layout selection** — all evidence jobs showed `requested_layout=linear, reason=explicit_linear, has_word_roles=false`. Root causes in repo: (a) `prompts/match_overlay_format.txt` restricted `cluster` to calm/scenic content; (b) `prompts/overlay_examples.json` was 19 linear vs 3 cluster; (c) `overlay_format_matcher.py` silently coerced a missing/invalid `layout` to `linear`, indistinguishable from an explicit pick. The renderer is faithful — "basic in prod" was a policy/prompt problem, not a render problem. (All three fixed in Slice 2.)
- **Refusal-text leak convicted (architect session #2):** prod job `22c0bc36` rendered the literal intro "i need more information to write this hook". `intro_writer.parse()` guarded only by sanitization + 12-word/80-char clamp; an 8-word refusal string passed. Fixed in Slice 2 (`_REFUSAL_PATTERNS` → `RefusalError` + orchestrator fallback).
- **Cluster eligibility broadened content-agnostically (architect taste call, 2026-06-12):** moved from "calm/scenic only" to hook-shape-driven (3–6 strong words, visual fit) for any content class; linear stays right for karaoke-momentum and wordy hooks; Turkish conservatism stays. Shipped in Slice 2.
- **Slice 3a scope + "engine owns typeface pairing" (architect session #3, 2026-06-13).** Slice 3 (editorial build toward the 3 refs) decomposed; **3a = mixed-typeface cluster lockup** chosen first (Emir, 2026-06-13) as the cheapest/lowest-risk foundation. Grounded in session-#3 exploration: cluster blocks are already **separate Skia draws** each carrying their own `font_family` (`generative_overlays.py:278-309`), and `font_family` is already honored by both renderers — so per-block font pairing needs **no Skia span work and no new burn-dict field**. Decision: the **engine** owns the pairing (extends "agent annotates, engine owns geometry"), so no agent/prompt change, no `prompt_version` bump, no live evals. Color sweep (3b) and scene-adaptive palette (3c) deferred; font additions (italic serifs — none bundled today) are a separate conscious slice due to 6-point registry coupling.

## Visual targets (reference analysis — architect, 2026-06-12)

The three TikTok references are captured locally at `.sources/tiktok-refs/` (gitignored — never commit; re-fetch via yt-dlp/gallery-dl if lost). This section is the durable spec-input distilled from them; Slice 3 is judged against it.

**Ref 1 — @denocampo_/photo/7352488708433612037 (`ref1-fonts-carousel/`, 20 slides).** A "my favorite fonts" carousel (all free, dafont.com). Font list, in slide order: Birds of Paradise (script), Apple Garamond, Forward Serif, Dream Orphans, CODIGRA, Roseblue, Motena (Golden), Recoleta, Favorite Notification, Amelina Script, Summer Dreams, Creato, Coolvetica, Couture, TTPhobos, MADESOULMAZE, Marola, Helvetica Neue. Shared aesthetic: warm cream/off-white text (NOT pure white) over moody warm photos; soft glow; **roman+italic mixed within one two-line lockup** (line 2 italic and right-shifted); layered-repetition effects (same word ×3 in different weights/offsets — Creato, MADESOULMAZE, Couture). Matches the existing taste memory: editorial serifs, sans reads cheap.

**Ref 2 — @mafeanzures/video/7610489921840614686 (`ref2-kinetic-frames/`, 1fps, 78s).** Word-synced kinetic captions: ONE word/short phrase on screen at a time, each phrase at a different position AND scale, and — the key capability — **each phrase can use a DIFFERENT font** (heavy sans for "that"/"really"/"little", ornate script for "Magical Night", condensed serif for "automatically cool", bold-sans-with-small-subline for "my fonts / which"). It's literally a video about her edit fonts (shows dafont.com). Target capability: per-phrase font/scale/position variation across a timed caption sequence — not one global caption style.

**Ref 3 — @salvadorlerma/video/7646572293430217998 (`ref3-colorsweep-frames/`, 2fps, 26s; Peanuts edit).** Lyric typography with three distinct capabilities: (1) **progressive color sweep** — letters transition white→blue (or navy→accent) sweeping across the word as the lyric is sung (see `ref3_036.png` "it's someone that you", `ref3_048.png` "your / best friend"); (2) **mixed-font lockup within one lyric line** — plain sans "your" + large serif-italic "best friend"; (3) **scene-adaptive palette** — navy text on the bright-yellow scene, white on dark scenes. This is the "test color change effect" Emir called out.

**Capability gaps these imply (vs. current engine, from architect session #1 exploration):** one overlay = one font + one base color (no per-word font/color mix in the Skia path — span support exists only in Pillow); no time-varying color (feasible: Skia already renders per-frame PNG sequences, so per-frame paint is the lever); no scene-adaptive text palette; word-cluster intro covers multi-block size hierarchy but only for the intro. Fonts: several reference fonts (Garamond-class, Recoleta-class soft serif) have close bundled analogs (EB Garamond, Fraunces, Playfair, Bodoni Moda, Cormorant); true script (Amelina/Birds of Paradise class) is thin in the bundle (Great Vibes, Pacifico) — Slice 3 should decide additions consciously (font-registry coupling: registry-embeddings.npz + sync:fonts mirror + 2 count tests).

## Open disagreements

- **Slice 2 geometry scope expansion (architect session #3 verdict: ACCEPT, FLAG process).** Slice 2 changed `intro_cluster.py` geometry (`_HERO_STEP_RATIO` 0.95→1.08, `_CLOSER_STEP_RATIO` 0.62→0.92, new `_BLOCK_GAP_FRAC` + collision-resolution block) despite the Slice 2 freeze stating "NO `intro_cluster.py` geometry/role-rule changes." **Accepted** as functionally necessary — once the cluster fired on real 3-word hooks the connector collided with the first hero (`bridge_sunset_overlap_x_frac: 0.40`), so criterion 5 was unshippable without it; it is tested (`test_connector_never_collides_with_first_hero_regression`). **Flagged:** this should have surfaced as a PHASE 0 disagreement, not been silently absorbed. The Slice 2 geometry is now **frozen** — Slice 3a must not re-tune it.
- **PHASE 0 discipline re-asserted (sessions #2 + #3).** Slice 1 came back with zero PHASE 0 disagreements; Slice 2's PHASE 0 was never recorded in the repo (the HANDOFF divergence below). For Slice 3a: PHASE 0 MUST record EITHER file-cited disagreements OR a per-reality-check "verified, matches repo at `<file:line>`" list, and the result-recording HANDOFF update MUST paste the PHASE 0 reply verbatim. A PHASE 0 with neither is a failed slice.

> **⚠️ HANDOFF divergence (architect session #3, reconciled 2026-06-13).** Architect session #2's judgments (this section + the Slice 1 PASS table + the frozen Slice 2 spec) were never committed — they lived only as uncommitted edits in the shared checkout. PR #507 appended its Slice 2 raw results onto the stale bootstrap version, so this file's framing read "Last completed slice: None" until session #3 reconciled it here. Root cause: builder worked from a stale base (the worktree hazard CLAUDE.md warns about). This file is now the single canonical brain — keep architect verdicts AND builder raw results in it, on `main`.

## Acceptance criteria for last slice

Slice 2, frozen by architect session #2 (2026-06-12), judged by architect session #3 (2026-06-13): **PARTIAL** (5/6 fully met; criterion 6 partial; one accepted-but-unflagged scope expansion — see Open disagreements). Slice 1 was judged **PASS 5/5** by session #2 (settled; #505 shipped).

| # | Criterion (frozen) | Verdict | Evidence |
|---|---|---|---|
| 1 | `-k "overlay_format or intro_writer or overlay_verify"` passes no-skip incl. 3 new tests (refusal raises; coerced_default; orchestrator safe fallback) | PASS | `86 passed, 4989 deselected` exit 0. Tests by name: `test_literal_prod_refusal_text_raises_refusal`, `test_layout_source_threads_from_matcher_output`, `test_run_text_agents_refusal_returns_safe_fallback` (+ wrapped variant). `_fallback_intro_text()` → clip-metadata hook, never meta-text, never hard-fail. |
| 2 | `make verify-overlays` exits 0; unknown-font as expected-fail | PASS | exit 0, `PASS=19 FAIL=0`; `expectation_matched: true`; locked by `test_unknown_font_family_fixture_is_expected_fail` + `test_expected_fail_fixture_fails_if_expectation_stops_matching`. |
| 3 | ≥15 replay inputs; cluster after-rate ≥30%; 100% of cluster picks 3–6 words; before-rate reported | PASS | 15 rows; `cluster_after_rate 0.3333`; all 5 cluster picks 3–6 words; `cluster_words_all_3_to_6: true`. Soft note: "before" column mostly `null` (no prior recorded layout) → before-rate weakly evidenced ~0% cluster. |
| 4 | Live evals pasted; `prompt_version` bumped per edited prompt | PASS | `overlay_format_matcher` 3 passed / `intro_writer` 10 passed, both `--with-judge` live; both `prompt_version` → `2026-06-12`; `layout_source` plumbed. |
| 5 | 3 jobs re-rendered; ≥1 selects cluster; rendered intro ≥2 distinct sizes at pixel layer | PASS | `dfb9713d` selected `cluster` (`agent_pick`, 3 words, `has_word_roles=true`); `distinct_height_count: 2` via overlay-verify pixel bbox; `collisions: 0`. |
| 6 | Full suites green; explicit `intro_layout` override honored (cite a test) | PARTIAL | ruff clean. One fully-green `pytest tests/` (`5017 passed` exit 0), but a second run had `11 failed`, only **9** re-verified (redis/celery flakes) → **2 unexplained**; and the **override-test citation is missing** (evidence shows matcher-chosen linear, not the explicit-override path). |

## Slice 1 results (raw)

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

### Slice 3a — "Editorial multi-typeface cluster lockup" (FROZEN 2026-06-13, architect session #3)

**Outcome:** the editorial word-cluster intro renders with a deliberate, taste-curated **per-block typeface pairing** (≥2 distinct typefaces in one lockup) so prod clusters read like the references' mixed-font lockups instead of one uniform font — using **only existing bundled fonts**, with **no new burn-dict field, no color animation, no font additions.**

**Why this is the right first capability (grounded in architect session #3 exploration):**
- Cluster blocks are **already separate Skia draws** — each is its own overlay dict carrying its own `font_family`/`text_size_px`/`text_color` (`generative_overlays.py:278-309`, `build_intro_overlay`). Per-block font is a localized change, **not** a Skia span rewrite (Skia has no intra-line span support; only Pillow does).
- `font_family` is **already honored by both renderers**, so a per-block font pairing introduces **no new burn-dict field** and does not trip the renderer-parity invariant.
- The engine already assigns the connector a "Regular" sibling via `_connector_font` (`intro_cluster.py:362-375`) — this slice generalizes that into a curated pairing.

#### What to build (builder decides PR breakdown — expected: one PR)

1. **Engine-owned per-block typeface pairing.** Generalize `_connector_font` / `compute_cluster_blocks` (`intro_cluster.py`) so the **connector** (and optionally **closer**) block resolves to a deliberately *contrasting* bundled family rather than just the hero's Regular sibling. The **engine owns the pairing** ("agent annotates, engine owns geometry" → now "engine owns typeface pairing"). The agent keeps emitting only the hero `font_family`/`font_style`; **do not** add a per-block-font field to any agent output → no prompt change, no `prompt_version` bump, no live-eval cycle.
2. **Single-source-of-truth pairing table.** A curated map `hero family → {connector, closer}` family in ONE place (extend `font-registry.json` with a `cluster_pairing` block, or a constant in `intro_cluster.py` — builder's call, exactly one home). Taste-curated per the font memory (editorial serifs; sans reads cheap) and the "Visual targets" notes (warm serif hero + script/lighter connector). Every referenced family MUST exist and be non-deprecated in `font-registry.json`.
3. **Turkish-safe pairing (hard requirement).** Script/handwriting faces (Great Vibes, Pacifico, Satisfy) likely lack Turkish diacritics (ı, ş, ğ, ç, ö, ü) → tofu. When the hook language is `tr` (or the connector face fails glyph coverage for the actual text), the engine MUST fall back to a Turkish-safe pairing (serif+serif, e.g. Playfair Display + Instrument Serif). Verify via the font's `cmap`, not by assumption.
4. **Rendered-output proof.** No new instrumentation — `font_resolved` already records `resolved_typeface.file` per block. Re-render ≥1 cluster-eligible prod job (reuse scenic `dfb9713d` from Slice 2) through `make local-render`; capture the cluster blocks' events + an intro still + montage into "Slice 3a results".

#### Hard acceptance criteria (frozen before work starts)

1. **Pairing logic test** (new, `tests/pipeline/test_intro_cluster.py`): a hero+connector(+closer) cluster gets **≥2 distinct `font_family`** across blocks, **deterministically** from the curated table (same input → same pairing; not random), for ≥2 different hero families.
2. **Pairing-integrity test** (new): every `(connector, closer)` family referenced by the table resolves to an existing, **non-deprecated** `font-registry.json` entry (no pairing points at a missing/deprecated font).
3. **Turkish-safety test** (new): a `tr`-language cluster (or a connector face lacking coverage for the block text) yields a pairing whose connector/closer faces have full `cmap` coverage of the rendered characters — asserted by loading the resolved `.ttf` and checking codepoints, not by hard-coding a family name.
4. **Rendered proof in "Slice 3a results":** for ≥1 re-rendered cluster-eligible prod job, the `font_resolved` events for the cluster blocks show **≥2 distinct `resolved_typeface.file`**; intro still + montage paths recorded.
5. **No new burn-dict field; parity + gate green:** `make verify-overlays` exits 0; renderer-parity tests pass unchanged; `git grep` confirms no new key threaded into the burn dict for this feature.
6. **Full suites green + no non-cluster regression:** `cd src/apps/api && pytest` exit 0 (re-verify ALL failures if any flake appears — leave none unexplained); `ruff check` + `ruff format --check` clean. A test proves **linear/non-cluster overlays render byte-identically** (pairing applies only to `layout == "cluster"`); cite the existing or a new test.

#### Explicit out-of-scope (builder must NOT touch)

- **NO color work:** no per-word/per-block color override, no time-varying color/color sweep (Slice 3b), no scene-adaptive palette (Slice 3c).
- **NO font additions:** no new `.ttf`, no `registry-embeddings.npz` regen, no `sync:fonts` run, no italic-serif additions. Use only the 39 active bundled fonts. (True roman+italic needs italic faces we don't have — a conscious later slice; 6-point coupling: registry JSON + `.npz` + web mirror + 2 count tests.)
- **NO Skia intra-line span support** — single linear overlays stay single-font; this slice is cluster-blocks-only.
- **NO `intro_cluster.py` geometry/spacing changes** — the Slice 2 collision geometry (`_HERO_STEP_RATIO`, `_CLOSER_STEP_RATIO`, `_BLOCK_GAP_FRAC`, collision resolution) is **frozen**. Touch only per-block **font** assignment.
- **NO agent/prompt changes** (engine owns the pairing) → no `prompt_version` bump, no live evals. If the builder believes the agent MUST pick fonts, that is a **PHASE 0 disagreement to raise**, not a silent scope addition.
- NO frontend changes; NO DB migrations; NO CI workflow changes.

#### Reality checks (verify against the repo before writing code)

- Cluster blocks are separate overlay dicts each carrying `font_family` (`generative_overlays.py:278-309`, `build_intro_overlay`).
- `_connector_font` (`intro_cluster.py:362-375`) + the `typeface_cache`/`_typeface` flow (`intro_cluster.py:462-471`) — extend, don't fork.
- `font-registry.json` `fonts` entries + `deprecated` flags + `style_defaults` — `serif_italic` maps to **non-italic** Instrument Serif; the table must reference only active families.
- `font_resolved` already records `resolved_typeface.file` per block (Slice 1) → criterion 4 needs no new event.
- `_LANGUAGE_HINTS` / `tr` handling in `intro_writer.py` + `overlay_format_matcher.py` for where the language signal is available to gate the Turkish-safe pairing.
- Work from a **fresh worktree** (`nova-fresh slice3a-mixed-typeface`) off `origin/main` — never the shared checkout (the Slice 2 HANDOFF divergence was caused by stale-checkout drift).

#### Slice preview (NOT frozen — orientation only)

- **Slice 3b:** progressive color sweep (Ref 3) — time-varying per-frame paint over the existing per-frame PNG sequence; builds on the karaoke per-word-color path that already exists in both renderers. Highest render risk; likely its own multi-PR effort.
- **Slice 3c:** scene-adaptive text palette — pick text color from scene luminance (navy on bright, cream on dark); pre-render decision, low render risk.
- **Font additions (separate, conscious slice):** add italic serif + a Turkish-safe script face to unlock true roman+italic lockups (Ref 1), paying the full 6-point font-registry coupling cost.

## Slice 3a results (raw)

_Builder fills this in after the work block: tables, numbers, test output, the `font_resolved` events showing ≥2 distinct `resolved_typeface.file` across cluster blocks, intro still + montage paths. No interpretation. Paste the PHASE 0 reply verbatim above it._
