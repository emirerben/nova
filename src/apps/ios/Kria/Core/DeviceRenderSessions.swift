import Foundation
import Observation
import CryptoKit
import Network
import UIKit
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

/// Why a device render is waiting rather than rendering. None of these is a
/// failure: each clears on its own (the network returns, the server answers,
/// the phone cools, space is freed) and `DeviceRenderSessions` resumes the
/// render itself, so the creator is never asked to tap Try again for them.
enum DeviceRenderWait: Equatable, Sendable {
    /// The status read never got a response (offline, timed out, dropped).
    case connection
    /// Kria's server answered 5xx / 429, or kept losing a lock race.
    case server
    /// `ProcessInfo.thermalState` is serious or critical.
    case cooling
    /// The phone needs about `neededBytes` more free space for this export.
    case storage(neededBytes: Int64)
    /// The server answered with a state this build doesn't recognise yet.
    case checking

    /// Connection and server waits sit quietly on top of whatever the render
    /// was already showing; the others replace it with their own state.
    var isQuiet: Bool { self == .connection || self == .server }
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
    /// Set while the render waits on something that resolves by itself.
    var waiting: DeviceRenderWait? = nil
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

    /// A render waiting on the phone itself (heat, storage) or on a server
    /// state this build doesn't know must not read "Rendering on iPhone";
    /// quiet network waits keep the label of the phase they sit on.
    static func `for`(presentation: DeviceRenderPresentation) -> String {
        switch presentation.waiting {
        case .cooling, .storage: "Waiting to render"
        case .checking: "Checking render…"
        case .connection, .server, nil:
            presentation.phase == .cancelled && presentation.reasonCode == DeviceRenderSessions.pausedReason
                ? "Render paused" : Self.for(phase: presentation.phase)
        }
    }
}


/// How the phone reads a failed status read. Only a server answer may move a
/// render into a failure; a read that never reached the server, or reached it
/// while it was busy, waits and retries instead (S1-a, S1-c, S1-d, S1-m), and
/// the server's own 4xx answers map to the state they really describe.
enum DeviceRenderReadFailure: Equatable {
    case cancelled
    case transient(DeviceRenderWait)
    /// Session expiry and a retired client protocol have their own app-wide gates.
    case appGate
    /// A newer approved attempt (or a new job for the project) replaced this render.
    case superseded
    /// The job itself was cancelled, from another device, the web or the server.
    case jobCancelled
    /// There is no device render to show: the job or record is gone, or the editor draft is empty.
    case inactive
    /// The pinned recipe uses a feature this build can't decode (426 `client_update_required`).
    case updateRequired
    /// The recipe arrived, but this build can't read it.
    case unreadableRecipe(DeviceRenderRecipeUnreadable)
    case other

    init(_ error: Error, taskCancelled: Bool = Task.isCancelled) {
        // Async URLSession reports a cancelled task as URLError(.cancelled),
        // not CancellationError; both mean nobody is waiting for this answer.
        if taskCancelled || error is CancellationError { self = .cancelled; return }
        if let error = error as? URLError, error.code == .cancelled { self = .cancelled; return }
        if let unreadable = error as? DeviceRenderRecipeUnreadable { self = .unreadableRecipe(unreadable); return }
        switch error {
        case APIError.sessionExpired, APIError.nativeUpdateRequired:
            self = .appGate
        case let APIError.conflict(detail):
            self = Self.conflict(code: detail.code, message: detail.message)
        case let APIError.requestFailed(status, detail):
            switch status {
            case 404, 410: self = .inactive
            // The device-render GET answers 426 only for `client_update_required`
            // (the protocol-wide `native_update_required` is `.nativeUpdateRequired`).
            case 426: self = .updateRequired
            case 408, 429, 500...599: self = .transient(.server)
            default: self = detail.code == "client_update_required" ? .updateRequired : .other
            }
        default:
            self = RequestFailureCause(error) == .connection ? .transient(.connection) : .other
        }
    }

