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
    /// Real export progress (0...1) while `phase == .rendering`; nil when the exporter has reported none.
    /// Drives the live plan feed (KRI-443); never shown as a number.
    var exportProgress: Double?
    var outputVerificationPending: Bool = false
    var repairAttempt: Int? = nil
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
        case .superseded: "Edit updated"
        }
    }
}

/// App-owned so navigating between chat, projects, and the editor does not
/// interrupt an export. Each server revision still owns its own coordinator.
@Observable @MainActor final class DeviceRenderSessions {
    typealias Fetch = @Sendable (UUID, String) async throws -> DeviceRenderStatusResponse
    typealias Factory = @MainActor (DeviceRenderKey, DeviceRenderRequest) throws -> DeviceRenderCoordinator
    typealias RetryFailure = @Sendable (UUID, DeviceRenderIdentity) async throws -> DeviceRenderRetryAck
    typealias ReportPreparationFailure = @Sendable (UUID, Error) async -> Void
    private(set) var presentations: [DeviceRenderKey: DeviceRenderPresentation] = [:]
    private(set) var retryApprovalKeys: Set<DeviceRenderKey> = []
    private var verificationKeys: Set<DeviceRenderKey> = []
    func retryNeedsApproval(_ key: DeviceRenderKey) -> Bool { retryApprovalKeys.contains(key) }
    @ObservationIgnored private let fetch: Fetch
    @ObservationIgnored private let factory: Factory
    @ObservationIgnored private let retryFailure: RetryFailure
    @ObservationIgnored private let reportPreparationFailure: ReportPreparationFailure
    @ObservationIgnored private let projectDirectory: @MainActor (UUID) -> ProjectDirectory
    @ObservationIgnored private var entries: [DeviceRenderKey: DeviceRenderCoordinator] = [:]
    @ObservationIgnored private var requests: [DeviceRenderKey: DeviceRenderRequest] = [:]
    @ObservationIgnored private var observations: [DeviceRenderKey: Task<Void, Never>] = [:]
    @ObservationIgnored private var tickets: [DeviceRenderKey: UUID] = [:]
    /// Capabilities of the last reconcile per key, so a superseded export can follow the newest request.
    @ObservationIgnored private var lastCapabilities: [DeviceRenderKey: PhoneRenderingCapabilities] = [:]
    /// KRI-935: the superseded request each key already followed once, so a server that keeps
    /// pointing at the same request cannot loop the refresh.
    @ObservationIgnored private var followedSuperseded: [DeviceRenderKey: DeviceRenderIdentity] = [:]

