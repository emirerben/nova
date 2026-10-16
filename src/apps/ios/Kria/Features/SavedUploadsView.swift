import SwiftUI
import UniformTypeIdentifiers
import ImageIO
import KriaMediaEngine

struct SavedUploadsView: View {
    let projectID: UUID
    let itemID: String?
    let role: CreationMediaRole
    let destination: ProjectUploadDestination
    let capacity: Int
    @ObservedObject var uploads: BackgroundUploadCoordinator
    let onAdded: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var selectionBudget: Int?
    @State private var originalRevision = 0
    @State private var query = ""
    @State private var kind = "all"
    @State private var assets: [SavedMediaAsset] = []
    @State private var cursor: String?
    @State private var activeCount = 0
    @State private var quota = 500
    @State private var loading = false
    @State private var error: String?
    @State private var loadID = UUID()
    @State private var reload = 0
    @State private var adding: UUID?
    @State private var added: Set<UUID> = []
    @State private var alreadyAttached: Set<UUID> = []
    @State private var loadedAttached = false
    @State private var mediaIDs: [UUID: String] = [:]
    @State private var archive: SavedMediaAsset?
    @State private var relinking: SavedMediaAsset?
    @State private var showsRelink = false
    @State private var restoring = false
    @State private var repairsCloud = false
    private var api: any KriaAPIClient { uploads.libraryAPI }
    private var owner: String? { api.mediaLibraryOwner }
    private var filterKind: String? { role.isAudio ? "audio" : role == .clip ? "video" : kind == "all" ? nil : kind }
    private var budget: Int { min(selectionBudget ?? capacity, capacity + added.count) }
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
                List {
                    Section {
                        Text("Reuse uploads in another post. Edits stay separate for each post.")
                            .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                        Text("\(activeCount) of \(quota) saved uploads")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                    if let error {
                        Section {
                            Text(error).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("saved-uploads-error")
                            Button("Try again") { reload += 1 }.accessibilityIdentifier("saved-uploads-retry")
                        }
                    }
                    if assets.isEmpty && !loading && error == nil {
                        ContentUnavailableView(query.isEmpty ? "No saved uploads yet" : "No matching uploads", systemImage: "square.stack", description: Text(query.isEmpty ? "Add media to a post from Photos or Files. Your uploads will appear here for reuse." : "Try another filename."))
                            .accessibilityIdentifier("saved-uploads-empty")
                    }
                    ForEach(assets) { asset in row(asset) }
                    if restoring { ProgressView("Restoring from original…") }
                    if loading { ProgressView("Loading saved uploads…") }
                    if cursor != nil && !loading {
                        Button("Load more") { Task { await load(more: true) } }.accessibilityIdentifier("saved-uploads-more")
                    }
                }
                .scrollContentBackground(.hidden)
                .refreshable { reload += 1 }
            }
            .background(KriaColor.paper)
            .navigationTitle("Saved uploads")
            .navigationBarTitleDisplayMode(.inline)
            .searchable(text: $query, prompt: "Search filenames")
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.disabled(adding != nil || restoring) } }
            .onAppear { if selectionBudget == nil { selectionBudget = capacity } }
            .task(id: searchIdentity) {
                do { try await Task.sleep(for: .milliseconds(200)); await load(more: false) } catch {}
            }
            .confirmationDialog("Remove from saved uploads?", isPresented: Binding(get: { archive != nil }, set: { if !$0 { archive = nil } }), titleVisibility: .visible) {
                Button("Remove from saved uploads", role: .destructive) {
                    guard let asset = archive else { return }
                    Task {
                        do { try await api.archiveSavedMedia(id: asset.id); assets.removeAll { $0.id == asset.id }; activeCount = max(0, activeCount - 1) }
                        catch { self.error = error.localizedDescription }
                    }
                }
            } message: { Text("Existing posts keep their media and edits.") }
            .fileImporter(isPresented: $showsRelink, allowedContentTypes: [.movie, .image, .audio], allowsMultipleSelection: false) { result in
                guard let asset = relinking else { return }
                relinking = nil
                guard case .success = result else { return }
                Task {
                    restoring = true
                    defer { restoring = false }
                    do {
                        guard let owner, let fingerprint = asset.fingerprint, let file = try result.get().first else { throw SavedMediaError.originalRequired }
                        let scoped = file.startAccessingSecurityScopedResource()
                        defer { if scoped { file.stopAccessingSecurityScopedResource() } }
                        try await MediaLibraryOriginals(owner: owner).relink(assetID: asset.id, fingerprint: fingerprint, file: file)
                        let updated: SavedMediaAsset
                        if repairsCloud || asset.source == nil {
                            updated = try await api.repairSavedMedia(asset: asset, file: file)
                            replace(updated)
                        } else { updated = asset }
                        error = nil
                        await add(updated)
                    } catch { self.error = "This file must match the original video. \(error.localizedDescription)" }
                }
            }
            .interactiveDismissDisabled(adding != nil || restoring)
        }
    }

    private func row(_ asset: SavedMediaAsset) -> some View {
        HStack(alignment: .top, spacing: 12) {
            SavedUploadThumbnail(asset: asset, owner: owner, api: api) { originalRevision += 1 }
            VStack(alignment: .leading, spacing: 6) {
                Text(asset.displayName).font(KriaFont.body(15).weight(.medium)).lineLimit(2)
                Text(availability(asset)).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                if !asset.storageWarnings.isEmpty { Text("Cloud copy expires soon. Saved posts keep their media.").font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText) }
                if !asset.usage.isEmpty { Text("Used in \(Set(asset.usage.compactMap(\.creationThreadID)).count) posts").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk) }
                if added.contains(asset.id) || alreadyAttached.contains(asset.id) { Label("Added to this post", systemImage: "checkmark").font(KriaFont.body(13)) }
                else {
                    Button { Task { await add(asset) } } label: {
                        if adding == asset.id { ProgressView("Adding…") }
                        else { Text("Add to post") }
                    }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("saved-upload-add-\(asset.id)")
                    .disabled(restoring || adding != nil || added.count >= budget || !canUse(asset))
                }
                if asset.source == nil || (asset.kind == "video" && asset.originalAvailability == "device_only" && !hasLocal(asset)) {
                    Button(asset.source == nil ? "Restore from original…" : "Locate original…") { repairsCloud = asset.source == nil; relinking = asset; showsRelink = true }
                        .accessibilityIdentifier("saved-upload-relink-\(asset.id)")
                }
            }
            Spacer(minLength: 0)
            Menu {
                Button("Restore cloud copy from original…") { repairsCloud = true; relinking = asset; showsRelink = true }
                if asset.status == "failed" {
                    Button("Retry analysis") { Task { do { let updated = try await api.retrySavedMedia(id: asset.id); replace(updated) } catch { self.error = error.localizedDescription } } }
                }
                Button("Remove from saved uploads", role: .destructive) { archive = asset }
            } label: { Image(systemName: "ellipsis").frame(minWidth: 44, minHeight: 44) }
            .accessibilityLabel("Options for \(asset.displayName)")
        }
        .padding(.vertical, 8)
    }
    private func hasLocal(_ asset: SavedMediaAsset) -> Bool {
        _ = originalRevision
        guard let owner, let fingerprint = asset.fingerprint else { return false }
        return MediaLibraryOriginals(owner: owner).localURL(assetID: asset.id, fingerprint: fingerprint) != nil
    }
    private func availability(_ asset: SavedMediaAsset) -> String {
        if asset.source == nil { return "Cloud copy expired or unavailable. Restore it from the original to reuse it." }
        if asset.kind == "video", asset.originalAvailability == "device_only" {
            return hasLocal(asset) ? "Original on this iPhone" : "Full-quality original needed on this iPhone"
        }
        return asset.status == "failed" ? "Analysis needs retry" : asset.status == "ready" ? "Available to reuse" : "Analysis in progress"
    }
    private func canUse(_ asset: SavedMediaAsset) -> Bool {
        guard owner != nil, let source = asset.source, destination.canUpload else { return false }
        if role.isAudio { return asset.kind == "audio" }
        if role == .clip { return asset.kind == "video" }
        guard source.role == "original", asset.kind != "audio" else { return false }
        if case .phoneVisuals(let kinds) = destination {
            return kinds.contains(asset.kind == "image" ? .image : .video)
        }
        return true
    }
    private func replace(_ updated: SavedMediaAsset) { assets = assets.map { $0.id == updated.id ? updated : $0 } }
    private func load(more: Bool) async {
        let search = searchIdentity
        let identity = UUID(); loadID = identity
        loading = true; error = nil
        if !more { assets = []; cursor = nil }
        do {
            if !loadedAttached {
                if role == .visual, let itemID {
                    alreadyAttached = Set(try await api.visuals(itemID: itemID).assets.compactMap(\.libraryAssetID))
                } else {
                    let thread = try await api.project(threadID: projectID)
                    alreadyAttached = Set(CreationAttachedMedia.parse(thread.state ?? [:]).filter { media in
                        role == .song ? media.isSong : role == .voiceover ? media.isVoiceover : media.kind == "video"
                    }.compactMap(\.libraryAssetID))
                }
                loadedAttached = true
            }
            let page = try await api.savedMedia(query: query.trimmingCharacters(in: .whitespacesAndNewlines), kind: filterKind, cursor: more ? cursor : nil)
            guard loadID == identity, searchIdentity == search, !Task.isCancelled else { return }
            let allowed = page.assets.filter { role != .visual || $0.kind != "audio" }
            assets = more ? assets + allowed.filter { candidate in !assets.contains { $0.id == candidate.id } } : allowed
            cursor = page.nextCursor; activeCount = page.activeCount; quota = page.limit
            loading = false
        } catch {
            guard loadID == identity, searchIdentity == search, !Task.isCancelled else { return }
            self.error = error.localizedDescription; loading = false
        }
    }
    private func add(_ asset: SavedMediaAsset) async {
        guard adding == nil, !added.contains(asset.id), !alreadyAttached.contains(asset.id), added.count < budget, let owner, let source = asset.source else { return }
        adding = asset.id; error = nil
        defer { adding = nil }
        let mediaID = mediaIDs[asset.id] ?? (source.role == "analysis_proxy" ? "analysis-proxy-" : "") + UUID().uuidString
        mediaIDs[asset.id] = mediaID
        do {
            let thread = try await SavedMediaReuse.attach(asset: asset, source: source, threadID: projectID, itemID: itemID, role: role, mediaID: mediaID, owner: owner, api: api)
            uploads.acceptSavedMediaThread(thread)
            added.insert(asset.id)
            onAdded()
        } catch {
            if error is SavedMediaError { relinking = asset; showsRelink = true }
            self.error = error.localizedDescription
        }
    }
}

