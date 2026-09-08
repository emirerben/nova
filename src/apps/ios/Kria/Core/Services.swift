import Foundation
import Security
import AuthenticationServices
import Photos
import CryptoKit
import UIKit
import OpenAPIRuntime
import OpenAPIURLSession

extension Notification.Name {
    static let kriaSessionExpired = Notification.Name("kria.session-expired")
}

enum KriaEnvironment: Sendable {
    case development, staging, production
    static var current: Self {
        #if DEBUG
        return .development
        #else
        return .production
        #endif
    }
    var baseURL: URL {
        switch self { case .development: URL(string: "http://localhost:8000")!; case .staging: URL(string: "https://staging.usekria.com")!; case .production: URL(string: "https://nova-video.fly.dev")! }
    }
}

/// Runtime configuration is deliberately small. Authentication and rendering
/// credentials are never read from source or bundled into the application.
struct AppConfiguration: Sendable {
    let environment: KriaEnvironment
    let apiBaseURL: URL
    let allowsDevelopmentAuth: Bool
    static let current: Self = {
        let environment = KriaEnvironment.current
        let configuredURL = (Bundle.main.object(forInfoDictionaryKey: "KriaAPIBaseURL") as? String)
            .flatMap(URL.init(string:))
        #if DEBUG
        return Self(environment: environment, apiBaseURL: configuredURL ?? environment.baseURL, allowsDevelopmentAuth: true)
        #else
        return Self(environment: environment, apiBaseURL: configuredURL ?? environment.baseURL, allowsDevelopmentAuth: false)
        #endif
    }()
}

protocol TokenStore: Sendable {
    func read() throws -> MobileSession?
    func write(_ session: MobileSession) throws
    func delete() throws
}

struct MobileSession: Codable, Sendable, Equatable {
    let accessToken: String
    let refreshToken: String
    let expiresIn: Int
    enum CodingKeys: String, CodingKey { case accessToken = "access_token"; case refreshToken = "refresh_token"; case expiresIn = "expires_in" }
}

struct KeychainTokenStore: TokenStore, @unchecked Sendable {
    let service: String
    let account: String
    init(service: String = "com.kria.auth", account: String = "access-token") { self.service = service; self.account = account }
    func read() throws -> MobileSession? {
        var query = baseQuery; query[kSecReturnData as String] = true; query[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?; let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }; guard status == errSecSuccess, let data = item as? Data else { throw KeychainError(status) }
        return try JSONDecoder().decode(MobileSession.self, from: data)
    }
    func write(_ session: MobileSession) throws {
        let data = try JSONEncoder().encode(session); let status = SecItemUpdate(baseQuery as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound { var query = baseQuery; query[kSecValueData as String] = data; let add = SecItemAdd(query as CFDictionary, nil); guard add == errSecSuccess else { throw KeychainError(add) } }
        else if status != errSecSuccess { throw KeychainError(status) }
    }
    func delete() throws { let status = SecItemDelete(baseQuery as CFDictionary); guard status == errSecSuccess || status == errSecItemNotFound else { throw KeychainError(status) } }
    private var baseQuery: [String: Any] { [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service, kSecAttrAccount as String: account] }
}
struct KeychainError: Error, LocalizedError { let status: OSStatus; init(_ status: OSStatus) { self.status = status }; var errorDescription: String? { "Secure sign-in storage is unavailable." } }

protocol KriaAPIClient: Sendable {
    func projects() async throws -> [ProjectSummary]
    func project(threadID: UUID) async throws -> CreationThread
    func creationCapabilities() async throws -> CreationCapabilities
    func library() async throws -> [ProjectSummary]
    func createThread(message: String?) async throws -> CreationThread
    func exchangeMobileToken(_ credential: AuthCredential, provider: String) async throws -> MobileSession
    func refreshMobileSession(_ refreshToken: String) async throws -> MobileSession
    func revokeMobileSession(_ refreshToken: String) async throws
    func submitTurn(threadID: UUID, message: String, expectedRevision: Int) async throws -> TurnAccepted
    func applyCreationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int) async throws -> CreationThread
    func threadDelta(threadID: UUID, afterSequence: Int) async throws -> ThreadDelta
    func draft(threadID: UUID) async throws -> DraftSnapshot
    func writeDraft(threadID: UUID, snapshot: [String: JSONValue], expectedRevision: Int, etag: String) async throws -> DraftSnapshot
    func openJobInEditor(jobID: UUID) async throws -> OpenInEditorResponse
    func editorVariants(jobID: UUID) async throws -> [[String: JSONValue]]
    func editorVariant(jobID: UUID, variantID: String) async throws -> [String: JSONValue]
    func editorCommit(itemID: String, variantID: String, request: EditorCommitRequest) async throws -> EditorCommitResponse
    func undoDraft(threadID: UUID, expectedRevision: Int) async throws -> DraftSnapshot
    func approval(threadID: UUID, approvalID: UUID) async throws -> ApprovalSnapshot
    func decideApproval(threadID: UUID, approvalID: UUID, decision: String, expectedThreadRevision: Int, expectedDraftRevision: Int, fingerprint: String) async throws
    func playbackURL(jobID: UUID) async throws -> URL
    func editRecipe(jobID: UUID, variantID: String?) async throws -> EditRecipe
    func reserveUpload(filename: String, contentType: String, size: Int64, purpose: UploadPurpose) async throws -> UploadReservation
    func cancelUpload(reservationID: UUID) async throws
    func reserveProjectUpload(threadID: UUID, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> ProjectUploadReservation
    func attachProjectMedia(threadID: UUID, mediaID: String, gcsPath: String, filename: String, contentType: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread
}

/// Existing API test doubles can remain focused on the older protocol. Native
/// editor saves fail explicitly when the production commit endpoint is not
/// implemented by a substitute.
extension KriaAPIClient {
    func creationCapabilities() async throws -> CreationCapabilities { throw APIError.unsupported }

    func openJobInEditor(jobID: UUID) async throws -> OpenInEditorResponse {
        _ = jobID
        throw APIError.unsupported
    }

    func editorVariant(jobID: UUID, variantID: String) async throws -> [String: JSONValue] {
        _ = jobID; _ = variantID
        throw APIError.unsupported
    }

    func editorVariants(jobID: UUID) async throws -> [[String: JSONValue]] {
        _ = jobID
        throw APIError.unsupported
    }

    func editorCommit(itemID: String, variantID: String, request: EditorCommitRequest) async throws -> EditorCommitResponse {
        _ = itemID; _ = variantID; _ = request
        throw APIError.unsupported
    }
}

struct TurnAccepted: Codable, Sendable { let turnID: String; let threadRevision: Int; let status: String; enum CodingKeys: String, CodingKey { case turnID = "turn_id"; case threadRevision = "thread_revision"; case status } }
struct CreationFormatCapability: Codable, Equatable, Sendable {
    let id: String
    let editFormat: String
    let maxClips: Int
    enum CodingKeys: String, CodingKey { case id; case editFormat = "edit_format"; case maxClips = "max_clips" }
}
struct CreationCapabilities: Codable, Equatable, Sendable { let formats: [CreationFormatCapability] }
struct CreationThread: Codable, Identifiable, Sendable {
    let id: String
    let title: String
    let status: String
    let revision: Int
    let runtimeVersion: Int
    let activeJobID: String?
    let activePlanItemID: String?
    let job: CreationJob?
    let updatedAt: Date
    let state: [String: JSONValue]?
    /// Full projections include the conversation transcript. List summaries
    /// from older servers may omit it, so decoding treats a missing value as
    /// an empty transcript.
    let events: [ThreadEvent]
    enum CodingKeys: String, CodingKey { case id, title, status, revision, job, state, events; case runtimeVersion = "runtime_version"; case activeJobID = "active_job_id"; case activePlanItemID = "active_plan_item_id"; case updatedAt = "updated_at" }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        title = try values.decode(String.self, forKey: .title)
        status = try values.decode(String.self, forKey: .status)
        revision = try values.decode(Int.self, forKey: .revision)
        runtimeVersion = try values.decodeIfPresent(Int.self, forKey: .runtimeVersion) ?? 1
        activeJobID = try values.decodeIfPresent(String.self, forKey: .activeJobID)
        activePlanItemID = try values.decodeIfPresent(String.self, forKey: .activePlanItemID)
        job = try values.decodeIfPresent(CreationJob.self, forKey: .job)
        updatedAt = try values.decode(Date.self, forKey: .updatedAt)
        state = try values.decodeIfPresent([String: JSONValue].self, forKey: .state)
        events = try values.decodeIfPresent([ThreadEvent].self, forKey: .events) ?? []
    }

    var summary: ProjectSummary {
        let playableVariant = selectedPlayableVariant
        let outputVariantID: String?
        if let playableVariant {
            outputVariantID = playableVariant.variantID
        } else {
            outputVariantID = selectedVariantID
        }
        return ProjectSummary(
            id: UUID(uuidString: id) ?? UUID(),
            title: title,
            status: projectStatus,
            updatedAt: updatedAt,
            posterURL: playableVariant?.posterURL,
            outputURL: playableVariant?.outputURL,
            outputVariantID: outputVariantID,
            runtimeVersion: runtimeVersion,
            serverRevision: revision,
            activeJobID: activeJobID.flatMap(UUID.init(uuidString:)),
            activePlanItemID: activePlanItemID
        )
    }

    /// List projections intentionally omit media URLs, but their state and
    /// variant lifecycle still identify the edit the user selected.
    private var selectedVariantID: String? {
        let variants = job?.variants ?? []
        if let selectedID = state?["selected_variant_id"]?.stringValue,
           variants.contains(where: { $0.variantID == selectedID }) {
            return selectedID
        }
        return variants.first {
            guard let status = $0.renderStatus?.lowercased() else { return false }
            return status == "ready" && $0.variantID != nil
        }?.variantID
    }

    /// Prefer the server-selected variant, but only when it has media. A
    /// selected variant can be absent from list projections' URL-free payload;
    /// in that case, fall back to the first playable variant in the full
    /// projection. Ready and terminal-failure variants may carry a last-good
    /// URL; an in-flight variant must not displace the currently playable cut.
    private var selectedPlayableVariant: CreationVariant? {
        let variants = job?.variants ?? []
        if let selectedID = state?["selected_variant_id"]?.stringValue,
           let selected = variants.first(where: { $0.variantID == selectedID && $0.isPlayable }) {
            return selected
        }
        return variants.first(where: \.isPlayable)
    }
    private var projectStatus: ProjectStatus {
        guard activeJobID != nil else { return .draft }
        guard let status = job?.status.lowercased() else { return .rendering }
        if status == "ready" || status == "done" || status.contains("_ready") { return .ready }
        if status == "failed" || status == "cancelled" || status == "no_labeled_tracks" || status.contains("_failed") { return .failed }
        return .rendering
    }
}
struct CreationVariant: Codable, Sendable {
    let variantID: String?
    let renderStatus: String?
    let renderGenerationID: String?
    let renderFinishedAt: String?
    let outputURL: URL?
    let posterURL: URL?
    let failureReason: String?

