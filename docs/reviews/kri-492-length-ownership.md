# KRI-492 / KRI-493 length ownership audit

Status: implementation and local verification; release, deployed canaries, clean observation window, and enforcement promotion remain open. Capture base audit: `ab315dbbc22c28e04e68e8f37b38980a3ad572c9`. The catalog below includes every declared format/audio pair on phone and cloud. Compiler availability is conditional on the dispatch and capability gates; a policy declaration does not enable an unsupported route.

## Shared policy and provenance

`length_policy.NATURAL_OWNER_DECLARATION` declares all 9 formats × 4 audio sources. `natural_length_owner` adds three source-backed overrides: a server-frozen licensed music section owns its selected span; a recorded voiceover owns its recording; continuous voice owns its take. `length_owner` returns `creator` only when creator-authored timing or an authoritative answered duration choice establishes a target. `creator_duration_matches` is the single finite-positive ±10% comparison used by planning and verification.

The ordinary schema default of 24 seconds never establishes creator intent. `creator_duration_s` reads creator text, live brief timing, and authoritative scoped answers. Main creator, draft sanitization, contract building, and generative dispatch use that policy. Client/model `creator_duration_s` and selected catalog metadata are sanitized at entrypoints. An explicit request for 24 seconds survives serialization. A validated creator timeline edit establishes timing for that new generation without changing a sibling generation's contract.

Source references (paths relative to the repository):

- **P**: `src/apps/api/app/services/length_policy.py`, `natural_length_owner`, `creator_duration_s`, `creator_duration_matches`.
- **L**: `src/apps/api/app/services/length_planning.py`, `build_length_plan`, `read_length_plan`; `src/apps/api/app/pipeline/best_parts.py`, `select_best_parts`; `src/apps/api/app/services/single_hero_policy.py`, shared hero ordering and composition allocation.
- **D**: `src/apps/api/app/tasks/content_plan_build.py`, phone dispatch gates; `src/apps/api/app/services/phone_rollout.py`, `phone_render_supported_formats`, `phone_user_song_supported`, `phone_talking_head_supported`.
- **G**: `src/apps/api/app/tasks/generative_build.py`, `_approved_length_plan_envelope`, `_decide_generative_variant`, `_effective_music_window`; `src/apps/api/app/pipeline/unified_montage.py`, approved window reader; `src/apps/api/app/pipeline/phone_voiceover_montage_plan.py`, exact decision window consumption.
- **V**: `src/apps/api/app/services/length_policy_state.py`, `LengthPolicyState`, `check_creator_duration`; `src/apps/api/app/services/creator_render_contract.py`, `verify_phone_recipe`; `src/apps/api/app/services/cloud_render_contract.py`, cloud validation and adapter declarations; `src/apps/api/app/kria/brief_checks.py`, `_check_timing`.
- **Q**: `src/apps/api/app/services/choice_questions.py`, `collect_conflicts`, `fold_scoped_answers`, `resolve_choices`; `src/apps/api/app/services/length_planning.py`, scoped conflict and input binding.

## Catalog and route verdicts

Owner/source codes: **F** selected footage windows; **T** speech take minus approved pauses; **V** recording; **S** song placements/selected section. Licensed music is F until the server selects and freezes a section, then S. Any creator-proven target overrides these within ±10%.

Selection codes: **B** server-selected best parts, exact windows and selection receipts consumed by compilers (L/G); **A** song alignment or frozen music section, preserving approved placements (G); **T** approved speech/cleanup windows, preserving spoken continuity; **V** selected picture windows fitted to the recording, preserving narration. Silent footage uses B. A creator target cannot authorize looping, retiming, dropping required clips, or changing audio by itself; infeasible constraints enter Q before rendering.

Best-parts allocation uses the creator-proven target first, then a recorded voiceover or frozen licensed section's natural duration, then footage highlights. An unreachable natural audio clock produces a capacity conflict rather than silently shortening the edit. User-song placements retain their song-owned alignment and are excluded from this best-parts window allocator; a declared song owner alone does not make that path eligible.

Phone codes: **M** montage family behind account enrollment/approval gates; **MV** M plus verified narration capability and narration rollout; **MS** M plus user-song rollout (phone only); **ST** single-clip subtitled plus subtitled/archetype rollout; **NV** narrated plus narration/archetype rollout; **NT** narrated self-narration: one clip through ST, or multiple clips through verified talking-head rollout. **U** explicitly unsupported. All phone verdicts depend on D and the recipe's verified capabilities.

