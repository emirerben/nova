import SwiftUI

struct ChatWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("kria.workspace.last-project-id") private var lastProjectID = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showsProjects = false
    @State private var drawerDrag: CGFloat = 0
    @GestureState private var drawerGestureActive = false
    @State private var drawerMounted = false
    @State private var horizontalDrawerDrag: Bool?
    @State private var drawerDragStartTime: Date?
    @State private var drawerGestureExclusions: [CGRect] = []
    @State private var showsGallery = false
    @State private var showsAccount = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        GeometryReader { geometry in
            let drawerWidth = min(326, max(0, geometry.size.width - 76))
            let drawerOffset = min(drawerWidth, max(0, (showsProjects ? drawerWidth : 0) + drawerDrag))
            let drawerActive = showsProjects || drawerOffset > 0
            let drawerProgress = drawerWidth > 0 ? drawerOffset / drawerWidth : 0
            let topInset = geometry.safeAreaInsets.top
            let bottomInset = geometry.safeAreaInsets.bottom
            ZStack(alignment: .topLeading) {
                if drawerActive || drawerMounted {
                    ProjectsDrawer(
                        close: { setDrawerOpen(false) },
                        openGallery: { setDrawerOpen(false); showsGallery = true },
                        openAccount: { setDrawerOpen(false); showsAccount = true }
                    )
                    .frame(width: drawerWidth, height: geometry.size.height)
                    .offset(x: drawerOffset - drawerWidth, y: topInset)
                    .accessibilityHidden(!drawerActive)
                    .allowsHitTesting(drawerActive)
                }
                NavigationStack {
                    Group {
                        if let project = model.selectedProject {
                            CreationWorkspaceView(
                                project: project,
                                openProjects: { setDrawerOpen(!showsProjects) },
                                openAccount: { showsAccount = true }
                            )
                            .id(project.id)
                        } else {
                            Group {
                                if model.isLoading || model.projectsState == .loading || model.projectsState == .idle {
                                    WorkspaceLoadingView()
                                } else if model.projectsState == .empty {
                                    WorkspaceEmptyView { Task { await model.createProject() } }
                                } else {
                                    WorkspaceRecoveryView { Task { await model.openWorkspace() } }
                                }
                            }
                            .accessibilityHidden(showsProjects)
                            .allowsHitTesting(!showsProjects)
                            .safeAreaInset(edge: .top) {
                                HStack {
                                    Button { setDrawerOpen(!showsProjects) } label: {
                                        KriaIcon(.menu).frame(width: 44, height: 44).background(KriaColor.menu, in: Circle())
                                    }
                                    .accessibilityLabel(showsProjects ? "Close projects" : "Open projects")
                                    .accessibilityIdentifier("workspace-menu-toggle")
                                    Spacer()
                                    KriaWordmark().accessibilityHidden(showsProjects)
                                    Spacer()
                                    Color.clear.frame(width: 44, height: 44).accessibilityHidden(true)
                                }.padding(.horizontal, 16).frame(minHeight: 64).background(WorkspaceSurface())
                            }
                        }
                    }
                    .toolbar(.hidden, for: .navigationBar)
                }
                .environment(\.projectsDrawerOpen, drawerActive)
                .frame(width: geometry.size.width, height: geometry.size.height)
                .padding(.top, topInset)
                .padding(.bottom, bottomInset)
                .background(WorkspaceSurface())
                .clipShape(RoundedRectangle(cornerRadius: 44 * drawerProgress, style: .continuous))
                .offset(x: drawerOffset)
            }
            .environment(\.projectsDrawerProgress, drawerProgress)
            .frame(width: geometry.size.width, height: geometry.size.height + topInset + bottomInset, alignment: .topLeading)
            .clipped()
            .offset(y: -topInset)
            .background(KriaColor.paper.ignoresSafeArea())
            .onPreferenceChange(DrawerGestureExclusionPreference.self) { drawerGestureExclusions = $0 }
            .simultaneousGesture(drawerGesture(width: drawerWidth))
            .onChange(of: drawerGestureActive) { _, active in
                // onEnded is not called when another recognizer or the system
                // cancels a drag. GestureState resets for both outcomes.
                guard !active else { return }
                let needsSettlement = horizontalDrawerDrag == true || drawerDrag != 0
                horizontalDrawerDrag = nil
                if needsSettlement {
                    setDrawerOpen(drawerOffset > drawerWidth / 2)
                }
            }
            .accessibilityAction(.escape) { setDrawerOpen(false) }
        }
        .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: showsProjects)
        .onChange(of: showsProjects) { _, isOpen in
            if isOpen { UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil) }
        }
        .task {
            await model.openWorkspace(preferredProjectID: UUID(uuidString: lastProjectID))
            await model.sweepAbandonedChats()
        }
        .onChange(of: model.selectedProject?.id) { previous, identifier in
            lastProjectID = identifier?.uuidString ?? ""
            if let previous, previous != identifier { Task { await model.discardIfAbandoned(previous) } }
        }
        .onChange(of: scenePhase) { _, phase in
            guard phase == .background, let id = model.selectedProject?.id else { return }
            Task { await model.discardIfAbandoned(id) }
        }
        .fullScreenCover(isPresented: $showsGallery) {
            NavigationStack { GalleryView(openProjects: { showsGallery = false; setDrawerOpen(true) }) }
                .environmentObject(model)
        }
        .sheet(isPresented: $showsAccount) {
            NavigationStack { AccountView() }
        }
    }

    private func setDrawerOpen(_ isOpen: Bool) {
        // Commit the destination and release the drag in the same transaction.
        // GestureState's automatic reset previously replayed the closing offset.
        drawerMounted = true
        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.24)) {
            showsProjects = isOpen
            drawerDrag = 0
        } completion: {
            if !showsProjects && drawerDrag == 0 { drawerMounted = false }
        }
    }

    private func drawerGesture(width: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 3, coordinateSpace: .global)
            .updating($drawerGestureActive) { _, active, _ in active = true }
            .onChanged { value in
                // Choose an axis once, without a 20-point dead zone at touch-down.
                if horizontalDrawerDrag == nil {
                    let startsInScroller = !showsProjects && drawerGestureExclusions.contains { $0.contains(value.startLocation) }
                    horizontalDrawerDrag = !startsInScroller && abs(value.translation.width) > abs(value.translation.height)
                    drawerDragStartTime = value.time
                }
                guard horizontalDrawerDrag == true else { return }
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { drawerDrag = value.translation.width }
            }
            .onEnded { value in
                let wasHorizontal = horizontalDrawerDrag == true
                let startTime = drawerDragStartTime
                horizontalDrawerDrag = nil
                drawerDragStartTime = nil
                guard wasHorizontal else { return }
                // SwiftUI's own value.velocity/predictedEndTranslation are
                // measured (not requested) and, for a short, fast release,
                // unreliable here — the same nominally-fast flick has been
                // observed reporting wildly different velocities from one run
                // to the next. Elapsed wall-clock time over the gesture's own
                // translation is exact and reproduces the real release speed.
                let elapsed = startTime.map { value.time.timeIntervalSince($0) } ?? 0
                let measuredVelocity = elapsed > 0.001 ? value.translation.width / elapsed : 0
                let isDeliberateFlick = abs(measuredVelocity) > Self.flickVelocityThreshold
                let isOpen: Bool
                if isDeliberateFlick {
                    // Honor a deliberate flick's direction outright, regardless
                    // of exactly how far it travelled.
                    isOpen = measuredVelocity > 0
                } else {
                    // Anything slower settles purely by how far it was actually
                    // dragged, landing at the true nearest endpoint.
                    let settledOffset = (showsProjects ? width : 0) + value.translation.width
                    isOpen = settledOffset > width / 2
                }
                setDrawerOpen(isOpen)
            }
    }

    // XCUIGestureVelocity.slow synthesizes ~250pt/s and .fast ~750pt/s; a real
    // deliberate flick is comfortably faster than an intentional slow drag.
    private static let flickVelocityThreshold: CGFloat = 450
}

