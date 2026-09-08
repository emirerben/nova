import Foundation
import SwiftData

enum ProjectStatus: String, Codable, Sendable { case draft, rendering, ready, failed }
enum UploadState: String, Codable, Sendable { case queued, uploading, uploaded, failed }
enum AssetKind: String, Codable, Sendable { case video, photo, audio }
enum CacheConflictError: Error, LocalizedError, Equatable {
    case staleProject(expected: Int, actual: Int)
    var errorDescription: String? { "This project changed on another device. Refresh before saving again." }
}

struct ProjectSummary: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    var title: String
    var status: ProjectStatus
    var updatedAt: Date
    var posterURL: URL?
    /// Fresh-signed playback URL for the selected render variant, when the
    /// creation-thread response includes full media projection.
    var outputURL: URL?
    /// Variant identity paired with ``outputURL``. This must travel with the
    /// URL so editor callers do not accidentally select a different variant.
    var outputVariantID: String?
    var runtimeVersion: Int
    var serverRevision: Int
    var activeJobID: UUID?
    /// Existing plan-item ownership for creation-thread projects. Keeping this
    /// identity lets the editor load its authoritative job directly without
    /// trying to promote the same video through the Gallery route.
    var activePlanItemID: String?

    init(
        id: UUID,
        title: String,
        status: ProjectStatus,
        updatedAt: Date,
        posterURL: URL?,
        outputURL: URL? = nil,
        outputVariantID: String? = nil,
        runtimeVersion: Int = 2,
        serverRevision: Int = 0,
        activeJobID: UUID? = nil,
        activePlanItemID: String? = nil
    ) {
        self.id = id
        self.title = title
        self.status = status
        self.updatedAt = updatedAt
        self.posterURL = posterURL
        self.outputURL = outputURL
        self.outputVariantID = outputVariantID
        self.runtimeVersion = runtimeVersion
        self.serverRevision = serverRevision
        self.activeJobID = activeJobID
        self.activePlanItemID = activePlanItemID
    }
}

struct AssetSummary: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    let projectID: UUID
    var name: String
    var kind: AssetKind
    var duration: TimeInterval?
    var thumbnailURL: URL?
}

struct UploadJob: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    let projectID: UUID
    var filename: String
    var state: UploadState
    var progress: Double
    var source: UploadSource
    var reservationID: UUID? = nil
    var retentionExpiresAt: Date? = nil
}

enum UploadSource: String, Codable, Sendable { case photos, files, cloudDrive }

struct RenderReceipt: Codable, Identifiable, Hashable, Sendable {
    let id: UUID
    let projectID: UUID
    let generation: Int
    let createdAt: Date
    let status: String
    let message: String
    let outputURL: URL?
}

@Model final class CachedProject {
    @Attribute(.unique) var id: UUID
    var title: String
    var statusRaw: String
    var updatedAt: Date
    var posterURLString: String?
    var runtimeVersion: Int
    var serverRevision: Int
    var activeJobID: UUID?
    var outputVariantID: String?
    var activePlanItemID: String?
    init(id: UUID, title: String, status: ProjectStatus = .draft, updatedAt: Date = .now, posterURL: URL? = nil) {
        self.id = id; self.title = title; self.statusRaw = status.rawValue; self.updatedAt = updatedAt; self.posterURLString = posterURL?.absoluteString; self.runtimeVersion = 2; self.serverRevision = 0; self.outputVariantID = nil; self.activePlanItemID = nil
    }
    var status: ProjectStatus { ProjectStatus(rawValue: statusRaw) ?? .draft }
    var posterURL: URL? { posterURLString.flatMap(URL.init(string:)) }
    var summary: ProjectSummary {
        ProjectSummary(
            id: id,
            title: title,
            status: status,
            updatedAt: updatedAt,
            posterURL: posterURL,
            outputVariantID: outputVariantID,
            runtimeVersion: runtimeVersion,
            serverRevision: serverRevision,
            activeJobID: activeJobID,
            activePlanItemID: activePlanItemID
        )
    }
}

@Model final class CachedAsset {
    @Attribute(.unique) var id: UUID
    var projectID: UUID
    var name: String
    var kindRaw: String
    var duration: Double?
    var thumbnailURLString: String?
    var fingerprint: String?
    var proxyPath: String?
    var waveformPath: String?
    var serverRevision: Int
    init(_ asset: AssetSummary) {
        id = asset.id; projectID = asset.projectID; name = asset.name; kindRaw = asset.kind.rawValue; duration = asset.duration; thumbnailURLString = asset.thumbnailURL?.absoluteString; serverRevision = 0
    }
}

