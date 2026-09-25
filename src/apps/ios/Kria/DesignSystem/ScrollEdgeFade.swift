import SwiftUI

/// How much content is hidden past the top and bottom of a vertical scroll
/// view, as a 0...1 fade strength. Pure so the edge cases (content that fits,
/// float residue, insets) are unit-tested without a view.
///
/// Strength ramps with the hidden distance (`hidden / length`), so the fade
/// eases in as content starts to pass an edge instead of popping, and an edge
/// at rest stays crisp (strength 0).
struct ScrollEdgeFadeMetrics: Equatable {
    var top: CGFloat = 0
    var bottom: CGFloat = 0
    /// Hidden distance below this is float residue (KRI-128), not content.
    static let hiddenFloor: CGFloat = 0.5
    /// Strengths are rounded to this step so the geometry action fires on real
    /// changes, not on every scrolled frame.
    static let step: CGFloat = 0.05

    init(top: CGFloat = 0, bottom: CGFloat = 0) {
        self.top = top
        self.bottom = bottom
    }

    /// `visibleRect` is the whole scroll frame in content coordinates, INCLUDING
    /// the inset regions (`containerSize` excludes them, so it can't be used
    /// here). At rest its top edge is `-inset.top`, and the last position is
    /// where its bottom edge reaches `content + inset.bottom`.
    init(visibleRect: CGRect, contentSize: CGSize, contentInsets: EdgeInsets, length: CGFloat) {
        self.init(
            top: Self.strength(hidden: visibleRect.minY + contentInsets.top, length: length),
            bottom: Self.strength(
                hidden: contentSize.height + contentInsets.bottom - visibleRect.maxY,
                length: length
            )
        )
    }

    static func strength(hidden: CGFloat, length: CGFloat) -> CGFloat {
        guard length > 0, hidden >= hiddenFloor else { return 0 }
        let raw = min(1, hidden / length)
        return (raw / step).rounded() * step
    }
}

extension View {
    /// Fades a vertical scroll view's top and bottom edges by a hairline
    /// (`length`, default 2pt) where content continues past them, instead of
    /// slicing it with a hard cut. Apply directly on the `ScrollView`, before any
    /// `.overlay`/`.background` that must stay unmasked.
    ///
    /// The fade sits at the scroll view's real frame edges, which are the screen
    /// edges when the transcript runs beneath floating chrome, so content stays
    /// fully crisp (even behind that chrome) until it is `length` points from the
    /// edge. An edge at rest, with nothing past it, is not faded. The mask never
    /// changes frames or hit testing.
    func kriaScrollEdgeFade(length: CGFloat = 2) -> some View {
        modifier(KriaScrollEdgeFade(length: length))
    }
}

private struct KriaScrollEdgeFade: ViewModifier {
    /// Thickness of the fade at each frame edge.
    let length: CGFloat
    @State private var metrics = ScrollEdgeFadeMetrics()

    /// Hidden distance over which the fade eases on, so it doesn't pop the
    /// instant a point of content passes an edge.
    private static let rampLength: CGFloat = 24

    func body(content: Content) -> some View {
        content
            .onScrollGeometryChange(for: ScrollEdgeFadeMetrics.self) { geometry in
                ScrollEdgeFadeMetrics(
                    visibleRect: geometry.visibleRect,
                    contentSize: geometry.contentSize,
                    contentInsets: geometry.contentInsets,
                    length: Self.rampLength
                )
            } action: { _, newValue in
                metrics = newValue
            }
            // A mask is laid out inside the safe-area-inset region; the fade
            // belongs at the real frame edges (the screen edges).
            .mask { fadeMask.ignoresSafeArea() }
    }

    private var fadeMask: some View {
        VStack(spacing: 0) {
            edge(strength: metrics.top, atTop: true)
            Rectangle().fill(.black)
            edge(strength: metrics.bottom, atTop: false)
        }
    }

    /// Transparent at the frame edge, opaque `length` points in; `strength`
    /// (0...1) scales it so an edge at rest is untouched.
    private func edge(strength: CGFloat, atTop: Bool) -> some View {
        Rectangle()
            .fill(LinearGradient(
                colors: [.black.opacity(1 - strength), .black],
                startPoint: atTop ? .top : .bottom,
                endPoint: atTop ? .bottom : .top
            ))
            .frame(height: length)
    }
}
