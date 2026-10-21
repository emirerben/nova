import SwiftUI

struct EditorialTasteView: View {
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
        NavigationStack {
            Form {
                if let profile {
                    Section {
                        Toggle("Use my editorial taste", isOn: $enabled)
                        if profile.memoryPaused { Text("Personalization is paused in your account settings.").foregroundStyle(KriaColor.mutedInk) }
                    }
                    Section {
                        ForEach(choices, id: \.id) { choice in
                            Button {
                                if selected.contains(choice.id) { selected.remove(choice.id) }
                                else if selected.count < 50 { selected.insert(choice.id) }
                            } label: {
                                HStack {
                                    Text(choice.title).foregroundStyle(KriaColor.ink).multilineTextAlignment(.leading)
                                    Spacer()
                                    Image(systemName: selected.contains(choice.id) ? "checkmark.circle.fill" : "circle")
                                        .foregroundStyle(selected.contains(choice.id) ? KriaColor.ink : KriaColor.mutedInk)
                                }.frame(minHeight: 44)
                            }.accessibilityIdentifier("taste-example-\(choice.id)")
                                .accessibilityAddTraits(selected.contains(choice.id) ? .isSelected : [])
                        }
                    } header: {
                        Text("Choose posts you like")
                    } footer: {
                        Text("Kria uses the saved content as examples for future ideas.")
                    }
                    Section("What makes a good post for you?") {
                        TextField("Optional preferences, one per line", text: $rules, axis: .vertical)
                            .lineLimit(3...8).accessibilityIdentifier("taste-rules")
                    }
                    Section {
                        Button("Use these examples") { Task { await save(profile) } }
                            .disabled(busy || selected.isEmpty).accessibilityIdentifier("taste-save")
                    }
                    if profile.revision > 0 {
                        Section {
                            Button("Forget editorial taste", role: .destructive) { confirmsForget = true }
                                .accessibilityIdentifier("taste-forget")
                        }
                    }
                } else if busy { ProgressView("Loading preferences…") }
                if let error { Text(error).foregroundStyle(KriaColor.failureText).accessibilityIdentifier("taste-error") }
            }.font(KriaFont.body(15)).scrollContentBackground(.hidden)
                .background(KriaColor.paper).kriaPage()
                .navigationTitle("Idea preferences").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() }.disabled(busy) } }
                .disabled(busy)
                .task { await load() }
                .alert("Forget editorial taste?", isPresented: $confirmsForget) {
                    Button("Cancel", role: .cancel) { }
                    Button("Forget", role: .destructive) { Task { await forget() } }
                } message: { Text("Removes your saved examples, rules and idea feedback. Your projects stay saved.") }
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

struct IdeaFeedbackView: View {
    let api: any KriaAPIClient
    let collectionID: UUID
    let story: CameraRollStory
    let onSaved: @MainActor () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var verdict = ""
    @State private var reason = ""
    @State private var note = ""
    @State private var rewrite = ""
    @State private var busy = false
    @State private var error: String?
    @State private var eventID = UUID().uuidString
    private var reasons: [(String, String)] {
        verdict == "would_post" ? [("good_observation", "Good observation"), ("useful_detail", "Useful detail"), ("good_mood", "Good atmosphere")] : [("too_generic", "Too generic"), ("forced_question", "Forced question"), ("not_my_voice", "Not my voice"), ("invented_experience", "Invented experience"), ("footage_mismatch", "Doesn’t fit the footage"), ("missing_point", "Missing the point")]
    }
    var body: some View {
        NavigationStack {
            Form {
                Section {
                    ForEach([("would_post", "Would post"), ("needs_work", "Needs work"), ("not_for_me", "Not for me")], id: \.0) { value, label in
                        Button { verdict = value; reason = ""; eventID = UUID().uuidString } label: {
                            HStack { Text(label); Spacer(); if verdict == value { Image(systemName: "checkmark") } }.frame(minHeight: 44)
                        }.foregroundStyle(KriaColor.ink)
                    }
                }
                if !verdict.isEmpty {
                    Section("Why? · optional") {
                        Picker("Reason", selection: $reason) {
                            Text("Choose a reason").tag("")
                            ForEach(reasons, id: \.0) { value, label in Text(label).tag(value) }
                        }
                        TextField("What would you change?", text: $note, axis: .vertical).lineLimit(2...4)
                        TextField("Your version of the idea", text: $rewrite, axis: .vertical).lineLimit(2...4)
                    }
                }
                if let error { Text(error).foregroundStyle(KriaColor.failureText) }
            }.font(KriaFont.body(15)).scrollContentBackground(.hidden).background(KriaColor.paper).kriaPage()
                .navigationTitle("Would you post this?").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(busy) }
                    ToolbarItem(placement: .confirmationAction) { Button("Save") { Task { await save() } }.disabled(verdict.isEmpty || busy || note.count > 1000 || rewrite.count > 1000).accessibilityIdentifier("idea-feedback-save") }
                }.disabled(busy).interactiveDismissDisabled(busy)
                .onChange(of: note) { _, _ in eventID = UUID().uuidString }
                .onChange(of: rewrite) { _, _ in eventID = UUID().uuidString }
                .onChange(of: reason) { _, _ in eventID = UUID().uuidString }
        }
    }
    @MainActor private func save() async {
        busy = true; error = nil; defer { busy = false }
        do {
            let receipt = try await api.recordIdeaFeedback(IdeaFeedbackWrite(collection_id: collectionID, story_id: story.id, expected_story_revision: story.revision, client_event_id: eventID, verdict: verdict, reasons: reason.isEmpty ? [] : [reason], note: note, rewrite: rewrite))
            guard receipt.recorded else { throw APIError.invalidResponse }
            onSaved(); dismiss()
        } catch { self.error = error.localizedDescription }
    }
}
