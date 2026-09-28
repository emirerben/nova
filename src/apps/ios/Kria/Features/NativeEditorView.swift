import SwiftUI

struct NativeEditorView: View {
    let project: ProjectSummary
    let libraryJobID: UUID?
    let onBack: () -> Void
    private let conversation: (() -> AnyView)?
    private let conversationAcceptedID: UUID?
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var session: NativeEditorSession
    @StateObject private var exporter = NativeEditorExporter()
    @StateObject private var panelDrafts = NativeEditorPanelDrafts()
    @StateObject private var panelLifecycle = NativeEditorPanelLifecycle()
    @State private var panel: NativeEditorPanel?
    @State private var inspector: NativeEditorInspector?
    @State private var showsUnsavedExit = false
    @State private var showsDeviceRender = false
    @State private var showsConversation = false
    @State private var selectedTextForActions: String?
    /// KRI-185: a block opened from the Text tab's list edits its words first and
    /// returns to that list; one opened from the timeline keeps the old behaviour.
    @State private var textEditOrigin: TextEditOrigin = .timeline
    private enum TextEditOrigin { case timeline, list }
    @State private var keyboardVisible = false
    /// KRI-170: the timeline handle and the panel handle are independent.
    /// `previewResize` is in points (positive shrinks the preview, negative
    /// grows it); `panelExpansion` is 0…1 and may lift the panel over the preview.
    @State private var previewResize: CGFloat = 0
    @State private var panelExpansion: CGFloat = 0
    @State private var topChromeHeight: CGFloat = 0
    /// `previewFullscreen` mounts the overlay; `fullscreenExpanded` drives its
    /// grow/shrink so the overlay can animate out before it is removed.
    @State private var previewFullscreen = false
    @State private var fullscreenExpanded = false
    @State private var previewGlobalFrame: CGRect = .zero
    @State private var wasPlayingBeforeFullscreen = false

    private var shouldReduceMotion: Bool {
        reduceMotion || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }

    init(
        project: ProjectSummary,
        initialDraft: EditorDraft? = nil,
        initialPlaybackURL: URL? = nil,
        libraryJobID: UUID? = nil,
        sharedSession: NativeEditorSession? = nil,
        conversationAcceptedID: UUID? = nil,
        conversation: (() -> AnyView)? = nil,
        onBack: @escaping () -> Void
    ) {
        self.project = project
        self.libraryJobID = libraryJobID
        self.onBack = onBack
        self.conversation = conversation
        self.conversationAcceptedID = conversationAcceptedID
        _session = StateObject(
            wrappedValue: sharedSession ?? initialDraft.map {
                NativeEditorSession(
                    draft: $0,
                    operations: LocalEditorOperations(),
                    initialPlaybackURL: initialPlaybackURL
                )
            }
                ?? NativeEditorSession(
                    project: project,
                    operations: LocalEditorOperations(),
                    initialPlaybackURL: initialPlaybackURL
                )
        )
    }

