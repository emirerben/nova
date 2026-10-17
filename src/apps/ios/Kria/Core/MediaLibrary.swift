import Foundation
import CryptoKit
import KriaMediaEngine

/// Mirrors the server-owned MediaAssetOut and immutable source contract.
struct SavedMediaAsset: Codable, Identifiable, Sendable {
    let id: UUID
    let kind: String
    let status: String
    let originalDeclaredSHA256: String?
    let originalByteCount: Int64?
    let originalAvailability: String
    let metadata: [String: JSONValue]
    let sources: [SavedMediaSource]
    let usage: [SavedMediaUsage]
    let storageWarnings: [SavedMediaWarning]
    var filename: String { metadata["filename"]?.stringValue ?? "Saved \(kind)" }
    var displayName: String { BackgroundUploadCoordinator.displayFilename(filename) }
    var source: SavedMediaSource? { sources.first { $0.status == "available" && ["original", "analysis_proxy"].contains($0.role) } }
    var fingerprint: AssetFingerprint? {
        if let hash = originalDeclaredSHA256, let bytes = originalByteCount { return AssetFingerprint(hex: hash, byteCount: bytes) }
        guard let original = sources.first(where: { $0.role == "original" }), let hash = original.uploadedSHA256 else { return nil }
        return AssetFingerprint(hex: hash, byteCount: original.byteCount)
    }
    enum CodingKeys: String, CodingKey {
        case id, kind, status, sources, usage
        case originalDeclaredSHA256 = "original_declared_sha256", originalByteCount = "original_byte_count"
        case originalAvailability = "original_availability", metadata = "metadata_json", storageWarnings = "storage_warnings"
    }
}
struct SavedMediaSource: Codable, Identifiable, Sendable {
    let id: UUID
    let role: String
    let status: String
    let byteCount: Int64
    let contentType: String
    let uploadedSHA256: String?
    let generation: String
    enum CodingKeys: String, CodingKey { case id, role, status, generation; case byteCount = "byte_count", contentType = "content_type", uploadedSHA256 = "uploaded_sha256" }
}
struct SavedMediaUsage: Codable, Sendable {
    let creationThreadID: UUID?
    let planItemID: UUID?
    enum CodingKeys: String, CodingKey { case creationThreadID = "creation_thread_id", planItemID = "plan_item_id" }
}
struct SavedMediaWarning: Codable, Sendable {
    let sourceID: UUID
    let eligibleAt: Date
    enum CodingKeys: String, CodingKey { case sourceID = "source_id", eligibleAt = "eligible_at" }
}
struct SavedMediaPage: Codable, Sendable {
    let assets: [SavedMediaAsset]
    let nextCursor: String?
    let activeCount: Int
    let limit: Int
    enum CodingKeys: String, CodingKey { case assets, limit; case nextCursor = "next_cursor", activeCount = "active_count" }
}
struct SavedMediaAccess: Decodable, Sendable { let url: URL; let generation: String }

extension KriaAPIClient {
    var mediaLibraryOwner: String? { nil }
    func savedMedia(query: String, kind: String?, cursor: String?) async throws -> SavedMediaPage { throw APIError.unsupported }
    func savedMediaAsset(id: UUID) async throws -> SavedMediaAsset { throw APIError.unsupported }
    func savedMediaAccess(assetID: UUID, sourceID: UUID) async throws -> SavedMediaAccess { throw APIError.unsupported }
    func archiveSavedMedia(id: UUID) async throws { throw APIError.unsupported }
    func retrySavedMedia(id: UUID) async throws -> SavedMediaAsset { throw APIError.unsupported }
    func attachSavedMedia(threadID: UUID, asset: SavedMediaAsset, source: SavedMediaSource, mediaID: String, revision: Int, role: CreationMediaRole) async throws -> CreationThread { throw APIError.unsupported }
    func attachSavedVisual(itemID: String, asset: SavedMediaAsset, source: SavedMediaSource) async throws -> CreationVisual { throw APIError.unsupported }
}