    /// Prefers a structured `{code}` detail; falls back to today's string details.
    static func conflict(code: String?, message: String?) -> Self {
        switch code {
        case "superseded", "project_changed", "device_recipe_changed": return .superseded
        case "render_cancelled", "job_cancelled": return .jobCancelled
        case "editor_empty", "device_recipe_unavailable": return .inactive
        case "client_update_required": return .updateRequired
        case "concurrent_update": return .transient(.server)
        default: break
        }
        switch message {
        case "Device recipe changed", "Project changed": return .superseded
        case "Render cancelled": return .jobCancelled
        case "Editor draft has no video to render": return .inactive
        // A server-side inconsistency, not something this phone did. Keep checking.
        case "This export has no matching successful output check": return .transient(.checking)
        // `request()` already resent a deadlock victim twice; the lock race is still transient.
        case let message? where message.hasPrefix("That project was being updated at the same time"): return .transient(.server)
        default: return .other
        }
    }
}

/// Inputs the route decision reads from the phone itself. Injectable so the
/// heat and storage waits can be exercised without a hot or full device.
struct DeviceRenderEnvironment: Sendable {
    var thermalState: @Sendable () -> ThermalState
    var freeStorageBytes: @Sendable () -> Int64?

    static let live = DeviceRenderEnvironment(
        thermalState: {
            switch ProcessInfo.processInfo.thermalState {
            case .nominal: .nominal
            case .fair: .fair
            case .serious: .serious
            case .critical: .critical
            @unknown default: .unknown
            }
        },
        freeStorageBytes: {
            let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            return try? directory.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]).volumeAvailableCapacityForImportantUsage
        }
    )
}

/// What this phone does with a recipe the server is waiting on it to render.
enum DeviceRenderGate: Equatable {
    case local(CapabilityDecision)
    /// The phone can render this, just not right now (heat, storage, unknown capabilities).
    case wait(DeviceRenderWait)
    /// The phone will never render this request; the server must be told (typed reason code).
    case decline(reasonCode: String, detail: String)
}

/// App-owned so navigating between chat, projects, and the editor does not
/// interrupt an export. Each server revision still owns its own coordinator.
@Observable @MainActor final class DeviceRenderSessions {
    typealias Fetch = @Sendable (UUID, String) async throws -> DeviceRenderStatusResponse
    typealias Factory = @MainActor (DeviceRenderKey, DeviceRenderRequest) throws -> DeviceRenderCoordinator
    typealias RetryFailure = @Sendable (UUID, DeviceRenderIdentity) async throws -> DeviceRenderRetryAck
    typealias ReportPreparationFailure = @Sendable (UUID, Error) async -> Void
    /// `POST /device-render/failures` for a request this phone won't render: (job, identity, reason code, detail).
    typealias ReportDecline = @Sendable (UUID, DeviceRenderIdentity, String, String) async throws -> Void
    typealias Sleep = @Sendable (Duration) async throws -> Void

    /// `needsAttention` reason for a job cancelled outside this phone. No retry: the job is gone.
    nonisolated static let jobCancelledReason = "job_cancelled"
    /// `.cancelled` reason for the creator's own Stop: a resumable pause, never a failure.
    nonisolated static let pausedReason = "cancelled_by_user"
    /// Automatic re-reads per key before waiting for a reconnect, foreground or thermal change.
    static let maxAutomaticRetries = 12
    static func retryDelay(attempt: Int) -> Duration { .seconds(min(60, 1 << min(max(attempt, 1), 6))) }
    /// Heat and storage are re-checked on this cadence (plus their own notifications).
    static let waitRecheck: Duration = .seconds(30)

