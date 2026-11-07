import SwiftUI
import UIKit

struct CanonicalPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var fill = KriaColor.butter
    var foreground = KriaColor.ink

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(14).weight(.semibold))
            .foregroundStyle(foreground)
            .frame(maxWidth: .infinity, minHeight: 48)
            .padding(.horizontal, 16)
            .background(fill)
            .clipShape(Capsule())
            .opacity(isEnabled ? (configuration.isPressed ? 0.72 : 1) : 0.45)
    }
}

struct CanonicalSecondaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(KriaFont.body(14).weight(.semibold))
            .foregroundStyle(KriaColor.ink)
            .frame(maxWidth: .infinity, minHeight: 48)
            .padding(.horizontal, 16)
            .background(configuration.isPressed ? KriaColor.fillPressed : KriaColor.fill)
            .clipShape(Capsule())
            .opacity(isEnabled ? 1 : 0.45)
    }
}

extension EnvironmentValues {
    @Entry var projectsDrawerOpen = false
    @Entry var projectsDrawerProgress: CGFloat = 0
}

struct WorkspaceSurface: View {
    @Environment(\.projectsDrawerProgress) private var progress

    var body: some View {
        KriaColor.paper.overlay { KriaColor.menu.opacity(Double(progress)) }
            .ignoresSafeArea(.container)
    }
}

struct WorkspaceHeader: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    let project: ProjectSummary
    let showsEditorSwitch: Bool
    let openProjects: () -> Void
    let openEditor: () -> Void
    let openAccount: () -> Void
    @EnvironmentObject private var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            WorkspaceTopRow(
                title: project.workspaceTitle,
                titleIdentifier: "workspace-project-title",
                titleHidden: projectsDrawerOpen
            ) {
                Button(action: openProjects) { KriaIcon(.menu).frame(width: 44, height: 44).kriaFloatingSurface(Circle()) }
                    .accessibilityLabel(projectsDrawerOpen ? "Close projects" : "Open projects")
                    .accessibilityIdentifier("workspace-menu-toggle")
            } trailing: {
                ProjectActionsMenu(project: project, floating: true)
                    .accessibilityHidden(projectsDrawerOpen)
                    .allowsHitTesting(!projectsDrawerOpen)
            }
            if showsEditorSwitch {
                WorkspaceModeSwitch(selected: .chat, onEditor: openEditor)
                    .accessibilityHidden(projectsDrawerOpen)
                    .allowsHitTesting(!projectsDrawerOpen)
            }
        }.foregroundStyle(KriaColor.ink)
    }
}

struct ProjectsDrawer: View {
    let close: () -> Void
    let openGallery: () -> Void
    var openAccount: () -> Void = {}
    var openCollections: (() -> Void)? = nil
    @EnvironmentObject private var model: AppModel
    @AccessibilityFocusState private var menuFocused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            HStack {
                KriaWordmark().accessibilityFocused($menuFocused)
                Spacer()
            }.padding(.horizontal, 12)
            Button(action: openGallery) {
                HStack(spacing: 12) {
                    KriaIcon(.gallery)
                    Text("Gallery").font(KriaFont.body(15).weight(.medium))
                    Spacer()
                    Text("\(model.libraryProjects.count)\(model.libraryNextCursor == nil ? "" : "+") \(model.libraryProjects.count == 1 && model.libraryNextCursor == nil ? "video" : "videos")").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }.frame(minHeight: 44).padding(.horizontal, 12)
            }
            if let openCollections {
                Button(action: openCollections) {
                    HStack(spacing: 12) {
                        Image(systemName: "photo.stack").font(.system(size: 18, weight: .regular)).frame(width: 22, height: 22)
                        Text("Camera roll").font(KriaFont.body(15).weight(.medium))
                        Spacer()
                    }
                    .frame(minHeight: 44).padding(.horizontal, 12).contentShape(Rectangle())
                    .background(model.showsCameraRoll ? KriaColor.selectionSoft : .clear, in: RoundedRectangle(cornerRadius: 10))
                }
                .buttonStyle(.plain).accessibilityIdentifier("camera-roll-collection-open")
                .accessibilityAddTraits(model.showsCameraRoll ? .isSelected : [])
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("Recent chats").font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk).padding(.horizontal, 12)
                    .onAppear { Perf.end("drawer.open") }
                ScrollView(showsIndicators: false) {
                    LazyVStack(spacing: 3) {
                        if model.projects.isEmpty {
                            Text("No projects yet").font(KriaFont.body(14)).padding(12)
                            if case .failed = model.projectsState {
                                Button("Try again") { Task { await model.loadProjects() } }.buttonStyle(KriaSecondaryButtonStyle())
                            }
                        }
                        ForEach(model.projects) { project in
                            HStack(spacing: 0) {
                                Button { model.selectProject(project); close() } label: {
                                    ProjectDrawerRow(project: project, isSelected: !model.showsCameraRoll && project.id == model.selectedProject?.id)
                                }.buttonStyle(.plain)
                                ProjectActionsMenu(project: project)
                            }.background(!model.showsCameraRoll && project.id == model.selectedProject?.id ? KriaColor.selectionSoft : .clear, in: RoundedRectangle(cornerRadius: 10))
                        }
                        if model.projectsNextCursor != nil {
                            // Reaching the end of the loaded chats fetches the next older page.
                            ProgressView().padding(12).frame(maxWidth: .infinity)
                                .accessibilityIdentifier("drawer-loading-older-chats")
                                .onAppear { Task { await model.loadMoreProjects() } }
                        }
                    }
                }
            }
            Spacer(minLength: 0)
            HStack {
                NewChatButton(afterCreate: close)
                Spacer()
                Button(action: openAccount) { KriaIcon(.settings).frame(width: 44, height: 44) }
                    .accessibilityLabel("Open account")
            }
        }
        .padding(.horizontal, 14).padding(.top, 8).padding(.bottom, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity).background(KriaColor.paper)
        .accessibilityAction(.escape, close)
        .foregroundStyle(KriaColor.ink)
        .task { menuFocused = true }
    }
}

struct ProjectDrawerRow: View {
    let project: ProjectSummary
    let isSelected: Bool
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(project.workspaceTitle).font(KriaFont.body(14).weight(.medium)).lineLimit(1)
            Text(project.workspaceStatusLabel).font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
        }
        .frame(maxWidth: .infinity, minHeight: 48, alignment: .leading).padding(.horizontal, 12)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

