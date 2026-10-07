# Creator render contract

KRI-470 closes the handoff between an approved creator edit and the artifact
that gets rendered. A strategy, brief, or chat request is intent; it is not
proof that a renderer obeyed that intent. The contract makes the hard parts of
the approved edit explicit, routes from those facts, and refuses to pin or
publish when the compiled output cannot prove them.

## Authority and lifecycle

The approval boundary projects the accepted `CreativeStrategy` and pinned brief
into a versioned `creator_render_requirements` object. It records the
`generation_id`, strategy and brief digests, explicit duration, audio source
IDs and camera-audio policy, voiceover requirement, exact text with role and
timing, and the resolved clip order. The object has its own integrity digest.
Duration explicitness is retained before default serialization, including an
explicit 24-second request; a default value must not be mistaken for an
approved target.

The execution path is:

```text
approved strategy + brief + media identities
                -> contract (immutable generation and digest)
                -> capability route
                -> existing compiler / EditRecipeV2
                -> compiled contract verifier
                -> pin only if all hard requirements pass
                -> native/cloud output evidence
                -> final publication guard
```

On phones, the mandatory pin verifier checks the recipe timeline, audible
track sources and mute coverage, text content and placement, and the visual
sequence. The final device publication check revalidates the contract, recipe
digest, generation, brief identity, and source identity while holding the job
lock. A retry receives a fresh attempt identity but retains the approved
authority. An editor revision is a per-variant contract; sibling variants and
retries must not inherit another variant's edits.

Replacement renders are staged and generation-fenced before they can replace a
published artifact. Cloud update/upsert boundaries return `False` on rejection
and preserve the last-good video and poster. Staged replacements need fresh
evidence before callers can retire the previous artifact; a later status-only
`ready` write rechecks that evidence and cannot revive a rejected attempt.

The contract marker in `all_candidates` is mandatory for new contracted work.
If the marker is present but the assembly contract is missing or invalid, the
job fails closed. Historical jobs without the marker keep the legacy behavior
for compatibility. Requirements are never reconstructed from generated output
or reread from mutable chat during a retry.

## What is hard and what is not

The core contract covers duration, audio, ordering, and exact text. Every
`CreativeStrategy` field path still has an explicit disposition in code (see
"Field matrix"), but fields outside this projection remain under their existing
capability or planning policy. A matrix entry is not evidence that the field is
enforced by every renderer.

### Field matrix

`FIELD_MATRIX` in `services/creator_render_contract.py` replaces the old flat
per-field note. It is keyed by field path, nested and with list fields marked
`[]` (`shot_labels[]`, `clip_intents[].op`, `montage_audio.source_media_ids[]`),
and each entry carries a disposition and the owning component:

| Disposition | Meaning |
| --- | --- |
| `supported` | The contract pins it and a verifier produces evidence (duration, audio source/policy, exact text, order, voiceover requirement). |
| `preference_only` | Taste; nothing is held to it. |
| `upstream_resolved` | Resolved or repaired before the contract (planner, capability policy, server resolvers); not re-verified. |
| `unsupported` | A creator can state it but no component proves it (a named licensed SFX). A request depending on it must not be treated as met. |

The required path set is derived by walking the `CreativeStrategy` pydantic
schema recursively (`schema_field_paths`). `test_every_strategy_path_is_assigned`
fails when any field or nested field is added, removed, or renamed without a
matrix entry, so new fields cannot ship unclassified.

### Adapter declarations

`ADAPTER_DECLARATIONS` (phone, in `creator_render_contract.py`) and
`CLOUD_ADAPTER_DECLARATIONS` (cloud, in `cloud_render_contract.py`) state, per
render path, which contract requirements it `consumes` (routes from and
verifies) and which it `declines`, with a typed reason. Every requirement
(`duration_s`, `require_voiceover`, `audio_source_ids`, `original_audio`,
`exact_texts`, `order_required`, `unresolved`) must be consumed or declined
exactly once. Today only the phone speech montage routes from the audio, duration
and order requirements; every other phone path is verified at pin time
(`verify_phone_recipe`). The cloud adapters consume exactly what their renderer
emits receipt evidence for (see "Cloud evidence"); the rest stay declined.
Behaviour tests drive the real entry points (`verify_phone_recipe`,
`check_phone_dispatch_contract`, `preflight_cloud_contract`,
`verify_cloud_variant`) and assert the declared reason, not the table.