    private(set) var presentations: [DeviceRenderKey: DeviceRenderPresentation] = [:]
    private(set) var retryApprovalKeys: Set<DeviceRenderKey> = []
    /// Keys with no device render behind them any more (job or record gone,
    /// editor draft empty). Callers show the job's own status instead.
    private(set) var inactiveKeys: Set<DeviceRenderKey> = []
    private var verificationKeys: Set<DeviceRenderKey> = []
    /// Re-approval in chat applies only to a server that really needs a new
    /// attempt. A phone-local failure (server still awaiting this device) is
    /// retried locally; routing it to chat approval was a dead end (S1-k).
    func retryNeedsApproval(_ key: DeviceRenderKey) -> Bool {
        retryApprovalKeys.contains(key) && presentations[key]?.requiresServerRetry == true
    }
    func isInactive(_ key: DeviceRenderKey) -> Bool { inactiveKeys.contains(key) }
    /// Nothing about this render changes until the thread itself moves, so
    /// polling callers may stop reconciling it on every tick.
    func isSettled(_ key: DeviceRenderKey) -> Bool {
        if inactiveKeys.contains(key) { return true }
        guard let presentation = presentations[key] else { return false }
        return presentation.phase == .synced || presentation.phase == .superseded
            || (presentation.phase == .cancelled && presentation.reasonCode == Self.jobCancelledReason)
    }
    /// The last capability set the server actually enabled, kept so a failed
    /// capability fetch (handed in as `.disabled`) never reads as "not available".
    private(set) var lastEnabledCapabilities: PhoneRenderingCapabilities?
    @ObservationIgnored private let fetch: Fetch
    @ObservationIgnored private let factory: Factory
    @ObservationIgnored private let retryFailure: RetryFailure
    @ObservationIgnored private let reportPreparationFailure: ReportPreparationFailure
    @ObservationIgnored private let reportDecline: ReportDecline
    @ObservationIgnored private let environment: DeviceRenderEnvironment
    @ObservationIgnored private let sleep: Sleep
    @ObservationIgnored private let projectDirectory: @MainActor (UUID) -> ProjectDirectory
    @ObservationIgnored private var entries: [DeviceRenderKey: DeviceRenderCoordinator] = [:]
    @ObservationIgnored private var requests: [DeviceRenderKey: DeviceRenderRequest] = [:]
    @ObservationIgnored private var observations: [DeviceRenderKey: Task<Void, Never>] = [:]
    @ObservationIgnored private var tickets: [DeviceRenderKey: UUID] = [:]
    /// KRI-935: the superseded request each key already followed once, so a server that keeps
    /// pointing at the same request cannot loop the refresh.
    @ObservationIgnored private var followedSuperseded: [DeviceRenderKey: DeviceRenderIdentity] = [:]
    @ObservationIgnored private var capabilities: [DeviceRenderKey: PhoneRenderingCapabilities] = [:]
    @ObservationIgnored private var retryTasks: [DeviceRenderKey: Task<Void, Never>] = [:]
    @ObservationIgnored private var retryAttempts: [DeviceRenderKey: Int] = [:]
    @ObservationIgnored private var declines: [DeviceRenderKey: DeclineReport] = [:]
    @ObservationIgnored private var observers: [NSObjectProtocol] = []
    @ObservationIgnored private var pathMonitor: NWPathMonitor?
    @ObservationIgnored private var wasOnline = true

    private struct DeclineReport {
        enum State { case sending, accepted, rejected, exhausted }
        let identity: DeviceRenderIdentity
        var state: State
        var task: Task<Void, Never>?
    }

    convenience init(api: any KriaAPIClient) {
        self.init(fetch: { try await api.deviceRender(jobID: $0, variantID: $1) }, factory: { key, request in
            let project = BackgroundUploadCoordinator.projectDirectory(key.projectID)
            let variant = SHA256.hash(data: Data(key.variantID.utf8)).map { String(format: "%02x", $0) }.joined()
            let directory = project.root.appending(path: "device-renders/\(key.jobID.uuidString)/\(variant)", directoryHint: .isDirectory)
            return try Self.openCoordinator(directory: directory) {
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
            }
        }, retryFailure: { jobID, identity in try await api.retryDeviceRenderFailure(jobID: jobID, identity: identity) },
        reportPreparationFailure: { jobID, error in
            let report = NativePreviewDiagnostics.previewFailure(error: error, sourceState: "device_render_preparation")
            try? await api.reportPlaybackFailure(jobID: jobID, report: report)
        },
        reportDecline: { jobID, identity, reasonCode, detail in
            _ = try await api.reportDeviceRenderFailure(jobID: jobID, identity: identity, reasonCode: reasonCode, detail: String(detail.prefix(2000)))
        })
        startMonitoring()
    }

    init(fetch: @escaping Fetch, factory: @escaping Factory,
         retryFailure: @escaping RetryFailure = { _, _ in throw APIError.unsupported },
         reportPreparationFailure: @escaping ReportPreparationFailure = { _, _ in },
         reportDecline: @escaping ReportDecline = { _, _, _, _ in },
         environment: DeviceRenderEnvironment = .live,
         sleep: @escaping Sleep = { try await Task.sleep(for: $0) },
         projectDirectory: @escaping @MainActor (UUID) -> ProjectDirectory = { BackgroundUploadCoordinator.projectDirectory($0) }) {
        self.fetch = fetch; self.factory = factory; self.retryFailure = retryFailure
        self.reportPreparationFailure = reportPreparationFailure; self.reportDecline = reportDecline
        self.environment = environment; self.sleep = sleep; self.projectDirectory = projectDirectory
    }

