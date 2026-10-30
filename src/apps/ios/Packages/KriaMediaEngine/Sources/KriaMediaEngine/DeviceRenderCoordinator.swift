import Foundation

#if canImport(AVFoundation)
import AVFoundation
#endif

public struct DeviceRenderIdentity: Codable, Equatable, Sendable {
    public let jobID: UUID
    public let variantID: String
    public let recipeRevision: Int
    public let recipeDigest: String
    private enum CodingKeys: String, CodingKey {
        case jobID = "jobId", variantID = "variantId", recipeRevision, recipeDigest
    }
    public init(jobID: UUID, variantID: String, recipeRevision: Int, recipeDigest: String) {
        self.jobID = jobID; self.variantID = variantID
        self.recipeRevision = recipeRevision; self.recipeDigest = recipeDigest
    }
}

public struct DeviceRepairInstruction: Codable, Equatable, Sendable {
    public let action: String
    public let attempt: Int
    public let assetIDs: [String]
    private enum CodingKeys: String, CodingKey { case action, attempt; case assetIDs = "assetIds" }
}

public struct DeviceRenderRequest: Codable, Equatable, Sendable {
    public let identity: DeviceRenderIdentity
    public let recipe: EditRecipe
    public let recovery: DeviceRepairInstruction?
    /// A server-owned, exclusive output duration ceiling. Omitted by older
    /// servers and local receipts, which deliberately preserves their legacy
    /// export behavior.
    public let maxExportDurationS: Double?
    public init(identity: DeviceRenderIdentity, recipe: EditRecipe, recovery: DeviceRepairInstruction? = nil, maxExportDurationS: Double? = nil) {
        self.identity = identity; self.recipe = recipe; self.recovery = recovery; self.maxExportDurationS = maxExportDurationS
    }
}

/// The server rejects exports at or above its duration ceiling. Keep the
/// wording local and actionable so the creator can adjust the edit before an
/// upload attempt, while retaining both numbers for support/debugging.
public struct DeviceExportDurationLimitError: Error, Equatable, Sendable, LocalizedError {
    public let limitS: Double
    public let durationS: Double
    public let isMeasuredOutput: Bool

    public init(limitS: Double, durationS: Double, isMeasuredOutput: Bool) {
        self.limitS = limitS; self.durationS = durationS; self.isMeasuredOutput = isMeasuredOutput
    }

    public var errorDescription: String? {
        message(locale: .current)
    }

    /// Kept explicit for deterministic tests and for callers that already
    /// know the creator's language. The package otherwise follows the device
    /// locale used by `LocalizedError` presentation.
    public func message(locale: Locale) -> String {
        let style = Duration.UnitsFormatStyle.units(allowed: [.minutes, .seconds], width: .wide)
        let limit = Duration.seconds(limitS).formatted(style.locale(locale))
        let isTurkish = locale.identifier.lowercased().hasPrefix("tr")
        guard durationS.isFinite, durationS > 0 else {
            return isTurkish
                ? "Dışa aktarılan videonun süresi okunamadı. Sınır \(limit). Düzenlemeyi kısaltın veya daha az konuşma seçin, ardından yeniden deneyin."
                : "The exported video's length could not be read. The limit is \(limit). Shorten the edit or select less speech, then try again."
        }
        let duration = Duration.seconds(durationS).formatted(style.locale(locale))
        if isTurkish {
            let subject = isMeasuredOutput ? "Bu dışa aktarılan video \(duration) uzunluğunda." : "Bu düzenleme yaklaşık \(duration) uzunluğunda."
            return "\(subject) Sınır \(limit). Düzenlemeyi kısaltın veya daha az konuşma seçin, ardından yeniden deneyin."
        }
        let subject = isMeasuredOutput ? "This exported video is \(duration) long." : "This edit will be about \(duration) long."
        return "\(subject) The limit is \(limit). Shorten the edit or select less speech, then try again."
    }
}

public enum DeviceRenderPhase: String, Codable, Sendable {
    case preparing, rendering, localReady, syncing, synced, cancelled, needsAttention, superseded
}

public struct DeviceExportAttemptReused: Error, Sendable { public init() {} }

public struct DeviceRenderReceipt: Codable, Equatable, Sendable {
    public let request: DeviceRenderRequest
    public let attemptID: UUID
    public var phase: DeviceRenderPhase
    public var outputURL: URL?
    public var outputFingerprint: AssetFingerprint?
    public var error: String?
    /// A report the server has not accepted yet (a Stop, or a genuine failure). It is
    /// persisted with the receipt so a relaunch keeps delivering it: the phone and the
    /// server must never disagree about whether this render stopped. Absent in older
    /// receipts, which decode unchanged.
    public var pendingReport: DeviceRenderPendingReport? = nil
}