struct ChatMessageRow: View {
    let message: ChatTranscriptMessage
    var onSelectOption: ((String) -> Void)? = nil
    var responseStartedAt: Date? = nil
    /// KRI-207: names for the receipt chips, keyed by requirement id (the brief, once loaded).
    var requirements: [String: CreativeBriefRequirement] = [:]
    /// The brief has loaded, or its fetch definitively failed; until then the chips wait.
    var briefSettled = false
    /// Starts a correction for a guessed name; nil hides the "Guessed names" row.
    var onCorrectGuess: ((InferredLabel) -> Void)? = nil
    /// KRI-282: how to present `message.clipQuestion`. nil (old server, flag off, not a question) = text question only.
    var clipSelectionMode: ClipSelectionCardMode? = nil
    var clipSelectionMedia: [CreationAttachedMedia] = []
    /// KRI-694: the chat's reply language, which the clip answer is posted in.
    var clipSelectionLanguage: ClipAnswerLanguage = .english
    /// KRI-374: how to present `message.songOrderQuestion`. nil (old server, flag off, not a question) = text only.
    var songOrderMode: SongOrderCardMode? = nil
    var songOrderMedia: [CreationAttachedMedia] = []
    /// KRI-561: set when the server advertises `song_order_placements`; the order question is then the song timeline.
    var songTimeline: SongTimelineConfiguration? = nil
    var songDurationS: Double? = nil
    var projectID: UUID? = nil
    /// KRI-282: how to present `message.choiceQuestion`. nil (old server, flag off, not a question) = text question only.
    var choiceQuestionMode: ChoiceQuestionCardMode? = nil
    /// True for the user message whose text is the "You chose" capsule of the choice question it answered.
    var hidesAnswerEcho = false
    /// KRI-802: set when this reply asks the choice card and the clip picker together; their answers wait here and
    /// go out on one turn. The cards then follow the reply's own order: the choice first, then the clips.
    var questionBatch: QuestionBatch? = nil
    /// KRI-588: how to present `message.songRangeQuestion`. nil (old server, flag off, not a question) = text only.
    var songRangeMode: SongRangeCardMode? = nil
    /// KRI-901: how to present `message.protectedRegionQuestion`. nil (old server, not a question) = text only.
    var objectMarkMode: ObjectMarkCardMode? = nil
    /// KRI-723: how to present `message.momentOrderQuestion`. nil (old server, not a question) = text only.
    var momentOrderMode: MomentOrderCardMode? = nil
    /// Tappable answers for a plain `assistant_response` question. nil = not the newest unanswered question
    /// (or no options): no chips are drawn, so history never keeps stale tappable answers.
    var replyOptionsMode: ReplyOptionsMode? = nil
    /// KRI-815: how to present `message.voiceoverScript`. nil (old server, flag off, no script) = the reply text,
    /// which holds the script too.
    var voiceoverScriptMode: VoiceoverScriptCardMode? = nil
    /// "Type my own" on a choice card: hands the creator the composer for this question; nil hides the row.
    var answerOwn: (() -> Void)? = nil
    /// The creator is typing their own answer, so every answer block is disabled.
    var isAnsweringOwn = false
    /// KRI-802: undoes the held choice of a reply that also asks for clips.
    var changeHeldChoice: (() -> Void)? = nil

    /// With the script card shown, the reply text drops the script it ends with (the card holds it).
    private var shownContent: String {
        guard voiceoverScriptMode != nil, let script = message.voiceoverScript else { return message.displayedContent }
        return script.reply(from: message.displayedContent)
    }

    private static let userBubbleShape = UnevenRoundedRectangle(
        topLeadingRadius: 18,
        bottomLeadingRadius: 18,
        bottomTrailingRadius: 6,
        topTrailingRadius: 18,
        style: .continuous
    )

    var body: some View {
        if message.role == .user && hidesAnswerEcho {
            // The question above already shows this exact text as its "You chose" capsule: one of them is enough.
            EmptyView()
        } else if message.role == .user {
            HStack {
                Spacer(minLength: 54)
                Text(message.content)
                    .font(KriaFont.body(14).weight(.medium))
                    .lineSpacing(2)
                    .multilineTextAlignment(.leading)
                    .foregroundStyle(Color.white)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 10)
                    .background { Self.userBubbleShape.fill(KriaColor.ink) }
                    .opacity(message.isPending ? 0.68 : 1)
                    .copyableMessage(message.content, previewShape: Self.userBubbleShape)
                    .accessibilityLabel("You: \(message.content)")
                    .accessibilityIdentifier("chat-message-\(message.id)")
                    .onAppear {
                        Perf.endOpenOnNextFrame("launch.toPixels")
                        Perf.endOpenOnNextFrame("chat.toPixels")
                    }
            }
            .frame(maxWidth: .infinity)
        } else {
            VStack(alignment: .leading, spacing: 7) {
                // Only the reply text is long-pressable, so the option chips
                // below keep their plain taps.
                if message.isProposal {
                    Text("Proposed direction").font(KriaFont.body(12).weight(.semibold)).foregroundStyle(KriaColor.zinc)
                }
                // With receipts and a server `reply_lead`, the lead replaces the long prose: the receipt rows
                // below carry each fact once. No lead (old server) = today's text, unchanged.
                ChatResponseText(content: shownContent, startedAt: responseStartedAt)
                    .font(KriaFont.body(14))
                    .foregroundStyle(KriaColor.ink)
                    .lineSpacing(4)
                    .copyableMessage(shownContent, previewShape: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    // On the text, not the stack: a stack's label is applied to
                    // every child, which read each option chip as the question.
                    .accessibilityLabel("Kria: \(shownContent)")
                if !message.options.isEmpty, let onSelectOption {
                    QuestionOptionsRow(
                        options: message.options,
                        recommendedOption: message.recommendedOption,
                        select: onSelectOption
                    )
                }
                if !message.replyOptions.isEmpty, let replyOptionsMode {
                    ReplyOptionRows(options: message.replyOptions, mode: replyOptionsMode)
                }
                if questionBatch != nil { choiceCard }
                if let question = message.clipQuestion, let clipSelectionMode {
                    ClipSelectionCard(question: question, media: clipSelectionMedia, projectID: projectID, mode: clipSelectionMode,
                                      language: clipSelectionLanguage, note: questionBatch?.clipsNote)
                        .id(question.questionID)
                }
                if let question = message.songOrderQuestion, let songOrderMode, let projectID {
                    if let songTimeline {
                        SongTimelineCard(question: question, media: songOrderMedia, projectID: projectID, mode: songOrderMode,
                                         configuration: songTimeline, songDurationS: songDurationS)
                            .id(question.questionID)
                    } else {
                        SongOrderCard(question: question, media: songOrderMedia, projectID: projectID, mode: songOrderMode)
                            .id(question.questionID)
                    }
                }
                if let question = message.songRangeQuestion, let songRangeMode {
                    SongRangeQuestionCard(question: question, fallbackDurationS: songDurationS, mode: songRangeMode)
                        .id(question.questionID)
                }
                if let question = message.protectedRegionQuestion, let objectMarkMode {
                    ObjectMarkCard(question: question, projectID: projectID, mode: objectMarkMode)
                        .id(question.questionID)
                }
                if let question = message.momentOrderQuestion, let momentOrderMode {
                    MomentOrderCard(question: question, projectID: projectID, language: clipSelectionLanguage, mode: momentOrderMode)
                        .id(question.questionID)
                }
                if questionBatch == nil { choiceCard }
                if let script = message.voiceoverScript, let voiceoverScriptMode {
                    VoiceoverScriptCard(script: script, mode: voiceoverScriptMode)
                        .id("\(script.scriptID)-\(script.revision)")
                }
                if !message.receipts.isEmpty {
                    RequirementChipsView(
                        receipts: message.receipts, requirements: requirements, titlesReady: briefSettled,
                        alsoChanged: message.usesReplyLead ? message.alsoChanged : [],
                        showsReasons: message.usesReplyLead, correct: onCorrectGuess
                    )
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder private var choiceCard: some View {
        if let question = message.choiceQuestion, let choiceQuestionMode {
            ChoiceQuestionCard(question: question, mode: choiceQuestionMode,
                               heldKey: questionBatch?.choice?.optionKey, note: questionBatch?.choiceNote,
                               changeHeld: questionBatch?.choice != nil ? changeHeldChoice : nil,
                               typeMyOwn: answerOwn, isTyping: isAnsweringOwn, allowsDecide: questionBatch == nil)
                .id(question.questionID)
        }
    }
}

extension View {
    /// Long-press Copy for chat text, which plain SwiftUI `Text` can't select.
    /// (`.textSelection(.enabled)` would also copy only the whole message on
    /// iOS.) VoiceOver gets the same Copy as an action, since the menu alone
    /// exposes none.
    func copyableMessage(_ text: String, previewShape: some Shape) -> some View {
        modifier(CopyableMessage(text: text, previewShape: previewShape))
    }
}

private struct CopyableMessage<PreviewShape: Shape>: ViewModifier {
    let text: String
    let previewShape: PreviewShape

    func body(content: Content) -> some View {
        content
            .contentShape(.contextMenuPreview, previewShape)
            .contextMenu {
                Button("Copy", systemImage: "doc.on.doc", action: copy)
            }
            .accessibilityAction(named: "Copy", copy)
    }

    private func copy() {
        UIPasteboard.general.string = text
    }
}

private struct AssistantHeading: View {
    let title: String
    let bodyText: String
    var isFormatHeading = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .font(isFormatHeading ? KriaFont.headline(28) : KriaFont.body(20))
                .foregroundStyle(KriaColor.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text(bodyText)
                .font(KriaFont.body(14))
                .foregroundStyle(KriaColor.zinc)
                .lineSpacing(4)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }
}

/// Custom drag surfaces (not scroll views) that keep their own drags while the drawer is closed. Sideways scroll views and
/// text-selection handles are found by `DrawerGestureTracking.startsOnOwnDrag`.
struct DrawerGestureExclusionPreference: PreferenceKey {
    static var defaultValue: [CGRect] { [] }
    static func reduce(value: inout [CGRect], nextValue: () -> [CGRect]) { value += nextValue() }
}

struct FormatStage: View {
    let formats: [CreationFormat]
    let isBusy: Bool
    let select: (CreationFormat) -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var leadingFormat: CreationFormat?
    @State private var visible = true

    private var autoplay: Bool {
        visible && scenePhase == .active && !reduceMotion && !isBusy &&
        ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] != "1" && UIAccessibility.isVideoAutoplayEnabled
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            AssistantHeading(
                title: "What are we making?",
                bodyText: "Choose a starting point. We can shape the details together.",
                isFormatHeading: true
            )
            GeometryReader { viewport in
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 10) {
                        ForEach(formats) { format in
                            Group {
                                if format == .slides {
                                    SlidePostFormatCard(isBusy: isBusy, active: leadingFormat == format && autoplay) { select(format) }
                                } else {
                                    FormatVideoCard(format: format, isBusy: isBusy, active: leadingFormat == format,
                                                    allowed: autoplay) { select(format) }
                                }
                            }
                            .id(format)
                        }
                    }
                    .scrollTargetLayout()
                }
                .contentMargins(.trailing, max(0, viewport.size.width - FormatTileMetrics.image.width), for: .scrollContent)
                .scrollTargetBehavior(.viewAligned)
                .scrollPosition(id: $leadingFormat, anchor: .leading)
                .accessibilityIdentifier("format-carousel")
            }
            .frame(height: FormatTileMetrics.image.height + 32)
            .onScrollVisibilityChange(threshold: 0.5) { visible = $0 }
        }
        .onAppear { if leadingFormat == nil { leadingFormat = formats.first } }
        .onChange(of: formats) { _, values in
            if let leadingFormat, !values.contains(leadingFormat) { self.leadingFormat = values.first }
        }
    }
}

