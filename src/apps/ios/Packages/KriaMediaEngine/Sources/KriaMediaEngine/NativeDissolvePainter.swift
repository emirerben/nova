#if canImport(AVFoundation)
import Foundation
import CoreImage

extension RecipeTextLayer {
    static func makeDissolve(_ layer: PortableTextLayer, assetURLs: [String: URL], canvas: CGSize, maxBitmapBytes: Int) throws -> Self {
        guard let seed = layer.dissolveSeed, canvas.width.isFinite, canvas.height.isFinite,
              (1...4096).contains(canvas.width), (1...4096).contains(canvas.height) else { throw RecipeError.invalidTimeline }
        let width = Int(canvas.width), height = Int(canvas.height)
        // Two noise maps, the retained text canvas and two intermediate canvases.
        let bytes = width * height * 20
        guard bytes <= maxBitmapBytes else { throw MediaEngineError.unsupportedCapability }
        let extent = CGRect(origin: .zero, size: canvas)
        let drawing = NativeDiscreteRevealPainter.paintingLayer(layer, runs: layer.runs)
        let settled = try make(drawing, assetURLs: assetURLs, canvas: canvas,
                               maxBitmapBytes: width * height * 4, fixedBounds: extent)
        let renderer = try NativeDissolveRenderer(width: width, height: height, seed: seed,
                                                 maxBitmapBytes: width * height * 8)
        return Self(image: settled.image, frame: extent, start: layer.start, end: layer.end,
                    animation: .none, portable: layer,
                    portableAnchor: CGPoint(x: layer.anchorX, y: canvas.height - layer.anchorY), dissolve: renderer)
    }
}
#endif
