# KRI-160 — Mobile slide-post design handoff

Selected on 2026-09-22 for [KRI-160](https://linear.app/kria/issue/KRI-160/the-slide-post-for-ios): **Option A, preview first, retaining the floating Kria AI button.** This document captures the approved visual direction and complete illustrative journey. It is not a native implementation or engineering sign-off.

## Product intent

Turn a creator's photos and videos into a connected, editable, swipeable post inside Kria's existing mobile workspace. Kria proposes the story; the creator controls the order, cover, text, look, and caption. The deliverable is separate ordered slide assets and a caption. An abbreviated preview MP4 is never presented as the finished post.

## Entry and animated cover

Keep the existing format carousel in this order: Montage → Narrated → Talking to camera → Photo & video post. The shared heading reads “What are we making?” The prototype abbreviates the existing card label to “Talking”; the new post card remains immediately after it.

The post cover fills a square with one edge-to-edge image at a time. It swipes horizontally through a photo, a muted video, and a second photo, then returns seamlessly to the first. Each frame holds for roughly 2.6–2.7 seconds; each swipe takes 600ms, making a 10-second loop. A count and synchronized pagination dots identify the series. This supersedes the overlapping two-photo and three-thumbnail explorations.

Use a separate 44pt Pause/Play control that does not select the format. Animate only while at least 65% visible and the page/app is active. Play the muted video only while its frame is entering or visible; pause it behind other frames. Reduce Motion shows the first photo and sequence indicators without autoplay. The reference square is 156pt, or 148pt at compact widths.

## Complete journey

| Step | Intended behavior |
| --- | --- |
| Choose format | Enter through the existing chat carousel; do not add global navigation. |
| Start empty | Show the selected format/platform and one Add photos & videos action above the existing composer. |
| Pick media | Use actual system Photos or Files pickers with multi-selection, duration badges, and explicit confirmation. Cancelling preserves prior selection and typed text. |
| Import | Return thumbnail receipts with per-item status. Preserve successes, identify failed files, offer retry/remove, and block submission while imports are incomplete. |
| Prompt Kria | Send selected media and optional free text together. Suggested directions are editable. A blank prompt is valid when sufficient media is ready. |
| Review direction | Propose an opening, connected middle, ending, feel, and concise text. Refine in the same chat; preserve the original brief and subsequent instructions. Create post requires an explicit tap. |
| Create | Show a truthful stage, not a fabricated percentage. Returning to chat keeps creation running and exposes Open post when ready. |
| Review | Open the large preview with visible slide order. Play video in its slide. Preserve context when returning to chat. |
| Edit | Provide order, cover, text, look, caption, and floating Kria AI tools. Keep the full video timeline out of this slide-post surface. |
| Prepare | Prepare the current saved version as separate ordered slides plus caption. Identify failed slides and require retry or explicit removal before complete export. |
| Save/share | Request Photos permission at save time. Report actual saved count; offer Files when permission is denied. Keep caption copy separate. No direct social-publishing promise. |
| Return | Gallery labels the format and slide count. Reopen the post workspace; New post returns to the chooser. |

## Option A and floating Kria

Use an output-ratio preview, ordered thumbnail strip, and focused direct tools. Cover follows stable slide identity when the creator reorders slides. Accessible Move earlier/Move later actions accompany native drag reordering.

Retain the native editor's 52×52pt warm-ink circle with a white 23pt sparkle icon, accessible as “Open Kria conversation.” Anchor it at the preview's lower trailing edge, with the existing 16pt screen and 14pt bottom insets. Keep it separate from the primary export action and hide it during focused text entry as the native editor already does.

Open contextual editing chat in a medium/large native sheet with a drag indicator. Closing restores the selected slide and pending message. A request produces a proposal; applying it requires a separate tap and returns to the preview. Preserve an undo path. The prototype demonstrates shortening text, changing the cover, and moving the last slide first with canned responses; it does not define arbitrary AI editing support.

Implementation references: [NativeEditorView.swift](../../../src/apps/ios/Kria/Features/NativeEditorView.swift), its `Open Kria conversation` control, and existing conversation sheet. Reuse those native semantics. Web editor chat routing is not a substitute for this explicitly approved mobile interaction.

## Visual and accessibility specification

Calibrate implementation against [DESIGN.md](../../../DESIGN.md) and native design tokens: white canvas, warm ink `#30352C`, muted `#526071`, Sky `#9BCAFF` for selection, Butter `#FFF0A6` for primary actions, and Sage `#DDE6CB` for direction. Use Fraunces headings and Inter UI/body text; use the approved native wordmark asset.

Reference phone: 393×852pt. Check 375–430pt widths, 20pt horizontal insets, 8/16/24pt spacing, 16pt body/input text, 29pt main headings, 44pt minimum touch targets, and 50pt primary buttons. Secondary metadata must not carry essential constraints alone. Preview uses the platform output ratio.

Preserve Dynamic Type; large text scrolls without shrinking controls and the safe-area action stays reachable. Reduce Motion removes travel; Reduce Transparency uses solid readable surfaces. Exclude slide/carousel swipes from the project drawer gesture. VoiceOver order should follow header → status → slide/position → navigation → tools → primary action. Use actual iOS picker, keyboard, permission, and share behavior. iPad may place preview beside inspector without changing navigation.

The HTML approximates typography, status bars, system sheets, and filters. CSS looks do not establish native renderer parity. Browser checks do not validate VoiceOver, Dynamic Type, native keyboard behavior, or device performance.

## Existing contracts and implementation boundaries

The web/backend already has slide-post draft schemas, composition/editing, platform profiles, normalized slide assets, and ordered ZIP export. Relevant sources are [slide_post.py](../../../src/apps/api/app/schemas/slide_post.py) and [profiles.py](../../../src/apps/api/app/pipeline/slide_post/profiles.py). These are app profiles, not a claim about current external platform limits:

| Profile | Media/count | Output |
| --- | --- | --- |
| Instagram | Mixed images/videos, 2–20; video up to 90s | 1080×1350 (4:5) |
| TikTok photo mode | Images only, 1–35 | 1080×1920 (9:16) |

The existing edit surface supports stable slide IDs, order, cover, caption, one text block of up to 120 characters at top/center/bottom, and five looks: Original, Olive Film, Smoky Split-Tone, Golden Hour, Faded Analog. The current caption cap is 2,200 characters.

Reference API operations are `PUT /plan-items/{id}/slide-post`, `POST /plan-items/{id}/slide-post/compose`, and `GET /plan-items/{id}/variants/{variant_id}/slides/bundle`. Bundle output contains ordered normalized media, `post.json`, and `caption.txt`. The silent abbreviated MP4 (2.5s photos, at most 4s video previews) is only for preview.

Native integration must use authoritative draft versions and the native recipe/capability model. Phone originals stay local unless the creator explicitly consents to supported cloud routing. Do not introduce a silent cloud fallback. Native slide rendering/export is still an engineering qualification even though web/backend slide-post support exists.

Explain incompatible media before composition. A TikTok photo-only branch must preserve the original mixed draft rather than silently dropping videos or converting them to stills. The prototype can illustrate the warning but keeps only one in-memory demonstration draft.

## Required state handling for implementation

| Surface | Required recovery or receipt |
| --- | --- |
| Media import/upload | Per-file progress and failure; keep successful attachments; retry/remove only affected items. |
| AI composition | Preserve selection/prompt; retry or manual arrangement; disclose fallback and any exclusion before acceptance. |
| Draft save | Distinct Saving, Saved, and Save failed states; preserve input; resolve changed server versions explicitly. |
| Preview | Real placeholder while loading; name affected slide; retry preview separately from render. |
| Export | Prepare the current version; no obsolete or silently incomplete output; actionable preparation failure. |
| Photos | Permission at save, accurate per-file receipt, retry remaining files without duplicate saves. |
| Gallery | Preserve prior list on failure; readable format/count; missing-preview placeholder and retry. |

The connected happy path, denial-to-Files path, and a separately selectable render-recovery scenario are illustrated. The entire production state matrix above is not implemented in the HTML.

## Open decisions before native build

1. Qualify native slide rendering/export and account-specific capability/consent routing.
2. Validate Photos multi-asset saves, ordering guidance, partial-save receipts, and retries on a physical device.
3. Decide whether coordinated AI-authored text/style is in KRI-160 beyond existing composition support.
4. Define draft conflict handling and acceptance/undo under full-draft writes, including preserving a separate photo-only branch.
5. Complete the full seven-pass design review and engineering review. Visual selection is approved; neither review is being claimed as complete.

Direct social publishing, a full video timeline/audio/transitions, free-position text, a new generative typography model, and silent cloud fallback are outside this approved design scope.

## Review and verification status

The prototype assumes a returning signed-in creator with existing AI consent completed. All sample AI replies, imports, creation progress, save receipts, system surfaces, and Gallery contents are local simulation. No native build, authenticated backend journey, physical-device export, or spoken VoiceOver session has been validated by this package.

Browser verification covers the connected creation and editing flow, Photos/Files selection and cancellation, prompt retention, background creation via chat, local video playback, Kria apply/undo, save and denial-to-Files receipts, Gallery return, compact layouts, and the swipe-cover motion controls. See the [package README](README.md) for review files and reproducible startup instructions.

| Review | Status |
| --- | --- |
| Visual direction | Approved: Option A + floating Kria + full journey + animated final format card |
| Full design review | Open; seven-pass review incomplete |
| Native engineering qualification | Pending |
| Design artifact verification | Browser/static checks only; no production capability claim |
