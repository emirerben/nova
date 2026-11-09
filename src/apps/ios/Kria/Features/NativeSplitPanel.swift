import KriaMediaEngine
import Photos
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// KRI-1335: the Split tool, the only way into split screen. With the playhead outside a split it creates one
/// (This clip | Whole video, 2 or 3 panels). Inside a split it fills each empty panel from Gallery or Saved, and
/// once every panel has footage it offers the layout count, the Layout area (gutter, background) and Remove split.
/// Panel heights are set by dragging the dividers in the preview; the timeline shows the split as one block.
struct NativeSplitPanel: View {
    enum Tab: String { case split = "Split" }

    @ObservedObject var session: NativeEditorSession
    let onDone: () -> Void
    @State private var tab = Tab.split
    @State private var scope = NativeSplitScope.selectedClip
    /// Empty panels the creator chose to fill later.
    @State private var skipped = Set<Int>()
    @State private var showsLayout = false

    private enum ScopeTab: String { case clip = "This clip", whole = "Whole video" }

    private var context: NativeSplitContext? { session.splitContextAtPlayhead }
    private var fillPanel: Int? { context?.emptyPanels.first { !skipped.contains($0) } }

    private var heading: String {
        guard let context else { return "Split screen" }
        guard let panel = fillPanel else { return "Split screen" }
        return "Add to " + NativeSplitScreenRows.panelTitle(index: panel, count: context.section.panelCount)
    }

    var body: some View {
        NativeEditorLanePanel(title: "Split", tabs: [Tab](), tab: $tab, onDone: onDone, heading: heading) {
            VStack(alignment: .leading, spacing: 14) {
                if let error = session.splitScreenActionError {
                    Text(error).kriaType(.caption).foregroundStyle(KriaColor.failureText)
                        .accessibilityIdentifier("native-editor-split-error")
                }
                if let context {
                    if let panel = fillPanel {
                        NativeSplitFillView(
                            session: session, context: context, panel: panel, multi: scope == .wholeVideo,
                            remaining: context.emptyPanels.filter { !skipped.contains($0) }.count,
                            skip: { skipped.insert(panel) }
                        )
                        .id(context.sectionID + "-" + String(panel))
                    } else {
                        editing(context)
                    }
                } else {
                    creating
                }
            }
        }
        .onAppear { if context == nil { selectClipUnderPlayhead() } }
    }

    // MARK: Create

    private var scopeBinding: Binding<ScopeTab> {
        Binding(
            get: { scope == .wholeVideo ? .whole : .clip },
            set: { scope = $0 == .whole ? .wholeVideo : .selectedClip }
        )
    }

    private var canCreate: Bool {
        scope == .wholeVideo ? session.canCreateWholeVideoSplit : session.canCreateSplitScreen
    }

    private var creating: some View {
        VStack(alignment: .leading, spacing: 14) {
            NativeEditorPanelTabs(
                tabs: [ScopeTab.clip, .whole], selection: scopeBinding, accessibilityPrefix: "native-editor-split-scope",
                identifier: { "native-editor-split-scope-" + ($0 == .whole ? "whole" : "clip") },
                isEnabled: { $0 == .clip || session.canCreateWholeVideoSplit }
            )
            HStack(spacing: 12) {
                layoutCard(2)
                layoutCard(3)
            }
            Text(scope == .wholeVideo
                 ? "Your whole video becomes the Top panel. Add footage for the rest next."
                 : "Starts with this clip. You pick the rest next.")
                .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                .frame(maxWidth: .infinity, alignment: .center)
        }
    }