    /// A receipt this build can't read (written by a newer build, or truncated)
    /// used to fail every reconcile forever at a fixed path (S1-o). A fresh
    /// render is always safe, so set the file aside and start without it.
    static func openCoordinator(directory: URL, make: () throws -> DeviceRenderCoordinator) throws -> DeviceRenderCoordinator {
        do { return try make() } catch {
            let receipt = directory.appending(path: "device-render.json")
            guard FileManager.default.fileExists(atPath: receipt.path) else { throw error }
            let quarantine = directory.appending(path: "device-render.unreadable.json")
            try? FileManager.default.removeItem(at: quarantine)
            try FileManager.default.moveItem(at: receipt, to: quarantine)
            NativePreviewDiagnostics.record("device-render-receipt-quarantined", fields: ["error": String(describing: type(of: error))])
            return try make()
        }
    }

    func reconcile(_ key: DeviceRenderKey, capabilities offered: PhoneRenderingCapabilities, retry: Bool = false) async {
        // `.disabled` is also what a failed capability fetch turns into. The
        // server masks a real rollout pause on the status read itself, so
        // keep rendering against the last set it actually enabled.
        if offered.enabled, lastEnabledCapabilities != offered { lastEnabledCapabilities = offered }
        let capabilities = offered == .disabled ? (lastEnabledCapabilities ?? offered) : offered
        self.capabilities[key] = capabilities
        guard tickets[key] == nil else { return }
        let ticket = UUID()
        tickets[key] = ticket
        defer { if tickets[key] == ticket { tickets[key] = nil } }
        let status: DeviceRenderStatusResponse
        do {
            status = try await fetch(key.jobID, key.variantID)
        } catch {
            let failure = DeviceRenderReadFailure(error)
            guard tickets[key] == ticket else { return }
            await readFailed(key, failure: failure, error: error, ticket: ticket)
            return
        }
        guard tickets[key] == ticket, !Task.isCancelled else { return }
        // Observed by the chat body: only write when it actually changes.
        if inactiveKeys.contains(key) { inactiveKeys.remove(key) }
        guard status.request.identity.jobID == key.jobID, status.request.identity.variantID == key.variantID else {
            // Unreachable today: the server builds identity from this record. Never a failure screen.
            wait(.checking, for: key)
            return
        }
        do {
            try await apply(status, for: key, ticket: ticket, capabilities: capabilities, retry: retry)
        } catch {
            guard tickets[key] == ticket else { return }
            await preparationFailed(key, error: error, ticket: ticket)
        }
    }

