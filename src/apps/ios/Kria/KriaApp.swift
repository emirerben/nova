import SwiftUI
import SwiftData

@main struct KriaApp: App {
    @UIApplicationDelegateAdaptor(KriaAppDelegate.self) private var appDelegate
    @StateObject private var auth = AuthStore()
    @StateObject private var model: AppModel
    private let container: ModelContainer
    init() {
        let container = (try? ModelContainer(for: CachedProject.self, CachedAsset.self, CachedUploadJob.self, CachedReceipt.self)) ?? Self.fallbackContainer()
        self.container = container
        _model = StateObject(
            wrappedValue: AppModel(cache: CacheRepository(context: container.mainContext))
        )
    }
    var body: some Scene {
        WindowGroup { RootView().environmentObject(auth).environmentObject(model) }
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
    var body: some View {
        Group {
            #if DEBUG
            if ProcessInfo.processInfo.arguments.contains("-ui-testing-editor") {
                NativeEditorUITestHost()
            }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat-bubbles") {
                ChatBubbleUITestHost()
            }
            else if ProcessInfo.processInfo.arguments.contains("-ui-testing-chat") { ChatWorkspaceView() }
            else if !auth.isSignedIn { SignInView() }
            else { ChatWorkspaceView() }
            #else
            if !auth.isSignedIn { SignInView() }
            else { ChatWorkspaceView() }
            #endif
        }
        .kriaPage()
        .background(KriaColor.paper.ignoresSafeArea())
    }
}

#if DEBUG
private struct ChatBubbleUITestHost: View {
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
                        initialPlaybackURL: Bundle.main.url(forResource: "montage", withExtension: "mp4"),
                        onBack: {
                            showsEditor = false
                            showsProjects = true
                        }
                    )
                }
                .accessibilityIdentifier("native-editor-fixture-\(fixture.shape.rawValue)")
                .accessibilityValue("Dynamic type \(dynamicTypeLabel); reduce motion \(reduceMotion ? "on" : "off")")
                .environment(\.dynamicTypeSize, dynamicTypeSize)
            }
        }
    }
}
#endif
