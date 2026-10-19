# In-app, footage-grounded research

The creator can ask chat to research and explain their footage. The same path must
work during creation and as a follow-up to an existing edit. Research produces
cited evidence before captions are written. An existing edit keeps its order,
opening, audio selection and unrelated text unless the creator asks to change them.
Source links stay with the explanation and remain available in the app.

## Failure cases to prove before implementation

- Research is promised but no search runs; invented links or unsupported dates reach captions.
- A true fact about another landmark/artifact is placed over an unrelated shot.
- Generic museum footage is confidently named as a specific object without identity evidence.
- Provider grounding is absent, malformed, private-addressed, or irrelevant; an unchanged draft
  is reported as researched instead of giving an actionable limitation.
- Retrieved text issues instructions; the editor follows it or changes unrelated lanes.
- Follow-up research drops the horse-first opening, moves the car, replaces music, or loses text.
- A caption extends onto a different subject, overlaps existing copy, or has insufficient reading time.
- Repeated Save/reopen/export loses citations or changes the accepted plan.
- Search charges bypass the existing reservation/settlement/deadline controls.
- English-only detection prevents other users/languages from invoking research.
- Offline/cached fixtures are mistakenly reported as live research or physical-device proof.

## Timeline contract

1. Duration owner: the existing compiled story timeline / editor source windows; no new UI clock.
2. Ripple: research edits use existing text/timeline operations and the canonical projection.
   Research itself cannot reorder clips, alter camera/music audio, or change other lanes.
   Extending a source window uses the existing extension operation and its ripple semantics;
   continuous soundtrack source offset is retained. Existing fixed limits remain authoritative.
3. Scrub bounds: the existing canonical document duration drives all native controls.
4. Resize: no new gesture or rounding model; use existing source limits and frame rounding.
5. Undo: one researched revision is one existing editor draft/undo boundary.
6. Parity: replay the request through real planner/copilot parsing, save, phone compiler and
   native export. Prove order, caption windows, persisted sources, audio and failure controls.

## Delivery and evidence

Continue in the same task worktree, now merged with origin/main. Reuse the app's
agent cost controls and canonical editor/compiler paths. No Troya-specific rules,
hard-coded public facts, manual caption scripts or account allowlist in production code.
Use source-backed offline provider cassettes for failure controls and app journeys;
separate those from live provider checks. Install a separate QA bundle on the user's
physical iPhone, preserving the production app. No production rollout until release approval.