private struct FormatVideoCard: View {
    let format: CreationFormat
    let isBusy: Bool
    let active: Bool
    let allowed: Bool
    let select: () -> Void
    @State private var paused = false
    @State private var playing = false

    private var edit: FeaturedEdit? { format.previewEdit }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ZStack(alignment: .bottomTrailing) {
                Button(action: select) {
                    Group {
                        if let edit {
                            FeaturedEditPreview(edit: edit, active: active, allowed: allowed && !paused,
                                                loops: true, playingChanged: { playing = $0 })
                        } else {
                            BundledPosterImage(name: format.imageName).scaledToFill()
                        }
                    }
                    .frame(width: FormatTileMetrics.image.width, height: FormatTileMetrics.image.height)
                    .clipped()
                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(isBusy)
                .accessibilityLabel(format.title)
                .accessibilityValue(edit == nil ? "Image preview" : playing ? "Playing preview" : "Paused preview")
                .accessibilityIdentifier("format-\(format.serverValue)")
                .accessibilityHint("Choose \(format.title) as this creation format")
                if edit != nil {
                    Button { paused.toggle() } label: {
                        Image(systemName: paused ? "play.fill" : "pause.fill")
                            .font(.system(size: 12, weight: .bold))
                            .foregroundStyle(.white)
                            .frame(width: 44, height: 44)
                            .background(.black.opacity(0.38), in: Circle())
                    }
                    .buttonStyle(.plain)
                    .padding(5)
                    .accessibilityLabel(paused ? "Play format preview" : "Pause format preview")
                    .accessibilityIdentifier("format-preview-playback-\(format.serverValue)")
                }
            }
            Text(format.title).font(KriaFont.headline(18)).foregroundStyle(KriaColor.ink)
        }
        .frame(width: FormatTileMetrics.image.width, alignment: .leading)
    }
}

private struct SlidePostFormatCard: View {
    let isBusy: Bool
    let active: Bool
    let select: () -> Void
    @State private var paused = false
    @State private var slideIndex = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ZStack(alignment: .bottomTrailing) {
                Button(action: select) {
                    SlidePostFormatCover(index: $slideIndex, paused: $paused, active: active)
                        .frame(width: FormatTileMetrics.image.width, height: FormatTileMetrics.image.height)
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(isBusy)
                .accessibilityIdentifier("format-slides")
                .accessibilityLabel("Slider")
                .accessibilityValue("Slide \(slideIndex % SlidePostFormatCover.frames.count + 1) of \(SlidePostFormatCover.frames.count)")
                .accessibilityHint("Choose Slider as this creation format")
                // The swipe strip must not expand the selection button's accessibility frame.
                .accessibilityElement(children: .ignore)

                // This is a sibling of the format-selection button. Nesting a
                // Button inside a Button sends the pause tap to the card too.
                Button { paused.toggle() } label: {
                    Image(systemName: paused ? "play.fill" : "pause.fill")
                        .font(.system(size: 12, weight: .bold))
                        .frame(width: 44, height: 44)
                        .background(.black.opacity(0.38), in: Circle())
                }
                .buttonStyle(.plain)
                .padding(.trailing, 5)
                .padding(.bottom, 5)
                .accessibilityLabel(paused ? "Play format preview" : "Pause format preview")
                .accessibilityIdentifier("slide-format-cover-playback")
            }
            Text("Slider")
                .font(KriaFont.headline(18))
                .foregroundStyle(KriaColor.ink)
        }
        .frame(width: 156, alignment: .leading)
    }
}

/// Footage-step wording that depends on the server's clip cap.
enum FootageStageCopy {
    /// "1 clip", "10 clips".
    static func count(_ value: Int, _ noun: String) -> String {
        "\(value) \(noun)\(value == 1 ? "" : "s")"
    }

    /// Talking to camera: one take, or (KRI-719) the take plus clips shown
    /// while the creator talks when the server allows more than one.
    static func talkingBody(maximumClipCount: Int) -> String {
        maximumClipCount > 1
            ? "Record yourself talking, or upload that video. Add clips to show while you talk."
            : "Record one clear take, or upload one you already have. I’ll keep your voice at the center."
    }

    /// The Add media sheet's footage subtitle.
    static func attachmentSubtitle(talking: Bool, maximumClipCount: Int) -> String {
        guard talking else { return "Choose the clips you want Kria to work with." }
        return maximumClipCount > 1
            ? "Record yourself, or upload a video you already have. Add clips to show while you talk."
            : "Record yourself, or upload a video you already have."
    }
}

