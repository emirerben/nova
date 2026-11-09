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
    /// How many more content-height changes may scroll to the end before the transcript itself changes again.
    @State private var heightFollows = HeightFollowBudget()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

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
            heightFollows.refill()
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
            if userIsScrolling {
                followsLatest = value
            } else if value {
                // Content shrank (a card was replaced by a shorter stage) and the scroll view
                // clamped to its end, or a programmatic scroll landed there: the reader is at the
                // latest message, so follow again and drop the stale "Latest" pill.
                followsLatest = true
            }
        }
        .onScrollPhaseChange { _, phase in
            userIsScrolling = phase == .tracking || phase == .interacting || phase == .decelerating
            if phase == .idle {
                followsLatest = nearBottom
                heightFollows.refill()
            }
        }
        .onScrollGeometryChange(for: CGFloat.self) { $0.contentSize.height } action: { old, new in
            // KRI-1239: in a lazy stack, scrolling to the end measures other rows, which changes the height again.
            // On a long transcript (12-receipt replies) that loop did not settle: after "Make it without these" it
            // rebuilt every row 90+ times in a few seconds, or forever, and iOS killed the app (0x8BADF00D). New
            // content refills the budget; re-measuring alone cannot keep scrolling.
            guard old != new, followsLatest, !userIsScrolling, heightFollows.take() else { return }
            scrollToEnd()
        }
        .onScrollGeometryChange(for: CGFloat.self) { $0.contentInsets.bottom } action: { old, new in
            // Keyboard / composer growth changes the bottom inset; keep the end in view for a
            // reader who is following, so the last message never hides behind them.
            if old != new, followsLatest, !userIsScrolling { scrollToEnd() }
        }
        .onChange(of: updateToken) { _, _ in
            heightFollows.refill()
            if followsLatest && !userIsScrolling { scrollToEnd() }
        }
        .onChange(of: scrollRequest) { _, _ in
            heightFollows.refill()
            followsLatest = true
            scrollToEnd()
        }
        .kriaScrollEdgeFade()
        .overlay(alignment: .bottomTrailing) {
            ZStack {
                if !followsLatest {
                    Button {
                        followsLatest = true
                        scrollToEnd(animated: true)
                    } label: {
                        Label("Latest", systemImage: "arrow.down")
                            .font(KriaFont.body(12).weight(.semibold))
                            .padding(.horizontal, 16)
                            .frame(minHeight: 44)
                            .foregroundStyle(KriaColor.ink)
                            .background(KriaColor.fill, in: Capsule())
                            .shadow(color: KriaColor.ink.opacity(0.12), radius: 8, y: 3)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Jump to latest message")
                    .accessibilityIdentifier("chat-jump-to-latest")
                    .padding(16)
                    .transition(reduceMotion ? .opacity : .scale(scale: 0.85, anchor: .bottomTrailing).combined(with: .opacity))
                }
            }
            .animation(reduceMotion ? .easeOut(duration: 0.15) : .snappy(duration: 0.22), value: followsLatest)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Conversation history")
    }

    /// Streaming auto-follow stays instant (it must not fight the reader); only the
    /// explicit "Latest" tap glides.
    private func scrollToEnd(animated: Bool = false) {
        Perf.event("chat.scrollToEnd")
        if animated && !reduceMotion {
            withAnimation(.smooth(duration: 0.35)) { scrollPosition.scrollTo(edge: .bottom) }
            return
        }
        var transaction = Transaction(animation: nil)
        transaction.disablesAnimations = true
        withTransaction(transaction) { scrollPosition.scrollTo(edge: .bottom) }
    }
}

/// A count kept outside SwiftUI's state on purpose: spending it inside a geometry callback must not redraw the
/// transcript (that redraw is what fed the loop it bounds).
final class HeightFollowBudget {
    /// Opening a long chat measured up to 6 rows into place in the KRI-1239 replay; a little headroom on top.
    static let limit = 10
    private var remaining = HeightFollowBudget.limit

    func take() -> Bool {
        guard remaining > 0 else { return false }
        remaining -= 1
        return true
    }

    func refill() { remaining = Self.limit }
}
