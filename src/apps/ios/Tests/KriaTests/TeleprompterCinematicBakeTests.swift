import AVFoundation
import XCTest
@testable import Kria

/// Stop reaches Review on the raw take and the Cinematic bake runs afterwards. Failure modes, written before the code:
/// 1. The app is killed mid-bake: the manifest still says a bake is owed, the movie is the raw one, and the next opening
///    bakes again from the start. Nothing is lost or half-written.
/// 2. The app leaves the foreground mid-bake: Metal refuses background GPU work, so the bake stops cleanly (movie
///    untouched, manifest back to owed) and starts over on return. It never falls back to a plain copy because of it.
/// 3. Use take while the bake runs: nothing reaches intake until the bake has settled, and intake sees the baked take.
///    Use take while the app is away: the creator is told, nothing is attached.
/// 4. Record again / Discard while the bake runs: the bake is cancelled, the movie is deleted only once the bake task has
///    ended, and a late bake result never writes the discarded manifest back.
/// 5. The bake fails: the raw take stays playable and usable, and Review says it is standard.
/// 6. Two takes owe a bake: only the one on screen bakes, one at a time; the other waits until it is shown.
/// 7. The screen goes away mid-bake: the bake is cancelled with it, and the manifest still owes it.
/// 8. Settled states are final: replaying an event, or a relaunch, does not change a baked take.
/// 9. A manifest written before the bake existed still decodes (`applied` / `standard`).
/// 10. Not enough free space: refused before a long bake starts (the finisher falls back to keeping the raw take).
/// 11. The reader, writer or encoder fails with a non-Metal error because the app is being suspended, and the failure
///     lands before the scene phase cancels the task: that is a pause, never the plain-copy fallback. Likewise a cancelled
///     plain copy pauses. Only a failure with the app active, task live and no system-interruption error settles standard.
/// Scenarios 1, 2 and 5 against real Cinematic movies need a physical iPhone; the baker is faked here, so this proves the
/// lifecycle around it.
@MainActor final class TeleprompterCinematicBakeTests: XCTestCase {
    private var root: URL!
    private var defaults: UserDefaults!
    private var suiteName: String!
    private var projectID: UUID!

