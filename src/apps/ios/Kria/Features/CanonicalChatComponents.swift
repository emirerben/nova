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
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
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
            .background(Color.white)
            .overlay {
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(KriaColor.line, lineWidth: 1)
            }
            .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
            .opacity(isEnabled ? (configuration.isPressed ? 0.65 : 1) : 0.45)
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
            HStack(spacing: 8) {
                Button(action: openProjects) { KriaIcon(.menu).frame(width: 44, height: 44).background(KriaColor.menu, in: Circle()) }
                    .accessibilityLabel(projectsDrawerOpen ? "Close projects" : "Open projects")
                    .accessibilityIdentifier("workspace-menu-toggle")
                Text(project.workspaceTitle)
                    .font(KriaFont.body(15).weight(.semibold))
                    .lineLimit(1).frame(maxWidth: .infinity)
                    .accessibilityIdentifier("workspace-project-title")
                    .accessibilityHidden(projectsDrawerOpen)
                ProjectActionsMenu(project: project)
                    .accessibilityHidden(projectsDrawerOpen)
                    .allowsHitTesting(!projectsDrawerOpen)
            }
            .padding(.horizontal, 16).frame(minHeight: 64)
            if showsEditorSwitch {
                HStack(spacing: 4) {
                    Text("Chat").font(KriaFont.body(13).weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 44)
                        .background(KriaColor.selectionSoft, in: RoundedRectangle(cornerRadius: 10))
                        .accessibilityAddTraits(.isSelected)
                    Button("Editor", action: openEditor).font(KriaFont.body(13).weight(.medium))
                        .frame(maxWidth: .infinity, minHeight: 44)
                }.padding(.horizontal, 16).padding(.bottom, 8)
                    .accessibilityHidden(projectsDrawerOpen)
                    .allowsHitTesting(!projectsDrawerOpen)
            }
        }.foregroundStyle(KriaColor.ink).background(WorkspaceSurface())
    }
}

struct ProjectsDrawer: View {
    let close: () -> Void
    let openGallery: () -> Void
    var openAccount: () -> Void = {}
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
                    Text("\(model.libraryProjects.count) \(model.libraryProjects.count == 1 ? "video" : "videos")").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                }.frame(minHeight: 44).padding(.horizontal, 12)
            }
            VStack(alignment: .leading, spacing: 10) {
                Text("Recent chats").font(KriaFont.body(13).weight(.medium)).foregroundStyle(KriaColor.mutedInk).padding(.horizontal, 12)
                ScrollView {
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
                                    ProjectDrawerRow(project: project, isSelected: project.id == model.selectedProject?.id)
                                }.buttonStyle(.plain)
                                ProjectActionsMenu(project: project)
                            }.background(project.id == model.selectedProject?.id ? KriaColor.selectionSoft : .clear, in: RoundedRectangle(cornerRadius: 10))
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
        .task { menuFocused = true; await model.loadLibrary() }
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
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
    }
}

struct ChatMessageRow: View {
    let message: ChatTranscriptMessage

    var body: some View {
        if message.role == .user {
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
                    .background {
                        UnevenRoundedRectangle(
                            topLeadingRadius: 18,
                            bottomLeadingRadius: 18,
                            bottomTrailingRadius: 6,
                            topTrailingRadius: 18,
                            style: .continuous
                        )
                        .fill(KriaColor.ink)
                    }
                    .opacity(message.isPending ? 0.68 : 1)
                    .accessibilityLabel("You: \(message.content)")
                    .accessibilityIdentifier("chat-message-\(message.id)")
            }
            .frame(maxWidth: .infinity)
        } else {
            VStack(alignment: .leading, spacing: 7) {
                Text("Kria")
                    .font(KriaFont.body(11).weight(.semibold))
                    .foregroundStyle(KriaColor.zinc)
                Text(message.content)
                    .font(KriaFont.body(14))
                    .foregroundStyle(KriaColor.ink)
                    .lineSpacing(4)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityLabel("Kria: \(message.content)")
        }
    }
}

private struct AssistantHeading: View {
    let title: String
    let bodyText: String
    var isFormatHeading = false

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .font(isFormatHeading ? KriaFont.display(28) : KriaFont.body(20))
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

struct FormatStage: View {
    let formats: [CreationFormat]
    let isBusy: Bool
    let select: (CreationFormat) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            AssistantHeading(
                title: "What kind of video are we making?",
                bodyText: "Choose a starting point. We can shape the details together.",
                isFormatHeading: true
            )

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(formats) { format in
                        Button { select(format) } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                BundledPosterImage(name: format.imageName)
                                    .scaledToFill()
                                    .frame(width: 156, height: 164)
                                    .clipped()
                                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                                Text(format.title)
                                    .font(KriaFont.display(18))
                                    .foregroundStyle(KriaColor.ink)
                            }
                            .frame(width: 156, alignment: .leading)
                        }
                        .buttonStyle(.plain)
                        .disabled(isBusy)
                        .accessibilityHint("Choose \(format.title) as this video's format")
                    }
                }
            }
            .contentMargins(.horizontal, 0, for: .scrollContent)
        }
    }
}

