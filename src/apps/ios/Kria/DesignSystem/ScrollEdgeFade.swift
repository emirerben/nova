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
    /// Blurs a vertical scroll view's top and bottom edges by a thin band
    /// (`length`, default 6pt) where content continues past them, instead of
    /// slicing it with a hard cut. Apply directly on the `ScrollView`, before any
    /// `.overlay`/`.background` that must stay unmasked.
    ///
    /// The band sits at the scroll view's real frame edges, which are the screen
    /// edges when the transcript runs beneath floating chrome, so content stays
    /// fully crisp (even behind that chrome) until it is `length` points from the
    /// edge. An edge at rest, with nothing past it, is not touched. Under Reduce
    /// Transparency the blur becomes a plain fade. Frames and hit testing are
    /// never changed.
    func kriaScrollEdgeFade(length: CGFloat = 6) -> some View {
        modifier(KriaScrollEdgeFade(length: length))
    }
}

private struct KriaScrollEdgeFade: ViewModifier {
    /// Thickness of the band at each frame edge.
    let length: CGFloat
    @State private var metrics = ScrollEdgeFadeMetrics()
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    /// Hidden distance over which the effect eases on, so it doesn't pop the
    /// instant a point of content passes an edge.
    private static let rampLength: CGFloat = 24

    private var reduced: Bool { KriaTransparency.isReduced(reduceTransparency) }

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
            // Both a mask and an overlay are laid out inside the safe-area-inset
            // region; the band belongs at the real frame edges (the screen edges).
            .mask { fadeMask.ignoresSafeArea() }
            .overlay { blurBands.ignoresSafeArea() }
    }

    // MARK: Reduce Transparency: plain fade

    @ViewBuilder
    private var fadeMask: some View {
        if reduced {
            VStack(spacing: 0) {
                fadeEdge(strength: metrics.top, atTop: true)
                Rectangle().fill(.black)
                fadeEdge(strength: metrics.bottom, atTop: false)
            }
        } else {
            Rectangle().fill(.black)
        }
    }

    /// Transparent at the frame edge, opaque `length` points in; `strength`
    /// (0...1) scales it so an edge at rest is untouched.
    private func fadeEdge(strength: CGFloat, atTop: Bool) -> some View {
        Rectangle()
            .fill(LinearGradient(
                colors: [.black.opacity(1 - strength), .black],
                startPoint: atTop ? .top : .bottom,
                endPoint: atTop ? .bottom : .top
            ))
            .frame(height: length)
    }

    // MARK: Blur

    @ViewBuilder
    private var blurBands: some View {
        if !reduced {
            VStack(spacing: 0) {
                blurEdge(strength: metrics.top, atTop: true)
                Spacer(minLength: 0)
                blurEdge(strength: metrics.bottom, atTop: false)
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
    }

    /// A frosted band that is strongest at the frame edge and eases out inward.
    /// `strength` (0...1) scales it so an edge at rest is untouched.
    private func blurEdge(strength: CGFloat, atTop: Bool) -> some View {
        Rectangle()
            .fill(.ultraThinMaterial)
            .mask(LinearGradient(
                colors: [.black, .clear],
                startPoint: atTop ? .top : .bottom,
                endPoint: atTop ? .bottom : .top
            ))
            .frame(height: length)
            .opacity(strength)
    }
}
