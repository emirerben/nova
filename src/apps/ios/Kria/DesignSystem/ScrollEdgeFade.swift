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
    /// Height of the block floating over the top of the scroll view (status bar +
    /// header, plus the Chat/Editor row when shown), from the top content inset.
    /// The scroll view's frame runs beneath it.
    var insetTop: CGFloat = 0
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
        insetTop = max(0, contentInsets.top)
    }

    static func strength(hidden: CGFloat, length: CGFloat) -> CGFloat {
        guard length > 0, hidden >= hiddenFloor else { return 0 }
        let raw = min(1, hidden / length)
        return (raw / step).rounded() * step
    }
}

extension View {
    /// Blurs what scrolls past the edges of a vertical scroll view instead of
    /// slicing it with a hard cut. Apply directly on the `ScrollView`, before any
    /// `.overlay`/`.background` that must stay unmasked.
    ///
    /// - Top: everything above the bottom of the block floating over the top of
    ///   the scroll view (its top content inset: status bar, header, and the
    ///   Chat/Editor row when it is shown) is blurred, so text passing under that
    ///   block is frosted. With no top inset it is a thin `length` band.
    /// - Bottom: a thin `length` band at the screen edge.
    ///
    /// A blurred edge appears only once content has scrolled past it; at rest,
    /// with nothing past an edge, nothing is drawn. Under Reduce Transparency the
    /// blur becomes a plain fade. Frames and hit testing are never changed.
    func kriaScrollEdgeFade(length: CGFloat = 6) -> some View {
        modifier(KriaScrollEdgeFade(length: length))
    }
}

private struct KriaScrollEdgeFade: ViewModifier {
    /// Thickness of the band at an edge with no floating block (the bottom, or a
    /// top with no inset).
    let length: CGFloat
    @State private var metrics = ScrollEdgeFadeMetrics()
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    /// Hidden distance over which the effect eases on, so it doesn't pop the
    /// instant a point of content passes an edge.
    private static let rampLength: CGFloat = 24
    /// How much of a band's height eases out at its inner end, so a tall band
    /// doesn't end on a hard line.
    private static let easeLength: CGFloat = 10

    private var reduced: Bool { KriaTransparency.isReduced(reduceTransparency) }
    private var topHeight: CGFloat { max(metrics.insetTop, length) }

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
            // region; the bands belong at the real frame edges (the screen edges).
            .mask { fadeMask.ignoresSafeArea() }
            .overlay { blurBands.ignoresSafeArea() }
    }

    // MARK: Reduce Transparency: plain fade

    @ViewBuilder
    private var fadeMask: some View {
        if reduced {
            VStack(spacing: 0) {
                fadeEdge(strength: metrics.top, height: topHeight, atTop: true)
                Rectangle().fill(.black)
                fadeEdge(strength: metrics.bottom, height: length, atTop: false)
            }
        } else {
            Rectangle().fill(.black)
        }
    }

    /// Transparent at the frame edge, opaque at `height`; `strength` (0...1)
    /// scales it so an edge at rest is untouched.
    private func fadeEdge(strength: CGFloat, height: CGFloat, atTop: Bool) -> some View {
        Rectangle()
            .fill(LinearGradient(
                colors: [.black.opacity(1 - strength), .black],
                startPoint: atTop ? .top : .bottom,
                endPoint: atTop ? .bottom : .top
            ))
            .frame(height: height)
    }

    // MARK: Blur

    @ViewBuilder
    private var blurBands: some View {
        if !reduced {
            VStack(spacing: 0) {
                blurEdge(strength: metrics.top, height: topHeight, atTop: true)
                Spacer(minLength: 0)
                blurEdge(strength: metrics.bottom, height: length, atTop: false)
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
    }

    /// A frosted band, at full strength for most of its height and easing out
    /// only over its inner end so a thin band still reads as a blur and a tall one
    /// doesn't end on a hard line. `strength` (0...1) scales it so an edge at rest
    /// is untouched.
    private func blurEdge(strength: CGFloat, height: CGFloat, atTop: Bool) -> some View {
        let ease = min(Self.easeLength, height * 0.4)
        let solid = height > 0 ? 1 - ease / height : 1
        return Rectangle()
            .fill(.regularMaterial)
            .mask(LinearGradient(
                stops: [
                    .init(color: .black, location: 0),
                    .init(color: .black, location: solid),
                    .init(color: .clear, location: 1)
                ],
                startPoint: atTop ? .top : .bottom,
                endPoint: atTop ? .bottom : .top
            ))
            .frame(height: height)
            .opacity(strength)
    }
}