    var isPlayable: Bool {
        guard outputURL != nil, let status = renderStatus?.lowercased() else { return false }
        return ["ready", "failed", "error", "render_failed"].contains(status)
    }

    enum CodingKeys: String, CodingKey {
        case variantID = "variant_id"
        case renderStatus = "render_status"
        case renderGenerationID = "render_generation_id"
        case renderFinishedAt = "render_finished_at"
        case outputURL = "output_url"
        case posterURL = "poster_url"
        case failureReason = "failure_reason"
    }
}

struct CreationJob: Codable, Sendable {
    let id: String
    let status: String
    let currentPhase: String?
    let failureReason: String?
    let variants: [CreationVariant]

    enum CodingKeys: String, CodingKey {
        case id, status, variants
        case currentPhase = "current_phase"
        case failureReason = "failure_reason"
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(String.self, forKey: .id)
        status = try values.decode(String.self, forKey: .status)
        currentPhase = try values.decodeIfPresent(String.self, forKey: .currentPhase)
        failureReason = try values.decodeIfPresent(String.self, forKey: .failureReason)
        variants = try values.decodeIfPresent([CreationVariant].self, forKey: .variants) ?? []
    }
}
struct ThreadDelta: Codable, Sendable { let threadID: String; let runtimeVersion: Int; let status: String; let threadRevision: Int; let events: [ThreadEvent]; let afterSequence: Int; let nextAfterSequence: Int; let hasMore: Bool; enum CodingKeys: String, CodingKey { case threadID = "thread_id"; case runtimeVersion = "runtime_version"; case status; case threadRevision = "thread_revision"; case events; case afterSequence = "after_sequence"; case nextAfterSequence = "next_after_sequence"; case hasMore = "has_more" } }
struct ThreadEvent: Codable, Identifiable, Sendable { let id: String; let sequence: Int; let revision: Int; let role: String; let eventType: String; let content: String?; let payload: [String: JSONValue]?; let createdAt: Date; enum CodingKeys: String, CodingKey { case id, sequence, revision, role, content, payload; case eventType = "event_type"; case createdAt = "created_at" } }
struct DraftSnapshot: Codable, Sendable {
    let draftID: String
    let itemID: String
    let variantKey: String
    let draftRevision: Int
    let snapshotHash: String
    let etag: String
    let baseJobID: String?
    let baseGenerationID: String?
    let snapshot: [String: JSONValue]
    let canUndo: Bool
    let createdAt: Date
    enum CodingKeys: String, CodingKey { case snapshot, etag; case draftID = "draft_id"; case itemID = "item_id"; case variantKey = "variant_key"; case draftRevision = "draft_revision"; case snapshotHash = "snapshot_hash"; case baseJobID = "base_job_id"; case baseGenerationID = "base_generation_id"; case canUndo = "can_undo"; case createdAt = "created_at" }
}

struct OpenInEditorResponse: Codable, Sendable, Equatable {
    let planItemID: String
    let variantID: String
    private enum CodingKeys: String, CodingKey {
        case planItemID = "plan_item_id"
        case variantID = "variant_id"
    }
}

enum EditorMusicAlignment: String, Codable, Equatable, Sendable {
    case preserveCuts = "preserve_cuts"
    case resyncBeats = "resync_beats"
}

struct EditorCommitMusicWindow: Codable, Equatable, Sendable {
    var startS: Double
    var alignment: EditorMusicAlignment
    init(startS: Double, alignment: EditorMusicAlignment = .preserveCuts) { self.startS = startS; self.alignment = alignment }
    private enum CodingKeys: String, CodingKey { case startS = "start_s"; case alignment }
}

struct EditorCommitBackgroundMusic: Codable, Equatable, Sendable {
    var trackID: String?
    var enabled: Bool
    var startS: Double?
    var endS: Double?
    var gainDB: Double?
    var muted: Bool
    init(trackID: String? = nil, enabled: Bool = true, startS: Double? = nil, endS: Double? = nil, gainDB: Double? = nil, muted: Bool = false) {
        self.trackID = trackID; self.enabled = enabled; self.startS = startS; self.endS = endS; self.gainDB = gainDB; self.muted = muted
    }
    private enum CodingKeys: String, CodingKey { case trackID = "track_id"; case enabled; case startS = "start_s"; case endS = "end_s"; case gainDB = "gain_db"; case muted }
}

struct EditorCommitLyrics: Codable, Equatable, Sendable {
    var enabled: Bool?
    var lineOverrides: [String: JSONValue]?
    var lineOverridesPatch: EditorCommitLyricsOverridesPatch
    init(enabled: Bool? = nil, lineOverrides: [String: JSONValue]? = nil, lineOverridesPatch: EditorCommitLyricsOverridesPatch = .omitted) {
        self.enabled = enabled; self.lineOverrides = lineOverrides; self.lineOverridesPatch = lineOverridesPatch
    }
    private enum CodingKeys: String, CodingKey { case enabled; case lineOverrides = "line_overrides" }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(enabled, forKey: .enabled)
        switch lineOverridesPatch {
        case .omitted: try c.encodeIfPresent(lineOverrides, forKey: .lineOverrides)
        case .remove: try c.encodeNil(forKey: .lineOverrides)
        case let .replace(value): try c.encode(value, forKey: .lineOverrides)
        }
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        enabled = try c.decodeIfPresent(Bool.self, forKey: .enabled)
        if !c.contains(.lineOverrides) {
            lineOverrides = nil; lineOverridesPatch = .omitted
        } else if try c.decodeNil(forKey: .lineOverrides) {
            lineOverrides = nil; lineOverridesPatch = .remove
        } else {
            let value = try c.decode([String: JSONValue].self, forKey: .lineOverrides)
            lineOverrides = value; lineOverridesPatch = .replace(value)
        }
    }
}

/// Tri-state patch for the nullable lyrics override map. The backend uses
/// Pydantic field presence to distinguish omitted (leave untouched) from null
/// (clear all overrides), so an optional dictionary alone is insufficient.
enum EditorCommitLyricsOverridesPatch: Equatable, Sendable {
    case omitted
    case remove
    case replace([String: JSONValue])
}

enum EditorCarouselMomentPatch: Equatable, Sendable {
    case omitted
    case remove
    case replace([String: JSONValue])
}

