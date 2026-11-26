import Combine
import Foundation

// KRI-1591: the clip-order card. Before the creator approves a plan, the draft message carries the order the
// clips will play in as a numbered strip (`clip_order_card` on the event payload). The creator can change it in a
// sheet and confirm it: the whole order goes back as ONE `clip_order_ops` turn that makes no model call and adds
// no chat message. The server answers with a fresh `clip_order_card` event, and that newest card is what the
// strip shows.
//
// Hand-rolled tolerant parsing on purpose, like `SongOrderQuestion`: an absent, newer or malformed card means
// "no strip" (the text still shows), never a decode failure that breaks the thread. Everything wire-shaped is in
// this file; the models are pure values so the order logic is unit-testable. Server contract:
// docs/pipelines/kria-agent-runtime.md "The clip-order card".

/// One `clip_order_card` payload, plus the event it came from.
struct ClipOrderCard: Equatable, Sendable {
    /// Where a clip's position came from. An unknown value (a newer server) is kept, never a decode failure.
    enum Source: Equatable, Sendable {
        case serverDefault, creatorText, creatorDrag, creatorEditor, aiStory
        case unknown(String)

        init(wire: String?) {
            switch wire {
            case "server_default": self = .serverDefault
            case "creator_text": self = .creatorText
            case "creator_drag": self = .creatorDrag
            case "creator_editor": self = .creatorEditor
            case "ai_story": self = .aiStory
            case let other?: self = .unknown(other)
            case nil: self = .unknown("")
            }
        }

        /// The one badge a sheet row wears; nil for a source this build cannot name.
        var origin: Origin? {
            switch self {
            case .creatorText, .creatorDrag, .creatorEditor: .yours
            case .serverDefault: .upload
            case .aiStory: .ai
            case .unknown: nil
            }
        }
    }

    enum Origin: Equatable, Sendable { case yours, upload, ai }

    struct Item: Equatable, Sendable, Identifiable {
        let mediaID: String
        let position: Int
        /// The server's words for this clip, exactly as sent. Show `ClipOrderLabel.text`, never this raw value.
        let label: String?
        /// Anything but `image` plays as a video.
        let isPhoto: Bool
        let group: String?
        let source: Source
        let moved: Bool
        var id: String { mediaID }
    }

    let eventID: String
    let sequence: Int
    let proposalID: String
    /// The strip is in the chat. False = render nothing.
    let shown: Bool
    /// The creator tapped "use this order" for exactly this order.
    let confirmed: Bool
    /// False once a video exists or its render was approved: the strip stays, the buttons go.
    let editable: Bool
    let total: Int
    /// Only on a drag's own card: the server did not apply the list as sent.
    let rebased: Bool
    let items: [Item]

    var orderedMediaIDs: [String] { items.map(\.mediaID) }

    /// The wire allows 200 ids per drag; a longer card is shown read-only rather than sent short.
    static let maxOrderedIDs = 200
    var allowsEditing: Bool { shown && editable && !items.isEmpty && items.count <= Self.maxOrderedIDs }