## Typed declines

A refusal carries a `DeclineReason` and a `field_path` (matrix path, or
`brief:text` for a brief-only literal) on `CreatorRenderContractError` and
`CloudRenderContractError`:

| Reason | Meaning | Creator recovery (`tasks/kria_runtime.py`) |
| --- | --- | --- |
| `capability_unavailable` | The path can never honour or prove it. | Refusal naming the limit and a supported alternative. |
| `evidence_missing` | Supported, but the output did not demonstrate it. | Repair / retry, only where the evidence can appear on a re-run (cloud publication verification, `creator_render_contract_unverified`; cloud preflight). A phone `phone_plan_unsupported` stays deterministic: it asks, with the original copy plus the typed alternative. |
| `requirement_conflict` | Two approved requirements cannot both hold. | Existing ask behaviour (real questions arrive with the clarification gate); a typed alternative is appended to phone copy. |
| `needs_choice` | Ambiguous until the creator decides (includes `unresolved`). | Existing ask behaviour. |

The reason is persisted beside, never in place of, the existing failure strings:
`phone_plan_unsupported`, `creator_render_contract_unsupported` and
`creator_render_contract_unverified` are unchanged. Phone and cloud-preflight
declines land in `Job.assembly_plan["creator_decline"]`
(`{"decline_reason", "field_path"?, "alternative"?, "failure_reason"}`), stamped with
the failure code they belong to; recovery honours it only when it matches the
job's current failure code, and a new worker run or a successful finalization
clears it, so a later unrelated failure never inherits an old refusal. A cloud
publication decline lands on the failed variant next to `error_class`, and
recovery reads only the targeted variant's own decline. `unresolved` stays a tuple of
strings; it is reported as `needs_choice` without a field path.

## Stored-contract compatibility

`CreatorRenderContract` forbids extra fields and digests its full dump, so adding
a field would change the digest of every stored contract. `_digest` skips any
field registered in `_POST_V1_FIELD_DEFAULTS` while it holds its default, and
`STORED_V1_CONTRACT` in `tests/services/test_creator_render_contract.py` is a real
stored v1 contract that must keep reading. Register a new field there in the same
change that adds it.

## Plan-authority stamp

`KRIA_PLAN_AUTHORITY_ENABLED` (default on) is read once, where a job's contract is
first stamped (`services/generative_jobs.py`, or `tasks/content_plan_build.py` when
dispatch is what first builds it), and persisted as
`Job.all_candidates["creator_plan_authority_version"] = 1` (a JSON key; no
migration). Workers branch on the stamp and never on the live flag, so a running
job cannot change behaviour; jobs without the stamp keep legacy behaviour. Nothing
branches on it yet.

| Requirement | Phone evidence | Cloud evidence | Current rule |
| --- | --- | --- | --- |
| Duration | Compiled recipe duration within the accepted tolerance | Renderer measured duration (`actual_duration_s` or measured `duration_s`) | Compare actual output; never compare desired metadata |
| Recorded voice | Audible `VoiceoverRenderAsset` on an audio track | `narration_applied`, set only when the mixer reports the recording is in the output file | Guided and classic honour it; missing evidence declines |
| Camera audio (require / forbid) | Audible original assets, source IDs, and complete mute-window coverage | `source_audio_ids` + `source_audio_state` from the audio graph actually mixed | Guided honours and proves it; classic declines up front; named sources decline everywhere |
| Chronological order | Every selected clip has capture evidence; recipe picture sequence matches resolved IDs | `actual_clip_order` / `picture_timeline` from the rendered moments | Guided: gated before render, then verified; classic declines up front |
| Exact text | Normalized text layers, role/clip placement, and timing | `text_evidence` rows (role, text, window, media id) from the burned, pixel-checked layers | Guided honours and proves it; classic and slides decline |

