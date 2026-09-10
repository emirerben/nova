# Kria Design System

This document is the **calibration target** for all design reviews and the token source for implementation.
Consumers: `/plan-design-review` and `/design-review` skills, implementers, and AI agents.
**Rule:** any change to the design system must update this document in the same PR. The doc codifies shipped reality — open a new PR to change the system, then update the doc in that same PR.
Product language is governed by [`docs/UX_COPY.md`](docs/UX_COPY.md); this file remains authoritative for visual presentation.

---

## §1 The three surfaces at a glance

| Surface | Canvas | Accent | Type | Mood |
|---|---|---|---|---|
| Landing (`/`, `/auto-story`) | white `#ffffff` | Butter `#fff0a6` + Sky `#9bcaff` | Inter edit-story statements | sunlit editorial |
| Light product (`/plan` chat workspace, `/plan/items/`) | white `#ffffff` / warm ink | Sky, Butter, Sage; Lilac/Plum for text tools | Fraunces headings + Inter UI | chat-first editorial |
| Dark render system (`/template-jobs`) | `bg-black` | amber-400/300 | Fraunces headings | dark theater |
| Admin (`/admin/*`) | `bg-black` | none (white CTAs) | default sans | plain utility |

**Standing rule:** Light editorial = the entire user-facing product (landing, the `/plan` chat workspace, and `/plan/items`; `/plan/new`, `/create`, `/create/manual`, `/library`, and `/generative` are compatibility redirects). Dark render system = render-status flow (`/template-jobs/*`) + `/admin/*` only (the `/template/[id]` config flow was deleted in v0.7.8.2). ProgressTheater is tone-aware (`tone="light"` on all light surfaces, default dark for /template-jobs + admin). Intentional, not drift.

---

## §2 Light editorial system (landing + /plan flow)

Token source: `src/apps/web/src/app/globals.css`, `src/apps/web/tailwind.config.ts`, and `src/apps/web/src/components/KriaEditStory.module.css`. The linked Paper brand file is the visual source for the palette and wordmark; `src/apps/web/src/components/KriaWordmark.tsx` is the web implementation.

- **Canvas:** `bg-[#ffffff]` (`--cream`, pure white); cards separate via zinc borders + shadows.
- **Warm ink scale:** `#30352c` primary (`--ink`), `#526071` secondary, `#677587` control/muted, `#a1a1aa` faint.
- **Sunlit palette:** Sky `#9bcaff` is the brand/selection accent; Butter `#fff0a6` is the primary-action fill; Sage `#dde6cb` carries audio/direction; Lilac `#e7ddf5` and Plum `#332847` are reserved for text-editing tools; White `#ffffff` is the canvas.
- **Landing story accent:** `--story-lime` is retained as a compatibility name for Butter `#fff0a6`; new code should use the semantic brand names above.
- **Legacy `lime-*` compatibility roles:** existing call sites retain their
  class names while `tailwind.config.ts` maps them to Sunlit values: `lime-700`
  is warm ink for small text, `lime-600` is warm ink for readable text and
  solid cells, `lime-500` is Sky for selection and display accents,
  `lime-400`/`lime-300` are Butter actions,
  `lime-200`/`lime-50` are Lilac/soft surfaces, and `lime-800`/`lime-900` are
  Plum text/companion surfaces. New code should use semantic names directly.
- **Cards:** `rounded-2xl border border-zinc-200 shadow-sm`, fill `bg-white` or `bg-[#ffffff]`.
- **Notice line (light surfaces):** `border-zinc-200 bg-white text-[#526071]` quiet informational line — transient warnings/conflicts stay zinc; semantic warning/error colors are reserved for state labels.
- **Landing story screen:** centered 16:9 frame with a 1px ink border, 44px desktop / 30px mobile radius, and transparent fill until the first shot arrives. The screen contains only the active video; captions, visual effects, and placed media must be reflected inside the rendered footage, never as separate DOM cards layered above it. Surrounding source media has no border.
- **Type scale:**
  - Landing story line: Inter medium, `clamp(54px,6.53vw,94px)` desktop; the long middle line uses `clamp(26px,7.6vw,38px)` on mobile.
  - Feature chips: Inter 15/18 desktop and 11/16 mobile, weight 700.
  - Ordinary product headings remain Fraunces; see the landing exception in §5.
