# Vendored: @blossom-carousel/web

- **Package:** `@blossom-carousel/web`
- **Version:** `1.4.2` (latest on npm as of 2026-08-04)
- **Author/maintainer:** Jesper Vos (`jespervos`), https://github.com/jespervos/blossom-carousel
- **Homepage:** https://www.blossom-carousel.com
- **License:** upstream inconsistency — `package.json` (and the npm registry) declare MIT, but the LICENSE file shipped in the tarball (and the GitHub repo root) is Apache-2.0. Both are permissive; we preserve the shipped `lib/blossom-vendor/LICENSE` verbatim, which satisfies attribution under either. Re-check before any external redistribution of this directory.
- **Fetched via:** `npm pack @blossom-carousel/web@1.4.2` (no CDN — vendored into this repo so capture is network-free)

## Why `@blossom-carousel/web`, not `@blossom-carousel/core`

`jespervos` publishes the carousel as three packages under one monorepo:

| package | what it is |
|---|---|
| `@blossom-carousel/core` | framework-agnostic drag/scroll engine, no custom element |
| `@blossom-carousel/web` | the `<blossom-carousel>` **web component** (wraps `core`), plus `<blossom-prev>`/`<blossom-next>`/`<blossom-dots>` | `@blossom-carousel/react` | React bindings |

The task called for "the WEB-COMPONENT or CORE browser build" and the vendored package's own docs use `<blossom-carousel>` throughout the examples site (`docs/examples/*`) — so `@blossom-carousel/web` is what all four reference pages here import. `@blossom-carousel/core` is a dependency baked directly into `web`'s bundle (confirmed by an inlined `//#region .../@blossom-carousel+core@1.1.8/...` comment at the top of the vendored `.es.js` — no separate fetch needed).

## What was vendored

Extracted from the npm tarball into `lib/blossom-vendor/`:

- `dist/blossom-carousel-web.es.js` — ES module build (self-contained; no bare-specifier `import`s, verified with `node --check`). Loaded via `<script type="module">import "./lib/blossom-vendor/dist/blossom-carousel-web.es.js"</script>` in each effect page — this is what registers the `blossom-carousel` custom element.
- `dist/blossom-carousel-web.css` — base component styles (scoped inside `@layer blossom-carousel`, so any unlayered page CSS — which is everything the four effect pages declare — wins the cascade on a per-property basis without needing `!important`).
- `dist/blossom-carousel-web.umd.js` — kept for reference/non-module fallback; not used by the reference pages (they all load the ES build).
- `package.json`, `README.md`, `LICENSE`, `CHANGELOG.md` — for version pinning / provenance / license compliance.

`dist/src/*.d.ts` (TypeScript declarations from the tarball) were not copied — irrelevant to a plain-JS browser harness.

## Canonical CSS reused vs. authored

Per the task's instruction to prefer Blossom's own example CSS where it's canonical, and note mismatches against `effects.py` (still a stub at the time this lane ran — see below):

| page | source | what was reused | what was authored/adapted |
|---|---|---|---|
| `scale-sweep.html` | authored | — (no dedicated "scale sweep" example exists on the docs site; closest is `advanced/smart-stack`'s plain `scale(0.8→1→0.8)` sweep, which has no opacity fade or asymmetry) | Full `@keyframes scale-sweep` written directly from the task's literal spec (0%/50%/100% scale+opacity), using `transform: scale()` instead of the standalone `scale` CSS property — see note below. |
| `cover-flow.html` | `docs/examples/advanced/cover-flow` | The `li`/`view-timeline`/`animation-range: contain` **structure** (per-card view-timeline, `perspective`, rotateY+translateZ+scale in one `transform`) | Numeric values: canonical uses `rotateY(±55deg)` + a `translateX(±30%)` slide-in on a wrapper `.slide` layer; this page uses `rotateY(±35deg)` (matching `effects.py`'s intended `rotate_y_deg = -35 * n` naming) with no separate slide layer, and adds an animated `z-index` (canonical cover-flow relies on `perspective`+scale falloff alone, no z-index) borrowed from the **flipbook**/**cards** examples' `sibling-index()` pattern, per the task's explicit preference for animated z-index. |
| `cards.html` | `docs/examples/advanced/cards` + `docs/examples/advanced/smart-stack` | The `position: sticky` centering trick and the two-animation split (`stack-z` for `z-index` via `sibling-index()`, `stack-transform` for the visual pose) | Canonical `cards` example drives translateX by **percentage** (`-80%`→`0%`→`80%`) plus `rotate()`, on a `grid-auto-columns: 100%` track where `left: calc(var(--card-width) * -1)`. This page keeps the task's fixed 540×720/48px-gap/270px-padding flex layout (visible neighbors, not one-card-at-a-time), so the sticky inset is `left/right: 270px` (= `(1080 viewport − 540 card) / 2`, i.e. this layout's own centering padding) instead of a negative `var(--card-width)`. Depth motion uses **px**, not `%`/`rotate()`: `24px` translate step, `0.94` (`1 − 0.06`) scale step, `1.6×` exit multiplier, per the task's numeric spec. |
| `flipbook.html` | `docs/examples/advanced/flipbook` | Same sticky-centering + `perspective` + `sibling-index()` z-index split as `cards.html`; transform shape (`translateZ` dip + `rotateY` sweep) | Canonical flipbook has 7 keyframe stops (`0/25/37.5/50/62.5/75/100%`) mixing `translateX` (%) with `translateZ`/`rotateY`; this page uses the task's literal 3-stop spec (`0%{translateZ(-200px) rotateY(-35deg)} 50%{none} 100%{translateZ(-200px) rotateY(35deg)}`), with `z-index: 1000` split out into its own `flip-z` animation (task described it inline on the transform keyframe at 50%, which isn't how CSS keyframes can express "hold at max z-index only at the midpoint" cleanly alongside a 3D transform — Blossom's own canonical examples do exactly this two-animation split for the same reason). |

## `effects.py` mismatch note

`src/apps/api/app/pipeline/carousel/effects.py` is **still a stub** in this worktree (`*_transform` functions all `raise NotImplementedError`, no `STACK_*`/rotation/scale constants committed yet — presumably another lane's in-flight work). This lane could not diff against real Python constants, so every numeric choice above (`STACK_STEP_PX = 24`, `STACK_SCALE_STEP = 0.06`, `1.6×` exit multiplier, `±35deg` cover-flow/flipbook rotation, the `0.5`/`0.35` scale-sweep floor) comes straight from this task's own prose spec, which is explicitly framed as mirroring `effects.py`'s intended constants. **Per the task's instruction, the browser is ground truth** — when `effects.py` lands for real, tune its constants to match what's captured here, not the other way around.

## Standalone-`scale` CSS property gotcha (applies to all four pages)

`harness.js`'s per-frame trace reads each card's pose via `getComputedStyle(card).transform` (fed into a `DOMMatrixReadOnly`). The individual `scale`/`rotate`/`translate` CSS properties (as opposed to the `transform` property's `scale(...)`/`rotate(...)`/`translate(...)` *functions*) do **not** show up in `getComputedStyle().transform` per spec — they compose only at render/used-value time. Every keyframe in every page therefore uses `transform: <functions>`, never the bare `scale:`/`rotate:`/`translate:` shorthand properties, even where the task's prose examples wrote the shorthand form (e.g. `scale-sweep`'s `0%{scale:.5}`) — that prose was informal shorthand for "the scale factor," not a literal instruction to use the standalone property.
