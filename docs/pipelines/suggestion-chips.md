
# Suggestion chips (KRI-756)

Footage-aware starter and follow-up chips for the Kria creation chat in the **iOS app only**
(web is out of scope and ignores every field below). Ships dark behind
`KRIA_SUGGESTION_CHIPS_ENABLED`.

**The generator decides which chips may exist. The agent only words them.**

1. A pure, free, LLM-free **candidate generator** (`build_candidates`) emits only chips that are
   *real* (the footage or edit exists), *enabled* (the manifest / editor `allowed_op_families` /
   client protocol say so), *executable* (a dry-run witness proved the change is not a no-op)
   and *not already in the edit*.
2. A small text-only Flash agent, `nova.chat.suggester`, runs in a worker **off the request
   path**. It can only return candidate ids it was given; it ranks them and rewrites their
   display `label` and one-line `why`. `kind`, `capability`, `message`, `target` and `anchor`
   are always copied from the candidate. A deterministic validator gates every label and `why`.
3. `GET /creation-threads/{id}/suggestions` **never calls a model**. It recomputes the
   candidates, joins the cached agent wording, falls back per chip to the template label, and
   freezes what it served.
4. The app renders server chips only when `capabilities.suggestion_chips` is true AND the GET
   returns `status: "ok"` with at least one chip; otherwise it keeps the old fixed ready-card
   chips. Flag off is byte-identical to legacy: no capability, GET answers `unavailable`, no
   agent runs, no payload keys.

Code: [`services/suggestion_chips.py`](../../src/apps/api/app/services/suggestion_chips.py)
(models, candidates, assembly, tap detection, ring, loader),
[`services/suggestion_chip_copy.py`](../../src/apps/api/app/services/suggestion_chip_copy.py)
(label and `why` validators),
[`services/suggestion_serve.py`](../../src/apps/api/app/services/suggestion_serve.py)
(the GET read path, enqueue helpers),
[`services/suggestion_store.py`](../../src/apps/api/app/services/suggestion_store.py) (Redis),
[`agents/chip_suggester.py`](../../src/apps/api/app/agents/chip_suggester.py) +
[`prompts/chip_suggester.txt`](../../src/apps/api/prompts/chip_suggester.txt),
[`tasks/kria_suggester.py`](../../src/apps/api/app/tasks/kria_suggester.py), and the route /
capability in [`routes/creation_threads.py`](../../src/apps/api/app/routes/creation_threads.py).
Eval, report and funnel tooling: [`suggested-chips`](suggested-chips.md).

## Wire contract (additive; old builds and web ignore it)

`CreationCapabilitiesOut.suggestion_chips: bool` (default false). The iOS contract is
`src/apps/ios/Kria/Generated/openapi.yaml` (`getCreationSuggestions`); regenerate with
`python -m app.cli.kria_contracts --write` (CI: `--check`). The browser TypeScript contract
deliberately omits the mobile-only turn field (`_WEB_HIDDEN_FIELDS` in `kria_contracts.py`), so
`src/apps/web` stays byte-identical.

`GET /creation-threads/{thread_id}/suggestions` (bearer, `X-Kria-Client-Protocol`,
`Accept-Language` until the thread has a language). No body. Idempotent. Appends no event and
never changes `thread.revision`. Foreign or missing thread: the usual 404. Flag off, cohort
miss, or any failure after the ownership check:
`200 {"status":"unavailable","state":"none","chips":[]}`.

```json
{
  "schema_version": 1,
  "set_id": "sg_<sha16(thread_id|input_digest)>",
  "state": "first_prompt | ready | none",
  "status": "ok | pending | unavailable",
  "understanding": "pending | done | empty",
  "retry_after_s": null,
  "wording": "agent | fallback | pending",
  "anchor": {"media_digest": "...", "manifest_hash": null, "render_generation_id": null, "variant_id": null},
  "chips": [{
    "id": "sc_<sha12(kind|target|base_digest)>",
    "kind": "default_edit | hook | captions | music | sfx | transition | title_animation | pinned_text | opener_clip",
    "label": "Let Kria cut the first version",
    "message": "Suggest an edit.",
    "rank": 1, "default_edit": true,
    "capability": "dispatch_render",
    "anchor": {"media_id": null, "clip_ordinal": null, "moment": null, "text_bar_index": null},
    "why": null, "label_source": "template", "requires_protocol": null
  }]
}
```

