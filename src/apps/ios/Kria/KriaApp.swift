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
private struct NativeEditorUITestHost: View {
    @EnvironmentObject private var model: AppModel
    @State private var showsEditor = true
    @State private var showsProjects = false

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
                        initialDraft: PreviewFixtures.editorDraft,
                        initialPlaybackURL: Bundle.main.url(forResource: "montage", withExtension: "mp4"),
                        onProjects: {
                            showsEditor = false
                            showsProjects = true
                        },
                        onChat: { showsEditor = false }
                    )
                }
            }
        }
    }
}
#endif
