import SwiftUI
import UIKit

struct CanonicalPrimaryButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    var fill = KriaColor.ink
    var foreground = Color.white

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

struct WorkspaceHeader: View {
    let project: ProjectSummary
    let showsEditorSwitch: Bool
    let openProjects: () -> Void
    let openEditor: () -> Void
    let openAccount: () -> Void
    @EnvironmentObject private var auth: AuthStore

    private var initial: String {
        String((auth.displayName?.trimmingCharacters(in: .whitespacesAndNewlines).first ?? "E")).uppercased()
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Text(project.workspaceTitle)
                    .font(KriaFont.body(14).weight(.semibold))
                    .foregroundStyle(KriaColor.ink)
                    .lineLimit(1)
                    .frame(maxWidth: 170)
                    .accessibilityIdentifier("workspace-project-title")

                HStack {
                    Button("Projects", action: openProjects)
                        .font(KriaFont.body(14).weight(.medium))
                        .foregroundStyle(KriaColor.ink)
                        .frame(minWidth: 68, minHeight: 44, alignment: .leading)
                        .accessibilityLabel("Open projects")

                    Spacer()

                    Button(action: openAccount) {
                        Text(initial)
                            .font(KriaFont.body(13).weight(.semibold))
                            .foregroundStyle(KriaColor.ink)
                            .frame(width: 32, height: 32)
                            .background(KriaColor.softZinc)
                            .clipShape(Circle())
                    }
                    .frame(width: 44, height: 44)
                    .accessibilityLabel("Open account")
                }
            }
            .padding(.horizontal, 16)
            .frame(height: 54)

            if showsEditorSwitch {
                HStack(spacing: 4) {
                    Text("Chat")
                        .font(KriaFont.body(13).weight(.semibold))
                        .frame(maxWidth: .infinity, minHeight: 34)
                        .background(Color.white)
                        .clipShape(RoundedRectangle(cornerRadius: 7, style: .continuous))

                    Button("Editor", action: openEditor)
                        .font(KriaFont.body(13).weight(.medium))
                        .foregroundStyle(KriaColor.zinc)
                        .frame(maxWidth: .infinity, minHeight: 34)
                }
                .padding(3)
                .background(KriaColor.softZinc)
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                .padding(.horizontal, 16)
                .padding(.bottom, 9)
            }

            Rectangle().fill(KriaColor.line).frame(height: 1)
        }
        .background(Color.white)
    }
}

struct ProjectsDrawer: View {
    let close: () -> Void
    let openGallery: () -> Void
    @EnvironmentObject private var model: AppModel
    @State private var isCreating = false

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Color.black.opacity(0.18)
                    .ignoresSafeArea()
                    .contentShape(Rectangle())
                    .onTapGesture(perform: close)

