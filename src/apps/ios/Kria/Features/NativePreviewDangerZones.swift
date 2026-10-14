import SwiftUI

/// Product guidance, not platform guarantees: controls vary with device and post.
/// Fractions belong to the video canvas, never the surrounding editor viewport.
enum NativePreviewDangerZones {
    enum Edge: String, CaseIterable { case top, bottom, left, right }
    static let top: CGFloat = 0.10
    static let bottom: CGFloat = 0.25
    static let left: CGFloat = 0.05
    static let right: CGFloat = 0.15

    static func safeRect(in canvas: CGSize) -> CGRect {
        let origin = CGPoint(x: canvas.width * left, y: canvas.height * top)
        return CGRect(x: origin.x, y: origin.y,
                      width: canvas.width * (1 - right) - origin.x,
                      height: canvas.height * (1 - bottom) - origin.y)
    }

    static func overlaps(frame: CGRect, rotation: Double, canvas: CGSize) -> Set<Edge> {
        guard canvas.width > 0, canvas.height > 0 else { return [] }
        let angle = rotation * .pi / 180
        let cosine = CGFloat(abs(cos(angle)))
        let sine = CGFloat(abs(sin(angle)))
        let halfWidth = (cosine * frame.width + sine * frame.height) / 2
        let halfHeight = (sine * frame.width + cosine * frame.height) / 2
        let safe = safeRect(in: canvas)
        var edges = Set<Edge>()
        if frame.midX - halfWidth < safe.minX { edges.insert(.left) }
        if frame.midX + halfWidth > safe.maxX { edges.insert(.right) }
        if frame.midY - halfHeight < safe.minY { edges.insert(.top) }
        if frame.midY + halfHeight > safe.maxY { edges.insert(.bottom) }
        return edges
    }
}

/// Pin the media footprint before the first mutation. Asynchronous preview
/// rebuilds must not make the advisory lag a finger or apply a transform twice.
struct NativePreviewFootprint {
    var rect: CGRect
    var rotation: Double

    init(frame: CGRect, rotation: Double, canvas: CGSize) {
        rect = CGRect(x: frame.minX / max(1, canvas.width), y: frame.minY / max(1, canvas.height),
                      width: frame.width / max(1, canvas.width), height: frame.height / max(1, canvas.height))
        self.rotation = rotation
    }

    func frame(in canvas: CGSize) -> CGRect {
        CGRect(x: rect.minX * canvas.width, y: rect.minY * canvas.height,
               width: rect.width * canvas.width, height: rect.height * canvas.height)
    }

    func transformed(scale: CGFloat, rotation delta: Double, translation: CGPoint) -> Self {
        var result = self
        result.rect = CGRect(x: rect.midX + translation.x - rect.width * scale / 2,
                             y: rect.midY + translation.y - rect.height * scale / 2,
                             width: rect.width * scale, height: rect.height * scale)
        result.rotation += delta
        return result
    }
}

struct NativePreviewDangerGuide: View {
    let canvas: CGSize
    let edges: Set<NativePreviewDangerZones.Edge>

    private var message: String {
        edges.isEmpty ? "Reels & TikTok guide · approximate" : "May be covered on Reels or TikTok"
    }

    var body: some View {
        let safe = NativePreviewDangerZones.safeRect(in: canvas)
        ZStack(alignment: .topLeading) {
            band(.top, rect: CGRect(x: 0, y: 0, width: canvas.width, height: safe.minY))
            band(.bottom, rect: CGRect(x: 0, y: safe.maxY, width: canvas.width, height: canvas.height - safe.maxY))
            band(.left, rect: CGRect(x: 0, y: safe.minY, width: safe.minX, height: safe.height))
            band(.right, rect: CGRect(x: safe.maxX, y: safe.minY, width: canvas.width - safe.maxX, height: safe.height))
            Rectangle().strokeBorder(.white.opacity(0.65), style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
                .frame(width: safe.width, height: safe.height)
                .offset(x: safe.minX, y: safe.minY)
        }
        .frame(width: canvas.width, height: canvas.height, alignment: .topLeading)
        .overlay(alignment: .bottom) {
            Label(message, systemImage: edges.isEmpty ? "info.circle" : "exclamationmark.triangle.fill")
                .font(KriaFont.body(11).weight(.medium))
                .foregroundStyle(.white)
                .multilineTextAlignment(.center)
                .padding(6)
                .background(.black.opacity(0.78), in: RoundedRectangle(cornerRadius: 6))
                .padding(8)
        }
        .allowsHitTesting(false)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(message)
        .accessibilityIdentifier("native-editor-danger-guide")
    }

    private func band(_ edge: NativePreviewDangerZones.Edge, rect: CGRect) -> some View {
        Rectangle()
            .fill(edges.contains(edge) ? Color.orange.opacity(0.32) : Color.white.opacity(0.10))
            .frame(width: rect.width, height: rect.height)
            .offset(x: rect.minX, y: rect.minY)
    }
}
