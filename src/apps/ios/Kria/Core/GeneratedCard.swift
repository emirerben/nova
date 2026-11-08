import CryptoKit
import Foundation

/// The suggestion card that spends (`action_type == "async_generate"`): a generated image the server makes once the
/// creator agrees to its price. The phone asks first (`SpendConsentSheet`), the tap goes out as an ordinary pinned card
/// tap, and the image is then found by the id both sides derive from the set and the card, so there is nothing to be
/// told and nothing to pay for twice.
extension SuggestionCard {
    static let spendActionType = "async_generate"

    /// A tap on this card costs money, so it never goes out without the creator's yes.
    var needsSpendConsent: Bool { actionType == Self.spendActionType }
}

/// The client event id of the one image a card can make. UUIDv5 over `"<set_id>:<card_id>"` in the server's namespace
/// (`generated_overlay.event_id` in the API); `GeneratedCardTests` pins a vector both sides must produce.
enum GeneratedCardEvent {
    static let namespace = UUID(uuidString: "5B0C1F7E-2D3A-5C4B-9A61-7E2F0D8B4C19")!

    static func id(setID: String, cardID: String) -> UUID {
        var bytes = withUnsafeBytes(of: namespace.uuid) { Array($0) }
        bytes.append(contentsOf: Array("\(setID):\(cardID)".utf8))
        var digest = Array(Insecure.SHA1.hash(data: bytes).prefix(16))
        digest[6] = (digest[6] & 0x0F) | 0x50
        digest[8] = (digest[8] & 0x3F) | 0x80
        let tuple = (digest[0], digest[1], digest[2], digest[3], digest[4], digest[5], digest[6], digest[7],
                     digest[8], digest[9], digest[10], digest[11], digest[12], digest[13], digest[14], digest[15])
        return UUID(uuid: tuple)
    }

    /// What goes on the wire: the server parses either case, but its own `str(uuid)` is lowercase.
    static func wireID(setID: String, cardID: String) -> String { id(setID: setID, cardID: cardID).uuidString.lowercased() }
}

/// What the creator sees of the image a card is making: one row, independent of the set it came from (the server
/// replaces the set the moment the card is spent).
struct GeneratedCardResult: Equatable, Identifiable, Sendable {
    enum Phase: Equatable, Sendable {
        case making
        case ready
        /// The server cannot say whether the image finished. The review screen has the "Check again" for it.
        case unsure
        case failed
    }

    let id: String
    let title: String
    let eventID: String
    var operationID: String?
    var phase: Phase

    /// A row still waiting for an operation to appear has nothing to protect: a tap the server turned away never makes
    /// one, and the chat reply is the only notice. Once an operation exists the image is paid for, so it stays.
    var canDismiss: Bool { phase != .making || operationID == nil }
}

/// Follows the one image a tapped card is making: finds the operation by its derived event id, polls until it settles,
/// and hands it to the overlay review (`GeneratedOverlaySet`) on request. It never starts a generation and never
/// retries one: the server did the only request, and a missing operation just means the tap was declined.
@MainActor
final class GeneratedCardTracker: ObservableObject {
    @Published private(set) var result: GeneratedCardResult?
    private var task: Task<Void, Never>?
    private let interval: Duration
    private let rounds: Int
    /// How long a missing operation is waited for. The server creates it before it calls the provider, so no operation
    /// after this long means the tap ended without one (declined, no longer fits); the chat reply says why.
    private let unseenRounds: Int

    init(interval: Duration = .seconds(2), rounds: Int = 150, unseenRounds: Int = 60) {
        self.interval = interval
        self.rounds = rounds
        self.unseenRounds = unseenRounds
    }

    func begin(card: SuggestionCard, setID: String, itemID: String, accountID: String?, title: String, api: any KriaAPIClient) {
        task?.cancel()
        let event = GeneratedCardEvent.wireID(setID: setID, cardID: card.id)
        result = GeneratedCardResult(id: card.id, title: title, eventID: event, operationID: nil, phase: .making)
        remember(accountID: accountID, itemID: itemID)
        task = Task { [weak self] in await self?.follow(itemID: itemID, accountID: accountID, event: event, api: api) }
    }