                VStack(alignment: .leading, spacing: 0) {
                    HStack {
                        Text("Projects").font(KriaFont.display(26))
                        Spacer()
                        Button(action: close) {
                            Image(systemName: "xmark")
                                .font(.system(size: 14, weight: .semibold))
                                .frame(width: 40, height: 40)
                        }
                        .accessibilityLabel("Close projects")
                    }
                    .padding(.horizontal, 20)
                    .padding(.top, 8)

                    Button {
                        Task {
                            isCreating = true
                            await model.createProject()
                            isCreating = false
                            if model.selectedProject != nil { close() }
                        }
                    } label: {
                        HStack(spacing: 10) {
                            Image(systemName: "plus")
                                .font(.system(size: 13, weight: .semibold))
                            Text(isCreating ? "Starting…" : "New video")
                                .font(KriaFont.body(14).weight(.semibold))
                            Spacer()
                        }
                        .padding(.horizontal, 14)
                        .frame(height: 44)
                    }
                    .foregroundStyle(KriaColor.ink)
                    .background(KriaColor.softZinc)
                    .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
                    .disabled(isCreating)
                    .padding(.horizontal, 20)
                    .padding(.top, 18)

                    Text("RECENT")
                        .font(KriaFont.body(10).weight(.semibold))
                        .tracking(1.4)
                        .foregroundStyle(KriaColor.zinc)
                        .padding(.horizontal, 20)
                        .padding(.top, 28)
                        .padding(.bottom, 9)

                    ScrollView {
                        LazyVStack(spacing: 2) {
                            if model.projects.isEmpty {
                                switch model.projectsState {
                                case .idle, .loading:
                                    ProgressView("Loading projects…")
                                        .tint(KriaColor.limeText)
                                        .frame(maxWidth: .infinity, minHeight: 96)
                                case .failed:
                                    VStack(spacing: 8) {
                                        Text("Projects couldn’t load.")
                                            .font(KriaFont.body(13))
                                            .foregroundStyle(KriaColor.zinc)
                                        Button("Try again") { Task { await model.loadProjects() } }
                                            .font(KriaFont.body(12).weight(.semibold))
                                    }
                                    .frame(maxWidth: .infinity, minHeight: 96)
                                case .empty, .loaded:
                                    Text("No projects yet")
                                        .font(KriaFont.body(13))
                                        .foregroundStyle(KriaColor.zinc)
                                        .frame(maxWidth: .infinity, minHeight: 96)
                                }
                            } else {
                                ForEach(model.projects) { project in
                                    Button {
                                        model.selectProject(project)
                                        close()
                                    } label: {
                                        ProjectDrawerRow(
                                            project: project,
                                            isSelected: project.id == model.selectedProject?.id
                                        )
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                        .padding(.horizontal, 10)
                    }

                    Rectangle().fill(KriaColor.line).frame(height: 1)
                    Button(action: openGallery) {
                        HStack {
                            Image(systemName: "square.grid.2x2")
                            Text("Gallery").font(KriaFont.body(14).weight(.medium))
                            Spacer()
                            Text("\(model.libraryProjects.count)")
                                .font(KriaFont.body(12))
                                .foregroundStyle(KriaColor.zinc)
                        }
                        .frame(height: 52)
                        .padding(.horizontal, 20)
                    }
                    .foregroundStyle(KriaColor.ink)
                }
                .frame(width: min(332, geometry.size.width * 0.86))
                .frame(maxHeight: .infinity)
                .background(Color.white)
                .shadow(color: .black.opacity(0.08), radius: 20, x: 4)
                .transition(.move(edge: .leading))
            }
        }
        .task { await model.loadLibrary() }
    }
}

private struct ProjectDrawerRow: View {
    let project: ProjectSummary
    let isSelected: Bool

