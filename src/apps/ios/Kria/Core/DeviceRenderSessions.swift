import Foundation
import Observation
import CryptoKit
import KriaMediaEngine

struct DeviceRenderKey: Hashable, Sendable {
    let projectID: UUID
    let jobID: UUID
    let variantID: String
}

struct DeviceRelinkTarget: Identifiable, Sendable {
    var id: String { asset.id }
    let identity: DeviceRenderIdentity
    let asset: RenderAssetReference
    let title: String
}

struct DeviceRenderPresentation: Equatable, Sendable {
    var phase: DeviceRenderPhase
    var localFile: URL?
    var message: String?
    /// Server-classified reason for a `needsAttention` phase (e.g. "thermal",
    /// "insufficient_storage"); nil when the server hasn't reported one yet
    /// or the failure is still purely local.
    var reasonCode: String?
    /// The server has invalidated this request and requires a fresh identity
    /// before another device attempt. This is independent of `phase`: a
    /// local MP4 can remain useful (`.localReady`) while the server still
    /// needs `/device-render/retry`.
    var requiresServerRetry: Bool = false
    /// Generation published by the server for the authoritative device attempt.
    /// This is populated only for a server `published` response.
    var publishedGeneration: String?
}

/// Short label for the editor's top-of-preview device-render affordance
/// (`NativeEditorView`'s "native-editor-device-render" button). Kept distinct
/// from `DeviceRenderStatusCard`'s longer per-phase title/detail copy, which
/// explains the state inside the opened sheet rather than labeling the
/// button that opens it. A finished, synced render must stop reading
/// "Rendering on iPhone" — see the KRI job 9c7a1f4f report where the phone's
/// own receipt was already `phase: synced` but the button never updated.
enum DeviceRenderButtonTitle {
    static func `for`(phase: DeviceRenderPhase) -> String {
        switch phase {
        case .preparing, .rendering: "Rendering on iPhone…"
        case .syncing: "Syncing…"
        case .localReady, .synced: "Rendered on iPhone"
        case .needsAttention: "Needs attention"
        case .cancelled: "Render stopped"
        case .superseded: "Newer edit available"
        }
    }
}

/// App-owned so navigating between chat, projects, and the editor does not
/// interrupt an export. Each server revision still owns its own coordinator.
@Observable @MainActor final class DeviceRenderSessions {
    typealias Fetch = @Sendable (UUID, String) async throws -> DeviceRenderStatusResponse
    typealias Factory = @MainActor (DeviceRenderKey, DeviceRenderRequest) throws -> DeviceRenderCoordinator
    typealias RetryFailure = @Sendable (UUID, DeviceRenderIdentity) async throws -> DeviceRenderRetryAck
    private(set) var presentations: [DeviceRenderKey: DeviceRenderPresentation] = [:]
    @ObservationIgnored private let fetch: Fetch
    @ObservationIgnored private let factory: Factory
    @ObservationIgnored private let retryFailure: RetryFailure
    @ObservationIgnored private let projectDirectory: @MainActor (UUID) -> ProjectDirectory
    @ObservationIgnored private var entries: [DeviceRenderKey: DeviceRenderCoordinator] = [:]
    @ObservationIgnored private var requests: [DeviceRenderKey: DeviceRenderRequest] = [:]
    @ObservationIgnored private var observations: [DeviceRenderKey: Task<Void, Never>] = [:]
    @ObservationIgnored private var tickets: [DeviceRenderKey: UUID] = [:]

