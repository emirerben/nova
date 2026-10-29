import Foundation

/// "Use this idea": checks the originals, adds each clip to a new chat, then hands the chat to the idea and opens
/// it. Owned by whoever started it (the Camera roll's model), not by a screen, so it keeps going while the creator
/// looks elsewhere and its progress sheet can be closed and reopened.
@MainActor @Observable final class CameraRollIdeaUse: @MainActor Identifiable {
    enum Step: Int, CaseIterable {
        case originals, clips, chat
        var title: String {
            switch self {
            case .originals: "Checking originals"
            case .clips: "Uploading clips"
            case .chat: "Starting your chat"
            }
        }
    }

    enum Phase: Equatable {
        case running(Step)
        case failed(Step, String)
        /// Some videos' full-quality originals are not on this iPhone; the creator picks them in Photos.
        case needsOriginals
        /// The chat exists but this phone will not open it (the creator deleted it).
        case blocked(String)
        case opened
    }

    /// Clips added to the chat at once. Preparing a clip (finding its original, copying it beside the chat) is
    /// the slow part; the server attaches them one at a time so each sees the chat's latest revision.
    static let parallelClips = 3

    let story: CameraRollStory
    var id: UUID { story.id }
    private(set) var phase: Phase = .running(.originals)
    private(set) var clipsDone = 0
    private(set) var clipsTotal = 0
    private(set) var missingOriginals: [SavedMediaAsset] = []

    @ObservationIgnored private let api: any KriaAPIClient
    /// Read on every attempt: a Retry after the collection moved on must send its current revision.
    @ObservationIgnored private let currentCollection: @MainActor () -> CameraRollCollection
    private var collection: CameraRollCollection { currentCollection() }
    @ObservationIgnored private let open: @MainActor (CreationThread) async -> Bool
    @ObservationIgnored private var task: Task<Void, Never>?
    /// The server held the idea's first turn; Retry asks it to try that turn again rather than starting over.
    @ObservationIgnored private var held: (threadID: UUID, turnID: UUID, revision: Int)?

    init(story: CameraRollStory, api: any KriaAPIClient, collection: @escaping @MainActor () -> CameraRollCollection,
         open: @escaping @MainActor (CreationThread) async -> Bool) {
        self.story = story; self.api = api; self.currentCollection = collection; self.open = open
    }

    var owner: String? { api.mediaLibraryOwner }
    var isRunning: Bool { if case .running = phase { true } else { false } }

    /// Every step is idempotent on the server (the thread, each clip and the selection carry stable ids), so
    /// starting again after a failure picks up where it stopped.
    func start() {
        guard !isRunning || task == nil else { return }
        task = Task { await run() }
    }

    private func run() async {
        if let held { await recover(held); return }
        phase = .running(.originals)
        clipsDone = 0
        do {
            guard let owner = api.mediaLibraryOwner else { throw APIError.unsupported }
            let assets = collection.entries.compactMap(\.savedAsset).filter { asset in story.moments.contains { $0.assetID == asset.id } }
            var missing: [SavedMediaAsset] = []
            for asset in assets where asset.kind == "video" {
                if try await SavedMediaReuse.adoptOriginal(asset: asset, owner: owner, api: api) == nil { missing.append(asset) }
            }
            if !missing.isEmpty { missingOriginals = missing; phase = .needsOriginals; return }

            phase = .running(.clips)
            var thread = try await api.createCameraRollStoryThread(id: story.id, revision: story.revision)
            guard let threadID = UUID(uuidString: thread.id), thread.runtimeVersion == 2 else { throw APIError.invalidResponse }
            var used = Set<UUID>()
            let clips = try story.moments.filter { used.insert($0.assetID).inserted }.map { moment -> Clip in
                guard let asset = collection.entries.compactMap(\.savedAsset).first(where: { $0.id == moment.assetID }),
                      let source = asset.sources.first(where: { $0.id == moment.sourceID && $0.generation == moment.generation }) else { throw APIError.invalidResponse }
                return Clip(asset: asset, source: source, mediaID: (source.role == "analysis_proxy" ? "analysis-proxy-" : "") + asset.id.uuidString)
            }
            clipsTotal = clips.count
            thread = try await addClips(clips, to: threadID, thread: thread, owner: owner)

            phase = .running(.chat)
            let accepted = try await api.selectCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0,
                                                               threadID: threadID, threadRevision: thread.revision, eventID: "choose-\(story.id)-\(story.revision)")
            if let heldTurnID = accepted.recoverableHeldTurnID {
                held = (threadID, heldTurnID, accepted.threadRevision)
                phase = .failed(.chat, "This idea stopped before a draft was prepared. Try again to get a new draft.")
                return
            }
            await finish(threadID)
        } catch {
            phase = .failed(currentStep, CameraRollCollectionFailure.message(error))
        }
    }

    private struct Clip: Sendable { let asset: SavedMediaAsset; let source: SavedMediaSource; let mediaID: String }

    /// Prepares up to `parallelClips` clips at once and attaches them to the chat in story order as they become ready.
    private func addClips(_ clips: [Clip], to threadID: UUID, thread start: CreationThread, owner: String) async throws -> CreationThread {
        var thread = start
        let api = api
        try await withThrowingTaskGroup(of: (Int, SavedMediaSource).self) { group in
            var launched = 0, next = 0
            var prepared: [Int: SavedMediaSource] = [:]
            func launch() {
                let index = launched, clip = clips[index]
                group.addTask {
                    (index, try await SavedMediaReuse.prepare(asset: clip.asset, source: clip.source, threadID: threadID, role: .clip, mediaID: clip.mediaID, owner: owner, api: api))
                }
                launched += 1
            }
            while launched < min(Self.parallelClips, clips.count) { launch() }
            for try await (index, source) in group {
                prepared[index] = source
                while let ready = prepared.removeValue(forKey: next) {
                    let clip = clips[next]
                    thread = try await SavedMediaReuse.attachPrepared(asset: clip.asset, source: ready, threadID: threadID, mediaID: clip.mediaID, role: .clip, api: api)
                    next += 1; clipsDone = next
                }
                if launched < clips.count { launch() }
            }
        }
        return thread
    }

    private func recover(_ held: (threadID: UUID, turnID: UUID, revision: Int)) async {
        phase = .running(.chat)
        do {
            let accepted = try await api.recoverCameraRollStory(collectionID: collection.id, story: story, collectionRevision: collection.serverRevision ?? 0,
                                                                threadID: held.threadID, threadRevision: held.revision,
                                                                eventID: "choose-\(story.id)-\(story.revision)", heldTurnID: held.turnID)
            guard let successor = UUID(uuidString: accepted.turnID), successor != held.turnID else { throw APIError.invalidResponse }
            self.held = nil
            await finish(held.threadID)
        } catch {
            phase = .failed(.chat, CameraRollCollectionFailure.message(error))
        }
    }

    private func finish(_ threadID: UUID) async {
        do {
            let current = try await api.project(threadID: threadID)
            phase = await open(current) ? .opened
                : .blocked("The chat for this idea was deleted, so it can't open again. Ask for another idea to start a new chat.")
        } catch {
            phase = .failed(.chat, CameraRollCollectionFailure.message(error))
        }
    }

    private var currentStep: Step {
        switch phase {
        case let .running(step), let .failed(step, _): step
        case .needsOriginals: .originals
        case .blocked, .opened: .chat
        }
    }
}
