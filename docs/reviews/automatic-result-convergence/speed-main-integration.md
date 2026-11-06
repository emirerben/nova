# Controlled integration with landed speed and current main

Local merge parents: reviewed mapping checkpoint60437739121c70166ff855af700af83b9197c93e and verified main4bfa30f34fe450d4b5ea8dd15a1fe1796b021d13. Main includes landed speeddef205781d526765f75df03720fb644341f1cd73 and already-landed Cunda70bff5f3738ca8f6873e3a85ddd9fbad374ada49. No unpublished reset/model branch was stacked. Final merge SHA/tree is returned in the handoff; the staged source hashes below pin this evidence before the commit.

Only two files conflicted. NativeEditorSession composes the parallel committed-variant read with device startup, retaining whole-Save attempt ID, save-journal ID, editor lifetime/target/generation checks in the child callback and after every awaited branch, plus cancellation cleanup. Incoming pendingDeviceRenderGeneration and coherent saved-file receipt checks remain. DeviceRenderSessions composes retry task cancellation/pacing with unresolved-reread state, retains all parked safe-resume helpers, and clears accepted binding/old presentation before request replacement awaits. Saved receipt additionally waits for the retained underlying transport, which can outlive a logical ticket after Stop. This preserves the cancellation-ignoring-read guarantee rather than publishing the old file during unresolved status work.

Auto merges preserve pendingNewChat presentation/model and delayed-create fixture alongside owner/environment-scoped original AI recovery; no lost-result branch was replaced. Main API contract/source-replacement/caption/mute changes remain, as do our permanent claim/write fences. Independent bounded read-only integration review found no concrete source blocker. It ran no tests and does not establish compilation or native correctness.

Permitted execution on the combined source:168 CI Python contracts passed42.518s (`/tmp/nova-auto-integrated-ci-contracts.log`);129 offline backend checks passed3.37s (`/tmp/nova-auto-integrated-offline.log`, JUnit `/tmp/nova-auto-integrated-offline.xml`) using fake keys, no dotenv, denied sockets. The129 are the six previously selected AI files plus the four saved-song fixture cases, not additional disjoint proof beyond earlier125/4. Saved-song API receipt verifies all3named selectors. Generation PostgreSQL collection remains blocked by unavailable loopback5432 and was not counted as passing. No service was started or local migration run.

Native source selection now781 app methods in18classes (incoming NativeEditorSession294, ProjectActions35),39 package methods and8UI selectors; all are source candidates, not compilation/discovery/execution. All selected file fingerprints were refreshed to this combined source. Named journey requirements include coherent replacement receipts, failed status after new Save, and parallel-read Save startup; native-units must verify actual Passed entries. Incoming speed's earlier hosted/native results remain bound to its own source and do not prove this combination.

Formal handoff: SavedRenderReceipt companion/skill/verifier are inherited unchanged from main. Reconcile/apply/reset/receipt now compose our retained-transport/parked-target semantics with main's accepted binding/reread gates; Native Save/refreshDeviceRender/savedEditDeviceRenderFile compose parallel reads with attempt/lifetime guards. Parent retains shared model ownership. No coverage hash was resynced and no Java/TLC/model/native/package/simulator/device process launched. Default/shared/companion correspondence and full aggregate/Linux/physical rollout gates remain pending.

The old hosted3d run also reports API failures outside the mapping gate. Logs were inspected, not rerun. Some reflect our keyed-write helper reading FakeJob.id outside claim context, strict process scratch DB guard rejecting CI's disposable database, an outdated render-entry pin/schema head expectation, public editor parity entries and asynchronous sticker DB mocks. They require separate reviewed fixes/evidence; the mapping/integration commit does not claim to repair them. No red test or skipped descendant is green. KRI-935 remains Done/archived; active tracking belongs to parent coordination, no ticket created or reopened. No PR merge, rollout, operational migration or flag change occurred.

Staged integration source SHA256 (later follow-ups must retain their own evidence):

```json
{
  "src/apps/ios/Kria/Core/DeviceRenderSessions.swift": "b88f3c7158d67a1623728d5fd9a4abe3fcce5f88c1043c7c82412a992da10eec",
  "src/apps/ios/Kria/Features/NativeEditorSession.swift": "69505b872be6542182479999224bb7a2856b1df21a3d786a36e1706432d92a3d",
  "src/apps/ios/Kria/Features/ChatWorkspaceView.swift": "7d66636be9a7dedf9c57387375de933824ad3c2d1f0f0c070efb46067335fa89",
  "src/apps/ios/Kria/Features/ChatUITestTransport.swift": "249bd1f68eac20b77eb32bdd16ea4ba8f9ec1cea9eae445f5c17e23cb206ffd0",
  "src/apps/ios/Kria/Core/AppState.swift": "1e1a06261f8ae29aa6ad9b27d26f0c09e5ecdd39683ca2c4f2db9311ef68a151",
  "src/apps/api/app/tasks/generative_build.py": "11773361ad84322898c91e84fd30f51a5e547b9d336ff61a9387e8dbcd3fe901",
  "src/apps/api/app/config.py": "1c20a551c076ee91362a086de1246af0b64df9dfe6ad31ea7410309fb53f15c8",
  "src/apps/api/app/services/slide_post_generation_delivery.py": "2525495d2f5d3b94979c9b1d447368be0310f8e32b8ca7c4a691ba78f6e3792b"
}
```
