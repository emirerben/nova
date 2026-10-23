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
}

struct CameraRollCollectionSelection: Codable, Sendable, Equatable {
    let name: String
    let members: [CameraRollCollectionMember]
}

struct SavedCameraRollIdeas: Codable, Sendable {
    let revision: Int
    let selection: CameraRollCollectionSelection
    let query: String
    let researchTopic: String?
    let result: CameraRollStories
}

extension CameraRollCollection {
    var remoteSelection: CameraRollCollectionSelection? {
        var members: [CameraRollCollectionMember] = []
        for entry in entries {
            // Phone stories pin the analysis proxy; a cloud original is not interchangeable.
            guard let asset = entry.savedAsset,
                  let source = asset.kind == "video" ? asset.bindableSource(for: .clip, destination: .phone) : asset.source else { return nil }
            members.append(CameraRollCollectionMember(asset_id: asset.id, source_id: source.id, group: group(for: entry.id)))
        }
        return CameraRollCollectionSelection(name: name.trimmingCharacters(in: .whitespacesAndNewlines), members: members)
    }
    var matchingSavedIdeas: SavedCameraRollIdeas? {
        guard let savedIdeas, savedIdeas.revision == serverRevision, savedIdeas.selection == remoteSelection,
              savedIdeas.result.stories.contains(where: { !$0.rejected }) else { return nil }
        return savedIdeas
    }
}

/// Collection failures must not inherit the editor's generic conflict wording.
enum CameraRollCollectionFailure {
    static let unavailable = "This footage needs analysis. Your collection is saved; new analysis is unavailable right now."
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
        case "collection_media_unavailable", "Selected media is unavailable or its source changed. Remove or repair it.": return "Some footage is unavailable. Remove or restore it before finding ideas."
        default:
            if case .conflict = apiError { return "Could not finish finding ideas. Your collection is saved. Try again." }
            return apiError.localizedDescription
        }
    }
}

extension KriaAPIClient {
    func cameraRollCollection(id: UUID) async throws -> SavedCameraRollCollection { throw APIError.unsupported }
    func importCollectionMedia(file: URL, kind: String) async throws -> SavedMediaAsset { throw APIError.unsupported }
    func saveCameraRollCollection(_ collection: CameraRollCollection, revision: Int) async throws -> SavedCameraRollCollection { throw APIError.unsupported }
}

extension KriaAPI {
    func saveCameraRollCollection(_ collection: CameraRollCollection, revision: Int) async throws -> SavedCameraRollCollection {
        struct Body: Encodable { let name: String; let expected_revision: Int; let members: [CameraRollCollectionMember] }
        guard let selection = collection.remoteSelection else { throw APIError.invalidResponse }
        return try await request(path: "me/media-collections/\(collection.id)", method: "PUT",
                                 bodyData: JSONEncoder().encode(Body(name: selection.name, expected_revision: revision, members: selection.members)),
                                 decode: SavedCameraRollCollection.self)
    }
    func cameraRollCollection(id: UUID) async throws -> SavedCameraRollCollection {
        try await request(path: "me/media-collections/\(id)", method: "GET", bodyData: nil, decode: SavedCameraRollCollection.self)
    }

    /// Explicit import uses the existing reservation/finalize catalogue contract. Videos upload
    /// only a timeline-preserving proxy; the verified original stays in the account's local store.
    @MainActor func importCollectionMedia(file: URL, kind: String) async throws -> SavedMediaAsset {
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
            let (_, response) = try await session.upload(for: put, fromFile: uploadFile)
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
