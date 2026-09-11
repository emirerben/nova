# Kria

AI-powered tool that transforms raw real-life videos into viral-ready short-form content (TikTok, Reels, YouTube Shorts).

## Quick start

```bash
cp .env.example .env    # fill in your values
./scripts/dev-auto.sh    # starts infra, migrations, API, worker, and web with hot reload
```

- Frontend: http://localhost:3000
- API: http://localhost:8000

## Structure

```
src/apps/web/   — Next.js frontend
src/apps/api/   — Python FastAPI + Celery
src/apps/ios/   — native SwiftUI/AVFoundation app (iOS 18; XcodeGen)
agents/         — agent context (read before working on video processing)
docs/           — pipeline internals, runbooks, specs, designs (start at docs/pipelines/)
CLAUDE.md       — working agreements, invariants, key paths, env vars
DESIGN.md       — design-system tokens, loading rules, anti-slop rules, a11y baseline
LICENSES.md     — third-party component and font licenses
TODOS.md        — deferred work backlog, grouped by the PR that deferred it
```

Native setup, build commands, and architecture boundaries are in the [Kria iOS development runbook](docs/runbooks/ios-development.md). The [phone-rendering pilot runbook](docs/runbooks/phone-rendering.md) describes the account-gated experimental path, supported edits, consent, and remaining KRI-29 release gates.

## Features

- **Chat-first creation** — start at `/plan`, choose Montage, Narrated, or Talking to camera, add your footage, direct Kria in the conversation, approve a render, then keep editing in the same conversation beside the ready cut. Previewable AI edits stay undoable until Save/export; direct editor links reopen this workspace ([creation runbook](docs/runbooks/chat-first-creation.md); [editor chat runbook](docs/runbooks/unified-editor-chat.md); [implementation plan](plans/021-chat-first-creation.md)). Legacy `/create` and `/create/manual` links redirect here.
- **Template mode** — drop your clips into a viral template; Gemini analyzes each clip and matches it to the right slot
- **Music beat-sync** — browse a music gallery, pick a song, upload clips; every cut lands on a detected beat (`/music`)
- **Guided Plan edit** — describe the story you want in ordinary language, review how Kria uses all uploaded photos and videos, approve before rendering, then edit the approved story's timeline, Looks, music, and supported layers on desktop or mobile ([pipeline and rollout guide](docs/pipelines/guided-edit.md); [mobile timeline plan](plans/020-mobile-video-editor-timeline.md); [native editor parity plan](plans/024-native-editor-component-parity.md))
- **Main Creator Agent (dark rollout)** — describe the feeling or story for a plan item, review one capability-aware creative direction, and explicitly confirm the typed render ([architecture, API, and rollout guide](docs/pipelines/creator-agent.md))
- **Kria domain agent (dark rollout)** — one durable creation and revision conversation with useful editorial decisions, reversible server drafts, portable text/timeline/caption/music/mixed-media edits, exact approval before every render, receipt-backed outcomes, and recovery across refreshes and worker failures ([runtime architecture](docs/pipelines/kria-agent-runtime.md); [operator runbook](docs/runbooks/kria-agent-runtime.md))
- **Creator Blocks** — add, customize, time, and ask Nova to edit nine deterministic animated text and image blocks in the video editor ([runtime and rollout guide](docs/pipelines/motion-runtime.md))
- **TikTok publishing beta** — connect TikTok, privately publish an approved final render, or send it to TikTok to finish as a draft ([operator runbook](docs/runbooks/tiktok-direct-publishing.md))
- **Admin tools** — upload music tracks (YouTube/SoundCloud via yt-dlp), monitor beat analysis, publish/archive (`/admin/music`)

## Branch conventions

- `main` — protected, requires PR + 1 approval
- `dev` — integration branch
- `{initials}/{feature-slug}` — feature branches (e.g. `ee/upload-endpoint`)

## Cofounder setup

```bash
bash setup-cofounder.sh
```