private struct WorkspaceEmptyView: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    let create: () -> Void
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Start your first video.").font(KriaFont.display(29))
            Text("Create a project, then tell Kria what you want the finished cut to feel like.")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
            if let error = model.errorMessage {
                Text(error).font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText)
            }
            Button("New video", action: create).buttonStyle(CanonicalPrimaryButtonStyle())
        }
        .padding(24)
        .frame(maxWidth: 480, maxHeight: .infinity, alignment: .leading)
        .background(WorkspaceSurface())
    }
}

private struct WorkspaceLoadingView: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    var body: some View {
        VStack(spacing: 14) {
            ProgressView().tint(KriaColor.ink)
            Text("Opening your conversation…")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(WorkspaceSurface())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Opening your creation conversation")
    }
}

private struct WorkspaceRecoveryView: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    let retry: () -> Void
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Your workspace couldn’t open.").font(KriaFont.display(29))
            Text(model.errorMessage ?? "Check your connection, then try again. Your projects are safe.")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
            Button("Try again", action: retry).buttonStyle(CanonicalPrimaryButtonStyle())
        }
        .padding(24)
        .frame(maxWidth: 480, maxHeight: .infinity, alignment: .leading)
        .background(WorkspaceSurface())
    }
}

private struct ConversationEntrance: ViewModifier {
    let visible: Bool
    let order: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(visible ? 1 : 0)
            .offset(y: visible || reduceMotion ? 0 : -6)
            .animation(
                reduceMotion ? .easeOut(duration: 0.15) :
                    .timingCurve(0.23, 1, 0.32, 1, duration: 0.2).delay(min(Double(order) * 0.035, 0.105)),
                value: visible
            )
    }
}

private struct CreationWorkspaceView: View {
    let project: ProjectSummary
    let openProjects: () -> Void
    let openAccount: () -> Void

    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    @FocusState private var composerFocused: Bool
    @State private var prompt = ""
    @State private var events: [ThreadEvent] = []
    @State private var initialConversationLoaded = false
    @State private var pendingMessages: [ChatPendingMessage] = []
    @State private var submissionAnchor: ChatPendingMessage?
    @State private var uploadAnchors: [UUID: ChatPendingUpload] = [:]
    @State private var nextLocalOrder = 0
    @State private var scrollRequest = 0
    @State private var hasHistoryBaseline = false
    @StateObject private var responsePresentation = ChatResponsePresentation()
    @Environment(\.scenePhase) private var scenePhase
    @State private var pendingTurnSubmission: ChatTurnSubmissionIdentity?
    @State private var approval: ApprovalSnapshot?
    @State private var approvalNotice: String?
    @State private var selectedFormat: CreationFormat?
    @State private var availableFormats: [CreationFormat] = []
    @State private var capabilities: CreationCapabilities?
    @State private var capabilitiesFailure: ChatFailure?
    @State private var fullThread: CreationThread?
    @State private var pendingAction: CreationActionIdentity?
    @State private var uploadRecords: [UploadRecoveryRecord] = []
    @State private var uploadProgress: [UUID: Double] = [:]
    /// What the coordinator is still preparing, and the assets the user has chosen. `uploadRecords`
    /// alone is blind to a clip until it is prepared AND reserved, so without these Continue would
    /// be enabled while chosen clips were still on their way.
    @State private var uploadInFlight: [UUID: BackgroundUploadCoordinator.InFlightUpload] = [:]
    @State private var photoSelections: [String: ProjectPhotoSelection] = [:]
    @State private var uploadFailures: [UploadFailure] = []
    @State private var previewVersion = 0
    @State private var maximumClipsByFormat: [CreationFormat: Int] = [:]
    @State private var capabilitiesAreAuthoritative = false
    @State private var isChoosingFormat = false
    @State private var threadState: [String: JSONValue] = [:]
    @State private var afterSequence = -1
    @State private var threadRevision: Int
    @State private var projectionOrder = ThreadProjectionOrder()
    @State private var isSending = false
    @State private var isActing = false
    @State private var isThinking = false
    @State private var failure: ChatFailure?
    /// The server's reason the last confirmation was rejected, shown inside the card.
    @State private var confirmationConflict: CreationConfirmationConflict?
    @State private var showsAttachments = false
    @State private var showsResult = false
    @StateObject private var editorSession: NativeEditorSession
    @State private var conversationAcceptedID: UUID?

    init(project: ProjectSummary, openProjects: @escaping () -> Void, openAccount: @escaping () -> Void) {
        self.project = project
        self.openProjects = openProjects
        self.openAccount = openAccount
        _threadRevision = State(initialValue: project.serverRevision)
        _editorSession = StateObject(wrappedValue: NativeEditorSession(project: project))
    }

    private var currentProject: ProjectSummary {
        model.projects.first(where: { $0.id == project.id }) ?? project
    }

    private var canAttachMedia: Bool {
        selectedFormat != nil && !isSending && !isActing && !isThinking
            && (selectedFormat != .slides || hasDedicatedSlideWorkspace)
            && currentProject.status != .rendering && (selectedFormat == .slides || currentProject.status != .ready)
    }

    private var readyMediaCount: Int {
        guard workspaceStage == .footage else { return 0 }
        // A slide post is composed from the PlanItemAsset Visuals pool. The
        // primary clip list is intentionally irrelevant: the server rejects it
        // for this format, and counting it here would enable a misleading send.
        if selectedFormat == .slides { return fullThread?.readyVisualCount ?? 0 }
        if attachedClipCount > 0 { return attachedClipCount }
        let destination = ProjectUploadDestination.resolve(
            capabilities: capabilities?.phoneRendering, capabilitiesLoaded: capabilitiesAreAuthoritative,
            sourcePurposes: [], role: .visual
        )
        return selectedFormat == .montage && destination.visualKinds != nil ? fullThread?.deviceReadyVisualCount ?? 0 : 0
    }

    private var hasUploadFailures: Bool {
        uploadFailures.contains { $0.projectID == project.id }
    }

    private var activeProposalEvent: ThreadEvent? {
        if let approval {
            return events.last { $0.eventType == "draft_applied" && $0.payload?["turn_id"]?.stringValue == approval.turnID }
        }
        guard let thread = fullThread else { return nil }
        let planHash = thread.creatorAgent?["plan_hash"]?.stringValue
        return events.last {
            ["assistant_strategy", "agent_assistant_strategy"].contains($0.eventType)
                && (planHash == nil || $0.payload?["plan_hash"]?.stringValue == planHash)
        }
    }

    /// A stage belongs beside the event that introduced it, not below every
    /// subsequent message. This is also stable when a poll arrives during Send.
    private var stageAnchor: ChatTimelineStageAnchor? {
        guard fullThread != nil || isUITesting else { return nil }
        let anchor: ThreadEvent?
        switch workspaceStage {
        case .format:
            anchor = events.last { $0.eventType == "format_prompt" }
        case .footage:
            anchor = events.last { ["action_select_format", "media_prompt", "upload_prompt"].contains($0.eventType) }
        case .direction:
            anchor = activeProposalEvent ?? events.last { $0.eventType == "approval_requested" }
        case .rendering:
            anchor = events.last { ["action_generate", "action_retry", "agent_assistant_execution", "render_started", "generation_started", "render_queued"].contains($0.eventType) }
        case .ready:
            anchor = events.last { ["agent_assistant_review", "assistant_review", "generation_ready"].contains($0.eventType) }
        case .failed:
            anchor = events.last { ["agent_assistant_error", "assistant_error", "agent_assistant_render_failed", "assistant_render_failed", "generation_failed"].contains($0.eventType) }
        }
        return ChatTimelineStageAnchor(id: "stage-\(workspaceStage)", afterSequence: anchor?.sequence ?? afterSequence)
    }

    private var suppressedTimelineMessages: Set<String> {
        if workspaceStage == .format, let event = events.last(where: { $0.eventType == "format_prompt" }) {
            return [event.id]
        }
        if workspaceStage == .direction, let event = activeProposalEvent { return [event.id] }
        return []
    }