    private func layoutCard(_ count: Int) -> some View {
        Button {
            session.createSplitScreen(panelCount: count, scope: scope)
        } label: {
            VStack(spacing: 10) {
                VStack(spacing: 2) {
                    ForEach(0..<count, id: \.self) { index in
                        ZStack {
                            if index == 0 {
                                KriaColor.ink.opacity(0.14)
                                Image(systemName: "play.fill").font(.system(size: 13)).foregroundStyle(KriaColor.ink.opacity(0.55))
                            } else {
                                KriaColor.sky.opacity(0.42)
                                Image(systemName: "plus").font(.system(size: 14, weight: .semibold)).foregroundStyle(KriaColor.ink)
                            }
                        }
                    }
                }
                .frame(width: 58, height: 104)
                .clipShape(RoundedRectangle(cornerRadius: 9))
                Text("\(count) panels").kriaType(.button).foregroundStyle(KriaColor.ink)
            }
            .frame(maxWidth: .infinity)
            .padding(.vertical, 14)
            .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 20))
            .contentShape(RoundedRectangle(cornerRadius: 20))
        }
        .buttonStyle(.plain)
        .disabled(!canCreate)
        .opacity(canCreate ? 1 : 0.5)
        .accessibilityLabel("\(count) panels")
        .accessibilityIdentifier("native-editor-split-create-\(count)")
    }

    /// Opened with nothing selected, "This clip" means the clip under the playhead.
    private func selectClipUnderPlayhead() {
        guard session.selection?.kind != .clip else { return }
        let time = session.currentTime
        guard let clip = session.timelineClips.first(where: { $0.start <= time && time < $0.end }) ?? session.timelineClips.last else { return }
        session.select(EditorSelection(kind: .clip, id: clip.id.uuidString), seekToStart: false)
    }

    // MARK: Edit

    @ViewBuilder private func editing(_ context: NativeSplitContext) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("Layout", selection: Binding(
                get: { context.section.panelCount },
                set: { session.setSplitScreenPanelCount($0) }
            )) {
                Text("2 panels").tag(2)
                Text("3 panels").tag(3)
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("native-editor-split-count")

            ForEach(context.emptyPanels, id: \.self) { panel in
                Button {
                    skipped.removeAll()
                } label: {
                    Label("Add to " + NativeSplitScreenRows.panelTitle(index: panel, count: context.section.panelCount), systemImage: "plus")
                }
                .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
                .accessibilityIdentifier("native-editor-split-fill-\(panel)")
            }

            Button {
                showsLayout.toggle()
            } label: {
                HStack {
                    Text("Layout").kriaType(.button).foregroundStyle(KriaColor.ink)
                    Spacer()
                    Image(systemName: showsLayout ? "chevron.up" : "chevron.down")
                        .font(.system(size: 13, weight: .semibold)).foregroundStyle(KriaColor.mutedInk)
                }
                .padding(.horizontal, 16)
                .frame(minHeight: 44)
                .background(KriaColor.fill, in: Capsule())
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityValue(showsLayout ? "Expanded" : "Collapsed")
            .accessibilityIdentifier("native-editor-split-layout-toggle")

            if showsLayout {
                VStack(alignment: .leading, spacing: 8) {
                    LabeledContent("Divider spacing", value: String(format: "%.1f%%", context.section.gutter * 100))
                    NativeEditorSlider(session: session, value: Binding(
                        get: { session.splitContextAtPlayhead?.section.gutter ?? context.section.gutter },
                        set: { session.setSplitScreenGutter($0) }
                    ), in: 0...0.1, step: 0.005) { Text("Divider spacing") }
                    .accessibilityIdentifier("native-editor-split-gutter")
                    ColorPicker("Background color", selection: Binding(
                        get: { nativeEditorColor(session.splitContextAtPlayhead?.section.backgroundColor ?? context.section.backgroundColor) },
                        set: { session.setSplitScreenBackgroundColor(nativeEditorHex($0)) }
                    ), supportsOpacity: false)
                    .accessibilityIdentifier("native-editor-split-background")
                    .accessibilityValue(context.section.backgroundColor)
                }
                .padding(.horizontal, 4)
            }

            Button("Remove split") { session.removeSelectedSplitScreen() }
                .buttonStyle(KriaButtonStyle(role: .destructive, size: .medium, fullWidth: true))
                .accessibilityIdentifier("native-editor-split-remove")
        }
        .onAppear { selectMember(of: context) }
        .onChange(of: context.sectionID) { _, _ in selectMember(of: context) }
        .disabled(!session.canEditSplitScreen)
    }

    /// The layout setters act on the selected clip's section, so make sure a member of this one is selected.
    private func selectMember(of context: NativeSplitContext) {
        guard session.selectedSplitScreenSectionID != context.sectionID,
              let slot = session.document.clips.first(where: { $0.splitSectionID == context.sectionID }),
              let id = slot.id else { return }
        session.select(EditorSelection(kind: .clip, id: id), seekToStart: false)
    }
}

