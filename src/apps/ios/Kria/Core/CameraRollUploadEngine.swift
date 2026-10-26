import Foundation
import Photos
import PhotosUI
import SwiftUI
import Observation

// KRI-944: the per-item engine behind the camera roll. A bounded upload loop in which one item's failure never
// stops its siblings, a 3 s poll of the server's per-item states, and a per-id store so one progress tick redraws
// one tile. Pure logic lives in `CameraRollItemState.swift`; this file is the concurrency around it.

/// Checks that need no network: refuse what can never be analyzed before spending an iCloud download or an upload.
enum CameraRollPreflight {
    static let maximumImageBytes: Int64 = 25 * 1024 * 1024
    static let maximumVideoSeconds: Double = 30 * 60
    static let imageExtensions: Set<String> = ["jpg", "jpeg", "png", "webp", "heic", "heif"]

    /// Known from Photos metadata, so a 40-minute video is refused before it is downloaded from iCloud.
    static func checkMetadata(kind: String, duration: Double) -> CameraRollItemReason? {
        kind == "video" && duration > maximumVideoSeconds ? .tooLong : nil
    }

    /// Known once the file exists. Videos are proxied on the phone, so only images have a type and size gate.
    static func checkFile(kind: String, pathExtension: String, byteCount: Int64) -> CameraRollItemReason? {
        guard kind == "image" else { return nil }
        if !imageExtensions.contains(pathExtension.lowercased()) { return .unsupportedType }
        if byteCount > maximumImageBytes { return .tooLarge }
        return nil
    }

    static func checkFile(at url: URL, kind: String) -> CameraRollItemReason? {
        let size = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
        return checkFile(kind: kind, pathExtension: url.pathExtension, byteCount: size)
    }
}

enum CameraRollUploadFailure {
    /// Maps whatever an export or upload threw onto the one event the reducer understands.
    static func event(for error: Error) -> CameraRollItemEvent {
        if let export = error as? LibraryAssetExportError {
            switch export {
            case .assetNotFound: return .unreachable(.accessRevoked)
            case .noResource: return .rejected(.unsupportedType)
            case .byteLimitExceeded: return .rejected(.tooLarge)
            case .timedOut: return .failedLocally(.icloudUnavailable)
            case .writeFailed: return .failedLocally(.uploadFailed)
            }
        }
        if let photos = error as? PHPhotosError, photos.code == .networkAccessRequired || photos.code == .networkError {
            return .failedLocally(.icloudUnavailable)
        }
        if case .requestFailed(status: 413, _)? = error as? APIError { return .rejected(.tooLarge) }
        return .failedLocally(.uploadFailed)
    }
}

/// Runs `body` over `items` with at most `limit` in flight, starting no new item once the task is cancelled.
func boundedForEach<T: Sendable>(_ items: [T], limit: Int, _ body: @escaping @Sendable (T) async -> Void) async {
    await withTaskGroup(of: Void.self) { group in
        var iterator = items.makeIterator()
        for _ in 0..<max(1, limit) {
            guard !Task.isCancelled, let item = iterator.next() else { break }
            group.addTask { await body(item) }
        }
        while await group.next() != nil {
            guard !Task.isCancelled, let item = iterator.next() else { continue }
            group.addTask { await body(item) }
        }
    }
}

struct CameraRollUploadItem: Sendable, Equatable {
    let id: String
    let kind: String
    let duration: Double
}

struct CameraRollUploadLoop: Sendable {
    static let concurrency = 3
    static let exportTimeout: Duration = .seconds(60)

    typealias Report = @Sendable (String, CameraRollItemEvent) async -> Void
    typealias Uploaded = @Sendable (String, SavedMediaAsset) async throws -> Void

    var concurrency = Self.concurrency
    var exportTimeout = Self.exportTimeout
    var export: @Sendable (CameraRollUploadItem) async throws -> URL
    var upload: @Sendable (URL, String, @escaping @Sendable (Double) -> Void) async throws -> SavedMediaAsset

