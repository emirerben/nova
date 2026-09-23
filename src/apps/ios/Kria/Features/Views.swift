import SwiftUI
import AuthenticationServices
import PhotosUI
import AVKit

struct MainShellView: View {
    @EnvironmentObject private var model: AppModel
    @State private var tab = 0
    var body: some View {
        TabView(selection: $tab) {
            NavigationStack { ProjectsView() }.tabItem { Label("Projects", systemImage: "square.stack.3d.up") }.tag(0)
            NavigationStack { GalleryView() }.tabItem { Label("Gallery", systemImage: "play.rectangle") }.tag(1)
            NavigationStack { AccountView() }.tabItem { Label("Account", systemImage: "person") }.tag(2)
        }.task { await model.openWorkspace() }
    }
}

struct ProjectsView: View {
    @EnvironmentObject private var model: AppModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                HStack(alignment: .firstTextBaseline) { Text("Projects").font(KriaFont.display(34)); Spacer(); Button(action: { Task { await model.createProject() } }) { Image(systemName: "plus").font(.headline).frame(width: 44, height: 44).background(KriaColor.sky).clipShape(Circle()) }.accessibilityLabel("New project") }
                Text("A home for the stories you’re shaping.").foregroundStyle(KriaColor.zinc)
                if model.projects.isEmpty {
                    switch model.projectsState {
                    case .idle, .loading:
                        VStack(alignment: .leading, spacing: 12) {
                            ProgressView().tint(KriaColor.ink)
                            Text("Loading your projects…").font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                        }
                        .frame(maxWidth: .infinity, minHeight: 180, alignment: .leading)
                        .accessibilityElement(children: .combine)
                    case .failed(let message):
                        VStack(alignment: .leading, spacing: 14) {
                            Text("Projects couldn’t load.").font(KriaFont.display(24))
                            Text(message).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                            Button("Try again") { Task { await model.loadProjects() } }
                                .buttonStyle(KriaSecondaryButtonStyle())
                        }
                    case .empty, .loaded:
                        KriaEmptyState(title: "Start with a few clips.", action: "Create a project") { Task { await model.createProject() } }
                    }
                } else {
                    if let error = model.errorMessage {
                        Text(error).font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc).accessibilityAddTraits(.isStaticText)
                    }
                    ForEach(model.projects) { project in
                        Button { model.selectedProject = project } label: { ProjectRow(project: project) }
                            .buttonStyle(.plain)
                    }
                }
            }.padding(20)
        }
        .navigationTitle("").navigationDestination(item: $model.selectedProject) { project in ProjectDetailView(project: project) }
    }
}

struct ProjectRow: View {
    let project: ProjectSummary
    var body: some View {
        HStack(spacing: 14) {
            ProjectPosterView(project: project)
                .frame(width: 88, height: 112)
                .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            VStack(alignment: .leading, spacing: 8) { Text(project.title).font(KriaFont.display(21)); KriaStatusPill(status: project.status); Text(project.updatedAt, style: .relative).font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc) }
            Spacer(); Image(systemName: "chevron.right").foregroundStyle(KriaColor.zinc)
        }.padding(12).background(Color.white.opacity(0.7)).clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous)).accessibilityElement(children: .combine).accessibilityHint("Open project")
    }
}

struct ProjectDetailView: View {
    let project: ProjectSummary
    @EnvironmentObject private var model: AppModel
    @State private var showThread = false
    private var currentProject: ProjectSummary { model.projects.first(where: { $0.id == project.id }) ?? project }
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 22) {
            Text(currentProject.title).font(KriaFont.display(34)); KriaStatusPill(status: currentProject.status)
            if currentProject.status == .ready {
                NavigationLink(destination: ResultsView(project: currentProject)) {
                    ProjectPosterView(project: currentProject)
                        .aspectRatio(9/16, contentMode: .fit)
                        .frame(maxWidth: 240)
                        .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
                        .overlay(Image(systemName: "play.fill").font(.largeTitle).foregroundStyle(KriaColor.sky))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Play finished cut")
            } else {
                ProjectPosterView(project: currentProject)
                    .aspectRatio(9/16, contentMode: .fit)
                    .frame(maxWidth: 240)
                    .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
            }
            Button("Create with Kria") { showThread = true }
                .buttonStyle(KriaPrimaryButtonStyle())
            if currentProject.status == .ready { NavigationLink("View finished cut", destination: ResultsView(project: currentProject)).font(KriaFont.body(16).weight(.semibold)) }
            KriaSectionLabel(title: "Receipts")
            Text("Your changes and renders will appear here as a calm, reviewable history.").foregroundStyle(KriaColor.zinc)
        }.padding(20) }.navigationTitle("").sheet(isPresented: $showThread) { CreationThreadView(project: currentProject) }
    }
}