/// Per-upload progress, read by the views that draw it rather than held as `@State` by the whole workspace: a
/// progress tick used to re-evaluate the entire chat screen (every row, the composer, the timeline), several times
/// a second for as long as anything uploaded. Observation re-runs only the bodies that read `values`.
@Observable final class UploadProgressStore {
    var values: [UUID: Double] = [:]
}

struct FootageStage: View {
    let format: CreationFormat
    let mediaCount: Int
    let maximumClipCount: Int
    let uploads: [UploadRecoveryRecord]
    /// Clips still being prepared or chosen. They are not in `uploads` yet, but Continue must wait
    /// for them or it would proceed with fewer clips than the user picked.
    var preparingCount = 0
    /// `BackgroundUploadCoordinator.previewVersion`, so thumbnails re-read from disk as they appear.
    var previewVersion = 0
    let progress: UploadProgressStore
    /// Upload: opens the "Upload from" list.
    let addFootage: () -> Void
    /// Record: opens the camera. Nil on a surface that cannot film.
    var recordFootage: (() -> Void)?
    /// Opens Add media on its clip list, where clips can be removed. Offered once the edit holds its most clips.
    var manageFootage: (() -> Void)?
    var isBusy = false
    /// Clips that failed to add. They are otherwise only shown inside the picker sheet, so a clip that
    /// fails after the sheet was closed would silently leave Continue enabled with fewer clips than
    /// the user chose.
    var failures: [UploadFailure] = []
    var dismissFailure: (UUID) -> Void = { _ in }
    /// Retries the upload behind a failure that still has its record (`cause == .uploadFailed`).
    var retryFailure: (UUID) -> Void = { _ in }
    /// Ready photos and videos in the PlanItemAsset Visuals pool.
    var visualCount = 0

    @Environment(\.dynamicTypeSize) private var typeSize

    private var readiness: FootageReadiness {
        FootageReadiness(attachedCount: mediaCount, pendingCount: uploads.count + preparingCount)
    }

    private var visualsReadiness: FootageReadiness {
        FootageReadiness(attachedCount: format.usesVisualPool ? visualCount : 0, pendingCount: uploads.count + preparingCount)
    }

    private var talkingLeads: Bool { format == .talkingToCamera && readiness.attachedCount == 0 && readiness.pendingCount == 0 }
    private var hasMedia: Bool { readiness.attachedCount > 0 || readiness.pendingCount > 0 }
    private var limitText: String {
        format.usesVisualPool ? "Up to \(FootageStageCopy.count(maximumClipCount, "item"))" : "Up to \(FootageStageCopy.count(maximumClipCount, "clip"))"
    }
    private var reachedLimit: Bool { !format.usesVisualPool && mediaCount + readiness.pendingCount >= maximumClipCount }

    private var sourceOptions: [KriaSourceOption] {
        let canAdd = !reachedLimit && !isBusy
        let noun = format.usesVisualPool ? "media" : "video"
        return [
            format.usesVisualPool ? nil : recordFootage.map { KriaSourceOption.record(caption: "Film now", identifier: "record-footage", isEnabled: canAdd, action: $0) },
            KriaSourceOption.upload(caption: "Use existing \(noun)", identifier: format.usesVisualPool ? "choose-photos-videos" : "choose-videos", isEnabled: canAdd, action: addFootage)
        ].compactMap { $0 }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            VStack(alignment: .leading, spacing: 8) {
                Text(format.usesVisualPool ? "Add photos & videos" : "Add your footage").kriaType(.heading).foregroundStyle(KriaColor.ink)
                    .accessibilityAddTraits(.isHeader)
                if !hasMedia {
                    Text(format.usesVisualPool
                         ? "Choose the photos and videos for your post. You can mix both."
                         : format == .talkingToCamera
                         ? FootageStageCopy.talkingBody(maximumClipCount: maximumClipCount)
                         : "Record new clips or upload the moments you want me to work with.")
                        .kriaType(.body).foregroundStyle(KriaColor.mutedInk)
                }
            }
            .accessibilityElement(children: .combine)

            if hasMedia {
                HStack(spacing: 8) {
                    if readiness.attachedCount > 0, readiness.pendingCount == 0 { KriaCheckDisc(size: 16) }
                    Text(readiness.pendingCount > 0
                         ? "Uploading \(FootageStageCopy.count(readiness.pendingCount, "file"))…"
                         : "\(readiness.attachedCount) \(readiness.attachedCount == 1 ? "clip" : "clips") ready")
                        .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                }
                .accessibilityElement(children: .combine)
                .accessibilityIdentifier(readiness.pendingCount > 0 ? "footage-upload-progress" : "footage-ready-count")
                if !uploads.isEmpty {
                    VStack(spacing: 8) {
                        ForEach(uploads) { record in
                            KriaMediaRow(name: BackgroundUploadCoordinator.displayFilename(record.filename),
                                         status: .uploading(progress.values[record.id] ?? 0), onFill: true) {
                                CreationRecordThumbnail(recordID: record.id, version: previewVersion)
                            }
                        }
                    }
                    .accessibilityIdentifier("footage-uploading-clips")
                }
            }

            if hasMedia, reachedLimit {
                // At the most clips there is nothing to add: say so, and offer the place where a clip can be removed.
                VStack(alignment: .leading, spacing: 8) {
                    Text("You’ve added \(FootageStageCopy.count(maximumClipCount, "clip")), the most for this format. Remove one to add another.")
                        .kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                    if let manageFootage {
                        Button("Manage clips", action: manageFootage)
                            .buttonStyle(KriaButtonStyle(role: .secondary, size: .medium))
                            .disabled(isBusy).accessibilityIdentifier("manage-footage")
                    }
                }
            } else if hasMedia {
                let addMore = Text("Add more").kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                if typeSize.isAccessibilitySize {
                    VStack(alignment: .leading, spacing: 8) {
                        addMore
                        KriaSourceRow(options: sourceOptions, layout: .compact)
                    }
                } else {
                    HStack(spacing: 12) {
                        addMore
                        KriaSourceRow(options: sourceOptions, layout: .compact)
                    }
                }
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    // A failure notice never sits under the butter lead tile.
                    KriaSourceRow(options: sourceOptions, leadsWithFirst: talkingLeads && recordFootage != nil && failures.isEmpty)
                    Text(limitText).kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                }
            }
            if !failures.isEmpty {
                // One notice, not one per file (KRI-211): a file that couldn't be sent never holds up the rest.
                KriaNoticeCard(
                    tone: .failure, title: UploadFailureBanner.message(for: failures),
                    action: UploadFailureBanner.canRetry(failures)
                        ? KriaNoticeAction(title: "Retry") { for failure in failures where failure.cause == .uploadFailed { retryFailure(failure.id) } } : nil,
                    link: KriaNoticeAction(title: "Dismiss") { failures.forEach { dismissFailure($0.id) } })
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("footage-upload-failures")
            }

            if visualsReadiness.attachedCount > 0 {
                Text("\(visualsReadiness.attachedCount) \(visualsReadiness.attachedCount == 1 ? "visual" : "visuals") ready")
                    .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
            }
        }
    }
}

struct FootageReadiness: Equatable, Sendable {
    let attachedCount: Int
    let pendingCount: Int

    init(attachedCount: Int, pendingCount: Int) {
        self.attachedCount = max(0, attachedCount)
        self.pendingCount = max(0, pendingCount)
    }