// MARK: - Fill

/// "Add to Top": the picker for the first empty panel. One tap fills it and the panel moves on to the next empty one.
private struct NativeSplitFillView: View {
    @ObservedObject var session: NativeEditorSession
    let context: NativeSplitContext
    let panel: Int
    /// Whole-video split: several picks line up in this panel.
    let multi: Bool
    let remaining: Int
    let skip: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(remaining > 1 ? "\(remaining) panels still empty" : "Pick a photo or video for this panel")
                    .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                Spacer()
                if remaining > 1 {
                    Button("Skip", action: skip)
                        .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                        .accessibilityIdentifier("native-editor-split-skip")
                }
            }
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") {
                // UI tests fill from the footage already in the edit; the real pickers need Photos and an account.
                ForEach(session.splitScreenSourceChoices, id: \.clipIndex) { source in
                    Button(source.label) {
                        session.addSplitPanelClip(sectionID: context.sectionID, panel: panel, sourceClipIndex: source.clipIndex)
                    }
                    .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                    .accessibilityIdentifier("native-editor-split-source-\(source.clipIndex)")
                }
            }
            #endif
            NativeSplitMediaPicker(session: session, multi: multi) { url in
                await session.addSplitPanelClip(sectionID: context.sectionID, panel: panel, fileURL: url)
            }
        }
    }
}

// MARK: - Picker

/// Gallery | Saved. Gallery is the iPhone's photo library; Saved is the creator's saved uploads, offered only while
/// the server opens `media_library`. Choosing returns a local file to `onAdd`, which the editor then admits like any
/// other import.
struct NativeSplitMediaPicker: View {
    enum Tab: String { case gallery = "Gallery", saved = "Saved" }
    private enum Pick: Hashable { case photo(String), saved(UUID) }
    private typealias Loader = @MainActor () async throws -> URL

    @EnvironmentObject private var model: AppModel
    @ObservedObject var session: NativeEditorSession
    let multi: Bool
    let onAdd: @MainActor (URL) async -> Void
    @State private var tab = Tab.gallery
    @State private var savedAvailable = false
    @State private var savedAssets: [SavedMediaAsset] = []
    @State private var savedLoaded = false
    @State private var savedError: String?
    @State private var picked: [Pick] = []
    @State private var busy = false
    @State private var photoStatus = PHPhotoLibrary.authorizationStatus(for: .readWrite)
    @State private var systemPicks: [PhotosPickerItem] = []
    @State private var showsSystemPicker = false