Audio intent copied into a receipt or field such as
`source_audio_preserved` is not proof that camera audio survived the render.
The phone verifier needs actual recipe track/gain/mute evidence; the cloud
verifier needs the evidence's own `source_audio_ids`. Likewise, a loose list of
text strings cannot prove opening, closing, or per-clip placement: only rows
that carry a role and window, measured on the burned output, can.

### Cloud evidence (KRI-470 PR-E)

Two different things are declared per cloud adapter, and only one of them lifts a
preflight decline:

- **Honours** (`CLOUD_ADAPTER_DECLARATIONS[...].consumes`): the renderer follows the
  requirement by construction, or a cheap pre-render gate proves the plan already does.
  Only these are lifted at preflight, so a default contracted job never renders
  everything and then fails publication.
- **Evidences** (`CLOUD_EVIDENCES`): what the receipts can report. A superset; the
  post-render verifier checks it as the last line of defence, never the first.

| Requirement | `cloud_guided_story` | `cloud_classic` | `cloud_slides` |
| --- | --- | --- | --- |
| `duration_s` | honours, evidences | honours, evidences | declined (stills) |
| `require_voiceover` | honours (execution contract + pinned narration mix), evidences | honours (by archetype), evidences (**was declined: rendered then rejected**) | declined |
| `original_audio` | honours (`montage_audio` is in the snapshot), evidences | **declined** (matcher never reads `audio_strategy`; song variants replace camera audio, track-less ones keep it), evidences | declined |
| `order_required` | **gated** (pre-render plan gate), evidences | **declined** (greedy matcher never reads the contract), evidences | declined |
| `exact_texts` | honours (typed copy flows into the snapshot), evidences | declined | declined |
| `audio_source_ids` | declined (mixes every clip's sound) | declined | declined |

Before this PR every cloud adapter declined `exact_texts`, `audio_source_ids`,
`original_audio` and `order_required`, and classic voiceover/narrated jobs rendered
and were then rejected at publication for lack of a receipt.

**The guided order gate.** The guided builder selects a coverage set and does not order
it by the contract (`ordering_choice` is never read by the proposal builder; fast
montage records `ordering_not_applied`). So after `_guided_execution_plan` and before
the attempt is claimed or any media is touched, `check_guided_plan_order` compares the
collapsed (adjacent repeats merged) media order of the pinned plan's `story_timeline`
with `contract.order_ids` restricted to the media the plan uses. Equal proceeds; anything
else is a typed `capability_unavailable` on `ordering_choice` with an iPhone alternative,
persisted like other typed declines. The route resolver (PR-D/F) is what will eventually
route order. The verifier applies the same rule to the rendered order
(`order_satisfied`), so a gate that passed and a render that drifted is still refused.

**Where the evidence lives.** In a plain dict on the variant, `variant["cloud_evidence"]`
(`app/pipeline/cloud_render_evidence.py`), beside `render_receipt` and never inside the
strict `GuidedStoryRenderReceipt`: that model forbids unknown fields, so an older worker
reading a newer receipt during a rolling deploy or rollback would otherwise raise
`guided_story_receipt_mismatch`. The strict receipt is byte-compatible with before.
`verify_cloud_variant` reads the receipt and the sibling together.

| Evidence key | Meaning |
| --- | --- |
| `actual_clip_order` | Source media ids in output order (immediate repeats merged) |
| `picture_timeline` | `{media_id, start_s, end_s}` per segment of the output picture (guided only) |
| `source_audio_ids` / `source_audio_state` / `source_audio_reason` | Sources whose own sound is audible; `muted` carries why (`replaced_by_narration`, `replaced_by_music`, `not_preserved`, `level_zero`, `no_source_audio`) |
| `text_evidence` | `{role, text, start_s, end_s, media_id?}` per text layer measured visible (guided only) |
| `narration_applied` | The recording is in the output file (the voiceover mixer copies the video through on failure, so intent is never proof) |
| `actual_duration_s` | Probed length of the output |

`None`/absent means "this renderer did not produce it" and the verifier reports
`evidence_missing`; `[]` means "produced, and there was nothing". Evidence describes
one artifact and never outlives it: a new artifact without fresh evidence drops the old
(`_update_variant_entry`, the staged-merge helper and `_merge_finalized_variants`), a text
reburn re-derives it from the edited elements, and a later artifact with none is
`evidence_missing`, not a pass on stale evidence. Receipts written before this change
have none and are never failed retroactively for requirements they never had to prove.

Evidence is emitted only for jobs that carry the contract marker
(`_cloud_evidence_context`), so a job without it gains no key on its variants. This is
narrower than "unchanged stored data": the marker is set whenever a creator strategy
exists, so every creator-flow job gains a `cloud_evidence` key (and the guided path one
extra read of the job row per render). The context carries the approved
`gcs_path -> media_id` map; an unreadable job logs
`cloud_evidence_context_unreadable` (with the job id) and the render proceeds without
evidence, which a contracted publication then refuses visibly.

How each adapter evidences a requirement and where it stops:

- **Guided story** (`guided_cloud_evidence`): order and picture windows come from the
  moments actually rendered; camera audio from the branches `_mux_guided_source_audio`
  mixed (replaced by a narration or a song when one is applied, 0 when the creator's
  level is 0); text from `burn_text_overlays_skia_with_evidence`'s per-element pixel
  check, with the role taken from the compiler's own element ids (`guided-title`
  opening, `guided-closing-title` closing, `clip-label-*` / `montage-text-*` per shot,
  bound to the segment they overlap). A closing title's planned end is compared with the
  measured duration using the renderer's own slack, `max(0.2, 0.04 * moments)`
  (`duration_tolerance_s`, shared with the guided duration check).
