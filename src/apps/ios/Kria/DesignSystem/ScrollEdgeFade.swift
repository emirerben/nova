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
    /// The scroll view's content insets (a floating header/composer added as a
    /// `safeAreaInset`). The scroll view's frame runs beneath that chrome, so the
    /// fade zone has to cover the space under it.
    var insetTop: CGFloat = 0
    var insetBottom: CGFloat = 0

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
        insetBottom = max(0, contentInsets.bottom)
    }

    static func strength(hidden: CGFloat, length: CGFloat) -> CGFloat {
        guard length > 0, hidden >= hiddenFloor else { return 0 }
        let raw = min(1, hidden / length)
        return (raw / step).rounded() * step
    }
}

extension View {
    /// Softly fades a vertical scroll view's top and bottom edges where content
    /// continues past them, instead of slicing it with a hard cut. Apply directly
    /// on the `ScrollView`, before any `.overlay`/`.background` that must stay
    /// unmasked.
    ///
    /// The fade is confined to the edges: near-transparent under any floating
    /// chrome (the content inset) and back to fully crisp `length` points beyond
    /// it. An edge at rest, with nothing past it, is not faded at all. The mask
    /// never changes frames or hit testing.
    func kriaScrollEdgeFade(length: CGFloat = 20) -> some View {
        modifier(KriaScrollEdgeFade(length: length))
    }
}

private struct KriaScrollEdgeFade: ViewModifier {
    let length: CGFloat
    @State private var metrics = ScrollEdgeFadeMetrics()

    /// Opacity of content at the inset boundary (just under the floating
    /// chrome), at full strength. It then ramps to fully opaque over `length`.
    private static let opacityAtInset: CGFloat = 0.25

    func body(content: Content) -> some View {
        content
            .onScrollGeometryChange(for: ScrollEdgeFadeMetrics.self) { geometry in
                ScrollEdgeFadeMetrics(
                    visibleRect: geometry.visibleRect,
                    contentSize: geometry.contentSize,
                    contentInsets: geometry.contentInsets,
                    length: length
                )
            } action: { _, newValue in
                metrics = newValue
            }
            // Laid out against the real frame edges: the scroll view runs beneath
            // any floating header/composer, so the fade zones must too.
            .mask { fadeMask.ignoresSafeArea() }
    }

    private var fadeMask: some View {
        VStack(spacing: 0) {
            zone(strength: metrics.top, inset: metrics.insetTop, atTop: true)
            Rectangle().fill(.black)
            zone(strength: metrics.bottom, inset: metrics.insetBottom, atTop: false)
        }
    }

    /// One edge's gradient: transparent at the frame edge, `opacityAtInset` at
    /// the inset boundary, opaque `length` points beyond it. `strength` (0...1)
    /// scales how much of that fade applies, so an edge at rest is untouched.
    private func zone(strength: CGFloat, inset: CGFloat, atTop: Bool) -> some View {
        let height = inset + length
        let insetLocation = height > 0 ? inset / height : 0
        func opacity(_ base: CGFloat) -> Color { .black.opacity(1 - strength * (1 - base)) }
        // Stops run edge -> content; flip for the bottom zone.
        let stops: [Gradient.Stop] = [
            .init(color: opacity(0), location: 0),
            .init(color: opacity(Self.opacityAtInset), location: insetLocation),
            .init(color: opacity(1), location: 1)
        ]
        return Rectangle()
            .fill(LinearGradient(
                stops: stops,
                startPoint: atTop ? .top : .bottom,
                endPoint: atTop ? .bottom : .top
            ))
            .frame(height: height)
    }
}