    /// nil when the payload has no usable card (absent, no proposal id, no clips).
    static func parse(payload: [String: JSONValue]?, eventID: String, sequence: Int) -> ClipOrderCard? {
        guard let fields = payload?["clip_order_card"]?.objectValue,
              let proposalID = fields["proposal_id"]?.stringValue, !proposalID.isEmpty else { return nil }
        var seen = Set<String>()
        let parsed: [(index: Int, item: Item)] = (fields["cards"]?.arrayValue ?? []).enumerated().compactMap { index, entry in
            guard let object = entry.objectValue,
                  let mediaID = object["media_id"]?.stringValue, !mediaID.isEmpty, seen.insert(mediaID).inserted else { return nil }
            let position = object["position"]?.numberValue.flatMap { $0.isFinite && abs($0) < 1e6 ? Int($0) : nil } ?? index + 1
            return (index, Item(
                mediaID: mediaID, position: position, label: object["label"]?.stringValue,
                isPhoto: object["kind"]?.stringValue == "image", group: object["group"]?.stringValue,
                source: Source(wire: object["source"]?.stringValue), moved: object["moved"]?.booleanValue ?? false))
        }
        guard !parsed.isEmpty else { return nil }
        // Play order is `position`; a missing or repeated position falls back to the order the server listed.
        let items = parsed.sorted { ($0.item.position, $0.index) < ($1.item.position, $1.index) }.map(\.item)
        let total = fields["total"]?.numberValue.flatMap { $0.isFinite && $0 >= 0 && $0 < 1e6 ? Int($0) : nil } ?? items.count
        return ClipOrderCard(
            eventID: eventID, sequence: sequence, proposalID: proposalID,
            shown: fields["shown"]?.booleanValue ?? false, confirmed: fields["confirmed"]?.booleanValue ?? false,
            editable: fields["editable"]?.booleanValue ?? true, total: max(total, items.count),
            rebased: fields["rebased"]?.booleanValue ?? false, items: items)
    }

    func item(_ mediaID: String) -> Item? { items.first { $0.mediaID == mediaID } }
}

/// What a clip is called on screen. The server sends a label that is already safe to show; the phone never
/// builds one from a media id or a file name, and a label that looks like one falls back to "Clip N".
enum ClipOrderLabel {
    static func text(_ item: ClipOrderCard.Item, number: Int? = nil) -> String {
        let n = number ?? item.position
        guard let raw = item.label?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty,
              raw != item.mediaID, !looksLikeAnIdentifier(raw) else { return String(localized: "Clip \(n)") }
        return raw
    }

