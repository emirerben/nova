import Foundation

// KRI-374: takes the server could not place against a creator's song with confidence are shown in chat as
// video widgets in a proposed order. The creator previews and reorders them, then sends the confirmed
// order back on the next turn (`song_order`).
//
// Hand-rolled tolerant parsing on purpose, like `ClipQuestion`: an absent, newer, or malformed payload
// means "no order card" (the text question still shows), never a decode failure that breaks the turn.
// Everything wire-shaped is in this file; the models are pure values so reorder logic is unit-testable.

/// The optional `song_order_question` on an assistant event payload (or a turn response).
struct SongOrderQuestion: Equatable, Sendable {
    enum Status: String, Equatable, Sendable {
        case confident, ambiguous, unmatched
        /// Anything but `confident` is a take the creator should check.
        var isUncertain: Bool { self != .confident }
    }

    struct Alternate: Equatable, Sendable {
        let deltaS: Double
        let score: Double
    }

    struct Item: Equatable, Sendable, Identifiable {
        let mediaID: String
        let status: Status
        /// Where in the song the server thinks this take starts, in seconds; nil when it could not place it.
        let songStartS: Double?
        let alternates: [Alternate]
        var id: String { mediaID }
    }

    let questionID: String
    let proposedOrder: [String]
    let items: [Item]

    /// nil when the payload has no usable question (absent, no id, no items).
    static func parse(payload: [String: JSONValue]?) -> SongOrderQuestion? {
        guard let fields = payload?["song_order_question"]?.objectValue else { return nil }
        return parse(fields: fields)
    }

    init?(json: JSONValue) {
        guard let fields = json.objectValue, let parsed = Self.parse(fields: fields) else { return nil }
        self = parsed
    }

    init(questionID: String, proposedOrder: [String], items: [Item]) {
        self.questionID = questionID; self.proposedOrder = proposedOrder; self.items = items
    }

    private static func parse(fields: [String: JSONValue]) -> SongOrderQuestion? {
        guard let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        var seen = Set<String>()
        let items: [Item] = (fields["items"]?.arrayValue ?? []).compactMap { entry in
            guard let object = entry.objectValue,
                  let mediaID = object["media_id"]?.stringValue, !mediaID.isEmpty, seen.insert(mediaID).inserted else { return nil }
            // A status this build does not know is treated as uncertain: showing a "?" is the safe direction.
            let status = object["status"]?.stringValue.flatMap(Status.init(rawValue:)) ?? .unmatched
            let start = object["song_start_s"]?.numberValue.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
            let alternates: [Alternate] = (object["alternates"]?.arrayValue ?? []).compactMap { alternate in
                guard let delta = alternate.objectValue?["delta_s"]?.numberValue, delta.isFinite else { return nil }
                return Alternate(deltaS: delta, score: alternate.objectValue?["score"]?.numberValue ?? 0)
            }
            return Item(mediaID: mediaID, status: status, songStartS: start, alternates: alternates)
        }
        guard !items.isEmpty else { return nil }
        let proposed = (fields["proposed_order"]?.arrayValue ?? []).compactMap(\.stringValue)
        return SongOrderQuestion(questionID: questionID, proposedOrder: proposed, items: items)
    }

    /// Takes the creator should look at.
    var uncertainCount: Int { items.filter { $0.status.isUncertain }.count }
    /// Takes whose place in the song is doubtful but have a best guess: the ones the creator can fix by reordering.
    var ambiguousCount: Int { items.filter { $0.status == .ambiguous }.count }
    /// Takes the server could not place at all. It can only use them as filler, so they are not reorderable.
    var unmatchedCount: Int { items.filter { $0.status == .unmatched }.count }

    /// The proposed order cleaned for display: only known items, each once; items the server listed in `items`
    /// but left out of `proposed_order` follow, in item order. Never drops a take.
    var normalizedOrder: [String] {
        let known = Set(items.map(\.mediaID))
        var seen = Set<String>()
        var result = proposedOrder.filter { known.contains($0) && seen.insert($0).inserted }
        for item in items where seen.insert(item.mediaID).inserted { result.append(item.mediaID) }
        return result
    }

    func item(for mediaID: String) -> Item? { items.first { $0.mediaID == mediaID } }
}

/// `song_order` on the submit-turn body, exactly as the server contract names it.
struct SongOrderSubmission: Encodable, Equatable, Sendable {
    let questionID: String
    let orderedMediaIDs: [String]
    enum CodingKeys: String, CodingKey { case questionID = "question_id", orderedMediaIDs = "ordered_media_ids" }
    /// The 409 `code` the server answers a `song_order` for a question it has since replaced.
    static let staleConflictCode = "song_order_stale"

    /// The `song_order` a user message carried (the server echoes it on the stored event), or nil when the
    /// message has none or it is malformed. This is how a relaunch knows which question was really answered.
    static func parse(payload: [String: JSONValue]?) -> SongOrderSubmission? {
        guard let fields = payload?["song_order"]?.objectValue,
              let questionID = fields["question_id"]?.stringValue, !questionID.isEmpty else { return nil }
        let ids = (fields["ordered_media_ids"]?.arrayValue ?? []).compactMap(\.stringValue)
        return ids.isEmpty ? nil : SongOrderSubmission(questionID: questionID, orderedMediaIDs: ids)
    }

