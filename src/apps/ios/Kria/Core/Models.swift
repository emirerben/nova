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
    var runtimeVersion: Int
    var serverRevision: Int
    var activeJobID: UUID?

    init(
        id: UUID,
        title: String,
        status: ProjectStatus,
        updatedAt: Date,
        posterURL: URL?,
        runtimeVersion: Int = 2,
        serverRevision: Int = 0,
        activeJobID: UUID? = nil
    ) {
        self.id = id
        self.title = title
        self.status = status
        self.updatedAt = updatedAt
        self.posterURL = posterURL
        self.runtimeVersion = runtimeVersion
        self.serverRevision = serverRevision
        self.activeJobID = activeJobID
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
    init(id: UUID, title: String, status: ProjectStatus = .draft, updatedAt: Date = .now, posterURL: URL? = nil) {
        self.id = id; self.title = title; self.statusRaw = status.rawValue; self.updatedAt = updatedAt; self.posterURLString = posterURL?.absoluteString; self.runtimeVersion = 2; self.serverRevision = 0
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
            runtimeVersion: runtimeVersion,
            serverRevision: serverRevision,
            activeJobID: activeJobID
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
                cached.title = project.title; cached.statusRaw = project.status.rawValue; cached.updatedAt = project.updatedAt; cached.posterURLString = project.posterURL?.absoluteString; cached.runtimeVersion = project.runtimeVersion; cached.serverRevision = project.serverRevision; cached.activeJobID = project.activeJobID
            } else {
                let cached = CachedProject(id: id, title: project.title, status: project.status, updatedAt: project.updatedAt, posterURL: project.posterURL)
                cached.runtimeVersion = project.runtimeVersion; cached.serverRevision = project.serverRevision; cached.activeJobID = project.activeJobID
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
        cached.title = project.title; cached.statusRaw = project.status.rawValue; cached.updatedAt = project.updatedAt; cached.posterURLString = project.posterURL?.absoluteString; cached.runtimeVersion = project.runtimeVersion; cached.serverRevision = project.serverRevision; cached.activeJobID = project.activeJobID
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
    var start: TimeInterval
    var end: TimeInterval
    var trimIn: TimeInterval
    var trimOut: TimeInterval
}

struct TextLayer: Codable, Equatable, Identifiable, Sendable { let id: UUID; var content: String; var position: CGPoint; var style: String }
struct CaptionStyle: Codable, Equatable, Sendable { var enabled: Bool; var style: String }
struct MusicSelection: Codable, Equatable, Sendable { var trackID: UUID; var title: String; var start: TimeInterval }
