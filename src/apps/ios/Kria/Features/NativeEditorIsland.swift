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

/// Applies the island's glass/material surface to a capsule-shaped view.
/// Three branches, checked in order:
/// 1. Reduce Transparency: solid paper + a hairline stroke, no blur.
/// 2. iOS 26+ with a Swift 6.2+ toolchain: Liquid Glass (`.glassEffect`).
/// 3. Everything else: `.ultraThinMaterial` with a manual stroke + shadow.
struct NativeEditorIslandSurface: ViewModifier {
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
        if effectiveReduceTransparency {
            content
                .background(KriaColor.paper, in: Capsule())
                .overlay(Capsule().strokeBorder(KriaColor.line, lineWidth: 1))
                .shadow(color: KriaColor.ink.opacity(0.14), radius: 12, y: 8)
                .shadow(color: KriaColor.ink.opacity(0.08), radius: 1.5, y: 1)
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
                .glassEffect(.regular.interactive(), in: Capsule())
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
            .background(.ultraThinMaterial, in: Capsule())
            .overlay(Capsule().strokeBorder(Color.white.opacity(0.75), lineWidth: 1))
            .overlay(Capsule().strokeBorder(KriaColor.line.opacity(0.6), lineWidth: 0.5))
            .shadow(color: KriaColor.ink.opacity(0.14), radius: 12, y: 8)
            .shadow(color: KriaColor.ink.opacity(0.08), radius: 1.5, y: 1)
    }
}

extension View {
    func nativeEditorIslandSurface() -> some View {
        modifier(NativeEditorIslandSurface())
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
