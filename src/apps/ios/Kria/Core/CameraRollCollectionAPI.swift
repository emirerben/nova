import Foundation
import UniformTypeIdentifiers
import KriaMediaEngine

struct SavedCameraRollCollection: Codable, Sendable, Identifiable {
    let id: UUID
    let revision: Int
    var name: String? = nil
    var members: [CameraRollCollectionMember]? = nil
}

struct CameraRollCollectionMember: Codable, Sendable, Equatable {
    let asset_id: UUID
    let source_id: UUID
    let group: String

    init(asset_id: UUID, source_id: UUID, group: String) {
        self.asset_id = asset_id; self.source_id = source_id; self.group = group
    }

    /// The server's `group` is nullable (`CollectionMember`); a member it holds without one is simply ungrouped.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        asset_id = try c.decode(UUID.self, forKey: .asset_id)
        source_id = try c.decode(UUID.self, forKey: .source_id)
        group = try c.decodeIfPresent(String.self, forKey: .group) ?? ""
    }
    private enum CodingKeys: String, CodingKey { case asset_id, source_id, group }
}

struct CameraRollCollectionSelection: Codable, Sendable, Equatable {
    let name: String
    let members: [CameraRollCollectionMember]
}

/// One event's ideas as last read from the server.
struct SavedCameraRollGroupIdeas: Codable, Sendable {
    var revision: Int
    var stories: [CameraRollStory]
    var query: String
    var researchTopic: String?
}

extension CameraRollCollection {
    /// The server never learns about an item that has not uploaded, so a collection saves only what is bound
    /// to a cloud source. Phone stories pin the analysis proxy; a cloud original is not interchangeable.
    var uploadedSelection: CameraRollCollectionSelection {
        var members: [CameraRollCollectionMember] = []
        let map = groupMap()
        for entry in entries {
            guard let asset = entry.savedAsset,
                  let source = asset.kind == "video" ? asset.bindableSource(for: .clip, destination: .phone) : asset.source else { continue }
            members.append(CameraRollCollectionMember(asset_id: asset.id, source_id: source.id, group: map[entry.id] ?? name))
        }
        return CameraRollCollectionSelection(name: name.trimmingCharacters(in: .whitespacesAndNewlines), members: members)
    }
    func members(inGroup group: String) -> [CameraRollCollectionMember] {
        uploadedSelection.members.filter { $0.group == group }
    }
}

/// Collection failures must not inherit the editor's generic conflict wording.
enum CameraRollCollectionFailure {
    static let unavailable = "This footage needs analysis. Your collection is saved; new analysis is unavailable right now."
    static let openQuestions = "Answer or skip this idea's questions before building it."
    static func isChanged(_ error: Error) -> Bool {
        guard let error = error as? APIError else { return false }
        return error.conflictCode == "collection_changed" || error.conflictDetail == "This collection changed. Refresh before continuing."
    }
    static func message(_ error: Error) -> String {
        guard let apiError = error as? APIError else { return error.localizedDescription }
        if isChanged(error) { return "Your collection was updated. Your local changes are saved. Review the latest collection to continue." }
        let reason: String
        if case let .requestFailed(_, detail) = apiError { reason = detail.code ?? detail.message ?? "" }
        else { reason = apiError.conflictCode ?? apiError.conflictDetail ?? "" }
        switch reason {
        case "collection_analysis_unavailable", "No eligible cached result; new model calls are disabled": return unavailable
        case "collection_analysis_pending", "Stories are pending; retry shortly": return "Your story ideas are still being prepared. Your collection is saved; try again shortly."
        case "story_open_questions": return openQuestions
        case "collection_media_unavailable", "Selected media is unavailable or its source changed. Remove or repair it.": return "Some footage is unavailable. Remove or restore it before finding ideas."
        default:
            if case .conflict = apiError { return "Could not finish finding ideas. Your collection is saved. Try again." }
            return apiError.localizedDescription
        }
    }
}

