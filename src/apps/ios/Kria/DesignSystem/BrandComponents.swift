import SwiftUI

/// Exact Paper artwork, cropped to its visible bounds so headers center the mark.
struct KriaWordmark: View {
    var body: some View {
        Image("KriaWordmark")
            .resizable()
            .renderingMode(.original)
            .scaledToFit()
            .frame(width: 76, height: 36)
            .accessibilityLabel("Kria")
    }
}

/// Shared 24-unit outline geometry from Mobile Flow.
struct KriaIcon: View {
    enum Kind { case menu, pencil, more, close, plus, gallery, settings, move, resize }
    let kind: Kind
    init(_ kind: Kind) { self.kind = kind }
    var body: some View {
        Group {
            if kind == .settings {
                Image(systemName: "gearshape").font(.system(size: 21, weight: .regular))
            } else {
                Canvas { context, size in
                    context.scaleBy(x: size.width / 24, y: size.height / 24)
                    var path = Path()
                    func line(_ x: CGFloat, _ y: CGFloat, _ x2: CGFloat, _ y2: CGFloat) {
                        path.move(to: CGPoint(x: x, y: y)); path.addLine(to: CGPoint(x: x2, y: y2))
                    }
                    switch kind {
                    case .menu: line(4, 7, 20, 7); line(4, 15, 15, 15)
                    case .plus: line(12, 5, 12, 19); line(5, 12, 19, 12)
                    case .close: line(6, 6, 18, 18); line(18, 6, 6, 18)
                    case .more:
                        for x in [4.0, 11.0, 18.0] { path.addEllipse(in: CGRect(x: x, y: 11, width: 2, height: 2)) }
                    case .pencil:
                        path.move(to: CGPoint(x: 4, y: 20)); [CGPoint(x: 9, y: 19), CGPoint(x: 21, y: 7), CGPoint(x: 16, y: 2), CGPoint(x: 4, y: 14)].forEach { path.addLine(to: $0) }; path.closeSubpath(); line(15, 4, 20, 9)
                    case .gallery:
                        path.addRoundedRect(in: CGRect(x: 3, y: 3, width: 18, height: 18), cornerSize: CGSize(width: 2, height: 2))
                        path.addEllipse(in: CGRect(x: 7, y: 7, width: 3, height: 3))
                        path.move(to: CGPoint(x: 3, y: 17)); [CGPoint(x: 10, y: 11), CGPoint(x: 14, y: 15), CGPoint(x: 18, y: 11), CGPoint(x: 21, y: 14)].forEach { path.addLine(to: $0) }
                    case .move:
                        line(4, 12, 20, 12); line(12, 4, 12, 20)
                        line(4, 12, 7, 9); line(4, 12, 7, 15); line(20, 12, 17, 9); line(20, 12, 17, 15)
                        line(12, 4, 9, 7); line(12, 4, 15, 7); line(12, 20, 9, 17); line(12, 20, 15, 17)
                    case .resize:
                        line(5, 5, 19, 19); line(12, 19, 19, 19); line(19, 19, 19, 12)
                    case .settings: break
                    }
                    context.stroke(path, with: .color(KriaColor.ink), style: StrokeStyle(lineWidth: 1.7, lineCap: .round, lineJoin: .round))
                }
            }
        }.frame(width: 22, height: 22).accessibilityHidden(true)
    }
}

/// Floating chrome for controls above live media. The material stays on a background leaf so
/// the button's accessibility frame remains accurate.
struct KriaFloatingIconButton: View {
    let kind: KriaIcon.Kind
    let label: String
    let identifier: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            KriaIcon(kind).frame(width: 44, height: 44)
        }
        .buttonStyle(.plain)
        .kriaFloatingSurface(Circle())
        .accessibilityLabel(label)
        .accessibilityIdentifier(identifier)
    }
}

struct NewChatButton: View {
    var compact = false
    var icon: KriaIcon.Kind = .pencil
    var afterCreate: () -> Void = {}
    @EnvironmentObject private var model: AppModel
    @State private var busy = false
    @State private var error: String?
    var body: some View {
        Button {
            guard !busy else { return }
            busy = true
            let previousID = model.selectedProject?.id
            Task {
                await model.createProject()
                busy = false
                if model.selectedProject?.id != previousID { afterCreate() }
                else { error = model.errorMessage ?? "Your chat couldn’t be created. Try again." }
            }
        } label: {
            HStack(spacing: 10) {
                if busy { ProgressView() } else { KriaIcon(icon) }
                if !compact { Text("New chat").font(KriaFont.body(15).weight(.semibold)) }
            }
            .frame(minWidth: 44, minHeight: 44).padding(.horizontal, compact ? 0 : 16)
            .background(compact ? Color.clear : KriaColor.butter, in: Capsule())
        }
        .disabled(busy || model.isCreatingProject || model.isLoading).accessibilityLabel("New chat")
        .accessibilityIdentifier(compact ? "header-new-chat" : "drawer-new-chat")
        .alert("Couldn’t create chat", isPresented: Binding(get: { error != nil }, set: { if !$0 { error = nil } })) {
            Button("OK", role: .cancel) { error = nil }
        } message: { Text(error ?? "") }
    }
}