- **CTA (InkButton):** the shared primary action uses the Sunlit Butter fill with warm-ink text; the landing story keeps its single CTA visually anchored in the warm-ink family for contrast against footage.
  **Single-primary-CTA rule on landing:** one CTA to `/plan` in its original centered position near the bottom of the edit story — never duplicate it in the header or below the story.
- **Primary-action viewport budget:** on any flow step whose purpose is a single next action, keep that action visible in the first viewport at 1280×720 and 375×667, using realistic maximum AI-generated content length.
- **Light-surface pinned action bar:** when adaptive pinning is needed on `#ffffff`, use `sticky bottom-0 z-10 -mx-5 border-t border-zinc-200 bg-[#ffffff] px-5 pt-4 pb-[max(16px,env(safe-area-inset-bottom))] md:mx-0 md:px-0` (bleeds to the pane edge on mobile, aligns to the text column on desktop). The bar's `border-t` is its only divider — never pair it with a `border-t` on the section that follows.
  Apply it only when the action would otherwise fall below the fold.
- **Hover colors:** Never use purple, violet, lilac, or plum for hover states, including text, borders, and shared `accent` tokens. Use soft Sky with warm-ink text on light surfaces and neutral zinc on dark/admin surfaces. This also applies to legacy `lime-*` aliases that resolve to purple.
- **Touch pressed state:** on touch surfaces, pressed/drag state replaces hover affordance. Active handles solidify and scale slightly; active chips go `opacity-100`; drags show a floating value readout offset from the thumb.
- **Landing story rhythm:** `/` starts the timed composition automatically in one viewport, with no mode selector or playback control; `/auto-story` remains a compatibility route. `/?mode=scroll` retains the pinned `760svh` choreography for direct comparison without exposing it in the interface. Source footage and feature chips surround the centered screen, then travel directly into it before the three statements replace one another. The soundtrack starts muted for autoplay compatibility and makes one synchronized audible attempt at the sound-effects beat; browsers may keep it muted when policy forbids unprompted audio.
- **Shared primitives:** `LightShell`, `LightCard`, `Eyebrow`, `InkButton`, `InfoDot`, `ConfirmDialog` in `src/apps/web/src/components/ui/` (canonical location since v0.4.87.0; `plan/_components/ui/` files are re-export stubs for backward compat). Creator-agent conversations additionally share `ChatMessage`, `ChatThinking`, `AgentComposer`, `AgentApprovalCard`, and `ChatArtifactCard` from `src/apps/web/src/components/chat/`. Since v0.47.0.0, `LightCard`/`InkButton`/`ConfirmDialog` are thin wrappers over the shadcn/ui primitives (`Card`/`Button`/`AlertDialog`) — see §15 for the full component-library contract; every NEW control should use the shadcn primitives directly rather than the legacy wrapper names.
- **Editorial interview layout:** Fraunces question, LEFT-aligned answers, one prior-answer pull-quote with accent left-border (lime), NO message bubbles, NO bot avatar.
- **Chat-first creation workspace exception:** `/plan` is the canonical creation surface. Its Paper workspace shares a 260px project rail between chat and Gallery on desktop (64px restore rail when collapsed). Chat has a full-width conversation before the first render and a 420px conversation rail beside the embedded `EditorShell` after a render is ready. This is a real creative conversation surface, so concise user/assistant turns, artifact cards, upload receipts, confirmations, and recovery actions are allowed here; the editorial interview rule above remains in force for onboarding and other interview surfaces.
- **Creator-agent conversation grammar (AICSS-derived, Nova-owned):** user turns are compact right-aligned ink bubbles (`18px` radius, `6px` lower-right corner); assistant turns are unboxed 14/20 prose with safe long-word wrapping and preserved line breaks. New turns enter with a restrained 180ms opacity/4px rise; thinking is an immediately visible muted shimmer that progresses to task-specific copy at 1.5s/8s/20s, with each label change softened by a 160ms opacity transition. Reduced motion keeps a 120ms opacity-only reveal. Thinking communicates status only, never model reasoning, and responses never simulate token streaming. The composer is a quiet white bordered frame with a circular ink send action, 44px controls, IME-safe Enter submission, Shift+Enter multiline input, and separate input-disabled versus submit-disabled states. Confirmation cards use the shared bordered approval shell and always require a creator click — no countdowns or automatic approval. `AskKriaPanel` reuses the editorial variants (one lime pull-quote + Fraunces reply), not conversation bubbles. These visual patterns are adapted from AICSS under MIT; Nova retains shadcn ownership and has no AICSS runtime dependency.
- **Chat-first layout contract:** the page owns `h-dvh`; only the transcript and editor panes scroll. Chat has no separate workspace heading; the embedded editor has no extra heading or Ready badge. On mobile, use a compact project header, horizontally scrollable format cards, a safe-area-aware composer, a Projects sheet, Gallery view, and Chat/Editor tabs after a cut is ready. The embedded editor uses `?embedded=1` and keeps `EditorShell` as the source of truth; narrow iframes force its feature-complete overlay mode.
- **Chat-first state contract:** the transcript is a projection of durable `creation_thread_events`; `PlanItem` and `Job` remain authoritative for media, renders, and editor state. Every render and revision has an explicit confirmation artifact. Upload failure, unavailable formats, voiceover-needed, stale revision, offline/reconnecting, partial-success, and terminal render failure states must have a visible recovery action. Attached-media counts are hydrated from the thread after refresh.
- **Chat-first semantic transcript:** the conversation is an outcome ledger, not an audit log. User requests and value-adding Kria decisions, questions, reviews, and recoveries may be bubbles. Planning chatter, mirrored prompts, tool events, and raw worker callbacks are never bubbles. Each Job/variant/generation owns one stable lifecycle artifact that progresses in place; late callbacks update only that historical artifact.
- **Chat-first response and approval copy:** Kria may not answer by repeating the request or `thread.state.intent`. Every response leads with a decision, receipt-backed action, decision-changing question, useful progress, bounded review, or concrete recovery. Approval cards name the editorial change and consequence, distinguish “ready to render” from “rendered,” and require a separate explicit action for every initial render and rerender.
- **Chat-first card eligibility:** cards are reserved for playable/media artifacts, state-changing approvals, inspectable draft receipts, and recoveries that require action. A card has one heading, plain-language consequence/evidence, and one primary next move; internal codes remain secondary diagnostic detail.
- **Chat-first motion and a11y:** format cards are keyboard-selectable, the composer submits with Enter (Shift+Enter inserts a newline), focus remains visible at 200% zoom, and reduced motion removes entrance travel while preserving state changes. Match the existing PlanItem media contract: the primary Clips picker accepts video, supporting photos/videos live in the separate Visuals pool, and Narrated reuses the existing voice recorder.
- **Unified editor AI:** the main `/plan` conversation owns creation, editing, review suggestions, confirmations and receipts. The editor has no separate AI drawer or composer. Previewable AI changes stage the live editor draft and remain undoable until Save/export; server-only actions require an explicit main-chat confirmation. Direct editor links open this shared workspace. See `docs/runbooks/unified-editor-chat.md`.
- **D16 lime contrast rule:** lime TEXT under ~18px and text-bearing lime fills → `lime-700`. Display ems, bars, dots, non-text fills → `lime-600`.
- **InfoDot (ⓘ popover, `components/ui/InfoDot.tsx`):** the ONLY sanctioned home for optional helper copy. 16px zinc-400 glyph inline after a label (sibling, never inside a `<label>`); hover/focus ink on zinc-100 disc; open lime-700 on lime-50. Popover: Radix portal `z-[130]` (above the editor Sheet `z-[95]` and the `z-[120]` overlay tier — the popover is always the topmost transient), white `border-zinc-200 rounded-[12px]` shadow, max-w 280px, 13/19 Inter `#3f3f46`, plain sentences only (no heading, no CTA, ≤3 lines). Motion: 180ms scale 0.96→1 + 4px rise from the trigger origin, 120ms fade-out, reduced-motion = fade only (`.info-dot-pop` in `globals.css`). Desktop: hover opens after 150ms, closes 200ms after the pointer leaves; click pins it open. Touch: tap toggles. Outside tap/Esc dismisses; hover-open never steals focus. Hit area 44px (`size="compact"` = 32px in dense inspector rows). NEVER for warnings, errors, disabled reasons, or destructive confirms — those stay visible. Max ~2 dots per screen section.

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
  - Serif accent moments: `text-lg` / `text-xl` (incl. italic `text-amber-300` in `PersonaEditor`); editorial-interview pull-quotes use `text-sm text-zinc-400 line-clamp-3` (zinc, not amber)
  - Body: default sans; secondary: `text-sm text-zinc-400`
