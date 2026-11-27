import XCTest
@testable import Kria

/// CA-8: generated overlays are asked for, answered and made in the chat thread. Failure modes pinned here, written
/// before the code:
///
/// - an event of an unknown version, phase or row state drawing a half-read card; a build reading a set it cannot
///   decode must draw nothing for that set;
/// - the newest event of a set not winning (an older `progress` drawn over `done`), or one set drawn as many rows;
/// - an ask that was answered, cancelled or replaced by a typed reply staying open and holding every later ask;
/// - the progress phase asking again, so the composer locks for a status card;
/// - a partly failed set reading as done, or hiding which overlay failed;
/// - Retry reaching an overlay that is not failed, or one that may already have been paid for (`retryable: false`);
/// - Retry making the other overlays again;
/// - a relaunch repeating `generate` for ready or failed overlays (a failed one would be retried and charged), or
///   asking `compose` or `plan` again for a set whose plan exists;
/// - a relaunch in the `compose` phase (answer stored, no plan) never planning, which kills the set;
/// - a double tap sending the answer twice, or one overlay's `generate` twice at once;
/// - a stale `making` event arriving after the call settled repeating the paid call;
/// - the ask card flashing back between the answer and the event that records it;
/// - a ready overlay with no operation being handed to the review sheet;
/// - the price or the row cap read from anywhere but the event.
@MainActor
final class OverlayAskCardsTests: XCTestCase {
    private func payload(_ set: String = "s1", phase: String = "ask", mode: String? = nil, prompts: [String] = [],
                         overlays: [[String: Any]] = [], v: Int = 1, extra: [String: Any] = [:]) -> [String: JSONValue] {
        var body: [String: Any] = ["v": v, "set_id": set, "phase": phase, "mode": mode as Any? ?? NSNull(), "prompts": prompts,
                                    "unit_price_usd": 0.04, "max_overlays": 6, "overlays": overlays,
                                    "overlay_ids": overlays.filter { $0["state"] as? String == "ready" }.compactMap { $0["id"] },
                                    "message": ""]
        body.merge(extra) { $1 }
        return try! JSONDecoder().decode([String: JSONValue].self, from: JSONSerialization.data(withJSONObject: body))
    }

    private func row(_ id: String, _ state: String, retryable: Bool = false, op: String? = nil, code: String? = nil) -> [String: Any] {
        ["id": id, "title": "Title \(id)", "prompt": "Prompt \(id)", "state": state,
         "operation_id": op as Any? ?? NSNull(), "error_code": code as Any? ?? NSNull(), "retryable": retryable]
    }

    private func event(_ sequence: Int, _ body: [String: JSONValue], role: String = "assistant", type: String = "overlay_set") -> ThreadEvent {
        ThreadEvent(id: "e\(sequence)", sequence: sequence, revision: sequence, role: role, eventType: type, content: nil,
                    payload: body, createdAt: Date(timeIntervalSince1970: 0))
    }

    private func set(_ id: String = "s1", phase: OverlaySet.Phase, overlays: [OverlaySet.Overlay] = [], mode: OverlaySet.Mode? = nil) -> OverlaySet {
        OverlaySet(id: id, phase: phase, mode: mode, prompts: [], unitPrice: Decimal(string: "0.04")!, maxOverlays: 6,
                   overlays: overlays, readyIDs: overlays.filter { $0.state == .ready }.map(\.id), message: "")
    }

    private func overlay(_ id: String, _ state: OverlaySet.Overlay.State, retryable: Bool = false, op: String? = nil) -> OverlaySet.Overlay {
        OverlaySet.Overlay(id: id, title: "Title \(id)", prompt: "Prompt \(id)", state: state, operationID: op, errorCode: nil, retryable: retryable)
    }

    // MARK: payload

