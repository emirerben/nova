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
    @State private var keyboardVisible = false
    @State private var timelineExpansion: CGFloat = 0
    @State private var topChromeHeight: CGFloat = 0

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
            .navigationBarBackButtonHidden(true)
            .sheet(item: $inspector) { inspector in
                NativeEditorInspectorView(inspector: inspector, session: session)
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            }
            .onChange(of: session.selectionRequest) { _, _ in routeSelection() }
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
            .onDisappear {
                finishPanelEditing()
                session.suspendEditorImports()
                session.pausePlayback()
                exporter.removeSharedFile()
            }
        }
    }

    @ViewBuilder private func editor(viewport: GeometryProxy) -> some View {
        let referenceHeight = !keyboardVisible
            ? viewport.size.height + viewport.safeAreaInsets.top + viewport.safeAreaInsets.bottom
            : viewport.size.height
        let portraitHeight = dynamicTypeSize.isAccessibilitySize ? 150 : min(284, max(150, referenceHeight * 0.34))
        // Banners and the posting-song bar share this fixed-height column.
        // Their measured height comes out of the preview so the timeline and
        // tool rail stay on screen.
        let portraitBudget = max(80, portraitHeight - topChromeHeight)
        let preferredPreviewHeight = session.previewAspectRatio > 1 ? min(124, portraitBudget) : portraitBudget
        // Reserve room for the header, divider and usable text controls above
        // the keyboard rather than allowing their minimum heights to overflow.
        let defaultPreviewHeight = keyboardVisible
            ? min(preferredPreviewHeight, max(80, viewport.size.height - 320 - topChromeHeight))
            : preferredPreviewHeight
        let resizeRange = max(0, defaultPreviewHeight - 80)
        let showsTimeline = panel == nil
        let showsContext = showsTimeline && (session.selection?.kind == .text || session.selectedClipID != nil)
        // KRI-131: the context capsule now floats over the timeline instead
        // of pushing it up, so selecting a clip/text no longer shrinks the
        // preview.
        let previewHeight = max(80, defaultPreviewHeight - timelineExpansion * resizeRange)
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

            NativeVideoPreview(session: session)
                .frame(width: previewHeight * session.previewAspectRatio, height: previewHeight)
                .clipped()
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

            timelineResizeHandle(range: resizeRange)
            connectedEditorArea(viewport: viewport, showsContext: showsContext, resizeRange: resizeRange)
        }
        .environment(\.nativeEditorConnectedPanel, true)
        .environment(\.nativeEditorPanelLifecycle, panelLifecycle)
    }

    private var panelIsOpen: Bool { panel != nil }

    private var panelTransition: AnyTransition {
        shouldReduceMotion ? .opacity.animation(.easeOut(duration: 0.15)) : .opacity
    }

    private func connectedEditorArea(viewport: GeometryProxy, showsContext: Bool, resizeRange: CGFloat) -> some View {
        let bottomInset = viewport.safeAreaInsets.bottom
        let clearance = NativeEditorIslandMetrics.bottomClearance(showsContext: showsContext, safeAreaBottom: bottomInset)
        return GeometryReader { area in
            ZStack(alignment: .bottom) {
                NativeEditorTimeline(session: session, uploads: model.uploads, bottomClearance: clearance, isCovered: panelIsOpen)
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
                    NativeEditorTransport(session: session)
                        .frame(maxHeight: .infinity, alignment: .top)
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
                            } else if session.selectedClipID != nil {
                                NativeEditorContextStrip(session: session, onAdjust: { inspector = .adjust })
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
                                            expansion: $timelineExpansion,
                                            range: resizeRange,
                                            reduceMotion: shouldReduceMotion,
                                            accessibilityIdentifier: "native-editor-panel-resize",
                                            topAligned: true
                                        )
                                    }
                                    .transition(panelTransition)
                            }
                            if !keyboardVisible {
                                NativeEditorToolRail(selected: panel?.tool, availableWidth: area.size.width - 24, connected: true, onSelect: selectTool)
                            }
                        }
                        .frame(width: panelIsOpen ? max(0, area.size.width - 24) : min(328, max(0, area.size.width - 24)))
                        .frame(height: panelIsOpen ? panelHeight(available: area.size.height) : NativeEditorIslandMetrics.islandHeight)
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

    private func panelHeight(available: CGFloat) -> CGFloat {
        let budget = max(0, available - NativeEditorIslandMetrics.bottomPadding - (keyboardVisible ? 0 : 54))
        if keyboardVisible || dynamicTypeSize.isAccessibilitySize { return budget }
        return min(budget, 284 + timelineExpansion * 240)
    }

    @ViewBuilder private var panelContent: some View {
        switch panel {
        case .textCreation:
            NativeTextCreationPanel(session: session) { selection in
                selectedTextForActions = selection.id
                changePanel(to: .text(selection.id))
            }
        case .text(let id):
            NativeEditorTextPanel(id: id, session: session) { changePanel(to: nil) }.id(id)
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
                finishPanelEditing()
                session.beginTextCreation()
                guard session.pendingText != nil else { return }
                inspector = nil
                withAnimation(shouldReduceMotion ? nil : .spring(response: 0.34, dampingFraction: 0.88)) {
                    panel = .textCreation
                }
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

    private func timelineResizeHandle(range: CGFloat) -> some View {
        NativeEditorPanelResizeGrabber(
            expansion: $timelineExpansion,
            range: range,
            reduceMotion: shouldReduceMotion,
            accessibilityIdentifier: "native-editor-timeline-resize"
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
    enum Tab: String { case music = "Music" }
    @ObservedObject var session: NativeEditorSession
    @ObservedObject var panelDrafts: NativeEditorPanelDrafts
    let onDone: () -> Void
    @State private var tab: Tab = .music
    var body: some View {
        NativeEditorLanePanel(title: "Sounds", tabs: [Tab.music], tab: $tab, onDone: onDone) {
            NativeSoundsControls(session: session, panelDrafts: panelDrafts)
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

private struct NativeStylesInspector: View {
    @ObservedObject var session: NativeEditorSession
    @State private var selected = "Fraunces"
    private let presets = ["Fraunces", "Inter", "Space Grotesk"]

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Type sets the feeling before anyone reads the words.")
                .font(KriaFont.body(15))
                .foregroundStyle(KriaColor.zinc)
            ForEach(presets, id: \.self) { preset in
                Button {
                    selected = preset
                    session.setTextStyle(id: nil, style: preset)
                } label: {
                    HStack {
                        Text(preset).font(preset == "Fraunces" ? KriaFont.display(24) : KriaFont.body(19))
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
