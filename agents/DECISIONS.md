# Nova — Technical Decisions

> Key decisions logged here until ARCHITECTURE.md is written. Format: date, decision, why, revisit trigger.

---

## [2026-03-22] Monorepo over separate repos

**Decision:** Single repo `emirerben/nova` with `apps/web` + `apps/api`
**Why:** Frontend and backend API contract will change constantly early on. Monorepo eliminates cross-repo PRs for contract changes. Two-person team doesn't need the isolation overhead.
**Revisit if:** API needs to be licensed/distributed separately, or team grows >5 engineers.

---

## [2026-03-22] FastAPI over Flask

**Decision:** FastAPI for the Python backend
**Why:** Async-native (important for job status streaming), auto-generates OpenAPI docs (aids frontend/agent integration), faster than Flask for our use cases.
**Revisit if:** team has strong Flask expertise or a library we need is Flask-only.

---

## [2026-03-22] FFmpeg subprocess over MoviePy

**Decision:** FFmpeg via `subprocess.run()` directly, not MoviePy
**Why:** MoviePy's `VideoFileClip` buffers the entire video into RAM. A 2GB source video = OOM crash. FFmpeg streams. Existing `~/src/vid-to-audio/` project is the cautionary example.
**Revisit if:** never. This is a permanent constraint.

---

## [2026-03-22] GitHub under emirerben (personal)

**Decision:** Repos live at `github.com/emirerben/nova` and `github.com/emirerben/nova-workspace`
**Why:** Fastest setup, no new org to create. ybyesilyurt is collaborator.
**Revisit if:** Nova incorporates, or we add a third engineer.

---

## [2026-03-27] Interstitials as separate clips, not xfade parameters

**Decision:** Render interstitials (curtain-close, black hold, white flash) as standalone video clips inserted between template slots, rather than encoding them as xfade transition parameters.
**Why:** xfade can only blend two adjacent clips. Curtain-close is a three-phase effect (bars closing, hold, next clip) that needs its own timeline segment. Separate clips also make beat-snap accounting explicit (cumulative_s tracks total duration).
**Revisit if:** FFmpeg adds native curtain-close xfade type, or performance requires fewer concat segments.

---

## [2026-03-27] Playfair Display over Montserrat for editorial overlays

**Decision:** Bundle Playfair Display (Bold + Regular) as the primary editorial font. Montserrat retained for font-cycle contrast.
**Why:** Playfair's serif forms are more readable at mobile text sizes and signal editorial quality. Sans/serif contrast during font-cycle adds visual variety. ASS subtitle filter uses `fontsdir` to discover bundled .ttf files.
**Revisit if:** user testing shows readability issues on specific devices, or font-cycle contrast feels jarring.

---

## [2026-03-27] geq pixel expression over drawbox for curtain-close animation

**Decision:** Use FFmpeg `geq` filter to animate curtain-close bars, not `drawbox`.
**Why:** drawbox's `h`, `w`, `x`, `y` parameters are static expressions that do NOT have access to the `t` (timestamp) variable. Only drawbox's `enable` expression can reference `t`, but that only toggles visibility, it cannot animate bar height over time. `geq` evaluates per-pixel per-frame with full access to `T` (timestamp), `X`, `Y`, `H`, `W`. Performance cost is mitigated by splitting the clip: stream-copy the prefix, geq-process only the short tail segment.
**Revisit if:** FFmpeg adds timestamp support to drawbox geometry expressions, or a lighter filter gains per-frame expression evaluation.

---

## [2026-03-27] Font-cycle timing: separate multi-PNG timestamps from overlay timestamps

**Decision:** Font-cycle overlays generate their own `start_s`/`end_s` timestamps per PNG frame. `_burn_text_overlays()` must not overwrite these with the parent overlay's single timestamp.
**Why:** A font-cycle overlay produces N PNGs with precise per-frame timing (e.g., 0.15s intervals). The previous code had a 1:1 timing reassignment loop that overwrote all font-cycle timestamps with the single overlay's `start_s`/`end_s`, collapsing every frame to the same time window. This caused all font-cycle PNGs to display simultaneously instead of sequentially.
**Revisit if:** overlay rendering is refactored to a single-pass approach (e.g., ASS-only rendering for font-cycle).

---

## [2026-03-27] Gemini vocabulary translation layer

**Decision:** Map Gemini's human-friendly transition names (whip-pan, zoom-in, dissolve) to internal FFmpeg xfade types via `translate_transition()`, rather than constraining Gemini's output vocabulary.
**Why:** Gemini produces better creative direction when using natural film terminology. The translation layer is 10 lines and easy to extend. Unknown types default to "none" (hard-cut) for safety.
**Revisit if:** the vocabulary mapping grows beyond 20 entries, or Gemini starts generating types that don't map cleanly.