struct FootageStage: View {
    let format: CreationFormat
    let mediaCount: Int
    let maximumClipCount: Int
    let uploads: [UploadRecoveryRecord]
    let progress: [UUID: Double]
    let addFootage: () -> Void
    let continueWithFootage: () -> Void
    let changeFormat: () -> Void

    private var readiness: FootageReadiness {
        FootageReadiness(attachedCount: mediaCount, pendingCount: uploads.count)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: "Add your footage",
                bodyText: format == .talkingToCamera
                    ? "Choose one clear take. I’ll keep your voice at the center."
                    : "Choose the moments you want me to work with. You can add up to \(maximumClipCount) clips."
            )

            Button(action: addFootage) {
                VStack(spacing: 9) {
                    Image(systemName: "plus")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(KriaColor.ink)
                        .frame(width: 34, height: 34)
                        .background(KriaColor.sage)
                        .clipShape(Circle())
                    Text("Choose videos")
                        .font(KriaFont.body(14).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                    Text("MP4, MOV · up to \(maximumClipCount) clips")
                        .font(KriaFont.body(11))
                        .foregroundStyle(KriaColor.zinc)
                }
                .frame(maxWidth: .infinity, minHeight: 142)
                .background(Color.white)
                .overlay {
                    RoundedRectangle(cornerRadius: 12, style: .continuous)
                        .stroke(KriaColor.border, style: StrokeStyle(lineWidth: 1, dash: [6, 5]))
                }
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("choose-videos")

            if readiness.attachedCount > 0 {
                VStack(alignment: .leading, spacing: 10) {
                    Text("\(readiness.attachedCount) \(readiness.attachedCount == 1 ? "clip" : "clips") ready")
                        .font(KriaFont.body(12).weight(.medium))
                        .foregroundStyle(KriaColor.zinc)

                    HStack(spacing: 8) {
                        ForEach(0..<min(readiness.attachedCount, 5), id: \.self) { index in
                            FootageThumbnail(index: index)
                        }
                    }
                }

                Button("Continue with \(readiness.attachedCount) \(readiness.attachedCount == 1 ? "clip" : "clips")", action: continueWithFootage)
                    .buttonStyle(CanonicalPrimaryButtonStyle())
            }

            if readiness.pendingCount > 0 {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Uploading \(readiness.pendingCount) \(readiness.pendingCount == 1 ? "clip" : "clips")…")
                        .font(KriaFont.body(12).weight(.medium))
                        .foregroundStyle(KriaColor.zinc)
                    if let active = uploads.first {
                        ProgressView(value: progress[active.id] ?? 0)
                            .tint(KriaColor.ink)
                            .frame(maxWidth: 160)
                    }
                }
                .accessibilityIdentifier("footage-upload-progress")
            }

            Button("Change format", action: changeFormat)
                .font(KriaFont.body(12).weight(.medium))
                .foregroundStyle(KriaColor.zinc)
                .frame(minHeight: 44)
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

    var canContinue: Bool { attachedCount > 0 }
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
    let approval: ApprovalSnapshot
    let format: CreationFormat?
    let isBusy: Bool
    let decide: (String) -> Void

    private var directionTitle: String {
        approval.consequenceSummary
            .replacingOccurrences(of: "Render this draft:", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: "Here’s the direction I’ll use",
                bodyText: "I’ve shaped a clear creative direction from your footage and what you told me."
            )

            VStack(alignment: .leading, spacing: 14) {
                Text("CREATIVE DIRECTION")
                    .font(KriaFont.body(10).weight(.bold))
                    .tracking(1.3)
                    .foregroundStyle(KriaColor.ink)
                Text(directionTitle.isEmpty ? "A considered first cut" : directionTitle)
                    .font(KriaFont.body(22))
                    .foregroundStyle(KriaColor.ink)
                    .lineLimit(3)

                Rectangle().fill(KriaColor.line).frame(height: 1)

                DirectionRow(label: "Format", value: "\(format?.title ?? "Video") · short-form")
                DirectionRow(label: "Rhythm", value: "Natural, intentional")
                DirectionRow(label: "Opening", value: "The story, right away")
            }
            .padding(16)
            .background(KriaColor.sage)
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(KriaColor.border, lineWidth: 1)
            }
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

            Text("Nothing renders until you approve this direction.")
                .font(KriaFont.body(12))
                .foregroundStyle(KriaColor.zinc)

            Button("Create this video") { decide("approve") }
                .buttonStyle(CanonicalPrimaryButtonStyle())
                .disabled(isBusy)
            Button("Change direction") { decide("deny") }
                .buttonStyle(CanonicalSecondaryButtonStyle())
                .disabled(isBusy)
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
                    title: "Rendering final video",
                    detail: "Timing varies with footage length"
                )
            }
            .padding(16)
            .background(KriaColor.paper)
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(KriaColor.border, lineWidth: 1)
            }
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

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: "Your first cut is ready",
                bodyText: "Watch it through, then tell me what you want to refine or open the editor for hands-on changes."
            )

            HStack(spacing: 13) {
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
                    Text("READY")
                        .font(KriaFont.body(9).weight(.bold))
                        .tracking(1.1)
                        .foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 4)
                        .background(KriaColor.sage)
                        .clipShape(Capsule())
                    Text(project.workspaceTitle)
                        .font(KriaFont.body(14).weight(.semibold))
                        .lineLimit(1)
                    Text("First cut")
                        .font(KriaFont.body(11))
                        .foregroundStyle(KriaColor.zinc)

                    Button("Open editor", action: openEditor)
                        .buttonStyle(CanonicalPrimaryButtonStyle())
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(12)
            .background(KriaColor.paper)
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(KriaColor.border, lineWidth: 1)
            }
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

            Text("What would you like to change?")
                .font(KriaFont.body(13).weight(.medium))

            HStack(spacing: 8) {
                PromptChip(text: "Try a stronger opening", action: suggest)
                PromptChip(text: "Make it warmer", action: suggest)
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
        Button { action(text) } label: {
            Text(text)
                .font(KriaFont.body(11).weight(.medium))
                .foregroundStyle(KriaColor.ink)
                .padding(.horizontal, 11)
                .frame(minHeight: 44)
                .overlay(Capsule().stroke(KriaColor.border, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Use suggestion: \(text)")
    }
}

struct FailedStage: View {
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            AssistantHeading(
                title: "This cut needs another pass",
                bodyText: "Your direction and footage are safe. Refresh the project or tell me what you want to change."
            )
            Button("Refresh project", action: retry)
                .buttonStyle(CanonicalPrimaryButtonStyle())
        }
    }
}

