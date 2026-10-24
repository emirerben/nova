import Foundation

/// KRI-756: footage-aware suggestion chips served by `GET /creation-threads/{id}/suggestions`.
///
/// The server decides everything (state, order, wording, what is enabled). The client only decodes,
/// renders in rank order, and sends `message` as an ordinary chat message on tap. Decoding is tolerant
/// by contract: unknown keys and unknown `kind` values are ignored, and a chip missing its label or
/// message is dropped rather than failing the whole set.
struct SuggestionChip: Decodable, Equatable, Identifiable, Sendable {
    /// The server's hard limits (`SuggestionChipOut`): a longer value is not a chip we trust.
    static let maxLabelLength = 36
    static let maxMessageLength = 120
    static let maxWhyLength = 48

    let id: String
    /// Open string on purpose: a kind added by a newer server must not break an older app.
    let kind: String
    /// Display only. Never sent.
    let label: String
    /// The exact text sent on tap.
    let message: String
    /// 1-based, unique within a set. A missing rank sorts after the chips that have one.
    let rank: Int
    let isDefaultEdit: Bool
    /// Optional one-line subtitle with footage evidence. Display only; nil when absent or unusable.
    let why: String?
    /// `agent` or `template`. Open string; the app does not branch on it.
    let labelSource: String?
    /// The chip needs this client protocol or newer; nil = no requirement.
    let requiresProtocol: Int?

    init(id: String, kind: String, label: String, message: String, rank: Int = Int.max, isDefaultEdit: Bool = false,
         why: String? = nil, labelSource: String? = nil, requiresProtocol: Int? = nil) {
        self.id = id; self.kind = kind; self.label = label; self.message = message
        self.rank = rank; self.isDefaultEdit = isDefaultEdit
        self.why = why; self.labelSource = labelSource; self.requiresProtocol = requiresProtocol
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, label, message, rank, why
        case isDefaultEdit = "default_edit"
        case labelSource = "label_source"
        case requiresProtocol = "requires_protocol"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let label = try container.decode(String.self, forKey: .label).trimmingCharacters(in: .whitespacesAndNewlines)
        let message = try container.decode(String.self, forKey: .message).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !label.isEmpty, label.count <= Self.maxLabelLength,
              !message.isEmpty, message.count <= Self.maxMessageLength else {
            throw DecodingError.dataCorruptedError(forKey: .message, in: container, debugDescription: "Unusable suggestion chip")
        }
        self.label = label
        self.message = message
        self.id = try container.decodeIfPresent(String.self, forKey: .id) ?? message
        self.kind = try container.decodeIfPresent(String.self, forKey: .kind) ?? ""
        self.rank = try container.decodeIfPresent(Int.self, forKey: .rank) ?? Int.max
        self.isDefaultEdit = try container.decodeIfPresent(Bool.self, forKey: .isDefaultEdit) ?? false
        // `why` is a nicety: a bad one (wrong type, empty, too long) is dropped, never the chip.
        let why = (try? container.decodeIfPresent(String.self, forKey: .why))?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.why = (why?.isEmpty == false && why!.count <= Self.maxWhyLength) ? why : nil
        self.labelSource = try? container.decodeIfPresent(String.self, forKey: .labelSource)
        self.requiresProtocol = try? container.decodeIfPresent(Int.self, forKey: .requiresProtocol)
    }

    /// Spoken as "label, why" so VoiceOver reads the evidence with the chip.
    var accessibilityText: String { why.map { "\(label), \($0)" } ?? label }
}

/// The optional `suggestion` field on a turn POST: which offered chip the message came from. The server
/// validates it against the sets it offered and falls back to matching the text, so a wrong or stale
/// value costs nothing but the attribution.
struct SuggestionTap: Codable, Equatable, Sendable {
    let setID: String
    let chipID: String
    enum CodingKeys: String, CodingKey { case setID = "set_id", chipID = "chip_id" }
}

struct SuggestionSet: Decodable, Equatable, Sendable {
    /// The most chips the server ever returns. A longer list is truncated, never re-sorted.
    static let maxChips = 4
    /// A wait that cannot be right is clamped (the contract says 3 s, 2 s while wording).
    static let retryRange: ClosedRange<Double> = 1...10

