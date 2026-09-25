import SwiftUI

/// KRI-131: the editor's tool rail and clip/text context strip float as a
/// glass "island" over the timeline instead of sitting in an opaque row
/// beneath it. The timeline's lanes and playhead keep running underneath,
/// down to the screen's physical bottom edge, and a bottom scrim keeps their
/// content legible where it passes behind the island.
///
/// `NativeEditorIslandMetrics` is a pure, nonisolated namespace so its layout
/// math can be unit tested without spinning up SwiftUI.
enum NativeEditorIslandMetrics {
    static let islandHeight: CGFloat = 62
    static let islandVerticalPadding: CGFloat = 4
    static let islandHorizontalPadding: CGFloat = 8
    static let toolSpacing: CGFloat = 2
    static let toolWidth: CGFloat = 76
    static let toolHeight: CGFloat = 54
    static let contextHeight: CGFloat = 52
    static let stackSpacing: CGFloat = 8
    static let bottomPadding: CGFloat = 6
    static let scrimExtra: CGFloat = 38

    /// Extra breathing room above the island's own frame so the last lane of
    /// a scrolled timeline doesn't feel like it's touching the capsule.
    private static let breathingGap: CGFloat = 8

    /// How much bottom clearance the timeline's scrollable content needs so
    /// its last lane can always scroll clear of the floating island.
    static func bottomClearance(showsContext: Bool, safeAreaBottom: CGFloat) -> CGFloat {
        bottomPadding + safeAreaBottom + islandHeight
            + (showsContext ? stackSpacing + contextHeight : 0)
            + breathingGap
    }

    /// The bottom scrim's height: tall enough to cover the clearance plus a
    /// little extra so the fade reads as intentional rather than clipped.
    static func scrimHeight(showsContext: Bool, safeAreaBottom: CGFloat) -> CGFloat {
        bottomClearance(showsContext: showsContext, safeAreaBottom: safeAreaBottom) + scrimExtra
    }
}

/// KRI-170: preview and panel sizing for the connected editor, extracted from
/// `NativeEditorView` so it can be unit tested. The two are independent: the
/// timeline handle resizes only the preview (`previewResize`, in points,
/// positive = shrink, negative = grow) and the panel handle resizes only the
/// panel (`panelExpansion`, 0…1), which may rise over the transport and preview.
struct NativeEditorLayoutMetrics: Equatable {
    /// Project header: 44pt title row + 44pt tab row + 6pt bottom padding.
    static let headerHeight: CGFloat = 94
    static let previewVerticalPadding: CGFloat = 10
    static let resizeHandleHeight: CGFloat = 44
    static let transportHeight: CGFloat = 54
    static let minPreviewHeight: CGFloat = 80
    static let defaultPanelCap: CGFloat = 284
    /// A strip of timeline that must stay visible however far the preview grows.
    static let minTimelineStrip: CGFloat = 96
    static let maxPreviewScreenFraction: CGFloat = 0.6
    static let fullscreenScreenFraction: CGFloat = 0.9

    var viewportSize: CGSize
    var safeAreaTop: CGFloat
    var safeAreaBottom: CGFloat
    var topChromeHeight: CGFloat
    var previewAspectRatio: CGFloat
    var keyboardVisible: Bool
    var isAccessibilitySize: Bool
    /// A text panel that is being typed into may rise over the preview even with the
    /// keyboard up, so the box has the room it needs (KRI-185). Other panels keep the
    /// keyboard-up ceiling, so this is `false` unless the caller opts in.
    var raisesPanelWhileTyping = false

    private var referenceHeight: CGFloat {
        keyboardVisible ? viewportSize.height : viewportSize.height + safeAreaTop + safeAreaBottom
    }

    /// The size the preview has always started at (unchanged by KRI-170).
    var defaultPreviewHeight: CGFloat {
        let portrait = isAccessibilitySize ? 150 : min(284, max(150, referenceHeight * 0.34))
        // Banners and the posting-song bar share this fixed-height column;
        // their measured height comes out of the preview so the timeline and
        // tool rail stay on screen.
        let budget = max(Self.minPreviewHeight, portrait - topChromeHeight)
        let preferred = previewAspectRatio > 1 ? min(124, budget) : budget
        // With the keyboard up, reserve room for the header, divider and usable
        // text controls rather than letting their minimum heights overflow.
        return keyboardVisible
            ? min(preferred, max(Self.minPreviewHeight, viewportSize.height - 320 - topChromeHeight))
            : preferred
    }

    /// Largest preview the user can drag out to. Never below the default, and
    /// never while the keyboard is up.
    var maxPreviewHeight: CGFloat {
        let base = defaultPreviewHeight
        guard !keyboardVisible else { return base }
        let minBelow = NativeEditorIslandMetrics.bottomClearance(showsContext: false, safeAreaBottom: 0)
            + Self.minTimelineStrip
        let spaceBound = viewportSize.height - Self.headerHeight - topChromeHeight
            - Self.previewVerticalPadding - Self.resizeHandleHeight - minBelow
        let widthBound = (viewportSize.width - 32) / max(0.01, previewAspectRatio)
        return max(base, min(referenceHeight * Self.maxPreviewScreenFraction, widthBound, spaceBound))
    }

