import Foundation
import SwiftUI

/// What went wrong, in the six outcomes the creator can act on (Paper S1.09). One value per outcome keeps the
/// copy, the tone and the live button together, so a view never rebuilds them from status strings.
enum OverlayProblem: Hashable {
    case couldNotMake(failed: Int, total: Int)
    case unsure
    case didNotSave(count: Int)
    case changedAfterStart
    case sourceGone
    case sessionEnded
    case updateRequired
    case offline
    case couldNotAdd

    /// Failures that belong to the account or the connection, not to one overlay.
    var isSetWide: Bool {
        switch self {
        case .sessionEnded, .updateRequired, .offline, .couldNotAdd: true
        default: false
        }
    }

    /// Shown in the failure tone (red), where butter never sits beside it and the primary turns ink.
    var isFailure: Bool {
        switch self {
        case .unsure, .offline, .changedAfterStart, .sourceGone: false
        default: true
        }
    }

    static func classifyRequest(_ error: Error) -> OverlayProblem {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .notConnectedToInternet, .dataNotAllowed, .internationalRoamingOff: return .offline
            default: return .unsure
            }
        }
        guard let api = error as? APIError else { return .unsure }
        switch api {
        case .sessionExpired: return .sessionEnded
        case .nativeUpdateRequired: return .updateRequired
        case .offline: return .offline
        default: return GeneratedMediaRequestFailure.isDefiniteRejection(error) ? .couldNotMake(failed: 1, total: 1) : .unsure
        }
    }

    /// A rejected request never reached the provider, so its recovery record is dropped and the prompt stays.
    static func isDefinitelyRejected(_ error: Error) -> Bool {
        if case .couldNotMake = classifyRequest(error) { return true }
        return false
    }
}

enum OverlayLimits {
    static let maxOverlays = 8
    static let maxPrompt = 1500
}

/// One paid request and what came back: the prompt that was sent, the idempotency record, the latest
/// server state and what the creator should be told about it.
struct OverlayGeneration {
    var prompt: String
    var recovery: GeneratedMediaRecovery
    var operation: GeneratedMediaOperation?
    var problem: OverlayProblem?
    /// The wait outran the poll budget. The server is still working, so this is not a failure.
    var stalled = false

    enum Phase { case making, ready, failed, unsure, unsaved }

    var phase: Phase {
        if let operation {
            switch operation.state {
            case .ready: return operation.canAccept ? .ready : .unsure
            case .pending, .submitting: return .making
            case .providerComplete: return operation.canRetryStorage ? .unsaved : .making
            case .unknown: return .unsure
            case .failed: return .failed
            }
        }
        switch problem {
        case .unsure, .offline: return .unsure
        case .some: return .failed
        case .none: return .making
        }
    }

    var previewURL: URL? { operation.flatMap { $0.previewURL ?? $0.sourceURL } }
}

struct OverlayCard: Identifiable {
    let id: UUID
    var prompt: String
    /// 1-based position of the card this one started as a copy of, while its text is still that copy.
    var copiedFrom: Int?
    var copiedPrompt: String?
    var generation: OverlayGeneration?
    var remake: OverlayGeneration?
    var accepted = false

    init(id: UUID = UUID(), prompt: String = "", copiedFrom: Int? = nil) {
        self.id = id
        self.prompt = prompt
        self.copiedFrom = copiedFrom
        self.copiedPrompt = copiedFrom == nil ? nil : prompt
    }

    var isDraft: Bool { generation == nil }
    var showsCopyHint: Bool { copiedFrom != nil && prompt == copiedPrompt }
    var trimmedPrompt: String { prompt.trimmingCharacters(in: .whitespacesAndNewlines) }
    var promptIsValid: Bool { !trimmedPrompt.isEmpty && prompt.count <= OverlayLimits.maxPrompt }
}

/// Everything one "make overlays" session knows: the cards, which screen shows them, and the requests behind
/// them. Each card owns its prompt and its own idempotent request, so "N overlays" is N existing
/// `generated-media` operations, not a new server contract.
@MainActor final class GeneratedOverlaySet: ObservableObject, Identifiable {
    private static let pollRounds = 60
    private static let pollInterval: Duration = .seconds(3)
    private static let concurrentRequests = 3

    enum Origin: String, Codable { case describe, decide }
    enum Screen: Equatable { case choose, compose, making, results, edit(UUID), remade(UUID) }

