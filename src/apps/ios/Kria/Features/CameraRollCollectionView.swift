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
    @State private var query = ""
    @State private var researchTopic = ""
    @State private var stories: CameraRollStories?
    @State private var showsStories = false
    @State private var showsSaved = false
    @State private var showsOptions = false
    @State private var showsGroups = false
    @State private var showsTaste = false
    @State private var tasteAvailable = false
    @State private var confirmsUpload = false
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

    private struct EventGroup: Identifiable {
        let id: String
        let name: String
        let entries: [CameraRollCollection.Entry]
    }
    private var groups: [EventGroup] {
        var order: [String] = []
        var buckets: [String: [CameraRollCollection.Entry]] = [:]
        for entry in collection.entries {
            let key = collection.group(for: entry.id)
            if buckets[key] == nil { order.append(key) }
            buckets[key, default: []].append(entry)
        }
        return order.compactMap { name in
            guard let entries = buckets[name], let first = entries.first else { return nil }
            return EventGroup(id: first.id, name: name, entries: entries)
        }
    }
    private var canFindIdeas: Bool {
        !collection.entries.isEmpty && originals != nil && !fixture && !metadataOnly
    }

    var body: some View {
        NavigationStack {
            collectionScroll
            .background(KriaColor.paper)
            .scrollDismissesKeyboard(.interactively)
            .safeAreaInset(edge: .bottom, spacing: 0) { actionBar }
            .navigationTitle("Collection").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Menu {
                        Button("New collection") { newCollection() }.accessibilityIdentifier("collection-new")
                        ForEach(recentCollections) { choice in Button(choice.name) { openCollection(choice.id) } }
                        Divider()
                        Button("Edit groups") { showsGroups = true }.disabled(collection.entries.isEmpty)
                        Button("Analysis options") { showsOptions = true }
                        if tasteAvailable { Button("Idea preferences") { showsTaste = true }.accessibilityIdentifier("editorial-preferences") }
                    } label: { Image(systemName: "ellipsis").frame(width: 44, height: 44) }
                    .accessibilityLabel("Collection options").accessibilityIdentifier("collection-options").disabled(running)
                }
                ToolbarItem(placement: .confirmationAction) { Button("Done") { persist(); dismiss() }.disabled(running) }
            }
            .photosPicker(isPresented: $showsPicker, selection: $selection,
                          maxSelectionCount: max(1, CameraRollCollection.maximumCount - collection.entries.count),
                          matching: .any(of: [.images, .videos]), photoLibrary: .shared())
            .sheet(isPresented: $showsStories) {
                if let stories { CameraRollStoriesView(api: api, collection: collection, result: stories, cacheOnly: cacheOnly, onSelected: onSelected) }
            }
            .sheet(isPresented: $showsSaved) {
                CameraRollSavedPicker(api: api, capacity: CameraRollCollection.maximumCount - collection.entries.count) { addSaved($0) }
            }
            .sheet(isPresented: $showsOptions) { options }
            .sheet(isPresented: $showsGroups) { groupEditor }
            .sheet(isPresented: $showsTaste) { EditorialTasteView(api: api) { _ in } }
            .confirmationDialog("Find ideas in this footage?", isPresented: $confirmsUpload, titleVisibility: .visible) {
                Button("Upload and find ideas") {
                    work = Task {
                        await upload()
                        if collection.entries.allSatisfy({ $0.savedAsset != nil }) && !Task.isCancelled { await findStories() }
                    }
                }
                Button("Cancel", role: .cancel) { }
            } message: { Text("Kria uploads smaller video copies for analysis. Full-quality videos stay on your iPhone.") }
            .onChange(of: selection) { _, picked in add(picked); selection = [] }
            .task {
                restore(); await refreshMetadata()
                if !fixture && !metadataOnly { tasteAvailable = (try? await api.editorialTaste()) != nil }
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { work = Task { await refreshMetadata() } }
                else { persist() }
            }
            .onDisappear { work?.cancel(); persist() }
            .interactiveDismissDisabled(running)
            .kriaPage()
        }
    }

    private var collectionScroll: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 28) {
                VStack(alignment: .leading, spacing: 8) {
                    TextField("Collection name", text: $collection.name)
                        .font(KriaFont.display(30)).accessibilityIdentifier("collection-name")
                        .submitLabel(.done).onSubmit { persist() }
                    HStack {
                        Text("\(collection.entries.count) items")
                            .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        Spacer()
                        Menu {
                            Button("Photos and videos") { showsPicker = true }
                            Button("Saved uploads") { showsSaved = true }.disabled(fixture || metadataOnly)
                        } label: {
                            Label("Add", systemImage: "plus").font(KriaFont.body(14).weight(.semibold))
                                .padding(.horizontal, 14).frame(minHeight: 44)
                                .background(KriaColor.selectionSoft, in: Capsule())
                        }
                        .accessibilityIdentifier("collection-choose")
                        .disabled(running || collection.entries.count >= CameraRollCollection.maximumCount)
                    }
                }
                if collection.entries.isEmpty {
                    VStack(spacing: 18) {
                        Image(systemName: "photo.on.rectangle.angled").font(.system(size: 36)).foregroundStyle(KriaColor.zinc)
                        Text("What did you capture?").font(KriaFont.display(25))
                        Button("Add photos and videos") { showsPicker = true }
                            .buttonStyle(KriaPrimaryButtonStyle())
                    }.frame(maxWidth: .infinity).padding(.vertical, 64)
                }
                ForEach(groups) { group in collectionGroup(group) }
                if PHPhotoLibrary.authorizationStatus(for: .readWrite) == .notDetermined {
                    Button("Suggest groups from Photos dates") {
                        Task { _ = await PHPhotoLibrary.requestAuthorization(for: .readWrite); await refreshMetadata() }
                    }.font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk).frame(minHeight: 44)
                }
            }.padding(24)
        }
    }

    private func collectionGroup(_ group: EventGroup) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            if groups.count > 1 || group.name != collection.name { groupHeading(group) }
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 3), spacing: 8) {
                ForEach(group.entries) { entry in
                    Color.clear.aspectRatio(1, contentMode: .fit)
                        .overlay {
                            GeometryReader { geometry in
                                CameraRollCollectionThumbnail(entry: entry, api: api, metadataOnly: metadataOnly, size: geometry.size)
                            }
                        }
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                        .overlay(alignment: .topTrailing) {
                            Button { remove(entry.id) } label: {
                                Image(systemName: "xmark").font(.system(size: 10, weight: .bold))
                                    .frame(width: 24, height: 24).background(KriaColor.paper.opacity(0.94), in: Circle())
                                    .frame(width: 44, height: 44)
                            }.buttonStyle(.plain).accessibilityLabel("Remove item")
                                .accessibilityIdentifier("collection-remove-\(entry.id)").disabled(running)
                        }
                        .overlay(alignment: .bottomTrailing) {
                            if let duration = videoDuration(entry) {
                                Text(LibraryGalleryGrid.duration(duration))
                                    .font(KriaFont.body(11).weight(.semibold)).foregroundStyle(.white)
                                    .padding(.horizontal, 6).padding(.vertical, 3)
                                    .background(KriaColor.ink.opacity(0.75), in: Capsule()).padding(6)
                            }
                        }
                        .accessibilityElement(children: .contain)
                        .accessibilityLabel(entry.savedAsset?.displayName ?? (entry.metadata?.kind == "image" ? "Photo" : "Video"))
                }
            }
        }
    }

    private func videoDuration(_ entry: CameraRollCollection.Entry) -> Double? {
        if let metadata = entry.metadata, metadata.kind == "video" { return metadata.duration }
        if entry.savedAsset?.kind == "video", let raw = entry.savedAsset?.metadata["duration_s"], case .number(let value) = raw, value.isFinite, value > 0 { return value }
        return nil
    }
    private func groupHeading(_ group: EventGroup) -> some View {
        HStack(spacing: 12) {
            TextField("Event name", text: Binding(get: { groupDrafts[group.id] ?? group.name }, set: { value in
                groupDrafts[group.id] = value
                for entry in group.entries { collection.setGroup(value, for: entry.id) }
                persist()
            }))
            .font(KriaFont.body(16).weight(.semibold))
            .accessibilityIdentifier("collection-group-\(group.id)").submitLabel(.done)
            Image(systemName: "pencil").font(.system(size: 12)).foregroundStyle(KriaColor.zinc).accessibilityHidden(true)
        }.frame(minHeight: 44)
    }
    private var groupEditor: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    ForEach(groups) { group in
                        VStack(alignment: .leading, spacing: 4) {
                            groupHeading(group)
                            Text("\(group.entries.count) items").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                        }
                    }
                }.padding(24)
            }.background(KriaColor.paper).navigationTitle("Groups").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showsGroups = false }.accessibilityIdentifier("collection-groups-done") } }.kriaPage()
        }.presentationDetents([.medium, .large])
    }

    private var actionBar: some View {
        VStack(spacing: 12) {
            if let error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("collection-error") }
            if let status { Text(status).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk).accessibilityIdentifier("collection-status") }
            if running {
                HStack { ProgressView(); Text("Preparing your footage…").font(KriaFont.body(14)); Spacer(); Button("Cancel") { work?.cancel() } }
                    .frame(minHeight: 48)
            } else {
                HStack(spacing: 10) {
                    Image(systemName: "text.bubble").foregroundStyle(KriaColor.zinc)
                    TextField("Any direction? Optional", text: $query).font(KriaFont.body(14))
                        .accessibilityIdentifier("collection-story-query")
                }.padding(.horizontal, 16).frame(minHeight: 48)
                    .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 16))
                Button { requestIdeas() } label: { Text("Find story ideas").frame(maxWidth: .infinity) }
                    .buttonStyle(KriaPrimaryButtonStyle()).disabled(!canFindIdeas)
                    .accessibilityIdentifier("collection-find-stories")
                DisclosureGroup("Research an angle") {
                    TextField("Public place or topic", text: $researchTopic)
                        .font(KriaFont.body(14)).textInputAutocapitalization(.sentences)
                        .accessibilityIdentifier("collection-research-topic")
                    Text("Kria finds interesting facts and their sources.")
                        .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }.font(KriaFont.body(13))
            }
        }.padding(.horizontal, 24).padding(.vertical, 16).background(KriaColor.paper)
    }
    private var options: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 24) {
                Toggle("Use cached analysis only", isOn: $cacheOnly).disabled(cacheOnlyRequired)
                Text("Reuses previous analysis without new model calls.").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                DisclosureGroup("About uploads") {
                    Text("Videos use a smaller analysis copy. Full-quality originals stay on this iPhone. Photos are saved to uploads. iCloud originals may need to download.")
                        .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk).padding(.top, 8)
                }
                Spacer()
            }.padding(24).navigationTitle("Analysis options").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showsOptions = false } } }.kriaPage()
        }.presentationDetents([.medium])
    }
    @MainActor private func requestIdeas() {
        error = nil; status = nil
        if collection.entries.contains(where: { $0.savedAsset == nil }) {
            if cacheOnly { error = "This footage needs analysis. Check Analysis options to continue." }
            else { confirmsUpload = true }
        } else { work = Task { await findStories() } }
    }
    @MainActor private func newCollection() {
        persist()
        collection = try! CameraRollCollection(name: "My outing", identifiers: [])
        revision = 0; providers = [:]; groupDrafts = [:]; stories = nil; status = nil; error = nil
        persist()
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
            if !missing.isEmpty { status = "Ideas use the available footage. \(missing.count) clips aren't included yet." }
            let topic = researchTopic.trimmingCharacters(in: .whitespacesAndNewlines)
            let found = try await api.cameraRollStories(id: collection.id, revision: revision, query: query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Find a story in this outing" : query, cacheOnly: cacheOnly, researchTopic: topic.isEmpty ? nil : topic)
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

struct CameraRollCollectionThumbnail: View {
    let entry: CameraRollCollection.Entry
    let api: any KriaAPIClient
    var metadataOnly = false
    let size: CGSize
    @State private var asset: PHAsset?
    @State private var loader = GalleryThumbnailLoader()
    var body: some View {
        ZStack {
            KriaColor.softZinc
            if !metadataOnly, let saved = entry.savedAsset {
                SavedUploadThumbnail(asset: saved, owner: api.mediaLibraryOwner, api: api, size: size) { }
            } else if !metadataOnly, let asset {
                GalleryThumbnail(asset: asset, loader: loader)
            } else {
                Image(systemName: entry.metadata?.kind == "image" ? "photo" : "video")
                    .font(.system(size: 24)).foregroundStyle(KriaColor.zinc)
            }
        }.frame(width: size.width, height: size.height).clipped().accessibilityHidden(true)
            .task(id: entry.id) {
                guard !metadataOnly, entry.savedAsset == nil, !entry.id.hasPrefix("fixture-"), !entry.id.hasPrefix("picker:") else { return }
                asset = PHAsset.fetchAssets(withLocalIdentifiers: [entry.id], options: nil).firstObject
            }
    }
}
