import CoreGraphics
import Foundation
import ImageIO
import UIKit

/// KRI-460: a picture the creator pasted onto a slide (Strava's transparent PNG, say). Mirrors the server's
/// `SlideSticker`; `tests/fixtures/slide-sticker-parity/v1.json` pins the wire and the geometry.
///
/// `asset_id` names an IMAGE in the post's asset pool. The sticker is placed by its centre (`x_frac`, `y_frac`, fractions
/// of the slide canvas), sized by its displayed width (`width_frac`, a fraction of the canvas width; the height follows
/// the picture's own aspect so it is never distorted) and turned clockwise about its centre (`rotation_deg`).
struct SlidePostSticker: Codable, Equatable, Identifiable, Sendable {
    static let widthFracRange = 0.05...1.0
    static let rotationRange = -180.0...180.0
    static let idMaxLength = 64
    /// Where a freshly pasted picture lands: the middle of the slide, 40% of its width.
    static let defaultWidthFrac = 0.4

    var id: String
    var assetID: String
    var xFrac: Double
    var yFrac: Double
    var widthFrac: Double
    var rotationDeg: Double

    init(id: String = UUID().uuidString, assetID: String, xFrac: Double = 0.5, yFrac: Double = 0.5,
         widthFrac: Double = SlidePostSticker.defaultWidthFrac, rotationDeg: Double = 0) {
        self.id = id; self.assetID = assetID; self.xFrac = xFrac; self.yFrac = yFrac
        self.widthFrac = widthFrac; self.rotationDeg = rotationDeg
    }

    enum CodingKeys: String, CodingKey {
        case id
        case assetID = "asset_id", xFrac = "x_frac", yFrac = "y_frac", widthFrac = "width_frac", rotationDeg = "rotation_deg"
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        assetID = try c.decode(String.self, forKey: .assetID)
        xFrac = try c.decode(Double.self, forKey: .xFrac)
        yFrac = try c.decode(Double.self, forKey: .yFrac)
        widthFrac = try c.decode(Double.self, forKey: .widthFrac)
        rotationDeg = try c.decodeIfPresent(Double.self, forKey: .rotationDeg) ?? 0
    }

    /// The server's per-sticker rules (`bounds` in the fixture's `validation` section).
    var isValid: Bool {
        (1...Self.idMaxLength).contains(id.count) && !assetID.isEmpty
            && [xFrac, yFrac].allSatisfy { $0.isFinite && (0...1).contains($0) }
            && widthFrac.isFinite && Self.widthFracRange.contains(widthFrac)
            && rotationDeg.isFinite && Self.rotationRange.contains(rotationDeg)
    }

    /// Keeps a gesture's result inside the contract (position on the canvas, width and turn in range).
    mutating func clamp() {
        xFrac = min(max(xFrac, 0), 1); yFrac = min(max(yFrac, 0), 1)
        widthFrac = min(max(widthFrac, Self.widthFracRange.lowerBound), Self.widthFracRange.upperBound)
        rotationDeg = Self.normalized(rotationDeg)
    }

    /// An angle folded into -180...180 (a full turn of pinching must never write 400 degrees).
    static func normalized(_ degrees: Double) -> Double {
        guard degrees.isFinite else { return 0 }
        var value = degrees.truncatingRemainder(dividingBy: 360)
        if value > 180 { value -= 360 } else if value < -180 { value += 360 }
        return abs(value) < 0.005 ? 0 : value
    }
}

/// Where a sticker lands on a canvas, in canvas units. One function feeds the editor, the on-device export and the test
/// that pins it to the server's fixture, so the editor can never show a sticker anywhere the export does not put it.
enum SlidePostStickerGeometry {
    struct Placement: Equatable {
        var center: CGPoint
        var size: CGSize
        var rotationDegrees: Double
        /// The unrotated rectangle (draw it, rotated about `center`).
        var rect: CGRect { CGRect(x: center.x - size.width / 2, y: center.y - size.height / 2, width: size.width, height: size.height) }
    }

    static func placement(for sticker: SlidePostSticker, imageSize: CGSize, canvas: CGSize) -> Placement {
        let width = CGFloat(sticker.widthFrac) * canvas.width
        let aspect = imageSize.width > 0 ? imageSize.height / imageSize.width : 1
        return Placement(center: CGPoint(x: CGFloat(sticker.xFrac) * canvas.width, y: CGFloat(sticker.yFrac) * canvas.height),
                         size: CGSize(width: width, height: width * aspect), rotationDegrees: sticker.rotationDeg)
    }

    /// The corner a resize/rotate handle sits on (bottom-right of the rotated rectangle).
    static func handleCenter(of placement: Placement, padding: CGFloat) -> CGPoint {
        let radians = CGFloat(placement.rotationDegrees) * .pi / 180
        let dx = placement.size.width / 2 + padding, dy = placement.size.height / 2 + padding
        return CGPoint(x: placement.center.x + dx * cos(radians) - dy * sin(radians),
                       y: placement.center.y + dx * sin(radians) + dy * cos(radians))
    }

