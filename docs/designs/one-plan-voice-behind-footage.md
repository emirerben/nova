# One plan, composable tracks: voice behind footage

Status: SLICE IMPLEMENTED (KRI-479, PR-H); the rest of the design is still a proposal. See "Implementation
status" at the end for what was built and where it deviates. (Originally a DRAFT for human review, docs only.)
Date: 2026-10-07. Base: `origin/main` 0f3cfe6e8. Builds on PR-A (#1446, branch `codex/kri-470-a-matrix`,
commit 2998f553e), which is not merged yet.

Reading guide. `app/` means `src/apps/api/app/`. Every claim about current behaviour carries a
`path:symbol` cite to code read for this doc. Markers:

- **[main]** exists on `origin/main` today.
- **[PR-A]** exists only on PR-A's branch (names used as-is).
- **[plan]** is promised by the KRI-470 train (PR-C, PR-D, PR-F, PR-G) but not written; names are the plan's.
- **[proposed]** is new in this doc; a reviewer can reject it.

## 1. Problem and goals

**Failure class.** KRI-469: the approved plan said "use the talk-to-camera clip as the voice, montage of
the rest, chronological". The speech-montage lane played a different clip's 1.64 s quote for a 7.64 s edit
(`tests/fixtures/incidents/kri469-voice-clip-ignored.json`, observation `incident`). One render path
re-decided what the plan had already decided. Every new request shape (voice + order + text + length) adds
another path that can do this.

**What goes wrong today, from the code, for exactly this request shape:**

| Fact [main] | Where | Consequence |
| --- | --- | --- |
| Only the speech adapter consumes the contract; unified/guided montage, voiceover, subtitled, narrated do not (PR-A makes this explicit as `ADAPTER_DECLARATIONS`). | `app/services/phone_speech_montage_job.py:run_phone_speech_montage_job`; `app/tasks/generative_build.py` dispatcher (~2297-2404) | A requirement outside the speech lane is only caught at pin time. |
| The speech compiler emits no text. | `app/pipeline/phone_speech_montage_plan.py:compile_phone_speech_montage_plan` (`text_layers=[]`) | An approved opening text fails `verify_phone_recipe` (`exact_texts`, "missing confirmed on-screen text"). |
| Picture comes from a round-robin pool that wraps. | `phone_speech_montage_plan.py:_BrollPool.take` | Chronological order passes `verify_phone_recipe` only if every clip is used once, in order; a wrap produces `[a,b,c,a,...]` and fails. |
| Which part of the voice plays is chosen by an LLM that returns excerpts of at most 25 s. | `app/services/speech_montage_planning.py:plan_speech_montage`, `app/schemas/speech_montage.py:MAX_EXCERPT_S` | A continuous 30 s voice cannot be expressed, and the length can drift (the planner only appends a "came out ~Ns" adjustment, `_SHORT_RENDER_RATIO`). |
| The voice clip is in the contract's order set, and the job refuses that. | `app/services/creator_render_contract.py:build_render_contract` (rows = all `clip_assignments` when `selected_media_ids` is empty); `phone_speech_montage_job.py` ~267-273 ("I can't prove the confirmed picture order for this audio edit"); `docs/pipelines/creator-render-contract.md` "Verification scope" | A request of KRI-469's shape (voice clip not excluded from the picture order) is declined today on the contracted path. The prod job itself predates the contract (see the record's notes). |
| A 30-clip, 15 s ask is discovered only after planning. | `app/pipeline/unified_montage.py:_shrink` floors unlabelled cuts at `_MIN_UNLABELLED_FRAMES` (24 frames = 0.8 s), then stops | 30 x 0.8 = 24 s against a 15 s ask; `verify_phone_recipe` (10 % tolerance) fails afterwards. Nobody is asked. |

**Goals.**

1. One approved plan (strategy + brief + contract) is the only source of decisions. A render path composes it; it never re-reads the request.
2. A request is a composition of four tracks (voice, visual sequence, ordering, text) plus duration and mix policy, expressed in the existing `EditRecipeV2`.
3. Conflicts between creator statements are asked once, in one place, before approval; answers persist and are reused.
4. What cannot be built declines visibly with a typed reason and a supported alternative.
5. Output is proven from the compiled recipe (verifier) and from a real export (proof plan), not from the planner's intent.

**Non-goals.**

- No new timeline model, no new renderer, no cloud implementation (cloud has no way to put clip A's voice over clip B's picture; `phone_speech_montage_job.py` docstring "Cloud fallback: there is none by design").
- Not the full route resolver, clarification gate or honest-verdict work: those are PR-D, PR-C, PR-F and PR-G. This doc defines what the slice needs from them.
- No change to unstamped (legacy) jobs. No change to the editor.
- No audio-quality judgement (loudness taste, voice naturalness): human listening only (section 7).

## 2. Core model

An approved plan composes **four tracks plus duration and mix policy**. Each track has one owner, reads
named contract fields, writes named `EditRecipeV2` elements, and has a verifier. `EditRecipeV2` is
`app/kria/recipes_v2.py:EditRecipeV2` (over `EditRecipeV1` in `app/kria/recipes.py`): `tracks[]` of kind
`video | overlay | audio`, each with `TimelineClip`s (`source_asset_id`, `source_start`, `source_duration`,
`timeline_start`, per-clip `volume`, `audio_fade_in/out`), an `AudioMixRecipe`, and `text_layers`. Voice
and picture are already separate tracks (the speech compiler uses a `video` track and an `audio` track).

