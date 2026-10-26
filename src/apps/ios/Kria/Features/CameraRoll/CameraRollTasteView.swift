import SwiftUI

/// The header every pushed camera-roll page shares: a back circle, the page name, and a spacer the size of the
/// circle so the name stays centred (Paper A4 and A7).
struct CameraRollPageHeader: View {
    let title: String
    var titleIdentifier = "camera-roll-page-title"
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        HStack(spacing: 8) {
            CameraRollCircleButton(identifier: "camera-roll-back", label: "Back", action: { dismiss() }) {
                Image(systemName: "chevron.left").font(.system(size: 16, weight: .semibold)).foregroundStyle(KriaColor.ink)
            }
            Text(title).font(KriaFont.body(15).weight(.semibold)).foregroundStyle(KriaColor.ink).lineLimit(1)
                .frame(maxWidth: .infinity).accessibilityAddTraits(.isHeader).accessibilityIdentifier(titleIdentifier)
            Color.clear.frame(width: 44, height: 44)
        }.padding(.horizontal, 16).padding(.top, 4).frame(minHeight: 48)
    }
}

/// "Your taste" (Paper A7): the one place the creator says what a good post is. Pushed from the camera roll's
/// header, and from an idea review that offers preferences. Replaces the old Idea preferences sheet.
struct CameraRollTasteView: View {
    let api: any KriaAPIClient
    let onChanged: @MainActor (EditorialTasteProfile) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var profile: EditorialTasteProfile?
    @State private var projects: [ProjectSummary] = []
    @State private var selected = Set<UUID>()
    @State private var rules = ""
    @State private var enabled = true
    @State private var busy = false
    @State private var error: String?
    @State private var confirmsForget = false

    private var choices: [(id: UUID, title: String)] {
        var seen = Set<UUID>()
        let existing = (profile?.examples ?? []).map { (id: $0.projectID, title: $0.title) }
        let recent = projects.compactMap { project -> (id: UUID, title: String)? in
            guard let raw = project.activePlanItemID, let id = UUID(uuidString: raw) else { return nil }
            return (id, project.title)
        }
        return (existing + recent).filter { seen.insert($0.id).inserted }.prefix(50).map { $0 }
    }

