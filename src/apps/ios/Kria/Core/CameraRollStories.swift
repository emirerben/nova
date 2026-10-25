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
    var evidenceScope: String? = nil
    enum CodingKeys: String, CodingKey { case id, generation, kind, text, uncertain; case assetID = "asset_id", sourceID = "source_id", start = "start_s", end = "end_s", evidenceScope = "evidence_scope" }
}
struct CameraRollOpenQuestion: Codable, Identifiable, Sendable {
    let id: String
    let question: String
    var answer: String? = nil
    var dismissed: Bool
    var isResolved: Bool { answer != nil || dismissed }
}
struct CameraRollContentPlan: Codable, Sendable {
    let version: Int
    let format: String
    var form: String? = nil
    let audience: String
    let angle: String
    let whyItWorks: String
    var openingText: String
    var closingText: String?
    var caption: String
    let beatReasons: [String]
    var openQuestions: [CameraRollOpenQuestion]? = nil
    enum CodingKeys: String, CodingKey { case version, format, form, audience, angle, caption; case whyItWorks = "why_it_works", openingText = "opening_text", closingText = "closing_text", beatReasons = "beat_reasons", openQuestions = "open_questions" }
    var unresolvedQuestions: [CameraRollOpenQuestion] { (openQuestions ?? []).filter { !$0.isResolved } }
    /// A nil answer skips the question; the server keeps the question text itself.
    func resolving(_ id: String, answer: String?) -> CameraRollContentPlan {
        var revised = self
        revised.openQuestions = openQuestions?.map { value in
            guard value.id == id else { return value }
            var changed = value
            changed.answer = answer?.trimmingCharacters(in: .whitespacesAndNewlines)
            changed.dismissed = answer == nil
            return changed
        }
        return revised
    }
    var formLabel: String? {
        switch form ?? "" {
        case "guide": "Guide"
        case "factual_story": "Fact story"
        case "atmosphere": "Atmosphere"
        case "review": "Review"
        case "performance": "Performance"
        default: nil
        }
    }
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
    var contentPlan: CameraRollContentPlan? = nil
    var research: CameraRollStoryResearch? = nil
    enum CodingKeys: String, CodingKey { case id, revision, title, hook, payoff, rationale, moments, rejected, research; case duration = "duration_s", audioPolicy = "audio_policy", contentPlan = "content_plan" }
}
struct CameraRollStoryResearch: Codable, Sendable {
    let topic: String
    let reviewCurrent: Bool
    let evidence: Evidence
    enum CodingKeys: String, CodingKey { case topic, evidence; case reviewCurrent = "review_current" }
    struct Evidence: Codable, Sendable {
        let facts: [Fact]
        let retrievedAt: String
        let searchEntryHTML: String
        enum CodingKeys: String, CodingKey { case facts; case retrievedAt = "retrieved_at", searchEntryHTML = "search_entry_html" }
    }
    struct Fact: Codable, Identifiable, Sendable {
        let id: String
        let text: String
        let sources: [Source]
    }
    struct Source: Codable, Sendable {
        let title: String
        let url: String
    }
}
struct CameraRollStories: Codable, Sendable {
    let stories: [CameraRollStory]
    let coverage: Coverage?
    /// A public place the server noticed; a prefill the creator must confirm before anything is searched.
    var suggestedResearchTopic: String? = nil
    enum CodingKeys: String, CodingKey { case stories, coverage; case suggestedResearchTopic = "suggested_research_topic" }
    struct Coverage: Codable, Sendable {
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
    var unusableTiming: [UUID]? = nil
    enum CodingKeys: String, CodingKey { case nextOffset = "next_offset", missing = "missing_analysis_asset_ids", unavailable = "unavailable_asset_ids", untimed = "untimed_asset_ids", unusableTiming = "unusable_timing_asset_ids" }
}
struct CameraRollInspection: Decodable, Sendable { let answer: String; let confidence: Double; let evidence: String }

extension KriaAPIClient {
    func savedCameraRollStories(id: UUID, revision: Int, storyIDs: [UUID]) async throws -> CameraRollStories { throw APIError.unsupported }
    func updateCameraRollContentPlan(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, plan: CameraRollContentPlan) async throws -> CameraRollStory { throw APIError.unsupported }
    func changeCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, order: [UUID]?, audioPolicy: String?, eventID: String) async throws -> CameraRollStory { throw APIError.unsupported }
    func indexCameraRollCollection(id: UUID, revision: Int, offset: Int) async throws -> CameraRollIndexPass { throw APIError.unsupported }
    func cameraRollStories(id: UUID, revision: Int, query: String, cacheOnly: Bool) async throws -> CameraRollStories { throw APIError.unsupported }
    func cameraRollStories(id: UUID, revision: Int, query: String, cacheOnly: Bool, researchTopic: String?) async throws -> CameraRollStories {
        guard researchTopic == nil else { throw APIError.unsupported }
        return try await cameraRollStories(id: id, revision: revision, query: query, cacheOnly: cacheOnly)
    }
    func cameraRollStories(id: UUID, revision: Int, query: String, cacheOnly: Bool, researchTopic: String?, allowPartialAnalysis: Bool) async throws -> CameraRollStories {
        guard !allowPartialAnalysis else { throw APIError.unsupported }
        return try await cameraRollStories(id: id, revision: revision, query: query, cacheOnly: cacheOnly, researchTopic: researchTopic)
    }
    func recordCameraRollStoryExposure(collectionID: UUID, story: CameraRollStory, collectionRevision: Int) async throws { throw APIError.unsupported }
    func correctCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, index: Int, momentID: UUID, eventID: String) async throws -> CameraRollStory { throw APIError.unsupported }
    func rejectCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, eventID: String) async throws -> CameraRollStory { throw APIError.unsupported }
    func selectCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, threadID: UUID, threadRevision: Int, eventID: String) async throws -> TurnAccepted { throw APIError.unsupported }
    func recoverCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, threadID: UUID, threadRevision: Int, eventID: String, heldTurnID: UUID) async throws -> TurnAccepted { throw APIError.unsupported }
    func createCameraRollStoryThread(id: UUID, revision: Int) async throws -> CreationThread { throw APIError.unsupported }
    func inspectCameraRollMoment(collectionID: UUID, revision: Int, momentID: UUID, question: String, cacheOnly: Bool) async throws -> CameraRollInspection { throw APIError.unsupported }
}

