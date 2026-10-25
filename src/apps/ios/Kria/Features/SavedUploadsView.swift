import SwiftUI
import UniformTypeIdentifiers
import ImageIO
import KriaMediaEngine

/// Reuse earlier uploads in this post. Looks and works like the Photos gallery: pick tiles (tap or slide), then
/// Done adds them in pick order. Nothing is attached until Done.
struct SavedUploadsView: View {
    #if DEBUG
    /// UI-test fixture only: stands in for the file chosen in the Files picker.
    @MainActor static var debugRelinkFile: URL?
    #endif
    let projectID: UUID
    let itemID: String?
    let role: CreationMediaRole
    let destination: ProjectUploadDestination
    let capacity: Int
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let onAdded: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var selectionBudget: Int?
    @State private var localIDs: Set<UUID> = []
    @State private var query = ""
    @State private var kind = "all"
    @State private var assets: [SavedMediaAsset] = []
    /// Every asset seen this session, so a pick survives a search that no longer lists it.
    @State private var pool: [UUID: SavedMediaAsset] = [:]
    @State private var selected: [UUID] = []
    @State private var cursor: String?
    @State private var activeCount = 0
    @State private var quota = 500
    @State private var loading = false
    @State private var hasLoadedOnce = false
    @State private var error: String?
    @State private var loadFailed = false
    @State private var notice: String?
    @State private var progress: String?
    @State private var loadID = UUID()
    @State private var reload = 0
    @State private var adding: UUID?
    @State private var committing = false
    @State private var added: Set<UUID> = []
    @State private var alreadyAttached: Set<UUID> = []
    @State private var loadedAttached = false
    @State private var mediaIDs: [UUID: String] = [:]
    /// Add failed because the matching original is missing / the cloud copy changed: the tile needs a repair
    /// before it can be picked again.
    @State private var staleOriginal: Set<UUID> = []
    @State private var staleCloud: Set<UUID> = []
    @State private var archive: SavedMediaAsset?
    @State private var relinking: SavedMediaAsset?
    @State private var showsRelink = false
    @State private var restoring = false
    @State private var repairsCloud = false
    private var api: any KriaAPIClient { uploads.libraryAPI }
    private var owner: String? { api.mediaLibraryOwner }
    private var busy: Bool { committing || restoring }
    private var filterKind: String? { role.isAudio ? "audio" : role == .clip ? "video" : kind == "all" ? nil : kind }
    private var budget: Int { max(1, min(selectionBudget ?? capacity, capacity + added.count)) }
    private var searchIdentity: String { "\(query)|\(kind)|\(reload)" }

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                if role == .visual {
                    Picker("Media type", selection: $kind) {
                        Text("All").tag("all")
                        Text("Photos").tag("image")
                        Text("Videos").tag("video")
                    }.pickerStyle(.segmented).padding(.horizontal).padding(.bottom, 8)
                }
                if let error { errorBar(error) }
                content
            }
            .background(KriaColor.paper)
            .navigationTitle("Saved uploads")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, prompt: "Search filenames")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }.disabled(busy).accessibilityIdentifier("saved-uploads-cancel")
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { Task { await commit() } }.disabled(busy).accessibilityIdentifier("saved-uploads-done")
                }
            }
            .onAppear { if selectionBudget == nil { selectionBudget = capacity } }
            .task(id: searchIdentity) {
                // Typing is debounced; opening the sheet is not.
                if hasLoadedOnce { do { try await Task.sleep(for: .milliseconds(200)) } catch { return } }
                await load(more: false)
                hasLoadedOnce = true
            }
            .confirmationDialog("Remove from saved uploads?", isPresented: Binding(get: { archive != nil }, set: { if !$0 { archive = nil } }), titleVisibility: .visible) {
                Button("Remove from saved uploads", role: .destructive) {
                    guard let asset = archive else { return }
                    Task {
                        do {
                            try await api.archiveSavedMedia(id: asset.id)
                            assets.removeAll { $0.id == asset.id }
                            selected.removeAll { $0 == asset.id }
                            activeCount = max(0, activeCount - 1)
                        } catch { self.error = error.localizedDescription; loadFailed = false }
                    }
                }
            } message: { Text("Existing posts keep their media and edits.") }
            .fileImporter(isPresented: $showsRelink, allowedContentTypes: [.movie, .image, .audio], allowsMultipleSelection: false) { result in
                guard let asset = relinking else { return }
                relinking = nil
                guard case .success(let files) = result, let file = files.first else { return }
                Task { await finishRelink(asset, file: file) }
            }
            .interactiveDismissDisabled(busy)
        }
    }

    @ViewBuilder private var content: some View {
        if assets.isEmpty {
            if loading {
                ProgressView("Loading saved uploads…").frame(maxHeight: .infinity)
            } else if error == nil {
                ContentUnavailableView(
                    query.isEmpty ? "No saved uploads yet" : "No matching uploads",
                    systemImage: "square.stack",
                    description: Text(query.isEmpty ? "Add media to a post from Photos or Files. Your uploads will appear here for reuse." : "Try another filename.")
                )
                .accessibilityIdentifier("saved-uploads-empty")
                .frame(maxHeight: .infinity)
            } else {
                Spacer(minLength: 0)
            }
        } else {
            SavedMediaGrid(
                assets: assets, selected: $selected, limit: budget, owner: owner, api: api,
                state: tileState, detail: detail,
                tileID: { "saved-upload-tile-\($0.id)" },
                message: progress ?? notice ?? (loading ? "Loading more…" : nil),
                onBlockedTap: blockedTap,
                onNearEnd: loadNextPage
            ) { asset in menu(asset) }
            .allowsHitTesting(!busy)
        }
    }

    private func errorBar(_ message: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText)
                .accessibilityIdentifier("saved-uploads-error")
            Spacer(minLength: 0)
            if loadFailed {
                Button("Try again") {
                    if assets.isEmpty { reload += 1 } else { Task { await load(more: true) } }
                }
                .font(KriaFont.body(13).weight(.semibold))
                .accessibilityIdentifier("saved-uploads-retry")
            }
        }
        .padding(.horizontal, 16).padding(.vertical, 8)
    }

    @ViewBuilder private func menu(_ asset: SavedMediaAsset) -> some View {
        Section(asset.displayName) {
            let posts = Set(asset.usage.compactMap(\.creationThreadID)).count
            if posts > 0 { Text("Used in \(posts) posts") }
            if !asset.storageWarnings.isEmpty { Text("Cloud copy expires soon. Saved posts keep their media.") }
            Button("Restore cloud copy from original…") { requestRelink(asset, cloud: true) }
            if asset.status == "failed" {
                Button("Retry analysis") {
                    Task { do { replace(try await api.retrySavedMedia(id: asset.id)) } catch { self.error = error.localizedDescription; loadFailed = false } }
                }
            }
            Button("Remove from saved uploads", role: .destructive) { archive = asset }
        }
    }

    // MARK: Tile state

    private func tileState(_ asset: SavedMediaAsset) -> SavedTileState {
        if added.contains(asset.id) || alreadyAttached.contains(asset.id) { return .inPost }
        if adding == asset.id { return .adding }
        if staleOriginal.contains(asset.id) || staleCloud.contains(asset.id) { return .blocked }
        switch eligibility(asset).action {
        case .attach, .uploadOriginal: return .selectable
        case .locateOriginal, .restoreCloudCopy, .unavailable: return .blocked
        }
    }

    private func detail(_ asset: SavedMediaAsset) -> String {
        let text = eligibility(asset).text
        return asset.storageWarnings.isEmpty ? text : "\(text). Cloud copy expires soon."
    }

    /// Repairable tiles open Files first and are picked once the file is verified; the rest say why not.
    private func blockedTap(_ asset: SavedMediaAsset) {
        notice = nil
        if staleCloud.contains(asset.id) { requestRelink(asset, cloud: true); return }
        if staleOriginal.contains(asset.id) { requestRelink(asset, cloud: false); return }
        let result = eligibility(asset)
        switch result.action {
        case .locateOriginal: requestRelink(asset, cloud: false)
        case .restoreCloudCopy: requestRelink(asset, cloud: true)
        default: notice = result.text.hasSuffix(".") ? result.text : "\(result.text). This can’t be added to this post."
        }
    }

    private func hasLocal(_ asset: SavedMediaAsset) -> Bool { localIDs.contains(asset.id) }
    /// KRI-822: the tile text, its state and the Add action all read this one decision.
    private func eligibility(_ asset: SavedMediaAsset) -> SavedMediaEligibility {
        SavedMediaEligibility.evaluate(asset, role: role, destination: destination, signedIn: owner != nil, hasLocalOriginal: hasLocal(asset))
    }
    /// A device-only video reaches Visuals through its own full original, never a new proxy.
    private func addsOriginalToVisuals(_ asset: SavedMediaAsset) -> Bool {
        role == .visual && asset.kind == "video" && asset.originalAvailability == "device_only"
    }

    /// Whether this iPhone still has each video's original, decided without reading any video: the hashing
    /// copy into the library happens when the creator adds it.
    private func refreshLocal(_ batch: [SavedMediaAsset]) {
        guard let owner else { return }
        let videos = batch.filter { $0.kind == "video" && $0.fingerprint != nil }
        guard !videos.isEmpty else { return }
        Task {
            let found = await Task.detached(priority: .utility) {
                Set(videos.filter { SavedMediaReuse.hasMigratableOriginal(asset: $0, owner: owner) }.map(\.id))
            }.value
            localIDs.subtract(videos.map(\.id))
            localIDs.formUnion(found)
        }
    }

    // MARK: Relink

    private func requestRelink(_ asset: SavedMediaAsset, cloud: Bool) {
        repairsCloud = cloud
        #if DEBUG
        if let file = Self.debugRelinkFile { Self.debugRelinkFile = nil; Task { await finishRelink(asset, file: file) }; return }
        #endif
        relinking = asset; showsRelink = true
    }

    /// Verify the chosen file against the saved original, repair the cloud copy if needed, then pick the tile.
    private func finishRelink(_ asset: SavedMediaAsset, file: URL) async {
        restoring = true
        defer { restoring = false }
        do {
            guard let owner, let fingerprint = asset.fingerprint else { throw SavedMediaError.originalRequired }
            let scoped = file.startAccessingSecurityScopedResource()
            defer { if scoped { file.stopAccessingSecurityScopedResource() } }
            try await MediaLibraryOriginals(owner: owner).relink(assetID: asset.id, fingerprint: fingerprint, file: file)
            var updated = asset
            // For Visuals the add itself uploads this verified original in full; re-uploading
            // another analysis proxy here would leave the creator at a disabled tile again.
            if !addsOriginalToVisuals(asset), repairsCloud || asset.bindableSource(for: role, destination: destination) == nil {
                updated = try await api.repairSavedMedia(asset: asset, file: file)
                replace(updated)
            }
            staleOriginal.remove(asset.id); staleCloud.remove(asset.id)
            localIDs.insert(asset.id)
            error = nil; notice = nil
            if tileState(updated) == .selectable, !selected.contains(updated.id), selected.count < budget { selected.append(updated.id) }
        } catch { self.error = "This file must match the original video. \(error.localizedDescription)"; loadFailed = false }
    }

    private func replace(_ updated: SavedMediaAsset) {
        assets = assets.map { $0.id == updated.id ? updated : $0 }
        pool[updated.id] = updated
    }

    // MARK: Loading

    private func loadNextPage() {
        guard cursor != nil, !loading, error == nil else { return }
        Task { await load(more: true) }
    }

    private func fetchAttached() async throws -> Set<UUID> {
        if role == .visual, let itemID {
            return Set(try await api.visuals(itemID: itemID).assets.compactMap(\.libraryAssetID))
        }
        let thread = try await api.project(threadID: projectID)
        return Set(CreationAttachedMedia.parse(thread.state ?? [:]).filter { media in
            role == .song ? media.isSong : role == .voiceover ? media.isVoiceover : media.kind == "video"
        }.compactMap(\.libraryAssetID))
    }

    private func load(more: Bool) async {
        let search = searchIdentity
        let identity = UUID(); loadID = identity
        loading = true; error = nil; loadFailed = false
        if !more { assets = []; cursor = nil }
        do {
            // The attached set and the first page are independent; don't make the grid wait for both in turn.
            async let attached: Set<UUID>? = loadedAttached ? nil : fetchAttached()
            let page = try await api.savedMedia(query: query.trimmingCharacters(in: .whitespacesAndNewlines), kind: filterKind, cursor: more ? cursor : nil)
            let attachedIDs = try await attached
            guard loadID == identity, searchIdentity == search, !Task.isCancelled else { return }
            if let attachedIDs { alreadyAttached = attachedIDs; loadedAttached = true }
            let allowed = page.assets.filter { role != .visual || $0.kind != "audio" }
            assets = more ? assets + allowed.filter { candidate in !assets.contains { $0.id == candidate.id } } : allowed
            for asset in allowed { pool[asset.id] = asset }
            refreshLocal(allowed)
            cursor = page.nextCursor; activeCount = page.activeCount; quota = page.limit
            loading = false
        } catch {
            guard loadID == identity, searchIdentity == search, !Task.isCancelled else { return }
            self.error = error.localizedDescription; loadFailed = true; loading = false
        }
    }

    // MARK: Commit

    /// Done: attach every pick, one at a time and in pick order (each attach re-reads the project revision).
    /// A failure stops the batch and keeps the rest picked, so Done retries only what is left.
    private func commit() async {
        let ids = selected
        guard !ids.isEmpty else { dismiss(); return }
        committing = true; error = nil; notice = nil
        defer { committing = false; progress = nil }
        for (index, id) in ids.enumerated() {
            guard let asset = pool[id] else { selected.removeAll { $0 == id }; continue }
            progress = ids.count > 1 ? "Adding \(index + 1) of \(ids.count)…" : "Adding…"
            if eligibility(asset).action == .uploadOriginal { progress = "Uploading full-quality original…" }
            guard await add(asset) else { return }
            selected.removeAll { $0 == id }
        }
        if selected.isEmpty { dismiss() }
    }

    /// Returns whether the asset is now attached. On failure the tile is flagged for repair and dropped from the
    /// picks; the error stays on screen.
    private func add(_ asset: SavedMediaAsset) async -> Bool {
        guard adding == nil, !added.contains(asset.id), !alreadyAttached.contains(asset.id), let owner, eligibility(asset).canAdd else { return false }
        adding = asset.id; error = nil; loadFailed = false
        defer { adding = nil }
        let source = asset.bindableSource(for: role, destination: destination)
        let mediaID = mediaIDs[asset.id] ?? (source?.role == "analysis_proxy" ? "analysis-proxy-" : "") + UUID().uuidString
        mediaIDs[asset.id] = mediaID
        do {
            let thread = try await SavedMediaReuse.attach(asset: asset, threadID: projectID, itemID: itemID, role: role, destination: destination, mediaID: mediaID, owner: owner, api: api)
            uploads.acceptSavedMediaThread(thread)
            added.insert(asset.id)
            onAdded()
            return true
        } catch {
            self.error = (error as? APIError)?.conflictDetail ?? error.localizedDescription
            if error is SavedMediaError {
                staleOriginal.insert(asset.id); selected.removeAll { $0 == asset.id }
            } else if role == .visual, (error as? APIError)?.isCloudCopyUnavailable == true, !addsOriginalToVisuals(asset) {
                // Only the generation-drift conflict means the cloud copy changed: restore it from
                // the original, then add. A device-only video already re-uploaded its original in
                // attach, so another relink would only loop; its message stays on screen.
                self.error = "The cloud copy of this file changed. Tap it and choose the original to restore it."
                staleCloud.insert(asset.id); selected.removeAll { $0 == asset.id }
            }
            return false
        }
    }
}

