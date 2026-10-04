import SwiftUI
import SwiftData

@main struct KriaApp: App {
    @UIApplicationDelegateAdaptor(KriaAppDelegate.self) private var appDelegate
    @StateObject private var auth: AuthStore
    @StateObject private var model: AppModel
    private let container: ModelContainer
    init() {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-account") {
            let container = Self.fallbackContainer()
            self.container = container
            let store = AccountUITestTokenStore()
            let api = AccountUITestTransport.api(tokenStore: store)
            let auth = AuthStore(tokenStore: store, api: api)
            let payload = Data("{\"sub\":\"\(UUID().uuidString)\"}".utf8).base64EncodedString()
            try? auth.signIn(with: MobileSession(accessToken: "fixture.\(payload).fixture", refreshToken: "fixture-refresh", expiresIn: 3600), displayName: "Test creator")
            _auth = StateObject(wrappedValue: auth)
            _model = StateObject(wrappedValue: AppModel(api: api, cache: CacheRepository(context: container.mainContext)))
            return
        }
        if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") {
            let container = Self.fallbackContainer()
            self.container = container
            _auth = StateObject(wrappedValue: AuthStore(tokenStore: ChatUITestTokenStore()))
            _model = StateObject(wrappedValue: AppModel(api: ChatUITestTransport.api(), cache: CacheRepository(context: container.mainContext)))
            return
        }
        #endif
        _auth = StateObject(wrappedValue: AuthStore())
        let container = (try? ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self)) ?? Self.fallbackContainer()
        self.container = container
        _model = StateObject(
            wrappedValue: AppModel(cache: CacheRepository(context: container.mainContext))
        )
    }
    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(auth)
                .environmentObject(model)
                .preferredColorScheme(.light)
        }
            .modelContainer(container)
    }
    private static func fallbackContainer() -> ModelContainer {
        try! ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self, configurations: ModelConfiguration(isStoredInMemoryOnly: true))
    }
}

@MainActor final class KriaAppDelegate: NSObject, UIApplicationDelegate {
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
            else if ProcessInfo.processInfo.arguments.contains("-device-effects") { DeviceEffectsView() }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-brand") { BrandPreviewHost() }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") {
                NativeEditorUITestHost()
            }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat-bubbles") {
                ChatBubbleUITestHost()
            }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") { ChatWorkspaceView() }
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
        .onChange(of: auth.isSignedIn) { _, signedIn in
            if !signedIn { Task { await model.deviceRenders.stopAll() } }
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
    private let appStoreURL = URL(string: "https://apps.apple.com/")!
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
                    Text("This version of Kria is no longer supported. Update the app from the App Store, then reopen it.")
                        .font(KriaFont.body(16))
                        .foregroundStyle(KriaColor.zinc)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                    VStack(spacing: 12) {
                        Link("Open the App Store", destination: appStoreURL)
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
            fixtureAction.wrappedValue = url == appStoreURL ? "app-store" : "support"
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