struct EditorCommitRequest: Codable, Sendable {
    // Optionals are intentional: nil means “section omitted/untouched”, while
    // [] means “replace this section with empty”.
    var timelineSlots: [JSONValue]?
    var textElements: [JSONValue]?
    var captionCues: [JSONValue]?
    var captionMeta: [String: JSONValue]?
    var mix: [String: JSONValue]?
    var musicTrackID: String?
    var removeMusic: Bool
    var musicWindow: EditorCommitMusicWindow?
    var backgroundMusic: EditorCommitBackgroundMusic?
    var lyrics: EditorCommitLyrics?
    var orientation: String?
    var soundEffects: [JSONValue]?
    var mediaOverlays: [JSONValue]?
    var visualBlocks: [JSONValue]?
    var motionScenes: [JSONValue]?
    var motionRuntimeHash: String?
    var cameraEffects: [JSONValue]?
    var carouselMoment: EditorCarouselMomentPatch
    var title: String?
    var acceptedSuggestionIDs: [String]?
    var copilotReceiptIDs: [UUID]
    var guidedRevision: [String: JSONValue]?
    var guidedRevisionNumber: Int?
    var retryGuidedRevision: Bool
    var baseGeneration: String

    init(timelineSlots: [JSONValue]? = nil, textElements: [JSONValue]? = nil, captionCues: [JSONValue]? = nil, captionMeta: [String: JSONValue]? = nil, mix: [String: JSONValue]? = nil, musicTrackID: String? = nil, removeMusic: Bool = false, musicWindow: EditorCommitMusicWindow? = nil, backgroundMusic: EditorCommitBackgroundMusic? = nil, lyrics: EditorCommitLyrics? = nil, orientation: String? = nil, soundEffects: [JSONValue]? = nil, mediaOverlays: [JSONValue]? = nil, visualBlocks: [JSONValue]? = nil, motionScenes: [JSONValue]? = nil, motionRuntimeHash: String? = nil, cameraEffects: [JSONValue]? = nil, carouselMoment: EditorCarouselMomentPatch = .omitted, title: String? = nil, acceptedSuggestionIDs: [String]? = nil, copilotReceiptIDs: [UUID] = [], guidedRevision: [String: JSONValue]? = nil, guidedRevisionNumber: Int? = nil, retryGuidedRevision: Bool = false, baseGeneration: String) {
        self.timelineSlots = timelineSlots; self.textElements = textElements; self.captionCues = captionCues; self.captionMeta = captionMeta; self.mix = mix; self.musicTrackID = musicTrackID; self.removeMusic = removeMusic; self.musicWindow = musicWindow; self.backgroundMusic = backgroundMusic; self.lyrics = lyrics; self.orientation = orientation; self.soundEffects = soundEffects; self.mediaOverlays = mediaOverlays; self.visualBlocks = visualBlocks; self.motionScenes = motionScenes; self.motionRuntimeHash = motionRuntimeHash; self.cameraEffects = cameraEffects; self.carouselMoment = carouselMoment; self.title = title; self.acceptedSuggestionIDs = acceptedSuggestionIDs; self.copilotReceiptIDs = copilotReceiptIDs; self.guidedRevision = guidedRevision; self.guidedRevisionNumber = guidedRevisionNumber; self.retryGuidedRevision = retryGuidedRevision; self.baseGeneration = baseGeneration
    }

    private enum CodingKeys: String, CodingKey { case timelineSlots = "timeline_slots"; case textElements = "text_elements"; case captionCues = "caption_cues"; case captionMeta = "caption_meta"; case mix; case musicTrackID = "music_track_id"; case removeMusic = "remove_music"; case musicWindow = "music_window"; case backgroundMusic = "background_music"; case lyrics; case orientation; case soundEffects = "sound_effects"; case mediaOverlays = "media_overlays"; case visualBlocks = "visual_blocks"; case motionScenes = "motion_scenes"; case motionRuntimeHash = "motion_runtime_hash"; case cameraEffects = "camera_effects"; case carouselMoment = "carousel_moment"; case title; case acceptedSuggestionIDs = "accepted_suggestion_ids"; case copilotReceiptIDs = "copilot_receipt_ids"; case guidedRevision = "guided_revision"; case guidedRevisionNumber = "guided_revision_number"; case retryGuidedRevision = "retry_guided_revision"; case baseGeneration = "base_generation" }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encodeIfPresent(timelineSlots, forKey: .timelineSlots); try c.encodeIfPresent(textElements, forKey: .textElements); try c.encodeIfPresent(captionCues, forKey: .captionCues); try c.encodeIfPresent(captionMeta, forKey: .captionMeta); try c.encodeIfPresent(mix, forKey: .mix); try c.encodeIfPresent(musicTrackID, forKey: .musicTrackID); try c.encode(removeMusic, forKey: .removeMusic); try c.encodeIfPresent(musicWindow, forKey: .musicWindow); try c.encodeIfPresent(backgroundMusic, forKey: .backgroundMusic); try c.encodeIfPresent(lyrics, forKey: .lyrics); try c.encodeIfPresent(orientation, forKey: .orientation); try c.encodeIfPresent(soundEffects, forKey: .soundEffects); try c.encodeIfPresent(mediaOverlays, forKey: .mediaOverlays); try c.encodeIfPresent(visualBlocks, forKey: .visualBlocks); try c.encodeIfPresent(motionScenes, forKey: .motionScenes); try c.encodeIfPresent(motionRuntimeHash, forKey: .motionRuntimeHash); try c.encodeIfPresent(cameraEffects, forKey: .cameraEffects)
        switch carouselMoment { case .omitted: break; case .remove: try c.encodeNil(forKey: .carouselMoment); case let .replace(value): try c.encode(value, forKey: .carouselMoment) }
        try c.encodeIfPresent(title, forKey: .title)
        try c.encodeIfPresent(acceptedSuggestionIDs, forKey: .acceptedSuggestionIDs); if !copilotReceiptIDs.isEmpty { try c.encode(copilotReceiptIDs, forKey: .copilotReceiptIDs) }; try c.encodeIfPresent(guidedRevision, forKey: .guidedRevision); try c.encodeIfPresent(guidedRevisionNumber, forKey: .guidedRevisionNumber); if retryGuidedRevision { try c.encode(retryGuidedRevision, forKey: .retryGuidedRevision) }; try c.encode(baseGeneration, forKey: .baseGeneration)
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        timelineSlots = try c.decodeIfPresent([JSONValue].self, forKey: .timelineSlots); textElements = try c.decodeIfPresent([JSONValue].self, forKey: .textElements); captionCues = try c.decodeIfPresent([JSONValue].self, forKey: .captionCues); captionMeta = try c.decodeIfPresent([String: JSONValue].self, forKey: .captionMeta); mix = try c.decodeIfPresent([String: JSONValue].self, forKey: .mix); musicTrackID = try c.decodeIfPresent(String.self, forKey: .musicTrackID); removeMusic = try c.decodeIfPresent(Bool.self, forKey: .removeMusic) ?? false; musicWindow = try c.decodeIfPresent(EditorCommitMusicWindow.self, forKey: .musicWindow); backgroundMusic = try c.decodeIfPresent(EditorCommitBackgroundMusic.self, forKey: .backgroundMusic); lyrics = try c.decodeIfPresent(EditorCommitLyrics.self, forKey: .lyrics); orientation = try c.decodeIfPresent(String.self, forKey: .orientation); soundEffects = try c.decodeIfPresent([JSONValue].self, forKey: .soundEffects); mediaOverlays = try c.decodeIfPresent([JSONValue].self, forKey: .mediaOverlays); visualBlocks = try c.decodeIfPresent([JSONValue].self, forKey: .visualBlocks); motionScenes = try c.decodeIfPresent([JSONValue].self, forKey: .motionScenes); motionRuntimeHash = try c.decodeIfPresent(String.self, forKey: .motionRuntimeHash); cameraEffects = try c.decodeIfPresent([JSONValue].self, forKey: .cameraEffects); if !c.contains(.carouselMoment) { carouselMoment = .omitted } else if try c.decodeNil(forKey: .carouselMoment) { carouselMoment = .remove } else { carouselMoment = .replace(try c.decode([String: JSONValue].self, forKey: .carouselMoment)) }; title = try c.decodeIfPresent(String.self, forKey: .title); acceptedSuggestionIDs = try c.decodeIfPresent([String].self, forKey: .acceptedSuggestionIDs); copilotReceiptIDs = try c.decodeIfPresent([UUID].self, forKey: .copilotReceiptIDs) ?? []; guidedRevision = try c.decodeIfPresent([String: JSONValue].self, forKey: .guidedRevision); guidedRevisionNumber = try c.decodeIfPresent(Int.self, forKey: .guidedRevisionNumber); retryGuidedRevision = try c.decodeIfPresent(Bool.self, forKey: .retryGuidedRevision) ?? false; baseGeneration = try c.decodeIfPresent(String.self, forKey: .baseGeneration) ?? ""
    }
}

