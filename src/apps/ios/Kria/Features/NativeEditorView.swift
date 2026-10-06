import SwiftUI
import KriaMediaEngine

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
    /// KRI-240: the caption line open in the caption editor (plan 026 D27: the
    /// source of truth for caption edit state; keyboard focus only follows it).
    @State private var captionEditingCueID: String?
    @State private var headerHeight: CGFloat = 0
    /// Preview taps while a caption line is open toggle its loop (plan 026 D5).
    @State private var captionLoopRequest = 0
    @ScaledMetric(relativeTo: .body) private var captionLineHeight: CGFloat = 22
    @State private var inspector: NativeEditorInspector?
    @State private var showsUnsavedExit = false
    @State private var showsDeviceRender = false
    @State private var showsOriginalsRecovery = false
    @State private var showsConversation = false
    @State private var showsEmptyAddClip = false
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
    /// KRI-253: points a drag has pulled the panel below its smallest size.
    @State private var panelDismissPull: CGFloat = 0
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
                            projectTitle: project.workspaceTitle,
                            isLoading: true,
                            onBack: requestBack,
                            retry: nil
                        )
                    }
                case .failed(let message):
                    NativeEditorLoadSurface(
                        title: "The editor couldn’t open",
                        detail: message,
                        projectTitle: project.workspaceTitle,
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
                    // The sheet is its own presentation; hand it the editor's text size explicitly.
                    .environment(\.dynamicTypeSize, dynamicTypeSize)
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            }
            .onChange(of: session.selectionRequest) { _, _ in routeSelection() }
            // A tall panel is a per-session choice: opening a tool later must
            // never start out covering the preview.
            .onChange(of: panel == nil) { _, closed in
                if closed { panelExpansion = 0 }
            }
            // A drag on an outgoing panel can't end on it.
            .onChange(of: panel) { _, _ in panelDismissPull = 0 }
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
            .sheet(isPresented: $showsOriginalsRecovery) {
                EditorOriginalsRecoveryView(session: session)
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            }
            .sheet(isPresented: $showsEmptyAddClip) { NativeEditorAddClipSheet(session: session) }
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
            // Sending from the conversation sheet must not dismiss it (only the
            // creator closes it); `conversationAcceptedID` is intentionally unused here.
            // A re-plan can finish as a NEW job while this editor is open: show it.
            .onChange(of: project.activeJobID) { _, _ in Task { await loadEditor() } }
            .safeAreaInset(edge: .top) {
                if let newer = session.newerJobPrompt {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("A new version of your video is ready")
                            .font(KriaFont.body().weight(.semibold))
                        Text("Switching discards your unsaved edits.")
                            .font(.footnote)
                        HStack {
                            Button("Switch") { Task { await session.switchToLatestJob(newer, api: model.api) } }
                                .accessibilityIdentifier("native-editor-switch-job")
                            Button("Keep editing") { session.keepEditingCurrentJob() }
                                .accessibilityIdentifier("native-editor-keep-editing")
                        }
                    }
                    .padding(12)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(KriaColor.paper)
                }
            }
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
            reservesSongReferencePreviewFloor: session.editorSongReferencePresentation != nil,
            shrinksPreviewWhileTyping: panel?.tool == .text,
            captionEditBarHeight: captionEditing ? CaptionEditBar.height(lineHeight: captionLineHeight, lines: captionEditLines(viewport: viewport)) : nil,
            measuredHeaderHeight: headerHeight > 0 ? headerHeight : nil
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
                onShare: { Task { await exporter.share(from: session, api: model.api, deviceLocalFile: deviceLocalFile) } },
                beforeSave: { if captionEditing { panelLifecycle.prepareToClose(); resignKeyboard() } },
                onVideoShape: { changePanel(to: nil); inspector = .videoShape }
            )
            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { headerHeight = $0 }
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

            NativeVideoPreview(
                session: session, onEmptyTap: handleEmptyPreviewTap,
                onFindOriginals: { showsOriginalsRecovery = true },
                onAddClip: { showsEmptyAddClip = true }
            )
                .frame(width: previewHeight * session.previewAspectRatio, height: previewHeight)
                .clipped()
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .global) } action: { previewGlobalFrame = $0 }
                .overlay {
                    // KRI-240 (plan 026 R10): the finished render still shows the old
                    // caption, so say so instead of looking like the fix didn't take.
                    if captionEditing, session.sourcePreviewState.isFailure || session.sourcePreviewState == .originalsUnavailable {
                        ZStack {
                            KriaColor.paper.opacity(0.5)
                            Text("Preview shows your last render. Your fix appears after Save.")
                                .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                                .multilineTextAlignment(.center).padding(12)
                        }
                        .allowsHitTesting(false)
                        .accessibilityIdentifier("native-editor-caption-stale-preview")
                    }
                }
                .accessibilityIdentifier("native-editor-preview")
                .frame(maxWidth: .infinity)
                .padding(.vertical, 5)
                .overlay(alignment: .bottomTrailing) {
                    if conversation != nil, session.pendingText == nil, !captionEditing {
                        KriaAIButton(identifier: "native-editor-conversation") { showsConversation = true }
                    }
                }
                // The centered preview leaves horizontal editor margins on
                // wider layouts. Keep their tap surface behind the preview
                // and conversation control so it only dismisses context.
                .background {
                    Color.clear
                        .contentShape(Rectangle())
                        .onTapGesture { dismissSelectedClipContext() }
                }

            if !captionEditing { timelineResizeHandle(metrics: metrics) }
            connectedEditorArea(viewport: viewport, showsContext: showsContext, metrics: metrics, previewHeight: previewHeight)
                // The panel may rise over the preview; paint and hit-test it
                // above the preview and the timeline handle.
                .zIndex(1)
        }
        .environment(\.nativeEditorConnectedPanel, true)
        .environment(\.nativeEditorPanelLifecycle, panelLifecycle)
    }

    private var panelIsOpen: Bool { panel != nil }

    /// KRI-240: a caption line is open in the caption editor (Variant A edit state).
    /// The line must still exist: if a save or reload drops it, the chrome comes back
    /// in the same pass rather than staying hidden over the caption list.
    private var captionEditing: Bool {
        guard panel?.tool == .captions, let id = captionEditingCueID else { return false }
        return session.document.captionUnits.contains { $0.id == id }
    }

    /// Field lines in the caption edit bar. Computed once here and handed to the panel,
    /// so the height the layout reserves is the height the bar draws.
    private func captionEditLines(viewport: GeometryProxy) -> Int {
        // The screen, not the viewport: the viewport shrinks by the keyboard, which
        // would make every phone "short" the moment the keyboard rises.
        CaptionEditBar.fieldLineCount(
            isAccessibilitySize: dynamicTypeSize.isAccessibilitySize,
            screenHeight: UIScreen.main.bounds.height,
            contentWidth: max(0, viewport.size.width - 72)
        )
    }

    private var fullscreenSpring: Animation {
        shouldReduceMotion ? .easeOut(duration: 0.15) : .spring(response: 0.36, dampingFraction: 0.88)
    }

    /// Empty preview taps and the VoiceOver "Expand preview" action land here.
    /// The grow animation starts first; playback (which synchronously
    /// activates the audio session) waits until it settles so it can't stall
    /// the animation. Playback is restored on exit. At the end of the timeline
    /// `togglePlayback()` restarts from 0.
    private func enterFullscreen() {
        guard !previewFullscreen, !keyboardVisible, !captionEditing, session.pendingText == nil,
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

    /// An empty preview tap first closes clip-only context. Once nothing is
    /// selected, the next tap retains the established fullscreen behavior.
    private func handleEmptyPreviewTap() {
        if captionEditing {
            captionLoopRequest += 1
            return
        }
        guard !dismissSelectedClipContext() else { return }
        enterFullscreen()
    }

    @discardableResult
    private func dismissSelectedClipContext() -> Bool {
        guard session.selection?.kind == .clip else { return false }
        session.select(nil)
        return true
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

                if panelIsOpen && !keyboardVisible && !captionEditing {
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
                                    onDeselect: { session.select(nil) },
                                    onDelete: { session.deleteText(id: selection.id) },
                                    deleteBlockedReason: blockedReason(session.textDeletion(id: selection.id))
                                )
                                .transition(panelTransition)
                            } else if let selection = session.selection, selection.kind == .clip {
                                NativeEditorContextStrip(
                                    session: session,
                                    onAdjust: { inspector = .adjust },
                                    onTransition: { inspector = .selection(selection) }
                                )
                                    .transition(panelTransition)
                            }
                        }
                        VStack(spacing: 0) {
                            if panelIsOpen {
                                let panelRange = metrics.panelRange(areaHeight: area.size.height, previewHeight: previewHeight)
                                let panelDismiss = NativeEditorPanelDismiss(pull: $panelDismissPull, complete: completePanel)
                                panelContent(captionEditLines: captionEditLines(viewport: viewport))
                                    .environment(\.nativeEditorPanelContentWidth, max(0, area.size.width - 72))
                                    .environment(\.nativeEditorPanelResize, NativeEditorPanelResize(
                                        expansion: $panelExpansion, range: panelRange, dismiss: panelDismiss
                                    ))
                                    .padding(.top, captionEditing ? 0 : 18)
                                    .overlay(alignment: .top) {
                                        // The band beside the grabber resizes too (KRI-235).
                                        if !captionEditing {
                                            Color.clear.frame(height: 18).contentShape(Rectangle())
                                                .modifier(NativeEditorPanelResizeDrag(
                                                    expansion: $panelExpansion, range: panelRange, minimumDistance: 8, dismiss: panelDismiss
                                                ))
                                        }
                                    }
                                    .overlay(alignment: .top) {
                                        if !captionEditing {
                                            NativeEditorPanelResizeGrabber(
                                                expansion: $panelExpansion,
                                                range: panelRange,
                                                reduceMotion: shouldReduceMotion,
                                                accessibilityIdentifier: "native-editor-panel-resize",
                                                topAligned: true,
                                                accessibilityTitle: "Editor panel size",
                                                accessibilityHint: "Swipe up or down to resize the editor panel",
                                                dismiss: panelDismiss
                                            )
                                        }
                                    }
                                    .transition(panelTransition)
                            }
                            if !keyboardVisible && !captionEditing {
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
                        // KRI-253: the panel follows a drag past its smallest
                        // size and springs back when the drag ends.
                        .offset(y: NativeEditorPanelDismissRule.offset(forPull: panelDismissPull))
                        .animation(panelDismissPull == 0 && !shouldReduceMotion
                                   ? .spring(response: 0.34, dampingFraction: 0.88) : nil,
                                   value: panelDismissPull == 0)
                    }
                }
                .padding(.bottom, NativeEditorIslandMetrics.bottomPadding)
            }
            .frame(width: area.size.width, height: area.size.height, alignment: .bottom)
        }
        .frame(maxHeight: .infinity)
        .layoutPriority(1)
    }

    @ViewBuilder private func panelContent(captionEditLines: Int) -> some View {
        switch panel {
        case .textCreation:
            NativeTextCreationPanel(session: session, onDone: textCreated, onSelectBlock: { openTextBlock($0) })
        case .text(let id):
            NativeEditorTextPanel(
                id: id, session: session, initialTab: textEditOrigin == .list ? .edit : .style,
                onDone: textEditingDone
            ).id(id)
        case .captions:
            NativeCaptionPanel(
                session: session, editingCueID: $captionEditingCueID,
                loopToggleRequest: captionLoopRequest, editLines: captionEditLines, editLineHeight: captionLineHeight
            ) { changePanel(to: nil) }
        case .visuals:
            NativeVisualPanel(session: session, uploads: model.uploads, projectID: project.id, panelDrafts: panelDrafts) { changePanel(to: nil) }
        case .sounds:
            NativeSoundsPanel(session: session, panelDrafts: panelDrafts) { changePanel(to: nil) }
        case nil:
            EmptyView()
        }
    }

    private func textCreated(_ selection: EditorSelection) {
        selectedTextForActions = selection.id
        changePanel(to: .text(selection.id))
    }

    private func textEditingDone() {
        if textEditOrigin == .list { showTextTab() } else { changePanel(to: nil) }
    }

    /// The open panel's Done, for a drag that pulls the panel closed (KRI-253).
    /// The panels' draft cleanup still runs: `changePanel` and `showTextTab`
    /// flush it through `panelLifecycle`.
    private func completePanel() {
        switch panel {
        case .textCreation:
            // An empty draft returns nil and the pending-text observer closes the panel.
            if let selection = session.finishTextCreation() { textCreated(selection) }
        case .text: textEditingDone()
        case .captions, .visuals, .sounds: changePanel(to: nil)
        case nil: break
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
        if destination?.tool != .captions { captionEditingCueID = nil }
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
        if selection.kind == .soundEffect {
            panelDrafts.soundsTab = .effects
            panelDrafts.sfxScreen = .home
            changePanel(to: .sounds)
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
            incrementFraction: -0.25,
            spansRow: true
        )
    }

    private var deviceLocalFile: URL? {
        guard let key = session.deviceRenderKey else { return nil }
        return model.deviceRenders.presentations[key]?.localFile
    }

    private func loadEditor() async {
        #if DEBUG
        let arguments = ProcessInfo.processInfo.arguments
        // A generic failure is retryable; missing originals are not (KRI-211), so they
        // are separate fixtures with separate recovery UI.
        let forcedFailure: Error? = arguments.contains("-ui-testing-editor-missing-originals")
            ? SourceAssetError.missingOriginal("fixture-source")
            : arguments.contains("-ui-testing-editor-source-failure-permanent") ? NativeEditorRenderError.missingVideoTrack
            : arguments.contains("-ui-testing-editor-source-failure") ? APIError.invalidResponse : nil
        if forcedFailure != nil || arguments.contains("-ui-testing-editor-source-text") {
            let delayed = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-delayed-source")
            if delayed, session.loadState == .loaded { return }
            let url = forcedFailure != nil
                ? URL(fileURLWithPath: "/tmp/kria-missing-source-preview.mp4")
                : Bundle.main.url(forResource: "montage", withExtension: "mp4")!
            await session.prepareFixtureSourcePreview(
                url: url, delayedLoad: delayed, forceFailure: forcedFailure != nil,
                failure: forcedFailure ?? NativeEditorRenderError.missingVideoTrack
            )
            return
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") || ProcessInfo.processInfo.arguments.contains("-ui-testing-brand") { return }
        #endif
        session.useDeviceRendering(model.deviceRenders)
        session.useMediaUploads(model.uploads)
        if session.isBehindActiveJob(project) {
            await session.adoptLatestJob(project, api: model.api)
            return
        }
        guard session.needsReload(for: project) else {
            // Already loaded: a chat draft may have landed while this tab was hidden.
            if project.runtimeVersion == 2 { await session.synchronizePromptRevision() }
            return
        }
        if let libraryJobID {
            await session.load(libraryJobID: libraryJobID, api: model.api)
        } else {
            await session.load(project: project, api: model.api)
        }
        await session.refreshDeviceRender()
        if let file = deviceLocalFile { session.showDeviceOutput(file) }
    }

    private func blockedReason(_ deletion: NativeEditorSession.TextDeletion) -> String? {
        if case let .blocked(reason) = deletion { return reason }
        return nil
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
    let projectTitle: String
    let isLoading: Bool
    let onBack: () -> Void
    let retry: (() -> Void)?

    var body: some View {
        VStack(spacing: 0) {
            NativeEditorTopBar(title: projectTitle, onBack: onBack)
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
    /// KRI-306: Vertical / Landscape and Black bars / Crop for the finished video.
    case videoShape

    var id: String {
        switch self {
        case .tool(let tool): return "tool-\(tool.rawValue)"
        case .selection(let selection): return "selection-\(selection.kind.rawValue)-\(selection.id)"
        case .adjust: return "adjust"
        case .videoShape: return "video-shape"
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
        case .videoShape: return "Video shape"
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
                case .videoShape: NativeVideoShapeInspector(session: session)
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
                // toggleCaptions() writes caption_meta ("enabled"), so this is
                // gated by the meta capability (KRI-216), not the coarse
                // canEditCaptions, which can be true from cues alone.
                .disabled(!session.canEditCaptionMeta)
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
                // toggleCaptions()/setCaptionStyle() both write caption_meta,
                // so these are gated by the meta capability (KRI-216) rather
                // than the coarse canEditCaptions.
                Toggle("Captions", isOn: Binding(get: { session.draft.captions.enabled }, set: { _ in session.toggleCaptions() }))
                    .disabled(!session.canEditCaptionMeta)
                    .accessibilityIdentifier("native-editor-captions-toggle")
            } footer: {
                Text("Captions stay synchronized to the cut. Toggle them on to preview the readable version.")
            }
            Section("Style") {
                Picker("Caption style", selection: $style) { ForEach(styles, id: \.self, content: Text.init) }
                    .pickerStyle(.menu)
                    .onChange(of: style) { _, newValue in session.setCaptionStyle(newValue.lowercased()) }
                    .disabled(!session.canEditCaptionMeta)
                    .accessibilityIdentifier("native-editor-caption-style")
            }
            if !session.canEditCaptionMeta {
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
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    let onDone: () -> Void
    @StateObject private var player = NativeSfxAuditionPlayer()
    @StateObject private var waveforms = NativeSfxWaveformStore()

    private var selectedSfx: EditorTimedEffect? {
        panelDrafts.soundsTab == .effects ? NativeSoundEffectsPanelBody.selectedEffect(session) : nil
    }
    private var heading: String? { selectedSfx == nil ? nil : "Edit sound" }

    var body: some View {
        NativeEditorLanePanel(
            title: "Sounds", tabs: selectedSfx == nil ? [NativeSoundsTab.music, NativeSoundsTab.effects] : [],
            tab: $panelDrafts.soundsTab,
            onDone: { player.stop(); if selectedSfx != nil { session.select(nil) }; panelDrafts.sfxScreen = .home; onDone() },
            heading: heading,
            onAdd: selectedSfx == nil ? nil : { session.select(nil); panelDrafts.sfxScreen = .library },
            onDelete: selectedSfx.map { effect in { player.stop(); session.removeSoundEffect(id: effect.id); panelDrafts.sfxScreen = .home } },
            addLabel: "Add sound", addID: "native-editor-add-another-sfx",
            removeLabel: "Remove sound", removeID: "native-editor-remove-sfx"
        ) {
            switch panelDrafts.soundsTab {
            case .music: NativeSoundsControls(session: session, panelDrafts: panelDrafts)
            case .effects:
                NativeSoundEffectsPanelBody(session: session, panelDrafts: panelDrafts, player: player, waveforms: waveforms)
            }
        }
        .onChange(of: panelDrafts.soundsTab) { _, _ in player.stop(); session.songAudition.cancel() }
        .onDisappear { player.stop(); session.songAudition.cancel(); session.endTransaction() }
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
            if let song = session.yourSong {
                // KRI-374: the creator's own song is the soundtrack; no catalog entry, mix or alignment applies.
                NativeEditorYourSongRow(song: song, session: session)
            } else if session.userSongRemoved {
                // KRI-428: removed but unsaved. No catalog controls: the variant is reference-only, and
                // Undo (or leaving without saving) brings the song back.
                Label(NativeEditorYourSong.removedHelperCopy, systemImage: "speaker.wave.2")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("native-editor-your-song-removed")
            } else if session.document.music == nil {
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
            if session.yourSong == nil, !session.userSongRemoved, !session.canEditMix {
                Label("Music level is unavailable for this edit. Existing audio stays unchanged.", systemImage: "lock")
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
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
