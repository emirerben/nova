import Foundation

struct CameraRollMoment: Codable, Identifiable, Sendable {
    let id: UUID
    let assetID: UUID
    let sourceID: UUID
    let generation: String
    let start: Double
    let end: Double
    let kind: String
    let text: String
    let uncertain: Bool
    enum CodingKeys: String, CodingKey { case id, generation, kind, text, uncertain; case assetID = "asset_id", sourceID = "source_id", start = "start_s", end = "end_s" }
}
struct CameraRollStory: Codable, Identifiable, Sendable {
    let id: UUID
    let revision: Int
    let title: String
    let hook: String
    let payoff: String
    let rationale: String
    let duration: Double
    let moments: [CameraRollMoment]
    let rejected: Bool
    let audioPolicy: String
    enum CodingKeys: String, CodingKey { case id, revision, title, hook, payoff, rationale, moments, rejected; case duration = "duration_s", audioPolicy = "audio_policy" }
}
struct CameraRollStories: Decodable, Sendable {
    let stories: [CameraRollStory]
    let coverage: Coverage
    struct Coverage: Decodable, Sendable {
        let moments: [CameraRollMoment]
        let incompleteSpeech: [UUID]
        let unindexed: [UUID]
        enum CodingKeys: String, CodingKey { case moments; case incompleteSpeech = "incomplete_speech_asset_ids", unindexed = "unindexed_asset_ids" }
    }
}
struct CameraRollIndexPass: Decodable, Sendable {
    let nextOffset: Int?
    let missing: [UUID]
    let unavailable: [UUID]
    let untimed: [UUID]
    enum CodingKeys: String, CodingKey { case nextOffset = "next_offset", missing = "missing_analysis_asset_ids", unavailable = "unavailable_asset_ids", untimed = "untimed_asset_ids" }
}
struct CameraRollInspection: Decodable, Sendable { let answer: String; let confidence: Double; let evidence: String }

extension KriaAPIClient {
    func changeCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, order: [UUID]?, audioPolicy: String?, eventID: String) async throws -> CameraRollStory { throw APIError.unsupported }
    func indexCameraRollCollection(id: UUID, revision: Int, offset: Int) async throws -> CameraRollIndexPass { throw APIError.unsupported }
    func cameraRollStories(id: UUID, revision: Int, query: String, cacheOnly: Bool) async throws -> CameraRollStories { throw APIError.unsupported }
    func recordCameraRollStoryExposure(collectionID: UUID, story: CameraRollStory, collectionRevision: Int) async throws { throw APIError.unsupported }
    func correctCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, index: Int, momentID: UUID, eventID: String) async throws -> CameraRollStory { throw APIError.unsupported }
    func rejectCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, eventID: String) async throws -> CameraRollStory { throw APIError.unsupported }
    func selectCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, threadID: UUID, threadRevision: Int, eventID: String) async throws -> TurnAccepted { throw APIError.unsupported }
    func createCameraRollStoryThread(id: UUID, revision: Int) async throws -> CreationThread { throw APIError.unsupported }
    func inspectCameraRollMoment(collectionID: UUID, revision: Int, momentID: UUID, question: String, cacheOnly: Bool) async throws -> CameraRollInspection { throw APIError.unsupported }
}