Cloud codes: **C** classic/guided route where selected; **CV** C with recorded narration receipt; **CT** speech route where selected, with classic pinned camera-audio restrictions; **SL** still-image post. Classic preflight declines pinned original audio, order, and exact text; guided consumes those requirements when its plan passes preflight. Cloud user-song is U. SL cannot prove a duration or voice requirement. These declarations are in V, not inferred from renderer metadata.

Verifier codes: **R** generation-bound creator target versus measured recipe/export or cloud receipt duration; derived length remains a receipt note. Audio, text, order, and recipe integrity retain separate checks. **RT** R plus take/cleanup receipt; **RV** R plus audible narration receipt; **RA** R plus placement/section and audio identity receipt; **RS** still post: duration/voice unsupported. Receipt absence is **missing**, mismatched actual requirements are **differs**, fully evidenced requirements are **matches**, and an unavailable compiler/requirement is **unsupported**. The table's conditional verdict deliberately remains **missing** until an actual affected-path receipt qualifies it; static code inspection is not a production canary.

| Format | Audio | Natural source (P) | Best parts | Phone (D/G) | Cloud (V/G) | Verifier / audit verdict |
| --- | --- | --- | --- | --- | --- | --- |
| montage | licensed_music | F → S when section frozen | B/A | M | C | R/RA; conditional, receipt missing |
| montage | original_audio | F; continuous voice → T | B/T | M | C; pinned audio needs guided | R/RT; conditional, receipt missing |
| montage | voiceover | V | V | MV | CV | RV; conditional, receipt missing |
| montage | user_song | S | A | MS | U | RA phone missing; cloud unsupported |
| day_vlog | licensed_music | F → S when section frozen | B/A | M | C | R/RA; conditional, receipt missing |
| day_vlog | original_audio | F; continuous voice → T | B/T | M | C; pinned audio needs guided | R/RT; conditional, receipt missing |
| day_vlog | voiceover | V | V | MV | CV | RV; conditional, receipt missing |
| day_vlog | user_song | S | A | MS | U | RA phone missing; cloud unsupported |
| single_hero | licensed_music | F → S when section frozen | B/A | M | C | R/RA; conditional, receipt missing |
| single_hero | original_audio | F; continuous voice → T | B/T | M | C; pinned audio needs guided | R/RT; conditional, receipt missing |
| single_hero | voiceover | V | V | MV | CV | RV; conditional, receipt missing |
| single_hero | user_song | S | A | MS | U | RA phone missing; cloud unsupported |
| talking_head | licensed_music | F → S when section frozen | B/A | U declared format | C if resolved route | R/RA cloud missing; phone unsupported |
| talking_head | original_audio | T | T | U declared format | CT | RT cloud missing; phone unsupported |
| talking_head | voiceover | V | V | U declared format | CV | RV cloud missing; phone unsupported |
| talking_head | user_song | S | A | U declared format | U | Unsupported |
| subtitled | licensed_music | F → S when section frozen | B/A; speech compiler keeps take | ST; requested mix must be evidenced | CT; requested mix must be evidenced | R/RA/RT; conditional, receipt missing |
| subtitled | original_audio | T | T | ST | CT | RT; conditional, receipt missing |
| subtitled | voiceover | V | V | U recorded VO on subtitled | CV if resolved narration route | RV cloud missing; phone unsupported |
| subtitled | user_song | S | A | U user-song family | U | Unsupported |
| narrated | licensed_music | F → S when section frozen | B/A; self-narration keeps take | NT; requested mix must be evidenced | C/CT | R/RA/RT; conditional, receipt missing |
| narrated | original_audio | T | T | NT | CT | RT; conditional, receipt missing |
| narrated | voiceover | V | V | NV | CV | RV; conditional, receipt missing |
| narrated | user_song | S | A | U user-song family | U | Unsupported |
| narrated_planned | licensed_music | F → S when section frozen | B/A; self-narration keeps take | NT; requested mix must be evidenced | C/CT | R/RA/RT; conditional, receipt missing |
| narrated_planned | original_audio | T | T | NT | CT | RT; conditional, receipt missing |
| narrated_planned | voiceover | V | V | NV | CV | RV; conditional, receipt missing |
| narrated_planned | user_song | S | A | U user-song family | U | Unsupported |
| narrated_ready | licensed_music | F → S when section frozen | B/A; self-narration keeps take | NT; requested mix must be evidenced | C/CT | R/RA/RT; conditional, receipt missing |
| narrated_ready | original_audio | T | T | NT | CT | RT; conditional, receipt missing |
| narrated_ready | voiceover | V | V | NV | CV | RV; conditional, receipt missing |
| narrated_ready | user_song | S | A | U user-song family | U | Unsupported |
| slides | licensed_music | F declared; S if section frozen | Still selection; no timed export | U | SL | RS; duration unsupported |
| slides | original_audio | F declared | Still selection; no timed export | U | SL | RS; duration/audio unsupported |
| slides | voiceover | V declared | Still selection; no timed export | U | SL; VO requirement declined | RS; voice unsupported |
| slides | user_song | S declared | No compiler | U | U | Unsupported |

