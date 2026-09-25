import SwiftUI

/// How much content is hidden past each edge of a scroll view, as a 0...1
/// fade strength. Pure so the edge cases (content that fits, float residue,
/// insets) are unit-tested without a view.
///
/// Strength ramps with the hidden distance (`hidden / length`), so the fade
/// eases out as the reader nears an end instead of popping, and an edge at
/// rest stays crisp (strength 0).
struct ScrollEdgeFadeMetrics: Equatable {
    var top: CGFloat = 0
    var bottom: CGFloat = 0
    var leading: CGFloat = 0
    var trailing: CGFloat = 0
    /// The scroll view's content insets (e.g. a floating header/composer added as
    /// a `safeAreaInset`). The fade zone must cover the space under them, since
    /// the scroll view's frame runs beneath that chrome.
    var insetTop: CGFloat = 0
    var insetBottom: CGFloat = 0
    var insetLeading: CGFloat = 0
    var insetTrailing: CGFloat = 0

    /// Hidden distance below this is float residue (KRI-128), not content.
    static let hiddenFloor: CGFloat = 0.5
    /// Strengths are rounded to this step so the geometry action fires on real
    /// changes, not on every scrolled frame.
    static let step: CGFloat = 0.05

    init(top: CGFloat = 0, bottom: CGFloat = 0, leading: CGFloat = 0, trailing: CGFloat = 0) {
        self.top = top
        self.bottom = bottom
        self.leading = leading
        self.trailing = trailing
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
            ),
            leading: Self.strength(hidden: visibleRect.minX + contentInsets.leading, length: length),
            trailing: Self.strength(
                hidden: contentSize.width + contentInsets.trailing - visibleRect.maxX,
                length: length
            )
        )
        insetTop = max(0, contentInsets.top)
        insetBottom = max(0, contentInsets.bottom)
        insetLeading = max(0, contentInsets.leading)
        insetTrailing = max(0, contentInsets.trailing)
    }

    static func strength(hidden: CGFloat, length: CGFloat) -> CGFloat {
        guard length > 0, hidden >= hiddenFloor else { return 0 }
        let raw = min(1, hidden / length)
        return (raw / step).rounded() * step
    }
}

/// `fade`: content fades to transparent at the edge. `blur`: the same fade plus
/// a progressive frosted band (vertical edges only). Content stays faintly
/// visible (blurred) at the very edge instead of fading to a white curtain;
/// `wash` is the surface color the band tints slightly toward so the frost
/// never reads as a grey stripe against a light header/composer.
enum KriaEdgeFadeStyle {
    case fade
    case blur(wash: Color)
    /// `blur` whose wash follows `WorkspaceSurface` as the projects drawer opens.
    /// Resolved inside the modifier so the host view never observes the drawer
    /// progress (which changes every frame of a drawer drag).
    case blurWorkspaceSurface
}

extension View {
    /// Softens the edges of a scroll view where content continues past them,
    /// instead of slicing it with a hard cut. Apply directly on the
    /// `ScrollView`, before any `.overlay`/`.background` that must stay
    /// unmasked, and pass edges of ONE axis.
    ///
    /// The mask never changes frames or hit testing. The band is laid out inside
    /// the scroll view's own frame (safe-area layout already places it above a
    /// bottom composer); insets only feed the overflow math.
    func kriaScrollEdgeFade(_ edges: Edge.Set, length: CGFloat = 24, style: KriaEdgeFadeStyle = .fade) -> some View {
        modifier(KriaScrollEdgeFade(edges: edges, length: length, style: style))
    }
}

