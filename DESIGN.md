# Nova Design System

This document is the **calibration target** for all design reviews and the token source for implementation.
Consumers: `/plan-design-review` and `/design-review` skills, implementers, and AI agents.
**Rule:** any change to the design system must update this document in the same PR. The doc codifies shipped reality — open a new PR to change the system, then update the doc in that same PR.

---

## §1 The three surfaces at a glance

| Surface | Canvas | Accent | Type | Mood |
|---|---|---|---|---|
| Landing (`/`) | cream `#fafaf8` | lime-700 family | Fraunces headings | light editorial |
| Light product (`/plan`, `/plan/items/`, `/library`, `/generative`) | cream `#fafaf8` / ink / lime | lime-700 | Fraunces headings | light editorial |
| Dark render system (`/template/[id]`, `/template-jobs`) | `bg-black` | amber-400/300 | Fraunces headings | dark theater |
| Admin (`/admin/*`) | `bg-black` | none (white CTAs) | default sans | plain utility |

**Standing rule:** Light editorial = entire user-facing product (landing, /plan, /plan/items, /library, /generative). Dark render system = template flow (`/template/*`, `/template-jobs/*`) + `/admin/*` only. ProgressTheater is tone-aware (`tone="light"` on all light surfaces, default dark for template flow + admin). Intentional, not drift.

---

## §2 Light editorial system (landing + /plan flow)

Token source: `src/apps/web/src/app/page.tsx` on origin/main.

- **Canvas:** `bg-[#fafaf8]` (`--cream`); alt section surface `bg-white` with `border-y border-zinc-200`.
- **Ink scale:** `#0c0c0e` primary (`--ink`), `#3f3f46` secondary, `#71717a` muted, `#a1a1aa` faint.
- **Lime accent roles (D16 contrast rule):**
  - `text-lime-700` — eyebrows, small text labels, emphasis under ~18px
  - `text-lime-600` — large display ems (h1/h2/h3 level), non-text fills, bars, dots
  - `bg-lime-600 text-white` — solid cells
  - `border-lime-200 bg-lime-50 text-lime-800` — pills / soft cells
  - `border-lime-600` — answer left-border (plan ChatInterview pull-quote)
  - `outline-lime-500` — selection
- **Cards:** `rounded-2xl border border-zinc-200 shadow-sm`, fill `bg-white` or `bg-[#fafaf8]`.
- **Notice line (light surfaces):** `border-zinc-200 bg-white text-[#3f3f46]` quiet informational line — transient warnings/conflicts (e.g. "another variant is rendering") stay zinc; NO amber on light surfaces (amber is the dark-render-system accent, §9).
- **Media / phone tiles:** `rounded-[18px]` (marquee) / `rounded-[14px]` desktop, `rounded-[10px]` mobile; heavier shadow `shadow-[0_12px_30px_rgba(0,0,0,0.18)]`.
- **Type scale:**
  - Hero h1: `font-display text-[clamp(36px,6vw,64px)] font-medium leading-[1.08]`
  - h2: `font-display` 36px; h3: 28px; step numerals: 44px italic `text-zinc-200`
  - Eyebrows: `text-[11px] font-semibold uppercase tracking-[0.18em]` (dominant, 5× in section cards); hero eyebrow uses `tracking-[0.24em]` — see §10 ledger
- **CTA (InkButton):** ink pill `rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white hover:opacity-80`.
  **Single-primary-CTA rule on landing:** one CTA to `/plan`, proof via showcase — never a second CTA alongside it.
- **Section rhythm:** `max-w-[900px]` hero, alternating two-column steps, `FadeInOnScroll` (IO threshold 0.12) on every section.
- **Shared primitives:** `LightShell`, `LightCard`, `Eyebrow`, `InkButton` in `src/apps/web/src/components/ui/` (canonical location since v0.4.87.0; `plan/_components/ui/` files are re-export stubs for backward compat).
- **Editorial interview layout:** Fraunces question, LEFT-aligned answers, one prior-answer pull-quote with accent left-border (lime), NO message bubbles, NO bot avatar.
- **D16 lime contrast rule:** lime TEXT under ~18px and text-bearing lime fills → `lime-700`. Display ems, bars, dots, non-text fills → `lime-600`.

---

## §3 Dark render system (template flow + admin)

Token source: `src/apps/web/src/app/template/`, `template-jobs/` on origin/main. Admin is a separate variant (§4).

