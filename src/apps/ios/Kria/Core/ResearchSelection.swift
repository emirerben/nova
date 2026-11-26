import Foundation

/// CA-6 (KRI-1645): the sourced facts of a research draft the creator can keep or drop before approving, as the
/// server offers them on `draft_applied.payload.research_preview` (`selection_id`, a `fact_id` per row; CA-5).
///
/// The pick is offered only when every row can be shown and named: a card the phone cannot draw in full would send
/// "all kept" with a fact missing, and the server would drop it unseen. Anything else keeps today's approval card.
/// A draft that carries `research_selection` is the answer to an earlier card, never a question of its own.
struct ResearchSelectionQuestion: Equatable {
    struct Fact: Identifiable, Equatable {
        let id: String
        let text: String
        let window: String?
        let source: ResearchPreview.Source
    }

    let selectionID: String
    let facts: [Fact]

    init?(payload: [String: JSONValue]?) {
        guard let payload, payload["research_selection"] == nil,
              let preview = ResearchPreview.parse(payload["research_preview"]),
              let selectionID = preview.selectionID, preview.droppedRows == 0 else { return nil }
        var seen = Set<String>()
        var facts: [Fact] = []
        for explanation in preview.explanations {
            guard let factID = explanation.factID, seen.insert(factID).inserted, let source = explanation.sources.first else { return nil }
            facts.append(Fact(id: factID, text: explanation.text, window: ResearchPreview.window(explanation), source: source))
        }
        self.selectionID = selectionID
        self.facts = facts
    }

    /// The ask's place in the sequence. The proposal's own message is not in the transcript while its approval card
    /// is up, so the chat adds the ask here; an answer that left no event (all kept) is a local one.
    func foldEntries(proposalID: String, answeredLocally: Bool) -> [AskFold.Entry] {
        var entries = [AskFold.Entry(messageID: proposalID, asks: [AskFold.Ask(id: selectionID, kind: .research, closer: .answer(selectionID))])]
        if answeredLocally {
            entries.append(AskFold.Entry(messageID: "local-research-\(selectionID)", isUser: true,
                                         answers: [AskFold.AnswerKey(kind: .research, value: selectionID)]))
        }
        return entries
    }

    /// Approve is offered once the ask is answered. Open, queued behind another ask, sending, failed and stale all wait.
    static func gatesApproval(phase: AskPhase<ResearchSelectionLabel.Receipt>?) -> Bool {
        switch phase {
        case .answered?: false
        default: true
        }
    }
}

/// `draft_applied.payload.research_selection`: which facts of which card the creator kept (CA-5).
struct ResearchSelectionReceipt: Equatable {
    let selectionID: String
    let keptFactIDs: [String]
    let waived: Bool

    init?(payload: [String: JSONValue]?) {
        guard case .object(let object)? = payload?["research_selection"], let selectionID = object["selection_id"]?.stringValue else { return nil }
        self.selectionID = selectionID
        keptFactIDs = (object["kept_fact_ids"]?.arrayValue ?? []).compactMap(\.stringValue)
        if case .bool(let waived)? = object["waived"] { self.waived = waived } else { waived = keptFactIDs.isEmpty }
    }

    /// What the card reads as an answer. The total is the answered card's own count, so no card in `events` means no
    /// receipt rather than a guess.
    func summary(in events: [ThreadEvent]) -> ResearchSelectionLabel.Receipt? {
        if waived { return ResearchSelectionLabel.waived }
        guard let answered = events.lazy.compactMap({ ResearchSelectionQuestion(payload: $0.payload) })
            .first(where: { $0.selectionID == selectionID }) else { return nil }
        return ResearchSelectionLabel.answered(kept: keptFactIDs.count, total: answered.facts.count)
    }
}

enum ResearchSelectionLabel {
    /// What the creator chose: a small caption over a right-aligned ink capsule.
    struct Receipt: Equatable {
        let caption: String
        let text: String
        var spoken: String { "\(caption) \(text)" }
    }

    /// The primary button names the count. None ticked has no primary: it is the waiver's own confirm.
    static func primary(kept: Int, total: Int) -> String? {
        guard kept > 0 else { return nil }
        if total == 1 { return String(localized: "Use this fact") }
        return kept >= total ? String(localized: "Use all \(total)") : String(localized: "Use \(kept) of \(total)")
    }

