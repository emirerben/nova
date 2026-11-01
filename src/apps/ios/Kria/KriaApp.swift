import SwiftUI
import SwiftData
import UserNotifications

@main struct KriaApp: App {
    @UIApplicationDelegateAdaptor(KriaAppDelegate.self) private var appDelegate
    @StateObject private var auth: AuthStore
    @StateObject private var model: AppModel
    @StateObject private var notifications = NativeNotificationCoordinator()
    private let container: ModelContainer
    init() {
        // Launch ends when the first transcript row is visible (`ChatWorkspaceView`); `launch.init` is the
        // synchronous setup in this initializer and `AppModel.init`.
        Perf.mark("app.init")
        Perf.begin("launch.toTranscript")
        Perf.begin("launch.toPixels")
        Perf.begin("launch.init")
        defer { Perf.end("launch.init") }
        // Every phone render coordinator keeps encoding briefly in the background.
        UIKitDeviceRenderBackgroundExecution.install()
        #if DEBUG
        // First, before anything can touch `URLSession.shared`: AsyncImage loads gallery posters through it.
        PerfPosterURLProtocol.registerIfNeeded()
        // Opt-in hardware journey: real HTTP/API + scratch database, separate QA bundle.
        // Never uses a developer's Keychain or connects without both explicit inputs.
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-retry-api"),
           let address = ProcessInfo.processInfo.environment["KRIA_RETRY_TEST_API_URL"],
           let baseURL = URL(string: address),
           let subject = ProcessInfo.processInfo.environment["KRIA_RETRY_TEST_USER_ID"],
           UUID(uuidString: subject) != nil {
            let container = Self.fallbackContainer()
            self.container = container
            let store = AccountUITestTokenStore()
            let api = KriaAPI(baseURL: baseURL, tokenStore: store)
            let auth = AuthStore(tokenStore: store, api: api)
            let payload = Data("{\"sub\":\"\(subject)\"}".utf8).base64EncodedString()
            try? auth.signIn(with: MobileSession(accessToken: "fixture.\(payload).fixture", refreshToken: "fixture-refresh", expiresIn: 3600), displayName: "Retry QA")
            auth.acceptAIConsent()
            _auth = StateObject(wrappedValue: auth)
            _model = StateObject(wrappedValue: AppModel(api: api, cache: CacheRepository(context: container.mainContext), renderOwner: { [weak auth] in auth?.localStorageIdentity }))
            return
        }
        if let configuration = CameraRollLiveQA.configuration() {
            UIApplication.shared.isIdleTimerDisabled = true
            let container = Self.fallbackContainer()
            self.container = container
            let store = AccountUITestTokenStore()
            let api = KriaAPI(baseURL: configuration.baseURL, tokenStore: store)
            let auth = AuthStore(tokenStore: store, api: api)
            try? auth.signIn(with: configuration.session, displayName: "Emir · local QA")
            auth.acceptAIConsent()
            _auth = StateObject(wrappedValue: auth)
            _model = StateObject(wrappedValue: AppModel(api: api, cache: CacheRepository(context: container.mainContext), renderOwner: { [weak auth] in auth?.localStorageIdentity }))
            return
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-account") {
            let container = Self.fallbackContainer()
            self.container = container
            let store = AccountUITestTokenStore()
            let api = AccountUITestTransport.api(tokenStore: store)
            let auth = AuthStore(tokenStore: store, api: api)
            let payload = Data("{\"sub\":\"\(UUID().uuidString)\"}".utf8).base64EncodedString()
            try? auth.signIn(with: MobileSession(accessToken: "fixture.\(payload).fixture", refreshToken: "fixture-refresh", expiresIn: 3600), displayName: "Test creator")
            _auth = StateObject(wrappedValue: auth)
            _model = StateObject(wrappedValue: AppModel(api: api, cache: CacheRepository(context: container.mainContext), renderOwner: { [weak auth] in auth?.localStorageIdentity }))
            return
        }
        if ProcessInfo.processInfo.arguments.contains("-camera-roll-original-link") || ProcessInfo.processInfo.arguments.contains("-camera-roll-benchmark") || ProcessInfo.processInfo.arguments.contains("-camera-roll-collection") || ProcessInfo.processInfo.arguments.contains("-camera-roll-device-collection") || ProcessInfo.processInfo.arguments.contains("-camera-roll-story-review") || ProcessInfo.processInfo.arguments.contains("-camera-roll-export-proof") {
            let container = Self.fallbackContainer()
            self.container = container
            let auth = AuthStore(tokenStore: ChatUITestTokenStore())
            _auth = StateObject(wrappedValue: auth)
            _model = StateObject(wrappedValue: AppModel(api: ChatUITestTransport.api(), cache: CacheRepository(context: container.mainContext), renderOwner: { [weak auth] in auth?.localStorageIdentity }))
            return
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") {
            let container = PerfFixture.container()
            self.container = container
            CreationChatFixture.seedClipThumbnails()
            PerfFixture.installHashObserver()
            let api = ChatUITestTransport.api()
            // With an expired fixture session the account is "signed in", so its profile read must hit the fixture too.
            let auth = AuthStore(tokenStore: ChatUITestTokenStore(), api: PerfFixture.expiredToken || ProcessInfo.processInfo.environment["KRIA_NATIVE_ACTIVITY_FIXTURE"] != nil ? api : nil)
            _auth = StateObject(wrappedValue: auth)
            let model = AppModel(api: api, cache: CacheRepository(context: container.mainContext), renderOwner: { [weak auth] in auth?.localStorageIdentity })
            if PerfFixture.uploadTicks { model.uploads.startProgressTicksForTesting() }
            _model = StateObject(wrappedValue: model)
            return
        }
        #endif
        let auth = AuthStore()
        _auth = StateObject(wrappedValue: auth)
        let container = (try? ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self)) ?? Self.fallbackContainer()
        self.container = container
        _model = StateObject(
            wrappedValue: AppModel(cache: CacheRepository(context: container.mainContext), renderOwner: { [weak auth] in auth?.localStorageIdentity })
        )
    }
    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(auth)
                .environmentObject(model)
                .environmentObject(notifications)
                .task(id: auth.localStorageIdentity) {
                    notifications.configure(api: model.api)
                    notifications.openActivity = { [weak model] id, owner in
                        guard let model else { return false }
                        return try await model.openNativeActivity(id: id, expectedOwner: owner)
                    }
                    appDelegate.notifications = notifications
                    await notifications.bind(owner: auth.isSignedIn ? model.api.mediaLibraryOwner : nil)
                    await appDelegate.deliverPendingTap()
                }
                .preferredColorScheme(.light)
        }
            .modelContainer(container)
    }
    private static func fallbackContainer() -> ModelContainer {
        try! ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
    }
}

