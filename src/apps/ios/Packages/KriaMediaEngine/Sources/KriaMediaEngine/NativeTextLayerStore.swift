#if canImport(AVFoundation)
import Foundation
import CoreImage
import CoreGraphics

/// Immutable timeline geometry with a bounded cache for the current frame's
/// text. A long story must not retain every caption bitmap for its whole life.
final class NativeTextLayerStore: @unchecked Sendable {
    let layers: [PortableTextLayer]
    private let assetURLs: [String: URL]
    private let canvas: CGSize
    private let maxBitmapBytes: Int
    private let selectionGeometry: [String: ResolvedTextSelectionBounds]
    private let layersByID: [String: PortableTextLayer]
    private let lock = NSLock()
    private var cached: [String: RecipeTextLayer] = [:]

    init(layers: [PortableTextLayer], assetURLs: [String: URL], canvas: CGSize,
         maxBitmapBytes: Int = 64 * 1024 * 1024, reusing previous: NativeTextLayerStore? = nil) throws {
        guard maxBitmapBytes >= 0 else { throw MediaEngineError.unsupportedCapability }
        self.layers = layers; self.assetURLs = assetURLs; self.canvas = canvas; self.maxBitmapBytes = maxBitmapBytes
        for id in Set(layers.flatMap { $0.runs.map(\.fontAssetID) }) {
            guard let url = assetURLs[id], let provider = CGDataProvider(url: url as CFURL), CGFont(provider) != nil else {
                throw MediaEngineError.missingAsset(id)
            }
        }
        var geometry: [String: ResolvedTextSelectionBounds] = [:]
        for layer in layers {
            if let previous, previous.canvas == canvas, previous.layersByID[layer.id] == layer,
               layer.runs.allSatisfy({ previous.assetURLs[$0.fontAssetID] == assetURLs[$0.fontAssetID] }) {
                geometry[layer.id] = previous.selectionGeometry[layer.id]
            } else if !layer.runs.isEmpty {
                geometry[layer.id] = try PortableTextVectorPainter(layer: layer, assetURLs: assetURLs, canvas: canvas).selectionBounds
            }
        }
        selectionGeometry = geometry
        layersByID = Dictionary(uniqueKeysWithValues: layers.map { ($0.id, $0) })
        if let previous, previous.canvas == canvas {
            previous.lock.lock()
            defer { previous.lock.unlock() }
            for layer in layers {
                if let value = previous.cached[layer.id], value.portable == layer,
                   layer.runs.allSatisfy({ previous.assetURLs[$0.fontAssetID] == assetURLs[$0.fontAssetID] }) {
                    cached[layer.id] = value
                }
            }
            if cached.values.reduce(0, { $0 + $1.bitmapBytes }) > maxBitmapBytes { cached = [:] }
        }
    }

    /// UI hit testing reads immutable vector geometry; it never enters the
    /// compositor's bitmap lock or rasterizes the next caption on the main thread.
    func selectionBounds(id: String, at time: Double) -> TextSelectionBounds? {
        guard let layer = layersByID[id], time >= layer.start, time < layer.end,
              let geometry = selectionGeometry[id] else { return nil }
        return try? geometry.sample(layer: layer, time: time)
    }

    var residentBitmapBytes: Int {
        lock.lock(); defer { lock.unlock() }
        return cached.values.reduce(0) { $0 + $1.bitmapBytes }
    }

    func activeLayers(at time: Double) throws -> [RecipeTextLayer] {
        lock.lock(); defer { lock.unlock() }
        let active = layers.filter { time >= $0.start && time < $0.end }
        let ids = Set(active.map(\.id))
        cached = cached.filter { ids.contains($0.key) }
        var used = cached.values.reduce(0) { $0 + $1.bitmapBytes }
        for layer in active where cached[layer.id] == nil {
            let value: RecipeTextLayer
            do {
                value = try RecipeTextLayer.make(layer, assetURLs: assetURLs, canvas: canvas,
                                                maxBitmapBytes: maxBitmapBytes - used)
            } catch {
                throw NativePreviewFeatureError("active-text:count=\(active.count):effect=\(layer.effect.rawValue):used=\(used):budget=\(maxBitmapBytes):" + ((error as? NativePreviewFeatureError)?.feature ?? "render-error"))
            }
            guard value.bitmapBytes <= maxBitmapBytes - used else { throw MediaEngineError.unsupportedCapability }
            cached[layer.id] = value
            used += value.bitmapBytes
        }
        return active.compactMap { cached[$0.id] }
    }
}

extension RecipeTextLayer {
    static func deferred(_ layer: PortableTextLayer) -> Self {
        Self(image: CIImage(color: .clear).cropped(to: CGRect(x: 0, y: 0, width: 1, height: 1)),
             frame: .zero, start: layer.start, end: layer.end, animation: .none, portable: layer)
    }
}
#endif