* Chips are sorted by `rank` (1..4, unique, contiguous). First prompt: max 4 (rank 1 is
  `default_edit`, then up to 3). Ready: max 4. At most one chip per kind, no duplicate message.
* `label` (<= 36 chars) is display only; **a tap sends `message`** (<= 120 chars, a closed
  server template). `why` (<= 48 chars) is a display-only subtitle with footage evidence, present
  only if it passed the lexical grounding check. Clients must render an unknown `kind` by label.
* `requires_protocol`: the app drops the chip locally when it is non-null and above the app's own
  protocol (currently 13). No emitted kind needs one today; `ChipKindSpec.min_protocol` is the
  hook, and the server also drops a chip when the client's `X-Kria-Client-Protocol` is below it.
  Text bend (protocol 9) and freehand drawing (6/7) have no chat op, so no kind carries them.
* **Chip 1** at `first_prompt` is always `default_edit`, `message` exactly `Suggest an edit.`
  (iOS `ChatSubmission.mediaOnlyMessage`). Its label is the template ("Let Kria cut the first
  version" / Turkish) unless the agent's `default_label` passed validation. The message is never
  agent text.
* `opener_clip` is never emitted while `CLIP_ORDINAL_PROVEN` is `False` (a replay cassette must
  first prove filename-to-hero resolution), and `pinned_text` is never emitted
  (`PINNED_TEXT_PROVEN`): both need creator words or an unproven resolver.

### States

| state | when | chips |
| --- | --- | --- |
| `first_prompt` | stage footage, >= 1 video clip, no `user_message`, no job, no open question | `default_edit` + levers once understanding is done |
| `ready` | a completed render with an editor target, no pending question, turn or render | up to 4 from the editor snapshot, static per `render_generation_id` |
| `none` | no media, photos or slides only, open `ChoiceQuestion` / clip question / song order / song range / creative-copy discussion, in-flight turn or render, no editor target | `[]` (`status: "ok"`) |

Serving rules (`serve_suggestion_set`):

* understanding pending: `status: "pending"`, `default_edit` only, `retry_after_s: 3`;
* understanding settled-empty (3 failed attempts): `status: "ok"`, `default_edit` only;
* understanding done, agent result not cached: `status: "pending"` for at most **6 s** after the
  digest was first seen (`default_edit` only at first_prompt, no chips at ready,
  `retry_after_s: 2`), then template labels (`wording: "fallback"`);
* a served `ok` set is **frozen** per input digest (Redis `served` key, first writer wins): its
  labels never change, and a new `set_id` appears only when the input digest changes. A pending
  set and its final set share a `set_id`, so a tap on the pending `default_edit` chip is
  attributed;
* `KRIA_SUGGESTER_MAX_RUNS_PER_THREAD=0`, an agent fallback marker (budget, run cap, schema
  error, timeout) or a Redis outage: template labels immediately, no wait.

`input_digest = sha(media_digest, manifest_hash, render_generation_id, variant_id, state,
language, candidate cid list, prompt_version)`.

## Kinds (`CHIP_KIND_GATE`, one row each; a guard test fails if a `ChipKind` lacks a row)

| kind | first_prompt | ready | witness | message |
| --- | --- | --- | --- | --- |
| `default_edit` | `dispatch_render` (+ `main_creator_agent` on v1) | n/a | policy only: it IS the open request | `Suggest an edit.` |
| `hook` | same gate; nothing in the edit yet | `title` family, no title bar (roles `title`/`intro`/`hook`/`generative_intro`, id `guided-title*` all count as a title) | strategy: `opening_title` survives `check_strategy_for_runtime_v2`; editor: `set_title` PROBE compiles | `Add a hook that matches the video` (routes into the creative-copy flow; carries no wording) |
| `captions` | a clip with speech or talking to camera; not on formats that always caption. The manifest `caption_style` capability is **not** consulted (it only mirrors `main_creator_agent_execution_enabled`) | `caption` family, captions off with cues | strategy `caption_style=clean` not coerced; editor `set_caption_meta {enabled}` | `Add captions` |
| `sfx` | `sound_effects` capability | `sfx` family, non-empty catalog, no placements | strategy `sfx` survives; editor `add_sfx` | `Add sound effects` |
| `transition` | `transitions` capability, 2+ clips | `transition` family, 2+ slots, all cuts | strategy `transitions` survives; editor `set_transition` | `Add transitions between the clips` |
| `music` | n/a | `music` family, a track, some clip has speech, level above 0.3 | editor `set_mix` | `Turn the music down` |
| `title_animation` | n/a | a title bar with no entrance animation, archetype `guided_story`/`unified_montage`, **`text` family** (the op is `patch_text`) | allow-list | `Animate the title` |
| `opener_clip` | dark | dark | editor `reorder_clip` | `Start with {filename}` |
| `pinned_text` | dark | dark | n/a | n/a |

Ranking is a priority table plus footage cues (speech and no captions first, no title next, 2+
clips lifts the opener, no speech lifts sfx/transition, talking to camera lifts `music`). Time
references (`{time}`) may only be an **output-timeline** slot boundary read from the editor
snapshot (`output_boundary_time`), never a source-clip time. A candidate that fails any step
never reaches the client.

### Editor-op decisions (the denylist)

`CHIP_OP_ALLOW` (`set_caption_meta`, `add_sfx`, `set_mix`, `set_transition`, `reorder_clip`,
`patch_text`) are the only ops a chip may claim. `CHIP_OP_DENY` holds every text-carrying op
(`add_text`, `rewrite_text`, `set_title`, `label_each_clip`, caption text edits ...), overlays,
`set_intro_layout`, `apply_speech_cut_candidate` (server-async), `swap_music` (`swappable` is
false) and camera / motion / custom effects. `CHIP_OP_WITNESS_PROBES` (`set_title`) may be used
by a dry run only. **`test_every_text_carrying_editor_op_has_an_explicit_chip_decision` fails
when a new text-carrying op enters the editor op vocabulary without an explicit decision.**

### Adding a kind

1. Add the `ChipKind`, copy in `_COPY` (en + tr), lexicon stems in
   `suggestion_chip_copy.LEXICON`, and a `ChipKindSpec` row in `CHIP_KIND_GATE` (`available`,
   `already_present`, `ground`, `dry_run` per state, `expects`, `claimed_op`, `witness_op`,
   `min_protocol`).
2. Decide the op in `CHIP_OP_ALLOW` / `CHIP_OP_DENY`; prove it with a strategy or editor witness
   (or an explicit allow-list entry and a reason).
3. Extend the replay eval ([`suggested-chips`](suggested-chips.md)) and this table.

## Wording agent (`nova.chat.suggester`)

AgentSpec: `prompt_id chip_suggester`, `prompt_version 2026-10-19.2` (`PROMPT_VERSION` in
`suggestion_chips.py`; bump it with the prompt), `gemini-2.5-flash`, `thinking_budget 128`,
`max_attempts 2`, `timeout_s 12`, `sensitive_io false`. Feature name for cost control:
`chip_suggester`.

* **Input** (`ChipSuggesterInput`): `state`, `language`, `max_chips`, `footage` (opaque handles
  `c1..cN`, moments `c2.m1`; `subject` <= 6 words, `setting`, `speech none|voiceover|to_camera`,
  moment `description` <= 60 chars), `already_in_edit` tags, `candidates` (`cid`, `kind`,
  `gap_tags`, `facts`, `label_slots` names, `fallback_label`), and `typed_request` (ready only,
  <= 200 chars). No media id, timestamp, transcript or creator-typed text at first_prompt.
* **Output**: `{default_label, ranked: [{cid, label_template, why}]}`; `parse()` drops unknown
  cids, duplicate kinds and over-limit items (<= 3 first_prompt, <= 4 ready).
* **Label rules** (`validate_label`, deterministic): <= 36 chars; no digits or quotes outside
  placeholders; only `{time}` / `{filename}` and only if listed in the candidate's `label_slots`;
  every word in the kind's per-language lexicon or the function-word list; at least one word that
  names the kind's action; no word that belongs only to a different kind (label parity); no
  negation or reverse-direction word (`polarity`: `up`/`loud` on the music chip, whose tap only
  lowers it; `hard`/`cut` on transitions; `no`, `not`, `without`, `off`, `remove`); stoplist
  and cringe stoplist; `en` / `tr` only (others fall back to `en`). Failure: the template label,
  `label_source: "template"`, drop reason `label_*`.
* **`why` rules** (`validate_why`): <= 48 chars, no digits / quotes / handles / links, stoplist,
  in the chat language, no capability word of another kind or an unoffered one (`capability`:
  a sfx chip's subtitle cannot say music or captions), and at least one folded content word
  (filler words like `with` do not count) that also appears in the footage analysis. The
  analyzer writes English, so a non-English `why` normally has no overlap and is dropped (the
  prompt tells the agent to use null instead of mixing languages). Failure: `why` is dropped. **Gemini-derived text (subject, moment, setting) appears
  only in `why`, never in `message` or any overlay.**
* Copy-bearing kinds (`hook`, `pinned_text`) are asks only and at most one per set.

## Triggers, storage and caps

* `tasks.kria_suggester` (queue `agent-control`, soft/hard limit 60/90 s, in the `worker.py`
  include list and `task_routes`; guard `tests/tasks/test_task_time_limits.py`). Runs under
  `RunContext(creator_id, request_id="kria-suggest:{thread}:{digest}",
  usage_purpose="optional_background")`; ready runs are wrapped in `pipeline_trace_for(job_id)`.
* Enqueue-only triggers, each re-checking the flag, the per-thread run cap and (in
  `ai_cost_control.py`) the per-user daily limit: (a) the end of
  `kria_clip_understanding._run` (first prompt), (b) a render settles ready
  (`_observe_dispatched_execution`, phone device and cloud), (c) lazily from the GET on a cache
  miss, single-flighted by a Redis lock.
* Redis (24 h, all fail-open, `services/suggestion_store.py`): `kria:suggester:{thread}:{digest}`
  agent result (or a fallback marker), plus `served`, `seen`, `lock`, `inflight`, `runs`, `hint`
  (the language and protocol the GET last saw, because the worker has no request headers).
  **Nothing is written to `thread.state` from the worker.** No migration.
* Settings that bound the agent (not flags): `KRIA_SUGGESTER_MAX_RUNS_PER_THREAD` (default 12;
  **0 disables the agent only**, chips fall back to templates: the cost kill lever) and
  `KRIA_SUGGESTER_DAILY_LIMIT_PER_USER` (default 20 paid model ATTEMPTS per creator per UTC day, not chip runs: a retry is a second
  reservation, so flaky runs reach it sooner; counted from `ai_cost_reservations` by the
  literal feature name `chip_suggester`, because a new feature name escapes the `edit_director`
  check). A budget, quota or cap stop gives a fallback marker, never a 500.

## Tap path and persistence

* `thread.state["suggestion_sets"]` is a ring of the last 5 offered sets
  (`{set_id, state, digest, media_digest, created_at, final, wording, prompt_version,
  chips:[{id, kind, rank, message, label_source, default_edit}]}`), written by the GET under
  `FOR UPDATE SKIP LOCKED` (never waits behind a turn). No event, no revision bump. Popped from
  the public thread state. Never `record_pipeline_event` here (the FOR UPDATE self-deadlock trap).
* The turn POST (`SubmitTurnBody`) gains one OPTIONAL field `suggestion: {set_id, chip_id}`. The
  server validates it against the ring (and the message must still equal that chip's message);
  absent or invalid it falls back to `match_offered_suggestion` (casefold + whitespace-collapse
  text match, newest set first, current media only). The stored `user_message` payload gets
  `payload.suggestion = {set_id, chip_id, kind, rank, state, label_source, prompt_version,
  default_edit}`. The field is outside `request_digest`, the planner ignores it, and the send path
  is otherwise untouched: a tap is an ordinary creator message.
* Flag off: `suggestion_tap_payload` returns `{}` before touching the database.

## Telemetry (structlog; no new tables; no `record_pipeline_event`, there is no job at first_prompt)

* `suggestion_chips.shown {thread_id, set_id, state, wording, kinds, n}` once per `set_id` when
  the final set is first recorded. It means **served**, not seen (no beacon yet).
* `suggestion_chips.agent_run {agent_run_id, thread_id, set_id, trigger, model, prompt_version,
  latency_ms, n_in, n_out, n_dropped, drop_reasons, cost_usd, fallback_reason?}` with
  `fallback_reason` in `schema_error | label_rejected | timeout | budget | stale_digest |
  flag_off | run_cap`.
* `suggestion_chips.tap` / `payload.suggestion` on the `user_message` event.
* **kept** is derived at query time: tapped AND the turn completed AND no `plan_section_undo` /
  `plan_undo_all` / `draft_undone` before the next `user_message`. Split by `label_source`.
* Errors: `suggestion_chips.error` (stage `candidates | build | route | tap | worker_load |
  worker_store`), `.kind_error`, `.witness_error`, `.persist_skipped`, `.enqueue_failed`.
  The witness tier (`strategy | editor | policy_only`) is on each candidate, not on the wire.

```sql
select payload->'suggestion'->>'state' as state, payload->'suggestion'->>'kind' as kind,
       payload->'suggestion'->>'label_source' as source, count(*)
from creation_thread_events
where event_type = 'user_message' and payload ? 'suggestion'
group by 1, 2, 3 order by 4 desc;
```

## Rollout and kill switch

Ship dark: `KRIA_SUGGESTION_CHIPS_ENABLED=false` (default) and `KRIA_SUGGESTION_CHIPS_USER_IDS=[]`
(creator UUIDs; the list only ever adds, so a single account can canary while the global flag is
off). Apply: `fly secrets set -a nova-video KRIA_SUGGESTION_CHIPS_ENABLED=true`, then restart api
and worker. There is no Vercel or `NEXT_PUBLIC_` twin: the app reads the server capability.
Deploy the API (Fly) first, then ship the app build. Kill switch: set the flag false + restart api
and worker (the capability flips, the GET answers `unavailable`, no agent runs, nothing is
stamped; the ring goes stale). Cost lever only: `KRIA_SUGGESTER_MAX_RUNS_PER_THREAD=0` + restart.
Close the `Rollout: KRIA_SUGGESTION_CHIPS_ENABLED` issue when the flag is on and verified on a
TestFlight build.

## Honest limitations

* Nothing here was measured live: no latency, cost or pass-rate numbers exist. Live Gemini and
  judge runs need an approved development-ledger run (hard cap $2, no judge).
* Chip 1 EXECUTES the default edit but does not DESCRIBE it. A previewed label needs a paid,
  speculative Main Creator call (a later, separately flagged option).
* The `ready` state needs an editor target. It is unverified whether phone-rendered or
  `IOS_DEVICE_ONLY_MODE` threads have one; if they do not, ready chips never appear for them and
  the app keeps its fixed chips (the safe failure).
* The wording worker has no request headers: language and protocol come from the thread or the
  `hint` the GET last stored. A digest mismatch (a different protocol that changes the candidate
  list) just costs one lazy run from the GET.
* `shown` means served, not seen; there is no `retry` / failed-render chip and no seen beacon yet.
* A typed message equal to a chip counts as a tap when no explicit `suggestion` id is sent.
* The label vocabulary is deliberately closed, so some agent rewordings fall back to the
  template; the eval lane measures the rate.