// MARK: Thumbnails

/// A saved upload's poster. Never hashes or copies an original (that happens when the upload is added): it
/// reads the memory cache, the on-disk poster, a local original that is already in place, and only then the
/// smallest cloud copy, a few at a time.
struct SavedUploadThumbnail: View {
    let asset: SavedMediaAsset
    let owner: String?
    let api: any KriaAPIClient
    /// nil fills whatever square the grid gives it.
    var size: CGSize? = CGSize(width: 58, height: 72)
    @State private var image: UIImage?
    var body: some View {
        let content = ZStack {
            KriaColor.softZinc
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: asset.kind == "audio" ? "music.note" : asset.kind == "image" ? "photo" : "video").foregroundStyle(KriaColor.mutedInk) }
        }
        Group {
            if let size { content.frame(width: size.width, height: size.height).clipShape(RoundedRectangle(cornerRadius: 8)) }
            else { content.clipped() }
        }
        .accessibilityHidden(true)
        .task(id: asset.id) { image = await SavedMediaThumbnails.image(for: asset, owner: owner, api: api) }
    }
}

@MainActor enum SavedMediaThumbnails {
    private static let memory: NSCache<NSString, UIImage> = {
        let cache = NSCache<NSString, UIImage>()
        cache.countLimit = 300
        return cache
    }()
    private static let gate = FetchGate(limit: 4)

