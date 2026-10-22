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

    /// `pinned` binds an exact source (camera-roll stories); otherwise the role picks one.
    static func attach(asset: SavedMediaAsset, source pinned: SavedMediaSource? = nil, threadID: UUID, itemID: String?, role: CreationMediaRole, destination: ProjectUploadDestination = .phone, mediaID: String, owner: String, api: any KriaAPIClient) async throws -> CreationThread {
        if role == .visual {
            guard let itemID, asset.kind != "audio" else { throw SavedMediaError.originalRequired }
            var target = asset
            if target.bindableSource(for: .visual, destination: destination) == nil {
                // KRI-822: Visuals render a full-quality cloud copy. A device-only video gets one
                // from its verified original on this iPhone; the analysis proxy never stands in.
                guard asset.kind == "video", asset.originalAvailability == "device_only",
                      let local = try await adoptOriginal(asset: asset, owner: owner, api: api) else { throw SavedMediaError.originalRequired }
                target = try await api.uploadSavedMediaOriginal(asset: asset, file: local)
            }
            guard let original = target.bindableSource(for: .visual, destination: destination) else { throw SavedMediaError.originalRequired }
            _ = try await api.attachSavedVisual(itemID: itemID, asset: target, source: original)
            return try await api.project(threadID: threadID)
        }
        guard let source = pinned ?? asset.bindableSource(for: role, destination: destination) else { throw SavedMediaError.originalRequired }
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

/// KRI-822: one decision drives a saved upload's row text, its Add button and the Add action,
/// so a row can never say "Original on this iPhone" next to a disabled Add.
struct SavedMediaEligibility: Equatable {
    enum Action: Equatable {
        /// A cloud source for this role exists.
        case attach
        /// Visuals: upload the verified original on this iPhone in full, then attach it.
        case uploadOriginal
        /// The original must be chosen in Files first; Add opens Files and then finishes.
        case locateOriginal
        /// No usable cloud copy; restore it from the original first.
        case restoreCloudCopy
        case unavailable
    }
    let action: Action
    let text: String
    var canAdd: Bool { action == .attach || action == .uploadOriginal || action == .locateOriginal }

    static let onThisIPhone = "Original on this iPhone"
    static let originalNeeded = "Full-quality original needed on this iPhone"
    static let expired = "Cloud copy expired or unavailable. Restore it from the original to reuse it."

    static func evaluate(_ asset: SavedMediaAsset, role: CreationMediaRole, destination: ProjectUploadDestination, signedIn: Bool, hasLocalOriginal: Bool) -> Self {
        let status = asset.status == "failed" ? "Analysis needs retry" : asset.status == "ready" ? "Available to reuse" : "Analysis in progress"
        let deviceOnlyVideo = asset.kind == "video" && asset.originalAvailability == "device_only" && asset.fingerprint != nil
        let fits = role.isAudio ? asset.kind == "audio" : role == .clip ? asset.kind == "video" : asset.kind != "audio"
        if role == .visual, let kinds = destination.visualKinds, !kinds.contains(asset.kind == "image" ? .image : .video) {
            return Self(action: .unavailable, text: asset.kind == "video" ? "Add videos to this post in Footage." : "Photos can’t be added to this post on iPhone.")
        }
        let source = asset.bindableSource(for: role, destination: destination)
        let result: Self
        if role == .visual, source == nil, deviceOnlyVideo {
            result = hasLocalOriginal ? Self(action: .uploadOriginal, text: onThisIPhone) : Self(action: .locateOriginal, text: originalNeeded)
        } else if source == nil {
            result = Self(action: .restoreCloudCopy, text: expired)
        } else if deviceOnlyVideo, role == .clip {
            result = hasLocalOriginal ? Self(action: .attach, text: onThisIPhone) : Self(action: .locateOriginal, text: originalNeeded)
        } else {
            result = Self(action: .attach, text: status)
        }
        guard fits, signedIn, destination.canUpload else { return Self(action: .unavailable, text: result.text) }
        return result
    }
}

enum SavedMediaError: LocalizedError {
    case originalRequired
    var errorDescription: String? { "Choose the matching full-quality original from Files to use this video on this iPhone." }
}