struct EditorCommitSections: Codable, Sendable, Equatable {
    var textElements: Bool; var captionMeta: Bool; var timeline: Bool; var mix: Bool; var captionCues: Bool; var music: Bool; var backgroundMusic: Bool; var lyrics: Bool; var orientation: Bool; var soundEffects: Bool; var mediaOverlays: Bool; var visualBlocks: Bool; var motionScenes: Bool; var cameraEffects: Bool; var carouselMoment: Bool; var title: Bool
    init(textElements: Bool, captionMeta: Bool, timeline: Bool, mix: Bool, captionCues: Bool = false, music: Bool = false, backgroundMusic: Bool = false, lyrics: Bool = false, orientation: Bool = false, soundEffects: Bool = false, mediaOverlays: Bool = false, visualBlocks: Bool = false, motionScenes: Bool = false, cameraEffects: Bool = false, carouselMoment: Bool = false, title: Bool = false) { self.textElements = textElements; self.captionMeta = captionMeta; self.timeline = timeline; self.mix = mix; self.captionCues = captionCues; self.music = music; self.backgroundMusic = backgroundMusic; self.lyrics = lyrics; self.orientation = orientation; self.soundEffects = soundEffects; self.mediaOverlays = mediaOverlays; self.visualBlocks = visualBlocks; self.motionScenes = motionScenes; self.cameraEffects = cameraEffects; self.carouselMoment = carouselMoment; self.title = title }
    private enum CodingKeys: String, CodingKey { case textElements = "text_elements"; case captionMeta = "caption_meta"; case timeline, mix; case captionCues = "caption_cues"; case music; case backgroundMusic = "background_music"; case lyrics, orientation; case soundEffects = "sound_effects"; case mediaOverlays = "media_overlays"; case visualBlocks = "visual_blocks"; case motionScenes = "motion_scenes"; case cameraEffects = "camera_effects"; case carouselMoment = "carousel_moment"; case title }
    init(from decoder: Decoder) throws { let c = try decoder.container(keyedBy: CodingKeys.self); textElements = try c.decodeIfPresent(Bool.self, forKey: .textElements) ?? false; captionMeta = try c.decodeIfPresent(Bool.self, forKey: .captionMeta) ?? false; timeline = try c.decodeIfPresent(Bool.self, forKey: .timeline) ?? false; mix = try c.decodeIfPresent(Bool.self, forKey: .mix) ?? false; captionCues = try c.decodeIfPresent(Bool.self, forKey: .captionCues) ?? false; music = try c.decodeIfPresent(Bool.self, forKey: .music) ?? false; backgroundMusic = try c.decodeIfPresent(Bool.self, forKey: .backgroundMusic) ?? false; lyrics = try c.decodeIfPresent(Bool.self, forKey: .lyrics) ?? false; orientation = try c.decodeIfPresent(Bool.self, forKey: .orientation) ?? false; soundEffects = try c.decodeIfPresent(Bool.self, forKey: .soundEffects) ?? false; mediaOverlays = try c.decodeIfPresent(Bool.self, forKey: .mediaOverlays) ?? false; visualBlocks = try c.decodeIfPresent(Bool.self, forKey: .visualBlocks) ?? false; motionScenes = try c.decodeIfPresent(Bool.self, forKey: .motionScenes) ?? false; cameraEffects = try c.decodeIfPresent(Bool.self, forKey: .cameraEffects) ?? false; carouselMoment = try c.decodeIfPresent(Bool.self, forKey: .carouselMoment) ?? false; title = try c.decodeIfPresent(Bool.self, forKey: .title) ?? false }
}
struct EditorCommitResponse: Codable, Sendable {
    let ok: Bool
    let generation: String
    let sections: EditorCommitSections
    let revisionNumber: Int?
    let revisionHash: String?
    let expectedDuration: Double?
    private enum CodingKeys: String, CodingKey { case ok, generation, sections; case revisionNumber = "revision_number"; case revisionHash = "revision_hash"; case expectedDuration = "expected_duration_s" }
}
private struct EditorStatusEnvelope: Decodable {
    let variants: [[String: JSONValue]]
}
struct ApprovalSnapshot: Codable, Sendable, Identifiable {
    let approvalID: String
    let turnID: String
    let draftID: String?
    let draftRevision: Int?
    let status: String
    let consequenceSummary: String
    let costSummary: String?
    let expiresAt: Date
    let approvalFingerprint: String
    var id: String { approvalID }
    enum CodingKeys: String, CodingKey { case status; case approvalID = "approval_id"; case turnID = "turn_id"; case draftID = "draft_id"; case draftRevision = "draft_revision"; case consequenceSummary = "consequence_summary"; case costSummary = "cost_summary"; case expiresAt = "expires_at"; case approvalFingerprint = "approval_fingerprint" }
}
enum JSONValue: Codable, Equatable, Sendable {
    case string(String), number(Double), bool(Bool), object([String: JSONValue]), array([JSONValue]), null
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let value = try? c.decode(Bool.self) { self = .bool(value) }
        else if let value = try? c.decode(Double.self) { self = .number(value) }
        else if let value = try? c.decode(String.self) { self = .string(value) }
        else if let value = try? c.decode([String: JSONValue].self) { self = .object(value) }
        else { self = .array(try c.decode([JSONValue].self)) }
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .string(let v): try c.encode(v)
        case .number(let v): try c.encode(v)
        case .bool(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .null: try c.encodeNil()
        }
    }
    var stringValue: String? { if case .string(let value) = self { value } else { nil } }
}

enum UploadPurpose: String, Codable, Sendable { case analysisProxy = "analysis_proxy", cloudRenderSource = "cloud_render_source" }
struct UploadReservation: Codable, Sendable {
    let uploadURL: URL
    let gcsPath: String
    let kind: String
    let contentType: String
    let uploadHeaders: [String: String]
    let purpose: UploadPurpose?
    let reservationID: UUID?
    let retentionExpiresAt: Date?
    enum CodingKeys: String, CodingKey { case kind, purpose; case uploadURL = "upload_url"; case gcsPath = "gcs_path"; case contentType = "content_type"; case uploadHeaders = "upload_headers"; case reservationID = "reservation_id"; case retentionExpiresAt = "retention_expires_at" }
}
struct ProjectUploadReservation: Codable, Sendable {
    let mediaID: String
    let uploadURL: URL
    let gcsPath: String
    let contentType: String
    let uploadHeaders: [String: String]
    enum CodingKeys: String, CodingKey { case mediaID = "media_id"; case uploadURL = "upload_url"; case gcsPath = "gcs_path"; case contentType = "content_type"; case uploadHeaders = "upload_headers" }
}
struct EditRecipe: Codable, Sendable {
    let schemaVersion: Int
    let rendererVersion: String
    let frameRate: Double
    let assets: [RecipeAsset]
    let tracks: [RecipeTrack]
    let requiredCapabilities: Set<String>
    enum CodingKeys: String, CodingKey { case assets, tracks; case schemaVersion = "schema_version"; case rendererVersion = "renderer_version"; case frameRate = "frame_rate"; case requiredCapabilities = "required_capabilities" }
}
struct RecipeAsset: Codable, Sendable, Identifiable { let id: String; let relativePath: String; let duration: Double?; enum CodingKeys: String, CodingKey { case id, duration; case relativePath = "relative_path" } }
struct RecipeTrack: Codable, Sendable, Identifiable { let id: String; let kind: String; let clips: [RecipeClip] }
struct RecipeClip: Codable, Sendable, Identifiable { let id: String; let sourceAssetID: String; let sourceStart: Double; let sourceDuration: Double; let timelineStart: Double; let rate: Double; enum CodingKeys: String, CodingKey { case id, rate; case sourceAssetID = "source_asset_id"; case sourceStart = "source_start"; case sourceDuration = "source_duration"; case timelineStart = "timeline_start" } }

private actor MobileSessionRefreshCoordinator {
    private var inFlight: (failedAccessToken: String, task: Task<MobileSession, Error>)?

    func refresh(
        failedAccessToken: String,
        baseURL: URL,
        tokenStore: TokenStore,
        session: URLSession
    ) async throws -> MobileSession {
        guard let current = try tokenStore.read() else { throw APIError.sessionExpired }
        // Another request may already have completed rotation while this one
        // waited for the actor. Reuse that result instead of replaying the
        // now-single-use refresh token and revoking the whole session family.
        if current.accessToken != failedAccessToken { return current }

        if let inFlight, inFlight.failedAccessToken == failedAccessToken {
            return try await inFlight.task.value
        }

        let task = Task {
            var request = URLRequest(url: baseURL.appending(path: "auth/mobile/refresh"))
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONEncoder().encode(["refresh_token": current.refreshToken])
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
            guard (200..<300).contains(http.statusCode) else { throw APIError.sessionExpired }
            let refreshed = try JSONDecoder().decode(MobileSession.self, from: data)
            try tokenStore.write(refreshed)
            return refreshed
        }
        inFlight = (failedAccessToken, task)

        do {
            let refreshed = try await task.value
            inFlight = nil
            return refreshed
        } catch {
            inFlight = nil
            throw error
        }
    }
}

