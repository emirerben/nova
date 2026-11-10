import SwiftUI
import KriaMediaEngine

struct ChatWorkspaceView: View {
    @EnvironmentObject private var model: AppModel
    @AppStorage("kria.workspace.last-project-id") private var lastProjectID = ""
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    // Discrete drawer state only. The per-frame drag lives in `ProjectsDrawerContainer`
    // so a drag tick never re-evaluates the (heavy) workspace below.
    @State private var showsProjects = false
    @State private var drawerMounted = false
    @State private var showsGallery = false
    @State private var showsAccount = false
    @State private var collectionsEnabled = false
    @State private var collectionCacheOnlyRequired = false
    /// Kept for the life of the workspace so uploads and analysis carry on while the creator is in a chat.
    @State private var cameraRoll: CameraRollSurfaceModel?
    @State private var projectActions = ProjectActionsPresenter()
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        ProjectsDrawerContainer(
            isOpen: showsProjects,
            mounted: drawerMounted,
            reduceMotion: reduceMotion,
            commit: commitDrawer,
            settled: drawerSettled,
            close: { setDrawerOpen(false) },
            drawer: ProjectsDrawer(
                close: { setDrawerOpen(false) },
                openGallery: { setDrawerOpen(false); Perf.begin("gallery.open"); showsGallery = true },
                openAccount: { setDrawerOpen(false); showsAccount = true },
                openCollections: collectionsEnabled ? { openCameraRoll() } : nil
            ),
            content: workspace
        )
        .environment(projectActions)
        .projectActionAlerts(projectActions)
        .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: showsProjects)
        .onChange(of: showsProjects) { _, isOpen in
            guard isOpen else { return }
            UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            // The drawer holds New chat: have capabilities ready before it is tapped.
            Task { [api = model.api] in await api.prefetchCreationCapabilities() }
        }
        .task {
            let collectionCapabilities = try? await model.api.creationCapabilities()
            collectionsEnabled = collectionCapabilities?.mediaCollections == true
            collectionCacheOnlyRequired = collectionCapabilities?.mediaCollectionCacheOnly ?? false
            Perf.mark("workspace.task")
            await model.openWorkspace(preferredProjectID: UUID(uuidString: lastProjectID))
            Perf.mark("workspace.opened")
            await model.sweepAbandonedChats()
            Perf.mark("sweep.done")
        }
        .onChange(of: model.selectedProject?.id) { previous, identifier in
            lastProjectID = identifier?.uuidString ?? ""
            if let previous, previous != identifier { Task { await model.discardIfAbandoned(previous) } }
        }
        .onChange(of: scenePhase) { _, phase in
            // Phone renders parked by a background suspension or an outage
            // continue on their own when Kria comes back (S3-c, S3-d).
            if phase == .active { Task { await model.deviceRenders.resumeParked() } }
            guard phase == .background, let id = model.selectedProject?.id else { return }
            Task { await model.discardIfAbandoned(id) }
        }
        .onChange(of: model.chatRouteRequest) { _, request in
            // "Review in chat" from the gallery editor: close the gallery (and the editor
            // and video it presented) onto this edit's conversation, or the projects list
            // when the conversation is unknown.
            guard let request else { return }
            showsGallery = false
            if request.threadID == nil { setDrawerOpen(true) }
        }
        .fullScreenCover(isPresented: $showsGallery) {
            NavigationStack { GalleryView(openProjects: { showsGallery = false; setDrawerOpen(true) }) }
                .environmentObject(model)
        }
        .sheet(isPresented: $showsAccount) {
            NavigationStack { AccountView() }
        }
    }

    private func openCameraRoll() {
        if cameraRoll == nil {
            cameraRoll = CameraRollSurfaceModel.make(api: model.api, cacheOnlyRequired: collectionCacheOnlyRequired)
        }
        withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.22)) { model.openCameraRoll() }
        setDrawerOpen(false)
    }

    /// The camera roll sits outside the chat's NavigationStack. It has its own, and a nested stack pushes its pages
    /// onto the outer one, where they outlive the switch to a chat.
    @ViewBuilder private var workspace: some View {
        if model.showsCameraRoll, model.pendingNewChat == nil, let cameraRoll {
            WorkspaceCrossfade(dismiss: {}) { _ in
                CameraRollSurfaceView(model: cameraRoll, openProjects: { setDrawerOpen(!showsProjects) }, onSelected: { thread in
                    await model.loadProjects()
                    return model.selectProject(thread.summary)
                })
            }
        } else {
            chatWorkspace
        }
    }

    private var chatWorkspace: some View {
        NavigationStack {
            Group {
                if let project = model.selectedProject, model.pendingNewChat == nil {
                    CreationWorkspaceView(
                        project: project,
                        openProjects: { setDrawerOpen(!showsProjects) },
                        openAccount: { showsAccount = true }
                    )
                    .environmentObject(model.chatDrafts)
                    .id(project.id)
                } else {
                    Group {
                        if let pending = model.pendingNewChat {
                            PendingNewChatView(pending: pending)
                        } else if model.isLoading || model.projectsState == .loading || model.projectsState == .idle {
                            WorkspaceLoadingView()
                        } else if model.projectsState == .empty {
                            WorkspaceEmptyView { Task { await model.createProject() } }
                        } else {
                            WorkspaceRecoveryView { Task { await model.openWorkspace() } }
                        }
                    }
                    .accessibilityHidden(showsProjects)
                    .allowsHitTesting(!showsProjects)
                    .safeAreaInset(edge: .top) {
                        HStack {
                            Button { setDrawerOpen(!showsProjects) } label: {
                                KriaIcon(.menu).frame(width: 44, height: 44).background(KriaColor.menu, in: Circle())
                            }
                            .accessibilityLabel(showsProjects ? "Close projects" : "Open projects")
                            .accessibilityIdentifier("workspace-menu-toggle")
                            Spacer()
                            KriaWordmark().accessibilityHidden(showsProjects)
                            Spacer()
                            Color.clear.frame(width: 44, height: 44).accessibilityHidden(true)
                        }.padding(.horizontal, 16).frame(minHeight: 64).background(WorkspaceSurface())
                    }
                }
            }
            .toolbar(.hidden, for: .navigationBar)
        }
    }

    /// Programmatic open/close (menu button, row taps, gallery return).
    private func setDrawerOpen(_ isOpen: Bool) {
        withAnimation(DrawerMotion.settle(reduceMotion: reduceMotion)) {
            commitDrawer(isOpen)
        } completion: {
            drawerSettled()
        }
    }

    private func commitDrawer(_ isOpen: Bool) {
        if isOpen { Perf.begin("drawer.open") }
        drawerMounted = true
        showsProjects = isOpen
    }

    private func drawerSettled() {
        if showsProjects {
            // Refresh the gallery count once the slide has come to rest, not while it runs.
            Task { await model.loadLibraryIfStale() }
        } else {
            drawerMounted = false
        }
    }
}

/// The chat composer's text, kept out of `@State` so a keystroke does not invalidate the workspace.
@Observable
final class ChatPromptDraft {
    var text = ""
    @ObservationIgnored var onChange: ((String) -> Void)?

    func set(_ newValue: String) {
        guard newValue != text else { return }
        text = newValue
        onChange?(newValue)
    }
}

enum DrawerMotion {
    /// Critically damped, so a release never overshoots the endpoints. `velocity` is in
    /// SwiftUI's unit-relative terms (fraction of the remaining distance per second).
    static func settle(reduceMotion: Bool, velocity: CGFloat = 0) -> Animation? {
        reduceMotion ? nil : .interpolatingSpring(.init(duration: 0.32, bounce: 0), initialVelocity: Double(velocity))
    }
}

/// Reference-typed so writes never invalidate a view: scroller frames change every drag
/// tick (they sit inside the moving content) and velocity samples are only read at release.
private final class DrawerGestureTracking {
    var exclusions: [CGRect] = []
    var previous: (time: Date, x: CGFloat)?
    var latest: (time: Date, x: CGFloat)?

    func record(time: Date, x: CGFloat) {
        previous = latest
        latest = (time, x)
    }

    /// Instantaneous horizontal velocity (pt/s) from the last two samples, 0 if stale.
    func releaseVelocity(at time: Date) -> CGFloat {
        guard let previous, let latest, time.timeIntervalSince(latest.time) < 0.1 else { return 0 }
        let dt = latest.time.timeIntervalSince(previous.time)
        return dt > 0.001 ? (latest.x - previous.x) / dt : 0
    }

    func reset() { previous = nil; latest = nil }

    /// Whether a touch began on something that keeps its own sideways drag: a horizontal scroller (a `ScrollView` or
    /// a paging `TabView`) or the handles of an active text selection. Asking UIKit means a new row or field works
    /// without remembering to opt out.
    @MainActor static func startsOnOwnDrag(at point: CGPoint) -> Bool {
        let windows = UIApplication.shared.connectedScenes.compactMap { ($0 as? UIWindowScene)?.windows }.flatMap { $0 }
        guard let window = windows.first(where: { $0.isKeyWindow }) else { return false }
        if selectionHandlesContain(point, in: window) { return true }
        guard var view = window.hitTest(point, with: nil) else { return false }
        while true {
            if let scroll = view as? UIScrollView, scroll.isScrollEnabled,
               scroll.contentSize.width + scroll.adjustedContentInset.left + scroll.adjustedContentInset.right > scroll.bounds.width + 0.5 {
                return true
            }
            guard let parent = view.superview else { return false }
            view = parent
        }
    }

    /// A selection's handles hang outside the text view, so a touch within a handle's reach of the field counts.
    @MainActor private static func selectionHandlesContain(_ point: CGPoint, in window: UIWindow) -> Bool {
        guard let input = selectingTextInput(in: window) else { return false }
        return input.convert(input.bounds, to: window).insetBy(dx: -44, dy: -44).contains(point)
    }

    @MainActor private static func selectingTextInput(in view: UIView) -> (UIView & UITextInput)? {
        if view.isFirstResponder, let input = view as? (UIView & UITextInput),
           let range = input.selectedTextRange, !range.isEmpty {
            return input
        }
        for subview in view.subviews {
            if let found = selectingTextInput(in: subview) { return found }
        }
        return nil
    }
}

/// Owns the per-frame drag state. `drawer` and `content` arrive as stored values (built
/// once by the parent) so SwiftUI can skip re-evaluating them while only the offset moves.
private struct ProjectsDrawerContainer<Drawer: View, Content: View>: View {
    let isOpen: Bool
    let mounted: Bool
    let reduceMotion: Bool
    let commit: (Bool) -> Void
    let settled: () -> Void
    let close: () -> Void
    let drawer: Drawer
    let content: Content

    @State private var drawerDrag: CGFloat = 0
    @GestureState private var drawerGestureActive = false
    @State private var horizontalDrawerDrag: Bool?
    @State private var drawerDragStartTime: Date?
    @State private var tracking = DrawerGestureTracking()

    var body: some View {
        GeometryReader { geometry in
            let drawerWidth = min(326, max(0, geometry.size.width - 76))
            let drawerOffset = min(drawerWidth, max(0, (isOpen ? drawerWidth : 0) + drawerDrag))
            let drawerActive = isOpen || drawerOffset > 0
            let drawerProgress = drawerWidth > 0 ? drawerOffset / drawerWidth : 0
            let topInset = geometry.safeAreaInsets.top
            let bottomInset = geometry.safeAreaInsets.bottom
            ZStack(alignment: .topLeading) {
                if drawerActive || mounted {
                    drawer
                        .frame(width: drawerWidth, height: geometry.size.height)
                        .offset(x: drawerOffset - drawerWidth, y: topInset)
                        .accessibilityHidden(!drawerActive)
                        .allowsHitTesting(drawerActive)
                }
                content
                    .environment(\.projectsDrawerOpen, drawerActive)
                    .frame(width: geometry.size.width, height: geometry.size.height)
                    .padding(.top, topInset)
                    .padding(.bottom, bottomInset)
                    .background(WorkspaceSurface())
                    .clipShape(RoundedRectangle(cornerRadius: 44 * drawerProgress, style: .continuous))
                    .offset(x: drawerOffset)
            }
            .environment(\.projectsDrawerProgress, drawerProgress)
            .frame(width: geometry.size.width, height: geometry.size.height + topInset + bottomInset, alignment: .topLeading)
            .clipped()
            .offset(y: -topInset)
            .background(KriaColor.paper.ignoresSafeArea())
            .onPreferenceChange(DrawerGestureExclusionPreference.self) { tracking.exclusions = $0 }
            .simultaneousGesture(drawerGesture(width: drawerWidth))
            .onChange(of: drawerGestureActive) { _, active in
                // onEnded is not called when another recognizer or the system
                // cancels a drag. GestureState resets for both outcomes.
                guard !active else { return }
                let needsSettlement = horizontalDrawerDrag == true || drawerDrag != 0
                horizontalDrawerDrag = nil
                if needsSettlement {
                    settle(open: drawerOffset > drawerWidth / 2, width: drawerWidth, velocity: 0)
                }
            }
            .accessibilityAction(.escape, close)
        }
    }

    /// Commit the destination and release the drag in the same transaction.
    /// GestureState's automatic reset previously replayed the closing offset.
    private func settle(open: Bool, width: CGFloat, velocity: CGFloat) {
        let current = min(width, max(0, (isOpen ? width : 0) + drawerDrag))
        let remaining = max(1, open ? width - current : current)
        let towardTarget = open ? velocity : -velocity
        let relative = min(12, max(0, towardTarget / remaining))
        withAnimation(DrawerMotion.settle(reduceMotion: reduceMotion, velocity: relative)) {
            commit(open)
            drawerDrag = 0
        } completion: {
            if drawerDrag == 0 { settled() }
        }
    }

    private func drawerGesture(width: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 3, coordinateSpace: .global)
            .updating($drawerGestureActive) { _, active, _ in active = true }
            .onChanged { value in
                // Choose an axis once, without a 20-point dead zone at touch-down.
                if horizontalDrawerDrag == nil {
                    let startsInScroller = !isOpen && (tracking.exclusions.contains { $0.contains(value.startLocation) }
                        || DrawerGestureTracking.startsOnOwnDrag(at: value.startLocation))
                    horizontalDrawerDrag = !startsInScroller && abs(value.translation.width) > abs(value.translation.height)
                    drawerDragStartTime = value.time
                    tracking.reset()
                }
                guard horizontalDrawerDrag == true else { return }
                tracking.record(time: value.time, x: value.translation.width)
                var transaction = Transaction()
                transaction.disablesAnimations = true
                withTransaction(transaction) { drawerDrag = value.translation.width }
            }
            .onEnded { value in
                let wasHorizontal = horizontalDrawerDrag == true
                let startTime = drawerDragStartTime
                horizontalDrawerDrag = nil
                drawerDragStartTime = nil
                guard wasHorizontal else { return }
                // SwiftUI's own value.velocity/predictedEndTranslation are
                // measured (not requested) and, for a short, fast release,
                // unreliable here — the same nominally-fast flick has been
                // observed reporting wildly different velocities from one run
                // to the next. Elapsed wall-clock time over the gesture's own
                // translation is exact and reproduces the real release speed.
                let elapsed = startTime.map { value.time.timeIntervalSince($0) } ?? 0
                let measuredVelocity = elapsed > 0.001 ? value.translation.width / elapsed : 0
                let isDeliberateFlick = abs(measuredVelocity) > Self.flickVelocityThreshold
                let isOpenAfter: Bool
                if isDeliberateFlick {
                    // Honor a deliberate flick's direction outright, regardless
                    // of exactly how far it travelled.
                    isOpenAfter = measuredVelocity > 0
                } else {
                    // Anything slower settles purely by how far it was actually
                    // dragged, landing at the true nearest endpoint.
                    let settledOffset = (isOpen ? width : 0) + value.translation.width
                    isOpenAfter = settledOffset > width / 2
                }
                // The decision above uses the average speed; the spring that follows
                // continues at the finger's instantaneous release speed.
                settle(open: isOpenAfter, width: width, velocity: tracking.releaseVelocity(at: value.time))
            }
    }

    // XCUIGestureVelocity.slow synthesizes ~250pt/s and .fast ~750pt/s; a real
    // deliberate flick is comfortably faster than an intentional slow drag.
    private static var flickVelocityThreshold: CGFloat { 450 }
}

private struct WorkspaceEmptyView: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    let create: () -> Void
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Start your first video.").font(KriaFont.headline(29))
            Text("Create a project, then tell Kria what you want the finished cut to feel like.")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
            if let error = model.errorMessage {
                Text(error).font(KriaFont.body(12)).foregroundStyle(KriaColor.failureText)
            }
            Button("New video", action: create).buttonStyle(CanonicalPrimaryButtonStyle())
        }
        .padding(24)
        .frame(maxWidth: 480, maxHeight: .infinity, alignment: .leading)
        .background(WorkspaceSurface())
    }
}

/// The new conversation, shown the moment New chat is tapped while the server makes the real one. It has
/// no thread id yet: the creator can type, but sending and attaching wait for the real one.
private struct PendingNewChatView: View {
    let pending: AppModel.PendingNewChat
    @EnvironmentObject private var model: AppModel
    @FocusState private var focus: Bool

    var body: some View {
        VStack(spacing: 0) {
            VStack(spacing: 14) {
                if let failure = pending.failure {
                    Text("Your chat couldn’t be created.").font(KriaFont.headline(22))
                    Text(failure).font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc).multilineTextAlignment(.center)
                    Button("Try again") { model.startNewChat() }
                        .buttonStyle(CanonicalPrimaryButtonStyle())
                        .accessibilityIdentifier("pending-new-chat-retry")
                    Button("Back to my chats") { model.cancelPendingNewChat() }
                        .font(KriaFont.body(14))
                        .accessibilityIdentifier("pending-new-chat-cancel")
                } else {
                    ProgressView().tint(KriaColor.ink)
                    Text("Starting your chat…").font(KriaFont.body(14)).foregroundStyle(KriaColor.zinc)
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("pending-new-chat")
            // Typing works at once; sending and attaching wait for the real thread, so nothing typed can
            // reach any other conversation.
            ChatComposer(
                text: Binding(get: { pending.draft }, set: { model.setPendingNewChatDraft($0) }),
                isSending: false, canAttach: false, blocksSubmission: true,
                isFocused: $focus, attach: {}, send: {}
            )
        }
        .background(WorkspaceSurface())
    }
}

private struct WorkspaceLoadingView: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    var body: some View {
        VStack(spacing: 14) {
            ProgressView().tint(KriaColor.ink)
            Text("Opening your conversation…")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(WorkspaceSurface())
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Opening your creation conversation")
    }
}

private struct WorkspaceRecoveryView: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    let retry: () -> Void
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Your workspace couldn’t open.").font(KriaFont.headline(29))
            Text(model.errorMessage ?? "Check your connection, then try again. Your projects are safe.")
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
            Button("Try again", action: retry).buttonStyle(CanonicalPrimaryButtonStyle())
        }
        .padding(24)
        .frame(maxWidth: 480, maxHeight: .infinity, alignment: .leading)
        .background(WorkspaceSurface())
    }
}

private struct ConversationEntrance: ViewModifier {
    let visible: Bool
    let order: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func body(content: Content) -> some View {
        content
            .opacity(visible ? 1 : 0)
            .offset(y: visible || reduceMotion ? 0 : -6)
            .animation(
                reduceMotion ? .easeOut(duration: 0.15) :
                    .timingCurve(0.23, 1, 0.32, 1, duration: 0.2).delay(min(Double(order) * 0.035, 0.105)),
                value: visible
            )
    }
}

private struct CreationWorkspaceView: View {
    let project: ProjectSummary
    let openProjects: () -> Void
    let openAccount: () -> Void

    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var chatDrafts: ChatDraftStore
    @EnvironmentObject private var auth: AuthStore
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    @FocusState private var composerFocused: Bool
    /// Reference-typed and observation-tracked per reader: typing re-renders only
    /// `ChatComposer`, not this whole workspace (timeline rebuild + every row).
    @State private var promptDraft = ChatPromptDraft()
    private var prompt: String {
        get { promptDraft.text }
        nonmutating set { promptDraft.set(newValue) }
    }
    private var promptBinding: Binding<String> {
        Binding(get: { promptDraft.text }, set: { promptDraft.set($0) })
    }
    /// KRI-207: requirement names for the receipt chips; loaded once a reply carries receipts.
    @State private var briefRequirements: [String: CreativeBriefRequirement] = [:]
    /// The brief loaded (or, after one retry, definitively failed): chips can show their final titles.
    @State private var briefSettled = false
    @State private var events: [ThreadEvent] = []
    @State private var initialConversationLoaded = false
    @State private var pendingMessages: [ChatPendingMessage] = []
    @State private var submissionAnchor: ChatPendingMessage?
    @State private var uploadAnchors: [UUID: ChatPendingUpload] = [:]
    @State private var nextLocalOrder = 0
    @State private var scrollRequest = 0
    @State private var finishedEditSuggestionUpdate = 0
    @State private var finishedEditSuggestionRefresh = 0
    /// Values derived from the transcript, kept between redraws (see `ChatDerivedMemo`).
    @State private var derived = ChatDerivedMemo()
    @State private var hasHistoryBaseline = false
    /// False only while the transcript on screen came from the on-disk cache and the server has not answered yet:
    /// the question cards stay read-only, because a cached "active" question may already be answered elsewhere.
    @State private var transcriptIsFresh = true
    @StateObject private var responsePresentation = ChatResponsePresentation()
    @Environment(\.scenePhase) private var scenePhase
    @ObservedObject private var reachability = NetworkReachability.shared
    @State private var pendingTurnSubmission: ChatTurnSubmissionIdentity?
    @State private var sendReceipt: ChatSendReceipt?
    @State private var approval: ApprovalSnapshot?
    /// KRI-306: the creator's pending video-shape pick for the approval / plan on screen.
    @State private var renderShapePick = RenderShapePickerState()
    @State private var approvalNotice: String?
    @State private var selectedFormat: CreationFormat?
    @State private var availableFormats: [CreationFormat] = []
    @State private var capabilities: CreationCapabilities?
    @State private var capabilitiesFailure: ChatFailure?
    /// KRI-742: built once the server advertises `voice_dictation`; nil (no mic) until then.
    @State private var dictationController: DictationController?
    private var dictationForComposer: DictationController? {
        capabilities?.voiceDictationEnabled == true ? dictationController : nil
    }
    private func dictationContextTerms() -> [String] {
        DictationController.contextTerms(title: project.title, mediaNames: CreationAttachedMedia.parse(threadState).map(\.displayName))
    }
    @State private var fullThread: CreationThread?
    @State private var pendingAction: CreationActionIdentity?
    @State private var uploadRecords: [UploadRecoveryRecord] = []
    /// Reference-typed and observed by the progress bars alone: see `UploadProgressStore`.
    @State private var uploadProgress = UploadProgressStore()
    /// What the coordinator is still preparing, and the assets the user has chosen. `uploadRecords`
    /// alone is blind to a clip until it is prepared AND reserved, so without these Continue would
    /// be enabled while chosen clips were still on their way.
    @State private var uploadInFlight: [UUID: BackgroundUploadCoordinator.InFlightUpload] = [:]
    @State private var photoSelections: [String: ProjectPhotoSelection] = [:]
    @State private var uploadFailures: [UploadFailure] = []
    @State private var previewVersion = 0
    @State private var maximumClipsByFormat: [CreationFormat: Int] = [:]
    @State private var capabilitiesAreAuthoritative = false
    /// When capabilities were last read; the editor-state capability is re-read at send time once stale
    /// so a server-side rollback of `editor_state_turns` takes effect without relaunching.
    @State private var capabilitiesReadAt: Date?
    @State private var isChoosingFormat = false
    @State private var formatResolveFailed = false
    @State private var threadState: [String: JSONValue] = [:]
    /// KRI-693: the plan item's Visuals, read only while a clip picker offers Visuals (`asset-…`) candidates.
    @State private var clipPoolVisuals: [CreationVisual] = []
    @State private var afterSequence = -1
    @State private var threadRevision: Int
    @State private var projectionOrder = ThreadProjectionOrder()
    /// Ticks once a second while the speech check runs, so it lives in a box: a plain `@State` struct
    /// would re-render this view every tick. Only the timed-out flag below is observed.
    @State private var speechCheckClock = SpeechCheckClock()
    @State private var speechCheckTimedOut = false
    @State private var ownsSendAttempt = false
    @State private var hasActivatedDraftStorage = false
    private var isSending: Bool { ownsSendAttempt || chatDrafts.isSubmitting(for: project.id) }
    /// Clip-picker answers sent from this device, by question id, for the collapsed "answered" summary.
    @State private var answeredClipSelections: [String: ClipSelectionSubmission] = [:]
    /// Conflict-choice answers sent from this device, by question id, for the collapsed "answered" summary.
    @State private var answeredChoices: [String: ChoiceSelectionSubmission] = [:]
    /// A suggestions request is in flight (or re-asking while the server says pending), so the first prompt shows
    /// its placeholders rather than nothing.
    @State private var suggestionsLoading = false
    /// KRI-802: answers held for a reply that asks a choice card and a clip picker together, by message id. Both go
    /// out on one turn once both are in.
    @State private var questionBatches: [String: QuestionBatch] = [:]
    /// The question the creator chose to answer in their own words ("Type my own"): shown above the composer
    /// while they type, and the answer blocks are disabled meanwhile.
    @State private var answeringQuestion: String?
    /// The suggestion chip whose message is on its way (it shows a spinner while the others dim).
    @State private var sendingSuggestionID: String?
    /// KRI-588: song-range questions the server has said are closed (409 `song_range_stale`), and the server's
    /// reason for refusing the last range, shown on that question's card.
    @State private var closedSongRangeQuestions: Set<String> = []
    @State private var songRangeProblem: SongRangeProblem?
    /// KRI-901: the server's reason for refusing the last mark (404 / 422), shown on that question's card.
    @State private var protectedRegionProblem: ProtectedRegionProblem?
    /// KRI-723: the server's reason for refusing the last moments answer (404 / 422), shown on that question's card.
    @State private var momentOrderProblem: MomentOrderProblem?
    @State private var isActing = false
    @State private var isThinking = false
    /// The turn request id doubles as the thought-summary lookup key. It is
    /// retained across the accepted-turn wait so a retry cannot inherit a
    /// previous attempt's successful disclosure.
    @State private var thoughtRequestID: String?
    @State private var liveThoughtSummaries: [KriaThoughtSummary] = []
    /// Server history is keyed by the user's stable request id, so a row is
    /// placed immediately after the request that produced it.
    @State private var completedThoughtSummaries: [String: [KriaThoughtSummary]] = [:]
    /// Highest transcript sequence known when the thinking turn was accepted; only later events can settle it.
    @State private var thinkingAnchor: Int?
    @State private var thinkingTurnID: String?
    /// Approvals have no reply message, so a terminal job status also ends the wait (KRI-222).
    @State private var thinkingSettlesOnJobStatus = false
    @State private var failure: ChatFailure?
    /// KRI-443: Stop on the live plan feed.
    @State private var isStoppingRender = false
    @State private var stopMessage: String?
    /// Steer now: a Steer request is in flight; earlier turns the server refused to stop (their video is being made);
    /// the plain note under the Up next row (a third message, a Steer that came too late).
    @State private var isSteering = false
    @State private var steerRefusedTurnIDs: Set<String> = []
    @State private var steerNote: String?
    /// Steer now: a message being sent while Kria works shows in the bar at once (not as a chat bubble) until the
    /// server's queued `user_message` takes over; cleared when the send ends.
    @State private var pendingWaitingText: String?
    @State private var waitingClientEventID: String?
    @State private var stopUnavailableJobID: String?
    /// The server's reason the last confirmation was rejected, shown inside the card.
    @State private var confirmationConflict: CreationConfirmationConflict?
    @State private var showsAttachments = false
    /// The creator's own re-record steps; the rest of the flow is read from uploads and the thread (`rerecordPhase`).
    @State private var rerecordLocal: RerecordPhase.Local = .none
    /// The camera for a re-record. Open independently of `rerecordLocal` so it is not torn down by the flow moving on.
    @State private var rerecordCamera: RerecordTarget?
    /// KRI-815: the script the attachment sheet shows on its voiceover step; nil = the sheet as before.
    @State private var attachmentScript: VoiceoverScript?
    @State private var attachmentLaunch: AttachmentLaunch?
    @State private var attachmentOrigin: AttachmentOrigin = .addMedia
    @State private var showsResult = false
    /// Live plan & review: the "Review your video" sheet, opened from the feed's CTA / Change or the project.
    @State private var reviewPresentation: ReviewPlanPresentation?
    @StateObject private var editorSession: NativeEditorSession
    @State private var conversationAcceptedID: UUID?
    /// Bumped when "Open editor" is tapped in the editor's own Kria sheet: the editor is already
    /// underneath, so the sheet closes instead.
    @State private var editorConversationCloseID: UUID?
    private var editorAIActivity: NativeEditorAIActivity? {
        let sending = pendingTurnSubmission?.clientEventID
            ?? chatDrafts.submission(for: project.id).flatMap { $0.phase == .accepted ? nil : $0.clientEventID }
        return NativeEditorAIActivity.current(
            projectID: project.id, isSending: isSending, sendingRequestID: sending,
            isThinking: isThinking, thinkingRequestID: thinkingTurnID ?? thoughtRequestID
        )
    }
    /// KRI-756: the server's latest suggestion chips for this chat. Only read through `suggestionChips(for:)`.
    @State private var suggestionSet: SuggestionSet?
    /// The `suggestionScopeKey` that `suggestionSet` was fetched under; a set from another scope is never drawn.
    @State private var suggestionSetKey: String?
    /// AI suggestion blocks: what each card is doing on this phone (sending, handled, stale) and the refresh nudge
    /// that the editor Save and a stale tap give the poll.
    @StateObject private var suggestionLedger = SuggestionCardLedger()
    /// The image a paid card is making after its tap, found by the id both sides derive (`GeneratedCardEvent`).
    @StateObject private var generatedCards = GeneratedCardTracker()