public struct DeviceRenderPendingReport: Codable, Equatable, Sendable {
    public let identity: DeviceRenderIdentity
    public let reasonCode: DeviceRenderFailureReasonCode
    public let detail: String
}

/// Server-side classification for why a device render could not finish, sent
/// via `DeviceRenderFailureReporter` so the server stops waiting on a device
/// that has already given up (rather than only learning about it, if ever,
/// from an eventual client timeout). Raw values match the server's contract.
public enum DeviceRenderFailureReasonCode: String, Codable, Sendable {
    case exportFailed = "export_failed"
    case insufficientStorage = "insufficient_storage"
    case thermal
    /// A specific feature within an otherwise-understood, valid recipe isn't
    /// something this renderer can produce (e.g. a missing capability, or a
    /// `recipe.validate()` failure that isn't a schema-version mismatch).
    /// Structural, not transient — retrying recompiles the identical recipe
    /// and fails the same way, so this reason suppresses the retry button.
    case unsupportedRecipe = "unsupported_recipe"
    /// This build's renderer doesn't speak the recipe's version at all —
    /// either `rendererVersion`/`schemaVersion` mismatched outright, or
    /// `recipe.validate()` threw `RecipeError.unsupportedSchema`. Unlike
    /// `.unsupportedRecipe`, the fix isn't "start a new edit" — it's
    /// updating the app — so this gets its own reason code and copy.
    /// Also structural: the retry button is suppressed the same way.
    case rendererOutdated = "renderer_outdated"
    /// The creator tapped Stop. The server records a resumable pause, not a failure.
    case cancelledByUser = "cancelled_by_user"
    /// An original clip (or its analysis proxy) is missing or changed on this iPhone.
    /// Relinking the original fixes it, so this is the creator's to act on, unlike a
    /// generic `export_failed`.
    case originalMissing = "original_missing"
    /// The phone cannot render right now for a reason of its own (not storage or heat).
    case deviceUnavailable = "device_unavailable"
    case unknown

    /// Classifies a `CapabilityDecision` that routed away from local rendering.
    public static func forRouteDecision(reason: String?) -> Self {
        guard let reason, !reason.isEmpty else { return .unknown }
        let lowered = reason.lowercased()
        if lowered.contains("thermal") { return .thermal }
        if lowered.contains("storage") { return .insufficientStorage }
        // Covers both "Unsupported renderer version" (schemaVersion/rendererVersion
        // mismatch) and "Unsupported renderer version for this recipe schema"
        // (recipe.validate() threw RecipeError.unsupportedSchema) — see
        // `CapabilityNegotiator.decide`.
        if lowered.contains("renderer version") { return .rendererOutdated }
        return .unsupportedRecipe
    }

    /// Classifies an error thrown while resolving sources or exporting locally.
    /// With the recipe, a missing asset that is one of its originals is `originalMissing`
    /// (prod `missingAsset("1")`); a missing font or library file stays `exportFailed`.
    public static func forRenderFailure(_ error: Error, recipe: EditRecipe? = nil) -> Self {
        if error is CancellationError { return .cancelledByUser }
        if error is DeviceExportDurationLimitError { return .exportFailed }
        if case MediaEngineError.insufficientStorage = error { return .insufficientStorage }
        if case SourceAssetError.missingOriginal = error { return .originalMissing }
        if case SourceAssetError.changedOriginal = error { return .originalMissing }
        if case let MediaEngineError.missingAsset(id) = error, let recipe, Self.originalIDs(in: recipe).contains(id) {
            return .originalMissing
        }
        // A schema-version mismatch surfacing here (recipe.validate() is also
        // called mid-render by `OriginalSourceResolver.resolve`) means this
        // build's renderer doesn't understand the recipe's schema at all —
        // not a specific unsupported feature within a schema it does
        // understand, which is what the plain `RecipeError` branch below covers.
        if case RecipeError.unsupportedSchema = error { return .rendererOutdated }
        if error is RecipeError { return .unsupportedRecipe }
        if let feature = error as? NativePreviewFeatureError, feature.isStructural { return .unsupportedRecipe }
        if error is MediaEngineError || error is SourceAssetError { return .exportFailed }
        return .unknown
    }

    private static func originalIDs(in recipe: EditRecipe) -> Set<String> {
        guard let manifest = recipe.assetManifest else { return Set(recipe.assets.map(\.id)) }
        return Set(manifest.assets.compactMap { asset in
            if case .original = asset.source { return asset.id } else { return nil }
        })
    }
}

/// Conformed to by app errors that know they are temporary (offline, a 5xx, rate
/// limiting). The coordinator waits and retries those instead of failing the render
/// or telling the server the phone gave up. `URLError` transport failures are
/// recognised without a conformance.
public protocol DeviceRenderTransientFailure: Error {
    var isTransientDeviceRenderFailure: Bool { get }
}