@Model final class CachedUploadJob {
    @Attribute(.unique) var id: UUID
    var projectID: UUID
    var filename: String
    var stateRaw: String
    var progress: Double
    var sourceRaw: String
    var reservationID: UUID?
    var retentionExpiresAt: Date?
    var backgroundTaskIdentifier: Int?
    var lastError: String?
    init(_ job: UploadJob) {
        id = job.id; projectID = job.projectID; filename = job.filename; stateRaw = job.state.rawValue; progress = job.progress; sourceRaw = job.source.rawValue; reservationID = job.reservationID; retentionExpiresAt = job.retentionExpiresAt
    }
}

@Model final class CachedReceipt {
    @Attribute(.unique) var id: UUID
    var projectID: UUID
    var generation: Int
    var createdAt: Date
    var status: String
    var message: String
    var outputURLString: String?
    init(_ receipt: RenderReceipt) {
        id = receipt.id; projectID = receipt.projectID; generation = receipt.generation; createdAt = receipt.createdAt; status = receipt.status; message = receipt.message; outputURLString = receipt.outputURL?.absoluteString
    }
}

@MainActor struct CacheRepository {
    let context: ModelContext
    init(context: ModelContext) { self.context = context }
    func upsert(_ projects: [ProjectSummary]) throws {
        for project in projects {
            let id = project.id
            let descriptor = FetchDescriptor<CachedProject>(predicate: #Predicate { $0.id == id })
            if let cached = try context.fetch(descriptor).first {
                cached.title = project.title; cached.statusRaw = project.status.rawValue; cached.updatedAt = project.updatedAt; cached.posterURLString = project.posterURL?.absoluteString; cached.runtimeVersion = project.runtimeVersion; cached.serverRevision = project.serverRevision; cached.activeJobID = project.activeJobID; cached.outputVariantID = project.outputVariantID; cached.activePlanItemID = project.activePlanItemID
            } else {
                let cached = CachedProject(id: id, title: project.title, status: project.status, updatedAt: project.updatedAt, posterURL: project.posterURL)
                cached.runtimeVersion = project.runtimeVersion; cached.serverRevision = project.serverRevision; cached.activeJobID = project.activeJobID; cached.outputVariantID = project.outputVariantID; cached.activePlanItemID = project.activePlanItemID
                context.insert(cached)
            }
        }
        try context.save()
    }
    /// Apply a local edit only when the caller still owns the server revision.
    /// Sync/upsert may overwrite from an authoritative response; user edits
    /// must use this compare-and-set boundary instead.
    func update(_ project: ProjectSummary, expectedServerRevision: Int) throws {
        let id = project.id
        guard let cached = try context.fetch(FetchDescriptor<CachedProject>(predicate: #Predicate { $0.id == id })).first else {
            guard expectedServerRevision == 0 else { throw CacheConflictError.staleProject(expected: expectedServerRevision, actual: -1) }
            try upsert([project]); return
        }
        guard cached.serverRevision == expectedServerRevision else { throw CacheConflictError.staleProject(expected: expectedServerRevision, actual: cached.serverRevision) }
        cached.title = project.title; cached.statusRaw = project.status.rawValue; cached.updatedAt = project.updatedAt; cached.posterURLString = project.posterURL?.absoluteString; cached.runtimeVersion = project.runtimeVersion; cached.serverRevision = project.serverRevision; cached.activeJobID = project.activeJobID; cached.outputVariantID = project.outputVariantID; cached.activePlanItemID = project.activePlanItemID
        try context.save()
    }
    func projects() throws -> [CachedProject] { try context.fetch(FetchDescriptor<CachedProject>(sortBy: [SortDescriptor(\.updatedAt, order: .reverse)])) }
    func store(_ receipt: RenderReceipt) throws { context.insert(CachedReceipt(receipt)); try context.save() }
}

struct EditorDraft: Codable, Equatable, Sendable {
    var projectID: UUID
    var clips: [EditorClip]
    var text: [TextLayer]
    var captions: CaptionStyle
    var music: MusicSelection?
    var revision: Int
    var etag: String = ""
    var serverSnapshot: [String: JSONValue] = [:]
}

struct EditorClip: Codable, Equatable, Identifiable, Sendable {
    let id: UUID
    var assetID: UUID
    /// Stable index into the render job's source pool. This is deliberately
    /// independent from the clip's position in the edited timeline: moving a
    /// clip changes its order, not which source video it references.
    var sourceClipIndex: Int?
    var start: TimeInterval
    var end: TimeInterval
    var trimIn: TimeInterval
    var trimOut: TimeInterval
    var sourceDuration: TimeInterval?
    var muted: Bool
    var slotID: String?

    init(id: UUID, assetID: UUID, sourceClipIndex: Int? = nil, start: TimeInterval, end: TimeInterval, trimIn: TimeInterval, trimOut: TimeInterval, sourceDuration: TimeInterval? = nil, muted: Bool = false, slotID: String? = nil) {
        self.id = id; self.assetID = assetID; self.sourceClipIndex = sourceClipIndex; self.start = start; self.end = end; self.trimIn = trimIn; self.trimOut = trimOut; self.sourceDuration = sourceDuration; self.muted = muted; self.slotID = slotID
    }

    private enum CodingKeys: String, CodingKey { case id; case assetID = "asset_id"; case sourceClipIndex = "clip_index"; case start, end; case trimIn = "trim_in"; case trimOut = "trim_out"; case sourceDuration = "source_duration"; case muted; case slotID = "slot_id" }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        id = try values.decode(UUID.self, forKey: .id); assetID = try values.decode(UUID.self, forKey: .assetID); sourceClipIndex = try values.decodeIfPresent(Int.self, forKey: .sourceClipIndex); start = try values.decode(TimeInterval.self, forKey: .start); end = try values.decode(TimeInterval.self, forKey: .end); trimIn = try values.decode(TimeInterval.self, forKey: .trimIn); trimOut = try values.decode(TimeInterval.self, forKey: .trimOut); sourceDuration = try values.decodeIfPresent(TimeInterval.self, forKey: .sourceDuration); muted = try values.decodeIfPresent(Bool.self, forKey: .muted) ?? false; slotID = try values.decodeIfPresent(String.self, forKey: .slotID)
    }
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(id, forKey: .id); try values.encode(assetID, forKey: .assetID); try values.encodeIfPresent(sourceClipIndex, forKey: .sourceClipIndex); try values.encode(start, forKey: .start); try values.encode(end, forKey: .end); try values.encode(trimIn, forKey: .trimIn); try values.encode(trimOut, forKey: .trimOut); try values.encodeIfPresent(sourceDuration, forKey: .sourceDuration); try values.encode(muted, forKey: .muted); try values.encodeIfPresent(slotID, forKey: .slotID)
    }
}

