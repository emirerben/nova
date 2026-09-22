import Foundation
import Combine
import SwiftData
import CryptoKit

enum ProjectCollectionState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case empty
    case failed(String)
}

enum ProfileFetchState: Equatable, Sendable {
    case idle
    case loading
    case loaded
    case failed
}

@MainActor final class AuthStore: ObservableObject {
    private static let invalidatedSessionKey = "kria.mobile-session.invalidated"
    private static let aiConsentVersion = "current1"
    @Published private(set) var isSignedIn = false
    @Published private(set) var displayName: String?
    // email/linkedProviders/profileState are in-memory only — refetched from
    // GET /auth/mobile/me on sign-in and on each AccountView appearance, never
    // written to the Keychain (avoids persisting PII at rest / migrating
    // already-stored sessions).
    @Published private(set) var email: String?
    @Published private(set) var linkedProviders: [String] = []
    @Published private(set) var profileState: ProfileFetchState = .idle
    @Published private(set) var hasAIConsent = false
    private let tokenStore: TokenStore
    private let api: KriaAPIClient
    private let defaults: UserDefaults
    private var sessionExpiredSubscription: AnyCancellable?
    init(tokenStore: TokenStore = KeychainTokenStore(), api: (any KriaAPIClient)? = nil, defaults: UserDefaults = .standard) {
        self.tokenStore = tokenStore
        self.api = api ?? KriaAPI(tokenStore: tokenStore)
        self.defaults = defaults
        let session = try? tokenStore.read()
        isSignedIn = !defaults.bool(forKey: Self.invalidatedSessionKey) && session != nil
        if isSignedIn, let session { hasAIConsent = defaults.bool(forKey: Self.aiConsentKey(for: session)) }
        sessionExpiredSubscription = NotificationCenter.default.publisher(for: .kriaSessionExpired)
            .receive(on: RunLoop.main)
            .sink { [weak self] _ in self?.expireSession() }
        if isSignedIn {
            Task { [weak self] in await self?.refreshProfile() }
        }
    }
    func acceptAIConsent() {
        guard isSignedIn, let session = try? tokenStore.read() else { return }
        defaults.set(true, forKey: Self.aiConsentKey(for: session))
        hasAIConsent = true
    }
    func signIn(with session: MobileSession, displayName: String?) throws {
        try tokenStore.write(session)
        defaults.set(false, forKey: Self.invalidatedSessionKey)
        self.displayName = displayName
        isSignedIn = true
        hasAIConsent = defaults.bool(forKey: Self.aiConsentKey(for: session))
        Task { [weak self] in await self?.refreshProfile() }
    }
    func signOut() {
        let refreshToken = try? tokenStore.read()?.refreshToken
        // Persist the logical logout before touching Keychain. If secure-item
        // deletion fails, a process restart must not resurrect that session.
        defaults.set(true, forKey: Self.invalidatedSessionKey)
        try? tokenStore.delete()
        displayName = nil
        email = nil
        linkedProviders = []
        profileState = .idle
        isSignedIn = false
        if let refreshToken {
            Task { [api] in try? await api.revokeMobileSession(refreshToken) }
        }
    }
    private func expireSession() {
        defaults.set(true, forKey: Self.invalidatedSessionKey)
        try? tokenStore.delete()
        displayName = nil
        email = nil
        linkedProviders = []
        profileState = .idle
        isSignedIn = false
    }
    /// Fail-open by design: a network error must never sign the user out or
    /// clear a value already on screen. AccountView retries by calling this
    /// again (e.g. its "Retry" row, or re-appearing).
    func refreshProfile() async {
        guard isSignedIn else { return }
        profileState = .loading
        do {
            let user = try await api.currentUser()
            email = user.email
            linkedProviders = user.linkedProviders
            if let name = user.name, !name.isEmpty { displayName = name }
            profileState = .loaded
        } catch {
            profileState = .failed
        }
    }

    private static func aiConsentKey(for session: MobileSession) -> String {
        let identity = jwtSubject(in: session.accessToken) ?? "refresh:\(session.refreshToken)"
        let digest = SHA256.hash(data: Data(identity.utf8)).map { String(format: "%02x", $0) }.joined()
        return "kria.ai-consent.version/\(aiConsentVersion).\(digest)"
    }

    private static func jwtSubject(in token: String) -> String? {
        let parts = token.split(separator: ".")
        guard parts.count >= 2 else { return nil }
        var value = String(parts[1]).replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        value += String(repeating: "=", count: (4 - value.count % 4) % 4)
        guard let data = Data(base64Encoded: value),
              let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return ["sub", "user_id", "email"].compactMap { payload[$0] as? String }.first
    }
}

/// Pure text rules for the Account identity block (KRI-113). Kept out of the
/// view so the two-line collapse and the Apple-relay caption are each one
/// unit-testable function, not per-view string-building.
enum AccountIdentity {
    static let privateRelaySuffix = "@privaterelay.appleid.com"

    static func isPrivateRelay(email: String) -> Bool {
        email.lowercased().hasSuffix(privateRelaySuffix)
    }