    convenience init(api: any KriaAPIClient) {
        self.init(fetch: { try await api.deviceRender(jobID: $0, variantID: $1) }, factory: { key, request in
            let project = BackgroundUploadCoordinator.projectDirectory(key.projectID)
            let variant = SHA256.hash(data: Data(key.variantID.utf8)).map { String(format: "%02x", $0) }.joined()
            let directory = project.root.appending(path: "device-renders/\(key.jobID.uuidString)/\(variant)", directoryHint: .isDirectory)
            let library = RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
            return try DeviceRenderCoordinator(
                directory: directory,
                exporter: AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory)),
                sources: AuthorizedDeviceSourceResolver(api: api, request: request, originals: SourceAssetStore(project: project), library: library),
                publisher: DeviceExportPublisher(api: api),
                failureReporter: APIDeviceRenderFailureReporter(api: api)
            )
        }, retryFailure: { jobID, identity in try await api.retryDeviceRenderFailure(jobID: jobID, identity: identity) })
    }

    init(fetch: @escaping Fetch, factory: @escaping Factory,
         retryFailure: @escaping RetryFailure = { _, _ in throw APIError.unsupported },
         projectDirectory: @escaping @MainActor (UUID) -> ProjectDirectory = { BackgroundUploadCoordinator.projectDirectory($0) }) {
        self.fetch = fetch; self.factory = factory; self.retryFailure = retryFailure; self.projectDirectory = projectDirectory
    }

    func reconcile(_ key: DeviceRenderKey, capabilities: PhoneRenderingCapabilities, retry: Bool = false) async {
        guard tickets[key] == nil else { return }
        let ticket = UUID()
        tickets[key] = ticket
        defer { if tickets[key] == ticket { tickets[key] = nil } }
        do {
            let status = try await fetch(key.jobID, key.variantID)
            guard tickets[key] == ticket, !Task.isCancelled else { return }
            guard status.request.identity.jobID == key.jobID,
                  status.request.identity.variantID == key.variantID else { throw APIError.invalidResponse }
            let request = status.request
            if requests[key] != request {
                observations.removeValue(forKey: key)?.cancel()
                if let previous = entries[key] { try await previous.cancel() }
                guard tickets[key] == ticket else { return }
                entries[key] = try factory(key, request)
                requests[key] = request
            }
            guard let coordinator = entries[key] else { return }
            let saved = await coordinator.snapshot()
            guard tickets[key] == ticket else { return }
            if status.phase == "published" {
                observations.removeValue(forKey: key)?.cancel()
                presentations[key] = DeviceRenderPresentation(phase: .synced, localFile: saved?.request == request ? saved?.outputURL : nil, publishedGeneration: status.publishedGeneration)
                return
            }
            let decision = Self.decision(request.recipe, capabilities: capabilities)
            if status.phase == "needs_attention" || decision.route != .local {
                // Preserve a finished export and its receipt when rollout is paused.
                observations.removeValue(forKey: key)?.cancel()
                presentations[key] = DeviceRenderPresentation(
                    phase: saved?.outputURL == nil ? .needsAttention : .localReady,
                    localFile: saved?.request == request ? saved?.outputURL : nil,
                    message: status.reason ?? decision.reason,
                    reasonCode: status.reasonCode,
                    requiresServerRetry: status.phase == "needs_attention"
                )
                return
            }
            guard ["awaiting_device", "syncing"].contains(status.phase) else { throw APIError.invalidResponse }
            if saved?.request != request || retry {
                try await coordinator.start(request, decision: decision)
            } else if observations[key] == nil {
                try await coordinator.recover()
            }
            guard tickets[key] == ticket else { return }
            await observe(key, coordinator: coordinator)
        } catch is CancellationError {
            return
        } catch {
            guard tickets[key] == ticket else { return }
            var presentation = presentations[key] ?? DeviceRenderPresentation(phase: .needsAttention)
            presentation.message = RequestFailureCause(error) == .connection
                ? "Kria couldn’t prepare this edit on your iPhone. Your project is saved. Try again when you’re connected."
                : "Kria couldn’t prepare this edit on your iPhone. Your project is saved. \(error.localizedDescription)"
            presentations[key] = presentation
        }
    }

    /// The request identity last accepted by reconciliation. The editor uses
    /// it to fence a ready response against another device attempt.
    func request(for key: DeviceRenderKey) -> DeviceRenderRequest? { requests[key] }

    /// Calls the server's `/device-render/retry` endpoint for a `needsAttention`
    /// identity, which mints a fresh identity (incremented recipe revision) and
    /// moves the server back to `awaiting_device`. Drops the stale local
    /// coordinator/receipt so the next `reconcile()` starts clean against it.
    @discardableResult
    func retryNeedsAttention(_ key: DeviceRenderKey) async -> Bool {
        guard let request = requests[key] else { return false }
        do {
            _ = try await retryFailure(key.jobID, request.identity)
            // A reconcile/save may have installed a newer request while the
            // retry was in flight. Its coordinator and receipt now own this
            // key; a stale retry completion must not tear them down.
            guard requests[key] == request else { return false }
            observations.removeValue(forKey: key)?.cancel()
            let previous = entries.removeValue(forKey: key)
            requests.removeValue(forKey: key)
            // Invalidate a fetch that was already in flight before the retry
            // completed. Otherwise its stale status can reinstall the old
            // request after this method clears the maps.
            tickets[key] = nil
            presentations[key] = DeviceRenderPresentation(phase: .preparing)
            if let previous { try? await previous.cancel() }
            return true
        } catch {
            guard requests[key] == request else { return false }
            var presentation = presentations[key] ?? DeviceRenderPresentation(phase: .needsAttention)
            presentation.message = RequestFailureCause(error) == .connection
                ? "Kria couldn’t retry this edit. Check your connection and try again."
                : "Kria couldn’t retry this edit. \(error.localizedDescription)"
            presentations[key] = presentation
            return false
        }
    }

    func sourcesNeedingRelink(_ key: DeviceRenderKey) async throws -> [DeviceRelinkTarget] {
        guard let request = requests[key], let manifest = request.recipe.assetManifest else { throw APIError.invalidResponse }
        let store = SourceAssetStore(project: projectDirectory(key.projectID))
        return await Task.detached {
            var missing: [DeviceRelinkTarget] = [], seen = Set<String>()
            for asset in manifest.assets {
                guard case .original(let mediaID) = asset.source, seen.insert(mediaID).inserted else { continue }
                let binding = try? store.bindings().first { $0.mediaID == mediaID }
                let matches = binding?.original.fingerprint?.hex == asset.fingerprint.sha256
                    && binding?.original.fingerprint?.byteCount == asset.fingerprint.byteCount
                if !matches || (try? store.resolve(mediaIDs: [mediaID])) == nil {
                    missing.append(DeviceRelinkTarget(identity: request.identity, asset: asset, title: "Original \(seen.count)"))
                }
            }
            return missing
        }.value
    }

    func relink(_ target: DeviceRelinkTarget, for key: DeviceRenderKey, from file: URL) async throws {
        guard requests[key]?.identity == target.identity else { throw APIError.conflict }
        let project = projectDirectory(key.projectID)
        let original = try await AssetImportCoordinator(project: project).importAsset(from: file)
        let imported = project.root.appendingPathComponent(original.relativePath)
        do {
            guard requests[key]?.identity == target.identity else { throw APIError.conflict }
            let store = SourceAssetStore(project: project)
            try await Task.detached { try store.relink(target.asset, original: original) }.value
        } catch {
            try? FileManager.default.removeItem(at: imported)
            throw error
        }
    }

    /// The only call site wired to a user-facing affordance (`DeviceRenderPanel`'s
    /// "Stop rendering" button) — reports the cancellation to the server via
    /// `cancelByUser()`. Every other cancel in this file is housekeeping and
    /// must keep calling the coordinator's plain `cancel()`.
    func cancel(_ key: DeviceRenderKey) async {
        tickets[key] = nil
        observations.removeValue(forKey: key)?.cancel()
        try? await entries[key]?.cancelByUser()
        if let saved = await entries[key]?.snapshot() { presentations[key] = Self.presentation(saved) }
    }

    func stopAll() async {
        tickets.removeAll()
        for observer in observations.values { observer.cancel() }
        observations.removeAll()
        for coordinator in entries.values { try? await coordinator.cancel() }
        entries.removeAll(); requests.removeAll(); presentations.removeAll()
    }

    private func observe(_ key: DeviceRenderKey, coordinator: DeviceRenderCoordinator) async {
        if let saved = await coordinator.snapshot() { presentations[key] = Self.presentation(saved) }
        guard observations[key] == nil else { return }
        observations[key] = Task { [weak self] in
            while !Task.isCancelled {
                guard let saved = await coordinator.snapshot(), !Task.isCancelled, let self,
                      self.requests[key] == saved.request else { return }
                self.presentations[key] = Self.presentation(saved)
                if ![.preparing, .rendering, .syncing].contains(saved.phase), !(await coordinator.isBusy()) {
                    guard !Task.isCancelled else { return }
                    self.observations[key] = nil
                    return
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

    private static func presentation(_ receipt: DeviceRenderReceipt) -> DeviceRenderPresentation {
        DeviceRenderPresentation(
            phase: receipt.phase, localFile: receipt.outputURL,
            message: receipt.error == nil ? nil : (receipt.outputURL == nil
                ? "This edit couldn’t finish on your iPhone. Your project is saved."
                : "Your video is ready on this iPhone. Syncing didn’t finish; you can retry without rendering again.")
        )
    }

    static func decision(_ recipe: KriaMediaEngine.EditRecipe, capabilities: PhoneRenderingCapabilities) -> CapabilityDecision {
        guard capabilities.enabled, capabilities.recipeVersions.contains(recipe.schemaVersion) else {
            return CapabilityDecision(route: .cloud, reason: "Rendering on iPhone is not available for this edit yet.")
        }
        let verified = Set(capabilities.verifiedFeatures.compactMap(MediaCapability.init(rawValue:)))
        let thermal: ThermalState = switch ProcessInfo.processInfo.thermalState {
        case .nominal: .nominal
        case .fair: .fair
        case .serious: .serious
        case .critical: .critical
        @unknown default: .unknown
        }
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let available = try? directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage
        // Storage need scales with the OUTPUT this render will write, not the source
        // footage (which already lives on the phone) -- see `StorageEstimate.forEstimatedOutput`.
        let durationS = TimelineMath.totalDuration(of: recipe)
        return CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: verified)).decide(
            for: recipe, freeStorageBytes: available,
            estimatedTemporaryBytes: StorageEstimate.forEstimatedOutput(durationS: durationS).requiredBytes,
            thermalState: thermal
        )
    }
}