    var body: some View {
        GeometryReader { viewport in
            Group {
                switch session.loadState {
                case .loaded:
                    editor(viewport: viewport)
                case .idle, .loading:
                    if session.canDisplayCurrentPlayer {
                        editor(viewport: viewport)
                    } else {
                        NativeEditorLoadSurface(
                            title: "Opening the editor…",
                            detail: "Loading the latest cut and its editing controls.",
                            isLoading: true,
                            onBack: requestBack,
                            retry: nil
                        )
                    }
                case .failed(let message):
                    NativeEditorLoadSurface(
                        title: "The editor couldn’t open",
                        detail: message,
                        isLoading: false,
                        onBack: requestBack,
                        retry: { Task { await loadEditor() } }
                    )
                }
            }
            .frame(width: viewport.size.width, height: viewport.size.height, alignment: .top)
            .font(KriaFont.body())
            .foregroundStyle(KriaColor.ink)
            .background(KriaColor.paper)
            .overlay {
                if previewFullscreen {
                    fullscreenOverlay(viewport: viewport)
                }
            }
            .navigationBarBackButtonHidden(true)
            .sheet(item: $inspector) { inspector in
                NativeEditorInspectorView(inspector: inspector, session: session)
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            }
            .onChange(of: session.selectionRequest) { _, _ in routeSelection() }
            // A tall panel is a per-session choice: opening a tool later must
            // never start out covering the preview.
            .onChange(of: panel == nil) { _, closed in
                if closed { panelExpansion = 0 }
            }
            .onChange(of: session.pendingText == nil) { _, finished in
                if finished {
                    resignKeyboard()
                    if case .textCreation = panel { changePanel(to: nil) }
                }
            }
            .sheet(isPresented: $showsDeviceRender) {
                if let key = session.deviceRenderKey {
                    DeviceRenderPanel(key: key, sessions: model.deviceRenders,
                        retry: { await session.refreshDeviceRender(retry: true) })
                        .padding(20)
                        .presentationDetents([.medium, .large])
                        .presentationDragIndicator(.visible)
                }
            }
            .onChange(of: deviceLocalFile) { _, file in
                if let file { session.showDeviceOutput(file) }
            }
            .sheet(isPresented: $showsConversation) {
                if let conversation {
                    conversation()
                        .presentationDetents([.medium, .large])
                        .presentationDragIndicator(.visible)
                }
            }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillShowNotification)) { _ in keyboardVisible = true }
            .onReceive(NotificationCenter.default.publisher(for: UIResponder.keyboardWillHideNotification)) { _ in keyboardVisible = false }
            .onChange(of: conversationAcceptedID) { _, _ in showsConversation = false }
            .sheet(isPresented: $exporter.isSharing, onDismiss: exporter.removeSharedFile) {
                if let file = exporter.sharedFile { ShareSheetView(url: file) }
            }
            .interactiveDismissDisabled(session.hasUnsavedChanges)
            .confirmationDialog(
                "Save your changes before leaving?",
                isPresented: $showsUnsavedExit,
                titleVisibility: .visible
            ) {
                Button("Save and exit") { Task { await saveAndExit() } }
                Button("Discard changes", role: .destructive, action: onBack)
                Button("Stay", role: .cancel) {}
            } message: {
                Text("Unsaved editor changes are stored only on this device until you save.")
            }
            .task { await loadEditor(); await session.resumeEditorImports() }
            .onReceive(model.uploads.$pendingEditorPlacements) { _ in
                Task { await session.resumePendingEditorPlacements() }
            }
            // A source import outlives the screen: the upload runs in a
            // background URLSession, so the app can be suspended (or woken in
            // the background) while the editor's in-process waiter is gone,
            // and returning to the app doesn't re-run `.task`. Re-arm the
            // waiter whenever the app becomes active again so a finished
            // server probe is picked up instead of leaving "Preparing media
            // import…" up until a relaunch.
            .onChange(of: scenePhase) { _, phase in
                if phase == .active { Task { await session.resumeEditorImports() } }
            }
            .onDisappear {
                finishPanelEditing()
                session.suspendEditorImports()
                session.pausePlayback()
                exporter.removeSharedFile()
            }
        }
    }

    @ViewBuilder private func editor(viewport: GeometryProxy) -> some View {
        let metrics = NativeEditorLayoutMetrics(
            viewportSize: viewport.size,
            safeAreaTop: viewport.safeAreaInsets.top,
            safeAreaBottom: viewport.safeAreaInsets.bottom,
            topChromeHeight: topChromeHeight,
            previewAspectRatio: session.previewAspectRatio,
            keyboardVisible: keyboardVisible,
            isAccessibilitySize: dynamicTypeSize.isAccessibilitySize,
            shrinksPreviewWhileTyping: panel?.tool == .text
        )
        let showsTimeline = panel == nil
        let showsContext = showsTimeline && (session.selection?.kind == .text || session.selectedClipID != nil)
        // KRI-131: the context capsule now floats over the timeline instead
        // of pushing it up, so selecting a clip/text no longer shrinks the
        // preview.
        let previewHeight = metrics.previewHeight(resize: previewResize)
        VStack(spacing: 0) {
            NativeEditorProjectHeader(
                title: project.workspaceTitle, session: session, exporter: exporter,
                onBack: requestBack, onChat: conversation == nil ? requestBack : onBack,
                onSaveToPhotos: { Task { await exporter.saveToPhotos(from: session, api: model.api, deviceLocalFile: deviceLocalFile) } },
                onShare: { Task { await exporter.share(from: session, api: model.api, deviceLocalFile: deviceLocalFile) } }
            )
            VStack(spacing: 0) {
                NativeEditorSaveBanner(session: session)
                NativeEditorExportBanner(exporter: exporter)
                if let presentation = session.editorSongReferencePresentation {
                    NativeSongReferenceCard(presentation: presentation)
                        .padding(.horizontal, 16)
                        .padding(.bottom, 4)
                }
                if let key = session.deviceRenderKey, let phase = model.deviceRenders.presentations[key]?.phase {
                    Button(DeviceRenderButtonTitle.for(phase: phase)) { showsDeviceRender = true }
                        .font(KriaFont.body(12).weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .accessibilityIdentifier("native-editor-device-render")
                }
            }
            // Measure at the ideal height; a compressed measurement would feed
            // back into the preview budget.
            .fixedSize(horizontal: false, vertical: true)
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { topChromeHeight = $0 }

            NativeVideoPreview(session: session, onEmptyTap: enterFullscreen)
                .frame(width: previewHeight * session.previewAspectRatio, height: previewHeight)
                .clipped()
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { previewGlobalFrame = $0 }
                .accessibilityIdentifier("native-editor-preview")
                .frame(maxWidth: .infinity)
                .padding(.vertical, 5)
                .overlay(alignment: .bottomTrailing) {
                    if conversation != nil, session.pendingText == nil {
                        Button { showsConversation = true } label: {
                            Image(systemName: "sparkles")
                                .font(.system(size: 23))
                                .foregroundStyle(.white)
                                .frame(width: 52, height: 52)
                                .background(KriaColor.ink, in: Circle())
                        }
                        .accessibilityLabel("Open Kria conversation")
                        .accessibilityIdentifier("native-editor-conversation")
                        .padding(.trailing, 16)
                        .padding(.bottom, 14)
                    }
                }

            timelineResizeHandle(metrics: metrics)
            connectedEditorArea(viewport: viewport, showsContext: showsContext, metrics: metrics, previewHeight: previewHeight)
                // The panel may rise over the preview; paint and hit-test it
                // above the preview and the timeline handle.
                .zIndex(1)
        }
        .environment(\.nativeEditorConnectedPanel, true)
        .environment(\.nativeEditorPanelLifecycle, panelLifecycle)
    }

    private var panelIsOpen: Bool { panel != nil }

    private var fullscreenSpring: Animation {
        shouldReduceMotion ? .easeOut(duration: 0.15) : .spring(response: 0.36, dampingFraction: 0.88)
    }

    /// Empty preview taps and the VoiceOver "Expand preview" action land here.
    /// The grow animation starts first; playback (which synchronously
    /// activates the audio session) waits until it settles so it can't stall
    /// the animation. Playback is restored on exit. At the end of the timeline
    /// `togglePlayback()` restarts from 0.
    private func enterFullscreen() {
        guard !previewFullscreen, !keyboardVisible, session.pendingText == nil,
              session.canDisplayCurrentPlayer else { return }
        wasPlayingBeforeFullscreen = session.isPlaying
        fullscreenExpanded = false
        previewFullscreen = true
        DispatchQueue.main.async {
            withAnimation(fullscreenSpring) { fullscreenExpanded = true }
        }
        if !wasPlayingBeforeFullscreen {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                if previewFullscreen, fullscreenExpanded, !session.isPlaying { session.togglePlayback() }
            }
        }
    }

    private func exitFullscreen() {
        guard previewFullscreen else { return }
        if !wasPlayingBeforeFullscreen { session.pausePlayback() }
        withAnimation(fullscreenSpring) { fullscreenExpanded = false }
        DispatchQueue.main.asyncAfter(deadline: .now() + (shouldReduceMotion ? 0.16 : 0.34)) {
            if !fullscreenExpanded { previewFullscreen = false }
        }
    }

    private func fullscreenOverlay(viewport: GeometryProxy) -> some View {
        let screen = CGSize(
            width: viewport.size.width,
            height: viewport.size.height + viewport.safeAreaInsets.top + viewport.safeAreaInsets.bottom
        )
        let size = NativeEditorLayoutMetrics.fullscreenSize(screen: screen, aspect: session.previewAspectRatio)
        let collapsedScale = size.width > 0 ? max(0.05, previewGlobalFrame.width / size.width) : 1
        let collapsedOffset = CGSize(
            width: previewGlobalFrame.midX - screen.width / 2,
            height: previewGlobalFrame.midY - screen.height / 2
        )
        return ZStack {
            Color.black.opacity(fullscreenExpanded ? 0.8 : 0)
            NativeEditorFullscreenPreview(
                session: session,
                size: size,
                reduceMotion: shouldReduceMotion,
                expanded: fullscreenExpanded,
                collapsedScale: collapsedScale,
                collapsedOffset: collapsedOffset
            )
        }
        .ignoresSafeArea()
        .contentShape(Rectangle())
        .onTapGesture(perform: exitFullscreen)
        .accessibilityAddTraits(.isModal)
        .accessibilityAction(.escape, exitFullscreen)
        .accessibilityAction(named: "Close fullscreen", exitFullscreen)
    }

    private var panelTransition: AnyTransition {
        shouldReduceMotion ? .opacity.animation(.easeOut(duration: 0.15)) : .opacity
    }

    private func connectedEditorArea(
        viewport: GeometryProxy, showsContext: Bool, metrics: NativeEditorLayoutMetrics, previewHeight: CGFloat
    ) -> some View {
        let bottomInset = viewport.safeAreaInsets.bottom
        let clearance = NativeEditorIslandMetrics.bottomClearance(showsContext: showsContext, safeAreaBottom: bottomInset)
        return GeometryReader { area in
            ZStack(alignment: .bottom) {
                NativeEditorTimeline(
                    session: session, uploads: model.uploads, bottomClearance: clearance, isCovered: panelIsOpen,
                    onSelectVisual: { selectTool(.visuals) }, onSelectText: { selectTool(.text) }
                )
                    .ignoresSafeArea(.container, edges: .bottom)
                    // Safe-area expansion must not let the retained timeline
                    // paint over the preview when the keyboard shortens us.
                    .frame(width: area.size.width, height: area.size.height, alignment: .top)
                    .clipped()
                    .allowsHitTesting(!panelIsOpen)
                    .disabled(panelIsOpen)
                    .scrollDisabled(panelIsOpen)
                    .accessibilityHidden(panelIsOpen)

                VStack(spacing: 0) {
                    Spacer(minLength: 0)
                    NativeEditorIslandScrim(showsContext: showsContext, safeAreaBottom: bottomInset)
                }
                .ignoresSafeArea(.container, edges: .bottom)
                .allowsHitTesting(false)

                if panelIsOpen && !keyboardVisible {
                    // Stays pinned to the top of the area; a tall panel rises
                    // over it rather than dragging it along. Fixed to the
                    // area's height so the taller ZStack can't stretch it.
                    NativeEditorTransport(session: session)
                        .frame(height: area.size.height, alignment: .top)
                        .transition(.opacity)
                }

                NativeEditorIslandGroup {
                    VStack(spacing: NativeEditorIslandMetrics.stackSpacing) {
                        if showsContext {
                            if let selection = session.selection, selection.kind == .text {
                                NativeEditorTextContextStrip(
                                    onEdit: { changePanel(to: .text(selection.id)) },
                                    onDeselect: { session.select(nil) }
                                )
                                .transition(panelTransition)
                            } else if let selection = session.selection, selection.kind == .clip {
                                NativeEditorContextStrip(
                                    session: session,
                                    onBack: { session.select(nil) },
                                    onAdjust: { inspector = .adjust },
                                    onTransition: { inspector = .selection(selection) }
                                )
                                    .transition(panelTransition)
                            }
                        }
                        VStack(spacing: 0) {
                            if panelIsOpen {
                                panelContent
                                    .environment(\.nativeEditorPanelContentWidth, max(0, area.size.width - 72))
                                    .padding(.top, 18)
                                    .overlay(alignment: .top) {
                                        NativeEditorPanelResizeGrabber(
                                            expansion: $panelExpansion,
                                            range: metrics.panelRange(areaHeight: area.size.height, previewHeight: previewHeight),
                                            reduceMotion: shouldReduceMotion,
                                            accessibilityIdentifier: "native-editor-panel-resize",
                                            topAligned: true,
                                            accessibilityTitle: "Editor panel size",
                                            accessibilityHint: "Swipe up or down to resize the editor panel"
                                        )
                                    }
                                    .transition(panelTransition)
                            }
                            if !keyboardVisible {
                                NativeEditorToolRail(selected: panel?.tool, availableWidth: area.size.width - 24, connected: true, onSelect: selectTool)
                            }
                        }
                        .frame(width: panelIsOpen ? max(0, area.size.width - 24) : min(328, max(0, area.size.width - 24)))
                        .frame(height: panelIsOpen
                            ? metrics.panelHeight(areaHeight: area.size.height, previewHeight: previewHeight, expansion: panelExpansion)
                            : NativeEditorIslandMetrics.islandHeight)
                        .nativeEditorIslandSurface(cornerRadius: panelIsOpen ? 32 : 999)
                        // The marker must remain a plain leaf: glass ancestors
                        // corrupt AX frames on iOS 26 (see island surface).
                        .background {
                            if panelIsOpen {
                                Color.clear.accessibilityElement().accessibilityLabel("Editor controls")
                                    .accessibilityAddTraits(.isHeader)
                                    .accessibilityIdentifier("native-editor-connected-panel")
                            }
                        }
                    }
                }
                .padding(.bottom, NativeEditorIslandMetrics.bottomPadding)
            }
            .frame(width: area.size.width, height: area.size.height, alignment: .bottom)
        }
        .frame(maxHeight: .infinity)
        .layoutPriority(1)
    }

    @ViewBuilder private var panelContent: some View {
        switch panel {
        case .textCreation:
            NativeTextCreationPanel(session: session, onDone: { selection in
                selectedTextForActions = selection.id
                changePanel(to: .text(selection.id))
            }, onSelectBlock: { openTextBlock($0) })
        case .text(let id):
            NativeEditorTextPanel(
                id: id, session: session, initialTab: textEditOrigin == .list ? .edit : .style
            ) {
                if textEditOrigin == .list { showTextTab() } else { changePanel(to: nil) }
            }.id(id)
        case .captions:
            NativeCaptionPanel(session: session) { changePanel(to: nil) }
        case .visuals:
            NativeVisualPanel(session: session, uploads: model.uploads, projectID: project.id, panelDrafts: panelDrafts) { changePanel(to: nil) }
        case .sounds:
            NativeSoundsPanel(session: session, panelDrafts: panelDrafts) { changePanel(to: nil) }
        case nil:
            EmptyView()
        }
    }

    private func resignKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }

    private func finishPanelEditing() {
        panelLifecycle.prepareToClose()
        resignKeyboard()
        session.endTransaction()
    }

    private func changePanel(to destination: NativeEditorPanel?) {
        guard panel != destination else { return }
        finishPanelEditing()
        inspector = nil
        if case .text = destination {} else { textEditOrigin = .timeline }
        withAnimation(shouldReduceMotion ? nil : .spring(response: 0.34, dampingFraction: 0.88)) {
            panel = destination
        }
    }

    private func selectTool(_ tool: NativeEditorTool) {
        if panel?.tool == tool {
            changePanel(to: nil)
            return
        }
        switch tool {
        case .text:
            if session.pendingText != nil {
                if panel?.tool != .text { changePanel(to: .textCreation) }
            } else {
                // Finish the outgoing destination before creating the draft,
                // then install the creation destination directly. This keeps
                // the panel mounted throughout the transition.
                showTextTab()
            }
        case .captions: changePanel(to: .captions)
        case .visuals:
            changePanel(to: .visuals)
            session.select(nil)
        case .sounds: changePanel(to: .sounds)
        default:
            changePanel(to: nil)
            inspector = .tool(tool)
        }
    }

    /// Opens the Text tab: the new-text field with the list of existing blocks.
    private func showTextTab() {
        finishPanelEditing()
        textEditOrigin = .timeline
        session.beginTextCreation()
        guard session.pendingText != nil else { return }
        inspector = nil
        withAnimation(shouldReduceMotion ? nil : .spring(response: 0.34, dampingFraction: 0.88)) {
            panel = .textCreation
            // The default panel fits about one row; open taller when there is a list to
            // browse. The handle still resizes it, and closing the panel resets it.
            if session.document.textBlocks.count > 1 { panelExpansion = max(panelExpansion, 0.55) }
        }
    }

    /// A row in the Text tab's list: jump to the block and edit its words. The
    /// timeline is disabled while a panel is open, so this selects the block itself.
    private func openTextBlock(_ id: String) {
        guard session.document.textElements.contains(where: { $0.id == id }),
              EditorTextBlock.listIsInteractive(draft: session.pendingText?.text, canEdit: session.canEdit(.text))
        else { return }
        session.cancelTextCreation()
        // The raised list would hide the very text being edited under the panel.
        withAnimation(shouldReduceMotion ? nil : .spring(response: 0.34, dampingFraction: 0.88)) {
            panelExpansion = 0
        }
        selectedTextForActions = id
        textEditOrigin = .list
        session.select(EditorSelection(kind: .text, id: id))
        changePanel(to: .text(id))
    }

    private func routeSelection() {
        guard !session.isDirectManipulating, !session.isTimingGestureActive else { return }
        guard let selection = session.selection else {
            if case .text = panel { changePanel(to: nil) }
            selectedTextForActions = nil
            return
        }
        if [.mediaOverlay, .visualBlock, .motionScene, .cameraEffect].contains(selection.kind) {
            changePanel(to: .visuals)
            return
        }
        // Guided-story captions are text bars tagged as caption_cue.
        let isTextLaneCaption = selection.kind == .text
            && session.document.textElements.first(where: { $0.id == selection.id })?.isCaption == true
        if selection.kind == .captionCue || isTextLaneCaption {
            changePanel(to: .captions)
            return
        }
        if selection.kind == .text {
            if selectedTextForActions == selection.id { changePanel(to: .text(selection.id)) }
            else {
                changePanel(to: nil)
                selectedTextForActions = selection.id
            }
            return
        }
        selectedTextForActions = nil
        changePanel(to: nil)
        if selection.kind != .clip { inspector = .selection(selection) }
    }

    /// Drag up shrinks the preview, drag down grows it (KRI-170). Values are
    /// points, so the finger tracks the preview edge 1:1.
    private func timelineResizeHandle(metrics: NativeEditorLayoutMetrics) -> some View {
        NativeEditorPanelResizeGrabber(
            expansion: $previewResize,
            range: 1,
            reduceMotion: shouldReduceMotion,
            accessibilityIdentifier: "native-editor-timeline-resize",
            bounds: -metrics.growRange...metrics.shrinkRange,
            accessibilityTitle: "Preview size",
            accessibilityHint: "Swipe up to shrink or down to enlarge the video preview",
            describe: { value, bounds in
                let span = max(0.0001, bounds.upperBound - bounds.lowerBound)
                return "\(Int(((bounds.upperBound - value) / span) * 100)) percent of maximum size"
            },
            incrementFraction: -0.25
        )
    }

    private var deviceLocalFile: URL? {
        guard let key = session.deviceRenderKey else { return nil }
        return model.deviceRenders.presentations[key]?.localFile
    }

    private func loadEditor() async {
        #if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        let sourceFailure = arguments.contains("-ui-testing-editor-source-failure")
        if sourceFailure || arguments.contains("-ui-testing-editor-source-text") {
            let delayed = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-delayed-source")
            if delayed, session.loadState == .loaded { return }
            let url = sourceFailure
                ? URL(fileURLWithPath: "/tmp/kria-missing-source-preview.mp4")
                : Bundle.main.url(forResource: "montage", withExtension: "mp4")!
            await session.prepareFixtureSourcePreview(url: url, delayedLoad: delayed, forceFailure: sourceFailure)
            return
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") || ProcessInfo.processInfo.arguments.contains("-ui-testing-brand") { return }
        #endif
        session.useDeviceRendering(model.deviceRenders)
        session.useMediaUploads(model.uploads)
        guard session.needsReload(for: project) else { return }
        if let libraryJobID {
            await session.load(libraryJobID: libraryJobID, api: model.api)
        } else {
            await session.load(project: project, api: model.api)
        }
        await session.refreshDeviceRender()
        if let file = deviceLocalFile { session.showDeviceOutput(file) }
    }

    private func requestBack() {
        if session.pendingText != nil { session.cancelTextCreation(); resignKeyboard(); return }
        if panel != nil { changePanel(to: nil); return }
        finishPanelEditing()
        if session.hasUnsavedChanges { showsUnsavedExit = true }
        else { onBack() }
    }

    private func saveAndExit() async {
        await session.save()
        guard !session.hasUnsavedChanges else { return }
        switch session.saveState {
        case .conflict, .loadFailed, .failed, .renderRetryNeeded, .deviceRenderRetryNeeded:
            return
        default:
            onBack()
        }
    }
}