/// One row of `GET me/media-collections/{id}/items`. `state` and `reason` stay raw strings so a value this build
/// does not know never fails the whole snapshot; `itemState` maps them.
struct CameraRollServerItem: Decodable, Sendable, Equatable {
    let assetID: UUID
    let kind: String
    let group: String?
    let state: String
    let reason: String?
    let retryable: Bool
    let attempts: Int
    let updatedAt: Date?
    enum CodingKeys: String, CodingKey { case kind, group, state, reason, retryable, attempts; case assetID = "asset_id", updatedAt = "updated_at" }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        assetID = try c.decode(UUID.self, forKey: .assetID)
        kind = try c.decode(String.self, forKey: .kind)
        group = try c.decodeIfPresent(String.self, forKey: .group)
        state = try c.decode(String.self, forKey: .state)
        reason = try c.decodeIfPresent(String.self, forKey: .reason)
        retryable = try c.decodeIfPresent(Bool.self, forKey: .retryable) ?? false
        attempts = try c.decodeIfPresent(Int.self, forKey: .attempts) ?? 0
        updatedAt = try c.decodeIfPresent(Date.self, forKey: .updatedAt)
    }

    var itemState: CameraRollItemState {
        let why = CameraRollItemReason(serverValue: reason)
        switch state {
        case "queued": return .queued
        case "uploading": return .uploading(progress: 1)
        case "analyzing": return .analyzing
        case "ready": return .ready
        case "needs_retry": return .needsRetry(why)
        case "failed": return .failed(why)
        case "excluded": return .excluded(why)
        // A state from a newer server: settle it so polling stops, and let the creator retry.
        default: return .needsRetry(.providerError)
        }
    }
}

struct CameraRollItemsSnapshot: Decodable, Sendable {
    let revision: Int
    let items: [CameraRollServerItem]
}

/// The two calls the per-item engine needs; narrow so the poll loop is testable without the whole client.
protocol CameraRollItemsClient: Sendable {
    func cameraRollItems(id: UUID) async throws -> CameraRollItemsSnapshot
    func retrySavedMedia(id: UUID) async throws -> SavedMediaAsset
}

/// Body of `POST me/media-collections/{id}/stories`. `group` limits evidence to that event; `excludeStoryIDs`
/// are the event's existing ideas, so asking again adds one idea and never repeats or replaces.
struct CameraRollStoryRequest: Sendable, Equatable {
    var revision: Int
    var query: String
    var cacheOnly = false
    var researchTopic: String? = nil
    var allowPartialAnalysis = true
    var group: String? = nil
    var excludeStoryIDs: [UUID] = []
}

struct CameraRollStoryDeletion: Decodable, Sendable {
    let restoreUntil: Date?
    enum CodingKeys: String, CodingKey { case restoreUntil = "restore_until" }
}

struct CameraRollGroupDeletion: Decodable, Sendable {
    let revision: Int
    let deletedStoryIDs: [UUID]
    enum CodingKeys: String, CodingKey { case revision; case deletedStoryIDs = "deleted_story_ids" }
}

extension KriaAPIClient {
    func savedCameraRollStories(id: UUID, revision: Int, group: String) async throws -> [CameraRollStory] { throw APIError.unsupported }
    func cameraRollCollection(id: UUID) async throws -> SavedCameraRollCollection { throw APIError.unsupported }
    func importCollectionMedia(file: URL, kind: String) async throws -> SavedMediaAsset { throw APIError.unsupported }
    func importCollectionMedia(file: URL, kind: String, progress: (@Sendable (Double) -> Void)?) async throws -> SavedMediaAsset {
        try await importCollectionMedia(file: file, kind: kind)
    }
    func saveCameraRollCollection(_ collection: CameraRollCollection, revision: Int) async throws -> SavedCameraRollCollection { throw APIError.unsupported }
    func cameraRollItems(id: UUID) async throws -> CameraRollItemsSnapshot { throw APIError.unsupported }
    func generateCameraRollStories(id: UUID, request: CameraRollStoryRequest) async throws -> CameraRollStories { throw APIError.unsupported }
    func deleteCameraRollStory(collectionID: UUID, storyID: UUID) async throws -> CameraRollStoryDeletion { throw APIError.unsupported }
    func restoreCameraRollStory(collectionID: UUID, storyID: UUID) async throws { throw APIError.unsupported }
    func deleteCameraRollGroup(collectionID: UUID, group: String, expectedRevision: Int) async throws -> CameraRollGroupDeletion { throw APIError.unsupported }
    func replaceCameraRollMembers(collectionID: UUID, name: String, members: [CameraRollCollectionMember], expectedRevision: Int) async throws -> SavedCameraRollCollection { throw APIError.unsupported }
}