    init(project: ProjectSummary, openProjects: @escaping () -> Void, openAccount: @escaping () -> Void) {
        self.project = project
        self.openProjects = openProjects
        self.openAccount = openAccount
        _threadRevision = State(initialValue: project.serverRevision)
        _editorSession = StateObject(wrappedValue: NativeEditorSession(project: project))
    }

    private var currentProject: ProjectSummary {
        model.projects.first(where: { $0.id == project.id }) ?? project
    }

    /// Why the composer's + cannot add media right now, in the words its tray shows. Nil = it can.
    private var attachBlockedReason: String? {
        if selectedFormat == nil { return "Choose a video format first." }
        if selectedFormat == .slides, !hasDedicatedSlideWorkspace { return "This post is still being set up." }
        if isSending || isActing || isThinking { return "Kria is replying. Add media in a moment." }
        if currentProject.status == .rendering { return "Your video is rendering. Add media once it’s ready." }
        return nil
    }

    private var canAttachMedia: Bool { attachBlockedReason == nil }

    private var rerecordFacts: RerecordPhase.Facts {
        let swapping = uploadRecords.contains { $0.projectID == project.id && $0.replacesMediaID != nil && $0.uploadFailed != true }
            || uploadInFlight.values.contains { $0.projectID == project.id && $0.replacesMediaID != nil }
        let failure = uploadFailures.first { $0.projectID == project.id && $0.replacesMediaID != nil }
        let offer: RerecordOffer = workspaceStage == .ready && currentProject.status == .ready && !isSending && !isActing && !isThinking
            ? .resolve(thread: fullThread) : .none
        return RerecordPhase.Facts(
            offer: offer, replaceInFlight: swapping,
            failure: failure.map { RerecordFailure(code: nil, message: $0.message, retryable: $0.cause == .uploadFailed) },
            debt: RerecordPlanDebt.owed(state: threadState, activeJobID: fullThread?.activeJobID ?? currentProject.activeJobID?.uuidString, events: events)
        )
    }

    private var rerecordPhase: RerecordPhase { .resolve(local: rerecordLocal, facts: rerecordFacts) }

    private var rerecordActions: RerecordActions {
        RerecordActions(
            start: {
                switch rerecordFacts.offer {
                case .none: break
                case let .one(target): rerecordLocal = .confirming([target])
                case let .choose(targets): rerecordLocal = .confirming(targets)
                }
            },
            retry: { if let id = rerecordFacts.failureRecordID(in: uploadFailures, project: project.id) { Task { await model.uploads.retryUpload(recordID: id) } } },
            dismissFailure: {
                guard let id = rerecordFacts.failureRecordID(in: uploadFailures, project: project.id) else { return }
                Task { await model.uploads.cancel(recordID: id); model.uploads.dismissFailure(id: id) }
            },
            makeAgain: { if let debt = rerecordFacts.debt { Task { await sendRerecordPlan(debt) } } }
        )
    }

    /// The fresh-plan turn the swap owes. A failed send leaves the swap owed, so the button is still there to retry.
    private func sendRerecordPlan(_ debt: SourceReplacement) async {
        rerecordLocal = .planning(debt)
        for _ in 0..<30 where isSending || isActing || isThinking || pendingUploadCount > 0 { try? await Task.sleep(for: .milliseconds(100)) }
        // `send` returns silently while a turn runs or an earlier receipt is pending; staying `.planning` then would
        // hide the Make it again button with nothing left to send the turn.
        guard !isSending, !isActing, !isThinking, sendReceipt == nil else { rerecordLocal = .none; return }
        await send(message: RerecordPlanDebt.message, keepsDraft: true)
        if rerecordFacts.debt == debt, !isThinking, sendReceipt == nil { rerecordLocal = .none }
    }

    private func rerecordCameraClosed() {
        // Nothing was handed over: the creator backed out, so this session has no swap to follow.
        if case .swapping = rerecordLocal, !rerecordFacts.replaceInFlight, rerecordFacts.debt == nil { rerecordLocal = .none }
    }

    private func rerecordDebtChanged(_ debt: SourceReplacement?) {
        guard let debt else { return }
        if case .swapping = rerecordLocal { Task { await sendRerecordPlan(debt) } }
    }

    private var rerecordConfirmation: Binding<Bool> {
        Binding(
            get: { if case .confirming = rerecordLocal { true } else { false } },
            set: { if !$0, case .confirming = rerecordLocal { rerecordLocal = .none } }
        )
    }


    private var readyMediaCount: Int {
        guard workspaceStage == .footage else { return 0 }
        // A slide post is composed from the PlanItemAsset Visuals pool. The
        // primary clip list is intentionally irrelevant: the server rejects it
        // for this format, and counting it here would enable a misleading send.
        if selectedFormat == .slides { return fullThread?.readyVisualCount ?? 0 }
        if attachedClipCount > 0 { return attachedClipCount }
        let destination = ProjectUploadDestination.resolve(
            capabilities: capabilities?.phoneRendering, creationMode: capabilities?.creationMode, capabilitiesLoaded: capabilitiesAreAuthoritative,
            sourcePurposes: [], role: .visual
        )
        return selectedFormat?.runsAsMontage == true && destination.visualKinds != nil ? fullThread?.deviceReadyVisualCount ?? 0 : 0
    }

    private var activeProposalEvent: ThreadEvent? {
        if let approval {
            return events.last { $0.eventType == "draft_applied" && $0.payload?["turn_id"]?.stringValue == approval.turnID }
        }
        guard let thread = fullThread else { return nil }
        let planHash = thread.creatorAgent?["plan_hash"]?.stringValue
        return events.last {
            ["assistant_strategy", "agent_assistant_strategy"].contains($0.eventType)
                && (planHash == nil || $0.payload?["plan_hash"]?.stringValue == planHash)
        }
    }

    /// A stage belongs beside the event that introduced it, not below every
    /// subsequent message. This is also stable when a poll arrives during Send.
    private var stageAnchor: ChatTimelineStageAnchor? {
        guard fullThread != nil || isUITesting else { return nil }
        let anchor: ThreadEvent?
        switch workspaceStage {
        case .format:
            anchor = events.last { $0.eventType == "format_prompt" }
        case .footage:
            anchor = events.last { ["action_select_format", "media_prompt", "upload_prompt"].contains($0.eventType) }
        case .direction:
            anchor = activeProposalEvent ?? events.last { $0.eventType == "approval_requested" }
        case .rendering:
            anchor = events.last { ["action_generate", "action_retry", "agent_assistant_execution", "render_started", "generation_started", "render_queued"].contains($0.eventType) }
        case .ready:
            anchor = events.last { ["agent_assistant_review", "assistant_review", "generation_ready"].contains($0.eventType) }
        case .failed:
            anchor = events.last { ["agent_assistant_error", "assistant_error", "agent_assistant_render_failed", "assistant_render_failed", "generation_failed"].contains($0.eventType) }
        }
        return ChatTimelineStageAnchor(id: "stage-\(workspaceStage)", afterSequence: anchor?.sequence ?? afterSequence)
    }

    private var suppressedTimelineMessages: Set<String> {
        // Steer now: the waiting message lives in the bar, and a message the creator took back is gone.
        let waiting = steerNowEnabled ? SteerNowPolicy.hiddenMessageEventIDs(in: events) : []
        if workspaceStage == .format, let event = events.last(where: { $0.eventType == "format_prompt" }) {
            return waiting.union([event.id])
        }
        if workspaceStage == .direction, let event = activeProposalEvent { return waiting.union([event.id]) }
        return waiting
    }

    private var activeUploadIDs: Set<UUID> {
        var ids = Set(BackgroundUploadCoordinator.inProgressRecords(uploadRecords).filter { $0.projectID == project.id }.map(\.id))
        ids.formUnion(uploadInFlight.filter { $0.value.projectID == project.id }.keys)
        if let selection = photoSelections[project.id.uuidString] {
            ids.formUnion(selection.entries.values.filter { $0.mediaID == nil }.map(\.recordID))
        }
        return ids
    }

    /// The merged, sorted, parsed transcript. Building it parses every event, and one redraw reads it several times
    /// (the body, the scroll token, each question card), so the result is kept until one of its inputs changes.
    private var timeline: [ChatTimelineEntry] {
        let stage = stageAnchor
        let suppressed = suppressedTimelineMessages
        let uploads = activeUploadIDs.compactMap { uploadAnchors[$0] }
        // A pending message that carries a song answer also changes how its card reads: always rebuild for those.
        let cacheable = !pendingMessages.contains { $0.songOrder != nil || $0.songRange != nil || $0.protectedRegion != nil || $0.momentOrder != nil }
        var hasher = Hasher()
        hasher.combine(ChatFingerprint.events(events))
        for message in pendingMessages {
            hasher.combine(message.id); hasher.combine(message.content)
            hasher.combine(message.afterSequence); hasher.combine(message.localOrder)
        }
        hasher.combine(stage?.id); hasher.combine(stage?.afterSequence)
        hasher.combine(waitingClientEventID)
        for id in suppressed.sorted() { hasher.combine(id) }
        for upload in uploads { hasher.combine(upload.id); hasher.combine(upload.afterSequence); hasher.combine(upload.localOrder) }
        for requestID in completedThoughtSummaries.keys.sorted() {
            hasher.combine(requestID)
            for summary in completedThoughtSummaries[requestID] ?? [] {
                // KRI-1239: never the text itself. A finished thought's text is final, and hashing it (tens of KB of
                // non-ASCII prose, normalised on every read) on every row of every redraw froze the main thread
                // past the iOS watchdog. Its length (O(1) for native strings) still marks a changed record.
                hasher.combine(summary.id); hasher.combine(summary.status.rawValue)
                hasher.combine(summary.text.utf8.count); hasher.combine(summary.durationMS)
            }
        }
        let key = hasher.finalize()
        if cacheable, derived.timelineKey == key { return derived.timeline }
        Perf.event("timeline.build")
        let built = ChatTimeline.build(
            events: events, pending: pendingMessages.filter { $0.clientEventID != waitingClientEventID }, stage: stage,
            suppressedMessageIDs: suppressed, uploads: uploads, thoughtSummaries: completedThoughtSummaries
        )
        derived.timelineKey = cacheable ? key : nil
        derived.timeline = built
        derived.timelineMessages = nil
        derived.timelineRows = nil
        return built
    }

    /// The timeline's messages in order. Each question card asks for them on every redraw, so they are kept with the
    /// timeline they came from instead of being filtered out again per row.
    private var timelineMessages: [ChatTranscriptMessage] {
        let entries = timeline
        if let cached = derived.timelineMessages, derived.timelineKey != nil { return cached }
        let messages = entries.compactMap { entry -> ChatTranscriptMessage? in
            if case .message(let message) = entry.content { message } else { nil }
        }
        if derived.timelineKey != nil { derived.timelineMessages = messages }
        return messages
    }

    /// The timeline as the lazy stack's rows, kept with the timeline it came from (each media receipt is parsed to
    /// count its files).
    private var timelineRows: [ChatTimelineRow] {
        let entries = timeline
        if let cached = derived.timelineRows, derived.timelineKey != nil { return cached }
        let rows = ChatTimelineRow.rows(entries)
        if derived.timelineKey != nil { derived.timelineRows = rows }
        return rows
    }

    /// The newest reply that carries requirement receipts; a new one means the brief may have changed.
    private var latestReceiptEventID: String? {
        events.last { $0.payload?["requirement_receipts"] != nil }?.id
    }

    /// The newest assistant event that carries a clip picker, if any.
    private var latestClipQuestionID: String? {
        let key = ChatFingerprint.events(events)
        if derived.clipQuestionKey == key { return derived.clipQuestionID }
        let found = events.last { ChatTranscriptMessage.from(event: $0)?.clipQuestion != nil }?.id
        derived.clipQuestionKey = key
        derived.clipQuestionID = found
        return found
    }

    /// KRI-443: the live plan feed, reduced from the thread's `plan_block` events.
    private var planFeed: PlanBlockFeedState {
        let key = ChatFingerprint.events(events)
        if derived.feedKey == key, let feed = derived.feed { return feed }
        Perf.event("planFeed.reduce")
        let reduced = PlanBlockFeedState.reduce(events: events)
        derived.feedKey = key
        derived.feed = reduced
        return reduced
    }

    private var showsPlanFeed: Bool {
        PlanFeedVisibility.shows(
            capabilityEnabled: capabilities?.livePlanReviewEnabled,
            runtimeVersion: currentProject.runtimeVersion,
            feed: planFeed,
            activeJobID: fullThread?.activeJobID ?? currentProject.activeJobID?.uuidString
        )
    }

    /// Where this iPhone's own build of the video has got to, when the render runs on the device. With no
    /// presentation yet the device is about to prepare, so it reads as `.preparing` (the server's up-front
    /// "all decided" must not show first). Cloud renders return nil and the feed shows the server's states.
    private var deviceBuildStage: DeviceBuildStage? {
        guard let key = deviceRenderKey else { return nil }
        guard let presentation = model.deviceRenders.presentations[key] else { return .preparing }
        return DeviceBuildStage(phase: presentation.phase, exportProgress: presentation.exportProgress)
    }

    /// Finished exports use the status card so sync retries keep playback available.
    private var feedHostsDeviceBuild: Bool {
        guard let key = deviceRenderKey, showsPlanFeed else { return false }
        guard let presentation = model.deviceRenders.presentations[key] else { return true }
        guard let stage = deviceBuildStage, stage != .finished else { return false }
        guard !presentation.outputVerificationPending, presentation.repairAttempt == nil else { return false }
        // A quiet network wait stays in the feed; heat, storage and unknown states need the card.
        return presentation.message == nil && !presentation.requiresServerRetry && presentation.waiting?.isQuiet != false
    }

    @ViewBuilder private var planFeedView: some View {
        let serverFeed = planFeed
        let feed = serverFeed.paced(by: deviceBuildStage)
        let device = deviceRenderKey
        let devicePhase = device.flatMap { model.deviceRenders.presentations[$0]?.phase } ?? .preparing
        PlanBlockFeed(
            feed: feed,
            canStop: device != nil
                ? [.preparing, .rendering].contains(devicePhase)
                : feed.turnID != nil && stopUnavailableJobID != feed.jobID,
            isStopping: isStoppingRender,
            stopMessage: stopMessage,
            stop: stopRender,
            // The Change pills exist only when the server speaks live-plan contract v2.
            openReview: capabilities?.livePlanReviewAvailable == true ? { openPlanReview(section: $0) } : nil,
            // The server's plan, not the device-paced one: an iPhone build holds `feed` incomplete while it builds.
            phase: serverFeed.phase(workspaceReady: currentProject.status == .ready),
            buildStage: deviceBuildStage
        )
        .id("plan-feed")
    }

    /// Opens the Review sheet (KRI-440). `section` is the card whose Change was tapped. The sheet starts from the
    /// feed's blocks so it shows at once, then loads the snapshot.
    private func openPlanReview(section: PlanSectionID) {
        let seed = planFeed.isEmpty ? [] : planFeed.paced(by: deviceBuildStage).blocks
        reviewPresentation = ReviewPlanPresentation(seed: seed, initialFlag: section)
    }

    private func reviewActions() -> ReviewPlanActions {
        let api = model.api
        let threadID = project.id
        let account = ReviewAccountScope(uiOwner: auth.localStorageIdentity, transportOwner: api.mediaLibraryOwner)
        let environment = (api as? KriaAPI)?.baseURL
        let checkScope: @MainActor () throws -> Void = {
            guard let account, reviewScopeIsCurrent(threadID: threadID, account: account, environment: environment) else { throw CancellationError() }
        }
        return ReviewPlanActions(
            loadSnapshot: {
                guard let account, reviewScopeIsCurrent(threadID: threadID, account: account, environment: environment) else { throw CancellationError() }
                let snapshot = try await api.planSnapshot(threadID: threadID, expectedOwner: account.transportOwner)
                guard reviewScopeIsCurrent(threadID: threadID, account: account, environment: environment) else { throw CancellationError() }
                return snapshot
            },
            update: { scope, edits, message in
                guard let account, reviewScopeIsCurrent(threadID: threadID, account: account, environment: environment) else { throw CancellationError() }
                try await submitReviewTurn(scope: scope, edits: edits, message: message, expectedOwner: account.transportOwner, checkScope: checkScope)
            },
            undoSection: { section, blockRevision, draftRevision in
                guard let account, reviewScopeIsCurrent(threadID: threadID, account: account, environment: environment) else { throw CancellationError() }
                return try await undoReviewSection(section, blockRevision: blockRevision, draftRevision: draftRevision, expectedOwner: account.transportOwner, checkScope: checkScope)
            },
            undoAll: { draftRevision in
                guard let account, reviewScopeIsCurrent(threadID: threadID, account: account, environment: environment) else { throw CancellationError() }
                try await undoAllReview(draftRevision: draftRevision, expectedOwner: account.transportOwner, checkScope: checkScope)
            }
        )
    }

    private func reviewScopeIsCurrent(threadID: UUID, account: ReviewAccountScope?, environment: URL?) -> Bool {
        model.selectedProject?.id == threadID && project.id == threadID && account?.matches(uiOwner: auth.localStorageIdentity, transportOwner: model.api.mediaLibraryOwner) == true && (model.api as? KriaAPI)?.baseURL == environment
    }

    private var reviewScopeToken: String {
        "\(project.id):\(auth.localStorageIdentity ?? "signed-out"):\(model.api.mediaLibraryOwner ?? "no-token"):\((model.api as? KriaAPI)?.baseURL.absoluteString ?? "fixture")"
    }