    /// Line 1: the name if we have one, else the email.
    static func primaryLine(name: String?, email: String?) -> String? {
        let trimmedName = name?.trimmingCharacters(in: .whitespacesAndNewlines)
        if let trimmedName, !trimmedName.isEmpty { return trimmedName }
        return email
    }

    /// Line 2: combines email + provider when a name is known ("email · Google");
    /// otherwise line 1 already holds the email, so this becomes the provider
    /// line, with an explicit "hidden email" note for an Apple private-relay
    /// address so it doesn't read as a bug.
    static func detailLine(name: String?, email: String?, providers: [String]) -> String? {
        let trimmedName = name?.trimmingCharacters(in: .whitespacesAndNewlines)
        let provider = providers.first?.capitalized
        let signedInWith = provider.map { "Signed in with \($0)" }
        if let trimmedName, !trimmedName.isEmpty {
            guard let email, !email.isEmpty else { return signedInWith }
            guard let provider else { return email }
            return "\(email) · \(provider)"
        }
        guard let signedInWith else { return nil }
        if let email, isPrivateRelay(email: email) { return "\(signedInWith) · hidden email" }
        return signedInWith
    }

    /// Avatar initial: first character of the name, else the email, else "?".
    static func initial(name: String?, email: String?) -> String {
        let trimmedName = name?.trimmingCharacters(in: .whitespacesAndNewlines)
        let source = (trimmedName?.isEmpty == false ? trimmedName : nil) ?? email
        guard let first = source?.first else { return "?" }
        return String(first).uppercased()
    }
}