/// Reports `didSendBodyData` as a 0...1 fraction. URLSession holds its task delegate strongly for the task's life.
private final class UploadProgressDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let report: @Sendable (Double) -> Void
    init(_ report: @escaping @Sendable (Double) -> Void) { self.report = report }
    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64, totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        report(Double(totalBytesSent) / Double(totalBytesExpectedToSend))
    }
}

extension KriaAPI {
    func saveCameraRollCollection(_ collection: CameraRollCollection, revision: Int) async throws -> SavedCameraRollCollection {
        let selection = collection.uploadedSelection
        return try await replaceCameraRollMembers(collectionID: collection.id, name: selection.name, members: selection.members, expectedRevision: revision)
    }
    func replaceCameraRollMembers(collectionID: UUID, name: String, members: [CameraRollCollectionMember], expectedRevision: Int) async throws -> SavedCameraRollCollection {
        struct Body: Encodable { let name: String; let expected_revision: Int; let members: [CameraRollCollectionMember] }
        return try await request(path: "me/media-collections/\(collectionID)", method: "PUT",
                                 bodyData: JSONEncoder().encode(Body(name: name, expected_revision: expectedRevision, members: members)),
                                 decode: SavedCameraRollCollection.self)
    }
    func cameraRollItems(id: UUID) async throws -> CameraRollItemsSnapshot {
        try await request(path: "me/media-collections/\(id)/items", method: "GET", bodyData: nil, decode: CameraRollItemsSnapshot.self)
    }
    func generateCameraRollStories(id: UUID, request story: CameraRollStoryRequest) async throws -> CameraRollStories {
        // The server bounds these (`StoryGenerationBody`): at most 20 excluded ideas, a research topic of 4 to 200 characters.
        var body: [String: JSONValue] = ["expected_revision": .number(Double(story.revision)), "query": .string(String(story.query.prefix(500))),
                                         "cache_only": .bool(story.cacheOnly), "allow_partial_analysis": .bool(story.allowPartialAnalysis),
                                         "exclude_story_ids": .array(story.excludeStoryIDs.suffix(20).map { .string($0.uuidString) })]
        if let topic = story.researchTopic?.trimmingCharacters(in: .whitespacesAndNewlines), topic.count >= 4 { body["research_topic"] = .string(String(topic.prefix(200))) }
        if let group = story.group { body["group"] = .string(group) }
        return try await request(path: "me/media-collections/\(id)/stories", method: "POST", bodyData: JSONEncoder().encode(body),
                                 decode: CameraRollStories.self, timeoutInterval: story.researchTopic == nil ? 90 : 130)
    }
    /// The ideas the server holds for one event, newest first (`GET .../stories?group=`).
    func savedCameraRollStories(id: UUID, revision: Int, group: String) async throws -> [CameraRollStory] {
        struct Saved: Decodable { let stories: [CameraRollStory] }
        let query = [URLQueryItem(name: "expected_revision", value: String(revision)), URLQueryItem(name: "group", value: group)]
        return try await request(path: "me/media-collections/\(id)/stories", method: "GET", query: query, bodyData: nil, decode: Saved.self).stories
    }
    func deleteCameraRollStory(collectionID: UUID, storyID: UUID) async throws -> CameraRollStoryDeletion {
        try await request(path: "me/media-collections/\(collectionID)/stories/\(storyID)", method: "DELETE", bodyData: nil, decode: CameraRollStoryDeletion.self)
    }
    func restoreCameraRollStory(collectionID: UUID, storyID: UUID) async throws {
        let _: EmptyProjectResponse = try await request(path: "me/media-collections/\(collectionID)/stories/\(storyID)/restore", method: "POST", bodyData: nil, decode: EmptyProjectResponse.self)
    }
    func deleteCameraRollGroup(collectionID: UUID, group: String, expectedRevision: Int) async throws -> CameraRollGroupDeletion {
        let body: [String: JSONValue] = ["group": .string(group), "expected_revision": .number(Double(expectedRevision))]
        return try await request(path: "me/media-collections/\(collectionID)/groups", method: "DELETE", bodyData: JSONEncoder().encode(body), decode: CameraRollGroupDeletion.self)
    }
    func cameraRollCollection(id: UUID) async throws -> SavedCameraRollCollection {
        try await request(path: "me/media-collections/\(id)", method: "GET", bodyData: nil, decode: SavedCameraRollCollection.self)
    }

