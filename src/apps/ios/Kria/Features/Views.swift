import SwiftUI
import AuthenticationServices
import PhotosUI
import AVKit

struct SignInView: View {
    @EnvironmentObject private var auth: AuthStore
    @EnvironmentObject private var model: AppModel
    @State private var message: String?
    @State private var appleNonce = UUID().uuidString
    var body: some View {
        VStack(alignment: .leading, spacing: 28) {
            Spacer()
            Text("Make something\nworth sharing.").font(KriaFont.display(42)).foregroundStyle(KriaColor.ink)
            Text("Kria turns the footage in your camera roll into a considered short-form cut.").font(KriaFont.body(17)).foregroundStyle(KriaColor.zinc).fixedSize(horizontal: false, vertical: true)
            Spacer()
            SignInWithAppleButton(.signIn, onRequest: handleAppleRequest, onCompletion: handleApple)
                .signInWithAppleButtonStyle(.black).frame(height: 52).clipShape(Capsule()).accessibilityLabel("Sign in with Apple")
            Button("Continue with Google") { Task { await signInWithGoogle() } }.buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity)
            #if DEBUG
            Button("Continue with local account") {
                do { try auth.signIn(with: MobileSession(accessToken: "local-access", refreshToken: "local-refresh", expiresIn: 3600), displayName: "Local creator") }
                catch { message = error.localizedDescription }
            }.buttonStyle(KriaSecondaryButtonStyle()).frame(maxWidth: .infinity)
            #endif
            if let message { Text(message).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc) }
            Text("By signing in, you agree to Kria’s Terms and Privacy Policy.").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
        }
        .padding(24).frame(maxWidth: 520).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
    private func handleAppleRequest(_ request: ASAuthorizationAppleIDRequest) {
        appleNonce = UUID().uuidString
        request.requestedScopes = [.fullName, .email]
        request.nonce = appleNonce.sha256Hex
    }
    private func signInWithGoogle() async {
        do {
            let credential = try await GoogleAuthProvider().signIn()
            let session = try await model.api.exchangeMobileToken(credential, provider: "google")
            try auth.signIn(with: session, displayName: credential.displayName)
        } catch { message = error.localizedDescription }
    }
    private func handleApple(_ result: Result<ASAuthorization, any Error>) {
        switch result {
        case .success(let authorization):
            Task { @MainActor in
                do {
                    let credential = try AppleAuthProvider().credential(from: authorization, nonce: appleNonce)
                    let session = try await model.api.exchangeMobileToken(credential, provider: "apple")
                    try auth.signIn(with: session, displayName: credential.displayName)
                } catch { message = error.localizedDescription }
            }
        case .failure: message = AuthError.cancelled.localizedDescription
        }
    }
}

struct OnboardingView: View {
    @EnvironmentObject private var model: AppModel
    @State private var page = 0
    private let pages = [("Your footage, edited with intent.", "Tell Kria what you want to feel. It will find the story in your clips."), ("Keep the final say.", "Kria prepares a cut and shows you what changed before anything renders."), ("Pick up where you left off.", "Your projects, conversations, and finished videos stay connected to your Kria account.")]
    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            Spacer()
            Text(pages[page].0).font(KriaFont.display(38))
            Text(pages[page].1).font(KriaFont.body(18)).foregroundStyle(KriaColor.zinc)
            HStack(spacing: 8) { ForEach(pages.indices, id: \.self) { index in Capsule().fill(index == page ? KriaColor.ink : KriaColor.line).frame(width: index == page ? 28 : 8, height: 6) } }
            Spacer()
            Button(page == pages.count - 1 ? "Start creating" : "Continue") { if page == pages.count - 1 { model.completeOnboarding() } else { withAnimation { page += 1 } } }.buttonStyle(KriaPrimaryButtonStyle())
        }.padding(24).frame(maxWidth: 560).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
    }
}

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
                HStack(alignment: .firstTextBaseline) { Text("Projects").font(KriaFont.display(34)); Spacer(); Button(action: { Task { await model.createProject() } }) { Image(systemName: "plus").font(.headline).frame(width: 44, height: 44).background(KriaColor.lime).clipShape(Circle()) }.accessibilityLabel("New project") }
                Text("A home for the stories you’re shaping.").foregroundStyle(KriaColor.zinc)
                if model.projects.isEmpty {
                    switch model.projectsState {
                    case .idle, .loading:
                        VStack(alignment: .leading, spacing: 12) {
                            ProgressView().tint(KriaColor.limeText)
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
                        .overlay(Image(systemName: "play.fill").font(.largeTitle).foregroundStyle(KriaColor.lime))
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
                    if let image = phase.image {
                        image.resizable().scaledToFill()
                    } else if phase.error == nil {
                        ZStack { KriaColor.softZinc; ProgressView().tint(KriaColor.limeText) }
                    } else {
                        fallback
                    }
                }
            } else {
                fallback
            }
        }
        .clipped()
        .accessibilityLabel("Poster for \(project.workspaceTitle)")
    }

    private var fallback: some View {
        BundledPosterImage(name: project.status == .ready ? "montage" : "voiceover")
            .scaledToFill()
    }
}

