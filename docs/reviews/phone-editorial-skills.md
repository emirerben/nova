# Phone editorial skills

## Acceptance fixed before implementation

Related work is KRI-215 and KRI-730. Neither ticket closes with this change.
The creator asks for meaningful-moment selection and format-specific story skills,
starting with evaluation, focused on the iPhone app.

Expected behavior is that phone planners receive bounded editorial procedures at
actual model-call boundaries. Selection favors evidence for the requested action
and complete spoken meaning over energy, keyword overlap, or account themes.
Story decisions follow the current format and preserve explicit source selection,
order, duration, audio, and approved text. Skills grant no rendering capability.

Failure modes include wrong but visually busy footage, a requested action late in
a long take, quiet footage discarded for low energy, an absent action guessed from
a related subject, uncertain footage treated as proven, negation cut from speech,
translated quotations, forced narrative over a visual montage, reordered approved
media, invented overlays, and slide media dropped for a cleaner story.

## Evaluation design

Freeze hand-authored synthetic inputs, expected decisions, and plausible wrong
responses before changing their production consumers. Use existing real Agent.run
and parse boundaries with replayed provider transport. The corpus checks request
selection, late-action source membership, complete quotations, and format constraints.
Each wrong response must parse and pass the ordinary structural floor, yet fail
the independent expected decision. This checks the evaluator's ability to reject
wrong content rather than merely rejecting malformed JSON.

The sources are synthetic descriptions and timed words, not inspected source
videos. Recorded responses are authored examples, not model generations. Cases
are development fixtures, not held-out evidence. A passing replay proves prompt
wiring, schema compatibility and evaluator discrimination. It proves no model
quality improvement, visual grounding in actual bytes, or native export success.
No paid provider or judge calls are authorized by this implementation.

## Architecture decision

Keep the existing allowlisted load_skill(name) interface. Add meaningful-moment
and extend edit-story with format-specific decisions. Reuse current schemas and
agents. Compare this with a new skill router plus format-specific files: that
alternative adds routing and version ownership without a new runtime decision.
The existing agents already know their format or role, so local loading is enough.

Main Creator chooses strategy within the phone capability manifest. The resolver
chooses clip membership from shared records, not source timestamps. Speech excerpt
planning chooses verbatim passages; workers ground their timing. Phone narrated alignment
places each clip at a supplied word ID while retaining locked order and pins.
Slide composition keeps every input asset. The deterministic phone montage compiler has no model prompt
and is unchanged. No web editor implementation or browser repair is in scope.

## Verification and remaining gaps

The first evaluation-only commit precedes runtime changes. It has 27 passing
checks and 13 expected skill-wiring failures. A later phone caller trace establishes
that narration alignment, rather than the cloud-only storyboard, is the phone
consumer. Its replacement cases are frozen before changing that consumer. That
baseline has 40 passing checks and three expected alignment-wiring failures.

The final corpus contains 14 synthetic cases. Each has a positive example and a
structurally valid wrong-content control. Cases cover quiet and late actions,
absent and uncertain matches, complete and Turkish speech, continuous voice,
visual montage, Talking, narrated word cues and pins, and mixed-media slide order.
The late-action resolver case establishes source membership only. No new agent
can select exact source intervals that its input and output contracts do not expose.

Run from the task worktree:

```bash
bash scripts/check-api.sh test tests/evals/test_phone_editorial_skills.py -q
bash scripts/check-api.sh test tests/evals/ tests/agents/test_main_creator_agent.py tests/agents/test_main_creator_user_song.py tests/agents/test_clip_request_resolver.py tests/agents/test_narrated_clip_alignment.py tests/agents/test_slide_post_composer.py tests/services/test_phone_speech_montage_job.py tests/pipeline/test_phone_narrated_plan.py tests/tasks/test_phone_voiceover_requested_edits.py -q
python3 scripts/reliability-gates.py run --changed --log .dev/editorial-reliability.log
make ios-verify
```

The final corpus passes 43 checks. Full free replay plus affected-agent and phone
backend checks pass 2486 tests, with 16 skips and 7 expected failures. Selected
reliability gates pass 844 checks with no environment skips or errors. The iPhone
build succeeds; its unit suite executes 1937 tests with 26 skips and no failures.
The broad local UI run is intentionally stopped after the build and unit phase.
The full make ios-verify command is therefore incomplete, not passed. Required
iPhone UI coverage remains selected in CI and pending. These native checks exercise the existing app,
not fresh model-generated edits under the new backend prompts.

Existing base-prompt hashes remain unchanged beneath the explicit skill prefix.
Both skills validate. All changed paths have a CI journey. The original selector
run preceded the final phone-aligner mapping. Hosted CI caught its stale exact
journey-list expectation. The repaired expectation includes the editorial journey
and retains the native-runner checks. The full CI policy command passes 121 tests
after this repair, including the actual Git-diff selection cases.
The new mapping tests actual phone alignment and the existing requested-voiceover
recipe path. Other mandatory CI selections remain intact.

The synthetic corpus also supports the existing live-eval interface when explicitly
approved under its development ledger. Its exact expected choices are development
constraints, not a general measure of subjective editing quality. No paid run was
performed. Default CI uses replay only. Prompt versions are bumped on every changed
agent. CI selects the corpus when its runtime consumers or skills change.

The phone narration worker deliberately skips the cloud-only storyboard. Its
alignment agent owns clip placements at supplied word IDs. Locked order and pins
remain hard constraints; the existing policy may override advisory described
sequences when narration reaches clips in another order. No parser, fallback,
compiler, native renderer, or additional model-call behavior changes.

Current-agent review covers skill scope, real phone callers, source identity,
prompt-version ownership, quote preservation, capability and approval boundaries,
and wrong-content controls. The user prohibits delegation, so no independent
agent review is claimed. No application comments or suppressions are added.

KRI-215 owns live creative comparison; KRI-730 owns retrieval and source-inspection
gaps. Native output verification of fresh model choices and production rollout are
separate from this bounded integration. No flags, models, spend limits, endpoints,
renderer behavior, approval rules, or output schemas change.

Experience First keeps the scope on actual iPhone callers. Sequence Work into
Verifiable Units puts evaluation cases before each new runtime consumer. Test
Behavior, Not Implementation requires wrong decisions to fail even when their
response shapes are valid. Prove It Works separates replay, native compatibility,
and unverified creative quality.

## Delivery acceptance

The creator authorizes fixing CI, merging PR 1821, and deployment. All required
checks must pass for the final head. Deploy only through Fly Deploy. Verify the
managed fleet revision and digest, health, and actual prompt construction on the
API and worker images without a paid model call. A reachable API alone does not
prove skill loading. No client protocol, binary, capability flag, or output schema
changes are required for this backend update. Creative quality remains separate.
