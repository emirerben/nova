import Foundation
import KriaMediaEngine

struct CreationCapabilities: Codable, Equatable, Sendable {
    let formats: [CreationFormatCapability]
    var media: [String: CreationMediaLimit]? = nil
    var runtimeVersions: [Int]? = nil
    var visualsEnabled: Bool? = nil
    var phoneRendering: PhoneRenderingCapabilities? = nil
    var preferredRuntimeVersion: Int { runtimeVersions?.contains(2) == true ? 2 : 1 }
    enum CodingKeys: String, CodingKey {
        case formats, media
        case runtimeVersions = "runtime_versions", visualsEnabled = "visuals_enabled"
        case phoneRendering = "phone_rendering"
    }
}

struct PhoneRenderingCapabilities: Codable, Equatable, Sendable {
    let enabled: Bool
    let recipeVersions: [Int]
    let verifiedFeatures: [String]
    enum CodingKeys: String, CodingKey {
        case enabled, recipeVersions = "recipe_versions", verifiedFeatures = "verified_features"
    }
    static let disabled = Self(enabled: false, recipeVersions: [], verifiedFeatures: [])
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
    var gcsPath: String? = nil
    var sourceURL: URL? = nil
    var durationS: Double? = nil
    var mediaStatus: String? = nil

    /// Older APIs return original videos in display_url. For images, accept
    /// that URL only when its key is the original, never a flattened preview.
    var originalMediaURL: URL? {
        if let sourceURL { return sourceURL }
        guard let displayURL else { return nil }
        if kind == "video" { return displayURL }
        guard let gcsPath, !gcsPath.isEmpty,
              displayURL.path.removingPercentEncoding?.hasSuffix("/" + gcsPath) == true else { return nil }
        return displayURL
    }
    enum CodingKeys: String, CodingKey {
        case id, kind, status, retryable
        case gcsPath = "gcs_path", sourceURL = "source_url", durationS = "duration_s", mediaStatus = "media_status"
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
    var uploadPurpose: String = UploadPurpose.cloudRenderSource.rawValue
    static func parse(_ state: [String: JSONValue]) -> [Self] {
        guard case .array(let media) = state["media"] else { return [] }
        return media.compactMap { entry in
            guard case .object(let fields) = entry, let id = fields["media_id"]?.stringValue else { return nil }
            let url = fields["poster_url"]?.stringValue ?? fields["thumbnail_url"]?.stringValue
            let purpose = fields["upload_contract"]?.objectValue?["purpose"]?.stringValue
                ?? (id.hasPrefix("analysis-proxy-") ? UploadPurpose.analysisProxy.rawValue : UploadPurpose.cloudRenderSource.rawValue)
            return Self(id: id, filename: fields["filename"]?.stringValue ?? "Attached media", kind: fields["kind"]?.stringValue ?? "video", previewURL: url.flatMap(URL.init(string:)), uploadPurpose: purpose)
        }
    }
}

/// Where the next attachment goes. `.phone` uploads a small analysis proxy and
/// keeps the original on this iPhone; `.cloud` uploads the full original.
/// Accounts that render on iPhone keep every project on iPhone (KRI-121):
/// Visuals upload in full to the plan item's pool (`.phoneVisuals`), limited to
/// the kinds this iPhone is verified to draw, and the iPhone downloads them
/// again to render. Nothing in Visuals can pull a project to the cloud.
enum ProjectUploadDestination: Equatable {
    case phone, cloud, phoneVisuals(Set<VisualMediaKind>), checking, paused, mixed, visualsUnavailableOnPhone, voiceoverUnavailableOnPhone
    var canUpload: Bool {
        switch self {
        case .phone, .cloud, .phoneVisuals: true
        case .checking, .paused, .mixed, .visualsUnavailableOnPhone, .voiceoverUnavailableOnPhone: false
        }
    }
    /// Visuals kinds the pickers may offer; nil means every kind (cloud projects).
    var visualKinds: Set<VisualMediaKind>? { if case .phoneVisuals(let kinds) = self { kinds } else { nil } }
    var message: String? {
        switch self {
        case .phone, .cloud: nil
        case .phoneVisuals(let kinds):
            kinds == [.image] ? "Add photos here, and videos in Footage. Kria renders your video on this iPhone."
                : kinds == [.video] ? "Add supporting videos here. Kria renders your video on this iPhone."
                : "Add photos or supporting videos here. Kria renders your video on this iPhone."
        case .checking: "Checking how this project renders…"
        case .paused: "Rendering on iPhone is temporarily unavailable. Your project is saved; try again later."
        case .mixed: "This project has sources from different rendering destinations. Keep the project and reconnect its original footage before continuing."
        case .visualsUnavailableOnPhone: "Visuals aren’t available yet for videos rendered on iPhone. Continue with your footage; Kria renders it on this iPhone."
        case .voiceoverUnavailableOnPhone: "Voiceover isn’t available yet for videos rendered on iPhone. Your project is saved."
        }
    }

