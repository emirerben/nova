# Editor AI activity feedback

Acceptance, recorded before implementation:

- Sending a prompt and waiting for its correlated turn produces a compact, readable activity control in the video editor. Tapping it still opens the conversation.
- The existing sparkle stays in place. A soft, slowly shifting edge uses Kria's existing sky, lilac and butter palette and glass surface. The video, timeline and authoring state are not animation inputs.
- Sending, thinking, preparing a preview and rendering have distinct, honest copy. A terminal reply (including no-op/failure/cancellation) clears turn feedback through the existing chat settlement rules. No percentage, success timer or extra generation request is added.
- Activity belonging to another project is rejected. A newer request replaces the displayed identity. Dismissal, a covering conversation sheet, backgrounding and Reduce Motion remove the continuous animation. Return uses current state.
- Reduce Motion presents a static edge and text. Reduce Transparency uses opaque paper and a static edge. No pulse, flash or completion celebration is used.

Failure cases to verify: missing request identity; stale project; sending then accepted then terminal; repeated requests; interrupted/superseded request; rendering then local readiness; connection/thermal/storage waits; failed/cancelled render; preview preparation then failure; hiding and returning; reduced accessibility settings; button still opens chat; video clock/player and unsaved edits survive.

Reference: [official Type to Siri on Mac clip](https://www.apple.com/newsroom/videos/videos-2024/autoplay/2024/10/apple-intelligence-type-to-siri/large_2x.mp4), inspected at 5–8 seconds. The reference contributes the soft moving edge around a small glass surface, adapted to Kria's palette. The saved Kadikoy editor screenshot was inspected locally. These are visual references, not application validation.

Ownership: no changes to `NativeEditorSession`, player construction, request ownership, saving, generation or rendering. Cunda #2126 owns session recovery and validated preview handoff. Integration must retain those semantics. This change observes existing state only.

Validation on the local candidate:

- `git diff --check`: passed.
- `xcrun swiftc -frontend -parse` on all six changed/new Swift files: passed. This is syntax parsing, not type checking or an app build.
- `python3 -m unittest discover -s scripts/ios/tests -p test_ui_tests.py -v`: 26 passed, 18.044 seconds. These exercise test selection/inventory, not the native UI.
- New native paths resolve through the existing editor journey mapping. The three new UI cases are in the creation inventory; no test is removed.
- Five native state tests and three UI tests are added but have never run. The UI fixtures use existing offline HTTP transport, including delayed submission, rejected submission and a held turn. They capture normal, reduced-motion and opaque surfaces and exercise repeated prompts and background/return.

Pending before merge: serialized native lane, final-head build/type checking and native state/UI execution; inspection of working and reduced-motion screenshots or recording; integration with Cunda and speed work on then-current main; approved tracking issue and full PR evidence gate. No simulator, physical device, live model or deployment check has run. No performance improvement is claimed.
