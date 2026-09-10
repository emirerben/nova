import Foundation

struct CreationCapabilities: Codable, Equatable, Sendable {
    let formats: [CreationFormatCapability]
    var media: [String: CreationMediaLimit]? = nil
    var runtimeVersions: [Int]? = nil
    var visualsEnabled: Bool? = nil
    var preferredRuntimeVersion: Int { runtimeVersions?.contains(2) == true ? 2 : 1 }
    enum CodingKeys: String, CodingKey {
        case formats, media
        case runtimeVersions = "runtime_versions", visualsEnabled = "visuals_enabled"
    }
}

struct CreationMediaLimit: Codable, Equatable, Sendable {
    let max: Int
    let maxFileBytes: JSONValue
    let contentTypes: [String]
    enum CodingKeys: String, CodingKey { case max; case maxFileBytes = "max_file_bytes", contentTypes = "content_types" }
    func byteLimit(contentType: String) -> Int64? {
        if case .number(let value) = maxFileBytes { return Int64(value) }
        if case .object(let values) = maxFileBytes,
           case .number(let value) = values[contentType.hasPrefix("image/") ? "image" : "video"] { return Int64(value) }
        return nil
    }
}

enum CreationMediaRole: String, Codable, CaseIterable, Sendable {
    case clip, visual, voiceover
    var title: String { switch self { case .clip: "Footage"; case .visual: "Visuals"; case .voiceover: "Voiceover" } }
    var capabilityKey: String { switch self { case .clip: "clips"; case .visual: "visuals"; case .voiceover: "voiceover" } }
    func accepts(_ contentType: String) -> Bool {
        switch self {
        case .clip: contentType.hasPrefix("video/")
        case .visual: contentType.hasPrefix("image/") || contentType.hasPrefix("video/")
        case .voiceover: contentType.hasPrefix("audio/")
        }
    }
}

struct CreationVisual: Codable, Sendable, Identifiable {
    let id: String
    let kind: String
    let status: String
    let sourceFilename: String?
    let displayURL: URL?
    let previewURL: URL?
    let retryable: Bool?
    enum CodingKeys: String, CodingKey {
        case id, kind, status, retryable
        case sourceFilename = "source_filename", displayURL = "display_url", previewURL = "preview_url"
    }
}
struct CreationVisuals: Codable, Sendable {
    let assets: [CreationVisual]
    let maxAssets: Int
    let occupiedAssets: Int?
    var activeReservations: [Reservation]? = nil
    struct Reservation: Codable, Sendable {
        let reservationID: String
        enum CodingKeys: String, CodingKey { case reservationID = "reservation_id" }
    }
    enum CodingKeys: String, CodingKey {
        case assets
        case maxAssets = "max_assets", occupiedAssets = "occupied_assets", activeReservations = "active_reservations"
    }
}
struct VisualUploadTarget: Codable, Sendable {
    let reservationID: String
    let uploadURL: URL
    let gcsPath: String
    let uploadHeaders: [String: String]
    enum CodingKeys: String, CodingKey {
        case reservationID = "reservation_id", uploadURL = "upload_url", gcsPath = "gcs_path", uploadHeaders = "upload_headers"
    }
}