- **Canvas:** `bg-black text-white`; `min-h-[calc(100vh-3.5rem)]` under the h-14 header.
- **Zinc scale roles:**
  - `border-zinc-700` — default border
  - `bg-zinc-900` — inputs / cards
  - `bg-zinc-800` — raised surfaces
  - `bg-zinc-950` — deeply recessed surfaces (menus/dropdowns, sticky input bars, deeply nested cards)
  - Text: `zinc-200/300` (strong), `zinc-400` (secondary), `zinc-500/600` (faint/decorative)
- **Amber roles:**
  - Primary CTA: `rounded-full bg-amber-400 text-black hover:bg-amber-300 disabled:bg-zinc-700`
  - Links: `text-amber-300 hover:text-amber-200`
  - Focus: `focus:border-amber-400/60`
  - Warnings: `border-amber-700 bg-amber-950/40 text-amber-200`
- **Input pattern:** `rounded-lg border border-zinc-700 bg-zinc-900 placeholder-zinc-600 focus:border-amber-400/60`.
- **Type scale (grep-grounded, 7× dominant):**
  - Page / section titles: `font-display text-3xl text-white`
  - State / loading titles: `font-display text-2xl`
  - Serif accent moments: `text-lg` / `text-xl` (incl. italic `text-amber-300` in `PersonaEditor`); ChatInterview prior-answer pull-quote is `text-sm text-zinc-400 line-clamp-3` (zinc, not amber)
  - Body: default sans; secondary: `text-sm text-zinc-400`
- **Radius roles:** `rounded-full` = buttons/pills; `rounded-lg` = inputs/surfaces.
- **Header:** product routes get sticky scroll-fade header (`rgba(0,0,0,0.6·progress)` + blur); `/` gets static cream header; `/admin` hides Header entirely.
- **Chat / interview surfaces:** editorial interview, not chat app — left-aligned Fraunces questions, one prior-answer pull-quote (amber left-border on dark surfaces; lime left-border on light surfaces), NO message bubbles, NO bot avatar.

---

## §4 Admin variant

Dark + zinc like product but: no amber (CTAs `bg-white text-black`), errors `text-red-400`, squared `rounded`/`rounded-lg`, own nav (`border-zinc-800`, active tab `bg-zinc-800`). Utility over mood — keep it plain.

---

## §5 Typography

- `font-display` → `"Fraunces", Georgia, serif` (defined in `tailwind.config.ts`). Headings, display moments, and serif accents only. Fraunces is an optical-size variable font — load with `opsz,wght@9..144` to get smooth weight/size interpolation.
- Body / labels: `"Inter", ui-sans-serif, system-ui` (explicit `font-sans` override in `tailwind.config.ts`). Body text is utility; Inter's neutrality pairs cleanly with Fraunces's personality.
- Fonts load via Google Fonts `@import` in `globals.css` (not `next/font`). Current import: `family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,600&family=Inter:wght@400;500;600`.
- **Taste rule:** editorial serifs at restrained sizes. Oversized sans display type reads as slop; `system-ui` headlines are the "gave up" signal.

---

## §6 Motion tokens

Token source: `src/apps/web/src/app/globals.css` and `tailwind.config.ts`. `tailwind.config.ts` is authoritative for keyframe definitions.

- `fade-up`: 0.35s ease-out, 8px rise — entrances.
- `shimmer`: 2.2s ease-in-out infinite, background-position sweep — skeletons / loading.
- `animate-ping` 1s (Tailwind default, no custom override) — amber activity dots.
- `animate-pulse` — skeletons.
- `glow` 2s — architecture viewer (`ModuleNode.tsx`) only; not a product primitive.
- **Reduced-motion:** `globals.css` disables `.animate-fade-up`; `FadeInOnScroll` / `ShowcaseMarquee` JS-guard; `/library` shimmer uses `motion-safe:animate-shimmer` today.
  ⚠️ **Gap (ledger):** shimmer and ping not covered by `globals.css` `@media` block — the loading-system D17 contract (§7) closes this globally; until then, use `motion-safe:animate-shimmer` / `motion-safe:animate-ping` on new surfaces.
- **CSS-only motion** — framer-motion stays out of the repo.

---

## §7 Loading-state system

*Spec'd and user-approved 2026-06-06. `components/progress/` lands with the ProgressTheater PR series.*

### Truth rules (D6)
- One progress source per zone.
- No on-screen figure may derive from an index or a constant.
- Progress not backed by a real timestamp or backend event renders as shimmer — never a number or a fill.

### Mood tiers (D13)