@MainActor final class KriaAppDelegate: NSObject, UIApplicationDelegate, UNUserNotificationCenterDelegate {
    weak var notifications: NativeNotificationCoordinator?
    private var pendingActivityID: String?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await notifications?.registered(token: deviceToken) }
    }

    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        notifications?.registrationFailed()
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse, withCompletionHandler completionHandler: @escaping () -> Void) {
        let activityID = response.actionIdentifier == UNNotificationDefaultActionIdentifier
            ? response.notification.request.content.userInfo["activity_id"] as? String : nil
        Task { @MainActor in
            if let activityID, UUID(uuidString: activityID) != nil {
                pendingActivityID = activityID
                await deliverPendingTap()
            }
        }
        completionHandler()
    }

    func deliverPendingTap() async {
        guard let notifications, let id = pendingActivityID else { return }
        pendingActivityID = nil
        await notifications.receiveTap(["activity_id": id])
    }

    func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String, completionHandler: @escaping () -> Void) {
        guard identifier == BackgroundUploadCoordinator.sessionIdentifier else {
            completionHandler()
            return
        }
        BackgroundUploadLifecycle.shared.store(completionHandler: completionHandler)
    }
}

struct RootView: View {
    @EnvironmentObject private var auth: AuthStore
    @EnvironmentObject private var model: AppModel
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @StateObject private var nativeUpdateState = NativeUpdateState.shared
    #if DEBUG
    @State private var updateFixtureAction = ""
    #endif
    var body: some View {
        Group {
            if nativeUpdateState.isUpdateRequired {
                NativeUpdateRequiredView(
                    fixtureAction: {
                        #if DEBUG
                        ProcessInfo.processInfo.arguments.contains("-ui-testing-native-update")
                            ? $updateFixtureAction
                            : nil
                        #else
                        nil
                        #endif
                    }()
                )
            } else {
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("-native-library-audit"), auth.isSignedIn {
                ProgressView("Checking video formats…").task { await NativeLibraryAudit.run(api: KriaAPI()) }
                    .onAppear { UIApplication.shared.isIdleTimerDisabled = true }
                    .onDisappear { UIApplication.shared.isIdleTimerDisabled = false }
            }
            else if ProcessInfo.processInfo.arguments.contains("-camera-roll-original-link") {
                CameraRollOriginalLinkView()
            }
            else if ProcessInfo.processInfo.arguments.contains("-camera-roll-export-proof") {
                CameraRollExportProofView()
            } else if ProcessInfo.processInfo.arguments.contains("-camera-roll-story-review") {
                CameraRollStoriesUITestHost()
            } else if ProcessInfo.processInfo.arguments.contains("-camera-roll-collection") {
                CameraRollSurfaceUITestHost()
            } else if ProcessInfo.processInfo.arguments.contains("-camera-roll-device-collection") {
                CameraRollDeviceDiagnosticHost(api: model.api)
            } else if ProcessInfo.processInfo.arguments.contains("-camera-roll-benchmark") { CameraRollBenchmarkView() }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-saved-uploads") { SavedUploadsUITestHost() }
            else if ProcessInfo.processInfo.arguments.contains("-device-effects") { DeviceEffectsView() }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-brand") { BrandPreviewHost() }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") {
                NativeEditorUITestHost()
            }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat-bubbles") {
                ChatBubbleUITestHost()
            }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") {
                ChatWorkspaceView().onAppear { PerfCountersWindow.installIfNeeded() }
            }
            else if !auth.isSignedIn { SignInView() }
            else { signedInContent }
            #else
            if !auth.isSignedIn { SignInView() }
            else { signedInContent }
            #endif
            }
        }
        .kriaPage()
        .background(KriaColor.paper.ignoresSafeArea())
        #if DEBUG
        .environment(\.dynamicTypeSize, (ProcessInfo.processInfo.arguments.contains("-ui-testing-account") || ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") || ProcessInfo.processInfo.arguments.contains("-ui-testing-native-update")) && ProcessInfo.processInfo.environment["UI_TEST_DYNAMIC_TYPE_SIZE"] == "accessibility5" ? .accessibility5 : dynamicTypeSize)
        .task {
            guard ProcessInfo.processInfo.arguments.contains("-ui-testing-native-update") else { return }
            await NativeUpdateUITestTransport.triggerTypedUpdateRequirement()
        }
        #endif
        .onChange(of: auth.localStorageIdentity) { previous, current in
            if previous != nil, previous != current { Task { await model.deviceRenders.stopAll() } }
        }
    }

    @ViewBuilder private var signedInContent: some View {
        if auth.hasAIConsent {
            ChatWorkspaceView()
        } else {
            AIConsentView(accept: { auth.acceptAIConsent() }, decline: { auth.signOut() })
        }
    }
}

