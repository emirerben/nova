import CryptoKit
import Foundation

enum SlidePostAIKind: String, Codable, CaseIterable, Sendable { case propose; case chatEdit = "chat_edit" }
struct SlidePostAIRequest: Codable, Sendable {
    let clientRequestID: UUID
    let projectID: UUID
    let kind: SlidePostAIKind
    let input: JSONValue
    var deliveryVersion: Int? = nil
    enum CodingKeys: String, CodingKey { case clientRequestID = "client_request_id", projectID = "project_id", kind, input; case deliveryVersion = "delivery_version" }
}
struct SlidePostAIReceipt: Codable, Equatable, Sendable {
    enum Status: String, Codable, Sendable { case pending, running, completed, unknown, rejected }
    let clientRequestID: UUID
    let itemID: UUID
    let projectID: UUID
    let kind: SlidePostAIKind
    let inputDigest: String
    let input: JSONValue
    let status: Status
    let result: JSONValue?
    var deliveryVersion: Int? = nil
    enum CodingKeys: String, CodingKey {
        case clientRequestID = "client_request_id", itemID = "item_id", projectID = "project_id"
        case inputDigest = "input_digest", input, kind, status, result
        case deliveryVersion = "delivery_version"
    }
}
struct SlidePostAIRecord: Codable, Sendable {
    let scope: SlidePostSubmissionScope
    let projectID: UUID
    let kind: SlidePostAIKind
    let input: JSONValue
    let baseline: SlidePostDraft?
    let operationID: UUID
    var deliveryVersion: Int? = nil
    var remainingReads = 40
    var remainingPosts = 3
    var notBefore: Date?
    var slowReadNotBefore: Date?
    var serverNotBefore: Date?
    var receipt: SlidePostAIReceipt?
    var delivered = false
    var request: SlidePostAIRequest { .init(clientRequestID: operationID, projectID: projectID, kind: kind, input: input, deliveryVersion: deliveryVersion) }
}