    let id = UUID()
    @Published var screen: Screen = .choose
    @Published private(set) var origin: Origin = .describe
    @Published private(set) var cards: [OverlayCard] = []
    @Published private(set) var setProblem: OverlayProblem?
    @Published private(set) var isAdding = false

    private enum Slot { case main, remake }
    private struct DriverKey: Hashable { let card: UUID; let slot: Slot }

    private var api: (any KriaAPIClient)?
    private var itemID: String?
    private var storageKey: String?
    private var drivers: [DriverKey: Task<Void, Never>] = [:]
    private let gate = RequestGate(limit: GeneratedOverlaySet.concurrentRequests)

    // MARK: Derived state

    var hasContent: Bool { cards.contains { $0.generation != nil || $0.remake != nil } }
    var drafts: [OverlayCard] { cards.filter(\.isDraft) }
    var madeCards: [OverlayCard] { cards.filter { !$0.isDraft } }
    var readyCount: Int { cards.filter { $0.generation?.phase == .ready && !$0.accepted }.count }
    var makingCount: Int { cards.filter { $0.generation?.phase == .making }.count }
    var isStalled: Bool { cards.contains { $0.generation?.phase == .making && $0.generation?.stalled == true } }
    var canAddMore: Bool { cards.count < OverlayLimits.maxOverlays }
    /// Made cards that have an answer to show: used by the "still being made" notice.
    var isMakingAny: Bool { makingCount > 0 }
    var hasUnreviewed: Bool { hasContent && !cards.allSatisfy(\.accepted) }

    func index(of id: UUID) -> Int? { cards.firstIndex { $0.id == id } }
    func card(_ id: UUID) -> OverlayCard? { cards.first { $0.id == id } }
    func number(of id: UUID) -> Int { (index(of: id) ?? 0) + 1 }

    /// Notices to show, set-wide first, then per-outcome counts over the cards.
    var problems: [OverlayProblem] {
        var result: [OverlayProblem] = []
        if let setProblem { result.append(setProblem) }
        let made = madeCards.compactMap(\.generation)
        let failed = made.filter { $0.phase == .failed }
        if !failed.isEmpty {
            let kinds = failed.compactMap(\.problem)
            if kinds.contains(.sessionEnded), setProblem != .sessionEnded { result.append(.sessionEnded) }
            else if kinds.contains(.updateRequired), setProblem != .updateRequired { result.append(.updateRequired) }
            else { result.append(.couldNotMake(failed: failed.count, total: made.count)) }
        }
        if made.contains(where: { $0.phase == .unsure && $0.problem != .offline }) { result.append(.unsure) }
        else if made.contains(where: { $0.phase == .unsure }), setProblem != .offline { result.append(.offline) }
        let unsaved = made.filter { $0.phase == .unsaved }.count
        if unsaved > 0 { result.append(.didNotSave(count: unsaved)) }
        return result
    }

    // MARK: Setup and persistence

    func configure(api: any KriaAPIClient, itemID: String, accountID: String) {
        self.api = api
        guard self.itemID != itemID || storageKey == nil else { return }
        self.itemID = itemID
        storageKey = Self.storageKey(accountID: accountID, itemID: itemID)
        restoreSaved()
    }

    private static func storageKey(accountID: String, itemID: String) -> String? {
        (try? JSONEncoder().encode([accountID, itemID])).map { "generated-overlays." + $0.base64EncodedString() }
    }

    /// Hands an image another surface already paid for to the review screen: the next set that is configured for this
    /// account and item restores it as a made overlay and looks it up by its operation, exactly like one this sheet
    /// started and was interrupted on. Staged once per request.
    static func stage(prompt: String, eventID: String, operationID: String, accountID: String, itemID: String) {
        guard let key = storageKey(accountID: accountID, itemID: itemID) else { return }
        var saved = UserDefaults.standard.data(forKey: key).flatMap { try? JSONDecoder().decode(Saved.self, from: $0) }
            ?? Saved(origin: .describe, cards: [])
        guard !saved.cards.contains(where: { $0.main?.recovery.clientEventID == eventID }) else { return }
        let recovery = GeneratedMediaRecovery(clientEventID: eventID, operationID: operationID)
        saved.cards.append(.init(id: UUID(), prompt: prompt, copiedFrom: nil, copiedPrompt: nil,
                                 main: .init(prompt: prompt, recovery: recovery), remake: nil, accepted: false))
        if let data = try? JSONEncoder().encode(saved) { UserDefaults.standard.set(data, forKey: key) }
    }

