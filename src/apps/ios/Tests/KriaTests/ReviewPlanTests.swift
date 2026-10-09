import XCTest
import KriaMediaEngine
@testable import Kria

/// Live plan & review: what the Review sheet sends and how it recovers. The server body is `extra="forbid"`, so the
/// exact keys matter; the rest pins the rules the client owns (an edit flags its section, a chip's x discards its
/// edits, a failed update leaves the creator where they were).
@MainActor
final class ReviewPlanTests: XCTestCase {
    private func json(_ value: some Encodable) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(value)) as? [String: Any])
    }

    func testScopedTurnBodyCarriesExactlyTheContractKeys() throws {
        let edit = ManualPlanEdit(kind: "rewrite_text", targetID: "guided-title-1", text: "Hello", musicLevel: nil, originalLevel: nil, musicGainDB: nil)
        let body = try json(ScopedSubmitTurnRequest(
            message: "Update: title", clientEventID: "c1", expectedThreadRevision: 7, scope: [.title, .captions], manualEdits: [edit]
        ))
        XCTAssertEqual(Set(body.keys), ["message", "client_event_id", "expected_thread_revision", "scope", "manual_edits"])
        XCTAssertEqual(body["scope"] as? [String], ["title", "captions"])
        let edits = try XCTUnwrap(body["manual_edits"] as? [[String: Any]])
        XCTAssertEqual(Set(edits[0].keys), ["kind", "target_id", "text"], "nil fields are omitted, never null")

        let bare = try json(ScopedSubmitTurnRequest(message: "m", clientEventID: "c", expectedThreadRevision: 1, scope: [.music], manualEdits: []))
        XCTAssertNil(bare["manual_edits"], "no manual edits means the key is absent")
    }

    func testAnEditFlagsItsSectionAndRevertingOrRemovingTheChipUnflagsIt() {
        var draft = ReviewPlanDraft()
        XCTAssertFalse(draft.canUpdate)
        draft.editTitle("New title", original: "Old title")
        draft.editCaption(id: "c1", text: "Hi", original: "Hello")
        draft.editMix(musicLevel: 0.2, current: PlanMixLevels(musicLevel: 0.7, originalLevel: 0.5, musicGainDB: nil))
        XCTAssertEqual(draft.scope, [.title, .captions, .music], "edits flag their sections, in display order")

        draft.editTitle("Old title", original: "Old title")
        XCTAssertEqual(draft.scope, [.captions, .music], "typing the original back removes the edit")
        draft.remove(.captions)
        XCTAssertTrue(draft.captionEdits.isEmpty, "a chip's x discards that section's edits")
        draft.toggle(.music)
        XCTAssertTrue(draft.scope.isEmpty)
        draft.toggle(.postCaption)
        XCTAssertTrue(draft.scope.isEmpty, "a post caption can never be flagged")
    }

    func testManualEditsAndTheMessage() {
        var draft = ReviewPlanDraft()
        draft.editTitle("B", original: "A")
        draft.editCaption(id: "c2", text: "x", original: "y")
        draft.editMix(originalLevel: 0.1, current: PlanMixLevels(musicLevel: nil, originalLevel: 0.5, musicGainDB: nil))
        let edits = draft.manualEdits(titleBarID: "bar-1", captionOrder: ["c1", "c2"])
        XCTAssertEqual(edits.map(\.kind), ["rewrite_text", "rewrite_text", "set_mix"])
        XCTAssertEqual(edits.map(\.targetID), ["bar-1", "c2", nil])
        XCTAssertEqual(edits[2].originalLevel, 0.1)
        XCTAssertNil(edits[2].musicLevel, "only the slider that moved is sent")
        XCTAssertEqual(draft.message, "Update: title, captions, music")
        draft.prompt = "  make it calmer \n"
        XCTAssertEqual(draft.message, "make it calmer", "what the creator typed is sent verbatim, trimmed")
        XCTAssertTrue(ReviewPlanDraft().manualEdits(titleBarID: nil).isEmpty)
        var untargetable = ReviewPlanDraft(); untargetable.editTitle("B", original: "A")
        XCTAssertTrue(untargetable.manualEdits(titleBarID: nil).isEmpty, "a title without a bar id cannot be targeted")
    }

    private func snapshot(status: String = "ready", job: String = "job-1", editable: Bool = true) throws -> PlanSnapshot {
        let block: [String: Any] = ["section_id": "captions", "state": "decided", "intent": false, "skipped": false,
                                    "summary": "4 lines", "revision": 1, "changed": false, "editable": editable]
        let body: [String: Any] = ["thread_id": "t", "thread_revision": 3, "job_id": job, "status": status, "blocks": [block],
                                   "next_after_sequence": 9, "draft": ["draft_id": "d", "draft_revision": 2, "etag": "e", "can_undo": false]]
        return try JSONDecoder().decode(PlanSnapshot.self, from: JSONSerialization.data(withJSONObject: body))
    }

    func testAFailedUpdateReturnsToReviewingKeepingTheDraftAndSaysWhy() async throws {
        let first = try snapshot()
        let actions = ReviewPlanActions(
            loadSnapshot: { first },
            update: { _, _, _ in throw ReviewPlanError.declined("That would change music, which you did not flag.") },
            undoSection: { _, _, _ in true }, undoAll: { _ in }
        )
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, pollInterval: .milliseconds(5))
        await model.load()
        XCTAssertTrue(model.canChange(try XCTUnwrap(model.block(.captions))))
        model.toggle(.captions)
        model.submitUpdate()
        XCTAssertEqual(model.phase, .submitting)
        for _ in 0..<100 where model.phase != .reviewing { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(model.phase, .reviewing)
        XCTAssertEqual(model.notice, "That would change music, which you did not flag.")
        XCTAssertEqual(model.draft.scope, [.captions], "the creator's flags survive a failed update")
    }

    func testASuccessfulUpdateFollowsTheNewJobUntilItIsReady() async throws {
        var reads = 0
        let actions = ReviewPlanActions(
            loadSnapshot: {
                reads += 1
                // First read is the plan as it was; then the update renders; then the new job is ready.
                if reads == 1 { return try self.snapshot(job: "job-1") }
                if reads < 4 { return try self.snapshot(status: "updating", job: "job-2", editable: false) }
                return try self.snapshot(job: "job-2")
            },
            update: { _, _, _ in }, undoSection: { _, _, _ in true }, undoAll: { _ in }
        )
        let model = ReviewPlanModel(seed: [], initialFlag: .captions, actions: actions, pollInterval: .milliseconds(5))
        await model.load()
        XCTAssertEqual(model.draft.scope, [.captions], "the card whose Change was tapped starts flagged")
        model.submitUpdate()
        for _ in 0..<200 where !(model.phase == .reviewing && reads >= 4) { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(model.phase, .reviewing)
        XCTAssertEqual(model.snapshot?.jobID, "job-2")
        XCTAssertNil(model.notice)
        XCTAssertTrue(model.draft.isEmpty, "the draft is spent once the update is sent")
    }

    private func changedSnapshot(status: String = "ready", job: String, withDraft: Bool = true) throws -> PlanSnapshot {
        let before: [String: Any] = ["revision": 1, "job_id": "job-1", "summary": "4 lines", "skipped": false]
        let block: [String: Any] = ["section_id": "captions", "state": "decided", "intent": false, "skipped": false,
                                    "summary": "4 lines, shorter", "revision": 2, "changed": true, "previous": before, "editable": true]
        var body: [String: Any] = ["thread_id": "t", "thread_revision": 3, "job_id": job, "status": status, "blocks": [block],
                                   "scope": ["captions"], "next_after_sequence": 9]
        if withDraft { body["draft"] = ["draft_id": "d", "draft_revision": 2, "etag": "e", "can_undo": true] }
        return try JSONDecoder().decode(PlanSnapshot.self, from: JSONSerialization.data(withJSONObject: body))
    }

    func testReopeningMidUpdateFollowsTheRenderToTheEnd() async throws {
        var reads = 0
        let actions = ReviewPlanActions(
            loadSnapshot: {
                reads += 1
                return try self.changedSnapshot(status: reads < 4 ? "updating" : "ready", job: "job-2")
            },
            update: { _, _, _ in }, undoSection: { _, _, _ in true }, undoAll: { _ in }
        )
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, pollInterval: .milliseconds(5))
        await model.load()
        XCTAssertTrue(model.isUpdating, "the render was already running when the sheet opened")
        for _ in 0..<200 where model.isUpdating { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertFalse(model.isUpdating, "nothing else polls, so the sheet must follow the render itself")
        XCTAssertTrue(model.showsUpdated)
    }

    func testARestoreThatQueuesNoRenderSettlesAtOnceInsteadOfWaitingForANewJob() async throws {
        let actions = ReviewPlanActions(
            loadSnapshot: { try self.changedSnapshot(job: "job-2") },
            update: { _, _, _ in }, undoSection: { _, _, _ in false }, undoAll: { _ in }
        )
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, pollInterval: .seconds(30))
        await model.load()
        model.undo(.captions)
        for _ in 0..<100 where model.phase != .reviewing { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(model.phase, .reviewing, "no successor turn means no new job to wait for")
        XCTAssertNil(model.notice)
    }

    func testUndoIsOfferedOnlyWhenTheServerSentADraftHead() async throws {
        for (withDraft, expected) in [(true, true), (false, false)] {
            let actions = ReviewPlanActions(
                loadSnapshot: { try self.changedSnapshot(job: "job-2", withDraft: withDraft) },
                update: { _, _, _ in }, undoSection: { _, _, _ in true }, undoAll: { _ in }
            )
            let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, pollInterval: .milliseconds(5))
            await model.load()
            XCTAssertEqual(model.canUndo(try XCTUnwrap(model.block(.captions))), expected)
            model.undo(.captions)
            if !withDraft { XCTAssertEqual(model.phase, .reviewing, "a missing draft head must not start a phantom undo") }
        }
    }
}
