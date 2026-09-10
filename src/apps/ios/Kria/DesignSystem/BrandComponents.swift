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
    enum Kind { case menu, pencil, more, close, plus, gallery, settings }
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
                    case .settings: break
                    }
                    context.stroke(path, with: .color(KriaColor.ink), style: StrokeStyle(lineWidth: 1.7, lineCap: .round, lineJoin: .round))
                }
            }
        }.frame(width: 22, height: 22).accessibilityHidden(true)
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

struct ProjectActionsMenu: View {
    let project: ProjectSummary
    @EnvironmentObject private var model: AppModel
    @State private var renaming = false
    @State private var deleting = false
    @State private var title = ""
    @State private var busy = false
    @State private var error: String?
    @State private var renameIdentity = UUID().uuidString
    @State private var submittedTitle: String?
    private var current: ProjectSummary { model.projects.first { $0.id == project.id } ?? project }
    private var deletionBlocked: Bool { current.status == .rendering || model.uploads.records.contains { $0.projectID == project.id } }

    var body: some View {
        Menu {
            Button("Rename project") { title = current.workspaceTitle; submittedTitle = nil; renameIdentity = UUID().uuidString; error = nil; renaming = true }
            Button(deletionBlocked ? "Delete after rendering or uploading" : "Delete project", role: .destructive) { error = nil; deleting = true }
                .disabled(deletionBlocked)
        } label: { KriaIcon(.more).frame(width: 44, height: 44) }
        .disabled(busy).accessibilityLabel("Project actions for \(current.workspaceTitle)")
        .sheet(isPresented: $renaming) {
            NavigationStack {
                VStack(alignment: .leading, spacing: 20) {
                    TextField("Project name", text: $title).font(KriaFont.body()).textFieldStyle(.roundedBorder)
                        .accessibilityIdentifier("rename-project-title")
                    if let error { Text(error).foregroundStyle(KriaColor.failureText).font(KriaFont.body(14)) }
                    Button(busy ? "Saving…" : "Save name") { rename() }
                        .buttonStyle(CanonicalPrimaryButtonStyle())
                        .disabled(busy || title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || title.count > 120)
                    Text("Up to 120 characters.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
                    Spacer()
                }.padding(24).navigationTitle("Rename project").navigationBarTitleDisplayMode(.inline)
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { renaming = false }.disabled(busy) } }
            }.kriaPage().presentationDetents([.medium, .large]).interactiveDismissDisabled(busy)
        }
        .alert("Delete this project?", isPresented: $deleting) {
            Button("Cancel", role: .cancel) {}
            Button("Delete project", role: .destructive) {
                busy = true
                Task {
                    do { try await model.deleteProject(current) }
                    catch { self.error = "The project couldn’t be deleted. It may have changed or still be rendering. Try again after refreshing." }
                    busy = false
                }
            }
        } message: { Text("This permanently deletes the project and its rendered videos and media. This cannot be undone.") }
        .alert("Couldn’t delete project", isPresented: Binding(get: { error != nil && !renaming }, set: { if !$0 { error = nil } })) {
            Button("OK", role: .cancel) { error = nil }
        } message: { Text(error ?? "") }
    }
    private func rename() {
        guard !busy else { return }
        let cleaned = title.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        if submittedTitle != cleaned { renameIdentity = UUID().uuidString; submittedTitle = cleaned }
        busy = true
        Task {
            do {
                try await model.renameProject(current, title: cleaned, clientEventID: renameIdentity)
                renaming = false; error = nil; submittedTitle = nil; renameIdentity = UUID().uuidString
            } catch {
                if error as? APIError == .conflict {
                    renameIdentity = UUID().uuidString
                    self.error = "This project changed elsewhere. The latest version is loaded; review your name and save again."
                } else { self.error = "The name couldn’t be saved. Your text is still here; try again." }
            }
            busy = false
        }
    }
}
