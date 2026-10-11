# KRI-492 / KRI-493 length ownership review

Status: base audit at `ab315dbbc`; final policy evidence is intentionally open.
This review covers the requested phone and cloud edit-format/audio combinations.

## Evidence boundary

The model/schema default is 24 seconds, not creator intent. `MainCreatorAgent`
now derives creator duration from creator text, brief timing, or an authoritative
answer through `length_policy.creator_duration_s`, then writes
`creator_duration_s` and `target_duration_requested`
(`src/apps/api/app/agents/main_creator.py:604-612,665-678`). Draft sanitization
uses the same policy and rejects a client-supplied marker
(`src/apps/api/app/kria/drafts.py:43-82`). The schema serializer keeps explicit
24 seconds (`src/apps/api/app/agents/_schemas/creator_agent.py:546-555`), and
the render contract accepts a strategy duration only with that provenance or an
equivalent live brief/answer (`src/apps/api/app/services/creator_render_contract.py:945-983`).
The former non-24 raw-value compatibility fallback is no longer the policy
source; final workers consume the stamped creator-duration state. Final
draft/build parity still needs receipts.

`best_parts.select_best_parts` keeps source order, caps at 120 seconds, includes
each selected source for at least 0.1 seconds, and returns typed conflicts rather
than silently dropping a source (`src/apps/api/app/pipeline/best_parts.py:16-18,149-220`).
The ordinary silent montage still has a 0.8-second readability floor. Image
holds are conditional: 1.2 seconds is preferred and 3 seconds is profiled
capacity when the source permits. These are planning capacities, not creator
requests. The selector caps the portable plan at 120 seconds and requires a
0.1-second included window.
The energy selector's `window_start_s` is a source-window coordinate, not a
general duration owner. Pools/order/pins and `use_all` do not independently own
duration.

## Combination audit

| Combination | Actual owner in base | Verifier evidence | Base verdict | Approved final behavior |
| --- | --- | --- | --- | --- |
| Phone plain/silent montage | Footage allocation; fast path uses 0.8 s normal cuts, 1.2 s default cut/image capacity, and 3 s minimum (`edit_direction_planner.py:56-64,322-359`) | Compiled recipe duration, source windows, capacity | `matches` only with measured output; `missing` for desired metadata alone | Explicit creator length ±10%; otherwise footage owner and approved cap |
| Cloud plain/silent montage | Cloud montage allocation | Renderer measured duration, if adapter exposes it | `missing` where no owner receipt exists | Footage-owned rule; receipt evidence open |
| Phone user song/background | Selected, server-frozen catalog section | Song window and picture span | `matches` only when the frozen section and measured picture span persist | The selected catalog section owns length; missing catalog evidence falls back to footage |
| Phone user song/lip-sync | Lip-sync song/video timeline; song alignment has a 120 s maximum take window (`song_alignment.py:69-90`) | Song section, take assignments, final window | `matches` only when all persist; otherwise `missing` | Lip-sync song cap is 120 s |
| Cloud user song/background or lip-sync | No supported cloud user-song compiler | No cloud receipt proving a user-song window | `unsupported` (not `missing`) for the cloud user-song route | Explicitly `cloud user_song unsupported`; a missing catalog section falls back to footage |
| Phone recorded voiceover montage | Recorded voiceover duration; compiler consumes exact decision/source windows and does not choose them (`phone_voiceover_montage_plan.py:191-215`) | Voiceover asset and audible narration track | `matches` with narration evidence; otherwise `missing` | VO recording owns length |
| Cloud recorded voiceover/narrated | Cloud narration renderer | `narration_applied` plus measured duration | `matches` only with both; otherwise `missing` | VO recording owns length |
| Phone talking/subtitled original speech | Original take plus applied cleanup windows | Take duration and cleanup receipt | `matches` only with measured take evidence; otherwise `missing` | Speech take/pause owns length; there is no exemption from receipt verification |
| Cloud talking/subtitled original speech | Cloud take/speech renderer where available | Take and cleanup receipt | `missing` where adapter declines route | Speech take/pause owns length; route evidence open |
| Phone continuous voice | Voice-behind-footage composer; voice and picture spans are separate (`choice_questions.py:651-672,718-745`) | Voice span, picture span, silent-tail answer | `matches` with composer receipt; otherwise `missing` | Speech take owns voice span; durable answer owns silent tail |
| Cloud continuous voice | Cloud voice-behind-footage route if enabled | Voice/picture spans and receipt | `missing` until adapter evidence | Same owner and answer persistence |
| Phone montage with speech excerpts | Selected source recording and excerpt windows | Source id, excerpt windows, measured output | `missing` when only generic montage metadata exists | Selected excerpt windows own length |
| Cloud montage with speech excerpts | Route-specific excerpt renderer if enabled | Source ids, windows, measured output | `missing` unless declared and evidenced | Same selected-window owner |
| Explicit creator length | Raw provenance → contract → compiler | Actual rendered duration versus requested | `matches`, `differs`, or `missing` only from receipts | ±10%; default 24 never counts as evidence |

## Approved policy and open gaps

The approved final map is: explicit creator length is honored within ±10%; with
no named length, a song owns a background section, speech owns a take/pause
span, a recorded voiceover owns the output span, and footage owns best-parts
selection capped at 120 seconds with a 0.1-second included floor. Derived lengths
and receipts remain informative when capacity is insufficient and do not fail a
render by themselves.

These acceptance rows stay open until parent integration supplies final evidence.