struct KriaAPI: KriaAPIClient {
    let baseURL: URL
    let tokenStore: TokenStore
    let session: URLSession
    /// Compile-time integration seam generated from the server-owned OpenAPI
    /// subset. Handwritten request policy currently wraps it with Keychain
    /// refresh/retry behavior; endpoint DTO drift still fails this target.
    private let checkedClient: Client
    private let refreshCoordinator: MobileSessionRefreshCoordinator
    /// Compile-time sentinels: removing any critical native route from the
    /// server-owned mobile OpenAPI subset must break the iOS build.
    private static let checkedEditorOperationIDs = [
        Operations.applyCreationAction.id,
        Operations.openLibraryJobInEditor.id,
        Operations.getGenerativeJobStatus.id,
        Operations.commitPlanItemEditor.id,
    ]
    init(baseURL: URL = AppConfiguration.current.apiBaseURL, tokenStore: TokenStore = KeychainTokenStore(), session: URLSession = .shared) {
        self.baseURL = baseURL
        self.tokenStore = tokenStore
        self.session = session
        self.checkedClient = Client(serverURL: baseURL, transport: URLSessionTransport())
        self.refreshCoordinator = MobileSessionRefreshCoordinator()
        _ = Self.checkedEditorOperationIDs
    }
    func projects() async throws -> [ProjectSummary] { try await request(path: "creation-threads", method: "GET", bodyData: nil, decode: [CreationThread].self).map(\.summary) }
    func project(threadID: UUID) async throws -> CreationThread { try await request(path: "creation-threads/\(threadID.uuidString)", method: "GET", query: [URLQueryItem(name: "projection", value: "full")], bodyData: nil, decode: CreationThread.self) }
    func creationCapabilities() async throws -> CreationCapabilities { try await request(path: "creation-threads/capabilities", method: "GET", bodyData: nil, decode: CreationCapabilities.self) }
    func library() async throws -> [ProjectSummary] { try await request(path: "me/jobs", method: "GET", bodyData: nil, decode: LibraryResponse.self).jobs.map { $0.summary } }
    func createThread(message: String?) async throws -> CreationThread { try await request(path: "creation-threads", method: "POST", bodyData: try JSONEncoder().encode(CreateThreadRequest(message: message, clientEventID: UUID().uuidString, runtimeVersion: 2)), decode: CreationThread.self) }
    func exchangeMobileToken(_ credential: AuthCredential, provider: String) async throws -> MobileSession { try await request(path: "auth/mobile/exchange", method: "POST", bodyData: try JSONEncoder().encode(["id_token": credential.token, "provider": provider, "nonce": credential.nonce]), decode: MobileSession.self) }
    func refreshMobileSession(_ refreshToken: String) async throws -> MobileSession { try await request(path: "auth/mobile/refresh", method: "POST", bodyData: try JSONEncoder().encode(["refresh_token": refreshToken]), decode: MobileSession.self) }
    func revokeMobileSession(_ refreshToken: String) async throws { _ = try await request(path: "auth/mobile/revoke", method: "POST", bodyData: try JSONEncoder().encode(["refresh_token": refreshToken]), decode: RevokeResponse.self) }
    func submitTurn(threadID: UUID, message: String, expectedRevision: Int) async throws -> TurnAccepted { try await request(path: "creation-threads/\(threadID.uuidString)/turns", method: "POST", bodyData: try JSONEncoder().encode(SubmitTurnRequest(message: message, clientEventID: UUID().uuidString, expectedThreadRevision: expectedRevision)), decode: TurnAccepted.self) }
    func applyCreationAction(threadID: UUID, action: String, payload: [String: JSONValue], expectedRevision: Int) async throws -> CreationThread {
        try await request(
            path: "creation-threads/\(threadID.uuidString)/actions",
            method: "POST",
            bodyData: try JSONEncoder().encode(CreationActionRequest(action: action, payload: payload, clientActionID: UUID().uuidString, expectedRevision: expectedRevision)),
            decode: CreationThread.self
        )
    }
    func threadDelta(threadID: UUID, afterSequence: Int) async throws -> ThreadDelta { try await request(path: "creation-threads/\(threadID.uuidString)/delta", method: "GET", query: [URLQueryItem(name: "after_sequence", value: String(afterSequence))], bodyData: nil, decode: ThreadDelta.self) }
    func draft(threadID: UUID) async throws -> DraftSnapshot { try await request(path: "creation-threads/\(threadID.uuidString)/draft", method: "GET", bodyData: nil, decode: DraftSnapshot.self) }
    func writeDraft(threadID: UUID, snapshot: [String: JSONValue], expectedRevision: Int, etag: String) async throws -> DraftSnapshot { try await request(path: "creation-threads/\(threadID.uuidString)/draft", method: "PUT", headers: ["If-Match": etag], bodyData: try JSONEncoder().encode(DraftWriteRequest(expectedRevision: expectedRevision, snapshot: snapshot)), decode: DraftSnapshot.self) }
    func openJobInEditor(jobID: UUID) async throws -> OpenInEditorResponse {
        try await request(path: "me/jobs/\(jobID.uuidString)/open-in-editor", method: "POST", bodyData: nil, decode: OpenInEditorResponse.self)
    }
    func editorVariants(jobID: UUID) async throws -> [[String: JSONValue]] {
        try await request(path: "generative-jobs/\(jobID.uuidString)/status", method: "GET", bodyData: nil, decode: EditorStatusEnvelope.self).variants
    }
    func editorVariant(jobID: UUID, variantID: String) async throws -> [String: JSONValue] {
        guard let variant = try await editorVariants(jobID: jobID).first(where: { $0["variant_id"]?.stringValue == variantID }) else { throw APIError.invalidResponse }
        return variant
    }
    func editorCommit(itemID: String, variantID: String, request commit: EditorCommitRequest) async throws -> EditorCommitResponse {
        try await request(path: "plan-items/\(itemID)/variants/\(variantID)/editor-commit", method: "POST", bodyData: try JSONEncoder().encode(commit), decode: EditorCommitResponse.self)
    }
    func undoDraft(threadID: UUID, expectedRevision: Int) async throws -> DraftSnapshot { try await request(path: "creation-threads/\(threadID.uuidString)/draft/undo", method: "POST", bodyData: try JSONEncoder().encode(DraftUndoRequest(expectedRevision: expectedRevision)), decode: DraftSnapshot.self) }
    func approval(threadID: UUID, approvalID: UUID) async throws -> ApprovalSnapshot { try await request(path: "creation-threads/\(threadID.uuidString)/approvals/\(approvalID.uuidString)", method: "GET", bodyData: nil, decode: ApprovalSnapshot.self) }
    func decideApproval(threadID: UUID, approvalID: UUID, decision: String, expectedThreadRevision: Int, expectedDraftRevision: Int, fingerprint: String) async throws { _ = try await request(path: "creation-threads/\(threadID.uuidString)/approvals/\(approvalID.uuidString)/\(decision)", method: "POST", bodyData: try JSONEncoder().encode(ApprovalDecisionRequest(expectedThreadRevision: expectedThreadRevision, expectedDraftRevision: expectedDraftRevision, fingerprint: fingerprint)), decode: ApprovalResponse.self) }
    func playbackURL(jobID: UUID) async throws -> URL { let response = try await request(path: "me/jobs/\(jobID.uuidString)/playback-url", method: "GET", bodyData: nil, decode: PlaybackResponse.self); guard let url = URL(string: response.videoURL) else { throw APIError.invalidResponse }; return url }
    func editRecipe(jobID: UUID, variantID: String?) async throws -> EditRecipe { try await request(path: "me/jobs/\(jobID.uuidString)/edit-recipe", method: "GET", query: variantID.map { [URLQueryItem(name: "variant_id", value: $0)] } ?? [], bodyData: nil, decode: EditRecipe.self) }
    func reserveUpload(filename: String, contentType: String, size: Int64, purpose: UploadPurpose) async throws -> UploadReservation { try await request(path: "generative-jobs/upload-url", method: "POST", bodyData: try JSONEncoder().encode(UploadReservationRequest(filename: filename, contentType: contentType, fileSizeBytes: size, purpose: purpose)), decode: UploadReservation.self) }
    func cancelUpload(reservationID: UUID) async throws { _ = try await request(path: "generative-jobs/uploads/\(reservationID.uuidString)", method: "DELETE", bodyData: nil, decode: UploadCancellation.self) }
    func reserveProjectUpload(threadID: UUID, clientUploadID: String, filename: String, contentType: String, size: Int64) async throws -> ProjectUploadReservation {
        let body = ProjectUploadReservationRequest(files: [.init(filename: filename, contentType: contentType, fileSizeBytes: size, clientUploadID: clientUploadID)])
        let reservations = try await request(path: "creation-threads/\(threadID.uuidString)/upload-urls", method: "POST", bodyData: try JSONEncoder().encode(body), decode: [ProjectUploadReservation].self)
        guard let reservation = reservations.first, reservations.count == 1 else { throw APIError.invalidResponse }
        return reservation
    }
    func attachProjectMedia(threadID: UUID, mediaID: String, gcsPath: String, filename: String, contentType: String, expectedRevision: Int, clientEventID: String) async throws -> CreationThread {
        let media = ProjectMediaInput(mediaID: mediaID, gcsPath: gcsPath, kind: "video", filename: filename, contentType: contentType)
        let body = ProjectMediaAttachmentRequest(media: [media], clientEventID: clientEventID, expectedRevision: expectedRevision)
        return try await request(path: "creation-threads/\(threadID.uuidString)/media", method: "POST", bodyData: try JSONEncoder().encode(body), decode: CreationThread.self)
    }
    private func request<T: Decodable>(path: String, method: String, query: [URLQueryItem] = [], headers: [String: String] = [:], bodyData: Data?, decode: T.Type) async throws -> T {
        var components = URLComponents(url: baseURL.appending(path: path), resolvingAgainstBaseURL: false)
        components?.queryItems = query.isEmpty ? nil : query
        guard let url = components?.url else { throw APIError.invalidResponse }
        var request = URLRequest(url: url); request.httpMethod = method; request.setValue("application/json", forHTTPHeaderField: "Accept")
        for (name, value) in headers { request.setValue(value, forHTTPHeaderField: name) }
        let storedSession = try tokenStore.read()
        if let token = storedSession?.accessToken { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let bodyData { request.httpBody = bodyData; request.setValue("application/json", forHTTPHeaderField: "Content-Type") }
        var (data, response) = try await session.data(for: request)
        if let http = response as? HTTPURLResponse, http.statusCode == 401, let storedSession {
            let refreshed: MobileSession
            do {
                refreshed = try await refreshCoordinator.refresh(
                    failedAccessToken: storedSession.accessToken,
                    baseURL: baseURL,
                    tokenStore: tokenStore,
                    session: session
                )
            } catch let error as APIError where error == .sessionExpired {
                clearExpiredSession()
                throw error
            }
            request.setValue("Bearer \(refreshed.accessToken)", forHTTPHeaderField: "Authorization")
            (data, response) = try await session.data(for: request)
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.invalidResponse }
        if http.statusCode == 401 { clearExpiredSession(); throw APIError.sessionExpired }
        if http.statusCode == 409 || http.statusCode == 412 { throw APIError.conflict }
        guard (200..<300).contains(http.statusCode) else { throw APIError.requestFailed }
        let decoder = JSONDecoder(); decoder.dateDecodingStrategy = .custom(ServerDateCoding.decode); return try decoder.decode(T.self, from: data)
    }
    private func clearExpiredSession() {
        try? tokenStore.delete()
        NotificationCenter.default.post(name: .kriaSessionExpired, object: nil)
    }
}
private struct LibraryResponse: Decodable { let jobs: [LibraryJob]; struct LibraryJob: Decodable { let id: String; let mode: String; let status: String; let posterURL: String?; let createdAt: Date; enum CodingKeys: String, CodingKey { case id, mode, status; case posterURL = "poster_url"; case createdAt = "created_at" }; var summary: ProjectSummary { ProjectSummary(id: UUID(uuidString: id) ?? UUID(), title: mode.capitalized, status: status == "ready" ? .ready : status == "failed" ? .failed : .rendering, updatedAt: createdAt, posterURL: posterURL.flatMap(URL.init(string:))) } } }
private struct PlaybackResponse: Decodable { let videoURL: String; enum CodingKeys: String, CodingKey { case videoURL = "video_url" } }
private struct ApprovalResponse: Decodable {}
private struct RevokeResponse: Decodable { let revoked: Bool? }
private enum ServerDateCoding {
    static func decode(_ decoder: Decoder) throws -> Date {
        let value = try decoder.singleValueContainer().decode(String.self)
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        if let date = formatter.date(from: value) { return date }
        throw DecodingError.dataCorruptedError(in: try decoder.singleValueContainer(), debugDescription: "Expected ISO-8601 date")
    }
}
private struct SubmitTurnRequest: Encodable { let message: String; let clientEventID: String; let expectedThreadRevision: Int; enum CodingKeys: String, CodingKey { case message; case clientEventID = "client_event_id"; case expectedThreadRevision = "expected_thread_revision" } }
private struct CreationActionRequest: Encodable { let action: String; let payload: [String: JSONValue]; let clientActionID: String; let expectedRevision: Int; enum CodingKeys: String, CodingKey { case action, payload; case clientActionID = "client_action_id"; case expectedRevision = "expected_revision" } }
private struct ApprovalDecisionRequest: Encodable { let expectedThreadRevision: Int; let expectedDraftRevision: Int; let fingerprint: String; enum CodingKeys: String, CodingKey { case expectedThreadRevision = "expected_thread_revision"; case expectedDraftRevision = "expected_draft_revision"; case fingerprint = "expected_approval_fingerprint" } }
private struct UploadCancellation: Decodable { let reservationID: String; let status: String; enum CodingKeys: String, CodingKey { case status; case reservationID = "reservation_id" } }
private struct UploadReservationRequest: Encodable { let filename: String; let contentType: String; let fileSizeBytes: Int64; let purpose: UploadPurpose; enum CodingKeys: String, CodingKey { case filename, purpose; case contentType = "content_type"; case fileSizeBytes = "file_size_bytes" } }
private struct ProjectUploadReservationRequest: Encodable { let files: [ProjectUploadFileRequest] }
private struct ProjectUploadFileRequest: Encodable { let filename: String; let contentType: String; let fileSizeBytes: Int64; let clientUploadID: String; enum CodingKeys: String, CodingKey { case filename; case contentType = "content_type"; case fileSizeBytes = "file_size_bytes"; case clientUploadID = "client_upload_id" } }
private struct ProjectMediaAttachmentRequest: Encodable { let media: [ProjectMediaInput]; let clientEventID: String; let expectedRevision: Int; enum CodingKeys: String, CodingKey { case media; case clientEventID = "client_event_id"; case expectedRevision = "expected_revision" } }
private struct ProjectMediaInput: Encodable { let mediaID: String; let gcsPath: String; let kind: String; let filename: String; let contentType: String; enum CodingKeys: String, CodingKey { case kind, filename; case mediaID = "media_id"; case gcsPath = "gcs_path"; case contentType = "content_type" } }
private struct CreateThreadRequest: Encodable { let message: String?; let clientEventID: String; let runtimeVersion: Int; enum CodingKeys: String, CodingKey { case message; case clientEventID = "client_event_id"; case runtimeVersion = "runtime_version" } }
private struct DraftWriteRequest: Encodable { let expectedRevision: Int; let snapshot: [String: JSONValue]; enum CodingKeys: String, CodingKey { case expectedRevision = "expected_draft_revision"; case snapshot } }
private struct DraftUndoRequest: Encodable { let expectedRevision: Int; enum CodingKeys: String, CodingKey { case expectedRevision = "expected_draft_revision" } }
enum APIError: Error, LocalizedError, Equatable { case requestFailed, offline, invalidResponse, sessionExpired, conflict, unsupported; var errorDescription: String? { switch self { case .sessionExpired: "Your session expired. Please sign in again."; case .conflict: "This edit changed elsewhere. Review your local changes before saving again."; case .unsupported: "This API client does not support native editor saves."; default: "Kria couldn’t complete that request. Check your connection and try again." } } }

protocol AuthProvider { func signIn() async throws -> AuthCredential }
struct AuthCredential: Sendable { let token: String; let displayName: String?; let nonce: String; init(token: String, displayName: String?, nonce: String = "") { self.token = token; self.displayName = displayName; self.nonce = nonce } }
struct DevAuthProvider: AuthProvider { func signIn() async throws -> AuthCredential { AuthCredential(token: "local-development-token", displayName: "Local creator") } }
@MainActor final class GoogleAuthProvider: NSObject, AuthProvider, ASWebAuthenticationPresentationContextProviding {
    private var webSession: ASWebAuthenticationSession?
    func signIn() async throws -> AuthCredential {
        guard
            let clientID = Bundle.main.object(forInfoDictionaryKey: "KriaGoogleClientID") as? String,
            !clientID.isEmpty,
            let redirectScheme = Bundle.main.object(forInfoDictionaryKey: "KriaGoogleRedirectScheme") as? String,
            !redirectScheme.isEmpty
        else { throw AuthError.notConfigured }

        let nonce = UUID().uuidString
        let state = UUID().uuidString
        let verifier = GoogleOAuth.makeCodeVerifier()
        let redirectURI = "\(redirectScheme):/oauth2redirect"
        guard let authorizationURL = GoogleOAuth.authorizationURL(
            clientID: clientID,
            redirectURI: redirectURI,
            state: state,
            nonce: nonce,
            codeChallenge: GoogleOAuth.codeChallenge(for: verifier)
        ) else { throw AuthError.invalidCredential }
        return try await withCheckedThrowingContinuation { continuation in
            let session = ASWebAuthenticationSession(url: authorizationURL, callbackURLScheme: redirectScheme) { [weak self] callbackURL, error in
                defer { self?.webSession = nil }
                if let sessionError = error as? ASWebAuthenticationSessionError, sessionError.code == .canceledLogin {
                    continuation.resume(throwing: AuthError.cancelled)
                    return
                }
                guard error == nil, let callbackURL else {
                    continuation.resume(throwing: AuthError.interactiveRequired)
                    return
                }
                Task {
                    do {
                        let code = try GoogleOAuth.authorizationCode(from: callbackURL, expectedState: state)
                        let request = GoogleOAuth.tokenRequest(clientID: clientID, code: code, verifier: verifier, redirectURI: redirectURI)
                        let (data, response) = try await URLSession.shared.data(for: request)
                        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { throw AuthError.tokenExchangeFailed }
                        let tokenResponse = try JSONDecoder().decode(GoogleOAuthTokenResponse.self, from: data)
                        continuation.resume(returning: AuthCredential(token: tokenResponse.idToken, displayName: nil, nonce: nonce))
                    } catch let error as AuthError {
                        continuation.resume(throwing: error)
                    } catch {
                        continuation.resume(throwing: AuthError.tokenExchangeFailed)
                    }
                }
            }
            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = true
            webSession = session
            guard session.start() else {
                webSession = nil
                continuation.resume(throwing: AuthError.interactiveRequired)
                return
            }
        }
    }
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        return scenes.flatMap(\.windows).first(where: \.isKeyWindow) ?? ASPresentationAnchor()
    }
}
struct AppleAuthProvider: AuthProvider {
    func signIn() async throws -> AuthCredential { throw AuthError.interactiveRequired }
    func credential(from result: ASAuthorization, nonce: String = "") throws -> AuthCredential {
        guard let apple = result.credential as? ASAuthorizationAppleIDCredential, let data = apple.identityToken, let token = String(data: data, encoding: .utf8) else { throw AuthError.invalidCredential }
        return AuthCredential(token: token, displayName: [apple.fullName?.givenName, apple.fullName?.familyName].compactMap { $0 }.joined(separator: " ").nilIfEmpty, nonce: nonce)
    }
}
struct GoogleOAuthTokenResponse: Decodable, Sendable {
    let idToken: String
    enum CodingKeys: String, CodingKey { case idToken = "id_token" }
}

