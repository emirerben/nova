# Creator request-following fixes

## Product outcome

Ship the demonstrated title/placeholder fixes independently of the model-comparison experiment. Keep the production provider unchanged. A character-by-character entrance must not be mistaken for word-by-word title segmentation; universal placeholders must not require recognition of a real place or duplicate the opening hook.

## Failure modes fixed or guarded

Written before extracting the production changes on 21 October 2026:

- Existing skip logic sees a matching entrance effect and skips a requested word sequence.
- The composer changes protected placeholder copy while restyling the title.
- Splitting the title invalidates selectors that were bound before the split; a later broad style operation then fails.
- A request for placeholders on every clip is interpreted as identifying actual locations, so matching cannot resolve it.
- Describing the opening shot while supplying a hook creates a second copy of the hook as a clip caption.
- A frozen composition must replay without another provider call. Source order, clip durations and unrelated lanes must survive save/reload and compilation.

## Timeline contract

1. Duration source: the approved EditProposalSnapshot and compile_proposal_execution_plan own the picture timeline and output duration. This change supplies text operations, not another duration calculation.
2. Ripple policy: the existing editor operation compiler owns title segmentation; picture, audio, clip labels and other lanes stay unchanged. No timeline insertion or downstream ripple is introduced.
3. Scrub bounds: unchanged; editor playback and scrubbing continue using the canonical output duration.
4. Resize behavior: no resize interaction changes. Requested word intervals must remain within the opening clip; existing renderer frame rounding applies.
5. Undo behavior: no new UI transaction boundary. Composition is pinned on the snapshot and reloads as the same operation program. Stale selectors and protected copy still fail closed.
6. Preview/render parity: the creation-composition replay checks save/reload and canonical compilation; run the existing editor timeline gate and a local native proxy export from the frozen real-request composition. Native desktop evidence does not establish physical-device or deployed-production success.

## Evidence

Extracted onto freshly fetched main at `857ab6738`, without the comparison adapters, provider routing, capture/storage changes or evaluation infrastructure. The current-main KRI-767 copy-move behavior is preserved.

- Before implementation: the word-sequence regression fails on main because the composer is skipped (`0` calls where the requested sequence needs one).
- Focused composition/placeholder checks: **64 passed**.
- Existing confirmed-copy move guards: **20 passed**.
- Full offline agent replay suite: **2,066 passed, 15 skipped, 7 expected failures**.
- `make verify-editor-timeline`: **209 frontend checks, 28 desktop browser cases, 42 mobile browser cases and 583 backend checks passed**.
- Current-worktree native engine rebuilt, then the frozen real-request composition replayed through canonical compilation and local export. The **54.333-second** proxy video fully decodes; the **13-word** title, **10 protected placeholders**, white Inter/bottom-left placement and opening-clip word bounds all pass. Sampled title and placeholder frames were visually inspected.
- New paid model calls: **zero**. Real source permissions were rechecked before reuse. Raw requests, source media, rendered media and provider outputs remain private and outside Git.

Repeat the public offline checks from this worktree with:

```sh
bash scripts/check-api.sh test tests/evals/request_following/test_creation_composition.py tests/services/test_kri520_clip_intent_copy.py tests/services/test_creation_text_copy_guard.py -q
bash scripts/check-api.sh test tests/evals/ -q
make verify-editor-timeline
bash scripts/preship-check.sh
```

## Limits and rollout

The real export replays an already frozen composition. It is evidence for current-code compilation/rendering, not a fresh model decision, an independent holdout, physical-device execution or a deployed production result. Prompt guidance remains probabilistic; deterministic copy/source/compiler guards are unchanged.

An animated title with a live brief may now invoke the text-composition pass where the old entrance-only shortcut skipped it. That adds the existing composer cost/latency for that affected path. Pinned compositions still replay without another model call. No provider selection, schema migration, renderer change or production feature-flag change is included.

The pre-PR gate passes: scoped lint/format, no main drift, release-metadata ownership, **907 mapped incident checks**, and complete mapping to **four CI journeys**. CI results for the pushed head and production deployment remain separate evidence.