final class SlidePostAIJournal: @unchecked Sendable {
    enum Failure: Error { case staleIntent, invalidRecord, paused }
    private static let lock = NSLock()
    private let root: URL
    private let io: SlidePostSubmissionJournal.IO
    init(root: URL, io: SlidePostSubmissionJournal.IO = .fileSystem) { self.root = root; self.io = io }
    private func url(_ scope: SlidePostSubmissionScope, _ projectID: UUID, _ kind: SlidePostAIKind) throws -> URL {
        let data = try JSONEncoder().encode([scope.owner, scope.environment, scope.itemID.lowercased(), projectID.uuidString])
        let name = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        return root.appending(path: name + ".json")
    }
    private func read(_ scope: SlidePostSubmissionScope, _ projectID: UUID, _ kind: SlidePostAIKind) throws -> SlidePostAIRecord? {
        guard let data = try io.read(url(scope, projectID, kind)) else { return nil }
        let value = try JSONDecoder().decode(SlidePostAIRecord.self, from: data)
        guard value.scope == scope, value.projectID == projectID else { throw Failure.invalidRecord }
        return value.kind == kind ? value : nil
    }
    private func localURL(_ scope: SlidePostSubmissionScope, _ projectID: UUID?) throws -> URL {
        if let projectID { return try url(scope, projectID, .propose).appendingPathExtension("local") }
        let data = try JSONEncoder().encode([scope.owner, scope.environment, scope.itemID.lowercased(), "manual-only"])
        let name = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        return root.appending(path: name + ".local")
    }
    func readLocal(scope: SlidePostSubmissionScope, projectID: UUID?) throws -> Data? {
        Self.lock.lock(); defer { Self.lock.unlock() }
        return try io.read(localURL(scope, projectID))
    }
    func writeLocal(_ data: Data, scope: SlidePostSubmissionScope, projectID: UUID?) throws {
        Self.lock.lock(); defer { Self.lock.unlock() }
        try io.write(data, localURL(scope, projectID))
    }
    func load(scope: SlidePostSubmissionScope, projectID: UUID, kind: SlidePostAIKind) throws -> SlidePostAIRecord? {
        Self.lock.lock(); defer { Self.lock.unlock() }
        return try read(scope, projectID, kind)
    }
    /// Only an explicit NEW user action may replace the slot. Recovery uses update/CAS.
    func reserve(_ record: SlidePostAIRecord) throws {
        Self.lock.lock(); defer { Self.lock.unlock() }
        try io.write(JSONEncoder().encode(record), url(record.scope, record.projectID, record.kind))
    }
    func update(_ candidate: SlidePostAIRecord) throws {
        Self.lock.lock(); defer { Self.lock.unlock() }
        guard let current = try read(candidate.scope, candidate.projectID, candidate.kind),
              current.operationID == candidate.operationID, current.input == candidate.input, current.baseline == candidate.baseline, current.deliveryVersion == candidate.deliveryVersion else { throw Failure.staleIntent }
        var record = candidate
        record.remainingReads = min(current.remainingReads, record.remainingReads)
        record.remainingPosts = min(current.remainingPosts, record.remainingPosts)
        record.delivered = current.delivered || record.delivered
        if let deadline = current.slowReadNotBefore, record.slowReadNotBefore.map({ $0 < deadline }) ?? true { record.slowReadNotBefore = deadline }
        if let deadline = current.notBefore, record.notBefore.map({ $0 < deadline }) ?? true { record.notBefore = deadline }
        if let deadline = current.serverNotBefore, record.serverNotBefore.map({ $0 < deadline }) ?? true { record.serverNotBefore = deadline }
        if current.receipt?.status == .completed { record.receipt = current.receipt }
        try io.write(JSONEncoder().encode(record), url(record.scope, record.projectID, record.kind))
    }
    /// Cross-session pacing and budgets are reserved atomically before network I/O.
    func reserveAttempt(_ original: SlidePostAIRecord, post: Bool, explicit: Bool, now: Date, ownReadDeadline: Date? = nil) throws -> SlidePostAIRecord {
        Self.lock.lock(); defer { Self.lock.unlock() }
        guard var record = try read(original.scope, original.projectID, original.kind), record.operationID == original.operationID else { throw Failure.staleIntent }
        if let deadline = record.serverNotBefore, deadline > now { throw Failure.paused }
        if let deadline = record.notBefore, deadline > now, deadline != ownReadDeadline { throw Failure.paused }
        if post {
            if record.deliveryVersion == 1 {
                guard record.remainingPosts == 3, record.receipt == nil else { throw Failure.paused }
            }
            guard record.receipt == nil || record.receipt?.status == .pending,
                  record.remainingPosts > 0 || explicit else { throw Failure.paused }
            record.remainingPosts = max(0, record.remainingPosts - 1)
        } else {
            if record.remainingReads == 0 && !explicit {
                if let deadline = record.slowReadNotBefore, deadline > now { throw Failure.paused }
                record.slowReadNotBefore = now.addingTimeInterval(30)
            }
            record.remainingReads = max(0, record.remainingReads - 1)
        }
        record.notBefore = now.addingTimeInterval(3)
        try io.write(JSONEncoder().encode(record), url(record.scope, record.projectID, record.kind))
        return record
    }

}

extension KriaAPI {
    static func slidePostOwnerHash(_ subject: String) -> String { SHA256.hash(data: Data(subject.utf8)).map { String(format: "%02x", $0) }.joined() }
    func submitSlidePostAI(itemID: String, body: SlidePostAIRequest, expectedOwner: String) async throws -> SlidePostAIReceipt {
        try await request(path: "plan-items/\(itemID)/slide-post/ai-requests", method: "POST", bodyData: JSONEncoder().encode(body), decode: SlidePostAIReceipt.self, expectedOwner: Self.slidePostOwnerHash(expectedOwner))
    }
    func slidePostAIReceipt(itemID: String, requestID: UUID, expectedOwner: String) async throws -> SlidePostAIReceipt {
        try await request(path: "plan-items/\(itemID)/slide-post/ai-requests/\(requestID.uuidString)", method: "GET", bodyData: nil, decode: SlidePostAIReceipt.self, expectedOwner: Self.slidePostOwnerHash(expectedOwner))
    }
}