## Budget, durable choice, and compatibility

Silent best parts cap at 120 seconds. Every included clip has at least 0.1 seconds. Fair allocation redistributes unused capacity; valid moments rank by energy and earliest start breaks ties. Missing analysis produces a disclosed deterministic fallback receipt. `use_all` may grow to the cap; short sources, coverage above capacity, more than 80 portable sources, and cap conflicts produce a focused constraint question. Readability preferences (ordinary 0.8-second cuts and 1.2-second image holds) do not become creator intent. The portable compiler minimum is 3 seconds; a target already within ±10% of that minimum does not need a question.

Mixed-media hold limits bound allocation separately from physical source duration, preserving valid late video coordinates. Single-hero edits require one hero and one to three cutaways, at most four sources, with at least 60% hero duration and a 3-second minimum hero. The inclusion floor may raise a creator's 3-second target to 3.1 seconds for one cutaway while retaining the original target, within ±10%. A source count, order, or duration that cannot satisfy this composition enters the durable choice gate.

The approved `length_plan` persists exact windows, capacities, source storage identity, generation, selection reasons, and analysis provenance. Phone unified/voiceover and cloud classic/unified consume those windows; an authored assembly override retains its own approved timing. No compiler silently chooses a new best-parts window for the same approved plan. `read_length_plan` rejects stale identity, generation, duration, and incomplete coverage. Speech continuity and song placement remain separate constraints.

An infeasible creator length asks once, naming the requested duration, natural owner, and achievable option. Scoped answer digests include the relevant source/pool/owner inputs, survive save/reload, and invalidate only when those inputs change. No auto-default dispatch is allowed while that gate is unanswered. Accepted timing answers are authoritative; derived durations stay notes.

`creator_length_policy` and per-contract `creator_length_policies` live beside the stored v1 render contract. They bind owner, target, digest, generation, and check mode without changing the old wire schema or digest. Missing state on historical contracts retains legacy verification. New owner declarations are mandatory when edit formats/audio sources expand; the format/audio matrix regression checks that declaration.

## Resumable evidence and open release requirements

| Issue | Code and evidence | Remaining completion evidence |
| --- | --- | --- |
| KRI-493 | This 36-pair phone/cloud audit with source, verifier, selection, route verdict | Deployed affected-path receipt qualification |
| KRI-494 | P/L/V and tests/services/test_length_policy.py, test_length_policy_state.py | Exact deployed API/worker revision and canaries |
| KRI-495 | main_creator, drafts, contract builder, generative dispatch; provenance regressions, explicit 24 | Deployed request/approval and retry evidence |
| KRI-496 | L/G; best-parts and classic frozen-plan regressions | Export receipts for changed supported compilers |
| KRI-497 | Q; scoped answer/save-reload/stale-source tests | Affected deployed clarification journey |
| KRI-498 | tests/services/test_length_ownership_matrix.py; incident shapes; native explicit-24 export | CI and affected deployed journeys |
| KRI-499 | replay-creator-contracts, contract_check_rollout, collect-length-checks, Fly Deploy proof guard | Log-only deploy, actual 24 continuous clean hours, per-path canaries, enforce promotion and verification |

The replay packet contains all 23 complete available captured successful snapshots (20 device, 3 cloud), bounded by the latest-100 rule. Missing required replay inputs do not count as passing. Inputs are server-minimized with source hashes; creator text is masked. This establishes structural contract/route behavior, not original visual/text equality. Actual production incident shapes `887b683a`, `30f1c7d2`, and `e1c5f89e` are separately captured with timestamps, hashes, and the `no_creator_payload` qualification in `scripts/ci/fixtures/kri492-incident-shapes.json`.

The explicit-24 native journey has exported successfully through the actual simulator renderer. It is one bounded native proof. No PR, production deploy, all-path canary pass, 24-hour window, or enforcement promotion is claimed here. New duration checks start log-only; only measured contiguous coverage, complete replay, and exact-revision export canaries qualify promotion. The operator verifies fleet/log provenance; the collector validates and saves sanitized counts, timestamps, and revisions, never raw creator logs. The Fly workflow validates promotion evidence before mutation. All required work remains in KRI-492–499.
