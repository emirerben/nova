import KriaMediaEngine
import Photos
import PhotosUI
import SwiftUI
import UniformTypeIdentifiers

/// KRI-1335: the Split tool, the only way into split screen. With the playhead outside a split it creates one
/// (This clip | Whole video, 2 or 3 panels). Inside a split it fills each empty panel from Gallery or Saved, and
/// once every panel has footage it offers extend, per-panel add/loop, full screen, the layout count, the Layout area
/// (gutter, background) and Remove split. Panel heights are set by dragging the dividers in the preview; the timeline
/// shows the split as one block. A tap on a panel's ghost in the preview arrives here as `addRequest`.
struct NativeSplitPanel: View {
    enum Tab: String { case split = "Split" }

    @ObservedObject var session: NativeEditorSession
    /// A ghost tapped in the preview: open the picker for that panel. Consumed (set back to nil) once adopted.
    @Binding var addRequest: NativeSplitAddRequest?
    let onDone: () -> Void
    @State private var tab = Tab.split
    /// Seeded once from the session (and again when the playhead leaves a split), never while the creator is choosing.
    @State private var scope: NativeSplitScope
    /// The panel the picker is explicitly aimed at ("Add to Top", a ghost tap). Nil lets the first empty panel lead.
    @State private var target: Int?
    /// Empty panels the creator chose to fill later.
    @State private var skipped = Set<Int>()
    @State private var showsLayout = false
    @State private var confirmsRemove = false
    @State private var pendingPanelCount: Int?

    init(session: NativeEditorSession, addRequest: Binding<NativeSplitAddRequest?>, onDone: @escaping () -> Void) {
        self.session = session
        _addRequest = addRequest
        self.onDone = onDone
        _scope = State(initialValue: session.defaultSplitScope)
    }

    private enum ScopeTab: String { case clip = "This clip", whole = "Whole video" }

    private var context: NativeSplitContext? { session.splitContextAtPlayhead }
    private var fillPanel: Int? { context?.emptyPanels.first { !skipped.contains($0) } }
    /// The explicit target while it still names a panel of the section under the playhead.
    private var explicitTarget: Int? {
        guard let context, let target, (0..<context.section.panelCount).contains(target) else { return nil }
        return target
    }

    private var heading: String {
        guard let context else { return "Split screen" }
        guard let panel = explicitTarget ?? fillPanel else { return "Split screen" }
        return "Add to " + NativeSplitScreenRows.panelTitle(index: panel, count: context.section.panelCount)
    }