    /// Photos render on iPhone once the server verifies `stillImages`, Visuals
    /// videos once it verifies `visualVideos`. Until the account's capabilities
    /// have loaded nothing uploads: guessing `.cloud` would send full originals
    /// to the cloud and lock an iPhone account's project there.
    static func resolve(capabilities: PhoneRenderingCapabilities?, capabilitiesLoaded: Bool = true, sourcePurposes: [String], role: CreationMediaRole) -> Self {
        let known = Set(sourcePurposes)
        let phone = UploadPurpose.analysisProxy.rawValue, cloud = UploadPurpose.cloudRenderSource.rawValue
        guard known.isSubset(of: [phone, cloud]), known.count <= 1 else { return .mixed }
        if known.contains(cloud) { return .cloud }
        guard capabilitiesLoaded else { return .checking }
        let minimum = Set(["basicComposition", "positionedText", "audioMix", "local1080Export"])
        let verified = Set(capabilities?.verifiedFeatures ?? [])
        let available = capabilities?.enabled == true && capabilities?.recipeVersions.contains(2) == true
            && minimum.isSubset(of: verified)
        if known.contains(phone) {
            guard available else { return .paused }
        } else if !available {
            return .cloud
        }
        switch role {
        case .clip: return .phone
        case .visual:
            var kinds: Set<VisualMediaKind> = []
            if verified.contains(MediaCapability.stillImages.rawValue) { kinds.insert(.image) }
            if verified.contains(MediaCapability.visualVideos.rawValue) { kinds.insert(.video) }
            return kinds.isEmpty ? .visualsUnavailableOnPhone : .phoneVisuals(kinds)
        // Narration has no on-device path yet, and this account renders on
        // iPhone: say so rather than start a project the cloud would render.
        case .voiceover: return .voiceoverUnavailableOnPhone
        }
    }

    /// Footage and voiceover are project media; Visuals live in the pool and never
    /// decide the destination, so a pending Visuals upload can neither make a
    /// phone project `.mixed` nor pull an empty project to the cloud.
    static func sourcePurposes(media: [CreationAttachedMedia], records: [UploadRecoveryRecord], projectID: UUID) -> [String] {
        media.map(\.uploadPurpose) + records.filter { $0.projectID == projectID && $0.role != .visual }.map(\.purpose.rawValue)
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

    /// Runtime-v2 equivalent of `awaitsCreationConfirmation`: an approval was
    /// requested and nothing terminal (approved/denied/cancelled/expired) has
    /// followed it yet. Computed straight from the event log so it's available
    /// synchronously wherever a full `CreationThread` projection is in hand —
    /// unlike the view's `synchronizeApproval()`, it doesn't confirm the
    /// approval hasn't since expired, so it's a good-enough signal for status
    /// labels, not a substitute for the authoritative live check.
    var hasPendingApprovalRequest: Bool {
        guard runtimeVersion == 2 else { return false }
        let terminalTypes: Set<String> = ["approval_approved", "approval_denied", "approval_cancelled", "approval_expired"]
        guard let lastRequest = events.last(where: { $0.eventType == "approval_requested" }) else { return false }
        return !events.contains(where: { $0.sequence > lastRequest.sequence && terminalTypes.contains($0.eventType) })
    }

    /// True while a newly proposed plan hasn't been confirmed or declined yet,
    /// across both the runtime-v1 (`creatorAgent.status`) and runtime-v2
    /// (`approval_requested` events) mechanisms. This can be true even when
    /// ``ProjectSummary/status`` still reports `.ready`/`.failed` from the last
    /// minted job — the two are independent axes.
    var awaitsNewPlanConfirmation: Bool { awaitsCreationConfirmation || hasPendingApprovalRequest }
}

extension JSONValue {
    var objectValue: [String: JSONValue]? { if case .object(let value) = self { value } else { nil } }
    var booleanValue: Bool? { if case .bool(let value) = self { value } else { nil } }
}
