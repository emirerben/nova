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
planning chooses verbatim passages; workers ground their timing. Narrated storyboard
planning matches timed voiceover beats to available clips. Slide composition keeps
every input asset. The deterministic phone montage compiler has no model prompt
and is unchanged. No web editor implementation or browser repair is in scope.

## Verification and remaining gaps

Pending. KRI-215 owns live creative comparison; KRI-730 owns retrieval and source
inspection gaps. Native output verification and production rollout are separate
from this bounded prompt integration. No flags, models, spend limits, endpoints,
renderer behavior, approval rules, or output schemas change.
