import Foundation
import Combine
import SwiftData

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
    }
    func completeOnboarding() { hasCompletedOnboarding = true; UserDefaults.standard.set(true, forKey: "kria.onboarding.complete") }
    func loadProjects() async {
        if let cache, let cached = try? cache.projects(), !cached.isEmpty {
            projects = cached.map(\.summary)
        }
        isLoading = true
        defer { isLoading = false }
        do {
            projects = try await api.projects()
            try? cache?.upsert(projects)
        }
        catch {
            #if DEBUG
            if projects.isEmpty { projects = PreviewFixtures.projects }
            #else
            errorMessage = APIError.requestFailed.localizedDescription
            #endif
        }
    }
    func loadLibrary() async {
        do { libraryProjects = try await api.library() }
        catch {
            #if DEBUG
            libraryProjects = PreviewFixtures.projects.filter { $0.status == .ready }
            #else
            errorMessage = error.localizedDescription
            #endif
        }
    }
    func createProject() async {
        do {
            let thread = try await api.createThread(message: nil)
            let project = thread.summary
            projects.insert(project, at: 0)
            try? cache?.upsert([project])
            selectedProject = project
        } catch {
            #if DEBUG
            let project = ProjectSummary(id: UUID(), title: "Untitled project", status: .draft, updatedAt: .now, posterURL: nil)
            projects.insert(project, at: 0); selectedProject = project
            #else
            errorMessage = error.localizedDescription
            #endif
        }
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
        ProjectSummary(id: projectID, title: "A quiet Sunday", status: .ready, updatedAt: Date(timeIntervalSince1970: 1_756_000_000), posterURL: nil),
        ProjectSummary(id: UUID(), title: "Matcha launch", status: .rendering, updatedAt: .now.addingTimeInterval(-3600), posterURL: nil)
    ]
    static let draft = EditorDraft(projectID: projectID, clips: [EditorClip(id: UUID(), assetID: UUID(), start: 0, end: 4.2, trimIn: 0, trimOut: 4.2), EditorClip(id: UUID(), assetID: UUID(), start: 4.2, end: 9.8, trimIn: 0, trimOut: 5.6)], text: [], captions: CaptionStyle(enabled: false, style: "clean"), music: nil, revision: 0)
}