    private func apply(_ status: DeviceRenderStatusResponse, for key: DeviceRenderKey, ticket: UUID,
                       capabilities: PhoneRenderingCapabilities, retry: Bool) async throws {
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
        let unchecked = status.outputVerificationRequired && saved?.phase != .synced
        if status.phase == "published" {
            settle(key)
            observations.removeValue(forKey: key)?.cancel()
            presentations[key] = DeviceRenderPresentation(phase: .synced, localFile: saved?.request == request ? saved?.outputURL : nil, publishedGeneration: status.publishedGeneration)
            return
        }
        let gate = Self.gate(request.recipe, capabilities: capabilities, environment: environment)
        if status.phase == "needs_attention" {
            settle(key)
            observations.removeValue(forKey: key)?.cancel()
            var reasonCode = status.reasonCode, message = status.reason
            // A decline this build reported earlier (outdated renderer, rollout
            // paused) no longer holds once the phone can render the recipe:
            // offer the retry instead of repeating "Update Kria".
            if case .local = gate, ["renderer_outdated", "device_unavailable"].contains(reasonCode) {
                reasonCode = nil
                message = "Kria can render this edit on your iPhone now. Try again to start it."
            }
            // A server from before Stop became a pause records it as a failure.
            // It is still the creator's own pause: Resume, never "needs attention".
            let paused = reasonCode == Self.pausedReason && saved?.outputURL == nil
            presentations[key] = DeviceRenderPresentation(
                phase: paused ? .cancelled : (saved?.outputURL == nil || unchecked) ? .needsAttention : .localReady,
                localFile: !unchecked && saved?.request == request ? saved?.outputURL : nil,
                message: message, reasonCode: reasonCode, requiresServerRetry: true
            )
            return
        }
        guard ["awaiting_device", "syncing"].contains(status.phase) else {
            // A phase this build doesn't know yet is never a failure; keep checking.
            wait(.checking, for: key)
            return
        }
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
        if status.pausedByUser {
            // The creator tapped Stop. The server holds that pause under the same
            // identity; nothing restarts the render until the creator resumes it.
            let pausedHere = saved?.request != request || saved?.phase == .cancelled
            if pausedHere && !retry {
                settle(key)
                observations.removeValue(forKey: key)?.cancel()
                presentations[key] = DeviceRenderPresentation(phase: .cancelled, reasonCode: Self.pausedReason)
                return
            }
            // Resume (or a Stop report that landed after the creator already resumed
            // here): `/retry` lifts the pause and keeps the identity. Best effort --
            // the server takes this phone's upload either way.
            _ = try? await retryFailure(key.jobID, request.identity)
            guard tickets[key] == ticket else { return }
        }
        // Finishing an export that already exists only uploads; heat and
        // storage gates are about encoding, so they don't hold a sync back.
        let resumesSync = saved?.request == request && saved?.outputURL != nil && [.localReady, .syncing].contains(saved?.phase)
        // An encode already running keeps going whatever the heat or free
        // space does now (the export uses the space the estimate reserved);
        // those gates only hold back starting a new one.
        var inFlight = false
        if case .wait = gate, saved?.request == request, [.preparing, .rendering].contains(saved?.phase) {
            inFlight = await coordinator.observationSnapshot().isBusy
            guard tickets[key] == ticket else { return }
        }
        var decision = CapabilityDecision(route: .local)
        switch gate {
        case .local(let local):
            decision = local
        case .wait where resumesSync || inFlight:
            break
        case .wait(let reason):
            observations.removeValue(forKey: key)?.cancel()
            presentations[key] = DeviceRenderPresentation(phase: .preparing, waiting: reason)
            scheduleRetry(key, after: reason == .checking ? nil : Self.waitRecheck)
            return
        case .decline(let reasonCode, let detail):
            settle(key)
            // Preserve a finished export and its receipt; the creator can still watch and share it.
            observations.removeValue(forKey: key)?.cancel()
            let keepsExport = saved?.request == request && saved?.outputURL != nil
            presentations[key] = DeviceRenderPresentation(
                phase: keepsExport ? .localReady : .needsAttention,
                localFile: !unchecked && saved?.request == request ? saved?.outputURL : nil,
                message: keepsExport ? "Your video is ready on this iPhone. It can’t sync from this iPhone yet." : nil,
                reasonCode: keepsExport ? nil : reasonCode,
                outputVerificationPending: unchecked
            )
            if !keepsExport { sendDecline(key, identity: request.identity, reasonCode: reasonCode, detail: detail) }
            return
        }
        settle(key)
        // Builds before this one parked a hot or full phone in a needsAttention
        // receipt; once the phone can render again, that wait is over.
        let parkedWait = saved?.request == request && saved?.phase == .needsAttention && saved?.outputURL == nil
            && [.thermal, .insufficientStorage].contains(DeviceRenderFailureReasonCode.forRouteDecision(reason: saved?.error))
        if saved?.request != request || retry || parkedWait {
            try await coordinator.start(request, decision: decision)
        } else if !(await coordinator.isBusy()) {
            // A parked render (suspension, outage, pending check) resumes on
            // every reconcile, not only when nothing is observing it.
            try await coordinator.recover()
        }
        guard tickets[key] == ticket else { return }
        await observe(key, coordinator: coordinator)
    }