    var canContinue: Bool { attachedCount > 0 && pendingCount == 0 }

}

private struct FootageThumbnail: View {
    let index: Int

    var body: some View {
        BundledPosterImage(name: index.isMultiple(of: 2) ? "montage" : "voiceover")
            .scaledToFill()
            .frame(width: 64, height: 76)
            .clipped()
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(alignment: .bottomTrailing) {
                Image(systemName: "checkmark")
                    .font(.system(size: 8, weight: .bold))
                    .foregroundStyle(Color.white)
                    .frame(width: 17, height: 17)
                    .background(KriaColor.ink)
                    .clipShape(Circle())
                    .padding(5)
            }
    }
}

struct DirectionStage: View {
    /// Where a silent video sends the creator: back to the camera, or to a different file.
    var recordAgain: (() -> Void)? = nil
    var chooseAnother: (() -> Void)? = nil
    let approval: ApprovalSnapshot
    let format: CreationFormat?
    /// The v2 thread's `speech_cleanup` projection (`CreationThread.speechCleanup`).
    /// Nil for a thread that predates the field or hasn't loaded it yet, which
    /// resolves to the same plain "Create this video" flow as `applicable: false`.
    var speechCleanup: [String: JSONValue]? = nil
    /// KRI-306: the video shapes on offer and the creator's current pick. Nil
    /// (older server, nothing choosable) hides the picker; the parent reads the pick
    /// when it sends the approval, so `decide` needs no extra argument.
    var renderShape: (offer: RenderShapeOffer, choice: Binding<RenderShapeChoice>)? = nil
    /// KRI-881: the draft's sourced wording and sources (`draft_applied.research_preview`). Nil (no research, an
    /// older server, or an unreadable payload) leaves the card exactly as it was.
    var researchPreview: ResearchPreview? = nil
    var replyLanguage: ClipAnswerLanguage = .english
    let isBusy: Bool
    var responseStartedAt: Date? = nil
    /// `decision` is "approve" or "deny". `cleanupChoice` is only meaningful on
    /// "approve" -- "clean" / "keep_original" / "create_without_cleanup" -- and
    /// nil otherwise. `analysisID`, when known, always rides along so the
    /// server can match the decision to the analysis it answered about.
    let decide: (_ decision: String, _ cleanupChoice: String?, _ analysisID: String?) -> Void
    /// Posts the v1-style `retry_speech_cleanup` thread action (shared endpoint,
    /// not the approval decision) when the analysis itself failed.
    let retrySpeechCheck: (_ analysisID: String?) -> Void

    private var directionTitle: String {
        approval.consequenceSummary
            .replacingOccurrences(of: "Render this draft:", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var cleanupResolution: (offer: SpeechCleanupOffer, analysisID: String?) {
        SpeechCleanupOffer.resolve(speechCleanup)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: approval.retryOfExecutionID == nil ? "Here’s the direction I’ll use" : "Retry your approved edit",
                bodyText: approval.retryOfExecutionID == nil ? "I’ve shaped a clear creative direction from your footage and what you told me." : "This reruns the saved edit with the same clips, text, timing and audio."
            )

            VStack(alignment: .leading, spacing: 14) {
                Text("Creative direction")
                    .kriaType(.label)
                    .foregroundStyle(KriaColor.mutedInk)
                ChatResponseText(content: directionTitle.isEmpty ? "A considered first cut" : directionTitle, startedAt: responseStartedAt)
                    .font(KriaFont.body(22))
                    .foregroundStyle(KriaColor.ink)
                    .copyableMessage(directionTitle, previewShape: RoundedRectangle(cornerRadius: 8))

                if approval.retryOfExecutionID == nil {
                    Rectangle().fill(KriaColor.line).frame(height: 1)
                    DirectionRow(label: "Format", value: "\(format?.title ?? "Video") · short-form")
                    DirectionRow(label: "Rhythm", value: "Natural, intentional")
                    DirectionRow(label: "Opening", value: "The story, right away")
                }
            }
            .padding(16)
            .background(KriaColor.sage)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

            if let researchPreview {
                ResearchPreviewCard(preview: researchPreview, strings: ResearchPreviewStrings(language: replyLanguage))
            }

            if let renderShape {
                VideoShapeCard(offer: renderShape.offer, choice: renderShape.choice, isEnabled: !isBusy)
            }

            cleanupActions

            Button(approval.retryOfExecutionID == nil ? "Change direction" : "Cancel retry") { decide("deny", nil, nil) }
                .buttonStyle(CanonicalSecondaryButtonStyle())
                .disabled(isBusy)
        }
        .accessibilityIdentifier("direction-stage")
    }

    @ViewBuilder private var cleanupActions: some View {
        let resolution = cleanupResolution
        switch resolution.offer {
        case .plain:
            Button(approval.retryOfExecutionID == nil ? "Create this video" : "Retry approved edit") { decide("approve", nil, resolution.analysisID) }
                .buttonStyle(CanonicalPrimaryButtonStyle())
                .disabled(isBusy)
        case .checking:
            ProgressView("Checking speech…")
                .accessibilityIdentifier("speech-cleanup-checking")
            Button("Create without cleanup") { decide("approve", "create_without_cleanup", resolution.analysisID) }
                .buttonStyle(CanonicalSecondaryButtonStyle())
                .disabled(isBusy)
        case .noSpeech:
            NoSpeechRecoveryCard(recordAgain: recordAgain, chooseAnother: chooseAnother)
        case .failed:
            Text("The speech check couldn’t finish.")
                .font(KriaFont.body(13)).foregroundStyle(KriaColor.zinc)
            Button("Retry speech check") { retrySpeechCheck(resolution.analysisID) }
                .disabled(isBusy)
            Button("Create without cleanup") { decide("approve", "create_without_cleanup", resolution.analysisID) }
                .buttonStyle(CanonicalPrimaryButtonStyle())
                .disabled(isBusy)
        case let .choice(stats):
            if stats.requestedInChat {
                SpeechCleanupPreselectedChoice(
                    stats: stats,
                    isDisabled: isBusy,
                    create: { clean in decide("approve", clean ? "clean" : "keep_original", resolution.analysisID) }
                )
            } else {
                SpeechCleanupChoiceButtons(
                    stats: stats,
                    isDisabled: isBusy,
                    clean: { decide("approve", "clean", resolution.analysisID) },
                    keepOriginal: { decide("approve", "keep_original", resolution.analysisID) }
                )
            }
        }
    }
}

private struct DirectionRow: View {
    let label: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(label)
                .font(KriaFont.body(12))
                .foregroundStyle(KriaColor.zinc)
                .frame(width: 64, alignment: .leading)
            Text(value)
                .font(KriaFont.body(12).weight(.medium))
                .foregroundStyle(KriaColor.ink)
            Spacer(minLength: 0)
        }
    }
}

struct RenderingStage: View {
    var isPreparing = false
    var preparationMessage: String? = nil
    var preparationCompleted: Int = 0
    var preparationTotal: Int = 0
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: "I’m shaping your video",
                bodyText: "Your direction is locked. I’m assembling the story, sound, and final details now."
            )

            VStack(alignment: .leading, spacing: 0) {
                RenderStep(
                    icon: nil,
                    isActive: true,
                    title: isPreparing ? "Preparing your footage and edit" : "Rendering final video",
                    detail: preparationDetail
                )
            }
            .padding(16)
            .background(KriaColor.fill)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "sparkles")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(KriaColor.ink)
                Text("You can leave this screen. I’ll keep working and your cut will appear here when it’s ready.")
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.ink)
                    .lineSpacing(3)
            }
            .padding(13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(KriaColor.sage)
            .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
        }
    }

    private var preparationDetail: String {
        if isPreparing, preparationTotal > 0 {
            return "\(preparationMessage ?? "Preparing your clips…") · \(preparationCompleted) of \(preparationTotal) ready"
        }
        return preparationMessage ?? "Timing varies with footage length"
    }
}

