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
                } else if model.isLoading {
                    WorkspaceLoadingView()
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
    @State private var approval: ApprovalSnapshot?
    @State private var selectedFormat: CreationFormat?
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

    private var attachedMediaCount: Int { Int(threadState["media_count"]?.numberValue ?? 0) }

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
                attach: { showsAttachments = true },
                send: { Task { await send() } }
            )
        }
        .task { await pollUntilDismissed() }
        .sheet(isPresented: $showsAttachments) {
            AttachmentSheet(projectID: project.id)
                .environmentObject(model)
                .presentationDetents([.medium, .large])
        }
        .fullScreenCover(isPresented: $showsResult) {
            NativeEditorView(
                project: currentProject,
                onProjects: {
                    showsResult = false
                    openProjects()
                },
                onChat: { showsResult = false }
            )
                .environmentObject(model)
        }
    }

    @ViewBuilder private var stageContent: some View {
        switch workspaceStage {
        case .format:
            FormatStage(isBusy: isActing, select: selectFormat).id("format-picker")
        case .footage:
            if transcript.last(where: { $0.role == .user }) == nil, let selectedFormat {
                ChatMessageRow(message: .syntheticUser(selectedFormat.choiceSentence))
            }
            if let selectedFormat {
                FootageStage(
                    format: selectedFormat,
                    mediaCount: attachedMediaCount,
                    uploads: model.uploads.records.filter { $0.projectID == project.id },
                    progress: model.uploads.progress,
                    addFootage: { showsAttachments = true },
                    continueWithFootage: {
                        Task { await send(message: "Continue with \(attachedMediaCount) clips") }
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
            RenderingStage(format: selectedFormat).id("rendering")
        case .ready:
            ReadyStage(project: currentProject, openEditor: { showsResult = true }).id("ready")
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
        isSending = true
        errorMessage = nil
        defer { isSending = false }
        do {
            let accepted = try await model.api.submitTurn(threadID: project.id, message: message, expectedRevision: threadRevision)
            threadRevision = accepted.threadRevision
            pendingMessages.append(PendingChatMessage(content: message))
            prompt = ""
            isThinking = true
            try await refreshDelta()
        } catch {
            errorMessage = "Your message wasn’t sent. \(error.localizedDescription)"
        }
    }

    private func selectFormat(_ format: CreationFormat) {
        guard !isActing else { return }
        Task {
            isActing = true
            errorMessage = nil
            defer { isActing = false }
            do {
                let thread = try await model.api.applyCreationAction(
                    threadID: project.id,
                    action: "select_format",
                    payload: ["format": .string(format.serverValue)],
                    expectedRevision: threadRevision
                )
                apply(thread)
                selectedFormat = CreationFormat(thread: thread) ?? format
                isChoosingFormat = false
                try await refreshDelta()
            } catch {
                errorMessage = "That format wasn’t saved. \(error.localizedDescription)"
            }
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
            _ = try await refreshDelta()
        } catch {
            if !isUITesting {
                errorMessage = "Kria couldn’t refresh this conversation. Check your connection and try again."
            }
        }
    }

    private func apply(_ thread: CreationThread) {
        threadRevision = thread.revision
        threadState = thread.state ?? [:]
        selectedFormat = CreationFormat(thread: thread) ?? selectedFormat
        model.updateProject(thread.summary)
    }

    @discardableResult private func refreshDelta() async throws -> Bool {
        let delta = try await model.api.threadDelta(threadID: project.id, afterSequence: afterSequence)
        threadRevision = delta.threadRevision
        let known = Set(events.map(\.id))
        let fresh = delta.events.filter { !known.contains($0.id) }
        events.append(contentsOf: fresh)
        events.sort { $0.sequence < $1.sequence }
        afterSequence = delta.nextAfterSequence
        for event in fresh {
            if let format = CreationFormat(event: event) { selectedFormat = format }
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
            approval = try await model.api.approval(threadID: project.id, approvalID: identifier)
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
                self.approval = nil
                isThinking = decision == "approve"
                try await refreshDelta()
            } catch {
                errorMessage = "Kria couldn’t record that decision. \(error.localizedDescription)"
            }
        }
    }
}

enum WorkspaceStage { case format, footage, direction, rendering, ready, failed }
enum ChatMessageRole: Equatable { case user, assistant }

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
            "voiceover_prompt", "revision_queued", "status_update",
            "assistant_question", "assistant_response", "assistant_strategy",
            "assistant_review", "assistant_error", "assistant_render_failed", "memory_updated",
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

    private init?(serverValue: String) {
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
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            ScrollView { FootagePickerView(projectID: projectID).padding(20) }
                .background(KriaColor.paper)
                .navigationTitle("Add footage")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
                }
        }
    }
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

private extension String {
    var normalizedChatText: String { split(whereSeparator: \.isWhitespace).joined(separator: " ").lowercased() }
}
