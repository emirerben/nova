import Foundation
import Observation
import CryptoKit
import KriaMediaEngine

struct DeviceRenderKey: Hashable, Sendable {
    let projectID: UUID
    let jobID: UUID
    let variantID: String
}

struct DeviceRenderPresentation: Equatable, Sendable {
    var phase: DeviceRenderPhase
    var localFile: URL?
    var message: String?
}

/// App-owned so navigating between chat, projects, and the editor does not
/// interrupt an export. Each server revision still owns its own coordinator.
@Observable @MainActor final class DeviceRenderSessions {
    typealias Fetch = @Sendable (UUID, String) async throws -> DeviceRenderStatusResponse
    typealias Factory = @MainActor (DeviceRenderKey, DeviceRenderRequest) throws -> DeviceRenderCoordinator
    private(set) var presentations: [DeviceRenderKey: DeviceRenderPresentation] = [:]
    @ObservationIgnored private let fetch: Fetch
    @ObservationIgnored private let factory: Factory
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
                publisher: DeviceExportPublisher(api: api)
            )
        })
    }

    init(fetch: @escaping Fetch, factory: @escaping Factory) {
        self.fetch = fetch; self.factory = factory
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
                observations[key]?.cancel()
                if let previous = entries[key] { try await previous.cancel() }
                guard tickets[key] == ticket else { return }
                entries[key] = try factory(key, request)
                requests[key] = request
            }
            guard let coordinator = entries[key] else { return }
            let saved = await coordinator.snapshot()
            guard tickets[key] == ticket else { return }
            if status.phase == "published" {
                presentations[key] = DeviceRenderPresentation(phase: .synced, localFile: saved?.request == request ? saved?.outputURL : nil)
                return
            }
            let decision = Self.decision(request.recipe, capabilities: capabilities)
            if status.phase == "needs_attention" || decision.route != .local {
                // Preserve a finished export and its receipt when rollout is paused.
                presentations[key] = DeviceRenderPresentation(
                    phase: saved?.outputURL == nil ? .needsAttention : .localReady,
                    localFile: saved?.request == request ? saved?.outputURL : nil,
                    message: status.reason ?? decision.reason
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
            presentation.message = "Kria couldn’t prepare this edit on your iPhone. Your project is saved. Try again when you’re connected."
            presentations[key] = presentation
        }
    }

    func cancel(_ key: DeviceRenderKey) async {
        tickets[key] = nil
        observations.removeValue(forKey: key)?.cancel()
        try? await entries[key]?.cancel()
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
                guard let saved = await coordinator.snapshot(), let self,
                      self.requests[key] == saved.request else { return }
                self.presentations[key] = Self.presentation(saved)
                if ![.preparing, .rendering, .syncing].contains(saved.phase), !(await coordinator.isBusy()) {
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
        let bytes = recipe.assets.reduce(Int64(0)) { total, asset in
            let (sum, overflow) = total.addingReportingOverflow(asset.fingerprint?.byteCount ?? 0)
            return overflow ? .max : sum
        }
        return CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: verified)).decide(
            for: recipe, freeStorageBytes: available, estimatedTemporaryBytes: StorageEstimate.forAssetBytes(bytes).requiredBytes, thermalState: thermal
        )
    }
}
