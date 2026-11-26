import XCTest
@testable import Kria

/// CA-6 (KRI-1645): the creator picks which sourced facts of a research draft stay in the video, in the chat, before
/// approving. Failure modes, each pinned below or in `ResearchSelectionUITests`:
///
///  1. A preview with ids and a preview without them: the first offers the pick, the second keeps today's card.
///                                                          testPreviewWithIdsOffersThePick, testPreviewWithoutIdsKeepsTheLegacyCard
///  2. Ids of the wrong shape, a row with no fact id, a text layer, or a row the phone drops for lacking a public
///     source never offer the pick: "Use all N" would silently narrow away the fact nobody saw.
///                                                          testAHalfIdentifiedPreviewNeverOffersThePick
///  3. A narrowed draft carries ids too, yet it is an answer and never a second question; a waived draft has none.
///                                                          testASelectionReceiptIsNotAQuestion
///  4. The primary names the count for all, some and one; none ticked has no primary at all (it is a waiver).
///                                                          testTheCountLabelForAllSomeAndNone
///  5. The wire body is exactly `research_selection {selection_id, kept_fact_ids}` plus the placeholder message,
///     in the facts' own order, and every key is one the generated contract names.
///                                                          testTheWireBodyMatchesTheContract
///  6. A retry of a failed send resends the same client event id and the same body (a reused id with another body
///     is a 409); a different pick is a new request with a new id.
///                                                          testRetryResendsTheSameBodyAndEventId
///  7. A stale 409 clears the attempt, refreshes and says so; another 409 discards the attempt too; a lost
///     connection keeps it; a second tap while one is in flight sends nothing.
///                                                          testStaleRefreshesAndOtherConflictsDiscardTheAttempt, testASecondTapWhileSendingSendsNothing
///  8. The approval is gated until the ask is answered; all kept is answered locally; an earlier open ask keeps
///     the research ask (and so the approval) waiting; a new draft with a receipt is not gated.
///                                                          testTheApprovalWaitsForTheAsk
///  9. The receipt reads kept and total from the server's own events, and nothing when the earlier card is gone.
///                                                          testTheReceiptReadsTheEarlierCardsTotal
/// 10. Long Turkish text, the source host tap, the dimmed controls while saving, and the stale and failed cards:
///                                                          ResearchSelectionUITests
/// 11. EN/TR/DE strings: `scripts/ios/localization.py check`, `scan` and `built`.
final class ResearchSelectionTests: XCTestCase {
    // MARK: fixtures

    private static let selectionID = String(repeating: "ab", count: 32)
    private static let facts = ["1111111111111111", "2222222222222222", "3333333333333333"]

    private func json(_ text: String) -> [String: JSONValue] {
        (try? JSONDecoder().decode([String: JSONValue].self, from: Data(text.utf8))) ?? [:]
    }

