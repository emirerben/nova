# Declutter audit — helper-text inventory (2026-08-17)

Three-agent sweep of every page in `src/apps/web/` for static explanatory text.
146 strings classified: **86 KEEP / 39 INFO-ICON / 20 DELETE**. The v1 train
(this PR) executes the public + editor lanes; the admin lane is deferred (see
bottom table). Design contract: DESIGN.md §2 "InfoDot" + §9 "No inline helper
paragraphs". Line numbers are as-of audit date; treat as anchors, not gospel.

## Classification rules

- **KEEP (visible):** disabled-state reasons, destructive confirms, conditional
  warnings/errors, async reassurance, hard constraints ("Up to 10 videos"),
  option-card disambiguation captions. Hiding these causes task failures.
- **INFO-ICON:** useful on first read, dead weight afterward → `<InfoDot>`.
- **DELETE:** restates the heading/UI, happy-talk, leaked internal vocabulary.

## Executed in this PR

### Public pages — DELETE
| File | Text |
|---|---|
| `plan/_components/TikTokPreScreen.tsx` | "Drop your handle and we'll skip to the interesting questions." |
| `plan/_components/workspace/IdeasHome.tsx` | "Every idea here becomes a video." |
| `library/page.tsx` | "Everything you've made, newest first." |
| `plan/items/[id]/page.tsx` | "You can generate anyway — this is just a read on the brief." |
| `transcript/BriefStep.tsx` | no-clips elaboration + "Watching your clips so the words land…" |
| `plan/items/[id]/components/ShotSlotUploader.tsx` | "Got more good moments? Add them…" |
| `template-jobs/[id]/page.tsx` | "Assembly breakdown" section (internal `slot_type` vocab) |
| `plan/items/[id]/components/EditProposalCard.tsx` | both "Plan edit" captions |

### Public pages — INFO-ICON
PersonaEditor persona-purpose sublines + posts-per-week note; SeedUploadCard
initial/loading/success blurbs (headline + one status line stay); item-page
iCloud tip; generative-page auto-length + voiceover-conditional captions.

### Editor — DELETE
InspectorPanel "Visual effect and playback", "Controls for this element arrive
with the timeline update.", "Focus pulse"; ToolDrawer "Animated building
blocks, matched in export"; OverlaySuggestions "Kria places your screenshots…";
EditorShell "Full timeline editing on desktop"; VariantCard duplicate `title`
attr (visible editorial-hook line KEEPS — it is a disabled reason).

### Editor — INFO-ICON
InspectorPanel bed-level explainer, per-cue scoping note, ONE group-level dot
for Look presets; CaptionsDrawer "Applies to every line"; CaptionStyleToggle
clarifiers; BackgroundSoundControl ducking explainer; CarouselPanel mode
descriptions; CopilotDrawer first-message capabilities bubble; EditProposalCard
"AI thoughts stay drafts…". CaptionEditor instructions compressed to one
visible line (procedural, not hidden).

## Deferred — admin lane (16 INFO-ICON, 0 DELETE)

| # | File (~line at audit) | Text (lead) |
|---|---|---|
| 1 | `admin/templates/new/page.tsx` ~L276 | "Recipe is generated end-to-end by agents…" (agentic checkbox, 3 sentences) |
| 2 | `admin/templates/[id]/page.tsx` ~L1080 | "Subject (optional) — substituted for ALL-CAPS placeholders…" |
| 3 | `admin/templates/[id]/page.tsx` ~L1103 | Fast preview "skips curtain-close + copy generation…" |
| 4 | `admin/templates/[id]/page.tsx` ~L1726 | "Click to cycle: null → true → false… ?use_layer2=" |
| 5 | `admin/templates/[id]/components/EditorTab.tsx` ~L282 | AgenticEditorLock two-paragraph lock explanation |
| 6 | `admin/templates/[id]/components/EditorTab.tsx` ~L364 | "Sets font_default. Cascades to every overlay…" |
| 7 | `admin/templates/[id]/components/OverlaysTab.tsx` ~L303 | 4-sentence header explaining the overlay editing model |
| 8 | `admin/templates/[id]/components/RequiredInputsEditor.tsx` ~L74 | "Fields the upload UI collects from end users…" |
| 9 | `admin/music/[id]/page.tsx` ~L677 | Smart Captions licensing caveat (2 sentences) |
| 10 | `admin/music/[id]/components/LyricsTab.tsx` ~L127 | "Tune lyric overlays independently…" header |
| 11 | `admin/music/[id]/components/LyricsTab.tsx` ~L132 | "Line carries per-knob tuning…" header |
| 12 | `admin/music/[id]/components/LyricsTimingPanel.tsx` ~L205 | "Inter-line lyric transitions use automatic crossfade…" |
| 13 | `admin/_shared/LyricsConfigPanel.tsx` ~L279 | inheritance strip (custom): "Custom to this template…" |
| 14 | `admin/_shared/LyricsConfigPanel.tsx` ~L292 | inheritance strip (inherited): "Inherits from the linked music track…" |
| 15 | `admin/extension/install/page.tsx` ~L57 | "Interim internal install." roadmap banner |
| 16 | `admin/extension/install/page.tsx` ~L65 | BasicAuth/zip-contents reassurance note |

Everything else in admin was KEEP (single-line labels, conditional error/safety
states, or already behind `title=`/`<details>` disclosure).

## Explicitly out of scope
Landing page (`/`) marketing copy; `/tiktok` compliance sandbox; existing
`title=""` attr hints (migrate to InfoDot opportunistically).
