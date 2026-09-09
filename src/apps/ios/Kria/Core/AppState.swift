import Foundation
import Combine
import SwiftData

enum ProjectCollectionState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case empty
    case failed(String)
}

@MainActor final class AuthStore: ObservableObject {
    private static let invalidatedSessionKey = "kria.mobile-session.invalidated"
    @Published private(set) var isSignedIn = false
    @Published private(set) var displayName: String?
    private let tokenStore: TokenStore
    private let api: KriaAPIClient
    private var sessionExpiredSubscription: AnyCancellable?
    init(tokenStore: TokenStore = KeychainTokenStore()) {
        self.tokenStore = tokenStore
        self.api = KriaAPI(tokenStore: tokenStore)
        isSignedIn = !UserDefaults.standard.bool(forKey: Self.invalidatedSessionKey) && (try? tokenStore.read()) != nil
        sessionExpiredSubscription = NotificationCenter.default.publisher(for: .kriaSessionExpired)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.expireSession() }
    }
    func signIn(with session: MobileSession, displayName: String?) throws {
        try tokenStore.write(session)
        UserDefaults.standard.set(false, forKey: Self.invalidatedSessionKey)
        self.displayName = displayName
        isSignedIn = true
    }
    func signOut() {
        let refreshToken = try? tokenStore.read()?.refreshToken
        // Persist the logical logout before touching Keychain. If secure-item
        // deletion fails, a process restart must not resurrect that session.
        UserDefaults.standard.set(true, forKey: Self.invalidatedSessionKey)
        try? tokenStore.delete()
        displayName = nil
        isSignedIn = false
        if let refreshToken {
            Task { [api] in try? await api.revokeMobileSession(refreshToken) }
        }
    }
    private func expireSession() {
        UserDefaults.standard.set(true, forKey: Self.invalidatedSessionKey)
        try? tokenStore.delete()
        displayName = nil
        isSignedIn = false
    }
}

@MainActor final class AppModel: ObservableObject {
    @Published var projects: [ProjectSummary] = []
    @Published var libraryProjects: [ProjectSummary] = []
    @Published var selectedProject: ProjectSummary?
    @Published var hasCompletedOnboarding: Bool
    @Published var isLoading = false
    @Published var errorMessage: String?
    @Published private(set) var projectsState: ProjectCollectionState = .idle
    @Published private(set) var libraryState: ProjectCollectionState = .idle
    let api: KriaAPIClient
    let editorOperations: EditorOperations
    let uploads: BackgroundUploadCoordinator
    private let cache: CacheRepository?
    init(
        api: KriaAPIClient = KriaAPI(),
        editorOperations: EditorOperations = LocalEditorOperations(),
        cache: CacheRepository? = nil
    ) {
        self.api = api
        self.editorOperations = editorOperations
        self.uploads = BackgroundUploadCoordinator(api: api)
        self.cache = cache
        hasCompletedOnboarding = UserDefaults.standard.bool(forKey: "kria.onboarding.complete")
        if let cache, let cached = try? cache.projects(), !cached.isEmpty {
            projects = cached.map(\.summary)
            projectsState = .loaded
        }
    }
    func completeOnboarding() { hasCompletedOnboarding = true; UserDefaults.standard.set(true, forKey: "kria.onboarding.complete") }
    func loadProjects() async {
        if let cache, let cached = try? cache.projects(), !cached.isEmpty {
            projects = cached.map(\.summary)
        }
        isLoading = true
        if projects.isEmpty { projectsState = .loading }
        defer { isLoading = false }
        do {
            projects = try await api.projects()
            projectsState = projects.isEmpty ? .empty : .loaded
            errorMessage = nil
            if let selectedProject,
               let refreshed = projects.first(where: { $0.id == selectedProject.id }) {
                self.selectedProject = refreshed
            }
            try? cache?.upsert(projects)
        }
        catch {
            #if DEBUG
            if projects.isEmpty {
                projects = PreviewFixtures.projects
                projectsState = .loaded
            }
            #else
            let message = APIError.requestFailed.localizedDescription
            errorMessage = message
            projectsState = projects.isEmpty ? .failed(message) : .loaded
            #endif
        }
    }
    func loadLibrary() async {
        if libraryProjects.isEmpty { libraryState = .loading }
        do {
            libraryProjects = try await api.library()
            libraryState = libraryProjects.isEmpty ? .empty : .loaded
            errorMessage = nil
        }
        catch {
            #if DEBUG
            libraryProjects = PreviewFixtures.projects.filter { $0.status == .ready }
            libraryState = libraryProjects.isEmpty ? .empty : .loaded
            #else
            errorMessage = error.localizedDescription
            libraryState = libraryProjects.isEmpty ? .failed(error.localizedDescription) : .loaded
            #endif
        }
    }
    func createProject() async {
        errorMessage = nil
        do {
            let thread = try await api.createThread(message: nil)
            let project = thread.summary
            projects.insert(project, at: 0)
            projectsState = .loaded
            try? cache?.upsert([project])
            selectedProject = project
        } catch {
            #if DEBUG
            let project = ProjectSummary(id: UUID(), title: "Untitled project", status: .draft, updatedAt: .now, posterURL: nil)
            projects.insert(project, at: 0); selectedProject = project; projectsState = .loaded
            #else
            errorMessage = error.localizedDescription
            #endif
        }
    }
    func openWorkspace(preferredProjectID: UUID? = nil) async {
        selectWorkspaceProject(preferredProjectID: preferredProjectID)
        async let uploadRecovery: Void = uploads.restorePendingTasks()
        await loadProjects()
        selectWorkspaceProject(preferredProjectID: preferredProjectID)
        await uploadRecovery
    }
    private func selectWorkspaceProject(preferredProjectID: UUID?) {
        if let preferredProjectID,
           let preferred = projects.first(where: { $0.id == preferredProjectID }) {
            selectedProject = preferred
        } else if let selectedProject,
                  let refreshed = projects.first(where: { $0.id == selectedProject.id }) {
            self.selectedProject = refreshed
        } else if let newest = projects.first {
            selectedProject = newest
        }
    }
    func selectProject(_ project: ProjectSummary) {
        selectedProject = project
        errorMessage = nil
    }
    func updateProject(_ project: ProjectSummary) {
        if let index = projects.firstIndex(where: { $0.id == project.id }) {
            projects[index] = project
        } else {
            projects.insert(project, at: 0)
        }
        try? cache?.upsert([project])
    }
}