private struct NativeEditorLoadSurface: View {
    let title: String
    let detail: String
    let isLoading: Bool
    let onBack: () -> Void
    let retry: (() -> Void)?

    var body: some View {
        VStack(spacing: 0) {
            NativeEditorTopBar(onBack: onBack)
            VStack(alignment: .leading, spacing: 14) {
                if isLoading { ProgressView().tint(KriaColor.ink) }
                Text(title).font(KriaFont.display(29))
                Text(detail).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                if let retry {
                    Button("Try again", action: retry)
                        .buttonStyle(KriaPrimaryButtonStyle())
                        .accessibilityIdentifier("native-editor-load-retry")
                }
            }
            .padding(24)
            .frame(maxWidth: 480, maxHeight: .infinity, alignment: .leading)
        }
        .background(KriaColor.paper)
        .accessibilityIdentifier(isLoading ? "native-editor-loading" : "native-editor-load-failed")
    }
}

private enum NativeEditorInspector: Identifiable {
    case tool(NativeEditorTool)
    case selection(EditorSelection)
    case adjust

    var id: String {
        switch self {
        case .tool(let tool): return "tool-\(tool.rawValue)"
        case .selection(let selection): return "selection-\(selection.kind.rawValue)-\(selection.id)"
        case .adjust: return "adjust"
        }
    }

