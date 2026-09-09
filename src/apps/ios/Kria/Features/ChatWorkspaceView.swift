import SwiftUI

struct ChatWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("kria.workspace.last-project-id") private var lastProjectID = ""
    @State private var showsProjects = false
    @State private var showsGallery = false
    @State private var showsAccount = false

    var body: some View {
        NavigationStack {
            Group {
                if let project = model.selectedProject {
                    CreationWorkspaceView(
                        project: project,
                        openProjects: { showsProjects = true },
                        openAccount: { showsAccount = true }
                    )
                    .id(project.id)
                } else if model.isLoading || model.projectsState == .loading || model.projectsState == .idle {
                    WorkspaceLoadingView()
                } else if model.projectsState == .empty {
                    WorkspaceEmptyView { Task { await model.createProject() } }
                } else {
                    WorkspaceRecoveryView { Task { await model.openWorkspace() } }
                }
            }
            .toolbar(.hidden, for: .navigationBar)
        }
        .task { await model.openWorkspace(preferredProjectID: UUID(uuidString: lastProjectID)) }
        .onChange(of: model.selectedProject?.id) { _, identifier in
            lastProjectID = identifier?.uuidString ?? ""
        }
        .overlay {
            if showsProjects {
                ProjectsDrawer(
                    close: { showsProjects = false },
                    openGallery: {
                        showsProjects = false
                        showsGallery = true
                    }
                )
                .environmentObject(model)
                .transition(.opacity)
                .zIndex(10)
            }
        }
        .animation(.easeOut(duration: 0.2), value: showsProjects)
        .fullScreenCover(isPresented: $showsGallery) {
            NavigationStack { GalleryView() }
                .environmentObject(model)
        }
        .sheet(isPresented: $showsAccount) {
            NavigationStack { AccountView() }
        }
    }
}

private struct WorkspaceEmptyView: View {
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
        .background(KriaColor.paper)
    }
}

private struct WorkspaceLoadingView: View {
    var body: some View {
        VStack(spacing: 14) {
            ProgressView().tint(KriaColor.limeText)
            Text("Opening your conversation…")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(KriaColor.paper)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Opening your creation conversation")
    }
}

private struct WorkspaceRecoveryView: View {
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
        .background(KriaColor.paper)
    }
}

private struct CreationWorkspaceView: View {
    let project: ProjectSummary
    let openProjects: () -> Void
    let openAccount: () -> Void

    @EnvironmentObject private var model: AppModel
    @State private var prompt = ""
    @State private var events: [ThreadEvent] = []
    @State private var pendingMessages: [PendingChatMessage] = []
    @State private var pendingTurnSubmission: ChatTurnSubmissionIdentity?
    @State private var approval: ApprovalSnapshot?
    @State private var selectedFormat: CreationFormat?
    @State private var availableFormats: [CreationFormat] = [.montage]
    @State private var maximumClipsByFormat: [CreationFormat: Int] = [:]
    @State private var capabilitiesAreAuthoritative = false
    @State private var isChoosingFormat = false
    @State private var threadState: [String: JSONValue] = [:]
    @State private var afterSequence = -1
    @State private var threadRevision: Int
    @State private var isSending = false
    @State private var isActing = false
    @State private var isThinking = false
    @State private var errorMessage: String?
    @State private var showsAttachments = false
    @State private var showsResult = false

    init(project: ProjectSummary, openProjects: @escaping () -> Void, openAccount: @escaping () -> Void) {
        self.project = project
        self.openProjects = openProjects
        self.openAccount = openAccount
        _threadRevision = State(initialValue: project.serverRevision)
    }

    private var currentProject: ProjectSummary {
        model.projects.first(where: { $0.id == project.id }) ?? project
    }

    private var transcript: [ChatTranscriptMessage] {
        events.compactMap(ChatTranscriptMessage.from(event:)) + pendingMessages.map(\.transcriptMessage)
    }

