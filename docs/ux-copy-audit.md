# Production UX copy audit

Status: implementation ledger for the site-wide copy pass. The historical
helper-text classification remains in `docs/declutter-audit.md`.

Scope: production non-admin routes, shared creator components, metadata, and
error boundaries. Excluded: `app/admin/**`, `app/dev-qa/**`, redirects with no
visible UI, user-authored text, generated creative copy, and substantive legal
clauses.

| Route or surface | Previous copy or behavior | Approved copy or behavior | Status |
|---|---|---|---|
| `/` and shared header | Product story, CTA, clickwrap, metadata, and accessible story used mixed product language and did not name the sign-in action consistently. | `Keep filming` / `Let Kria edit your videos` / `Share more`; `Create a video`; `By signing in, you agree to Kria's Terms of Service and Privacy Policy.` Visible, screen-reader, and metadata copy match. | Complete |
| `/plan` onboarding | `Do you post on TikTok?`, generic continue/skip actions, and limited explanation of profile access. | `Do you already post on TikTok?`, `Use my TikTok profile`, `Skip for now`, `What do you like to make?`, and `Create my content plan`. TikTok is optional and only the public profile is reviewed. | Complete |
| `/plan` first-video fork | The fork used vague start/skip language and an unsupported fixed `~90s` promise. | `Let's make your first video`, `Use footage I already have`, `Start with an idea`, and `Skip for now`. Fixed timing copy is removed unless supported by real state. | Complete |
| `/plan` footage upload | Failed uploads used a retry label that only removed the tile and did not explain accepted files. | `Add videos`; `You can add up to 10 clips`; failed uploads name the file and accepted MP4/MOV constraint, preserve the tile, and expose a working `Retry upload` action. | Complete |
| `/plan` workspace | `Make a new video`, `Past edits`, generic pagination, and an empty grid with no next step. | `Create a new video`, `Create a video`, `Your videos`, `Load more videos`, `Connected accounts`, and `Your finished videos will appear here. Create your first video to get started.` | Complete |
| `/plan/new` | Internal format/preset names and generic save failures. | `What do you want to make?`, `Choose a format`, `Choose a visual style`, `Music montage`, `Voiceover story`, `Talking head + supporting clips`, `Collage`, and `Photo wall`; failed saves name the connection recovery. | Complete |
| `/create` | Creation language mixed clips, assets, instructions, and voiceover concepts; failures could surface raw error details. | `Create a video from your footage`, `Tell Kria what to emphasize (optional)`, `Add narration (optional)`, `Add videos`, and `Create video`; failures use the shared safe taxonomy and action labels. | Complete |
| `/create/manual` | Generic uploader/editor labels and a dead-end unsupported-photo state. | `Arrange your footage`, outcome-based editor actions, `Export video` for output, and direct recovery guidance for unsupported photos. | Complete |
| `/plan/persona` | `Meet your persona`, `Tweak`, `Save edits`, blank loading, and silent redirect on request failure. | `Your creator profile`, `Edit profile`, `Save profile`, `Back to content plan`, an accessible loading status, and `Retry loading profile` on failure. | Complete |
| `/plan/style` | `Your style`, generic retry, render terminology, and no working retry for the initial request. | `Your editing style`, `Style saved. Your next video will use it.`, `Retry loading style`, `Retry style update`, and `Back to content plan`. | Complete |
| `/plan/items/[id]` setup | Mixed generate/upload language, ambiguous instruction fields, and narration entry copy that did not distinguish a script from a recording. | `Tell Kria (optional)`, `Need narration? Write a script with Kria`, `Drop videos here or choose files`, `Add videos`, `You can add up to 10 clips`, and `Create video`. | Complete |
| `/plan/items/[id]` editor | User-visible Nova copilot labels, vague empty selection, decorative action glyphs, raw failures, and `Place visuals for me` / `Re-match visuals`. | Kria throughout; `Select a clip, caption, or overlay to edit it.`, `Place visuals automatically`, `Match visuals again`, creator-safe errors, and visible/ARIA outcome parity. | Complete |
| `/plan/items/[id]/transcript` | Transcript/voiceover/script terms overlapped and reading time used fragmented copy. | `Write narration`, `What should this video say?`, `Your narration script`, backend-derived `About … to read`, `Record this script`, `Use this recording`, and `Record again`. | Complete |
| Shared progress and results | Phase labels included hype, UK spelling, internal edit/variant terms, and fixed timing claims. | `Waiting to start`, `Reviewing your footage`, `Choosing music`, `Rendering your video`, `Finishing up`, `See how Kria made it`, and `Your video is ready`; elapsed and ETA text is state-derived only. | Complete |
| Preview and render failures | Preview failures could imply the video failed; unknown failures could expose backend text or omit recovery. | `The preview couldn't load, but your finished video is safe. Try the preview again or download the video.` Render failures use shared safe categories, action labels, and support references without backend codes. | Complete |
| `/tiktok` and shared TikTok controls | Delivery modes, Direct Post, inbox handoff, and `Exact approved render` were unclear on first use. | `Publish now` and `Finish in TikTok` are contrasted and explained; `The exact video you approved` replaces internal render language while compliance copy remains intact. | Complete |
| `/template-jobs/[id]` | A local failure dictionary, raw reroll errors, and inconsistent result/version labels. | Shared failure copy and recovery labels; `Your video is ready`, `Create another version`, and `No more versions available`. | Complete |
| `error.tsx` and `global-error.tsx` | Generic page failure language and digest display without a clear support label. | `This page couldn't load`; `Your saved videos are safe. Reload this page, or return to your videos.`; `Reload page`; `Go to My videos`; and `Support reference`. | Complete |
| `/terms`, `/privacy`, and root metadata | Visible review markers, clickwrap/action mismatch, and generic Nova metadata. | Review warnings remain source comments only; legal meaning is unchanged; metadata is `Kria — AI video editor for short-form content` with a concrete matching description. | Complete |
| `/architecture` | Missing route metadata, title-case controls, terse abbreviations, and weak async failure/loading semantics. | Route metadata, sentence-case controls, expanded labels, accessible loading/errors, and plain-language explanations alongside necessary developer vocabulary. | Complete |

Completed with updated focused tests, the production-copy contract scan, mobile
and desktop wrapping checks, the full frontend Jest suite, lint, and TypeScript.