    var title: String {
        switch self {
        case .tool(let tool): return tool.rawValue
        case .selection(let selection):
            switch selection.kind {
            case .clip: return "Clip"
            case .text: return "Text"
            case .captionCue: return "Caption"
            case .music: return "Music"
            case .soundEffect: return "Sound effect"
            case .mediaOverlay: return "Overlay"
            case .visualBlock, .motionScene, .cameraEffect: return "Visual"
            case .carousel: return "Carousel"
            }
        case .adjust: return "Adjust"
        }
    }

    init(tool: NativeEditorTool) {
        self = .tool(tool)
    }
}

private struct NativeEditorInspectorView: View {
    let inspector: NativeEditorInspector
    @ObservedObject var session: NativeEditorSession
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            Group {
                switch inspector {
                case .tool(.kria): NativeKriaInspector(session: session)
                case .tool(.text): NativeTextInspector(session: session)
                case .tool(.captions): NativeCaptionsInspector(session: session)
                case .tool(.visuals): NativeEffectBrowserInspector(session: session, kinds: [.visualBlock, .motionScene, .cameraEffect, .carousel], title: "Visual lanes")
                case .tool(.sounds): NativeSoundsInspector(session: session)
                case .tool: NativeEditorUnavailableView(title: "Editor", reason: "This tool is not available for the current render.", systemImage: "lock")
                case .selection(let selection): NativeSelectionInspector(selection: selection, session: session)
                case .adjust: NativeAdjustInspector(session: session)
                }
            }
            .navigationTitle(inspector.title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") { dismiss() }
                        .accessibilityIdentifier("native-editor-inspector-done")
                }
            }
        }
        .tint(KriaColor.ink)
    }
}

