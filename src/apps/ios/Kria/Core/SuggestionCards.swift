import Foundation

/// AI suggestion blocks: the v2 `suggestions` array of `SuggestionSetOut`, drawn as cards in the chat's ready
/// stage and in the editor's Improve sheet. Both surfaces read the one set the chat holds.
///
/// The server decides what a card says and what its tap does. The app decodes, renders in rank order, and sends
/// `prompt` as the creator's chat message together with `{set_id, id}`. Decoding is tolerant by contract: an
/// unknown `kind` or `action_type` still renders (by its own title), unknown keys are ignored, and a card that
/// cannot be trusted is dropped on its own, never the set.
struct SuggestionCard: Decodable, Equatable, Identifiable, Sendable {
    /// The server's hard limits (`Suggestion` / `MAX_*` in `services/suggestions/models.py`).
    static let maxIDLength = 64
    static let maxTitleLength = 40
    static let maxWhyLength = 96
    static let maxPromptLength = 120
    static let maxCopyLength = 200
    static let maxEvidenceLength = 48

    let id: String
    /// Open string on purpose: a kind added by a newer server must not break an older app.
    let kind: String
    let title: String
    /// One line of why this card is offered; nil when absent or unusable. Display only.
    let why: String?
    /// Short measured facts behind the card ("Wind in clip 3"). Display only.
    let evidence: [String]
    /// The exact chat message sent on tap.
    let prompt: String
    /// Hook and title cards: the on-screen text the tap approves, shown in full.
    let copyText: String?
    let rank: Int
    /// Open string (`editor_ops`, `brief_update`, `item_setting`, `copy_decision`, `async_generate`, or a newer one).
    let actionType: String
    /// Generated media only: what the creator pays for, shown before they tap.
    let costUSD: Double?
    let mediaID: String?
    let clipOrdinal: Int?
    /// The card needs this client protocol or newer; nil = no requirement.
    let requiresProtocol: Int?

    /// Kinds whose tap approves text the creator has to be able to read first.
    static let copyKinds: Set<String> = ["hook", "title"]

    init(id: String, kind: String, title: String, why: String? = nil, evidence: [String] = [], prompt: String,
         copyText: String? = nil, rank: Int = Int.max, actionType: String = "", costUSD: Double? = nil,
         mediaID: String? = nil, clipOrdinal: Int? = nil, requiresProtocol: Int? = nil) {
        self.id = id; self.kind = kind; self.title = title; self.why = why; self.evidence = evidence
        self.prompt = prompt; self.copyText = copyText; self.rank = rank; self.actionType = actionType
        self.costUSD = costUSD; self.mediaID = mediaID; self.clipOrdinal = clipOrdinal
        self.requiresProtocol = requiresProtocol
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, title, why, evidence, prompt, rank, anchor
        case copyText = "copy_text", actionType = "action_type", costUSD = "cost_usd"
        case requiresProtocol = "requires_protocol"
    }

