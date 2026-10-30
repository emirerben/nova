import Foundation

struct NativeActivity: Decodable, Identifiable, Sendable {
    enum Kind: String, Decodable, Sendable {
        case videoReady = "video_ready"
        case actionRequired = "action_required"
    }
    let id: UUID
    let threadID: UUID
    let kind: Kind
    let createdAt: Date
    let readAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, kind
        case threadID = "thread_id", createdAt = "created_at", readAt = "read_at"
    }

    var title: String {
        switch kind {
        case .videoReady: String(localized: "Export available")
        case .actionRequired: String(localized: "Your video needs attention")
        }
    }
    var detail: String {
        switch kind {
        case .videoReady: String(localized: "Open the project to view your export.")
        case .actionRequired: String(localized: "Open the project on your iPhone to continue.")
        }
    }
}

struct NativeActivityPage: Decodable, Sendable {
    let items: [NativeActivity]
    let nextBefore: String?
    enum CodingKeys: String, CodingKey { case items; case nextBefore = "next_before" }
}

extension KriaAPIClient {
    func nativeActivity(id: UUID, owner: String) async throws -> NativeActivity { throw APIError.unsupported }
    func nativeActivityProject(threadID: UUID, owner: String) async throws -> CreationThread { throw APIError.unsupported }
    func readNativeActivity(id: UUID, owner: String) async throws -> NativeActivity { throw APIError.unsupported }
    func nativeActivities(before: String? = nil) async throws -> NativeActivityPage { throw APIError.unsupported }
    func nativeActivity(id: UUID) async throws -> NativeActivity { throw APIError.unsupported }
    func readNativeActivity(id: UUID) async throws -> NativeActivity { throw APIError.unsupported }
}

extension KriaAPI {
    func nativeActivity(id: UUID, owner: String) async throws -> NativeActivity {
        try await request(path: "me/activities/\(id.uuidString)", method: "GET", bodyData: nil, decode: NativeActivity.self, expectedOwner: owner)
    }
    func nativeActivityProject(threadID: UUID, owner: String) async throws -> CreationThread {
        try await request(path: "creation-threads/\(threadID.uuidString)", method: "GET", query: [URLQueryItem(name: "projection", value: "full")], bodyData: nil, decode: CreationThread.self, expectedOwner: owner)
    }
    func readNativeActivity(id: UUID, owner: String) async throws -> NativeActivity {
        try await request(path: "me/activities/\(id.uuidString)/read", method: "POST", bodyData: nil, decode: NativeActivity.self, expectedOwner: owner)
    }
    func nativeActivities(before: String? = nil) async throws -> NativeActivityPage {
        let query = before.map { [URLQueryItem(name: "before", value: $0)] } ?? []
        return try await request(path: "me/activities", method: "GET", query: query, bodyData: nil, decode: NativeActivityPage.self)
    }
    func nativeActivity(id: UUID) async throws -> NativeActivity {
        try await request(path: "me/activities/\(id.uuidString)", method: "GET", bodyData: nil, decode: NativeActivity.self)
    }
    func readNativeActivity(id: UUID) async throws -> NativeActivity {
        try await request(path: "me/activities/\(id.uuidString)/read", method: "POST", bodyData: nil, decode: NativeActivity.self)
    }
}