    /// The app was closed (or the chat left) while an image was being made: the card is already spent, so pick the
    /// image back up instead of losing it. Nothing happens when no tap is waiting on this item.
    func resume(itemID: String, accountID: String?, api: any KriaAPIClient) {
        guard result == nil, let pending = Self.pending(accountID: accountID, itemID: itemID) else { return }
        result = GeneratedCardResult(id: pending.cardID, title: pending.title, eventID: pending.eventID, operationID: nil, phase: .making)
        storageKey = Self.key(accountID: accountID, itemID: itemID)
        task = Task { [weak self] in await self?.follow(itemID: itemID, accountID: accountID, event: pending.eventID, api: api) }
    }

    func dismiss() {
        task?.cancel()
        if let result { forget(eventID: result.eventID) }
        result = nil
    }

    func stop() { task?.cancel() }

    private func follow(itemID: String, accountID: String?, event: String, api: any KriaAPIClient) async {
        var unseen = 0
        for _ in 0..<rounds {
            if Task.isCancelled { return }
            do {
                let operation = try await api.generatedMediaByEvent(itemID: itemID, clientEventID: event)
                guard !Task.isCancelled, result?.eventID == event else { return }
                result?.operationID = operation.id
                switch operation.state {
                case .pending, .submitting, .providerComplete:
                    result?.phase = .making
                case .ready:
                    result?.phase = operation.canAccept ? .ready : .unsure
                    return
                case .unknown:
                    result?.phase = .unsure
                    return
                case .failed:
                    result?.phase = .failed
                    return
                }
            } catch is CancellationError {
                return
            } catch {
                if case .requestFailed(let status, _) = error as? APIError, status == 404 {
                    unseen += 1
                    if unseen >= unseenRounds, result?.operationID == nil {
                        if result?.eventID == event { result = nil }
                        forget(eventID: event)
                        return
                    }
                }
                // Anything else is a bad moment on the network; the operation is still there.
            }
            try? await Task.sleep(for: interval)
        }
        if result?.eventID == event, result?.phase == .making { result?.phase = .unsure }
    }

    // MARK: Surviving a closed app

    private struct Pending: Codable {
        let cardID: String
        let title: String
        let eventID: String
    }

    private var storageKey: String?

    private static func key(accountID: String?, itemID: String) -> String? {
        accountID.map { "generated-card." + $0 + "." + itemID }
    }

    private static func pending(accountID: String?, itemID: String) -> Pending? {
        key(accountID: accountID, itemID: itemID)
            .flatMap { UserDefaults.standard.data(forKey: $0) }
            .flatMap { try? JSONDecoder().decode(Pending.self, from: $0) }
    }

    private func remember(accountID: String?, itemID: String) {
        guard let result, let key = Self.key(accountID: accountID, itemID: itemID),
              let data = try? JSONEncoder().encode(Pending(cardID: result.id, title: result.title, eventID: result.eventID)) else { return }
        storageKey = key
        UserDefaults.standard.set(data, forKey: key)
    }

    private func forget(eventID: String) {
        guard let storageKey, let data = UserDefaults.standard.data(forKey: storageKey),
              let saved = try? JSONDecoder().decode(Pending.self, from: data), saved.eventID == eventID else { return }
        UserDefaults.standard.removeObject(forKey: storageKey)
    }

    /// Puts the finished image where the overlay review looks (`GeneratedOverlaySet.stage`) and says whether it could.
    func stageForReview(prompt: String, accountID: String, itemID: String) -> Bool {
        guard let result, let operationID = result.operationID, result.phase == .ready || result.phase == .unsure else { return false }
        GeneratedOverlaySet.stage(prompt: prompt, eventID: result.eventID, operationID: operationID, accountID: accountID, itemID: itemID)
        return true
    }
}
