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

public enum DevicePublication: Sendable { case published, superseded }
public protocol DeviceRenderPublishing: Sendable {
    /// Must authorize and compare the exact current server revision, including after retries.
    func isCurrent(_ identity: DeviceRenderIdentity) async throws -> Bool
    /// Reserve/upload/verify/publish idempotently by attempt ID. Never uploads source media.
    func publish(file: URL, identity: DeviceRenderIdentity, attemptID: UUID) async throws -> DevicePublication
}
public protocol DeviceSourceResolving: Sendable {
    func resolve(for recipe: EditRecipe) async throws -> [String: URL]
}
public struct OriginalSourceResolver: DeviceSourceResolving {
    public let store: SourceAssetStore
    public init(store: SourceAssetStore) { self.store = store }
    public func resolve(for recipe: EditRecipe) async throws -> [String: URL] {
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
    private var receipt: DeviceRenderReceipt?
    private var running: Task<Void, Never>?
    private var receiptURL: URL { directory.appendingPathComponent("device-render.json") }

    public init(directory: URL, exporter: any LocalExporting, sources: any DeviceSourceResolving, publisher: any DeviceRenderPublishing) throws {
        self.directory = directory; self.exporter = exporter; self.sources = sources; self.publisher = publisher
        let url = directory.appendingPathComponent("device-render.json")
        if FileManager.default.fileExists(atPath: url.path) {
            receipt = try JSONDecoder().decode(DeviceRenderReceipt.self, from: Data(contentsOf: url))
        }
    }
    public func snapshot() -> DeviceRenderReceipt? { receipt }
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
        guard phase == .preparing else { return }
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

    public func cancel() throws {
        running?.cancel(); running = nil
        guard receipt != nil else { return }
        receipt?.phase = .cancelled
        try persist()
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
            let result = try await publisher.publish(file: output, identity: saved.request.identity, attemptID: attempt)
            guard current(attempt) else { return }
            try update(attempt, phase: result == .published ? .synced : .superseded)
        } catch is CancellationError {
            if current(attempt) { try? update(attempt, phase: .cancelled) }
        } catch {
            if current(attempt) {
                try? update(attempt, phase: receipt?.outputURL == nil ? .needsAttention : .localReady, error: String(describing: error))
            }
        }
    }
}