    /// Points the preview can shrink below its default.
    var shrinkRange: CGFloat { max(0, defaultPreviewHeight - Self.minPreviewHeight) }
    /// Points the preview can grow beyond its default.
    var growRange: CGFloat { max(0, maxPreviewHeight - defaultPreviewHeight) }

    /// `previewResize` is stored raw and clamped here, so a banner appearing or
    /// the keyboard rising can never leave the preview out of range.
    func previewHeight(resize: CGFloat) -> CGFloat {
        min(maxPreviewHeight, max(Self.minPreviewHeight, defaultPreviewHeight - resize))
    }

    /// Room for the panel below the transport and above the island's bottom padding.
    func panelBudget(areaHeight: CGFloat) -> CGFloat {
        max(0, areaHeight - NativeEditorIslandMetrics.bottomPadding
            - (keyboardVisible ? 0 : Self.transportHeight))
    }

    func panelDefaultHeight(areaHeight: CGFloat) -> CGFloat {
        let budget = panelBudget(areaHeight: areaHeight)
        return keyboardVisible || isAccessibilitySize ? budget : min(budget, Self.defaultPanelCap)
    }

    /// The panel may rise over the transport and the preview, up to the
    /// header/top chrome. The transport stays where it is and is simply covered.
    func panelMaxHeight(areaHeight: CGFloat, previewHeight: CGFloat) -> CGFloat {
        let budget = panelBudget(areaHeight: areaHeight)
        guard !isAccessibilitySize, !keyboardVisible || raisesPanelWhileTyping else { return budget }
        // The transport is hidden while the keyboard is up, so there is nothing to cover.
        return budget + (keyboardVisible ? 0 : Self.transportHeight) + previewHeight
            + Self.previewVerticalPadding + Self.resizeHandleHeight
    }

    func panelHeight(areaHeight: CGFloat, previewHeight: CGFloat, expansion: CGFloat) -> CGFloat {
        let floor = panelDefaultHeight(areaHeight: areaHeight)
        let ceiling = panelMaxHeight(areaHeight: areaHeight, previewHeight: previewHeight)
        return floor + min(1, max(0, expansion)) * max(0, ceiling - floor)
    }

    /// Points the panel handle moves through between default and max.
    func panelRange(areaHeight: CGFloat, previewHeight: CGFloat) -> CGFloat {
        max(0, panelMaxHeight(areaHeight: areaHeight, previewHeight: previewHeight)
            - panelDefaultHeight(areaHeight: areaHeight))
    }

    /// Aspect-fit box for the fullscreen preview inside 90% of the screen.
    static func fullscreenSize(screen: CGSize, aspect: CGFloat) -> CGSize {
        let box = CGSize(width: screen.width * fullscreenScreenFraction,
                         height: screen.height * fullscreenScreenFraction)
        let a = max(0.01, aspect)
        let width = min(box.width, box.height * a)
        return CGSize(width: width, height: width / a)
    }
}

/// Applies the island's glass/material surface to a capsule-shaped view.
/// Three branches, checked in order:
/// 1. Reduce Transparency: solid paper + a hairline stroke, no blur.
/// 2. iOS 26+ with a Swift 6.2+ toolchain: Liquid Glass (`.glassEffect`).
/// 3. Everything else: `.ultraThinMaterial` with a manual stroke + shadow.
struct NativeEditorIslandSurface: ViewModifier {
    var cornerRadius: CGFloat = 999
    var isEnabled = true
    private var shape: RoundedRectangle { RoundedRectangle(cornerRadius: cornerRadius, style: .continuous) }
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    // Neither `xcrun simctl ui` nor writing the `com.apple.Accessibility`
    // defaults domain actually flips `UIAccessibility.isReduceTransparencyEnabled`
    // (and therefore this SwiftUI environment value) for a simulator app
    // process — this mirrors the existing `UI_TEST_REDUCE_MOTION` override
    // pattern (`NativeEditorView.shouldReduceMotion`) so fixtures and
    // screenshots can exercise this branch deterministically.
    private var effectiveReduceTransparency: Bool {
        reduceTransparency || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_TRANSPARENCY"] == "1"
    }

    func body(content: Content) -> some View {
        if !isEnabled {
            content
        } else if effectiveReduceTransparency {
            content
                .background {
                    shape.fill(KriaColor.paper)
                        .shadow(color: KriaColor.ink.opacity(0.14), radius: 12, y: 8)
                        .shadow(color: KriaColor.ink.opacity(0.08), radius: 1.5, y: 1)
                }
                .overlay(shape.strokeBorder(KriaColor.line, lineWidth: 1))
        } else {
            glass(content)
        }
    }