enum GoogleOAuth {
    static func authorizationURL(clientID: String, redirectURI: String, state: String, nonce: String, codeChallenge: String) -> URL? {
        var components = URLComponents(string: "https://accounts.google.com/o/oauth2/v2/auth")
        components?.queryItems = [
            URLQueryItem(name: "client_id", value: clientID),
            URLQueryItem(name: "redirect_uri", value: redirectURI),
            URLQueryItem(name: "response_type", value: "code"),
            URLQueryItem(name: "scope", value: "openid email profile"),
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "nonce", value: nonce),
            URLQueryItem(name: "code_challenge", value: codeChallenge),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
            URLQueryItem(name: "prompt", value: "select_account"),
        ]
        return components?.url
    }

    static func authorizationCode(from callbackURL: URL, expectedState: String) throws -> String {
        let queryItems = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false)?.queryItems ?? []
        if queryItems.contains(where: { $0.name == "error" }) { throw AuthError.interactiveRequired }
        guard queryItems.first(where: { $0.name == "state" })?.value == expectedState else { throw AuthError.stateMismatch }
        guard let code = queryItems.first(where: { $0.name == "code" })?.value, !code.isEmpty else { throw AuthError.authorizationCodeMissing }
        return code
    }

    static func tokenRequest(clientID: String, code: String, verifier: String, redirectURI: String) -> URLRequest {
        var request = URLRequest(url: URL(string: "https://oauth2.googleapis.com/token")!)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        var components = URLComponents()
        components.queryItems = [
            URLQueryItem(name: "client_id", value: clientID),
            URLQueryItem(name: "code", value: code),
            URLQueryItem(name: "code_verifier", value: verifier),
            URLQueryItem(name: "redirect_uri", value: redirectURI),
            URLQueryItem(name: "grant_type", value: "authorization_code"),
        ]
        request.httpBody = components.percentEncodedQuery?.data(using: .utf8)
        return request
    }

    static func makeCodeVerifier() -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            // RFC 7636 requires 43–128 unreserved characters. Two UUIDs keep
            // the emergency fallback inside that range even without entropy
            // from Security.framework.
            return (UUID().uuidString + UUID().uuidString).replacingOccurrences(of: "-", with: "")
        }
        return base64URL(Data(bytes))
    }

    static func codeChallenge(for verifier: String) -> String { base64URL(Data(SHA256.hash(data: Data(verifier.utf8)))) }

    private static func base64URL(_ data: Data) -> String { data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "") }
}