    /// "Update video": a turn scoped to the flagged sections (planned as editor operations only), then the
    /// approval it produces is approved at once. Tapping Update video is the consent.
    private func submitReviewTurn(scope: [PlanSectionID], edits: [ManualPlanEdit], message: String, expectedOwner: String, checkScope: @escaping @MainActor () throws -> Void) async throws {
        let threadID = project.id
        try checkScope()
        _ = try? await refreshDelta()
        try checkScope()
        let startSequence = afterSequence
        let accepted = try await model.api.submitScopedTurn(
            threadID: threadID, message: message, expectedRevision: threadRevision,
            clientEventID: UUID().uuidString, scope: scope, manualEdits: edits, expectedOwner: expectedOwner
        )
        try checkScope()
        guard project.id == threadID, let identity = ReviewApprovalIdentity(threadID: threadID, turnID: accepted.turnID) else { throw ReviewPlanError.timedOut }
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: accepted.threadRevision)
        try await approveReviewDraft(identity: identity, afterSequence: startSequence, expectedOwner: expectedOwner, checkScope: checkScope)
    }

    private func undoReviewSection(_ section: PlanSectionID, blockRevision: Int, draftRevision: Int, expectedOwner: String, checkScope: @escaping @MainActor () throws -> Void) async throws -> Bool {
        let threadID = project.id
        try checkScope()
        _ = try? await refreshDelta()
        try checkScope()
        let startSequence = afterSequence
        let result = try await model.api.undoPlanSection(
            threadID: threadID, sectionID: section, expectedThreadRevision: threadRevision,
            expectedBlockRevision: blockRevision, expectedDraftRevision: draftRevision, expectedOwner: expectedOwner
        )
        try checkScope()
        guard project.id == threadID else { throw ReviewPlanError.timedOut }
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: result.threadRevision)
        // A null successor turn means nothing was queued to render: there is no approval to wait for.
        guard let turnID = result.turnID else { return false }
        guard let identity = ReviewApprovalIdentity(threadID: threadID, turnID: turnID) else { throw ReviewPlanError.timedOut }
        try await approveReviewDraft(identity: identity, afterSequence: startSequence, expectedOwner: expectedOwner, checkScope: checkScope)
        return true
    }

    private func undoAllReview(draftRevision: Int, expectedOwner: String, checkScope: @escaping @MainActor () throws -> Void) async throws {
        let threadID = project.id
        try checkScope()
        _ = try? await refreshDelta()
        try checkScope()
        let startSequence = afterSequence
        let restored = try await model.api.undoDraft(threadID: threadID, expectedRevision: draftRevision, render: true, expectedOwner: expectedOwner)
        try checkScope()
        guard project.id == threadID,
              let identity = ReviewApprovalIdentity(threadID: threadID, draftID: restored.draftID, draftRevision: restored.draftRevision) else { throw ReviewPlanError.timedOut }
        try await approveReviewDraft(identity: identity, afterSequence: startSequence, expectedOwner: expectedOwner, checkScope: checkScope)
    }

    /// Waits for the exact review turn or restored draft's approval after `startSequence`.
    /// A reply that is not an approval (Kria declined or found nothing to
    /// change) ends the wait with its text.
    private func approveReviewDraft(identity: ReviewApprovalIdentity, afterSequence startSequence: Int, expectedOwner: String, checkScope: @escaping @MainActor () throws -> Void) async throws {
        var repliesWithoutApproval = 0
        for _ in 0..<150 {
            try checkScope()
            _ = try? await refreshDelta()
            try checkScope()
            guard project.id == identity.threadID else { throw ReviewPlanError.timedOut }
            let fresh = events.filter { $0.sequence > startSequence }
            if let selected = fresh.compactMap({ identity.select($0, threadID: project.id) }).last {
                try await approveReviewApproval(selected, expectedOwner: expectedOwner, checkScope: checkScope)
                return
            }
            let reply = fresh.last {
                $0.role == "assistant" && $0.eventType.hasPrefix("assistant")
                    && identity.matchesReply($0, threadID: project.id)
            }
            if let reply {
                repliesWithoutApproval += 1
                if repliesWithoutApproval > 8 { throw ReviewPlanError.declined(reply.content ?? "") }
            }
            try await Task.sleep(for: .milliseconds(400))
        }
        throw ReviewPlanError.timedOut
    }

    private func approveReviewApproval(_ selected: ReviewApprovalIdentity.Selection, expectedOwner: String, checkScope: @escaping @MainActor () throws -> Void) async throws {
        try checkScope()
        let approvalID = selected.approvalID
        let snapshot = try await model.api.approval(threadID: selected.threadID, approvalID: approvalID, expectedOwner: expectedOwner)
        try checkScope()
        guard selected.matches(snapshot, threadID: project.id), snapshot.status == "pending", let draftRevision = snapshot.draftRevision else { throw ReviewPlanError.timedOut }
        func decide() async throws {
            try checkScope()
            guard selected.matches(snapshot, threadID: project.id) else { throw ReviewPlanError.timedOut }
            try await model.api.decideReviewApproval(
                threadID: project.id, approvalID: approvalID,
                expectedThreadRevision: threadRevision, expectedDraftRevision: draftRevision,
                fingerprint: snapshot.approvalFingerprint, expectedOwner: expectedOwner
            )
        }
        do {
            try await decide()
        } catch let error as APIError where error.isConflict {
            // The revision moved (every plan_block event bumps it): take the newest and try once more.
            try checkScope()
            _ = try? await refreshDelta()
            try checkScope()
            try await decide()
        }
        try checkScope()
        approval = nil
        thinkingAnchor = afterSequence
        thinkingTurnID = snapshot.turnID
        thinkingSettlesOnJobStatus = true
        isThinking = true
        try checkScope()
        _ = try? await refreshDelta()
        try checkScope()
    }


    /// Cancels the render behind the feed. Always sends the newest thread revision: every `plan_block`
    /// event bumps it, so the revision the feed was drawn with is usually stale.
    private func stopRender() {
        if let key = deviceRenderKey {
            // A device render is not a server job: the server cancel route answers 409 for it. Stop it the
            // way the status card does, which also tells the server the creator stopped it.
            guard !isStoppingRender else { return }
            isStoppingRender = true
            Task {
                await model.deviceRenders.cancel(key)
                isStoppingRender = false
            }
            return
        }
        let feed = planFeed
        guard let turnID = feed.turnID, !isStoppingRender else { return }
        isStoppingRender = true
        stopMessage = nil
        Task {
            defer { isStoppingRender = false }
            func hideStop() {
                stopUnavailableJobID = feed.jobID
                stopMessage = "This render can’t be stopped any more."
            }
            do {
                _ = try await stopRenderRequest(turnID: turnID)
                _ = try? await refreshDelta()
                await refreshNow()
            } catch let error as APIError where error.isConflict {
                if error.conflictCode == "turn_not_cancellable" || (error.conflictDetail ?? "").contains("turn_not_cancellable") {
                    hideStop()
                } else {
                    // A stale revision: take the latest from a fresh delta and try once more.
                    _ = try? await refreshDelta()
                    do {
                        _ = try await stopRenderRequest(turnID: turnID)
                        _ = try? await refreshDelta()
                        await refreshNow()
                    } catch let retry as APIError where retry.isConflict {
                        hideStop()
                    } catch {
                        failure = ChatFailure("Kria couldn’t stop the render.", error: error)
                    }
                }
            } catch {
                failure = ChatFailure("Kria couldn’t stop the render.", error: error)
            }
        }
    }

    private func stopRenderRequest(turnID: String) async throws -> TurnCancelled {
        try await model.api.cancelRender(threadID: project.id, turnID: turnID, revision: threadRevision)
    }

    // MARK: Steer now

    private var steerNowEnabled: Bool { capabilities?.steerNowEnabled == true && currentProject.runtimeVersion == 2 }
    private var upNext: SteerNowPolicy.Waiting? { steerNowEnabled ? SteerNowPolicy.waiting(in: events) : nil }
    private var steerStrings: SteerStrings { SteerStrings(ClipAnswerLanguage(threadState: threadState)) }

    /// Kria is planning, its card is up, or a video is being made: a typed message now waits.
    private var kriaIsBusy: Bool { isThinking || approval != nil || workspaceStage == .rendering }

    /// Steer now: the bar on top of the input box. Tucked 13 pt under the composer's top edge (its outer padding is 12)
    /// and inset 12 pt from its sides, like Codex's bar.
    @ViewBuilder private var waitingBar: some View {
        if steerNowEnabled, let waiting = upNext {
            WaitingMessageBar(
                text: waiting.text, strings: steerStrings, canSteer: canSteer(waiting), isBusy: isSteering, note: steerNote,
                steer: { steerNow(waiting) }, remove: { removeWaiting(waiting, edit: false) }, edit: { removeWaiting(waiting, edit: true) }
            )
            .padding(.horizontal, 26).padding(.bottom, -13)
        } else if steerNowEnabled, let text = pendingWaitingText {
            WaitingMessageBar(text: text, strings: steerStrings, canSteer: false, isBusy: true, note: nil,
                              steer: {}, remove: {}, edit: {})
                .padding(.horizontal, 26).padding(.bottom, -13)
        }
    }

    /// The trash and Edit: cancel the waiting turn only while it still waits (the server checks that atomically). The
    /// server marks it so Kria never reads it; Edit then puts the words back in the box (ahead of anything already typed
    /// there). A message Kria already picked up is never stopped: the bar says so and keeps the words out of the box.
    private func removeWaiting(_ waiting: SteerNowPolicy.Waiting, edit: Bool) {
        guard !isSteering else { return }
        isSteering = true
        steerNote = nil
        Task {
            defer { isSteering = false }
            for attempt in 0..<2 {
                do {
                    let removed = try await model.api.removeWaitingMessage(threadID: project.id, turnID: waiting.turnID, revision: threadRevision)
                    threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: removed.threadRevision)
                    if edit {
                        let typed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
                        prompt = typed.isEmpty ? waiting.text : waiting.text + " " + typed
                        composerFocused = true
                    }
                    _ = try? await refreshDelta()
                    return
                } catch let error as APIError where error.isConflict {
                    if error.conflictCode == "turn_no_longer_queued" || error.conflictCode == "turn_not_cancellable" {
                        steerNote = steerStrings.alreadyStarted
                        await refreshNow()
                        return
                    }
                    guard attempt == 0 else {
                        steerNote = steerStrings.couldNotRemove
                        await refreshNow()
                        return
                    }
                    // The project moved on since the last read: take the latest and decide again before the second try.
                    // Kria may have picked the message up in between; then it is not tried again.
                    _ = try? await refreshDelta()
                    switch SteerNowPolicy.removalRetry(for: waiting, in: events) {
                    case .retry:
                        continue
                    case .alreadyStarted:
                        steerNote = steerStrings.alreadyStarted
                        return
                    case .alreadyRemoved:
                        // Gone already (removed elsewhere): Edit still gets its words back.
                        if edit {
                            let typed = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
                            prompt = typed.isEmpty ? waiting.text : waiting.text + " " + typed
                            composerFocused = true
                        }
                        return
                    }
                } catch {
                    steerNote = steerStrings.couldNotRemove
                    return
                }
            }
        }
    }

    private func canSteer(_ waiting: SteerNowPolicy.Waiting) -> Bool {
        guard let behind = waiting.behindTurnID, !steerRefusedTurnIDs.contains(behind) else { return false }
        return SteerNowPolicy.canSteer(waiting, events: events)
    }

    /// Stops the turn Kria is working on so the waiting message runs now. A stale revision (a worker event landed
    /// since the last read) refreshes and tries once more; a turn whose video already started keeps its message
    /// waiting and says so; a turn that already finished just refreshes.
    private func steerNow(_ waiting: SteerNowPolicy.Waiting) {
        guard let behind = waiting.behindTurnID, !isSteering else { return }
        isSteering = true
        steerNote = nil
        Task {
            defer { isSteering = false }
            for attempt in 0..<2 {
                do {
                    let stopped = try await model.api.steerNow(threadID: project.id, turnID: behind, revision: threadRevision)
                    threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: stopped.threadRevision)
                    // Kria works on the waiting message now: it gets the Thinking row until its own reply lands.
                    thinkingAnchor = afterSequence
                    thinkingTurnID = waiting.turnID
                    thinkingSettlesOnJobStatus = false
                    isThinking = true
                    _ = try? await refreshDelta()
                    settleThinking()
                    return
                } catch let error as APIError where error.isConflict {
                    switch error.conflictCode {
                    case "turn_not_cancellable":
                        steerRefusedTurnIDs.insert(behind)
                        steerNote = steerStrings.videoAlreadyStarted
                        await refreshNow()
                        return
                    case "nothing_to_steer_to":
                        steerRefusedTurnIDs.insert(behind)
                        await refreshNow()
                        return
                    default:
                        // The project moved on since the last read: take the latest and try once more.
                        guard attempt == 0 else {
                            steerNote = steerStrings.couldNotSwitch
                            await refreshNow()
                            return
                        }
                        _ = try? await refreshDelta()
                    }
                } catch {
                    steerNote = steerStrings.couldNotSwitch
                    return
                }
            }
        }
    }

    private var timelineUpdateToken: String {
        let feed = planFeed.paced(by: deviceBuildStage)
        let readyCardsToken: String = readyCardsPresentation?.cards.map(\.id).joined(separator: ",") ?? ""
        let liveThoughtToken: String = liveThoughtSummaries.map { "\($0.id):\($0.status.rawValue):\($0.text.utf8.count)" }.joined(separator: "|")
        let timelineIDs: String = timeline.map(\.id).joined(separator: "|")
        let failureMessage: String = failure?.message ?? ""
        let upNextTurnID: String = upNext?.turnID ?? ""
        let steerNoteText: String = steerNote ?? ""
        // KRI-769: the after-render chips arrive after the card, below the fold; a reader at the bottom follows them in.
        let readyChipIDs: String = suggestionChips(for: .ready).map(\.id).joined(separator: ",")
        // Separate parts keep this within the compiler's type-check time limit on CI.
        let parts: [String] = [
            timelineIDs,
            "\(isThinking)",
            "\(isSending)",
            failureMessage,
            "upNext\(upNextTurnID)/\(steerNoteText)",
            "feed\(feed.decidedCount)/\(feed.totalCount)",
            "thoughts\(liveThoughtToken)",
            "finishedEdits\(finishedEditSuggestionUpdate)",
            "readyChips\(readyChipIDs)",
            "readyCards\(readyCardsToken)",
        ]
        return parts.joined(separator: "|")
    }

    private var streamingThoughtSummaries: [KriaThoughtSummary] {
        liveThoughtSummaries.filter { $0.status == .streaming && $0.hasText }
    }

    private var hasCompletedThoughtForActiveRequest: Bool {
        guard let requestID = thoughtRequestID else { return false }
        return !(completedThoughtSummaries[requestID] ?? []).isEmpty
    }

    @ViewBuilder private var submissionStatus: some View {
        if let receipt = sendReceipt, receipt.phase != .accepted {
            HStack {
                Text(isSending ? "Sending your message…" : "Message not confirmed")
                    .accessibilityIdentifier("chat-submission-status")
                if !isSending {
                    Button { Task { await send(retrying: receipt) } } label: {
                        Text("Retry message").frame(minHeight: 44).contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("chat-retry-message")
                }
            }
            .font(KriaFont.body(13))
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, 44)
        }
    }

    @ViewBuilder private var conversationContent: some View {
        // The video type leads the transcript while footage is being chosen, so the timeline reads in order.
        if workspaceStage == .footage, let selectedFormat {
            VideoTypeRow(format: selectedFormat) { isChoosingFormat = true }
        }
        ForEach(timelineRows) { row in
            if let index = row.mediaIndex {
                mediaReceipt(row.entry, index: index)
                    .padding(.top, row.joinsMediaAbove ? ChatConversationSpacing.mediaStrip - ChatConversationSpacing.row : 0)
            } else {
                timelineRow(row.entry)
                    .transition(Self.rowTransition(for: row.entry, reduceMotion: reduceMotion))
            }
        }
        if currentProject.status == .ready {
            FinishedEditSuggestionsView(api: model.api, refreshToken: "\(currentProject.serverRevision):\(finishedEditSuggestionRefresh)") {
                finishedEditSuggestionUpdate += 1
            }
        }
        submissionStatus
        if isThinking && workspaceStage != .rendering {
            ThoughtSummaryDisclosure(summaries: streamingThoughtSummaries)
                .id("thought-summary")
            if streamingThoughtSummaries.isEmpty && !hasCompletedThoughtForActiveRequest { ThinkingRow().id("thinking") }
        }
        if let approvalNotice { Text(approvalNotice).font(KriaFont.body(13)) }
        if let failure {
            RecoveryCard(failure: failure) { Task { await refreshCapabilities(); await refreshNow() } }
                .id("recovery")
        }
    }

    /// A sent message rises out of the composer like iMessage; every other row keeps its
    /// existing (instant) insertion. Removal is instant so the pending → confirmed swap
    /// never replays the motion.
    private static func rowTransition(for entry: ChatTimelineEntry, reduceMotion: Bool) -> AnyTransition {
        guard case .message(let message) = entry.content, message.role == .user else { return .identity }
        if reduceMotion { return .asymmetric(insertion: .opacity, removal: .identity) }
        return .asymmetric(
            insertion: .scale(scale: 0.86, anchor: .bottomTrailing)
                .combined(with: .offset(y: 28))
                .combined(with: .opacity),
            removal: .identity
        )
    }

    private func messageRow(_ message: ChatTranscriptMessage) -> some View {
        ChatMessageRow(message: message, onSelectOption: { option in Task { await send(message: option) } },
                       responseStartedAt: responsePresentation.startTime(for: message.id),
                       requirements: briefRequirements,
                       briefSettled: briefSettled,
                       onCorrectGuess: { label in
                           // One tap: the sentence is started (after whatever the creator already
                           // typed), the keyboard is up, and only the right name is left to type.
                           prompt = label.correctionDraft(appendingTo: prompt)
                           composerFocused = true
                       },
                       clipSelectionMode: clipSelectionMode(for: message),
                       clipSelectionMedia: clipSelectionMedia(for: message),
                       clipSelectionLanguage: ClipAnswerLanguage(threadState: threadState),
                       songOrderMode: songOrderMode(for: message),
                       songOrderMedia: CreationAttachedMedia.parse(threadState),
                       songTimeline: songTimelineConfiguration,
                       songDurationS: CreationAttachedMedia.song(threadState)?.durationS,
                       projectID: project.id,
                       choiceQuestionMode: choiceQuestionMode(for: message),
                       hidesAnswerEcho: echoesChoiceAnswer(message),
                       questionBatch: asksTogether(message) ? questionBatches[message.id] ?? QuestionBatch() : nil,
                       songRangeMode: songRangeMode(for: message),
                       objectMarkMode: objectMarkMode(for: message),
                       momentOrderMode: momentOrderMode(for: message),
                       replyOptionsMode: replyOptionsMode(for: message),
                       voiceoverScriptMode: voiceoverScriptMode(for: message),
                       answerOwn: { startAnsweringOwn(message) },
                       isAnsweringOwn: answeringQuestion != nil && composerFocused,
                       changeHeldChoice: { questionBatches[message.id]?.releaseChoice() })
    }

    @ViewBuilder private func timelineRow(_ entry: ChatTimelineEntry) -> some View {
        let _ = Perf.event("chat.rowBuilt")
        switch entry.content {
        case .message(let message):
            messageRow(message)
                .id(entry.id)
            if let operationID = message.memoryOperationID {
                SavedTextPreferenceUndo(operationID: operationID, api: model.api)
            }
        case .stage:
            stageContent.id(entry.id)
        case .pendingUpload(let recordID):
            KriaMediaRow(name: uploadRecords.first { $0.id == recordID }.map { BackgroundUploadCoordinator.displayFilename($0.filename) }
                            ?? uploadInFlight[recordID]?.filename ?? "Preparing media…",
                         status: uploadRecords.contains { $0.id == recordID } ? .uploading(uploadProgress.values[recordID] ?? 0) : .preparing,
                         onFill: true) {
                CreationRecordThumbnail(recordID: recordID, version: previewVersion)
            }
            .accessibilityIdentifier("chat-upload-\(recordID.uuidString)")
            .id(entry.id)
        case .thoughts(let summaries):
            ThoughtSummaryDisclosure(summaries: summaries).id(entry.id)
        case .media:
            EmptyView() // Drawn one file per row by `mediaReceipt`.
        }
    }

    /// Chat media plus the Visuals photos/videos the question offers, appended in candidate order so the picker
    /// numbers them exactly as before (KRI-693: they were "Clip 9" with no thumbnail).
    private func clipSelectionMedia(for message: ChatTranscriptMessage) -> [CreationAttachedMedia] {
        let chatMedia = CreationAttachedMedia.parse(threadState)
        guard let question = message.clipQuestion, !clipPoolVisuals.isEmpty else { return chatMedia }
        return chatMedia + ClipPoolMedia.media(for: question, pool: clipPoolVisuals)
    }

    /// Reads the plan item's Visuals once per question that offers pool candidates. A failed read keeps the
    /// server's photo names with a placeholder tile.
    private func loadClipPoolVisuals() async {
        guard let eventID = latestClipQuestionID, let itemID = fullThread?.activePlanItemID,
              let event = events.first(where: { $0.id == eventID }),
              let question = ChatTranscriptMessage.from(event: event)?.clipQuestion,
              !ClipPoolMedia.poolIDs(in: question).isEmpty else { return }
        if let loaded = try? await model.api.visuals(itemID: itemID) { clipPoolVisuals = loaded.assets }
    }

    /// Picker only when the server advertises `clip_selection_questions`. Interactive on the newest question
    /// that no later user message has answered; read-only (with the counts, if known) afterwards.
    private func clipSelectionMode(for message: ChatTranscriptMessage) -> ClipSelectionCardMode? {
        // The payload only exists when the server supports it, so an unloaded capability snapshot never hides it.
        guard transcriptIsFresh, ClipSelectionAvailability.isAvailable(capabilities: capabilities),
              let question = message.clipQuestion else { return nil }
        let messages = timelineMessages
        guard let index = messages.firstIndex(where: { $0.id == message.id }) else { return nil }
        let isLatestQuestion = !messages[(index + 1)...].contains { $0.clipQuestion != nil }
        let isAnswered = messages[(index + 1)...].contains { $0.role == .user }
        if isAnswered { return .answered(summary: answeredClipSelections[question.questionID]?.summary(question: question)) }
        if isLatestQuestion {
            return .active(isSending: isSending || isActing || isThinking) { submission, text in
                if asksTogether(message) {
                    holdBatchAnswer(for: message) { $0.hold(clips: submission, text: text) }
                    return
                }
                answeredClipSelections[question.questionID] = submission
                // A rejected send removes the pending message, which reopens the card for a retry.
                Task { await send(message: text, clipSelection: submission) }
            }
        }
        // Not the newest clip question and nothing answered it: a newer one replaced it.
        return .superseded
    }

    /// The song timeline (instead of the vertical order list) when the server also advertises
    /// `song_order_placements`; it plays the song from a signed URL fetched through the API.
    private var songTimelineConfiguration: SongTimelineConfiguration? {
        guard capabilities?.songOrderQuestionsEnabled == true, capabilities?.songOrderPlacementsEnabled == true else { return nil }
        let api = model.api, threadID = project.id
        return SongTimelineConfiguration { generation in
            await SongAudioCache.load(threadID: threadID, generation: generation) {
                try await api.songAudio(threadID: threadID, generation: generation)
            }
        }
    }

    /// Order card only when the server advertises `song_order_questions`. Interactive on the newest question
    /// that no later user message has answered; read-only afterwards; a quiet "replaced" line once a newer question replaced it.
    private func songOrderMode(for message: ChatTranscriptMessage) -> SongOrderCardMode? {
        guard transcriptIsFresh, capabilities?.songOrderQuestionsEnabled == true,
              let question = message.songOrderQuestion else { return nil }
        let entries = timeline.compactMap { entry -> SongOrderFold.Entry? in
            guard case .message(let message) = entry.content else { return nil }
            return SongOrderFold.Entry(messageID: message.id, question: message.songOrderQuestion, isUser: message.role == .user, answer: message.songOrderAnswer)
        }
        switch SongOrderFold.phases(entries)[message.id] {
        case .active:
            return .active(isSending: isSending || isActing || isThinking) { submission, text in
                // A rejected send removes the pending message, which reopens the card for a retry.
                Task { await send(message: text, songOrder: submission) }
            }
        case .answered(let submission):
            let positions = SongOrderPositions.map(media: CreationAttachedMedia.parse(threadState), question: question)
            let summary = submission.summary(positions: positions)
            return .answered(summary: summary.isEmpty ? nil : summary)
        case .superseded: return .superseded
        case nil: return nil
        }
    }

    /// KRI-588: the "which part of the song did you film?" card, only when the server advertises
    /// `song_range_questions` (and the song affordance). Interactive on the newest question no `song_range`
    /// has answered; read-only once answered; a quiet "replaced" line once a newer question replaced it or the server closed it.
    private func songRangeMode(for message: ChatTranscriptMessage) -> SongRangeCardMode? {
        guard transcriptIsFresh, capabilities?.songRangeQuestionsEnabled == true,
              let question = message.songRangeQuestion else { return nil }
        let entries = timeline.compactMap { entry -> SongRangeFold.Entry? in
            guard case .message(let message) = entry.content else { return nil }
            return SongRangeFold.Entry(messageID: message.id, question: message.songRangeQuestion,
                                       isUser: message.role == .user, answer: message.songRangeAnswer)
        }
        switch SongRangeFold.phases(entries, closed: closedSongRangeQuestions)[message.id] {
        case .active:
            let problem = songRangeProblem?.questionID == question.questionID ? songRangeProblem?.message : nil
            return .active(isSending: isSending || isActing || isThinking, problem: problem) { answer, text in
                // A rejected send removes the pending message, which reopens the card for a retry.
                Task { await send(message: text, songRange: answer) }
            }
        case .answered(let answer):
            return .answered(summary: answer.summary, spoken: answer.spokenSummary)
        case .superseded: return .superseded
        case nil: return nil
        }
    }

    /// KRI-901: the "Mark the plate" card, only when the server advertises `protected_region_marks`. Interactive on
    /// the newest question no mark of that object has answered; read-only once answered; a quiet "replaced" line once replaced.
    private func objectMarkMode(for message: ChatTranscriptMessage) -> ObjectMarkCardMode? {
        guard transcriptIsFresh, capabilities?.protectedRegionMarks == true,
              let question = message.protectedRegionQuestion else { return nil }
        let entries = timeline.compactMap { entry -> ProtectedRegionFold.Entry? in
            guard case .message(let message) = entry.content else { return nil }
            return ProtectedRegionFold.Entry(messageID: message.id, question: message.protectedRegionQuestion,
                                             isUser: message.role == .user, answer: message.protectedRegionAnswer)
        }
        switch ProtectedRegionFold.phases(entries)[message.id] {
        case .active:
            let problem = protectedRegionProblem?.questionID == question.questionID ? protectedRegionProblem?.message : nil
            return .active(isSending: isSending || isActing || isThinking, problem: problem) { answer in
                // A refused send removes the pending message, which reopens the card (the box is kept).
                protectedRegionProblem = nil
                Task { await send(message: answer.message, protectedRegion: answer, markQuestionID: question.questionID) }
            }
        case .answered(let answer):
            return .answered(summary: answer.message)
        case .superseded: return .superseded
        case nil: return nil
        }
    }

    /// KRI-723: the "show me your moments" card, only when the server advertises `moment_order_questions`.
    /// Interactive on the newest question no answer has closed; read-only once answered; a quiet "replaced" line once replaced.
    private func momentOrderMode(for message: ChatTranscriptMessage) -> MomentOrderCardMode? {
        guard transcriptIsFresh, capabilities?.momentOrderQuestions == true,
              let question = message.momentOrderQuestion else { return nil }
        let entries = timeline.compactMap { entry -> MomentOrderFold.Entry? in
            guard case .message(let message) = entry.content else { return nil }
            return MomentOrderFold.Entry(messageID: message.id, question: message.momentOrderQuestion,
                                         isUser: message.role == .user, answer: message.momentOrderAnswer)
        }
        let language = ClipAnswerLanguage(threadState: threadState)
        switch MomentOrderFold.phases(entries)[message.id] {
        case .active:
            let problem = momentOrderProblem?.questionID == question.questionID ? momentOrderProblem?.message : nil
            return .active(isSending: isSending || isActing || isThinking, problem: problem) { answer in
                // A refused send removes the pending message, which reopens the card (the choices are kept).
                momentOrderProblem = nil
                Task { await send(message: MomentOrderAnswer.message(language: language), momentOrder: answer) }
            }
        case .answered(let answer):
            return .answered(summary: MomentOrderStrings(language).answered(count: answer.answers.count))
        case .superseded: return .superseded
        case nil: return nil
        }
    }

    /// Options only when the server advertises `choice_questions`. Interactive on the newest question that no
    /// later user message has answered; read-only (with the choice made, if known) afterwards.
    private func choiceQuestionMode(for message: ChatTranscriptMessage) -> ChoiceQuestionCardMode? {
        guard transcriptIsFresh, capabilities?.choiceQuestionsEnabled == true,
              let question = message.choiceQuestion else { return nil }
        let messages = timelineMessages
        guard let index = messages.firstIndex(where: { $0.id == message.id }) else { return nil }
        let later = messages[(index + 1)...]
        if let answer = later.first(where: { $0.role == .user }) {
            // The answer's message is still pending: the card stays open so a refused send reopens it with a reason.
            if answer.isPending { return .active(isSending: true) { _, _ in } }
            let chosen = answeredChoices[question.questionID].flatMap { question.option(key: $0.optionKey)?.label }
            return .answered(summary: chosen ?? answer.content)
        }
        guard !later.contains(where: { $0.choiceQuestion != nil }) else { return .superseded }
        return .active(isSending: isSending || isActing || isThinking) { submission, text in
            if asksTogether(message) {
                holdBatchAnswer(for: message) { $0.hold(choice: submission, text: text) }
                return
            }
            answeredChoices[question.questionID] = submission
            // A rejected send removes the pending message, which reopens the card for a retry.
            Task { await send(message: text, choiceSelection: submission) }
        }
    }

    /// The user message that is nothing more than the "You chose" capsule of the choice question it answers (same
    /// text, confirmed by the server). The capsule stands alone then; a combined batch message, a typed answer that
    /// differs from the capsule, or a pending send keep their bubble.
    private func echoesChoiceAnswer(_ message: ChatTranscriptMessage) -> Bool {
        guard message.role == .user, !message.isPending, transcriptIsFresh,
              capabilities?.choiceQuestionsEnabled == true else { return false }
        let messages = timelineMessages
        guard let index = messages.firstIndex(where: { $0.id == message.id }) else { return false }
        // The nearest question above, with no other user message in between: this one is its answer.
        for earlier in messages[..<index].reversed() {
            if earlier.role == .user { return false }
            guard let question = earlier.choiceQuestion else { continue }
            if earlier.clipQuestion != nil { return false }
            let shown = answeredChoices[question.questionID].flatMap { question.option(key: $0.optionKey)?.label } ?? message.content
            return shown == message.content
        }
        return false
    }

    /// KRI-815: the script card only when the server advertises it. Interactive on the newest script of THIS
    /// project (its item, its latest revision); every other script card is read-only text.
    private func voiceoverScriptMode(for message: ChatTranscriptMessage) -> VoiceoverScriptCardMode? {
        guard capabilities?.voiceoverScriptCardsEnabled == true, let script = message.voiceoverScript else { return nil }
        let messages = timelineMessages
        let scripts = messages.compactMap(\.voiceoverScript)
        // A draft is live while it is the newest draft; a failed draft only while no draft exists to record.
        let current = script.isDraft ? scripts.last(where: \.isDraft) : (scripts.contains(where: \.isDraft) ? nil : scripts.last)
        let index = messages.firstIndex(where: { $0.id == message.id }) ?? messages.endIndex
        let alreadyPlanned = messages[index...].contains { $0.role == .user && $0.content == script.recordedMessage }
        guard transcriptIsFresh, current == script, !alreadyPlanned,
              script.itemID == fullThread?.activePlanItemID else { return .readOnly }
        let voiceoverAttached = CreationAttachedMedia.parse(threadState).contains(where: \.isVoiceover)
        return .active(
            voiceoverAttached: voiceoverAttached,
            isSending: isSending || isActing || isThinking,
            record: { openVoiceoverStep(with: $0) },
            importVoiceover: { openVoiceoverStep(with: $0) },
            planWithVoiceover: { script in Task { await send(message: script.recordedMessage) } },
            generateNarration: capabilities?.generatedNarration == true ? { openVoiceoverStep(with: $0) } : nil
        )
    }

    /// KRI-815: the attachment sheet, opened on the voiceover step with this script to read along.
    private func openVoiceoverStep(with script: VoiceoverScript) {
        guard canAttachMedia else { return }
        attachmentLaunch = nil
        attachmentOrigin = .addMedia
        attachmentScript = script
        scrollRequest += 1
        showsAttachments = true
    }

    /// KRI-802: this reply asks a choice card and a clip picker together (servers send that only to protocol 14
    /// builds), and both cards are shown. Their answers then wait for each other and go out on one turn.
    private func asksTogether(_ message: ChatTranscriptMessage) -> Bool {
        message.choiceQuestion != nil && message.clipQuestion != nil && capabilities?.choiceQuestionsEnabled == true
            && ClipSelectionAvailability.isAvailable(capabilities: capabilities)
    }

    /// Holds one answer of a two-question reply; the second answer sends both as one message.
    private func holdBatchAnswer(for message: ChatTranscriptMessage, _ answer: (inout QuestionBatch) -> Void) {
        var batch = questionBatches[message.id] ?? QuestionBatch()
        answer(&batch)
        guard let ready = batch.ready, let choice = message.choiceQuestion, let clips = message.clipQuestion else {
            questionBatches[message.id] = batch
            return
        }
        questionBatches[message.id] = nil
        answeredChoices[choice.questionID] = ready.choice
        answeredClipSelections[clips.questionID] = ready.clips
        // A rejected send removes the pending message, which reopens both cards for a retry.
        Task { await send(message: ready.message, clipSelection: ready.clips, choiceSelection: ready.choice) }
    }

    /// Answer blocks for a plain `assistant_response` question, interactive on the newest question that no later
    /// user message has answered. A later message that is still pending keeps them open (the tapped one spins);
    /// anything later that landed (an answer, a newer question) removes them from history.
    private func replyOptionsMode(for message: ChatTranscriptMessage) -> ReplyOptionsMode? {
        guard transcriptIsFresh, !message.replyOptions.isEmpty else { return nil }
        let messages = timelineMessages
        guard let index = messages.firstIndex(where: { $0.id == message.id }) else { return nil }
        let later = messages[(index + 1)...]
        guard !later.contains(where: { ($0.role == .user && !$0.isPending) || !$0.replyOptions.isEmpty || !$0.options.isEmpty }) else { return nil }
        let pending = later.first(where: { $0.role == .user && $0.isPending })
        return ReplyOptionsMode(
            select: { option in Task { await send(message: option) } },
            other: { startAnsweringOwn(message) },
            isSending: pending != nil,
            pendingOption: pending.flatMap { pending in message.replyOptions.first { $0 == pending.content } },
            isTyping: answeringQuestion != nil && composerFocused
        )
    }

    /// "Type my own": the composer takes focus, names the question above it, and nothing is sent.
    private func startAnsweringOwn(_ message: ChatTranscriptMessage) {
        answeringQuestion = message.displayedContent
        composerFocused = true
    }

    /// One file of a media receipt, as the project holds it now (a poster that arrived later, a new name).
    @ViewBuilder private func mediaReceipt(_ entry: ChatTimelineEntry, index: Int) -> some View {
        if case .media(let event) = entry.content {
            let received = CreationAttachedMedia.parse(event.payload ?? [:])
            if received.indices.contains(index) {
                let attachment = CreationAttachedMedia.parse(threadState).last { $0.id == received[index].id } ?? received[index]
                KriaMediaRow(name: attachment.displayName,
                             status: .ready(attachment.isSong ? "Song · Ready" : attachment.isVoiceover ? "Voiceover · Ready" : "Video · Ready"),
                             trailing: .check, onFill: true) {
                    CreationAttachmentThumbnail(media: attachment)
                }
                .accessibilityElement(children: .combine)
                .accessibilityLabel("Uploaded \(attachment.displayName)")
                .accessibilityIdentifier("chat-media-\(attachment.id)")
            }
        }
    }

    /// Presents the editor with no slide-up animation; `WorkspaceCrossfade` fades it in.
    private func openEditor() {
        // Format still unknown (thread not loaded): no editor entry at all.
        guard isFormatKnown else { return }
        // The editor's Kria sheet shows this same chat, so its ready card offers "Open editor" while
        // the editor is already open underneath: close the sheet to land back in it.
        if showsResult || workspaceScreen == .slideWorkspace {
            editorConversationCloseID = UUID()
            // The card names the thread's newest version. An editor still on an older job (the
            // creator tapped "Keep editing" earlier, or the adopt never ran) is asked again; own
            // unsaved edits raise the Switch / Keep editing prompt, never a silent switch.
            if showsResult, currentProject.runtimeVersion == 2, editorSession.isBehindActiveJob(currentProject) {
                let target = currentProject
                Task { await editorSession.adoptLatestJob(target, api: model.api) }
            }
            return
        }
        // A slide post has no video timeline: every "open editor" entry lands in the slide workspace.
        if SlidePostRouting.editorDestination(isSlidePost: isSlidePostProject) == .slideWorkspace {
            isChoosingFormat = false
            return
        }
        // The flight the chat started while it was idle is the creator's from here on.
        editorSession.claimWarm()
        Perf.begin("editor.tap.toFrame")
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) { showsResult = true }
    }

    /// Whether the chat may prepare its editor now. The value is the `.task(id:)` identity, so the warm-up restarts only
    /// when the job, the variant or the permission changes, never on a chat poll.
    private var editorWarmPlan: EditorWarmPlan {
        var warmable = true
        #if DEBUG
        warmable = ProcessInfo.processInfo.environment["KRIA_EDITOR_WARM_DISABLED"] != "1"
        #endif
        return EditorWarmPlan(project: currentProject, facts: .init(
            videoIsReady: currentProject.status == .ready && warmable, formatIsKnown: isFormatKnown,
            isSlidePost: isSlidePostProject, canOpenVideoEditor: canOpenVideoEditor,
            sceneIsActive: scenePhase == .active, chatIsIdle: !isSending && !isActing && !isThinking,
            isOnline: reachability.isOnline, isLowDataMode: reachability.isConstrained))
    }

    /// Prepares the editor (variant, draft, sources, composition) while the creator reads the chat, so the tap finds it
    /// loaded. A load that fails leaves the editor as it was: the tap opens it the way it always has.
    private func warmEditor() async {
        let plan = editorWarmPlan
        guard plan.isWarmable, !showsResult else { editorSession.cancelWarm(); return }
        // Let the chat finish drawing and reading before competing with it; a plan that flips back cancels this sleep.
        try? await Task.sleep(for: EditorWarmPlan.settleDelay)
        guard !Task.isCancelled else { return }
        await editorSession.openOrWarm(project: currentProject, libraryJobID: nil, api: model.api,
                                       deviceRenders: model.deviceRenders, uploads: model.uploads, intent: .warm)
    }

    private func dismissEditor() {
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) { showsResult = false }
        // A completed device export updates the job without necessarily
        // advancing the chat revision. Re-read proposals on return.
        finishedEditSuggestionRefresh += 1
        suggestionLedger.requestRefresh()
    }

    private func attachmentSheet(rerecordTarget: RerecordTarget?) -> some View {
        AttachmentSheet(
            projectID: project.id,
            maximumClipCount: selectedMaximumClipCount,
            attachedClipCount: attachedClipCount,
            format: selectedFormat,
            thread: fullThread,
            capabilities: capabilities,
            capabilitiesLoaded: capabilitiesAreAuthoritative,
            voiceoverScript: rerecordTarget == nil ? attachmentScript : nil,
            launch: rerecordTarget == nil ? attachmentLaunch : nil,
            origin: rerecordTarget == nil ? attachmentOrigin : .addMedia,
            rerecordTarget: rerecordTarget,
            refresh: {
                if !capabilitiesAreAuthoritative { await refreshCapabilities() }
                await refreshNow()
            }
        )
            .environmentObject(model)
            .environment(\.dynamicTypeSize, dynamicTypeSize)
    }

    private func openAttachments(launch: AttachmentLaunch? = nil, origin: AttachmentOrigin = .addMedia) {
        guard canAttachMedia else { return }
        attachmentLaunch = launch
        attachmentOrigin = origin
        attachmentScript = nil
        scrollRequest += 1
        showsAttachments = true
    }

    /// "Record again" / "Upload another" after a silent video. When the edit already holds its most clips (a one-clip
    /// Talk to camera), a new take would be refused, so open Add media on its clip list where the silent one can be removed.
    private func openAttachmentsToReplaceFootage(launch: AttachmentLaunch) {
        let pendingClips = uploadRecords.filter { $0.projectID == project.id && $0.role == .clip }.count
        let isFull = attachedClipCount + pendingClips >= selectedMaximumClipCount
        openAttachments(launch: isFull ? nil : launch)
    }

    private func rememberUploadAnchors() {
        let newIDs = activeUploadIDs.subtracting(uploadAnchors.keys)
        for id in newIDs.sorted(by: { $0.uuidString < $1.uuidString }) {
            nextLocalOrder += 1
            uploadAnchors[id] = ChatPendingUpload(id: id, afterSequence: afterSequence, localOrder: nextLocalOrder)
        }
    }

    private func observeResponses() {
        responsePresentation.observe(events: events, isInitialLoad: !hasHistoryBaseline,
                                     isActive: scenePhase == .active && !projectsDrawerOpen && !showsAttachments)
        hasHistoryBaseline = true
    }

    private var attachedClipCount: Int {
        attachedVideoClipCount(in: threadState)
    }

    private var selectedMaximumClipCount: Int {
        CreationFormat.maximumClipCount(for: selectedFormat, serverLimits: maximumClipsByFormat)
    }

    /// Clips being prepared or still being chosen — not yet visible as an upload record.
    private var preparingUploadCount: Int {
        BackgroundUploadCoordinator.reservedCount(projectID: project.id, role: nil, inFlight: uploadInFlight, records: uploadRecords, selections: photoSelections)
    }

    private var pendingUploadCount: Int {
        // A record whose upload failed stays in `uploadRecords` for Retry, but Send must not wait for it.
        BackgroundUploadCoordinator.inProgressRecords(uploadRecords).filter { $0.projectID == project.id }.count + preparingUploadCount
    }

    private var isUITesting: Bool {
        #if DEBUG
        ProcessInfo.processInfo.arguments.contains("-ui-testing-chat")
        #else
        false
        #endif
    }

    /// A new Creator plan is proposed and not yet confirmed or declined,
    /// across both the runtime-v1 (`creatorAgent.status`) and runtime-v2
    /// (live `approval`, synchronized with expiry checked) mechanisms.
    private var awaitsNewPlanConfirmation: Bool {
        approval != nil || fullThread?.awaitsCreationConfirmation == true
    }

    private var workspaceStage: WorkspaceStage {
        WorkspaceStage.resolve(
            status: currentProject.status,
            awaitsNewPlanConfirmation: awaitsNewPlanConfirmation,
            isChoosingFormat: isChoosingFormat,
            hasFormat: selectedFormat != nil,
            preparationIsActive: fullThread?.preparationIsActive == true,
            preparationFailed: fullThread?.preparationFailed == true,
            approvedRenderRetryPending: approval?.retryOfExecutionID != nil,
            // An approval the creator just gave (or one the server recorded) whose render the
            // agent-control task has not started yet: show the render stage at once instead of
            // falling back to the footage stage for a poll or two.
            renderDispatchPending: thinkingSettlesOnJobStatus || fullThread?.awaitsRenderDispatch == true
        )
    }

    /// A slide post switches out of the generic creator runtime as soon as
    /// select_format has provisioned its PlanItemAsset pool. Before then, keep
    /// polling the existing chat rather than presenting an upload surface with
    /// no item to reserve against.
    private var hasDedicatedSlideWorkspace: Bool {
        !isChoosingFormat && isSlidePostProject
    }

    /// The one slide-post decision (format, library/project row, or thread projection). The
    /// plan-item id is NOT part of it: `SlidePostWorkspaceView` resolves it itself and shows
    /// "Setting up your post…" while it is unavailable, instead of falling to the video editor.
    private var isFormatKnown: Bool {
        SlidePostRouting.isFormatKnown(selectedFormat: selectedFormat, project: currentProject, thread: fullThread)
    }

    private var hasSavedEditor: Bool {
        guard let fullThread,
              UUID(uuidString: fullThread.id) == currentProject.id,
              fullThread.activeJobID.flatMap(UUID.init(uuidString:)) == currentProject.activeJobID,
              fullThread.activePlanItemID == currentProject.activePlanItemID else { return false }
        return fullThread.savedEditorVariant != nil
    }

    private var hasAccessibleVideo: Bool {
        if hasSavedEditor { return true }
        if currentProject.status == .ready { return true }
        guard let key = deviceRenderKey, let presentation = model.deviceRenders.presentations[key] else { return false }
        return presentation.localFile != nil && !presentation.outputVerificationPending
            && [.localReady, .syncing, .synced].contains(presentation.phase)
    }

    private var canOpenVideoEditor: Bool {
        SlidePostRouting.canOpenVideoEditor(selectedFormat: selectedFormat, project: currentProject, thread: fullThread)
    }

    private var workspaceScreen: SlidePostRouting.Screen {
        SlidePostRouting.screen(selectedFormat: selectedFormat, project: currentProject, thread: fullThread, isChoosingFormat: isChoosingFormat)
    }

    /// Format unknown (list row had no signal): fetch the full projection NOW. The delta poll only pulls
    /// it when the revision advances, which a quiet ready project never does.
    private func resolveFormat() async {
        formatResolveFailed = false
        var attempts = 0
        while !isFormatKnown, attempts < 5, !Task.isCancelled {
            if let thread = try? await model.api.project(threadID: project.id) { apply(thread) }
            attempts += 1
            if !isFormatKnown { try? await Task.sleep(for: .seconds(1)) }
        }
        if !isFormatKnown, !Task.isCancelled { formatResolveFailed = true }
    }

    private func loadCapabilitiesWithRetry() async {
        // KRI-640: reuse the read New chat (or opening the workspace) just made, the
        // same one the create call trusted, so the format picker works on first paint.
        if let recent = await model.api.recentCreationCapabilities(maxAge: CreationCapabilitiesCache.reuseWindow) {
            await refreshCapabilities(preloaded: recent)
            if capabilitiesAreAuthoritative { return }
        }
        for delay in [0.0, 1.0, 3.0, 8.0] {
            if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
            if Task.isCancelled { return }
            await refreshCapabilities()
            if capabilitiesAreAuthoritative { return }
        }
    }

    private var isSlidePostProject: Bool {
        SlidePostRouting.isSlidePost(selectedFormat: selectedFormat, project: currentProject, thread: fullThread)
    }

    var body: some View {
        Group {
            switch workspaceScreen {
            case .slideWorkspace:
                SlidePostWorkspaceView(
                    project: currentProject,
                    thread: fullThread,
                    capabilities: capabilities,
                    capabilitiesLoaded: capabilitiesAreAuthoritative,
                    conversation: { AnyView(editorConversation) },
                    conversationAcceptedID: conversationAcceptedID,
                    conversationCloseID: editorConversationCloseID,
                    // Back returns to the chats drawer (where the user came from), never the format chooser.
                    onBack: openProjects,
                    onAddMedia: { openAttachments() }
                )
                .environmentObject(model)
            case .resolvingFormat:
                SlidePostResolvingView(
                    title: currentProject.workspaceTitle, failed: formatResolveFailed,
                    openProjects: openProjects,
                    retry: { Task { await resolveFormat() } }
                )
            case .genericChat:
                genericChatWorkspace
            }
        }
        .background(WorkspaceSurface())
        .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: responsePresentation.hapticToken)
        // The composer is no longer added here: it floats as a safeAreaInset
        // directly on ChatConversationScroll inside genericChatWorkspace (KRI-197
        // floating chat chrome), which also implicitly gates it on
        // !hasDedicatedSlideWorkspace via the if/else above.
        .onAppear {
            activateDraftStorage()
            Task { await loadCompletedThoughtSummaries() }
        }
        .onReceive(chatDrafts.$revision) { _ in synchronizeSubmissionStorage() }
        .onChange(of: renderShapeScope) { _, scope in renderShapePick.reset(scope: scope) }
        .task {
            Perf.begin("chat.open", id: project.id.uuidString)
            Perf.begin("chat.toPixels", id: project.id.uuidString)
            Perf.mark("chat.task")
            // KRI-640: a chat New chat just made opens on the thread its create call returned, so it
            // shows without reading itself back first; the poll below still refreshes it.
            if let created = model.takeCreatedThread(project.id) {
                apply(created)
                initialConversationLoaded = true
                markTranscriptVisible(source: "created")
            } else {
                await restoreCachedTranscript()
            }
            // History should not wait for the independent capability request. Requested as soon as the
            // project opens, and retried: a slow or failed fetch must not leave the screen without it.
            async let capabilities: Void = loadCapabilitiesWithRetry()
            await resolveFormat()
            Perf.mark("format.resolved")
            await pollUntilDismissed()
            await capabilities
        }
        .task(id: suggestionFetchKey) { await loadSuggestions() }
        .task(id: editorWarmPlan) { await warmEditor() }
        .onReceive(NotificationCenter.default.publisher(for: UIApplication.didReceiveMemoryWarningNotification)) { _ in
            editorSession.releaseWarmForMemoryPressure()
        }
        .onDisappear { editorSession.cancelWarm() }
        // An editor Save changes the Job without a new thread revision, so cards built on the old edit are asked about again.
        .onChange(of: editorSession.saveState) { old, new in
            if case .saving = old, new == .saved || new == .previewPending { suggestionLedger.requestRefresh() }
        }
        .task(id: thoughtRequestID) {
            guard let requestID = thoughtRequestID else { return }
            while !Task.isCancelled, (isSending || isThinking), thoughtRequestID == requestID {
                if let response = try? await model.api.creationThoughtSummaries(threadID: project.id, clientRequestID: requestID),
                   response.clientRequestID == requestID {
                    guard !Task.isCancelled, thoughtRequestID == requestID else { return }
                    liveThoughtSummaries = response.summaries.filter { $0.hasText }
                    let completed = liveThoughtSummaries.filter { $0.status == .completed }
                    if !completed.isEmpty { completedThoughtSummaries[requestID] = completed }
                }
                try? await Task.sleep(for: .seconds(1))
            }
            if !Task.isCancelled, thoughtRequestID == requestID {
                await fetchFinalThoughtSummary(requestID: requestID)
            }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active, capabilitiesAreAuthoritative { Task { await refreshEditorStateCapability() } }
        }
        .onChange(of: currentProject.serverRevision) { _, revision in
            threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: revision)
        }
        .task(id: capabilities?.voiceDictationEnabled == true) {
            if capabilities?.voiceDictationEnabled == true, dictationController == nil {
                dictationController = DictationController.make(api: model.api)
            }
        }
        .onReceive(model.uploads.$records) { uploadRecords = $0; rememberUploadAnchors() }
        .onReceive(model.uploads.$inFlight) { uploadInFlight = $0; rememberUploadAnchors() }
        .onReceive(model.uploads.$photoSelections) { photoSelections = $0; rememberUploadAnchors() }
        .onReceive(model.uploads.$failures) { uploadFailures = $0 }
        .task(id: capabilities == nil && latestClipQuestionID != nil) {
            // A question is waiting but the capability read failed or was cancelled: read it again (a few
            // times, backing off) instead of leaving this screen without a snapshot until the next send.
            guard capabilities == nil, latestClipQuestionID != nil else { return }
            for delay in [0, 2, 5, 12] {
                if delay > 0 { try? await Task.sleep(for: .seconds(delay)) }
                guard !Task.isCancelled, capabilities == nil else { return }
                await refreshCapabilities()
            }
        }
        .task(id: "\(latestClipQuestionID ?? "")|\(fullThread?.activePlanItemID ?? "")") {
            await loadClipPoolVisuals()
        }
        .task(id: latestReceiptEventID) {
            guard latestReceiptEventID != nil else { return }
            // One retry, then settle either way: a failed fetch leaves neutral titles, not bare chips
            // that wait forever. Titles already learned are kept, so an older reply keeps its wording
            // when a newer brief drops that requirement.
            var brief = try? await model.api.creationBrief(threadID: project.id)
            if brief == nil, !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                brief = try? await model.api.creationBrief(threadID: project.id)
            }
            guard !Task.isCancelled else { return }
            if let brief { briefRequirements.merge(brief.requirementsByID) { _, new in new } }
            briefSettled = true
        }
        .task(id: fullThread?.activePlanItemID) {
            guard let itemID = fullThread?.activePlanItemID else { return }
            generatedCards.resume(itemID: itemID, accountID: auth.localStorageIdentity, api: model.api)
        }
        #if DEBUG
        // KRI-211 fixtures: `1` = one file that couldn't be read; `record` = an upload that failed on
        // the way and still has its record (Retry). Neither may block Send.
        .task(id: project.id) {
            switch ProcessInfo.processInfo.environment["KRIA_CHAT_FIXTURE_UPLOAD_FAILURE"] {
            case "1":
                model.uploads.reportFailure(
                    projectID: project.id, role: .clip, filename: "Selected item",
                    message: "This file couldn’t be read. Try Files or choose it again."
                )
            case "record":
                model.uploads.seedFailedUploadForTesting(projectID: project.id)
            default:
                break
            }
        }
        #endif
        .onReceive(model.uploads.$previewVersion) { previewVersion = $0 }
        .onReceive(model.uploads.$progress) { uploadProgress.values = $0 }
        .onReceive(model.uploads.$attachedThreads) { threads in
            guard let thread = threads[project.id] else { return }
            apply(thread)
        }
        .sheet(isPresented: $showsAttachments) {
            attachmentSheet(rerecordTarget: nil)
                .presentationDetents([.large])
        }
        .fullScreenCover(item: $rerecordCamera, onDismiss: rerecordCameraClosed) { target in
            attachmentSheet(rerecordTarget: target)
        }
        .confirmationDialog("Re-record your take?", isPresented: rerecordConfirmation, titleVisibility: .visible) {
            if case let .confirming(targets) = rerecordLocal {
                ForEach(targets) { target in
                    Button(targets.count == 1 ? "Record new take" : "Re-record \(target.displayName)") { rerecordLocal = .swapping; rerecordCamera = target }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Kria keeps your script, background and other clips, then makes the video again from your new take. Text and caption changes you made to this video won’t carry over. Your current video stays until the new one is ready.")
        }
        .onChange(of: rerecordFacts.debt) { _, debt in rerecordDebtChanged(debt) }
        .sheet(item: $reviewPresentation) { presentation in
            ReviewPlanView(
                seed: presentation.seed, initialFlag: presentation.initialFlag,
                actions: reviewActions(),
                refreshToken: "\(afterSequence):\(threadRevision):\(currentProject.activeJobID?.uuidString ?? ""):\(currentProject.status):\(failure == nil)",
                expectedThreadID: project.id.uuidString,
                isCurrentScope: { [threadID = project.id, account = ReviewAccountScope(uiOwner: auth.localStorageIdentity, transportOwner: model.api.mediaLibraryOwner), environment = (model.api as? KriaAPI)?.baseURL] in
                    reviewScopeIsCurrent(threadID: threadID, account: account, environment: environment)
                },
                dictation: dictationForComposer, dictationTerms: dictationContextTerms,
                close: { reviewPresentation = nil }
            )
            .id(reviewScopeToken)
            .presentationDetents([.large])
            .presentationDragIndicator(.hidden)
            .presentationBackground(KriaColor.paper)
        }
        .fullScreenCover(isPresented: $showsResult) {
            // Chat <-> Editor is a switch, not a page rising from the bottom: the
            // editor cross-dissolves over the chat (see `WorkspaceCrossfade`).
            WorkspaceCrossfade(dismiss: { dismissEditor() }) { close in
                if isSlidePostProject {
                    // Defensive: never present the video editor for a slide post.
                    Color.clear.onAppear { close() }
                } else {
                NativeEditorView(
                    project: currentProject,
                    sharedSession: editorSession,
                    conversationAcceptedID: conversationAcceptedID,
                    conversationCloseID: editorConversationCloseID,
                    conversationActivity: editorAIActivity,
                    conversation: { AnyView(editorConversation) },
                    onBack: close
                )
                .environmentObject(model)
                }
            }
        }
    }

    /// The transcript runs full-bleed and the header and composer float over it
    /// as `safeAreaInset`s, so text scrolls (and softly fades) underneath both
    /// instead of ending at an opaque bar. The scroll view is disabled while the
    /// drawer is open; the insets are added after that so the header's menu
    /// button stays tappable.
    private var genericChatWorkspace: some View {
        ChatConversationScroll(isLoaded: initialConversationLoaded, updateToken: timelineUpdateToken, scrollRequest: scrollRequest, dismissKeyboard: { composerFocused = false }) {
            conversationContent
            firstPromptSuggestions
        }
        .accessibilityHidden(projectsDrawerOpen)
        .allowsHitTesting(!projectsDrawerOpen)
        .safeAreaInset(edge: .top, spacing: 0) {
            WorkspaceHeader(
                project: currentProject,
                // Local playback remains usable while cloud publication is pending.
                showsEditorSwitch: hasAccessibleVideo && canOpenVideoEditor,
                openProjects: openProjects,
                openEditor: openEditor,
                openAccount: openAccount
            )
            .simultaneousGesture(TapGesture().onEnded { composerFocused = false })
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                if let routedKey = routedDeviceRenderKey {
                    routedDeviceRenderPanel(routedKey)
                        .padding(.horizontal, 16).padding(.top, 8)
                        .background(KriaColor.paper)
                        .accessibilityIdentifier("routed-device-render")
                }
                waitingBar
                ChatComposer(
                    text: promptBinding,
                    isSending: isSending || isActing,
                    canAttach: canAttachMedia,
                    canSendWithoutText: readyMediaCount > 0,
                    // be99a3f12 (origin/main): a failed attach never blocks Send.
                    blocksSubmission: (isThinking && !steerNowEnabled) || sendReceipt != nil || pendingUploadCount > 0,
                    placeholder: answeringQuestion != nil ? "Type your answer" : (readyMediaCount > 0 ? "Add instructions (optional)" : "Tell Kria what you want…"),
                    answering: answeringQuestion,
                    cancelAnswering: { answeringQuestion = nil },
                    isFocused: $composerFocused,
                    dictation: dictationForComposer,
                    dictationTerms: dictationContextTerms,
                    attach: { openAttachments() },
                    send: { Task { await sendComposerDraft() } },
                    attachMenu: attachMenu
                )
            }
            .accessibilityHidden(projectsDrawerOpen)
            .allowsHitTesting(!projectsDrawerOpen)
        }
    }

    /// KRI-756: the first-prompt chips end the transcript, so they scroll away with it instead of covering the page
    /// while the keyboard is up.
    @ViewBuilder private var firstPromptSuggestions: some View {
        let firstPromptChips = suggestionChips(for: .firstPrompt)
        if !firstPromptChips.isEmpty {
            SuggestionChipRow(chips: firstPromptChips, sendingID: sendingSuggestionID) { sendSuggestion($0) }
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityIdentifier("first-prompt-suggestions")
        } else if suggestionsLoading, suggestionSurface == .firstPrompt,
                  prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            // The first prompt's chips are still being written (Paper S2.09 state A): two quiet placeholders.
            HStack(spacing: 8) {
                KriaSkeletonBar(width: 140, height: 44)
                KriaSkeletonBar(width: 112, height: 44)
                Spacer(minLength: 0)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Loading suggestions")
            .accessibilityIdentifier("first-prompt-suggestions-loading")
        }
    }

    /// What the composer's + offers (Paper S3.05): Record and Upload for every format, and the optional script and
    /// voiceover rows only where they mean something. Every row opens Add media on the place it names; at the most
    /// clips Record and Upload give way to the limit notice and Manage clips.
    private var attachMenu: ChatAttachMenu {
        let takesPhotos = selectedFormat?.usesVisualPool == true
        var replaceOffered = false
        if selectedFormat == .talkingToCamera, case .one = RerecordOffer.resolve(thread: fullThread) { replaceOffered = true }
        let room: AttachRoom = takesPhotos ? .open : .resolve(used: clipSlotsUsed, maximum: selectedMaximumClipCount, recordReplaces: replaceOffered)
        return ChatAttachMenu(
            disabledReason: attachBlockedReason,
            offersSaved: capabilities?.mediaLibrary == true,
            takesPhotos: takesPhotos,
            room: room,
            record: takesPhotos ? nil : { openAttachments(launch: .record, origin: .composerTray) },
            upload: { openAttachments(launch: .source($0), origin: .composerTray) },
            addScript: selectedFormat == .talkingToCamera ? { openAttachments(launch: .recordWithScript) } : nil,
            addVoiceover: selectedFormat == .narrated ? { openAttachments(launch: .voiceover) } : nil,
            addSong: capabilities?.songUploadEnabled == true && selectedFormat?.runsAsMontage == true ? { openAttachments(launch: .step(.song)) } : nil,
            addOverlays: capabilities?.visualsEnabled == true && selectedFormat?.usesVisualPool == false ? { openAttachments(launch: .step(.overlays)) } : nil,
            manageClips: { openAttachments() }
        )
    }

    /// Clips attached plus clips still uploading or being chosen: the count the picker measures its room against.
    private var clipSlotsUsed: Int {
        attachedClipCount + uploadRecords.filter { $0.projectID == project.id && $0.role == .clip }.count
            + BackgroundUploadCoordinator.reservedCount(projectID: project.id, role: .clip, inFlight: uploadInFlight, records: uploadRecords, selections: photoSelections)
    }

    private var editorConversation: some View {
        ChatConversationScroll(isLoaded: initialConversationLoaded, updateToken: timelineUpdateToken, scrollRequest: scrollRequest, dismissKeyboard: { composerFocused = false }) {
            conversationContent
        }
        .safeAreaInset(edge: .bottom, spacing: 0) {
            VStack(spacing: 0) {
                waitingBar
                ChatComposer(
                    text: promptBinding, isSending: isSending || isActing,
                    canAttach: false, blocksSubmission: (isThinking && !steerNowEnabled) || sendReceipt != nil || pendingUploadCount > 0, isFocused: $composerFocused, dictation: dictationForComposer, dictationTerms: dictationContextTerms, attach: {}, send: { Task { await sendComposerDraft() } }
                )
            }
        }
        .onAppear { activateDraftStorage() }
        .background(KriaColor.paper)
        .accessibilityIdentifier("native-editor-project-conversation")
    }

    @ViewBuilder private var stageContent: some View {
        switch workspaceStage {
        case .format:
            if let selectedFormat {
                FormatReselectStage(formats: availableFormats, selected: selectedFormat, isBusy: isActing || !capabilitiesAreAuthoritative, unavailableReason: formatSwitchBlock) { format in
                    if format == selectedFormat { isChoosingFormat = false } else { selectFormat(format) }
                }
                .id("format-reselect")
            } else {
                FormatStage(formats: availableFormats, isBusy: isActing || !capabilitiesAreAuthoritative, select: selectFormat).id("format-picker")
            }
            if let capabilitiesFailure {
                RecoveryCard(failure: capabilitiesFailure) { Task { await refreshCapabilities() } }
            } else if !capabilitiesAreAuthoritative {
                ProgressView("Loading formats…")
            }
        case .footage:
            if let selectedFormat {
                FootageStage(
                    format: selectedFormat,
                    mediaCount: selectedFormat == .slides ? 0 : attachedClipCount,
                    maximumClipCount: selectedMaximumClipCount,
                    uploads: [],
                    preparingCount: 0,
                    previewVersion: previewVersion,
                    progress: uploadProgress,
                    addFootage: { openAttachments(launch: .chooseSource) },
                    recordFootage: canAttachMedia ? { openAttachments(launch: .record) } : nil,
                    manageFootage: { openAttachments() },
                    isBusy: isSending || isActing || isThinking,
                    failures: uploadFailures.filter { $0.projectID == project.id },
                    dismissFailure: { model.uploads.dismissFailure(id: $0) },
                    retryFailure: { id in Task { await model.uploads.retryUpload(recordID: id) } },
                    // Only a project this iPhone renders can be made of Visuals
                    // alone, and only from the Visuals the server's rule counts.
                    visualCount: selectedFormat == .slides
                        ? (fullThread?.readyVisualCount ?? 0)
                        : ProjectUploadDestination.resolve(
                        capabilities: capabilities?.phoneRendering, creationMode: capabilities?.creationMode, capabilitiesLoaded: capabilitiesAreAuthoritative,
                        sourcePurposes: [], role: .visual
                    ).visualKinds == nil ? 0 : fullThread?.deviceReadyVisualCount ?? 0
                )
                .id("upload-prompt")
            }
        case .direction:
            if let approval {
                DirectionStage(
                    recordAgain: canAttachMedia ? { openAttachmentsToReplaceFootage(launch: .record) } : nil,
                    chooseAnother: canAttachMedia ? { openAttachmentsToReplaceFootage(launch: .chooseSource) } : nil,
                    approval: approval,
                    format: selectedFormat,
                    speechCleanup: approval.retryOfExecutionID == nil ? fullThread?.speechCleanup : nil,
                    speechCheckTimedOut: speechCheckTimedOut,
                    renderShape: approval.retryOfExecutionID == nil ? renderShapePicker : nil,
                    researchPreview: ResearchPreview.parse(activeProposalEvent?.payload?["research_preview"]),
                    replyLanguage: ClipAnswerLanguage(threadState: threadState),
                    isBusy: isActing || pendingUploadCount > 0,
                    responseStartedAt: activeProposalEvent.flatMap { responsePresentation.startTime(for: $0.id) },
                    decide: decide,
                    retrySpeechCheck: { analysisID in
                        var payload: [String: JSONValue] = [:]
                        if let analysisID { payload["speech_cleanup_analysis_id"] = .string(analysisID) }
                        performAction("retry_speech_cleanup", payload: payload)
                    }
                )
                .id("approval-\(approval.id)")
            } else if let thread = fullThread {
                CreationConfirmationStage(
                    recordAgain: canAttachMedia ? { openAttachmentsToReplaceFootage(launch: .record) } : nil,
                    chooseAnother: canAttachMedia ? { openAttachmentsToReplaceFootage(launch: .chooseSource) } : nil,
                    thread: thread,
                    isBusy: isActing || isSending || pendingUploadCount > 0,
                    responseStartedAt: activeProposalEvent.flatMap { responsePresentation.startTime(for: $0.id) },
                    conflict: visibleConfirmationConflict(for: thread),
                    renderShape: renderShapePicker,
                    refreshDirection: refreshDirection,
                    speechCheckTimedOut: speechCheckTimedOut,
                    action: performAction
                )
            }
            // A new plan's confirmation card can appear over an already-ready
            // cut (see `WorkspaceStage.resolve`); confirming it is a choice,
            // not something the old cut's reachability should be sacrificed for.
            if hasAccessibleVideo && canOpenVideoEditor {
                Button("Open current cut", action: openEditor)
                    .buttonStyle(CanonicalSecondaryButtonStyle())
                    .disabled(isActing)
                    .accessibilityIdentifier("open-current-cut")
            }
        case .rendering:
            if feedHostsDeviceBuild {
                planFeedView
            } else if let deviceRenderKey {
                deviceRenderPanel(deviceRenderKey)
            } else if showsPlanFeed {
                planFeedView
            } else {
                RenderingStage(
                    isPreparing: currentProject.activeJobID == nil,
                    preparationMessage: fullThread?.preparationMessage,
                    preparationCompleted: fullThread?.preparationCompleted ?? 0,
                    preparationTotal: fullThread?.preparationTotal ?? 0
                ).id("rendering")
            }
        case .ready where !isFormatKnown:
            // Ready but we cannot tell a slide post from a video yet: wait for the thread.
            ProgressView("Opening your project…")
                .frame(maxWidth: .infinity, minHeight: 120)
                .accessibilityIdentifier("format-resolving")
                .id("format-resolving")
        case .ready:
            ReadyStage(
                project: currentProject,
                openEditor: openEditor,
                suggest: { prompt = $0 },
                // Capability on: only server chips are drawn (none yet or none offered => no chips, never the generic fixed
                // ones). Capability off: nil, the fixed chips.
                serverSuggestions: SuggestionChipsPolicy.isEnabled(capabilities) ? suggestionChips(for: .ready) : nil,
                sendSuggestion: { sendSuggestion($0) },
                suggestionCards: readyCardsPresentation,
                rerecord: rerecordPhase,
                rerecordActions: rerecordActions
            )
            .id("ready")
            if let variants = fullThread?.job?.variants, variants.count > 1 {
                ForEach(variants.compactMap { $0.variantID }, id: \.self) { variantID in
                    let variant = variants.first { $0.variantID == variantID }
                    Button(variantID.replacingOccurrences(of: "_", with: " ").capitalized) {
                        performAction("select_variant", payload: ["variant_id": .string(variantID)])
                    }.disabled(isActing || variant?.isPlayable != true)
                    if variant?.renderStatus == "failed", currentProject.runtimeVersion == 1 {
                        Button("Retry \(variantID.replacingOccurrences(of: "_", with: " "))") {
                            performAction("retry", payload: ["variant_id": .string(variantID)])
                        }.disabled(isActing)
                    }
                }
            }
        case .failed where fullThread == nil && currentProject.deliveryNeedsAttention == true:
            // KRI-823: the list row already knows this is a delivery failure; wait
            // for the full thread's Retry card instead of flashing "wasn't made".
            ProgressView("Opening your project…")
                .frame(maxWidth: .infinity, minHeight: 120)
                .accessibilityIdentifier("delivery-resolving")
                .id("delivery-resolving")
        case .failed where fullThread?.deliveryNeedsAttention == true && deviceRenderKey != nil:
            // KRI-823: a device delivery failure keeps its device Retry/Stop card,
            // the same one the rendering stage hosts, never the generic failure.
            if feedHostsDeviceBuild {
                planFeedView
            } else if let deviceRenderKey {
                deviceRenderPanel(deviceRenderKey)
            }
        case .failed:
            if hasSavedEditor && canOpenVideoEditor {
                Button("Open saved edit", action: openEditor)
                    .buttonStyle(CanonicalSecondaryButtonStyle())
                    .accessibilityIdentifier("open-saved-edit")
            }
            if failedWorkspacePresentation == .legacyCreationConfirmation, let thread = fullThread {
                CreationConfirmationStage(
                    recordAgain: canAttachMedia ? { openAttachmentsToReplaceFootage(launch: .record) } : nil,
                    chooseAnother: canAttachMedia ? { openAttachmentsToReplaceFootage(launch: .chooseSource) } : nil,
                    thread: thread,
                    isBusy: isActing || isSending || pendingUploadCount > 0,
                    responseStartedAt: activeProposalEvent.flatMap { responsePresentation.startTime(for: $0.id) },
                    conflict: visibleConfirmationConflict(for: thread),
                    refreshDirection: refreshDirection,
                    speechCheckTimedOut: speechCheckTimedOut,
                    action: performAction
                )
            } else {
                if failedWorkspacePresentation == .preparationRetry {
                    FailedStage(
                        title: "Clip preparation needs another try",
                        bodyText: fullThread?.preparationMessage ?? fullThread?.lastAssistantErrorMessage ?? "Kria couldn’t prepare your clips. Your direction and footage are still saved.",
                        retryLabel: "Retry preparing clips",
                        retry: fullThread?.preparationRetryable == true ? { Task { await send(message: "Retry preparing my clips.") } } : nil
                    ).id("failed-preparation")
                } else {
                    FailedStage(
                        title: fullThread?.lastAssistantErrorTitle ?? "Your video wasn’t made",
                        bodyText: fullThread?.lastAssistantErrorContent
                            ?? fullThread?.job?.failureMessage
                            ?? "Your direction and footage are saved. Send a message to try again or change direction.",
                        retryLabel: "Retry approved edit",
                        retry: fullThread?.failedRenderExecutionID != nil ? { Task { await retryApprovedRender() } } : nil,
                        nonRetryableReasonCode: fullThread?.lastAssistantErrorCode,
                        declineReason: fullThread?.lastAssistantDeclineReason,
                        replyInChat: { composerFocused = true }
                    ).id("failed")
                }
            }
        }
    }

    @ViewBuilder
    private func deviceRenderPanel(_ key: DeviceRenderKey) -> some View {
        DeviceRenderPanel(key: key, sessions: model.deviceRenders,
            retryApproved: model.deviceRenders.retryNeedsApproval(key) ? { await retryApprovedRender() } : nil,
            // The edit needs a change only the conversation can make: hand over the composer.
            reviewInChat: { composerFocused = true }) {
            await refreshCapabilities()
            await refreshDeviceRender(retry: true)
            // The retry re-pins the delivery server-side without a thread revision;
            // re-read the project so drawer, detail and chat leave Needs attention.
            await refreshNow()
        }
    }

    /// The card of the video "Review in chat" came from, when it is not this thread's
    /// current job (`AppModel.chatDeviceRenderFocus`): its own way forward (Update Kria,
    /// Render on this iPhone, Try again, a change in the composer) against its own record.
    private func routedDeviceRenderPanel(_ key: DeviceRenderKey) -> some View {
        DeviceRenderPanel(key: key, sessions: model.deviceRenders,
            reviewInChat: { composerFocused = true }) {
            await refreshCapabilities()
            await refreshDeviceRender(key, retry: true)
        }
    }

    private var failedWorkspacePresentation: FailedWorkspacePresentation {
        FailedWorkspacePresentation.resolve(
            runtimeVersion: fullThread?.runtimeVersion,
            preparationFailed: fullThread?.preparationFailed == true,
            hasConfirmablePlan: fullThread?.creatorAgent?["plan_hash"]?.stringValue?.isEmpty == false
        )
    }

    /// Mirrors `stageContent`: whether the runtime-v1 confirmation card is on screen.
    private var showsCreationConfirmation: Bool {
        guard fullThread != nil else { return false }
        switch workspaceStage {
        case .direction: return approval == nil
        case .failed: return failedWorkspacePresentation == .legacyCreationConfirmation
        default: return false
        }
    }

    private func visibleConfirmationConflict(for thread: CreationThread) -> CreationConfirmationConflict? {
        guard let confirmationConflict, confirmationConflict.planIdentity == thread.creatorPlanIdentity else { return nil }
        return confirmationConflict
    }

    /// Asks Kria to plan again against the footage attached now, so a direction
    /// the server rejected as stale can be confirmed.
    private func refreshDirection() {
        Task { await send(message: CreationConfirmationConflict.refreshDirectionMessage) }
    }

    private func loadCompletedThoughtSummaries() async {
        guard let response = try? await model.api.creationThoughtSummaryHistory(threadID: project.id) else { return }
        let history = Dictionary(grouping: response.summaries.filter {
            $0.status == .completed && $0.hasText && $0.clientRequestID != nil
        }, by: { $0.clientRequestID! })
        // A history request started on appear can finish after a newer live
        // turn; keep the completed record that was already shown for that turn.
        completedThoughtSummaries.merge(history) { current, _ in current }
    }

    /// KRI-756: the newest chat message, if it is an assistant question nobody has answered. Chips yield to it
    /// (choice card, clip / song-order / song-range question, reply chips, exact-string options).
    private var newestMessageIsOpenQuestion: Bool {
        for event in events.reversed() {
            if let message = ChatTranscriptMessage.from(event: event) { return message.asksAQuestion }
        }
        return false
    }

    /// KRI-756: which suggestion surface the chat is on right now, if any.
    private var suggestionSurface: SuggestionSurface? {
        // Capability off (the default): none of the work below runs, so old servers cost nothing.
        guard SuggestionChipsPolicy.isEnabled(capabilities) else { return nil }
        return SuggestionChipsPolicy.surface(
            enabled: SuggestionChipsPolicy.isEnabled(capabilities), stage: workspaceStage,
            isSlides: selectedFormat == .slides || isSlidePostProject, isFormatKnown: isFormatKnown,
            readyMediaCount: readyMediaCount, pendingUploadCount: pendingUploadCount,
            hasUserMessage: events.contains { $0.eventType == "user_message" },
            hasPendingMessage: !pendingMessages.isEmpty || isSending || isThinking || isActing,
            hasOpenQuestion: newestMessageIsOpenQuestion
        )
    }

    /// Which set a fetched answer belongs to. A set from another scope is never drawn: other footage on the
    /// first prompt, another chat once ready. The thread revision is NOT part of it, so a held ready set does not
    /// blink while the app re-asks after an unrelated event.
    private var suggestionScopeKey: String {
        switch suggestionSurface {
        case .firstPrompt?:
            return "first|\(project.id)|\(readyMediaCount)|\(CreationAttachedMedia.parse(threadState).map(\.id).joined(separator: ","))"
        case .ready?:
            return "ready|\(project.id)"
        case nil:
            return "idle"
        }
    }

    /// What re-asks the server: a new scope, or (once ready) a new revision, which may carry a new render.
    private var suggestionFetchKey: String {
        suggestionSurface == .ready ? "\(suggestionScopeKey)|\(threadRevision)|\(suggestionLedger.refreshToken)" : suggestionScopeKey
    }

    /// The v2 cards for the finished video, or nil when the chat is not on the ready surface or the server's set has
    /// none (then the chips stay in charge). The same value feeds the ready stage in the chat and in the editor's chat sheet.
    private var readyCardsPresentation: SuggestionCardsPresentation? {
        guard SuggestionChipsPolicy.isEnabled(capabilities) else { return nil }
        // A spent paid card leaves the set at once, but its image does not: the row outlives the set it came from, and
        // it shows while the tap's own turn is still running (that is when the image is being made), which hides cards.
        let onSurface = suggestionSurface == .ready && suggestionSetKey == suggestionScopeKey
        guard onSurface || generatedCards.result != nil else { return nil }
        var served: [SuggestionCard] = []
        if onSurface, case .cards(let found) = SuggestionReadyContent.resolve(set: suggestionSet) {
            served = found
        } else if generatedCards.result == nil {
            return nil
        }
        return SuggestionCardsPresentation(
            cards: suggestionLedger.visible(served),
            isSelected: { SuggestionDraft.isSelected($0, in: promptDraft.text) },
            toggle: { promptDraft.set(SuggestionDraft.toggle($0, in: promptDraft.text)) },
            generated: generatedCards.result,
            reviewGenerated: { reviewGeneratedCard() },
            dismissGenerated: { generatedCards.dismiss() }
        )
    }

    /// Hands the finished image to the overlay review, the same screen "Add media" uses for an image made there, so the
    /// creator sees it before it goes anywhere and accepts it in the one place that does that.
    private func reviewGeneratedCard() {
        guard selectedFormat != nil, selectedFormat != .slides, let accountID = auth.localStorageIdentity,
              let itemID = fullThread?.activePlanItemID, let title = generatedCards.result?.title else { return }
        let subject = (fullThread?.title ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard generatedCards.stageForReview(prompt: subject.isEmpty ? title : subject, accountID: accountID, itemID: itemID) else { return }
        generatedCards.dismiss()
        // Not `openAttachments`: the image is ready while the tap's own turn may still be closing, and the creator who
        // pays for an image must not find Review dead for those seconds.
        attachmentLaunch = .generatedOverlays
        attachmentScript = nil
        scrollRequest += 1
        guard showsResult else {
            showsAttachments = true
            return
        }
        // The editor's chat sheet is up: it closes first, because a sheet cannot open while another is still leaving.
        editorConversationCloseID = UUID()
        Task {
            try? await Task.sleep(for: .milliseconds(450))
            showsAttachments = true
        }
    }

    /// The chips to draw for one surface; empty unless the chat is on that surface right now.
    private func suggestionChips(for surface: SuggestionSurface) -> [SuggestionChip] {
        guard suggestionSurface == surface, suggestionSetKey == suggestionScopeKey else { return [] }
        // Typing their own prompt: the starter chips step aside (a tap would also replace the draft).
        if surface == .firstPrompt, !prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return [] }
        return suggestionSet?.chips(forState: surface.rawValue) ?? []
    }

    private func loadSuggestions() async {
        guard suggestionSurface != nil else {
            suggestionSet = nil
            suggestionSetKey = nil
            return
        }
        let scope = suggestionScopeKey
        // Another scope's set is dropped at once; the same scope keeps what it holds until a new set_id arrives.
        if suggestionSetKey != scope { suggestionSet = nil; suggestionSetKey = scope }
        let api = model.api, threadID = project.id
        suggestionsLoading = true
        defer { suggestionsLoading = false }
        await SuggestionChipsPolicy.poll(
            fetch: { try await api.creationSuggestions(threadID: threadID) },
            onSet: {
                suggestionSet = SuggestionChipsPolicy.merge(current: suggestionSet, incoming: $0)
                suggestionLedger.reconcile(with: suggestionSet)
            }
        )
    }

    /// Send from either composer. A draft that is exactly one offered card's prompt goes out pinned to that card, so the
    /// server runs the action it proved (the exact hook text, the image the creator asked for). The pin names the set the
    /// creator SAW; the server decides if it still fits, and a refused pin comes back as an ordinary accepted turn whose
    /// reply says the card no longer fits. Anything else (other words, several prompts) is a plain message the planner
    /// reads, and every card whose prompt it carried is handled for this set.
    private func sendComposerDraft() async {
        let cards = readyCardsPresentation?.cards ?? []
        if let card = SuggestionDraft.pinnedCard(for: promptDraft.text, among: cards),
           !isSending, !isActing, !isThinking, let setID = suggestionSet?.setID, suggestionLedger.begin(card) {
            let tap = SuggestionTap(setID: setID, chipID: card.id)
            await send(message: card.prompt, suggestion: tap) { _ in
                suggestionLedger.finish(card, accepted: true)
                // The tap spends on the server; this only watches for the image it makes.
                if card.needsSpendConsent, let itemID = fullThread?.activePlanItemID {
                    generatedCards.begin(card: card, setID: setID, itemID: itemID, accountID: auth.localStorageIdentity, title: card.title, api: model.api)
                }
            }
            // Nothing reached the server (offline, rejected, blocked): the card is tappable again.
            suggestionLedger.finish(card, accepted: false)
            return
        }
        let carried = SuggestionDraft.cards(in: promptDraft.text, among: cards)
        await send(suggestionOutcome: carried.isEmpty ? nil : { _ in carried.forEach { suggestionLedger.markApplied($0) } })
    }

    /// A chip tap is a real send of the chip's `message` (the old fixed chips only filled the composer), tagged
    /// with the offered set and chip so the server can attribute it. It rides the ordinary send path, so the
    /// optimistic bubble, idempotency key and revision handling are shared.
    private func sendSuggestion(_ chip: SuggestionChip) {
        guard !isSending, !isActing, !isThinking else { return }
        let tap = suggestionSet?.setID.map { SuggestionTap(setID: $0, chipID: chip.id) }
        sendingSuggestionID = chip.id
        Task {
            await send(message: chip.message, suggestion: tap, keepsDraft: true)
            sendingSuggestionID = nil
        }
    }

    private func send(message submittedMessage: String? = nil, clipSelection: ClipSelectionSubmission? = nil, songOrder: SongOrderSubmission? = nil, choiceSelection: ChoiceSelectionSubmission? = nil, songRange: SongRangeAnswer? = nil, suggestion: SuggestionTap? = nil, protectedRegion: ProtectedRegionAnswer? = nil, markQuestionID: String? = nil, momentOrder: MomentOrderAnswer? = nil, keepsDraft: Bool = false, retrying: ChatSendReceipt? = nil, suggestionOutcome: ((SuggestionTapOutcome) -> Void)? = nil) async {
        // Steer now: a typed message may go while Kria works; it waits as "Up next". Card answers and chips still wait.
        let composerSend = submittedMessage == nil && clipSelection == nil && songOrder == nil && choiceSelection == nil
            && songRange == nil && suggestion == nil && protectedRegion == nil && momentOrder == nil && retrying == nil
        guard selectedFormat != .slides, !isSending, !isActing, !isThinking || (steerNowEnabled && composerSend) else { return }
        if steerNowEnabled, composerSend, upNext != nil {
            // The server keeps one message waiting. A third stays in the composer with a plain note, never an error card.
            steerNote = steerStrings.oneAlreadyWaiting
            return
        }
        if let retrying {
            guard let current = chatDrafts.submission(for: project.id),
                  current.clientEventID == retrying.clientEventID, current.phase == .unconfirmed else {
                synchronizeSubmissionStorage()
                return
            }
        }
        guard sendReceipt == nil || retrying?.clientEventID == sendReceipt?.clientEventID else { return }
        guard let message = retrying?.message ?? ChatSubmission.message(
            text: submittedMessage ?? prompt, readyMediaCount: readyMediaCount,
            pendingUploadCount: pendingUploadCount
        ), !ChatSubmission.isBareCorrectionStub(message) else { return }
        let draftToRestore = retrying?.draft ?? (keepsDraft ? "" : (submittedMessage == nil ? prompt : message))
        var submission = retrying.map {
            ChatTurnSubmissionIdentity(message: $0.message, clientEventID: $0.clientEventID, expectedRevision: $0.expectedRevision)
        } ?? ChatTurnSubmissionIdentity(message: message, clientEventID: UUID().uuidString, expectedRevision: threadRevision)
        var receipt = retrying ?? ChatSendReceipt(message: message, clientEventID: submission.clientEventID,
            expectedRevision: submission.expectedRevision, afterSequence: afterSequence,
            draft: draftToRestore, runtimeVersion: currentProject.runtimeVersion ?? 1)
        if retrying == nil {
            if let songRange { receipt.responseContext = .songRange(songRange.questionID) }
            else if let momentOrder { receipt.responseContext = .momentOrder(momentOrder.questionID) }
            else if protectedRegion != nil { receipt.responseContext = .protectedRegion(markQuestionID) }
        }
        let songRangeQuestionID = receipt.responseContext?.songRangeQuestionID
        let momentOrderQuestionID = receipt.responseContext?.momentOrderQuestionID
        ownsSendAttempt = true
        do {
            guard try chatDrafts.beginSubmission(receipt, for: project.id) else {
                ownsSendAttempt = false
                synchronizeSubmissionStorage()
                return
            }
        } catch {
            ownsSendAttempt = false
            failure = ChatFailure("Your message is still here. It could not be saved for recovery.", error: error)
            return
        }
        sendReceipt = receipt
        pendingTurnSubmission = submission
        model.clearChatDeviceRenderFocus()
        failure = nil
        confirmationConflict = nil
        defer {
            ownsSendAttempt = false
            chatDrafts.endSubmission(clientEventID: receipt.clientEventID, for: project.id)
        }
        // Steer now: sent while Kria works, the message goes straight to the bar (it will wait), never a chat bubble.
        let waitsBehindKria = steerNowEnabled && composerSend && kriaIsBusy
        if waitsBehindKria {
            pendingWaitingText = message
            waitingClientEventID = submission.clientEventID
        }
        defer {
            if waitsBehindKria {
                pendingWaitingText = nil
                waitingClientEventID = nil
            }
        }
        nextLocalOrder += 1
        let optimistic = submissionAnchor ?? ChatPendingMessage(content: message, clientEventID: submission.clientEventID,
            afterSequence: receipt.afterSequence, localOrder: nextLocalOrder, songOrder: songOrder, songRange: songRange,
            protectedRegion: protectedRegion, momentOrder: momentOrder)
        submissionAnchor = optimistic
        if !pendingMessages.contains(where: { $0.id == optimistic.id }) {
            withAnimation(reduceMotion ? .easeOut(duration: 0.15) : .spring(duration: 0.42, bounce: 0.18)) {
                pendingMessages.append(optimistic)
            }
        }
        if !keepsDraft && retrying == nil { prompt = "" }
        if retrying == nil { answeringQuestion = nil }
        scrollRequest += 1
        // Edits staged from a chat draft already live in the server's draft head;
        // committing them here would render on every send. Only real local edits flush.
        // Runtime-v2 + server `editor_state_turns`: the copilot continues on top of the editor's
        // CURRENT unsaved state, so nothing is saved or rendered here.
        var editorState: EditorStateRequest?
        if retrying?.requestBody == nil && retrying?.phase != .unconfirmed {
            if currentProject.runtimeVersion == 2 {
                if Date().timeIntervalSince(capabilitiesReadAt ?? .distantPast) > 30 {
                    await refreshEditorStateCapability()
                }
                if capabilities?.editorStateTurnsEnabled == true {
                    // KRI-529: stage the previous chat edit first; the server ignores its own head
                    // draft when it receives this state, so an unstaged edit would be dropped.
                    editorState = await editorSession.exportEditorStateSyncingDraft(maxBytes: capabilities?.editorStateMaxBytes)
                }
            }
            if editorState == nil, editorSession.hasUnsavedChanges, !editorSession.hasOnlyChatStagedChanges {
                await editorSession.save()
                guard !editorSession.hasUnsavedChanges else {
                    rejectSubmission(optimistic)
                    failure = ChatFailure("Your message is still here. Save or resolve your editor changes before sending it.")
                    return
                }
                await refreshNow()
            }
        }
        if receipt.phase == .preparing {
            submission = ChatTurnSubmissionIdentity(message: message, clientEventID: receipt.clientEventID,
                                                    expectedRevision: threadRevision)
            receipt.expectedRevision = threadRevision
            pendingTurnSubmission = submission
        }
        guard model.chatDrafts.submission(for: project.id)?.clientEventID == receipt.clientEventID else { return }
        let earlierThoughts = (requestID: thoughtRequestID, summaries: liveThoughtSummaries)
        thoughtRequestID = submission.clientEventID
        liveThoughtSummaries = []
        songRangeProblem = nil
        if receipt.runtimeVersion != 2 {
            receipt.phase = .unconfirmed
            do { try model.chatDrafts.setSubmission(receipt, for: project.id); sendReceipt = receipt }
            catch { rejectSubmission(optimistic); failure = ChatFailure("Your message could not be saved for recovery.", error: error); return }
            let requestSequence = projectionOrder.begin()
            do {
                let thread = try await model.api.sendCreationMessage(threadID: project.id, message: message, expectedRevision: submission.expectedRevision, clientEventID: submission.clientEventID)
                if model.chatDrafts.submission(for: project.id)?.clientEventID == receipt.clientEventID {
                    receipt.phase = .accepted
                    try? model.chatDrafts.setSubmission(receipt, for: project.id)
                    sendReceipt = receipt
                }
                pendingTurnSubmission = nil
                model.uploads.clearUnreadableFailures(projectID: project.id)
                apply(thread, requestSequence: requestSequence)
                suggestionOutcome?(.accepted)
                conversationAcceptedID = UUID()
                await editorSession.synchronizePromptRevision()
                await fetchFinalThoughtSummary(requestID: submission.clientEventID)
            } catch APIError.conflict {
                thoughtRequestID = nil; liveThoughtSummaries = []
                pendingMessages.removeAll { $0.id == optimistic.id }
                recoverSubmissionDraft()
                pendingTurnSubmission = nil
                submissionAnchor = nil
                clearSubmissionReceipt()
                await refreshNow()
                failure = ChatFailure("This conversation changed. Your message is still here; review the latest direction and send again.")
            } catch {
                thoughtRequestID = nil; liveThoughtSummaries = []
                if ChatSendReceipt.isRejected(error) {
                    rejectSubmission(optimistic)
                    failure = ChatFailure("Your message was not sent. Your draft is ready to edit.", error: error)
                    return
                }
                await refreshNow()
                guard sendReceipt != nil else { return }
                failure = ChatFailure(
                    "Kria couldn’t confirm that message. Retry message checks the same submission safely.",
                    cause: RequestFailureCause(error)
                )
            }
            return
        }
        let accepted: TurnAccepted
        do {
            var request = SubmitTurnRequest(
                message: message, clientEventID: submission.clientEventID,
                expectedThreadRevision: submission.expectedRevision, editorState: editorState,
                clipSelection: clipSelection, songOrder: songOrder, choiceSelection: choiceSelection,
                songRange: songRange, suggestion: suggestion, protectedRegion: protectedRegion, momentOrder: momentOrder
            )
            if capabilities?.requestOutcomeContext == true {
                request.outcomeContext = .forSubmission(
                    events: events,
                    continuation: clipSelection != nil || songOrder != nil || choiceSelection != nil
                        || songRange != nil || protectedRegion != nil || momentOrder != nil,
                    choice: choiceSelection
                )
            }
            if let frozen = retrying?.requestBody { request.frozenBody = frozen }
            receipt.requestBody = try JSONEncoder().encode(request)
            receipt.phase = .unconfirmed
            try model.chatDrafts.setSubmission(receipt, for: project.id)
            sendReceipt = receipt
            accepted = try await model.api.submitTurn(threadID: project.id, request: request)
        } catch let error as APIError where error == .conflict {
            thoughtRequestID = nil; liveThoughtSummaries = []
            pendingMessages.removeAll { $0.id == optimistic.id }
            let staleSongRange = songRangeQuestionID != nil && error.conflictCode == SongRangeAnswer.staleConflictCode
            // KRI-723: a moments answer is the card's, never composer text to restore.
            if !staleSongRange && momentOrderQuestionID == nil { recoverSubmissionDraft() }
            pendingTurnSubmission = nil
            submissionAnchor = nil
            clearSubmissionReceipt()
            if let thread = try? await model.api.project(threadID: project.id) { apply(thread) }
            // KRI-588: the question is already closed on the server. Refresh and close the card here too, with
            // no alert: there is nothing left for the creator to fix.
            if staleSongRange, let songRangeQuestionID {
                closedSongRangeQuestions.insert(songRangeQuestionID)
                return
            }
            // KRI-374: the order was answered against a question Kria has since replaced. The thread refresh above
            // brings the new question (or the settled plan); say so instead of a generic conflict.
            // KRI-723: the moments question was replaced (or the take re-uploaded). The refresh above brings the newest
            // card (the old one hides as superseded); say why, in the chat's language, instead of a generic conflict.
            if momentOrderQuestionID != nil, error.conflictCode == MomentOrderAnswer.staleCode {
                failure = ChatFailure(MomentOrderStrings(ClipAnswerLanguage(threadState: threadState)).stale)
                return
            }
            if error.conflictCode == SongOrderSubmission.staleConflictCode {
                failure = ChatFailure("Kria checked your clips again, so that order is out of date. Review the latest order and confirm it.")
                return
            }
            // KRI-295: surface the server's real reason; stay quiet when the expired-approval notice already explains.
            failure = submitTurnConflictMessage(
                detail: error.conflictDetail, approvalExpired: approvalNotice != nil
            ).map { ChatFailure($0) }
            return
        } catch let error as APIError where receipt.responseContext?.isProtectedRegion == true && ProtectedRegionProblem.isMarkProblem(error) {
            // KRI-901: the server refused the mark (not this thread's clip, or too small). Say why on the card.
            thoughtRequestID = nil; liveThoughtSummaries = []
            pendingMessages.removeAll { $0.id == optimistic.id }
            pendingTurnSubmission = nil
            submissionAnchor = nil
            clearSubmissionReceipt()
            if let questionID = receipt.responseContext?.protectedRegionQuestionID {
                protectedRegionProblem = ProtectedRegionProblem(questionID: questionID, error: error)
            }
            return
        } catch let error as APIError where momentOrderQuestionID != nil && MomentOrderProblem.isAnswerProblem(error) {
            // KRI-723: the server refused the answer (not this thread's take, bad bounds, overlap). Say why on the card.
            thoughtRequestID = nil; liveThoughtSummaries = []
            pendingMessages.removeAll { $0.id == optimistic.id }
            pendingTurnSubmission = nil
            submissionAnchor = nil
            clearSubmissionReceipt()
            if let momentOrderQuestionID {
                momentOrderProblem = MomentOrderProblem(questionID: momentOrderQuestionID, error: error,
                                                        language: ClipAnswerLanguage(threadState: threadState))
            }
            return
        } catch let error as APIError where songRangeQuestionID != nil && SongRangeProblem.isInvalid(error) {
            // KRI-588: the server refused the range. Say why on the card; the answer is not a draft to restore.
            thoughtRequestID = nil; liveThoughtSummaries = []
            pendingMessages.removeAll { $0.id == optimistic.id }
            pendingTurnSubmission = nil
            submissionAnchor = nil
            clearSubmissionReceipt()
            if let songRangeQuestionID { songRangeProblem = SongRangeProblem(questionID: songRangeQuestionID, error: error) }
            return
        } catch {
            thoughtRequestID = nil; liveThoughtSummaries = []
            if sendReceipt?.phase == .preparing || ChatSendReceipt.isRejected(error) {
                rejectSubmission(optimistic)
                failure = ChatFailure("Your message was not sent. Your draft is ready to edit.", error: error)
                return
            }
            await refreshNow()
            guard sendReceipt != nil else { return }
            failure = ChatFailure("Kria couldn’t confirm that message. Retry message checks the same submission safely.", error: error)
            return
        }
        // A poll may already have acknowledged this identity while POST was returning.
        if sendReceipt?.clientEventID == receipt.clientEventID,
           model.chatDrafts.submission(for: project.id)?.clientEventID == receipt.clientEventID {
            receipt.phase = .accepted
            receipt.turnID = accepted.turnID
            try? model.chatDrafts.setSubmission(receipt, for: project.id)
            sendReceipt = receipt
        }
        pendingTurnSubmission = nil
        // The banner said these files won't be sent. Only now that the server has the message has it
        // done its job; a conflict, a network error or an unsaved editor leaves it (and the draft) alone.
        model.uploads.clearUnreadableFailures(projectID: project.id)
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: accepted.threadRevision)
        suggestionOutcome?(.accepted)
        conversationAcceptedID = UUID()
        steerNote = nil
        let waitsUpNext = steerNowEnabled && accepted.status == "queued"
        if waitsUpNext {
            // Steer now: the message waits as "Up next"; whatever Kria is doing keeps its own Thinking row.
            thoughtRequestID = earlierThoughts.requestID
            liveThoughtSummaries = earlierThoughts.summaries
        } else {
            thinkingAnchor = receipt.afterSequence
            thinkingTurnID = accepted.turnID
            thinkingSettlesOnJobStatus = false
            isThinking = true
            settleThinking()
        }
        failure = await acceptedMutationRefreshError(
            "Your message was sent, but the conversation couldn’t refresh.",
            refresh: { try await refreshDelta() }
        )
        if !waitsUpNext { await fetchFinalThoughtSummary(requestID: submission.clientEventID) }
    }

    /// A turn can settle before the one-second live poll sees its completed
    /// record. Read once after the successful request path, then let the
    /// timeline own completed display so it cannot duplicate the live row.
    private func fetchFinalThoughtSummary(requestID: String) async {
        guard let response = try? await model.api.creationThoughtSummaries(threadID: project.id, clientRequestID: requestID),
              response.clientRequestID == requestID else { return }
        liveThoughtSummaries = response.summaries.filter { $0.hasText }
        let completed = liveThoughtSummaries.filter { $0.status == .completed }
        if !completed.isEmpty { completedThoughtSummaries[requestID] = completed }
    }

    /// Why the project can't switch to `format` right now (the clips it already holds don't fit), or nil.
    private func formatSwitchBlock(_ format: CreationFormat) -> String? {
        if format.usesVisualPool, attachedClipCount > 0 {
            return "Sliders use Photos & videos. Remove primary footage before switching formats."
        }
        let pendingClips = model.uploads.records.filter { $0.projectID == project.id && $0.role == .clip }.count
        return formatClipCapacityError(format: format, clipLimit: CreationFormat.maximumClipCount(for: format, serverLimits: maximumClipsByFormat), occupiedClipCount: attachedClipCount + pendingClips)
    }

    private func selectFormat(_ format: CreationFormat) {
        guard capabilitiesAreAuthoritative, availableFormats.contains(format) else { return }
        if let blocked = formatSwitchBlock(format) {
            failure = ChatFailure(blocked)
            return
        }
        performAction("select_format", payload: ["format": .string(format.serverValue)])
    }

    private func performAction(_ action: String, payload: [String: JSONValue] = [:]) {
        guard !isActing, !isSending else { return }
        isActing = true
        failure = nil
        confirmationConflict = nil
        let identity = CreationActionIdentity.reusing(pendingAction, action: action, payload: payload, revision: threadRevision)
        pendingAction = identity
        Task {
            defer { isActing = false }
            do {
                let requestSequence = projectionOrder.begin()
                let thread = try await model.api.creationAction(threadID: project.id, action: action, payload: payload, expectedRevision: identity.revision, clientActionID: identity.id)
                pendingAction = nil
                apply(thread, requestSequence: requestSequence)
                if action == "select_format" { isChoosingFormat = false }
                failure = await acceptedMutationRefreshError("Saved, but the conversation couldn’t refresh.", refresh: { try await refreshDelta() })
            } catch let error as APIError where error == .conflict {
                pendingAction = nil
                await refreshCapabilities()
                await refreshNow()
                guard CreationConfirmationConflict.confirmationActions.contains(action), payload["variant_id"] == nil else {
                    failure = ChatFailure(nonConfirmationConflictMessage(
                        action: action,
                        detail: error.conflictDetail,
                        planIdentity: fullThread?.creatorPlanIdentity ?? ""
                    ))
                    return
                }
                let conflict = CreationConfirmationConflict(
                    detail: error.conflictDetail,
                    planIdentity: fullThread?.creatorPlanIdentity ?? ""
                )
                if showsCreationConfirmation { confirmationConflict = conflict }
                else { failure = ChatFailure(conflict.message) }
            } catch is CancellationError {
                // The view went away mid-request; leave pendingAction alone so an
                // in-flight duplicate can still be deduplicated if this task is
                // somehow still observed.
            } catch {
                // Any other failure must clear pendingAction and refresh, or a
                // retry replays this identity's now-stale `expected_revision`
                // and loops on 409s forever.
                pendingAction = nil
                await refreshCapabilities()
                await refreshNow()
                failure = ChatFailure("That change wasn’t saved.", error: error)
            }
        }
    }

    /// Lightweight re-read that only updates `capabilities` on success (no banner, no device-render
    /// reconcile), so a transient failure keeps the last-known value.
    private func refreshEditorStateCapability() async {
        guard let response = try? await model.api.creationCapabilities() else { return }
        capabilities = response
        capabilitiesReadAt = Date()
    }

    private func refreshCapabilities(preloaded: CreationCapabilities? = nil) async {
        do {
            let response: CreationCapabilities
            if let preloaded { response = preloaded } else { response = try await model.api.creationCapabilities() }
            var limits: [CreationFormat: Int] = [:]
            let formats = response.formats.compactMap { capability -> CreationFormat? in
                guard let format = CreationFormat(serverValue: capability.id) else { return nil }
                limits[format] = max(1, capability.maxClips)
                return format
            }
            // The service advertises capability, not presentation order. Keep
            // Lip-sync as the final card (Slider just before it) even if a rollout reorders its list.
            availableFormats = formats.sorted { $0.carouselOrder < $1.carouselOrder }
            capabilities = response
            capabilitiesReadAt = Date()
            capabilitiesFailure = formats.isEmpty ? ChatFailure("No creation formats are currently available. Try again in a moment.") : nil
            maximumClipsByFormat = limits
            capabilitiesAreAuthoritative = true
            // Initial history and capabilities load independently. Reconcile
            // only after this authoritative response arrives; substituting a
            // temporary `.disabled` capability can terminalize a device job.
            await refreshDeviceRender()
        } catch is CancellationError {
            // The view went away mid-request; leave the last-known state as-is.
        } catch let error as URLError where error.code == .cancelled {
            // URLSession reports a cancelled task (e.g. the attachment sheet
            // closing) this way; it must not wipe what another request loaded.
        } catch {
            capabilitiesAreAuthoritative = false
            capabilities = nil
            availableFormats = []
            capabilitiesFailure = ChatFailure("Kria couldn’t load creation options.", error: error)
        }
    }

    private func pollUntilDismissed() async {
        await refreshNow()
        await refreshDeviceRender()
        if let routedKey = routedDeviceRenderKey { await refreshDeviceRender(routedKey) }
        var delay: UInt64 = 1_000_000_000
        while !Task.isCancelled {
            do {
                let changed = try await refreshDelta()
                // A published device render never changes on its own; keep
                // polling it only while it is unsettled or the thread moved.
                let deviceSettled = serverDeviceRenderKey.map { model.deviceRenders.isSettled($0) } ?? false
                if changed || !deviceSettled || currentProject.status == .rendering { await refreshDeviceRender() }
                if let routedKey = routedDeviceRenderKey { await refreshDeviceRender(routedKey) }
                await refreshWhileCheckingSpeech()
                delay = changed || isSending || isActing || isThinking || currentProject.status == .rendering
                    || fullThread?.preparationIsActive == true || (speechCleanupIsChecking && !speechCheckTimedOut)
                    ? 1_000_000_000 : min(delay * 2, 8_000_000_000)
            } catch is CancellationError {
                return
            } catch {
                if !isUITesting {
                    failure = RequestFailureCause(error) == .connection
                        ? ChatFailure("Kria lost the live connection. Your conversation is safe.", cause: .connection)
                        : ChatFailure("Kria couldn’t refresh this conversation.", error: error)
                }
                delay = min(delay * 2, 15_000_000_000)
            }
            try? await Task.sleep(nanoseconds: delay)
        }
    }

    /// The analysis task never bumps the thread revision or appends an event, so `/delta` cannot
    /// announce it finishing. While the card says "Checking speech…", re-read the full thread on
    /// `SpeechCheckWatch`'s schedule. This loop awaits it, so reads never overlap each other.
    private func refreshWhileCheckingSpeech() async {
        let resolution = SpeechCleanupOffer.resolve(fullThread?.speechCleanup)
        let shouldRefetch = speechCheckClock.watch.observe(resolution.offer, analysisID: resolution.analysisID, now: .now)
        let timedOut = speechCheckClock.watch.hasTimedOut
        if timedOut != speechCheckTimedOut { speechCheckTimedOut = timedOut }
        guard shouldRefetch else { return }
        let requestSequence = projectionOrder.begin()
        guard let thread = try? await model.api.project(threadID: project.id), !Task.isCancelled else { return }
        apply(thread, requestSequence: requestSequence)
    }

    /// True while the v2 approval card's speech-check analysis is queued,
    /// running, or not yet created -- keeps `pollUntilDismissed`'s fast 1s
    /// cadence instead of backing off to 8s, so "Checking speech…" resolves
    /// promptly whether it started from the approval or from a fresh 409.
    private var speechCleanupIsChecking: Bool {
        SpeechCleanupOffer.resolve(fullThread?.speechCleanup).offer == .checking
    }

    /// The device card's key. Nil once the server has no device render behind it
    /// (job or record gone, editor draft empty), so the job's own status shows instead.
    private var deviceRenderKey: DeviceRenderKey? {
        serverDeviceRenderKey.flatMap { model.deviceRenders.isInactive($0) ? nil : $0 }
    }

    /// Polled and shown beside the conversation until it settles or the creator sends a
    /// message (a change there makes a new job, whose card the stage shows).
    private var routedDeviceRenderKey: DeviceRenderKey? {
        guard let key = model.chatDeviceRenderFocus, key.projectID == project.id,
              key != serverDeviceRenderKey, !model.deviceRenders.isSettled(key),
              !model.deviceRenders.isInactive(key) else { return nil }
        return key
    }

    private var serverDeviceRenderKey: DeviceRenderKey? {
        guard let job = fullThread?.job, let jobID = UUID(uuidString: job.id),
              let variant = job.variants.first(where: {
                  $0.renderDestination == "device" || $0.renderStatus == "awaiting_device" || $0.deliveryPhase != nil
              }), let variantID = variant.variantID else { return nil }
        return DeviceRenderKey(projectID: project.id, jobID: jobID, variantID: variantID)
    }

    private func refreshDeviceRender(_ key: DeviceRenderKey? = nil, retry: Bool = false) async {
        // A failed capability read keeps the last set the server enabled; it
        // never substitutes `.disabled`, which reads as "not available" (S4-d).
        guard let deviceRenderKey = key ?? serverDeviceRenderKey,
              let phoneRendering = capabilitiesAreAuthoritative
                ? capabilities?.phoneRendering : model.deviceRenders.lastEnabledCapabilities else { return }
        await model.deviceRenders.reconcile(deviceRenderKey, capabilities: phoneRendering, retry: retry)
    }

    private func retryApprovedRender() async {
        guard !isActing, !isSending else { return }
        isActing = true
        defer { isActing = false }
        if fullThread?.failedRenderExecutionID == nil { await refreshNow() }
        guard let executionID = fullThread?.failedRenderExecutionID else {
            // The authoritative projection may already have moved on. Its
            // existing live poll follows the result; there is no exact failed
            // execution to retry or a manual reload the creator must perform.
            return
        }
        do {
            let accepted = try await model.api.retryApprovedRender(threadID: project.id,
                executionID: executionID, expectedRevision: threadRevision)
            threadRevision = max(threadRevision, accepted.threadRevision)
            await refreshNow()
            if let approvalID = UUID(uuidString: accepted.approvalID) {
                let snapshot = try await model.api.approval(threadID: project.id, approvalID: approvalID)
                if snapshot.status == "pending" { approval = snapshot }
            }
        } catch {
            await refreshNow()
            failure = ChatFailure((error as? APIError)?.conflictDetail
                ?? "Kria couldn’t prepare this exact retry. Your edit is still saved.", error: error)
        }
    }

    /// Shows the transcript this device saved last time while the network answers. Events only: the mutable
    /// projection (state, job, variants) still comes from the server, and nothing here can be acted on until it has.
    private func restoreCachedTranscript() async {
        guard let owner = auth.localStorageIdentity,
              let cached = await TranscriptCache.shared.load(owner: owner, threadID: project.id),
              !cached.events.isEmpty, !Task.isCancelled, events.isEmpty else { return }
        events = ChatTranscriptHistory.merge(events, with: cached.events)
        observeResponses()
        // The server's first projection is still the baseline: a reply that landed while the app was closed must
        // not fire a reveal or haptic as if it had just arrived.
        hasHistoryBaseline = false
        afterSequence = ChatTranscriptHistory.nextAfterSequence(current: afterSequence, response: afterSequence, events: events)
        transcriptIsFresh = false
        initialConversationLoaded = true
        markTranscriptVisible(source: "cache")
    }

    /// Saves what the server last confirmed (debounced and written off the main thread by the cache).
    private func scheduleTranscriptSave() {
        guard transcriptIsFresh, !events.isEmpty, let owner = auth.localStorageIdentity else { return }
        let transcript = CachedTranscript(
            threadID: project.id, savedAt: .now, revision: threadRevision,
            runtimeVersion: currentProject.runtimeVersion, events: Array(events.suffix(TranscriptCache.maxEvents))
        )
        Task { await TranscriptCache.shared.save(owner: owner, transcript) }
    }

    /// Closes the open and launch intervals the first time a transcript can be seen (later calls are no-ops).
    private func markTranscriptVisible(source: String) {
        Perf.mark("transcript.visible")
        Perf.end("chat.open", id: project.id.uuidString, "source=\(source)")
        Perf.end("launch.toTranscript", "source=\(source)")
    }

    private func refreshNow() async {
        defer {
            if !Task.isCancelled {
                initialConversationLoaded = true
                markTranscriptVisible(source: "network")
            }
        }
        failure = nil
        do {
            Perf.mark("refresh.start")
            let requestSequence = projectionOrder.begin()
            let thread = try await model.api.project(threadID: project.id)
            Perf.mark("refresh.fullread")
            apply(thread, requestSequence: requestSequence)
            transcriptIsFresh = true
            // The full projection contains the complete transcript. Reveal it
            // before a slow delta or approval request returns, so the user
            // never sees an empty ready state followed by old AI messages.
            if fullThread != nil, !Task.isCancelled {
                initialConversationLoaded = true
                markTranscriptVisible(source: "network")
            }
            if thread.runtimeVersion == 2 {
                _ = try await refreshDelta()
            }
        } catch is CancellationError {
            // The view went away mid-request; leave the last-known state as-is.
        } catch let APIError.requestFailed(status, _) where status == 404 {
            // Deleted elsewhere: the saved copy must not outlive the chat.
            if let owner = auth.localStorageIdentity { await TranscriptCache.shared.remove(owner: owner, threadID: project.id) }
            if !isUITesting { failure = ChatFailure("Kria couldn’t refresh this conversation.", error: APIError.requestFailed(status: 404)) }
        } catch {
            if !isUITesting {
                failure = ChatFailure("Kria couldn’t refresh this conversation.", error: error)
            }
        }
    }

    private func apply(_ thread: CreationThread, requestSequence: Int? = nil) {
        let acceptsProjection = ThreadRevisionOrder.acceptsProjection(
            current: threadRevision,
            incoming: thread.revision
        )
        events = ChatTranscriptHistory.merge(events, with: thread.events)
        observeResponses()

        afterSequence = ChatTranscriptHistory.nextAfterSequence(
            current: afterSequence,
            response: afterSequence,
            events: events
        )
        reconcilePendingMessages()
        // Before the stale-revision guard: a reply delivered by an out-of-order projection still counts.
        settleThinking()
        guard acceptsProjection, projectionOrder.accept(requestSequence) else { return }
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: thread.revision)
        fullThread = thread
        threadState = thread.state ?? [:]
        selectedFormat = CreationFormat(thread: thread) ?? selectedFormat
        model.updateProject(thread.summary)
        if events.contains(where: { $0.role == "user" }) { model.chatKeepers.insert(project.id) }
        scheduleTranscriptSave()
    }

    @discardableResult private func refreshDelta() async throws -> Bool {
        // Runtime-v1 projects predate the delta endpoint but remain part of a
        // creator's real project history. Poll their authoritative full
        // projection so opening an older project never replaces its transcript
        // with an empty chat or leaves a permanent connection error banner.
        if currentProject.runtimeVersion != 2 {
            let previousRevision = threadRevision
            let previousEventIDs = events.map(\.id)
            let requestSequence = projectionOrder.begin()
            let thread = try await model.api.project(threadID: project.id)
            apply(thread, requestSequence: requestSequence)
            reconcilePendingMessages()
            let changed = threadRevision != previousRevision || events.map(\.id) != previousEventIDs
            clearChatRefreshRecoveryMessage(&failure)
            return changed
        }
        let delta = try await model.api.threadDelta(threadID: project.id, afterSequence: afterSequence)
        let revisionBeforeDelta = threadRevision
        // `threadRevision` advances as soon as the delta is acknowledged, even
        // if the subsequent full-projection request fails. Keep the revision
        // of the last successfully applied projection separately so the next
        // idle delta retries a title (or another mutable projection) sync.
        let lastFullProjectionRevision = fullThread?.revision ?? project.serverRevision
        threadRevision = ThreadRevisionOrder.advance(current: threadRevision, incoming: delta.threadRevision)
        let known = Set(events.map(\.id))
        let fresh = delta.events.filter { !known.contains($0.id) }
        events = ChatTranscriptHistory.merge(events, with: fresh)
        observeResponses()
        afterSequence = ChatTranscriptHistory.nextAfterSequence(
            current: afterSequence,
            response: delta.nextAfterSequence,
            events: events
        )
        if let latestFormat = events.reversed().compactMap({ CreationFormat(event: $0) }).first {
            selectedFormat = latestFormat
        }
        reconcilePendingMessages()
        settleThinking()
        // The delta fetch and merge above already succeeded, so the banner is
        // stale regardless of what happens next — clear it here rather than
        // after `synchronizeApproval()`, whose own failure would otherwise
        // re-arm the recovery card underneath messages that just landed
        // successfully.
        clearChatRefreshRecoveryMessage(&failure)
        try await synchronizeApproval()
        // A background mutation, such as assigning the first-prompt title,
        // advances the thread revision but may intentionally be a system event
        // that does not produce a chat row. Delta responses do not contain the
        // mutable thread projection (including `title`), so fetch it whenever
        // the revision advances as well as for ordinary conversational updates.
        if ThreadProjectionRefresh.requiresFullProjection(
            lastFullProjectionRevision: lastFullProjectionRevision,
            incomingRevision: delta.threadRevision,
            receivedEvents: fresh,
            // A device delivery changes without a thread revision (KRI-823).
            isRendering: currentProject.status == .rendering || fullThread?.deliveryNeedsAttention == true
        ) {
            if let thread = try? await model.api.project(threadID: project.id) { apply(thread) }
        }
        // A re-plan can finish as a NEW render job while the editor (or its chat
        // sheet) is open; the thread summary is the authority on the active job.
        if currentProject.runtimeVersion == 2, editorSession.loadState == .loaded,
           fresh.contains(where: { ["generation_ready", "assistant_review"].contains($0.eventType) && $0.payload?["job_id"]?.stringValue != nil }),
           let thread = try? await model.api.project(threadID: project.id) {
            apply(thread)
            await editorSession.adoptLatestJob(thread.summary, api: model.api)
        }
        if !fresh.isEmpty, !isThinking { await editorSession.synchronizePromptRevision() }
        if !fresh.isEmpty { scheduleTranscriptSave() }
        return !fresh.isEmpty || threadRevision > revisionBeforeDelta
    }

    /// Clears `isThinking` once the reply or a render-state event has landed in `events`, no matter
    /// which fetch (delta or full projection) delivered it.
    private func settleThinking() {
        guard isThinking, let anchor = thinkingAnchor else { return }
        let jobTerminal = thinkingSettlesOnJobStatus
            && ChatThinkingSettlement.isTerminalJobStatus(fullThread?.job?.status)
        if ChatThinkingSettlement.isSettled(events: events, after: anchor, turnID: thinkingTurnID) || jobTerminal {
            isThinking = false
            thinkingAnchor = nil
            thinkingTurnID = nil
            thinkingSettlesOnJobStatus = false
        }
    }

    private func activateDraftStorage() {
        hasActivatedDraftStorage = true
        promptDraft.onChange = nil
        synchronizeSubmissionStorage()
        promptDraft.onChange = { [chatDrafts, id = project.id] in chatDrafts.setDraft($0, for: id) }
    }

    private func synchronizeSubmissionStorage() {
        guard hasActivatedDraftStorage, !ownsSendAttempt else { return }
        prompt = chatDrafts.draft(for: project.id)
        let stored = chatDrafts.submission(for: project.id)
        if let previous = sendReceipt, stored?.clientEventID != previous.clientEventID {
            sendReceipt = nil
            if case .acknowledged(let receipt) = chatDrafts.resolution(for: project.id),
               receipt.clientEventID == previous.clientEventID {
                thinkingAnchor = receipt.afterSequence
                thinkingTurnID = receipt.turnID
                isThinking = true
                settleThinking()
            } else {
                pendingMessages.removeAll { $0.clientEventID == previous.clientEventID }
                if submissionAnchor?.clientEventID == previous.clientEventID { submissionAnchor = nil }
                if pendingTurnSubmission?.clientEventID == previous.clientEventID { pendingTurnSubmission = nil }
            }
        }
        if sendReceipt == nil, stored != nil { restoreSubmission() }
        else if let stored {
            sendReceipt = stored
            if stored.phase == .accepted {
                thinkingAnchor = stored.afterSequence
                thinkingTurnID = stored.turnID
                isThinking = true
                settleThinking()
            }
        }
    }

    private func clearSubmissionReceipt() {
        if let id = sendReceipt?.clientEventID,
           model.chatDrafts.submission(for: project.id)?.phase != .accepted {
            model.chatDrafts.discardSubmission(clientEventID: id, for: project.id)
        }
        sendReceipt = nil
    }

    private func recoverSubmissionDraft() {
        guard let receipt = sendReceipt,
              let recovered = model.chatDrafts.recoverSubmission(clientEventID: receipt.clientEventID, for: project.id) else { return }
        prompt = recovered
    }

    private func rejectSubmission(_ pending: ChatPendingMessage) {
        pendingMessages.removeAll { $0.id == pending.id }
        pendingTurnSubmission = nil
        submissionAnchor = nil
        recoverSubmissionDraft()
        clearSubmissionReceipt()
    }

    private func restoreSubmission() {
        guard sendReceipt == nil, let receipt = model.chatDrafts.submission(for: project.id) else { return }
        if receipt.phase == .preparing, !chatDrafts.isSubmitting(for: project.id) {
            if let recovered = model.chatDrafts.recoverSubmission(clientEventID: receipt.clientEventID, for: project.id) {
                prompt = recovered
            }
            return
        }
        sendReceipt = receipt
        pendingTurnSubmission = ChatTurnSubmissionIdentity(message: receipt.message,
            clientEventID: receipt.clientEventID, expectedRevision: receipt.expectedRevision)
        nextLocalOrder += 1
        let pending = ChatPendingMessage(content: receipt.message, clientEventID: receipt.clientEventID,
            afterSequence: receipt.afterSequence, localOrder: nextLocalOrder)
        submissionAnchor = pending
        pendingMessages.append(pending)
        if receipt.phase == .accepted, let turnID = receipt.turnID {
            thinkingAnchor = receipt.afterSequence
            thinkingTurnID = turnID
            isThinking = true
        }
        reconcilePendingMessages()
    }

    private func reconcilePendingMessages() {
        var candidates = pendingMessages
        if let submissionAnchor, !candidates.contains(where: { $0.id == submissionAnchor.id }) {
            candidates.append(submissionAnchor)
        }
        let acknowledged = ChatPendingReconciliation.acknowledged(candidates, events: events)
        pendingMessages.removeAll { acknowledged.contains($0.id) }
        if let submissionAnchor, acknowledged.contains(submissionAnchor.id) {
            model.chatDrafts.acknowledgeSubmission(clientEventID: submissionAnchor.clientEventID, for: project.id)
            if sendReceipt?.clientEventID == submissionAnchor.clientEventID { sendReceipt = nil }
            if let submission = pendingTurnSubmission, submission.clientEventID == submissionAnchor.clientEventID {
                pendingTurnSubmission = nil
            }
            self.submissionAnchor = nil
        }
    }

    private func synchronizeApproval() async throws {
        let terminalTypes = Set(["approval_approved", "approval_denied", "approval_cancelled", "approval_expired"])
        let lastRequest = events.last(where: { $0.eventType == "approval_requested" })
        guard let lastRequest,
              !events.contains(where: { $0.sequence > lastRequest.sequence && terminalTypes.contains($0.eventType) }),
              let identifier = lastRequest.payload?["approval_id"]?.stringValue.flatMap(UUID.init(uuidString:))
        else {
            approval = nil
            approvalNotice = nil
            return
        }
        if approval?.approvalID != identifier.uuidString || (approval?.expiresAt ?? .distantPast) <= .now {
            let fetched = try await model.api.approval(threadID: project.id, approvalID: identifier)
            let requestStillCurrent = events.contains(where: {
                $0.id == lastRequest.id && $0.sequence == lastRequest.sequence
            }) && !events.contains(where: {
                $0.sequence > lastRequest.sequence && terminalTypes.contains($0.eventType)
            })
            if requestStillCurrent {
                approval = fetched.status == "pending" && fetched.expiresAt > .now ? fetched : nil
                approvalNotice = fetched.status == "expired" || fetched.expiresAt <= .now
                    ? "This approval expired. Send a message to request an updated direction." : nil
            }
        }
    }

    /// `cleanupChoice`/`analysisID` carry the speech-cleanup offer's answer on
    /// "approve" ("clean" / "keep_original" / "create_without_cleanup"); both
    /// are nil for "deny" and for a thread with no cleanup offer. This build
    /// always sends `speech_cleanup_aware: true` so the server can safely gate
    /// the decision on the `speech_cleanup_*` conflict codes below.
    /// Identifies what the pick belongs to: the pending approval (v2) or, for the v1
    /// confirmation card, the thread. A different scope re-seeds from the server default.
    private var renderShapeScope: String? { approval?.id ?? fullThread?.id }

    /// The offer + the creator's current choice, or nil when the server offered nothing
    /// choosable (older server, no pending approval) -- then nothing is shown or sent.
    private var renderShapePicker: (offer: RenderShapeOffer, choice: Binding<RenderShapeChoice>)? {
        guard let offer = fullThread?.renderShape, offer.isChoosable else { return nil }
        let scope = renderShapeScope
        return (offer, Binding(
            get: { renderShapePick.choice(for: offer, scope: scope) },
            set: { renderShapePick.select($0, scope: scope) }
        ))
    }

    private func decide(_ decision: String, cleanupChoice: String? = nil, analysisID: String? = nil) {
        guard !isActing, let approval else { return }
        // Silent no-op only for a double tap (`isActing`); every other refusal
        // tells the creator why, so "Create this video" never appears dead.
        if approval.expiresAt <= .now {
            failure = ChatFailure("This approval expired. Ask for the edit again.")
            return
        }
        if pendingUploadCount > 0 {
            failure = ChatFailure("Wait for your footage to finish uploading.")
            return
        }
        guard let identifier = UUID(uuidString: approval.approvalID),
              let draftRevision = approval.draftRevision
        else {
            Task {
                await refreshNow()
                failure = ChatFailure("Kria couldn’t start this yet. Pull to refresh and try again.")
            }
            return
        }
        isActing = true
        let offerBefore = SpeechCleanupOffer.resolve(fullThread?.speechCleanup)
        let retakesAware: Bool
        if case let .choice(stats) = offerBefore.offer, cleanupChoice == "clean" {
            retakesAware = stats.removesRetakes
        } else {
            retakesAware = false
        }
        // Only an approval carries the video shape, and only when the server offered one.
        let shape = decision == "approve" && approval.retryOfExecutionID == nil
            ? renderShapePicker.map { $0.offer.wire(for: $0.choice.wrappedValue) }
            : nil
        Task {
            failure = nil
            defer { isActing = false }
            do {
                try await model.api.decideApproval(
                    threadID: project.id,
                    approvalID: identifier,
                    decision: decision,
                    expectedThreadRevision: threadRevision,
                    expectedDraftRevision: draftRevision,
                    fingerprint: approval.approvalFingerprint,
                    speechCleanupAware: true,
                    speechCleanupRetakesAware: retakesAware,
                    speechCleanupAnalysisID: analysisID,
                    speechCleanupChoice: cleanupChoice,
                    outputOrientation: shape?.orientation,
                    landscapeFit: shape?.landscapeFit,
                    automaticRepairAttempts: decision == "approve" ? approval.automaticRepairLimit ?? 0 : 0
                )
            } catch let error as APIError where error == .conflict && speechCleanupConflictCodes.contains(error.conflictCode ?? "") {
                // The approval stays PENDING with the same id/fingerprint --
                // this isn't a failure, just a stale or not-yet-ready cleanup
                // offer. Refresh so `fullThread.speechCleanup` picks up the
                // latest analysis/outcome and the same card re-renders with
                // it, instead of a generic "couldn't record that" toast.
                await refreshNow()
                let offerAfter = SpeechCleanupOffer.resolve(fullThread?.speechCleanup)
                if shouldSurfaceSpeechCleanupConflict(before: offerBefore, after: offerAfter) {
                    // The card didn't visibly change, so staying silent reads
                    // as a dead button. Say what the server said.
                    failure = ChatFailure(error.conflictDetail ?? "Kria couldn’t start this yet. Pull to refresh and try again.")
                }
                return
            } catch {
                await refreshNow()
                // A video shape the server no longer offers (render_shape_unsupported /
                // render_shape_not_applicable, 422): say what it said. The refresh above
                // already re-seeded the picker from the current offer.
                if shape != nil, let api = error as? APIError, case .requestFailed(422, _) = api,
                   let detail = api.requestFailureDetail, !detail.isEmpty {
                    failure = ChatFailure(detail)
                } else {
                    failure = ChatFailure("Kria couldn’t record that decision.", error: error)
                }
                return
            }
            self.approval = nil
            thinkingAnchor = decision == "approve" ? afterSequence : nil
            thinkingTurnID = decision == "approve" ? approval.turnID : nil
            thinkingSettlesOnJobStatus = decision == "approve"
            isThinking = decision == "approve"
            // The tall approval card is about to be replaced by a short render row. Like Send,
            // re-pin the conversation to its end so the viewport (and the "Latest" pill, which
            // stays up while the reader was above the card's buttons) follows the new stage
            // instead of staying stranded where the card used to be.
            if decision == "approve" { scrollRequest += 1 }
            failure = await acceptedMutationRefreshError(
                "Kria recorded that decision, but the conversation couldn’t refresh.",
                refresh: { try await refreshDelta() }
            )
        }
    }
}