    var setID: String?
    /// `first_prompt`, `ready` or `none`. Anything else renders nothing.
    var state: String
    /// `ok`, `pending` or `unavailable`. Anything else renders nothing.
    var status: String
    /// `pending`, `done` or `empty`. Informational.
    var understanding: String
    var retryAfterSeconds: Double?
    /// `agent`, `fallback` or `pending`. Informational.
    var wording: String?
    /// Sorted by rank (stable), de-duplicated by id and by message, capped at four.
    var chips: [SuggestionChip]

    static let unavailable = SuggestionSet(setID: nil, state: "none", status: "unavailable", understanding: "done", retryAfterSeconds: nil, chips: [])

    init(setID: String?, state: String, status: String, understanding: String = "done", retryAfterSeconds: Double? = nil,
         wording: String? = nil, chips: [SuggestionChip]) {
        self.setID = setID; self.state = state; self.status = status
        self.understanding = understanding; self.retryAfterSeconds = retryAfterSeconds
        self.wording = wording
        self.chips = Self.normalized(chips)
    }

    enum CodingKeys: String, CodingKey {
        case state, status, understanding, wording, chips
        case setID = "set_id", retryAfterSeconds = "retry_after_s"
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        setID = try container.decodeIfPresent(String.self, forKey: .setID)
        state = try container.decodeIfPresent(String.self, forKey: .state) ?? "none"
        status = try container.decodeIfPresent(String.self, forKey: .status) ?? "unavailable"
        understanding = try container.decodeIfPresent(String.self, forKey: .understanding) ?? "done"
        retryAfterSeconds = try? container.decodeIfPresent(Double.self, forKey: .retryAfterSeconds)
        wording = try? container.decodeIfPresent(String.self, forKey: .wording)
        // One malformed chip must not take the rest down with it.
        let lossy = try container.decodeIfPresent([Lossy<SuggestionChip>].self, forKey: .chips) ?? []
        chips = Self.normalized(lossy.compactMap(\.value))
    }

    private static func normalized(_ chips: [SuggestionChip]) -> [SuggestionChip] {
        var ids = Set<String>(), messages = Set<String>()
        let unique = chips.filter { ids.insert($0.id).inserted && messages.insert($0.message).inserted }
        // Rank order, ties and missing ranks keep the order the server sent.
        let ordered = unique.enumerated().sorted { ($0.element.rank, $0.offset) < ($1.element.rank, $1.offset) }.map(\.element)
        return Array(ordered.prefix(maxChips))
    }

    /// True while the server is still working on this set (clip understanding or wording).
    var isPending: Bool { status == "pending" }

    /// The chips to show for one surface. The server says which surface a set belongs to (`state`), so a
    /// first-prompt set can never leak into the ready card or the other way round. `pending` is shown too:
    /// at the first prompt it carries chip #1 alone while the rest is being worked out. Chips whose
    /// `requires_protocol` is above this app are dropped here, so the app never offers what it cannot do.
    func chips(forState wanted: String, appProtocol: Int = KriaClientProtocolContract.version) -> [SuggestionChip] {
        guard state == wanted, status == "ok" || status == "pending" else { return [] }
        return chips.filter { ($0.requiresProtocol ?? 0) <= appProtocol }
    }

    /// How long to wait before asking again, or nil when the server is not waiting on anything.
    var retryDelay: Duration? {
        guard isPending else { return nil }
        let seconds = min(max(retryAfterSeconds ?? 3, Self.retryRange.lowerBound), Self.retryRange.upperBound)
        return .milliseconds(Int(seconds * 1000))
    }

    private struct Lossy<Value: Decodable>: Decodable {
        let value: Value?
        init(from decoder: Decoder) throws { value = try? Value(from: decoder) }
    }
}

/// Which surface a chip set feeds, and when the app asks for it.
enum SuggestionSurface: String, Sendable {
    /// Footage is attached and nothing has been said yet.
    case firstPrompt = "first_prompt"
    /// A finished render sits in the thread.
    case ready

