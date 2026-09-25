import SwiftUI

/// Incoming replies follow the bottom only while the creator is reading there.
/// Text reveal changes opacity, not geometry, and never drives this scroll view.
struct ChatConversationScroll<Content: View>: View {
    let isLoaded: Bool
    let updateToken: String
    let scrollRequest: Int
    var dismissKeyboard: () -> Void = {}
    @ViewBuilder let content: () -> Content

    @State private var followsLatest = true
    @State private var userIsScrolling = false
    @State private var nearBottom = true
    /// The scroll view runs beneath the floating header and composer, so "the
    /// end" must respect its content insets. `scrollTo(edge: .bottom)` does;
    /// scrolling to an end marker anchored to the frame bottom left the last
    /// content under the composer.
    @State private var scrollPosition = ScrollPosition(edge: .bottom)

    var body: some View {
        ScrollView(showsIndicators: false) {
            LazyVStack(alignment: .leading, spacing: 20) {
                content()
            }
            .frame(maxWidth: 620, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.top, 20)
            .padding(.bottom, 28)
            .frame(maxWidth: .infinity)
        }
        .scrollPosition($scrollPosition)
        .defaultScrollAnchor(.bottom, for: .initialOffset)
        .opacity(isLoaded ? 1 : 0)
        .task(id: isLoaded) {
            guard isLoaded else { return }
            scrollToEnd()
        }
        .scrollDismissesKeyboard(.interactively)
        .simultaneousGesture(TapGesture().onEnded(dismissKeyboard))
        .onScrollGeometryChange(for: Bool.self) { geometry in
            // The visible rect includes the inset regions under the header and
            // composer, so add the bottom inset back to measure real distance.
            geometry.contentSize.height + geometry.contentInsets.bottom - geometry.visibleRect.maxY < 80
        } action: { _, value in
            nearBottom = value
            if userIsScrolling { followsLatest = value }
        }
        .onScrollPhaseChange { _, phase in
            userIsScrolling = phase == .tracking || phase == .interacting || phase == .decelerating
            if phase == .idle { followsLatest = nearBottom }
        }
        .onChange(of: updateToken) { _, _ in
            if followsLatest && !userIsScrolling { scrollToEnd() }
        }
        .onChange(of: scrollRequest) { _, _ in
            followsLatest = true
            scrollToEnd()
        }
        .kriaScrollEdgeFade(.vertical, style: .blurWorkspaceSurface)
        .overlay(alignment: .bottomTrailing) {
            if !followsLatest {
                Button {
                    followsLatest = true
                    scrollToEnd()
                } label: {
                    Label("Latest", systemImage: "arrow.down")
                        .font(KriaFont.body(12).weight(.semibold))
                        .padding(.horizontal, 14).padding(.vertical, 10)
                        .background(KriaColor.paper, in: Capsule())
                        .overlay(Capsule().stroke(KriaColor.border, lineWidth: 1))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Jump to latest message")
                .accessibilityIdentifier("chat-jump-to-latest")
                .padding(16)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Conversation history")
    }

    private func scrollToEnd() {
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) { scrollPosition.scrollTo(edge: .bottom) }
    }
}
