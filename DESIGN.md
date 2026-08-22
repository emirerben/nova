# Kria Design System

This document is the **calibration target** for all design reviews and the token source for implementation.
Consumers: `/plan-design-review` and `/design-review` skills, implementers, and AI agents.
**Rule:** any change to the design system must update this document in the same PR. The doc codifies shipped reality — open a new PR to change the system, then update the doc in that same PR.

---

## §1 The three surfaces at a glance

| Surface | Canvas | Accent | Type | Mood |
|---|---|---|---|---|
| Landing (`/`, `/auto-story`) | white `#ffffff` | Paper lime `#d7ff90` | Inter edit-story statements | light editorial |
| Light product (`/plan`, `/plan/new`, `/plan/items/`) | white `#ffffff` / ink / lime | lime-700 | Fraunces headings | light editorial |
| Dark render system (`/template-jobs`) | `bg-black` | amber-400/300 | Fraunces headings | dark theater |
| Admin (`/admin/*`) | `bg-black` | none (white CTAs) | default sans | plain utility |

**Standing rule:** Light editorial = entire user-facing product (landing, /plan, /plan/new, /plan/items; /library and /generative are redirects to /plan since v0.44). Dark render system = render-status flow (`/template-jobs/*`) + `/admin/*` only (the `/template/[id]` config flow was deleted in v0.7.8.2). ProgressTheater is tone-aware (`tone="light"` on all light surfaces, default dark for /template-jobs + admin). Intentional, not drift.

---

## §2 Light editorial system (landing + /plan flow)

Token source: `src/apps/web/src/app/globals.css` plus `src/apps/web/src/components/KriaEditStory.module.css`.

- **Canvas:** `bg-[#ffffff]` (`--cream`, now pure white — 2026-08-21 founder call); cards separate via zinc borders + shadows.
- **Ink scale:** `#0c0c0e` primary (`--ink`), `#3f3f46` secondary, `#71717a` muted, `#a1a1aa` faint.
- **Landing story accent:** `#d7ff90` (`--story-lime`) is the light Paper-derived lime used only for explanatory feature chips.
- **Lime accent roles (D16 contrast rule):**
  - `text-lime-700` — eyebrows, small text labels, emphasis under ~18px
  - `text-lime-600` — large display ems (h1/h2/h3 level), non-text fills, bars, dots
  - `bg-lime-600 text-white` — solid cells
  - `border-lime-200 bg-lime-50 text-lime-800` — pills / soft cells
  - `border-lime-600` — answer left-border (plan ChatInterview pull-quote)
  - `outline-lime-500` — selection
- **Cards:** `rounded-2xl border border-zinc-200 shadow-sm`, fill `bg-white` or `bg-[#ffffff]`.
- **Notice line (light surfaces):** `border-zinc-200 bg-white text-[#3f3f46]` quiet informational line — transient warnings/conflicts (e.g. "another variant is rendering") stay zinc; NO amber on light surfaces (amber is the dark-render-system accent, §9).
- **Landing story screen:** centered 16:9 frame with a 1px ink border, 44px desktop / 30px mobile radius, and transparent fill until the first shot arrives. The screen contains only the active video; captions, visual effects, and placed media must be reflected inside the rendered footage, never as separate DOM cards layered above it. Surrounding source media has no border.
- **Type scale:**
  - Landing story line: Inter medium, `clamp(54px,6.53vw,94px)` desktop; the long middle line uses `clamp(26px,7.6vw,38px)` on mobile.
  - Feature chips: Inter 15/18 desktop and 11/16 mobile, weight 700.
  - Ordinary product headings remain Fraunces; see the landing exception in §5.
- **CTA (InkButton):** ink pill `rounded-full bg-[#0c0c0e] px-9 py-[15px] text-[15px] font-semibold text-white hover:opacity-80`.
  **Single-primary-CTA rule on landing:** one CTA to `/plan` in its original centered position near the bottom of the edit story — never duplicate it in the header or below the story.
- **Primary-action viewport budget:** on any flow step whose purpose is a single next action, keep that action visible in the first viewport at 1280×720 and 375×667, using realistic maximum AI-generated content length.
- **Light-surface pinned action bar:** when adaptive pinning is needed on `#ffffff`, use `sticky bottom-0 z-10 -mx-5 border-t border-zinc-200 bg-[#ffffff] px-5 pt-4 pb-[max(16px,env(safe-area-inset-bottom))] md:mx-0 md:px-0` (bleeds to the pane edge on mobile, aligns to the text column on desktop). The bar's `border-t` is its only divider — never pair it with a `border-t` on the section that follows.
  Apply it only when the action would otherwise fall below the fold; the existing always-on variant lives in `ChatInterview.tsx`.