    func stopWork() {
        drivers.values.forEach { $0.cancel() }
        drivers.removeAll()
    }

    /// Picks the requests back up after `stopWork` ran for a reason that was not a real dismissal (the Add media
    /// content disappears while a full-screen cover such as the camera is up). Requests that outran the poll
    /// budget stay stalled: the creator asks for those with Check again.
    func resumeWork() {
        for card in cards where !card.accepted {
            for slot in [Slot.main, .remake] {
                let key = DriverKey(card: card.id, slot: slot)
                guard drivers[key] == nil, let value = generation(card.id, slot),
                      value.phase == .making, !value.stalled else { continue }
                drive(card.id, slot, post: false)
            }
        }
    }

    private struct Saved: Codable {
        struct SavedCard: Codable {
            let id: UUID
            var prompt: String
            var copiedFrom: Int?
            var copiedPrompt: String?
            var main: SavedGeneration?
            var remake: SavedGeneration?
            var accepted: Bool
        }
        struct SavedGeneration: Codable { var prompt: String; var recovery: GeneratedMediaRecovery }
        var origin: Origin
        var cards: [SavedCard]
    }

    private func persist() {
        guard let storageKey else { return }
        let keep = cards.contains { $0.generation != nil || $0.remake != nil || !$0.prompt.isEmpty }
        guard keep, !cards.allSatisfy(\.accepted) else { UserDefaults.standard.removeObject(forKey: storageKey); return }
        let saved = Saved(origin: origin, cards: cards.map { card in
            .init(id: card.id, prompt: card.prompt, copiedFrom: card.copiedFrom, copiedPrompt: card.copiedPrompt,
                  main: card.generation.map { .init(prompt: $0.prompt, recovery: $0.recovery) },
                  remake: card.remake.map { .init(prompt: $0.prompt, recovery: $0.recovery) }, accepted: card.accepted)
        })
        if let data = try? JSONEncoder().encode(saved) { UserDefaults.standard.set(data, forKey: storageKey) }
    }

    private func restoreSaved() {
        guard let storageKey, cards.isEmpty, let data = UserDefaults.standard.data(forKey: storageKey),
              let saved = try? JSONDecoder().decode(Saved.self, from: data), !saved.cards.isEmpty else { return }
        origin = saved.origin
        cards = saved.cards.map { value in
            var card = OverlayCard(id: value.id, prompt: value.prompt, copiedFrom: value.copiedFrom)
            card.copiedPrompt = value.copiedPrompt
            card.accepted = value.accepted
            card.generation = value.main.map { OverlayGeneration(prompt: $0.prompt, recovery: $0.recovery) }
            card.remake = value.remake.map { OverlayGeneration(prompt: $0.prompt, recovery: $0.recovery) }
            return card
        }
        screen = initialScreen()
        for card in cards {
            if card.generation != nil, !card.accepted { drive(card.id, .main, post: false) }
            if card.remake != nil { drive(card.id, .remake, post: false) }
        }
    }

    /// Where reopening the sheet lands: never a disabled form, always the newest truth.
    func initialScreen() -> Screen {
        if let card = cards.first(where: { $0.remake != nil }) { return .remade(card.id) }
        if !hasContent { return cards.isEmpty ? .choose : .compose }
        return makingCount > 0 ? .making : .results
    }

    func resumeScreen() { screen = initialScreen() }

    // MARK: Composing

    func start(_ origin: Origin, ideas: [String] = []) {
        self.origin = origin
        cards = origin == .decide && !ideas.isEmpty ? ideas.map { OverlayCard(prompt: $0) } : [OverlayCard()]
        screen = .compose
        persist()
    }

    func setPrompt(_ text: String, for id: UUID) {
        guard let index = index(of: id) else { return }
        cards[index].prompt = text
        persist()
    }

    /// One tap: a new card that starts as a copy of the previous prompt.
    func addCard() {
        guard canAddMore else { return }
        let last = cards.last
        let card = OverlayCard(prompt: last?.prompt ?? "", copiedFrom: last.map { _ in cards.count })
        cards.append(card)
        screen = .compose
        persist()
    }