    var body: some View {
        HStack(spacing: 11) {
            ProjectPosterView(project: project)
                .frame(width: 34, height: 42)
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay {
                    if project.status == .ready {
                        Image(systemName: "play.fill")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(KriaColor.lime)
                            .shadow(color: .black.opacity(0.35), radius: 2)
                    }
                }

            VStack(alignment: .leading, spacing: 3) {
                Text(project.workspaceTitle)
                    .font(KriaFont.body(13).weight(.medium))
                    .foregroundStyle(KriaColor.ink)
                    .lineLimit(1)
                Text(project.workspaceStatusLabel)
                    .font(KriaFont.body(11))
                    .foregroundStyle(KriaColor.zinc)
            }
            Spacer()
        }
        .padding(.horizontal, 10)
        .frame(height: 56)
        .background(isSelected ? KriaColor.softZinc : Color.clear)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
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

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("Kria")
                .font(KriaFont.body(11).weight(.semibold))
                .foregroundStyle(KriaColor.zinc)
            Text(title)
                .font(KriaFont.display(25))
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
                bodyText: "Choose a starting point. We’ll shape the direction together next."
            )

            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    ForEach(formats) { format in
                        Button { select(format) } label: {
                            VStack(alignment: .leading, spacing: 8) {
                                BundledPosterImage(name: format.imageName)
                                    .scaledToFill()
                                    .frame(width: 120, height: 154)
                                    .clipped()
                                    .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                                Text(format.title)
                                    .font(KriaFont.body(13).weight(.semibold))
                                    .foregroundStyle(KriaColor.ink)
                            }
                            .frame(width: 120, alignment: .leading)
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
                    : "Choose the moments you want me to work with. You can add up to 10 clips."
            )

            Button(action: addFootage) {
                VStack(spacing: 9) {
                    Image(systemName: "plus")
                        .font(.system(size: 18, weight: .medium))
                        .foregroundStyle(KriaColor.limeText)
                        .frame(width: 34, height: 34)
                        .background(KriaColor.limeSoft)
                        .clipShape(Circle())
                    Text("Choose videos")
                        .font(KriaFont.body(14).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                    Text("MP4, MOV · up to 10 clips")
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
                            .tint(KriaColor.limeText)
                            .frame(maxWidth: 160)
                    }
                }
                .accessibilityIdentifier("footage-upload-progress")
            }

            Button("Change format", action: changeFormat)
                .font(KriaFont.body(12).weight(.medium))
                .foregroundStyle(KriaColor.zinc)
                .frame(minHeight: 34)
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
                    .background(KriaColor.limeText)
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
                    .foregroundStyle(KriaColor.limeText)
                Text(directionTitle.isEmpty ? "A considered first cut" : directionTitle)
                    .font(KriaFont.display(20))
                    .foregroundStyle(KriaColor.ink)
                    .lineLimit(3)

                Rectangle().fill(KriaColor.line).frame(height: 1)

                DirectionRow(label: "Format", value: "\(format?.title ?? "Video") · short-form")
                DirectionRow(label: "Rhythm", value: "Natural, intentional")
                DirectionRow(label: "Opening", value: "The story, right away")
            }
            .padding(16)
            .background(Color.white)
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
            .background(Color.white)
            .overlay {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .stroke(KriaColor.border, lineWidth: 1)
            }
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))

            HStack(alignment: .top, spacing: 10) {
                Image(systemName: "sparkles")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(KriaColor.limeText)
                Text("You can leave this screen. I’ll keep working and your cut will appear here when it’s ready.")
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.limeText)
                    .lineSpacing(3)
            }
            .padding(13)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(KriaColor.limeSoft)
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
                        .foregroundStyle(KriaColor.limeText)
                } else {
                    Circle().fill(KriaColor.ink).frame(width: 8, height: 8)
                }
            }
            .frame(width: 20, height: 20)
            .background(icon == nil ? KriaColor.softZinc : KriaColor.limeSoft)
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
                                ProgressView().tint(KriaColor.limeText)
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
                        .foregroundStyle(KriaColor.limeText)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 4)
                        .background(KriaColor.limeSoft)
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
            .background(Color.white)
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
                .foregroundStyle(KriaColor.lime)
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
            ProgressView().controlSize(.small).tint(KriaColor.limeText)
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
    @Binding var text: String
    let isSending: Bool
    let attach: () -> Void
    let send: () -> Void

    private var canSend: Bool {
        !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !isSending
    }

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .bottom) {
                TextField("Message Kria…", text: $text, axis: .vertical)
                    .font(KriaFont.body(14))
                    .lineLimit(1...4)
                    .padding(.horizontal, 14)
                    .padding(.top, 12)
                    .padding(.bottom, 45)
                    .accessibilityLabel("Message Kria")
                    .submitLabel(.send)
                    .onSubmit { if canSend { send() } }

                HStack {
                    Button(action: attach) {
                        Image(systemName: "plus")
                            .font(.system(size: 17, weight: .medium))
                            .foregroundStyle(KriaColor.ink)
                            .frame(width: 40, height: 40)
                    }
                    .accessibilityLabel("Attach footage")

                    Spacer()

                    Button(action: send) {
                        Image(systemName: isSending ? "ellipsis" : "arrow.up")
                            .font(.system(size: 15, weight: .bold))
                            .foregroundStyle(Color.white)
                            .frame(width: 40, height: 40)
                            .background(KriaColor.ink)
                            .clipShape(Circle())
                    }
                    .disabled(!canSend)
                    .accessibilityLabel(isSending ? "Sending message" : "Send message")
                }
                .padding(.horizontal, 6)
                .padding(.bottom, 5)
            }
            .frame(minHeight: 76)
            .background(Color.white)
            .overlay {
                RoundedRectangle(cornerRadius: 15, style: .continuous)
                    .stroke(KriaColor.border, lineWidth: 1)
            }
            .clipShape(RoundedRectangle(cornerRadius: 15, style: .continuous))
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 7)
        }
        .background(Color.white)
    }
}

struct BundledPosterImage: View {
    let name: String

    var body: some View {
        if let image = UIImage(named: name, in: .main, compatibleWith: nil) {
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