private struct RenderStep: View {
    let icon: String?
    let isActive: Bool
    let title: String
    let detail: String

    var body: some View {
        HStack(spacing: 11) {
            Group {
                if let icon {
                    Image(systemName: icon)
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(KriaColor.ink)
                } else {
                    Circle().fill(KriaColor.ink).frame(width: 8, height: 8)
                }
            }
            .frame(width: 20, height: 20)
            .background(icon == nil ? KriaColor.softZinc : KriaColor.sage)
            .clipShape(Circle())

            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(KriaFont.body(13).weight(isActive ? .semibold : .medium))
                Text(detail)
                    .font(KriaFont.body(11))
                    .foregroundStyle(KriaColor.zinc)
            }
            Spacer()
            if isActive {
                ProgressView().controlSize(.small).tint(KriaColor.ink)
            }
        }
        .padding(.vertical, 7)
    }
}

struct ReadyStage: View {
    let project: ProjectSummary
    let openEditor: () -> Void
    let suggest: (String) -> Void
    /// KRI-756. Nil or empty = no server chips to show (capability off, still loading, failed, unavailable, or
    /// everything is already in the edit), so the fixed chips below stay exactly as they were. Non-empty = the
    /// server owns this row: its chips are sent on tap, and the fixed ones are gone.
    var serverSuggestions: [SuggestionChip]? = nil
    var sendSuggestion: (SuggestionChip) -> Void = { _ in }
    /// AI suggestion blocks. Non-nil = the server's set carries v2 suggestions: the cards own this row, whatever
    /// the chips say, and stay quiet (no label, no chips) once the creator has handled them all.
    var suggestionCards: SuggestionCardsPresentation? = nil
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    /// Re-record: drawn only when the server offers it (`.unavailable` draws nothing, so an old server changes nothing here).
    var rerecord: RerecordPhase = .unavailable
    var rerecordActions = RerecordActions()

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: "Your first cut is ready",
                bodyText: "Watch it through, then tell me what you want to refine or open the editor for hands-on changes."
            )

            let cardLayout = dynamicTypeSize.isAccessibilitySize
                ? AnyLayout(VStackLayout(alignment: .leading, spacing: 13))
                : AnyLayout(HStackLayout(spacing: 13))
            cardLayout {
                Group {
                    if let posterURL = project.posterURL {
                        AsyncImage(url: posterURL) { phase in
                            if let image = phase.image {
                                image.resizable().scaledToFill()
                            } else if phase.error == nil {
                                ProgressView().tint(KriaColor.ink)
                            } else {
                                ProjectPosterPlaceholder()
                            }
                        }
                    } else {
                        ProjectPosterPlaceholder()
                    }
                }
                .frame(width: 76, height: 98)
                .clipped()
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))

                VStack(alignment: .leading, spacing: 7) {
                    Text("Ready")
                        .kriaType(.label)
                        .foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 4)
                        .background(KriaColor.sage)
                        .clipShape(Capsule())
                    Text(project.workspaceTitle)
                        .font(KriaFont.body(14).weight(.semibold))
                        .fixedSize(horizontal: false, vertical: true)
                    Text("First cut")
                        .font(KriaFont.body(11))
                        .foregroundStyle(KriaColor.zinc)

                    Button("Open editor", action: openEditor)
                        .buttonStyle(CanonicalPrimaryButtonStyle())
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(12)
            .background(KriaColor.fill)
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

            RerecordCard(phase: rerecord, actions: rerecordActions)

            if let suggestionCards {
                if !suggestionCards.isEmpty {
                    Text("Ways to improve it")
                        .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                    SuggestionCardList(presentation: suggestionCards, idPrefix: "suggestion-card")
                }
            } else {
                // KRI-769: no chips to offer (capability on, set empty or still loading) = no dangling question either.
                if serverSuggestions.map({ !$0.isEmpty }) ?? true {
                    Text("What would you like to change?")
                        .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                }

                if let serverSuggestions {
                    // Capability on: the server's chips or nothing. The fixed chips are generic and the server's own
                    // stoplist rejects them, so they must not flash while the set loads or fill in when it is empty.
                    if !serverSuggestions.isEmpty {
                        SuggestionChipRow(chips: serverSuggestions, send: sendSuggestion)
                            .accessibilityIdentifier("ready-suggestions")
                    }
                } else {
                    PlanChipFlow(spacing: 8) {
                        PromptChip(text: "Try a stronger opening", action: suggest)
                        PromptChip(text: "Make it warmer", action: suggest)
                    }
                }
            }
        }
    }
}

private struct ProjectPosterPlaceholder: View {
    var body: some View {
        ZStack {
            KriaColor.ink
            Image(systemName: "film.stack")
                .font(.system(size: 20, weight: .medium))
                .foregroundStyle(KriaColor.sky)
        }
        .accessibilityLabel("Video thumbnail loading")
    }
}

private struct PromptChip: View {
    let text: String
    let action: (String) -> Void
    var body: some View {
        KriaAnswerChip(title: text) { action(text) }
            .accessibilityLabel("Use suggestion: \(text)")
    }
}

/// Tappable, exact-string reply choices for a clarifying question (e.g. the all-media capacity preflight).
/// Each choice is a full-width answer block, one under the other, so a full sentence wraps and the block grows.
struct QuestionOptionsRow: View {
    let options: [String]
    let recommendedOption: String?
    let select: (String) -> Void

    var body: some View {
        KriaAnswerStack(
            options: options.map { KriaAnswerOption(id: $0, title: $0, isRecommended: $0 == recommendedOption) },
            select: { select($0.id) }
        )
        .padding(.top, 2)
    }
}

/// Newest-unanswered-question state for `ReplyOptionRows`. History rows get no mode, so they draw no answers.
struct ReplyOptionsMode {
    /// Sends the tapped label as a normal user message (plain text, no structured answer).
    let select: (String) -> Void
    /// "Type my own": puts the creator in the composer; nothing is sent.
    let other: () -> Void
    /// The tapped answer is on its way (its message is pending); the card stays open until the send lands.
    var isSending = false
    /// The answer whose message is pending, when it is one of the offered labels.
    var pendingOption: String?
    /// The creator is typing their own answer, so the blocks are disabled.
    var isTyping = false
}

/// A filled capsule for a SUGGESTION, never for a question's answer (those are `KriaAnswerStack` blocks).
/// A thin wrapper over `KriaAnswerChip` that keeps the call shape the suggestion rows use.
struct KriaFilledChip: View {
    let text: String
    /// KRI-756: one muted line under the label (footage evidence). Hidden when nil.
    var caption: String? = nil
    /// KRI-756: the lead chip of a set (the stock "let Kria cut it" move) is announced as recommended to VoiceOver only.
    var isPrimary = false
    var state: KriaAnswerState = .idle
    let action: () -> Void

    var body: some View {
        KriaAnswerChip(title: text, caption: caption, state: state, isSuggestion: true, isLead: isPrimary, action: action)
            .accessibilityLabel(caption.map { "\(text), \($0)" } ?? text)
            .accessibilityValue(isPrimary ? "Recommended" : "")
    }
}

