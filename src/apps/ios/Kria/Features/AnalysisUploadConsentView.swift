import SwiftUI

struct AnalysisUploadConsentView: View {
    let onConsent: () -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var consent = false
    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text("Render on this iPhone.").font(KriaFont.display(30))
                    Text("Kria uploads a smaller copy of each video, including its audio, so its AI can plan your edit. Full-quality originals stay on this iPhone. The finished video uploads to your Gallery after rendering.")
                        .foregroundStyle(KriaColor.mutedInk)
                    Text("These smaller copies, including faces, voices, and transcripts, may be shared with Google Gemini and OpenAI for analysis, transcription, and editing.")
                        .foregroundStyle(KriaColor.mutedInk)
                    Link("Privacy Policy", destination: KriaLegal.privacyURL).frame(minHeight: 44)
                    Toggle("I agree to share smaller copies with Kria, Google Gemini, and OpenAI for analysis", isOn: $consent)
                }.padding(24).frame(maxWidth: 560).frame(maxWidth: .infinity)
            }
            .safeAreaInset(edge: .bottom) {
                Button("Continue") { onConsent(); dismiss() }
                    .buttonStyle(CanonicalPrimaryButtonStyle()).disabled(!consent)
                    .padding(24).frame(maxWidth: 560).frame(maxWidth: .infinity).background(KriaColor.paper)
            }
            .navigationTitle("Analysis consent").navigationBarTitleDisplayMode(.inline)
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } } }
        }
    }
}

#Preview { AnalysisUploadConsentView(onConsent: {}) }
