#if DEBUG
import SwiftUI

/// Deterministic component states for screenshot review; never enters release navigation.
struct BrandPreviewHost: View {
    @EnvironmentObject private var model: AppModel
    @FocusState private var composerFocused: Bool
    @State private var text = ""
    private var state: String { ProcessInfo.processInfo.environment["KRIA_BRAND_STATE"] ?? "format" }
    private var project: ProjectSummary { PreviewFixtures.projects[0] }
    private var approval: ApprovalSnapshot {
        ApprovalSnapshot(approvalID: "preview", turnID: "preview", draftID: nil, draftRevision: 1, status: "pending", consequenceSummary: "A slow summer postcard", costSummary: nil, expiresAt: .distantFuture, approvalFingerprint: "preview")
    }
    var body: some View {
        Group {
            switch state {
            case "signin": SignInView()
            case "account": NavigationStack { AccountView() }
            case "gallery": NavigationStack { GalleryView() }
            case "gallery-posters":
                // Drive the real poster component without network timing, so
                // every AsyncImage state is repeatable in native UI checks.
                VStack(spacing: 12) {
                    ProjectPosterContent(phase: nil, title: "Missing poster")
                    ProjectPosterContent(phase: .empty, title: "Loading poster")
                    ProjectPosterContent(phase: .failure(URLError(.cannotDecodeContentData)), title: "Failed poster")
                    ProjectPosterContent(phase: .success(Image(systemName: "checkmark")), title: "Loaded poster")
                }
                .frame(width: 174)
                .padding(20)
            case "projects": ProjectsDrawer(close: {}, openGallery: {})
            case "editor": NativeEditorView(project: PreviewFixtures.editorProject, initialDraft: PreviewFixtures.editorDraft, initialPlaybackURL: Bundle.main.url(forResource: "montage", withExtension: "mp4"), onBack: {})
            default:
                VStack(spacing: 0) {
                    WorkspaceHeader(project: project, showsEditorSwitch: state == "ready", openProjects: {}, openEditor: {}, openAccount: {})
                    ScrollView {
                        VStack(alignment: .leading, spacing: 20) {
                            switch state {
                            case "footage": FootageStage(format: .montage, mediaCount: 2, maximumClipCount: 10, uploads: [], progress: [:], addFootage: {}, changeFormat: {})
                            case "direction": DirectionStage(approval: approval, format: .montage, isBusy: false, decide: { _ in })
                            case "rendering": RenderingStage()
                            case "phone-rendering": DeviceRenderStatusCard(presentation: DeviceRenderPresentation(phase: .rendering), retry: {}, stop: {})
                            case "phone-sync": DeviceRenderStatusCard(presentation: DeviceRenderPresentation(phase: .localReady,
                                localFile: Bundle.main.url(forResource: "montage", withExtension: "mp4"), message: "Syncing didn’t finish. Your video is saved on this iPhone."), retry: {}, stop: {})
                            case "ready": ReadyStage(project: PreviewFixtures.projects[1], openEditor: {}, suggest: { _ in })
                            case "recovery": FailedStage(retry: {})
                            default: FormatStage(formats: [.montage, .narrated, .talkingToCamera], isBusy: false, select: { _ in })
                            }
                        }.padding(20).frame(maxWidth: 620, alignment: .leading).frame(maxWidth: .infinity)
                    }
                    ChatComposer(text: $text, isSending: false, canAttach: state != "format", isFocused: $composerFocused, attach: {}, send: {})
                }
            }
        }
        .environment(\.dynamicTypeSize, ProcessInfo.processInfo.environment["KRIA_BRAND_LARGE_TEXT"] == "1" ? .accessibility3 : .large)
        .background(KriaColor.paper)
        .task { model.projects = PreviewFixtures.projects; model.selectedProject = project; model.libraryProjects = [PreviewFixtures.projects[1]] }
    }
}
#endif
