# Kria UX copy standard

This document is the source of truth for product language across Kria's
production, non-admin web experience. `DESIGN.md` remains authoritative for
visual presentation; this file governs what the interface says.

## Voice

Kria sounds calm, direct, editorial, concise, and helpful. Write like a
capable collaborator who respects the creator's time.

- Prefer concrete verbs and active voice.
- Use sentence case. Contractions are welcome.
- Use US English in source copy.
- Avoid hype, apology filler, exclamation marks, decorative emoji, and
  textual arrows.
- Button labels use a verb plus the outcome and have no period: `Create
  video`, `Download video`, `Publish to TikTok`.
- Use an ellipsis only while work is actively happening.
- Success is calm. Warnings are explicit. Errors are blame-free.

## Product vocabulary

| Term | Meaning and use |
|---|---|
| Kria | The product and assistant. `Nova` is an internal implementation name and never user-visible. |
| video | The intended or finished result. |
| edit | The editing process, not the finished artifact. |
| version | An alternate result made from the same source. |
| footage | A collection of source media. |
| clip | One source video. Use `photo` for one image. |
| content plan | A creator's ideas or publishing schedule. |
| edit plan | A proposed structure for one video. Never use bare `plan` when the meaning is ambiguous. |
| narration | The spoken story or its script. |
| voiceover recording | The recorded audio used for narration. |
| captions | Timed text representing speech. |
| on-screen text | Authored text overlays that are not speech captions. |
| song / music | Creator-facing audio choices. `Track` is reserved for technical or catalog contexts. |
| render | Allowed in editor and progress contexts, but never as the primary marketing action. |

Keep `variant`, `lane`, `asset pool`, `agentic`, `assembly`, raw status enums,
and infrastructure names out of creator-facing copy. The developer-facing
architecture map may use necessary technical terms, paired with a plain-English
description when the term is not widely understood.

## Interface patterns

### Calls to action

Name the outcome, not the form action. Prefer `Create video` over `Continue`,
`Save profile` over `Save`, and `Retry render` over `Try again`. A secondary
action may be short when its destination is already explicit, such as `Cancel`.

### Errors

State what failed, give the known reason or admit uncertainty, then provide a
specific recovery action. Never render raw exceptions, HTTP statuses, worker
output, enum values, or stack traces on creator surfaces.

> We couldn't save this format. Check your connection and try again.

If data is safe, say so only when the product can guarantee it. Distinguish a
preview failure from a failed video render.

### Empty states

Explain what belongs in the space, why it is empty, and the first useful
action. Lead with the invitation, not `Nothing here yet`, and offer one primary
CTA.

### Loading and progress

Say what Kria is doing and set a useful expectation. Tell creators when they
can leave safely. Percentages, ETAs, counts, and completion claims must come
from real backend data. Every visual skeleton has a polite screen-reader
status. Never infer failure from silence.

### Confirmations

Name the action and its consequence. Use an explicit destructive label and a
neutral escape: `Delete clip` / `Keep clip`, not `OK` / `Cancel`.

### Help and constraints

Keep constraints, warnings, disabled reasons, and recovery guidance visible.
Put optional first-use education in `InfoDot`; do not hide load-bearing copy.

## Accessibility and localization readiness

- Visible and accessible labels describe the same outcome.
- Give every icon-only control a contextual accessible name.
- Announce async state changes once with the appropriate live-region role.
- Write complete interpolated sentences so a future translator can reorder
  them. Avoid sentence fragments split across JSX nodes.
- Use shared number, duration, date, and plural formatters.
- Avoid idioms, slash constructions, and culture-specific metaphors.
- English is the only shipped locale today; this standard does not introduce
  locale routing or a translation catalog.

## Boundaries and review

These rules apply to headings, labels, buttons, links, placeholders, helper
text, empty/loading/error/success states, confirmations, tooltips, toasts,
accessible names, metadata, and non-substantive legal UI labels.

Do not rewrite user content, transcripts, generated creative copy, legal
meaning, backend identifiers, or operator diagnostics. Admin and developer-QA
surfaces are outside this standard. A copy change updates this document only
when it changes a rule or term; reusable or high-risk copy must have a focused
test.
