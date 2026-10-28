import Foundation
import SwiftUI
import Photos
import PhotosUI
import Observation
import KriaMediaEngine

// KRI-944: what the full-screen Camera roll knows and does. Views read this model and send it intents; it owns
// the collection on disk, the per-item engine (`CameraRollItemEngine`), the per-event ideas board
// (`CameraRollIdeaBoard`) and every server call between them. It outlives the screen, so uploads and analysis
// carry on while the creator is in a chat.

struct CameraRollEvent: Identifiable {
    /// The server's group key. The creator's name for it is `CameraRollSurfaceModel.title(for:)`.
    let id: String
    let entries: [CameraRollCollection.Entry]
}

@MainActor @Observable final class CameraRollSurfaceModel {
    enum Mode: Equatable {
        case live
        /// Offline: no Photos access, no upload, a seeded collection. UI tests and screenshots.
        case fixture
        /// Diagnostic: Photos metadata only, nothing leaves the phone.
        case metadataOnly
    }

    struct Notice: Equatable {
        enum Kind { case info, error }
        var text: String
        var kind: Kind
    }

    /// Select-and-move inside one event (Paper A5).
    struct Selecting {
        var group: String
        var selected: Set<String> = []
        /// Opened from "Rename": the name field takes focus.
        var renaming = false
    }

    /// The inline "Find ideas" panel's inputs for one event (Paper A3).
    struct FinderDraft: Equatable {
        var query = ""
        var researchOn = false
        var topic = ""
    }

    static let undoWindow: Duration = .seconds(6)
    static let defaultQuery = "Suggest different content ideas from this outing"
    static let metadataOnlyOwner = "camera-roll-device-collection"
    static let fixtureOwner = "camera-roll-ui-fixture"

    @ObservationIgnored let api: any KriaAPIClient
    let mode: Mode
    let cacheOnlyRequired: Bool
    let store: CameraRollItemStore
    @ObservationIgnored let engine: CameraRollItemEngine

    private(set) var collection: CameraRollCollection {
        didSet { events = Self.events(of: collection) }
    }
    private(set) var events: [CameraRollEvent] = []
    private(set) var revision = 0
    var board = CameraRollIdeaBoard()
    /// What each event's last generation covered, kept so a moment can be swapped in the idea's detail.
    private(set) var coverage: [String: CameraRollStories.Coverage] = [:]
    private(set) var suggestedTopic: String?
    var notice: Notice?
    private(set) var collectionChanged = false
    var selecting: Selecting?
    /// The event whose inline "Find ideas" panel is open.
    var finderGroup: String?
    var drafts: [String: FinderDraft] = [:]
    /// Events whose page shows all of its footage rather than the first row.
    var expanded: Set<String> = []
    private(set) var taste: EditorialTasteProfile?
    private(set) var permission = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    private(set) var sharedItemCount = 0
    /// Items dropped because Photos access was withdrawn; shown until the creator dismisses it.
    private(set) var accessRemovedCount = 0
    private(set) var isRefreshing = false
    private(set) var isUploading = false
    private(set) var latestCollection: SavedCameraRollCollection?
    var showsLatestReview = false
    private(set) var started = false

    @ObservationIgnored private var providers: [String: PhotosPickerItem] = [:]
    @ObservationIgnored private var removedEntries: [String: [CameraRollCollection.Entry]] = [:]
    @ObservationIgnored private var removedInfo: [String: CameraRollEventInfo] = [:]
    @ObservationIgnored private var syncedSelection: CameraRollCollectionSelection?
    @ObservationIgnored private var previousPermission: PHAuthorizationStatus?
    @ObservationIgnored private var mutationChain: Task<Void, Never>?
    @ObservationIgnored private var undoExpiry: Task<Void, Never>?
    @ObservationIgnored private var resumedCollection = false
    @ObservationIgnored private var metadataElapsedMs = 0.0
    @ObservationIgnored private var pendingAddGroup: String?
    /// Fixture only: what the phone itself reports for a tile (an upload in flight) that no server snapshot covers.
    @ObservationIgnored private let localEvents: [String: [CameraRollItemEvent]]