/// Carries a rename/delete request from a `ProjectActionsMenu` to `ProjectActionAlerts`.
///
/// The alerts used to hang off each menu. A drawer row sits in a `LazyVStack` that is re-laid-out when
/// the alert's keyboard appears, which tore the row (and its open alert) down. Slide posts only reach
/// rename through those rows, so the alert closed as soon as it opened.
@MainActor @Observable
final class ProjectActionsPresenter {
    enum Request: Equatable { case rename(UUID), delete(UUID) }
    var request: Request?
    /// The project with a rename or delete in flight; its menu is disabled.
    var busyProjectID: UUID?
}

struct ProjectActionsMenu: View {
    let project: ProjectSummary
    /// The chat header floats over the transcript; drawer rows keep the plain label.
    var floating = false
    @EnvironmentObject private var model: AppModel
    /// Nil in previews that render a row without a workspace host; the menu then does nothing.
    @Environment(ProjectActionsPresenter.self) private var presenter: ProjectActionsPresenter?
    private var current: ProjectSummary { model.projects.first { $0.id == project.id } ?? project }
    private var deletionBlocked: Bool { current.status == .rendering || model.uploads.records.contains { $0.projectID == project.id } }

    var body: some View {
        Menu {
            Button("Rename project") { presenter?.request = .rename(project.id) }
            Button(deletionBlocked ? "Delete after rendering or uploading" : "Delete project", role: .destructive) { presenter?.request = .delete(project.id) }
                .disabled(deletionBlocked)
        } label: {
            if floating {
                KriaIcon(.more).frame(width: 44, height: 44).kriaFloatingSurface(Circle())
            } else {
                KriaIcon(.more).frame(width: 44, height: 44)
            }
        }
        .disabled(presenter?.busyProjectID == project.id).accessibilityLabel("Project actions for \(current.workspaceTitle)")
    }
}

/// Presents the rename and delete alerts for `ProjectActionsMenu`. Attach it once, high in the tree
/// (`ChatWorkspaceView`), where no scrolling list or keyboard layout can dismantle it.
struct ProjectActionAlerts: ViewModifier {
    let presenter: ProjectActionsPresenter
    @EnvironmentObject private var model: AppModel
    @State private var target: ProjectSummary?
    @State private var renaming = false
    @State private var deleting = false
    @State private var title = ""
    @State private var busy = false
    // Rename and delete each present a native alert. Keeping their errors
    // separate prevents a rename retry's automatic alert dismissal from
    // momentarily activating the delete-error presenter.
    @State private var renameError: String?
    @State private var deleteError: String?
    @State private var renameIdentity = UUID().uuidString
    @State private var submittedTitle: String?
    private var current: ProjectSummary? {
        guard let target else { return nil }
        return model.projects.first { $0.id == target.id } ?? target
    }

    func body(content: Content) -> some View {
        content
            .onChange(of: presenter.request) { _, request in
                guard let request else { return }
                presenter.request = nil
                switch request {
                case .rename(let id):
                    guard let project = model.projects.first(where: { $0.id == id }) else { return }
                    target = project
                    title = project.workspaceTitle; submittedTitle = nil; renameIdentity = UUID().uuidString; renameError = nil; renaming = true
                case .delete(let id):
                    guard let project = model.projects.first(where: { $0.id == id }) else { return }
                    target = project
                    deleteError = nil; deleting = true
                }
            }
            .alert("Rename project", isPresented: $renaming) {
                TextField("Project name", text: $title)
                    .accessibilityIdentifier("rename-project-title")
                Button("Cancel", role: .cancel) { renameError = nil }
                    .disabled(busy)
                Button(busy ? "Saving…" : "OK") { rename() }
                    .disabled(busy || title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || title.count > 120)
            } message: {
                Text(renameError ?? "Enter a name up to 120 characters.")
            }
            .alert("Delete this project?", isPresented: $deleting) {
                Button("Cancel", role: .cancel) {}
                Button("Delete project", role: .destructive) {
                    guard let project = current else { return }
                    setBusy(true)
                    Task {
                        do { try await model.deleteProject(project) }
                        catch { self.deleteError = "The project couldn’t be deleted. It may have changed or still be rendering. Try again after refreshing." }
                        setBusy(false)
                    }
                }
            } message: { Text("This permanently deletes the project and its rendered videos and media. This cannot be undone.") }
            .alert("Couldn’t delete project", isPresented: Binding(get: { deleteError != nil }, set: { if !$0 { deleteError = nil } })) {
                Button("OK", role: .cancel) { deleteError = nil }
            } message: { Text(deleteError ?? "") }
    }

    private func setBusy(_ value: Bool) {
        busy = value
        presenter.busyProjectID = value ? target?.id : nil
    }

    private func rename() {
        guard !busy, let project = current else { return }
        let cleaned = title.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        if submittedTitle != cleaned { renameIdentity = UUID().uuidString; submittedTitle = cleaned }
        renameError = nil
        setBusy(true)
        Task {
            do {
                try await model.renameProject(project, title: cleaned, clientEventID: renameIdentity)
                renaming = false; renameError = nil; submittedTitle = nil; renameIdentity = UUID().uuidString
            } catch {
                if error as? APIError == .conflict {
                    renameIdentity = UUID().uuidString
                    self.renameError = "This project changed elsewhere. The latest version is loaded; review your name and save again."
                } else { self.renameError = "The name couldn’t be saved. Your text is still here; try again." }
                renaming = true
            }
            setBusy(false)
        }
    }
}

extension View {
    func projectActionAlerts(_ presenter: ProjectActionsPresenter) -> some View {
        modifier(ProjectActionAlerts(presenter: presenter))
    }
}