    /// Every item ends in a reported event; nothing thrown by one item reaches another.
    func run(_ items: [CameraRollUploadItem], uploaded: @escaping Uploaded, report: @escaping Report) async {
        let loop = self
        await boundedForEach(items, limit: concurrency) { item in
            await loop.process(item, uploaded: uploaded, report: report)
        }
    }

    private func process(_ item: CameraRollUploadItem, uploaded: Uploaded, report: @escaping Report) async {
        let id = item.id
        if let reason = CameraRollPreflight.checkMetadata(kind: item.kind, duration: item.duration) {
            await report(id, .rejected(reason)); return
        }
        await report(id, .exportStarted)
        do {
            let file = try await exportWithTimeout(item)
            defer { try? FileManager.default.removeItem(at: file) }
            if let reason = CameraRollPreflight.checkFile(at: file, kind: item.kind) {
                await report(id, .rejected(reason)); return
            }
            await report(id, .uploadProgress(0))
            let asset = try await upload(file, item.kind) { fraction in
                Task { await report(id, .uploadProgress(fraction)) }
            }
            try await uploaded(id, asset)
            await report(id, .uploaded)
        } catch is CancellationError {
            await report(id, .interrupted)
        } catch {
            await report(id, Task.isCancelled ? .interrupted : CameraRollUploadFailure.event(for: error))
        }
    }

    private func exportWithTimeout(_ item: CameraRollUploadItem) async throws -> URL {
        let export = export, timeout = exportTimeout
        return try await withThrowingTaskGroup(of: URL.self) { group in
            group.addTask { try await export(item) }
            group.addTask { try await Task.sleep(for: timeout); throw LibraryAssetExportError.timedOut }
            defer { group.cancelAll() }
            guard let first = try await group.next() else { throw LibraryAssetExportError.writeFailed }
            return first
        }
    }
}

extension CameraRollUploadLoop {
    /// Photos export (or the picker's own provider file) followed by the existing proxy-and-upload import.
    /// Images are byte-capped inside the exporter, so an oversized photo stops downloading early.
    static func live(api: any KriaAPIClient, providers: [String: PhotosPickerItem]) -> Self {
        CameraRollUploadLoop(
            export: { item in
                let exporter = PhotosLibraryAssetExporter(maximumBytes: item.kind == "image" ? CameraRollPreflight.maximumImageBytes : nil)
                let picked = providers[item.id] ?? PhotosPickerItem(itemIdentifier: item.id)
                return try await PhotoItemFileLoader(exporter: exporter).load(item: picked, identifier: item.id)
            },
            upload: { file, kind, progress in
                try await api.importCollectionMedia(file: file, kind: kind, progress: progress)
            })
    }
}

// MARK: - Per-id store

/// One tile's state. Only views that read this tile's `state` redraw when it changes.
@MainActor @Observable final class CameraRollTile {
    let id: String
    fileprivate(set) var state: CameraRollItemState = .picking
    init(id: String) { self.id = id }
}

/// `UploadProgressStore` keeps one dictionary, so a tick for any id invalidates every reader of `values`. Here each
/// id owns its own observable tile, and the aggregate counts change only when an item changes phase, never on a
/// progress tick.
@MainActor @Observable final class CameraRollItemStore {
    @ObservationIgnored private var tiles: [String: CameraRollTile] = [:]
    @ObservationIgnored private var order: [String] = []
    /// Bumped when tiles are added or removed, so a view that asked for a not-yet-registered id redraws once.
    private(set) var registryVersion = 0
    private(set) var counts: [CameraRollItemPhase: Int] = [:]

    func count(_ phase: CameraRollItemPhase) -> Int { counts[phase] ?? 0 }
    var awaitsServerCount: Int { count(.queued) + count(.analyzing) }