    private func readFailed(_ key: DeviceRenderKey, failure: DeviceRenderReadFailure, error: Error, ticket: UUID) async {
        switch failure {
        case .cancelled, .appGate:
            return
        case .transient(let reason):
            wait(reason, for: key)
        case .superseded:
            presentations[key] = DeviceRenderPresentation(phase: .superseded)
            await retire(key)
        case .jobCancelled:
            presentations[key] = DeviceRenderPresentation(phase: .cancelled, reasonCode: Self.jobCancelledReason)
            await retire(key)
        case .inactive:
            presentations[key] = nil
            inactiveKeys.insert(key)
            await retire(key)
        case .updateRequired:
            settle(key)
            // A server answer, but never one that unpublishes a finished video.
            guard !holdsFinishedVideo(key) else { return }
            observations.removeValue(forKey: key)?.cancel()
            presentations[key] = DeviceRenderPresentation(phase: .needsAttention, reasonCode: "renderer_outdated")
            if let identity = requests[key]?.identity {
                sendDecline(key, identity: identity, reasonCode: "renderer_outdated", detail: "Client update required for this recipe")
            }
        case .unreadableRecipe(let unreadable):
            settle(key)
            if unreadable.phase == "published" {
                if presentations[key]?.phase != .synced {
                    presentations[key] = DeviceRenderPresentation(phase: .synced, publishedGeneration: unreadable.publishedGeneration)
                }
                return
            }
            presentations[key] = DeviceRenderPresentation(phase: .needsAttention, reasonCode: unreadable.reasonCode,
                                                          requiresServerRetry: unreadable.phase == "needs_attention")
            if ["awaiting_device", "syncing"].contains(unreadable.phase) {
                sendDecline(key, identity: unreadable.identity, reasonCode: unreadable.reasonCode, detail: unreadable.detail)
            }
            await retire(key)
        case .other:
            // Only a server answer may move a finished video; an unusable read never does.
            guard !holdsFinishedVideo(key) else { return }
            await preparationFailed(key, error: error, ticket: ticket)
        }
    }

    /// A local step after a good status read failed (receipt, coordinator, start).
    private func preparationFailed(_ key: DeviceRenderKey, error: Error, ticket: UUID) async {
        if DeviceRenderReadFailure(error) == .cancelled { return }
        if Self.isOutOfSpace(error) {
            let required = requests[key].map { StorageEstimate.forEstimatedOutput(durationS: TimelineMath.totalDuration(of: $0.recipe)).requiredBytes } ?? 0
            let free = environment.freeStorageBytes() ?? 0
            observations.removeValue(forKey: key)?.cancel()
            presentations[key] = DeviceRenderPresentation(phase: .preparing, waiting: .storage(neededBytes: max(required - free, 0)))
            scheduleRetry(key, after: Self.waitRecheck)
            return
        }
        var presentation = presentations[key] ?? DeviceRenderPresentation(phase: .needsAttention)
        // A completed or stopped previous request is not success for a new
        // save whose preparation just failed. Keep an actual in-flight
        // coordinator observable; otherwise clear the stale output identity.
        let active = await entries[key]?.observationSnapshot()
        guard tickets[key] == ticket else { return }
        if active?.isBusy != true || active?.receipt?.request != requests[key] {
            presentation = DeviceRenderPresentation(phase: .needsAttention)
        }
        presentation.waiting = nil
        presentation.message = "Kria couldn’t prepare this edit on your iPhone. Your project is saved. \(error.localizedDescription)"
        presentations[key] = presentation
        let report = reportPreparationFailure
        Task { await report(key.jobID, error) }
    }

    /// The phone already holds a finished video for the request it knows.
    private func holdsFinishedVideo(_ key: DeviceRenderKey) -> Bool {
        guard let presentation = presentations[key] else { return false }
        return presentation.phase == .synced || (presentation.phase == .localReady && presentation.localFile != nil)
    }

    /// Keeps whatever the render was showing and waits. Quiet waits sit on top
    /// of the current state; a synced render has nothing left to wait for.
    private func wait(_ reason: DeviceRenderWait, for key: DeviceRenderKey) {
        var presentation = presentations[key] ?? DeviceRenderPresentation(phase: .preparing)
        guard presentation.phase != .synced else { return }
        presentation.waiting = reason
        presentations[key] = presentation
        scheduleRetry(key)
    }

    /// Re-reads `key` after a backoff. Reconnect, foreground and thermal
    /// changes cut the wait short through `resumeWaiting`.
    private func scheduleRetry(_ key: DeviceRenderKey, after delay: Duration? = nil) {
        retryTasks.removeValue(forKey: key)?.cancel()
        let attempt = (retryAttempts[key] ?? 0) + 1
        retryAttempts[key] = attempt
        guard attempt <= Self.maxAutomaticRetries else { return }
        let pause = delay ?? Self.retryDelay(attempt: attempt), sleep = self.sleep
        retryTasks[key] = Task { [weak self] in
            do { try await sleep(pause) } catch { return }
            guard !Task.isCancelled, let self else { return }
            self.retryTasks[key] = nil
            guard let capabilities = self.capabilities[key] else { return }
            await self.reconcile(key, capabilities: capabilities)
        }
    }