- **Touch pressed state:** on touch surfaces, pressed/drag state replaces hover affordance. Active handles solidify and scale slightly; active chips go `opacity-100`; drags show a floating value readout offset from the thumb.
- **Landing story rhythm:** `/` starts the timed composition automatically in one viewport, with no mode selector or playback control; `/auto-story` remains a compatibility route. `/?mode=scroll` retains the pinned `760svh` choreography for direct comparison without exposing it in the interface. Source footage and feature chips surround the centered screen, then travel directly into it before the three statements replace one another. The soundtrack starts muted for autoplay compatibility and makes one synchronized audible attempt at the sound-effects beat; browsers may keep it muted when policy forbids unprompted audio.
- **Shared primitives:** `LightShell`, `LightCard`, `Eyebrow`, `InkButton`, `InfoDot`, `ConfirmDialog` in `src/apps/web/src/components/ui/` (canonical location since v0.4.87.0; `plan/_components/ui/` files are re-export stubs for backward compat). Since v0.47.0.0, `LightCard`/`InkButton`/`ConfirmDialog` are thin wrappers over the shadcn/ui primitives (`Card`/`Button`/`AlertDialog`) — see §15 for the full component-library contract; every NEW control should use the shadcn primitives directly rather than the legacy wrapper names.
- **Editorial interview layout:** Fraunces question, LEFT-aligned answers, one prior-answer pull-quote with accent left-border (lime), NO message bubbles, NO bot avatar.
- **Editor Nova copilot drawer exception:** the full-screen editor's Nova tool may use texting bubbles because it is a command/receipt surface, not an onboarding interview. Tokens: user bubble `bg-[#0c0c0e] text-white` with 18px radius / 6px bottom-right corner; assistant bubble `bg-zinc-100 text-[#0c0c0e]` with 18px radius / 6px bottom-left corner; change chips `border-lime-200 bg-lime-50 text-lime-800`; rejected chips `border-dashed border-zinc-300 bg-white text-[#71717a]`; suggestion chips `border-zinc-200 bg-white` with lime hover/focus.
- **D16 lime contrast rule:** lime TEXT under ~18px and text-bearing lime fills → `lime-700`. Display ems, bars, dots, non-text fills → `lime-600`.
- **InfoDot (ⓘ popover, `components/ui/InfoDot.tsx`):** the ONLY sanctioned home for optional helper copy. 16px zinc-400 glyph inline after a label (sibling, never inside a `<label>`); hover/focus ink on zinc-100 disc; open lime-700 on lime-50. Popover: Radix portal `z-[130]` (above the editor Sheet/CopilotDrawer `z-[95]` and the `z-[120]` overlay tier — the popover is always the topmost transient), white `border-zinc-200 rounded-[12px]` shadow, max-w 280px, 13/19 Inter `#3f3f46`, plain sentences only (no heading, no CTA, ≤3 lines). Motion: 180ms scale 0.96→1 + 4px rise from the trigger origin, 120ms fade-out, reduced-motion = fade only (`.info-dot-pop` in `globals.css`). Desktop: hover opens after 150ms, closes 200ms after the pointer leaves; click pins it open. Touch: tap toggles. Outside tap/Esc dismisses; hover-open never steals focus. Hit area 44px (`size="compact"` = 32px in dense inspector rows). NEVER for warnings, errors, disabled reasons, or destructive confirms — those stay visible. Max ~2 dots per screen section.

---

## §3 Dark render system (/template-jobs + admin)

Token source: `src/apps/web/src/app/template-jobs/` on origin/main (the `/template/[id]` config flow was deleted in v0.7.8.2). Admin is a separate variant (§4).

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
- **Header:** product routes get sticky scroll-fade header (`rgba(0,0,0,0.6·progress)` + blur); landing routes (`/`, `/auto-story`) get a static, borderless white header with no anonymous auth action. Their single “Create my first edit” CTA stays centered near the bottom of the story, with Terms and Privacy beneath it rather than in the header. `/admin` hides Header entirely.
- **Chat / interview surfaces:** editorial interview, not chat app — left-aligned Fraunces questions, one prior-answer pull-quote (amber left-border on dark surfaces; lime left-border on light surfaces), NO message bubbles, NO bot avatar.

---

## §4 Admin variant

Dark + zinc like product but: no amber (CTAs `bg-white text-black`), errors `text-red-400`, squared `rounded`/`rounded-lg`, own nav (`border-zinc-800`, active tab `bg-zinc-800`). Utility over mood — keep it plain.

---

## §5 Typography

- `font-display` → `"Fraunces", Georgia, serif` (defined in `tailwind.config.ts`). Headings, display moments, and serif accents only. Fraunces is an optical-size variable font — load with `opsz,wght@9..144` to get smooth weight/size interpolation.
- Body / labels: `"Inter", ui-sans-serif, system-ui` (explicit `font-sans` override in `tailwind.config.ts`). Body text is utility; Inter's neutrality pairs cleanly with Fraunces's personality.
- **Landing edit-story exception:** the three over-video statements use oversized Inter at medium weight. They are moving image-composition elements, not section headings: each occupies the same centered slot and uses `mix-blend-mode: difference` as footage passes behind it. All ordinary landing and product headings remain Fraunces.
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

### transitions.dev additions (globals.css `:root`, curated slice — branch transitions-motion)