enum WorkspaceStage {
    case format, footage, direction, rendering, ready, failed

    /// Pure decision table behind `CreationWorkspaceView.workspaceStage`, split
    /// out so the precedence rules (a pending plan vs. the last job's status)
    /// are unit-testable without standing up the view. See
    /// `ChatWorkspaceTests` for the full resolution table.
    static func resolve(
        status: ProjectStatus,
        awaitsNewPlanConfirmation: Bool,
        isChoosingFormat: Bool,
        hasFormat: Bool,
        preparationIsActive: Bool = false,
        preparationFailed: Bool = false,
        approvedRenderRetryPending: Bool = false,
        renderDispatchPending: Bool = false
    ) -> WorkspaceStage {
        // A failed native job retains awaiting_device until consent re-pins it.
        // Its fresh retry approval must be visible before any new export starts.
        if approvedRenderRetryPending && awaitsNewPlanConfirmation { return .direction }
        if preparationIsActive { return .rendering }
        if preparationFailed { return .failed }
        // Approved, render not yet queued: the server's projection is still a plain draft
        // (or the previous cut), but the creator just pressed Create. Never show the
        // footage or ready stage in that window; the render stage reads "Preparing".
        if renderDispatchPending && !awaitsNewPlanConfirmation { return .rendering }
        switch status {
        case .rendering:
            // A render already in flight wins even over a freshly proposed
            // plan: the old job's progress must stay visible and reachable.
            return .rendering
        case .ready:
            return awaitsNewPlanConfirmation ? .direction : .ready
        case .failed:
            return awaitsNewPlanConfirmation ? .direction : .failed
        case .draft:
            if awaitsNewPlanConfirmation { return .direction }
            if isChoosingFormat { return .format }
            if hasFormat { return .footage }
            return .format
        }
    }
}