    /// Never mutates, so it is safe to call from a view body. An unknown id gets an inert `.picking` tile.
    func tile(_ id: String) -> CameraRollTile {
        _ = registryVersion
        return tiles[id] ?? CameraRollTile(id: id)
    }

    func state(of id: String) -> CameraRollItemState? { tiles[id]?.state }

    var retryableIDs: [String] { order.filter { tiles[$0]?.state.isRetryable == true } }

    /// Makes the store match `ids`: new ids start at `.picking`, dropped ids disappear.
    func register(_ ids: [String]) {
        let wanted = Set(ids)
        var changed = false
        for id in order where !wanted.contains(id) {
            if let old = tiles.removeValue(forKey: id) { counts[old.state.phase, default: 0] -= 1 }
            changed = true
        }
        order = ids
        for id in ids where tiles[id] == nil {
            tiles[id] = CameraRollTile(id: id)
            counts[.picking, default: 0] += 1
            changed = true
        }
        if changed { registryVersion += 1 }
    }

    @discardableResult
    func send(_ event: CameraRollItemEvent, to id: String) -> CameraRollItemState? {
        guard let tile = tiles[id] else { return nil }
        let before = tile.state
        let after = CameraRollItemReducer.reduce(before, event)
        guard after != before else { return after }
        if after.phase != before.phase {
            counts[before.phase, default: 0] -= 1
            counts[after.phase, default: 0] += 1
        }
        tile.state = after
        return after
    }
}

// MARK: - Engine

@MainActor final class CameraRollItemEngine {
    enum RetryOutcome: Equatable { case requested, needsReupload, notRetryable, failed }
    struct RetryAllResult: Equatable { var requested = 0; var failed = 0; var needsReupload: [String] = [] }

    static let pollInterval: Duration = .seconds(3)
    private static let maximumPollFailures = 5

    let store: CameraRollItemStore
    private let client: any CameraRollItemsClient
    private let pollInterval: Duration
    private let sleep: @Sendable (Duration) async throws -> Void
    private var assetForEntry: [String: UUID] = [:]
    private var entryForAsset: [UUID: String] = [:]
    private var pollTask: Task<Void, Never>?
    private var pollRun = UUID()
    /// Bumped by a retry so a snapshot requested before it can never put the item back to its old state.
    private var pollGeneration = 0
    private(set) var pollError: Error?

    init(client: any CameraRollItemsClient, store: CameraRollItemStore = CameraRollItemStore(),
         pollInterval: Duration = CameraRollItemEngine.pollInterval,
         sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.client = client; self.store = store; self.pollInterval = pollInterval; self.sleep = sleep
    }

    /// Aligns tiles with the collection. An entry already bound to an uploaded asset starts at `.queued`; the next
    /// poll replaces that with the server's real state.
    func sync(entries: [CameraRollCollection.Entry]) {
        store.register(entries.map(\.id))
        assetForEntry = [:]; entryForAsset = [:]
        for entry in entries {
            guard let asset = entry.savedAsset else { continue }
            assetForEntry[entry.id] = asset.id; entryForAsset[asset.id] = entry.id
            if store.state(of: entry.id) == .picking { store.send(.uploaded, to: entry.id) }
        }
    }

    /// True for items the upload loop should still try: a rejected or revoked item would only be exported again for
    /// nothing, because the reducer ignores every event for it.
    func needsUpload(_ id: String) -> Bool {
        guard let state = store.state(of: id) else { return true }
        switch state {
        case .excluded, .failed: return false
        default: return true
        }
    }