    func testDecodesTheServerShapeAndTheEventsOwnPriceAndCap() throws {
        let parsed = try XCTUnwrap(OverlaySet.parse(payload(phase: "progress", mode: "prompt", prompts: ["a", "b"],
            overlays: [row("o1", "ready", op: "op1"), row("o2", "making"), row("o3", "failed", retryable: true, code: "provider_rejected")])))
        XCTAssertEqual(parsed.phase, .progress)
        XCTAssertEqual(parsed.mode, .prompt)
        XCTAssertEqual(parsed.unitPrice, Decimal(string: "0.04"))
        XCTAssertEqual(parsed.maxOverlays, 6)
        XCTAssertEqual(parsed.readyCount, 1)
        XCTAssertEqual(parsed.makingCount, 1)
        XCTAssertEqual(parsed.overlays[2].errorCode, "provider_rejected")
        XCTAssertTrue(parsed.overlays[2].canRetry)
        XCTAssertFalse(parsed.overlays[0].canRetry)
    }

    func testAnUnknownVersionPhaseOrRowStateDrawsNothing() {
        XCTAssertNil(OverlaySet.parse(payload(v: 2)))
        XCTAssertNil(OverlaySet.parse(payload(phase: "review")))
        XCTAssertNil(OverlaySet.parse(payload(phase: "progress", overlays: [row("o1", "stalled")])))
        XCTAssertNil(OverlaySet.parse(payload("")))
        XCTAssertNil(OverlaySet.parse(nil))
    }

    func testThePriceIsExactDecimalMoney() throws {
        let parsed = try XCTUnwrap(OverlaySet.parse(payload()))
        XCTAssertEqual(parsed.cost(rows: 3), Decimal(string: "0.12"))
        XCTAssertEqual(parsed.cost(rows: 0), 0)
    }

    // MARK: fold

    func testTheNewestEventOfASetWinsAndTheSetSitsWhereItWasAsked() {
        let folded = OverlaySetThread.fold([
            event(5, payload(phase: "ask")),
            event(6, payload(phase: "compose", mode: "automatic"), role: "user"),
            event(9, payload(phase: "done", overlays: [row("o1", "ready", op: "op1")])),
            event(7, payload(phase: "progress", overlays: [row("o1", "making")])),
        ])
        XCTAssertEqual(folded.count, 1)
        XCTAssertEqual(folded[0].set.phase, .done)
        XCTAssertEqual(folded[0].sequence, 5)
        XCTAssertEqual(folded[0].id, "overlay-set-s1")
    }

    func testTwoSetsStayTwoEntriesInAskOrder() {
        let folded = OverlaySetThread.fold([event(2, payload("a")), event(4, payload("b")), event(5, payload("a", phase: "done"))])
        XCTAssertEqual(folded.map(\.set.id), ["a", "b"])
        XCTAssertEqual(folded.map(\.set.phase), [.done, .ask])
    }

    func testASetWhoseNewestEventCannotBeReadIsHiddenNotShownStale() {
        let folded = OverlaySetThread.fold([event(1, payload(phase: "ask")), event(2, payload(phase: "progress", v: 2))])
        XCTAssertTrue(folded.isEmpty)
    }

    func testOtherEventTypesAreIgnored() {
        XCTAssertTrue(OverlaySetThread.fold([event(1, payload(), type: "assistant_response")]).isEmpty)
    }

    // MARK: asks

    func testOnlyTheAskPhaseIsAnAskAndATypedReplyClosesIt() {
        let entry = OverlaySetEntry(set: set(phase: .ask), anchorEventID: "e1", sequence: 1)
        let ask = entry.askEntry(enabled: [.overlay])
        XCTAssertEqual(ask.asks.map(\.kind), [.overlay])
        XCTAssertEqual(ask.asks.first?.closer, .confirmedReply)
        for phase in [OverlaySet.Phase.compose, .progress, .done] {
            XCTAssertTrue(OverlaySetEntry(set: set(phase: phase), anchorEventID: "e1", sequence: 1).askEntry(enabled: [.overlay]).asks.isEmpty, "\(phase) is a status card")
        }
        XCTAssertTrue(entry.askEntry(enabled: []).asks.isEmpty, "capability off draws no ask")
    }