enum FailedWorkspacePresentation: Equatable {
    case preparationRetry
    case legacyCreationConfirmation
    case genericFailure

    /// Preparation can fail before runtime-v1 has a valid Creator direction.
    /// That recovery path must outrank the legacy confirmation card, which
    /// otherwise exposes create actions backed by no active plan.
    static func resolve(
        runtimeVersion: Int?,
        preparationFailed: Bool,
        hasConfirmablePlan: Bool
    ) -> Self {
        if preparationFailed { return .preparationRetry }
        if runtimeVersion == 1 && hasConfirmablePlan { return .legacyCreationConfirmation }
        return .genericFailure
    }
}

enum ChatMessageRole: Equatable { case user, assistant }

/// Keeps append-only history stable while full projections and forward deltas
/// overlap. Event IDs are authoritative; sequence determines display/cursor
/// order, with the ID tie-breaker making equal-sequence responses deterministic.
/// Reference-typed so reading or filling it from `body` mutates no `@State` and never invalidates the view. A hit
/// needs the key of every input to match, so it can never return something built from older data.
final class ChatDerivedMemo {
    var timelineKey: Int?
    var timeline: [ChatTimelineEntry] = []
    /// The `.message` entries of `timeline`; nil until read after each rebuild.
    var timelineMessages: [ChatTranscriptMessage]?
    /// The lazy stack's rows for `timeline`; nil until read after each rebuild.
    var timelineRows: [ChatTimelineRow]?
    var feedKey: Int?
    var feed: PlanBlockFeedState?
    var clipQuestionKey: Int?
    var clipQuestionID: String?
}