extension KriaAPI {
    func changeCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, order: [UUID]?, audioPolicy: String?, eventID: String) async throws -> CameraRollStory {
        var body = storyReviewBody(story, revision: collectionRevision, eventID: eventID)
        if let order { body["moment_order"] = .array(order.map { .string($0.uuidString) }) }
        if let audioPolicy { body["audio_policy"] = .string(audioPolicy) }
        return try await request(path: "me/media-collections/\(collectionID)/stories/\(story.id)", method: "PATCH", bodyData: JSONEncoder().encode(body), decode: CameraRollStory.self)
    }
    func indexCameraRollCollection(id: UUID, revision: Int, offset: Int) async throws -> CameraRollIndexPass {
        let body: [String: JSONValue] = ["expected_revision": .number(Double(revision)), "offset": .number(Double(offset))]
        return try await request(path: "me/media-collections/\(id)/index", method: "POST", bodyData: JSONEncoder().encode(body), decode: CameraRollIndexPass.self)
    }
    func cameraRollStories(id: UUID, revision: Int, query: String, cacheOnly: Bool) async throws -> CameraRollStories {
        let body: [String: JSONValue] = ["expected_revision": .number(Double(revision)), "query": .string(String(query.prefix(500))), "cache_only": .bool(cacheOnly)]
        return try await request(path: "me/media-collections/\(id)/stories", method: "POST", bodyData: JSONEncoder().encode(body), decode: CameraRollStories.self, timeoutInterval: 90)
    }
    func correctCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, index: Int, momentID: UUID, eventID: String) async throws -> CameraRollStory {
        var body = storyReviewBody(story, revision: collectionRevision, eventID: eventID)
        body["replace_index"] = .number(Double(index)); body["moment_id"] = .string(momentID.uuidString)
        return try await request(path: "me/media-collections/\(collectionID)/stories/\(story.id)", method: "PATCH", bodyData: JSONEncoder().encode(body), decode: CameraRollStory.self)
    }
    func rejectCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, eventID: String) async throws -> CameraRollStory {
        try await request(path: "me/media-collections/\(collectionID)/stories/\(story.id)/reject", method: "POST", bodyData: JSONEncoder().encode(storyReviewBody(story, revision: collectionRevision, eventID: eventID)), decode: CameraRollStory.self)
    }
    func selectCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, threadID: UUID, threadRevision: Int, eventID: String) async throws -> TurnAccepted {
        var body = storyReviewBody(story, revision: collectionRevision, eventID: eventID)
        body["thread_id"] = .string(threadID.uuidString); body["expected_thread_revision"] = .number(Double(threadRevision))
        return try await request(path: "me/media-collections/\(collectionID)/stories/\(story.id)/select", method: "POST", bodyData: JSONEncoder().encode(body), decode: TurnAccepted.self)
    }
    func createCameraRollStoryThread(id: UUID, revision: Int) async throws -> CreationThread {
        let capabilities = try await creationCapabilities()
        guard capabilities.runtimeVersions?.contains(2) == true, capabilities.mediaCollections == true else { throw APIError.unsupported }
        return try await request(path: "creation-threads", method: "POST", bodyData: JSONEncoder().encode(["client_event_id": JSONValue.string("camera-roll-\(id)-\(revision)"), "runtime_version": .number(2)]), decode: CreationThread.self)
    }
    func recordCameraRollStoryExposure(collectionID: UUID, story: CameraRollStory, collectionRevision: Int) async throws {
        let _: CameraRollStory = try await request(path: "me/media-collections/\(collectionID)/stories/\(story.id)/expose", method: "POST", bodyData: JSONEncoder().encode(storyReviewBody(story, revision: collectionRevision, eventID: "shown-\(story.id)-\(story.revision)")), decode: CameraRollStory.self)
    }
    func inspectCameraRollMoment(collectionID: UUID, revision: Int, momentID: UUID, question: String, cacheOnly: Bool) async throws -> CameraRollInspection {
        let body: [String: JSONValue] = ["expected_revision": .number(Double(revision)), "moment_id": .string(momentID.uuidString), "question": .string(String(question.prefix(200))), "cache_only": .bool(cacheOnly)]
        return try await request(path: "me/media-collections/\(collectionID)/inspect", method: "POST", bodyData: JSONEncoder().encode(body), decode: CameraRollInspection.self, timeoutInterval: 60)
    }
    private func storyReviewBody(_ story: CameraRollStory, revision: Int, eventID: String) -> [String: JSONValue] {
        ["expected_revision": .number(Double(revision)), "expected_story_revision": .number(Double(story.revision)), "client_event_id": .string(eventID)]
    }
}