    private var api: any KriaAPIClient { model.uploads.libraryAPI }
    private var working: Bool { busy || session.isAddingClip }
    private var pickedPhotoIDs: [String] {
        var ids: [String] = []
        for pick in picked { if case .photo(let id) = pick { ids.append(id) } }
        return ids
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-split-import") {
                Button("Use sample video") {
                    guard let url = Bundle.main.url(forResource: "istanbul", withExtension: "mp4") else { return }
                    add([{ url }])
                }
                .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                .accessibilityIdentifier("native-editor-split-import")
            }
            #endif
            if savedAvailable {
                NativeEditorPanelTabs(
                    tabs: [Tab.gallery, .saved], selection: $tab, accessibilityPrefix: "native-editor-split",
                    identifier: { "native-editor-split-tab-" + $0.rawValue.lowercased() }
                )
            }
            switch tab {
            case .gallery: gallery
            case .saved: saved
            }
            if working {
                HStack(spacing: 8) { KriaSpinner(size: 16); Text("Adding…").kriaType(.label).foregroundStyle(KriaColor.mutedInk) }
            }
            if let error = session.addClipError {
                Text(error).kriaType(.caption).foregroundStyle(KriaColor.failureText)
                    .accessibilityIdentifier("native-editor-split-source-error")
            }
            if multi, !picked.isEmpty {
                Button("Add \(picked.count) \(picked.count == 1 ? "clip" : "clips")") { add(picked.map { loader(for: $0) }) }
                    .buttonStyle(KriaButtonStyle(role: .primary, size: .large, fullWidth: true))
                    .disabled(working)
                    .accessibilityIdentifier("native-editor-split-add-picked")
            }
        }
        .task { await requestPhotosIfNeeded() }
        .task { await checkSaved() }
        .task(id: tab) { if tab == .saved { await loadSaved() } }
    }

    // MARK: Gallery

    @ViewBuilder private var gallery: some View {
        if photoStatus == .authorized || photoStatus == .limited {
            NativeSplitPhotoGrid(picked: pickedPhotoIDs, showsPicks: multi, disabled: working) { id in
                choose(.photo(id))
            }
        } else {
            // No library access: the system picker still works without it.
            Button { showsSystemPicker = true } label: {
                Label("Choose from Photos", systemImage: "photo.on.rectangle.angled")
                    .kriaType(.button).foregroundStyle(KriaColor.ink)
                    .frame(maxWidth: .infinity, minHeight: 48)
                    .background(KriaColor.fill, in: Capsule())
            }
            .photosPicker(isPresented: $showsSystemPicker, selection: $systemPicks, maxSelectionCount: multi ? 10 : 1,
                          selectionBehavior: .ordered, matching: .any(of: [.videos, .images]))
            .disabled(working)
            .accessibilityIdentifier("native-editor-split-system-picker")
            .onChange(of: systemPicks) { _, items in
                guard !items.isEmpty else { return }
                systemPicks = []
                add(items.map { loader(for: $0) })
            }
        }
    }

    private func requestPhotosIfNeeded() async {
        guard photoStatus == .notDetermined else { return }
        #if DEBUG
        // A permission alert would swallow the UI tests' taps.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") { return }
        #endif
        photoStatus = await PHPhotoLibrary.requestAuthorization(for: .readWrite)
    }

    // MARK: Saved

    @ViewBuilder private var saved: some View {
        if let savedError {
            Text(savedError).kriaType(.caption).foregroundStyle(KriaColor.failureText)
        } else if savedAssets.isEmpty {
            if savedLoaded {
                Text("No saved uploads yet. Photos and videos you add to a post show up here.")
                    .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
            } else {
                ProgressView().frame(maxWidth: .infinity)
            }
        } else {
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 4), count: 4), spacing: 4) {
                ForEach(savedAssets) { asset in
                    let number = picked.firstIndex(of: .saved(asset.id)).map { $0 + 1 }
                    Button { choose(.saved(asset.id)) } label: {
                        Color.clear
                            .aspectRatio(1, contentMode: .fit)
                            .overlay { SavedUploadThumbnail(asset: asset, owner: api.mediaLibraryOwner, api: api, size: nil) }
                            .clipped()
                            .modifier(GalleryTileChrome(number: multi ? number : nil, duration: asset.durationSeconds))
                    }
                    .buttonStyle(.plain)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .disabled(working)
                    .accessibilityLabel(asset.displayName)
                    .accessibilityAddTraits(number != nil ? .isSelected : [])
                    .accessibilityIdentifier("native-editor-split-saved-tile-\(asset.id)")
                }
            }
        }
    }

    private func checkSaved() async {
        let api = api
        guard api.mediaLibraryOwner != nil, let capabilities = try? await api.creationCapabilities(),
              capabilities.mediaLibrary == true else { return }
        savedAvailable = true
    }

    private func loadSaved() async {
        guard !savedLoaded else { return }
        do {
            let page = try await api.savedMedia(query: "", kind: nil, cursor: nil)
            savedAssets = page.assets.filter { $0.kind != "audio" }
            savedError = nil
        } catch {
            savedError = "Saved uploads couldn’t load. Try again in a moment."
        }
        savedLoaded = true
    }

    /// A saved upload's bytes: the original already on this iPhone, else its cloud original downloaded to a temporary file.
    /// A video whose original lives only on another device has no file here, and its small analysis copy never stands in.
    private static func file(for asset: SavedMediaAsset, api: any KriaAPIClient) async throws -> URL {
        if let owner = api.mediaLibraryOwner, let fingerprint = asset.fingerprint {
            let library = MediaLibraryOriginals(owner: owner)
            if let local = await Task.detached(operation: { library.localURL(assetID: asset.id, fingerprint: fingerprint) }).value { return local }
        }
        guard let source = asset.availableSource(role: "original") else { throw AddClipSourceUnreadable() }
        let access = try await api.savedMediaAccess(assetID: asset.id, sourceID: source.id)
        let (downloaded, _) = try await URLSession.shared.download(from: access.url)
        let fallback = asset.kind == "image" ? "jpg" : "mov"
        let ext = UTType(mimeType: source.contentType)?.preferredFilenameExtension ?? fallback
        let destination = FileManager.default.temporaryDirectory.appending(path: "\(UUID().uuidString)-saved.\(ext)")
        try FileManager.default.moveItem(at: downloaded, to: destination)
        return destination
    }

    // MARK: Picking

    private func loader(for item: PhotosPickerItem) -> Loader {
        {
            guard let media = try await item.loadTransferable(type: ImportedMedia.self) else { throw AddClipSourceUnreadable() }
            return media.url
        }
    }

    private func loader(for pick: Pick) -> Loader {
        switch pick {
        case .photo(let id):
            return {
                guard let media = try await PhotosPickerItem(itemIdentifier: id).loadTransferable(type: ImportedMedia.self) else {
                    throw AddClipSourceUnreadable()
                }
                return media.url
            }
        case .saved(let id):
            let api = api
            let asset = savedAssets.first { $0.id == id }
            return {
                guard let asset else { throw AddClipSourceUnreadable() }
                return try await Self.file(for: asset, api: api)
            }
        }
    }

    /// Single mode adds on the first tap. Multi mode numbers the picks and waits for Add.
    private func choose(_ pick: Pick) {
        guard !working else { return }
        if !multi {
            add([loader(for: pick)])
        } else if let index = picked.firstIndex(of: pick) {
            picked.remove(at: index)
        } else {
            picked.append(pick)
        }
    }

    private func add(_ loaders: [Loader]) {
        guard !busy else { return }
        busy = true
        session.addClipError = nil
        let onAdd = onAdd
        Task { @MainActor in
            defer { busy = false; picked = [] }
            for load in loaders {
                let url: URL
                do {
                    url = try await load()
                } catch {
                    session.addClipError = "This file couldn’t be read. Try another photo or video."
                    return
                }
                await onAdd(url)
                if session.addClipError != nil { return }
            }
        }
    }
}