/// Cheap identity of a transcript: events are append-only, so id and revision say whether any of them changed.
enum ChatFingerprint {
    static func events(_ events: [ThreadEvent]) -> Int {
        var hasher = Hasher()
        hasher.combine(events.count)
        for event in events { hasher.combine(event.id); hasher.combine(event.sequence); hasher.combine(event.revision) }
        return hasher.finalize()
    }
}

enum ChatTranscriptHistory {
    static func merge(_ existing: [ThreadEvent], with incoming: [ThreadEvent]) -> [ThreadEvent] {
        var byID: [String: ThreadEvent] = [:]
        for event in existing + incoming {
            byID[event.id] = event
        }
        return byID.values.sorted {
            if $0.sequence != $1.sequence { return $0.sequence < $1.sequence }
            return $0.id < $1.id
        }
    }

    static func nextAfterSequence(current: Int, response: Int, events: [ThreadEvent]) -> Int {
        max(current, response, events.map(\.sequence).max() ?? current)
    }
}

/// Network requests overlap by design: a poll can start before a submit or
/// action and finish after it. Event history is append-only and may always be
/// merged, but mutable projections must never move back to an older revision.
struct ThreadProjectionOrder {
    private var issued = 0
    private var accepted = 0
    mutating func begin() -> Int { issued += 1; return issued }
    mutating func accept(_ sequence: Int?) -> Bool {
        let sequence = sequence ?? begin()
        guard sequence >= accepted else { return false }
        accepted = sequence
        return true
    }
}