    /// The render reached a state only the server or the creator moves next.
    private func settle(_ key: DeviceRenderKey) {
        retryTasks.removeValue(forKey: key)?.cancel()
        retryAttempts[key] = nil
    }

    /// Drops the local coordinator for a request the server no longer wants
    /// rendered on this phone. Housekeeping only: nothing is reported.
    private func retire(_ key: DeviceRenderKey) async {
        settle(key)
        observations.removeValue(forKey: key)?.cancel()
        retryApprovalKeys.remove(key); verificationKeys.remove(key)
        requests.removeValue(forKey: key)
        if let previous = entries.removeValue(forKey: key) { try? await previous.cancel() }
    }

    /// Tells the server, until it accepts, that this phone won't render the
    /// request, so it stops waiting 24 hours for the reaper (CloudDecisionReported).
    private func sendDecline(_ key: DeviceRenderKey, identity: DeviceRenderIdentity, reasonCode: String, detail: String) {
        if let report = declines[key], report.identity == identity, report.state != .exhausted { return }
        declines[key]?.task?.cancel()
        let send = reportDecline, sleep = self.sleep
        declines[key] = DeclineReport(identity: identity, state: .sending)
        declines[key]?.task = Task { [weak self] in
            var attempt = 0
            while !Task.isCancelled {
                do {
                    try await send(key.jobID, identity, reasonCode, detail)
                    guard let self, self.declines[key]?.identity == identity else { return }
                    self.declines[key]?.state = .accepted
                    self.declines[key]?.task = nil
                    // Pick up the server's needs_attention answer (reason, retry rules).
                    if let capabilities = self.capabilities[key] { await self.reconcile(key, capabilities: capabilities) }
                    return
                } catch {
                    attempt += 1
                    let retryable: Bool = if case .transient = DeviceRenderReadFailure(error, taskCancelled: false) { true } else { false }
                    guard retryable, attempt < Self.maxAutomaticRetries else {
                        guard let self, self.declines[key]?.identity == identity else { return }
                        self.declines[key]?.state = retryable ? .exhausted : .rejected
                        self.declines[key]?.task = nil
                        return
                    }
                    do { try await sleep(Self.retryDelay(attempt: attempt)) } catch { return }
                }
            }
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
            settle(key)
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
            if case let APIError.conflict(detail) = error, detail.code == "requires_change" {
                // The same recipe would be refused again: the edit needs a change in
                // chat, so Try again goes away instead of looping (S3-o).
                presentation.reasonCode = "requires_change"
                presentation.message = detail.message
                presentations[key] = presentation
                return false
            }
            presentation.message = RequestFailureCause(error) == .connection
                ? "Kria couldn’t retry this edit. Check your connection and try again."
                : "Kria couldn’t retry this edit. \(error.localizedDescription)"
            presentations[key] = presentation
            return false
        }
    }

    /// Re-reads every waiting render now instead of after its backoff.
    /// `thermalOnly` limits it to renders waiting for the phone to cool.
    func resumeWaiting(thermalOnly: Bool = false) {
        for (key, presentation) in presentations {
            guard let waiting = presentation.waiting, !thermalOnly || waiting == .cooling,
                  let capabilities = capabilities[key] else { continue }
            retryTasks.removeValue(forKey: key)?.cancel()
            retryAttempts[key] = nil
            Task { await self.reconcile(key, capabilities: capabilities) }
        }
    }

