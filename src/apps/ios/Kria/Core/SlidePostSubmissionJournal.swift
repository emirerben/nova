import CryptoKit
import Foundation

struct SlidePostGenerationReceipt: Codable, Equatable, Sendable {
    enum DispatchState: String, Codable, Sendable { case unconfirmed, published; case publishFailed = "publish_failed" }
    var clientRequestID: UUID
    let itemID: UUID
    let draftVersion: Int
    let jobID: UUID
    let dispatchState: DispatchState
    let jobStatus: String?
    let isCurrentTarget: Bool
    enum CodingKeys: String, CodingKey {
        case clientRequestID = "client_request_id", itemID = "item_id", draftVersion = "draft_version"
        case jobID = "job_id", dispatchState = "dispatch_state", jobStatus = "job_status", isCurrentTarget = "is_current_target"
    }
}

struct SlidePostSubmissionScope: Codable, Equatable, Sendable {
    let owner: String
    let environment: String
    let itemID: String
}

struct SlidePostSubmissionRecord: Codable, Sendable {
    enum Phase: String, Codable, Sendable { case prepared, submitted, exhausted }

    let schemaVersion: Int
    let scope: SlidePostSubmissionScope
    let operationID: UUID
    let expectedVersion: Int
    let previousJobID: UUID?
    var phase: Phase
    var remainingReads: Int
    var observedActive: Bool
    var remainingPostAttempts = 3
    var nextReplayAt: Date?
    var readNotBefore: Date?
    var readFailureStatus: Int?
    var readFailureMessage: String?
    var receipt: SlidePostGenerationReceipt?

    init(scope: SlidePostSubmissionScope, expectedVersion: Int, previousJobID: UUID?, operationID: UUID = UUID()) {
        schemaVersion = 1
        self.scope = scope
        self.operationID = operationID
        self.expectedVersion = expectedVersion
        self.previousJobID = previousJobID
        phase = .prepared
        remainingReads = 200
        observedActive = false
    }
}

/// A synchronous reservation must succeed before a caller may send its operation ID.
/// Atomic replacement contains process death during a write; it is not a hardware power-loss guarantee.
final class SlidePostSubmissionJournal: @unchecked Sendable {
    enum Failure: Error { case invalidScope, invalidRecord, invalidTransition }
    struct Reservation {
        let record: SlidePostSubmissionRecord
        let inserted: Bool
    }
    struct IO: Sendable {
        let read: @Sendable (URL) throws -> Data?
        let write: @Sendable (Data, URL) throws -> Void
        let remove: @Sendable (URL) throws -> Void

        static let fileSystem = IO(
            read: { url in
                do { return try Data(contentsOf: url) }
                catch let error as CocoaError where error.code == .fileReadNoSuchFile { return nil }
            },
            write: { data, url in
                let directory = url.deletingLastPathComponent()
                try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
                var excluded = directory
                var attributes = URLResourceValues()
                attributes.isExcludedFromBackup = true
                try excluded.setResourceValues(attributes)
                try data.write(to: url, options: .atomic)
            },
            remove: { try FileManager.default.removeItem(at: $0) }
        )
    }

    // Shared across store instances: separate editor sessions cannot both reserve the same file.
    // App extensions do not share this app-private directory; multiprocess writers would need a file lock.
    private static let lock = NSLock()
    private let root: URL
    private let io: IO

    init(root: URL, io: IO = .fileSystem) {
        self.root = root.standardizedFileURL
        self.io = io
    }

    func aiJournal() -> SlidePostAIJournal { SlidePostAIJournal(root: root.appending(path: "AI"), io: io) }

    static func applicationStore() -> SlidePostSubmissionJournal {
        SlidePostSubmissionJournal(root: URL.applicationSupportDirectory.appending(path: "SlidePostSubmissions", directoryHint: .isDirectory))
    }

    static func authenticatedScope(api: any KriaAPIClient, itemID: String) throws -> SlidePostSubmissionScope {
        guard let api = api as? KriaAPI, let session = try api.tokenStore.read(),
              let claims = KriaAPI.jwtClaims(session.accessToken),
              let owner = ["sub", "user_id", "email"].compactMap({ claims[$0] as? String }).first,
              !owner.isEmpty else { throw APIError.sessionExpired }
        guard let item = UUID(uuidString: itemID),
              var components = URLComponents(url: api.baseURL.standardized, resolvingAgainstBaseURL: false) else { throw Failure.invalidScope }
        components.scheme = components.scheme?.lowercased()
        components.host = components.host?.lowercased()
        var environment = components.string ?? api.baseURL.standardized.absoluteString
        while environment.hasSuffix("/") { environment.removeLast() }
        return .init(owner: owner, environment: environment, itemID: item.uuidString.lowercased())
    }