    private var activeUploadIDs: Set<UUID> {
        var ids = Set(uploadRecords.filter { $0.projectID == project.id }.map(\.id))
        ids.formUnion(uploadInFlight.filter { $0.value.projectID == project.id }.keys)
        if let selection = photoSelections[project.id.uuidString] {
            ids.formUnion(selection.entries.values.filter { $0.mediaID == nil }.map(\.recordID))
        }
        return ids
    }

    private var timeline: [ChatTimelineEntry] {
        ChatTimeline.build(
            events: events, pending: pendingMessages, stage: stageAnchor,
            suppressedMessageIDs: suppressedTimelineMessages,
            uploads: activeUploadIDs.compactMap { uploadAnchors[$0] }
        )
    }

    private var timelineUpdateToken: String {
        timeline.map(\.id).joined(separator: "|") + "|\(isThinking)|\(isSending)|\(failure?.message ?? "")"
    }

    @ViewBuilder private var conversationContent: some View {
        ForEach(ChatTimelineGroup.group(timeline)) { group in
            if case .media = group.entries[0].content {
                mediaReceipts(group.entries)
            } else {
                timelineRow(group.entries[0])
            }
        }
        if (isThinking || isSending) && workspaceStage != .rendering { ThinkingRow().id("thinking") }
        if let approvalNotice { Text(approvalNotice).font(KriaFont.body(13)) }
        if let failure {
            RecoveryCard(failure: failure) { Task { await refreshCapabilities(); await refreshNow() } }
                .id("recovery")
        }
    }

    @ViewBuilder private func timelineRow(_ entry: ChatTimelineEntry) -> some View {
        switch entry.content {
        case .message(let message):
            ChatMessageRow(message: message, onSelectOption: { option in Task { await send(message: option) } },
                           responseStartedAt: responsePresentation.startTime(for: message.id))
                .id(entry.id)
        case .stage:
            stageContent.id(entry.id)
        case .pendingUpload(let recordID):
            HStack(spacing: 10) {
                CreationRecordThumbnail(recordID: recordID, version: previewVersion)
                Text(uploadRecords.first { $0.id == recordID }.map { BackgroundUploadCoordinator.displayFilename($0.filename) }
                     ?? uploadInFlight[recordID]?.filename ?? "Preparing media…")
                    .font(KriaFont.body(12)).lineLimit(1)
                Spacer()
                ProgressView(value: uploadProgress[recordID] ?? 0).frame(width: 60).tint(KriaColor.ink)
            }
            .accessibilityIdentifier("chat-upload-\(recordID.uuidString)")
            .id(entry.id)
        case .media:
            EmptyView() // Consecutive receipts are rendered together above.
        }
    }