extension KriaAPIClient {
    func sendCreationMessage(threadID: UUID, message: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread { throw APIError.unsupported }
    func creationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int, clientActionID: String) async throws -> CreationThread {
        try await applyCreationAction(threadID: threadID, action: action, payload: payload, expectedRevision: expectedRevision)
    }
    func reserveVisualUpload(itemID: String, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> VisualUploadTarget { throw APIError.unsupported }
    func registerVisual(itemID: String, reservationID: String, gcsPath: String, contentType: String, filename: String) async throws -> CreationVisual { throw APIError.unsupported }
    func visuals(itemID: String) async throws -> CreationVisuals { throw APIError.unsupported }
    func removeVisual(itemID: String, assetID: String) async throws { throw APIError.unsupported }
    func retryVisual(itemID: String, assetID: String) async throws -> CreationVisual { throw APIError.unsupported }
}

extension KriaAPI {
    func sendCreationMessage(threadID: UUID, message: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread {
        try await request(path: "creation-threads/\(threadID.uuidString)/messages", method: "POST", bodyData: JSONEncoder().encode([
            "message": JSONValue.string(message), "expected_revision": .number(Double(expectedRevision)), "client_event_id": .string(clientEventID)
        ]), decode: CreationThread.self)
    }
    func creationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int, clientActionID: String) async throws -> CreationThread {
        try await request(path: "creation-threads/\(threadID.uuidString)/actions", method: "POST", bodyData: JSONEncoder().encode([
            "action": JSONValue.string(action), "payload": .object(payload), "expected_revision": .number(Double(expectedRevision)), "client_action_id": .string(clientActionID)
        ]), decode: CreationThread.self)
    }
    func reserveVisualUpload(itemID: String, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> VisualUploadTarget {
        struct Response: Decodable { let urls: [VisualUploadTarget] }
        let body: [String: JSONValue] = ["files": .array([.object([
            "filename": .string(filename), "content_type": .string(contentType), "file_size_bytes": .number(Double(size)), "client_upload_id": .string(clientUploadID)
        ])])]
        let response = try await request(path: "plan-items/\(itemID)/assets/upload-urls", method: "POST", bodyData: JSONEncoder().encode(body), decode: Response.self)
        guard let target = response.urls.first else { throw APIError.invalidResponse }
        return target
    }
    func registerVisual(itemID: String, reservationID: String, gcsPath: String, contentType: String, filename: String) async throws -> CreationVisual {
        try await request(path: "plan-items/\(itemID)/assets", method: "POST", bodyData: JSONEncoder().encode([
            "reservation_id": reservationID, "gcs_path": gcsPath, "content_type": contentType, "source_filename": filename
        ]), decode: CreationVisual.self)
    }
    func visuals(itemID: String) async throws -> CreationVisuals {
        try await request(path: "plan-items/\(itemID)/assets", method: "GET", bodyData: nil, decode: CreationVisuals.self)
    }
    func removeVisual(itemID: String, assetID: String) async throws {
        let _: [String: JSONValue] = try await request(path: "plan-items/\(itemID)/assets/\(assetID)", method: "DELETE", bodyData: nil, decode: [String: JSONValue].self)
    }
    func retryVisual(itemID: String, assetID: String) async throws -> CreationVisual {
        try await request(path: "plan-items/\(itemID)/assets/\(assetID)/reanalyze", method: "POST", bodyData: nil, decode: CreationVisual.self)
    }
}

struct CreationAttachedMedia: Identifiable {
    let id: String
    let filename: String
    let kind: String
    let previewURL: URL?
    static func parse(_ state: [String: JSONValue]) -> [Self] {
        guard case .array(let media) = state["media"] else { return [] }
        return media.compactMap { entry in
            guard case .object(let fields) = entry, let id = fields["media_id"]?.stringValue else { return nil }
            let url = fields["poster_url"]?.stringValue ?? fields["thumbnail_url"]?.stringValue
            return Self(id: id, filename: fields["filename"]?.stringValue ?? "Attached media", kind: fields["kind"]?.stringValue ?? "video", previewURL: url.flatMap(URL.init(string:)))
        }
    }
}

struct CreationActionIdentity: Equatable {
    let id: String
    let action: String
    let payload: [String: JSONValue]
    let revision: Int
    static func reusing(_ pending: Self?, action: String, payload: [String: JSONValue], revision: Int) -> Self {
        if let pending, pending.action == action, pending.payload == payload { return pending }
        return Self(id: UUID().uuidString, action: action, payload: payload, revision: revision)
    }
}

extension CreationThread {
    var awaitsCreationConfirmation: Bool {
        runtimeVersion == 1 && creatorAgent?["status"]?.stringValue == "awaiting_confirmation"
    }
}

extension JSONValue {
    var objectValue: [String: JSONValue]? { if case .object(let value) = self { value } else { nil } }
    var booleanValue: Bool? { if case .bool(let value) = self { value } else { nil } }
}