private struct SavedUploadThumbnail: View {
    let asset: SavedMediaAsset
    let owner: String?
    let api: any KriaAPIClient
    let didFindOriginal: () -> Void
    @State private var image: UIImage?
    var body: some View {
        Group {
            if let image { Image(uiImage: image).resizable().scaledToFill() }
            else { Image(systemName: asset.kind == "audio" ? "music.note" : asset.kind == "image" ? "photo" : "video").foregroundStyle(KriaColor.mutedInk) }
        }
        .frame(width: 58, height: 72).background(KriaColor.paper).clipShape(RoundedRectangle(cornerRadius: 8))
        .accessibilityHidden(true)
        .task(id: asset.id) {
            let key = "library-\(asset.id)"
            image = await CreationMediaPreview.image(mediaID: key, projectID: nil)
            guard image == nil, asset.kind != "audio" else { return }
            // Migrate before displaying availability; only this owner's authorized usages are scanned.
            if let owner, let local = try? await SavedMediaReuse.adoptOriginal(asset: asset, owner: owner, api: api) {
                didFindOriginal()
                await CreationMediaPreview.save(localURL: local, mediaID: key)
            } else if let source = asset.source,
                      let access = try? await api.savedMediaAccess(assetID: asset.id, sourceID: source.id), access.generation == source.generation {
                if asset.kind == "image" {
                    if let (data, response) = try? await URLSession.shared.data(from: access.url),
                       (response as? HTTPURLResponse)?.statusCode == 200,
                       let source = CGImageSourceCreateWithData(data as CFData, nil),
                       let thumb = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceThumbnailMaxPixelSize: 320
                       ] as CFDictionary) { image = UIImage(cgImage: thumb) }
                    return
                }
                await CreationMediaPreview.save(localURL: access.url, mediaID: key)
            }
            image = await CreationMediaPreview.image(mediaID: key, projectID: nil)
        }
    }
}