    private func mediaReceipts(_ entries: [ChatTimelineEntry]) -> some View {
        let media = entries.flatMap { entry -> [CreationAttachedMedia] in
            guard case .media(let event) = entry.content else { return [] }
            let current = Dictionary(CreationAttachedMedia.parse(threadState).map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
            return CreationAttachedMedia.parse(event.payload ?? [:]).map { current[$0.id] ?? $0 }
        }
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: 10) {
                ForEach(Array(media.enumerated()), id: \.offset) { _, attachment in
                    VStack(alignment: .leading, spacing: 5) {
                        CreationAttachmentThumbnail(media: attachment)
                        Text(attachment.filename).font(KriaFont.body(11)).lineLimit(2)
                    }
                    .frame(width: 92, alignment: .leading)
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("Uploaded \(attachment.filename)")
                    .accessibilityIdentifier("chat-media-\(attachment.id)")
                }
            }
        }
        .background {
            GeometryReader { geometry in
                Color.clear.preference(key: DrawerGestureExclusionPreference.self, value: [geometry.frame(in: .global)])
            }
        }
    }

    private func openAttachments() {
        guard canAttachMedia else { return }
        scrollRequest += 1
        showsAttachments = true
    }

    private func rememberUploadAnchors() {
        let newIDs = activeUploadIDs.subtracting(uploadAnchors.keys)
        for id in newIDs.sorted(by: { $0.uuidString < $1.uuidString }) {
            nextLocalOrder += 1
            uploadAnchors[id] = ChatPendingUpload(id: id, afterSequence: afterSequence, localOrder: nextLocalOrder)
        }
    }

    private func observeResponses() {
        responsePresentation.observe(events: events, isInitialLoad: !hasHistoryBaseline,
                                     isActive: scenePhase == .active && !projectsDrawerOpen && !showsAttachments)
        hasHistoryBaseline = true
    }

    private var attachedClipCount: Int {
        attachedVideoClipCount(in: threadState)
    }

    private var selectedMaximumClipCount: Int {
        guard let selectedFormat else { return 10 }
        return maximumClipsByFormat[selectedFormat] ?? selectedFormat.fallbackMaximumClipCount
    }

    /// Clips being prepared or still being chosen — not yet visible as an upload record.
    private var preparingUploadCount: Int {
        BackgroundUploadCoordinator.reservedCount(projectID: project.id, role: nil, inFlight: uploadInFlight, records: uploadRecords, selections: photoSelections)
    }

    private var pendingUploadCount: Int {
        uploadRecords.filter { $0.projectID == project.id }.count + preparingUploadCount
    }

    private var isUITesting: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("-ui-testing-chat")
        #else
        false
        #endif
    }

    /// A new Creator plan is proposed and not yet confirmed or declined,
    /// across both the runtime-v1 (`creatorAgent.status`) and runtime-v2
    /// (live `approval`, synchronized with expiry checked) mechanisms.
    private var awaitsNewPlanConfirmation: Bool {
        approval != nil || fullThread?.awaitsCreationConfirmation == true
    }

    private var workspaceStage: WorkspaceStage {
        WorkspaceStage.resolve(
            status: currentProject.status,
            awaitsNewPlanConfirmation: awaitsNewPlanConfirmation,
            isChoosingFormat: isChoosingFormat,
            hasFormat: selectedFormat != nil,
            preparationIsActive: fullThread?.preparationIsActive == true,
            preparationFailed: fullThread?.preparationFailed == true
        )
    }

    /// A slide post switches out of the generic creator runtime as soon as
    /// select_format has provisioned its PlanItemAsset pool. Before then, keep
    /// polling the existing chat rather than presenting an upload surface with
    /// no item to reserve against.
    private var hasDedicatedSlideWorkspace: Bool {
        !isChoosingFormat && selectedFormat == .slides && fullThread?.activePlanItemID != nil
    }

    var body: some View {
        Group {
            if hasDedicatedSlideWorkspace {
                SlidePostWorkspaceView(
                    project: currentProject,
                    thread: fullThread,
                    capabilities: capabilities,
                    capabilitiesLoaded: capabilitiesAreAuthoritative,
                    conversation: { AnyView(editorConversation) },
                    conversationAcceptedID: conversationAcceptedID,
                    onBack: { isChoosingFormat = true },
                    onAddMedia: openAttachments
                )
                .environmentObject(model)
            } else {
                genericChatWorkspace
            }
        }
        .background(WorkspaceSurface())
        .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: responsePresentation.hapticToken)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            if !hasDedicatedSlideWorkspace {
                ChatComposer(
                    text: $prompt,
                    isSending: isSending || isActing,
                    canAttach: canAttachMedia,
                    canSendWithoutText: readyMediaCount > 0,
                    blocksSubmission: isThinking || pendingUploadCount > 0 || hasUploadFailures,
                    placeholder: readyMediaCount > 0 ? "Add instructions (optional)" : "Tell Kria what you want…",
                    isFocused: $composerFocused,
                    attach: openAttachments,
                    send: { Task { await send() } }
                )
                .accessibilityHidden(projectsDrawerOpen)
                .allowsHitTesting(!projectsDrawerOpen)
            }
        }
        .onAppear { if prompt.isEmpty { prompt = model.chatDrafts.draft(for: project.id) } }
        .onChange(of: prompt) { _, text in model.chatDrafts.setDraft(text, for: project.id) }
        .task {
            // History should not wait for the independent capability request.
            async let capabilities: Void = refreshCapabilities()
            await pollUntilDismissed()
            await capabilities
        }
        .onChange(of: currentProject.serverRevision) { _, revision in
            threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: revision)
        }
        .onReceive(model.uploads.$records) { uploadRecords = $0; rememberUploadAnchors() }
        .onReceive(model.uploads.$inFlight) { uploadInFlight = $0; rememberUploadAnchors() }
        .onReceive(model.uploads.$photoSelections) { photoSelections = $0; rememberUploadAnchors() }
        .onReceive(model.uploads.$failures) { uploadFailures = $0 }
        .onReceive(model.uploads.$previewVersion) { previewVersion = $0 }
        .onReceive(model.uploads.$progress) { uploadProgress = $0 }
        .onReceive(model.uploads.$attachedThreads) { threads in
            guard let thread = threads[project.id] else { return }
            apply(thread)
        }
        .sheet(isPresented: $showsAttachments) {
            AttachmentSheet(
                projectID: project.id,
                maximumClipCount: selectedMaximumClipCount,
                attachedClipCount: attachedClipCount,
                format: selectedFormat,
                thread: fullThread,
                capabilities: capabilities,
                capabilitiesLoaded: capabilitiesAreAuthoritative,
                refresh: {
                    if !capabilitiesAreAuthoritative { await refreshCapabilities() }
                    await refreshNow()
                }
            )
                .environmentObject(model)
                .presentationDetents([.medium, .large])
        }
        .fullScreenCover(isPresented: $showsResult) {
            NativeEditorView(
                project: currentProject,
                sharedSession: editorSession,
                conversationAcceptedID: conversationAcceptedID,
                conversation: { AnyView(editorConversation) },
                onBack: { showsResult = false }
            )
                .environmentObject(model)
        }
    }

    private var genericChatWorkspace: some View {
        VStack(spacing: 0) {
            WorkspaceHeader(
                project: currentProject,
                // `currentProject.status` (not `workspaceStage`) so the switch
                // to the existing cut stays available even while a new plan's
                // confirmation card is showing on top of it.
                showsEditorSwitch: currentProject.status == .ready,
                openProjects: openProjects,
                openEditor: { showsResult = true },
                openAccount: openAccount
            )
            .simultaneousGesture(TapGesture().onEnded { composerFocused = false })

            ChatConversationScroll(isLoaded: initialConversationLoaded, updateToken: timelineUpdateToken, scrollRequest: scrollRequest, dismissKeyboard: { composerFocused = false }) {
                conversationContent
            }
            .accessibilityHidden(projectsDrawerOpen)
            .allowsHitTesting(!projectsDrawerOpen)
        }
    }

    private var editorConversation: some View {
        VStack(spacing: 0) {
            Text("Kria").font(KriaFont.body(17).weight(.semibold)).padding(.top, 20)
                .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
            ChatConversationScroll(isLoaded: initialConversationLoaded, updateToken: timelineUpdateToken, scrollRequest: scrollRequest, dismissKeyboard: { composerFocused = false }) {
                conversationContent
            }
            ChatComposer(
                text: $prompt, isSending: isSending || isActing,
                canAttach: false, blocksSubmission: isThinking || pendingUploadCount > 0 || hasUploadFailures, isFocused: $composerFocused, attach: {}, send: { Task { await send() } }
            )
        }
        .background(KriaColor.paper)
        .accessibilityIdentifier("native-editor-project-conversation")
    }

    @ViewBuilder private var stageContent: some View {
        switch workspaceStage {
        case .format:
            FormatStage(formats: availableFormats, isBusy: isActing || !capabilitiesAreAuthoritative, select: selectFormat).id("format-picker")
            if let capabilitiesFailure {
                RecoveryCard(failure: capabilitiesFailure) { Task { await refreshCapabilities() } }
            } else if !capabilitiesAreAuthoritative {
                ProgressView("Loading formats…")
            }
        case .footage:
            if let selectedFormat {
                FootageStage(
                    format: selectedFormat,
                    mediaCount: selectedFormat == .slides ? 0 : attachedClipCount,
                    maximumClipCount: selectedMaximumClipCount,
                    uploads: [],
                    preparingCount: 0,
                    previewVersion: previewVersion,
                    progress: uploadProgress,
                    addFootage: openAttachments,
                    changeFormat: { isChoosingFormat = true },
                    attachedMedia: [],
                    isBusy: isSending || isActing || isThinking,
                    removeMedia: { mediaID in performAction("remove_media", payload: ["media_id": .string(mediaID)]) },
                    failures: uploadFailures.filter { $0.projectID == project.id },
                    dismissFailure: { model.uploads.dismissFailure(id: $0) },
                    // Only a project this iPhone renders can be made of Visuals
                    // alone, and only from the Visuals the server's rule counts.
                    visualCount: selectedFormat == .slides
                        ? (fullThread?.readyVisualCount ?? 0)
                        : ProjectUploadDestination.resolve(
                        capabilities: capabilities?.phoneRendering, capabilitiesLoaded: capabilitiesAreAuthoritative,
                        sourcePurposes: [], role: .visual
                    ).visualKinds == nil ? 0 : fullThread?.deviceReadyVisualCount ?? 0
                )
                .id("upload-prompt")
            }
        case .direction:
            if let approval {
                DirectionStage(
                    approval: approval,
                    format: selectedFormat,
                    isBusy: isActing || pendingUploadCount > 0,
                    responseStartedAt: activeProposalEvent.flatMap { responsePresentation.startTime(for: $0.id) },
                    decide: decide
                )
                .id("approval-\(approval.id)")
            } else if let thread = fullThread {
                CreationConfirmationStage(
                    thread: thread,
                    isBusy: isActing || isSending || pendingUploadCount > 0,
                    responseStartedAt: activeProposalEvent.flatMap { responsePresentation.startTime(for: $0.id) },
                    conflict: visibleConfirmationConflict(for: thread),
                    refreshDirection: refreshDirection,
                    action: performAction
                )
            }
            // A new plan's confirmation card can appear over an already-ready
            // cut (see `WorkspaceStage.resolve`); confirming it is a choice,
            // not something the old cut's reachability should be sacrificed for.
            if currentProject.status == .ready {
                Button("Open current cut", action: { showsResult = true })
                    .buttonStyle(CanonicalSecondaryButtonStyle())
                    .disabled(isActing)
                    .accessibilityIdentifier("open-current-cut")
            }
        case .rendering:
            if let deviceRenderKey {
                DeviceRenderPanel(key: deviceRenderKey, sessions: model.deviceRenders) {
                    await refreshCapabilities()
                    await refreshDeviceRender(retry: true)
                }
            } else {
                RenderingStage(
                    isPreparing: currentProject.activeJobID == nil,
                    preparationMessage: fullThread?.preparationMessage,
                    preparationCompleted: fullThread?.preparationCompleted ?? 0,
                    preparationTotal: fullThread?.preparationTotal ?? 0
                ).id("rendering")
            }
        case .ready:
            ReadyStage(
                project: currentProject,
                openEditor: { showsResult = true },
                suggest: { prompt = $0 }
            )
            .id("ready")
            if let variants = fullThread?.job?.variants, variants.count > 1 {
                ForEach(variants.compactMap { $0.variantID }, id: \.self) { variantID in
                    let variant = variants.first { $0.variantID == variantID }
                    Button(variantID.replacingOccurrences(of: "_", with: " ").capitalized) {
                        performAction("select_variant", payload: ["variant_id": .string(variantID)])
                    }.disabled(isActing || variant?.isPlayable != true)
                    if variant?.renderStatus == "failed", currentProject.runtimeVersion == 1 {
                        Button("Retry \(variantID.replacingOccurrences(of: "_", with: " "))") {
                            performAction("retry", payload: ["variant_id": .string(variantID)])
                        }.disabled(isActing)
                    }
                }
            }
        case .failed:
            if let thread = fullThread, thread.runtimeVersion == 1 {
                CreationConfirmationStage(
                    thread: thread,
                    isBusy: isActing || isSending || pendingUploadCount > 0,
                    responseStartedAt: activeProposalEvent.flatMap { responsePresentation.startTime(for: $0.id) },
                    conflict: visibleConfirmationConflict(for: thread),
                    refreshDirection: refreshDirection,
                    action: performAction
                )
            } else {
                if fullThread?.preparationFailed == true {
                    FailedStage(
                        title: "Clip preparation needs another try",
                        bodyText: fullThread?.preparationMessage ?? fullThread?.lastAssistantErrorMessage ?? "Kria couldn’t prepare your clips. Your direction and footage are still saved.",
                        retryLabel: "Retry preparing clips",
                        retry: fullThread?.preparationRetryable == true ? { Task { await send(message: "Retry preparing my clips.") } } : nil
                    ).id("failed-preparation")
                } else {
                    FailedStage(retry: { Task { await send(message: "Try generating this edit again") } }).id("failed")
                }
            }
        }
    }

    /// Mirrors `stageContent`: whether the runtime-v1 confirmation card is on screen.
    private var showsCreationConfirmation: Bool {
        guard let fullThread else { return false }
        switch workspaceStage {
        case .direction: return approval == nil
        case .failed: return fullThread.runtimeVersion == 1
        default: return false
        }
    }

    private func visibleConfirmationConflict(for thread: CreationThread) -> CreationConfirmationConflict? {
        guard let confirmationConflict, confirmationConflict.planIdentity == thread.creatorPlanIdentity else { return nil }
        return confirmationConflict
    }

    /// Asks Kria to plan again against the footage attached now, so a direction
    /// the server rejected as stale can be confirmed.
    private func refreshDirection() {
        Task { await send(message: CreationConfirmationConflict.refreshDirectionMessage) }
    }

    private func send(message submittedMessage: String? = nil) async {
        // Slide direction is intentionally handled by SlidePostWorkspaceView.
        // Generic creator runtime has no slide proposal/create tools.
        guard selectedFormat != .slides else { return }
        guard !isSending, !isActing, !isThinking,
              let message = ChatSubmission.message(
                text: submittedMessage ?? prompt, readyMediaCount: readyMediaCount,
                pendingUploadCount: pendingUploadCount, hasUploadFailures: hasUploadFailures
              ) else { return }
        let draftToRestore = submittedMessage == nil ? prompt : message
        isSending = true
        failure = nil
        confirmationConflict = nil
        defer { isSending = false }
        if editorSession.hasUnsavedChanges {
            await editorSession.save()
            guard !editorSession.hasUnsavedChanges else {
                failure = ChatFailure("Your message is still here. Save or resolve your editor changes before sending it.")
                return
            }
            await refreshNow()
        }
        let submission = ChatTurnSubmissionIdentity.reusing(
            pendingTurnSubmission, for: message, expectedRevision: threadRevision
        )
        pendingTurnSubmission = submission
        let optimistic: ChatPendingMessage
        if let previous = submissionAnchor, previous.clientEventID == submission.clientEventID {
            optimistic = previous
        } else {
            nextLocalOrder += 1
            optimistic = ChatPendingMessage(content: message, clientEventID: submission.clientEventID,
                                            afterSequence: afterSequence, localOrder: nextLocalOrder)
        }
        submissionAnchor = optimistic
        if !pendingMessages.contains(where: { $0.id == optimistic.id }) { pendingMessages.append(optimistic) }
        prompt = ""
        scrollRequest += 1
        if currentProject.runtimeVersion != 2 {
            let requestSequence = projectionOrder.begin()
            do {
                let thread = try await model.api.sendCreationMessage(threadID: project.id, message: message, expectedRevision: submission.expectedRevision, clientEventID: submission.clientEventID)
                pendingTurnSubmission = nil
                apply(thread, requestSequence: requestSequence)
                conversationAcceptedID = UUID()
                await editorSession.synchronizePromptRevision()
            } catch APIError.conflict {
                pendingMessages.removeAll { $0.id == optimistic.id }
                if prompt.isEmpty { prompt = draftToRestore }
                pendingTurnSubmission = nil
                submissionAnchor = nil
                await refreshNow()
                failure = ChatFailure("This conversation changed. Your message is still here; review the latest direction and send again.")
            } catch {
                pendingMessages.removeAll { $0.id == optimistic.id }
                if prompt.isEmpty { prompt = draftToRestore }
                failure = ChatFailure(
                    "Kria couldn’t confirm that message. Your draft is saved here; retry to check it safely.",
                    cause: RequestFailureCause(error)
                )
            }
            return
        }
        let accepted: TurnAccepted
        do {
            accepted = try await model.api.submitTurn(
                threadID: project.id, message: message,
                expectedRevision: submission.expectedRevision, clientEventID: submission.clientEventID
            )
        } catch APIError.conflict {
            pendingMessages.removeAll { $0.id == optimistic.id }
            if prompt.isEmpty { prompt = draftToRestore }
            pendingTurnSubmission = nil
            submissionAnchor = nil
            if let thread = try? await model.api.project(threadID: project.id) { apply(thread) }
            failure = ChatFailure("This conversation changed while you were sending. Review it and try again.")
            return
        } catch {
            pendingMessages.removeAll { $0.id == optimistic.id }
            if prompt.isEmpty { prompt = draftToRestore }
            failure = ChatFailure("Kria couldn’t confirm that message. Your draft is saved here; retry to check it safely.", error: error)
            return
        }
        pendingTurnSubmission = nil
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: accepted.threadRevision)
        conversationAcceptedID = UUID()
        isThinking = true
        failure = await acceptedMutationRefreshError(
            "Your message was sent, but the conversation couldn’t refresh.",
            refresh: { try await refreshDelta() }
        )
    }

    private func selectFormat(_ format: CreationFormat) {
        guard capabilitiesAreAuthoritative, availableFormats.contains(format) else { return }
        if format.usesVisualPool, attachedClipCount > 0 {
            failure = ChatFailure("Photo & video posts use Photos & videos. Remove primary footage before switching formats.")
            return
        }
        let pendingClips = model.uploads.records.filter { $0.projectID == project.id && $0.role == .clip }.count
        if let capacityError = formatClipCapacityError(format: format, clipLimit: maximumClipsByFormat[format] ?? format.fallbackMaximumClipCount, occupiedClipCount: attachedClipCount + pendingClips) {
            failure = ChatFailure(capacityError)
            return
        }
        performAction("select_format", payload: ["format": .string(format.serverValue)])
    }

    private func performAction(_ action: String, payload: [String: JSONValue] = [:]) {
        guard !isActing, !isSending else { return }
        isActing = true
        failure = nil
        confirmationConflict = nil
        let identity = CreationActionIdentity.reusing(pendingAction, action: action, payload: payload, revision: threadRevision)
        pendingAction = identity
        Task {
            defer { isActing = false }
            do {
                let requestSequence = projectionOrder.begin()
                let thread = try await model.api.creationAction(threadID: project.id, action: action, payload: payload, expectedRevision: identity.revision, clientActionID: identity.id)
                pendingAction = nil
                apply(thread, requestSequence: requestSequence)
                if action == "select_format" { isChoosingFormat = false }
                failure = await acceptedMutationRefreshError("Saved, but the conversation couldn’t refresh.", refresh: { try await refreshDelta() })
            } catch let error as APIError where error == .conflict {
                pendingAction = nil
                await refreshCapabilities()
                await refreshNow()
                guard CreationConfirmationConflict.confirmationActions.contains(action), payload["variant_id"] == nil else {
                    failure = ChatFailure(CreationConfirmationConflict.changedMessage)
                    return
                }
                let conflict = CreationConfirmationConflict(
                    detail: error.conflictDetail,
                    planIdentity: fullThread?.creatorPlanIdentity ?? ""
                )
                if showsCreationConfirmation { confirmationConflict = conflict }
                else { failure = ChatFailure(conflict.message) }
            } catch is CancellationError {
                // The view went away mid-request; leave pendingAction alone so an
                // in-flight duplicate can still be deduplicated if this task is
                // somehow still observed.
            } catch {
                // Any other failure must clear pendingAction and refresh, or a
                // retry replays this identity's now-stale `expected_revision`
                // and loops on 409s forever.
                pendingAction = nil
                await refreshCapabilities()
                await refreshNow()
                failure = ChatFailure("That change wasn’t saved.", error: error)
            }
        }
    }

    private func refreshCapabilities() async {
        do {
            let response = try await model.api.creationCapabilities()
            var limits: [CreationFormat: Int] = [:]
            let formats = response.formats.compactMap { capability -> CreationFormat? in
                guard let format = CreationFormat(serverValue: capability.id) else { return nil }
                limits[format] = max(1, capability.maxClips)
                return format
            }
            // The service advertises capability, not presentation order. Keep
            // the post as the final card even if a rollout reorders its list.
            availableFormats = formats.sorted { $0.carouselOrder < $1.carouselOrder }
            capabilities = response
            capabilitiesFailure = formats.isEmpty ? ChatFailure("No creation formats are currently available. Try again in a moment.") : nil
            maximumClipsByFormat = limits
            capabilitiesAreAuthoritative = true
            // Initial history and capabilities load independently. Reconcile
            // only after this authoritative response arrives; substituting a
            // temporary `.disabled` capability can terminalize a device job.
            await refreshDeviceRender()
        } catch is CancellationError {
            // The view went away mid-request; leave the last-known state as-is.
        } catch let error as URLError where error.code == .cancelled {
            // URLSession reports a cancelled task (e.g. the attachment sheet
            // closing) this way; it must not wipe what another request loaded.
        } catch {
            capabilitiesAreAuthoritative = false
            capabilities = nil
            availableFormats = []
            capabilitiesFailure = ChatFailure("Kria couldn’t load creation options.", error: error)
        }
    }

    private func pollUntilDismissed() async {
        await refreshNow()
        await refreshDeviceRender()
        var delay: UInt64 = 1_000_000_000
        while !Task.isCancelled {
            do {
                let changed = try await refreshDelta()
                await refreshDeviceRender()
                delay = changed || isSending || isActing || currentProject.status == .rendering || fullThread?.preparationIsActive == true
                    ? 1_000_000_000 : min(delay * 2, 8_000_000_000)
            } catch is CancellationError {
                return
            } catch {
                if !isUITesting {
                    failure = RequestFailureCause(error) == .connection
                        ? ChatFailure("Kria lost the live connection. Your conversation is safe.", cause: .connection)
                        : ChatFailure("Kria couldn’t refresh this conversation.", error: error)
                }
                delay = min(delay * 2, 15_000_000_000)
            }
            try? await Task.sleep(nanoseconds: delay)
        }
    }

    private var deviceRenderKey: DeviceRenderKey? {
        guard let job = fullThread?.job, let jobID = UUID(uuidString: job.id),
              let variant = job.variants.first(where: {
                  $0.renderDestination == "device" || $0.renderStatus == "awaiting_device"
              }), let variantID = variant.variantID else { return nil }
        return DeviceRenderKey(projectID: project.id, jobID: jobID, variantID: variantID)
    }

    private func refreshDeviceRender(retry: Bool = false) async {
        guard capabilitiesAreAuthoritative,
              let deviceRenderKey,
              let phoneRendering = capabilities?.phoneRendering else { return }
        await model.deviceRenders.reconcile(deviceRenderKey, capabilities: phoneRendering, retry: retry)
    }

    private func refreshNow() async {
        defer {
            if !Task.isCancelled { initialConversationLoaded = true }
        }
        failure = nil
        do {
            let requestSequence = projectionOrder.begin()
            let thread = try await model.api.project(threadID: project.id)
            apply(thread, requestSequence: requestSequence)
            // The full projection contains the complete transcript. Reveal it
            // before a slow delta or approval request returns, so the user
            // never sees an empty ready state followed by old AI messages.
            if fullThread != nil, !Task.isCancelled {
                initialConversationLoaded = true
            }
            if thread.runtimeVersion == 2 {
                _ = try await refreshDelta()
            }
        } catch is CancellationError {
            // The view went away mid-request; leave the last-known state as-is.
        } catch {
            if !isUITesting {
                failure = ChatFailure("Kria couldn’t refresh this conversation.", error: error)
            }
        }
    }

    private func apply(_ thread: CreationThread, requestSequence: Int? = nil) {
        let acceptsProjection = ThreadRevisionOrder.acceptsProjection(
            current: threadRevision,
            incoming: thread.revision
        )
        events = ChatTranscriptHistory.merge(events, with: thread.events)
        observeResponses()

        afterSequence = ChatTranscriptHistory.nextAfterSequence(
            current: afterSequence,
            response: afterSequence,
            events: events
        )
        reconcilePendingMessages()
        guard acceptsProjection, projectionOrder.accept(requestSequence) else { return }
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: thread.revision)
        fullThread = thread
        threadState = thread.state ?? [:]
        selectedFormat = CreationFormat(thread: thread) ?? selectedFormat
        model.updateProject(thread.summary)
    }

    @discardableResult private func refreshDelta() async throws -> Bool {
        // Runtime-v1 projects predate the delta endpoint but remain part of a
        // creator's real project history. Poll their authoritative full
        // projection so opening an older project never replaces its transcript
        // with an empty chat or leaves a permanent connection error banner.
        if currentProject.runtimeVersion != 2 {
            let previousRevision = threadRevision
            let previousEventIDs = events.map(\.id)
            let requestSequence = projectionOrder.begin()
            let thread = try await model.api.project(threadID: project.id)
            apply(thread, requestSequence: requestSequence)
            reconcilePendingMessages()
            let changed = threadRevision != previousRevision || events.map(\.id) != previousEventIDs
            clearChatRefreshRecoveryMessage(&failure)
            return changed
        }
        let delta = try await model.api.threadDelta(threadID: project.id, afterSequence: afterSequence)
        let revisionBeforeDelta = threadRevision
        // `threadRevision` advances as soon as the delta is acknowledged, even
        // if the subsequent full-projection request fails. Keep the revision
        // of the last successfully applied projection separately so the next
        // idle delta retries a title (or another mutable projection) sync.
        let lastFullProjectionRevision = fullThread?.revision ?? project.serverRevision
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: delta.threadRevision)
        let known = Set(events.map(\.id))
        let fresh = delta.events.filter { !known.contains($0.id) }
        events = ChatTranscriptHistory.merge(events, with: fresh)
        observeResponses()
        afterSequence = ChatTranscriptHistory.nextAfterSequence(
            current: afterSequence,
            response: delta.nextAfterSequence,
            events: events
        )
        if let latestFormat = events.reversed().compactMap({ CreationFormat(event: $0) }).first {
            selectedFormat = latestFormat
        }
        reconcilePendingMessages()
        let settledThinkingTypes: Set<String> = [
            "generation_started", "render_queued", "render_started", "rendering",
            "generation_ready", "render_failed", "generation_failed"
        ]
        if fresh.contains(where: {
            ChatTranscriptMessage.from(event: $0)?.role == .assistant || settledThinkingTypes.contains($0.eventType)
        }) {
            isThinking = false
        }
        // The delta fetch and merge above already succeeded, so the banner is
        // stale regardless of what happens next — clear it here rather than
        // after `synchronizeApproval()`, whose own failure would otherwise
        // re-arm the recovery card underneath messages that just landed
        // successfully.
        clearChatRefreshRecoveryMessage(&failure)
        try await synchronizeApproval()
        // A background mutation, such as assigning the first-prompt title,
        // advances the thread revision but may intentionally be a system event
        // that does not produce a chat row. Delta responses do not contain the
        // mutable thread projection (including `title`), so fetch it whenever
        // the revision advances as well as for ordinary conversational updates.
        if ThreadProjectionRefresh.requiresFullProjection(
            lastFullProjectionRevision: lastFullProjectionRevision,
            incomingRevision: delta.threadRevision,
            receivedEvents: fresh,
            isRendering: currentProject.status == .rendering
        ) {
            if let thread = try? await model.api.project(threadID: project.id) { apply(thread) }
        }
        if !fresh.isEmpty, !isThinking { await editorSession.synchronizePromptRevision() }
        return !fresh.isEmpty || threadRevision > revisionBeforeDelta
    }

    private func reconcilePendingMessages() {
        var candidates = pendingMessages
        if let submissionAnchor, !candidates.contains(where: { $0.id == submissionAnchor.id }) {
            candidates.append(submissionAnchor)
        }
        let acknowledged = ChatPendingReconciliation.acknowledged(candidates, events: events)
        pendingMessages.removeAll { acknowledged.contains($0.id) }
        if let submissionAnchor, acknowledged.contains(submissionAnchor.id) {
            if let submission = pendingTurnSubmission, submission.clientEventID == submissionAnchor.clientEventID {
                pendingTurnSubmission = nil
                if prompt.normalizedChatText == submission.message.normalizedChatText { prompt = "" }
            }
            self.submissionAnchor = nil
        }
    }

    private func synchronizeApproval() async throws {
        let terminalTypes = Set(["approval_approved", "approval_denied", "approval_cancelled", "approval_expired"])
        let lastRequest = events.last(where: { $0.eventType == "approval_requested" })
        guard let lastRequest,
              !events.contains(where: { $0.sequence > lastRequest.sequence && terminalTypes.contains($0.eventType) }),
              let identifier = lastRequest.payload?["approval_id"]?.stringValue.flatMap(UUID.init(uuidString:))
        else {
            approval = nil
            approvalNotice = nil
            return
        }
        if approval?.approvalID != identifier.uuidString || (approval?.expiresAt ?? .distantPast) <= .now {
            let fetched = try await model.api.approval(threadID: project.id, approvalID: identifier)
            let requestStillCurrent = events.contains(where: {
                $0.id == lastRequest.id && $0.sequence == lastRequest.sequence
            }) && !events.contains(where: {
                $0.sequence > lastRequest.sequence && terminalTypes.contains($0.eventType)
            })
            if requestStillCurrent {
                approval = fetched.status == "pending" && fetched.expiresAt > .now ? fetched : nil
                approvalNotice = fetched.status == "expired" || fetched.expiresAt <= .now
                    ? "This approval expired. Send a message to request an updated direction." : nil
            }
        }
    }

    private func decide(_ decision: String) {
        guard !isActing, pendingUploadCount == 0,
              let approval, approval.expiresAt > .now,
              let identifier = UUID(uuidString: approval.approvalID),
              let draftRevision = approval.draftRevision
        else { return }
        isActing = true
        Task {
            failure = nil
            defer { isActing = false }
            do {
                try await model.api.decideApproval(
                    threadID: project.id,
                    approvalID: identifier,
                    decision: decision,
                    expectedThreadRevision: threadRevision,
                    expectedDraftRevision: draftRevision,
                    fingerprint: approval.approvalFingerprint
                )
            } catch {
                await refreshNow()
                failure = ChatFailure("Kria couldn’t record that decision.", error: error)
                return
            }
            self.approval = nil
            isThinking = decision == "approve"
            failure = await acceptedMutationRefreshError(
                "Kria recorded that decision, but the conversation couldn’t refresh.",
                refresh: { try await refreshDelta() }
            )
        }
    }
}