- **Classic** (`classic_cloud_evidence`): emitted by the montage/voiceover/day-vlog/
  single-hero renderer (`_process_generative_variant`) and the narrated renderer. Its
  slots are SOURCE-time windows, so classic reports order and camera audio only and
  never per-clip output timing (no `picture_timeline`). A collage (masonry) cut or a
  spliced carousel moment withholds order/audio evidence rather than guessing.
  Talking-head and subtitled renders emit no evidence, so `check_classic_archetype`
  refuses `require_voiceover` for them (`capability_unavailable`) once the archetype is
  known, before any variant renders. Order and camera audio are *reported* but declined
  up front (see the table).
- **Slides**: stills, no length, no voice, no camera audio, no order: everything stays
  declined with `capability_unavailable`.

Known limits: lanes that publish a new artifact without going through the renderers
above carry no evidence, so on a contract with objective requirements they stay refused
(`evidence_missing` or the in-place decline). For classic that is every reburn/edit lane:
narrated caption and bed-level reburns, camera-effect re-renders, retranscribe, the
overlay and sound-effect passes, and fast text reburns (`_update_variant_entry` drops the
receipt on a new artifact). Editor re-renders of a guided plan (timeline, revision,
orientation) re-run `render_execution_plan` and so re-emit evidence, but the pre-render
order gate only runs on the first render (the publication verifier covers the rest).
Real-output check: an independent per-segment `ebur128` of the rendered file matches the
evidence's order and camera-audio claims (each IstRun clip has a distinct loudness
signature), a muted edit measures -70 LUFS, and a voiceover whose mix failed is a
copy-through of the footage with no voice, reported `narration_applied: false`.

Preflight takes the dispatched adapter (`cloud_adapter_for_job`); without a named
adapter it keeps the pre-PR conservative refusal, so nothing is lifted on an unknown
route. `verify_cloud_variant` picks the adapter from the variant
(`cloud_adapter_for_variant`).

## Routing and failure behavior

Routing consumes the typed contract rather than treating raw prompt wording as
a new source of truth. In the speech montage path, the selected camera-audio
IDs constrain transcription and planning. A planner refusal or unavailable
capability cannot silently fall through to an ordinary montage when the
contract requires speech. Unsupported combinations decline with a
creator-facing reason before pinning; missing evidence is a contract failure,
not an invitation to accept a one-off raw-text override.