    init(api: any KriaAPIClient, mode: Mode = .live, cacheOnlyRequired: Bool = false,
         seed: CameraRollCollection? = nil, localEvents: [String: [CameraRollItemEvent]] = [:],
         store: CameraRollItemStore = CameraRollItemStore()) {
        self.api = api; self.mode = mode; self.cacheOnlyRequired = cacheOnlyRequired
        self.localEvents = localEvents
        self.store = store
        engine = CameraRollItemEngine(client: api, store: store)
        let initial = seed ?? (try! CameraRollCollection(name: "My outing", identifiers: []))
        collection = initial
        events = Self.events(of: initial)
        revision = initial.serverRevision ?? 0
    }

    static func events(of collection: CameraRollCollection) -> [CameraRollEvent] {
        let map = collection.groupMap()
        var order: [String] = []
        var buckets: [String: [CameraRollCollection.Entry]] = [:]
        for entry in collection.entries {
            let key = map[entry.id] ?? collection.name
            if buckets[key] == nil { order.append(key) }
            buckets[key, default: []].append(entry)
        }
        for info in collection.events where buckets[info.group] == nil { order.append(info.group); buckets[info.group] = [] }
        return order.map { CameraRollEvent(id: $0, entries: buckets[$0] ?? []) }
    }

    // MARK: Derived

    /// Whose originals the collection lives beside. Checked again after every await that binds uploads, so a
    /// sign-out mid-call cannot attach another account's media.
    private var currentOwner: String? {
        switch mode {
        case .metadataOnly: Self.metadataOnlyOwner
        case .fixture: Self.fixtureOwner
        case .live: api.mediaLibraryOwner
        }
    }

    var originals: MediaLibraryOriginals? { currentOwner.map { MediaLibraryOriginals(owner: $0) } }

    var thumbnailsDisabled: Bool { mode != .live }
    var canAdd: Bool { mode != .metadataOnly && collection.entries.count < CameraRollCollection.maximumCount }
    var remainingCapacity: Int { CameraRollCollection.maximumCount - collection.entries.count }
    var isEmpty: Bool { collection.entries.isEmpty }
    func title(for group: String) -> String { collection.title(forGroup: group) }
    func about(for group: String) -> String { collection.about(forGroup: group) }
    /// About is only offered once the server has shown it stores it; titles work either way, on the phone at least.
    var aboutAvailable: Bool { collection.serverStoresEvents }

    func state(of entry: CameraRollCollection.Entry) -> CameraRollItemState { store.tile(entry.id).state }
    func readyCount(in event: CameraRollEvent) -> Int { event.entries.filter { state(of: $0) == .ready }.count }
    func waitingCount(in event: CameraRollEvent) -> Int { event.entries.filter { !state(of: $0).isSettled }.count }
    func retryCount(in event: CameraRollEvent) -> Int { event.entries.filter { if case .needsRetry = state(of: $0) { true } else { false } }.count }

    /// Said only while something in the event is unfinished; a settled event needs no readiness line.
    func readinessNote(_ event: CameraRollEvent) -> String? {
        let waiting = waitingCount(in: event), retry = retryCount(in: event)
        let parts = [waiting > 0 ? "\(waiting) still looking" : nil, retry > 0 ? "\(retry) \(retry == 1 ? "needs" : "need") a retry" : nil]
        let note = parts.compactMap { $0 }.joined(separator: " · ")
        return note.isEmpty ? nil : note
    }

    /// The header summary: settled items over all items, and how many wait on the creator.
    struct Progress: Equatable {
        var total = 0, settled = 0, done = 0, needRetry = 0
        var fraction: Double { total == 0 ? 0 : Double(settled) / Double(total) }
        var isWorking: Bool { total > 0 && settled < total }
    }
    var progress: Progress {
        let ready = store.count(.ready), excluded = store.count(.excluded), failed = store.count(.failed), retry = store.count(.needsRetry)
        let total = collection.entries.count
        return Progress(total: total, settled: ready + excluded + failed + retry, done: ready + excluded + failed, needRetry: retry)
    }

    func ideas(in group: String) -> [CameraRollStory] { (board.storiesByGroup[group] ?? []).filter { !$0.rejected } }
    func isGenerating(_ group: String) -> Bool { board.generating.contains(group) }
    func draft(for group: String) -> FinderDraft { drafts[group] ?? FinderDraft() }
    func setDraft(_ draft: FinderDraft, for group: String) { drafts[group] = draft }

    private var pollableCollectionID: UUID? { collection.serverRevision == nil ? nil : collection.id }

    // MARK: Lifecycle

    func start() async {
        guard !started else { return }
        started = true
        restore()
        syncEngine()
        // The model owns this work: the view's `.task` is cancelled when the creator opens a chat, and a resumed
        // upload must carry on rather than sit retryable until they come back and tap Try again.
        let launch = Task { await bootstrap() }
        await launch.value
    }