    private func row(_ text: String, id: String?, start: Double? = 3, end: Double? = 7, url: String = "https://www.britannica.com/topic/Galata-Tower") -> String {
        let idKey = id.map { #""fact_id":"\#($0)","# } ?? ""
        let window = start.flatMap { start in end.map { #""start_s":\#(start),"end_s":\#($0),"# } } ?? ""
        return #"{\#(idKey)"text":"\#(text)",\#(window)"sources":[{"title":"Galata Tower | History","url":"\#(url)"}]}"#
    }

    private func preview(selection: String? = ResearchSelectionTests.selectionID, rows: [String]? = nil, extra: String = "") -> String {
        let rows = rows ?? ResearchSelectionTests.facts.enumerated().map { row("Fact \($0.offset + 1).", id: $0.element) }
        let selectionKey = selection.map { #""selection_id":"\#($0)","# } ?? ""
        return #"{"version":1,"retrieved_at":"2026-11-26T09:00:00Z",\#(selectionKey)"explanations":[\#(rows.joined(separator: ","))]\#(extra)}"#
    }

    private func payload(_ preview: String?, receipt: String? = nil) -> [String: JSONValue] {
        var parts = [#""turn_id":"turn-1""#]
        if let preview { parts.append(#""research_preview":\#(preview)"#) }
        if let receipt { parts.append(#""research_selection":\#(receipt)"#) }
        return json("{" + parts.joined(separator: ",") + "}")
    }

    private func question(_ preview: String? = nil) throws -> ResearchSelectionQuestion {
        try XCTUnwrap(ResearchSelectionQuestion(payload: payload(preview ?? self.preview())))
    }

    private func event(_ id: String, _ sequence: Int, type: String = "draft_applied", payload: [String: JSONValue]?) -> ThreadEvent {
        ThreadEvent(id: id, sequence: sequence, revision: sequence, role: "assistant", eventType: type, content: "Draft.",
                    payload: payload, createdAt: Date(timeIntervalSince1970: 0))
    }

    // MARK: 1-3 parsing

    func testPreviewWithIdsOffersThePick() throws {
        let question = try question()
        XCTAssertEqual(question.selectionID, Self.selectionID)
        XCTAssertEqual(question.facts.map(\.id), Self.facts)
        XCTAssertEqual(question.facts.map(\.text), ["Fact 1.", "Fact 2.", "Fact 3."])
        XCTAssertEqual(question.facts.first?.window, "0:03–0:07")
        XCTAssertEqual(question.facts.first?.source.domain, "britannica.com")
    }

    func testPreviewWithoutIdsKeepsTheLegacyCard() throws {
        let noIds = preview(selection: nil, rows: Self.facts.map { _ in row("A fact.", id: nil) })
        XCTAssertNil(ResearchSelectionQuestion(payload: payload(noIds)))
        let legacy = try XCTUnwrap(ResearchPreview.parse(payload(noIds)["research_preview"]))
        XCTAssertEqual(legacy.explanations.count, 3, "today's display is unchanged")
        XCTAssertNil(legacy.selectionID)
        XCTAssertTrue(legacy.explanations.allSatisfy { $0.factID == nil })
        XCTAssertNil(ResearchSelectionQuestion(payload: nil))
        XCTAssertNil(ResearchSelectionQuestion(payload: payload(nil)))
    }

    func testAHalfIdentifiedPreviewNeverOffersThePick() throws {
        let rows = Self.facts.map { row("A fact.", id: $0) }
        func offered(_ preview: String) -> Bool { ResearchSelectionQuestion(payload: payload(preview)) != nil }
        XCTAssertTrue(offered(preview(rows: rows)))
        XCTAssertFalse(offered(preview(selection: "abc", rows: rows)), "a selection id of the wrong shape")
        XCTAssertFalse(offered(preview(selection: String(repeating: "AB", count: 32), rows: rows)), "upper case is not the server's id")
        XCTAssertFalse(offered(preview(rows: [rows[0], row("No id.", id: nil), rows[2]])), "one row without a fact id")
        XCTAssertFalse(offered(preview(rows: [rows[0], row("Short.", id: "1234"), rows[2]])), "a fact id of the wrong shape")
        XCTAssertFalse(offered(preview(rows: [rows[0], row("Private.", id: "4444444444444444", url: "http://intranet.local/x"), rows[2]])),
                       "a row the phone drops for lacking a public source would be narrowed away unseen")
        XCTAssertFalse(offered(preview(rows: [rows[0], rows[0]])), "two rows with one fact id")
        XCTAssertFalse(offered(preview(rows: [])), "nothing to pick")
    }

    func testASelectionReceiptIsNotAQuestion() throws {
        let receipt = #"{"selection_id":"\#(Self.selectionID)","kept_fact_ids":["\#(Self.facts[0])","\#(Self.facts[2])"],"waived":false}"#
        XCTAssertNil(ResearchSelectionQuestion(payload: payload(preview(), receipt: receipt)), "the narrowed draft is the answer")
        let parsed = try XCTUnwrap(ResearchSelectionReceipt(payload: payload(preview(), receipt: receipt)))
        XCTAssertEqual(parsed.selectionID, Self.selectionID)
        XCTAssertEqual(parsed.keptFactIDs, [Self.facts[0], Self.facts[2]])
        XCTAssertFalse(parsed.waived)
        let waived = #"{"selection_id":"\#(Self.selectionID)","kept_fact_ids":[],"waived":true}"#
        XCTAssertTrue(try XCTUnwrap(ResearchSelectionReceipt(payload: payload(nil, receipt: waived))).waived)
        XCTAssertNil(ResearchSelectionReceipt(payload: payload(preview())), "a draft with no selection has no receipt")
    }

    // MARK: 4 labels

    func testTheCountLabelForAllSomeAndNone() {
        XCTAssertEqual(ResearchSelectionLabel.primary(kept: 3, total: 3), "Use all 3")
        XCTAssertEqual(ResearchSelectionLabel.primary(kept: 2, total: 3), "Use 2 of 3")
        XCTAssertEqual(ResearchSelectionLabel.primary(kept: 1, total: 3), "Use 1 of 3")
        XCTAssertEqual(ResearchSelectionLabel.primary(kept: 1, total: 1), "Use this fact")
        XCTAssertNil(ResearchSelectionLabel.primary(kept: 0, total: 3), "none ticked is a waiver, not a count")
        XCTAssertEqual(ResearchSelectionLabel.answered(kept: 2, total: 3).text, "2 of 3 facts")
        XCTAssertEqual(ResearchSelectionLabel.answered(kept: 2, total: 3).spoken, "You kept 2 of 3 facts")
        XCTAssertEqual(ResearchSelectionLabel.answered(kept: 3, total: 3).text, "all 3 facts")
        XCTAssertEqual(ResearchSelectionLabel.answered(kept: 1, total: 1).text, "1 fact")
        XCTAssertEqual(ResearchSelectionLabel.waived.spoken, "You chose Continue without facts")
    }

    // MARK: 5 wire

    func testTheWireBodyMatchesTheContract() throws {
        let question = try question()
        let answer = ResearchSelectionAnswer(question: question, kept: [Self.facts[2], Self.facts[0]])
        XCTAssertEqual(answer.keptFactIDs, [Self.facts[0], Self.facts[2]], "in the facts' own order, whatever order they were ticked in")
        let request = SubmitTurnRequest(message: ResearchSelectionAnswer.placeholderMessage, clientEventID: "evt-1",
                                        expectedThreadRevision: 7, researchSelection: answer)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(request)) as? [String: Any])
        XCTAssertEqual(Set(body.keys), ["message", "client_event_id", "expected_thread_revision", "research_selection"])
        let selection = try XCTUnwrap(body["research_selection"] as? [String: Any])
        XCTAssertEqual(selection["selection_id"] as? String, Self.selectionID)
        XCTAssertEqual(selection["kept_fact_ids"] as? [String], [Self.facts[0], Self.facts[2]])
        XCTAssertEqual(Set(selection.keys), ["selection_id", "kept_fact_ids"])

        let schemas = try generatedSchemas()
        XCTAssertTrue(Set(body.keys).isSubset(of: try properties("SubmitTurnBody", in: schemas)), "every body key is in the generated contract")
        XCTAssertEqual(Set(selection.keys), try properties("ResearchSelectionIn", in: schemas))
        XCTAssertTrue(try properties("CreationCapabilitiesOut", in: schemas).contains("research_selections"))
        let none = ResearchSelectionAnswer(question: question, kept: [])
        XCTAssertEqual(none.keptFactIDs, [], "an empty list is the waiver and is still sent")
        let ids = Set(Self.facts)
        XCTAssertEqual(ResearchSelectionAnswer(question: question, kept: ["not-a-fact"] + Self.facts).keptFactIDs.filter { !ids.contains($0) }, [],
                       "an id the card never showed is never sent")
    }

    private func generatedSchemas() throws -> [String: Any] {
        let url = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Kria/Generated/openapi.yaml")
        let spec = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        return try XCTUnwrap((spec["components"] as? [String: Any])?["schemas"] as? [String: Any])
    }

    private func properties(_ name: String, in schemas: [String: Any]) throws -> Set<String> {
        Set(try XCTUnwrap(((schemas[name] as? [String: Any])?["properties"] as? [String: Any])).keys)
    }

    func testTheCapabilityDecodesAndAnOlderServerDoesNotSayIt() throws {
        func caps(_ extra: String) throws -> CreationCapabilities {
            try JSONDecoder().decode(CreationCapabilities.self, from: Data(#"{"formats":[]\#(extra)}"#.utf8))
        }
        XCTAssertEqual(try caps(#","research_selections":true"#).researchSelectionsEnabled, true)
        XCTAssertEqual(try caps(#","research_selections":false"#).researchSelectionsEnabled, false)
        XCTAssertEqual(try caps("").researchSelectionsEnabled, false, "an older server does not say")
        XCTAssertEqual(try caps(#","research_selections":"yes""#).researchSelectionsEnabled, false, "a garbled value never fails the load")
    }

    // MARK: 6-7 coordinator

    @MainActor private final class Wire {
        var requests: [SubmitTurnRequest] = []
        var results: [Result<TurnAccepted, Error>] = []
        var revision = 7
        var accepted = 0
        var refreshed = 0
        var gate: CheckedContinuation<Void, Never>?
        var holdFirst = false

        var transport: ResearchSelectionCoordinator.Transport {
            .init(
                revision: { self.revision },
                submit: { request in
                    self.requests.append(request)
                    if self.holdFirst, self.requests.count == 1 { await withCheckedContinuation { self.gate = $0 } }
                    let next = self.results.isEmpty ? .success(TurnAccepted(turnID: "t", threadRevision: 9, status: "awaiting_approval")) : self.results.removeFirst()
                    return try next.get()
                },
                accepted: { _ in self.accepted += 1 },
                refresh: { self.refreshed += 1 })
        }

        func body(_ index: Int) throws -> [String: Any] {
            try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder().encode(requests[index])) as? [String: Any])
        }
    }

    private func conflict(_ code: String, _ message: String? = nil) -> APIError {
        .conflict(detail: ConflictDetail(message ?? code, code: code, recovery: nil))
    }

    @MainActor func testRetryResendsTheSameBodyAndEventId() async throws {
        let wire = Wire(), coordinator = ResearchSelectionCoordinator(), question = try question()
        wire.results = [.failure(APIError.offline)]
        await coordinator.submit(question: question, kept: [Self.facts[0], Self.facts[1]], transport: wire.transport)
        guard case .failed(let selection, _) = coordinator.status else { return XCTFail("a lost connection is a failure, got \(coordinator.status)") }
        XCTAssertEqual(selection, Self.selectionID)
        XCTAssertTrue(coordinator.answered.isEmpty, "a failed send answers nothing")
        wire.revision = 12
        await coordinator.submit(question: question, kept: [Self.facts[0], Self.facts[1]], transport: wire.transport)
        XCTAssertEqual(coordinator.status, .idle)
        XCTAssertEqual(wire.requests.count, 2)
        let first = try wire.body(0), retry = try wire.body(1)
        XCTAssertEqual(first["client_event_id"] as? String, retry["client_event_id"] as? String, "the same client event id")
        XCTAssertEqual(retry["expected_thread_revision"] as? Int, 7, "the revision is frozen with the body")
        XCTAssertEqual(NSDictionary(dictionary: first), NSDictionary(dictionary: retry), "the same body")
        XCTAssertEqual(coordinator.answered[Self.selectionID], .init(kept: 2, total: 3))
        XCTAssertEqual(wire.accepted, 1)

        wire.results = [.failure(APIError.offline)]
        await coordinator.submit(question: question, kept: [Self.facts[0]], transport: wire.transport)
        await coordinator.submit(question: question, kept: [Self.facts[1]], transport: wire.transport)
        let third = try wire.body(2), fourth = try wire.body(3)
        XCTAssertNotEqual(third["client_event_id"] as? String, first["client_event_id"] as? String, "a new request is a new id")
        XCTAssertNotEqual(fourth["client_event_id"] as? String, third["client_event_id"] as? String, "another pick after a failure is a new request")
    }

    @MainActor func testStaleRefreshesAndOtherConflictsDiscardTheAttempt() async throws {
        let wire = Wire(), coordinator = ResearchSelectionCoordinator(), question = try question()
        wire.results = [.failure(conflict(ResearchSelectionAnswer.staleCode))]
        await coordinator.submit(question: question, kept: [Self.facts[0]], transport: wire.transport)
        XCTAssertEqual(coordinator.status, .stale, "the facts changed under the card")
        XCTAssertEqual(wire.refreshed, 1, "the newer list is fetched")
        XCTAssertTrue(coordinator.answered.isEmpty)
        await coordinator.submit(question: question, kept: [Self.facts[0]], transport: wire.transport)
        XCTAssertNotEqual(try wire.body(0)["client_event_id"] as? String, try wire.body(1)["client_event_id"] as? String,
                          "picking again after a stale card is a new request")
        XCTAssertEqual(coordinator.status, .idle)

        wire.results = [.failure(conflict("queued_successor_exists", "A follow-up is already waiting."))]
        await coordinator.submit(question: question, kept: [Self.facts[1]], transport: wire.transport)
        guard case .failed(_, let text) = coordinator.status else { return XCTFail("another conflict fails, got \(coordinator.status)") }
        XCTAssertEqual(text, "A follow-up is already waiting.", "the server's own reason, since a retry cannot fix it")
        XCTAssertEqual(wire.refreshed, 2)
        await coordinator.submit(question: question, kept: [Self.facts[1]], transport: wire.transport)
        XCTAssertNotEqual(try wire.body(2)["client_event_id"] as? String, try wire.body(3)["client_event_id"] as? String,
                          "a conflict discards the attempt, so the next try is fresh")
    }

    @MainActor func testAStaleNoticeLeavesWithTheCardItDescribes() async throws {
        let wire = Wire(), coordinator = ResearchSelectionCoordinator(), question = try question()
        let refreshed = String(repeating: "cd", count: 32)
        wire.results = [.failure(conflict(ResearchSelectionAnswer.staleCode))]
        await coordinator.submit(question: question, kept: [Self.facts[0]], transport: wire.transport)
        XCTAssertEqual(coordinator.status, .stale)
        coordinator.cardChanged(to: Self.selectionID)
        XCTAssertEqual(coordinator.status, .stale, "the old card is still the one on screen")
        coordinator.cardChanged(to: refreshed)
        XCTAssertEqual(coordinator.status, .stale, "the refreshed card carries the notice")
        coordinator.cardChanged(to: String(repeating: "ef", count: 32))
        XCTAssertEqual(coordinator.status, .idle, "a later card is a different story")
    }

    @MainActor func testASecondTapWhileSendingSendsNothing() async throws {
        let wire = Wire(), coordinator = ResearchSelectionCoordinator(), question = try question()
        wire.holdFirst = true
        let first = Task { await coordinator.submit(question: question, kept: Self.facts, transport: wire.transport) }
        for _ in 0..<50 where wire.gate == nil { await Task.yield() }
        XCTAssertEqual(coordinator.status, .sending(Self.selectionID))
        await coordinator.submit(question: question, kept: Self.facts, transport: wire.transport)
        XCTAssertEqual(wire.requests.count, 1, "one turn in flight")
        wire.gate?.resume()
        await first.value
        XCTAssertEqual(coordinator.status, .idle)
        XCTAssertEqual(coordinator.answered[Self.selectionID], .init(kept: 3, total: 3), "all kept is answered here: the server replays with no new draft")
    }

    // MARK: 8 sequencing

    private func ask(_ message: String, _ kind: AskKind, _ id: String) -> AskFold.Entry {
        AskFold.Entry(messageID: message, asks: [AskFold.Ask(id: id, kind: kind, closer: .answer(id))])
    }

    func testTheApprovalWaitsForTheAsk() throws {
        let question = try question()
        let receipt = ResearchSelectionLabel.answered(kept: 3, total: 3)
        func phase(_ entries: [AskFold.Entry], answered: Bool = false) -> AskPhase<ResearchSelectionLabel.Receipt>? {
            let fold = AskFold.fold(entries + question.foldEntries(proposalID: "draft-1", answeredLocally: answered))
            return fold.phase("draft-1", question.selectionID, answer: { receipt })
        }
        XCTAssertEqual(phase([]), .open)
        XCTAssertTrue(ResearchSelectionQuestion.gatesApproval(phase: phase([])), "open: no approve action")
        XCTAssertEqual(phase([], answered: true), .answered(receipt))
        XCTAssertFalse(ResearchSelectionQuestion.gatesApproval(phase: phase([], answered: true)), "answered: approve is offered")
        let behind = phase([ask("m0", .choice, "c1")])
        XCTAssertNil(behind, "an earlier open ask goes first and the research card waits")
        XCTAssertTrue(ResearchSelectionQuestion.gatesApproval(phase: behind), "waiting is not answered")
        XCTAssertEqual(phase([ask("m0", .choice, "c1"), AskFold.Entry(messageID: "u", isUser: true, answers: [.init(kind: .choice, value: "c1")])]),
                       .open, "the choice answered, the research ask opens")
        XCTAssertTrue(ResearchSelectionQuestion.gatesApproval(phase: .submitting(receipt)))
        XCTAssertTrue(ResearchSelectionQuestion.gatesApproval(phase: .failed("x")))
        XCTAssertTrue(ResearchSelectionQuestion.gatesApproval(phase: .stale))
    }

    // MARK: 9 receipt

    func testTheReceiptReadsTheEarlierCardsTotal() throws {
        let earlier = event("d1", 1, payload: payload(preview()))
        let newSelection = String(repeating: "cd", count: 32)
        let narrowed = preview(selection: newSelection, rows: [row("Fact 1.", id: Self.facts[0]), row("Fact 3.", id: Self.facts[2])])
        let receipt = #"{"selection_id":"\#(Self.selectionID)","kept_fact_ids":["\#(Self.facts[0])","\#(Self.facts[2])"],"waived":false}"#
        let later = event("d2", 2, payload: payload(narrowed, receipt: receipt))
        let summary = try XCTUnwrap(ResearchSelectionReceipt(payload: later.payload)).summary(in: [earlier, later])
        XCTAssertEqual(summary, ResearchSelectionLabel.answered(kept: 2, total: 3))
        XCTAssertNil(try XCTUnwrap(ResearchSelectionReceipt(payload: later.payload)).summary(in: [later]), "no earlier card, no total, no receipt")
        let waived = #"{"selection_id":"\#(Self.selectionID)","kept_fact_ids":[],"waived":true}"#
        let waivedEvent = event("d3", 3, payload: payload(nil, receipt: waived))
        XCTAssertEqual(try XCTUnwrap(ResearchSelectionReceipt(payload: waivedEvent.payload)).summary(in: [earlier, waivedEvent]),
                       ResearchSelectionLabel.waived)
    }
}