enum WorkspaceStage {
    case format, footage, direction, rendering, ready, failed

    /// Pure decision table behind `CreationWorkspaceView.workspaceStage`, split
    /// out so the precedence rules (a pending plan vs. the last job's status)
    /// are unit-testable without standing up the view. See
    /// `ChatWorkspaceTests` for the full resolution table.
    static func resolve(
        status: ProjectStatus,
        awaitsNewPlanConfirmation: Bool,
        isChoosingFormat: Bool,
        hasFormat: Bool,
        preparationIsActive: Bool = false,
        preparationFailed: Bool = false
    ) -> WorkspaceStage {
        if preparationIsActive { return .rendering }
        if preparationFailed { return .failed }
        switch status {
        case .rendering:
            // A render already in flight wins even over a freshly proposed
            // plan: the old job's progress must stay visible and reachable.
            return .rendering
        case .ready:
            return awaitsNewPlanConfirmation ? .direction : .ready
        case .failed:
            return awaitsNewPlanConfirmation ? .direction : .failed
        case .draft:
            if awaitsNewPlanConfirmation { return .direction }
            if isChoosingFormat { return .format }
            if hasFormat { return .footage }
            return .format
        }
    }
}

enum ChatMessageRole: Equatable { case user, assistant }

/// Keeps append-only history stable while full projections and forward deltas
/// overlap. Event IDs are authoritative; sequence determines display/cursor
/// order, with the ID tie-breaker making equal-sequence responses deterministic.
enum ChatTranscriptHistory {
    static func merge(_ existing: [ThreadEvent], with incoming: [ThreadEvent]) -> [ThreadEvent] {
        var byID: [String: ThreadEvent] = [:]
        for event in existing + incoming {
            byID[event.id] = event
        }
        return byID.values.sorted {
            if $0.sequence != $1.sequence { return $0.sequence < $1.sequence }
            return $0.id < $1.id
        }
    }