private struct NativeUpdateRequiredView: View {
    var fixtureAction: Binding<String>?

    var body: some View {
        GeometryReader { geometry in
            ScrollView {
                VStack(spacing: 20) {
                    Spacer(minLength: 28)
                    Image(systemName: "arrow.down.app.fill")
                        .font(.system(size: 40, weight: .semibold))
                        .foregroundStyle(KriaColor.ink)
                        .frame(width: 76, height: 76)
                        .background(KriaColor.sky, in: Circle())
                    Text("Update Kria to continue")
                        .font(KriaFont.display(30))
                        .multilineTextAlignment(.center)
                    Text("This version of Kria is no longer supported. Get the update in TestFlight, then reopen Kria.")
                        .font(KriaFont.body(16))
                        .foregroundStyle(KriaColor.zinc)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                    VStack(spacing: 12) {
                        Link("Update Kria", destination: KriaUpdateLink.destination)
                            .buttonStyle(KriaPrimaryButtonStyle())
                            .accessibilityIdentifier("kria-update-app-store-link")
                        Link("Contact support", destination: KriaLegal.supportURL)
                            .buttonStyle(KriaSecondaryButtonStyle())
                            .accessibilityIdentifier("kria-update-support-link")
                    }
                    .frame(maxWidth: 320)
                    Spacer(minLength: 28)
                }
                .padding(.horizontal, 28)
                .frame(maxWidth: 520)
                .frame(maxWidth: .infinity, minHeight: geometry.size.height)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .environment(\.openURL, OpenURLAction { url in
            guard let fixtureAction else { return .systemAction }
            fixtureAction.wrappedValue = KriaUpdateLink.isUpdate(url) ? "app-store" : "support"
            return .handled
        })
        .overlay(alignment: .bottom) {
            if let fixtureAction, !fixtureAction.wrappedValue.isEmpty {
                Text("Opened \(fixtureAction.wrappedValue)")
                    .accessibilityIdentifier("kria-update-fixture-action")
            }
        }
    }
}

#if DEBUG
private struct ChatBubbleUITestHost: View {
    @State private var pasted = ""
    @State private var selectedOption = ""

    private var dynamicTypeSize: DynamicTypeSize {
        switch ProcessInfo.processInfo.environment["UI_TEST_DYNAMIC_TYPE_SIZE"] {
        case "accessibility5": return .accessibility5
        case "accessibility3": return .accessibility3
        case "accessibility2": return .accessibility2
        case "xxLarge": return .xxLarge
        default: return .large
        }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                ChatMessageRow(
                    message: ChatTranscriptMessage(
                        id: "short",
                        role: .user,
                        content: "Montage works."
                    )
                )
                ChatMessageRow(
                    message: ChatTranscriptMessage(
                        id: "long",
                        role: .user,
                        content: "Make this a warm, energetic montage that starts with the arrival, keeps the candid reactions, and ends on the wide sunset shot."
                    )
                )
                ChatMessageRow(
                    message: ChatTranscriptMessage(
                        id: "reply",
                        role: .assistant,
                        content: "Should the edit end on the sunset or the arrival?",
                        options: ["End on the sunset", "End on the arrival"]
                    ),
                    onSelectOption: { selectedOption = $0 }
                )
                ChatMessageRow(
                    message: ChatTranscriptMessage(
                        id: "pending",
                        role: .user,
                        content: "Keep the laughter at the table.",
                        isPending: true
                    )
                )
                // Reads what a message's Copy wrote, through the system paste
                // control, so the test needs no paste permission prompt.
                PasteButton(payloadType: String.self) { strings in pasted = strings.first ?? "" }
                Text("Pasted: \(pasted)").accessibilityIdentifier("chat-bubbles-pasted")
                Text("Selected: \(selectedOption)").accessibilityIdentifier("chat-bubbles-selected-option")
            }
            .frame(maxWidth: 620, alignment: .leading)
            .padding(16)
            .frame(maxWidth: .infinity)
        }
        .background(KriaColor.paper)
        .environment(\.dynamicTypeSize, dynamicTypeSize)
    }
}