struct ProjectPosterView: View {
    let project: ProjectSummary

    var body: some View {
        Group {
            if let posterURL = project.posterURL {
                AsyncImage(url: posterURL) { phase in
                    ProjectPosterContent(phase: phase, title: project.workspaceTitle)
                }
            } else {
                ProjectPosterContent(phase: nil, title: project.workspaceTitle)
            }
        }
        .clipped()
    }
}

/// Only a successfully loaded poster represents the user's footage. Missing,
/// loading and failed images must never substitute an unrelated sample photo.
struct ProjectPosterContent: View {
    let phase: AsyncImagePhase?
    let title: String

    var body: some View {
        switch phase {
        case .some(.success(let image)):
            image.resizable().scaledToFill()
                .accessibilityLabel("Preview for \(title)")
                .accessibilityIdentifier("project-poster-image")
        case .some(.empty):
            ZStack {
                KriaColor.softZinc
                ProgressView().tint(KriaColor.ink)
                    .accessibilityLabel("Loading preview for \(title)")
            }
            .accessibilityIdentifier("project-poster-loading")
        default:
            ZStack {
                KriaColor.softZinc
                VStack(spacing: 8) {
                    Image(systemName: "video").font(.system(size: 24))
                    Text("Preview unavailable").font(KriaFont.body(12))
                }
                .foregroundStyle(KriaColor.zinc)
                .padding(12)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Preview unavailable for \(title)")
            .accessibilityIdentifier("project-poster-unavailable")
        }
    }
}

struct GalleryView: View {
    var openProjects: (() -> Void)? = nil
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var auth: AuthStore
    @Environment(\.dismiss) private var dismiss
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var filter: GalleryFilter = .all

    private var projects: [ProjectSummary] {
        switch filter {
        case .all: model.libraryProjects
        case .ready: model.libraryProjects.filter { $0.status == .ready }
        case .inProgress: model.libraryProjects.filter { $0.status == .draft || $0.status == .rendering }
        }
    }

    private var initial: String {
        AccountIdentity.initial(name: auth.displayName, email: auth.email)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button { if let openProjects { openProjects() } else { dismiss() } } label: {
                    KriaIcon(.menu).frame(width: 44, height: 44).background(KriaColor.menu, in: Circle())
                }.accessibilityLabel("Open projects")
                Spacer()
            }.padding(.horizontal, 16).frame(height: 56)

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("Gallery")
                            .font(KriaFont.body(29))
                        Text("Your videos and posts")
                            .font(KriaFont.body(14))
                            .foregroundStyle(KriaColor.zinc)
                    }