enum ThreadRevisionOrder {
    static func advance(current: Int, incoming: Int) -> Int { max(current, incoming) }
    static func acceptsProjection(current: Int, incoming: Int) -> Bool { incoming >= current }
}

/// Decides when a "thinking" turn is over. Pure so it is unit-testable: the answer depends only on
/// the merged transcript and the sequence the turn was accepted at, never on which fetch delivered it.
enum ChatThinkingSettlement {
    static let settledTypes: Set<String> = [
        "generation_started", "render_queued", "render_started", "rendering", "plan_block",
        "generation_ready", "render_failed", "generation_failed"
    ]

    static func settles(_ event: ThreadEvent) -> Bool {
        ChatTranscriptMessage.from(event: event)?.role == .assistant || settledTypes.contains(event.eventType)
    }

    static func isSettled(events: [ThreadEvent], after anchor: Int, turnID: String? = nil) -> Bool {
        events.contains { event in
            guard event.sequence > anchor, settles(event) else { return false }
            guard let turnID else { return true }
            if let eventTurnID = event.payload?["turn_id"]?.stringValue {
                return eventTurnID.caseInsensitiveCompare(turnID) == .orderedSame
            }
            // Legacy conversation replies have no turn identifier. Unowned plan/render
            // events cannot settle a known follow-up turn.
            return ChatTranscriptMessage.from(event: event)?.role == .assistant
        }
    }