| Gap | Required evidence | Base status |
| --- | --- | --- |
| KRI-494 | Shared natural-owner declaration covers every format/audio pair | Declaration is present; phone/cloud evidence open |
| KRI-495 | Main creator, drafts, and `generative_build` preserve creator provenance, including explicit 24 seconds | Main creator/draft policy landed; replay evidence open |
| KRI-496 | Final best-parts owner has 120 s cap, 0.1 s floor, frame-aligned windows, typed capacity receipt, and no silent source loss | Selector and receipts landed; candidate verification open |
| KRI-497 | `collect_conflicts`, `fold_scoped_answers`, and `resolve_choices` preserve durable scoped answers, digest invalidation, and derived-length receipts | Enrichment changes landed; replay evidence open |
| KRI-498 | Phone/cloud comparison for every row is grounded in actual receipts as `matches`, `differs`, or `missing` | Open; strict schema remains compatible; cloud user-song remains explicitly `unsupported` |
| KRI-499 | Replay/rollout evidence covers generation binding, `log_only`/`enforce`, editor revisions, and phone/cloud hooks | Hooks landed; rollout evidence open |

## Resumable acceptance evidence

The resumable replay packet is bounded to 23 complete available snapshots: 20
device journeys and 3 cloud journeys. Each snapshot records minimized actual
recipe inputs and source hashes. Text checks are structural and masked; they
qualify route shape but do not claim pixel or content correctness. The actual
three incident shapes are separately captured in
`scripts/ci/fixtures/kri492-incident-shapes.json`; each has a capture timestamp,
source SHA-256, and `no_creator_payload` projection qualification.

The native explicit-24-second journey passed the actual simulator export in
`scripts/ios/kri-492-length-e2e.py`. This is a bounded native proof, not a full
all-path canary pass. Promotion still requires 24 clean hours of replay and
canary evidence, the Fly workflow proof guard, and the available device/cloud
canaries; no final review, PR, or deploy claim is made here.

## Complete format/audio catalog

This is the complete 9-format × 4-audio-source matrix (36 combinations),
including combinations whose compiler is explicitly unsupported.

Owner codes: `F` footage, `T` source take, `V` recorded voiceover, `S` user
song. The declaration is the current natural owner before an explicit creator
target overrides it (`src/apps/api/app/services/length_policy.py:26-81`).

| Edit format | licensed music | original audio | voiceover | user song | Phone support | Cloud support |
| --- | --- | --- | --- | --- | --- | --- |
| montage | F | F | V | S | Supported; montage compiler/gates | Supported route except user-song; adapter receipt required |
| talking_head | F | T | V | S | Unsupported as a declared format | Supported cloud talking route where adapter is declared |
| day_vlog | F | F | V | S | Supported; guided family gate | Supported guided route; adapter receipt required |
| single_hero | F | F | V | S | Supported; guided family gate | Supported guided route; adapter receipt required |
| subtitled | F | T | V | S | Conditional: one clip and rollout flags | Supported cloud subtitled route where adapter is declared |
| narrated | F | T | V | S | Conditional: narration flags/audio and archetype gate | Supported narration route where adapter is declared |
| narrated_planned | F | T | V | S | Conditional: narration flags/audio and archetype gate | Supported narration route where adapter is declared |
| narrated_ready | F | T | V | S | Conditional: narration flags/audio and archetype gate | Supported narration route where adapter is declared |
| slides | F | F | V | S | Unsupported phone compiler | Supported slide-post route; user-song remains unsupported |

Phone support is the settings-aware intersection in
`phone_rollout.phone_render_supported_formats`
(`src/apps/api/app/services/phone_rollout.py:887-945`); static compiler
existence is not a runtime pass. Cloud support means the adapter must consume
and evidence the relevant requirement; desired metadata never qualifies. The
voiceover compiler consumes exact decision/source windows and does not choose
them (`src/apps/api/app/pipeline/phone_voiceover_montage_plan.py:185-215`).

Both compilers consume the same frozen approved windows. The phone path reads
and validates `length_plan` through
`generative_build._approved_length_plan_envelope` and
`length_planning.read_length_plan`; the cloud unified montage path performs the
same read through `unified_montage` (`approved_length_plan`). Generation,
storage identity, source duration, and window coverage are checked before either
compiler runs, so neither compiler may silently reselect windows.

Focused questions remain bounded: a source pool over 80 clips asks the creator
to choose fewer clips, while selected sources below 0.1 seconds, insufficient
natural capacity, or a requested duration beyond the 120-second portable cap
produce typed scoped conflicts. These answers are persisted with the question
input digest by `collect_conflicts`, `fold_scoped_answers`, and `resolve_choices`;
they do not turn a derived duration into creator proof.

Verifier behavior is generation-bound: `LengthPolicyState` records the owner,
generation id, contract digest, requested duration, and check mode
(`src/apps/api/app/services/length_policy_state.py:21-58`). Phone recipe checks
use it at `verify_phone_recipe` (`src/apps/api/app/services/creator_render_contract.py:1526-1562`);
cloud variant checks use the same state and measured receipt duration
(`src/apps/api/app/services/cloud_render_contract.py:612-699`); editor revisions
rebind and restamp the policy per variant
(`src/apps/api/app/services/phone_editor.py:350-360`). The default
`creator_duration_check_mode` is `log_only`, so an observation is not a clean
enforcement pass until rollout evidence exercises `enforce`.

Any final implementation must preserve the strict stored-contract schema and
digest behavior in `docs/pipelines/creator-render-contract.md`. A new owner field
requires the existing stored-v1 default/digest treatment; this audit does not
claim such a field is already persisted or verified.