    private struct Anchor: Decodable {
        let mediaID: String?
        let clipOrdinal: Int?
        enum CodingKeys: String, CodingKey { case mediaID = "media_id", clipOrdinal = "clip_ordinal" }
        init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            mediaID = try? c.decodeIfPresent(String.self, forKey: .mediaID)
            clipOrdinal = try? c.decodeIfPresent(Int.self, forKey: .clipOrdinal)
        }
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func text(_ key: CodingKeys) -> String? {
            (try? c.decodeIfPresent(String.self, forKey: key))?.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard let id = text(.id), !id.isEmpty, id.count <= Self.maxIDLength,
              let title = text(.title), !title.isEmpty, title.count <= Self.maxTitleLength,
              let prompt = text(.prompt), !prompt.isEmpty, prompt.count <= Self.maxPromptLength else {
            throw DecodingError.dataCorruptedError(forKey: .id, in: c, debugDescription: "Unusable suggestion card")
        }
        let kind = text(.kind) ?? ""
        // Approval text is shown whole or the card is not shown at all: a tap must never approve unseen words.
        let rawCopy = text(.copyText)
        let copy = (rawCopy?.isEmpty == false && rawCopy!.count <= Self.maxCopyLength) ? rawCopy : nil
        if Self.copyKinds.contains(kind), copy == nil {
            throw DecodingError.dataCorruptedError(forKey: .copyText, in: c, debugDescription: "Copy card without its text")
        }
        self.id = id; self.kind = kind; self.title = title; self.prompt = prompt; self.copyText = copy
        // `why` and evidence are niceties: a bad one is dropped, never the card.
        let why = text(.why)
        self.why = (why?.isEmpty == false && why!.count <= Self.maxWhyLength) ? why : nil
        let evidence = (try? c.decodeIfPresent([String].self, forKey: .evidence)) ?? []
        self.evidence = evidence
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty && $0.count <= Self.maxEvidenceLength }
        self.rank = (try? c.decodeIfPresent(Int.self, forKey: .rank)) ?? Int.max
        self.actionType = text(.actionType) ?? ""
        let cost = try? c.decodeIfPresent(Double.self, forKey: .costUSD)
        self.costUSD = cost.flatMap { $0.isFinite && $0 >= 0 ? $0 : nil }
        let anchor = try? c.decodeIfPresent(Anchor.self, forKey: .anchor)
        self.mediaID = anchor?.mediaID
        self.clipOrdinal = anchor?.clipOrdinal
        self.requiresProtocol = try? c.decodeIfPresent(Int.self, forKey: .requiresProtocol)
    }

    /// Spoken as "title, why. Evidence." so VoiceOver reads what the card is based on.
    var accessibilityText: String {
        var parts = [title]
        if let why { parts.append(why) }
        if let evidence = evidence.first { parts.append(evidence) }
        if let copyText { parts.append("Text: \(copyText)") }
        if let cost = costLabel { parts.append("Costs \(cost)") }
        return parts.joined(separator: ". ")
    }

    /// "$0.04": generated media states its price before the tap. Nil when the card is free.
    var costLabel: String? {
        guard let costUSD, costUSD > 0 else { return nil }
        return String(format: "$%.2f", costUSD)
    }

    /// SF Symbol for the kind; an unknown kind gets the generic one rather than no card.
    var symbolName: String {
        switch kind {
        case "hook", "title": "text.quote"
        case "captions": "captions.bubble"
        case "music", "music_level": "music.note"
        case "sfx": "speaker.wave.2"
        case "transition": "rectangle.on.rectangle"
        case "title_animation", "pinned_text": "textformat"
        case "opener_clip": "film"
        case "mute_clip": "speaker.slash"
        case "filler_words": "waveform.badge.minus"
        case "duplicates": "square.on.square"
        case "generated_overlay": "photo.badge.plus"
        default: "sparkles"
        }
    }
}

/// What a card is doing on this phone. Never sent anywhere; the server's set is the source of truth and this only
/// keeps one tap from doing two things and a handled card from coming straight back.
enum SuggestionCardPhase: Equatable, Sendable {
    case idle
    /// The tap is on its way. Blocks a second tap on the same card.
    case sending
    /// The tap was accepted. The card leaves until the next set.
    case applied
    case dismissed
    /// The server said the card no longer fits the edit.
    case stale
}

/// What happened to a card's tap once it left the phone. A tap that never reached the server reports nothing.
enum SuggestionTapOutcome: Equatable, Sendable {
    case accepted
    /// The server refused the pin with `suggestion_stale`: the card no longer fits the edit.
    case stale
}

/// The "no longer fits" outcome of a pinned tap, shown in place of the card until the refreshed set arrives.
struct SuggestionStaleNotice: Equatable, Sendable {
    static let message = "That suggestion no longer fits your edit. Refreshing."
    let cardID: String
    let setID: String
}

/// The decisions the card surfaces make, kept apart from the views so unit tests can pin them.
@MainActor
final class SuggestionCardLedger: ObservableObject {
    @Published private(set) var phases: [String: SuggestionCardPhase] = [:]
    @Published private(set) var staleNotice: SuggestionStaleNotice?
    /// Bumped to make the chat ask the server again (after a stale tap and after an editor Save).
    @Published private(set) var refreshToken = 0
    /// The set the phases belong to. A new set clears everything but dismissals, which stay for the chat's life.
    private var setID: String?
    private var dismissed: Set<String> = []
    /// How long "no longer fits" stays readable. The refresh it triggers can land in a blink, and a notice nobody
    /// had time to read is the card vanishing for no reason.
    private let noticeDuration: Duration
    private var noticeTask: Task<Void, Never>?

    init(noticeDuration: Duration = .seconds(4)) { self.noticeDuration = noticeDuration }