    static func nextAfterSequence(current: Int, response: Int, events: [ThreadEvent]) -> Int {
        max(current, response, events.map(\.sequence).max() ?? current)
    }
}

/// Network requests overlap by design: a poll can start before a submit or
/// action and finish after it. Event history is append-only and may always be
/// merged, but mutable projections must never move back to an older revision.
struct ThreadProjectionOrder {
    private var issued = 0
    private var accepted = 0
    mutating func begin() -> Int { issued += 1; return issued }
    mutating func accept(_ sequence: Int?) -> Bool {
        let sequence = sequence ?? begin()
        guard sequence >= accepted else { return false }
        accepted = sequence
        return true
    }
}

enum ThreadRevisionOrder {
    static func advance(current: Int, incoming: Int) -> Int { max(current, incoming) }
    static func acceptsProjection(current: Int, incoming: Int) -> Bool { incoming >= current }
}

/// Deltas carry append-only events and a revision, while a full thread carries
/// mutable fields such as the user-visible title. Keep the decision pure so a
/// non-conversational background update cannot leave the workspace header or
/// Recent chats stale. The comparison is with the last *applied full*
/// projection, not the last delta, because a transient projection-fetch
/// failure must retry on the next otherwise-idle delta.
enum ThreadProjectionRefresh {
    static func requiresFullProjection(
        lastFullProjectionRevision: Int,
        incomingRevision: Int,
        receivedEvents: [ThreadEvent],
        isRendering: Bool
    ) -> Bool {
        incomingRevision > lastFullProjectionRevision || !receivedEvents.isEmpty || isRendering
    }
}

