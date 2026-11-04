import Foundation
import ImageIO
import Photos

/// Item 24: a talking edit whose Footage also holds screen recordings. Footage clips become full-screen cut-ins that
/// hide the speaker; a screen recording belongs in Overlays. UI-free so the rules are testable.
enum FootageShape: Equatable, Sendable {
    case portrait, landscape, square, unknown

    init(width: Double, height: Double) {
        guard width > 0, height > 0 else { self = .unknown; return }
        if width >= 1.2 * height { self = .landscape }
        else if height >= 1.2 * width { self = .portrait }
        else { self = .square }
    }
}

struct FootagePlacementItem: Equatable, Sendable {
    let id: String
    let shape: FootageShape
    let isScreenRecording: Bool
}

enum FootagePlacement {
    /// macOS "Screen Recording … .mov", iOS "ScreenRecording_…", "RPReplay_Final…", "Simulator Screen Recording …".
    static func looksLikeScreenRecording(filename: String) -> Bool {
        let squashed = filename.lowercased().filter { !" _-.".contains($0) }
        return squashed.contains("screenrecording") || squashed.contains("rpreplay")
    }

    /// Ids (input order) of clips to point at Overlays. Shape alone cannot tell a portrait recording from a take, so
    /// only a landscape clip or a named/Photos-flagged recording is a candidate, and only when a portrait take that
    /// is not a recording sits beside it. A clip whose shape is not known yet is never a take: no hint until known.
    static func belongsInOverlays(_ items: [FootagePlacementItem]) -> [String] {
        guard items.contains(where: { !$0.isScreenRecording && $0.shape == .portrait }) else { return [] }
        return items.filter { $0.isScreenRecording || $0.shape == .landscape }.map(\.id)
    }
}

/// Reads the pixel size of a cached preview JPEG without decoding it. The previews are drawn with the preferred
/// transform applied, so the size is the display orientation.
@MainActor enum FootageShapeReader {
    private static var cache: [String: FootageShape] = [:]

    /// `version` is `uploads.previewVersion`: a thumbnail written later is read again, not served from a stale miss.
    static func shape(of url: URL, version: Int) -> FootageShape {
        let key = "\(url.path)#\(version)"
        if let known = cache[key] { return known }
        let read = pixelShape(of: url)
        cache[key] = read
        return read
    }

    nonisolated static func pixelShape(of url: URL) -> FootageShape {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = (properties[kCGImagePropertyPixelWidth] as? NSNumber)?.doubleValue,
              let height = (properties[kCGImagePropertyPixelHeight] as? NSNumber)?.doubleValue else { return .unknown }
        return FootageShape(width: width, height: height)
    }
}

/// Photos marks screen recordings with a subtype, which also catches a portrait one. Never prompts: it reads only
/// when the creator already allowed library access.
@MainActor enum PhotosScreenRecording {
    private static var cache: [String: Bool] = [:]

    static func isScreenRecording(assetIdentifier: String) -> Bool {
        if let known = cache[assetIdentifier] { return known }
        guard PHPhotoLibrary.authorizationStatus(for: .readWrite) == .authorized else { return false }
        let asset = PHAsset.fetchAssets(withLocalIdentifiers: [assetIdentifier], options: nil).firstObject
        let flagged = asset?.mediaSubtypes.contains(.videoScreenRecording) ?? false
        cache[assetIdentifier] = flagged
        return flagged
    }
}