    static func answered(kept: Int, total: Int) -> Receipt {
        let text = total <= 1 ? String(localized: "1 fact") : kept >= total ? String(localized: "all \(total) facts") : String(localized: "\(kept) of \(total) facts")
        return Receipt(caption: String(localized: "You kept"), text: text)
    }

    static var waived: Receipt { Receipt(caption: String(localized: "You chose"), text: String(localized: "Continue without facts")) }
}

/// `research_selection` on the submit-turn body, exactly as the server's `ResearchSelectionIn` names it.
struct ResearchSelectionAnswer: Encodable, Equatable, Sendable {
    /// `message` is required on every turn; the server never plans, stores or shows it.
    static let placeholderMessage = "research_selection"
    static let staleCode = "research_selection_stale"

    let selectionID: String
    let keptFactIDs: [String]

    /// The kept ids in the card's own order; an id the card never showed is never sent.
    init(question: ResearchSelectionQuestion, kept: [String]) {
        let ticked = Set(kept)
        selectionID = question.selectionID
        keptFactIDs = question.facts.map(\.id).filter(ticked.contains)
    }

    enum CodingKeys: String, CodingKey { case selectionID = "selection_id"; case keptFactIDs = "kept_fact_ids" }
}

/// Owns the one-turn-at-a-time send of the creator's pick. Lives on the workspace (not in a lazily-built row), so a
/// redraw never loses an attempt. A retry of a failed send carries the same `client_event_id` and the same frozen
/// body, because the server rejects a reused id with a different body (409 `idempotency_key_reused`).
@MainActor final class ResearchSelectionCoordinator: ObservableObject {
    enum Status: Equatable {
        case idle
        case sending(String)
        case failed(String, String)
        /// The card the creator answered is not the draft's card any more; the refreshed list is on screen.
        case stale
    }

    struct Answered: Equatable { let kept: Int; let total: Int }

    struct Transport {
        var revision: () -> Int
        var submit: (SubmitTurnRequest) async throws -> TurnAccepted
        var accepted: (TurnAccepted) async -> Void
        var refresh: () async -> Void
    }

    private struct Attempt {
        let clientEventID: String
        let answer: ResearchSelectionAnswer
        var frozenBody: Data?
    }

    @Published private(set) var status: Status = .idle
    /// Cards answered from here, with what was kept. An all-kept answer writes no event, so this is its only record.
    @Published private(set) var answered: [String: Answered] = [:]
    private var attempt: Attempt?
    private var staleFrom: String?
    private var staleShownOn: String?

    func submit(question: ResearchSelectionQuestion, kept: [String], transport: Transport) async {
        if case .sending = status { return }
        let answer = ResearchSelectionAnswer(question: question, kept: kept)
        var current = Attempt(clientEventID: UUID().uuidString, answer: answer, frozenBody: nil)
        if let attempt, attempt.answer == answer, case .failed = status { current = attempt }
        var request = SubmitTurnRequest(message: ResearchSelectionAnswer.placeholderMessage, clientEventID: current.clientEventID,
                                        expectedThreadRevision: transport.revision(), researchSelection: answer)
        if current.frozenBody == nil { current.frozenBody = try? JSONEncoder().encode(request) }
        request.frozenBody = current.frozenBody
        attempt = current
        status = .sending(question.selectionID)
        do {
            let accepted = try await transport.submit(request)
            attempt = nil
            answered[question.selectionID] = Answered(kept: answer.keptFactIDs.count, total: question.facts.count)
            status = .idle
            await transport.accepted(accepted)
        } catch let error as APIError {
            guard case .conflict(_) = error else { return failed(question.selectionID, Self.connectionText) }
            attempt = nil
            await transport.refresh()
            if error.conflictCode == ResearchSelectionAnswer.staleCode {
                staleFrom = question.selectionID
                staleShownOn = nil
                status = .stale
            } else {
                status = .failed(question.selectionID, error.conflictDetail ?? Self.connectionText)
            }
        } catch {
            failed(question.selectionID, Self.connectionText)
        }
    }

    /// The card on screen changed. A stale notice belongs to the first card after the refresh and goes with it.
    func cardChanged(to selectionID: String) {
        guard status == .stale else { return }
        if staleShownOn == nil {
            if selectionID != staleFrom { staleShownOn = selectionID }
        } else if selectionID != staleShownOn {
            status = .idle
        }
    }

    private func failed(_ selectionID: String, _ text: String) {
        status = .failed(selectionID, text)
    }

    private static var connectionText: String { String(localized: "Couldn’t save your choice. Check your connection.") }
}