    func load(_ scope: SlidePostSubmissionScope) throws -> SlidePostSubmissionRecord? {
        try Self.lock.withLock { try read(scope) }
    }

    func reserve(_ candidate: SlidePostSubmissionRecord) throws -> Reservation {
        try Self.lock.withLock {
            try validate(candidate, scope: candidate.scope)
            guard candidate.phase == .prepared, candidate.remainingReads == 200, !candidate.observedActive, candidate.remainingPostAttempts == 3, candidate.receipt == nil else {
                throw Failure.invalidTransition
            }
            if let existing = try read(candidate.scope) { return Reservation(record: existing, inserted: false) }
            try write(candidate)
            return Reservation(record: candidate, inserted: true)
        }
    }

    @discardableResult
    func update(_ scope: SlidePostSubmissionScope, operationID: UUID, change: (inout SlidePostSubmissionRecord) throws -> Void) throws -> Bool {
        try Self.lock.withLock {
            guard let previous = try read(scope), previous.operationID == operationID else { return false }
            var next = previous
            try change(&next)
            try validate(next, scope: scope)
            guard next.operationID == previous.operationID, next.expectedVersion == previous.expectedVersion,
                  next.previousJobID == previous.previousJobID, next.remainingReads <= previous.remainingReads,
                  next.remainingPostAttempts <= previous.remainingPostAttempts,
                  previous.receipt == nil || (next.receipt?.clientRequestID == previous.receipt?.clientRequestID &&
                      next.receipt?.itemID == previous.receipt?.itemID && next.receipt?.draftVersion == previous.receipt?.draftVersion &&
                      next.receipt?.jobID == previous.receipt?.jobID),
                  !previous.observedActive || next.observedActive,
                  previous.phase != .submitted || next.phase != .prepared,
                  previous.phase != .exhausted || next.phase == .exhausted else { throw Failure.invalidTransition }
            try write(next)
            return true
        }
    }

    @discardableResult
    func clear(_ scope: SlidePostSubmissionScope, operationID: UUID) throws -> Bool {
        try Self.lock.withLock {
            guard let current = try read(scope), current.operationID == operationID else { return false }
            try io.remove(file(scope))
            return true
        }
    }

    private func read(_ scope: SlidePostSubmissionScope) throws -> SlidePostSubmissionRecord? {
        try validate(scope)
        guard let data = try io.read(file(scope)) else { return nil }
        let record = try JSONDecoder().decode(SlidePostSubmissionRecord.self, from: data)
        try validate(record, scope: scope)
        return record
    }

    private func write(_ record: SlidePostSubmissionRecord) throws {
        try validate(record, scope: record.scope)
        try io.write(JSONEncoder().encode(record), file(record.scope))
    }

    private func validate(_ scope: SlidePostSubmissionScope) throws {
        guard !scope.owner.isEmpty, !scope.environment.isEmpty, !scope.itemID.isEmpty else { throw Failure.invalidScope }
    }

    private func validate(_ record: SlidePostSubmissionRecord, scope: SlidePostSubmissionScope) throws {
        try validate(scope)
        guard record.schemaVersion == 1, record.scope == scope, record.expectedVersion > 0,
              (0...200).contains(record.remainingReads), (0...3).contains(record.remainingPostAttempts),
              (record.phase == .exhausted) == (record.remainingReads == 0) else { throw Failure.invalidRecord }
        if let receipt = record.receipt {
            guard receipt.clientRequestID == record.operationID, receipt.itemID == UUID(uuidString: scope.itemID),
                  receipt.draftVersion == record.expectedVersion else { throw Failure.invalidRecord }
        }
    }

    private func file(_ scope: SlidePostSubmissionScope) -> URL {
        root.appending(path: Self.digest(scope.owner), directoryHint: .isDirectory)
            .appending(path: Self.digest(scope.environment), directoryHint: .isDirectory)
            .appending(path: Self.digest(scope.itemID) + ".json")
    }

    private static func digest(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}