    convenience init(api: any KriaAPIClient) {
        self.init(fetch: { try await api.deviceRender(jobID: $0, variantID: $1) }, factory: { key, request in
            let project = BackgroundUploadCoordinator.projectDirectory(key.projectID)
            let variant = SHA256.hash(data: Data(key.variantID.utf8)).map { String(format: "%02x", $0) }.joined()
            let directory = project.root.appending(path: "device-renders/\(key.jobID.uuidString)/\(variant)", directoryHint: .isDirectory)
            #if DEBUG
            // UI tests only (`-ui-testing-chat` + `KRIA_CHAT_DEVICE_RENDER`): a fixture export stands in for AVFoundation.
            if let fixture = try DeviceRenderUITestFixture.coordinator(directory: directory) { return fixture }
            #endif
            let library = RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory))
            return try DeviceRenderCoordinator(
                directory: directory,
                exporter: AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: directory)),
                sources: AuthorizedDeviceSourceResolver(api: api, request: request, originals: SourceAssetStore(project: project), library: library),
                publisher: DeviceExportPublisher(api: api),
                failureReporter: APIDeviceRenderFailureReporter(api: api)
            )
        }, retryFailure: { jobID, identity in try await api.retryDeviceRenderFailure(jobID: jobID, identity: identity) },
        reportPreparationFailure: { jobID, error in
            let report = NativePreviewDiagnostics.previewFailure(error: error, sourceState: "device_render_preparation")
            try? await api.reportPlaybackFailure(jobID: jobID, report: report)
        })
    }

    init(fetch: @escaping Fetch, factory: @escaping Factory,
         retryFailure: @escaping RetryFailure = { _, _ in throw APIError.unsupported },
         reportPreparationFailure: @escaping ReportPreparationFailure = { _, _ in },
         projectDirectory: @escaping @MainActor (UUID) -> ProjectDirectory = { BackgroundUploadCoordinator.projectDirectory($0) }) {
        self.fetch = fetch; self.factory = factory; self.retryFailure = retryFailure
        self.reportPreparationFailure = reportPreparationFailure; self.projectDirectory = projectDirectory
    }

    func reconcile(_ key: DeviceRenderKey, capabilities: PhoneRenderingCapabilities, retry: Bool = false) async {
        guard tickets[key] == nil else { return }
        lastCapabilities[key] = capabilities
        let ticket = UUID()
        tickets[key] = ticket
        defer { if tickets[key] == ticket { tickets[key] = nil } }
        do {
            let status = try await fetch(key.jobID, key.variantID)
            guard tickets[key] == ticket, !Task.isCancelled else { return }
            guard status.request.identity.jobID == key.jobID,
                  status.request.identity.variantID == key.variantID else { throw APIError.invalidResponse }
            let request = status.request
            if status.outputVerificationRequired { verificationKeys.insert(key) }
            else { verificationKeys.remove(key) }
            if status.retryRequiresApproval { retryApprovalKeys.insert(key) }
            else { retryApprovalKeys.remove(key) }
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
                let unchecked = status.outputVerificationRequired && saved?.phase != .synced
                presentations[key] = DeviceRenderPresentation(
                    phase: (saved?.outputURL == nil || (unchecked && status.phase == "needs_attention")) ? .needsAttention : .localReady,
                    localFile: !unchecked && saved?.request == request ? saved?.outputURL : nil,
                    message: status.reason ?? decision.reason,
                    reasonCode: status.reasonCode,
                    requiresServerRetry: status.phase == "needs_attention",
                    outputVerificationPending: unchecked && status.phase != "needs_attention"
                )
                return
            }
            guard ["awaiting_device", "syncing"].contains(status.phase) else { throw APIError.invalidResponse }
            if status.phoneRenderingPaused {
                // A rollout hold (S3-b): wait without starting. A grant or upload
                // would be refused, and that refusal is not the phone's failure.
                observations.removeValue(forKey: key)?.cancel()
                let finished = saved?.request == request ? saved?.outputURL : nil
                presentations[key] = DeviceRenderPresentation(
                    phase: finished == nil ? .preparing : .localReady,
                    localFile: status.outputVerificationRequired ? nil : finished,
                    message: status.reason
                )
                return
            }
            if saved?.request != request || retry {
                try await coordinator.start(request, decision: decision)
            } else if !(await coordinator.isBusy()) {
                // A parked render (suspension, outage, pending check) resumes on
                // every reconcile, not only when nothing is observing it.
                try await coordinator.recover()
            }
            guard tickets[key] == ticket else { return }
            await observe(key, coordinator: coordinator)
        } catch is CancellationError {
            return
        } catch {
            guard tickets[key] == ticket else { return }
            var presentation = presentations[key] ?? DeviceRenderPresentation(phase: .needsAttention)
            // A completed or stopped previous request is not success for a new
            // save whose preparation just failed. Keep an actual in-flight
            // coordinator observable; otherwise clear the stale output identity.
            let active = await entries[key]?.observationSnapshot()
            guard tickets[key] == ticket else { return }
            if active?.isBusy != true || active?.receipt?.request != requests[key] {
                presentation = DeviceRenderPresentation(phase: .needsAttention)
            }
            presentation.message = RequestFailureCause(error) == .connection
                ? "Kria couldn’t prepare this edit on your iPhone. Your project is saved. Try again when you’re connected."
                : "Kria couldn’t prepare this edit on your iPhone. Your project is saved. \(error.localizedDescription)"
            presentations[key] = presentation
            let report = reportPreparationFailure
            Task { await report(key.jobID, error) }
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
        if let saved = await entries[key]?.snapshot() { presentations[key] = presentation(saved, for: key) }
    }

    /// Resumes every render that parked while idle (a background suspension, a long
    /// outage, a check still running). Called when the app returns to the foreground,
    /// so a parked render continues on its own instead of sitting on "Preparing".
    func resumeParked() async {
        for (key, coordinator) in entries {
            guard !(await coordinator.isBusy()), requests[key] != nil else { continue }
            try? await coordinator.recover()
            guard await coordinator.isBusy() else { continue }
            await observe(key, coordinator: coordinator)
        }
    }

    func stopAll() async {
        tickets.removeAll()
        for observer in observations.values { observer.cancel() }
        observations.removeAll()
        for coordinator in entries.values { try? await coordinator.cancel() }
        entries.removeAll(); requests.removeAll(); presentations.removeAll(); retryApprovalKeys.removeAll()
        verificationKeys.removeAll(); lastCapabilities.removeAll(); followedSuperseded.removeAll()
    }

    private func observe(_ key: DeviceRenderKey, coordinator: DeviceRenderCoordinator) async {
        let initial = await coordinator.observationSnapshot()
        guard !Task.isCancelled, requests[key] == initial.receipt?.request else { return }
        if let saved = initial.receipt {
            var presentation = presentation(saved, for: key)
            presentation.exportProgress = initial.exportProgress
            presentations[key] = presentation
        }
        guard observations[key] == nil else { return }
        observations[key] = Task { [weak self] in
            while !Task.isCancelled {
                let state = await coordinator.observationSnapshot()
                guard let saved = state.receipt, !Task.isCancelled, let self,
                      self.requests[key] == saved.request else { return }
                if saved.phase == .superseded, !state.isBusy, self.followNewestRequest(key, superseded: saved.request.identity) {
                    self.observations[key] = nil
                    return
                }
                var presentation = self.presentation(saved, for: key)
                presentation.exportProgress = state.exportProgress
                if self.presentations[key] != presentation { self.presentations[key] = presentation }
                // Idle means nothing will change until something resumes it
                // (`reconcile`/`resumeParked` start a new observation then).
                if !state.isBusy {
                    self.observations[key] = nil
                    return
                }
                try? await Task.sleep(for: .milliseconds(250))
            }
        }
    }

    /// KRI-935: the server moved on to a newer request while this export ran. Reconcile against
    /// it right away (the same path a save takes) instead of showing "a newer edit is available".
    private func followNewestRequest(_ key: DeviceRenderKey, superseded identity: DeviceRenderIdentity) -> Bool {
        guard followedSuperseded[key] != identity, let capabilities = lastCapabilities[key] else { return false }
        followedSuperseded[key] = identity
        presentations[key] = DeviceRenderPresentation(phase: .preparing)
        Task { [weak self] in await self?.reconcile(key, capabilities: capabilities) }
        return true
    }

    private static func presentation(_ receipt: DeviceRenderReceipt) -> DeviceRenderPresentation {
        DeviceRenderPresentation(
            phase: receipt.phase, localFile: receipt.outputURL,
            message: receipt.error == nil ? nil : (receipt.outputURL == nil
                ? "This edit couldn’t finish on your iPhone. Your project is saved."
                : "Your video is ready on this iPhone. Syncing didn’t finish; you can retry without rendering again.")
        )
    }

    private func presentation(_ receipt: DeviceRenderReceipt, for key: DeviceRenderKey) -> DeviceRenderPresentation {
        var value = Self.presentation(receipt)
        value.repairAttempt = [.preparing, .rendering].contains(receipt.phase) ? receipt.request.recovery?.attempt : nil
        if verificationKeys.contains(key) && receipt.phase != .synced {
            value.localFile = nil
            value.outputVerificationPending = [.localReady, .syncing].contains(receipt.phase)
            if receipt.error != nil {
                value.message = "This export hasn't passed its checks yet. Your approved edit is saved."
            }
        }
        return value
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
