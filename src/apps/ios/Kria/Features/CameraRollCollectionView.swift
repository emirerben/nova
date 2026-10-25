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
    @State private var collectionChanged = false
    @State private var latestCollection: SavedCameraRollCollection?
    @State private var showsLatestReview = false
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
    @State private var reopensOptions = false
    @State private var researchExpanded = false
    @State private var showsGroups = false
    @State private var showsTaste = false
    @State private var tasteAvailable = false
    @State private var confirmsUpload = false
    @State private var cacheOnly = true
    @State private var missingAnalysisCount = 0
    @State private var timingReviewCount = 0
    @State private var readyVideoCount = 0
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
            .disabled(running)
            .background(KriaColor.paper)
            .scrollDismissesKeyboard(.interactively)
            .safeAreaInset(edge: .bottom, spacing: 0) { actionBar }
            .navigationTitle("Collection").navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Button("New collection") { newCollection() }.accessibilityIdentifier("collection-new")
                        ForEach(recentCollections) { choice in Button(choice.name) { openCollection(choice.id) } }
                        Divider()
                        Button("Edit groups") { showsGroups = true }.disabled(collection.entries.isEmpty)
                        Button("Idea options") { showsOptions = true }
                        if tasteAvailable { Button("Idea preferences") { showsTaste = true }.accessibilityIdentifier("editorial-preferences") }
                    } label: { Image(systemName: "ellipsis").frame(width: 44, height: 44) }
                    .accessibilityLabel("Collection options").accessibilityIdentifier("collection-options").disabled(running)
                }
                ToolbarItem(placement: .cancellationAction) { Button("Done") { persist(); dismiss() }.disabled(running) }
            }
            .photosPicker(isPresented: $showsPicker, selection: $selection,
                          maxSelectionCount: max(1, CameraRollCollection.maximumCount - collection.entries.count),
                          matching: .any(of: [.images, .videos]), photoLibrary: .shared())
            .sheet(isPresented: $showsStories, onDismiss: {
                if reopensOptions { reopensOptions = false; showsOptions = true }
            }) {
                if let stories {
                    CameraRollStoriesView(api: api, collection: collection, result: stories, cacheOnly: cacheOnlyRequired || cacheOnly, onAddBackground: { topic in
                        researchTopic = topic; researchExpanded = true; reopensOptions = true
                    }, onSelected: onSelected, onReviewed: { reviewed in
                        if var saved = collection.matchingSavedIdeas {
                            saved.result = reviewed
                            collection.savedIdeas = saved
                            persist()
                        }
                    })
                }
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
                        if await upload(), !Task.isCancelled { await findStories() }
                    }
                }
                Button("Cancel", role: .cancel) { }
            } message: { Text("Kria uploads smaller video copies for analysis. Full-quality videos stay on your iPhone.") }
            .confirmationDialog("Open the updated collection?", isPresented: $showsLatestReview, titleVisibility: .visible) {
                Button("Open latest collection") { work = Task { await adoptLatestCollection() } }
                Button("Keep my local collection", role: .cancel) { }
            } message: {
                Text("\(latestCollection?.name ?? collection.name) · \(latestCollection?.members?.count ?? 0) items. Your local changes will remain available as a separate collection.")
            }
            .onChange(of: selection) { _, picked in add(picked); selection = [] }
            .task {
                cacheOnly = cacheOnlyRequired
                restore(); await refreshMetadata()
                if !fixture && !metadataOnly { tasteAvailable = (try? await api.editorialTaste()) != nil }
            }
            .onChange(of: scenePhase) { _, phase in
                if phase == .active && !running { work = Task { await refreshMetadata() } }
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
                if let saved = collection.matchingSavedIdeas {
                    Label("\(saved.result.stories.filter { !$0.rejected }.count) saved ideas", systemImage: "checkmark.circle")
                        .font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
                        .accessibilityIdentifier("collection-saved-ideas")
                }
            }.padding(24)
        }
    }

    private func collectionGroup(_ group: EventGroup) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            if groups.count > 1 { Text(group.name).font(KriaFont.body(16).weight(.semibold)) }
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
                Button {
                    if collection.matchingSavedIdeas != nil { work = Task { await openSavedStories() } }
                    else { requestIdeas() }
                } label: {
                    Text(collection.matchingSavedIdeas == nil ? "Find story ideas" : "View saved ideas").frame(maxWidth: .infinity)
                }
                .buttonStyle(KriaPrimaryButtonStyle()).disabled(!canFindIdeas || collectionChanged)
                .accessibilityIdentifier("collection-find-stories")
                if missingAnalysisCount + timingReviewCount > 0 && readyVideoCount > 0 {
                    Button { work = Task { await findStories(allowPartial: true) } } label: {
                        Text("Use \(readyVideoCount) ready \(readyVideoCount == 1 ? "video" : "videos")").frame(maxWidth: .infinity)
                    }.buttonStyle(KriaSecondaryButtonStyle()).disabled(collectionChanged)
                        .accessibilityIdentifier("collection-use-ready-videos")
                }
                if collection.matchingSavedIdeas != nil {
                    Button("Find more ideas") { showsOptions = true }
                        .font(KriaFont.body(14)).frame(minHeight: 44)
                }
                if collectionChanged {
                    Button("Review latest collection") { work = Task { await loadLatestCollection() } }
                        .font(KriaFont.body(14)).frame(minHeight: 44)
                        .accessibilityIdentifier("collection-review-latest")
                }
            }
        }.padding(.horizontal, 24).padding(.vertical, 16).background(KriaColor.paper)
    }
    private var options: some View {
        NavigationStack {
            Form {
                Section("Direction") {
                    TextField("Any direction? Optional", text: $query)
                        .accessibilityIdentifier("collection-story-query")
                    DisclosureGroup("Research an angle", isExpanded: $researchExpanded) {
                        TextField("Public place or topic", text: $researchTopic)
                            .accessibilityIdentifier("collection-research-topic")
                        if researchTopic.isEmpty, let topic = stories?.suggestedResearchTopic {
                            Button("Add background facts: \(topic)") { researchTopic = topic }
                                .accessibilityIdentifier("collection-research-suggestion")
                        }
                        Text("Only this topic is searched. Your clips and direction stay private.")
                            .font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    }
                }
                if !cacheOnlyRequired {
                    Section {
                        Toggle("Use saved analysis only", isOn: $cacheOnly)
                    }
                }
                Section {
                    DisclosureGroup("About uploads") {
                        Text("Videos use a smaller analysis copy. Full-quality originals stay on this iPhone. iCloud originals may need to download.")
                    }
                    if PHPhotoLibrary.authorizationStatus(for: .readWrite) == .notDetermined {
                        Button("Suggest groups from Photos dates") {
                            work = Task { _ = await PHPhotoLibrary.requestAuthorization(for: .readWrite); await refreshMetadata() }
                        }
                    }
                }
                Section {
                    Button("Find story ideas") { showsOptions = false; requestIdeas() }
                        .disabled(!canFindIdeas || running)
                }
            }.font(KriaFont.body(15)).navigationTitle("Idea options").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { showsOptions = false } } }.kriaPage()
        }.presentationDetents([.medium, .large])
    }
    @MainActor private func requestIdeas() {
        error = nil; status = nil
        if collection.entries.contains(where: { $0.savedAsset == nil }) {
            if cacheOnlyRequired || cacheOnly { error = CameraRollCollectionFailure.unavailable }
            else { confirmsUpload = true }
        } else { work = Task { await findStories() } }
    }
    @MainActor private func newCollection() {
        persist()
        collection = try! CameraRollCollection(name: "My outing", identifiers: [])
        missingAnalysisCount = 0; timingReviewCount = 0; readyVideoCount = 0
        revision = 0; providers = [:]; groupDrafts = [:]; stories = nil; status = nil; error = nil; collectionChanged = false; query = ""; researchTopic = ""
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
        missingAnalysisCount = 0; timingReviewCount = 0; readyVideoCount = 0
        do { try collection.select(identifiers); persist(); work = Task { await refreshMetadata() } }
        catch { self.error = "Choose at most \(CameraRollCollection.maximumCount) items." }
    }

    @MainActor private func remove(_ id: String) {
        guard !running else { return }
        try? collection.select(collection.entries.map(\.id).filter { $0 != id })
        missingAnalysisCount = 0; timingReviewCount = 0; readyVideoCount = 0
        providers.removeValue(forKey: id)
        groupDrafts.removeValue(forKey: id)
        persist()
    }

    @MainActor private func refreshMetadata() async {
        guard !fixture, !running else { return }
        running = true
        let started = ContinuousClock.now
        defer {
            running = false
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

    @MainActor private func upload() async -> Bool {
        guard let originals, !fixture, !metadataOnly, !cacheOnly, !cacheOnlyRequired else { return false }
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
            return true
        } catch is CancellationError { status = "Paused. Completed uploads are saved; you can resume." }
        catch { showCollectionError(error) }
        return false
    }

    @MainActor private func addSaved(_ assets: [SavedMediaAsset]) {
        do {
            try collection.select(collection.entries.map(\.id) + assets.map { "saved:\($0.id)" })
            for asset in assets { collection.bind(asset, to: "saved:\(asset.id)") }
            missingAnalysisCount = 0; timingReviewCount = 0; readyVideoCount = 0
            persist()
        } catch { self.error = "Choose at most \(CameraRollCollection.maximumCount) items." }
    }

    @MainActor private func openSavedStories() async {
        guard !running, let saved = collection.matchingSavedIdeas else { return }
        running = true; error = nil; status = nil
        defer { running = false }
        do {
            var found = try await api.savedCameraRollStories(id: collection.id, revision: saved.revision, storyIDs: saved.result.stories.map(\.id))
            found.suggestedResearchTopic = saved.result.suggestedResearchTopic
            try Task.checkCancellation()
            stories = found
            showsStories = true
        } catch is CancellationError { }
        catch {
            if error as? APIError == .unsupported || error as? APIError == .invalidResponse || error as? APIError == .requestFailed(status: 404) || error as? APIError == .requestFailed(status: 405) {
                self.error = "Saved ideas cannot be opened on this server. Your collection is saved."
            } else { showCollectionError(error) }
        }
    }

    @MainActor private func findStories(allowPartial: Bool = false) async {
        guard !fixture, !metadataOnly else { return }
        guard !running else { return }
        running = true; error = nil; status = nil; collectionChanged = false
        missingAnalysisCount = 0; timingReviewCount = 0; readyVideoCount = 0
        defer { running = false }
        do {
            let saved = try await api.saveCameraRollCollection(collection, revision: revision)
            revision = saved.revision; collection.serverRevision = revision; persist()
            var offset = 0
            var missing = Set<UUID>()
            var unavailable = Set<UUID>()
            var untimed = Set<UUID>()
            var unusableTiming = Set<UUID>()
            repeat {
                try Task.checkCancellation()
                let pass = try await api.indexCameraRollCollection(id: collection.id, revision: revision, offset: offset)
                missing.formUnion(pass.missing); unavailable.formUnion(pass.unavailable); untimed.formUnion(pass.untimed)
                unusableTiming.formUnion(pass.unusableTiming ?? [])
                guard let next = pass.nextOffset else { break }
                offset = next
            } while offset < CameraRollCollection.maximumCount
            guard unavailable.isEmpty else { status = "\(unavailable.count) uploads are unavailable. Remove or restore them before continuing."; return }
            missingAnalysisCount = missing.count
            timingReviewCount = unusableTiming.count
            let excluded = missing.union(unusableTiming)
            let videos = collection.entries.filter { ($0.savedAsset?.kind ?? $0.metadata?.kind) == "video" }.count
            readyVideoCount = max(0, videos - excluded.count)
            let requestedPartial = allowPartial
            guard excluded.isEmpty || requestedPartial else {
                if missing.isEmpty {
                    status = "\(unusableTiming.count) \(unusableTiming.count == 1 ? "video needs" : "videos need") a usable source window. You can use the ready videos."
                } else if unusableTiming.isEmpty {
                    status = "\(missing.count) videos still need analysis. Wait for them, or use the ready videos."
                } else {
                    status = "\(missing.count) videos need analysis and \(unusableTiming.count) need timing review. You can use the ready videos."
                }
                return
            }
            let topic = researchTopic.trimmingCharacters(in: .whitespacesAndNewlines)
            let requestedQuery = query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Suggest different content ideas from this outing" : query
            let found = try await api.cameraRollStories(id: collection.id, revision: revision, query: requestedQuery, cacheOnly: cacheOnlyRequired || cacheOnly, researchTopic: topic.isEmpty ? nil : topic, allowPartialAnalysis: requestedPartial)
            try Task.checkCancellation()
            if let selection = collection.remoteSelection {
                collection.savedIdeas = SavedCameraRollIdeas(revision: revision, selection: selection, query: requestedQuery, researchTopic: topic.isEmpty ? nil : topic, result: found, allowPartialAnalysis: requestedPartial)
                persist()
            }
            stories = found
            if !untimed.isEmpty { status = "Story ideas currently use videos. Your \(untimed.count) photos remain in the collection." }
            showsStories = true
        } catch is CancellationError { status = "Paused. Your collection is saved." }
        catch { showCollectionError(error) }
    }

    @MainActor private func showCollectionError(_ failure: Error) {
        collectionChanged = CameraRollCollectionFailure.isChanged(failure)
        error = CameraRollCollectionFailure.message(failure)
    }

    @MainActor private func loadLatestCollection() async {
        guard !running else { return }
        running = true
        defer { running = false }
        do {
            let latest = try await api.cameraRollCollection(id: collection.id)
            guard latest.id == collection.id, latest.name != nil, latest.members != nil else { throw APIError.invalidResponse }
            latestCollection = latest; showsLatestReview = true
        } catch { showCollectionError(error) }
    }

    @MainActor private func adoptLatestCollection() async {
        guard !running, let latest = latestCollection, let name = latest.name, let members = latest.members, let originals else { return }
        running = true
        defer { running = false }
        do {
            var rebuilt = try CameraRollCollection(id: latest.id, name: name, identifiers: members.map { "saved:\($0.asset_id)" })
            for member in members {
                try Task.checkCancellation()
                let asset = try await api.savedMediaAsset(id: member.asset_id)
                guard api.mediaLibraryOwner == originals.owner else { throw SourceAssetError.invalidBinding }
                let id = "saved:\(asset.id)"
                rebuilt.bind(asset, to: id); rebuilt.setGroup(member.group, for: id)
            }
            guard rebuilt.remoteSelection == CameraRollCollectionSelection(name: name, members: members) else { throw APIError.invalidResponse }
            rebuilt.serverRevision = latest.revision
            try collection.localCopy().save(in: originals)
            try rebuilt.save(in: originals)
            collection = rebuilt; revision = latest.revision
            missingAnalysisCount = 0; timingReviewCount = 0; readyVideoCount = 0
            providers = [:]; groupDrafts = [:]; stories = nil; collectionChanged = false; error = nil
            status = "Opened the latest collection. Your local changes are saved separately."
            persist()
        } catch { showCollectionError(error) }
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
            missingAnalysisCount = 0; timingReviewCount = 0; readyVideoCount = 0
            query = collection.savedIdeas?.query ?? ""; researchTopic = collection.savedIdeas?.researchTopic ?? ""
            collectionChanged = false
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
            query = saved.savedIdeas?.query ?? ""; researchTopic = saved.savedIdeas?.researchTopic ?? ""
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
                SavedUploadThumbnail(asset: saved, owner: api.mediaLibraryOwner, api: api, size: size)
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
