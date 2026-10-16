import Foundation
import KriaMediaEngine

/// Resolves original bytes first, then creates an independent post binding. No PUT on reuse.
@MainActor enum SavedMediaReuse {
    static func adoptOriginal(asset: SavedMediaAsset, owner: String, api: any KriaAPIClient) async throws -> URL? {
        guard let fingerprint = asset.fingerprint else { return nil }
        let library = MediaLibraryOriginals(owner: owner)
        if let local = library.localURL(assetID: asset.id, fingerprint: fingerprint) { return local }
        // Lazy migration is bounded to usage authorized by the server for this owner.
        for threadID in Set(asset.usage.compactMap(\.creationThreadID)) {
            let project = BackgroundUploadCoordinator.projectDirectory(threadID)
            for binding in (try? SourceAssetStore(project: project).bindings()) ?? [] where binding.original.fingerprint == fingerprint {
                try await library.retain(assetID: asset.id, from: project, mediaID: binding.mediaID, original: binding.original)
                return library.localURL(assetID: asset.id, fingerprint: fingerprint)
            }
        }
        return nil
    }

    static func attach(asset: SavedMediaAsset, source: SavedMediaSource, threadID: UUID, itemID: String?, role: CreationMediaRole, mediaID: String, owner: String, api: any KriaAPIClient) async throws -> CreationThread {
        if role == .visual {
            guard let itemID, source.role == "original" else { throw SavedMediaError.originalRequired }
            _ = try await api.attachSavedVisual(itemID: itemID, asset: asset, source: source)
            return try await api.project(threadID: threadID)
        }
        if asset.kind == "video" {
            guard let fingerprint = asset.fingerprint else { throw SavedMediaError.originalRequired }
            let library = MediaLibraryOriginals(owner: owner)
            var local = try await adoptOriginal(asset: asset, owner: owner, api: api)
            if local == nil, source.role == "original" {
                // Only a verified full-quality cloud original can replace the local original.
                let access = try await api.savedMediaAccess(assetID: asset.id, sourceID: source.id)
                guard access.generation == source.generation else { throw APIError.invalidResponse }
                let (download, response) = try await URLSession.shared.download(from: access.url)
                defer { try? FileManager.default.removeItem(at: download) }
                guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw APIError.invalidResponse }
                // Preserve the media extension for AVFoundation's original resolver.
                let named = download.appendingPathExtension((asset.filename as NSString).pathExtension.isEmpty ? "mov" : (asset.filename as NSString).pathExtension)
                try FileManager.default.moveItem(at: download, to: named)
                defer { try? FileManager.default.removeItem(at: named) }
                try await library.relink(assetID: asset.id, fingerprint: fingerprint, file: named)
                local = library.localURL(assetID: asset.id, fingerprint: fingerprint)
            }
            guard local != nil else { throw SavedMediaError.originalRequired }
            do { try await library.bind(assetID: asset.id, fingerprint: fingerprint, mediaID: mediaID, to: BackgroundUploadCoordinator.projectDirectory(threadID)) }
            catch is SourceAssetError { throw SavedMediaError.originalRequired }
            if let local { await CreationMediaPreview.save(localURL: local, mediaID: mediaID) }
        }
        // Stable media/event ID survives conflicts and lost-response retries.
        for attempt in 0..<3 {
            let latest = try await api.project(threadID: threadID)
            do {
                return try await api.attachSavedMedia(threadID: threadID, asset: asset, source: source, mediaID: mediaID, revision: latest.revision, role: role)
            } catch {
                guard attempt < 2, error as? APIError == .conflict else { throw error }
            }
        }
        throw APIError.conflict
    }
}

enum SavedMediaError: LocalizedError {
    case originalRequired
    var errorDescription: String? { "Choose the matching full-quality original from Files to use this video on this iPhone." }
}
