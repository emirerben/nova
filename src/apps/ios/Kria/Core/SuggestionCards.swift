import Foundation

/// AI suggestion blocks: the v2 `suggestions` array of `SuggestionSetOut`, drawn as AI chips in the chat's ready
/// stage, which the editor's chat sheet shows too. Both read the one set the chat holds.
///
/// The server decides what a card says and what its tap does. The app decodes, renders in rank order, and puts
/// `prompt` in the composer on tap. A draft that is exactly one card's prompt is sent as the creator's chat message
/// together with `{set_id, id}`; any other draft is a plain message. Decoding is tolerant by contract: an
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
        if let copyText { parts.append(String(localized: "Text: \(copyText)")) }
        return parts.joined(separator: ". ")
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
}

/// What happened to a card's tap once it left the phone. A tap that never reached the server reports nothing.
enum SuggestionTapOutcome: Equatable, Sendable {
    case accepted
}

/// The decisions the card surfaces make, kept apart from the views so unit tests can pin them.
@MainActor
final class SuggestionCardLedger: ObservableObject {
    @Published private(set) var phases: [String: SuggestionCardPhase] = [:]
    /// Bumped to make the chat ask the server again (after an editor Save).
    @Published private(set) var refreshToken = 0
    /// The set the phases belong to. A new set clears everything but taps still in flight.
    private var setID: String?
    func phase(of id: String) -> SuggestionCardPhase { phases[id] ?? .idle }

    /// Cards still worth drawing, in the order given. A card in `.sending` stays so the creator sees it settle.
    func visible(_ cards: [SuggestionCard]) -> [SuggestionCard] {
        cards.filter { switch phase(of: $0.id) { case .idle, .sending: true; case .applied: false } }
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

    /// A message that carried this card's prompt went out as plain text. The card is handled for this set; a tap
    /// still in flight settles through `finish`.
    func markApplied(_ card: SuggestionCard) {
        guard phases[card.id] != .sending else { return }
        phases[card.id] = .applied
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

/// The composer draft is the selection. A suggestion is selected when one line of the draft is its prompt, so a
/// creator who deletes the line by hand unselects the chip and nothing else has to agree with it. Lines compare the
/// way the server matches a tapped message (`_norm`: casefold, collapsed whitespace), so a pin still resolves.
enum SuggestionDraft {
    private static func normalized(_ text: String) -> String {
        text.split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased()
    }

    private static func lines(_ draft: String) -> [String] {
        draft.components(separatedBy: .newlines)
    }

    static func isSelected(_ card: SuggestionCard, in draft: String) -> Bool {
        let wanted = normalized(card.prompt)
        return lines(draft).contains { normalized($0) == wanted }
    }

    /// Selected: drops that card's line and keeps every other line. Not selected: appends the prompt on a new line.
    static func toggle(_ card: SuggestionCard, in draft: String) -> String {
        if isSelected(card, in: draft) {
            let wanted = normalized(card.prompt)
            let kept = lines(draft).filter { normalized($0) != wanted }
            return kept.allSatisfy { normalized($0).isEmpty } ? "" : kept.joined(separator: "\n")
        }
        if normalized(draft).isEmpty { return card.prompt }
        return draft.hasSuffix("\n") ? draft + card.prompt : draft + "\n" + card.prompt
    }

    /// The one card the whole draft says, or nil. Only then may Send carry the card's pin: a prompt with other words
    /// or other prompts is a plain message the planner reads.
    static func pinnedCard(for draft: String, among cards: [SuggestionCard]) -> SuggestionCard? {
        let whole = normalized(draft)
        guard !whole.isEmpty else { return nil }
        return cards.first { normalized($0.prompt) == whole }
    }

    /// Every card whose prompt is a line of the draft, in the order the cards are offered.
    static func cards(in draft: String, among cards: [SuggestionCard]) -> [SuggestionCard] {
        cards.filter { isSelected($0, in: draft) }
    }
}