    @ViewBuilder
    private func glass(_ content: Content) -> some View {
        #if compiler(>=6.2)
        if #available(iOS 26.0, *) {
            // NOTE: `.glassEffect()` anywhere in this view's subtree — even
            // hidden in a decorative `.background` layer — corrupts the
            // accessibility/hit-test frame UIKit reports for any ANCESTOR
            // that also carries `.accessibilityElement(children: .contain)`
            // or a bare `.accessibilityIdentifier`: the reported frame
            // silently collapses to the union of only the "real" (non-glass)
            // accessible children, dropping this view's own padding.
            // Confirmed on iOS 26.2/26.3 by isolating every other modifier
            // here one at a time (animation, matchedGeometryEffect,
            // buttonStyle, fixedSize, explicit width) — only removing
            // `.glassEffect()` fixed it, and moving it to a `.background`
            // did NOT help. The actual fix lives at each call site: any
            // identifier that needs a correct frame must live on a plain,
            // non-glass marker leaf (see `NativeEditorToolRail`), not
            // directly on (or as an ancestor of) a `.glassEffect()` view.
            content
                .glassEffect(.regular.interactive(), in: shape)
                // Liquid Glass alone reads as almost invisible against the
                // white timeline/paper background; a soft shadow restores
                // enough definition to read as a floating surface.
                .shadow(color: KriaColor.ink.opacity(0.10), radius: 12, y: 6)
        } else {
            material(content)
        }
        #else
        material(content)
        #endif
    }

    private func material(_ content: Content) -> some View {
        content
            .background {
                shape.fill(.ultraThinMaterial)
                    .shadow(color: KriaColor.ink.opacity(0.14), radius: 12, y: 8)
                    .shadow(color: KriaColor.ink.opacity(0.08), radius: 1.5, y: 1)
            }
            .overlay(shape.strokeBorder(Color.white.opacity(0.75), lineWidth: 1))
            .overlay(shape.strokeBorder(KriaColor.line.opacity(0.6), lineWidth: 0.5))
    }
}

extension View {
    func nativeEditorIslandSurface(cornerRadius: CGFloat = 999, isEnabled: Bool = true) -> some View {
        modifier(NativeEditorIslandSurface(cornerRadius: cornerRadius, isEnabled: isEnabled))
    }
}

/// Wraps its content in a `GlassEffectContainer` on iOS 26+ so multiple
/// glass capsules (the context strip and the tool rail) blend together
/// instead of rendering as two independent panes of glass. A no-op on older
/// systems or reduced-transparency, where the surfaces are opaque/material
/// and don't need to share a container to look coherent.
struct NativeEditorIslandGroup<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        #if compiler(>=6.2)
        if #available(iOS 26.0, *) {
            GlassEffectContainer(spacing: NativeEditorIslandMetrics.stackSpacing) {
                content
            }
        } else {
            content
        }
        #else
        content
        #endif
    }
}

/// The bottom-pinned scrim the timeline's lanes and playhead pass behind.
/// Never hit-testable: touches beside or above the floating island must
/// reach the timeline underneath.
struct NativeEditorIslandScrim: View {
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    let showsContext: Bool
    let safeAreaBottom: CGFloat

    // See `NativeEditorIslandSurface.effectiveReduceTransparency`.
    private var effectiveReduceTransparency: Bool {
        reduceTransparency || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_TRANSPARENCY"] == "1"
    }

    private var height: CGFloat {
        NativeEditorIslandMetrics.scrimHeight(showsContext: showsContext, safeAreaBottom: safeAreaBottom)
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            if !effectiveReduceTransparency {
                // Progressive blur confined to roughly the lowest 64pt, so
                // the lanes stay crisp until they near the island.
                let blurStart = max(0, min(1, (height - 64) / height))
                Rectangle()
                    .fill(.ultraThinMaterial)
                    .mask(
                        LinearGradient(
                            stops: [
                                .init(color: .clear, location: 0),
                                .init(color: .clear, location: blurStart),
                                .init(color: .black, location: 1)
                            ],
                            startPoint: .top, endPoint: .bottom
                        )
                    )
            }
            LinearGradient(
                stops: effectiveReduceTransparency
                    ? [
                        .init(color: KriaColor.paper.opacity(0), location: 0),
                        .init(color: KriaColor.paper.opacity(0.92), location: 0.7),
                        .init(color: KriaColor.paper.opacity(1), location: 1)
                    ]
                    : [
                        .init(color: KriaColor.paper.opacity(0), location: 0),
                        .init(color: KriaColor.paper.opacity(0.28), location: 0.4),
                        .init(color: KriaColor.paper.opacity(0.62), location: 1)
                    ],
                startPoint: .top, endPoint: .bottom
            )
        }
        .frame(maxWidth: .infinity)
        .frame(height: height)
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}