    private func bootstrap() async {
        if mode == .live || mode == .metadataOnly { await refreshMetadata() }
        if let id = pollableCollectionID { engine.startPolling(collectionID: id) }
        async let tasteLoaded: Void = loadTaste()
        if mode == .live {
            if collection.entries.contains(where: { $0.savedAsset == nil }) { await uploadPending() }
            await refreshIdeas()
        } else if mode == .fixture { await refreshIdeas() }
        await tasteLoaded
    }

    func scenePhaseChanged(active: Bool) {
        if active {
            guard started else { return }
            if mode != .fixture { Task { await refreshMetadata() } }
            if let id = pollableCollectionID { engine.startPolling(collectionID: id) }
        } else {
            // A poll left running in the background would outlive the screen's sign-in state.
            engine.stopPolling()
            persist()
        }
    }

    func pause() { engine.stopPolling(); persist() }

    private func syncEngine() {
        engine.sync(entries: collection.entries)
        for (id, events) in localEvents { for event in events { store.send(event, to: id) } }
    }

    /// The surface for this build: the real one, or an offline fixture under UI tests.
    static func make(api: any KriaAPIClient, cacheOnlyRequired: Bool) -> CameraRollSurfaceModel {
        #if DEBUG
        if CameraRollSurfaceFixture.isDrawerFixture { return CameraRollSurfaceFixture.model() }
        #endif
        return CameraRollSurfaceModel(api: api, cacheOnlyRequired: cacheOnlyRequired)
    }

    #if DEBUG
    func seedAccessNoticesForFixture(removed: Int, shared: Int) {
        accessRemovedCount = removed; sharedItemCount = shared; permission = .limited
    }
    #endif