struct ThinkingRow: View {
    var body: some View {
        HStack(spacing: 9) {
            ProgressView().controlSize(.small).tint(KriaColor.ink)
            Text("Kria is thinking…")
                .font(KriaFont.body(12))
                .foregroundStyle(KriaColor.zinc)
        }
        .frame(minHeight: 30)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Kria is thinking")
    }
}

struct RecoveryCard: View {
    let message: String
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Connection interrupted")
                .font(KriaFont.body(13).weight(.semibold))
            Text(message)
                .font(KriaFont.body(12))
                .foregroundStyle(KriaColor.zinc)
                .lineSpacing(3)
            Button("Reconnect", action: retry)
                .font(KriaFont.body(12).weight(.semibold))
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(KriaColor.softZinc)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
    }
}

struct ChatComposer: View {
    @Environment(\.projectsDrawerOpen) private var projectsDrawerOpen
    @Binding var text: String
    let isSending: Bool
    let canAttach: Bool
    let attach: () -> Void
    let send: () -> Void

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isSending
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 4) {
            Button(action: attach) { KriaIcon(.plus).frame(width: 44, height: 44) }
                .disabled(!canAttach).accessibilityLabel("Attach footage")
                .accessibilityHint(canAttach ? "" : "Choose a video format first")
            TextField("Tell Kria what you want…", text: $text, axis: .vertical)
                .font(KriaFont.body(15)).lineLimit(1...4)
                .frame(minHeight: 44).accessibilityLabel("Message Kria")
                .submitLabel(.send).onSubmit { if canSend { send() } }
            Button(action: send) {
                Image(systemName: isSending ? "ellipsis" : "arrow.up")
                    .font(.system(size: 18, weight: .semibold)).foregroundStyle(.white)
                    .frame(width: 44, height: 44).background(KriaColor.ink, in: Circle())
            }.disabled(!canSend).opacity(canSend ? 1 : 0.45)
                .accessibilityLabel(isSending ? "Sending message" : "Send message")
        }
        .padding(7).background(WorkspaceSurface())
        .overlay(RoundedRectangle(cornerRadius: 30).stroke(KriaColor.border, lineWidth: 1))
        .padding(.horizontal, 14).padding(.vertical, 12).background(WorkspaceSurface())
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