struct GalleryView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var auth: AuthStore
    @Environment(\.dismiss) private var dismiss
    @State private var filter: GalleryFilter = .all

    private var projects: [ProjectSummary] {
        switch filter {
        case .all: model.libraryProjects
        case .ready: model.libraryProjects.filter { $0.status == .ready }
        case .inProgress: model.libraryProjects.filter { $0.status == .draft || $0.status == .rendering }
        }
    }

    private var initial: String {
        String((auth.displayName?.trimmingCharacters(in: .whitespacesAndNewlines).first ?? "E")).uppercased()
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Text("Gallery")
                    .font(KriaFont.body(14).weight(.semibold))

                HStack {
                    Button("Projects") { dismiss() }
                        .font(KriaFont.body(14).weight(.medium))
                        .frame(minWidth: 68, minHeight: 44, alignment: .leading)
                    Spacer()
                    Text(initial)
                        .font(KriaFont.body(13).weight(.semibold))
                        .frame(width: 32, height: 32)
                        .background(KriaColor.softZinc)
                        .clipShape(Circle())
                }
                .padding(.horizontal, 16)
            }
            .frame(height: 54)

            Rectangle().fill(KriaColor.line).frame(height: 1)

            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("Your gallery")
                            .font(KriaFont.display(29))
                        Text("Finished cuts and the stories still taking shape.")
                            .font(KriaFont.body(14))
                            .foregroundStyle(KriaColor.zinc)
                    }

                    HStack(spacing: 8) {
                        ForEach(GalleryFilter.allCases) { option in
                            Button(option.title) { filter = option }
                                .font(KriaFont.body(12).weight(.medium))
                                .foregroundStyle(filter == option ? Color.white : KriaColor.ink)
                                .padding(.horizontal, 12)
                                .frame(minHeight: 44)
                                .background(filter == option ? KriaColor.ink : Color.white)
                                .overlay(Capsule().stroke(KriaColor.border, lineWidth: filter == option ? 0 : 1))
                                .clipShape(Capsule())
                        }
                    }

                    if projects.isEmpty {
                        switch model.libraryState {
                        case .idle, .loading:
                            VStack(alignment: .leading, spacing: 10) {
                                ProgressView().tint(KriaColor.limeText)
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
                        LazyVGrid(columns: [GridItem(.flexible(), spacing: 12), GridItem(.flexible())], spacing: 20) {
                            ForEach(projects) { project in
                                if project.status == .ready {
                                    NavigationLink(destination: ResultsView(project: project, libraryJobID: project.id)) {
                                        GalleryProjectCard(project: project)
                                    }
                                    .buttonStyle(.plain)
                                    .accessibilityLabel("Play \(project.workspaceTitle)")
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
                .padding(.top, 42)
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
            Group {
                if let posterURL = project.posterURL {
                    AsyncImage(url: posterURL) { phase in
                        if let image = phase.image { image.resizable().scaledToFill() }
                        else { BundledPosterImage(name: "montage").scaledToFill() }
                    }
                } else {
                    BundledPosterImage(name: project.status == .ready ? "montage" : "voiceover")
                        .scaledToFill()
                }
            }
            .frame(maxWidth: .infinity)
            .aspectRatio(0.75, contentMode: .fit)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(alignment: .topTrailing) {
                Text(project.workspaceStatusLabel.uppercased())
                    .font(KriaFont.body(8).weight(.bold))
                    .tracking(0.8)
                    .foregroundStyle(project.status == .ready ? KriaColor.limeText : KriaColor.ink)
                    .padding(.horizontal, 7)
                    .padding(.vertical, 5)
                    .background(project.status == .ready ? KriaColor.limeSoft : Color.white.opacity(0.9))
                    .clipShape(Capsule())
                    .padding(7)
            }

            Text(project.workspaceTitle)
                .font(KriaFont.body(13).weight(.semibold))
                .lineLimit(1)
            Text(project.updatedAt, style: .relative)
                .font(KriaFont.body(11))
                .foregroundStyle(KriaColor.zinc)
        }
    }
}

struct AccountView: View {
    @EnvironmentObject private var auth: AuthStore
    var body: some View { List { Section { Label(auth.displayName ?? "Creator", systemImage: "person.crop.circle"); Label("Kria app · Development", systemImage: "gear")
        #if DEBUG
        NavigationLink("Media diagnostics", destination: MediaDiagnosticView())
        #endif
    }; Section { Button("Sign out", role: .destructive) { auth.signOut() } } }.scrollContentBackground(.hidden).background(KriaColor.paper).navigationTitle("Account") }
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
                FootagePickerView(projectID: project.id)
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

struct CloudUploadConsentView: View {
    let onConsent: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var consent = false
    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 22) {
                Text("Upload your originals.").font(KriaFont.display(30))
                Text("Kria will upload the full-quality originals you select and keep them with this project so it can render in the cloud. Delete the project to remove its uploaded footage. You can cancel while an upload is in progress.").foregroundStyle(KriaColor.zinc)
                Toggle("I consent to Kria using these originals for this edit", isOn: $consent).tint(KriaColor.limeText)
                Spacer()
                Button("Continue") { dismiss(); onConsent() }.buttonStyle(KriaPrimaryButtonStyle()).disabled(!consent)
            }
            .padding(24)
            .navigationTitle("Upload consent")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}