                        Spacer()
                        NewChatButton(compact: true, icon: .plus, afterCreate: { dismiss() })
                            .background(KriaColor.butter, in: RoundedRectangle(cornerRadius: 12))
                    }
                    HStack(spacing: 8) {
                        ForEach(GalleryFilter.allCases) { option in
                            Button(option.title) { filter = option }
                                .font(KriaFont.body(12).weight(.medium))
                                .foregroundStyle(KriaColor.ink)
                                .padding(.horizontal, 12)
                                .frame(minHeight: 44)
                                .background(filter == option ? KriaColor.selectionSoft : Color.white)
                                .overlay(Capsule().stroke(KriaColor.border, lineWidth: filter == option ? 0 : 1))
                                .clipShape(Capsule())
                        }
                    }

                    if projects.isEmpty {
                        switch model.libraryState {
                        case .idle, .loading:
                            VStack(alignment: .leading, spacing: 10) {
                                ProgressView().tint(KriaColor.ink)
                                Text("Loading your cuts…")
                                    .font(KriaFont.body(14))
                                    .foregroundStyle(KriaColor.zinc)
                            }
                            .frame(maxWidth: .infinity, minHeight: 180, alignment: .leading)
                            .accessibilityElement(children: .combine)
                        case .failed(let message):
                            VStack(alignment: .leading, spacing: 10) {
                                Text("Your gallery couldn’t load.")
                                    .font(KriaFont.display(22))
                                Text(message)
                                    .font(KriaFont.body(13))
                                    .foregroundStyle(KriaColor.zinc)
                                Button("Try again") { Task { await model.loadLibrary() } }
                                    .font(KriaFont.body(13).weight(.semibold))
                                    .frame(minHeight: 44)
                            }
                            .padding(.top, 34)
                        case .empty, .loaded:
                            VStack(alignment: .leading, spacing: 10) {
                                Text("Your next cut will live here.")
                                    .font(KriaFont.display(22))
                                Button("Refresh") { Task { await model.loadLibrary() } }
                                    .font(KriaFont.body(13).weight(.semibold))
                                    .frame(minHeight: 44)
                            }
                            .padding(.top, 34)
                        }
                    } else {
                        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 12), count: dynamicTypeSize.isAccessibilitySize ? 1 : 2), spacing: 20) {
                            ForEach(projects) { project in
                                if project.status == .ready {
                                    NavigationLink {
                                        if project.isSlidePost {
                                            SlidePostWorkspaceView(project: project)
                                        } else {
                                            ResultsView(project: project, libraryJobID: project.id)
                                        }
                                    } label: {
                                        GalleryProjectCard(project: project)
                                    }
                                    .buttonStyle(.plain)
                                    .accessibilityLabel("\(project.isSlidePost ? "Open post" : "Play") \(project.workspaceTitle)")
                                } else {
                                    Button {
                                        model.selectProject(project)
                                        dismiss()
                                    } label: {
                                        GalleryProjectCard(project: project)
                                    }
                                    .buttonStyle(.plain)
                                    .accessibilityLabel("Open \(project.workspaceTitle), \(project.workspaceStatusLabel)")
                                }
                            }
                        }
                    }
                }
                .padding(.horizontal, 16)
                .padding(.top, 20)
                .padding(.bottom, 28)
            }
        }
        .background(Color.white)
        .toolbar(.hidden, for: .navigationBar)
        .task { await model.loadLibrary() }
    }
}

private enum GalleryFilter: CaseIterable, Identifiable {
    case all, ready, inProgress
    var id: Self { self }
    var title: String {
        switch self {
        case .all: "All"
        case .ready: "Ready"
        case .inProgress: "In progress"
        }
    }
}

private struct GalleryProjectCard: View {
    let project: ProjectSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Color.clear
                .aspectRatio(174.0 / 246.0, contentMode: .fit)
                .overlay {
                    GeometryReader { geometry in
                        ProjectPosterView(project: project)
                        .frame(width: geometry.size.width, height: geometry.size.height)
                        .clipped()
                    }
                }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(alignment: .topTrailing) {
                Text(project.workspaceStatusLabel.uppercased())
                    .font(KriaFont.body(8).weight(.bold))
                    .tracking(0.8)
                    .foregroundStyle(KriaColor.ink)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 5)
                    .background(project.status == .ready ? KriaColor.sage : Color.white.opacity(0.9))
                    .clipShape(Capsule())
                    .padding(7)
            }

            Text(project.workspaceTitle)
                .font(KriaFont.body(13).weight(.semibold))
                .fixedSize(horizontal: false, vertical: true)
            if project.isSlidePost {
                Text(project.slideCount.map { "Photo & video post · \($0) slides" } ?? "Photo & video post")
                    .font(KriaFont.body(11)).foregroundStyle(KriaColor.zinc)
            }
            Text(project.updatedAt, style: .relative)
                .font(KriaFont.body(11))
                .foregroundStyle(KriaColor.zinc)
        }
    }
}