    /// Runs the bounded loop. `bind` runs on the main actor as each upload lands, so progress survives a crash. It
    /// returns the collection's entries afterwards: a duplicate asset removes an entry, so the id maps and tiles are
    /// rebuilt from that truth instead of patched. `pollingCollectionID` is set only when the server already has the
    /// collection, so uploads that finish early show server state without waiting for the whole loop.
    func upload(_ items: [CameraRollUploadItem], loop: CameraRollUploadLoop, pollingCollectionID: UUID? = nil,
                bind: @escaping @MainActor @Sendable (String, SavedMediaAsset) throws -> [CameraRollCollection.Entry]) async {
        let store = store
        await loop.run(items, uploaded: { [weak self] id, asset in
            try await MainActor.run {
                let entries = try bind(id, asset)
                guard let self else { return }
                self.sync(entries: entries)
                // Queued now, not when the loop reports it, so the poll that starts below sees a waiting item.
                self.store.send(.uploaded, to: id)
                if let pollingCollectionID { self.startPolling(collectionID: pollingCollectionID) }
            }
        }, report: { id, event in
            await MainActor.run { _ = store.send(event, to: id) }
        })
    }

    // MARK: Polling

    /// Polls while any item waits on the server. Local-only items are driven by the upload loop, and a settled item
    /// needs the creator, so neither keeps this running. Stops after repeated failures or snapshots that never
    /// mention a waiting item.
    func startPolling(collectionID: UUID) {
        guard pollTask == nil else { return }
        let run = UUID()
        pollRun = run
        pollTask = Task { [weak self] in
            await self?.pollLoop(collectionID)
            if let self, self.pollRun == run { self.pollTask = nil }
        }
    }

    func stopPolling() {
        pollTask?.cancel(); pollTask = nil
    }

    private func pollLoop(_ collectionID: UUID) async {
        var failures = 0, misses = 0
        while !Task.isCancelled, store.awaitsServerCount > 0 {
            let token = pollGeneration
            let waiting = Set(assetForEntry.filter { store.state(of: $0.key)?.awaitsServer == true }.map(\.value))
            do {
                let snapshot = try await client.cameraRollItems(id: collectionID)
                failures = 0; pollError = nil
                if token != pollGeneration { continue }
                apply(snapshot)
                misses = !waiting.isEmpty && !snapshot.items.contains(where: { waiting.contains($0.assetID) }) ? misses + 1 : 0
                if misses >= Self.maximumPollFailures { return }
            } catch is CancellationError {
                return
            } catch {
                failures += 1; pollError = error
                if failures >= Self.maximumPollFailures { return }
            }
            do { try await sleep(pollInterval) } catch { return }
        }
    }

    func apply(_ snapshot: CameraRollItemsSnapshot) {
        for item in snapshot.items {
            guard let entry = entryForAsset[item.assetID] else { continue }
            store.send(.server(item.itemState), to: entry)
        }
    }

    // MARK: Retry

    /// An item that never reached the server has no asset to retry; the caller re-runs `upload` for it instead.
    func retry(entryID: String, collectionID: UUID) async -> RetryOutcome {
        guard let previous = store.state(of: entryID), previous.isRetryable else { return .notRetryable }
        guard let assetID = assetForEntry[entryID] else { return .needsReupload }
        pollGeneration += 1
        store.send(.retryRequested, to: entryID)
        do {
            _ = try await client.retrySavedMedia(id: assetID)
            startPolling(collectionID: collectionID)
            return .requested
        } catch {
            store.send(.server(previous), to: entryID)
            return .failed
        }
    }

    func retryAllFailed(collectionID: UUID) async -> RetryAllResult {
        let tally = RetryTally()
        await boundedForEach(store.retryableIDs, limit: CameraRollUploadLoop.concurrency) { [weak self] id in
            guard let self else { return }
            switch await self.retry(entryID: id, collectionID: collectionID) {
            case .requested: await tally.record(requested: true)
            case .failed: await tally.record(requested: false)
            case .needsReupload: await tally.reupload(id)
            case .notRetryable: break
            }
        }
        return await tally.result
    }

    private actor RetryTally {
        var result = RetryAllResult()
        func record(requested: Bool) { if requested { result.requested += 1 } else { result.failed += 1 } }
        func reupload(_ id: String) { result.needsReupload.append(id) }
    }
}