    func phase(of id: String) -> SuggestionCardPhase { dismissed.contains(id) ? .dismissed : phases[id] ?? .idle }

    /// Cards still worth drawing, in the order given. A card in `.sending` stays so the creator sees it settle;
    /// a stale one stays only as the notice.
    func visible(_ cards: [SuggestionCard]) -> [SuggestionCard] {
        cards.filter { switch phase(of: $0.id) { case .idle, .sending: true; case .applied, .dismissed, .stale: false } }
    }

    /// A set arrived. Handled cards come back only for a different set: a tapped card is still the same card in
    /// the same set until the server says otherwise.
    func reconcile(with set: SuggestionSet?) {
        guard let set, let incoming = set.setID else { return }
        defer { setID = incoming }
        guard setID != incoming else { return }
        phases = phases.filter { $0.value == .sending }
    }

    /// Returns false when the tap must not go out: this card was handled, or any card is already sending (one tap
    /// at a time, so two fast taps on different cards never become two turns).
    func begin(_ card: SuggestionCard) -> Bool {
        guard phase(of: card.id) == .idle, !phases.values.contains(.sending) else { return false }
        phases[card.id] = .sending
        return true
    }

    /// The send finished. `accepted` false = nothing reached the server (offline, rejected): the card is tappable again.
    func finish(_ card: SuggestionCard, accepted: Bool) {
        guard phases[card.id] == .sending else { return }
        phases[card.id] = accepted ? .applied : .idle
    }

    func markStale(_ card: SuggestionCard, setID: String) {
        phases[card.id] = .stale
        let notice = SuggestionStaleNotice(cardID: card.id, setID: setID)
        staleNotice = notice
        noticeTask?.cancel()
        noticeTask = Task { [noticeDuration] in
            try? await Task.sleep(for: noticeDuration)
            guard !Task.isCancelled, staleNotice == notice else { return }
            staleNotice = nil
        }
        refreshToken += 1
    }

    func dismiss(_ card: SuggestionCard) {
        dismissed.insert(card.id)
        phases[card.id] = .dismissed
    }

    /// An editor Save changes the Job without a thread revision, so the chat has to be told to ask again.
    func requestRefresh() { refreshToken += 1 }
}

extension SuggestionSet {
    /// The v2 cards the server offers for one surface, in rank order. Same gating as chips: `state` must match,
    /// `status` must be `ok`, and a card above this app's protocol is dropped here so the app never offers
    /// what it cannot do.
    func cards(forState wanted: String, appProtocol: Int = KriaClientProtocolContract.version) -> [SuggestionCard] {
        guard state == wanted, status == "ok" else { return [] }
        return cards.filter { ($0.requiresProtocol ?? 0) <= appProtocol }
    }
}

extension SuggestionTap {
    /// A 409 with this code means the pinned card no longer fits the edit. The server sent no turn for it.
    static let staleConflictCode = "suggestion_stale"
}

/// What the ready stage draws for the server's set: cards when the set carries v2 suggestions, otherwise the
/// KRI-756 chips exactly as before.
enum SuggestionReadyContent: Equatable {
    case none
    case chips([SuggestionChip])
    /// `cards` may be empty after the creator handled every one; the row then draws nothing and does not fall back to chips.
    case cards([SuggestionCard])

    static func resolve(set: SuggestionSet?, appProtocol: Int = KriaClientProtocolContract.version) -> SuggestionReadyContent {
        guard let set else { return .none }
        let cards = set.cards(forState: SuggestionSurface.ready.rawValue, appProtocol: appProtocol)
        if !cards.isEmpty { return .cards(cards) }
        let chips = set.chips(forState: SuggestionSurface.ready.rawValue, appProtocol: appProtocol)
        return chips.isEmpty ? .none : .chips(chips)
    }
}

extension KriaAPI {
    /// `POST creation-threads/{id}/suggestions/dismiss`. The creator already lost the card on this phone; this
    /// tells the server so it never offers it again. A server without the route (404/405) or any other failure is
    /// not the creator's problem, so it never throws.
    func dismissSuggestion(threadID: UUID, setID: String, suggestionID: String) async {
        let body = (try? JSONEncoder().encode(SuggestionTap(setID: setID, chipID: suggestionID))) ?? Data()
        _ = try? await request(path: "creation-threads/\(threadID.uuidString)/suggestions/dismiss", method: "POST",
                               bodyData: body, decode: EmptyProjectResponse.self)
    }
}
