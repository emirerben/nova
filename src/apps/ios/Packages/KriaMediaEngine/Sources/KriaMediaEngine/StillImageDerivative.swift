import Foundation
#if canImport(ImageIO)
import ImageIO
import UniformTypeIdentifiers

/// Visuals-pool photos arrive at full camera resolution: a 48 MP HEIC decodes to
/// roughly 190 MB, and the composition keeps every still decoded for the whole
/// render. A fullscreen still only needs enough pixels to cover the canvas, so
/// write one orientation-applied, cover-sized copy per verified source and reuse it.
public enum StillImageDerivative {
    public static func prepare(source: URL, fingerprint: RenderFingerprint, canvas: Canvas, directory: URL) throws -> URL {
        guard let image = CGImageSourceCreateWithURL(source as CFURL, nil), CGImageSourceGetCount(image) > 0,
              let properties = CGImageSourceCopyPropertiesAtIndex(image, 0, nil) as? [CFString: Any],
              let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.doubleValue,
              let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.doubleValue,
              width > 0, height > 0, canvas.width > 0, canvas.height > 0 else { throw MediaEngineError.missingAsset(source.lastPathComponent) }
        let orientation = (properties[kCGImagePropertyOrientation] as? NSNumber)?.intValue ?? 1
        let (orientedWidth, orientedHeight) = orientation >= 5 ? (height, width) : (width, height)
        let scale = max(Double(canvas.width) / orientedWidth, Double(canvas.height) / orientedHeight)
        // Never upscale: the composition already scales small photos to cover.
        guard scale < 1 else { return source }
        let maxPixelSize = Int((max(orientedWidth, orientedHeight) * scale).rounded(.up))
        let hasAlpha = properties[kCGImagePropertyHasAlpha] as? Bool == true
        let type = hasAlpha ? UTType.png : UTType.jpeg
        let destination = directory.appendingPathComponent(
            "\(fingerprint.sha256)-\(fingerprint.byteCount)-\(maxPixelSize).\(type.preferredFilenameExtension ?? "img")")
        if FileManager.default.fileExists(atPath: destination.path) { return destination }
        guard let thumbnail = CGImageSourceCreateThumbnailAtIndex(image, 0, [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceShouldCacheImmediately: true,
            kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
        ] as CFDictionary) else { throw MediaEngineError.missingAsset(source.lastPathComponent) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let staging = directory.appendingPathComponent(UUID().uuidString + ".partial")
        defer { try? FileManager.default.removeItem(at: staging) }
        guard let writer = CGImageDestinationCreateWithURL(staging as CFURL, type.identifier as CFString, 1, nil) else {
            throw MediaEngineError.exportUnavailable
        }
        CGImageDestinationAddImage(writer, thumbnail, [kCGImageDestinationLossyCompressionQuality: 0.95] as CFDictionary)
        guard CGImageDestinationFinalize(writer) else { throw MediaEngineError.exportUnavailable }
        if FileManager.default.fileExists(atPath: destination.path) { return destination }
        try FileManager.default.moveItem(at: staging, to: destination)
        return destination
    }
}
#endif