    static func isTerminalJobStatus(_ status: String?) -> Bool {
        guard let status = status?.lowercased() else { return false }
        return status == "ready" || status == "done" || status == "failed" || status == "cancelled"
            || status.contains("_ready") || status.contains("_failed")
    }
}

/// Deltas carry append-only events and a revision, while a full thread carries
/// mutable fields such as the user-visible title. Keep the decision pure so a
/// non-conversational background update cannot leave the workspace header or
/// Recent chats stale. The comparison is with the last *applied full*
/// projection, not the last delta, because a transient projection-fetch
/// failure must retry on the next otherwise-idle delta.
enum ThreadProjectionRefresh {
    static func requiresFullProjection(
        lastFullProjectionRevision: Int,
        incomingRevision: Int,
        receivedEvents: [ThreadEvent],
        isRendering: Bool
    ) -> Bool {
        incomingRevision > lastFullProjectionRevision || !receivedEvents.isEmpty || isRendering
    }
}

struct ChatTranscriptMessage: Identifiable, Equatable {
    let id: String
    let role: ChatMessageRole
    let content: String
    var isPending = false
    var isProposal = false
    /// A clarifying question's exact, tappable reply choices. Sending one back
    /// verbatim is required — the backend matches it by casefolded,
    /// whitespace-collapsed equality against the fenced mapping it sent with
    /// the question, so a hand-typed paraphrase never resolves it.
    var options: [String] = []
    var recommendedOption: String? = nil
    /// KRI-207: one outcome per requirement in the creator's brief, from the event that carried them.
    var receipts: [RequirementReceiptItem] = []
    /// KRI-282: the clip picker the question carries, when the server sent one.
    var clipQuestion: ClipQuestion? = nil
    /// KRI-374: the take-order card the question carries, when the server sent one.
    var songOrderQuestion: SongOrderQuestion? = nil
    /// KRI-374: the `song_order` a user message carried (from the stored event, or the in-flight send).
    var songOrderAnswer: SongOrderSubmission? = nil
    /// KRI-282: the tappable options the question carries when the instructions conflict, if the server sent them.
    var choiceQuestion: ChoiceQuestion? = nil
    /// KRI-588: the "which part of the song did you film?" card the question carries, when the server sent one.
    var songRangeQuestion: SongRangeQuestion? = nil
    /// KRI-588: the `song_range` a user message carried (from the stored event, or the in-flight send).
    var songRangeAnswer: SongRangeAnswer? = nil
    /// KRI-901: the "Mark the plate" card the question carries, when the server sent one.
    var protectedRegionQuestion: ProtectedRegionQuestion? = nil
    /// KRI-901: the `protected_region` a user message carried (from the stored event, or the in-flight send).
    var protectedRegionAnswer: ProtectedRegionAnswer? = nil
    /// KRI-723: the "show me your moments" card the question carries, when the server sent one.
    var momentOrderQuestion: MomentOrderQuestion? = nil
    /// KRI-723: the `moment_order` a user message carried (from the stored event, or the in-flight send).
    var momentOrderAnswer: MomentOrderAnswer? = nil
    var memoryOperationID: String? = nil
    /// Chip labels of a plain `assistant_response` question (`turn_value == "question"`). Distinct from
    /// `options`, which are the legacy exact-string choices rendered by `QuestionOptionsRow`.
    var replyOptions: [String] = []
    /// Short lead sentence that replaces `content` when receipts are present (see `usesReplyLead`).
    var replyLead: String? = nil
    /// Deliberate extra changes no request claimed; shown as one muted line under the receipts.
    var alsoChanged: [String] = []
    /// KRI-815: the voiceover script this reply carries, when the server sent one.
    var voiceoverScript: VoiceoverScript? = nil

    /// True only with receipts AND a lead: an old server (no lead) or a reply without receipts renders `content`.
    var usesReplyLead: Bool { !receipts.isEmpty && !(replyLead ?? "").isEmpty }
    var displayedContent: String { usesReplyLead ? (replyLead ?? content) : content }

    static func syntheticUser(_ content: String) -> Self {
        Self(id: "synthetic-\(content)", role: .user, content: content)
    }

    static let clarifyingQuestionEventTypes: Set<String> = ["assistant_question", "agent_assistant_question"]

    static func from(event: ThreadEvent) -> Self? {
        let conversationalAssistantEvents: Set<String> = [
            "format_prompt", "media_prompt", "upload_prompt", "voiceover_prompt",
            "confirm_generation", "confirmation", "revision_queued", "status_update",
            "assistant_question", "assistant_response", "assistant_strategy",
            "assistant_review", "draft_applied", "assistant_error", "assistant_render_failed", "memory_updated",
            "creator_memory_receipt", "agent_assistant_question", "agent_assistant_strategy",
            "agent_assistant_review", "agent_assistant_error", "agent_assistant_render_failed",
            "agent_assistant_execution", "assistant_execution"
        ]
        if event.eventType == "action_select_format", let format = CreationFormat(event: event) {
            return Self(id: event.id, role: .user, content: format.choiceSentence)
        }
        let role: ChatMessageRole
        if event.role == "user" && event.eventType == "user_message" {
            role = .user
        } else if conversationalAssistantEvents.contains(event.eventType) {
            role = .assistant
        } else {
            return nil
        }
        let isProposal = ["assistant_strategy", "agent_assistant_strategy"].contains(event.eventType)
        let proposalSummary = isProposal ? event.payload?["proposal_summary"]?.stringValue : nil
        let rawContent = [proposalSummary, event.content, event.payload?["message"]?.stringValue]
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }.first { !$0.isEmpty }
        guard let content = rawContent?.trimmingCharacters(in: .whitespacesAndNewlines), !content.isEmpty else { return nil }
        var options: [String] = []
        var recommendedOption: String?
        if role == .assistant && clarifyingQuestionEventTypes.contains(event.eventType) {
            options = (event.payload?["options"]?.arrayValue ?? []).compactMap { $0.stringValue }
                .filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
            recommendedOption = event.payload?["recommended_option"]?.stringValue
        }
        let receipts = role == .assistant ? RequirementReceiptItem.parse(payload: event.payload) : []
        let replyExtras = role == .assistant ? ReplyExtras.parse(payload: event.payload, eventType: event.eventType, hasReceipts: !receipts.isEmpty) : nil
        let clipQuestion = role == .assistant ? ClipQuestion.parse(payload: event.payload) : nil
        let songOrderQuestion = role == .assistant ? SongOrderQuestion.parse(payload: event.payload) : nil
        let songOrderAnswer = role == .user ? SongOrderSubmission.parse(payload: event.payload) : nil
        let choiceQuestion = role == .assistant ? ChoiceQuestion.parse(payload: event.payload) : nil
        let songRangeQuestion = role == .assistant ? SongRangeQuestion.parse(payload: event.payload) : nil
        let songRangeAnswer = role == .user ? SongRangeAnswer.parse(payload: event.payload) : nil
        let protectedRegionQuestion = role == .assistant ? ProtectedRegionQuestion.parse(payload: event.payload) : nil
        let protectedRegionAnswer = role == .user ? ProtectedRegionAnswer.parse(payload: event.payload) : nil
        let momentOrderQuestion = role == .assistant ? MomentOrderQuestion.parse(payload: event.payload) : nil
        let momentOrderAnswer = role == .user ? MomentOrderAnswer.parse(payload: event.payload) : nil
        let memoryOperationID = event.payload?["kind"]?.stringValue == "creator_memory_receipt" ? event.payload?["operation_id"]?.stringValue : nil
        let voiceoverScript = role == .assistant ? VoiceoverScript.parse(payload: event.payload) : nil
        return Self(id: event.id, role: role, content: content, isProposal: isProposal, options: options, recommendedOption: recommendedOption, receipts: receipts, clipQuestion: clipQuestion, songOrderQuestion: songOrderQuestion, songOrderAnswer: songOrderAnswer, choiceQuestion: choiceQuestion, songRangeQuestion: songRangeQuestion, songRangeAnswer: songRangeAnswer, protectedRegionQuestion: protectedRegionQuestion, protectedRegionAnswer: protectedRegionAnswer, momentOrderQuestion: momentOrderQuestion, momentOrderAnswer: momentOrderAnswer, memoryOperationID: memoryOperationID, replyOptions: replyExtras?.options ?? [], replyLead: replyExtras?.lead, alsoChanged: replyExtras?.alsoChanged ?? [], voiceoverScript: voiceoverScript)
    }
}


enum CreationFormat: String, CaseIterable, Identifiable {
    case montage
    case lipsync
    case narrated
    case talkingToCamera
    case slides

    var id: String { rawValue }
    var serverValue: String {
        switch self {
        case .montage: "montage"
        case .lipsync: "lipsync"
        case .narrated: "narrated"
        case .talkingToCamera: "talking_to_camera"
        case .slides: "slides"
        }
    }
    var title: String {
        switch self {
        case .montage: "Montage"
        case .lipsync: "Lip-sync"
        case .narrated: "Voiceover"
        case .talkingToCamera: "Talk to camera"
        case .slides: "Slider"
        }
    }
    var choiceSentence: String {
        switch self {
        case .montage: "Let’s make a montage"
        case .lipsync: "Let’s make a lip-sync video"
        case .narrated: "Let’s make a voiceover video"
        case .talkingToCamera: "Let’s make a talk to camera video"
        case .slides: "Let’s make a slider"
        }
    }
    var imageName: String {
        switch self {
        case .montage: "montage"
        case .lipsync: "featured-lipsync"
        case .narrated: "voiceover"
        case .talkingToCamera: "talking"
        case .slides: "trulli-street"
        }
    }

    /// Used only before `/capabilities` has loaded (or if it fails to load).
    /// Talking-to-camera takes up to 10 clips (KRI-719: the video of you
    /// talking plus clips shown while you talk); a server with that rollout
    /// off still answers `max_clips: 1`, and the server value always wins
    /// (`maximumClipCount(for:serverLimits:)`). Every other format's real
    /// server default is 50 -- keep these in step with the server so a
    /// slow/failed capabilities fetch doesn't under-cap what the picker and
    /// composer allow.
    var fallbackMaximumClipCount: Int {
        self == .talkingToCamera ? 10 : CreationFormat.fallbackMaximumClipCountWithoutFormat
    }

    /// Same fallback, for the moment before any format is even selected.
    static let fallbackMaximumClipCountWithoutFormat = 50

    /// The clip cap the picker and composer enforce: the server's
    /// `/capabilities` `max_clips` for `format` when it has loaded, else the
    /// offline fallback.
    static func maximumClipCount(for format: CreationFormat?, serverLimits: [CreationFormat: Int]) -> Int {
        guard let format else { return fallbackMaximumClipCountWithoutFormat }
        return serverLimits[format] ?? format.fallbackMaximumClipCount
    }

    init?(thread: CreationThread) {
        if let format = thread.state?["format"]?.stringValue {
            self.init(serverValue: format)
            return
        }
        guard let editFormat = thread.state?["edit_format"]?.stringValue else { return nil }
        self.init(editFormat: editFormat)
    }

    init?(event: ThreadEvent) {
        guard event.eventType == "action_select_format" || event.eventType == "action_select_edit_format" else { return nil }
        if let format = event.payload?["format"]?.stringValue {
            self.init(serverValue: format)
        } else if let editFormat = event.payload?["edit_format"]?.stringValue {
            self.init(editFormat: editFormat)
        } else { return nil }
    }

    init?(serverValue: String) {
        switch serverValue {
        case "montage": self = .montage
        case "lipsync": self = .lipsync
        case "narrated": self = .narrated
        case "talking_to_camera": self = .talkingToCamera
        case "slides": self = .slides
        default: return nil
        }
    }

    private init?(editFormat: String) {
        switch editFormat {
        case "montage": self = .montage
        case "narrated", "narrated_planned": self = .narrated
        case "subtitled", "talking_to_camera": self = .talkingToCamera
        case "slides": self = .slides
        default: return nil
        }
    }
}

extension CreationFormat {
    var usesVisualPool: Bool { self == .slides }
    /// Lip-sync is a montage on the server (`edit_format: "montage"`); chat still decides
    /// lip-sync vs background. Anywhere montage logic applies, lip-sync shares it.
    var runsAsMontage: Bool { self == .montage || self == .lipsync }
    var carouselOrder: Int {
        switch self {
        case .montage: 0
        case .narrated: 1
        case .talkingToCamera: 2
        case .slides: 3
        case .lipsync: 4
        }
    }
}

extension CreationThread {
    /// Slide composition requires fully ready pool assets. `current` includes
    /// imports that can still fail, so it must never unlock the chat composer.
    /// `device_ready` is retained as an older-server fallback for the existing
    /// device-render pilot; current slide-capable servers return `ready`.
    var readyVisualCount: Int {
        let visuals = mediaCapabilities?["visuals"]?.objectValue
        if case .number(let count) = visuals?["ready"] { return max(0, Int(count)) }
        if case .number(let count) = visuals?["device_ready"] { return max(0, Int(count)) }
        return 0
    }
}

@MainActor
func acceptedMutationRefreshError(
    _ failurePrefix: String,
    refresh: () async throws -> Bool
) async -> ChatFailure? {
    do {
        _ = try await refresh()
        return nil
    } catch {
        return ChatFailure(failurePrefix, error: error)
    }
}

/// A completed full or delta response is authoritative even when it contains
/// no new events, so it clears a stale refresh-recovery banner.
func clearChatRefreshRecoveryMessage(_ failure: inout ChatFailure?) {
    guard let current = failure?.message else { return }
    let recoveryPrefixes = [
        "Kria lost the live connection.",
        "Kria couldn’t refresh this conversation.",
        "Your message was sent, but the conversation couldn’t refresh.",
        "Saved, but the conversation couldn’t refresh.",
        "Kria recorded that decision, but the conversation couldn’t refresh.",
        "Kria couldn’t confirm that message."
    ]
    if recoveryPrefixes.contains(where: current.hasPrefix) {
        failure = nil
    }
}

func attachedVideoClipCount(in threadState: [String: JSONValue]) -> Int {
    guard let media = threadState["media"] else {
        return Int(threadState["media_count"]?.numberValue ?? 0)
    }
    guard case .array(let entries) = media else { return 0 }
    return entries.filter { entry in
        guard case .object(let value) = entry else { return false }
        return value["kind"]?.stringValue == "video"
    }.count
}

/// KRI-118 item 5: an action's 409 outside `CreationConfirmationConflict
/// .confirmationActions` (`generate`/`retry`/`create_without_cleanup`)
/// normally collapses into the generic "This project changed" copy --
/// correct for most such actions, whose own conflict doesn't map to a
/// server-authored sentence. `select_format`/`select_edit_format` is the
/// exception: its 409 (e.g. a stale `format_mismatch`,
/// `app.routes.creation_threads`) DOES carry a specific, real reason the
/// user should see, so it reuses `CreationConfirmationConflict`'s own detail
/// classification (real sentence vs. machine code vs. missing) instead of
/// discarding `detail` and hand-rolling a second one.
func nonConfirmationConflictMessage(action: String, detail: String?, planIdentity: String) -> String {
    guard action == "select_format" || action == "select_edit_format" else {
        return CreationConfirmationConflict.changedMessage
    }
    return CreationConfirmationConflict(detail: detail, planIdentity: planIdentity).message
}

func formatClipCapacityError(
    format: CreationFormat,
    clipLimit: Int,
    occupiedClipCount: Int
) -> String? {
    guard occupiedClipCount > clipLimit else { return nil }
    return "\(format.title) supports \(clipLimit) \(clipLimit == 1 ? "clip" : "clips"). Keep your current format or remove extra footage first."
}

extension ProjectSummary {
    var workspaceTitle: String {
        let cleaned = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty || cleaned.lowercased() == "untitled project" ? "Untitled video" : cleaned
    }

    var workspaceStatusLabel: String {
        // Mirrors `WorkspaceStage.resolve`: a pending plan outranks a stale
        // `.ready`/`.failed` label left over from the last job, but never a
        // render actually in flight.
        switch status {
        case .draft: "Shaping direction"
        case .rendering: "Rendering"
        case .ready: awaitsConfirmation ? "Shaping direction" : "Ready"
        case .failed: awaitsConfirmation ? "Shaping direction" : "Needs attention"
        }
    }
}

extension JSONValue {
    var numberValue: Double? { if case .number(let value) = self { value } else { nil } }
}

struct ChatTurnSubmissionIdentity: Equatable, Sendable {
    let message: String
    let clientEventID: String
    let expectedRevision: Int

    static func reusing(_ current: Self?, for message: String, expectedRevision: Int) -> Self {
        if let current, current.message.canonicalSubmissionText == message.canonicalSubmissionText {
            return current
        }
        return Self(message: message, clientEventID: UUID().uuidString, expectedRevision: expectedRevision)
    }
}

private extension String {
    var canonicalSubmissionText: String { split(whereSeparator: \.isWhitespace).joined(separator: " ") }
}