/// The newest photos and videos on the iPhone as a plain grid inside the panel's own scroll view.
private struct NativeSplitPhotoGrid: View {
    let picked: [String]
    let showsPicks: Bool
    let disabled: Bool
    let onTap: (String) -> Void
    @State private var assets: PHFetchResult<PHAsset>?
    @State private var loader = GalleryThumbnailLoader()
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 4), count: 4)

    var body: some View {
        Group {
            if let assets, assets.count > 0 {
                LazyVGrid(columns: columns, spacing: 4) {
                    ForEach(0..<assets.count, id: \.self) { index in tile(assets.object(at: index), index: index) }
                }
            } else if assets != nil {
                Text("No photos or videos in Photos yet.").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    .frame(maxWidth: .infinity)
            } else {
                ProgressView().frame(maxWidth: .infinity)
            }
        }
        .task { load() }
    }

    private func load() {
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        options.predicate = NSPredicate(format: "mediaType == %d OR mediaType == %d", PHAssetMediaType.image.rawValue, PHAssetMediaType.video.rawValue)
        options.fetchLimit = 200
        assets = PHAsset.fetchAssets(with: options)
    }

    @ViewBuilder private func tile(_ asset: PHAsset, index: Int) -> some View {
        let id = asset.localIdentifier
        let number = picked.firstIndex(of: id).map { $0 + 1 }
        let isVideo = asset.mediaType == .video
        Button { onTap(id) } label: {
            Color.clear
                .aspectRatio(1, contentMode: .fit)
                .overlay { GalleryThumbnail(asset: asset, loader: loader) }
                .clipped()
                .modifier(Chrome(showsPicks: showsPicks, number: number, duration: isVideo ? asset.duration : nil))
        }
        .buttonStyle(.plain)
        .clipShape(RoundedRectangle(cornerRadius: 10))
        .disabled(disabled)
        .accessibilityLabel(isVideo ? "Video, \(GalleryTileChrome.duration(asset.duration))" : "Photo")
        .accessibilityAddTraits(number != nil ? .isSelected : [])
        .accessibilityIdentifier("native-editor-split-gallery-tile-\(index)")
    }

    /// Multi mode wears the shared pick ring and number; a tap-to-fill grid only shows a video's length.
    private struct Chrome: ViewModifier {
        let showsPicks: Bool
        let number: Int?
        let duration: TimeInterval?

        func body(content: Content) -> some View {
            if showsPicks {
                content.modifier(GalleryTileChrome(number: number, duration: duration))
            } else {
                content.overlay(alignment: .bottomTrailing) {
                    if let duration {
                        Text(GalleryTileChrome.duration(duration))
                            .font(KriaFont.body(11).weight(.semibold)).foregroundStyle(Color.white)
                            .padding(.horizontal, 5).padding(.vertical, 2)
                            .background(KriaColor.ink.opacity(0.7), in: Capsule())
                            .padding(4)
                    }
                }
            }
        }
    }
}