    func remove(_ id: UUID) {
        guard let index = index(of: id), cards.count > 1 || cards[index].generation != nil else { return }
        drivers[DriverKey(card: id, slot: .main)]?.cancel()
        drivers[DriverKey(card: id, slot: .remake)]?.cancel()
        cards.remove(at: index)
        if cards.isEmpty { screen = .choose }
        else if screen == .results, cards.allSatisfy(\.isDraft) { screen = .compose }
        persist()
    }

    func backFromCompose() {
        if hasContent { cards.removeAll { $0.isDraft }; screen = initialScreen() } else { screen = .choose; cards = [] }
        persist()
    }

    // MARK: Requests

    func makeOverlays() {
        for card in cards where card.isDraft && card.promptIsValid { beginGeneration(card.id, slot: .main) }
        setProblem = nil
        screen = .making
    }

    /// Redo and Try again: the same prompt in a new request, replacing only this overlay.
    func redo(_ id: UUID) {
        guard let card = card(id) else { return }
        if card.remake != nil { return }
        // A request still being made is paid for already: a second one would bill again and drop the first.
        if card.generation?.phase == .making { return }
        beginGeneration(id, slot: .main, prompt: card.generation?.prompt ?? card.trimmedPrompt)
    }

    /// Start again: drops every made version that was not accepted and returns to the prompts that made them.
    func startAgain() {
        for index in cards.indices where !cards[index].accepted {
            drivers[DriverKey(card: cards[index].id, slot: .main)]?.cancel()
            drivers[DriverKey(card: cards[index].id, slot: .remake)]?.cancel()
            if let prompt = cards[index].generation?.prompt { cards[index].prompt = prompt }
            cards[index].generation = nil
            cards[index].remake = nil
        }
        setProblem = nil
        screen = cards.contains { $0.isDraft } ? .compose : .results
        persist()
    }

    func retryFailed() {
        for card in cards where card.generation?.phase == .failed { redo(card.id) }
        setProblem = nil
    }

    func remake(_ id: UUID, prompt: String) {
        guard card(id) != nil, !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        beginGeneration(id, slot: .remake, prompt: prompt.trimmingCharacters(in: .whitespacesAndNewlines))
        screen = .remade(id)
    }

    func retryRemake(_ id: UUID) {
        guard let prompt = card(id)?.remake?.prompt else { return }
        beginGeneration(id, slot: .remake, prompt: prompt)
    }

    func useRemake(_ id: UUID) {
        guard let index = index(of: id), let remake = cards[index].remake, remake.phase == .ready else { return }
        drivers[DriverKey(card: id, slot: .main)]?.cancel()
        cards[index].generation = remake
        cards[index].prompt = remake.prompt
        cards[index].copiedFrom = nil
        cards[index].remake = nil
        cards[index].accepted = false
        screen = .results
        persist()
    }

    func discardRemake(_ id: UUID) {
        guard let index = index(of: id) else { return }
        drivers[DriverKey(card: id, slot: .remake)]?.cancel()
        cards[index].remake = nil
        screen = .results
        persist()
    }

    func discardUnfinished() {
        for card in cards where card.generation?.phase == .making {
            drivers[DriverKey(card: card.id, slot: .main)]?.cancel()
            guard let index = index(of: card.id) else { continue }
            cards[index].generation = nil
        }
        screen = cards.contains { $0.generation != nil } ? .results : .compose
        persist()
    }

    /// Check again: asks about the requests already made. It never starts a new generation.
    func checkAgain() {
        setProblem = nil
        for card in cards {
            if let generation = card.generation, generation.phase == .unsure || (generation.phase == .making && generation.stalled) {
                markReviewing(card.id, .main)
                drive(card.id, .main, post: false)
            }
            if let generation = card.remake, generation.phase == .unsure || (generation.phase == .making && generation.stalled) {
                markReviewing(card.id, .remake)
                drive(card.id, .remake, post: false)
            }
        }
    }

    func saveAgain() {
        guard let api, let itemID else { return }
        for card in cards {
            guard let generation = card.generation, generation.phase == .unsaved, let operation = generation.operation else { continue }
            let id = card.id
            Task { @MainActor in
                do {
                    let updated = try await api.retryGeneratedMediaStorage(itemID: itemID, operationID: operation.id)
                    update(id, .main) { $0.operation = updated; $0.problem = nil }
                    drive(id, .main, post: false)
                } catch { setProblem = OverlayProblem.classifyRequest(error).isSetWide ? OverlayProblem.classifyRequest(error) : .didNotSave(count: 1) }
            }
        }
    }