enum AuthError: Error, LocalizedError, Equatable {
    case interactiveRequired, invalidCredential, cancelled, notConfigured, stateMismatch, authorizationCodeMissing, tokenExchangeFailed
    var errorDescription: String? { switch self { case .notConfigured: "Google sign-in is not configured for this build."; default: "Sign-in didn’t finish. Please try again." } }
}
private extension String { var nilIfEmpty: String? { isEmpty ? nil : self } }
extension String { var sha256Hex: String { SHA256.hash(data: Data(utf8)).map { String(format: "%02x", $0) }.joined() } }

protocol EditorOperations: Sendable {
    func apply(_ operation: EditorOperation, to draft: EditorDraft) async throws -> EditorDraft
}
enum EditorOperation: Codable, Equatable, Sendable { case reorder(from: Int, to: Int), trim(clipID: UUID, start: TimeInterval, end: TimeInterval), addText(String), setCaptions(Bool), setMusic(UUID, String), undo }
struct LocalEditorOperations: EditorOperations {
    func apply(_ operation: EditorOperation, to draft: EditorDraft) async throws -> EditorDraft {
        var next = draft; next.revision += 1
        switch operation {
        case let .reorder(from, to) where draft.clips.indices.contains(from) && draft.clips.indices.contains(to): next.clips.move(fromOffsets: IndexSet(integer: from), toOffset: to > from ? to + 1 : to)
        case let .trim(id, start, end): if let index = next.clips.firstIndex(where: { $0.id == id }) { next.clips[index].trimIn = start; next.clips[index].trimOut = end }
        case let .addText(content): next.text.append(TextLayer(id: UUID(), content: content, position: CGPoint(x: 0.5, y: 0.5), style: "Fraunces"))
        case let .setCaptions(enabled): next.captions.enabled = enabled
        case let .setMusic(id, title): next.music = MusicSelection(trackID: id, title: title, start: 0)
        case .undo: next.revision = max(0, draft.revision - 1)
        default: break
        }
        return next
    }
}