    var body: some View {
        VStack(spacing: 0) {
            CameraRollPageHeader(title: "Your taste")
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("What makes a good post for you?").cameraRollTitle()
                        Text("Ideas are ranked against this. Change it any time.")
                            .font(KriaFont.body(15).weight(.medium)).foregroundStyle(KriaColor.mutedInk)
                    }
                    if let profile {
                        HStack {
                            Text("Use my taste").font(KriaFont.body(16).weight(.semibold))
                            Spacer()
                            Toggle("Use my taste", isOn: $enabled).labelsHidden().tint(KriaColor.success)
                                .accessibilityIdentifier("taste-enabled")
                        }
                        .padding(.horizontal, 18).frame(height: 60).background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                        if profile.memoryPaused {
                            Text("Personalization is paused in your account settings.").font(KriaFont.body(13)).foregroundStyle(KriaColor.mutedInk)
                        }
                        postsYouLike
                        VStack(alignment: .leading, spacing: 8) {
                            Text("Your rules").cameraRollKicker()
                            TextField("Optional preferences, one per line", text: $rules, axis: .vertical)
                                .font(KriaFont.body(15).weight(.medium)).lineLimit(3...8)
                                .padding(.horizontal, 16).padding(.vertical, 14).frame(minHeight: 96, alignment: .topLeading)
                                .background(KriaColor.fill, in: RoundedRectangle(cornerRadius: 24, style: .continuous))
                                .accessibilityIdentifier("taste-rules")
                        }
                        Button { Task { await save(profile) } } label: {
                            Text("Use these examples").font(KriaFont.body(16).weight(.bold)).foregroundStyle(KriaColor.ink)
                                .frame(maxWidth: .infinity, minHeight: 52).background(KriaColor.butter, in: Capsule()).contentShape(Capsule())
                        }
                        .buttonStyle(.plain).disabled(busy || selected.isEmpty).opacity(busy || selected.isEmpty ? 0.45 : 1)
                        .accessibilityIdentifier("taste-save")
                        if profile.revision > 0 {
                            Button { confirmsForget = true } label: {
                                Text("Forget my taste").font(KriaFont.body(14).weight(.semibold)).foregroundStyle(KriaColor.failureText)
                                    .padding(.horizontal, 16).frame(height: 40).background(KriaColor.failureSoft, in: Capsule())
                                    .frame(minHeight: 44).contentShape(Capsule())
                            }.buttonStyle(.plain).accessibilityIdentifier("taste-forget")
                        }
                    } else if busy { ProgressView("Loading preferences…") }
                    if let error { Text(error).font(KriaFont.body(13)).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("taste-error") }
                }.padding(.horizontal, 20).padding(.top, 14).padding(.bottom, 28)
            }
            .scrollDismissesKeyboard(.interactively)
            .kriaScrollEdgeFade()
        }
        .background(KriaColor.paper).kriaPage().disabled(busy)
        .toolbar(.hidden, for: .navigationBar)
        .task { await load() }
        .alert("Forget editorial taste?", isPresented: $confirmsForget) {
            Button("Cancel", role: .cancel) { }
            Button("Forget", role: .destructive) { Task { await forget() } }
        } message: { Text("Removes your saved examples, rules and idea feedback. Your projects stay saved.") }
    }

    private var postsYouLike: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Posts you like").cameraRollKicker()
            if choices.isEmpty {
                Text("Make a video you love and it shows up here.").font(KriaFont.body(14)).foregroundStyle(KriaColor.mutedInk)
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 8) {
                        ForEach(choices, id: \.id) { choice in
                            let isSelected = selected.contains(choice.id)
                            Button {
                                if isSelected { selected.remove(choice.id) } else if selected.count < 50 { selected.insert(choice.id) }
                            } label: {
                                Text(choice.title).font(KriaFont.body(13).weight(.semibold)).foregroundStyle(KriaColor.ink)
                                    .multilineTextAlignment(.leading).lineLimit(5).padding(10)
                                    .frame(width: 84, height: 112, alignment: .topLeading)
                                    .background(isSelected ? KriaColor.butter : KriaColor.fill, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                    .overlay(alignment: .topTrailing) {
                                        ZStack {
                                            Circle().fill(isSelected ? KriaColor.ink : Color.white.opacity(0.7))
                                            if isSelected { KriaCheckShape().stroke(.white, style: StrokeStyle(lineWidth: 1.8, lineCap: .round, lineJoin: .round)).frame(width: 11, height: 11) }
                                        }.frame(width: 22, height: 22).padding(6)
                                    }
                            }
                            .buttonStyle(.plain).accessibilityIdentifier("taste-example-\(choice.id)")
                            .accessibilityAddTraits(isSelected ? .isSelected : [])
                        }
                    }
                }
            }
            Text("Kria uses the saved content as examples for future ideas.").font(KriaFont.body(12)).foregroundStyle(KriaColor.mutedInk)
        }
    }

    @MainActor private func apply(_ value: EditorialTasteProfile) {
        profile = value; selected = Set(value.examples.map(\.projectID))
        rules = value.rules.joined(separator: "\n"); enabled = value.enabled || value.revision == 0
    }
    @MainActor private func load() async {
        busy = true; error = nil; defer { busy = false }
        do {
            apply(try await api.editorialTaste())
            projects = try await api.projects()
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func save(_ current: EditorialTasteProfile) async {
        busy = true; error = nil; defer { busy = false }
        let entries = rules.split(separator: "\n").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        guard entries.count <= 12, entries.allSatisfy({ $0.count <= 500 }), entries.joined().count <= 4000 else {
            error = "Keep up to 12 short preferences, with 500 characters each."; return
        }
        let notes = Dictionary(uniqueKeysWithValues: current.examples.filter { selected.contains($0.projectID) }.compactMap { example -> (String, String)? in
            guard let note = example.contextualNote else { return nil }; return (example.projectID.uuidString, note)
        })
        do {
            let value = try await api.saveEditorialTaste(EditorialTasteWrite(expected_revision: current.revision, idempotency_key: UUID().uuidString, project_ids: selected.sorted { $0.uuidString < $1.uuidString }, rules: entries, project_notes: notes, enabled: enabled))
            onChanged(value); dismiss()
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func forget() async {
        guard let profile else { return }
        busy = true; error = nil; defer { busy = false }
        do {
            let value = try await api.forgetEditorialTaste(revision: profile.revision, eventID: UUID().uuidString)
            apply(value); onChanged(value)
        } catch { self.error = error.localizedDescription }
    }
}