    /// Whether `point` (canvas units) falls inside the placement's rotated rectangle grown by `padding`.
    static func contains(_ point: CGPoint, in placement: Placement, padding: CGFloat = 0) -> Bool {
        let radians = -CGFloat(placement.rotationDegrees) * .pi / 180
        let dx = point.x - placement.center.x, dy = point.y - placement.center.y
        let x = dx * cos(radians) - dy * sin(radians), y = dx * sin(radians) + dy * cos(radians)
        return abs(x) <= placement.size.width / 2 + padding && abs(y) <= placement.size.height / 2 + padding
    }
}

extension SlidePostEdits {
    var effectiveStickers: [SlidePostSticker] { stickers ?? [] }
}

/// A sticker with its decoded picture, ready to draw. Pictures are immutable once decoded.
struct SlidePostPreparedSticker: @unchecked Sendable {
    let sticker: SlidePostSticker
    let image: UIImage

    /// Draws into a top-left-origin context of `canvas` pixels: centre, width, rotation about the centre, alpha kept.
    func draw(in context: CGContext, canvas: CGSize) {
        let placement = SlidePostStickerGeometry.placement(for: sticker, imageSize: image.size, canvas: canvas)
        context.saveGState()
        context.translateBy(x: placement.center.x, y: placement.center.y)
        context.rotate(by: CGFloat(placement.rotationDegrees) * .pi / 180)
        context.interpolationQuality = .high
        image.draw(in: CGRect(x: -placement.size.width / 2, y: -placement.size.height / 2, width: placement.size.width, height: placement.size.height))
        context.restoreGState()
    }
}

/// Decoded sticker pictures, kept apart from `SlidePostImageCache`: that cache stores JPEGs, which would flatten a
/// transparent PNG onto black. Here the ORIGINAL bytes are what stays on disk, so alpha survives a relaunch.
final class SlidePostStickerStore: @unchecked Sendable {
    static let shared = SlidePostStickerStore()
    /// Long edge a sticker is decoded at: sharp at the 2x export of the largest canvas, never beyond a memory-safe size.
    static let maxPixel = 2048

    private let memory = NSCache<NSString, UIImage>()
    private let directory: URL
    private let fetch: SlidePostImageCache.Fetcher
    private let lock = NSLock()
    private var inflight: [String: Task<UIImage, Error>] = [:]

    init(directory: URL? = nil, fetch: @escaping SlidePostImageCache.Fetcher = SlidePostImageCache.defaultFetch) {
        self.directory = directory ?? FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appending(path: "SlidePostStickers", directoryHint: .isDirectory)
        self.fetch = fetch
        memory.countLimit = 24
        try? FileManager.default.createDirectory(at: self.directory, withIntermediateDirectories: true)
    }

    /// Instant lookup of an already-decoded picture (memory only).
    func cachedImage(assetID: String) -> UIImage? { memory.object(forKey: assetID as NSString) }

    /// Memory, then the original bytes on disk, then the network. Throws `SlidePostImageCache.LoadError.expired` for a
    /// stale signed URL so the caller can refresh the session and try once more.
    func image(assetID: String, url: URL) async throws -> UIImage {
        if let hit = cachedImage(assetID: assetID) { return hit }
        let task: Task<UIImage, Error> = lock.withLock {
            if let running = inflight[assetID] { return running }
            let created = Task.detached(priority: .userInitiated) { [self] in try await load(assetID: assetID, url: url) }
            inflight[assetID] = created
            return created
        }
        defer { lock.withLock { inflight[assetID] = nil } }
        return try await task.value
    }

    /// Every sticker of a slide with its picture. A sticker whose asset is not in the pool throws, so an export fails
    /// loudly instead of quietly leaving out something the creator can see in the editor.
    func prepared(for slide: SlidePostSlide, assets: [SlidePostAsset]) async throws -> [SlidePostPreparedSticker] {
        var result: [SlidePostPreparedSticker] = []
        for sticker in slide.edits?.effectiveStickers ?? [] {
            guard let asset = assets.first(where: { $0.id == sticker.assetID }), let url = asset.sourceURL ?? asset.displayURL else {
                throw SlidePostImageCache.LoadError.failed
            }
            result.append(.init(sticker: sticker, image: try await image(assetID: asset.id, url: url)))
        }
        return result
    }

    func removeAll() {
        memory.removeAllObjects()
        try? FileManager.default.removeItem(at: directory)
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    }

    private func file(_ assetID: String) -> URL {
        directory.appending(path: assetID.filter { $0.isLetter || $0.isNumber || $0 == "-" })
    }

    private func load(assetID: String, url: URL) async throws -> UIImage {
        let path = file(assetID)
        if let data = try? Data(contentsOf: path), let image = Self.decode(data) {
            memory.setObject(image, forKey: assetID as NSString)
            return image
        }
        let data = try await fetch(url)
        guard let image = Self.decode(data) else { throw SlidePostImageCache.LoadError.failed }
        try? data.write(to: path, options: .atomic)
        memory.setObject(image, forKey: assetID as NSString)
        return image
    }

    /// Orientation-correct and downsampled (never up-scaled), keeping the alpha channel.
    static func decode(_ data: Data, maxPixel: Int = SlidePostStickerStore.maxPixel) -> UIImage? {
        guard let source = CGImageSourceCreateWithData(data as CFData, [kCGImageSourceShouldCache: false] as CFDictionary) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixel,
        ]
        guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
        return UIImage(cgImage: cg)
    }
}