    /// Accepts every ready overlay in order. Overlays already accepted are skipped, so a second try after
    /// a partial failure never repeats work.
    func useOverlays(done: @escaping () async -> Void) async -> Bool {
        guard let api, let itemID, !isAdding else { return false }
        isAdding = true
        defer { isAdding = false }
        setProblem = nil
        for card in cards where card.generation?.phase == .ready && !card.accepted {
            guard let operation = card.generation?.operation else { continue }
            do {
                _ = try await api.acceptGeneratedMedia(itemID: itemID, operationID: operation.id, body: GeneratedMediaAcceptance())
                if let index = index(of: card.id) { cards[index].accepted = true }
            } catch {
                let problem = OverlayProblem.classifyRequest(error)
                setProblem = problem.isSetWide ? problem : .couldNotAdd
                persist()
                return false
            }
        }
        await done()
        finish()
        return true
    }

    private func finish() {
        stopWork()
        cards = []
        screen = .choose
        if let storageKey { UserDefaults.standard.removeObject(forKey: storageKey) }
    }

    // MARK: Drivers

    private func generation(_ id: UUID, _ slot: Slot) -> OverlayGeneration? {
        slot == .main ? card(id)?.generation : card(id)?.remake
    }

    private func update(_ id: UUID, _ slot: Slot, _ change: (inout OverlayGeneration) -> Void) {
        guard let index = index(of: id) else { return }
        switch slot {
        case .main: if var value = cards[index].generation { change(&value); cards[index].generation = value }
        case .remake: if var value = cards[index].remake { change(&value); cards[index].remake = value }
        }
    }

    private func markReviewing(_ id: UUID, _ slot: Slot) {
        update(id, slot) { $0.problem = nil; $0.stalled = false }
    }

    private func beginGeneration(_ id: UUID, slot: Slot, prompt: String? = nil) {
        guard let index = index(of: id) else { return }
        drivers[DriverKey(card: id, slot: slot)]?.cancel()
        let text = prompt ?? cards[index].trimmedPrompt
        let value = OverlayGeneration(prompt: text, recovery: GeneratedMediaRecovery(clientEventID: UUID().uuidString, operationID: nil))
        switch slot {
        case .main:
            cards[index].generation = value
            cards[index].accepted = false
        case .remake: cards[index].remake = value
        }
        persist()
        drive(id, slot, post: true)
    }

    private func drive(_ id: UUID, _ slot: Slot, post: Bool) {
        let key = DriverKey(card: id, slot: slot)
        drivers[key]?.cancel()
        drivers[key] = Task { @MainActor [weak self] in
            guard let self else { return }
            await self.run(id, slot, post: post)
            if !Task.isCancelled { self.drivers[key] = nil }
        }
    }

    private func run(_ id: UUID, _ slot: Slot, post: Bool) async {
        guard let api, let itemID, let start = generation(id, slot) else { return }
        let event = start.recovery.clientEventID
        do {
            if post, start.operation == nil {
                await gate.enter()
                defer { Task { await gate.leave() } }
                let request = GeneratedMediaRequest(
                    clientEventID: event, capability: .image, prompt: start.prompt,
                    transparentBackground: false, editableTitle: String(start.prompt.prefix(60)))
                let operation = try await api.generateMedia(itemID: itemID, body: request)
                guard !Task.isCancelled, generation(id, slot)?.recovery.clientEventID == event else { return }
                store(operation, id, slot)
            } else if let operationID = start.recovery.operationID {
                let operation = try await api.generatedMedia(itemID: itemID, operationID: operationID)
                guard !Task.isCancelled, generation(id, slot)?.recovery.clientEventID == event else { return }
                store(operation, id, slot)
            } else {
                let operation = try await api.generatedMediaByEvent(itemID: itemID, clientEventID: event)
                guard !Task.isCancelled, generation(id, slot)?.recovery.clientEventID == event else { return }
                store(operation, id, slot)
            }
        } catch is CancellationError {
            return
        } catch {
            guard !Task.isCancelled, generation(id, slot)?.recovery.clientEventID == event else { return }
            report(error, id, slot, wasPost: post && start.operation == nil)
            return
        }
        await poll(id, slot, event: event)
    }

