import SwiftUI

/// Keyboard editing lives below the canvas, rather than covering it with a
/// modal sheet. The draft belongs to the session so view updates cannot lose it.
struct NativeTextCreationPanel: View {
    @ObservedObject var session: NativeEditorSession
    let onDone: (EditorSelection) -> Void
    @State private var focused = false

    var body: some View {
        VStack(spacing: 12) {
            HStack {
                Button("Cancel") { session.cancelTextCreation() }
                    .frame(minWidth: 44, minHeight: 44)
                    .accessibilityIdentifier("native-editor-text-cancel")
                Spacer()
                Text("Add text").font(KriaFont.body(15).weight(.semibold))
                Spacer()
                Button("Done") {
                    focused = false
                    if let selection = session.finishTextCreation() { onDone(selection) }
                }
                .frame(minWidth: 44, minHeight: 44)
                .accessibilityIdentifier("native-editor-text-done")
            }
            NativeExplicitLineTextEditor(text: Binding(
                get: { session.pendingText?.text ?? "" },
                set: { session.updatePendingText($0) }
            ), focused: $focused, identifier: "native-editor-new-text-input")
                .frame(height: 76)
                .padding(8)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 12)
        .background(KriaColor.paper)
        .task { focused = true }
    }
}
