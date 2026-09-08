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
            if !auth.isSignedIn { SignInView() }
            else if !model.hasCompletedOnboarding { OnboardingView() }
            else { MainShellView() }
        }
        .kriaPage()
        .background(KriaColor.paper.ignoresSafeArea())
    }
}