extension DraftSnapshot {
    /// Rebase the recoverable runtime draft onto the live render variant.
    /// Direct editor commits intentionally do not mint a runtime-draft head, so
    /// an older head may still exist when the editor is reopened. The status
    /// variant is authoritative for every renderer-owned section and baseline.
    func editorDraft(projectID: UUID, authoritativeVariant: [String: JSONValue]? = nil) -> EditorDraft {
        var document = snapshot
        if let authoritativeVariant {
            var editorPayload = Self.object(document["editor_payload"]) ?? [:]
            var sections = Self.object(editorPayload["sections"]) ?? [:]
            let directKeys = [
                "text_elements", "caption_cues", "captions_enabled", "caption_size_px",
                "caption_highlight_color", "caption_stroke_width", "caption_shadow_enabled",
                "music_track_id", "music_window", "background_music", "lyrics", "orientation",
                "sound_effects", "media_overlays", "visual_blocks", "motion_scenes",
                "motion_runtime_hash", "camera_effects", "carousel_moment",
            ]
            for key in directKeys where authoritativeVariant[key] != nil {
                sections[key] = authoritativeVariant[key]
            }
            if let style = authoritativeVariant["voiceover_caption_style"] ?? authoritativeVariant["caption_style"] {
                sections["caption_style"] = style
            }
            if let font = authoritativeVariant["voiceover_caption_font"] ?? authoritativeVariant["caption_font"] {
                sections["caption_font"] = font
            }
            if let color = authoritativeVariant["caption_text_color"] ?? authoritativeVariant["caption_color"] {
                sections["caption_color"] = color
            }
            if let audioMix = authoritativeVariant["audio_mix"] {
                sections["audio_mix"] = audioMix
            } else if let mix = authoritativeVariant["mix"] {
                var audioMix = Self.object(sections["audio_mix"]) ?? [:]
                audioMix["music_level"] = mix
                sections["audio_mix"] = .object(audioMix)
            }
            if let title = authoritativeVariant["track_title"] {
                sections["music_track_title"] = title
            }
            // Match the server's `user_timeline or ai_timeline` semantics:
            // legacy variants can persist an empty object, which is not an
            // intentional delete-all edit and must not hide the AI slots.
            let userTimeline = Self.object(authoritativeVariant["user_timeline"])
            let timeline = userTimeline?.isEmpty == false
                ? userTimeline
                : Self.object(authoritativeVariant["ai_timeline"])
            if let timeline,
               let slotValue = timeline["slots"], case let .array(slots) = slotValue {
                sections["timeline_slots"] = .array(slots)
            } else if authoritativeVariant["resolved_archetype"] == .string("narrated") {
                let slots = Self.narratedTimelineSlots(authoritativeVariant)
                if !slots.isEmpty { sections["timeline_slots"] = .array(slots) }
            }
            editorPayload["base_generation"] =
                authoritativeVariant["render_generation_id"]
                ?? authoritativeVariant["render_finished_at"]
                ?? baseGenerationID.map(JSONValue.string)
                ?? editorPayload["base_generation"]
                ?? .string("")
            editorPayload["sections"] = .object(sections)
            document["editor_payload"] = .object(editorPayload)
            // Capabilities are status-time policy, not draft state. Always
            // replace a stale/missing draft copy with the authoritative map so
            // advanced lanes do not stay permanently fail-closed after load.
            if let capabilities = authoritativeVariant["editor_capabilities"] {
                document["editor_capabilities"] = capabilities
            }
        }

        let editorPayload = Self.object(document["editor_payload"]) ?? [:]
        let sections = Self.object(editorPayload["sections"]) ?? [:]
        let legacy = Self.object(sections["ios_editor"]) ?? [:]
        let canonicalSlots = Self.array(sections["timeline_slots"])
        let usesCanonicalTimeline = sections["timeline_slots"] != nil && !canonicalSlots.isEmpty
        let slotValues = usesCanonicalTimeline ? canonicalSlots : Self.array(legacy["clips"])
        var cursor: TimeInterval = 0
        let clips = slotValues.enumerated().compactMap { index, value -> EditorClip? in
            guard let object = Self.object(value), object["removed"] != .bool(true) else { return nil }
            if !usesCanonicalTimeline {
                guard let id = Self.uuid(object["id"]), let assetID = Self.uuid(object["asset_id"]), let start = Self.number(object["start"]), let end = Self.number(object["end"]), let trimIn = Self.number(object["trim_in"]), let trimOut = Self.number(object["trim_out"]) else { return nil }
                return EditorClip(id: id, assetID: assetID, sourceClipIndex: Self.integer(object["clip_index"]), start: start, end: end, trimIn: trimIn, trimOut: trimOut, sourceDuration: Self.number(object["source_duration"] ?? object["source_duration_s"]), muted: object["muted"] == .bool(true))
            }
            guard let duration = Self.number(object["duration_s"]), duration > 0 else { return nil }
            let id = Self.uuid(object["id"]) ?? Self.uuid(object["slot_id"]) ?? UUID()
            let sourceStart = Self.number(object["in_s"]) ?? 0
            let start = cursor; cursor += duration
            let sourceDuration = Self.number(object["source_duration"] ?? object["source_duration_s"]) ?? (sourceStart + duration)
            return EditorClip(id: id, assetID: Self.uuid(object["asset_id"]) ?? id, sourceClipIndex: Self.integer(object["clip_index"]) ?? index, start: start, end: start + duration, trimIn: sourceStart, trimOut: sourceStart + duration, sourceDuration: sourceDuration, muted: object["muted"] == .bool(true), slotID: object["slot_id"]?.stringValue)
        }
        let textValues = Self.array(sections["text_elements"] ?? legacy["text"])
        let text = textValues.compactMap { value -> TextLayer? in
            guard let object = Self.object(value), let content = (object["text"] ?? object["content"])?.stringValue else { return nil }
            let id = Self.uuid(object["id"]) ?? UUID()
            let x = Self.number(object["x_frac"] ?? object["x"]) ?? 0.5; let y = Self.number(object["y_frac"] ?? object["y"]) ?? 0.5
            let style = (object["font_family"] ?? object["style"])?.stringValue ?? "Fraunces"
            return TextLayer(id: id, content: content, position: CGPoint(x: x, y: y), style: style)
        }
        let music: MusicSelection?
        if let object = Self.object(legacy["music"]), let trackID = Self.uuid(object["track_id"]) {
            music = MusicSelection(trackID: trackID, title: object["title"]?.stringValue ?? "Music", start: Self.number(object["start"]) ?? 0, volume: Self.number(object["volume"]) ?? 1)
        } else if let trackID = Self.uuid(sections["music_track_id"]) {
            let window = Self.object(sections["music_window"]); let mix = Self.object(sections["audio_mix"])
            music = MusicSelection(trackID: trackID, title: sections["music_track_title"]?.stringValue ?? "Music", start: Self.number(window?["start_s"]) ?? 0, volume: Self.number(mix?["music_level"]) ?? 1)
        } else { music = nil }
        let captionsEnabled = (sections["captions_enabled"] ?? legacy["captions_enabled"]) == .bool(true)
        let captionStyle = (sections["caption_style"] ?? legacy["captions_style"])?.stringValue ?? "sentence"
        return EditorDraft(
            projectID: projectID,
            clips: clips,
            text: text,
            captions: CaptionStyle(enabled: captionsEnabled, style: captionStyle),
            music: music,
            revision: draftRevision,
            etag: etag,
            serverSnapshot: document
        )
    }

    fileprivate static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(object) = value { object } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(array) = value { array } else { [] } }
    private static func number(_ value: JSONValue?) -> Double? { if case let .number(number) = value { number } else { nil } }
    private static func integer(_ value: JSONValue?) -> Int? { if case let .number(number) = value, number.rounded() == number { Int(number) } else { nil } }
    private static func uuid(_ value: JSONValue?) -> UUID? { value?.stringValue.flatMap(UUID.init(uuidString:)) }

    /// Narrated renders deliberately have no editable `ai_timeline`. Their exact
    /// visual cut is persisted as paired timings and clip assignments instead.
    /// Project that pair into canonical slots so native can show the read-only
    /// filmstrip without claiming the montage timeline is editable.
    private static func narratedTimelineSlots(_ variant: [String: JSONValue]) -> [JSONValue] {
        let timings = array(variant["narrated_timings"])
        let assignments = array(variant["narrated_clip_assignments"])
        guard !timings.isEmpty, !assignments.isEmpty else { return [] }

        return timings.compactMap { timingValue in
            guard let timing = object(timingValue),
                  let stepID = timing["step_id"]?.stringValue,
                  let start = number(timing["start_s"]),
                  let end = number(timing["end_s"]),
                  end > start,
                  let assignmentIndex = assignments.firstIndex(where: {
                      object($0)?["step_id"]?.stringValue == stepID
                  }),
                  let assignment = object(assignments[assignmentIndex]) else { return nil }
            let sourceStart = max(0, number(assignment["source_start_s"]) ?? 0)
            let duration = end - start
            let clipID = assignment["clip_id"]?.stringValue ?? ""
            let sourceIndex = clipID.hasPrefix("clip_")
                ? Int(clipID.dropFirst("clip_".count)) ?? assignmentIndex
                : assignmentIndex
            return .object([
                "slot_id": .string(stepID),
                "clip_index": .number(Double(sourceIndex)),
                "in_s": .number(sourceStart),
                "duration_s": .number(duration),
                "source_duration_s": .number(sourceStart + duration),
                "removed": .bool(false),
            ])
        }
    }
}

enum UploadConsentError: Error, LocalizedError { case required; var errorDescription: String? { "Please confirm that you want Kria to use this cloud footage." } }
struct UploadCoordinator: Sendable {
    func validate(source: UploadSource, purpose: UploadPurpose = .analysisProxy, consentGiven: Bool) throws {
        _ = source
        if purpose == .cloudRenderSource && !consentGiven { throw UploadConsentError.required }
    }
}

protocol PhotoLibrarySaving: Sendable {
    func saveVideo(at url: URL) async throws
}

struct PhotoLibrarySaver: PhotoLibrarySaving {
    func saveVideo(at url: URL) async throws {
        let authorization = await PHPhotoLibrary.requestAuthorization(for: .addOnly)
        guard authorization == .authorized || authorization == .limited else { throw PhotoLibraryError.notAuthorized }
        try await PHPhotoLibrary.shared().performChanges {
            PHAssetChangeRequest.creationRequestForAssetFromVideo(atFileURL: url)
        }
    }
}

enum PhotoLibraryError: Error, LocalizedError {
    case notAuthorized
    var errorDescription: String? { "Kria needs Photos access to save this video." }
}
