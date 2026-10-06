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
(`verify_phone_recipe`); the cloud paths decline what no receipt can prove.
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
| `evidence_missing` | Supported, but the output did not demonstrate it. | Repair / retry. |
| `requirement_conflict` | Two approved requirements cannot both hold. | Existing ask behaviour (real questions arrive with the clarification gate). |
| `needs_choice` | Ambiguous until the creator decides (includes `unresolved`). | Existing ask behaviour. |

The reason is persisted beside, never in place of, the existing failure strings:
`phone_plan_unsupported`, `creator_render_contract_unsupported` and
`creator_render_contract_unverified` are unchanged. Phone and cloud-preflight
declines land in `Job.assembly_plan["creator_decline"]`
(`{"decline_reason", "field_path"?, "alternative"?}`); a cloud publication decline
lands on the failed variant next to `error_class`. `unresolved` stays a tuple of
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
| Recorded voice | Audible `VoiceoverRenderAsset` on an audio track | Verified receipt with `narration_applied` | Missing evidence declines |
| Camera audio | Audible original assets, source IDs, and complete mute-window coverage | Not provable by current cloud receipts | Preserve/mute requests decline before cloud work |
| Chronological order | Every selected clip has capture evidence; recipe picture sequence matches resolved IDs | No current standardized evidence; preflight declines | Missing chronology evidence declines |
| Exact text | Normalized text layers, role/clip placement, and timing | No structured role/timing proof in current receipts | Cloud preflight and verification decline |

Audio intent copied into a receipt or field such as
`source_audio_preserved` is not proof that camera audio survived the render.
The phone verifier needs actual recipe track/gain/mute evidence. Cloud currently
cannot prove camera-audio source IDs or preservation, so it declines early.
Likewise, a loose cloud list of text strings cannot prove opening, closing, or
per-clip placement.

## Routing and failure behavior

Routing consumes the typed contract rather than treating raw prompt wording as
a new source of truth. In the speech montage path, the selected camera-audio
IDs constrain transcription and planning. A planner refusal or unavailable
capability cannot silently fall through to an ordinary montage when the
contract requires speech. Unsupported combinations decline with a
creator-facing reason before pinning; missing evidence is a contract failure,
not an invitation to accept a one-off raw-text override.

Cloud preflight declines unresolved order facts, exact text, camera-audio
source/preservation requirements, and other requirements for which the current
cloud compiler has no standardized proof. For supported duration and narration
checks, publication uses measured duration or renderer-produced narration receipts.
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
are declined until the cloud path emits proof that can establish them.

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