    func testAnAbandonedAskDoesNotHoldTheNextAsk() {
        let overlayEntry = OverlaySetEntry(set: set(phase: .ask), anchorEventID: "e1", sequence: 1).askEntry(enabled: [.overlay])
        let next = AskFold.Entry(messageID: "m2", asks: [.init(id: "q", kind: .moment, closer: .answer("q"))])
        let typed = AskFold.Entry(messageID: "u1", isUser: true)
        let fold = AskFold.fold([overlayEntry, next, typed])
        XCTAssertEqual(fold.standing("overlay-set-s1", "s1"), .answered)
        XCTAssertEqual(fold.openMessageID, "m2")
        let cancelled = AskFold.fold([overlayEntry, next], closed: ["s1"])
        XCTAssertEqual(cancelled.standing("overlay-set-s1", "s1"), .stale)
        XCTAssertEqual(cancelled.openMessageID, "m2")
    }

    func testTheAskPhaseMapsOntoOneAskPhasePerStanding() {
        let ask = set(phase: .ask)
        XCTAssertEqual(ask.askPhase(standing: .open, isSending: false, failed: false, isClosed: false), .open)
        XCTAssertEqual(ask.askPhase(standing: .open, isSending: true, failed: false, isClosed: false), .submitting(""))
        XCTAssertEqual(ask.askPhase(standing: .open, isSending: false, failed: true, isClosed: false), .failed("Couldn’t send your answer."))
        for standing in [AskFold.Standing.queued, .answered, .replaced, .stale] {
            XCTAssertNil(ask.askPhase(standing: standing, isSending: false, failed: false, isClosed: false), "\(standing) draws nothing")
        }
        XCTAssertNil(ask.askPhase(standing: .open, isSending: false, failed: false, isClosed: true), "a cancelled ask draws nothing")
        XCTAssertEqual(set(phase: .progress, mode: .prompt).askPhase(standing: nil, isSending: false, failed: false, isClosed: false), .answered("Describe them"))
        XCTAssertEqual(set(phase: .done, mode: .automatic).askPhase(standing: nil, isSending: false, failed: false, isClosed: false), .answered("Let Kria decide"))
    }

    // MARK: compose draft

    func testTheDraftCountsOnlyFilledRowsAndStopsAtTheEventsCap() {
        var draft = OverlayComposeDraft(maxRows: 3)
        XCTAssertEqual(draft.rows, [""])
        XCTAssertFalse(draft.canMake)
        draft.set(0, "A sticker of a mountain peak")
        draft.add(); draft.add(); draft.add()
        XCTAssertEqual(draft.rows.count, 3, "the cap is the event's max_overlays")
        XCTAssertFalse(draft.canAdd)
        XCTAssertEqual(draft.filled, ["A sticker of a mountain peak"])
        draft.set(1, "   A hand-drawn compass \n")
        XCTAssertEqual(draft.filled, ["A sticker of a mountain peak", "A hand-drawn compass"])
        XCTAssertTrue(draft.canMake)
    }

    func testRemovingTheLastRowClearsItAndAPromptIsCutAtTheServersLimit() {
        var draft = OverlayComposeDraft(maxRows: 6)
        draft.set(0, String(repeating: "x", count: 400))
        XCTAssertEqual(draft.rows[0].count, 300, "the server cuts at 300 silently, so the phone shows what is sent")
        draft.remove(0)
        XCTAssertEqual(draft.rows, [""])
        draft.add(); draft.set(1, "b")
        draft.remove(0)
        XCTAssertEqual(draft.rows, ["b"])
    }

    // MARK: what to do next

    func testNextStepsPlanAStoredAnswerAndMakeOnlyOverlaysStillMaking() {
        let memory = OverlayDriverMemory()
        XCTAssertEqual(OverlayDriver.steps(for: [set(phase: .ask)], memory: memory), [])
        XCTAssertEqual(OverlayDriver.steps(for: [set(phase: .compose, mode: .automatic)], memory: memory), [.plan("s1")])
        let progress = set(phase: .progress, overlays: [overlay("o1", .ready, op: "x"), overlay("o2", .making), overlay("o3", .failed, retryable: true),
                                                         overlay("o4", .failed, retryable: false), overlay("o5", .making)])
        XCTAssertEqual(OverlayDriver.steps(for: [progress], memory: memory), [.generate(set: "s1", overlay: "o2"), .generate(set: "s1", overlay: "o5")],
                       "a failed overlay is retried by a tap, never by a relaunch")
        XCTAssertEqual(OverlayDriver.steps(for: [set(phase: .done, overlays: [overlay("o1", .failed, retryable: true)])], memory: memory), [])
    }