    private func poll(_ id: UUID, _ slot: Slot, event: String) async {
        guard let api, let itemID else { return }
        for _ in 0..<Self.pollRounds {
            guard let current = generation(id, slot), current.recovery.clientEventID == event,
                  let operation = current.operation, operation.shouldPoll, !Task.isCancelled else { return }
            do {
                try await Task.sleep(for: Self.pollInterval)
                let latest = try await api.generatedMedia(itemID: itemID, operationID: operation.id)
                guard !Task.isCancelled, generation(id, slot)?.recovery.clientEventID == event else { return }
                store(latest, id, slot)
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled else { return }
                update(id, slot) { $0.problem = OverlayProblem.classifyRequest(error) == .offline ? .offline : .unsure }
                return
            }
        }
        guard let current = generation(id, slot), current.recovery.clientEventID == event,
              current.operation?.shouldPoll == true else { return }
        update(id, slot) { $0.stalled = true }
    }

    private func store(_ operation: GeneratedMediaOperation, _ id: UUID, _ slot: Slot) {
        update(id, slot) {
            $0.operation = operation
            $0.recovery.operationID = operation.id
            $0.problem = nil
            $0.stalled = false
        }
        persist()
        advanceScreenIfFinished()
    }

    private func report(_ error: Error, _ id: UUID, _ slot: Slot, wasPost: Bool) {
        var problem = OverlayProblem.classifyRequest(error)
        if case .requestFailed(let status, _) = error as? APIError, status == 404, !wasPost {
            // The server has no record of this request, so it never arrived and starting again cannot double up.
            problem = .couldNotMake(failed: 1, total: 1)
        }
        if problem.isSetWide, problem != .offline { setProblem = problem }
        update(id, slot) { $0.problem = problem }
        advanceScreenIfFinished()
    }

    /// The making screen gives way to the list once nothing is left to wait for.
    private func advanceScreenIfFinished() {
        if screen == .making, makingCount == 0 { screen = .results }
    }
}

/// A tiny async semaphore: image requests are paid and slow, so at most a few are in flight at once.
actor RequestGate {
    private var available: Int
    private var waiters: [CheckedContinuation<Void, Never>] = []
    init(limit: Int) { available = limit }
    func enter() async {
        if available > 0 { available -= 1; return }
        await withCheckedContinuation { waiters.append($0) }
    }
    func leave() {
        if waiters.isEmpty { available += 1 } else { waiters.removeFirst().resume() }
    }
}

/// Where "Let Kria decide" gets its variations. Nothing is sent until the creator taps Make, so a proposal is
/// only text they can read, edit and remove.
enum OverlayIdeaSource {
    /// The server has no endpoint that proposes image prompts outside the editor, so ideas are three
    /// lighting and framing variations of the edit's own title.
    static func ideas(for title: String) -> [String] {
        let subject = title.trimmingCharacters(in: .whitespacesAndNewlines)
        let base = subject.isEmpty ? "A simple, bright image for my video" : subject
        return [
            "\(base), soft morning light",
            "\(base), close detail, warm window light",
            "\(base), golden hour, long shadows",
        ]
    }
}

#if DEBUG
extension GeneratedOverlaySet {
    /// Fixed states for screenshot review (`GenerateImagePreviewHost`). Nothing here talks to the server.
    func installPreview(screen: Screen, origin: Origin = .describe, cards: [OverlayCard], setProblem: OverlayProblem? = nil) {
        self.origin = origin
        self.cards = cards
        self.screen = screen
        self.setProblem = setProblem
    }
}

extension OverlayGeneration {
    static func preview(_ prompt: String, state: String, url: URL?, stalled: Bool = false, retryable: Bool = false) -> OverlayGeneration {
        var json: [String: Any] = ["id": UUID().uuidString, "state": state, "capability": "image", "retryable": retryable]
        if let url { json["preview_url"] = url.absoluteString; json["source_url"] = url.absoluteString; json["asset_id"] = "preview-asset" }
        let data = (try? JSONSerialization.data(withJSONObject: json)) ?? Data()
        var value = OverlayGeneration(prompt: prompt, recovery: GeneratedMediaRecovery(clientEventID: UUID().uuidString, operationID: nil))
        value.operation = try? JSONDecoder().decode(GeneratedMediaOperation.self, from: data)
        value.stalled = stalled
        return value
    }
}
#endif
