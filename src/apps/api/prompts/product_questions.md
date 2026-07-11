# Product Questions — Kria's Rubric Spine

The questions Kria must answer "yes" to for users. Single source of truth that feeds:

- the **discovery gap-finder** (`/audit-product-questions`) — grades the live PRODUCT
  against the §A–C questions, then mints candidate build tasks for unanswered ones.
- the **final-video grader** (`tests/evals/rubrics/final_video.md`) — grades each
  rendered OUTPUT against the §D questions.

Versioned on purpose: editing this file is a reviewable PR. A guard test keeps the
grader rubric and the discovery question-set in sync with it.

Each question states the bar ("Answered well when"), the signal ("Evaluated by"),
and which consumer it feeds.

## A. Comprehension & Trust   (→ discovery gap-finder)

### A1 — Does the user understand what the product does?
Answered well when: a first-time visitor can state in one sentence that Kria turns
raw videos into short-form content — within ~10s of landing, no docs.
Evaluated by: landing/onboarding copy inspection; first-session drop-off.

### A2 — Does the user understand the outcome / value?
Answered well when: the user can describe the concrete result ("a TikTok-ready edit
of my footage") and why it's worth their time, before uploading anything.
Evaluated by: value-prop copy inspection; activation rate (visit → first upload).

### A3 — Does the product feel credible and trustworthy?
Answered well when: nothing reads as auto-generated filler, broken, or sketchy;
example outputs are real and good; what happens to their footage is clearly stated.
Evaluated by: design/copy inspection; real example outputs present; trust-signal checklist.

### A4 — Does onboarding explain what to upload and how?
Answered well when: before the first upload the user knows what footage works, how
long, how many clips, and how to add them — no guessing, no support.
Evaluated by: onboarding-flow inspection; first-upload success rate; upload-error rate.

## B. Editing & Control   (→ discovery gap-finder)

### B5 — Can users rearrange video order, music, text, timing, and audio?
Answered well when: each of the five — clip order, music, on-screen text, timing,
audio — is adjustable, discoverable, and reflected in the output.
Evaluated by: editor feature inventory, one row per control (present? discoverable?
reflected in render?). Tracked individually, not as one yes/no.

### B6 — Can users understand editing flows without support?
Answered well when: a non-technical user completes a basic edit (reorder + swap music
+ tweak text) unaided, no tutorial.
Evaluated by: task-completion in unguided user tests; support-ticket themes; dead-end telemetry.

## C. Audio   (→ discovery gap-finder)

### C7 — Is the UX intuitive for recording audio?
Answered well when: a user records a voiceover, knows it's recording, hears it back,
and re-records — no confusion about state.
Evaluated by: record/stop/playback/re-record flow inspection; record-step completion rate.

## D. Output Quality   (→ final-video grader)   [seeded — refine to taste]

These grade the rendered MP4, not the app. They become the grader's scored dimensions.

### D8 — Does the hook create a question in the first 2-3 seconds?
Answered well when: the opening makes a viewer want to keep watching (question,
tension, payoff-promise) — not a slow or generic intro.

### D9 — Is on-screen text legible and on-beat?
Answered well when: text is readable at a glance (size, contrast, not clipped) and
its timing lands with the cut/music, not floating arbitrarily.

### D10 — Does it look filmed, not templated?
Answered well when: the edit reads as authored short-form content, not a generic
slideshow — pacing, transitions, framing feel intentional.

### D11 — Is the music in sync with the cuts?
Answered well when: beats land on cuts/transitions; nothing drifts or sits arbitrarily.

### D12 — Is it free of slop / cringe?
Answered well when: no forced metaphors, cheesy hooks, or generic-motivational filler
(reuse the existing cringe rubric criteria from `/audit-plan-quality`).