| Tier | Wait | Chrome | ETA |
|---|---|---|---|
| THEATER | ≥60s | band + payoff zone + ETA | yes |
| PULSE | 15–60s | amber ping dot + serif line + shimmer preview | no chips/bar/ETA |
| SHIMMER | <5s | skeletons only | — |

Same tokens across tiers; chrome quantity signals wait length.
Assignments: generative / plan-item / template = Theater; activation = Theater-inline; persona / plan generation = Pulse; page loads = Shimmer.

### Motion constants (D14)
*Target source: `components/progress/constants.ts` (not yet created — lands with ProgressTheater PR series).*

| Event | Duration | Easing |
|---|---|---|
| Headline crossfade | 450ms | `cubic-bezier(.4,0,.2,1)`, min-dwell 600ms |
| Chip transition | 350ms | ease |
| Variant arrive | 500ms | `cubic-bezier(.2,.8,.3,1)`, fires once per variant |
| Bar fill | 500ms | linear, damping k=1.6 |
| Celebration | 1.2s | — |
| Band collapse | 650ms | — |
| Field → tiles | 500ms | — |
| Away-note | 400/3500/400ms | — |

### Surface ownership (D15)
Theater components draw **no border, no background, no outer padding** — the host owns the surface. No card-in-card, ever.

### Tone variant (D20)

Theater components accept `tone?: "dark" | "light"` (default `"dark"`). Light surfaces (`/plan/items`, `/library`, `/generative`) pass `tone="light"` to `ProgressTheater`; the template render flow and admin pass nothing → default dark is preserved. `UploadBar` is dark-only (only consumers are template flow; a tone prop would be dead code). The D15 host-owns-surface rule is unchanged across both tones — the Theater never draws its own background regardless of tone.

### A11y contract (D17)
- Status band: `role="status" aria-live="polite"`, each stage announced once.
- Progress bar: `aria-valuenow` updates only on real backend events.
- `prefers-reduced-motion` zeroes all loading animation (closes §6 gap).

### ETA copy ladder (D18)
`~N min left` → `about a minute left` → `less than a minute…`
Never m:ss countdown, never 0:00. Overrun: `almost there — taking a bit longer than usual.`

### Stall escalation (D19)
- >1.5× phase baseline → `Still working…`
- >2.5× phase baseline → amber leave-note
- Client never declares failure from silence.

### Copy derivation (D20)
Detail line derives only from backend state. Counts, not ordinals: `1 of 3 ready`, never `Variant 2 of 3`.

### Failure tone (D10)
Quiet, not alarming.
- Dashed `border-zinc-700` tile, zinc text — **no red error walls**.
- The tile states WHY in plain language via the failure-reason taxonomy (backend error classes → human copy; raw FFmpeg output and stack traces never reach users).
- Partial success is success: celebrate what rendered, one quiet zinc line for what didn't.

### Completion (D12)
Celebrate then recede.
- One amber pulse on arrival (fires once per variant, never re-fires).
- Progress band collapses (650ms) to a receipt line: `✓ Ready in 2:41`.
- Completed state is calm — not a confetti state.

---

## §8 Accessibility & responsive baseline

- **Visible focus** on every interactive element: product `focus:border-amber-400/60` or amber ring; landing ink outline (`outline-lime-500` for selection states).
- **Contrast floor:** text meets 4.5:1 against its surface. `zinc-600`-on-black fails — faint zinc is decorative only, never for content that must be read.
- **Touch targets** ≥44px on mobile.
- **Mobile-first:** single column default, `sm:`/`md:` enhance; landing display type scales via `clamp()`; phone tiles use mobile radii (§2).
- **Reduced-motion** honored globally — `prefers-reduced-motion` zeroes entrances (globals.css); new shimmer/ping uses `motion-safe:` prefix until D17 lands (see §6).

---

## §9 Anti-slop rules (Nova-specific)

- **One accent per surface:** lime = entire user-facing product (landing + all light editorial surfaces). Amber = dark render system (`/template/*`, `/template-jobs/*`) only. Never mixed on the same surface; never a third accent.
- No candy gradients, no rainbow palettes, no purple/violet defaults.
- No 3-column icon-in-circle feature grids; no centered-everything; no decorative blobs/wavy dividers; no emoji as design elements.
- **Serif display (Fraunces) is the brand voice;** system-ui display type is the "gave up" signal.
- **Cards earn existence** — calendar cells, process cards, video tiles are interactions/content, not decoration.
- **Chat = editorial interview** (see §3) — bubbles are an instant fail.
- **Empty states lead with the action, not the absence:** a serif invitation line + the single next-step CTA. Never icon-in-circle + "Nothing here yet!"; never apologize. On product surfaces an empty list is quiet zinc — no illustration.
- **Copy: product language.** If deleting 30% improves it, keep deleting.