/// KRI-756: the server's suggestion chips, in rank order (the set arrives sorted; this view never reorders). Tapping one
/// SENDS its `message`; the label is only what the creator reads. Draws nothing for an empty list. The lead
/// `default_edit` chip is the primary one.
struct SuggestionChipRow: View {
    let chips: [SuggestionChip]
    /// The chip whose message is on its way: it shows a spinner while the others dim.
    var sendingID: String? = nil
    let send: (SuggestionChip) -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        if !chips.isEmpty {
            PlanChipFlow(spacing: 8) {
                ForEach(Array(chips.enumerated()), id: \.element.id) { index, chip in
                    KriaFilledChip(text: chip.label, caption: chip.why, isPrimary: chip.isDefaultEdit,
                                   state: sendingID == chip.id ? .sending : .idle) { send(chip) }
                        .opacity(sendingID != nil && sendingID != chip.id ? 0.4 : 1)
                        .disabled(sendingID != nil)
                        .accessibilityIdentifier("suggestion-chip-\(index)")
                        .accessibilityHint("Sends this to Kria")
                }
            }
            .transition(.opacity)
            .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: chips.map(\.id))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("suggestion-chips")
        }
    }
}

/// The offered answers of a clarifying question as stacked answer blocks, plus "Type my own".
struct ReplyOptionRows: View {
    let options: [String]
    let mode: ReplyOptionsMode

    var body: some View {
        KriaAnswerStack(
            options: options.enumerated().map { KriaAnswerOption(id: $0.element, title: $0.element, identifier: "reply-option-\($0.offset)") },
            isSending: mode.isSending, pendingID: mode.pendingOption, isLocked: mode.isTyping,
            typeMyOwn: mode.other,
            select: { mode.select($0.id) }
        )
        .padding(.top, 2)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("reply-options")
    }
}

struct FailedStage: View {
    var title = "Your video wasn't made"
    var bodyText = "Your direction and footage are safe. Refresh the project or tell me what you want to change."
    var retryLabel = "Refresh project"
    var retry: (() -> Void)?
    /// Structural failures need a correction in the saved conversation.
    /// Retrying the same request cannot resolve them.
    var nonRetryableReasonCode: String? = nil
    /// The typed creator decline behind a non-retryable failure (`decline_reason`).
    var declineReason: String? = nil
    /// Hands the creator the composer so a correction keeps the saved conversation.
    var replyInChat: (() -> Void)? = nil

    /// The one way forward the failed card offers.
    enum Affordance: Equatable {
        /// Nothing to tap: the copy says to wait, and the composer stays.
        case none
        /// Focus the composer: the next step is a reply in this chat.
        case reply
        case newChat
        case retry
    }

    static func affordance(code: String?, declineReason: String?, canReply: Bool, canRetry: Bool) -> Affordance {
        // A Retry or Refresh would answer the same, and New chat would drop the conversation.
        if isNotRetryableNowFailureCode(code) { return .none }
        if isNonRetryableFailureCode(code) {
            return canReply ? .reply : .newChat
        }
        return canRetry ? .retry : .none
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: title,
                bodyText: bodyText
            )
            switch Self.affordance(code: nonRetryableReasonCode, declineReason: declineReason,
                                   canReply: replyInChat != nil, canRetry: retry != nil) {
            case .none:
                EmptyView()
            case .reply:
                if let replyInChat {
                    Button("Reply to Kria", action: replyInChat)
                        .buttonStyle(CanonicalPrimaryButtonStyle())
                        .accessibilityIdentifier("failed-stage-reply")
                }
            case .newChat:
                NewChatButton()
                    .accessibilityIdentifier("failed-stage-start-new-edit")
            case .retry:
                if let retry {
                    Button(retryLabel, action: retry)
                        .buttonStyle(CanonicalPrimaryButtonStyle())
                }
            }
        }
    }
}

struct ThinkingRow: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var shimmerOffset: CGFloat = -1

    private static let shimmerInterval = 2.25
    private static let shimmerDuration = 0.58
    private var animates: Bool { !reduceMotion && scenePhase == .active }

    var body: some View {
        Text("Thinking")
            .font(KriaFont.body(12))
            .foregroundStyle(KriaColor.zinc)
            .overlay {
                if animates {
                    GeometryReader { proxy in
                        LinearGradient(
                            colors: [KriaColor.paper.opacity(0), KriaColor.paper.opacity(0.65), KriaColor.paper.opacity(0)],
                            startPoint: .leading,
                            endPoint: .trailing
                        )
                        .frame(width: max(proxy.size.width * 0.65, 16))
                        .offset(x: shimmerOffset * proxy.size.width)
                    }
                    .mask(Text("Thinking").font(KriaFont.body(12)))
                }
            }
        .frame(minHeight: 30)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Kria is thinking")
        .accessibilityIdentifier("chat-thinking")
        .task(id: animates) { await shimmer() }
    }

    private func shimmer() async {
        guard animates else {
            resetShimmer()
            return
        }

        while !Task.isCancelled {
            resetShimmer()
            withAnimation(.linear(duration: Self.shimmerDuration)) { shimmerOffset = 1.25 }
            do { try await Task.sleep(for: .seconds(Self.shimmerInterval)) } catch { return }
        }
    }

    private func resetShimmer() {
        var transaction = Transaction()
        transaction.disablesAnimations = true
        withTransaction(transaction) { shimmerOffset = -1 }
    }
}

/// A problem shown in the chat's recovery card. Its cause picks the card's title
/// and button, so only a failure to reach Kria asks the user to reconnect.
struct ChatFailure: Equatable {
    let message: String
    let cause: RequestFailureCause

    init(_ message: String, cause: RequestFailureCause = .other) {
        self.message = message
        self.cause = cause
    }

    /// `summary` says what didn't happen; the rest explains `error`, pointing at
    /// the connection only when the request never got a response.
    init(_ summary: String, error: Error) {
        let cause = RequestFailureCause(error)
        let reason = cause == .connection ? "Check your connection and try again." : error.localizedDescription
        self.init("\(summary) \(reason)", cause: cause)
    }

    var title: String { cause == .connection ? "Connection interrupted" : "Something went wrong" }
    /// The card's button reloads the chat; it doesn't resend the failed request.
    var retryLabel: String { cause == .connection ? "Reconnect" : "Refresh" }
}

struct RecoveryCard: View {
    let failure: ChatFailure
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(failure.title)
                .font(KriaFont.body(13).weight(.semibold))
            Text(failure.message)
                .font(KriaFont.body(12))
                .foregroundStyle(KriaColor.zinc)
                .lineSpacing(3)
            Button(failure.retryLabel, action: retry)
                .font(KriaFont.body(12).weight(.semibold))
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.softZinc)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

/// Copy for the "some files weren't sent" banner (KRI-211). Says what actually happened: never
/// "couldn't be read" for a network failure, and only "won't be sent" where that is true.
enum UploadFailureBanner {
    static func message(for failures: [UploadFailure]) -> String {
        let unreadable = failures.filter { $0.cause == .unreadable }.count
        let failedUpload = failures.filter { $0.cause == .uploadFailed }.count
        let cannotResume = failures.filter { $0.cause == .cannotResume }.count
        func files(_ n: Int) -> String { n == 1 ? "1 file" : "\(n) files" }
        switch (unreadable > 0, failedUpload > 0, cannotResume > 0) {
        case (true, false, false):
            return "\(files(unreadable)) couldn’t be read and won’t be sent"
        case (false, true, false):
            return "\(files(failedUpload)) couldn’t upload"
        case (false, false, true):
            return "\(files(cannotResume)) couldn’t be resumed and won’t be sent"
        default:
            return "\(files(failures.count)) won’t be sent"
        }
    }