    var body: some View {
        NativeEditorLanePanel(title: "Split", tabs: [Tab](), tab: $tab, onDone: onDone, heading: heading) {
            VStack(alignment: .leading, spacing: 14) {
                if let error = session.splitScreenActionError {
                    Text(error).kriaType(.caption).foregroundStyle(KriaColor.failureText)
                        .accessibilityIdentifier("native-editor-split-error")
                }
                if let hint = session.splitTrimHint {
                    Text(hint).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                        .accessibilityIdentifier("native-editor-split-trim-hint")
                }
                if let context {
                    if let panel = explicitTarget ?? fillPanel {
                        let explicit = explicitTarget != nil
                        NativeSplitFillView(
                            session: session, context: context, panel: panel,
                            // Several picks line up only in a panel that has no footage yet.
                            multi: scope == .wholeVideo && context.emptyPanels.contains(panel),
                            explicit: explicit,
                            remaining: context.emptyPanels.filter { !skipped.contains($0) }.count,
                            skip: { skipped.insert(panel) },
                            cancel: { target = nil },
                            finished: { target = nil }
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
        .onAppear {
            if context == nil { selectClipUnderPlayhead() }
            adoptRequest()
        }
        .onChange(of: addRequest) { _, _ in adoptRequest() }
        // Leaving a split starts the next creation from the session's default again; staying never resets it.
        .onChange(of: context?.sectionID) { _, id in
            target = nil
            skipped.removeAll()
            if id == nil { scope = session.defaultSplitScope }
        }
        .onChange(of: session.wholeVideoUnavailableReason) { _, reason in
            if reason != nil, scope == .wholeVideo { scope = .selectedClip }
        }
    }

    /// A ghost tap names a section and panel; honour it only while that section is still under the playhead.
    private func adoptRequest() {
        guard let request = addRequest else { return }
        addRequest = nil
        guard let context, context.sectionID == request.sectionID,
              (0..<context.section.panelCount).contains(request.panel) else { return }
        skipped.remove(request.panel)
        target = request.panel
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
        let reason = session.wholeVideoUnavailableReason
        return VStack(alignment: .leading, spacing: 14) {
            NativeEditorPanelTabs(
                tabs: [ScopeTab.clip, .whole], selection: scopeBinding, accessibilityPrefix: "native-editor-split-scope",
                identifier: { "native-editor-split-scope-" + ($0 == .whole ? "whole" : "clip") },
                isEnabled: { $0 == .clip || reason == nil }
            )
            if let reason {
                Text(reason).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("native-editor-split-scope-reason")
            }
            HStack(spacing: 12) {
                layoutCard(2)
                layoutCard(3)
            }
            Text(scope == .wholeVideo
                 ? "Your whole video becomes the Top panel."
                 : "Starts with the clip under the playhead.")
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
        let sectionID = context.sectionID
        let count = context.section.panelCount
        let withFootage = (0..<count).filter { !context.emptyPanels.contains($0) }
        VStack(alignment: .leading, spacing: 12) {
            Picker("Layout", selection: Binding(
                get: { context.section.panelCount },
                set: { count in
                    if session.splitClipsRemoved(sectionID: sectionID, panelCount: count) > 0 {
                        pendingPanelCount = count
                    } else {
                        session.setSplitScreenPanelCount(count)
                    }
                }
            )) {
                Text("2 panels").tag(2)
                Text("3 panels").tag(3)
            }
            .pickerStyle(.segmented)
            .accessibilityIdentifier("native-editor-split-count")
            .confirmationDialog("Remove footage?", isPresented: Binding(
                get: { pendingPanelCount != nil }, set: { if !$0 { pendingPanelCount = nil } }
            ), titleVisibility: .visible) {
                Button("Remove footage", role: .destructive) {
                    if let count = pendingPanelCount { session.setSplitScreenPanelCount(count) }
                    pendingPanelCount = nil
                }
                .accessibilityIdentifier("native-editor-split-count-confirm")
                Button("Cancel", role: .cancel) { pendingPanelCount = nil }
                    .accessibilityIdentifier("native-editor-split-count-cancel")
            } message: {
                if let count = pendingPanelCount {
                    let removed = session.splitClipsRemoved(sectionID: sectionID, panelCount: count)
                    Text("Going to \(count) panels removes \(removed == 1 ? "1 clip" : "\(removed) clips") from the dropped panel. Undo brings \(removed == 1 ? "it" : "them") back.")
                }
            }

            if session.canCollapseSplitPanels || context.section.collapseEmptyPanels {
                VStack(alignment: .leading, spacing: 6) {
                    Text("When a panel ends").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    Picker("When a panel ends", selection: Binding(
                        get: { session.splitContextAtPlayhead?.section.collapseEmptyPanels ?? context.section.collapseEmptyPanels },
                        set: { session.setSplitCollapse(sectionID: sectionID, $0) }
                    )) {
                        Text("Others fill the screen").tag(true)
                        Text("Leave it empty").tag(false)
                    }
                    .pickerStyle(.segmented)
                    .accessibilityIdentifier("native-editor-split-collapse")
                }
            }

            if session.canExtendSplitToWholeVideo(sectionID: sectionID) {
                actionRow("Extend split to whole video", icon: "arrow.left.and.right", id: "native-editor-split-extend-whole") {
                    session.extendSplitToWholeVideo(sectionID: sectionID)
                }
            }

            ForEach(0..<count, id: \.self) { panel in
                panelActions(panel, context: context)
            }

            actionRow("Full screen from playhead", icon: "arrow.up.left.and.arrow.down.right", id: "native-editor-split-full-from",
                      enabled: session.canMakeSplitFullScreen(sectionID: sectionID, side: .fromPlayhead)) {
                session.makeSplitFullScreen(sectionID: sectionID, side: .fromPlayhead)
            }
            actionRow("Full screen until playhead", icon: "arrow.up.left.and.arrow.down.right", id: "native-editor-split-full-until",
                      enabled: session.canMakeSplitFullScreen(sectionID: sectionID, side: .untilPlayhead)) {
                session.makeSplitFullScreen(sectionID: sectionID, side: .untilPlayhead)
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

            // Removing keeps ONE panel's footage and drops the rest, so the creator chooses which.
            Button("Remove split") { confirmsRemove = true }
                .buttonStyle(KriaButtonStyle(role: .destructive, size: .medium, fullWidth: true))
                .accessibilityHint("Asks which panel to keep")
                .accessibilityIdentifier("native-editor-split-remove")
        }
        .onAppear { selectMember(of: context) }
        .onChange(of: context.sectionID) { _, _ in selectMember(of: context) }
        .disabled(!session.canEditSplitScreen)
        .confirmationDialog("Remove split?", isPresented: $confirmsRemove, titleVisibility: .visible) {
            ForEach(withFootage, id: \.self) { panel in
                Button("Keep " + NativeSplitScreenRows.panelTitle(index: panel, count: count)) {
                    session.removeSplitScreen(sectionID: sectionID, keepPanel: panel)
                }
                .accessibilityIdentifier("native-editor-split-keep-\(panel)")
            }
            Button("Cancel", role: .cancel) {}
                .accessibilityIdentifier("native-editor-split-keep-cancel")
        } message: {
            Text("The video goes back to full screen with only the panel you keep. The other panels' footage is removed.")
        }
    }

    /// One panel's footage actions. An empty panel only offers Add (its id stays `-fill-`); a panel with footage
    /// offers Add to it (at the playhead gap, else after its last clip) and Loop to fill.
    @ViewBuilder private func panelActions(_ panel: Int, context: NativeSplitContext) -> some View {
        let title = NativeSplitScreenRows.panelTitle(index: panel, count: context.section.panelCount)
        let sectionID = context.sectionID
        if context.emptyPanels.contains(panel) {
            actionRow("Add to " + title, icon: "plus", id: "native-editor-split-fill-\(panel)") {
                skipped.remove(panel)
                target = panel
            }
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text(title).kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                let canAdd = session.canAddToSplitPanel(sectionID: sectionID, panel: panel)
                let canLoop = Self.hasRoomToLoop(session: session, context: context, panel: panel)
                let reason = session.canEditSplitScreen ? NativeEditorSession.splitPanelFullMessage : nil
                HStack(spacing: 8) {
                    actionRow("Add to " + title, icon: "plus", id: "native-editor-split-add-\(panel)",
                              enabled: canAdd, reason: reason) {
                        target = panel
                    }
                    actionRow("Loop to fill", icon: "repeat", id: "native-editor-split-loop-\(panel)",
                              label: "Loop \(title) to fill", enabled: canLoop, reason: reason) {
                        session.loopFillSplitPanel(sectionID: sectionID, panel: panel)
                    }
                }
                if let reason, !canAdd || !canLoop {
                    Text(reason).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("native-editor-split-full-reason-\(panel)")
                }
            }
        }
    }

    private func actionRow(_ title: String, icon: String, id: String, label: String? = nil, enabled: Bool = true,
                           reason: String? = nil, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Label(title, systemImage: icon).lineLimit(1).minimumScaleFactor(0.8)
        }
        .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium, fullWidth: true))
        .disabled(!enabled)
        .accessibilityLabel(label ?? title)
        .accessibilityHint(enabled ? "" : reason ?? "")
        .accessibilityIdentifier(id)
    }

    /// Loop repeats a panel's clips after its last one until the section ends, so it needs free time there.
    private static func hasRoomToLoop(session: NativeEditorSession, context: NativeSplitContext, panel: Int) -> Bool {
        let members = session.document.clips.filter {
            !$0.removed && $0.splitSectionID == context.sectionID && $0.splitPanelIndex == panel
        }
        guard !members.isEmpty else { return false }
        let end = members.map { $0.splitOffsetS + ($0.durationS ?? 0) }.max() ?? 0
        return end < context.section.durationS - 0.05
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

/// "Add to Top": the picker for one panel. Without an explicit target it leads with the first empty panel and one tap
/// fills it, moving on to the next empty one. With an explicit target (a row or a preview ghost) it stays aimed at that
/// panel, at the playhead gap or after the panel's last clip, until the pick lands or the creator cancels.
private struct NativeSplitFillView: View {
    @ObservedObject var session: NativeEditorSession
    let context: NativeSplitContext
    let panel: Int
    /// Whole-video split into an empty panel: several picks line up in it.
    let multi: Bool
    let explicit: Bool
    let remaining: Int
    let skip: () -> Void
    let cancel: () -> Void
    /// A pick (or batch) landed without an error.
    let finished: () -> Void

    private var caption: String {
        guard explicit else { return remaining > 1 ? "\(remaining) panels still empty" : "Pick a photo or video for this panel" }
        switch NativeSplitPlayback.state(session: session, context: context, panel: panel) {
        case .empty: return "Pick a photo or video for this panel"
        case .gap: return "Goes in the free time at the playhead."
        case .playing: return "Goes after this panel's last clip."
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text(caption).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                Spacer()
                if explicit {
                    Button("Back", action: cancel)
                        .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                        .accessibilityIdentifier("native-editor-split-add-cancel")
                } else if remaining > 1 {
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
                        if explicit { finished() }
                    }
                    .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                    .accessibilityIdentifier("native-editor-split-source-\(source.clipIndex)")
                }
            }
            #endif
            NativeSplitMediaPicker(session: session, multi: multi, onFinished: finished) { url in
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
    /// Called once a pick (or a whole multi batch) has been added without an error.
    var onFinished: @MainActor () -> Void = {}
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
            if working {
                HStack(spacing: 8) { KriaSpinner(size: 16); Text("Adding…").kriaType(.label).foregroundStyle(KriaColor.mutedInk) }
            }
            switch tab {
            case .gallery: gallery
            case .saved: saved
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
        .onAppear { session.addClipAlertSuppressed = true }
        .onDisappear { session.addClipAlertSuppressed = false }
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
            if let id = item.itemIdentifier { return try await PhotoItemFileLoader().load(item: item, identifier: id) }
            guard let media = try await item.loadTransferable(type: ImportedMedia.self) else { throw AddClipSourceUnreadable() }
            return media.url
        }
    }

    private func loader(for pick: Pick) -> Loader {
        switch pick {
        case .photo(let id):
            return { try await PhotosLibraryAssetExporter().exportFile(localIdentifier: id) }
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
            onFinished()
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