@MainActor final class AppModel: ObservableObject {
    @Published var projects: [ProjectSummary] = []
    @Published var libraryProjects: [ProjectSummary] = []
    @Published var selectedProject: ProjectSummary?
    @Published var hasCompletedOnboarding: Bool
    @Published private(set) var isCreatingProject = false
    @Published var isLoading = false
    @Published var errorMessage: String?
    @Published private(set) var projectsState: ProjectCollectionState = .idle
    @Published private(set) var libraryState: ProjectCollectionState = .idle
    let api: KriaAPIClient
    let editorOperations: EditorOperations
    let uploads: BackgroundUploadCoordinator
    let deviceRenders: DeviceRenderSessions
    private let cache: CacheRepository?
    private var deletedProjectIDs: Set<UUID> = []
    private var reapingChatIDs: Set<UUID> = []
    let chatDrafts: ChatDraftStore
    private var collectionGeneration = 0
    init(
        api: KriaAPIClient = KriaAPI(),
        editorOperations: EditorOperations = LocalEditorOperations(),
        cache: CacheRepository? = nil,
        chatDrafts: ChatDraftStore = ChatDraftStore()
    ) {
        self.chatDrafts = chatDrafts
        self.api = api
        self.editorOperations = editorOperations
        self.uploads = BackgroundUploadCoordinator(api: api)
        self.deviceRenders = DeviceRenderSessions(api: api)
        self.cache = cache
        hasCompletedOnboarding = UserDefaults.standard.bool(forKey: "kria.onboarding.complete")
        if let cache, let cached = try? cache.projects(), !cached.isEmpty {
            projects = cached.map(\.summary).filter { !deletedProjectIDs.contains($0.id) }
            projectsState = .loaded
        }
    }
    func completeOnboarding() { hasCompletedOnboarding = true; UserDefaults.standard.set(true, forKey: "kria.onboarding.complete") }
    func loadProjects() async {
        if let cache, let cached = try? cache.projects(), !cached.isEmpty {
            projects = cached.map(\.summary).filter { !deletedProjectIDs.contains($0.id) }
        }
        let generation = collectionGeneration
        isLoading = true
        if projects.isEmpty { projectsState = .loading }
        defer { isLoading = false }
        do {
            let fetched = try await api.projects()
            guard generation == collectionGeneration else { return }
            projects = fetched.filter { !deletedProjectIDs.contains($0.id) }.map { incoming in
                if let current = projects.first(where: { $0.id == incoming.id }), current.serverRevision > incoming.serverRevision { return current }
                return incoming
            }
            projectsState = projects.isEmpty ? .empty : .loaded
            errorMessage = nil
            if let selectedProject,
               let refreshed = projects.first(where: { $0.id == selectedProject.id }) {
                self.selectedProject = refreshed
            }
            try? cache?.upsert(projects)
        }
        catch {
            guard generation == collectionGeneration else { return }
            #if DEBUG
            if projects.isEmpty {
                projects = PreviewFixtures.projects.filter { !deletedProjectIDs.contains($0.id) }
                projectsState = .loaded
            }
            #else
            // Keep Kria's own copy rather than raw system text, and mention the
            // connection only when the request never got a response.
            let message = RequestFailureCause(error) == .connection
                ? APIError.offline.localizedDescription
                : ((error as? APIError) ?? .invalidResponse).localizedDescription
            errorMessage = message
            projectsState = projects.isEmpty ? .failed(message) : .loaded
            #endif
        }
    }
    func loadLibrary() async {
        let generation = collectionGeneration
        if libraryProjects.isEmpty { libraryState = .loading }
        do {
            let fetched = try await api.library()
            guard generation == collectionGeneration else { return }
            libraryProjects = fetched
            libraryState = libraryProjects.isEmpty ? .empty : .loaded
            errorMessage = nil
        }
        catch {
            guard generation == collectionGeneration else { return }
            errorMessage = error.localizedDescription
            libraryState = libraryProjects.isEmpty ? .failed(error.localizedDescription) : .loaded
        }
    }
    func createProject() async {
        guard !isCreatingProject else { return }
        isCreatingProject = true
        defer { isCreatingProject = false }
        errorMessage = nil
        do {
            let thread = try await api.createThread(message: nil)
            let project = thread.summary
            collectionGeneration += 1
            projects.insert(project, at: 0)
            projectsState = .loaded
            try? cache?.upsert([project])
            selectedProject = project
        } catch {
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") {
                let project = ProjectSummary(id: UUID(), title: "Untitled project", status: .draft, updatedAt: .now, posterURL: nil)
                projects.insert(project, at: 0); selectedProject = project; projectsState = .loaded
            } else { errorMessage = error.localizedDescription }
            #else
            errorMessage = error.localizedDescription
            #endif
        }
    }
    func openWorkspace(preferredProjectID: UUID? = nil) async {
        selectWorkspaceProject(preferredProjectID: preferredProjectID)
        uploads.recoverInterruptedPreparations()
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
        guard !deletedProjectIDs.contains(project.id) else { return }
        selectedProject = projects.first(where: { $0.id == project.id }) ?? project
        errorMessage = nil
    }
    func renameProject(_ project: ProjectSummary, title: String, clientEventID: String) async throws {
        do {
            let updated = try await api.renameProject(project, title: title, clientEventID: clientEventID).summary
            updateProject(updated)
        } catch {
            if error as? APIError == .conflict, let latest = try? await api.project(threadID: project.id) {
                updateProject(latest.summary)
            }
            throw error
        }
    }
    func deleteProject(_ project: ProjectSummary) async throws {
        guard project.status != .rendering, !uploads.records.contains(where: { $0.projectID == project.id }) else { throw APIError.conflict }
        do { try await api.deleteProject(project) }
        catch {
            if error as? APIError == .conflict, let latest = try? await api.project(threadID: project.id) {
                updateProject(latest.summary)
            }
            throw error
        }
        deletedProjectIDs.insert(project.id)
        collectionGeneration += 1
        var cacheWarning: String?
        // The server deletion is already committed; never present it as a failed
        // delete just because the recoverable device cache cannot save.
        do { try cache?.removeProject(project.id) }
        catch { cacheWarning = "Project deleted. Device storage couldn’t finish updating." }
        projects.removeAll { $0.id == project.id }
        libraryProjects.removeAll { $0.id == project.id || $0.id == project.activeJobID }
        if selectedProject?.id == project.id { selectedProject = projects.first }
        projectsState = projects.isEmpty ? .empty : .loaded
        await loadLibrary()
        libraryProjects.removeAll { $0.id == project.id || $0.id == project.activeJobID }
        if let cacheWarning { errorMessage = cacheWarning }
    }
    /// Silently deletes a chat the user walked away from without sending anything, staging media,
    /// or leaving unsent text. Any doubt (fetch failure, conflict, still uploading) keeps the chat.
    func discardIfAbandoned(_ id: UUID) async {
        guard !deletedProjectIDs.contains(id), !reapingChatIDs.contains(id),
              let project = projects.first(where: { $0.id == id }), project.status == .draft else { return }
        reapingChatIDs.insert(id)
        defer { reapingChatIDs.remove(id) }
        guard let thread = try? await api.project(threadID: id),
              AbandonedChat.isEmpty(
                thread: thread,
                draft: chatDrafts.draft(for: id),
                hasPendingUploads: uploads.records.contains { $0.projectID == id }
              ) else { return }
        // The user may have come back and typed while the thread was loading.
        guard chatDrafts.draft(for: id).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        try? await deleteProject(thread.summary)
        chatDrafts.setDraft("", for: id)
    }
    /// Cleans up empty chats left by a killed app, keeping whichever chat is open when
    /// `keeping` is given (the app is foregrounded on it).
    func sweepAbandonedChats(keeping: UUID? = nil) async {
        for project in projects where project.status == .draft && project.id != keeping && !project.awaitsConfirmation {
            await discardIfAbandoned(project.id)
        }
    }
    func updateProject(_ project: ProjectSummary) {
        guard !deletedProjectIDs.contains(project.id) else { return }
        if let index = projects.firstIndex(where: { $0.id == project.id }) {
            guard project.serverRevision >= projects[index].serverRevision else { return }
            projects[index] = project
        } else {
            projects.insert(project, at: 0)
        }
        if selectedProject?.id == project.id { selectedProject = project }
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