private struct NativeEditorUITestHost: View {
    @EnvironmentObject private var model: AppModel
    @State private var showsEditor = true
    @State private var showsProjects = false

    private var fixture: NativeEditorUITestFixtures.Fixture {
        NativeEditorUITestFixtures.current
    }

    private var dynamicTypeSize: DynamicTypeSize {
        switch ProcessInfo.processInfo.environment["UI_TEST_DYNAMIC_TYPE_SIZE"] {
        case "accessibility5": return .accessibility5
        case "accessibility3": return .accessibility3
        case "accessibility2": return .accessibility2
        case "xxLarge": return .xxLarge
        default: return .large
        }
    }

    private var reduceMotion: Bool {
        ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }

    private var dynamicTypeLabel: String {
        ProcessInfo.processInfo.environment["UI_TEST_DYNAMIC_TYPE_SIZE"] ?? "large"
    }

    private var fixtureWidth: CGFloat? {
        guard let raw = ProcessInfo.processInfo.environment["UI_TEST_EDITOR_WIDTH"],
              let width = Double(raw) else { return nil }
        return CGFloat(width)
    }

    var body: some View {
        ZStack {
            Color.white.ignoresSafeArea()

            if showsProjects {
                ProjectsDrawer(
                    close: { showsProjects = false },
                    openGallery: { showsProjects = false }
                )
                .environmentObject(model)
            }

            if showsEditor {
                NavigationStack {
                    NativeEditorView(
                        project: PreviewFixtures.editorProject,
                        initialDraft: fixture.draft,
                        initialPlaybackURL: fixture.shape == .sourceText ? nil : Bundle.main.url(forResource: "montage", withExtension: "mp4"),
                        onBack: {
                            showsEditor = false
                            showsProjects = true
                        }
                    )
                    .frame(width: fixtureWidth)
                }
                .accessibilityIdentifier("native-editor-fixture-\(fixture.shape.rawValue)")
                .accessibilityValue("Dynamic type \(dynamicTypeLabel); reduce motion \(reduceMotion ? "on" : "off")")
                .environment(\.dynamicTypeSize, dynamicTypeSize)
            }
        }
    }
}
#endif
