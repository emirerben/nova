import Foundation

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

public struct DeviceRenderRequest: Codable, Equatable, Sendable {
    public let identity: DeviceRenderIdentity
    public let recipe: EditRecipe
    public init(identity: DeviceRenderIdentity, recipe: EditRecipe) { self.identity = identity; self.recipe = recipe }
}

public enum DeviceRenderPhase: String, Codable, Sendable {
    case preparing, rendering, localReady, syncing, synced, cancelled, needsAttention, superseded
}

public struct DeviceRenderReceipt: Codable, Equatable, Sendable {
    public let request: DeviceRenderRequest
    public let attemptID: UUID
    public var phase: DeviceRenderPhase
    public var outputURL: URL?
    public var outputFingerprint: AssetFingerprint?
    public var error: String?
}

/// Server-side classification for why a device render could not finish, sent
/// via `DeviceRenderFailureReporter` so the server stops waiting on a device
/// that has already given up (rather than only learning about it, if ever,
/// from an eventual client timeout). Raw values match the server's contract.
public enum DeviceRenderFailureReasonCode: String, Sendable {
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
    case cancelledByUser = "cancelled_by_user"
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
    public static func forRenderFailure(_ error: Error) -> Self {
        if error is CancellationError { return .cancelledByUser }
        if case MediaEngineError.insufficientStorage = error { return .insufficientStorage }
        // A schema-version mismatch surfacing here (recipe.validate() is also
        // called mid-render by `OriginalSourceResolver.resolve`) means this
        // build's renderer doesn't understand the recipe's schema at all —
        // not a specific unsupported feature within a schema it does
        // understand, which is what the plain `RecipeError` branch below covers.
        if case RecipeError.unsupportedSchema = error { return .rendererOutdated }
        if error is RecipeError { return .unsupportedRecipe }
        if error is MediaEngineError || error is SourceAssetError { return .exportFailed }
        return .unknown
    }
}

/// Injected by the app so the coordinator can tell the server about a local
/// failure without this package depending on app networking. Implementations
/// must not throw: a failed report should be logged and swallowed, never
/// crash or retry-loop the coordinator.
public protocol DeviceRenderFailureReporter: Sendable {
    func report(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) async
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
    private var receipt: DeviceRenderReceipt?
    private var running: Task<Void, Never>?
    private var receiptURL: URL { directory.appendingPathComponent("device-render.json") }

    public init(
        directory: URL, exporter: any LocalExporting, sources: any DeviceSourceResolving,
        publisher: any DeviceRenderPublishing, failureReporter: (any DeviceRenderFailureReporter)? = nil
    ) throws {
        self.directory = directory; self.exporter = exporter; self.sources = sources; self.publisher = publisher
        self.failureReporter = failureReporter
        let url = directory.appendingPathComponent("device-render.json")
        if FileManager.default.fileExists(atPath: url.path) {
            receipt = try JSONDecoder().decode(DeviceRenderReceipt.self, from: Data(contentsOf: url))
        }
    }
    /// Fire-and-forget: never awaited, so a slow or failing report cannot
    /// block `start()`/`cancel()` or make the coordinator retry-loop.
    private func reportFailure(identity: DeviceRenderIdentity, reasonCode: DeviceRenderFailureReasonCode, detail: String) {
        guard let failureReporter else { return }
        Task { await failureReporter.report(identity: identity, reasonCode: reasonCode, detail: detail) }
    }
    public func snapshot() -> DeviceRenderReceipt? { receipt }
    public func isBusy() -> Bool { running != nil }
    public func waitUntilIdle() async { await running?.value }

    public func start(_ request: DeviceRenderRequest, decision: CapabilityDecision) throws {
        if receipt?.request == request {
            if running != nil || receipt?.phase == .synced || receipt?.phase == .superseded { return }
            if receipt?.phase == .localReady { try retrySync(); return }
        }
        running?.cancel()
        running = nil
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
        running = Task { await self.perform(attempt: attempt) }
    }

    /// An interrupted encoder restarts; an intact completed MP4 retries publication without encoding.
    public func recover() throws {
        guard let saved = receipt, running == nil,
              ![.cancelled, .superseded, .synced, .needsAttention].contains(saved.phase) else { return }
        running = Task { await self.perform(attempt: saved.attemptID) }
    }

    public func retrySync() throws {
        guard let saved = receipt, saved.outputURL != nil, saved.phase == .localReady, running == nil else { return }
        running = Task { await self.perform(attempt: saved.attemptID) }
    }

    /// Housekeeping cancel: used when a request is superseded, a stale
    /// receipt is dropped before a retry, or the workspace tears down. Never
    /// reports to the server — the identity is often still perfectly valid
    /// server-side (e.g. `stopAll()` on the workspace disappearing mid-render),
    /// so reporting here would wrongly flip a healthy job to `needsAttention`.
    public func cancel() throws {
        running?.cancel(); running = nil
        guard receipt != nil else { return }
        receipt?.phase = .cancelled
        try persist()
    }

    /// Same effect as `cancel()`, but also tells the server the user
    /// explicitly stopped this render, so it stops waiting on a device that
    /// has given up. Call this ONLY from a user-facing cancel affordance
    /// (e.g. a "Stop rendering" button) — never from housekeeping.
    public func cancelByUser() throws {
        let saved = receipt
        try cancel()
        guard let saved, ![.cancelled, .synced, .superseded, .needsAttention].contains(saved.phase) else { return }
        reportFailure(identity: saved.request.identity, reasonCode: .cancelledByUser, detail: "Cancelled by user")
    }

    private func current(_ attempt: UUID) -> Bool {
        receipt?.attemptID == attempt && receipt?.phase != .cancelled
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
    private func perform(attempt: UUID) async {
        defer { if receipt?.attemptID == attempt { running = nil } }
        guard let saved = receipt, current(attempt) else { return }
        do {
            let serverCurrent = try await publisher.isCurrent(saved.request.identity)
            guard current(attempt) else { return }
            guard serverCurrent else { try update(attempt, phase: .superseded); return }
            let output: URL
            if let existing = try intactOutput(saved) {
                output = existing
            } else {
                try update(attempt, phase: .preparing)
                let assets = try await sources.resolve(for: saved.request.recipe)
                guard current(attempt) else { return }
                try update(attempt, phase: .rendering)
                output = directory.appendingPathComponent("\(attempt.uuidString).mp4")
                _ = try await exporter.export(recipe: saved.request.recipe, assetURLs: assets, outputURL: output, exportID: attempt.uuidString, progress: nil)
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
        } catch is CancellationError {
            if current(attempt) { try? update(attempt, phase: .cancelled) }
        } catch {
            if current(attempt) {
                let entersNeedsAttention = receipt?.outputURL == nil
                let identity = receipt?.request.identity
                let detail = String(describing: error)
                try? update(attempt, phase: entersNeedsAttention ? .needsAttention : .localReady, error: detail)
                if entersNeedsAttention, let identity {
                    reportFailure(identity: identity, reasonCode: .forRenderFailure(error), detail: detail)
                }
            }
        }
    }
}