    /// Only an upload that failed on the way still has its record, so only that can be retried.
    static func canRetry(_ failures: [UploadFailure]) -> Bool {
        failures.contains { $0.cause == .uploadFailed }
    }
}

struct ChatComposer: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    @Binding var text: String
    let isSending: Bool
    let canAttach: Bool
    var canSendWithoutText = false
    var blocksSubmission = false
    var placeholder = "Tell Kria what you want…"
    /// The question the creator is answering in their own words. Shown as a capsule above the field.
    var answering: String? = nil
    var cancelAnswering: () -> Void = {}
    var isFocused: FocusState<Bool>.Binding
    /// KRI-742: pass a controller (only when the server advertises `voice_dictation`) to show the mic.
    var dictation: DictationController? = nil
    var dictationTerms: () -> [String] = { [] }
    let attach: () -> Void
    let send: () -> Void
    /// When set, + opens the floating tray (Record, Upload, script, voiceover) instead of calling `attach`.
    var attachMenu: ChatAttachMenu? = nil
    @State private var showsTray = false

    private var dictationIsBusy: Bool {
        dictation.map { $0.phase == .finalizing || $0.phase == .requestingPermission } ?? false
    }
    private var canSend: Bool {
        (!text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || canSendWithoutText) && !isSending && !blocksSubmission
            && !ChatSubmission.isBareCorrectionStub(text) && !dictationIsBusy
    }

    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: 30, style: .continuous) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let answering { answeringCapsule(answering) }
            field
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .sensoryFeedback(.impact(weight: .light, intensity: 0.6), trigger: dictation?.phase)
        .onDisappear { dictation?.cancel() }
    }

    /// Just the question, with no prefix. It grows with the whole question (never cut off) and stays at least 44pt.
    private func answeringCapsule(_ question: String) -> some View {
        HStack(alignment: .center, spacing: 0) {
            Text(question)
                .kriaType(.label).foregroundStyle(KriaColor.mutedInk)
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.leading, 16).padding(.vertical, 12)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: cancelAnswering) {
                Image(systemName: "xmark").font(.system(size: 13, weight: .semibold)).foregroundStyle(KriaColor.ink)
                    .frame(width: 44, height: 44).contentShape(Rectangle())
            }
            .accessibilityLabel("Stop answering this question")
            .accessibilityIdentifier("chat-answering-cancel")
        }
        .frame(minHeight: 44)
        .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("chat-answering")
    }

    private var field: some View {
        VStack(alignment: .leading, spacing: 0) {
            if showsTray, let attachMenu {
                ChatAttachTray(menu: attachMenu) { showsTray = false }
                    .padding(.top, 8)
                    .transition(.opacity)
            }
            composer
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let notice = dictation?.notice {
                Text(notice)
                    .font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                    .padding(.horizontal, 14).padding(.top, 8).padding(.bottom, 2)
                    .accessibilityIdentifier("chat-dictation-notice")
            }
            HStack(alignment: .bottom, spacing: 4) {
                Button { if attachMenu != nil { showsTray.toggle() } else { attach() } } label: {
                    if attachMenu != nil {
                        Image(systemName: showsTray ? "xmark" : "plus").font(.system(size: 17, weight: .semibold))
                            .foregroundStyle(showsTray ? KriaColor.ink : KriaColor.paper)
                            .frame(width: 44, height: 44).background(showsTray ? KriaColor.fill : KriaColor.ink, in: Circle())
                    } else {
                        KriaIcon(.plus).frame(width: 44, height: 44)
                    }
                }
                .disabled(attachMenu == nil && !canAttach)
                .accessibilityLabel(showsTray ? "Close attach menu" : "Attach footage")
                .accessibilityHint(canAttach ? "" : "Choose a video format first")
                .accessibilityIdentifier("chat-attach")
                TextField(dictation?.isListening == true && text.isEmpty ? "Listening…" : placeholder, text: $text, axis: .vertical)
                    .font(KriaFont.body(15)).lineLimit(1...4)
                    .frame(minHeight: 44).accessibilityLabel("Message Kria")
                    .focused(isFocused)
                    .submitLabel(.send)
                    // Dictated words are written into the field; typing over them mid-sentence would race the recognizer.
                    .disabled(dictation?.isActive == true)
                    .onSubmit {
                        if canSend {
                            send()
                        } else {
                            isFocused.wrappedValue = false
                        }
                    }
                if let dictation {
                    Button { toggleDictation(dictation) } label: { DictationMicLabel(dictation: dictation) }
                        .disabled(dictationIsBusy || (isSending && !dictation.isListening))
                        .accessibilityLabel(dictation.isListening ? "Stop dictation" : "Dictate message")
                        .accessibilityIdentifier("chat-dictate")
                }
                Button(action: sendTapped) {
                    Image(systemName: isSending ? "ellipsis" : "arrow.up")
                        .font(.system(size: 18, weight: .semibold)).foregroundStyle(.white)
                        .frame(width: 44, height: 44).background(KriaColor.ink, in: Circle())
                }.disabled(!canSend).opacity(canSend ? 1 : 0.45)
                    .accessibilityLabel(isSending ? "Sending message" : (canSendWithoutText && text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Send clips" : "Send message"))
                    .accessibilityIdentifier("chat-send-message")
            }
        }
        .padding(7).kriaFloatingSurface(shape)
        .background(answering != nil ? KriaColor.selectionSoft : Color.clear, in: shape)
        .tint(answering != nil ? KriaColor.sky : nil)
        .onChange(of: isSending) { _, sending in if sending { showsTray = false } }
    }

    private func toggleDictation(_ dictation: DictationController) {
        if dictation.isActive {
            Task { await dictation.stop() }
        } else {
            isFocused.wrappedValue = false
            dictation.start(base: text, contextTerms: dictationTerms()) { text = $0 }
        }
    }

    /// Sending while the mic is live finishes the dictation first, so the message carries the final text.
    private func sendTapped() {
        guard let dictation, dictation.isListening else { send(); return }
        Task {
            await dictation.stop()
            if canSend { send() }
        }
    }
}

/// The mic button's face: a mic at rest, a stop square inside a ring that breathes with the voice while
/// listening, a spinner while the server pass runs.
struct DictationMicLabel: View {
    let dictation: DictationController
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        ZStack {
            switch dictation.phase {
            case .idle:
                Image(systemName: "mic").font(.system(size: 19, weight: .medium)).foregroundStyle(KriaColor.ink)
            case .requestingPermission, .finalizing:
                ProgressView().controlSize(.small).tint(KriaColor.ink)
            case .listening:
                Circle().fill(KriaColor.butter.opacity(0.55))
                    .scaleEffect(KriaMotion.isReduced(reduceMotion) ? 1 : 1 + dictation.level * 0.22)
                    .animation(.easeOut(duration: 0.08), value: dictation.level)
                Circle().fill(KriaColor.butter).frame(width: 34, height: 34)
                Image(systemName: "stop.fill").font(.system(size: 13, weight: .bold)).foregroundStyle(KriaColor.ink)
            }
        }
        .frame(width: 44, height: 44)
        .contentShape(Rectangle())
    }
}

struct BundledPosterImage: View {
    let name: String

    var body: some View {
        if let url = Bundle.main.url(forResource: name, withExtension: "jpg"),
           let image = UIImage(contentsOfFile: url.path) {
            Image(uiImage: image).resizable()
        } else {
            KriaColor.softZinc
                .overlay {
                    Image(systemName: "photo")
                        .foregroundStyle(KriaColor.zinc)
                }
        }
    }
}
