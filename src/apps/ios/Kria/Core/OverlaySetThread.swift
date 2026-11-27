import Foundation

/// One generated-overlay set as the thread last showed it (`overlay_set` event, API `OverlaySetEvent`, v1). The server
/// writes the whole set on every change, so the newest event of a `set_id` is the set.
struct OverlaySet: Equatable, Identifiable, Sendable {
    enum Phase: String, Sendable { case ask, compose, progress, done }
    enum Mode: String, Sendable { case automatic, prompt }

    struct Overlay: Equatable, Identifiable, Sendable {
        enum State: String, Sendable { case making, ready, failed }
        let id: String
        let title: String
        let prompt: String
        let state: State
        let operationID: String?
        let errorCode: String?
        let retryable: Bool

        /// Only a failed overlay whose reservation was released. `retryable: false` may already have been paid for.
        var canRetry: Bool { state == .failed && retryable }
    }

    let id: String
    let phase: Phase
    let mode: Mode?
    let prompts: [String]
    let unitPrice: Decimal
    let maxOverlays: Int
    let overlays: [Overlay]
    let readyIDs: [String]
    let message: String

    var readyCount: Int { overlays.filter { $0.state == .ready }.count }
    var makingCount: Int { overlays.filter { $0.state == .making }.count }
    var failedCount: Int { overlays.filter { $0.state == .failed }.count }

    /// What the review sheet is handed: ready overlays whose image the server can find.
    var reviewItems: [Overlay] { overlays.filter { $0.state == .ready && $0.operationID != nil } }

    /// What the creator chose, in the words of the two buttons.
    var answerText: String { mode == .prompt ? String(localized: "Describe them") : String(localized: "Let Kria decide") }

    /// What the card draws. An answered set is a status card whatever the ask sequence says; an `ask` is drawn only while
    /// it is the open ask, and not after Cancel.
    func askPhase(standing: AskFold.Standing?, isSending: Bool, failed: Bool, isClosed: Bool) -> AskPhase<String>? {
        guard !isClosed else { return nil }
        guard phase == .ask else { return .answered(answerText) }
        guard standing == .open else { return nil }
        if failed { return .failed(String(localized: "Couldn’t send your answer.")) }
        return isSending ? .submitting("") : .open
    }

    func cost(rows: Int) -> Decimal { unitPrice * Decimal(max(rows, 0)) }

    /// Nil for a payload this build cannot read in full: an unknown version, phase or row state draws nothing.
    static func parse(_ payload: [String: JSONValue]?) -> OverlaySet? {
        guard let payload, number(payload["v"]) == 1, let id = payload["set_id"]?.stringValue, !id.isEmpty,
              let phase = payload["phase"]?.stringValue.flatMap(Phase.init(rawValue:)) else { return nil }
        var overlays: [Overlay] = []
        for case .object(let row) in payload["overlays"]?.arrayValue ?? [] {
            guard let rowID = row["id"]?.stringValue, let state = row["state"]?.stringValue.flatMap(Overlay.State.init(rawValue:)) else { return nil }
            overlays.append(Overlay(id: rowID, title: row["title"]?.stringValue ?? "", prompt: row["prompt"]?.stringValue ?? "", state: state,
                                    operationID: row["operation_id"]?.stringValue, errorCode: row["error_code"]?.stringValue,
                                    retryable: row["retryable"]?.objectBool ?? false))
        }
        let price = number(payload["unit_price_usd"]).flatMap { Decimal(string: String($0)) } ?? Decimal(string: "0.04")!
        return OverlaySet(
            id: id, phase: phase, mode: payload["mode"]?.stringValue.flatMap(Mode.init(rawValue:)),
            prompts: (payload["prompts"]?.arrayValue ?? []).compactMap(\.stringValue), unitPrice: price,
            maxOverlays: number(payload["max_overlays"]).map(Int.init) ?? 6, overlays: overlays,
            readyIDs: (payload["overlay_ids"]?.arrayValue ?? []).compactMap(\.stringValue), message: payload["message"]?.stringValue ?? "")
    }

    private static func number(_ value: JSONValue?) -> Double? { if case .number(let number)? = value { number } else { nil } }
}

private extension JSONValue {
    var objectBool: Bool? { if case .bool(let value) = self { value } else { nil } }
}

/// A set drawn in the thread: where it was first asked, and what it last looked like.
struct OverlaySetEntry: Equatable, Identifiable {
    let set: OverlaySet
    let anchorEventID: String
    let sequence: Int