    func testAStepAlreadyStartedThisRunIsNotStartedAgain() {
        var memory = OverlayDriverMemory()
        memory.planned.insert("s1")
        memory.started.insert("s1/o2")
        XCTAssertEqual(OverlayDriver.steps(for: [set(phase: .compose, mode: .automatic)], memory: memory), [])
        let progress = set(phase: .progress, overlays: [overlay("o2", .making), overlay("o3", .making)])
        XCTAssertEqual(OverlayDriver.steps(for: [progress], memory: memory), [.generate(set: "s1", overlay: "o3")],
                       "a stale making event after the call settled must not repeat the paid call")
    }

    func testOnlyReadyOverlaysWithAnOperationGoToTheReview() {
        let all = set(phase: .done, overlays: [overlay("o1", .ready, op: "op1"), overlay("o2", .ready, op: nil), overlay("o3", .failed, retryable: true, op: "op3")])
        XCTAssertEqual(all.reviewItems.map(\.id), ["o1"])
    }

    // MARK: coordinator

    private func coordinator(_ client: FakeOverlayClient, planBody: Bool = true) -> OverlaySetCoordinator {
        OverlaySetCoordinator(client: client, threadID: UUID(), itemID: { "item-1" },
                              planRequest: { planBody ? Self.stubRequest : nil }, refresh: {}, retryDelay: .milliseconds(5))
    }

    private static let stubRequest = OverlayPlanRequest(jobID: UUID().uuidString, variantID: "v", baseGeneration: "g", documentFingerprint: "f",
                                                        mode: .automatic, prompt: "", tasteNotes: "", editorSnapshot: [:])