    override func setUp() async throws {
        root = FileManager.default.temporaryDirectory.appendingPathComponent("bake-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        suiteName = "bake-tests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)
        projectID = UUID()
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: root)
        defaults.removePersistentDomain(forName: suiteName)
    }

    // MARK: Telling a pause from a failed bake

    func testAFailureIsAPauseWheneverTheWorkWasStoppedFromOutside() {
        struct Other: Error {}
        let interrupted = NSError(domain: AVFoundationErrorDomain, code: AVError.Code.operationInterrupted.rawValue)
        let otherAV = NSError(domain: AVFoundationErrorDomain, code: AVError.Code.decodeFailed.rawValue)
        let table: [(Error, cancelled: Bool, active: Bool, pause: Bool)] = [
            (Other(), false, true, false),
            (Other(), true, true, true),
            (Other(), false, false, true),
            (interrupted, false, true, true),
            (otherAV, false, true, false),
            (otherAV, false, false, true),
        ]
        for row in table {
            XCTAssertEqual(CinematicBakeInterruption.shouldPause(after: row.0, taskCancelled: row.cancelled, appActive: row.active),
                           row.pause, "\(row.0) cancelled=\(row.cancelled) active=\(row.active)")
        }
        XCTAssertFalse(CinematicBakeInterruption.isSystemInterruption(nil))
        XCTAssertFalse(CinematicBakeInterruption.isSystemInterruption(NSError(domain: NSCocoaErrorDomain, code: AVError.Code.operationInterrupted.rawValue)),
                       "the code alone is not enough: the domain must be AVFoundation's")
    }

    // MARK: The state machine

    func testEveryStateAndEventHasOneDefinedResult() {
        typealias State = CinematicBakeState
        let table: [(State, State.Event, State)] = [
            (.rawReview, .started, .baking), (.rawReview, .baked, .baked), (.rawReview, .keptStandard, .standardKept),
            (.rawReview, .interrupted, .rawReview),
            (.baking, .started, .baking), (.baking, .baked, .baked), (.baking, .keptStandard, .standardKept),
            (.baking, .interrupted, .rawReview),
            (.baked, .started, .baked), (.baked, .baked, .baked), (.baked, .keptStandard, .baked), (.baked, .interrupted, .baked),
            (.standardKept, .started, .standardKept), (.standardKept, .baked, .standardKept),
            (.standardKept, .keptStandard, .standardKept), (.standardKept, .interrupted, .standardKept),
        ]
        for (state, event, expected) in table {
            XCTAssertEqual(state.applying(event), expected, "\(state) + \(event)")
        }
        XCTAssertEqual(table.count, 4 * 4, "every state meets every event")
    }

    func testOnlyTheTwoFinalStatesAreSettled() {
        XCTAssertEqual([CinematicBakeState.rawReview, .baking, .baked, .standardKept].map(\.isSettled), [false, false, true, true])
    }

    func testBakerAnswersMapToEvents() {
        XCTAssertEqual(CinematicBakeState.event(for: .applied), .baked)
        XCTAssertEqual(CinematicBakeState.event(for: nil), .baked, "a movie that was already ordinary is a finished bake")
        XCTAssertEqual(CinematicBakeState.event(for: .standard), .keptStandard)
    }

    func testManifestsWrittenBeforeTheBakeStateStillDecode() throws {
        for (raw, expected) in [("applied", CinematicBakeState.baked), ("standard", .standardKept)] {
            var json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(TeleprompterTakeManifest(script: TeleprompterScriptDraft()))) as! [String: Any]
            json["cinematic"] = raw
            let decoded = try JSONDecoder().decode(TeleprompterTakeManifest.self, from: JSONSerialization.data(withJSONObject: json))
            XCTAssertEqual(decoded.cinematic, expected)
        }
    }

    func testRoomNeededGrowsWithLengthAndNeverUndercutsTheBakedFile() {
        let bakedBytes = Double(TeleprompterCinematic.bakedBitsPerSecond) / 8 * 60
        XCTAssertGreaterThan(Double(TeleprompterCinematic.bytesNeededToBake(seconds: 60)), bakedBytes)
        XCTAssertGreaterThan(TeleprompterCinematic.bytesNeededToBake(seconds: 120), TeleprompterCinematic.bytesNeededToBake(seconds: 60))
        XCTAssertGreaterThan(TeleprompterCinematic.bytesNeededToBake(seconds: 0), 0)
    }

    // MARK: The lifecycle around the baker

    func testBakeStartsOnlyOnceReviewIsOpenAndIsPersistedAsRunning() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .rawReview)])
        await waitUntil { baker.calls == 1 }
        XCTAssertEqual(flow.stage, .review)
        XCTAssertEqual(try savedStates(), [.baking], "a kill now leaves a manifest that owes the bake")
        baker.finish(.applied)
        await waitUntil { flow.selectedTake?.cinematic == .baked }
        XCTAssertEqual(try savedStates(), [.baked])
        XCTAssertNil(flow.bakeProgress)
    }

    func testStopNeverWaitsOnTheBakerBeforeReviewShows() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .rawReview)])
        XCTAssertEqual(flow.stage, .review, "Review is open while the bake is still running")
        XCTAssertEqual(flow.selectedTake?.status, .review)
        baker.finish(.applied)
    }

    func testUseTakeWaitsForTheBakeAndHandsIntakeTheBakedTake() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .rawReview)])
        await waitUntil { baker.calls == 1 }
        let intake = IntakeProbe()
        let use = Task { await flow.useTake { take, _ in await intake.record(take); return .queued } }
        await waitUntil { flow.waitingForBake }
        try await Task.sleep(for: .milliseconds(150))
        let early = await intake.takes
        XCTAssertTrue(early.isEmpty, "nothing is attached while the bake runs")
        baker.finish(.applied)
        await use.value
        let taken = await intake.takes
        XCTAssertEqual(taken.map(\.cinematic), [.baked])
        XCTAssertFalse(flow.waitingForBake)
    }

    func testUseTakeOnABakedTakeDoesNotWait() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .baked)])
        let intake = IntakeProbe()
        await flow.useTake { take, _ in await intake.record(take); return .queued }
        let taken = await intake.takes
        XCTAssertEqual(taken.count, 1)
        XCTAssertEqual(baker.calls, 0, "a settled take is never baked again")
    }

    func testFailedBakeKeepsTheRawTakeUsable() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .rawReview)])
        await waitUntil { baker.calls == 1 }
        baker.finish(.standard)
        await waitUntil { flow.selectedTake?.cinematic == .standardKept }
        XCTAssertEqual(flow.selectedTake?.status, .review)
        XCTAssertTrue(FileManager.default.fileExists(atPath: flow.store.fileURL(for: try XCTUnwrap(flow.selectedTake)).path))
        let intake = IntakeProbe()
        await flow.useTake { take, _ in await intake.record(take); return .queued }
        let taken = await intake.takes
        XCTAssertEqual(taken.map(\.cinematic), [.standardKept])
    }

    func testLeavingTheForegroundStopsTheBakeAndReturnRestartsIt() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .rawReview)])
        await waitUntil { baker.calls == 1 }
        flow.scenePhaseChanged(.background)
        await waitUntil { baker.cancellations == 1 && flow.selectedTake?.cinematic == .rawReview }
        XCTAssertEqual(try savedStates(), [.rawReview], "the movie is untouched and the bake is owed again")
        flow.scenePhaseChanged(.active)
        await waitUntil { baker.calls == 2 }
        baker.finish(.applied)
        await waitUntil { flow.selectedTake?.cinematic == .baked }
    }

    func testUseTakeWhileTheAppIsAwayAttachesNothingAndSaysWhy() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .rawReview)])
        await waitUntil { baker.calls == 1 }
        let intake = IntakeProbe()
        let use = Task { await flow.useTake { take, _ in await intake.record(take); return .queued } }
        await waitUntil { flow.waitingForBake }
        flow.scenePhaseChanged(.background)
        await use.value
        let early = await intake.takes
        XCTAssertTrue(early.isEmpty)
        XCTAssertEqual(flow.error, TeleprompterRecordingFlow.bakePausedMessage)
        XCTAssertFalse(flow.waitingForBake)
        flow.scenePhaseChanged(.active)
        await waitUntil { baker.calls == 2 }
        baker.finish(.applied)
        await waitUntil { flow.selectedTake?.cinematic == .baked }
        await flow.useTake { take, _ in await intake.record(take); return .queued }
        let taken = await intake.takes
        XCTAssertEqual(taken.map(\.cinematic), [.baked])
    }

    func testDiscardingWhileBakingCancelsTheBakeAndNeverBringsTheManifestBack() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, nil), (.candidate, .rawReview)])
        await waitUntil { baker.calls == 1 }
        let candidate = try XCTUnwrap(flow.selectedTake)
        XCTAssertEqual(candidate.slot, .candidate)
        let movie = flow.store.fileURL(for: candidate)

        flow.discardSelected()
        await waitUntil { baker.cancellations == 1 }
        await waitUntil { !FileManager.default.fileExists(atPath: movie.path) }
        XCTAssertEqual(try savedStates().count, 1, "only the kept take has a manifest")
        XCTAssertNotEqual(flow.selectedTake?.id, candidate.id)
        XCTAssertEqual(baker.calls, 1, "the kept take is settled and is not baked")
    }

    func testOnlyTheTakeOnScreenBakesAndTheOtherWaitsUntilItIsShown() async throws {
        let baker = GatedBaker()
        let flow = try await openFlow(baker: baker, takes: [(.pending, .rawReview), (.candidate, .rawReview)])
        await waitUntil { baker.calls == 1 }
        let shown = try XCTUnwrap(flow.selectedTake)
        XCTAssertEqual(shown.slot, .candidate)
        XCTAssertEqual(Set(try savedStates()), [.baking, .rawReview], "one bake runs; the other take is owed, not running")

        flow.previousTake()
        await waitUntil { flow.selectedTake?.slot == .pending && baker.calls == 2 }
        baker.finish(.applied)
        await waitUntil { flow.selectedTake?.cinematic == .baked }
    }

    func testDroppingTheFlowCancelsTheBakeAndTheManifestStillOwesIt() async throws {
        let baker = GatedBaker()
        var flow: TeleprompterRecordingFlow? = try await openFlow(baker: baker, takes: [(.pending, .rawReview)])
        await waitUntil { baker.calls == 1 }
        weak var weakFlow = flow
        flow = nil
        await waitUntil { weakFlow == nil && baker.cancellations == 1 }
        XCTAssertEqual(baker.cancellations, 1)
        XCTAssertEqual(try savedStates(), [.baking], "left as the kill case: the next opening resets it and bakes again")
    }

    func testRelaunchAfterAKillBakesAgainFromTheStart() async throws {
        let first = GatedBaker()
        var flow: TeleprompterRecordingFlow? = try await openFlow(baker: first, takes: [(.pending, .rawReview)])
        await waitUntil { first.calls == 1 }
        flow = nil
        await waitUntil { first.cancellations == 1 }
        XCTAssertEqual(try savedStates(), [.baking])

        let second = GatedBaker()
        let relaunched = try await openFlow(baker: second, takes: [])
        await waitUntil { second.calls == 1 }
        second.finish(.applied)
        await waitUntil { relaunched.selectedTake?.cinematic == .baked }
        XCTAssertEqual(try savedStates(), [.baked])
    }

    func testABakedTakeIsLeftAloneByARelaunch() async throws {
        let baker = GatedBaker()
        _ = try await openFlow(baker: baker, takes: [(.pending, .baked)])
        try await Task.sleep(for: .milliseconds(100))
        XCTAssertEqual(baker.calls, 0)
        XCTAssertEqual(try savedStates(), [.baked])
    }

    // MARK: Helpers

    /// Writes the given takes (a placeholder movie each), then opens a flow the way a relaunch does.
    private func openFlow(baker: any CinematicBaking, takes: [(TeleprompterTakeSlot, CinematicBakeState?)]) async throws -> TeleprompterRecordingFlow {
        let flow = TeleprompterRecordingFlow(accountID: "test", projectID: projectID, root: root, cinematicAvailability: .available,
                                             baker: baker, defaults: defaults)
        for (offset, entry) in takes.enumerated() {
            var take = TeleprompterTakeManifest(status: .review, capturedAt: Date(timeIntervalSince1970: Double(offset)),
                                                script: TeleprompterScriptDraft(), slot: entry.0)
            take.cinematic = entry.1
            try flow.store.save(manifest: take)
            try Data("movie".utf8).write(to: flow.store.fileURL(for: take))
        }
        await flow.load()
        return flow
    }

    private func savedStates() throws -> [CinematicBakeState?] {
        let store = TeleprompterStore(accountID: "test", projectID: projectID, root: root)
        return try store.loadManifests().map(\.cinematic)
    }

    private func waitUntil(timeout: Duration = .seconds(5), _ condition: @MainActor () -> Bool, line: UInt = #line) async {
        let deadline = ContinuousClock.now + timeout
        while !condition(), ContinuousClock.now < deadline { try? await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(condition(), "timed out waiting", line: line)
    }
}

