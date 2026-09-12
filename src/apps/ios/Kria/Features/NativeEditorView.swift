import SwiftUI

struct NativeEditorView: View {
    let project: ProjectSummary
    let libraryJobID: UUID?
    let onBack: () -> Void
    private let conversation: (() -> AnyView)?
    private let conversationAcceptedID: UUID?
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @StateObject private var session: NativeEditorSession
    @State private var selectedTool: NativeEditorTool?
    @State private var inspector: NativeEditorInspector?
    @State private var showsUnsavedExit = false
    @State private var showsDeviceRender = false
    @State private var showsConversation = false
    @State private var textInspectorID: String?
    @State private var selectedTextForActions: String?
    @State private var keyboardVisible = false
    @State private var timelineExpansion: CGFloat = 0
    @State private var timelineDragOrigin: CGFloat?
    @State private var timelineResizeFeedback = 0

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
                ?? NativeEditorSession(project: project, operations: LocalEditorOperations())
        )
    }

    var body: some View {
        GeometryReader { viewport in
            Group {
                switch session.loadState {
                case .loaded:
                    editor(viewport: viewport)
                case .idle, .loading:
                    NativeEditorLoadSurface(
                        title: "Opening the editor…",
                        detail: "Loading the latest cut and its editing controls.",
                        isLoading: true,
                        onBack: requestBack,
                        retry: nil
                    )
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
            .frame(width: viewport.size.width, height: viewport.size.height)
            .font(KriaFont.body())
            .foregroundStyle(KriaColor.ink)
            .background(KriaColor.paper)
            .navigationBarBackButtonHidden(true)
            .sheet(item: $inspector) { inspector in
                NativeEditorInspectorView(inspector: inspector, session: session)
                    .presentationDetents([.medium, .large])
                    .presentationDragIndicator(.visible)
            }
            .onChange(of: session.selectionRequest) { _, _ in
                guard let selection = session.selection else {
                    textInspectorID = nil
                    selectedTextForActions = nil
                    return
                }
                guard !session.isDirectManipulating, !session.isTimingGestureActive else { return }
                if selection.kind == .text {
                    if selectedTextForActions == selection.id {
                        textInspectorID = selection.id
                    } else {
                        selectedTextForActions = selection.id
                        textInspectorID = nil
                    }
                    inspector = nil
                    return
                }
                selectedTextForActions = nil
                textInspectorID = nil
                // Clip selection keeps the timeline handles and context strip
                // directly reachable. The explicit Adjust action presents the
                // clip inspector without covering the trim gesture surface.
                guard selection.kind != .clip else { return }
                inspector = .selection(selection)
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
            .task { await loadEditor() }
        }
    }

    @ViewBuilder private func editor(viewport: GeometryProxy) -> some View {
        let referenceHeight = session.pendingText == nil && !keyboardVisible
            ? viewport.size.height + viewport.safeAreaInsets.top + viewport.safeAreaInsets.bottom
            : viewport.size.height
        let portraitHeight = min(338, max(150, referenceHeight * 0.40))
        let defaultPreviewHeight = session.previewAspectRatio > 1 ? min(124, portraitHeight) : portraitHeight
        let resizeRange = max(0, defaultPreviewHeight - 80)
        let showsTimeline = session.pendingText == nil && textInspectorID == nil
        let showsContext = showsTimeline && (session.selection?.kind == .text || session.selectedClipID != nil)
        let previewHeight = max(80, defaultPreviewHeight - (showsTimeline ? timelineExpansion * resizeRange : 0) - (showsContext ? 52 : 0))
        VStack(spacing: 0) {
            NativeEditorProjectHeader(
                title: project.workspaceTitle, session: session,
                onBack: requestBack, onChat: conversation == nil ? requestBack : onBack
            )
            NativeEditorSaveBanner(session: session)
            if session.deviceRenderKey != nil {
                Button("Rendering on iPhone") { showsDeviceRender = true }
                    .font(KriaFont.body(12).weight(.semibold))
                    .frame(maxWidth: .infinity, minHeight: 44)
                    .accessibilityIdentifier("native-editor-device-render")
            }

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

            if session.pendingText != nil {
                Spacer(minLength: 0)
                NativeTextCreationPanel(session: session) { selection in
                    selectedTextForActions = selection.id
                    textInspectorID = selection.id
                }
            } else if let id = textInspectorID {
                NativeEditorTextPanel(id: id, session: session) { textInspectorID = nil }
                    .id(id)
            } else {
            timelineResizeHandle(range: resizeRange)
            NativeEditorTimeline(session: session)
                .frame(maxHeight: .infinity)
                .layoutPriority(1)

            if let selection = session.selection, selection.kind == .text {
                NativeEditorTextContextStrip(
                    onEdit: { textInspectorID = selection.id },
                    onDeselect: { session.select(nil) }
                )
                .transition(shouldReduceMotion ? .identity : .move(edge: .bottom).combined(with: .opacity))
            } else if session.selectedClipID != nil {
                NativeEditorContextStrip(session: session, onAdjust: { inspector = .adjust })
                    .transition(shouldReduceMotion ? .identity : .move(edge: .bottom).combined(with: .opacity))
            }

            NativeEditorToolRail(selected: $selectedTool) { tool in
                if tool == .text { session.beginTextCreation() }
                else { inspector = .tool(tool) }
            }
            }
        }
    }

    private func timelineResizeHandle(range: CGFloat) -> some View {
        Capsule()
            .fill(KriaColor.ink.opacity(0.28))
            .frame(width: 36, height: 4)
            .frame(maxWidth: .infinity)
            .frame(height: 44)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 3, coordinateSpace: .global)
                    .onChanged { value in
                        guard range > 0 else { return }
                        if timelineDragOrigin == nil {
                            timelineDragOrigin = timelineExpansion
                            timelineResizeFeedback += 1
                        }
                        let next = min(1, max(0, (timelineDragOrigin ?? 0) - value.translation.height / range))
                        if next != timelineExpansion && (next == 0 || next == 1) {
                            timelineResizeFeedback += 1
                        }
                        timelineExpansion = next
                    }
                    .onEnded { _ in
                        timelineDragOrigin = nil
                        timelineResizeFeedback += 1
                    }
            )
            .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: timelineResizeFeedback)
            .accessibilityElement()
            .accessibilityLabel("Timeline size")
            .accessibilityValue("\(Int(timelineExpansion * 100)) percent expanded")
            .accessibilityHint("Swipe up or down to resize the timeline and preview")
            .accessibilityAdjustableAction { direction in
                let step: CGFloat = direction == .increment ? 0.25 : -0.25
                let next = min(1, max(0, timelineExpansion + step))
                guard next != timelineExpansion else { return }
                withAnimation(shouldReduceMotion ? nil : .easeOut(duration: 0.18)) {
                    timelineExpansion = next
                }
                timelineResizeFeedback += 1
            }
            .accessibilityIdentifier("native-editor-timeline-resize")
    }

    private var deviceLocalFile: URL? {
        guard let key = session.deviceRenderKey else { return nil }
        return model.deviceRenders.presentations[key]?.localFile
    }

    private func loadEditor() async {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-source-text"),
           let url = Bundle.main.url(forResource: "montage", withExtension: "mp4") {
            let delayed = ProcessInfo.processInfo.arguments.contains("-ui-testing-editor-delayed-source")
            if delayed, session.loadState == .loaded { return }
            await session.prepareFixtureSourcePreview(url: url, delayedLoad: delayed)
            return
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") || ProcessInfo.processInfo.arguments.contains("-ui-testing-brand") { return }
        #endif
        session.useDeviceRendering(model.deviceRenders)
        guard session.loadState != .loaded else { return }
        if let libraryJobID {
            await session.load(libraryJobID: libraryJobID, api: model.api)
        } else {
            await session.load(project: project, api: model.api)
        }
        await session.refreshDeviceRender()
        if let file = deviceLocalFile { session.showDeviceOutput(file) }
    }

    private func requestBack() {
        if session.pendingText != nil { session.cancelTextCreation(); return }
        if textInspectorID != nil { textInspectorID = nil; return }
        if session.hasUnsavedChanges { showsUnsavedExit = true }
        else { onBack() }
    }

    private func saveAndExit() async {
        await session.save()
        guard !session.hasUnsavedChanges else { return }
        switch session.saveState {
        case .conflict, .loadFailed, .failed, .renderRetryNeeded:
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
                case .tool(.overlays): NativeEffectBrowserInspector(session: session, kinds: [.mediaOverlay], title: "Overlays")
                case .tool(.styles): NativeStylesInspector(session: session)
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
    @State private var volume = 0.75
    @State private var trackID = ""

    var body: some View {
        Form {
            Section("Music") {
                if session.document.music == nil {
                    TextField("Music track ID", text: $trackID)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .accessibilityIdentifier("native-editor-music-track-input")
                    Button("Add music lane") {
                        session.setMusic(trackID: trackID.trimmingCharacters(in: .whitespacesAndNewlines))
                    }
                    .disabled(!session.canEdit(.music) || trackID.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    .accessibilityIdentifier("native-editor-add-music")
                } else {
                    HStack {
                        Image(systemName: "speaker.wave.2")
                        NativeEditorSlider(session: session, value: $volume, in: 0...1) { Text("Music volume") }
                        .onChange(of: volume) { _, newValue in session.setMusicVolume(newValue) }
                        Text("\(Int(volume * 100))%")
                            .font(.system(.caption, design: .monospaced))
                            .frame(width: 42, alignment: .trailing)
                    }
                    .accessibilityIdentifier("native-editor-music-volume")
                    .disabled(!session.canEditMix)
                    Text(session.draft.music?.title ?? "Music")
                        .font(KriaFont.body(13))
                        .foregroundStyle(KriaColor.zinc)
                }
            }
            if !session.canEditMix {
                Section { Label("Music level is unavailable for this edit. Existing audio stays unchanged.", systemImage: "lock") }
            }
        }
        .onAppear { volume = session.draft.music?.volume ?? 0 }
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