    private func restore() {
        guard mode != .fixture, let originals else { return }
        let directory = originals.root.appendingPathComponent("collection-indexes")
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        func modified(_ url: URL) -> Date { (try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast }
        guard let latest = files.sorted(by: { modified($0) > modified($1) }).first,
              let id = UUID(uuidString: latest.deletingPathExtension().lastPathComponent),
              let saved = try? CameraRollCollection.load(id: id, in: originals, sharesCaptureContext: ClipCaptureSetting.isEnabled()) else { return }
        collection = saved; revision = saved.serverRevision ?? 0
        resumedCollection = true
        for (group, cached) in saved.ideasByGroup where cached.revision == revision {
            board.storiesByGroup[group] = cached.stories
            var draft = FinderDraft(); draft.query = cached.query; draft.topic = cached.researchTopic ?? ""; draft.researchOn = cached.researchTopic != nil
            drafts[group] = draft
        }
    }

    func persist() {
        guard mode != .fixture, let originals else { return }
        collection.name = String(collection.name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(160))
        if collection.name.isEmpty { collection.name = "My outing" }
        do {
            try collection.save(in: originals)
            #if DEBUG
            if mode == .metadataOnly { writeMetadataReport() }
            #endif
        } catch { notice = Notice(text: "Could not save collection progress.", kind: .error) }
    }

    /// Ideas for the event, trimmed so a busy collection stays inside the snapshot's size limit.
    private func cacheIdeas() {
        var kept: [String: SavedCameraRollGroupIdeas] = [:]
        for (group, stories) in board.storiesByGroup where !stories.isEmpty {
            let draft = drafts[group] ?? FinderDraft()
            kept[group] = SavedCameraRollGroupIdeas(revision: revision, stories: Array(stories.prefix(10)), query: draft.query, researchTopic: draft.researchOn ? draft.topic : nil)
        }
        collection.ideasByGroup = kept
        persist()
    }

    // MARK: Taste

    func loadTaste() async {
        guard mode == .live || mode == .fixture else { return }
        taste = try? await api.editorialTaste()
    }
    func tasteChanged(_ profile: EditorialTasteProfile) { taste = profile }
    var tasteAvailable: Bool { taste != nil }

    // MARK: Adding

    /// `group` puts the new items straight into one event (the event menu's "Add photos and videos").
    func beginAdding(toGroup group: String?) { pendingAddGroup = group }

    func add(_ items: [PhotosPickerItem]) {
        guard !items.isEmpty else { return }
        let target = pendingAddGroup
        pendingAddGroup = nil
        let existing = Set(collection.entries.map(\.id))
        var identifiers = collection.entries.map(\.id)
        var added: [String] = []
        for item in items {
            let id = item.itemIdentifier ?? "picker:\(UUID())"
            providers[id] = item
            if !identifiers.contains(id) { identifiers.append(id); added.append(id) }
        }
        do { try collection.select(identifiers) }
        catch { notice = Notice(text: "Choose at most \(CameraRollCollection.maximumCount) items.", kind: .error); return }
        if let target { for id in added where !existing.contains(id) { collection.setGroup(target, for: id) } }
        syncEngine(); persist()
        Task { await refreshMetadata(); await uploadPending() }
    }

    func addSaved(_ assets: [SavedMediaAsset]) {
        let target = pendingAddGroup
        pendingAddGroup = nil
        do {
            let ids = assets.map { "saved:\($0.id)" }
            try collection.select(collection.entries.map(\.id) + ids)
            for asset in assets { collection.bind(asset, to: "saved:\(asset.id)") }
            if let target { for id in ids { collection.setGroup(target, for: id) } }
            syncEngine(); persist()
            Task { await syncAndPoll() }
        } catch { notice = Notice(text: "Choose at most \(CameraRollCollection.maximumCount) items.", kind: .error) }
    }

    // MARK: Metadata

    func refreshMetadata() async {
        guard mode != .fixture, !isRefreshing else { return }
        isRefreshing = true
        let started = ContinuousClock.now
        defer {
            isRefreshing = false
            let elapsed = started.duration(to: .now).components
            metadataElapsedMs = Double(elapsed.seconds) * 1000 + Double(elapsed.attoseconds) / 1e15
            persist()
        }
        let status = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        permission = status
        let canRead = status == .authorized || status == .limited
        if canRead, status == .limited { sharedItemCount = PHAsset.fetchAssets(with: nil).count }
        if let previousPermission, [.authorized, .limited].contains(previousPermission), !canRead { providers.removeAll() }
        previousPermission = status
        var accessible = Set(providers.keys).union(collection.entries.filter { $0.id.hasPrefix("saved:") }.map(\.id))
        let ids = collection.entries.map(\.id)
        for offset in stride(from: 0, to: ids.count, by: CameraRollCollection.batchSize) {
            guard !Task.isCancelled else { return }
            var metadata: [CameraRollMetadata] = []
            if canRead {
                PHAsset.fetchAssets(withLocalIdentifiers: Array(ids[offset..<min(offset + CameraRollCollection.batchSize, ids.count)]), options: nil)
                    .enumerateObjects { asset, _, _ in
                        accessible.insert(asset.localIdentifier)
                        metadata.append(CameraRollMetadata(id: asset.localIdentifier,
                            kind: asset.mediaType == .video ? "video" : "image", duration: asset.duration,
                            width: asset.pixelWidth, height: asset.pixelHeight, modifiedAt: asset.modificationDate,
                            capture: ClipCaptureSetting.isEnabled() ? ClipCaptureReader.read(assetIdentifier: asset.localIdentifier) : nil))
                    }
            }
            collection.record(metadata, sharesCaptureContext: ClipCaptureSetting.isEnabled())
            persist()
            await Task.yield()
        }
        // System-picker selections can be imported without broad Photos access. After relaunch their provider is
        // gone, so retain only already bound uploads or fresh accessible items.
        let before = Set(collection.entries.map(\.id))
        if !canRead { collection.clearCaptureContext() }
        collection.reconcileAccessibleIdentifiers(accessible)
        let removed = before.subtracting(collection.entries.map(\.id))
        if !removed.isEmpty {
            accessRemovedCount += removed.count
            syncEngine()
            if revision > 0 { await saveCollection(reportingFailureAs: "Access changes are saved on this iPhone. Reconnect before finding ideas.") }
        }
        persist()
    }

    func dismissAccessRemoved() { accessRemovedCount = 0 }

    // MARK: Upload

    /// Uploads whatever the server does not hold yet, three at a time. One item failing never stops its siblings;
    /// each ends in a tile state the creator can read.
    func uploadPending(only ids: Set<String>? = nil) async {
        guard mode == .live, !isUploading, let originals else { return }
        guard !cacheOnlyRequired else {
            notice = Notice(text: CameraRollCollectionFailure.unavailable, kind: .error); return
        }
        isUploading = true
        defer { isUploading = false }
        engine.sync(entries: collection.entries)
        let jobs = collection.entries.filter { entry in
            entry.savedAsset == nil && engine.needsUpload(entry.id) && (ids?.contains(entry.id) ?? true)
        }.map { entry -> CameraRollUploadItem in
            let isMovie = providers[entry.id]?.supportedContentTypes.contains { $0.conforms(to: .movie) } == true
            return CameraRollUploadItem(id: entry.id, kind: entry.metadata?.kind ?? (isMovie ? "video" : "image"), duration: entry.metadata?.duration ?? 0)
        }
        if !jobs.isEmpty {
            let owner = originals.owner
            let api = api
            await engine.upload(jobs, loop: .live(api: api, providers: providers), pollingCollectionID: pollableCollectionID) { [weak self] id, asset in
                guard let self, currentOwner == owner else { throw SourceAssetError.invalidBinding }
                collection.bind(asset, to: id)
                persist()
                return collection.entries
            }
        }
        guard !Task.isCancelled else { return }
        syncEngine()
        await syncAndPoll()
    }

    /// Saves the uploaded members and starts polling their analysis.
    private func syncAndPoll() async {
        guard mode == .live else { return }
        guard !collection.uploadedSelection.members.isEmpty else { return }
        guard await saveCollection(reportingFailureAs: nil) else { return }
        engine.startPolling(collectionID: collection.id)
    }

    /// The server must hold exactly the uploaded members before it can analyze them or write ideas about them.
    @discardableResult
    private func saveCollection(reportingFailureAs summary: String?) async -> Bool {
        let selection = collection.uploadedSelection
        if selection == syncedSelection, revision > 0 { return true }
        guard !selection.members.isEmpty else { return true }
        do {
            let saved = try await api.saveCameraRollCollection(collection, revision: revision)
            revision = saved.revision; collection.serverRevision = saved.revision; syncedSelection = selection
            if let stored = saved.events, !collection.serverStoresEvents {
                // First answer from a server that stores events: what the phone kept on its own goes up now.
                collection.serverStoresEvents = true
                collection.adoptServerEvents(stored)
                if collection.events == stored { syncedSelection = collection.uploadedSelection }
                persist()
                return await saveCollection(reportingFailureAs: summary)
            }
            persist()
            return true
        } catch {
            showError(error, summary: summary)
            return false
        }
    }

    // MARK: Retry

    func retry(_ entry: CameraRollCollection.Entry) {
        Task {
            switch await engine.retry(entryID: entry.id, collectionID: collection.id) {
            case .requested, .notRetryable: break
            case .needsReupload: await uploadPending(only: [entry.id])
            case .failed: notice = Notice(text: "Couldn't ask for another try. Check your connection.", kind: .error)
            }
        }
    }

    func retryAllFailed() {
        Task {
            let result = await engine.retryAllFailed(collectionID: collection.id)
            if !result.needsReupload.isEmpty { await uploadPending(only: Set(result.needsReupload)) }
            if result.failed > 0 { notice = Notice(text: "\(result.failed) \(result.failed == 1 ? "item" : "items") couldn't be retried. Check your connection.", kind: .error) }
        }
    }

    // MARK: Ideas

    /// Reads each event's ideas from the server so the surface agrees with it after any membership change.
    func refreshIdeas() async { await refreshIdeas(afterPendingMutations: true) }

    /// `afterPendingMutations` waits for the deletes and restores already tapped, so the server answers with them
    /// applied. A mutation's own failure path passes false: it runs inside that queue and would wait on itself.
    private func refreshIdeas(afterPendingMutations: Bool) async {
        guard revision > 0 || mode == .fixture else { return }
        guard await saveCollection(reportingFailureAs: nil) else { return }
        if afterPendingMutations { await mutationChain?.value }
        func ideaIDs(_ group: String) -> [UUID] { (board.storiesByGroup[group] ?? []).map(\.id) }
        let asked = Dictionary(uniqueKeysWithValues: events.map { ($0.id, ideaIDs($0.id)) })
        for event in events where !board.generating.contains(event.id) {
            guard let stories = try? await api.savedCameraRollStories(id: collection.id, revision: revision, group: event.id) else { continue }
            // A delete, Undo or new idea since the question was asked is newer than the answer, so it keeps its own result.
            guard ideaIDs(event.id) == asked[event.id], !board.generating.contains(event.id) else { continue }
            board.storiesByGroup[event.id] = stories
        }
        for group in board.storiesByGroup.keys where !events.contains(where: { $0.id == group }) { board.storiesByGroup[group] = nil }
        cacheIdeas()
    }

    func anotherIdea(in group: String) { apply(.anotherIdea(group: group)) }

    func deleteIdea(_ story: CameraRollStory, in group: String) { apply(.deleteIdea(storyID: story.id, group: group)) }

    func undo() {
        switch board.undo {
        case let .idea(_, story, _)?: apply(.undoDeleteIdea(storyID: story.id))
        case let .event(group, _, _)?: undoDeleteEvent(group)
        case nil: break
        }
    }

    func replaceStory(_ story: CameraRollStory) {
        for group in board.storiesByGroup.keys {
            if let index = board.storiesByGroup[group]?.firstIndex(where: { $0.id == story.id }) {
                board.storiesByGroup[group]?[index] = story
            }
        }
        cacheIdeas()
    }

    private func apply(_ action: CameraRollIdeaAction) {
        let effects = board.reduce(action)
        if board.undo != nil { scheduleUndoExpiry() }
        cacheIdeas()
        for effect in effects { run(effect) }
    }

    private func scheduleUndoExpiry() {
        undoExpiry?.cancel()
        undoExpiry = Task { [weak self] in
            do { try await Task.sleep(for: Self.undoWindow) } catch { return }
            self?.board.reduce(.undoExpired)
        }
    }

    /// Mutations run one after another so a delete and its Undo reach the server in the order they were tapped.
    /// Generating takes up to a minute, so it runs beside them rather than in front of them.
    private func run(_ effect: CameraRollIdeaEffect) {
        switch effect {
        case let .generate(group, excluding):
            Task { await generate(group: group, excluding: excluding) }
        case let .deleteStory(id):
            enqueue { [self] in
                do { _ = try await api.deleteCameraRollStory(collectionID: collection.id, storyID: id) }
                catch { await refreshIdeas(afterPendingMutations: false); showError(error, summary: "Couldn't delete that idea.") }
            }
        case let .restoreStory(id):
            enqueue { [self] in
                do { try await api.restoreCameraRollStory(collectionID: collection.id, storyID: id) }
                catch {
                    board.reduce(.restoreFailed(storyIDs: [id])); cacheIdeas()
                    showError(error, summary: "Couldn't bring that idea back.")
                }
            }
        case let .deleteGroup(group):
            enqueue { [self] in await deleteGroupOnServer(group) }
        case let .restoreGroup(group, members, storyIDs):
            enqueue { [self] in await restoreGroupOnServer(group, members: members, storyIDs: storyIDs) }
        }
    }

    private func enqueue(_ work: @escaping @MainActor () async -> Void) {
        let previous = mutationChain
        mutationChain = Task { await previous?.value; await work() }
    }

    private func generate(group: String, excluding: [UUID]) async {
        do {
            guard await saveCollection(reportingFailureAs: nil) else { board.reduce(.generationFailed(group: group)); return }
            // Indexing is a server pass that turns analysis into moments; its buckets no longer matter because
            // every tile already shows its own state.
            var offset = 0
            repeat {
                let pass = try await api.indexCameraRollCollection(id: collection.id, revision: revision, offset: offset)
                guard let next = pass.nextOffset else { break }
                offset = next
            } while offset < CameraRollCollection.maximumCount
            let draft = draft(for: group)
            let query = draft.query.trimmingCharacters(in: .whitespacesAndNewlines)
            let topic = draft.researchOn ? (draft.topic.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? suggestedTopic : draft.topic) : nil
            let result = try await api.generateCameraRollStories(id: collection.id, request: CameraRollStoryRequest(
                revision: revision, query: query.isEmpty ? Self.defaultQuery : query, cacheOnly: cacheOnlyRequired,
                researchTopic: topic, allowPartialAnalysis: true, group: group, excludeStoryIDs: excluding))
            suggestedTopic = result.suggestedResearchTopic ?? suggestedTopic
            if let found = result.coverage { coverage[group] = found }
            board.reduce(.ideasArrived(group: group, stories: result.stories))
            if result.stories.isEmpty {
                notice = Notice(text: excluding.isEmpty ? "No idea found in this event yet. Try adding a direction." : "No other idea found in this event.", kind: .info)
            }
            cacheIdeas()
        } catch {
            board.reduce(.generationFailed(group: group))
            showError(error, summary: nil)
        }
    }

    // MARK: Events

    func deleteEvent(_ group: String) {
        let members = collection.members(inGroup: group)
        removedEntries[group] = collection.remove(inGroup: group)
        removedInfo[group] = collection.removeEvent(group)
        if selecting?.group == group { selecting = nil }
        if finderGroup == group { finderGroup = nil }
        expanded.remove(group)
        syncEngine()
        apply(.deleteEvent(group: group, members: members))
    }

    private func undoDeleteEvent(_ group: String) {
        collection.restore(removedEntries[group] ?? [])
        removedEntries[group] = nil
        if let info = removedInfo.removeValue(forKey: group) { collection.restoreEvent(info) }
        syncEngine()
        apply(.undoDeleteEvent(group: group))
    }

    private func deleteGroupOnServer(_ group: String) async {
        let stillMembers = !(removedEntries[group] ?? []).compactMap(\.savedAsset).isEmpty
        guard revision > 0, stillMembers else { await saveCollection(reportingFailureAs: nil); persist(); return }
        do {
            let result = try await api.deleteCameraRollGroup(collectionID: collection.id, group: group, expectedRevision: revision)
            revision = result.revision; collection.serverRevision = result.revision
            // The group delete dropped the members on the server; the event's title and about leave with a save.
            let current = collection.uploadedSelection
            syncedSelection = CameraRollCollectionSelection(name: current.name, members: current.members, events: syncedSelection?.events)
            await saveCollection(reportingFailureAs: nil)
            persist()
        } catch let error as APIError where error == .requestFailed(status: 404) {
            // The server never held this event (nothing in it had uploaded), so there is nothing to delete there.
            persist()
        } catch {
            // The event stays on the server, so it comes back here as well.
            collection.restore(removedEntries[group] ?? [])
            removedEntries[group] = nil
            if let info = removedInfo.removeValue(forKey: group) { collection.restoreEvent(info) }
            syncEngine()
            board.reduce(.undoDeleteEvent(group: group))
            showError(error, summary: "Couldn't delete that event.")
        }
    }

    private func restoreGroupOnServer(_ group: String, members: [CameraRollCollectionMember], storyIDs: [UUID]) async {
        guard revision > 0, !members.isEmpty else { await saveCollection(reportingFailureAs: nil); persist(); return }
        do {
            let saved = try await api.saveCameraRollCollection(collection, revision: revision)
            revision = saved.revision; collection.serverRevision = saved.revision; syncedSelection = collection.uploadedSelection
            var failed: [UUID] = []
            for id in storyIDs {
                do { try await api.restoreCameraRollStory(collectionID: collection.id, storyID: id) }
                catch { failed.append(id) }
            }
            if !failed.isEmpty { board.reduce(.restoreFailed(storyIDs: failed)); showError(nil, summary: "Some ideas couldn't be brought back.") }
            persist()
            if mode == .live { engine.startPolling(collectionID: collection.id) }
        } catch { showError(error, summary: "Couldn't bring that event back.") }
    }

    // MARK: Editing events

    func beginSelecting(in group: String, renaming: Bool = false) { selecting = Selecting(group: group, renaming: renaming) }

    func toggleSelected(_ id: String) {
        guard var current = selecting else { return }
        if current.selected.contains(id) { current.selected.remove(id) } else { current.selected.insert(id) }
        selecting = current
    }

    func cancelSelecting() { selecting = nil }

    func rename(group: String, to title: String) {
        collection.rename(group: group, to: title)
        persist(); syncEvents()
    }

    func setAbout(group: String, to about: String) {
        collection.setAbout(group: group, to: about)
        persist(); syncEvents()
    }

    /// A new event with a title and about and nothing in it yet; the creator adds footage from its page or the
    /// picker that follows. Returns its group key.
    @discardableResult func createEvent(title: String, about: String) -> String {
        let group = newEventKey()
        collection.addEvent(group: group, title: title, about: about)
        persist(); syncEvents()
        return group
    }

    /// Titles and about reach the server on their own save; a failure says so and keeps them on the phone.
    private func syncEvents() {
        guard mode != .metadataOnly, revision > 0, collection.serverStoresEvents else { return }
        enqueue { [self] in await saveCollection(reportingFailureAs: "Event details are saved on this iPhone.") }
    }

    /// Where selected items can go: the other events, then a new one.
    func moveTargets(from group: String) -> [CameraRollEvent] { events.filter { $0.id != group } }

    func newEventKey() -> String {
        var number = 1
        let taken = Set(events.map(\.id))
        while taken.contains(number == 1 ? "New event" : "New event \(number)") { number += 1 }
        return number == 1 ? "New event" : "New event \(number)"
    }

    func moveSelected(to group: String) {
        guard let current = selecting, !current.selected.isEmpty else { return }
        for id in current.selected { collection.setGroup(group, for: id) }
        selecting?.selected = []
        persist()
    }

    /// Ends select mode. The server learns the new events here, and ideas it can no longer stand behind are cleared.
    func finishSelecting() {
        selecting = nil
        guard mode != .metadataOnly else { return }
        Task {
            let hadIdeas = Set(board.storiesByGroup.filter { !$0.value.isEmpty }.keys)
            await syncAndPoll()
            await refreshIdeas()
            if hadIdeas.contains(where: { board.storiesByGroup[$0]?.isEmpty ?? true }) {
                notice = Notice(text: "Ideas for events you changed were cleared, so you can find new ones.", kind: .info)
            }
        }
    }

    // MARK: Conflicts

    func loadLatestCollection() async {
        do {
            let latest = try await api.cameraRollCollection(id: collection.id)
            guard latest.id == collection.id, latest.name != nil, latest.members != nil else { throw APIError.invalidResponse }
            latestCollection = latest; showsLatestReview = true
        } catch { showError(error, summary: nil) }
    }

    func adoptLatestCollection() async {
        guard let latest = latestCollection, let name = latest.name, let members = latest.members, let originals else { return }
        do {
            var rebuilt = try CameraRollCollection(id: latest.id, name: name, identifiers: members.map { "saved:\($0.asset_id)" })
            for member in members {
                try Task.checkCancellation()
                let asset = try await api.savedMediaAsset(id: member.asset_id)
                guard currentOwner == originals.owner else { throw SourceAssetError.invalidBinding }
                let id = "saved:\(asset.id)"
                rebuilt.bind(asset, to: id)
                if !member.group.isEmpty { rebuilt.setGroup(member.group, for: id) }
            }
            rebuilt.serverRevision = latest.revision
            if let events = latest.events { rebuilt.serverStoresEvents = true; rebuilt.adoptServerEvents(events) }
            try collection.localCopy().save(in: originals)
            try rebuilt.save(in: originals)
            collection = rebuilt; revision = latest.revision; syncedSelection = rebuilt.uploadedSelection
            providers = [:]; board = CameraRollIdeaBoard(); collectionChanged = false
            syncEngine()
            engine.startPolling(collectionID: rebuilt.id)
            notice = Notice(text: "Opened the latest collection. Your local changes are saved separately.", kind: .info)
            await refreshIdeas()
        } catch { showError(error, summary: nil) }
    }

    private func showError(_ error: Error?, summary: String?) {
        if let error {
            collectionChanged = collectionChanged || CameraRollCollectionFailure.isChanged(error)
            let reason = CameraRollCollectionFailure.message(error)
            notice = Notice(text: [summary, reason].compactMap { $0 }.joined(separator: " "), kind: .error)
        } else if let summary { notice = Notice(text: summary, kind: .error) }
    }

    #if DEBUG
    private func writeMetadataReport() {
        var usage = rusage(); getrusage(RUSAGE_SELF, &usage)
        let report: [String: Any] = [
            "schema_version": 1, "captured_at": ISO8601DateFormatter().string(from: Date()),
            "os_version": ProcessInfo.processInfo.operatingSystemVersionString,
            "permission": PHPhotoLibrary.authorizationStatus(for: .readWrite).rawValue,
            "selected_count": collection.entries.count, "metadata_count": collection.completedCount,
            "photo_count": collection.entries.filter { $0.metadata?.kind == "image" }.count,
            "video_count": collection.entries.filter { $0.metadata?.kind == "video" }.count,
            "corrected_group_count": collection.entries.filter { $0.correctedGroup != nil }.count,
            "group_count": events.count,
            "resumed_collection": resumedCollection, "metadata_elapsed_ms": metadataElapsedMs,
            "process_peak_resident_bytes": usage.ru_maxrss, "media_bytes_read": 0,
            "media_bytes_uploaded": 0, "provider_calls": 0, "analysed_candidate_count": 0,
            "capture_context_enabled": ClipCaptureSetting.isEnabled(), "original_availability": "not_requested"
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys]) else { return }
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
        try? data.write(to: documents.appendingPathComponent("camera-roll-collection-check.json"), options: .atomic)
    }
    #endif
}