extension KriaAPI {
    var mediaLibraryOwner: String? { MediaLibraryOriginals.owner(accessToken: (try? tokenStore.read())?.accessToken) }
    func savedMedia(query: String, kind: String?, cursor: String?) async throws -> SavedMediaPage {
        var items = [URLQueryItem(name: "limit", value: "30")]
        if !query.isEmpty { items.append(URLQueryItem(name: "query", value: String(query.prefix(200)))) }
        if let kind { items.append(URLQueryItem(name: "kind", value: kind)) }
        if let cursor { items.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await request(path: "me/media-assets", method: "GET", query: items, bodyData: nil, decode: SavedMediaPage.self)
    }
    func savedMediaAsset(id: UUID) async throws -> SavedMediaAsset {
        try await request(path: "me/media-assets/\(id)", method: "GET", bodyData: nil, decode: SavedMediaAsset.self)
    }
    func savedMediaAccess(assetID: UUID, sourceID: UUID) async throws -> SavedMediaAccess {
        try await request(path: "me/media-assets/\(assetID)/sources/\(sourceID)/access-url", method: "GET", bodyData: nil, decode: SavedMediaAccess.self)
    }
    func archiveSavedMedia(id: UUID) async throws {
        let _: SavedMediaAsset = try await request(path: "me/media-assets/\(id)/archive", method: "POST", bodyData: nil, decode: SavedMediaAsset.self)
    }
    func retrySavedMedia(id: UUID) async throws -> SavedMediaAsset {
        try await request(path: "me/media-assets/\(id)/retry", method: "POST", bodyData: nil, decode: SavedMediaAsset.self)
    }
    func attachSavedMedia(threadID: UUID, asset: SavedMediaAsset, source: SavedMediaSource, mediaID: String, revision: Int, role: CreationMediaRole) async throws -> CreationThread {
        var media: [String: JSONValue] = ["media_id": .string(mediaID), "library_asset_id": .string(asset.id.uuidString), "library_source_id": .string(source.id.uuidString), "kind": .string(asset.kind), "filename": .string(asset.filename), "content_type": .string(source.contentType)]
        if role == .song { media["role"] = .string("song") }
        let body: [String: JSONValue] = ["media": .array([.object(media)]), "expected_revision": .number(Double(revision)), "client_event_id": .string("ios-library-\(mediaID)")]
        return try await request(path: "creation-threads/\(threadID)/media", method: "POST", bodyData: JSONEncoder().encode(body), decode: CreationThread.self)
    }
    func attachSavedVisual(itemID: String, asset: SavedMediaAsset, source: SavedMediaSource) async throws -> CreationVisual {
        let body: [String: JSONValue] = ["library_asset_id": .string(asset.id.uuidString), "library_source_id": .string(source.id.uuidString), "source_filename": .string(asset.filename)]
        return try await request(path: "plan-items/\(itemID)/assets", method: "POST", bodyData: JSONEncoder().encode(body), decode: CreationVisual.self)
    }
}

/// Library originals have their own account-scoped lifetime. Post stores receive hardlinks
/// (copy fallback); deleting either post cannot remove the other's original.
struct MediaLibraryOriginals: Sendable {
    let owner: String
    var base: URL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
    var root: URL { base.appendingPathComponent("KriaMediaLibrary").appendingPathComponent(Data(owner.utf8).map { String(format: "%02x", $0) }.joined()) }
    var erasureMarker: URL { root.appendingPathExtension("deleted") }
    func directory(_ id: UUID) -> ProjectDirectory { ProjectDirectory(root: root.appendingPathComponent(id.uuidString)) }
    func localURL(assetID: UUID, fingerprint: AssetFingerprint) -> URL? {
        let store = SourceAssetStore(project: directory(assetID))
        guard let binding = (try? store.bindings())?.first(where: { $0.mediaID == assetID.uuidString && $0.original.fingerprint == fingerprint }) else { return nil }
        return store.localFileIfPresent(mediaID: binding.mediaID)
    }
    func retain(assetID: UUID, from project: ProjectDirectory, mediaID: String, original: MediaAsset) async throws {
        guard let fingerprint = original.fingerprint else { throw SourceAssetError.invalidBinding }
        let file = project.root.appendingPathComponent(original.relativePath)
        try await MediaLibraryOriginalsIO.shared.retain(file: file, fingerprint: fingerprint, assetID: assetID, store: self, preferLink: true)
    }
    func relink(assetID: UUID, fingerprint: AssetFingerprint, file: URL) async throws {
        try await MediaLibraryOriginalsIO.shared.retain(file: file, fingerprint: fingerprint, assetID: assetID, store: self, preferLink: false)
    }
    func bind(assetID: UUID, fingerprint: AssetFingerprint, mediaID: String, to project: ProjectDirectory) async throws {
        try await MediaLibraryOriginalsIO.shared.bind(assetID: assetID, fingerprint: fingerprint, mediaID: mediaID, project: project, store: self)
    }
    func removeAccount() throws {
        // Fence late imports before erasing bytes, including work already awaiting file I/O.
        try FileManager.default.createDirectory(at: root.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: erasureMarker, options: .atomic)
        if FileManager.default.fileExists(atPath: root.path) { try FileManager.default.removeItem(at: root) }
    }
    static func owner(accessToken: String?) -> String? {
        guard let parts = accessToken?.split(separator: "."), parts.count >= 2 else { return nil }
        var raw = String(parts[1]).replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        raw += String(repeating: "=", count: (4 - raw.count % 4) % 4)
        guard let data = Data(base64Encoded: raw), let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let subject = ["sub", "user_id", "email"].compactMap({ payload[$0] as? String }).first else { return nil }
        return SHA256.hash(data: Data(subject.utf8)).map { String(format: "%02x", $0) }.joined()
    }
}

/// Actor serializes imports and keeps full-file hashing/copying off the UI actor.
private actor MediaLibraryOriginalsIO {
    static let shared = MediaLibraryOriginalsIO()
    func retain(file: URL, fingerprint: AssetFingerprint, assetID: UUID, store: MediaLibraryOriginals, preferLink: Bool) throws {
        guard !FileManager.default.fileExists(atPath: store.erasureMarker.path) else { throw SourceAssetError.invalidBinding }
        guard try SHA256Fingerprinter().fingerprint(file: file) == fingerprint else { throw SourceAssetError.changedOriginal(assetID.uuidString) }
        let directory = store.directory(assetID)
        if let existing = store.localURL(assetID: assetID, fingerprint: fingerprint),
           (try? SHA256Fingerprinter().fingerprint(file: existing)) == fingerprint { return }
        // A verified replacement repairs a missing/changed local copy. Unique paths keep
        // a failed copy from destroying a previous working binding.
        try directory.createIfNeeded()
        let name = UUID().uuidString + "." + (file.pathExtension.isEmpty ? "mov" : file.pathExtension)
        let target = directory.originals.appendingPathComponent(name)
        if preferLink { try CoordinatedFileCopier().linkAsset(from: file, to: target) }
        else { try CoordinatedFileCopier().copyAsset(from: file, to: target) }
        guard try SHA256Fingerprinter().fingerprint(file: target) == fingerprint else {
            try? FileManager.default.removeItem(at: target)
            throw SourceAssetError.changedOriginal(assetID.uuidString)
        }
        let original = MediaAsset(id: name, relativePath: "originals/\(name)", fingerprint: fingerprint)
        guard !FileManager.default.fileExists(atPath: store.erasureMarker.path) else {
            try? FileManager.default.removeItem(at: directory.root)
            throw SourceAssetError.invalidBinding
        }
        var rootURL = store.root
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try rootURL.setResourceValues(values)
        // Use an independent manifest directory for each immutable library identity.
        let manifest = directory.root.appendingPathComponent("source-assets.json")
        try JSONEncoder().encode([SourceAssetBinding(mediaID: assetID.uuidString, original: original)]).write(to: manifest, options: .atomic)
    }
    func bind(assetID: UUID, fingerprint: AssetFingerprint, mediaID: String, project: ProjectDirectory, store: MediaLibraryOriginals) throws {
        guard let file = store.localURL(assetID: assetID, fingerprint: fingerprint),
              try SHA256Fingerprinter().fingerprint(file: file) == fingerprint else { throw SourceAssetError.missingOriginal(assetID.uuidString) }
        let targetStore = SourceAssetStore(project: project)
        if let existing = (try? targetStore.bindings())?.first(where: { $0.mediaID == mediaID }) {
            guard existing.original.fingerprint == fingerprint else { throw SourceAssetError.invalidBinding }
            _ = try targetStore.resolve(mediaIDs: [mediaID]); return
        }
        try project.createIfNeeded()
        let name = UUID().uuidString + "." + file.pathExtension
        let target = project.originals.appendingPathComponent(name)
        try CoordinatedFileCopier().linkAsset(from: file, to: target)
        try targetStore.bind(mediaID: mediaID, original: MediaAsset(id: name, relativePath: "originals/\(name)", fingerprint: fingerprint))
    }
}

struct SavedMediaUpload: Decodable {
    let reservationID: UUID
    let objectPath: String
    let uploadURL: URL?
    let uploadHeaders: [String: String]
    let assetID: UUID?
    enum CodingKeys: String, CodingKey { case reservationID = "reservation_id", objectPath = "object_path", uploadURL = "upload_url", uploadHeaders = "upload_headers", assetID = "asset_id" }
}
extension KriaAPIClient {
    func repairSavedMedia(asset: SavedMediaAsset, file: URL) async throws -> SavedMediaAsset { throw APIError.unsupported }
}
extension KriaAPI {
    func repairSavedMedia(asset: SavedMediaAsset, file: URL) async throws -> SavedMediaAsset {
        guard let fingerprint = asset.fingerprint else { throw SourceAssetError.invalidBinding }
        let actual = try await Task.detached { try SHA256Fingerprinter().fingerprint(file: file) }.value
        guard actual == fingerprint else { throw SourceAssetError.changedOriginal(asset.id.uuidString) }
        let proxy = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString + ".mp4")
        defer { try? FileManager.default.removeItem(at: proxy) }
        let uploadFile: URL
        let contentType: String
        let contract: ProjectMediaUploadContract
        if asset.kind == "video", asset.originalAvailability == "device_only" {
            let result = try await AVFoundationProxyGenerator(preset: ProxyPreset(width: 640, height: 360)).makeProxy(for: file, destination: proxy)
            contract = try await ProjectMediaUploadContract.analysisProxy(original: file, proxy: result, fingerprint: actual)
            uploadFile = proxy; contentType = "video/mp4"
        } else {
            contract = ProjectMediaUploadContract(purpose: .cloudRenderSource, proxy: nil)
            uploadFile = file
            guard let original = asset.sources.first(where: { $0.role == "original" }) else { throw APIError.invalidResponse }
            contentType = original.contentType
        }
        let uploaded = try await Task.detached { try SHA256Fingerprinter().fingerprint(file: uploadFile) }.value
        struct Body: Encodable {
            let client_upload_id: String; let filename: String; let content_type: String
            let file_size_bytes: Int64; let kind: String; let upload_contract: ProjectMediaUploadContract; let repair_asset_id: UUID
        }
        let body = Body(client_upload_id: "ios-repair-\(asset.id)-\(uploaded.hex)", filename: asset.filename, content_type: contentType, file_size_bytes: uploaded.byteCount, kind: asset.kind, upload_contract: contract, repair_asset_id: asset.id)
        let target = try await request(path: "me/media-assets/uploads", method: "POST", bodyData: JSONEncoder().encode(body), decode: SavedMediaUpload.self)
        if let url = target.uploadURL {
            let reservation = UploadReservation(uploadURL: url, gcsPath: target.objectPath, kind: asset.kind, contentType: contentType, uploadHeaders: target.uploadHeaders, purpose: nil, reservationID: nil, retentionExpiresAt: nil)
            var put = URLRequest(url: reservation.uploadURL)
            put.httpMethod = "PUT"; put.setValue(contentType, forHTTPHeaderField: "Content-Type")
            for (key, value) in reservation.uploadHeaders { put.setValue(value, forHTTPHeaderField: key) }
            let (_, response) = try await session.upload(for: put, fromFile: uploadFile)
            guard let status = (response as? HTTPURLResponse)?.statusCode,
                  (200..<300).contains(status) || status == 412 else { throw APIError.invalidResponse }
            // A lost finalize response can leave an existing create-only object. Finalize
            // verifies its full bytes and generation before accepting a 412 retry.
        } else if let id = target.assetID { return try await savedMediaAsset(id: id) }
        let finalized: [String: JSONValue] = ["client_event_id": .string("ios-finalize-\(target.reservationID)")]
        return try await request(path: "me/media-assets/uploads/\(target.reservationID)/finalize", method: "POST", bodyData: JSONEncoder().encode(finalized), decode: SavedMediaAsset.self)
    }
}