private struct NativeKriaInspector: View {
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text("A considered next move")
                    .font(KriaFont.display(29))
                Text("Kria keeps proposals reversible. Pick one to make a small, visible change, then decide whether to save.")
                    .font(KriaFont.body(15))
                    .foregroundStyle(KriaColor.zinc)
                NativeProposalCard(title: "Give the story a starting point", detail: "Add a clean text hook to the opening frame.", actionTitle: "Add hook") {
                    session.addText()
                }
                .disabled(!session.canEditText)
                NativeProposalCard(title: "Make speech easier to follow", detail: "Turn on captions so the cut still works without sound.", actionTitle: session.draft.captions.enabled ? "Captions on" : "Turn on captions") {
                    if !session.draft.captions.enabled { session.toggleCaptions() }
                }
                .disabled(!session.canEditCaptions)
                NativeDocumentInspector(session: session)
            }
            .padding(24)
        }
    }
}

private struct NativeProposalCard: View {
    let title: String
    let detail: String
    let actionTitle: String
    let action: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(KriaFont.body(16).weight(.semibold))
            Text(detail).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            Button(actionTitle, action: action)
                .buttonStyle(KriaSecondaryButtonStyle())
                .accessibilityIdentifier("native-editor-proposal-\(actionTitle.lowercased().replacingOccurrences(of: " ", with: "-"))")
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.softZinc)
        .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
    }
}