Tokens reconciled with existing values (D14 constants, fade-up, shimmer) where overlap exists.
All four CSS blocks live in `globals.css` with their own `prefers-reduced-motion: reduce` guards,
closing the §6 D17 gap per-surface. Source skill: `npx skills add Jakubantalik/transitions.dev`.

| Token group | CSS vars | Usage |
|---|---|---|
| `t-modal` (#6) | `--modal-open-dur: 250ms`, `--modal-close-dur: 150ms`, `--modal-scale: 0.96`, `--modal-ease` | Pattern template for all future modals. No current consumer (last user `TemplatePreviewModal` removed with the dead `/template` route, 2026-07-11). |
| step-slide (derived #8) | `--page-slide-dur/fade-dur: 250ms`, `--page-slide-distance: 8px`, `--page-blur: 3px`, `--page-slide/fade-ease` | `OnboardingShell` `<StepSlide key={step}>` — slide+blur entrance on each wizard step. |
| `t-skel` (#14) | `--reveal-dur: 400ms`, `--reveal-blur: 2px`, `--reveal-ease: ease-in-out` | `VariantRenderCard` shimmer→video cross-blur reveal when status becomes `ready`. |
| `t-stagger` (#18) | `--stagger-dur: 500ms`, `--stagger-distance: 12px`, `--stagger-stagger: 40ms`, `--stagger-blur: 3px`, `--stagger-ease` | Legacy token with no current consumer; the previous landing hero was removed in v0.37.0.0. |
| `t-accordion` | `--t-accordion-dur: 300ms`, `--t-accordion-ease: cubic-bezier(0.23,1,0.32,1)` | `NovaStepRow` detail-line reveal (render-progress `NovaActivityFeed`, behind `NEXT_PUBLIC_NOVA_STEPS_FEED_ENABLED`). Grid-rows `0fr → 1fr` + opacity crossfade, same duration; `prefers-reduced-motion` zeroes it. Chat compact rows (a later PR) and the plan-item `SetupPicker` disclosure rows (v0.34.0.0) reuse the same token pair so all surfaces expand identically. |
| Landing edit story | `--story-move-dur: 500ms`, `--story-overlay-stagger: 60ms`, `--story-reduced-dur: 200ms`, `--story-ease-move`, `--story-ease-out` | `/` starts the timed choreography automatically with no mode or playback controls; `/?mode=scroll` and `/auto-story` remain direct compatibility paths. Movement is transform/opacity only; reduced motion shows the completed composition without positional travel. Navigation, CTA, and ambient background remain static. |

**Follow-up scope (not this PR):** `t-tabs`, `t-success-check`, `t-error-shake`, upload-dropzone drag feedback, spinner component consolidation.

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

Theater components accept `tone?: "dark" | "light"` (default `"dark"`). Light surfaces (`/plan`, `/plan/items`) pass `tone="light"` to `ProgressTheater`; the template render flow and admin pass nothing → default dark is preserved. `UploadBar` is dark-only (only consumers are template flow; a tone prop would be dead code). The D15 host-owns-surface rule is unchanged across both tones — the Theater never draws its own background regardless of tone.

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
- No duration, no claim: when the start/finish pair can't yield an honest span (a re-render moves `started_at` past the first render's `finished_at`), the receipt reads `Your edits are ready` instead of a number. One formatter — `deriveReceiptText` in `components/progress/logic.ts`.
- Collapse is not one-way: a new render on the same mount re-expands the band, so a restarted clock is never hidden behind a stale receipt.
- Completed state is calm — not a confetti state.

---

## §8 Accessibility & responsive baseline

- **Visible focus** on every interactive element: product `focus:border-amber-400/60` or amber ring; landing ink outline (`outline-lime-500` for selection states).
- **Contrast floor:** text meets 4.5:1 against its surface. `zinc-600`-on-black fails — faint zinc is decorative only, never for content that must be read.
- **Touch targets** ≥44px on mobile.
- **Touch inputs:** inputs on touch viewports are ≥44px tall with ≥16px font to prevent iOS Safari zoom-on-focus.
- **User scaling:** never disable zoom. Do not set `maximumScale` or `user-scalable=no`.
- **Mobile-first:** single column default, `sm:`/`md:` enhance; landing display type scales via `clamp()`; the story screen and source media use mobile radii (§2).
- **Reduced-motion** honored globally — `prefers-reduced-motion` zeroes entrances (globals.css); new shimmer/ping uses `motion-safe:` prefix until D17 lands (see §6).
- **Enforcement:** `src/apps/web/src/__tests__/plan/mobile-shell.test.tsx` guards the parts of this section that regressed silently for months (plans/013) — the setup shells' rails stay breakpoint-gated with a shrinkable `<main>`, and no form control renders below the 16px zoom floor. The 16px floor is applied once in `globals.css` at the base tier, NOT per call site; a new sub-16px control fails that test until its size class is added to the rule.

| Tier | Width | Canonical use |
|---|---:|---|
| base | <640px | Phone-first single column; 44px touch targets; 16px focused inputs. |
| `sm` | ≥640px | Tailwind small-tablet enhancement tier. |
| `md` | ≥768px | Tailwind tablet enhancement tier. |
| light editor | <1024px | `useEditorLayoutMode.ts` light mode. |
| `lg` / overlay editor | ≥1024px to <1280px | Tailwind desktop tier; editor overlay mode. |
| `xl` / full editor | ≥1280px | Tailwind wide tier; editor full mode. |

---

## §9 Anti-slop rules (Kria-specific)

- **One accent per surface:** lime = entire user-facing product (landing + all light editorial surfaces). Amber = dark render system (`/template-jobs/*`) only. Never mixed on the same surface; never a third accent.
- No candy gradients, no rainbow palettes, no purple/violet defaults.
- No 3-column icon-in-circle feature grids; no centered-everything; no decorative blobs/wavy dividers; no emoji as design elements.
- **Serif display (Fraunces) is the brand voice;** system-ui display type is the "gave up" signal.
- **Cards earn existence** — calendar cells, process cards, video tiles are interactions/content, not decoration.
- **Chat = editorial interview** (see §3) — bubbles are an instant fail except for the editor Nova copilot drawer scoped in §2.
- **Empty states lead with the action, not the absence:** a serif invitation line + the single next-step CTA. Never icon-in-circle + "Nothing here yet!"; never apologize. On product surfaces an empty list is quiet zinc — no illustration.
- **Copy: product language.** If deleting 30% improves it, keep deleting.
- **No inline helper paragraphs.** Static "how it works" copy next to a control is clutter: delete it, or move it behind an `InfoDot` (§2) if it earns a first-use read. Visible text is reserved for load-bearing states — constraints, disabled reasons, warnings, confirms. Audit inventory: `docs/declutter-audit.md`.

---

## §10 Known deviations ledger

Documented here, **not fixed** (D2 decision). Canonicals are user-ratified. Normalization happens opportunistically; see TODOS.md for the backlog item.

| # | Drift | Canonical pick | Note |
|---|---|---|---|
| 1 | Landing story radii: 44px screen / 24–13px source media / full feature pills | Role-based: the screen reads as the device; surrounding source media steps down by size; feature controls stay pill-shaped | Not one value — each radius serves a role |
| 2 | Product radius stragglers: bare `rounded`, lone `rounded-2xl` | `rounded-full` buttons/pills; `rounded-lg` surfaces | v0.47.0.0: now enforced structurally — the shadcn `Button`/`Badge` primitives hard-code `rounded-full` and `Card`/`Dialog`/`Sheet` hard-code `rounded-2xl`; new call sites can't drift. Pre-existing raw controls still normalize opportunistically as lanes migrate them (see §15 raw-control ratchet). |
| 3 | `--amber: #d97706` CSS var ≠ shipped amber-400 `#fbbf24` | Tailwind `amber-400` / `amber-300` | CSS var is stale; do not reference it |
| 4 | Landing raw-hex grays (= zinc-500/400) | `--ink*` CSS vars are the landing-identity tokens | Equivalence noted for greps |
| 5 | Montserrat 800 imported in `globals.css`, mapped to nothing | Removed in PR1 (light workspace reskin) | Dead import eliminated — closed |
| 6 | Product micro-label `letter-spacing` varies: `tracking-wide` (0.025em), 0.12, 0.14, 0.18, and 0.22em | `tracking-wide` product micro-labels are dominant; the v0.37 landing story has no eyebrow labels | Normalize opportunistically |
| 7 | `/generative` submit CTA deviates from amber-CTA rule: `rounded bg-white text-black` | Resolved v0.4.87.0 — `/generative` now uses `InkButton` (`bg-[#0c0c0e] text-white rounded-full`), same as all other light surfaces. Amber CTA exception closed. | DONE |
| 8 | Disabled CTA state varies: `disabled:bg-zinc-700` (most plan components), `disabled:opacity-25` (`ChatInterview`) | `disabled:bg-zinc-700` is the dominant pattern | Normalize opportunistically |
| 9 | Light editorial system covers landing + /plan flow. `/plan/items/[id]`, `/library`, `/generative` remain dark theater. | Resolved v0.4.87.0 — D20 + D21 landed. All user-facing surfaces are now light editorial. §1 standing rule updated. | DONE |
| 10 | Workspace route layout | `/plan` = mode router (setup flow for new users; workspace for returning users); `/plan/setup` = canonical onboarding URL (redirects to `/plan`); `/plan/persona` = real persona read+edit page | PR3 ships the canonical routes and back-compat redirects. |
| 11 | Display font: Playfair Display → Fraunces | `"Fraunces", Georgia, serif` — optical-size variable, `opsz,wght@9..144`. Rationale: 3-way user comparison (Fraunces / Space Grotesk / Instrument Serif), Fraunces chosen (D6/D8 in based-on-our-talk-deep-hopper plan). Body unchanged → Inter. **Web UI only** — burned-in video fonts (`assets/fonts/`, Skia ASS) unaffected. | DONE v0.4.106.0 |

---

## §12 Plan home + New-video flow (v0.44 redesign; supersedes the ideas ledger)

Rules here supplement §2 (light editorial system). Design source: Paper file
"Kria Plan Redesign", page "FINAL — Basic home". The ideas ledger was removed
2026-08-21 (tester feedback: /plan read as a content-ideas list, not an edit
tool); /plan is now openly the create-a-new-video page.

**Note (v0.47 Kria Design System migration):** the ideas ledger and the
"Plan this for me" panels referenced below as already-removed were removed in
PRs #869/#871 — this section documents the post-removal shape, not a
pending change.

### Basic home (`/plan`, `WorkspaceHome.tsx`)
- **Canvas:** `bg-white`; centered column `max-w-[900px] px-6 pt-14`, sections gap-10.
- **Create block (leads):** Fraunces `text-[32px] font-medium` "Make a new video." + `text-sm text-[#71717a]` sub-line "Pick what kind, add your footage — Kria edits it into a post." + ink pill `min-h-12 rounded-full bg-[#0c0c0e] text-white` "New video" → `/plan/new` (full-width on mobile, hugging on sm+). One primary CTA on the page.
- **PAST EDITS section:** eyebrow `text-[11px] font-semibold uppercase tracking-[0.18em] text-[#3f3f46]`; grid `grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-4` of `LibraryTile` (from `components/library/`, ex-/library); cursor-driven "Load more" zinc pill. Rendering tiles keep LibraryTile's shimmer; failed stay quiet zinc; no red.
- **Empty:** one quiet line `text-[15px] text-[#71717a]` "Your edits will live here." — no card, no icon.
- **Plan generating:** quiet `text-[13px] text-[#71717a]` line under the CTA ("…you can start a video anyway"); creation never blocks on plan state.
- **SeedUploadCard** still mounts above everything while `activation_status` ∈ {seeding, activating}.
- **TikTok connection:** `TikTokConnectionCard` at the bottom under `id="tiktok"`; TikTokReleaseRail connect/reconnect links target `/plan#tiktok`.
- **Initial load:** SHIMMER tier — 4 ghost 9:16 tiles.

### New-video chooser (`/plan/new`)
- Full-screen steps on white: `Button variant="ghost" size="icon"` (44px) `×`/`‹` back + "Step N of M" muted label (montage = 3 steps: kind → style → footage; other types = 2). Fraunces `text-[30px]` titles ("What kind of video?" / "Pick a style.") + one 14px ink-3 line ("Tap one — Kria edits each kind differently." / "How your clips are arranged.").
- Poster radio-cards reuse SetupPicker's `MediaRadioCard` + `TYPE_MEDIA`/`TYPE_COPY`/`STYLE_TILES` (montage / voiceover / talking-to-camera; talking_head stays legacy-only). Selection = lime ring + "Selected" chip, same as the item page. Style step (montage only): Classic / Masonry collage / Polaroid wall, Classic preselected. Both card scrollers carry `scrollbar-none` — no visible scrollbar, swipe/scroll still works.
- **Tap-to-advance, no Continue button, no footer:** picking a kind card either advances to the style step (montage) or mints the item immediately (every other kind); picking a style card mints the item immediately. `saving`/`aria-disabled` on the cards (`creating || planState !== "ready"`) blocks re-entry during the mint.
- A tap that mints the item runs `addIdea` + `updatePlanItem` (incl. `montage_preset`) → `/plan/items/{id}?setup=done`. Abandon before that final tap creates nothing. Errors are quiet zinc `role="alert"` lines; never red — the same card stays tappable to retry.

### Per-type item setup (`/plan/items/[id]`, pre-generation — V2 redesign)
Design source: Paper "Kria Plan Redesign", page "V2 — Item setup per type". Each edit
type shows ONLY what it needs; there is no generic audio/caption chrome.
- **Header:** back link "← your videos"; lime eyebrow setup receipt (`MONTAGE · CLASSIC` — type + montage style) with a quiet "Change" toggle that mounts the SetupPicker poster rail inline (the old nested "Advanced video style" `<details>` is gone); per-type Fraunces title ("Add your clips." / "Your voice tells the story." / "Add your clip."). Items minted by /plan/new are untitled (idea = type label) — the release desk shows the receipt eyebrow instead of an h1 reading "Montage".
- **Montage:** 1·Your clips (helper "3 or more clips work best. Kria cuts them to the beat of a matched song."; collage presets say photos work too) → 2·Direction (optional, with voice note) → Generate. NO audio section (music is automatic), no captions/voiceover UI.
- **Voiceover (narrated_ready):** 1·Your clips → 2·Your voiceover (white card, recorder + flag-gated script-helper link; "this becomes the soundtrack") → 3·Direction → Generate (gate requires the voiceover unless self-narration flag).
- **Talking to camera (subtitled):** 1·Your clip (single slot, maxClips 1; helper "Its own audio is the soundtrack — Kria captions every sentence…" + "Captions and dead-air cleanup happen automatically.") → 2·Direction → Generate.
- **Talking-head B-roll (legacy):** one pool, helper "First clip: you talking. Then extra footage to cut in."
- **Removed for good:** the "Audio choice" fieldset (it could silently re-type items — type changes go through the receipt only), "From your idea" seed badge, day badge, "Plan this for me" panels (filming-guide summaries still render for legacy guided items), generic "Photo collage" copy.

## §13 Teleprompter surface (transcript voiceover helper)

The "Get a transcript" focus takeover (`/plan/items/[id]/transcript`) — full-screen
step-rail wizard (Brief · Questions · Script · Record · Review) on the light
editorial system. Rules supplement §2. Entry: a quiet lime line on the item
voiceover section (narrated formats only), flag-gated by
`NEXT_PUBLIC_TRANSCRIPT_HELPER_ENABLED`.

- **Reading highlight (teleprompter):** the transcript line nearest the viewport
  center gets `bg-lime-50 border-l-[3px] border-lime-600`. It is **scroll-driven,
  not time-driven** — a reading aid, NOT karaoke. No auto-scroll, no auto-advance,
  no motion token: highlight moves only as the reader scrolls. Non-active lines
  carry a transparent `border-l-[3px]` so the size never jumps.
- **Read-time badge:** `border-lime-200 bg-lime-50 text-lime-800` soft pill
  (`≈ M:SS to read`) — same family as the §2 soft cell. Used on the Script step
  and derived from `read_time_s`.
- **Muted-video visual reference:** a plain `StableVideo` with `muted` (`loop`,
  `autoPlay`, `playsInline`). MUTED is load-bearing — footage audio would bleed
  into the mic and corrupt take alignment, so it is never un-muted here. Pin with
  `identity` (variant id) as elsewhere. When no render exists, the pane falls back
  to a dashed-zinc reading-only invite (empty-state = action, not absence).
- **Reading controls:** A−/A+ font-size buttons (a11y), 44px record targets,
  `aria-live` recording state, space = start/stop.

---

## §14 Pocket editor (mobile light mode)

Default **ON** since v0.18.1.0. Kill switch: `NEXT_PUBLIC_MOBILE_EDITOR_ENABLED="false"`
in Vercel + redeploy (build-time var) ⇒ legacy light mode (canvas + Nova chat only),
byte-identical. The `<1024px` editor carries full manual editing.
Rules supplement §2.

- **Tool dock** (`_editor/ToolDock.tsx`): `bg-[#ffffff] border-t border-zinc-200
  pb-[max(8px,env(safe-area-inset-bottom))]` — deliberate 8px safe-area floor vs
  the pinned-bar's 16px (56px tool targets + label descenders already provide the
  visual bottom space). 7 tools in desktop rail order; 24px icon +
  `text-[11px] font-medium` label; active = ink + 2px ink underline; disabled =
  icon `opacity-50`, label stays `#71717a`, honest reason via toast on tap.
- **Sheet primitive** (`_editor/Sheet.tsx`, generalizes the Nova drawer):
  `rounded-t-2xl bg-white border-t border-zinc-200`, grabber `bg-zinc-300`.
  Detents: **half 54dvh non-modal** (no scrim, no focus trap — canvas and
  transport stay interactive; the shell squeezes above via `padding-bottom`) /
  **full 88dvh modal** (`bg-[#0c0c0e]/15` scrim, focus trap, compact transport in
  the title row). Motion = `t-modal` tokens, transform/opacity only; heights
  snap, never tween. Keyboard promotes half→full for its lifetime.
- **Context strip** (`_editor/ContextStrip.tsx`): `role="toolbar"` selection
  quick actions; 44px pills; the first pill per type is the ink primary
  (text→Edit, caption→Edit cue, overlay→Edit, clip→Adjust); **Delete is always
  the word Delete** (§9: never an icon or emoji). Caption and clip selections
  flip the strip to the canvas top so it never covers what it acts on.
- **Mini strip** (`_editor/MiniStrip.tsx`): 44px proportional clip segments
  (alternating zinc-300/zinc-200), lime-600 playhead + presence dots + selected
  outline; drag = scrub (one playhead, two views — always in sync with the
  transport thumb), tap = select clip + seek. Filmstrip thumbnails are PR2.
- **Icons** (`_editor/editor-icons.tsx`): stroke SVGs, 24px / 1.6 /
  `currentColor` — supersedes ToolRail's no-icon-set note for mobile chrome; raw
  unicode glyphs take emoji presentation on iOS Safari.
- **Focus-visible:** pocket controls use the editor's shipped lime outline
  (`focus-visible:outline-lime-500`), matching sibling editor chrome — an
  editor-scoped deviation from the §12 ink-outline precedent, documented here.
- **Deviation (ledger-style):** LightTransport's shipped `accent-lime-500`
  predates D16 (fills = lime-600); new pocket fills are lime-600. Normalize the
  transport opportunistically.
- **PR2 (declared, not silent):** full-screen mobile timeline takeover
  (center-playhead, imperative rAF scroll, media budget, preview pane), sheet
  section restructure (Eyebrow headers, default expansion, ≥44px inner-control
  density), conflict choice-cards, Playwright touch e2e.

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

## §15 Component library (shadcn/ui)

Shipped v0.47.0.0 (Lane 0 of the Kria Design System migration). Every user-facing
surface — including the full-screen editor — now has a single component
standard instead of ad-hoc `className` chrome. `/admin` is deferred (keeps its
dark variant; adopts the primitives later).

### Where primitives live

- **`src/apps/web/src/components/ui/*.tsx` (lowercase filenames) = the shadcn
  primitives** — `button.tsx`, `input.tsx`, `textarea.tsx`, `select.tsx`,
  `checkbox.tsx`, `switch.tsx`, `radio-group.tsx`, `label.tsx`, `badge.tsx`,
  `card.tsx`, `dialog.tsx`, `alert-dialog.tsx`, `sheet.tsx`,
  `dropdown-menu.tsx`, `popover.tsx`, `tooltip.tsx`, `tabs.tsx`, `toggle.tsx`,
  `toggle-group.tsx`, `slider.tsx`, `separator.tsx`, `skeleton.tsx`,
  `scroll-area.tsx`, `progress.tsx`, `sonner.tsx`. Style `new-york`, installed
  via `npx shadcn@2 add …` then hand-edited for the Kria variants below.
  `components.json` aliases `utils` → `@/lib/cn` (no `utils.ts` file — `cn` is
  hand-authored, shadcn's own).
- **`src/apps/web/src/components/ui/*.tsx` (PascalCase filenames) = pre-existing
  Kria wrappers** — `InkButton`, `LightCard`, `ConfirmDialog` are now thin
  wrappers over `Button`/`Card`/`AlertDialog` with **identical props and
  import paths**; every existing call site kept working with zero edits.
  `InfoDot`, `StyleChip`, `Eyebrow`, `LightShell`, `useFocusTrap` are
  untouched by this migration (§2). New code should reach for the shadcn
  primitives directly (`<Button variant="ink">`, not `<InkButton>`).
- `<Toaster />` (from `ui/sonner.tsx`) is mounted once in `src/app/layout.tsx`
  inside `<Providers>`. Call `toast("message")` from `sonner` anywhere — never
  build a bespoke toast/`useState` toast again.

### Variant table

| Primitive | Variants | Notes |
|---|---|---|
| `Button` | `default`/`ink` (solid `#0c0c0e`), `outline`, `secondary`, `ghost`, `link`, `lime` (soft pill), `destructive` (zinc, **not red**) | Sizes: `default` (`px-9 py-[15px] text-[15px]`), `sm` (`h-9 px-5 text-[13px]`), `lg`, `icon` (44px), `icon-sm` (32px, ≥1024 inspector only), `pill`. `asChild` supported. |
| `Badge` | `ink` (solid pill), `lime` (bare eyebrow text, no fill), `lime-soft` (lime-50/200/800 pill), `zinc` (neutral default) | `rounded-full text-[11px] uppercase tracking-[0.18em]`. |
| `Input` / `Textarea` | — | `h-11 sm:h-10` (Textarea: `min-h-11 sm:min-h-10`), `text-base sm:text-sm` — never below the 16px iOS zoom-on-focus floor (§8). Lime focus border, no ring. |
| `Select` | — | Trigger matches `Input` sizing; content `z-[130]`. |
| `Dialog` / `AlertDialog` | — | `rounded-2xl border-zinc-200 bg-white shadow-sm`, overlay `bg-black/30`, `z-[100]`, scale-up-from-center via the existing `--modal-*` tokens (§6 t-modal), `motion-reduce:animate-none`. `AlertDialog` = the only sanctioned destructive-confirm surface — action button is ink/zinc, never red. |
| `Sheet` | `side="top"\|"bottom"\|"left"\|"right"` | Same `z-[100]`/token motion as Dialog; bottom sheets get `rounded-t-2xl`. **This is a different component from the pre-existing editor gesture `Sheet.tsx`** (`app/plan/items/[id]/_editor/Sheet.tsx`, half-detent, non-modal) — do not confuse the two; the editor keeps its own. |
| `DropdownMenu` / `Popover` / `Tooltip` | — | `z-[130]` (above dialogs/sheets at `z-[100]`, below `InfoDot` — §2). |
| `Tabs` | — | Underline style (`border-b-2` on the active trigger), not a pill segmented control. |
| `Slider` | — | Zinc-200 track, lime-600 range, 20px white thumb with an invisible 44px hit area. |
| `Toast` (`sonner`) | — | Single bottom-center, `duration=2600`, no close button, ink pill (`bg-[#0c0c0e] text-white`). |

### Token → hex map

CSS custom properties in `src/apps/web/src/app/globals.css` (`@layer base`,
after the pre-existing `:root` landing-token block). Consumed as
`hsl(var(--token))` via `tailwind.config.ts`.

| Token | Light | Dark (`/template-jobs` + `/admin` only) |
|---|---|---|
| `--background` | `#ffffff` | zinc-950 `#09090b` |
| `--foreground` / `--primary` | `#0c0c0e` (ink) | white `#fafafa` |
| `--card` / `--popover` | `#ffffff` | zinc-900 `#18181b` |
| `--secondary` / `--muted` | zinc-100 `#f4f4f5` | zinc-800 `#27272a` |
| `--muted-foreground` | `#71717a` (ink-3) | zinc-400 |
| `--accent` / `--accent-foreground` | lime-50 `#f7fee7` / lime-800 `#3f6212` | unchanged (inherits light — lime accent stays lime in dark) |
| `--destructive` | zinc `#3f3f46` (ink-2) — **never red, D10** | zinc-800 — never red |
| `--border` / `--input` | zinc-200 `#e4e4e7` | zinc-800 |
| `--ring` | lime-500 `#84cc16` | amber-400 `#fbbf24` |
| `--radius` | `0.5rem` (keeps `rounded-lg` byte-identical to pre-migration) | same |

`--radius` deliberately stayed at Tailwind's own default (`0.5rem`) rather than
the 12px the Paper Tokens board sketches for menus/inputs — changing it would
have silently reflowed every existing `rounded-lg`/`rounded-md` surface.
Primitives that need a bigger radius (`Card`, `Dialog`, `Sheet`, `Button`)
hard-code `rounded-2xl`/`rounded-full` directly instead of leaning on
`--radius`.

### DO

- Use `<Button>` for every control that performs an action — one solid
  `ink`/`default` CTA per surface, everything else `outline`/`ghost`/`link`.
- `size="icon"` (44px) for icon-only controls on touch surfaces.
- `<Badge variant="lime">` for eyebrow-style labels, `variant="lime-soft"` for
  positive status pills.
- `<AlertDialog>` for every destructive confirmation — never `window.confirm`.
- `toast("…")` from `sonner` for transient feedback — never a bespoke
  `useState` toast.
- `<Select>` for enumerated choices instead of a raw `<select>`.
- `<Input>`/`<Textarea>` as-is — the 16px-floor sizing is already built in,
  don't override `text-*`/`h-*` unless you have a specific reason.
- `<DropdownMenu>` for overflow (`⋯`) actions.
- `scrollbar-none` on every horizontal scroller (poster rails, kind/style
  choosers) — hides the scrollbar, keeps the scroll/swipe.

### DON'T

- Raw `<button>`/`<select>`/`<input>`/`<textarea>` with a hand-rolled
  `className` outside `components/ui/**` — see the raw-control ratchet below.
- Red anywhere in a destructive/error affordance — D10 "no red walls" is
  structural now (`--destructive` is zinc in both themes).
- `.dark` on any user-facing surface, **including the editor** — the editor
  root stays `bg-[#ffffff]` (`EditorShell.tsx`); `.dark` is `/template-jobs` +
  `/admin` only.
- Un-gated `tailwindcss-animate` enter/exit on anything that isn't wrapped in
  a `motion-reduce:` guard.
- Inline helper paragraphs under a label/control — `InfoDot` (§2) is the only
  sanctioned home for optional helper copy.
- Reach for the shadcn `Sheet` (`components/ui/sheet.tsx`) when you mean the
  editor's gesture `Sheet.tsx` (half-detent, non-modal) — they are different
  components with the same name; see the Variant table above.
- `cmdk` — deliberately not installed; no command-palette pattern in Kria yet.

### Raw-control ratchet (enforcement mechanism)

Two layers, both `src/apps/web/`:

1. **`.eslintrc.json` override** on `app/plan/**`, `app/generative/**`,
   `components/**` (excluding `components/ui/**` and `app/admin/**`):
   `no-restricted-syntax` **warn** for a raw `<button className>`, `<select>`,
   `<input className>` (type not `file`/`range`/`checkbox`/`radio`/`hidden`/
   `color`), or `<textarea className>`. Warn, not error, until the last
   migration lane flips it.
2. **`src/__tests__/ui/raw-controls-guard.test.ts`** — a numeric ratchet.
   Counts the same shapes per file against `RAW_CONTROL_BASELINE`, declared
   in per-lane blocks (`LANE_0_BASELINE`, `LANE_A_BASELINE`, …) so each lane's
   diff touches only its own block. A file present in the baseline may only
   go **down**; a file absent must be **zero** (new files are written with
   primitives from the start). The last lane to merge flips the ESLint rule
   to `error` and deletes this test.

### Jest / Radix-in-jsdom notes

`jest.setup.ts` polyfills `ResizeObserver`, pointer-capture
(`hasPointerCapture`/`setPointerCapture`/`releasePointerCapture`),
`Element.scrollIntoView`, and `window.matchMedia` — Radix's `Select`,
`DropdownMenu`, `Tooltip`, and `ScrollArea` call all of these during
open/position. Tests that open one of those primitives must use
`@testing-library/user-event`'s `userEvent.click`/`userEvent.hover`, not
`fireEvent.click` — Radix listens on `pointerdown`, which `fireEvent.click`
does not synthesize.

---

*Rendered-video (FFmpeg burn-in) overlay design is a separate medium — see `docs/pipelines/template.md` and `docs/pipelines/layer2-text-overlay.md` for font and sizing rules.*
