import Foundation
import CryptoKit

public struct DeviceRenderIntentKey: Hashable, Sendable {
    public let owner: String
    public let projectID: UUID
    public let jobID: UUID
    public let variantID: String

    public init(owner: String, projectID: UUID, jobID: UUID, variantID: String) {
        self.owner = owner
        self.projectID = projectID
        self.jobID = jobID
        self.variantID = variantID
    }
}

public struct DeviceRenderIntent: Codable, Equatable, Sendable {
    public enum State: String, Codable, Sendable { case retryPending, stopped, released }
    public enum Decision: Equatable, Sendable { case allow, hold, published }

    public let version: Int
    public let operationID: UUID
    public let sourceIdentity: DeviceRenderIdentity
    public let baseGeneration: String?
    public var state: State
    public var acknowledgement: DeviceRenderIdentity?
    public var observedIdentity: DeviceRenderIdentity?

    public init(operationID: UUID = UUID(), sourceIdentity: DeviceRenderIdentity, baseGeneration: String?, state: State) {
        version = 1
        self.operationID = operationID
        self.sourceIdentity = sourceIdentity
        self.baseGeneration = baseGeneration
        self.state = state
    }

    public func decision(identity: DeviceRenderIdentity, baseGeneration currentGeneration: String?, published: Bool) -> Decision {
        guard sameVariant(identity) else { return .hold }
        guard state != .released else { return .allow }
        let floor = max(sourceIdentity.recipeRevision, acknowledgement?.recipeRevision ?? 0, observedIdentity?.recipeRevision ?? 0)
        let sameGeneration = baseGeneration?.isEmpty == false && baseGeneration == currentGeneration
        // Nothing but a typed creator Resume releases a Stop. `.published` only lets the finished
        // video be shown; the session keeps the Stop for every later revision.
        if published, identity.recipeRevision >= floor,
           identity == sourceIdentity || identity == acknowledgement || identity == observedIdentity || sameGeneration {
            return .published
        }
        return .hold
    }

    public func reportTarget(identity: DeviceRenderIdentity, baseGeneration currentGeneration: String?) -> DeviceRenderIdentity? {
        guard state == .stopped, sameVariant(identity) else { return nil }
        let floor = max(sourceIdentity.recipeRevision, acknowledgement?.recipeRevision ?? 0, observedIdentity?.recipeRevision ?? 0)
        guard identity.recipeRevision >= floor else { return nil }
        if identity == sourceIdentity || identity == acknowledgement || identity == observedIdentity { return identity }
        guard baseGeneration?.isEmpty == false, baseGeneration == currentGeneration else { return nil }
        return identity
    }

    private func sameVariant(_ identity: DeviceRenderIdentity) -> Bool {
        identity.jobID == sourceIdentity.jobID && identity.variantID == sourceIdentity.variantID
    }
}

public final class DeviceRenderIntentStore: @unchecked Sendable {
    public enum Failure: Error { case invalidOwner, invalidRecord }
    private struct Envelope: Codable {
        let ownerDigest: String
        let projectID: UUID
        let intent: DeviceRenderIntent
    }
    private let root: URL?
    private let lock = NSLock()
    private var memory: [DeviceRenderIntentKey: DeviceRenderIntent] = [:]

    /// A nil root is an isolated in-memory store for callers with no persistent account storage.
    public init(root: URL? = nil) { self.root = root }

    public func load(_ key: DeviceRenderIntentKey) throws -> DeviceRenderIntent? {
        try lock.withLock { try read(key) }
    }

    public func save(_ intent: DeviceRenderIntent, for key: DeviceRenderIntentKey) throws {
        try lock.withLock { try write(intent, key: key) }
    }

    @discardableResult public func update(_ key: DeviceRenderIntentKey, operationID: UUID,
                                          change: (inout DeviceRenderIntent) -> Void) throws -> Bool {
        try lock.withLock {
            guard var intent = try read(key), intent.operationID == operationID else { return false }
            change(&intent)
            try write(intent, key: key)
            return true
        }
    }

    public func removeAccount(owner: String) throws {
        try lock.withLock {
            guard !owner.isEmpty else { throw Failure.invalidOwner }
            if let root {
                let directory = root.appendingPathComponent(Self.digest(owner), isDirectory: true)
                if FileManager.default.fileExists(atPath: directory.path) { try FileManager.default.removeItem(at: directory) }
            }
            memory = memory.filter { $0.key.owner != owner }
        }
    }

    public func removeProject(owner: String, projectID: UUID) throws {
        try lock.withLock {
            guard !owner.isEmpty else { throw Failure.invalidOwner }
            if let root {
                let directory = root.appendingPathComponent(Self.digest(owner), isDirectory: true)
                    .appendingPathComponent(projectID.uuidString, isDirectory: true)
                if FileManager.default.fileExists(atPath: directory.path) { try FileManager.default.removeItem(at: directory) }
            }
            memory = memory.filter { $0.key.owner != owner || $0.key.projectID != projectID }
        }
    }

    private func read(_ key: DeviceRenderIntentKey) throws -> DeviceRenderIntent? {
        try validate(key)
        guard let root else { return memory[key] }
        let url = file(root: root, key: key)
        let data: Data
        do { data = try Data(contentsOf: url) }
        catch let error as CocoaError where error.code == .fileReadNoSuchFile { return nil }
        let envelope = try JSONDecoder().decode(Envelope.self, from: data)
        guard envelope.ownerDigest == Self.digest(key.owner), envelope.projectID == key.projectID else { throw Failure.invalidRecord }
        try validate(envelope.intent, key: key)
        return envelope.intent
    }

    private func write(_ intent: DeviceRenderIntent, key: DeviceRenderIntentKey) throws {
        try validate(key)
        try validate(intent, key: key)
        guard let root else { memory[key] = intent; return }
        let url = file(root: root, key: key)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try JSONEncoder().encode(Envelope(ownerDigest: Self.digest(key.owner), projectID: key.projectID, intent: intent))
        try data.write(to: url, options: .atomic)
        var excluded = url
        var values = URLResourceValues()
        values.isExcludedFromBackup = true
        try excluded.setResourceValues(values)
    }

    private func validate(_ key: DeviceRenderIntentKey) throws {
        guard !key.owner.isEmpty, !key.variantID.isEmpty else { throw Failure.invalidOwner }
    }

    private func validate(_ intent: DeviceRenderIntent, key: DeviceRenderIntentKey) throws {
        guard intent.version == 1 else { throw Failure.invalidRecord }
        for identity in [Optional(intent.sourceIdentity), intent.acknowledgement, intent.observedIdentity].compactMap({ $0 }) {
            guard identity.jobID == key.jobID, identity.variantID == key.variantID, identity.recipeRevision > 0,
                  identity.recipeRevision >= intent.sourceIdentity.recipeRevision else { throw Failure.invalidRecord }
        }
    }

    private func file(root: URL, key: DeviceRenderIntentKey) -> URL {
        root.appendingPathComponent(Self.digest(key.owner), isDirectory: true)
            .appendingPathComponent(key.projectID.uuidString, isDirectory: true)
            .appendingPathComponent(key.jobID.uuidString, isDirectory: true)
            .appendingPathComponent(Self.digest(key.variantID) + ".json")
    }

    private static func digest(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
