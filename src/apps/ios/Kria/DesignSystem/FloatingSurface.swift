import SwiftUI

/// A frosted, softly shadowed surface for chrome that floats above scrolling
/// content (the chat header buttons and the composer), so the transcript can
/// scroll and fade out underneath it instead of hitting an opaque bar.
///
/// Material-only on purpose: the editor island's iOS 26 glass path corrupts the
/// accessibility frame of any ancestor that carries an identifier, and the
/// header buttons do. Under Reduce Transparency it is a solid paper surface.
private struct KriaFloatingSurface<S: Shape>: ViewModifier {
    let shape: S
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency

    func body(content: Content) -> some View {
        content
            .background {
                Group {
                    if KriaTransparency.isReduced(reduceTransparency) {
                        shape.fill(KriaColor.paper)
                    } else {
                        shape.fill(.regularMaterial)
                    }
                }
                .shadow(color: KriaColor.ink.opacity(0.10), radius: 14, y: 5)
            }
            .overlay(shape.stroke(KriaColor.line.opacity(0.55), lineWidth: 0.5))
    }
}

extension View {
    func kriaFloatingSurface<S: Shape>(_ shape: S) -> some View {
        modifier(KriaFloatingSurface(shape: shape))
    }
}