struct AccountView: View {
    @EnvironmentObject private var auth: AuthStore
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var showsDeletion = false
    @State private var showsSignOutConfirmation = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                KriaWordmark()
                Text("Account")
                    .font(KriaFont.display(30))
                    .padding(.top, 14)
                    .accessibilityAddTraits(.isHeader)
                identityBlock
                    .padding(.top, 24)
                    .padding(.bottom, 20)
                    .overlay(alignment: .bottom) { Rectangle().fill(KriaColor.line).frame(height: 1) }
                if auth.profileState == .failed {
                    profileFailureRow
                }
                AccountSection(title: "About") {
                    AccountLinkRow(title: "Privacy Policy", url: KriaLegal.privacyURL)
                        .accessibilityIdentifier("kria-privacy-link")
                    AccountLinkRow(title: "Terms of Service", url: KriaLegal.termsURL)
                        .accessibilityIdentifier("kria-terms-link")
                    AccountLinkRow(title: "Contact support", url: KriaLegal.supportURL)
                        .accessibilityIdentifier("kria-support-link")
                }
                #if DEBUG
                AccountSection(title: "Developer") {
                    NavigationLink(destination: MediaDiagnosticView()) {
                        AccountRowLabel(title: "Media diagnostics", trailingSymbol: "chevron.right")
                    }
                }
                #endif
                AccountSection(title: "Account") {
                    Button {
                        showsSignOutConfirmation = true
                    } label: {
                        Text("Sign out")
                            .font(KriaFont.body(15).weight(.semibold))
                            .foregroundStyle(KriaColor.ink)
                            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading)
                            .contentShape(Rectangle())
                    }
                    .accessibilityIdentifier("account-sign-out")
                    Button {
                        showsDeletion = true
                    } label: {
                        HStack(alignment: .top, spacing: 12) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text("Delete account")
                                    .font(KriaFont.body(15).weight(.semibold))
                                    .foregroundStyle(KriaColor.ink)
                                Text("Removes your account, projects, and uploaded media")
                                    .font(KriaFont.body(12))
                                    .foregroundStyle(KriaColor.mutedInk)
                            }
                            Spacer(minLength: 0)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 13))
                                .foregroundStyle(KriaColor.mutedInk)
                        }
                        .frame(minHeight: 44)
                        .contentShape(Rectangle())
                    }
                    .accessibilityIdentifier("account-delete")
                }
                Text("Kria · Version \(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "1.0")")
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.mutedInk)
                    .padding(.top, 40)
                    .frame(maxWidth: .infinity, alignment: .center)
            }
            .padding(24)
            .frame(maxWidth: 560, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .background(KriaColor.paper)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
        .task { await auth.refreshProfile() }
        .alert("Sign out of Kria?", isPresented: $showsSignOutConfirmation) {
            Button("Cancel", role: .cancel) {}
            Button("Sign out", role: .destructive) { auth.signOut() }
        } message: {
            Text("Your projects, creator profile, and saved renders stay on this account. You can sign back in anytime.")
        }
        .sheet(isPresented: $showsDeletion) {
            NavigationStack { AccountDeletionView(api: model.api) }
        }
    }

    private var identityBlock: some View {
        HStack(spacing: 14) {
            AccountAvatarView(
                initial: AccountIdentity.initial(name: auth.displayName, email: auth.email),
                isLoading: auth.profileState == .loading && auth.email == nil
            )
            VStack(alignment: .leading, spacing: 3) {
                if let primary = AccountIdentity.primaryLine(name: auth.displayName, email: auth.email) {
                    Text(primary).font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
                } else {
                    Text("Fetching your account…").font(KriaFont.body(15)).foregroundStyle(KriaColor.mutedInk)
                }
                if let detail = AccountIdentity.detailLine(name: auth.displayName, email: auth.email, providers: auth.linkedProviders) {
                    Text(detail).font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var profileFailureRow: some View {
        HStack {
            Text("Couldn’t refresh account details").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
            Spacer()
            Button("Retry") { Task { await auth.refreshProfile() } }
                .font(KriaFont.body(13).weight(.semibold))
                .foregroundStyle(KriaColor.ink)
        }
        .padding(.vertical, 14)
        .overlay(alignment: .bottom) { Rectangle().fill(KriaColor.line).frame(height: 1) }
    }
}

/// A labelled group of rows with a `KriaSectionLabel` header. Rows within a
/// group are separated by whitespace only — the app has no `Divider()`
/// anywhere, and a hairline per row read as noisier than the house style.
private struct AccountSection<Content: View>: View {
    let title: String
    @ViewBuilder var content: () -> Content
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            KriaSectionLabel(title: title)
                .padding(.top, 20)
                .padding(.bottom, 4)
            content()
        }
    }
}

private struct AccountLinkRow: View {
    let title: String
    let url: URL
    var body: some View {
        Link(destination: url) {
            AccountRowLabel(title: title, trailingSymbol: "arrow.up.right")
        }
    }
}

private struct AccountRowLabel: View {
    let title: String
    let trailingSymbol: String
    var body: some View {
        HStack {
            Text(title).font(KriaFont.body(15)).foregroundStyle(KriaColor.ink)
            Spacer()
            Image(systemName: trailingSymbol).font(.system(size: 13)).foregroundStyle(KriaColor.mutedInk)
        }
        .frame(minHeight: 44)
        .contentShape(Rectangle())
    }
}

private struct AccountAvatarView: View {
    let initial: String
    let isLoading: Bool
    var body: some View {
        ZStack {
            Circle().fill(isLoading ? KriaColor.line : KriaColor.butter)
            if !isLoading {
                Text(initial).font(KriaFont.body(17).weight(.semibold)).foregroundStyle(KriaColor.ink)
            }
        }
        .frame(width: 44, height: 44)
        .accessibilityHidden(true)
    }
}

struct CreationThreadView: View {
    let project: ProjectSummary
    @EnvironmentObject private var model: AppModel
    @State private var prompt = ""
    @State private var events: [ThreadEvent] = []
    @State private var approval: ApprovalSnapshot?
    @State private var afterSequence = -1
    @State private var threadRevision: Int
    @State private var isSending = false
    @State private var errorMessage: String?
    init(project: ProjectSummary) {
        self.project = project
        _threadRevision = State(initialValue: project.serverRevision)
    }
    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        if events.isEmpty { Text("What should this cut make someone feel?").font(KriaFont.display(24)) }
                        ForEach(events.compactMap(ChatTranscriptMessage.from(event:))) { message in
                            ChatMessageRow(message: message)
                        }
                        if let approval { ApprovalCard(approval: approval, decide: decide) }
                        if let errorMessage { Text(errorMessage).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }
                HStack {
                    TextField("Tell Kria what to emphasize", text: $prompt, axis: .vertical).textFieldStyle(.roundedBorder).font(KriaFont.body(16))
                    Button("Send") { Task { await send() } }.buttonStyle(KriaPrimaryButtonStyle()).disabled(prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || isSending)
                }
                FootagePickerView(projectID: project.id, uploads: model.uploads)
            }
            .padding(20)
            .navigationTitle("Create with Kria")
            .task { await pollUntilDismissed() }
        }
    }
    private func send() async {
        guard !isSending else { return }
        let message = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !message.isEmpty else { return }
        isSending = true
        defer { isSending = false }
        do {
            let accepted = try await model.api.submitTurn(threadID: project.id, message: message, expectedRevision: threadRevision)
            threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: accepted.threadRevision)
            prompt = ""
            try await refreshDelta()
        } catch { errorMessage = error.localizedDescription }
    }
    private func pollUntilDismissed() async {
        var delay: UInt64 = 1_000_000_000
        while !Task.isCancelled {
            do {
                let hadNewEvents = try await refreshDelta()
                delay = hadNewEvents ? 1_000_000_000 : min(delay * 2, 8_000_000_000)
            } catch {
                errorMessage = error.localizedDescription
                delay = min(delay * 2, 15_000_000_000)
            }
            try? await Task.sleep(nanoseconds: delay)
        }
    }
    @discardableResult private func refreshDelta() async throws -> Bool {
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
        if let current = try? await model.api.project(threadID: project.id) {
            if ThreadRevisionOrder.acceptsProjection(current: threadRevision, incoming: current.revision) {
                threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: current.revision)
                model.updateProject(current.summary)
            }
        }
        if let approvalEvent = fresh.last(where: { $0.eventType == "approval_requested" }),
           let identifier = approvalEvent.payload?["approval_id"]?.stringValue.flatMap(UUID.init(uuidString:)) {
            approval = try await model.api.approval(threadID: project.id, approvalID: identifier)
        }
        if fresh.contains(where: { $0.eventType == "approval_approved" || $0.eventType == "approval_denied" }) { approval = nil }
        return !fresh.isEmpty
    }
    private func decide(_ decision: String) {
        guard let approval, let identifier = UUID(uuidString: approval.approvalID), let draftRevision = approval.draftRevision else { return }
        Task {
            do {
                try await model.api.decideApproval(threadID: project.id, approvalID: identifier, decision: decision, expectedThreadRevision: threadRevision, expectedDraftRevision: draftRevision, fingerprint: approval.approvalFingerprint)
                self.approval = nil
                try await refreshDelta()
            } catch { errorMessage = error.localizedDescription }
        }
    }
}

private struct ApprovalCard: View {
    let approval: ApprovalSnapshot
    let decide: (String) -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            KriaSectionLabel(title: "Ready for your approval")
            Text(approval.consequenceSummary).font(KriaFont.body(16))
            if let cost = approval.costSummary { Text(cost).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }
            HStack {
                Button("Use this plan") { decide("approve") }.buttonStyle(KriaPrimaryButtonStyle())
                Button("Not yet") { decide("deny") }.buttonStyle(KriaSecondaryButtonStyle())
            }
        }.padding(16).background(Color.white.opacity(0.8)).clipShape(RoundedRectangle(cornerRadius: 18))
    }
}