struct ChatTranscriptMessage: Identifiable, Equatable {
    let id: String
    let role: ChatMessageRole
    let content: String
    var isPending = false
    var isProposal = false
    /// A clarifying question's exact, tappable reply choices. Sending one back
    /// verbatim is required — the backend matches it by casefolded,
    /// whitespace-collapsed equality against the fenced mapping it sent with
    /// the question, so a hand-typed paraphrase never resolves it.
    var options: [String] = []
    var recommendedOption: String? = nil

    static func syntheticUser(_ content: String) -> Self {
        Self(id: "synthetic-\(content)", role: .user, content: content)
    }

    static let clarifyingQuestionEventTypes: Set<String> = ["assistant_question", "agent_assistant_question"]

    static func from(event: ThreadEvent) -> Self? {
        let conversationalAssistantEvents: Set<String> = [
            "format_prompt", "media_prompt", "upload_prompt", "voiceover_prompt",
            "confirm_generation", "confirmation", "revision_queued", "status_update",
            "assistant_question", "assistant_response", "assistant_strategy",
            "assistant_review", "draft_applied", "assistant_error", "assistant_render_failed", "memory_updated",
            "creator_memory_receipt", "agent_assistant_question", "agent_assistant_strategy",
            "agent_assistant_review", "agent_assistant_error", "agent_assistant_render_failed",
            "agent_assistant_execution", "assistant_execution"
        ]
        if event.eventType == "action_select_format", let format = CreationFormat(event: event) {
            return Self(id: event.id, role: .user, content: format.choiceSentence)
        }
        let role: ChatMessageRole
        if event.role == "user" && event.eventType == "user_message" {
            role = .user
        } else if conversationalAssistantEvents.contains(event.eventType) {
            role = .assistant
        } else {
            return nil
        }
        let isProposal = ["assistant_strategy", "agent_assistant_strategy"].contains(event.eventType)
        let proposalSummary = isProposal ? event.payload?["proposal_summary"]?.stringValue : nil
        let rawContent = [proposalSummary, event.content, event.payload?["message"]?.stringValue]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }.first { !$0.isEmpty }
        guard let content = rawContent?.trimmingCharacters(in: .whitespacesAndNewlines), !content.isEmpty else { return nil }
        var options: [String] = []
        var recommendedOption: String?
        if role == .assistant && clarifyingQuestionEventTypes.contains(event.eventType) {
            options = (event.payload?["options"]?.arrayValue ?? []).compactMap { $0.stringValue }
                .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            recommendedOption = event.payload?["recommended_option"]?.stringValue
        }
        return Self(id: event.id, role: role, content: content, isProposal: isProposal, options: options, recommendedOption: recommendedOption)
    }
}


