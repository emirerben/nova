import SwiftUI

/// A frosted, softly shadowed surface for chrome that floats above scrolling
/// content (the chat header buttons and the composer), so the transcript can
/// scroll and fade out underneath it instead of hitting an opaque bar.
///
/// Material-only on purpose: the editor island's iOS 26 glass path corrupts the
/// accessibility frame of any ancestor that carries an identifier, and the
/// header buttons do. Under Reduce Transparency it is a solid paper surface.
private struct KriaFloatingSurface<S: Shape>: ViewModifier {
    let shape: S
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        content
            .background {
                Group {
                    if KriaTransparency.isReduced(reduceTransparency) {
                        shape.fill(KriaColor.paper)
                    } else {
                        shape.fill(.regularMaterial)
                    }
                }
                .shadow(color: KriaColor.ink.opacity(0.10), radius: 14, y: 5)
            }
            .overlay(shape.stroke(KriaColor.line.opacity(0.55), lineWidth: 0.5))
    }
}

extension View {
    func kriaFloatingSurface<S: Shape>(_ shape: S) -> some View {
        modifier(KriaFloatingSurface(shape: shape))
    }
}

/// The Chat / Editor switch shared by the chat header and the editor header, so
/// moving between them feels like switching a tab: the same floating capsule in
/// the same place, only the selected segment changes. The selected segment is
/// white (the pale selection blue vanishes on the frosted capsule).
struct WorkspaceModeSwitch: View {
    enum Mode { case chat, editor }

    let selected: Mode
    var onChat: () -> Void = {}
    var onEditor: () -> Void = {}
    var chatIdentifier: String?

    var body: some View {
        HStack(spacing: 4) {
            segment("Chat", isSelected: selected == .chat, action: onChat, identifier: chatIdentifier)
            segment("Editor", isSelected: selected == .editor, action: onEditor, identifier: nil)
        }
        .padding(4).kriaFloatingSurface(Capsule())
        .padding(.horizontal, 16)
        .padding(.top, 8) // breathing room below the title row, above the pill
        .padding(.bottom, 6)
    }

    @ViewBuilder
    private func segment(_ title: String, isSelected: Bool, action: @escaping () -> Void, identifier: String?) -> some View {
        // 36pt segments in a 44pt capsule; the hit area extends back out to 44pt.
        let label = Text(title)
            .font(KriaFont.body(13).weight(isSelected ? .semibold : .medium))
            .frame(maxWidth: .infinity, minHeight: 36)
        if isSelected {
            label
                .background(KriaColor.paper, in: Capsule())
                .shadow(color: KriaColor.ink.opacity(0.10), radius: 3, y: 1)
                .accessibilityAddTraits(.isSelected)
        } else {
            Button(action: action) { label.contentShape(Rectangle().inset(by: -4)) }
                .buttonStyle(.plain)
                .accessibilityIdentifier(identifier ?? "workspace-mode-\(title.lowercased())")
        }
    }
}

/// The floating top row shared by the chat header and the editor header: a
/// leading circle button, a centered title, and trailing controls. Same height
/// and insets in both, so the header doesn't move when switching modes.
/// Plain text, not a pill: only the leading/trailing icons and the Chat/Editor
/// switch below are actionable, so only those get a floating-capsule surface.
struct WorkspaceTopRow<Leading: View, Trailing: View>: View {
    let title: String
    var titleIdentifier: String?
    var titleHidden = false
    @ViewBuilder let leading: Leading
    @ViewBuilder let trailing: Trailing

    var body: some View {
        HStack(spacing: 8) {
            leading
            Text(title)
                .font(KriaFont.body(15).weight(.semibold))
                .lineLimit(1)
                .frame(maxWidth: .infinity)
                .accessibilityIdentifier(titleIdentifier ?? "workspace-title")
                .accessibilityHidden(titleHidden)
            trailing
        }
        .padding(.horizontal, 16).frame(minHeight: 44)
    }
}

/// Presents its content as a cross-dissolve over the screen beneath instead of a
/// page sliding up from the bottom, so moving between workspace modes (Chat ->
/// Editor) feels like switching a tab. Present it in a `fullScreenCover` with
/// animations disabled; it fades its content in, and `close` fades it out before
/// calling `dismiss` (which must also dismiss without animation).
struct WorkspaceCrossfade<Content: View>: View {
    let dismiss: () -> Void
    @ViewBuilder let content: (_ close: @escaping () -> Void) -> Content
    @State private var visible = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var animation: Animation? { reduceMotion ? nil : .easeInOut(duration: 0.22) }

    var body: some View {
        content(close)
            .opacity(visible ? 1 : 0)
            .presentationBackground(.clear)
            .onAppear { withAnimation(animation) { visible = true } }
    }

    private func close() {
        withAnimation(animation) {
            visible = false
        } completion: {
            dismiss()
        }
    }
}