struct TextLayer: Codable, Equatable, Identifiable, Sendable { let id: UUID; var content: String; var position: CGPoint; var style: String }
struct CaptionStyle: Codable, Equatable, Sendable { var enabled: Bool; var style: String }
struct MusicSelection: Codable, Equatable, Sendable {
    var trackID: UUID; var title: String; var start: TimeInterval; var volume: Double
    init(trackID: UUID, title: String, start: TimeInterval, volume: Double = 1) { self.trackID = trackID; self.title = title; self.start = start; self.volume = volume }
    private enum CodingKeys: String, CodingKey { case trackID = "track_id"; case title, start, volume }
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        trackID = try values.decode(UUID.self, forKey: .trackID); title = try values.decode(String.self, forKey: .title); start = try values.decode(TimeInterval.self, forKey: .start); volume = try values.decodeIfPresent(Double.self, forKey: .volume) ?? 1
    }
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(trackID, forKey: .trackID); try values.encode(title, forKey: .title); try values.encode(start, forKey: .start); try values.encode(volume, forKey: .volume)
    }
}

extension EditorDraft {
    /// Merge the native projection into the server envelope. Unknown root,
    /// section, and per-item fields remain untouched for forward compatibility.
    func persistedSnapshot() -> [String: JSONValue] {
        var root = serverSnapshot
        if root["schema_version"] == nil { root["schema_version"] = .number(2) }
        if root["kind"] == nil { root["kind"] = .string("editor") }
        if root["edit_format"] == nil { root["edit_format"] = .string("montage") }
        var editorPayload = Self.object(root["editor_payload"]) ?? [:]
        var sections = Self.object(editorPayload["sections"]) ?? [:]
        var iosEditor = Self.object(sections["ios_editor"]) ?? [:]
        let oldClips = Self.array(iosEditor["clips"])
        iosEditor["clips"] = .array(clips.map { clip in
            var value = Self.object(oldClips.first { Self.uuid(Self.object($0)?["id"]) == clip.id }) ?? [:]
            value["id"] = .string(clip.id.uuidString); value["asset_id"] = .string(clip.assetID.uuidString); value["start"] = .number(clip.start); value["end"] = .number(clip.end); value["trim_in"] = .number(clip.trimIn); value["trim_out"] = .number(clip.trimOut); value["muted"] = .bool(clip.muted)
            if let duration = clip.sourceDuration { value["source_duration"] = .number(duration) }
            return .object(value)
        })
        let oldText = Self.array(iosEditor["text"])
        iosEditor["text"] = .array(text.map { layer in
            var value = Self.object(oldText.first { Self.uuid(Self.object($0)?["id"]) == layer.id }) ?? [:]
            value["id"] = .string(layer.id.uuidString); value["content"] = .string(layer.content); value["x"] = .number(layer.position.x); value["y"] = .number(layer.position.y); value["style"] = .string(layer.style)
            return .object(value)
        })
        iosEditor["captions_enabled"] = .bool(captions.enabled); iosEditor["captions_style"] = .string(captions.style)
        if let music {
            var value = Self.object(iosEditor["music"]) ?? [:]
            value["track_id"] = .string(music.trackID.uuidString); value["title"] = .string(music.title); value["start"] = .number(music.start); value["volume"] = .number(music.volume); iosEditor["music"] = .object(value)
        } else { iosEditor["music"] = .null }
        // Production editor-commit sections are the canonical representation.
        // Keep the ios_editor projection as a compatibility mirror for older
        // mobile builds and for drafts created before this client shipped.
        let oldSlots = Self.array(sections["timeline_slots"])
        sections["timeline_slots"] = .array(clips.enumerated().map { index, clip in
            var value = Self.object(oldSlots.first {
                let object = Self.object($0)
                return Self.uuid(object?["id"]) == clip.id || object?["slot_id"]?.stringValue == clip.slotID
            }) ?? [:]
            value["slot_id"] = .string(clip.slotID ?? value["slot_id"]?.stringValue ?? clip.id.uuidString)
            let preservedIndex = clip.sourceClipIndex ?? Self.integer(value["clip_index"]) ?? index
            let editedDuration = max(0.1, clip.end - clip.start)
            let sourceWindowChanged = Self.number(value["in_s"]).map { abs($0 - clip.trimIn) > 0.000_001 } ?? true
            let durationChanged = Self.number(value["duration_s"]).map { abs($0 - editedDuration) > 0.000_001 } ?? true
            value["clip_index"] = .number(Double(preservedIndex)); value["in_s"] = .number(clip.trimIn); value["duration_s"] = .number(editedDuration)
            // A trim cannot keep the old beat count: the backend would snap it
            // back to the original length and make the handle appear broken.
            if sourceWindowChanged || durationChanged { value["duration_beats"] = .null }
            else { value["duration_beats"] = value["duration_beats"] ?? .null }
            value["removed"] = .bool(false)
            return .object(value)
        })
        sections["text_elements"] = .array(text.map { layer in
            var value = Self.object(Self.array(sections["text_elements"]).first { Self.uuid(Self.object($0)?["id"]) == layer.id }) ?? [:]
            let totalDuration = clips.map(\.end).max() ?? 0
            value["id"] = .string(layer.id.uuidString); value["text"] = .string(layer.content); value["start_s"] = value["start_s"] ?? .number(0); value["end_s"] = value["end_s"] ?? .number(max(0.1, totalDuration)); value["role"] = value["role"] ?? .string("generative_intro"); value["position"] = value["position"] ?? .string("custom"); value["x_frac"] = .number(layer.position.x); value["y_frac"] = .number(layer.position.y); value["font_family"] = .string(layer.style)
            return .object(value)
        })
        sections["captions_enabled"] = .bool(captions.enabled); sections["caption_style"] = .string(captions.style)
        var audioMix = Self.object(sections["audio_mix"]) ?? [:]; audioMix["music_level"] = .number(music?.volume ?? 0); sections["audio_mix"] = .object(audioMix)
        if let music { sections["music_track_id"] = .string(music.trackID.uuidString); var window = Self.object(sections["music_window"]) ?? [:]; window["start_s"] = .number(music.start); sections["music_window"] = .object(window) }
        else { sections["music_track_id"] = .null; sections["music_window"] = .null }
        sections["ios_editor"] = .object(iosEditor); editorPayload["sections"] = .object(sections); root["editor_payload"] = .object(editorPayload)
        return root
    }
    func serializedSnapshot() -> [String: JSONValue] { persistedSnapshot() }
    private static func object(_ value: JSONValue?) -> [String: JSONValue]? { if case let .object(value) = value { value } else { nil } }
    private static func array(_ value: JSONValue?) -> [JSONValue] { if case let .array(value) = value { value } else { [] } }
    private static func number(_ value: JSONValue?) -> Double? { if case let .number(value) = value { value } else { nil } }
    private static func uuid(_ value: JSONValue?) -> UUID? { value?.stringValue.flatMap(UUID.init(uuidString:)) }
    private static func integer(_ value: JSONValue?) -> Int? { if case let .number(value) = value, value.rounded() == value { Int(value) } else { nil } }
}