    private static let fileSuffix = try! NSRegularExpression(pattern: #"\.(mp4|mov|m4v|heic|heif|jpe?g|png|webp)$"#, options: .caseInsensitive)
    private static let hexRun = try! NSRegularExpression(pattern: #"^[0-9a-f]{12,}$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"#, options: .caseInsensitive)

    private static func looksLikeAnIdentifier(_ text: String) -> Bool {
        let range = NSRange(text.startIndex..., in: text)
        return fileSuffix.firstMatch(in: text, range: range) != nil || hexRun.firstMatch(in: text, range: range) != nil
    }
}

extension ThreadEvent {
    /// The creator's own drag turn: a stored user event whose payload carries `clip_order_ops`. It is a gesture, not
    /// something the creator said, so no transcript reader may show it or count it as a message.
    var isClipOrderDrag: Bool { role == "user" && payload?["clip_order_ops"]?.objectValue != nil }

    func clipOrderCard() -> ClipOrderCard? {
        role == "assistant" ? ClipOrderCard.parse(payload: payload, eventID: id, sequence: sequence) : nil
    }
}

/// The newest card in the thread, and which message rows it sits under. The newest card is what the creator last
/// saw (a drag's own event carries one with no message of its own), so the fold runs over events, not messages.
struct ClipOrderThread: Equatable {
    let newest: ClipOrderCard?
    /// The card before the newest: the order the server marked `moved` against.
    let previous: ClipOrderCard?
    /// The latest message that carries a card: the strip is drawn under it.
    let anchorMessageID: String?

    static let empty = ClipOrderThread(newest: nil, previous: nil, anchorMessageID: nil)

    static func fold(_ events: [ThreadEvent], isMessage: (ThreadEvent) -> Bool = { ChatTranscriptMessage.from(event: $0) != nil }) -> ClipOrderThread {
        var newest: ClipOrderCard?
        var previous: ClipOrderCard?
        var carriers: [String] = []
        for event in events.sorted(by: { ($0.sequence, $0.id) < ($1.sequence, $1.id) }) {
            guard let card = event.clipOrderCard() else { continue }
            previous = newest
            newest = card
            if isMessage(event) { carriers.append(event.id) }
        }
        // Nothing is drawn unless the newest card says the strip is shown (a thread with no card, or one the server
        // never showed, looks exactly as it did before this feature).
        guard let newest, newest.shown, let anchor = carriers.last else { return .empty }
        return ClipOrderThread(newest: newest, previous: previous, anchorMessageID: anchor)
    }
}

/// "was 3rd": where a moved clip stood before. The phone derives it from two orders it already holds and never
/// asks the server, so a clip that did not change place says nothing.
enum ClipOrderWas {
    static func positions(base: [String]?, order: [String], moved: Set<String>) -> [String: Int] {
        guard let base else { return [:] }
        var out: [String: Int] = [:]
        for id in moved {
            guard let before = base.firstIndex(of: id), let now = order.firstIndex(of: id), before != now else { continue }
            out[id] = before + 1
        }
        return out
    }

    static func ordinal(_ position: Int, locale: Locale = .current) -> String {
        let formatter = NumberFormatter()
        formatter.numberStyle = .ordinal
        formatter.locale = locale
        return formatter.string(from: NSNumber(value: position)) ?? "\(position)"
    }

    static func text(_ position: Int) -> String { String(localized: "was \(ordinal(position))") }
}

/// `clip_order_ops` on the submit-turn body, exactly as the server's `ClipOrderOpsIn` names it.
struct ClipOrderOps: Encodable, Equatable, Sendable {
    let proposalID: String
    let orderedMediaIDs: [String]
    let confirm: Bool
    enum CodingKeys: String, CodingKey { case proposalID = "proposal_id", orderedMediaIDs = "ordered_media_ids", confirm }

    /// `message` is required on every turn; the server never plans, stores or shows it.
    static let placeholderMessage = "clip_order_ops"

    init(proposalID: String, orderedMediaIDs: [String], confirm: Bool) {
        self.proposalID = String(proposalID.prefix(64))
        self.orderedMediaIDs = Array(orderedMediaIDs.prefix(ClipOrderCard.maxOrderedIDs))
        self.confirm = confirm
    }
}

/// The creator's working order in the change-order sheet. Local until they confirm: nothing here is sent.
struct ClipOrderDraft: Equatable {
    private(set) var order: [String]
    /// What the sheet opened with, so "changed" means changed from what the creator saw.
    let opening: [String]
    /// Clips the creator dragged or nudged themselves (drives the "was N" caption before the server answers).
    private(set) var moved: Set<String> = []

    init(order: [String]) { self.order = order; opening = order }

    var isChanged: Bool { order != opening }
    func position(of mediaID: String) -> Int? { order.firstIndex(of: mediaID).map { $0 + 1 } }
    func canMoveUp(_ mediaID: String) -> Bool { (order.firstIndex(of: mediaID) ?? 0) > 0 }
    /// Where the creator's own move took `mediaID` from, when it is not back where it stood.
    func was(_ mediaID: String) -> Int? {
        guard moved.contains(mediaID), let before = opening.firstIndex(of: mediaID), let now = order.firstIndex(of: mediaID), before != now else { return nil }
        return before + 1
    }
    func canMoveDown(_ mediaID: String) -> Bool { order.firstIndex(of: mediaID).map { $0 < order.count - 1 } ?? false }

    /// `List.onMove` semantics: `destination` is an index into the pre-move array.
    mutating func move(from source: IndexSet, to destination: Int) {
        guard !source.isEmpty, source.allSatisfy({ order.indices.contains($0) }) else { return }
        let moving = source.sorted().map { order[$0] }
        let insertion = destination - source.filter { $0 < destination }.count
        for index in source.sorted(by: >) { order.remove(at: index) }
        order.insert(contentsOf: moving, at: max(0, min(order.count, insertion)))
        moved.formUnion(moving)
    }

    mutating func moveUp(_ mediaID: String) {
        guard let index = order.firstIndex(of: mediaID), index > 0 else { return }
        order.swapAt(index, index - 1)
        moved.insert(mediaID)
    }

    mutating func moveDown(_ mediaID: String) {
        guard let index = order.firstIndex(of: mediaID), index < order.count - 1 else { return }
        order.swapAt(index, index + 1)
        moved.insert(mediaID)
    }
}

/// What has been confirmed and not yet acknowledged. One attempt is in flight at a time; a newer confirm while it
/// runs replaces whatever was waiting (the creator's last full order is the only one that matters), and a retry of
/// a failed attempt carries the same `client_event_id` and the same frozen body, because the server rejects a reused
/// id with a different body (409 `idempotency_key_reused`).
struct ClipOrderOutbox: Equatable {
    struct Attempt: Equatable, Sendable {
        let clientEventID: String
        let ops: ClipOrderOps
        /// The encoded request, kept after the first send so a retry resends the same body.
        var frozenBody: Data?
    }

    private(set) var inFlight: Attempt?
    private(set) var failed: Attempt?
    private var queued: ClipOrderOps?

    var isIdle: Bool { inFlight == nil && failed == nil && queued == nil }
    var hasQueued: Bool { queued != nil }

    /// The attempt to send now, or nil when one is already running (this order waits, or is the same one).
    mutating func confirm(_ ops: ClipOrderOps, makeID: () -> String = { UUID().uuidString }) -> Attempt? {
        if let failed, failed.ops == ops {
            self.failed = nil
            inFlight = failed
            return failed
        }
        failed = nil
        if let inFlight {
            if inFlight.ops != ops { queued = ops } else { queued = nil }
            return nil
        }
        let attempt = Attempt(clientEventID: makeID(), ops: ops)
        inFlight = attempt
        return attempt
    }

    /// Records the body that went out, so a retry can resend it unchanged.
    mutating func freeze(_ body: Data) {
        guard inFlight?.frozenBody == nil else { return }
        inFlight?.frozenBody = body
    }

    /// The server took it. Returns the waiting order to send next, if any.
    mutating func succeeded(makeID: () -> String = { UUID().uuidString }) -> Attempt? { advance(makeID: makeID) }

    /// The send did not get through. A waiting newer order goes next (it supersedes the lost one); with nothing
    /// waiting, the attempt is kept so the next confirm of the same order retries it as it was.
    mutating func failedToSend(makeID: () -> String = { UUID().uuidString }) -> Attempt? {
        guard let lost = inFlight else { return nil }
        if queued != nil { return advance(makeID: makeID) }
        failed = lost
        inFlight = nil
        return nil
    }

    mutating func discardFailed() { failed = nil }

    private mutating func advance(makeID: () -> String) -> Attempt? {
        inFlight = nil
        guard let queued else { return nil }
        self.queued = nil
        let attempt = Attempt(clientEventID: makeID(), ops: queued)
        inFlight = attempt
        return attempt
    }
}

/// A sheet the creator opened: the order to start from, and the card it is about.
struct ClipOrderSheetSeed: Identifiable, Equatable {
    let id = UUID()
    let card: ClipOrderCard
    let order: [String]
}

/// A clip the creator tapped in the strip, to see it large.
struct ClipOrderPreviewSeed: Identifiable {
    let id = UUID()
    let item: ClipOrderCard.Item
    let media: CreationAttachedMedia
    let number: Int
    let total: Int
    let was: Int?
}

/// Owns the optimistic order and the one-turn-at-a-time send for the strip and its sheet. Lives on the workspace
/// (not in a lazily-built row), so a poll, a redraw or a recycled row never closes the sheet or loses the order.
@MainActor final class ClipOrderCoordinator: ObservableObject {
    enum Status: Equatable { case idle, sending, failed }

    /// How the coordinator reaches the thread: the revision to send, the call, and what to do once it is taken.
    struct Transport {
        var revision: () -> Int
        var submit: (SubmitTurnRequest) async throws -> TurnAccepted
        var accepted: (TurnAccepted) async -> Void
    }

    @Published private(set) var status: Status = .idle
    @Published var sheet: ClipOrderSheetSeed?
    @Published var preview: ClipOrderPreviewSeed?
    /// The order the creator confirmed, shown until the server's card for it arrives.
    @Published private(set) var pendingOrder: [String]?
    @Published private(set) var pendingMoved: Set<String> = []
    private var pendingBaseCardID: String?
    private var outbox = ClipOrderOutbox()
    private var task: Task<Void, Never>?

    /// The strip's phase: a send in flight or failed outranks the card's own `confirmed`.
    /// A rebased card (the server put the move on top of a newer order) is a notice, never a receipt.
    nonisolated static func phase(status: Status, confirmed: Bool, rebased: Bool = false, order: [String], failure: String) -> AskPhase<[String]> {
        switch status {
        case .sending: .submitting(order)
        case .failed: .failed(failure)
        case .idle: rebased ? .stale : confirmed ? .answered(order) : .open
        }
    }

    /// The order and moved marks the strip draws for `card`: the creator's own order while their confirm is still
    /// outstanding (or the server has not answered it yet), otherwise exactly the server's card.
    func display(for card: ClipOrderCard) -> (order: [String], moved: Set<String>) {
        guard let pendingOrder, showsPending(for: card) else {
            return (card.orderedMediaIDs, Set(card.items.filter(\.moved).map(\.mediaID)))
        }
        let known = Set(card.orderedMediaIDs)
        return (pendingOrder.filter(known.contains) + card.orderedMediaIDs.filter { !pendingOrder.contains($0) }, pendingMoved)
    }

    /// True while the strip draws the creator's own order instead of `card`'s: their confirm is outstanding, or the
    /// server has not answered it yet. The moved clips are then marked against `card`'s own order.
    func showsPending(for card: ClipOrderCard) -> Bool {
        pendingOrder != nil && (status != .idle || pendingBaseCardID == card.eventID)
    }

    /// The creator confirmed `order` for `card`: send the whole order once. A second confirm while one runs
    /// replaces any order waiting behind it, so only the last full order is ever sent.
    func confirm(order: [String], moved: Set<String>, card: ClipOrderCard, transport: Transport) {
        pendingOrder = order
        pendingMoved = moved
        pendingBaseCardID = card.eventID
        let ops = ClipOrderOps(proposalID: card.proposalID, orderedMediaIDs: order, confirm: true)
        guard let attempt = outbox.confirm(ops) else { return }
        run(attempt, transport: transport)
    }

    /// A newer card arrived. A confirm that failed is moot once the server's order has moved on.
    func serverCardChanged(to cardID: String) {
        guard status == .failed, cardID != pendingBaseCardID else { return }
        outbox.discardFailed()
        pendingOrder = nil
        pendingMoved = []
        status = .idle
    }

    private func run(_ first: ClipOrderOutbox.Attempt, transport: Transport) {
        status = .sending
        task = Task { [weak self] in
            var current: ClipOrderOutbox.Attempt? = first
            while let attempt = current, let self {
                do {
                    let accepted = try await transport.submit(self.request(for: attempt, transport: transport))
                    await transport.accepted(accepted)
                    current = self.outbox.succeeded()
                } catch {
                    current = self.outbox.failedToSend()
                }
            }
            guard let self else { return }
            self.status = self.outbox.failed == nil ? .idle : .failed
        }
    }

    private func request(for attempt: ClipOrderOutbox.Attempt, transport: Transport) throws -> SubmitTurnRequest {
        var request = SubmitTurnRequest(
            message: ClipOrderOps.placeholderMessage, clientEventID: attempt.clientEventID,
            expectedThreadRevision: transport.revision(), clipOrderOps: attempt.ops)
        if let frozen = attempt.frozenBody {
            request.frozenBody = frozen
        } else {
            outbox.freeze(try JSONEncoder().encode(request))
        }
        return request
    }
}
