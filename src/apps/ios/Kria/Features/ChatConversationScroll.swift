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

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView(showsIndicators: false) {
                LazyVStack(alignment: .leading, spacing: 20) {
                    content()
                    Color.clear.frame(height: 1).id("conversation-end")
                }
                .frame(maxWidth: 620, alignment: .leading)
                .padding(.horizontal, 16)
                .padding(.top, 20)
                .padding(.bottom, 28)
                .frame(maxWidth: .infinity)
            }
            .defaultScrollAnchor(.bottom, for: .initialOffset)
            .opacity(isLoaded ? 1 : 0)
            .task(id: isLoaded) {
                guard isLoaded else { return }
                scrollToEnd(proxy)
            }
            .scrollDismissesKeyboard(.interactively)
            .simultaneousGesture(TapGesture().onEnded(dismissKeyboard))
            .onScrollGeometryChange(for: Bool.self) { geometry in
                geometry.contentSize.height - geometry.visibleRect.maxY < 80
            } action: { _, value in
                nearBottom = value
                if userIsScrolling { followsLatest = value }
            }
            .onScrollPhaseChange { _, phase in
                userIsScrolling = phase == .tracking || phase == .interacting || phase == .decelerating
                if phase == .idle { followsLatest = nearBottom }
            }
            .onChange(of: updateToken) { _, _ in
                if followsLatest && !userIsScrolling { scrollToEnd(proxy) }
            }
            .onChange(of: scrollRequest) { _, _ in
                followsLatest = true
                scrollToEnd(proxy)
            }
            .overlay(alignment: .bottomTrailing) {
                if !followsLatest {
                    Button {
                        followsLatest = true
                        scrollToEnd(proxy)
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
    }

    private func scrollToEnd(_ proxy: ScrollViewProxy) {
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) { proxy.scrollTo("conversation-end", anchor: .bottom) }
    }
}