private struct NativeTextInspector: View {
    @ObservedObject var session: NativeEditorSession
    @State private var newText = "Your story"
    @State private var editingTextID: UUID?

    var body: some View {
        Form {
            Section("Add text") {
                TextField("Write a short line", text: $newText)
                    .accessibilityIdentifier("native-editor-text-input")
                Button("Add text to preview") {
                    session.addText(content: newText)
                    newText = "Your story"
                }
                .buttonStyle(KriaPrimaryButtonStyle())
                .disabled(newText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("native-editor-add-text")
            }
            .disabled(!session.canEditText)
            if !session.canEditText {
                Section { Label("Text editing is unavailable for this render.", systemImage: "lock") }
            }
            Section("Existing text") {
                if session.draft.text.isEmpty {
                    Text("No text layers yet. Add one above, then adjust its style below.")
                        .foregroundStyle(KriaColor.zinc)
                } else {
                    ForEach(session.draft.text) { layer in
                        Text(layer.content)
                            .font(KriaFont.body(15).weight(.medium))
                            .contentShape(Rectangle())
                            .onTapGesture { editingTextID = layer.id }
                            .accessibilityIdentifier("native-editor-text-\(layer.id.uuidString)")
                    }
                    Text("Tap a line to select it. Content editing is kept local until you save the draft.")
                        .font(KriaFont.body(12))
                        .foregroundStyle(KriaColor.zinc)
                }
            }
        }
        .sheet(item: Binding(get: { editingTextID.map { NativeTextEditItem(id: $0) } }, set: { editingTextID = $0?.id })) { item in
            NativeTextEditSheet(session: session, layerID: item.id)
        }
    }
}

private struct NativeTextEditItem: Identifiable { let id: UUID }

private struct NativeTextEditSheet: View {
    @ObservedObject var session: NativeEditorSession
    let layerID: UUID
    @Environment(\.dismiss) private var dismiss
    @State private var value = ""