    private var attachedClipCount: Int {
        attachedVideoClipCount(in: threadState)
    }

    private var selectedMaximumClipCount: Int {
        guard let selectedFormat else { return 10 }
        return maximumClipsByFormat[selectedFormat] ?? selectedFormat.fallbackMaximumClipCount
    }

    private var pendingUploadCount: Int {
        model.uploads.records.filter { $0.projectID == project.id }.count
    }

    private var isUITesting: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("-ui-testing-chat")
        #else
        false
        #endif
    }

    private var workspaceStage: WorkspaceStage {
        switch currentProject.status {
        case .ready: return .ready
        case .rendering: return .rendering
        case .failed: return .failed
        case .draft:
            if approval != nil { return .direction }
            if isChoosingFormat { return .format }
            if selectedFormat != nil { return .footage }
            return .format
        }
    }

    var body: some View {
        VStack(spacing: 0) {
            WorkspaceHeader(
                project: currentProject,
                showsEditorSwitch: workspaceStage == .ready,
                openProjects: openProjects,
                openEditor: { showsResult = true },
                openAccount: openAccount
            )

            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 20) {
                        ForEach(transcript) { message in
                            ChatMessageRow(message: message).id(message.id)
                        }

                        stageContent

                        if isThinking && workspaceStage != .rendering {
                            ThinkingRow().id("thinking")
                        }

                        if let errorMessage {
                            RecoveryCard(message: errorMessage) { Task { await refreshNow() } }
                                .id("recovery")
                        }

                        Color.clear.frame(height: 1).id("conversation-end")
                    }
                    .frame(maxWidth: 620, alignment: .leading)
                    .padding(.horizontal, 16)
                    .padding(.top, 96)
                    .padding(.bottom, 28)
                    .frame(maxWidth: .infinity)
                }
                .scrollDismissesKeyboard(.interactively)
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Conversation history")
                .onChange(of: events.count) { _, _ in scrollToEnd(proxy) }
                .onChange(of: pendingMessages.count) { _, _ in scrollToEnd(proxy) }
                .onChange(of: isThinking) { _, _ in scrollToEnd(proxy) }
            }
        }
        .background(KriaColor.paper)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            ChatComposer(
                text: $prompt,
                isSending: isSending,
                canAttach: selectedFormat != nil,
                attach: { if selectedFormat != nil { showsAttachments = true } },
                send: { Task { await send() } }
            )
        }
        .task {
            await refreshCapabilities()
            await pollUntilDismissed()
        }
        .onReceive(model.uploads.$attachedThreads) { threads in
            guard let thread = threads[project.id] else { return }
            apply(thread)
        }
        .sheet(isPresented: $showsAttachments) {
            AttachmentSheet(
                projectID: project.id,
                maximumClipCount: selectedMaximumClipCount,
                attachedClipCount: attachedClipCount
            )
                .environmentObject(model)
                .presentationDetents([.medium, .large])
        }
        .fullScreenCover(isPresented: $showsResult) {
            NativeEditorView(
                project: currentProject,
                onBack: { showsResult = false }
            )
                .environmentObject(model)
        }
    }

    @ViewBuilder private var stageContent: some View {
        switch workspaceStage {
        case .format:
            FormatStage(formats: availableFormats, isBusy: isActing, select: selectFormat).id("format-picker")
        case .footage:
            if transcript.last(where: { $0.role == .user }) == nil, let selectedFormat {
                ChatMessageRow(message: .syntheticUser(selectedFormat.choiceSentence))
            }
            if let selectedFormat {
                FootageStage(
                    format: selectedFormat,
                    mediaCount: attachedClipCount,
                    maximumClipCount: selectedMaximumClipCount,
                    uploads: model.uploads.records.filter { $0.projectID == project.id },
                    progress: model.uploads.progress,
                    addFootage: { showsAttachments = true },
                    continueWithFootage: {
                        Task { await send(message: "Continue with \(attachedClipCount) clips") }
                    },
                    changeFormat: { isChoosingFormat = true }
                )
                .id("upload-prompt")
            }
        case .direction:
            if let approval {
                DirectionStage(
                    approval: approval,
                    format: selectedFormat,
                    isBusy: isActing,
                    decide: decide
                )
                .id("approval-\(approval.id)")
            }
        case .rendering:
            RenderingStage().id("rendering")
        case .ready:
            ReadyStage(
                project: currentProject,
                openEditor: { showsResult = true },
                suggest: { prompt = $0 }
            )
            .id("ready")
        case .failed:
            FailedStage(retry: { Task { await refreshNow() } }).id("failed")
        }
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy) {
        withAnimation(.easeOut(duration: 0.22)) { proxy.scrollTo("conversation-end", anchor: .bottom) }
    }

    private func send(message submittedMessage: String? = nil) async {
        guard !isSending else { return }
        let message = (submittedMessage ?? prompt).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return }
        let submission = ChatTurnSubmissionIdentity.reusing(
            pendingTurnSubmission,
            for: message,
            expectedRevision: threadRevision
        )
        pendingTurnSubmission = submission
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        let accepted: TurnAccepted
        do {
            accepted = try await model.api.submitTurn(
                threadID: project.id,
                message: message,
                expectedRevision: submission.expectedRevision,
                clientEventID: submission.clientEventID
            )
        } catch APIError.conflict {
            pendingTurnSubmission = nil
            if let thread = try? await model.api.project(threadID: project.id) {
                apply(thread)
            }
            errorMessage = "This conversation changed while you were sending. Review it and try again."
            return
        } catch {
            errorMessage = "Your message wasn’t sent. \(error.localizedDescription)"
            return
        }
        pendingTurnSubmission = nil
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: accepted.threadRevision)
        pendingMessages.append(PendingChatMessage(content: message))
        prompt = ""
        isThinking = true
        errorMessage = await acceptedMutationRefreshError(
            "Your message was sent, but the conversation couldn’t refresh.",
            refresh: { try await refreshDelta() }
        )
    }

    private func selectFormat(_ format: CreationFormat) {
        guard !isActing else { return }
        Task {
            isActing = true
            errorMessage = nil
            defer { isActing = false }
            let clipLimit = maximumClipsByFormat[format] ?? format.fallbackMaximumClipCount
            let occupiedClipCount = attachedClipCount + pendingUploadCount
            if let capacityError = formatClipCapacityError(
                format: format,
                clipLimit: clipLimit,
                occupiedClipCount: occupiedClipCount
            ) {
                errorMessage = capacityError
                return
            }
            let thread: CreationThread
            do {
                thread = try await model.api.applyCreationAction(
                    threadID: project.id,
                    action: "select_format",
                    payload: ["format": .string(format.serverValue)],
                    expectedRevision: threadRevision
                )
            } catch let error as APIError where error == .conflict {
                await refreshCapabilities()
                await refreshNow()
                errorMessage = "That format is no longer available. Choose one of the refreshed options."
                return
            } catch {
                errorMessage = "That format wasn’t saved. \(error.localizedDescription)"
                return
            }
            apply(thread)
            selectedFormat = CreationFormat(thread: thread) ?? format
            isChoosingFormat = false
            errorMessage = await acceptedMutationRefreshError(
                "The format was saved, but the conversation couldn’t refresh.",
                refresh: { try await refreshDelta() }
            )
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
            if !formats.isEmpty { availableFormats = formats }
            maximumClipsByFormat = limits
            capabilitiesAreAuthoritative = true
        } catch {
            capabilitiesAreAuthoritative = false
            let projected = CreationFormat.available(in: events)
            if !projected.isEmpty { availableFormats = projected }
        }
    }

    private func pollUntilDismissed() async {
        await refreshNow()
        var delay: UInt64 = 1_000_000_000
        while !Task.isCancelled {
            do {
                let changed = try await refreshDelta()
                delay = changed ? 1_000_000_000 : min(delay * 2, 8_000_000_000)
            } catch is CancellationError {
                return
            } catch {
                if !isUITesting {
                    errorMessage = "Kria lost the live connection. Your conversation is safe."
                }
                delay = min(delay * 2, 15_000_000_000)
            }
            try? await Task.sleep(nanoseconds: delay)
        }
    }

    private func refreshNow() async {
        errorMessage = nil
        do {
            let thread = try await model.api.project(threadID: project.id)
            apply(thread)
            if thread.runtimeVersion == 2 {
                _ = try await refreshDelta()
            }
        } catch {
            if !isUITesting {
                errorMessage = "Kria couldn’t refresh this conversation. Check your connection and try again."
            }
        }
    }

    private func apply(_ thread: CreationThread) {
        let acceptsProjection = ThreadRevisionOrder.acceptsProjection(
            current: threadRevision,
            incoming: thread.revision
        )
        events = ChatTranscriptHistory.merge(events, with: thread.events)
        if !capabilitiesAreAuthoritative {
            let projected = CreationFormat.available(in: events)
            if !projected.isEmpty { availableFormats = projected }
        }
        afterSequence = ChatTranscriptHistory.nextAfterSequence(
            current: afterSequence,
            response: afterSequence,
            events: events
        )
        guard acceptsProjection else { return }
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: thread.revision)
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
            let thread = try await model.api.project(threadID: project.id)
            apply(thread)
            reconcilePendingMessages()
            let changed = threadRevision != previousRevision || events.map(\.id) != previousEventIDs
            if changed { errorMessage = nil }
            return changed
        }
        let delta = try await model.api.threadDelta(threadID: project.id, afterSequence: afterSequence)
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: delta.threadRevision)
        let known = Set(events.map(\.id))
        let fresh = delta.events.filter { !known.contains($0.id) }
        events = ChatTranscriptHistory.merge(events, with: fresh)
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
        try await synchronizeApproval()
        if !fresh.isEmpty || currentProject.status == .rendering {
            if let thread = try? await model.api.project(threadID: project.id) { apply(thread) }
        }
        if !fresh.isEmpty { errorMessage = nil }
        return !fresh.isEmpty
    }

    private func reconcilePendingMessages() {
        let durable = Set(events.compactMap { event -> String? in
            guard event.role == "user", event.eventType == "user_message" else { return nil }
            return event.content?.normalizedChatText
        })
        pendingMessages.removeAll { durable.contains($0.content.normalizedChatText) }
        if let submission = pendingTurnSubmission,
           durable.contains(submission.message.normalizedChatText) {
            pendingTurnSubmission = nil
            if prompt.normalizedChatText == submission.message.normalizedChatText { prompt = "" }
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
            return
        }
        if approval?.approvalID != identifier.uuidString {
            let fetched = try await model.api.approval(threadID: project.id, approvalID: identifier)
            let requestStillCurrent = events.contains(where: {
                $0.id == lastRequest.id && $0.sequence == lastRequest.sequence
            }) && !events.contains(where: {
                $0.sequence > lastRequest.sequence && terminalTypes.contains($0.eventType)
            })
            if requestStillCurrent { approval = fetched }
        }
    }

    private func decide(_ decision: String) {
        guard !isActing,
              let approval,
              let identifier = UUID(uuidString: approval.approvalID),
              let draftRevision = approval.draftRevision
        else { return }
        Task {
            isActing = true
            errorMessage = nil
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
                errorMessage = "Kria couldn’t record that decision. \(error.localizedDescription)"
                return
            }
            self.approval = nil
            isThinking = decision == "approve"
            errorMessage = await acceptedMutationRefreshError(
                "Kria recorded that decision, but the conversation couldn’t refresh.",
                refresh: { try await refreshDelta() }
            )
        }
    }
}