| Track | Owner (proposed) | Contract inputs | Recipe output | Verified by |
| --- | --- | --- | --- | --- |
| **Voice** | `compose_voice` inside the new composer (section 6) | `audio_source_ids`, `original_audio`, `require_voiceover`, [proposed] `composition.voice_span_s`, `composition.voice_picture` | `TimelineTrack(kind="audio")` with one `TimelineClip` whose `source_asset_id` is the voice clip's `OriginalRenderAsset`, `volume=1`, fades | `verify_phone_recipe`: voice audible (`audible()`), `original_ids == set(audio_source_ids)`, `original_audio` forbid/require. **Missing:** `voice_covers_timeline`, `voice_window_contiguous` |
| **Visual sequence** | `compose_picture` | `order_ids` (already resolved), `duration_s`, [proposed] `composition.min_shot_s` | `TimelineTrack(kind="video")`, one clip per `order_ids` entry, `volume=0` | `verify_phone_recipe`: picture sequence == `order_ids` (collapsing consecutive repeats). **Missing:** `picture_shot_floor`; `picture_coverage` when order is not required |
| **Ordering** | Contract builder + collector (not the composer) | `order_required`, `order_basis`, `order_ids`, `unresolved` | none. The composer receives `order_ids` and never sorts | Same `order_ids` check. `unresolved` makes `verify_phone_recipe` raise (`needs_choice` [PR-A]) |
| **Text** | `compose_text` | `exact_texts` (`role`, `text`, `duration_s`) | `text_layers[]` via the narrated/talking title helpers (`app/pipeline/phone_narrated_plan.py:narrated_title_element`, `_compile_title_layers`; same shape as `phone_subtitled_title.py:talking_title_element`) | `verify_phone_recipe`: text matches, opening `start <= 1 frame`, hold `>= duration_s`, closing ends at duration, clip placement |
| **Duration** | `compose_picture` (sum of shots) | `duration_s` | `recipe.duration` = max end over all tracks (`EditRecipeV1.duration`) | `verify_phone_recipe`: within 10 % of `duration_s`. **Gap:** voice or text running past the picture would silently stretch `recipe.duration` |
| **Mix policy** | composer constants | `original_audio`, `require_voiceover`, (`user_song` conflicts) | picture clips `volume=0.0`; no `mute_windows`; no `music_asset_id` unless approved; `target_lufs` set | `audible()`; **Missing (PR-G, plan):** an unrequested music bed counts as audible |

**Mute policy is per-clip `volume=0`, not `mute_windows`.** `app/services/phone_rollout.py:validate_phone_pilot_recipe`
rejects any recipe with `audio.mute_windows` ("Muted sections aren't supported on iPhone yet"), and
`AudioMixRecipe.original_volume` only scales `video`-kind tracks (`src/apps/ios/Packages/KriaMediaEngine/Sources/KriaMediaEngine/Composition.swift:270`,
`gain: recipeTrack.kind == .video ? recipe.audio.originalVolume : 1`). The speech compiler already does this
(`add_main(..., volume=0.0)`), so the slice reuses it.

### Plan commitments [proposed]

The commitments live in a **sibling plain-dict key**, not in the strict contract model. The contract model is
`extra="forbid"`: any new field, even one that is skipped while unset, makes a stamped job unreadable by an older
worker during a rolling deploy and for every job stamped since a rollback (reproduced for PR-D's route; the same
reason `cloud_evidence` is a sibling key beside the guided receipt, [PR-E]). So:

```text
assembly_plan["creator_composition"] = {         # written and read only by new code
  "contract_digest":  str,                       # the contract it was resolved against; a mismatch means stale/absent
  "route":            str | None,                # shares PR-D's `assembly_plan["creator_route"]` (one source of truth)
  "voice_picture":    "hidden" | None,           # the voice clip's own picture is not in the picture sequence
  "voice_span_s":     float | None,              # seconds of voice the plan commits to (min(usable voice, duration))
  "min_shot_s":       float | None,              # readable-shot floor in force (None = policy constant)
}
```

These are commitments the verifier checks, not a timeline. `voice_picture="hidden"` is what lets
`build_render_contract` leave `audio_source_ids` out of `order_ids` (otherwise the speech job's refusal above
stands, and that case still matters for sources that are both heard and shown): `build_render_contract` takes the
commitments as an argument, so the contract model itself stays unchanged and `order_ids` is derived without a new
contract field. Each new strategy path gets a `FIELD_MATRIX` row [PR-A] (`supported`, owner
`render_contract:composition`); the PR-A guard `test_every_strategy_path_is_assigned` forces it. Wherever this
document writes `composition.<x>` it means `creator_composition["<x>"]`.

### Field to element mapping for the slice

