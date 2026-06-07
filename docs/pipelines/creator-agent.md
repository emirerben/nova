# Creator Agent Architecture

Nova's long-term vision: a **personalized AI agent per creator** that knows their
style, plans their content, guides their filming, and renders every edit to their
taste — while letting them override anything they want.

## Why this document exists

Today the pieces are disconnected:

| Signal | Reaches |
|---|---|
| Persona (TikTok + interview) | Hook *wording* only |
| Typography / style | Per-render, from clip content, not the user |
| Filming guide | UI display only — never reaches the renderer |
| Persona edits | Nothing — no propagation |

A user with an aesthetic city-walk persona and a user uploading gym content get the
same style-set selection if their footage happens to look the same. The Creator Agent
architecture fixes this.

---

## Canonical state model

Three durable per-user rows drive everything:

```
personas
  ├── questionnaire     (user answers from the chat interview)
  ├── tiktok_profile    (scraped + LLM-enriched profile)
  ├── persona           (AI-authored: summary, pillars, tone, audience, ...)
  └── style             (M1) UserStyle JSONB — pinned set + knob overrides
                             + footage_type_bias + instruction_level + status

content_plans
  └── plan_items[]
        ├── theme / idea / filming_guide
        ├── edit_format
        └── current_job_id (→ jobs)
```

**Per-job snapshot:** at job mint time the caller copies the persona/style/plan
context into `Job.all_candidates` (existing pattern). The orchestrator reads
`all_candidates` during async render so it never races the canonical row. A persona
edit after the job is queued doesn't silently change the in-flight render.

**Intent-driven re-tune:** user action (chat / PATCH) → structured task → read-merge-
write the canonical row. The next job mint picks up the new values automatically.
This is the existing `retune_persona_from_feedback` + `PATCH /personas/{id}` pattern;
the M2 conversational agent emits intents onto these same tasks.

---

## Propagation model

```
User edits persona tone
    └─► retune_persona_from_feedback.delay()
            └─► PersonaGeneratorAgent
                    └─► row.persona updated (ready)
                            └─► derive_user_style.delay()  (M1)
                                    └─► StyleDerivationAgent
                                            └─► row.style updated (ready)

Next plan item renders:
    _dispatch_item_render → build_generative_job(user_style=row.style)
                                → all_candidates["user_style"] = validated style
                                → orchestrate_generative_job reads it
                                → _resolve_intro_overlay_params applies knobs
```

Changes propagate to **future edits only** — never retroactively to completed jobs.
This is by design: retroactive re-render would break delivered content.

---

## Invariants

**Byte-identity-when-absent:** when `USER_STYLE_ENABLED=false` OR `style IS NULL`,
`all_candidates` has no `user_style` key. `_resolve_intro_overlay_params` with
`user_style_knobs=None` produces byte-identical output to pre-M1.

**"User's say wins":** `style.status == "edited"` → `derive_user_style` skips the
row (both initial and post-retune chains). Only `POST /personas/style/rederive`
(explicit user request) can overwrite an edited style (`force=True`).