enum WorkspaceStage { case format, footage, direction, rendering, ready, failed }
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
enum ThreadRevisionOrder {
    static func advance(current: Int, incoming: Int) -> Int { max(current, incoming) }
    static func acceptsProjection(current: Int, incoming: Int) -> Bool { incoming >= current }
}

struct ChatTranscriptMessage: Identifiable, Equatable {
    let id: String
    let role: ChatMessageRole
    let content: String
    var isPending = false

    static func syntheticUser(_ content: String) -> Self {
        Self(id: "synthetic-\(content)", role: .user, content: content)
    }

    static func from(event: ThreadEvent) -> Self? {
        let conversationalAssistantEvents: Set<String> = [
            "format_prompt", "media_prompt", "upload_prompt", "voiceover_prompt",
            "confirm_generation", "confirmation", "revision_queued", "status_update",
            "assistant_question", "assistant_response", "assistant_strategy",
            "assistant_review", "draft_applied", "assistant_error", "assistant_render_failed", "memory_updated",
            "creator_memory_receipt", "agent_assistant_question", "agent_assistant_strategy",
            "agent_assistant_review", "agent_assistant_error", "agent_assistant_render_failed"
        ]
        let role: ChatMessageRole
        if event.role == "user" && event.eventType == "user_message" {
            role = .user
        } else if conversationalAssistantEvents.contains(event.eventType) {
            role = .assistant
        } else {
            return nil
        }
        let rawContent = event.content ?? event.payload?["message"]?.stringValue
        guard let content = rawContent?.trimmingCharacters(in: .whitespacesAndNewlines), !content.isEmpty else { return nil }
        return Self(id: event.id, role: role, content: content)
    }
}