// MARK: - Replace

/// "Replace footage" for a clip inside a split: the same Gallery | Saved picker, swapping the clip's footage in place.
struct NativeSplitReplaceSheet: View {
    @ObservedObject var session: NativeEditorSession
    let slotID: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Replace footage").kriaType(.heading).foregroundStyle(KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button { dismiss() } label: { Image(systemName: "xmark").font(.system(size: 17, weight: .semibold)) }
                    .buttonStyle(.kriaIcon).accessibilityLabel("Close").accessibilityIdentifier("native-editor-split-replace-close")
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if !session.splitScreenSourceChoices.isEmpty {
                        VStack(alignment: .leading, spacing: 8) {
                            Text("In this edit").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                            HStack(spacing: 8) {
                                ForEach(session.splitScreenSourceChoices, id: \.clipIndex) { source in
                                    Button(source.label) {
                                        session.replaceSelectedClipSource(clipIndex: source.clipIndex)
                                        dismiss()
                                    }
                                    .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                                    .accessibilityIdentifier("native-editor-split-source-\(source.clipIndex)")
                                }
                            }
                        }
                    }
                    if session.canAddTimelineMedia {
                        NativeSplitMediaPicker(session: session, multi: false) { url in
                            // The session owns the upload, so the sheet can close as soon as a file is chosen.
                            dismiss()
                            await session.addClip(fileURL: url, replacingSlotID: slotID)
                        }
                    }
                }
            }
        }
        .padding(.horizontal, 24).padding(.top, 12)
        .background(KriaColor.paper)
        .presentationDetents([.large])
    }
}