    var body: some View {
        NavigationStack {
            Form { TextField("Text", text: $value, axis: .vertical).lineLimit(1...4).accessibilityIdentifier("native-editor-edit-text-input") }
                .navigationTitle("Edit text")
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { apply(); dismiss() } } }
                .task { value = session.draft.text.first(where: { $0.id == layerID })?.content ?? "" }
        }
    }

    private func apply() {
        session.updateText(id: layerID, content: value)
    }
}

private struct NativeCaptionsInspector: View {
    @ObservedObject var session: NativeEditorSession
    @State private var style = "Sentence"
    private let styles = ["Sentence", "Word"]

    var body: some View {
        Form {
            Section {
                Toggle("Captions", isOn: Binding(get: { session.draft.captions.enabled }, set: { _ in session.toggleCaptions() }))
                    .disabled(!session.canEditCaptions)
                    .accessibilityIdentifier("native-editor-captions-toggle")
            } footer: {
                Text("Captions stay synchronized to the cut. Toggle them on to preview the readable version.")
            }
            Section("Style") {
                Picker("Caption style", selection: $style) { ForEach(styles, id: \.self, content: Text.init) }
                    .pickerStyle(.menu)
                    .onChange(of: style) { _, newValue in session.setCaptionStyle(newValue.lowercased()) }
                    .disabled(!session.canEditCaptions)
                    .accessibilityIdentifier("native-editor-caption-style")
            }
            if !session.canEditCaptions {
                Section { Label("This video has no caption-safe render base, so caption changes are unavailable.", systemImage: "lock") }
            }
        }
        .onAppear { style = session.draft.captions.style.capitalized }
    }
}

private struct NativeSoundsInspector: View {
    @ObservedObject var session: NativeEditorSession
    @StateObject private var panelDrafts = NativeEditorPanelDrafts()
    var body: some View {
        ScrollView { NativeSoundsControls(session: session, panelDrafts: panelDrafts).padding(24) }
    }
}

private struct NativeSoundsPanel: View {
    enum Tab: String { case music = "Music", effects = "Effects" }
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    let onDone: () -> Void
    @State private var tab: Tab = .music
    var body: some View {
        NativeEditorLanePanel(title: "Sounds", tabs: [Tab.music, Tab.effects], tab: $tab, onDone: onDone) {
            switch tab {
            case .music: NativeSoundsControls(session: session, panelDrafts: panelDrafts)
            case .effects: NativeSoundEffectsControls(session: session, panelDrafts: panelDrafts)
            }
        }
        .onDisappear { session.endTransaction() }
    }
}