| Contract field | Recipe element | Detail |
| --- | --- | --- |
| `audio_source_ids = (V,)`, `original_audio = "require"` | `tracks["voice"]` (audio) clip `voice-0`; manifest `OriginalRenderAsset(media_id=V)` | `source_start = s0`, `source_duration = w`, `timeline_start = 0`, `volume = 1`, `audio_fade_in = EXCERPT_FADE_IN_S` (0.05), `audio_fade_out = min(EXCERPT_FADE_OUT_S, ...)` |
| `order_ids = (c1..cN)`, `order_required = true` | `tracks["footage"]` (video) clips `clip-0..N-1`, in order | `volume = 0`, `rate = 1`, `timeline_start` cumulative, each shot `>= min_shot_s` |
| `duration_s = D` | sum of shot durations = D (frame aligned, 30 fps) | voice length `w <= D - EXPORT_SAFETY_MARGIN_S`, so the audio clip never extends `recipe.duration` |
| `exact_texts = [opening("T", duration_s=h)]` | `text_layers[0]` | `start = 0`, `end = min(h, D - margin)`, role `generative_intro`, effect `fade-in`; capabilities `positionedText`, `animatedText` |
| `composition.voice_picture = "hidden"` | voice clip appears on no `video` track | its picture is excluded from `order_ids` (verifier's picture check proves it) |
| `composition.voice_span_s = w_target` | voice clip `source_duration` within one frame of `w_target` | new check `voice_covers_timeline` |
| `composition.min_shot_s` | every `footage` clip `source_duration/rate >= min_shot_s` (or the whole clip if shorter) | new check `picture_shot_floor` |
| `require_voiceover = false`, `user_song` absent | no `music_asset_id`, no `narration_asset_id` | a soundtrack mix is `requirement_conflict` (below) |

Capabilities required: `basicComposition`, `local1080Export`, `audioMix`, `positionedText`, `animatedText`
(same set the speech compiler and narrated title already need; gated by `settings.phone_render_verified_features`,
`app/services/phone_rollout.py:phone_narrated_title_supported`).

## 3. Conflict ownership

**One place.** `collect_conflicts(strategy, brief, media_snapshot, capability)` in
`app/services/choice_questions.py` [plan, PR-C], returning typed `UnresolvedChoice` items (`kind`,
`field_path`, `requirement_ids`, `options`, `input_digest`). It runs inside `plan_live_turn` after the media
snapshot is attached (`app/kria/planner.py:plan_live_turn`, `media_snapshot=after`), on a dry run of
`build_render_contract`, so a missing-dates question is never invented from a half-built snapshot.
Components (composer, adapters, verifier) never ask and never read the request text. They either compose
what the contract says or raise a typed decline.

What exists to build on [main]: `ConflictCandidate`/`ConflictOption`, `build_choice_question`,
`fold_choice_answers` (replays `conflict_id -> option_key` from thread events; last answer wins),
`latest_open_choice_question`, `detect_order_vs_group` (all `app/services/choice_questions.py`); the
server-owned answer pattern `ordering_choice` (`app/agents/_schemas/creator_agent.py:CreativeStrategy.ordering_choice`,
set only via `planner.py:adapt_creator_action`); `kria_choice_questions_enabled` default true (`app/config.py`).
Gaps the plan already assigns to PR-C: no `input_digest` on answers, `AskUser.options` is dropped
(`adapt_creator_action`), and free text is not mapped to an option.

**Order of questions.** One question per turn, fixed priority, because an answer changes later detectors'
inputs: `order_basis` -> `which_voice` -> `voice_mode` -> `duration_vs_count` -> `voice_vs_duration`.

### Conflict catalogue

`L` = usable voice seconds, `D` = approved duration, `N` = picture clips, `floor` = `MIN_READABLE_SHOT_S`
[proposed constant, 0.8 s, promoted from `unified_montage._MIN_UNLABELLED_FRAMES`; see the arithmetic below].

| Kind | Detector inputs | Question and supported options | Persisted answer (server-owned) |
| --- | --- | --- | --- |
| `voice_vs_duration` (voice too short) | explicit `duration_s = D`; voice duration from `phone_sources` binding; `L < D - 1 s` | "Your voice clip runs 20 s; you asked for 30 s." (a) End the edit when your voice ends (20 s) [recommended]; (b) Keep 30 s, the last 10 s play without voice | `choice_answers.voice_fit` in `{match_voice, silent_tail}`; (a) also rewrites `target_duration_s=L`, `target_duration_requested=true` (existing server-owned provenance, `creator_agent.py`); (b) sets `composition.voice_span_s = L` |
| `voice_vs_duration` (voice long, duration implicit) | `target_duration_requested` not true; `L > 60 s` (short-form ceiling) | "How long should this be?" (a) 30 s; (b) 60 s; free text maps to a number | `target_duration_s` + `target_duration_requested=true` |
| voice long, duration explicit | `L > D` | **No question.** Voice window = first sentence start to the last sentence end `<= D` (word-aligned fallback), disclosed on the plan card | `composition.voice_span_s` (disclosed, not asked; see Q3) |
| `duration_vs_count` | `order_required or media_scope == "all"` and `N x floor > D` (no creator `montage_cadence`) | "30 clips can't read in 15 s." (a) Extend to 24 s so every clip gets 0.8 s [recommended]; (b) Keep 15 s and use 18 evenly spaced clips. Not offered: 0.5 s flash cuts (below the floor) | `choice_answers.duration_fit` in `{extend, fewer}`; (a) rewrites explicit duration, (b) rewrites `selected_media_ids` (existing field) |
| `order_basis` (chronology without reliable dates) | `order_required` and some selected clip has no capture facts (`app/services/clip_facts.py:capture_from_assignment` returns none), i.e. today's `unresolved` message in `build_render_contract` | "Some clips have no capture time." (a) Use the order you attached them [recommended]; (b) Use the order I give you (the tray order, `all_candidates["creator_clip_order"]`) | `choice_answers.order_basis` in `{attachment_order, creator_sequence}`; the builder passes the chosen order as `clip_order` (`order_basis="confirmed"`, existing parameter) |
| `which_voice` (several candidates) | `len(montage_audio.source_media_ids) > 1`, or the plan names none and 2+ clips are `speech.to_camera` | "Which recording is the voice?" one option per candidate (duration + first words, no private transcript beyond a short quote) | `choice_answers.voice_choice` = media id; rewrites `montage_audio.source_media_ids` to one id |
| `voice_mode` (continuous vs excerpts) | `voice_mode` unset and both the continuous composer and the excerpt adapter can execute (route resolver [plan, PR-D]) | "Play your voice straight through, or use chosen lines?" (a) Straight through; (b) Chosen lines over the footage | `choice_answers.voice_mode`; normally set by the Creator agent in the plan, so the question is rare (Q4) |

Soundtrack collisions (voice with a creator song or a recorded voiceover) are not asked here: they are
`requirement_conflict` declines today (`check_phone_dispatch_contract` [PR-A], "Choose one soundtrack"), and
PR-F turns them into a `which soundtrack` question at route time. Contradictory durations stay a build error
(`build_render_contract`: "Your confirmed edit lengths conflict").

**Readable-shot arithmetic (30 clips in 15 s).**

- Average shot = 15 / 30 = 0.5 s, below the 0.8 s floor.
- Capacity at the floor = floor(15 / 0.8) = 18 clips (18 x 0.8 = 14.4 s; 19 x 0.8 = 15.2 s).
- Minimum length for all 30 = 30 x 0.8 = 24.0 s.
- The verifier's 10 % tolerance (13.5-16.5 s) is a rounding allowance, not pacing budget; at 16.5 s it would fit only 20 clips. The detector uses exact `D`.
- Hard schema floor is 0.4 s (`MIN_VIDEO_CUT_S`, `app/pipeline/unified_montage.py`; `MontageCadenceConstraint.cut_duration_s >= 0.4`, `app/schemas/edit_proposal.py`): 30 x 0.4 = 12 s fits, but that is flash cutting. It is allowed only when the creator authored a cadence, which sets `composition.min_shot_s`.
- Control: 30 clips in 60 s = 2.0 s per shot, above `DEFAULT_CUT_S` (1.2 s); no question (corpus: `clarify-thirty-clips-in-sixty-seconds-asks-nothing.json`).

**Persistence, scoping, reuse.** Each answer is stored as `{option, input_digest}` under a server-owned
`choice_answers` object [proposed; one nested model beats one top-level field per conflict, and
`schema_field_paths` [PR-A] already walks nested models]. `input_digest` = hash of the facts that detector
read (voice id + duration in ms, `D`, sorted clip ids and durations). The answer counts only while the
collector's freshly computed digest equals the stored one; a changed media set reopens only that kind. Replay
is by thread events (`fold_choice_answers`), so:

| Situation | Behaviour |
| --- | --- |
| Retry of a failed render | Contract is immutable and stored (`creator_render_requirements`, `docs/pipelines/creator-render-contract.md`: never rebuilt from mutable chat). The collector does not run. |
| Re-sent identical prompt, new turn | Planner re-derives the strategy; `fold_choice_answers` re-applies the answer if the digest still matches. No re-ask. |
| Rephrased prompt that changes a requirement (new duration) | Digests change; only affected kinds reopen. |
| Editor/chat edit | Variant-specific contract revision (`CreatorRenderContract.rebind`); the collector runs only if the edit changes a detector input. |
| Free-text reply to an open question | PR-C step in `kria/runtime.py:submit_turn` maps an exact option label to `choice_selection`; anything else goes to the Creator agent with the question; at most one re-ask. |

**What questions cannot do.** An answer picks among options the composer can execute; it cannot make an
unsupported capability exist. Options are generated only from supported behaviours (the table above lists
the full set). A combination with no supported option declines with a typed reason (`DeclineReason` [PR-A]):

- `capability_unavailable`: no path can do it (cloud voice-over-other-footage; two simultaneous voices; shown-speaker-then-cut-away in the slice). Copy names the limit and the supported alternative (`_capability_refusal_copy` [PR-A]).
- `evidence_missing`: supported, but the output did not show it; repair or retry, never a question.
- `requirement_conflict` / `needs_choice`: a question (this section), or a visible refusal where no option exists.

## 4. Flow

```text
Creator      Planner (plan_live_turn)   Collector            Approval / runtime        Worker             Composer      Verifier       Device
  |  request ->  |                          |                      |                      |                  |             |              |
  |              | strategy + brief; attach media_snapshot         |                      |                  |             |              |
  |              |--- dry-run contract ---->| collect_conflicts()  |                      |                  |             |              |
  | <- choice_question (one) <--------------|                      |                      |                  |             |              |
  |  choice_selection ->  re-plan: fold answer into strategy.choice_answers (digest-scoped); loop until none  |             |              |
  |              | draft approvable: plan card lists disclosed decisions                   |                  |             |              |
  |  approve ----------------------------------------------------->| build_render_contract (+composition, route stamp)         |              |
  |              |                          |                      |--- dispatch ------->| read stamp, route |             |              |
  |              |                          |                      |                      |-- compose ------->| recipe      |              |
  |              |                          |                      |                      |<----------------------- verify_phone_recipe ----->|
  |              |                          |                      |                      | pin (device request) ---------------------------->| export
  | <- delivered (last good artifact replaced only after evidence) <-------------------------------------------| device evidence
  |  technical failure: Worker retries same contract, same route (no question)                                |
  |  new decision needed: ask; previous artifact stays live; answer creates a plan revision                   |
```

| State | Entered when | Creator sees | Leaves to | Last good artifact |
| --- | --- | --- | --- | --- |
| DRAFTING | message received | typing | ASKING or APPROVABLE | n/a |
| ASKING | collector returns an item | one question, concrete options | DRAFTING (answer) | unchanged |
| APPROVABLE | collector empty | plan card incl. disclosed decisions (voice window, trimmed length) | APPROVED | unchanged |
| APPROVED | approval consumed | "making it" | COMPOSING | unchanged; contract + route stamp immutable |
| COMPOSING | worker reads stamp | progress | VERIFYING, REPAIRING | unchanged |
| REPAIRING | transient/technical error (transcription, source fetch, worker restart) | nothing, or "retrying" | COMPOSING, DECLINED | unchanged. Same contract, same route, no question. Bound: one automatic recompose [proposed], then a typed `evidence_missing` decline with a Retry |
| DECLINED | typed `capability_unavailable` / unresolvable `requirement_conflict` | limit + alternative | DRAFTING (creator changes request) | unchanged |
| NEEDS_DECISION | verification reveals a new decision no earlier detector could see (e.g. footage shorter than `D`) | one question | DRAFTING, which mints a plan revision | **stays live** (plan PR-G) |
| DELIVERED | verifier passed, device evidence recorded, publication guard passed | the video | EDITING | replaced |

Today's code covers APPROVED -> COMPOSING -> VERIFYING -> pin for the phone path, and
`_observe_dispatched_execution` already separates deterministic declines from retry
(`app/tasks/kria_runtime.py`, `_DETERMINISTIC_JOB_FAILURE_CODES`); PR-A adds the typed mapping
(`_typed_creator_decline`). ASKING/APPROVABLE gate, route stamp and last-good guarantees are plan items.

## 5. Support matrix

Verdict legend: S = supported, A = asks (collector), D(reason) = declines with typed reason.

| # | Voice | Picture | Text | Duration | Today [main] (read from code) | After this slice |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | talk-to-camera clip, continuous | chronological | exact opening | explicit | D(`evidence_missing`): compiler has no text; voice clip in order set is refused | **S** (PR-H) |
| 2 | talk-to-camera clip, continuous | chronological | none | explicit | Partly: LLM excerpts <= 25 s, wrapping pool, length can drift; verifier may refuse | **S** (same composer, text optional) |
| 3 | same as 1, voice shorter than D | chronological | opening | explicit | not detected; the planner only appends a shortfall note (`_SHORT_RENDER_RATIO`), verify may fail on length | **A** `voice_vs_duration` |
| 4 | same as 1, 30 clips, D = 15 s | chronological | opening | explicit | planned to 24 s then fails verify | **A** `duration_vs_count` |
| 5 | same as 1, some clips undated | chronological | opening | explicit | `build_render_contract` marks `unresolved`; dispatch/verify refuse with `needs_choice` [PR-A], no question yet | **A** `order_basis` |
| 6 | same as 1, implicit duration, voice > 60 s (KRI-469 as stated) | chronological | any | implicit | strategy picks 30 s, voice refused or wrong clip | **A** "how long" (collector); then row 1 |
| 7 | two candidate voices | chronological | any | explicit | LLM picks the speaker | **A** `which_voice` (PR-C/F) |
| 8 | talk-to-camera, chosen lines (excerpts) | alternating with montage | exact opening | explicit | speech adapter works, text D(`evidence_missing`) | unchanged; text lane is slice+1 (same title helper) |
| 9 | talk-to-camera, shown first then cut away | speaker then footage | any | explicit | only via the LLM excerpt planner (`visual="speaker"` then cutaways); the seam between a video-track clip's audio and an audio-track clip has no continuity test | D(`capability_unavailable`), alternative "voice only over footage" (Q1) |
| 10 | recorded voiceover (plan item) | chronological | exact opening | explicit | `phone_voiceover_montage` consumes `require_voiceover`; order/text only caught at pin | unchanged; composer reuse with `VoiceoverRenderAsset` is slice+2 |
| 11 | source audio of shown clips | any | any | any | unified montage D(`capability_unavailable`) [PR-A declaration] | unchanged |
| 12 | creator song (background) | chronological | opening | explicit | unified planner + song (KRI-374) + pin verifier, not contract-routed | unchanged |
| 13 | voice clip + creator song, or + voiceover | any | any | any | D(`requirement_conflict`) [PR-A `check_phone_dispatch_contract`] | unchanged (PR-F turns it into a question) |
| 14 | any voice-over-other-footage on cloud | any | any | any | D(`capability_unavailable`) (cloud has no source-audio evidence; `CLOUD_UNEVIDENCED` [PR-A]) | unchanged |
| 15 | voice + user-chosen order | creator sequence | any | explicit | contract supports `order_basis="confirmed"`; speech job refuses voice-in-order | **S** once `order_basis` answer is wired (composer already takes any `order_ids`) |

New in PR-H: rows 1, 2 and 15's composition. Rows 3-7 are PR-C/PR-F questions that PR-H only consumes. The rest are unchanged and listed so the slice claims nothing it does not do.

## 6. The vertical slice (what PR-H builds)

**Request.** "Use the voice from my talk-to-camera video over the rest of my clips in the order I filmed
them, 30 seconds, with the title 'T' at the start." Strategy (approved): `audio_strategy="original_audio"`,
`montage_audio={preserve_source_audio:true, source_media_ids:[V]}`, `ordering_choice="chronological"`,
`target_duration_s=30` with `target_duration_requested=true`, `opening_title="T"`,
`opening_title_duration_s=3` (optional), `render_program="guided"`.

**Inputs.** Voice clip `V` (>= 30 s, has speech, has audio). `N` other clips with capture times (e.g. 12,
mixed durations). Contract (built by `build_render_contract` + proposed `composition`): `duration_s=30`,
`audio_source_ids=(V,)`, `original_audio="require"`, `order_required=true`, `order_ids` = capture-time sort of
the N clips excluding `V`, `exact_texts=[opening "T" 3 s]`, `composition = {route: "voice_behind_footage",
voice_picture: "hidden", voice_span_s: w, min_shot_s: None}`.

**Composer** [proposed]: `compile_phone_voice_behind_footage_plan(contract, bindings, voice_words)` in
`app/pipeline/phone_speech_montage_plan.py` (sits next to `compile_phone_speech_montage_plan` and reuses its
`_Assets`, fade constants and `EXPORT_SAFETY_MARGIN_S`; Q5 weighs a sibling module). Pure: signature has no
request text and no settings. Returns `(EditRecipeV2, receipt)`.

1. Voice window: from the first spoken word minus `EXCERPT_LEAD_S` to the last sentence end `<= D - EXPORT_SAFETY_MARGIN_S` (`app/services/speech_segments.py:words_to_segments`, `ground_excerpt`), so `w <= D`. If the gap to `D` exceeds 3 s, cut at the last word boundary instead, with an `audio_fade` out. If no transcript is available, cut at `D` with the 0.5 s fade and record the adjustment (a missing transcript is a technical retry, never a question).
2. Shots: `D / N` frame-aligned (30 fps) per clip, never below `max(min_shot_s or 0.8, 0.4)` unless the clip is shorter (shown whole); remainder redistributed round-robin capped by each clip's usable seconds (same idea as `unified_montage._grow`). If total usable footage `< D` the plan is `NEEDS_DECISION` (offer: shorten to the footage), not a silent loop. No reuse (order requires each clip once).
3. Text: `narrated_title_element(opening_title, end_s=h, ...)` compiled to a layer with `start=0`.

**Recipe.**

```text
canvas 1080x1920 @30fps
tracks
  footage (video):  clip-0..clip-11   source = OriginalRenderAsset(c_i)  volume 0   in order_ids order
  voice   (audio):  voice-0           source = OriginalRenderAsset(V)    start 0 .. w   volume 1   fade 0.05 / <=0.18
audio    original_volume 1 (video clips are 0 anyway), no mute_windows, no music_asset_id, target_lufs set
text_layers  [ "T": 0 .. 3 s, fade-in ]
required_capabilities  basicComposition local1080Export audioMix positionedText animatedText
```

`V`'s picture appears on no track (`voice_picture="hidden"`). Other clips' own sound is silent by `volume=0`.

**Acceptance assertions** (all on the recipe; export-level ones in section 7):

1. `verify_phone_recipe(contract, recipe, source_audio=...)` passes with no changes to existing checks.
2. `original_ids == {V}` and no other `OriginalRenderAsset` is audible (the check that would have caught KRI-469).
3. `[clip order on footage] == order_ids` and `V` not in it.
4. `|recipe.duration - 30| <= 1 frame` (composer-route tolerance; Q7) and `voice end <= recipe.duration`.
5. Text layer: `start <= 1 frame`, `end - start + frame >= 3`.
6. [new checks] `voice_covers_timeline` (voice `[0, w]` covers `voice_span_s - 1 frame`), `picture_shot_floor`, no `music_asset_id`/`narration_asset_id`.
7. `validate_phone_pilot_recipe(recipe)` passes (no `mute_windows`, verified features present).
8. Raw-text guard: composer and the stamped route read no `creator_request`, `_first_user_message`, `speech_montage_possible`, `mentions_speech` (PR-F allow-list test; PR-H adds the slice's modules to its asserted-clean list).

**Control (a): a real conflict resolved by an answer.** Same request with `V` = 20 s and `D = 30`. Expected: the
collector returns one `voice_vs_duration` question with options `match_voice` / `silent_tail`; answering
`match_voice` rewrites the strategy to an explicit 20 s; the stored contract carries `duration_s=20`,
`voice_span_s=20`; composer output has `recipe.duration = 20`; a retried render and a re-sent prompt do not
re-ask; swapping `V` for a 40 s clip reopens only this question's digest (and, being long, produces no
question). Variant: the 30-clips-in-15 s record (`clarify-thirty-clips-in-fifteen-seconds`) asks
`duration_vs_count`, and answering `extend` yields 24 s with 0.8 s shots.

**Control (b): a clear request that asks nothing.** `V` = 147.7 s (KRI-469's clip), `D = 30` explicit, 12
dated clips. Expected: zero questions, voice window = first sentences up to 30 s (disclosed on the plan
card), recipe as above. Also: all 34 `tests/fixtures/request_following/threads/*.json` produce zero
questions (plan acceptance, PR-C).

**Where the slice stops.** No editor recompile, no cloud, no excerpts + text, no shown-speaker opening
(Q1, Q8). It exercises route-independent conflicts only; `which_voice` and `voice_mode` ship as detectors with
unit tests but are not part of the slice's acceptance.

## 7. Proof plan (existing assets)

**Pattern to follow.** `scripts/ios/phone-audio-parity.py` already does "real Python compilers -> recipe.json
+ assets.json -> Swift export -> measure with ffmpeg": `prepare` writes `cases/<name>/{recipe.json,assets.json}`;
`KriaMediaEngine`'s `AudioParityFixtureTests.testRendersEveryPreparedCase` (env `KRIA_AUDIO_PARITY_DIR`) renders
every prepared case through `AVFoundationLocalExporter` and writes `phone.mp4`; `compare` measures with
`ebur128`. The slice adds a sibling script and no new Swift test [proposed]:

```bash
# 1. build inputs (ffmpeg lavfi: voice clip = tone F_v + speech-like pulses, each picture clip a unique colour
#    and a unique tone F_i, so a leaked camera audio or a wrong order is measurable), compile with the REAL composer
src/apps/api/.venv/bin/python scripts/ios/phone-voice-behind-footage-proof.py prepare OUT      # proposed
# 2. render on macOS through the production exporter
(cd src/apps/ios/Packages/KriaMediaEngine && KRIA_AUDIO_PARITY_DIR=OUT swift test --filter AudioParityFixtureTests)
# 3. measure the exported mp4
src/apps/api/.venv/bin/python scripts/ios/phone-voice-behind-footage-proof.py verify OUT       # proposed
```

Tone/colour-tagged inputs are the same idea as `tests/incidents/synthetic.py:ffmpeg_argv`
(`SyntheticClip.role/color/tone_hz`); the script can import it or copy it.

| Check (automatic, Mac export) | How | Catches |
| --- | --- | --- |
| Duration | `ffprobe` format duration vs `D` | wrong length (KRI-469: 7.6 vs 30) |
| Voice present throughout | `ebur128` per 1 s window plus energy at `F_v` (Goertzel) above threshold for `[0, w]` | wrong or missing voice, dropouts |
| No camera audio leak | energy at each `F_i` near zero in every window | unmuted picture clips; a doubled voice (also run with `F_v` on the voice clip's picture file to prove it does not play twice) |
| Picture order | colour of the frame at each shot centre equals the expected sequence | order/wrap bugs |
| Opening text | render a twin export with `text_layers=[]`; per-frame difference in the text band is non-zero on `[0, 3 s)` and zero after | text missing, wrong timing, never removed |
| Loudness | `ebur128` integrated within the `target_lufs` window | gross mix errors (not taste) |

Other assets reused: `scripts/ios/phone-narration-delivery-replay.py` pattern (KRI-277: replay a persisted
`_device_render_v1` request through the simulator's real reservation/completion routes) for the delivery leg;
`scripts/ios/phone-montage-render-e2e.py` for simulator `DeviceMontageRenderE2ETests`; incident corpus
`src/apps/api/tests/incidents` (`models.py:PhoneRecipeSpec` gets a `voice_behind_footage` builder next to
`speech_montage`; new record `voice-behind-footage-slice` with `kind: output`, synthetic clips and a
`post-fix` observation whose `proof` names the script, which the loader checks); flip
`kri469-voice-clip-ignored.json`'s `output` scope when PR-F lands; add `clarify-*` turns for control (a) and
a request_following thread for control (b).

**Agent-verifiable vs human-only.**

| An agent can verify automatically | Only a person on a physical iPhone can verify |
| --- | --- |
| Recipe contents and `verify_phone_recipe`; question/answer persistence (pytest); macOS AVFoundation export duration, voice tone presence, leak, order, text window, loudness | That the voice sounds like the creator's chosen take and nothing else plays, on the iPhone speaker and headphones (listening) |
| Corpus records and the 34-thread zero-question gate | HEVC/HDR/Live Photo sources and the device's hardware encode path (macOS export uses the same AVFoundation code, not the same silicon) |
| Simulator delivery leg with the replay scripts | Text legibility and cut feel at real size; thermal or memory behaviour on long sources |

Closing evidence for KRI-479 is: `swift test` export + `verify` output attached to the PR, the corpus record,
and the end-of-train on-device checklist (voice identity, no double audio, order, opening text, duration)
from the plan.

## 8. Migration

Rules (from the plan): stamped jobs only; shadow first; one override per flip; legacy byte-identical.

| Step | Mechanism | Preserves |
| --- | --- | --- |
| 1. Add composer and route name behind the stamp | `all_candidates["creator_plan_authority_version"]` is read at dispatch (`stamp_plan_authority`, `PLAN_AUTHORITY_FIELD` [PR-A]); unstamped jobs never reach it | legacy jobs: they run today's speech lane / unified lane unchanged |
| 2. Shadow | PR-D's resolver records `route_mismatch` via `pipeline_trace_for` when it would pick `voice_behind_footage` but the legacy decision differs; legacy still renders | prod behaviour while evidence accumulates |
| 3. Flip the slice route | For stamped jobs whose contract has `composition.route == "voice_behind_footage"`, the dispatcher (`generative_build.py` ~2340-2376) calls the composer instead of `run_phone_speech_montage_job` | retries: route is stamped at approval, so a retry of an earlier approval keeps its original route |
| 4. Retire displaced decisions one at a time (PR-F list, voice-relevant subset) | Each becomes a typed refusal or repair, never a reroute | editor revisions: variant contracts are `rebind`s, not rebuilt |

Code paths displaced for stamped jobs on this shape:

- `app/services/phone_speech_montage_job.py:run_phone_speech_montage_job`: the `request` assembly (`_request_text`, `gb._first_user_message`), `speech_montage_possible(...)`, `order_by_capture` from `brief_view`, and the voice-in-order refusal (~164-172, 199-208, 267-273). The LLM excerpt planner stays for the excerpts route (row 8).
- `app/services/render_shape.py:_may_route_to_speech_montage` (a raw-text gate, `speech_montage_possible(creator_request...)`) decides the shape offer; it must read the stamped route.
- `app/tasks/generative_build.py` dispatcher: `required_speech or (contract is None and not user_song)` and the fallthrough to the unified lane when the speech job returns `False`.
- `app/pipeline/unified_montage.py:_shrink` duration handling for this shape: the composer owns allocation, so a count/duration conflict is asked before, not discovered after.
- Unchanged by design: `app/services/phone_editor.py` (speech montage variants raise "edit its timeline in the app", ~350); slice variants inherit this until the editor can recompile from the pure composer (Q8).

## 9. Risks and open questions

Risks.

| Risk | Mitigation |
| --- | --- |
| Rolling deploy / rollback: any new field in the strict contract model makes stamped jobs unreadable by older workers (`extra="forbid"`) | commitments live in the sibling dict `assembly_plan["creator_composition"]` (plain dict, read only by new code, stale when `contract_digest` differs), the contract model is unchanged; a test compares the model's JSON schema to main's |
| `generative_build.py` is a hot file (D, F, G, H) | PR-H touches the dispatcher by one branch; sequence per the plan's lane 3 |
| New composer duplicates the speech compiler | Q5: same file, shared private helpers |
| Prompt change for `voice_mode` (Q4) | bump `prompt_version`, free replay evals only |

Open questions for the human reviewer (recommended answer first).

1. **Does the voice clip's own picture show?** KRI-469's wording was "cut away from it". Recommend: slice hides it (`voice_picture="hidden"`, audio only) and row 9 declines `capability_unavailable` with the alternative; a later slice adds "speaker first N s" once the seam between a video-track clip and an audio-track clip has a swift test.
2. **Floor 0.8 s?** Recommend yes, one constant `MIN_READABLE_SHOT_S`, overridden only by a creator-authored cadence down to the 0.4 s schema floor. Alternative: pacing-dependent (0.6 s for `fast`).
3. **Long voice, explicit duration: ask which part?** Recommend no: first sentences up to `D`, disclosed on the plan card; "which part" is the excerpts route (`voice_mode`).
4. **`voice_mode` as a Creator-agent strategy field (prompt change) vs a derived rule?** Recommend the field plus a `prompt_version` bump (free replay evals only); a rule over plan fields would be a new heuristic of the kind this train removes.
5. **Composer location:** extend `phone_speech_montage_plan.py` (recommended, one owner of speech-over-picture recipes) or a new `phone_voice_behind_footage_plan.py`.
6. **Where do the commitments live, and who names `route`?** Decided by the PR-D review: a sibling key, never the strict contract model. `route` is PR-D's `assembly_plan["creator_route"]`; `creator_composition` holds the rest and reads the route from it.
7. **Duration tolerance on composer routes.** `verify_phone_recipe` allows 10 %. Recommend `max(0.1 s, 1 frame)` when `composition.route` is set, since the composer sums exact shots; keep 10 % for other routes.
8. **Editor.** Slice variants cannot be edited on the server (as `speech_montage`). Accept for the slice, file a follow-up to recompile from the composer on text/duration edits.
9. **Implicit duration (KRI-469 as stated).** Ask "how long" only when voice `> 60 s`; otherwise default to `min(L, D_strategy)` and disclose. Confirm the 60 s ceiling (CLAUDE.md says sub-60 s output; schema max is 120 s).
10. **No new env flag.** CLAUDE.md has about 12 characters of headroom and the plan's single kill switch (`KRIA_PLAN_AUTHORITY_ENABLED`) covers new jobs. Recommend accepting; rollback = flip the flag for new jobs.
11. **Capture-time ties.** KRI-469's record has two clips with identical `capture_time` (13:06:18). `build_render_contract` sorts stably, so ties follow snapshot order. Recommend disclosing the tie in the plan card rather than asking.

## Implementation status (PR-H)

Built, as section 6 describes it: `voice_mode` strategy field (model-authored, prompt `2026-10-07-v46`),
`compile_phone_voice_behind_footage_plan` + `select_voice_window` in `phone_speech_montage_plan.py`,
`build_render_contract(composition=)` + the sibling `creator_composition` key, the verifier checks
(`composition=`), `Route.VOICE_BEHIND_FOOTAGE` + `run_phone_voice_behind_footage_job` + one stamped-only
dispatcher branch, `which_voice` / `voice_vs_duration`, the `voice_behind_footage` export-proof case in
`scripts/ios/phone-audio-parity.py`, and corpus records. Details: `docs/pipelines/creator-render-contract.md`,
"Voice behind footage".

Deviations and decisions made while building (all within the accepted answers in section 9):

- Q4: `voice_mode` is `upstream_resolved` in `FIELD_MATRIX`, not `supported`: the matrix guard defines
  `supported` as "changes the pinned projection", and by design (hard rule: no contract field) it does not.
  Dispatch derives the commitments from it instead.
- `voice_span_s` is set ONLY for a chosen silent tail (the voice clip's length less the margin); `None` means
  "covers the whole picture". The plan-time `min(usable voice, duration)` of the table needs speech timing that
  does not exist before transcription. For a silent tail the worker verifies against the smaller of the
  commitment and the speech the transcript actually found.
- The verifier allows the voice to stop up to `VOICE_TAIL_SLACK_S` (3 s) before the picture ends (the sentence-snap
  slack of section 6 step 1), so `voice_covers_timeline` is "covers the picture end minus 3 s", not "to the frame".
- The slice variant reuses the `speech_montage` variant id / `resolved_archetype` so every existing consumer
  (editor refusal, client) treats it as a device-only fixed recipe; the record carries `route:
  "voice_behind_footage"`.
- The implicit "how long" question offers only lengths in which every picture clip can be seen at the 0.8 s floor
  (41 clips: 60 s only), and `duration_vs_count` now also applies to this shape.
- A model-added day-vlog / single-hero shape next to a continuous voice is cleared rather than demoting the voice.
- The `voice_mode` question of section 3 is not implemented (the Creator sets the field).
- `kri469-voice-clip-ignored` stays xfail with a precise note (see the pipeline doc); the shape is covered by
  synthetic records instead.