private struct PendingChatMessage: Identifiable {
    let id = UUID()
    let content: String
    var transcriptMessage: ChatTranscriptMessage {
        ChatTranscriptMessage(id: "pending-\(id.uuidString)", role: .user, content: content, isPending: true)
    }
}

enum CreationFormat: String, CaseIterable, Identifiable {
    case montage
    case narrated
    case talkingToCamera

    var id: String { rawValue }
    var serverValue: String {
        switch self {
        case .montage: "montage"
        case .narrated: "narrated"
        case .talkingToCamera: "talking_to_camera"
        }
    }
    var title: String {
        switch self {
        case .montage: "Montage"
        case .narrated: "Narrated"
        case .talkingToCamera: "Talking"
        }
    }
    var choiceSentence: String {
        switch self {
        case .montage: "Let’s make a montage"
        case .narrated: "Let’s make it narrated"
        case .talkingToCamera: "Let’s make a talking video"
        }
    }
    var imageName: String {
        switch self {
        case .montage: "montage"
        case .narrated: "voiceover"
        case .talkingToCamera: "talking"
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
        default: return nil
        }
    }

    private init?(editFormat: String) {
        switch editFormat {
        case "montage": self = .montage
        case "narrated", "narrated_planned": self = .narrated
        case "subtitled", "talking_to_camera": self = .talkingToCamera
        default: return nil
        }
    }
}

private struct AttachmentSheet: View {
    let projectID: UUID
    let maximumClipCount: Int
    let attachedClipCount: Int
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView {
                FootagePickerView(
                    projectID: projectID,
                    uploads: model.uploads,
                    maximumClipCount: maximumClipCount,
                    attachedClipCount: attachedClipCount
                )
                .padding(20)
            }
                .background(KriaColor.paper)
                .navigationTitle("Add footage")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
                }
        }
    }
}

@MainActor
func acceptedMutationRefreshError(
    _ failurePrefix: String,
    refresh: () async throws -> Bool
) async -> String? {
    do {
        _ = try await refresh()
        return nil
    } catch {
        return "\(failurePrefix) \(error.localizedDescription)"
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
        switch status {
        case .draft: "Shaping direction"
        case .rendering: "Rendering"
        case .ready: "Ready"
        case .failed: "Needs attention"
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