    /// Explicit import uses the existing reservation/finalize catalogue contract. Videos upload
    /// only a timeline-preserving proxy; the verified original stays in the account's local store.
    @MainActor func importCollectionMedia(file: URL, kind: String) async throws -> SavedMediaAsset {
        try await importCollectionMedia(file: file, kind: kind, progress: nil)
    }
    /// `progress` is the 0...1 fraction of the upload PUT only; hashing and proxy generation precede it.
    @MainActor func importCollectionMedia(file: URL, kind: String, progress: (@Sendable (Double) -> Void)?) async throws -> SavedMediaAsset {
        guard let owner = mediaLibraryOwner, ["video", "image"].contains(kind) else { throw APIError.unsupported }
        let fingerprint = try await Task.detached { try SHA256Fingerprinter().fingerprint(file: file) }.value
        let proxy = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
        defer { try? FileManager.default.removeItem(at: proxy) }
        let uploadFile: URL
        let contentType: String
        let contract: ProjectMediaUploadContract
        if kind == "video" {
            let result = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 640, height: 360)).makeProxy(for: file, destination: proxy)
            contract = try await ProjectMediaUploadContract.analysisProxy(original: file, proxy: result, fingerprint: fingerprint)
            uploadFile = proxy
            contentType = "video/mp4"
        } else {
            uploadFile = file
            contentType = UTType(filenameExtension: file.pathExtension)?.preferredMIMEType ?? "image/jpeg"
            contract = ProjectMediaUploadContract(purpose: .cloudRenderSource, proxy: nil)
        }
        try Task.checkCancellation()
        let uploaded = try await Task.detached { try SHA256Fingerprinter().fingerprint(file: uploadFile) }.value
        struct Body: Encodable {
            let client_upload_id: String; let filename: String; let content_type: String
            let file_size_bytes: Int64; let kind: String; let upload_contract: ProjectMediaUploadContract
        }
        let body = Body(client_upload_id: "ios-collection-\(uploaded.hex)", filename: String(file.lastPathComponent.suffix(255)),
                        content_type: contentType, file_size_bytes: uploaded.byteCount, kind: kind, upload_contract: contract)
        let target = try await request(path: "me/media-assets/uploads", method: "POST", bodyData: JSONEncoder().encode(body), decode: SavedMediaUpload.self)
        let asset: SavedMediaAsset
        if let url = target.uploadURL {
            var put = URLRequest(url: url)
            put.httpMethod = "PUT"
            put.setValue(contentType, forHTTPHeaderField: "Content-Type")
            for (key, value) in target.uploadHeaders { put.setValue(value, forHTTPHeaderField: key) }
            let delegate = progress.map { UploadProgressDelegate($0) }
            let (_, response) = try await session.upload(for: put, fromFile: uploadFile, delegate: delegate)
            guard let status = (response as? HTTPURLResponse)?.statusCode,
                  (200..<300).contains(status) || status == 412 else { throw APIError.invalidResponse }
            try Task.checkCancellation()
            let finalize: [String: JSONValue] = ["client_event_id": .string("ios-finalize-\(target.reservationID)")]
            asset = try await request(path: "me/media-assets/uploads/\(target.reservationID)/finalize", method: "POST", bodyData: JSONEncoder().encode(finalize), decode: SavedMediaAsset.self)
        } else if let id = target.assetID { asset = try await savedMediaAsset(id: id) }
        else { throw APIError.invalidResponse }
        guard mediaLibraryOwner == owner, asset.fingerprint == fingerprint else { throw SourceAssetError.invalidBinding }
        try await MediaLibraryOriginals(owner: owner).relink(assetID: asset.id, fingerprint: fingerprint, file: file)
        return asset
    }
}