    /// Short read-only summary once answered, e.g. "Clip 2 · Clip 1 · Clip 3".
    func summary(positions: [String: Int]) -> String {
        orderedMediaIDs.compactMap { positions[$0] }.map { "Clip \($0)" }.joined(separator: " · ")
    }

    /// The readable chat message sent alongside the structured payload.
    func message(positions: [String: Int]) -> String {
        let numbers = orderedMediaIDs.compactMap { positions[$0] }.map(String.init).joined(separator: ", ")
        return numbers.isEmpty ? "Use this order" : "Use this order: clips \(numbers)"
    }
}

/// Mutable order for one question. Pure value type so the interactions are unit-testable.
struct SongOrderState: Equatable {
    let question: SongOrderQuestion
    private let proposed: [String]
    private(set) var order: [String]

    init(question: SongOrderQuestion) {
        self.question = question
        proposed = question.normalizedOrder
        order = proposed
    }

    /// True once the creator has moved anything away from the proposal.
    var isChanged: Bool { order != proposed }

    func position(of mediaID: String) -> Int? { order.firstIndex(of: mediaID).map { $0 + 1 } }
    func canMoveUp(_ mediaID: String) -> Bool { (order.firstIndex(of: mediaID) ?? 0) > 0 }
    func canMoveDown(_ mediaID: String) -> Bool { order.firstIndex(of: mediaID).map { $0 < order.count - 1 } ?? false }

    /// `List.onMove` semantics: `destination` is an index into the pre-move array.
    mutating func move(from source: IndexSet, to destination: Int) {
        guard !source.isEmpty, source.allSatisfy({ order.indices.contains($0) }) else { return }
        let moving = source.sorted().map { order[$0] }
        let insertion = destination - source.filter { $0 < destination }.count
        for index in source.sorted(by: >) { order.remove(at: index) }
        order.insert(contentsOf: moving, at: max(0, min(order.count, insertion)))
    }

    mutating func moveUp(_ mediaID: String) {
        guard let index = order.firstIndex(of: mediaID), index > 0 else { return }
        order.swapAt(index, index - 1)
    }

    mutating func moveDown(_ mediaID: String) {
        guard let index = order.firstIndex(of: mediaID), index < order.count - 1 else { return }
        order.swapAt(index, index + 1)
    }

    /// Resets to the order the server proposed.
    mutating func reset() { order = proposed }

    var submission: SongOrderSubmission { SongOrderSubmission(questionID: question.questionID, orderedMediaIDs: order) }
}

/// Clip numbers shown on the order card: position among the thread's non-audio media, in attach order.
/// Matches the numbers the clip picker uses, so "Clip 3" means the same take everywhere in the chat.
enum SongOrderPositions {
    static func map(media: [CreationAttachedMedia], question: SongOrderQuestion) -> [String: Int] {
        var result: [String: Int] = [:]
        for (index, clip) in media.filter({ $0.kind != "audio" }).enumerated() { result[clip.id] = index + 1 }
        // A take the thread state doesn't list (stale state) still gets a stable number.
        var fallback = result.count
        for id in question.normalizedOrder where result[id] == nil {
            fallback += 1
            result[id] = fallback
        }
        return result
    }
}

/// How a question in the transcript is presented.
enum SongOrderPhase: Equatable {
    /// Still open: the newest question that no `song_order` has answered. Interactive.
    case active
    /// A later user message carried a `song_order` for this question: read-only. The submission is what
    /// that message sent, so the card can say what was confirmed even after a relaunch.
    case answered(SongOrderSubmission)
    /// A newer question replaced it and nobody answered this one: hidden.
    case superseded
}

/// Folds the transcript into per-question phases. The server keeps a question open until a `song_order` for
/// that question arrives (`latest_open_song_order_question`), so a plain reply, a reaction, or a message
/// about something else never closes the card: only a user message carrying a matching `song_order` does.
enum SongOrderFold {
    struct Entry: Equatable {
        let messageID: String
        let question: SongOrderQuestion?
        let isUser: Bool
        /// The `song_order` this message carried (user messages only).
        var answer: SongOrderSubmission? = nil
    }

    static func phases(_ entries: [Entry]) -> [String: SongOrderPhase] {
        var result: [String: SongOrderPhase] = [:]
        for (index, entry) in entries.enumerated() {
            guard let question = entry.question else { continue }
            let later = entries[(index + 1)...]
            if let answer = later.lazy.compactMap({ $0.isUser ? $0.answer : nil }).first(where: { $0.questionID == question.questionID }) {
                result[entry.messageID] = .answered(answer)
            } else if later.contains(where: { $0.question != nil }) {
                result[entry.messageID] = .superseded
            } else {
                result[entry.messageID] = .active
            }
        }
        return result
    }
}