    var id: String { "overlay-set-\(set.id)" }

    /// Only the `ask` phase is a question; every later phase is a status card and never holds the next ask. A typed reply
    /// closes an ask the creator walks away from.
    func askEntry(enabled: Set<AskKind>) -> AskFold.Entry {
        guard enabled.contains(.overlay), set.phase == .ask else { return AskFold.Entry(messageID: id) }
        return AskFold.Entry(messageID: id, asks: [.init(id: set.id, kind: .overlay, closer: .confirmedReply)])
    }
}

enum OverlaySetThread {
    /// One entry per `set_id`, in the order the sets were asked, each showing its newest event. A set whose newest event
    /// cannot be read is left out rather than shown from an older one.
    static func fold(_ events: [ThreadEvent]) -> [OverlaySetEntry] {
        var order: [String] = []
        var anchors: [String: (event: String, sequence: Int)] = [:]
        var newest: [String: OverlaySet?] = [:]
        for event in events.sorted(by: { ($0.sequence, $0.id) < ($1.sequence, $1.id) }) where event.eventType == "overlay_set" {
            guard let id = event.payload?["set_id"]?.stringValue, !id.isEmpty else { continue }
            if anchors[id] == nil { anchors[id] = (event.id, event.sequence); order.append(id) }
            newest[id] = OverlaySet.parse(event.payload)
        }
        return order.compactMap { id in
            guard let set = newest[id] ?? nil, let anchor = anchors[id] else { return nil }
            return OverlaySetEntry(set: set, anchorEventID: anchor.event, sequence: anchor.sequence)
        }
    }
}

/// The prompt rows of "Describe your overlays". The server keeps 1 to `max_overlays` rows of at most 300 characters and
/// cuts a longer prompt without saying so, so the phone never holds more than it sends.
struct OverlayComposeDraft: Equatable {
    static let maxPromptLength = 300
    let maxRows: Int
    private(set) var rows: [String] = [""]

    init(maxRows: Int) { self.maxRows = max(maxRows, 1) }

    var filled: [String] { rows.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty } }
    var canMake: Bool { !filled.isEmpty }
    var canAdd: Bool { rows.count < maxRows }

    mutating func set(_ index: Int, _ text: String) {
        guard rows.indices.contains(index) else { return }
        rows[index] = String(text.prefix(Self.maxPromptLength))
    }

    mutating func add() { if canAdd { rows.append("") } }

    mutating func remove(_ index: Int) {
        guard rows.indices.contains(index) else { return }
        if rows.count == 1 { rows[0] = "" } else { rows.remove(at: index) }
    }
}

enum OverlayStep: Hashable {
    case plan(String)
    case generate(set: String, overlay: String)
}

/// What this run has already started. A paid call is started at most once per run for a thing the thread shows; a
/// relaunch is a new run and starts again, which the server answers from the stored operation.
struct OverlayDriverMemory: Equatable {
    var planned: Set<String> = []
    var started: Set<String> = []

    static func key(set: String, overlay: String) -> String { "\(set)/\(overlay)" }
}

enum OverlayDriver {
    /// A stored answer with no plan is planned (a relaunch lost the call). An overlay still `making` is asked for again.
    /// A failed overlay is never in here: retrying one is the creator's tap.
    static func steps(for sets: [OverlaySet], memory: OverlayDriverMemory) -> [OverlayStep] {
        var steps: [OverlayStep] = []
        for set in sets {
            switch set.phase {
            case .compose where set.mode != nil && !memory.planned.contains(set.id):
                steps.append(.plan(set.id))
            case .progress:
                for overlay in set.overlays where overlay.state == .making && !memory.started.contains(OverlayDriverMemory.key(set: set.id, overlay: overlay.id)) {
                    steps.append(.generate(set: set.id, overlay: overlay.id))
                }
            default:
                break
            }
        }
        return steps
    }
}

/// The calls the chat flow makes. `KriaAPIClient` inherits it, and tests use a small fake.
protocol OverlaySetClient: Sendable {
    func openOverlaySet(threadID: UUID, setID: String) async throws -> OverlaySet
    func composeOverlaySet(threadID: UUID, setID: String, mode: OverlaySet.Mode, prompts: [String]) async throws -> OverlaySet
    func planOverlaySet(threadID: UUID, setID: String, body: OverlayPlanRequest) async throws
    func generateOverlay(threadID: UUID, setID: String, overlayID: String) async throws -> OverlaySet
    func generatedMedia(itemID: String, operationID: String) async throws -> GeneratedMediaOperation
}