private struct KriaScrollEdgeFade: ViewModifier {
    let edges: Edge.Set
    let length: CGFloat
    let style: KriaEdgeFadeStyle
    @State private var metrics = ScrollEdgeFadeMetrics()
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.projectsDrawerProgress) private var drawerProgress

    /// With a blur band, content keeps this much opacity at the outer edge so
    /// the blur has something to blur (a full fade reads as a white bar).
    private static let blurContentFloor: CGFloat = 0.22

    private var contentFloor: CGFloat { wash != nil ? Self.blurContentFloor : 0 }

    /// Fade zones run from the frame edge through any content inset (a floating
    /// header/composer sits over that space) plus `length` of visible content.
    private var topZone: CGFloat { metrics.insetTop + length }
    private var bottomZone: CGFloat { metrics.insetBottom + length }

    /// One axis per scroll view: vertical wins if a caller passes both.
    private var vertical: Bool { edges.contains(.top) || edges.contains(.bottom) }

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
            .overlay { blurBands.ignoresSafeArea() }
    }

    // MARK: Mask

    @ViewBuilder
    private var fadeMask: some View {
        if vertical {
            VStack(spacing: 0) {
                gradient(startOpacity: 1 - (edges.contains(.top) ? metrics.top : 0) * (1 - contentFloor), endOpacity: 1, vertical: true, length: topZone)
                Rectangle().fill(.black)
                gradient(startOpacity: 1, endOpacity: 1 - (edges.contains(.bottom) ? metrics.bottom : 0) * (1 - contentFloor), vertical: true, length: bottomZone)
            }
        } else {
            HStack(spacing: 0) {
                gradient(startOpacity: 1 - (edges.contains(.leading) ? metrics.leading : 0), endOpacity: 1, vertical: false)
                Rectangle().fill(.black)
                gradient(startOpacity: 1, endOpacity: 1 - (edges.contains(.trailing) ? metrics.trailing : 0), vertical: false)
            }
        }
    }

    private func gradient(startOpacity: CGFloat, endOpacity: CGFloat, vertical: Bool, length: CGFloat? = nil) -> some View {
        let length = length ?? self.length
        let fill = LinearGradient(
            colors: [.black.opacity(startOpacity), .black.opacity(endOpacity)],
            startPoint: vertical ? .top : .leading,
            endPoint: vertical ? .bottom : .trailing
        )
        return Rectangle().fill(fill)
            .frame(width: vertical ? nil : length, height: vertical ? length : nil)
    }

    // MARK: Blur

    private var wash: Color? {
        switch style {
        case .fade: nil
        case .blur(let wash): wash
        case .blurWorkspaceSurface: KriaColor.workspaceSurface(progress: drawerProgress)
        }
    }

    @ViewBuilder
    private var blurBands: some View {
        if let wash,
           !KriaTransparency.isReduced(reduceTransparency),
           vertical {
            VStack(spacing: 0) {
                if edges.contains(.top) { band(wash: wash, atTop: true, zone: topZone).opacity(metrics.top) }
                Spacer(minLength: 0)
                if edges.contains(.bottom) { band(wash: wash, atTop: false, zone: bottomZone).opacity(metrics.bottom) }
            }
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
    }

    /// The band is taller than the content fade, so its inner part progressively
    /// blurs crisp content. The frost tapers off at the very edge and a light
    /// wash blends it into the header/composer, so there is neither a hard grey
    /// line nor a white bar.
    private func band(wash: Color, atTop: Bool, zone: CGFloat) -> some View {
        let toEdge: (start: UnitPoint, end: UnitPoint) = atTop ? (.bottom, .top) : (.top, .bottom)
        return ZStack {
            Rectangle()
                .fill(.ultraThinMaterial)
                .mask(LinearGradient(
                    stops: [
                        .init(color: .black.opacity(0), location: 0),
                        .init(color: .black.opacity(0.9), location: 0.5),
                        .init(color: .black.opacity(0.35), location: 1)
                    ],
                    startPoint: toEdge.start, endPoint: toEdge.end
                ))
            LinearGradient(
                stops: [
                    .init(color: wash.opacity(0), location: 0),
                    .init(color: wash.opacity(0.05), location: 0.5),
                    .init(color: wash.opacity(0.3), location: 1)
                ],
                startPoint: toEdge.start, endPoint: toEdge.end
            )
        }
        .frame(height: zone + length * 1.5)
    }
}