**Parity-safe knob set (#296):** `StyleKnobs` uses `extra="forbid"`. Every field in
`StyleKnobs` MUST be confirmed to work in BOTH the Pillow renderer (`text_overlay.py`)
and the Skia renderer (`text_overlay_skia.py`). `effect` is deliberately excluded
pending Skia parity verification. Guard: `tests/test_user_style_schema.py::TestStyleKnobaParitySafety`.

**Precedence chain (most-specific wins):**
- Style set: per-variant `dispatch_change_style` > user-style pinned id > agent-selected > "default"
- Size: per-variant `size_override_px` (source "user") > user-style `text_size_px` (source "user_style") > curated-set px > `compute_overlay_size` (source "computed")
- Other knobs: user-style knob > curated-set value > agent advisory > hardcoded default

**Per-variant knob persistence:** `user_style_knobs` is stored in the variant entry
dict on `Job.assembly_plan["variants"]` alongside `style_set_id`/`intro_text_size_px`.
Re-renders (`regenerate_generative_variant`) read it back from the variant entry,
not the current persona row — so re-renders are hermetic even if the user's style
changed between the first render and the swap-song/retext.

---

## Milestones

### M1 — User Style entity (this PR, dark behind `USER_STYLE_ENABLED=false`)

**What's shipped:**
- `personas.style` JSONB column (migration 0050)
- `StyleKnobs` + `UserStyle` schemas (`app/agents/_schemas/user_style.py`)
- `StyleDerivationAgent` (`nova.plan.style_derivation`) with prompt + eval rubric
- `derive_user_style` Celery task — chained from `generate_persona` + `retune_persona_from_feedback`
- Render wiring: `build_generative_job(user_style=...)` → `all_candidates["user_style"]`; `_resolve_intro_overlay_params(user_style_knobs=...)` applies knobs with correct precedence
- API: `GET /personas/style`, `PATCH /personas/style` (→ status="edited"), `POST /personas/style/rederive`
- Kill switch: `USER_STYLE_ENABLED=false` (default)

**Not yet wired:**
- Frontend StyleCard (M1 backend ships first; UI ships once kill switch is flipped)
- Planner does not yet consume `instruction_level` or `footage_type_bias` (M3)

### M2 — Conversational agent

A chat endpoint where the user says "change my main font to something bolder" or "I
don't want to film running anymore". The agent parses the intent → dispatches to
existing re-tune tasks or PATCH routes → the user sees the impact on next render.

Key interactions:
- Font/color/style change → `PATCH /personas/style` (→ status="edited")
- Persona preference → `PATCH /personas/{id}` → `retune_persona_from_feedback`
- "No instructions" → style.instruction_level="none" → planner consumes in M3
- Scope reduction (stop filming X) → `PATCH /content_plans/{id}` category edit (new)

### M3 — Style-driven plan + filming guide in render

- Planner reads `style.instruction_level` + `preferred_edit_format_mix` to determine
  video category weights and instruction verbosity
- `filming_guide` (already in `plan_items`) threaded into `build_generative_job` →
  `all_candidates["filming_guide"]` → `intro_writer` uses it as context for the hook
- Persona-driven archetype dispatch: `footage_type_bias` biases `_select_archetype`
  toward the user's preferred footage style

### M4 — Per-item single-video upload + conformance agent

- UI change: upload one video per instructed plan item (vs. current bulk upload)
- Conformance agent: compare uploaded footage against the item's `filming_guide`
  (clip_metadata signals) → emit "this looks like X instead of Y; engagement risk Z"
- Reuse `clip_metadata` + `clip_plan_matcher` signals; new `ConformanceFeedbackAgent`

### M5 — Freeform / off-plan uploads

- User uploads a video not tied to any plan item
- `detect_plan_relevance` agent: does this match an existing plan item? a new topic?
- If match: fulfil + close the item
- If new topic: propose a new plan category; user approves → add to plan
- Editing follows the user's style regardless

### M6 — `day_vlog` and `single_hero` assemblers

Full format support for the two planned-but-unimplemented edit formats in the
`edit_format` contract. Gated behind `EDIT_FORMAT_DAY_VLOG_ENABLED` /
`EDIT_FORMAT_SINGLE_HERO_ENABLED` kill switches (same pattern as talking_head).

---

## Enabling in production

```bash
# After live-eval validation of StyleDerivationAgent output quality:
fly secrets set USER_STYLE_ENABLED=true --app nova-video
fly machine restart <worker-machine-id>

# The next persona generation or retune will auto-derive styles.
# Monitor: fly logs --app nova-video | grep style_build
```

Backfill existing personas (optional, once enabled):

```python
# Admin script — queue derive_user_style for all personas with status="ready"
from app.models import Persona
from app.tasks.style_build import derive_user_style
# ... query ready personas, derive_user_style.delay(str(p.id)) for each
```

---

## Key files

| File | Role |
|---|---|
| `app/agents/_schemas/user_style.py` | `StyleKnobs` + `UserStyle` + coerce helpers |
| `app/agents/style_derivation.py` | `StyleDerivationAgent` |
| `app/prompts/derive_user_style.txt` | Agent prompt template |
| `app/tasks/style_build.py` | `derive_user_style` Celery task |
| `app/migrations/versions/0050_persona_style.py` | `personas.style` column |
| `app/routes/personas.py` | Style API routes (GET/PATCH/rederive) |
| `app/services/generative_jobs.py` | `_build_user_style_context`, `build_generative_job` |
| `app/tasks/generative_build.py` | `_resolve_intro_overlay_params` (single source of truth for knob precedence) |
| `tests/test_user_style_schema.py` | Parity-safe guard + byte-identity contract |
| `tests/evals/test_style_derivation_evals.py` | Style derivation eval harness |
| `tests/evals/rubrics/style_derivation.md` | LLM judge rubric |