- **Radius roles:** `rounded-full` = buttons/pills; `rounded-lg` = inputs/surfaces.
- **Header:** product routes get sticky scroll-fade header (`rgba(0,0,0,0.6·progress)` + blur); landing routes (`/`, `/auto-story`) get a static, borderless white header with no anonymous auth action. Their single “Create my first edit” CTA stays centered near the bottom of the story, with Terms and Privacy beneath it rather than in the header. The light product header (all `isLight` routes) has no border and no nav link — Sky DynaPuff wordmark left, 32px Butter avatar right; the account menu (shadcn `DropdownMenu`) is name · My videos · Sign out. `/admin` hides Header entirely.
- **Chat / interview surfaces:** editorial interview, not chat app — left-aligned Fraunces questions, one prior-answer pull-quote (amber left-border on dark surfaces; lime left-border on light surfaces), NO message bubbles, NO bot avatar.

---

## §4 Admin variant

Dark + zinc like product but: no amber (CTAs `bg-white text-black`), errors `text-red-400`, squared `rounded`/`rounded-lg`, own nav (`border-zinc-800`, active tab `bg-zinc-800`). Utility over mood — keep it plain.

---

## §5 Typography

- `font-display` → `"Fraunces", Georgia, serif` (defined in `tailwind.config.ts`). Headings, display moments, and serif accents only. Fraunces is an optical-size variable font — load with `opsz,wght@9..144` to get smooth weight/size interpolation.
- Body / labels: `"Inter", ui-sans-serif, system-ui` (explicit `font-sans` override in `tailwind.config.ts`). Body text is utility; Inter's neutrality pairs cleanly with Fraunces's personality.
- **Wordmark:** the approved DynaPuff `kria` treatment is implemented as `KriaWordmark`; keep its individual letter rhythm and use Sky on white as the primary signature. Warm ink is the small-size fallback and Butter is the reversed-on-ink option. `KriaMark` is a compatibility alias for the wordmark. Browser favicons use standalone DynaPuff glyph outlines in `public/favicon.svg` and `public/favicon-white.svg`, so they do not depend on page fonts.
- **Landing edit-story exception:** the three over-video statements use oversized Inter at medium weight. They are moving image-composition elements, not section headings: each occupies the same centered slot and uses `mix-blend-mode: difference` as footage passes behind it. All ordinary landing and product headings remain Fraunces.
- Fonts load via Google Fonts `@import` in `globals.css` (not `next/font`). Current import: `DynaPuff:wght@700`, Fraunces optical-size weights 400/500/600, Inter weights 400/500/600/700, and Geist Mono for code.
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
| step-slide (derived #8) | `--page-slide-dur/fade-dur: 250ms`, `--page-slide-distance: 8px`, `--page-blur: 3px`, `--page-slide/fade-ease` | Transcript helper `<StepSlide key={step}>` — slide+blur entrance on each wizard step. |
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

- **Color has a job:** Sky = brand/selection, Butter = primary actions, Sage = audio/direction, Lilac + Plum = text-editing tools, and semantic green/amber/red/blue = status. Amber remains the dark render-system accent (`/template-jobs/*`) where it is already part of the theater language; never introduce an unrelated accent.
- No candy gradients, no rainbow palettes, no purple/violet defaults.
- No 3-column icon-in-circle feature grids; no centered-everything; no decorative blobs/wavy dividers; no emoji as design elements.
- **Serif display (Fraunces) is the brand voice;** system-ui display type is the "gave up" signal.
- **Cards earn existence** — calendar cells, process cards, video tiles are interactions/content, not decoration.
- **Chat = editorial interview** (see §3) — bubbles are an instant fail outside the shared `/plan` creation and editor conversation scoped in §2.
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
| 8 | Disabled CTA state varies across older plan components | `disabled:bg-zinc-700` is the dominant pattern | Normalize opportunistically |
| 9 | Light editorial system covers landing + /plan flow. `/plan/items/[id]`, `/library`, `/generative` remain dark theater. | Resolved v0.4.87.0 — D20 + D21 landed. All user-facing surfaces are now light editorial. §1 standing rule updated. | DONE |
| 10 | Workspace route layout | `/plan` = canonical chat-first creation workspace for every signed-in user; `/plan/items/*` = persisted item/editor compatibility; `/plan/persona` = creator-profile editor | Former plan home/onboarding UI retired. |
| 11 | Display font: Playfair Display → Fraunces | `"Fraunces", Georgia, serif` — optical-size variable, `opsz,wght@9..144`. Rationale: 3-way user comparison (Fraunces / Space Grotesk / Instrument Serif), Fraunces chosen (D6/D8 in based-on-our-talk-deep-hopper plan). Body unchanged → Inter. **Web UI only** — burned-in video fonts (`assets/fonts/`, Skia ASS) unaffected. | DONE v0.4.106.0 |

---

## §12 PlanItem setup and editor compatibility

- **Editor chrome:** the desktop canvas stays at 100% with selection enabled; the title field, canvas Select/Pan switch, canvas zoom selector, and “Re-renders on Save” badge are removed. Undo/redo, orientation, Save, and Cancel remain. Save and playback controls use warm ink with white text and explicit zinc disabled states. Pocket timeline zoom controls remain available.

### Per-type item setup (`/plan/items/[id]`, pre-generation — Lane D declutter)
Design source: Paper "Kria Design System", pages "P3 Item setup" + "C4 Overlays" (Sheet).
Reads top-to-bottom as **receipt → title → uploader → Tell Kria → Generate** — no step
numerals anywhere, no generic audio/caption chrome, no inline chat. Each edit type shows
only what it needs.
- **Header:** back link "← your videos"; `<Badge variant="lime">` setup receipt (`MONTAGE · CLASSIC` — type + montage style) with a `<Button variant="link" size="sm">` "Change"/"Done" that mounts the SetupPicker poster rail inline; per-type Fraunces title ("Add your clips." / "Your voice tells the story." / "Add your clip."). Items minted by /plan/new are untitled (idea = type label) — the release desk shows the receipt Badge instead of an h1 reading "Montage".
- **Uploader:** unchanged per-type branching (pool upload / ShotSlotUploader / single-clip); its section heading is now an sr-only `<h2>` (`aria-labelledby`, no visible step numeral). Any explanatory copy that used to run as a visible paragraph next to it is now a single `InfoDot` (e.g. talking-to-camera's "own audio is the soundtrack…" + "captions and dead-air cleanup" merged into one dot). Classic Clips accepts videos only: a mixed selection keeps valid videos, names every rejected photo in a visible alert, and links to Visuals when that lane is enabled; without Visuals it points to Collage or Photo wall. The Visuals tab shows the server-backed saved count, and the embedded pool repeats the count as a polite live receipt.
- **Tell Kria:** the OLD "Direction for Kria" textarea + its voice-note `<details>` disclosure (`DirectionVoiceNote`) are gone. One field replaces both: `<Label>` "Tell Kria" + inline 12px ink-4 "Optional" + `<Textarea rows={2}>` (`aria-label="Tell Kria"`, placeholder "For example: start fast and keep the candid moments"). Same `notes` → `updatePlanItem` → `refetch` contract as before, `onBlur`.
- **Voiceover (narrated_ready) only:** a white card between the uploader and Tell Kria holds the recorder; its heading is an sr-only `<h2>` "Your voiceover" + one `InfoDot` ("This recording becomes the soundtrack. It is separate from a note to Kria.") — no step numeral, no second helper paragraph. Generate gate still requires the voiceover unless the self-narration flag is on.
- **Guided-edit status row:** when `guided_edit_available`, a compact card under Tell Kria — `Badge` status ("Planning…" / "Draft ready" lime-soft / "Approved" lime-soft / "Needs a look" zinc) + one 14px ink-2 sentence + `Button variant="outline" size="sm"` ("Plan with Kria" / "Review Kria's plan" / "Change plan") that opens `PlanThreadPanel`. The multi-turn conversation itself (`EditProposalCard`) no longer mounts inline or morphs the setup zone — it lives ONLY inside the panel now, mounted with `defaultConversationOpen` so it opens straight onto the conversation surface (no "Plan edit" button morph inside the panel either).
- **`PlanThreadPanel`** (`components/PlanThreadPanel.tsx`): a shadcn `Sheet` — bottom sheet on phones (`inset-x-0 bottom-0 top-auto h-[88dvh] rounded-t-2xl`), right side panel ≥sm (`sm:inset-y-0 sm:left-auto sm:right-0 sm:h-full sm:w-[480px] sm:rounded-none`). Header is Fraunces "Plan with Kria"; body is `<EditProposalCard>` with its existing `applyData`/`forceFreshFetchRef`/`refetch` wiring unchanged.
- **Generate:** untouched — sticky ink `Button` + the shared upload/format/guided-edit gate hint.
- **Removed for good:** the "Audio choice" fieldset (it could silently re-type items — type changes go through the receipt only), the Direction textarea + voice-note recorder, step numerals, "From your idea" seed badge, day badge, "Plan this for me" panels (filming-guide summaries still render for legacy guided items), generic "Photo collage" copy.

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
  quick actions use stock shadcn Buttons in one horizontally scrollable row;
  the first action uses `secondary`, destructive wording stays explicit, and
  **Delete is always the word Delete** (§9: never an icon or emoji). Clip
  actions sit between the timeline and persistent tool dock; other selections
  stay preview-relative.
- **Pocket timeline** (`_editor/MiniStrip.tsx`, legacy name retained for flag
stability): real `Filmstrip` thumbnails scroll beneath a fixed ink near-left
playhead. Half-viewport leading/trailing padding lets first/last frames reach
  the playhead. Tap = select + seek; one-finger body drag = scroll/scrub only;
  source slip remains explicit in the inspector. A selected clip has a
  non-color-only outline, live In/Out/duration receipt, and separate 44px edge
  Buttons that reuse desktop trim math and record one Undo step per gesture.
  Pinch zoom plus visible shadcn − / Fit / + controls cover precision and a11y.
- **Icons** (`_editor/editor-icons.tsx`): stroke SVGs, 24px / 1.6 /
  `currentColor` — supersedes ToolRail's no-icon-set note for mobile chrome; raw
  unicode glyphs take emoji presentation on iOS Safari.
- **Focus-visible:** legacy pocket controls retain `focus-visible:outline-lime-500` (Sky); updated MiniStrip and transport controls use warm-ink outlines. Preserve visible focus in both families.
- **Transport and selection:** LightTransport uses warm-ink playback and scrubber controls. MiniStrip selection and trim handles use warm ink; text-lane items use a Sky border and pale-Sky fill.
- **Follow-up (declared, not silent):** expanded multi-lane mobile takeover,
  sheet section restructure (Eyebrow headers, default expansion, ≥44px
  inner-control density), conflict choice-cards, and real-device Playwright
  touch fixtures. The core near-left-playhead thumbnail trim surface is shipped.

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

Shipped v0.47.0.0 (Lane 0). **Re-skinned to Kria's Sunlit tokens on the
existing shadcn/ui `new-york` structure** (2026-09-09):
(2026-08-22, owner decision):** the component-chrome primitives now render
with Inter utility copy, Fraunces editorial display, Butter/Sky actions, and
warm-ink text. This keeps the component library consistent with the linked
Paper brand file while the dark render/admin variant remains a separate
neutral theater layer.

### Where primitives live

- **`src/apps/web/src/components/ui/*.tsx` (lowercase filenames) = the shadcn
  primitives** — `button.tsx`, `input.tsx`, `textarea.tsx`, `select.tsx`,
  `checkbox.tsx`, `switch.tsx`, `radio-group.tsx`, `label.tsx`, `badge.tsx`,
  `card.tsx`, `dialog.tsx`, `alert-dialog.tsx`, `sheet.tsx`,
  `dropdown-menu.tsx`, `popover.tsx`, `tooltip.tsx`, `tabs.tsx`, `toggle.tsx`,
  `toggle-group.tsx`, `slider.tsx`, `separator.tsx`, `skeleton.tsx`,
  `scroll-area.tsx`, `progress.tsx`, `sonner.tsx`. Style `new-york`, installed
  via `npx shadcn@2 add …`. `components.json` aliases `utils` → `@/lib/cn`
  (no `utils.ts` file — `cn` is hand-authored, shadcn's own).
- **`src/apps/web/src/components/ui/*.tsx` (PascalCase filenames) = pre-existing
  wrappers** — `InkButton`, `LightCard`, `ConfirmDialog` are thin wrappers
  over `Button`/`Card`/`AlertDialog` with **identical props and import
  paths**; every existing call site keeps working with zero edits.
  `InfoDot`, `StyleChip`, `Eyebrow`, `LightShell`, `useFocusTrap` are
  untouched by this migration (§2). New code should reach for the shadcn
  primitives directly (`<Button variant="default">`, not `<InkButton>`).
- `<Toaster />` (from `ui/sonner.tsx`) is mounted once in `src/app/layout.tsx`
  inside `<Providers>`. Call `toast("message")` from `sonner` anywhere — never
  build a bespoke toast/`useState` toast again.

### Variant aliases

Eight downstream lanes already consume the pre-skin variant/size names —
these are kept as aliases, byte-identical to the stock base variant they now
point at, so nothing breaks:

| Alias | Component | Points at (stock base) |
|---|---|---|
| `variant="ink"` | `Button` | `default` (`bg-primary`) |
| `variant="lime"` | `Button` | `secondary` |
| `size="pill"` | `Button` | same footprint as `size="sm"` |
| `size="icon-sm"` | `Button` | `h-8 w-8` (unchanged — no stock equivalent) |
| `variant="ink"` | `Badge` | `default` (`bg-primary`) |
| `variant="lime"` / `"lime-soft"` | `Badge` | `secondary` |
| `variant="zinc"` | `Badge` | `outline` |

New code should reach for the stock variant name directly (`default`,
`secondary`, `outline`, …) rather than an alias.

### Token map

Stock shadcn zinc theme, straight from `hsl(var(--token))` CSS custom
properties in `src/apps/web/src/app/globals.css` (`@layer base`, after the
pre-existing `:root` landing-token block) — see that file for the exact HSL
triplets. `--destructive` is stock red in both themes (no longer zinc — D10
superseded, see the note above). `--ring` is zinc in both themes (no longer
lime/amber). `--radius` is `0.5rem`. Dark exists only for `/template-jobs` +
`/admin`; every user-facing surface, including the editor, stays light.

### DO

- Use `<Button>` for every control that performs an action — `default` for
  the one primary CTA per surface, everything else `outline`/`secondary`/
  `ghost`/`link`.
- `size="icon"` for icon-only controls on touch surfaces.
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
- `.dark` on any user-facing surface, **including the editor** — the editor
  root stays `bg-[#ffffff]` (`EditorShell.tsx`); `.dark` is `/template-jobs` +
  `/admin` only.
- Un-gated `tailwindcss-animate` enter/exit on anything that isn't wrapped in
  a `motion-reduce:` guard.
- Inline helper paragraphs under a label/control — `InfoDot` (§2) is the only
  sanctioned home for optional helper copy.
- Reach for the shadcn `Sheet` (`components/ui/sheet.tsx`) when you mean the
  editor's gesture `Sheet.tsx` (half-detent, non-modal) — they are different
  components with the same name.
- `cmdk` — deliberately not installed; no command-palette pattern yet.

### Raw-control ratchet (enforcement mechanism)

**`.eslintrc.json` override** on `app/plan/**`, `app/generative/**`,
`components/**` (excluding `components/ui/**`, `app/admin/**`, and
`app/plan/items/[id]/components/EditProposalCard.tsx` — see below):
`no-restricted-syntax` is **error** for a raw `<button className>`,
`<select>`, `<input className>` (type not `file`/`range`/`checkbox`/`radio`/
`hidden`/`color`), or `<textarea className>`. Flipped from `warn` to `error`
once the last migration lane cleared every remaining warning (2026-08-23).
The earlier numeric-ratchet test (`src/__tests__/ui/raw-controls-guard.test.ts`)
was removed by the shadcn migration train (#889) once the ESLint rule became
the sole enforcement layer.

**`EditProposalCard.tsx` exclusion:** temporarily carved out of the override
because it was under active, unrelated development (its own Select-primitive
conversion PR) at the moment the guard flipped to `error`. Remove the
exclusion once that PR lands and the file's raw `<select>`s are migrated.

### Jest / Radix-in-jsdom notes

`jest.setup.ts` polyfills `ResizeObserver`, pointer-capture
(`hasPointerCapture`/`setPointerCapture`/`releasePointerCapture`),
`Element.scrollIntoView`, and `window.matchMedia` — Radix's `Select`,
`DropdownMenu`, `Tooltip`, and `ScrollArea` call all of these during
open/position. Tests that open one of those primitives must use
`@testing-library/user-event`'s `userEvent.click`/`userEvent.hover`, not
`fireEvent.click` — Radix listens on `pointerdown`, which `fireEvent.click`
does not synthesize.

### Backlog (deferred from Lane F)

- **`src/apps/web/src/components/TikTokPublishDialog.tsx`** — the outer shell
  is still NOT converted to the shadcn `Dialog` primitive. At 1166 lines it is
  a hand-rolled `createPortal` + `useFocusTrap` sheet with multi-step state
  (`details`/`confirm`), per-mode idempotency keys in `sessionStorage`, and a
  373-line test suite (`src/__tests__/tiktok/TikTokPublishDialog.test.tsx`)
  that pins exact focus behavior (e.g. `document.activeElement` lands on the
  step `<h2>` on open, not a button) incompatible with `Dialog`'s default
  auto-focus. Swapping the outer shell would mean re-deriving that focus
  contract under Radix, which did not fit Lane F's (or the raw-control
  ratchet flip's) budget. The file's internal controls (buttons, the caption
  `<textarea>`) WERE migrated to `<Button>`/`<Textarea>` — those are plain
  styled elements with no portal/focus-trap behavior of their own, so the
  swap is safe without touching the shell; the pinned focus-order test suite
  still passes unchanged. A future lane should re-scope the shell itself as
  its own PR: port the focus-trap/step semantics onto `Dialog` deliberately,
  updating the pinned test assertions alongside it.

---

*Rendered-video (FFmpeg burn-in) overlay design is a separate medium — see `docs/pipelines/template.md` and `docs/pipelines/layer2-text-overlay.md` for font and sizing rules.*

## Native iOS alignment

The iOS app uses the same Sunlit semantic roles and approved DynaPuff wordmark as
web, implemented in `src/apps/ios/Kria/DesignSystem/`. Paper’s **Mobile Flow** is
the native composition reference. Inter/Fraunces retain Dynamic Type; native
system authentication, pickers, consent, and share interfaces keep platform
behavior. Navigation uses a branded left drawer with Gallery, recent chats,
New chat, and account access. Composer input is text and attachments; microphone
chat input is deferred. See `docs/runbooks/ios-development.md` for review fixtures.

### iOS wordmark asset

The native app uses the approved Main Brand Assets icon artwork (Paper ETC-0): DynaPuff letterforms in `#9BCAFF` on white `#FFFFFF` for the app icon. `KriaWordmark.imageset` preserves the same lettering as a transparent vector PDF, cropped to the artwork bounds and centered within the existing header frame. Do not reconstruct this mark with independent SwiftUI text offsets. This artwork color does not change the semantic Sky selection token.
