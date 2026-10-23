import Foundation
import UniformTypeIdentifiers
import KriaMediaEngine

struct SavedCameraRollCollection: Codable, Sendable {
    let id: UUID
    let revision: Int
}

extension KriaAPIClient {
    func importCollectionMedia(file: URL, kind: String) async throws -> SavedMediaAsset { throw APIError.unsupported }
    func saveCameraRollCollection(_ collection: CameraRollCollection, revision: Int) async throws -> SavedCameraRollCollection { throw APIError.unsupported }
}

extension KriaAPI {
    func saveCameraRollCollection(_ collection: CameraRollCollection, revision: Int) async throws -> SavedCameraRollCollection {
        struct Member: Encodable { let asset_id: UUID; let source_id: UUID; let group: String }
        struct Body: Encodable { let name: String; let expected_revision: Int; let members: [Member] }
        let members = try collection.entries.map { entry in
            // Stories bind phone Footage: pin the proxy, never a full original added for Visuals.
            guard let asset = entry.savedAsset, let source = asset.kind == "video" ? asset.bindableSource(for: .clip, destination: .phone) : asset.source else { throw APIError.invalidResponse }
            return Member(asset_id: asset.id, source_id: source.id, group: collection.group(for: entry.id))
        }
        return try await request(path: "me/media-collections/\(collection.id)", method: "PUT",
                                 bodyData: JSONEncoder().encode(Body(name: collection.name, expected_revision: revision, members: members)),
                                 decode: SavedCameraRollCollection.self)
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