---

## §10 Known deviations ledger

Documented here, **not fixed** (D2 decision). Canonicals are user-ratified. Normalization happens opportunistically; see TODOS.md for the backlog item.

| # | Drift | Canonical pick | Note |
|---|---|---|---|
| 1 | Landing radii: `rounded-2xl` / `18px` / `14px` / `7px` | Role-based: `rounded-2xl` content cards; `rounded-[18px]` media tiles; `rounded-[7px]` dense micro-cells | Not one value — each radius serves a role |
| 2 | Product radius stragglers: bare `rounded`, lone `rounded-2xl` | `rounded-full` buttons/pills; `rounded-lg` surfaces | Normalize opportunistically |
| 3 | `--amber: #d97706` CSS var ≠ shipped amber-400 `#fbbf24` | Tailwind `amber-400` / `amber-300` | CSS var is stale; do not reference it |
| 4 | Landing raw-hex grays (= zinc-500/400) | `--ink*` CSS vars are the landing-identity tokens | Equivalence noted for greps |
| 5 | Montserrat 800 imported in `globals.css`, mapped to nothing | Removed in PR1 (light workspace reskin) | Dead import eliminated — closed |
| 6 | Eyebrow `letter-spacing` varies: `tracking-wide` (0.025em), 0.12, 0.14, 0.18, 0.22, 0.24em | `tracking-[0.18em]` landing section cards (dominant); `tracking-[0.24em]` hero eyebrow; `tracking-wide` product micro-labels (dominant in `/plan`) | Normalize opportunistically |
| 7 | `/generative` submit CTA deviates from amber-CTA rule: `rounded bg-white text-black` | Resolved v0.4.87.0 — `/generative` now uses `InkButton` (`bg-[#0c0c0e] text-white rounded-full`), same as all other light surfaces. Amber CTA exception closed. | DONE |
| 8 | Disabled CTA state varies: `disabled:bg-zinc-700` (most plan components), `disabled:bg-zinc-800 disabled:text-zinc-500` (`PlanCalendar`), `disabled:opacity-25` (`ChatInterview`) | `disabled:bg-zinc-700` is the dominant pattern | Normalize opportunistically |
| 9 | Light editorial system covers landing + /plan flow. `/plan/items/[id]`, `/library`, `/generative` remain dark theater. | Resolved v0.4.87.0 — D20 + D21 landed. All user-facing surfaces are now light editorial. §1 standing rule updated. | DONE |
| 10 | Workspace route layout | `/plan` = mode router (setup flow for new users; workspace for returning users); `/plan/setup` = canonical onboarding URL (redirects to `/plan`); `/plan/persona` = real persona read+edit page | PR3 ships the canonical routes and back-compat redirects. |
| 11 | Display font: Playfair Display → Fraunces | `"Fraunces", Georgia, serif` — optical-size variable, `opsz,wght@9..144`. Rationale: 3-way user comparison (Fraunces / Space Grotesk / Instrument Serif), Fraunces chosen (D6/D8 in based-on-our-talk-deep-hopper plan). Body unchanged → Inter. **Web UI only** — burned-in video fonts (`assets/fonts/`, Skia ASS) unaffected. | DONE v0.4.106.0 |

---

## §11 Calibration examples

Quick right/wrong pairs for common review questions.

| Scenario | ✓ Correct | ✗ Wrong |
|---|---|---|
| Landing CTA | Single ink pill → `/plan`, proof via showcase | Dual CTA, lime-colored button, ghost variant |
| Product loading | Shimmer skeleton + true elapsed clock from backend | Percent bar derived from a constant or an index |
| Product interview | Left-aligned Fraunces question + amber left-border pull-quote | Chat bubbles, bot avatar, centered Q&A |
| Empty product state | Serif invitation line + single next-step CTA in quiet zinc | Gray inbox icon in a circle + "Nothing here yet!" |
| Error tile | Dashed zinc tile, plain-language reason | Red alert wall, raw exception message |

---

*Rendered-video (FFmpeg burn-in) overlay design is a separate medium — see `docs/pipelines/template.md` and `docs/pipelines/layer2-text-overlay.md` for font and sizing rules.*