enum CreationFormat: String, CaseIterable, Identifiable {
    case montage
    case narrated
    case talkingToCamera
    case slides

    var id: String { rawValue }
    var serverValue: String {
        switch self {
        case .montage: "montage"
        case .narrated: "narrated"
        case .talkingToCamera: "talking_to_camera"
        case .slides: "slides"
        }
    }
    var title: String {
        switch self {
        case .montage: "Montage"
        case .narrated: "Narrated"
        case .talkingToCamera: "Talking"
        case .slides: "Photo & video post"
        }
    }
    var choiceSentence: String {
        switch self {
        case .montage: "Let’s make a montage"
        case .narrated: "Let’s make it narrated"
        case .talkingToCamera: "Let’s make a talking video"
        case .slides: "Let’s make a photo and video post"
        }
    }
    var imageName: String {
        switch self {
        case .montage: "montage"
        case .narrated: "voiceover"
        case .talkingToCamera: "talking"
        case .slides: "trulli-street"
        }
    }

    var fallbackMaximumClipCount: Int {
        self == .talkingToCamera ? 1 : 10
    }

    init?(thread: CreationThread) {
        if let format = thread.state?["format"]?.stringValue {
            self.init(serverValue: format)
            return
        }
        guard let editFormat = thread.state?["edit_format"]?.stringValue else { return nil }
        self.init(editFormat: editFormat)
    }

    init?(event: ThreadEvent) {
        guard event.eventType == "action_select_format" || event.eventType == "action_select_edit_format" else { return nil }
        if let format = event.payload?["format"]?.stringValue {
            self.init(serverValue: format)
        } else if let editFormat = event.payload?["edit_format"]?.stringValue {
            self.init(editFormat: editFormat)
        } else { return nil }
    }

    static func available(in events: [ThreadEvent]) -> [CreationFormat] {
        guard let prompt = events.last(where: { $0.eventType == "format_prompt" }),
              case let .object(formats) = prompt.payload?["formats"] else { return [] }
        return allCases.filter { formats[$0.serverValue] != nil }
    }

    init?(serverValue: String) {
        switch serverValue {
        case "montage": self = .montage
        case "narrated": self = .narrated
        case "talking_to_camera": self = .talkingToCamera
        case "slides": self = .slides
        default: return nil
        }
    }

    private init?(editFormat: String) {
        switch editFormat {
        case "montage": self = .montage
        case "narrated", "narrated_planned": self = .narrated
        case "subtitled", "talking_to_camera": self = .talkingToCamera
        case "slides": self = .slides
        default: return nil
        }
    }
}

extension CreationFormat {
    var usesVisualPool: Bool { self == .slides }
    var carouselOrder: Int {
        switch self {
        case .montage: 0
        case .narrated: 1
        case .talkingToCamera: 2
        case .slides: 3
        }
    }
}

extension CreationThread {
    /// Slide composition requires fully ready pool assets. `current` includes
    /// imports that can still fail, so it must never unlock the chat composer.
    /// `device_ready` is retained as an older-server fallback for the existing
    /// device-render pilot; current slide-capable servers return `ready`.
    var readyVisualCount: Int {
        let visuals = mediaCapabilities?["visuals"]?.objectValue
        if case .number(let count) = visuals?["ready"] { return max(0, Int(count)) }
        if case .number(let count) = visuals?["device_ready"] { return max(0, Int(count)) }
        return 0
    }
}

@MainActor
func acceptedMutationRefreshError(
    _ failurePrefix: String,
    refresh: () async throws -> Bool
) async -> ChatFailure? {
    do {
        _ = try await refresh()
        return nil
    } catch {
        return ChatFailure(failurePrefix, error: error)
    }
}

/// A completed full or delta response is authoritative even when it contains
/// no new events, so it clears a stale refresh-recovery banner.
func clearChatRefreshRecoveryMessage(_ failure: inout ChatFailure?) {
    guard let current = failure?.message else { return }
    let recoveryPrefixes = [
        "Kria lost the live connection.",
        "Kria couldn’t refresh this conversation.",
        "Your message was sent, but the conversation couldn’t refresh.",
        "Saved, but the conversation couldn’t refresh.",
        "Kria recorded that decision, but the conversation couldn’t refresh.",
        "Kria couldn’t confirm that message."
    ]
    if recoveryPrefixes.contains(where: current.hasPrefix) {
        failure = nil
    }
}

func attachedVideoClipCount(in threadState: [String: JSONValue]) -> Int {
    guard let media = threadState["media"] else {
        return Int(threadState["media_count"]?.numberValue ?? 0)
    }
    guard case .array(let entries) = media else { return 0 }
    return entries.filter { entry in
        guard case .object(let value) = entry else { return false }
        return value["kind"]?.stringValue == "video"
    }.count
}

func formatClipCapacityError(
    format: CreationFormat,
    clipLimit: Int,
    occupiedClipCount: Int
) -> String? {
    guard occupiedClipCount > clipLimit else { return nil }
    return "\(format.title) supports \(clipLimit) \(clipLimit == 1 ? "clip" : "clips"). Keep your current format or remove extra footage first."
}

extension ProjectSummary {
    var workspaceTitle: String {
        let cleaned = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty || cleaned.lowercased() == "untitled project" ? "Untitled video" : cleaned
    }

    var workspaceStatusLabel: String {
        // Mirrors `WorkspaceStage.resolve`: a pending plan outranks a stale
        // `.ready`/`.failed` label left over from the last job, but never a
        // render actually in flight.
        switch status {
        case .draft: "Shaping direction"
        case .rendering: "Rendering"
        case .ready: awaitsConfirmation ? "Shaping direction" : "Ready"
        case .failed: awaitsConfirmation ? "Shaping direction" : "Needs attention"
        }
    }
}

extension JSONValue {
    var numberValue: Double? { if case .number(let value) = self { value } else { nil } }
}

struct ChatTurnSubmissionIdentity: Equatable, Sendable {
    let message: String
    let clientEventID: String
    let expectedRevision: Int

    static func reusing(_ current: Self?, for message: String, expectedRevision: Int) -> Self {
        if let current, current.message.canonicalSubmissionText == message.canonicalSubmissionText {
            return current
        }
        return Self(message: message, clientEventID: UUID().uuidString, expectedRevision: expectedRevision)
    }
}

private extension String {
    var canonicalSubmissionText: String { split(whereSeparator: \.isWhitespace).joined(separator: " ") }
    var normalizedChatText: String { split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased() }
}