    func testADoubleTapSendsTheAnswerOnceAndPlansOnce() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        coordinator.chooseAutomatic("s1")
        coordinator.chooseAutomatic("s1")
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("compose") }, ["compose s1 automatic []"])
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("plan") }, ["plan s1"])
    }

    func testAnAnswerTheServerTookIsNotAskedAgainWhileTheThreadCatchesUp() async {
        let coordinator = coordinator(FakeOverlayClient())
        let ask = set(phase: .ask)
        XCTAssertEqual(coordinator.shown(ask).phase, .ask)
        coordinator.chooseAutomatic("s1")
        await coordinator.waitForIdle()
        XCTAssertEqual(coordinator.shown(ask).phase, .compose, "the ask card must not flash back between the answer and the next event")
        XCTAssertEqual(coordinator.shown(set(phase: .done)).phase, .done)
    }

    func testMakingFromTheComposeCardSendsTheFilledRowsOnce() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        coordinator.beginDescribing("s1", maxRows: 6)
        coordinator.draftChanged("s1") { $0.set(0, "a mountain"); $0.add(); $0.set(1, " ") }
        coordinator.make("s1")
        coordinator.make("s1")
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("compose") }, ["compose s1 prompt [a mountain]"])
        XCTAssertNil(coordinator.draft(for: "s1"), "the draft is spent once the answer is stored")
    }

    func testAFailedAnswerKeepsTheDraftAndSaysSoAndTheSameAnswerCanBeSentAgain() async {
        let client = FakeOverlayClient()
        client.failCompose = 1
        let coordinator = coordinator(client)
        coordinator.beginDescribing("s1", maxRows: 6)
        coordinator.draftChanged("s1") { $0.set(0, "a mountain") }
        coordinator.make("s1")
        await coordinator.waitForIdle()
        XCTAssertEqual(coordinator.failure(for: "s1"), .answer)
        XCTAssertEqual(coordinator.draft(for: "s1")?.filled, ["a mountain"])
        coordinator.make("s1")
        await coordinator.waitForIdle()
        XCTAssertNil(coordinator.failure(for: "s1"))
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("compose") }.count, 2)
    }

    func testAPlanThatIsStillClaimedWaitsAndTriesAgainWithoutShowingAFailure() async {
        let client = FakeOverlayClient()
        client.planConflicts = 2
        let coordinator = coordinator(client)
        coordinator.reconcile([set(phase: .compose, mode: .automatic)])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("plan") }.count, 3)
        XCTAssertNil(coordinator.failure(for: "s1"))
    }

    func testAPlanTheServerRefusesShowsAFailureAndRetryPlansAgain() async {
        let client = FakeOverlayClient()
        client.planRejections = 1
        let coordinator = coordinator(client)
        coordinator.reconcile([set(phase: .compose, mode: .automatic)])
        await coordinator.waitForIdle()
        XCTAssertEqual(coordinator.failure(for: "s1"), .plan)
        coordinator.retryPlan("s1")
        await coordinator.waitForIdle()
        XCTAssertNil(coordinator.failure(for: "s1"))
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("plan") }.count, 2)
    }

    func testARelaunchInTheComposePhasePlansAndNeverAsksAgain() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        coordinator.reconcile([set(phase: .compose, mode: .prompt)])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls, ["plan s1"])
    }

    func testARelaunchMidGenerationRepeatsGenerateForMakingOverlaysOnly() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        coordinator.reconcile([set(phase: .progress, overlays: [overlay("o1", .ready, op: "x"), overlay("o2", .making), overlay("o3", .failed, retryable: true),
                                                                  overlay("o4", .failed, retryable: false)])])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("generate") }, ["generate s1/o2"])
        coordinator.reconcile([set(phase: .progress, overlays: [overlay("o2", .making)])])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("generate") }, ["generate s1/o2"], "the same event again makes nothing a second time")
    }

    func testARepeatWhileTheOperationIsStillInFlightKeepsAskingUntilItSettles() async {
        let client = FakeOverlayClient()
        client.generateAnswers = ["s1/o2": [.making, .making, .ready]]
        let coordinator = coordinator(client)
        coordinator.reconcile([set(phase: .progress, overlays: [overlay("o2", .making)])])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls, ["generate s1/o2", "generate s1/o2", "generate s1/o2"])
    }

    func testRetryMakesOnlyTheFailedOverlayAndOnlyWhenItMayBeRetried() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        coordinator.reconcile([set(phase: .done, overlays: [overlay("o1", .ready, op: "x"), overlay("o2", .failed, retryable: true),
                                                              overlay("o3", .failed, retryable: false)])])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("generate") }, [])
        coordinator.retry(set: "s1", overlay: "o3")
        coordinator.retry(set: "s1", overlay: "o1")
        coordinator.retry(set: "s1", overlay: "missing")
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("generate") }, [], "a ready overlay, an unknown one and one that may have been paid for are never made again")
        coordinator.retry(set: "s1", overlay: "o2")
        coordinator.retry(set: "s1", overlay: "o2")
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.filter { $0.hasPrefix("generate") }, ["generate s1/o2"], "a double tap is one paid call")
    }

    func testAStaleMakingEventAfterARetrySettledDoesNotChargeAgain() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        coordinator.reconcile([set(phase: .done, overlays: [overlay("o2", .failed, retryable: true)])])
        coordinator.retry(set: "s1", overlay: "o2")
        await coordinator.waitForIdle()
        coordinator.reconcile([set(phase: .progress, overlays: [overlay("o2", .making)])])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls, ["generate s1/o2"])
    }

    func testAnOfflineCallIsRepeatedWhenTheSetIsReconciledAgain() async {
        let client = FakeOverlayClient()
        client.generateOffline = 1
        let coordinator = coordinator(client)
        let progress = set(phase: .progress, overlays: [overlay("o2", .making)])
        coordinator.reconcile([progress])
        await coordinator.waitForIdle()
        coordinator.reconcile([progress])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls, ["generate s1/o2", "generate s1/o2"])
    }

    func testReadyOverlaysLookUpTheirImageOnce() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        let done = set(phase: .done, overlays: [overlay("o1", .ready, op: "op1"), overlay("o2", .ready, op: "op2"), overlay("o3", .failed, retryable: true, op: "op3")])
        coordinator.reconcile([done])
        await coordinator.waitForIdle()
        coordinator.reconcile([done])
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls.sorted(), ["media op1", "media op2"])
        XCTAssertNotNil(coordinator.thumbnail(for: "op1"))
    }

    func testTheEntryOpensANewSetOnceAndCancelClosesItForAGoodReason() async {
        let client = FakeOverlayClient()
        let coordinator = coordinator(client)
        coordinator.open(setID: "s9")
        coordinator.open(setID: "s9")
        await coordinator.waitForIdle()
        XCTAssertEqual(client.calls, ["open s9"])
        coordinator.cancel("s9")
        XCTAssertTrue(coordinator.closed.contains("s9"))
    }
}

