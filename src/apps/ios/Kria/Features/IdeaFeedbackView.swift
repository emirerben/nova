import SwiftUI

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
    @State private var serverAcceptsWrongFacts = false
    @State private var serverAcceptsExamples = false
    @State private var offeredExample: UUID?
    private var reasons: [(String, String)] {
        if verdict == "would_post" { return [("good_observation", "Good observation"), ("useful_detail", "Useful detail"), ("good_mood", "Good atmosphere")] }
        let general = [("too_generic", "Too generic"), ("forced_question", "Forced question"), ("not_my_voice", "Not my voice"), ("invented_experience", "Invented experience"), ("footage_mismatch", "Doesn’t fit the footage"), ("missing_point", "Missing the point")]
        return story.research != nil && serverAcceptsWrongFacts ? general + [("wrong_facts", "The facts are wrong")] : general
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
                .task {
                    let capabilities = try? await api.creationCapabilities()
                    serverAcceptsWrongFacts = capabilities?.wrongFactFeedbackEnabled == true
                    serverAcceptsExamples = capabilities?.ideaExampleFeedbackEnabled == true
                }
                .alert("Use as a taste example?", isPresented: Binding(get: { offeredExample != nil }, set: { if !$0 { offeredExample = nil } }), presenting: offeredExample) { id in
                    Button("Use as example") { Task { await useExample(id) } }.accessibilityIdentifier("idea-example-confirm")
                    Button("Not now", role: .cancel) { onSaved(); dismiss() }.accessibilityIdentifier("idea-example-decline")
                } message: { _ in
                    Text(rewrite.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty ? "Kria can learn from this idea’s words when it writes future ideas for you. You can erase it any time with Forget editorial taste." : "Kria can learn from your version when it writes future ideas for you. You can erase it any time with Forget editorial taste.")
                }
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
            if serverAcceptsExamples && (verdict == "would_post" || !rewrite.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty) {
                offeredExample = receipt.id
            } else { onSaved(); dismiss() }
        } catch { self.error = error.localizedDescription }
    }
    @MainActor private func useExample(_ feedbackID: UUID) async {
        busy = true; error = nil; defer { busy = false }
        do {
            try await api.confirmIdeaExample(feedbackID: feedbackID, eventID: "example-\(feedbackID.uuidString)")
            onSaved(); dismiss()
        } catch { self.error = "Your answer is saved, but it couldn’t be used as an example. Save again to retry." }
    }
}
