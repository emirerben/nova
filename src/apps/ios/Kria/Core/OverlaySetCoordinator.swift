import Foundation

/// Drives one chat's overlay sets: the creator's answers, the plan, and every paid `generate`. The thread is the state;
/// this only decides which call is next (`OverlayDriver`) and keeps each paid call to one at a time.
@MainActor final class OverlaySetCoordinator: ObservableObject {
    enum Failure: Equatable { case answer, plan }

    private static let maxRounds = 60

    private var api: (any OverlaySetClient)?
    private var thread: UUID?
    private var itemID: () -> String? = { nil }
    private var planRequest: () -> OverlayPlanRequest? = { nil }
    private var refresh: () async -> Void = {}
    private let retryDelay: Duration
    private var closedKey: String?

    @Published private(set) var drafts: [String: OverlayComposeDraft] = [:]
    @Published private(set) var sending: Set<String> = []
    @Published private(set) var failures: [String: Failure] = [:]
    @Published private(set) var retrying: Set<String> = []
    @Published private(set) var thumbnails: [String: URL] = [:]
    @Published private(set) var closed: Set<String> = []
    @Published private(set) var openFailed = false
    @Published private(set) var answered: [String: OverlaySet] = [:]

    private var memory = OverlayDriverMemory()
    private var latest: [String: OverlaySet] = [:]
    private var tasks: [String: Task<Void, Never>] = [:]
    private var lookups: Set<String> = []

    init(retryDelay: Duration = .seconds(3)) { self.retryDelay = retryDelay }

    convenience init(client: any OverlaySetClient, threadID: UUID, itemID: @escaping () -> String?, planRequest: @escaping () -> OverlayPlanRequest?,
                     refresh: @escaping () async -> Void, retryDelay: Duration = .seconds(3)) {
        self.init(retryDelay: retryDelay)
        configure(client: client, threadID: threadID, itemID: itemID, planRequest: planRequest, refresh: refresh)
    }

    /// The closures read the chat's current state each time they run, so a later thread or edit is never a stale copy.
    func configure(client: any OverlaySetClient, threadID: UUID, itemID: @escaping () -> String?, planRequest: @escaping () -> OverlayPlanRequest?,
                   refresh: @escaping () async -> Void) {
        api = client
        self.itemID = itemID
        self.planRequest = planRequest
        self.refresh = refresh
        guard thread != threadID else { return }
        thread = threadID
        let key = "overlay-sets-closed.\(threadID.uuidString)"
        closedKey = key
        closed = Set(UserDefaults.standard.stringArray(forKey: key) ?? [])
    }

    /// The set as it is drawn: an ask the server already took the answer to reads as answered until the thread says so.
    func shown(_ set: OverlaySet) -> OverlaySet { set.phase == .ask ? answered[set.id] ?? set : set }

    func draft(for setID: String) -> OverlayComposeDraft? { drafts[setID] }
    func failure(for setID: String) -> Failure? { failures[setID] }
    func thumbnail(for operationID: String?) -> URL? { operationID.flatMap { thumbnails[$0] } }
    func isSending(_ setID: String) -> Bool { sending.contains(setID) }
    func isRetrying(set: String, overlay: String) -> Bool { retrying.contains(OverlayDriverMemory.key(set: set, overlay: overlay)) }

    func waitForIdle() async {
        while let task = tasks.values.first { await task.value }
    }

    // MARK: The creator's side

    func openNew() { open(setID: UUID().uuidString.lowercased()) }

    func open(setID: String) {
        run("open/\(setID)") { [self] in
            openFailed = false
            do { adopt(try await requireAPI().openOverlaySet(threadID: requireThread(), setID: setID)) }
            catch { openFailed = true }
        }
    }

    func chooseAutomatic(_ setID: String) { answer(setID, mode: .automatic, prompts: []) }

    func beginDescribing(_ setID: String, maxRows: Int) {
        if drafts[setID] == nil { drafts[setID] = OverlayComposeDraft(maxRows: maxRows) }
    }

    func draftChanged(_ setID: String, _ change: (inout OverlayComposeDraft) -> Void) {
        guard var draft = drafts[setID] else { return }
        change(&draft)
        drafts[setID] = draft
    }

    func make(_ setID: String) {
        guard let draft = drafts[setID], draft.canMake else { return }
        answer(setID, mode: .prompt, prompts: draft.filled)
    }