    static func image(for asset: SavedMediaAsset, owner: String?, api: any KriaAPIClient) async -> UIImage? {
        let name = "library-\(asset.id)"
        let cacheKey = name as NSString
        if let hit = memory.object(forKey: cacheKey) { return hit }
        guard asset.kind != "audio" else { return nil }
        var found = await CreationMediaPreview.image(mediaID: name, projectID: nil)
        if found == nil, let owner, let fingerprint = asset.fingerprint {
            // An original already in the library: no hashing, just a poster frame.
            let library = MediaLibraryOriginals(owner: owner)
            if let local = await Task.detached(operation: { library.localURL(assetID: asset.id, fingerprint: fingerprint) }).value {
                await CreationMediaPreview.save(localURL: local, mediaID: name)
                found = await CreationMediaPreview.image(mediaID: name, projectID: nil)
            }
        }
        if found == nil { found = await remote(asset, name: name, api: api) }
        if let found { memory.setObject(found, forKey: cacheKey) }
        return found
    }

    /// The smallest available cloud copy: a video's 640×360 analysis proxy beats its full original.
    private static func remote(_ asset: SavedMediaAsset, name: String, api: any KriaAPIClient) async -> UIImage? {
        let source = asset.kind == "video" ? (asset.availableSource(role: "analysis_proxy") ?? asset.source) : asset.source
        guard let source else { return nil }
        await gate.acquire()
        defer { Task { await gate.release() } }
        guard !Task.isCancelled,
              let access = try? await api.savedMediaAccess(assetID: asset.id, sourceID: source.id),
              access.generation == source.generation else { return nil }
        if asset.kind == "video" {
            await CreationMediaPreview.save(localURL: access.url, mediaID: name)
        } else {
            guard let (download, response) = try? await URLSession.shared.download(from: access.url),
                  (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
            let ext = (asset.filename as NSString).pathExtension
            let named = download.appendingPathExtension(ext.isEmpty ? "jpg" : ext)
            defer { try? FileManager.default.removeItem(at: download); try? FileManager.default.removeItem(at: named) }
            guard (try? FileManager.default.moveItem(at: download, to: named)) != nil else { return nil }
            await CreationMediaPreview.save(localURL: named, mediaID: name)
        }
        return await CreationMediaPreview.image(mediaID: name, projectID: nil)
    }
}

/// Counting gate: at most `limit` remote poster fetches at once, so a fast scroll can't open dozens of downloads.
private actor FetchGate {
    private let limit: Int
    private var running = 0
    private var waiting: [CheckedContinuation<Void, Never>] = []
    init(limit: Int) { self.limit = limit }
    func acquire() async {
        if running < limit { running += 1; return }
        await withCheckedContinuation { waiting.append($0) }
    }
    func release() {
        if waiting.isEmpty { running -= 1 } else { waiting.removeFirst().resume() }
    }
}