    /// Total time the row keeps re-asking while the server reports `pending` (contract: 60 s ceiling).
    static let pollCeiling: Duration = .seconds(60)
}

/// The decisions the chat workspace makes about suggestion chips, kept apart from the view so they
/// can be pinned by unit tests.
enum SuggestionChipsPolicy {
    /// Chips exist only when the server advertises them for this account. No flag name is read here.
    static func isEnabled(_ capabilities: CreationCapabilities?) -> Bool {
        capabilities?.suggestionChipsEnabled == true
    }

    /// The surface to ask about right now, or nil when the app should show nothing and ask nothing.
    /// `hasOpenQuestion` is true when the newest message is an assistant question the creator has not
    /// answered (choice card, clip/song question, reply options, exact-string options).
    static func surface(
        enabled: Bool, stage: WorkspaceStage, isSlides: Bool, isFormatKnown: Bool,
        readyMediaCount: Int, pendingUploadCount: Int, hasUserMessage: Bool, hasPendingMessage: Bool,
        hasOpenQuestion: Bool = false
    ) -> SuggestionSurface? {
        guard enabled, !isSlides, !hasOpenQuestion else { return nil }
        switch stage {
        case .footage:
            guard readyMediaCount > 0, pendingUploadCount == 0, !hasUserMessage, !hasPendingMessage else { return nil }
            return .firstPrompt
        case .ready:
            return isFormatKnown && !hasPendingMessage ? .ready : nil
        case .format, .direction, .rendering, .failed:
            return nil
        }
    }

    /// What to hold after an answer. A delivered set (`ok`, with chips) is frozen: the same `set_id` in the
    /// same state never replaces it, so chips are never re-ranked or relabelled under the creator's thumb.
    /// A pending set is replaced by whatever comes next, and a failed ask (nil) keeps what is held.
    static func merge(current: SuggestionSet?, incoming: SuggestionSet?) -> SuggestionSet? {
        guard let incoming else { return current }
        if let current, current.status == "ok", !current.chips.isEmpty,
           current.state == incoming.state, let id = current.setID, id == incoming.setID {
            return current
        }
        return incoming
    }

    /// Asks the server for a set, re-asking while it reports `pending`. `onSet` runs for every answer. Stops on
    /// cancellation (media attached or detached, stage changed), on any answer that is not pending, or once
    /// `ceiling` has passed.
    static func poll(
        ceiling: Duration = SuggestionSurface.pollCeiling,
        fetch: @Sendable () async throws -> SuggestionSet?,
        onSet: @MainActor (SuggestionSet?) -> Void,
        sleep: @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) async {
        var waited = Duration.zero
        while !Task.isCancelled {
            let set = try? await fetch()
            guard !Task.isCancelled else { return }
            await onSet(set)
            guard let delay = set?.retryDelay, waited + delay <= ceiling else { return }
            do { try await sleep(delay) } catch { return }
            waited += delay
        }
    }
}

extension ChatTranscriptMessage {
    /// True when this assistant message asks the creator something they have not answered by itself:
    /// a choice card, a clip / song-order / song-range question, reply chips, or exact-string options.
    var asksAQuestion: Bool {
        role == .assistant && (clipQuestion != nil || songOrderQuestion != nil || songRangeQuestion != nil
                               || protectedRegionQuestion != nil
                               || choiceQuestion != nil || !replyOptions.isEmpty || !options.isEmpty)
    }
}

extension KriaAPI {
    /// `GET /creation-threads/{id}/suggestions`. Nil when the server does not have the endpoint (404) or
    /// answers something unreadable: both mean "show no suggestion chips".
    func creationSuggestions(threadID: UUID) async throws -> SuggestionSet? {
        do {
            return try await request(path: "creation-threads/\(threadID.uuidString)/suggestions", method: "GET", bodyData: nil, decode: SuggestionSet.self)
        } catch APIError.requestFailed(let status, _) where status == 404 {
            return nil
        } catch is DecodingError {
            return nil
        }
    }
}