private struct NativeSoundsControls: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    private var volume: Binding<Double> {
        Binding(get: { session.draft.music?.volume ?? 0 }, set: { session.setMusicVolume($0) })
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if session.document.music == nil {
                Text("Add music").font(KriaFont.body(15).weight(.semibold))
                TextField("Music track ID", text: $panelDrafts.musicTrackID)
                    .textInputAutocapitalization(.never).autocorrectionDisabled()
                    .padding(.horizontal, 12).frame(minHeight: 44)
                    .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12))
                    .disabled(!session.canEdit(.music))
                    .accessibilityIdentifier("native-editor-music-track-input")
                Button("Add music lane") {
                    session.setMusic(trackID: panelDrafts.musicTrackID.trimmingCharacters(in: .whitespacesAndNewlines))
                }
                .buttonStyle(KriaSecondaryButtonStyle())
                .disabled(!session.canEdit(.music) || panelDrafts.musicTrackID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityIdentifier("native-editor-add-music")
            } else {
                HStack(spacing: 12) {
                    Image(systemName: "music.note").frame(width: 44, height: 44)
                        .background(KriaColor.sage, in: RoundedRectangle(cornerRadius: 12))
                    VStack(alignment: .leading, spacing: 4) {
                        Text(session.draft.music?.title ?? "Music").font(KriaFont.body(15).weight(.semibold))
                        Text("Music track").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    }
                    Spacer(minLength: 0)
                }
                HStack(spacing: 12) {
                    Text("Volume")
                    NativeEditorSlider(session: session, value: volume, in: 0...1) { Text("Music volume") }
                    Text("\(Int(volume.wrappedValue * 100))%")
                        .font(.system(.caption, design: .monospaced)).frame(width: 42, alignment: .trailing)
                }
                .frame(minHeight: 44)
                .accessibilityIdentifier("native-editor-music-volume")
                .disabled(!session.canEditMix)
            }
            if !session.canEditMix {
                Label("Music level is unavailable for this edit. Existing audio stays unchanged.", systemImage: "lock")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

private struct NativeSoundEffectsControls: View {
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts

    private var canAdd: Bool {
        session.canEditOperation(["lanes.sfx.add", "sfx.add", "sound_effects.add", "lanes.sfx"], section: .soundEffects)
    }
    private var groups: [NativeSfxBrowse.Group] {
        NativeSfxBrowse.groupEffects(session.soundEffectCatalog, query: panelDrafts.sfxQuery)
    }
    private var showsCategoryLabels: Bool { NativeSfxBrowse.hasCategories(session.soundEffectCatalog) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Sound effects").font(KriaFont.body(15).weight(.semibold))
            TextField("Search sound effects", text: $panelDrafts.sfxQuery)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
                .padding(.horizontal, 12).frame(minHeight: 44)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12))
                .accessibilityIdentifier("native-editor-sfx-search")
            if session.soundEffectCatalogLoading && session.soundEffectCatalog.isEmpty {
                ProgressView("Loading sounds…").frame(minHeight: 44)
            } else if session.soundEffectCatalog.isEmpty {
                Text("Sound effects aren’t available right now.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            } else if groups.isEmpty {
                Text("No sound effects match “\(panelDrafts.sfxQuery)”.")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            } else {
                ForEach(groups, id: \.key) { group in
                    VStack(alignment: .leading, spacing: 6) {
                        if showsCategoryLabels {
                            Text("\(group.label) (\(group.effects.count))")
                                .font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.zinc)
                        }
                        ForEach(group.effects) { effect in
                            HStack(spacing: 12) {
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(effect.name).font(KriaFont.body(14))
                                    if let duration = effect.durationS {
                                        Text(String(format: "%.1fs", duration))
                                            .font(KriaFont.body(11)).foregroundStyle(KriaColor.mutedInk)
                                    }
                                }
                                Spacer(minLength: 8)
                                Button("Add") { session.addSoundEffect(effect) }
                                    .buttonStyle(KriaSecondaryButtonStyle())
                                    .disabled(!canAdd)
                                    .accessibilityIdentifier("native-editor-sfx-add-\(effect.id)")
                            }
                            .padding(.horizontal, 12).frame(minHeight: 44)
                            .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
                        }
                    }
                }
            }
            if !canAdd {
                Label("Sound effects aren’t available for this edit.", systemImage: "lock")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task { await session.loadSoundEffectCatalog() }
    }
}

private struct NativeStylesInspector: View {
    @ObservedObject var session: NativeEditorSession
    @State private var selected = "Fraunces"
    private var presets: [String] { NativeFontCatalog.shared.pickerFonts }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Type sets the feeling before anyone reads the words.")
                .font(KriaFont.body(15))
                .foregroundStyle(KriaColor.zinc)
            ScrollView {
                LazyVStack(spacing: 8) {
                    ForEach(presets, id: \.self) { preset in
                        Button {
                            selected = preset
                            session.setTextStyle(id: nil, style: preset)
                        } label: {
                            HStack {
                                Text(preset).font(NativeFontCatalog.shared.previewFont(preset, size: 22))
                                Spacer()
                                Image(systemName: selected == preset ? "checkmark.circle.fill" : "circle")
                            }
                            .foregroundStyle(KriaColor.ink)
                            .padding(15)
                            .background(selected == preset ? KriaColor.sage : KriaColor.softZinc)
                            .clipShape(RoundedRectangle(cornerRadius: 13, style: .continuous))
                        }
                        .accessibilityIdentifier("native-editor-style-\(preset.lowercased())")
                        .frame(minHeight: 50)
                        .disabled(!session.canEditText)
                    }
                }
            }
            if session.draft.text.isEmpty {
                Label("Add text first to apply a type preset.", systemImage: "info.circle")
                    .font(KriaFont.body(13))
                    .foregroundStyle(KriaColor.zinc)
            }
            Spacer()
        }
        .padding(24)
        .onAppear { selected = session.draft.text.last?.style ?? "Fraunces" }
    }
}

private struct NativeAdjustInspector: View {
    @ObservedObject var session: NativeEditorSession

    var body: some View {
        Form {
            Section("Selected clip") {
                Button("Move earlier") { session.moveSelected(by: -0.1) }
                Button("Move later") { session.moveSelected(by: 0.1) }
                Button("Slide source earlier") { session.slideSourceWindow(by: -0.1) }
                Button("Slide source later") { session.slideSourceWindow(by: 0.1) }
            }
            .disabled(!session.canEditTimeline)
            Section { Text("Trim handles appear in the timeline as soon as a clip is selected. These small nudge controls keep the adjustment reversible and precise.").font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }
            Section("Clip audio") {
                Label("Per-clip mute is not supported by the renderer yet. Audio remains unchanged; whole-video music level is available under Sounds when supported.", systemImage: "speaker.slash")
                    .font(KriaFont.body(13))
                    .foregroundStyle(KriaColor.zinc)
            }
            if !session.canEditTimeline {
                Section { Label("Clip edits are locked for this render.", systemImage: "lock") }
            }
        }
    }
}