Cloud preflight declines unresolved order facts and every requirement the
dispatched adapter's declaration lists as declined (see "Cloud evidence"). For
consumed requirements, publication checks measured duration and the
renderer-produced receipt evidence.
Contracts with no objective requirements do not require receipts. A replacement
cannot reuse its predecessor's receipt or duration as proof of the new file.

The same fail-closed rule applies to fast in-place media passes. Overlay and
sound-effect mutations decline before overwriting a variant when its contract
contains objective requirements that the cloud path cannot re-verify. A staged
output must pass its own generation and receipt checks before publication; stale
workers return without touching newer work or the last-good artifact.

The final guard is therefore two-dimensional: native export checks still prove
the file and recipe are internally valid, while the creator contract proves
that the recipe was the approved one. Either side can refuse publication.

## Identity and editing rules

The contract binds the approved strategy and brief digests to a render
generation. Device records also retain the recipe digest and source identity;
publication rejects a changed recipe, stale generation, mismatched brief, or
unverified source. Deliberate editor changes mint a variant-specific revision
and are checked against that revision's authority. A stale root contract must
not be applied to an edited variant.

Legacy unmarked jobs remain readable and follow their previous path. New jobs
must carry the version marker and a valid contract; partial migration is
explicitly fail-closed so a missing contract cannot masquerade as an
unconstrained edit.

## Verification scope

The focused corpus covers the four core contract classes, wrong and missing
audio sources, partial mute coverage, misplaced text, reversed order, missing
chronology evidence, digest changes, retries, sibling editor revisions, and
legacy compatibility. Baseline tests were green while the original worker and
compiler still demonstrated silent violations; those overlapping baseline and
focused totals must not be added together. The final test counts and commands
belong to the release report.

The evidence uses synthetic fixtures with the real speech worker and native
recipe compiler. It does not claim production replay, iPhone export, native
pixel inspection, or listening to a rendered artifact. Cloud receipt limits are
intentional: exact text, camera-audio source identity/preservation, and order
are declined per adapter until that adapter's renderer emits proof that can
establish them (guided: exact text and camera audio, with order gated before render;
classic: recorded voice only; named audio sources: no cloud adapter).

Required speech cleanup also has a private winner stage. The accepted speech
snapshot and its upload generation are verified before the staged result is
swapped into the public variant; a missing, stale, or losing attempt cannot
publish an ordinary montage or overwrite the last-good result.

Text proof is based on rendered layers, not a flat string list. Multiline text
joins runs by baseline, karaoke lines join word runs with spaces, and every
required non-whitespace run needs non-transparent paint evidence. An invisible required word
is rejected unless an active karaoke highlight supplies the visible proof for
that word.

The existing `EditRecipeV2` already represents separate voice and picture
tracks, so a whole-track composer rewrite is deferred. Future fields must be
added with an explicit contract disposition, adapter evidence, and a failure
fixture before they become hard requirements.

Edit duration excludes the fixed, declared brand outro, matching
`finished_durations`: the device export probe verifies the recipe plus that
server-known tail. A picture order that includes a source used only as a speech
bed is unsupported; it is refused rather than silently dropping that source.

## Offline checks

From the repository root:

```sh
bash scripts/check-api.sh test tests/services/test_creator_render_contract.py tests/services/test_creator_render_binding.py tests/services/test_cloud_render_contract.py tests/services/test_device_render_contract.py tests/services/test_phone_editor_render_contract.py tests/services/test_phone_speech_montage_job.py tests/services/test_speech_montage_planning.py -q
bash scripts/check-api.sh test tests/tasks/test_unified_montage_dispatch.py tests/tasks/test_phone_format_matrix.py tests/tasks/test_generative_build.py tests/tasks/test_guided_story_build.py tests/routes/test_device_render.py tests/routes/test_phone_editor_commit.py -q
bash scripts/preship-check.sh
```