    func cancel(_ setID: String) {
        drafts[setID] = nil
        failures[setID] = nil
        closed.insert(setID)
        if let closedKey { UserDefaults.standard.set(Array(closed), forKey: closedKey) }
    }

    func retryPlan(_ setID: String) {
        failures[setID] = nil
        memory.planned.remove(setID)
        reconcile(Array(latest.values))
    }

    func retry(set setID: String, overlay overlayID: String) {
        guard latest[setID]?.overlays.first(where: { $0.id == overlayID })?.canRetry == true else { return }
        let key = OverlayDriverMemory.key(set: setID, overlay: overlayID)
        guard tasks["generate/\(key)"] == nil else { return }
        retrying.insert(key)
        generate(setID, overlayID)
    }

    // MARK: What the thread shows

    func reconcile(_ sets: [OverlaySet]) {
        for set in sets { latest[set.id] = set }
        for step in OverlayDriver.steps(for: sets.filter { !closed.contains($0.id) }, memory: memory) {
            switch step {
            case .plan(let id): plan(id)
            case .generate(let set, let overlay): generate(set, overlay)
            }
        }
        for set in sets { for overlay in set.reviewItems { lookUp(overlay.operationID) } }
    }

    // MARK: Calls

    private func requireAPI() throws -> any OverlaySetClient {
        guard let api else { throw APIError.unsupported }
        return api
    }

    private func requireThread() throws -> UUID {
        guard let thread else { throw APIError.unsupported }
        return thread
    }

    private func adopt(_ set: OverlaySet) {
        reconcile([set])
        Task { await refresh() }
    }

    private func run(_ key: String, _ body: @escaping @MainActor () async -> Void) {
        guard tasks[key] == nil else { return }
        tasks[key] = Task { @MainActor [weak self] in
            await body()
            self?.tasks[key] = nil
        }
    }

    private func answer(_ setID: String, mode: OverlaySet.Mode, prompts: [String]) {
        guard !sending.contains(setID) else { return }
        sending.insert(setID)
        failures[setID] = nil
        run("answer/\(setID)") { [self] in
            defer { sending.remove(setID) }
            do {
                let stored = try await requireAPI().composeOverlaySet(threadID: requireThread(), setID: setID, mode: mode, prompts: prompts)
                answered[setID] = stored
                drafts[setID] = nil
                adopt(stored)
            } catch APIError.conflict {
                await refresh()
            } catch {
                failures[setID] = .answer
            }
        }
    }

    private func plan(_ setID: String) {
        guard let body = planRequest(), tasks["plan/\(setID)"] == nil else { return }
        memory.planned.insert(setID)
        run("plan/\(setID)") { [self] in
            for _ in 0..<Self.maxRounds {
                do {
                    try await requireAPI().planOverlaySet(threadID: requireThread(), setID: setID, body: body)
                    await refresh()
                    return
                } catch APIError.conflict {
                    try? await Task.sleep(for: retryDelay)
                } catch {
                    memory.planned.remove(setID)
                    failures[setID] = .plan
                    return
                }
            }
        }
    }

    private func generate(_ setID: String, _ overlayID: String) {
        let key = OverlayDriverMemory.key(set: setID, overlay: overlayID)
        guard tasks["generate/\(key)"] == nil else { return }
        memory.started.insert(key)
        run("generate/\(key)") { [self] in
            defer { retrying.remove(key) }
            for _ in 0..<Self.maxRounds {
                do {
                    let result = try await requireAPI().generateOverlay(threadID: requireThread(), setID: setID, overlayID: overlayID)
                    guard result.overlays.first(where: { $0.id == overlayID })?.state == .making else { break }
                    try? await Task.sleep(for: retryDelay)
                } catch {
                    if Self.isTransient(error) { memory.started.remove(key) }
                    break
                }
            }
            await refresh()
        }
    }

    private func lookUp(_ operationID: String?) {
        guard let operationID, thumbnails[operationID] == nil, !lookups.contains(operationID), let item = itemID() else { return }
        lookups.insert(operationID)
        run("media/\(operationID)") { [self] in
            if let url = try? await requireAPI().generatedMedia(itemID: item, operationID: operationID).previewURL {
                thumbnails[operationID] = url
            } else {
                lookups.remove(operationID)
            }
        }
    }

    private static func isTransient(_ error: Error) -> Bool {
        if error is URLError { return true }
        guard let api = error as? APIError else { return false }
        switch api {
        case .offline: return true
        case .requestFailed(let status, _): return status >= 500 || status == 408 || status == 429
        default: return false
        }
    }
}