extension KriaAPI {
    func savedCameraRollStories(id: UUID, revision: Int, storyIDs: [UUID]) async throws -> CameraRollStories {
        struct Saved: Decodable { let revision: Int; let stories: [CameraRollStory] }
        guard !storyIDs.isEmpty, storyIDs.count <= 20, Set(storyIDs).count == storyIDs.count else { throw APIError.invalidResponse }
        let query = [URLQueryItem(name: "expected_revision", value: String(revision))] + storyIDs.map { URLQueryItem(name: "story_ids", value: $0.uuidString) }
        let saved = try await request(path: "me/media-collections/\(id)/stories", method: "GET", query: query, bodyData: nil, decode: Saved.self)
        guard saved.revision == revision, saved.stories.map(\.id) == storyIDs else { throw APIError.invalidResponse }
        return CameraRollStories(stories: saved.stories, coverage: nil)
    }
    func updateCameraRollContentPlan(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, plan: CameraRollContentPlan) async throws -> CameraRollStory {
        var body = storyReviewBody(story, revision: collectionRevision, eventID: "wording-\(story.id)-\(story.revision)")
        body["content_plan"] = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(plan))
        return try await request(path: "me/media-collections/\(collectionID)/stories/\(story.id)", method: "PATCH", bodyData: JSONEncoder().encode(body), decode: CameraRollStory.self)
    }
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
        try await cameraRollStories(id: id, revision: revision, query: query, cacheOnly: cacheOnly, researchTopic: nil)
    }
    func cameraRollStories(id: UUID, revision: Int, query: String, cacheOnly: Bool, researchTopic: String?) async throws -> CameraRollStories {
        try await cameraRollStories(id: id, revision: revision, query: query, cacheOnly: cacheOnly, researchTopic: researchTopic, allowPartialAnalysis: false)
    }
    func cameraRollStories(id: UUID, revision: Int, query: String, cacheOnly: Bool, researchTopic: String?, allowPartialAnalysis: Bool) async throws -> CameraRollStories {
        var body: [String: JSONValue] = ["expected_revision": .number(Double(revision)), "query": .string(String(query.prefix(500))), "cache_only": .bool(cacheOnly), "allow_partial_analysis": .bool(allowPartialAnalysis)]
        if let researchTopic { body["research_topic"] = .string(String(researchTopic.prefix(200))) }
        let result = try await request(path: "me/media-collections/\(id)/stories", method: "POST", bodyData: JSONEncoder().encode(body), decode: CameraRollStories.self, timeoutInterval: researchTopic == nil ? 90 : 130)
        guard result.coverage != nil else { throw APIError.invalidResponse }
        return result
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
    func recoverCameraRollStory(collectionID: UUID, story: CameraRollStory, collectionRevision: Int, threadID: UUID, threadRevision: Int, eventID: String, heldTurnID: UUID) async throws -> TurnAccepted {
        var body = storyReviewBody(story, revision: collectionRevision, eventID: eventID)
        body["thread_id"] = .string(threadID.uuidString)
        body["expected_thread_revision"] = .number(Double(threadRevision))
        body["recover_held_turn_id"] = .string(heldTurnID.uuidString)
        let accepted = try await request(path: "me/media-collections/\(collectionID)/stories/\(story.id)/select", method: "POST", bodyData: JSONEncoder().encode(body), decode: TurnAccepted.self)
        guard let successorID = UUID(uuidString: accepted.turnID), successorID != heldTurnID else { throw APIError.invalidResponse }
        return accepted
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