    private func startMonitoring() {
        let center = NotificationCenter.default
        observers.append(center.addObserver(forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.resumeWaiting() }
        })
        observers.append(center.addObserver(forName: ProcessInfo.thermalStateDidChangeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.resumeWaiting(thermalOnly: true) }
        })
        let monitor = NWPathMonitor()
        monitor.pathUpdateHandler = { [weak self] path in
            let online = path.status == .satisfied
            Task { @MainActor in self?.networkChanged(online: online) }
        }
        monitor.start(queue: DispatchQueue(label: "kria.device-render-reachability"))
        pathMonitor = monitor
    }

    private func networkChanged(online: Bool) {
        defer { wasOnline = online }
        if online && !wasOnline { resumeWaiting() }
    }

    static func isOutOfSpace(_ error: Error) -> Bool {
        if case MediaEngineError.insufficientStorage = error { return true }
        let error = error as NSError
        if error.domain == NSCocoaErrorDomain, error.code == NSFileWriteOutOfSpaceError { return true }
        if error.domain == NSPOSIXErrorDomain, error.code == Int(ENOSPC) { return true }
        if let underlying = error.userInfo[NSUnderlyingErrorKey] as? Error { return isOutOfSpace(underlying) }
        return false
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
        settle(key)
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
        for task in retryTasks.values { task.cancel() }
        retryTasks.removeAll(); retryAttempts.removeAll()
        for report in declines.values { report.task?.cancel() }
        declines.removeAll()
        for coordinator in entries.values { try? await coordinator.cancel() }
        entries.removeAll(); requests.removeAll(); presentations.removeAll(); retryApprovalKeys.removeAll()
        verificationKeys.removeAll(); inactiveKeys.removeAll(); capabilities.removeAll(); followedSuperseded.removeAll()
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
        guard followedSuperseded[key] != identity, let capabilities = capabilities[key] else { return false }
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
                : "Your video is ready on this iPhone. Syncing didn’t finish; you can retry without rendering again."),
            // A stopped receipt is the creator's Stop (`cancelByUser`); housekeeping
            // cancels drop or replace the presentation instead.
            reasonCode: receipt.phase == .cancelled ? pausedReason : nil
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

    static func decision(_ recipe: KriaMediaEngine.EditRecipe, capabilities: PhoneRenderingCapabilities,
                         environment: DeviceRenderEnvironment = .live) -> CapabilityDecision {
        guard capabilities.enabled, capabilities.recipeVersions.contains(recipe.schemaVersion) else {
            return CapabilityDecision(route: .cloud, reason: "Rendering on iPhone is not available for this edit yet.")
        }
        let verified = Set(capabilities.verifiedFeatures.compactMap(MediaCapability.init(rawValue:)))
        // Storage need scales with the OUTPUT this render will write, not the source
        // footage (which already lives on the phone) -- see `StorageEstimate.forEstimatedOutput`.
        let durationS = TimelineMath.totalDuration(of: recipe)
        return CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: verified)).decide(
            for: recipe, freeStorageBytes: environment.freeStorageBytes(),
            estimatedTemporaryBytes: StorageEstimate.forEstimatedOutput(durationS: durationS).requiredBytes,
            thermalState: environment.thermalState()
        )
    }

    /// Sorts a non-local decision into what resolves by itself (heat,
    /// storage, capabilities not known yet) and what the server must hear
    /// about (an outdated renderer, a recipe this phone can't produce).
    static func gate(_ recipe: KriaMediaEngine.EditRecipe, capabilities: PhoneRenderingCapabilities,
                     environment: DeviceRenderEnvironment = .live) -> DeviceRenderGate {
        let decision = decision(recipe, capabilities: capabilities, environment: environment)
        guard decision.route != .local else { return .local(decision) }
        // Disabled is indistinguishable from a capability read that failed, and
        // the server already reports a real rollout pause on the status read.
        guard capabilities.enabled else { return .wait(.checking) }
        let detail = decision.reason ?? "Renderer routed this edit away from the phone."
        guard capabilities.recipeVersions.contains(recipe.schemaVersion) else {
            return .decline(reasonCode: DeviceRenderFailureReasonCode.unsupportedRecipe.rawValue, detail: detail)
        }
        switch DeviceRenderFailureReasonCode.forRouteDecision(reason: decision.reason) {
        case .thermal:
            return .wait(.cooling)
        case .insufficientStorage:
            let required = StorageEstimate.forEstimatedOutput(durationS: TimelineMath.totalDuration(of: recipe)).requiredBytes
            return .wait(.storage(neededBytes: max(required - (environment.freeStorageBytes() ?? 0), 0)))
        case let code:
            return .decline(reasonCode: code == .unknown ? DeviceRenderFailureReasonCode.unsupportedRecipe.rawValue : code.rawValue, detail: detail)
        }
    }
}
