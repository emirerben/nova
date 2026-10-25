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
responses before changing production prompts. Use existing real Agent.run and
parse boundaries with replayed provider transport. The corpus checks request
selection, available source windows, complete quotations, and format constraints.
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
places each clip at a supplied word ID while retaining locked order and pins. Slide composition keeps
every input asset. The deterministic phone montage compiler has no model prompt
and is unchanged. No web editor implementation or browser repair is in scope.

## Verification and remaining gaps

The evaluation-only commit precedes the runtime integration. Its baseline has
27 passing checks and 13 expected skill-wiring failures. All 13 positive decision
examples and all 13 wrong-content controls pass their respective evaluator checks.
With the skills integrated, all 40 corpus checks pass.

The full free replay suite plus the changed-agent checks and existing phone speech
and narrated-plan checks pass 2445 tests, with 16 existing skips and 7 expected
failures. The selected reliability gates pass 619 checks with no environment
skips or errors. Both skill files validate. Existing base-prompt hashes remain
unchanged beneath the explicitly versioned skill prefix.

Run from the task worktree:

```bash
bash scripts/check-api.sh test tests/evals/test_phone_editorial_skills.py -q
bash scripts/check-api.sh test tests/evals/ tests/agents/test_main_creator_agent.py tests/agents/test_main_creator_user_song.py tests/agents/test_clip_request_resolver.py tests/agents/test_narrated_storyboard.py tests/agents/test_slide_post_composer.py tests/services/test_phone_speech_montage_job.py tests/pipeline/test_phone_narrated_plan.py -q
python3 scripts/reliability-gates.py run --changed --log .dev/editorial-reliability.log
```

The synthetic corpus runs through the existing live-eval interface when explicitly
approved under its development ledger. Its exact expected choices are development
constraints, not a general measure of subjective editing quality. No paid run was
performed. Default CI uses replay only. Prompt versions are bumped on every changed
agent. CI selects the new corpus when any of its runtime consumers or skills change.

The local iPhone verification is pending. The first sandboxed attempt could not
access simulator services and the Swift package cache. A retry with those services
available is running. This is not evidence of an application regression.

Current-agent review covers skill scope, source identity, prompt-version ownership,
quote preservation, capability and approval boundaries, and the wrong-content
controls. The user prohibits delegation, so no independent agent review is claimed.
No application comments or suppressions are added.

 KRI-215 owns live creative comparison; KRI-730 owns retrieval and source
inspection gaps. Native output verification and production rollout are separate
from this bounded prompt integration. No flags, models, spend limits, endpoints,
renderer behavior, approval rules, or output schemas change.

## Phone narrated consumer correction

A final caller trace found that the phone path deliberately skips the cloud-only
NarratedStoryboardAgent. That integration is removed. Replace its three synthetic
cases with spoken-cue, locked-order, and pinned-ending cases for the actual phone
NarratedClipAlignmentAgent before changing that agent. Keep a separate late-action
resolver case, explicitly limited to source membership rather than exact windows.
The corpus now has 14 cases. This does not add a new model call to any phone job.

The phone alignment contract owns every supplied clip and returns start_word_id,
not source timestamps. Its locked order and pins are hard constraints. Its existing
policy may override advisory described groups when narration reaches them in a
different order; the new guidance must preserve that distinction. No fallback or
parser behavior changes.