/// Thrown by a publisher whose upload landed but whose server-side output check is
/// still running after it stopped waiting. Nothing failed and nothing needs to be sent
/// again: the coordinator parks the finished MP4 in `.localReady` with no error and no
/// retry, and the next `recover()` resumes waiting on the same upload.
public struct DeviceRenderVerificationPending: Error, Equatable, Sendable {
    public init() {}
}

public enum DeviceRenderReportOutcome: Sendable, Equatable {
    /// The server recorded the report (or already holds the same state).
    case accepted
    /// The server refused it for good (e.g. the render already published); stop sending.
    case rejected
    /// No answer, or a temporary server problem: send it again later.
    case transient
}

/// How long the coordinator waits between automatic retries. Injected so tests run
/// without real sleeps.
public struct DeviceRenderRetryPolicy: Sendable {
    /// Waits before each retry of a transient failure inside one attempt. When they
    /// run out the attempt parks in a resumable phase (never needs-attention); the
    /// next `start`/`recover` continues it.
    public var transientDelays: [Duration]
    /// Longest wait between deliveries of a report the server has not accepted.
    public var reportBackoffCap: Duration
    public var sleep: @Sendable (Duration) async throws -> Void

    public init(transientDelays: [Duration], reportBackoffCap: Duration, sleep: @escaping @Sendable (Duration) async throws -> Void) {
        self.transientDelays = transientDelays; self.reportBackoffCap = reportBackoffCap; self.sleep = sleep
    }

    public static let standard = DeviceRenderRetryPolicy(
        transientDelays: [.seconds(2), .seconds(5), .seconds(15), .seconds(30)],
        reportBackoffCap: .seconds(60),
        sleep: { try await Task.sleep(for: $0) }
    )

    func reportDelay(after failures: Int) -> Duration {
        min(.seconds(1 << min(failures, 6)), reportBackoffCap)
    }
}

public enum DeviceRenderExecutionOrigin: Sendable, Equatable {
    case automaticRecovery
    case userInitiated(UUID)
}

public struct DeviceRenderExecutionContext: Sendable {
    public let identity: DeviceRenderIdentity
    public let attemptID: UUID
    public let runID: UUID
    public let origin: DeviceRenderExecutionOrigin
}

public enum DeviceRenderWindowOutcome: Sendable, Equatable {
    case completed, suspended, cancelled, superseded, failed
}

public actor DeviceRenderExecutionWindow {
    private let progressAction: @Sendable (Double) async -> Void
    private let finishAction: @Sendable (DeviceRenderWindowOutcome) async -> Void
    private let completionBarrier: (@Sendable (DeviceRenderWindowOutcome) async -> Void)?
    private let finishRequested: (@Sendable () async -> Void)?
    private var finished = false
    private var cleanupCompleted = false
    private var finishWaiters: [CheckedContinuation<Void, Never>] = []
    private var progress = 0.0

    public init(reportProgress: @escaping @Sendable (Double) async -> Void,
                finish: @escaping @Sendable (DeviceRenderWindowOutcome) async -> Void) {
        progressAction = reportProgress
        finishAction = finish
        completionBarrier = nil
        finishRequested = nil
    }

    init(reportProgress: @escaping @Sendable (Double) async -> Void,
         finish: @escaping @Sendable (DeviceRenderWindowOutcome) async -> Void,
         completionBarrier: @escaping @Sendable (DeviceRenderWindowOutcome) async -> Void,
         finishRequested: (@Sendable () async -> Void)? = nil) {
        progressAction = reportProgress
        finishAction = finish
        self.completionBarrier = completionBarrier
        self.finishRequested = finishRequested
    }
    public func reportProgress(_ fraction: Double) async {
        guard !finished, fraction.isFinite else { return }
        progress = max(progress, min(1, max(0, fraction)))
        await progressAction(progress)
    }

    public func finish(_ outcome: DeviceRenderWindowOutcome) async {
        await finishRequested?()
        await finishOwnedWindow(outcome)
        await completionBarrier?(outcome)
    }

    private func finishOwnedWindow(_ outcome: DeviceRenderWindowOutcome) async {
        guard !finished else {
            if !cleanupCompleted { await withCheckedContinuation { finishWaiters.append($0) } }
            return
        }
        finished = true
        await finishAction(outcome)
        cleanupCompleted = true
        let waiting = finishWaiters
        finishWaiters.removeAll()
        for continuation in waiting { continuation.resume() }
    }

}

public protocol DeviceRenderBackgroundExecution: Sendable {
    func begin(context: DeviceRenderExecutionContext, expiring: @escaping @Sendable () -> Void) async throws -> DeviceRenderExecutionWindow
}

