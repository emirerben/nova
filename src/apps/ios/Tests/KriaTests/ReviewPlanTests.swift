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

    private func snapshot(status: String = "ready", job: String = "job-1", editable: Bool = true, turn: String? = nil, sequence: Int = 9, thread: String = "t") throws -> PlanSnapshot {
        let block: [String: Any] = ["section_id": "captions", "state": "decided", "intent": false, "skipped": false,
                                    "summary": "4 lines", "revision": 1, "changed": false, "editable": editable]
        var body: [String: Any] = ["thread_id": thread, "thread_revision": 3, "job_id": job, "status": status, "blocks": [block],
                                   "next_after_sequence": sequence, "draft": ["draft_id": "d", "draft_revision": 2, "etag": "e", "can_undo": false]]
        if let turn { body["turn_id"] = turn }
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

    func testSameJobUpdateSettlesOnlyAfterTheNewTurnIsReady() async throws {
        var reads = 0
        let oldReadyRead = expectation(description: "old ready remains pending")
        let newReadyRead = expectation(description: "same job new turn becomes ready")
        var releaseOldRead: CheckedContinuation<Void, Never>?
        let actions = ReviewPlanActions(
            loadSnapshot: {
                reads += 1
                if reads == 1 { return try self.snapshot(turn: "turn-1") }
                if reads == 2 {
                    oldReadyRead.fulfill()
                    await withCheckedContinuation { releaseOldRead = $0 }
                    return try self.snapshot(turn: "turn-1")
                }
                if reads == 3 { return try self.snapshot(status: "updating", turn: "turn-2", sequence: 10) }
                if reads == 4 { newReadyRead.fulfill() }
                return try self.snapshot(turn: "turn-2", sequence: 11)
            },
            update: { _, _, _ in }, undoSection: { _, _, _ in true }, undoAll: { _ in }
        )
        let model = ReviewPlanModel(seed: [], initialFlag: .captions, actions: actions,
                                    pollInterval: .milliseconds(1), giveUpAfter: .seconds(1))
        await model.load()
        model.submitUpdate()
        await fulfillment(of: [oldReadyRead], timeout: 2)
        XCTAssertEqual(model.phase, .updating, "the old ready snapshot cannot finish the update")
        releaseOldRead?.resume()
        await fulfillment(of: [newReadyRead], timeout: 2)
        // Give the read's caller its MainActor turn to apply the response.
        for _ in 0..<100 where model.phase != .reviewing { await Task.yield() }
        XCTAssertEqual(model.phase, .reviewing, "an existing job can finish a new turn without a refresh tap")
        XCTAssertEqual(model.snapshot?.turnID, "turn-2")
        XCTAssertNil(model.notice)
        model.cancelFollowing()
    }

    func testLateSnapshotCannotReplaceANewerReviewResult() async throws {
        var reads = 0
        var activeReads = 0
        var maximumActiveReads = 0
        let slowReadStarted = expectation(description: "old read suspended")
        var releaseRead: CheckedContinuation<Void, Never>?
        let actions = ReviewPlanActions(
            loadSnapshot: {
                activeReads += 1
                maximumActiveReads = max(maximumActiveReads, activeReads)
                defer { activeReads -= 1 }
                reads += 1
                if reads == 2 {
                    slowReadStarted.fulfill()
                    await withCheckedContinuation { releaseRead = $0 }
                    return try self.snapshot(turn: "turn-1", sequence: 9)
                }
                return try self.snapshot(turn: reads == 1 ? "turn-1" : "turn-2", sequence: reads == 1 ? 9 : 11)
            },
            update: { _, _, _ in }, undoSection: { _, _, _ in true }, undoAll: { _ in }
        )
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions)
        await model.load()
        let old = Task { await model.refresh() }
        await fulfillment(of: [slowReadStarted], timeout: 2)
        let newest = Task { await model.refresh() }
        for _ in 0..<10 { await Task.yield() }
        XCTAssertEqual(reads, 2, "a newer refresh waits for the sole in-flight reader")
        releaseRead?.resume()
        await old.value
        await newest.value
        XCTAssertEqual(maximumActiveReads, 1)
        XCTAssertEqual(model.snapshot?.turnID, "turn-2", "the older response must not undo automatic convergence")
        XCTAssertEqual(model.snapshot?.nextAfterSequence, 11)
        model.cancelFollowing()
    }

    func testExhaustedReviewReadsContinueSlowlyUntilNewSameJobTurnIsReady() async throws {
        var reads = 0, submissions = 0
        var now = ContinuousClock.now
        var delays: [Duration] = []
        var model: ReviewPlanModel!
        let actions = ReviewPlanActions(loadSnapshot: {
            reads += 1
            if reads == 1 { return try self.snapshot(turn: "old") }
            if reads <= 7 { throw APIError.offline }
            if reads == 8 {
                XCTAssertEqual(model.phase, .updating)
                XCTAssertTrue(model.notice?.contains("automatically") == true)
                return try self.snapshot(turn: "old")
            }
            XCTAssertEqual(model.phase, .updating, "an old ready plan cannot settle an exhausted follower")
            return try self.snapshot(turn: "new", sequence: 10)
        }, update: { _, _, _ in submissions += 1 }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        model = ReviewPlanModel(seed: [], initialFlag: .captions, actions: actions,
            pollInterval: .seconds(1), recoveryInterval: .seconds(30), readNow: { now }, readSleep: { delay in
                delays.append(delay); now = now.advanced(by: delay); await Task.yield()
            })
        await model.load()
        model.submitUpdate()
        for _ in 0..<500 where model.phase != .reviewing { await Task.yield() }
        XCTAssertEqual(model.snapshot?.turnID, "new")
        XCTAssertEqual(model.phase, .reviewing)
        XCTAssertTrue(delays.contains(.seconds(30)))
        XCTAssertEqual(submissions, 1, "recovery only repeats GET")
        XCTAssertNil(model.notice)
        model.cancelFollowing()
    }

    func testReviewRetryAfterSurvivesAnotherRefreshAndCannotBeShortened() async throws {
        var reads = 0
        var now = ContinuousClock.now
        let started = now
        var delays: [Duration] = []
        let actions = ReviewPlanActions(loadSnapshot: {
            reads += 1
            if reads == 1 { throw APIError.requestFailed(status: 429, detail: .init("Wait", retryAfter: 60)) }
            XCTAssertGreaterThanOrEqual(now - started, .seconds(60))
            return try self.snapshot()
        }, update: { _, _, _ in XCTFail("read recovery must not submit") }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, readNow: { now }, readSleep: { delay in
            delays.append(delay); now = now.advanced(by: delay); await Task.yield()
        })
        await model.refresh()
        now = started.advanced(by: .seconds(3))
        await model.refresh()
        XCTAssertEqual(reads, 2)
        XCTAssertEqual(delays, [.seconds(57)])
        XCTAssertEqual(model.status, .ready)
        model.cancelFollowing()
    }

    func testShutdownFencesSuspendedReviewReadAndStopsAutomaticRecovery() async throws {
        var reads = 0
        var release: CheckedContinuation<Void, Never>?
        let entered = expectation(description: "snapshot in flight")
        let actions = ReviewPlanActions(loadSnapshot: {
            reads += 1
            entered.fulfill()
            await withCheckedContinuation { release = $0 }
            return try self.snapshot(turn: "late")
        }, update: { _, _, _ in XCTFail("unexpected mutation") }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions)
        let loading = Task { await model.load() }
        await fulfillment(of: [entered], timeout: 2)
        model.cancelFollowing()
        release?.resume()
        await loading.value
        await model.refresh()
        XCTAssertNil(model.snapshot)
        XCTAssertEqual(reads, 1)
    }

    func testSeededReviewAutomaticallyRecoversItsInitialFailedRead() async throws {
        let seed = try snapshot()
        var reads = 0
        let actions = ReviewPlanActions(loadSnapshot: {
            reads += 1
            if reads == 1 { throw APIError.offline }
            return try self.snapshot(turn: "latest", sequence: 10)
        }, update: { _, _, _ in XCTFail("read only") }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        let model = ReviewPlanModel(seed: seed.blocks, initialFlag: nil, actions: actions,
            readSleep: { _ in await Task.yield() })
        await model.load()
        for _ in 0..<100 where model.snapshot?.turnID == nil { await Task.yield() }
        XCTAssertEqual(model.snapshot?.turnID, "latest")
        XCTAssertEqual(reads, 2)
        model.cancelFollowing()
    }

    func testReviewDeadlineSlowsPollingAndVisibleSignalSettlesWithoutAnotherSubmission() async throws {
        var now = ContinuousClock.now
        var ready = false, submissions = 0, reads = 0
        let paused = expectation(description: "slow fallback scheduled")
        var release: CheckedContinuation<Void, Never>?
        let actions = ReviewPlanActions(loadSnapshot: {
            reads += 1
            if reads == 1 { return try self.snapshot(turn: "old") }
            return try self.snapshot(status: ready ? "ready" : "updating", turn: "new", sequence: 10)
        }, update: { _, _, _ in submissions += 1 }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        let model = ReviewPlanModel(seed: [], initialFlag: .captions, actions: actions,
            pollInterval: .seconds(2), giveUpAfter: .seconds(1), recoveryInterval: .seconds(30),
            readNow: { now }, readSleep: { delay in
                if delay == .seconds(30) {
                    paused.fulfill()
                    await withCheckedContinuation { release = $0 }
                }
                now = now.advanced(by: delay)
                await Task.yield()
            })
        await model.load()
        model.submitUpdate()
        await fulfillment(of: [paused], timeout: 2)
        XCTAssertEqual(model.phase, .updating)
        ready = true
        await model.refresh() // cursor / foreground / reconnect signal
        XCTAssertEqual(model.phase, .reviewing)
        XCTAssertNil(model.notice)
        release?.resume()
        let completedReads = reads
        for _ in 0..<20 { await Task.yield() }
        XCTAssertEqual(reads, completedReads)
        XCTAssertEqual(submissions, 1)
        model.cancelFollowing()
    }

    func testReviewAccountChangeRejectsLateReadAndFurtherRequests() async throws {
        var currentScope = true, reads = 0, sends = 0
        var release: CheckedContinuation<Void, Never>?
        let entered = expectation(description: "old account reading")
        let actions = ReviewPlanActions(loadSnapshot: {
            reads += 1
            entered.fulfill()
            await withCheckedContinuation { release = $0 }
            return try self.snapshot()
        }, update: { _, _, _ in sends += 1 }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        let model = ReviewPlanModel(seed: [], initialFlag: .captions, actions: actions,
            expectedThreadID: "t", isCurrentScope: { currentScope })
        let loading = Task { await model.load() }
        await fulfillment(of: [entered], timeout: 2)
        currentScope = false
        release?.resume()
        await loading.value
        await model.refresh()
        model.submitUpdate()
        XCTAssertNil(model.snapshot)
        XCTAssertEqual(reads, 1)
        XCTAssertEqual(sends, 0)
        model.cancelFollowing()
    }

    func testCompletedReviewFollowerReleasesOwnershipForTheNextUpdate() async throws {
        var submissions = 0
        let actions = ReviewPlanActions(loadSnapshot: {
            try self.snapshot(turn: "turn-\(submissions)", sequence: 9 + submissions)
        }, update: { _, _, _ in submissions += 1 }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, readSleep: { _ in await Task.yield() })
        await model.load()
        for turn in 1...2 {
            model.toggle(.captions)
            model.submitUpdate()
            for _ in 0..<100 where model.phase != .reviewing { await Task.yield() }
            XCTAssertEqual(model.phase, .reviewing)
            XCTAssertEqual(model.snapshot?.turnID, "turn-\(turn)")
        }
        XCTAssertEqual(submissions, 2)
        model.cancelFollowing()
    }

    func testReviewThreadIdentityAcceptsUUIDCaseButRejectsAnotherThread() async throws {
        let thread = UUID().uuidString
        var reads = 0
        let actions = ReviewPlanActions(loadSnapshot: {
            reads += 1
            return try self.snapshot(sequence: reads + 9,
                thread: reads == 1 ? thread.lowercased() : reads == 2 ? thread : UUID().uuidString)
        }, update: { _, _, _ in }, undoSection: { _, _, _ in true }, undoAll: { _ in })
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, expectedThreadID: thread)
        await model.refresh()
        XCTAssertEqual(model.snapshot?.nextAfterSequence, 10)
        await model.refresh()
        XCTAssertEqual(model.snapshot?.nextAfterSequence, 11)
        await model.refresh()
        XCTAssertEqual(model.snapshot?.nextAfterSequence, 11)
        model.cancelFollowing()
    }

    func testCapturedReviewOwnerRejectsTokenReplacementBeforeAnyTransport() async throws {
        func token(_ owner: String) -> MobileSession {
            let payload = Data("{\"sub\":\"\(owner)\"}".utf8).base64EncodedString()
            return .init(accessToken: "test.\(payload).test", refreshToken: "fixture", expiresIn: 3600)
        }
        let store = NativeEditorMemoryTokenStore(token("first"))
        let api: any KriaAPIClient = NativeEditorTestSupport.api(tokenStore: store)
        let capturedOwner = try XCTUnwrap(api.mediaLibraryOwner)
        try store.write(token("second"))
        var requests = 0
        NativeEditorURLProtocol.handler = { _ in requests += 1; return (500, Data()) }
        defer { NativeEditorURLProtocol.handler = nil }
        let thread = UUID(), approval = UUID()
        do { _ = try await api.planSnapshot(threadID: thread, expectedOwner: capturedOwner); XCTFail("read escaped") } catch { }
        do { _ = try await api.submitScopedTurn(threadID: thread, message: "change", expectedRevision: 1, clientEventID: UUID().uuidString, scope: [.captions], manualEdits: [], expectedOwner: capturedOwner); XCTFail("submit escaped") } catch { }
        do { _ = try await api.undoPlanSection(threadID: thread, sectionID: .captions, expectedThreadRevision: 1, expectedBlockRevision: 1, expectedDraftRevision: 1, expectedOwner: capturedOwner); XCTFail("section undo escaped") } catch { }
        do { _ = try await api.undoDraft(threadID: thread, expectedRevision: 1, render: true, expectedOwner: capturedOwner); XCTFail("undo escaped") } catch { }
        do { _ = try await api.approval(threadID: thread, approvalID: approval, expectedOwner: capturedOwner); XCTFail("approval read escaped") } catch { }
        do { try await api.decideReviewApproval(threadID: thread, approvalID: approval, expectedThreadRevision: 1, expectedDraftRevision: 1, fingerprint: "fixture", expectedOwner: capturedOwner); XCTFail("approve escaped") } catch { }
        XCTAssertEqual(requests, 0)
        try store.write(token("first"))
        do { _ = try await api.planSnapshot(threadID: thread, expectedOwner: capturedOwner) } catch { }
        do { _ = try await api.submitScopedTurn(threadID: thread, message: "change", expectedRevision: 1, clientEventID: UUID().uuidString, scope: [.captions], manualEdits: [], expectedOwner: capturedOwner) } catch { }
        do { _ = try await api.undoPlanSection(threadID: thread, sectionID: .captions, expectedThreadRevision: 1, expectedBlockRevision: 1, expectedDraftRevision: 1, expectedOwner: capturedOwner) } catch { }
        do { _ = try await api.undoDraft(threadID: thread, expectedRevision: 1, render: true, expectedOwner: capturedOwner) } catch { }
        do { _ = try await api.approval(threadID: thread, approvalID: approval, expectedOwner: capturedOwner) } catch { }
        do { try await api.decideReviewApproval(threadID: thread, approvalID: approval, expectedThreadRevision: 1, expectedDraftRevision: 1, fingerprint: "fixture", expectedOwner: capturedOwner) } catch { }
        XCTAssertEqual(requests, 6, "all six owner-bound witnesses must reach transport")
    }

    func testReviewUIFixtureIdentityIsDistinctFromItsTransportOwner() throws {
        let payload = Data("{\"sub\":\"chat-ui-fixture\"}".utf8).base64EncodedString()
        let token = "test.\(payload).test"
        let transportOwner = try XCTUnwrap(MediaLibraryOriginals.owner(accessToken: token))
        let account = try XCTUnwrap(ReviewAccountScope(uiOwner: "teleprompter-ui-fixture", transportOwner: transportOwner))
        XCTAssertEqual(account.transportOwner, transportOwner)
        XCTAssertNotEqual(account.transportOwner, "teleprompter-ui-fixture")
        XCTAssertTrue(account.matches(uiOwner: "teleprompter-ui-fixture", transportOwner: transportOwner))
        XCTAssertFalse(account.matches(uiOwner: "replacement", transportOwner: transportOwner))
        XCTAssertFalse(account.matches(uiOwner: "teleprompter-ui-fixture", transportOwner: "replacement"))
        XCTAssertNil(ReviewAccountScope(uiOwner: nil, transportOwner: transportOwner))
        XCTAssertNil(ReviewAccountScope(uiOwner: "teleprompter-ui-fixture", transportOwner: nil))
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
        var reads = 0
        let actions = ReviewPlanActions(
            loadSnapshot: {
                reads += 1
                return try self.changedSnapshot(job: reads == 1 ? "job-2" : "restored")
            },
            update: { _, _, _ in }, undoSection: { _, _, _ in false }, undoAll: { _ in }
        )
        let model = ReviewPlanModel(seed: [], initialFlag: nil, actions: actions, pollInterval: .seconds(30))
        await model.load()
        model.undo(.captions)
        for _ in 0..<100 where model.phase != .reviewing { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertEqual(model.phase, .reviewing, "no successor turn means no new job to wait for")
        XCTAssertEqual(model.snapshot?.jobID, "restored", "the no-render restore must still read its saved plan")
        XCTAssertEqual(reads, 2)
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
