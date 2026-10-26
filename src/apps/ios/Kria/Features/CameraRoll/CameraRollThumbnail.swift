import SwiftUI
import Photos

/// One picture for a collection entry: the Photos thumbnail, the saved upload's thumbnail, or a soft gradient when
/// neither is available (offline fixtures and the metadata-only diagnostic). The gradient is chosen from the entry
/// id so a given item always keeps its colours.
struct CameraRollCollectionThumbnail: View {
    let entry: CameraRollCollection.Entry
    let api: any KriaAPIClient
    var metadataOnly = false
    let size: CGSize
    @State private var asset: PHAsset?
    @State private var loader = GalleryThumbnailLoader()

    var body: some View {
        ZStack {
            if !metadataOnly, let saved = entry.savedAsset {
                SavedUploadThumbnail(asset: saved, owner: api.mediaLibraryOwner, api: api, size: size)
            } else if !metadataOnly, let asset {
                GalleryThumbnail(asset: asset, loader: loader)
            } else {
                CameraRollPlaceholder.gradient(for: entry.id)
            }
        }.frame(width: size.width, height: size.height).clipped().accessibilityHidden(true)
            .task(id: entry.id) {
                guard !metadataOnly, entry.savedAsset == nil, !entry.id.hasPrefix("fixture-"), !entry.id.hasPrefix("picker:") else { return }
                asset = PHAsset.fetchAssets(withLocalIdentifiers: [entry.id], options: nil).firstObject
            }
    }
}

enum CameraRollPlaceholder {
    /// Pairs of (top-leading, bottom-trailing) stops, from the Paper contact sheet.
    private static let palette: [(UInt32, UInt32)] = [
        (0xE8D5B5, 0xA58B62), (0xBFD6D2, 0x5E8A8A), (0xF2C6A0, 0xC77B52), (0xD9D2EE, 0x8B7CB8),
        (0xD3E0BF, 0x7C9560), (0xF6E3A8, 0xD6A63E), (0xCFE0F2, 0x7FA3C9), (0xEAD4E0, 0xB9809E)
    ]

    static func gradient(for id: String) -> some View {
        // `hashValue` is seeded per launch; a stable sum keeps the colours the same between runs.
        let index = id.unicodeScalars.reduce(0) { ($0 &* 31 &+ Int($1.value)) & 0x7fffffff } % palette.count
        let pair = palette[index]
        return LinearGradient(colors: [color(pair.0), color(pair.1)], startPoint: .topLeading, endPoint: .bottomTrailing)
    }

    private static func color(_ hex: UInt32) -> Color {
        Color(red: Double((hex >> 16) & 255) / 255, green: Double((hex >> 8) & 255) / 255, blue: Double(hex & 255) / 255)
    }
}