/// Process-wide installation point, so every coordinator the app creates gets the
/// background window without each factory having to pass it.
public enum DeviceRenderBackground {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var installed: (any DeviceRenderBackgroundExecution)?
    public static func install(_ execution: (any DeviceRenderBackgroundExecution)?) {
        lock.lock(); defer { lock.unlock() }
        installed = execution
    }
    public static var current: (any DeviceRenderBackgroundExecution)? {
        lock.lock(); defer { lock.unlock() }
        return installed
    }
}

/// Injected by the app so the coordinator can tell the server about a local
/// failure without this package depending on app networking. Implementations
/// must not throw.
public protocol DeviceRenderFailureReporter: Sendable {
    func report(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async
    /// Delivers the report and says whether the server took it. The coordinator keeps
    /// re-sending a `.transient` outcome with backoff until it is accepted or refused.
    func deliver(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async -> DeviceRenderReportOutcome
}

extension DeviceRenderFailureReporter {
    /// A reporter that cannot tell is treated as accepted (the previous behavior).
    public func deliver(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async -> DeviceRenderReportOutcome {
        await report(identity: identity, reasonCode: reasonCode, detail: detail)
        return .accepted
    }
}

public enum DevicePublication: Sendable { case published, superseded }
public protocol DeviceRenderPublishing: Sendable {
    /// Must authorize and compare the exact current server revision, including after retries.
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool
    /// Reserve/upload/verify/publish idempotently by attempt ID. Never uploads source media.
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID, brandTail: String) async throws -> DevicePublication
}
public protocol DeviceSourceResolving: Sendable {
    func resolve(for recipe: EditRecipe) async throws -> [String: URL]
}
public struct OriginalSourceResolver: DeviceSourceResolving {
    public let store: SourceAssetStore
    public init(store: SourceAssetStore) { self.store = store }
    public func resolve(for recipe: EditRecipe) async throws -> [String: URL] {
        try recipe.validate()
        if let manifest = recipe.assetManifest {
            return try await PortableAssetResolver(
                originals: store,
                library: RenderLibraryCache(root: store.project.root.appendingPathComponent("library", isDirectory: true))
            ).resolve(manifest)
        }
        let ids = Set(recipe.assets.map(\.id))
        return try await Task.detached { try store.resolve(mediaIDs: ids) }.value
    }
}

/// One instance per project/variant. Receipt writes precede effects; attempt identity fences every
/// await so a cancelled or superseded task cannot publish a late result over a newer edit.
public actor DeviceRenderCoordinator {
    private let directory: URL
    private let exporter: any LocalExporting
    private let sources: any DeviceSourceResolving
    private let publisher: any DeviceRenderPublishing
    private let failureReporter: (any DeviceRenderFailureReporter)?
    private let retryPolicy: DeviceRenderRetryPolicy
    private let backgroundExecution: (any DeviceRenderBackgroundExecution)?
    private var receipt: DeviceRenderReceipt?
    private var running: Task<Void, Never>?
    private var runID: UUID?
    private var executionWindow: DeviceRenderExecutionWindow?
    private enum Command {
        case starting(UUID), stopping(UUID)
        var id: UUID { switch self { case let .starting(id), let .stopping(id): id } }
    }
    private var commandGeneration = UUID()
    private var pendingCommand: Command?
    private var reporting: Task<Void, Never>?
    /// The attempt the system is suspending (background window expired). Its stop is
    /// parked as resumable, never reported as a failure.
    private var suspendedAttempt: UUID?
    /// Live fraction (0...1) of the CURRENT attempt's export, reported by the exporter. In memory only:
    /// it is progress of a running encode, not recoverable state, so it never enters the receipt.
    private var exportFraction: Double?
    private var receiptURL: URL { directory.appendingPathComponent("device-render.json") }

    public init(
        directory: URL, exporter: any LocalExporting, sources: any DeviceSourceResolving,
        publisher: any DeviceRenderPublishing, failureReporter: (any DeviceRenderFailureReporter)? = nil,
        retryPolicy: DeviceRenderRetryPolicy = .standard,
        backgroundExecution: (any DeviceRenderBackgroundExecution)? = DeviceRenderBackground.current
    ) throws {
        self.directory = directory; self.exporter = exporter; self.sources = sources; self.publisher = publisher
        self.failureReporter = failureReporter
        self.retryPolicy = retryPolicy
        self.backgroundExecution = backgroundExecution
        let url = directory.appendingPathComponent("device-render.json")
        if FileManager.default.fileExists(atPath: url.path) {
            receipt = try JSONDecoder().decode(DeviceRenderReceipt.self, from: Data(contentsOf: url))
            if var saved = receipt, let oldURL = saved.outputURL, oldURL.isFileURL {
                let relocated = directory.appendingPathComponent(saved.attemptID.uuidString + ".mp4")
                if oldURL != relocated,
                   relocated.resolvingSymlinksInPath().deletingLastPathComponent() == directory.resolvingSymlinksInPath(),
                   FileManager.default.fileExists(atPath: relocated.path),
                   try SHA256Fingerprinter().fingerprint(file: relocated) == saved.outputFingerprint {
                    saved.outputURL = relocated
                    receipt = saved
                    try JSONEncoder().encode(saved).write(to: url, options: .atomic)
                }
            }
        }
    }
    /// Never awaited by `start()`/`cancel()`: the report is persisted on the receipt
    /// first, then delivered in the background and re-sent with backoff until the
    /// server accepts or refuses it (`deliverPendingReport`).
    private func reportFailure(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) {
        guard failureReporter != nil else { return }
        if receipt?.request.identity == identity {
            receipt?.pendingReport = DeviceRenderPendingReport(identity: identity, reasonCode: reasonCode, detail: detail)
            try? persist()
        }
        startReporting(DeviceRenderPendingReport(identity: identity, reasonCode: reasonCode, detail: detail))
    }
    /// Re-sends a report a previous launch could not deliver. `recover()` calls it; the
    /// app may also call it when the connection returns.
    public func deliverPendingReport() {
        guard let pending = receipt?.pendingReport else { return }
        startReporting(pending)
    }
    /// Waits for the report currently being delivered (tests, sign-out flushing).
    public func waitForPendingReport() async { await reporting?.value }
    private func startReporting(_ pending: DeviceRenderPendingReport) {
        guard let failureReporter, reporting == nil else { return }
        let policy = retryPolicy
        reporting = Task {
            var failures = 0
            while !Task.isCancelled {
                let outcome = await failureReporter.deliver(identity: pending.identity, reasonCode: pending.reasonCode, detail: pending.detail)
                guard outcome == .transient else { break }
                do { try await policy.sleep(policy.reportDelay(after: failures)) } catch { break }
                failures += 1
            }
            await self.finishReporting(pending)
        }
    }
    private func finishReporting(_ pending: DeviceRenderPendingReport) {
        reporting = nil
        if receipt?.pendingReport == pending {
            receipt?.pendingReport = nil
            try? persist()
        }
        // A newer report queued while this one was in flight goes out next.
        if let next = receipt?.pendingReport { startReporting(next) }
    }
    public func snapshot() -> DeviceRenderReceipt? { receipt }
    public func isBusy() -> Bool { running != nil }
    /// Capture presentation and liveness together. Separate actor calls can pair
    /// a transient localReady receipt with an idle task that has already synced.
    public func observationSnapshot() -> (receipt: DeviceRenderReceipt?, exportProgress: Double?, isBusy: Bool) {
        (receipt, running == nil ? nil : exportFraction, running != nil)
    }
    /// Export progress of the running attempt, or nil when no encode is running or the exporter reports none.
    public func exportProgress() -> Double? { running == nil ? nil : exportFraction }
    private func recordExport(_ fraction: Double, attempt: UUID, run: UUID) async {
        guard runID == run, receipt?.attemptID == attempt, fraction.isFinite else { return }
        exportFraction = max(exportFraction ?? 0, min(1, max(0, fraction)))
        await executionWindow?.reportProgress(exportFraction ?? 0)
    }
    public func waitUntilIdle() async {
        while let task = running { await task.value }
    }

    public func start(_ request: DeviceRenderRequest, decision: CapabilityDecision, origin: DeviceRenderExecutionOrigin = .automaticRecovery) async throws {
        if case .stopping = pendingCommand, origin == .automaticRecovery { return }
        if pendingCommand == nil, receipt?.request == request {
            if running != nil || receipt?.phase == .synced || receipt?.phase == .superseded { return }
            if receipt?.phase == .localReady { try retrySync(); return }
        }
        let command = beginCommand()
        defer { if pendingCommand?.id == command { pendingCommand = nil } }
        running?.cancel()
        await executionWindow?.finish(.superseded)
        guard commandGeneration == command else { return }
        executionWindow = nil
        runID = nil
        running = nil
        exportFraction = nil
        let attempt = UUID()
        let phase: DeviceRenderPhase = decision.route == .local ? .preparing : .needsAttention
        receipt = DeviceRenderReceipt(request: request, attemptID: attempt, phase: phase, error: decision.reason)
        try persist()
        guard phase == .preparing else {
            reportFailure(
                identity: request.identity,
                reasonCode: .forRouteDecision(reason: decision.reason),
                detail: decision.reason ?? "Renderer routed this edit to cloud rendering."
            )
            return
        }
        pendingCommand = nil
        launch(attempt: attempt, origin: origin)
    }

    /// An interrupted encoder restarts; an intact completed MP4 retries publication without encoding.
    /// Also the foreground hook after a suspension (`DeviceRenderBackgroundExecution`).
    public func recover() throws {
        deliverPendingReport()
        guard pendingCommand == nil, let saved = receipt, running == nil,
              ![.cancelled, .superseded, .synced, .needsAttention].contains(saved.phase) else { return }
        if saved.outputURL != nil, try intactOutput(saved) == nil {
            try replaceAttempt(output: nil)
            return
        }
        launch(attempt: saved.attemptID)
    }

    private func replaceAttempt(output: URL?, launchImmediately: Bool = true) throws {
        guard pendingCommand == nil else { throw CancellationError() }
        guard let saved = receipt else { return }
        let attempt = UUID()
        var replacement: URL?
        if let output {
            let target = directory.appendingPathComponent(attempt.uuidString + ".mp4")
            try FileManager.default.copyItem(at: output, to: target)
            replacement = target
        }
        let next = DeviceRenderReceipt(request: saved.request, attemptID: attempt,
                                       phase: replacement == nil ? .preparing : .localReady,
                                       outputURL: replacement, outputFingerprint: replacement == nil ? nil : saved.outputFingerprint)
        try JSONEncoder().encode(next).write(to: receiptURL, options: .atomic)
        receipt = next
        if launchImmediately { launch(attempt: attempt) }
    }

    public func retrySync() throws {
        guard pendingCommand == nil, let saved = receipt, saved.outputURL != nil, saved.phase == .localReady, running == nil else { return }
        if try intactOutput(saved) == nil { try replaceAttempt(output: nil); return }
        launch(attempt: saved.attemptID)
    }

    /// Housekeeping cancel: used when a request is superseded, a stale
    /// receipt is dropped before a retry, or the workspace tears down. Never
    /// reports to the server — the identity is often still perfectly valid
    /// server-side (e.g. `stopAll()` on the workspace disappearing mid-render),
    /// so reporting here would wrongly flip a healthy job to `needsAttention`.
    public func cancel() async throws {
        _ = try await cancelOperation()
    }

    private func beginCommand(stopping: Bool = false) -> UUID {
        let command = UUID()
        commandGeneration = command
        pendingCommand = stopping ? .stopping(command) : .starting(command)
        return command
    }

    private func cancelOperation() async throws -> UUID? {
        let command = beginCommand(stopping: true)
        defer { if pendingCommand?.id == command { pendingCommand = nil } }
        running?.cancel()
        await executionWindow?.finish(.cancelled)
        guard commandGeneration == command else { return nil }
        executionWindow = nil
        runID = nil
        running = nil
        guard receipt != nil else { return command }
        receipt?.phase = .cancelled
        try persist()
        return command
    }

    /// Same effect as `cancel()`, but also tells the server the user explicitly
    /// stopped this render. The server records a resumable pause (not a failure), and
    /// the report is retried until it lands so both sides agree the render stopped.
    /// Call this ONLY from a user-facing cancel affordance (e.g. a "Stop rendering"
    /// button) — never from housekeeping.
    public func cancelByUser() async throws {
        let saved = receipt
        guard let command = try await cancelOperation(), commandGeneration == command else { return }
        guard let saved, ![.cancelled, .synced, .superseded, .needsAttention].contains(saved.phase) else { return }
        reportFailure(identity: saved.request.identity, reasonCode: .cancelledByUser, detail: "Cancelled by user")
    }

    private func current(_ attempt: UUID) -> Bool {
        receipt?.attemptID == attempt && receipt?.phase != .cancelled
    }
    /// The background window expired: stop the encode before the system suspends the
    /// app, and keep the receipt resumable (`recover()` continues it on foreground).
    private func suspend(_ attempt: UUID, run: UUID) {
        guard runID == run, receipt?.attemptID == attempt, running != nil else { return }
        suspendedAttempt = attempt
        running?.cancel()
    }
    private func isTransient(_ error: Error) -> Bool {
        if let error = error as? DeviceRenderTransientFailure { return error.isTransientDeviceRenderFailure }
        if let error = error as? URLError { return Self.transportCodes.contains(error.code) }
        return false
    }
    private static let transportCodes: Set<URLError.Code> = [
        .notConnectedToInternet, .networkConnectionLost, .timedOut, .cannotConnectToHost,
        .cannotFindHost, .dnsLookupFailed, .dataNotAllowed, .internationalRoamingOff, .callIsActive,
        .secureConnectionFailed, .badServerResponse,
    ]
    /// AVFoundation reports an export the system interrupted (the app went to the
    /// background) as an error, not a cancellation. It is a suspension, not a failure.
    private static func isInterruption(_ error: Error) -> Bool {
        #if canImport(AVFoundation)
        // AVErrorOperationInterrupted, AVErrorSessionWasInterrupted and
        // AVErrorMediaServicesWereReset; raw codes because the macOS SDK the package
        // tests build against does not name all of them.
        let error = error as NSError
        return error.domain == AVFoundationErrorDomain && [-11847, -11818, -11819].contains(error.code)
        #else
        return false
        #endif
    }
    /// Park the attempt where it can continue: before an output exists it waits in
    /// `.preparing`, after it the finished MP4 waits in `.localReady` for publication.
    private func park(_ attempt: UUID, error: String?) {
        guard current(attempt) else { return }
        try? update(attempt, phase: receipt?.outputURL == nil ? .preparing : .localReady, error: error)
    }
    private func persist() throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try JSONEncoder().encode(receipt).write(to: receiptURL, options: .atomic)
    }
    private func update(_ attempt: UUID, phase: DeviceRenderPhase, error: String? = nil) throws {
        guard current(attempt) else { throw CancellationError() }
        receipt?.phase = phase; receipt?.error = error
        try persist()
    }
    private func intactOutput(_ saved: DeviceRenderReceipt) throws -> URL? {
        guard let url = saved.outputURL, url.isFileURL,
              url.resolvingSymlinksInPath().deletingLastPathComponent() == directory.resolvingSymlinksInPath(),
              FileManager.default.fileExists(atPath: url.path),
              try SHA256Fingerprinter().fingerprint(file: url) == saved.outputFingerprint else { return nil }
        return url
    }
    /// The recipe timeline is the canonical pre-export estimate. A standard
    /// brand tail is measured from the exact bundled asset the exporter uses,
    /// rather than duplicated as a stale seconds constant here.
    private func estimatedExportDuration(for request: DeviceRenderRequest) async throws -> Double {
        let recipeDuration = TimelineMath.totalDuration(of: request.recipe)
        guard recipeDuration.isFinite, recipeDuration >= 0 else { throw MediaEngineError.exportFailed }
        #if canImport(AVFoundation)
        guard exporter.brandTail == KriaBranding.Options.standard.contractTail else { return recipeDuration }
        guard let outro = try await KriaOutroAssetCache.shared.load() else {
            throw MediaEngineError.missingBrandingResource(KriaBranding.outroFileName)
        }
        let total = recipeDuration + outro.duration.seconds
        guard total.isFinite else { throw MediaEngineError.exportFailed }
        return total
        #else
        return recipeDuration
        #endif
    }
    private func validateEstimatedDuration(for request: DeviceRenderRequest) async throws {
        guard let limit = request.maxExportDurationS else { return }
        guard limit.isFinite, limit > 0 else { throw MediaEngineError.exportFailed }
        let estimate = try await estimatedExportDuration(for: request)
        guard estimate < limit else {
            throw DeviceExportDurationLimitError(limitS: limit, durationS: estimate, isMeasuredOutput: false)
        }
    }
    private func validateOutputDuration(_ output: URL, for request: DeviceRenderRequest) async throws {
        guard let limit = request.maxExportDurationS else { return }
        guard limit.isFinite, limit > 0 else { throw MediaEngineError.exportFailed }
        #if canImport(AVFoundation)
        let duration = try await AVURLAsset(url: output).load(.duration).seconds
        guard duration.isFinite, duration > 0, duration < limit else {
            throw DeviceExportDurationLimitError(limitS: limit, durationS: duration, isMeasuredOutput: true)
        }
        #else
        throw MediaEngineError.avFoundationUnavailable
        #endif
    }
    private func launch(attempt: UUID, origin: DeviceRenderExecutionOrigin = .automaticRecovery) {
        guard pendingCommand == nil else { return }
        let run = UUID()
        runID = run
        running = Task { await self.perform(attempt: attempt, run: run, origin: origin) }
    }

    private func perform(attempt: UUID, run: UUID, origin: DeviceRenderExecutionOrigin) async {
        guard current(attempt), runID == run, let identity = receipt?.request.identity else { return }
        var window: DeviceRenderExecutionWindow?
        do {
            try Task.checkCancellation()
            window = try await backgroundExecution?.begin(context: .init(identity: identity, attemptID: attempt, runID: run, origin: origin)) { [weak self] in
                Task { await self?.suspend(attempt, run: run) }
            }
            try Task.checkCancellation()
            guard runID == run else { throw CancellationError() }
            executionWindow = window
            await performWork(attempt: attempt, run: run)
        } catch {
            if runID == run, current(attempt) { park(attempt, error: nil) }
        }
        let outcome: DeviceRenderWindowOutcome
        if runID != run || receipt?.attemptID != attempt { outcome = .superseded }
        else {
            switch receipt?.phase {
            case .synced: outcome = .completed
            case .cancelled: outcome = .cancelled
            case .superseded: outcome = .superseded
            case .needsAttention: outcome = .failed
            default: outcome = .suspended
            }
        }
        await window?.finish(outcome)
        if runID == run {
            executionWindow = nil
            runID = nil
            running = nil
            suspendedAttempt = nil
            if let replacement = receipt, replacement.attemptID != attempt { launch(attempt: replacement.attemptID) }
        }
    }

    private func performWork(attempt: UUID, run: UUID) async {
        var transientFailures = 0
        while true {
            do {
                try await performOnce(attempt: attempt, run: run)
                return
            } catch is CancellationError {
                if suspendedAttempt == attempt {
                    suspendedAttempt = nil
                    park(attempt, error: nil)
                } else if current(attempt) {
                    try? update(attempt, phase: .cancelled)
                }
                return
            } catch is DeviceExportAttemptReused {
                guard current(attempt), let saved = receipt, let output = try? intactOutput(saved) else { return }
                do { try replaceAttempt(output: output, launchImmediately: false) }
                catch { try? update(attempt, phase: .localReady, error: error.localizedDescription) }
                return
            } catch {
                guard current(attempt) else { return }
                if suspendedAttempt == attempt || Self.isInterruption(error) {
                    // Leaving Kria mid-render is not a failure: keep it resumable.
                    suspendedAttempt = nil
                    park(attempt, error: nil)
                    return
                }
                if error is DeviceRenderVerificationPending {
                    // Uploaded; the server is still checking it. Wait without
                    // re-uploading: the next recover() resumes the same check.
                    park(attempt, error: nil)
                    return
                }
                if isTransient(error) {
                    // Offline, a 5xx or rate limiting during an asset grant, the
                    // isCurrent read or the upload: wait and try again, never report.
                    guard transientFailures < retryPolicy.transientDelays.count else {
                        // Parked, not failed: no error, so it is never worded as one.
                        // The app resumes it on foreground / the next reconcile.
                        park(attempt, error: nil)
                        return
                    }
                    park(attempt, error: nil)
                    do { try await retryPolicy.sleep(retryPolicy.transientDelays[transientFailures]) } catch {
                        // Only a suspension cancels this wait while the receipt is
                        // current (Stop and a newer start already moved it on): the
                        // background window ended, so keep it resumable.
                        if suspendedAttempt == attempt { suspendedAttempt = nil }
                        park(attempt, error: nil)
                        return
                    }
                    transientFailures += 1
                    guard current(attempt) else { return }
                    continue
                }
                // A cached file may have reached `.localReady` in an older
                // build before the bound existed. Treat an invalid measured
                // duration as a local-render failure anyway: it is never
                // eligible for another sync attempt.
                let entersNeedsAttention = receipt?.outputURL == nil || error is DeviceExportDurationLimitError
                let identity = receipt?.request.identity
                let recipe = receipt?.request.recipe
                let detail = error.localizedDescription
                try? update(attempt, phase: entersNeedsAttention ? .needsAttention : .localReady, error: detail)
                if entersNeedsAttention, let identity {
                    reportFailure(identity: identity, reasonCode: .forRenderFailure(error, recipe: recipe), detail: detail)
                }
                return
            }
        }
    }
    private func performOnce(attempt: UUID, run: UUID) async throws {
        guard let saved = receipt, current(attempt) else { return }
        let serverCurrent = try await publisher.isCurrent(saved.request.identity)
        guard current(attempt) else { return }
        guard serverCurrent else { try update(attempt, phase: .superseded); return }
        try await validateEstimatedDuration(for: saved.request)
        guard current(attempt) else { return }
        let output: URL
        if let existing = try intactOutput(saved) {
            output = existing
            try await validateOutputDuration(output, for: saved.request)
            guard current(attempt) else { return }
        } else {
            if saved.outputURL != nil {
                try replaceAttempt(output: nil, launchImmediately: false)
                return
            }
            try update(attempt, phase: .preparing)
            let assets = try await sources.resolve(for: saved.request.recipe)
            guard current(attempt) else { return }
            try update(attempt, phase: .rendering)
            exportFraction = nil
            output = directory.appendingPathComponent("\(attempt.uuidString).mp4")
            _ = try await exporter.export(recipe: saved.request.recipe, assetURLs: assets, outputURL: output, exportID: attempt.uuidString,
                                     progress: { [weak self] fraction in Task { await self?.recordExport(fraction, attempt: attempt, run: run) } })
            guard current(attempt) else { return }
            try await validateOutputDuration(output, for: saved.request)
            guard current(attempt) else { return }
            receipt?.outputURL = output
            receipt?.outputFingerprint = try SHA256Fingerprinter().fingerprint(file: output)
            try update(attempt, phase: .localReady)
        }
        try Task.checkCancellation()
        let stillCurrent = try await publisher.isCurrent(saved.request.identity)
        guard current(attempt) else { return }
        guard stillCurrent else { try update(attempt, phase: .superseded); return }
        try update(attempt, phase: .syncing)
        let result = try await publisher.publish(file: output, identity: saved.request.identity, attemptID: attempt, brandTail: exporter.brandTail)
        guard current(attempt) else { return }
        try update(attempt, phase: result == .published ? .synced : .superseded)
    }
}
