import SwiftUI
import Photos
import PhotosUI
import KriaMediaEngine

/// Explicit selection and cheap metadata precede every media read. Account-scoped snapshots
/// survive interruptions; system-picker providers remain ephemeral and require reselection on resume.
struct CameraRollCollectionView: View {
    let api: any KriaAPIClient
    var fixture = false
    var cacheOnlyRequired = true
    var onSelected: @MainActor (CreationThread) async -> Void = { _ in }
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var collection = try! CameraRollCollection(name: "My outing", identifiers: [])
    @State private var selection: [PhotosPickerItem] = []
    @State private var providers: [String: PhotosPickerItem] = [:]
    @State private var groupDrafts: [String: String] = [:]
    @State private var showsPicker = false
    @State private var error: String?
    @State private var status: String?
    @State private var running = false
    @State private var work: Task<Void, Never>?
    @State private var revision = 0
    @State private var previousPermission: PHAuthorizationStatus?
    @State private var recentCollections: [LocalCollectionChoice] = []
    @State private var query = "Find a story in this outing"
    @State private var stories: CameraRollStories?
    @State private var showsStories = false
    @State private var showsSaved = false
    @State private var cacheOnly = true
    @State private var metadataElapsedMs = 0.0
    @State private var resumedCollection = false
    private var metadataOnly: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("-camera-roll-device-collection")
        #else
        false
        #endif
    }
    private var originals: MediaLibraryOriginals? {
        let owner = metadataOnly ? "camera-roll-device-collection" : (fixture ? "camera-roll-ui-fixture" : api.mediaLibraryOwner)
        return owner.map { MediaLibraryOriginals(owner: $0) }
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    if metadataOnly { Text("Device check: metadata only. Upload and analysis are disabled.").font(.footnote) }
                    Button("New collection") {
                        persist()
                        collection = try! CameraRollCollection(name: "My outing", identifiers: [])
                        revision = 0; providers = [:]; groupDrafts = [:]; stories = nil; status = nil; error = nil
                        persist()
                    }.disabled(running).accessibilityIdentifier("collection-new")
                    TextField("Collection name", text: $collection.name).accessibilityIdentifier("collection-name")
                    Text("Add up to 50 photos and videos from one outing. Adding an item includes it in this collection; no second selection is needed. Review the groups before sending anything.")
                        .font(.subheadline).foregroundStyle(.secondary)
                    Button("Add photos and videos…") { showsPicker = true }
                        .accessibilityIdentifier("collection-choose").disabled(running || collection.entries.count >= CameraRollCollection.maximumCount)
                    Button("Choose from saved uploads…") { showsSaved = true }
                        .disabled(running || collection.entries.count >= CameraRollCollection.maximumCount || fixture || metadataOnly)
                    if PHPhotoLibrary.authorizationStatus(for: .readWrite) == .notDetermined {
                        Button("Use Photos dates to suggest groups") {
                            Task { _ = await PHPhotoLibrary.requestAuthorization(for: .readWrite); await refreshMetadata() }
                        }.font(.footnote)
                    }
                    if !recentCollections.isEmpty {
                        Menu("Saved collections") {
                            ForEach(recentCollections) { choice in
                                Button(choice.name) { openCollection(choice.id) }
                            }
                        }
                    }
                    Text("\(collection.entries.count) added · \(collection.completedCount) with metadata")
                        .font(.footnote).foregroundStyle(.secondary)
                    if collection.entries.allSatisfy({ $0.savedAsset == nil }) {
                        Text("No media has been uploaded for this collection.").font(.footnote)
                    }
                }
                ForEach(Array(collection.entries.enumerated()), id: \.element.id) { index, entry in
                    Section("Item \(index + 1)") {
                        if let metadata = entry.metadata {
                            Text(metadata.kind == "image" ? "Photo" : "Video · \(Int(metadata.duration)) seconds")
                            Text("\(metadata.width) × \(metadata.height)").font(.caption).foregroundStyle(.secondary)
                        } else {
                            Text("Metadata unavailable. Reselect this item to import with its Photos picker permission.")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                        TextField("Event group", text: Binding(get: { groupDrafts[entry.id] ?? collection.group(for: entry.id) }, set: {
                            groupDrafts[entry.id] = $0
                            collection.setGroup($0, for: entry.id)
                            persist()
                        }))
                            .accessibilityIdentifier("collection-group-\(entry.id)")
                        if entry.savedAsset != nil { Label("Saved upload linked", systemImage: "checkmark.circle").font(.footnote) }
                        Button("Remove", role: .destructive) { remove(entry.id) }
                            .accessibilityIdentifier("collection-remove-\(entry.id)")
                    }
                }
                if let error { Section { Text(error).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("collection-error") } }
                if let status { Section { Text(status).accessibilityIdentifier("collection-status") } }
                Section {
                    if running {
                        ProgressView("Preparing selected material…")
                        Button("Cancel") { work?.cancel() }
                    } else {
                        Button("Save progress") { persist() }.accessibilityIdentifier("collection-save")
                        Toggle("Use cached analysis only", isOn: $cacheOnly).disabled(cacheOnlyRequired)
                        if cacheOnly { Text("No new model calls. Uncached results will be unavailable.").font(.footnote).foregroundStyle(.secondary) }
                        Button("Upload selected material for analysis") {
                            work = Task { await upload() }
                        }.disabled(collection.entries.isEmpty || originals == nil || fixture || metadataOnly || cacheOnly)
                            .accessibilityIdentifier("collection-analyze")
                        TextField("Story direction", text: $query).accessibilityIdentifier("collection-story-query")
                        Button("Find story ideas") { work = Task { await findStories() } }
                            .disabled(collection.entries.isEmpty || collection.entries.contains { $0.savedAsset == nil } || fixture || metadataOnly)
                            .accessibilityIdentifier("collection-find-stories")
                        Text("Videos send a smaller analysis copy. Full-quality originals stay on this iPhone. Photos are saved to your uploads. iCloud originals may need to download. You will review and approve a draft separately before rendering.")
                            .font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Camera-roll collection").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { persist(); dismiss() }.disabled(running) } }
            .photosPicker(isPresented: $showsPicker, selection: $selection,
                          maxSelectionCount: max(1, CameraRollCollection.maximumCount - collection.entries.count),
                          matching: .any(of: [.images, .videos]), photoLibrary: .shared())
            .sheet(isPresented: $showsStories) {
                if let stories { CameraRollStoriesView(api: api, collection: collection, result: stories, cacheOnly: cacheOnly, onSelected: onSelected) }
            }
            .sheet(isPresented: $showsSaved) {
                CameraRollSavedPicker(api: api, capacity: CameraRollCollection.maximumCount - collection.entries.count) { assets in
                    addSaved(assets)
                }
            }
            .onChange(of: selection) { _, picked in
                add(picked)
                selection = []
            }
            .task { restore(); await refreshMetadata() }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { work = Task { await refreshMetadata() } }
                else { persist() }
            }
            .onDisappear { work?.cancel(); persist() }
            .interactiveDismissDisabled(running)
        }
    }

    @MainActor private func add(_ items: [PhotosPickerItem]) {
        guard !items.isEmpty else { return }
        var identifiers = collection.entries.map(\.id)
        for item in items {
            let id = item.itemIdentifier ?? "picker:\(UUID())"
            providers[id] = item
            if !identifiers.contains(id) { identifiers.append(id) }
        }
        do { try collection.select(identifiers); persist(); work = Task { await refreshMetadata() } }
        catch { self.error = "Choose at most 50 items." }
    }

    @MainActor private func remove(_ id: String) {
        guard !running else { return }
        try? collection.select(collection.entries.map(\.id).filter { $0 != id })
        providers.removeValue(forKey: id)
        groupDrafts.removeValue(forKey: id)
        persist()
    }

    @MainActor private func refreshMetadata() async {
        guard !fixture, !running else { return }
        let started = ContinuousClock.now
        defer {
            let elapsed = started.duration(to: .now).components
            metadataElapsedMs = Double(elapsed.seconds) * 1000 + Double(elapsed.attoseconds) / 1e15
            persist()
        }
        let permission = PHPhotoLibrary.authorizationStatus(for: .readWrite)
        let canRead = permission == .authorized || permission == .limited
        if let previousPermission, [.authorized, .limited].contains(previousPermission), !canRead { providers.removeAll() }
        previousPermission = permission
        var accessible = Set(providers.keys).union(collection.entries.filter { $0.id.hasPrefix("saved:") }.map(\.id))
        let ids = collection.entries.map(\.id)
        for offset in stride(from: 0, to: ids.count, by: CameraRollCollection.batchSize) {
            guard !Task.isCancelled else { return }
            var metadata: [CameraRollMetadata] = []
            if canRead {
                PHAsset.fetchAssets(withLocalIdentifiers: Array(ids[offset..<min(offset + CameraRollCollection.batchSize, ids.count)]), options: nil)
                    .enumerateObjects { asset, _, _ in
                        accessible.insert(asset.localIdentifier)
                        metadata.append(CameraRollMetadata(id: asset.localIdentifier,
                            kind: asset.mediaType == .video ? "video" : "image", duration: asset.duration,
                            width: asset.pixelWidth, height: asset.pixelHeight, modifiedAt: asset.modificationDate,
                            capture: ClipCaptureSetting.isEnabled() ? ClipCaptureReader.read(assetIdentifier: asset.localIdentifier) : nil))
                    }
            }
            collection.record(metadata, sharesCaptureContext: ClipCaptureSetting.isEnabled())
            persist()
            await Task.yield()
        }
        // System-picker selections can be imported without broad Photos access. After relaunch
        // their provider is gone, so retain only already bound uploads or fresh accessible items.
        let before = Set(collection.entries.map(\.id))
        if canRead { collection.reconcileAccessibleIdentifiers(accessible) }
        else {
            collection.clearCaptureContext()
            collection.reconcileAccessibleIdentifiers(accessible)
        }
        let removed = before.subtracting(collection.entries.map(\.id))
        if !removed.isEmpty {
            status = "Removed \(removed.count) items that are no longer accessible."
            if revision > 0 {
                do {
                    var consented = collection
                    try consented.select(consented.entries.filter { $0.savedAsset != nil }.map(\.id))
                    let saved = try await api.saveCameraRollCollection(consented, revision: revision)
                    revision = saved.revision; collection.serverRevision = revision
                } catch { self.error = "Access changes are saved on this iPhone. Reconnect before finding stories." }
            }
        }
        persist()
    }

    @MainActor private func upload() async {
        guard let originals, !fixture, !metadataOnly, !cacheOnly, !cacheOnlyRequired else { return }
        running = true; error = nil; status = nil
        defer { running = false }
        do {
            for entry in collection.entries where entry.savedAsset == nil {
                try Task.checkCancellation()
                let item = providers[entry.id] ?? PhotosPickerItem(itemIdentifier: entry.id)
                let file = try await PhotoItemFileLoader().load(item: item, identifier: entry.id)
                defer { try? FileManager.default.removeItem(at: file) }
                let kind = entry.metadata?.kind ?? (item.supportedContentTypes.contains { $0.conforms(to: .movie) } ? "video" : "image")
                let asset = try await api.importCollectionMedia(file: file, kind: kind)
                guard api.mediaLibraryOwner == originals.owner else { throw SourceAssetError.invalidBinding }
                collection.bind(asset, to: entry.id)
                persist()
            }
            let saved = try await api.saveCameraRollCollection(collection, revision: revision)
            revision = saved.revision
            collection.serverRevision = revision
            persist()
            status = "Collection saved. Analysis is queued for the selected uploads."
        } catch is CancellationError { status = "Paused. Completed uploads are saved; you can resume." }
        catch { self.error = "Could not finish this collection. Completed uploads are saved. \(error.localizedDescription)" }
    }

    @MainActor private func addSaved(_ assets: [SavedMediaAsset]) {
        do {
            try collection.select(collection.entries.map(\.id) + assets.map { "saved:\($0.id)" })
            for asset in assets { collection.bind(asset, to: "saved:\(asset.id)") }
            persist()
        } catch { self.error = "Choose at most 50 items." }
    }

    @MainActor private func findStories() async {
        guard !fixture, !metadataOnly else { return }
        running = true; error = nil; status = nil
        defer { running = false }
        do {
            let saved = try await api.saveCameraRollCollection(collection, revision: revision)
            revision = saved.revision; collection.serverRevision = revision; persist()
            var offset = 0
            var missing = Set<UUID>()
            var unavailable = Set<UUID>()
            var untimed = Set<UUID>()
            repeat {
                try Task.checkCancellation()
                let pass = try await api.indexCameraRollCollection(id: collection.id, revision: revision, offset: offset)
                missing.formUnion(pass.missing); unavailable.formUnion(pass.unavailable); untimed.formUnion(pass.untimed)
                guard let next = pass.nextOffset else { break }
                offset = next
            } while offset < CameraRollCollection.maximumCount
            guard unavailable.isEmpty else { status = "\(unavailable.count) uploads are unavailable. Remove or restore them before continuing."; return }
            if !missing.isEmpty { status = "\(missing.count) videos still need analysis. Try again when analysis finishes."; return }
            let found = try await api.cameraRollStories(id: collection.id, revision: revision, query: query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Find a story in this outing" : query, cacheOnly: cacheOnly)
            stories = found
            if !untimed.isEmpty { status = "Story ideas currently use videos. Your \(untimed.count) photos remain in the collection." }
            showsStories = true
        } catch is CancellationError { status = "Paused. Your collection is saved." }
        catch { self.error = error.localizedDescription }
    }

    @MainActor private func persist() {
        guard let originals else { return }
        collection.name = String(collection.name.trimmingCharacters(in: .whitespacesAndNewlines).prefix(160))
        if collection.name.isEmpty { collection.name = "My outing" }
        do {
            try collection.save(in: originals); refreshCollectionChoices()
            #if DEBUG
            if metadataOnly {
                var usage = rusage(); getrusage(RUSAGE_SELF, &usage)
                let report: [String: Any] = [
                    "schema_version": 1, "captured_at": ISO8601DateFormatter().string(from: Date()),
                    "os_version": ProcessInfo.processInfo.operatingSystemVersionString,
                    "permission": PHPhotoLibrary.authorizationStatus(for: .readWrite).rawValue,
                    "selected_count": collection.entries.count, "metadata_count": collection.completedCount,
                    "photo_count": collection.entries.filter { $0.metadata?.kind == "image" }.count,
                    "video_count": collection.entries.filter { $0.metadata?.kind == "video" }.count,
                    "corrected_group_count": collection.entries.filter { $0.correctedGroup != nil }.count,
                    "group_count": Set(collection.entries.map { collection.group(for: $0.id) }).count,
                    "resumed_collection": resumedCollection, "metadata_elapsed_ms": metadataElapsedMs,
                    "process_peak_resident_bytes": usage.ru_maxrss, "media_bytes_read": 0,
                    "media_bytes_uploaded": 0, "provider_calls": 0, "analysed_candidate_count": 0,
                    "capture_context_enabled": ClipCaptureSetting.isEnabled(), "original_availability": "not_requested"
                ]
                let data = try JSONSerialization.data(withJSONObject: report, options: [.prettyPrinted, .sortedKeys])
                let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
                try data.write(to: documents.appendingPathComponent("camera-roll-collection-check.json"), options: .atomic)
            }
            #endif
        }
        catch { self.error = "Could not save collection progress." }
    }

    private struct LocalCollectionChoice: Identifiable { let id: UUID; let name: String }
    @MainActor private func refreshCollectionChoices() {
        guard let originals else { return }
        let directory = originals.root.appendingPathComponent("collection-indexes")
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        recentCollections = files.sorted { ((try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) > ((try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) }.prefix(20).compactMap { file in
            guard let id = UUID(uuidString: file.deletingPathExtension().lastPathComponent), id != collection.id,
                  let value = try? CameraRollCollection.load(id: id, in: originals, sharesCaptureContext: ClipCaptureSetting.isEnabled()) else { return nil }
            return LocalCollectionChoice(id: id, name: value.name)
        }
    }
    @MainActor private func openCollection(_ id: UUID) {
        guard let originals, !running else { return }
        persist()
        do {
            collection = try CameraRollCollection.load(id: id, in: originals, sharesCaptureContext: ClipCaptureSetting.isEnabled())
            resumedCollection = true
            revision = collection.serverRevision ?? 0; providers = [:]; groupDrafts = [:]; stories = nil; status = nil; error = nil
            work = Task { await refreshMetadata() }
        } catch { self.error = "Could not reopen this collection." }
    }

    @MainActor private func restore() {
        guard let originals else { return }
        let directory = originals.root.appendingPathComponent("collection-indexes")
        if fixture && ProcessInfo.processInfo.arguments.contains("-collection-fixture-reset") {
            try? FileManager.default.removeItem(at: directory)
        }
        let files = (try? FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey])) ?? []
        let latest = files.sorted { ((try? $0.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) > ((try? $1.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast) }.first
        if let latest, let id = UUID(uuidString: latest.deletingPathExtension().lastPathComponent),
           let saved = try? CameraRollCollection.load(id: id, in: originals, sharesCaptureContext: ClipCaptureSetting.isEnabled()) {
            collection = saved; revision = saved.serverRevision ?? 0
            resumedCollection = true
        } else if fixture {
            collection = try! CameraRollCollection(name: "Weekend outing", identifiers: ["fixture-video", "fixture-photo"])
            collection.record([
                CameraRollMetadata(id: "fixture-video", kind: "video", duration: 12, width: 1920, height: 1080, modifiedAt: nil, capture: nil),
                CameraRollMetadata(id: "fixture-photo", kind: "image", duration: 0, width: 3024, height: 4032, modifiedAt: nil, capture: nil)
            ], sharesCaptureContext: false)
        }
    }
}
