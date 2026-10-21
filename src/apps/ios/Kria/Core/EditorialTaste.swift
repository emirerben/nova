import Foundation

struct EditorialTasteExample: Decodable, Identifiable, Sendable {
    let id: UUID
    let projectID: UUID
    let title: String
    let kind: String
    let contextualNote: String?
    enum CodingKeys: String, CodingKey {
        case id, title, kind
        case projectID = "project_id", contextualNote = "creator_editorial_note"
    }
}
struct EditorialTasteProfile: Decodable, Sendable {
    let revision: Int
    let enabled: Bool
    let memoryPaused: Bool
    let rules: [String]
    let examples: [EditorialTasteExample]
    enum CodingKeys: String, CodingKey {
        case revision, enabled, rules, examples
        case memoryPaused = "memory_paused"
    }
}
struct EditorialTasteWrite: Encodable, Sendable {
    let expected_revision: Int
    let idempotency_key: String
    let project_ids: [UUID]
    let rules: [String]
    let project_notes: [String: String]
    let confirmed = true
    let enabled: Bool
}
struct IdeaFeedbackWrite: Encodable, Sendable {
    let collection_id: UUID
    let story_id: UUID
    let expected_story_revision: Int
    let client_event_id: String
    let verdict: String
    let reasons: [String]
    let note: String
    let rewrite: String
}
struct IdeaFeedbackReceipt: Decodable, Sendable { let id: UUID; let recorded: Bool }

extension KriaAPIClient {
    func editorialTaste() async throws -> EditorialTasteProfile { throw APIError.unsupported }
    func saveEditorialTaste(_ body: EditorialTasteWrite) async throws -> EditorialTasteProfile { throw APIError.unsupported }
    func forgetEditorialTaste(revision: Int, eventID: String) async throws -> EditorialTasteProfile { throw APIError.unsupported }
    func recordIdeaFeedback(_ body: IdeaFeedbackWrite) async throws -> IdeaFeedbackReceipt { throw APIError.unsupported }
}
extension KriaAPI {
    func editorialTaste() async throws -> EditorialTasteProfile {
        try await request(path: "me/editorial-taste", method: "GET", bodyData: nil, decode: EditorialTasteProfile.self)
    }
    func saveEditorialTaste(_ body: EditorialTasteWrite) async throws -> EditorialTasteProfile {
        try await request(path: "me/editorial-taste", method: "PUT", bodyData: JSONEncoder().encode(body), decode: EditorialTasteProfile.self)
    }
    func forgetEditorialTaste(revision: Int, eventID: String) async throws -> EditorialTasteProfile {
        let body: [String: JSONValue] = ["expected_revision": .number(Double(revision)), "idempotency_key": .string(eventID)]
        return try await request(path: "me/editorial-taste", method: "DELETE", bodyData: JSONEncoder().encode(body), decode: EditorialTasteProfile.self)
    }
    func recordIdeaFeedback(_ body: IdeaFeedbackWrite) async throws -> IdeaFeedbackReceipt {
        try await request(path: "me/editorial-taste/feedback", method: "POST", bodyData: JSONEncoder().encode(body), decode: IdeaFeedbackReceipt.self)
    }
}
