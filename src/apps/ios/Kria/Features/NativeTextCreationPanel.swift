import SwiftUI

/// Keyboard editing lives below the canvas, rather than covering it with a
/// modal sheet. The draft belongs to the session so view updates cannot lose it.
///
/// In the connected panel the Text tab also lists every on-screen text block
/// (KRI-185), so a title or a per-clip label is one tap away instead of a hunt
/// along the timeline.
struct NativeTextCreationPanel: View {
    @ObservedObject var session: NativeEditorSession
    let onDone: (EditorSelection) -> Void
    /// Opens an existing block for editing. `nil` hides the list.
    var onSelectBlock: ((String) -> Void)?
    @State private var focused = false
    @Environment(\.nativeEditorConnectedPanel) private var connected
    /// One line of body text plus the editor's insets; it grows with Dynamic Type.
    @ScaledMetric(relativeTo: .body) private var compactInputHeight: CGFloat = 36

    private var interactive: Bool {
        EditorTextBlock.listIsInteractive(draft: session.pendingText?.text, canEdit: session.canEdit(.text))
    }

    private var blocks: [EditorTextBlock] { connected && onSelectBlock != nil ? session.document.textBlocks : [] }

    var body: some View {
        let blocks = self.blocks
        return VStack(spacing: 6) {
            HStack {
                Button { focused = false; session.cancelTextCreation() } label: {
                    Text("Cancel").frame(minWidth: 64, minHeight: 44)
                }
                    .accessibilityIdentifier("native-editor-text-cancel")
                Spacer()
                Text("Add text").font(KriaFont.body(connected ? 18 : 15).weight(.semibold))
                Spacer()
                Button {
                    focused = false
                    if let selection = session.finishTextCreation() { onDone(selection) }
                } label: {
                    Text("Done").frame(minWidth: 64, minHeight: 44)
                        .background(KriaColor.ink.opacity(0.06), in: Capsule())
                }
                .accessibilityIdentifier("native-editor-text-done")
            }
            NativeExplicitLineTextEditor(text: Binding(
                get: { session.pendingText?.text ?? "" },
                set: { session.updatePendingText($0) }
            ), focused: $focused, identifier: "native-editor-new-text-input")
                .frame(minHeight: blocks.isEmpty ? (connected ? 44 : 76) : compactInputHeight,
                       maxHeight: blocks.isEmpty ? .infinity : compactInputHeight)
                .padding(8)
                .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
            if !blocks.isEmpty { blockList(blocks) }
        }
        .padding(.horizontal, connected ? 24 : 16)
        .padding(.bottom, 8)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .background(connected ? Color.clear : KriaColor.paper)
        .overlay(alignment: .top) {
            if !connected { KriaColor.line.opacity(0.4).frame(height: 1) }
        }
        .font(KriaFont.body(14))
        .tint(KriaColor.ink)
        .task {
            if !connected { focused = true }
        }
    }

    private func blockList(_ blocks: [EditorTextBlock]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Text("On screen").font(KriaFont.body(12).weight(.semibold))
                if !interactive {
                    // The reason the rows are inert stays visible.
                    Text(session.canEdit(.text) ? "Finish your new text first" : "Text editing is unavailable")
                        .font(KriaFont.body(11))
                }
                Spacer(minLength: 0)
            }
            .foregroundStyle(KriaColor.mutedInk)
            .padding(.top, 6)
            .accessibilityAddTraits(.isHeader)
            ScrollView {
                // Not lazy: a few dozen rows at most, and every row stays reachable to
                // VoiceOver and UI tests even while it is scrolled out of view.
                VStack(spacing: 8) {
                    ForEach(blocks) { block in
                        NativeEditorTextBlockRow(
                            block: block,
                            selected: session.selection == EditorSelection(kind: .text, id: block.id),
                            enabled: interactive
                        ) {
                            focused = false
                            onSelectBlock?(block.id)
                        }
                    }
                }
                .padding(.bottom, 8)
            }
            .scrollDismissesKeyboard(.interactively)
            .accessibilityIdentifier("native-editor-text-list")
        }
        .frame(maxHeight: .infinity, alignment: .top)
    }
}

/// One tappable line in the Text tab's list.
struct NativeEditorTextBlockRow: View {
    let block: EditorTextBlock
    let selected: Bool
    let enabled: Bool
    let onTap: () -> Void

    var body: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                if block.text.isEmpty {
                    Text("Empty text").foregroundStyle(KriaColor.mutedInk)
                } else {
                    Text(block.text).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                }
                Text("\(block.kindLabel) · \(block.timeRange)")
                    .font(KriaFont.body(11)).monospacedDigit().foregroundStyle(KriaColor.mutedInk)
            }
            Image(systemName: "chevron.right").font(.system(size: 12)).foregroundStyle(KriaColor.mutedInk)
        }
        .padding(.horizontal, 12).padding(.vertical, 8).frame(minHeight: 52)
        .background(selected ? KriaColor.selectionSoft : KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 10))
        .opacity(enabled ? 1 : 0.5)
        .contentShape(Rectangle())
        .onTapGesture { onTap() }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? [.isButton, .isSelected] : .isButton)
        .accessibilityAction(named: "Edit text") { if enabled { onTap() } }
        .accessibilityIdentifier("native-editor-text-row-" + block.id)
        .disabled(!enabled)
    }
}