private actor IntakeProbe {
    private(set) var takes: [TeleprompterTakeManifest] = []
    func record(_ take: TeleprompterTakeManifest) { takes.append(take) }
}

/// A baker that runs until the test lets it finish, and records how often it was started and cancelled.
private final class GatedBaker: CinematicBaking, @unchecked Sendable {
    private final class Slot: @unchecked Sendable {
        var cancelled = false
        var continuation: CheckedContinuation<CinematicTakeOutcome?, any Error>?
    }

    private let lock = NSLock()
    private var slots: [Slot] = []
    private var started = 0
    private var cancelled = 0
    var calls: Int { lock.withLock { started } }
    var cancellations: Int { lock.withLock { cancelled } }

    func needsBake(movieAt url: URL) async -> Bool { true }

    func bake(movieAt url: URL, progress: @escaping @Sendable (Double) -> Void) async throws -> CinematicTakeOutcome? {
        let slot = Slot()
        lock.withLock { started += 1; slots.append(slot) }
        return try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                lock.lock()
                if slot.cancelled { lock.unlock(); continuation.resume(throwing: CancellationError()) }
                else { slot.continuation = continuation; lock.unlock() }
            }
        } onCancel: {
            lock.lock()
            cancelled += 1
            slot.cancelled = true
            let waiting = slot.continuation
            slot.continuation = nil
            lock.unlock()
            waiting?.resume(throwing: CancellationError())
        }
    }

    /// Finishes every bake that is currently running.
    func finish(_ outcome: CinematicTakeOutcome?) {
        let waiting: [CheckedContinuation<CinematicTakeOutcome?, any Error>] = lock.withLock {
            let ready = slots.compactMap(\.continuation)
            slots.forEach { $0.continuation = nil }
            return ready
        }
        waiting.forEach { $0.resume(returning: outcome) }
    }
}