enum PreviewFixtures {
    static let projectID = UUID(uuidString: "B8D594F1-5D75-4C52-BF94-9EA05B9C0D9B")!
    static let projects = [
        ProjectSummary(id: projectID, title: "Untitled project", status: .draft, updatedAt: Date(timeIntervalSince1970: 1_756_000_000), posterURL: nil),
        ProjectSummary(id: UUID(), title: "A quiet Sunday", status: .ready, updatedAt: .now.addingTimeInterval(-1800), posterURL: nil),
        ProjectSummary(id: UUID(), title: "Matcha launch", status: .rendering, updatedAt: .now.addingTimeInterval(-3600), posterURL: nil)
    ]
    static let editorProject = ProjectSummary(id: projectID, title: "Sunday reset", status: .ready, updatedAt: .now, posterURL: nil)
    static let editorDraft = EditorDraft(
        projectID: projectID,
        clips: [
            EditorClip(id: UUID(uuidString: "56B34C9B-295A-4C4F-9E87-A88739FD6ED0")!, assetID: UUID(uuidString: "1F489F59-D2D2-4521-824A-E95832333119")!, sourceClipIndex: 0, start: 0, end: 3.6, trimIn: 0.4, trimOut: 4, sourceDuration: 7.2, slotID: "opening"),
            EditorClip(id: UUID(uuidString: "2FD4D14D-E2D7-4AB8-A6FE-98382C8D48A4")!, assetID: UUID(uuidString: "7B698578-A697-4925-90F5-3AF9303568F3")!, sourceClipIndex: 1, start: 3.6, end: 7.8, trimIn: 1.1, trimOut: 5.3, sourceDuration: 8.5, slotID: "middle"),
            EditorClip(id: UUID(uuidString: "4C07E934-EFDA-4067-A120-679DA7D8F8D2")!, assetID: UUID(uuidString: "F9E1261F-3F53-47A6-B488-76268B26C739")!, sourceClipIndex: 2, start: 7.8, end: 11.4, trimIn: 0, trimOut: 3.6, sourceDuration: 5.4, slotID: "close"),
        ],
        text: [TextLayer(id: UUID(uuidString: "47D01220-F257-4ED9-A73A-00D8077B8B0B")!, content: "Slow mornings", position: CGPoint(x: 0.5, y: 0.28), style: "Fraunces")],
        captions: CaptionStyle(enabled: true, style: "sentence"),
        music: MusicSelection(trackID: UUID(uuidString: "E228BD50-10F2-4D48-BD42-E05D447BA632")!, title: "Soft focus", start: 0, volume: 0.72),
        revision: 3
    )
    static let draft = EditorDraft(projectID: projectID, clips: [EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: 4.2, trimIn: 0, trimOut: 4.2), EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 1, start: 4.2, end: 9.8, trimIn: 0, trimOut: 5.6)], text: [], captions: CaptionStyle(enabled: false, style: "sentence"), music: nil, revision: 0)
}