private final class FakeOverlayClient: OverlaySetClient, @unchecked Sendable {
    enum Answer { case making, ready, failed }
    private let lock = NSLock()
    private var recorded: [String] = []
    var failCompose = 0
    var planConflicts = 0
    var planRejections = 0
    var generateOffline = 0
    var generateAnswers: [String: [Answer]] = [:]

    var calls: [String] { lock.withLock { recorded } }

    private func record(_ call: String) { lock.withLock { recorded.append(call) } }

    private func overlaySet(_ set: String, _ overlay: String? = nil, _ answer: Answer = .ready) -> OverlaySet {
        let state: OverlaySet.Overlay.State = answer == .making ? .making : (answer == .ready ? .ready : .failed)
        let rows = overlay.map { [OverlaySet.Overlay(id: $0, title: "t", prompt: "p", state: state, operationID: state == .ready ? "op-\($0)" : nil, errorCode: nil, retryable: state == .failed)] } ?? []
        return OverlaySet(id: set, phase: .progress, mode: .automatic, prompts: [], unitPrice: Decimal(string: "0.04")!, maxOverlays: 6,
                          overlays: rows, readyIDs: [], message: "")
    }

    func openOverlaySet(threadID: UUID, setID: String) async throws -> OverlaySet {
        record("open \(setID)")
        return overlaySet(setID)
    }

    func composeOverlaySet(threadID: UUID, setID: String, mode: OverlaySet.Mode, prompts: [String]) async throws -> OverlaySet {
        record("compose \(setID) \(mode.rawValue) [\(prompts.joined(separator: ","))]")
        if lock.withLock({ if failCompose > 0 { failCompose -= 1; return true } else { return false } }) { throw APIError.requestFailed(status: 500) }
        return OverlaySet(id: setID, phase: .compose, mode: mode, prompts: prompts, unitPrice: Decimal(string: "0.04")!, maxOverlays: 6, overlays: [], readyIDs: [], message: "")
    }

    func planOverlaySet(threadID: UUID, setID: String, body: OverlayPlanRequest) async throws {
        record("plan \(setID)")
        let action: Int = lock.withLock {
            if planConflicts > 0 { planConflicts -= 1; return 1 }
            if planRejections > 0 { planRejections -= 1; return 2 }
            return 0
        }
        if action == 1 { throw APIError.conflict }
        if action == 2 { throw APIError.requestFailed(status: 500) }
    }

    func generateOverlay(threadID: UUID, setID: String, overlayID: String) async throws -> OverlaySet {
        record("generate \(setID)/\(overlayID)")
        let key = "\(setID)/\(overlayID)"
        let outcome: (offline: Bool, answer: Answer) = lock.withLock {
            if generateOffline > 0 { generateOffline -= 1; return (true, .ready) }
            var queue = generateAnswers[key] ?? [.ready]
            let next = queue.isEmpty ? Answer.ready : queue.removeFirst()
            generateAnswers[key] = queue
            return (false, next)
        }
        if outcome.offline { throw APIError.offline }
        return overlaySet(setID, overlayID, outcome.answer)
    }

    func generatedMedia(itemID: String, operationID: String) async throws -> GeneratedMediaOperation {
        record("media \(operationID)")
        let json: [String: Any] = ["id": operationID, "state": "ready", "capability": "image", "retryable": false,
                                    "preview_url": "https://generated-media-image.invalid/\(operationID).jpg",
                                    "source_url": "https://generated-media-image.invalid/\(operationID).jpg", "asset_id": "a-\(operationID)"]
        return try JSONDecoder().decode(GeneratedMediaOperation.self, from: JSONSerialization.data(withJSONObject: json))
    }
}
