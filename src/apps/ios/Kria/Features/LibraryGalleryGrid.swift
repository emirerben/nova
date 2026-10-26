import Photos
import PhotosUI
import SwiftUI

/// In-app Photos gallery for the start of creation (KRI-282 follow-up): newest first, video duration badges,
/// pick-order numbers, and Photos-style slide-to-select (the system picker can't be given that).
///
/// It edits the same `[PhotosPickerItem]` the system picker edits (built from `localIdentifier`s), so the
/// upload ledger, diffing, proxy and un-choose paths downstream are untouched. Only used under full Photos
/// access; limited/denied access keeps the system picker, and the host offers "Apple's picker" as a fallback.
struct LibraryGalleryGrid: View {
    @Binding var selection: [PhotosPickerItem]
    let limit: Int
    let kinds: LibraryMediaKinds
    @State private var assets: PHFetchResult<PHAsset>?
    @State private var dragBase: LibraryGallerySelection?
    @State private var limitMessage: String?
    @State private var tapToken = 0
    @State private var loader = GalleryThumbnailLoader()
    private let columns = Array(repeating: GridItem(.flexible(), spacing: 2), count: 4)

    private var current: LibraryGallerySelection {
        LibraryGallerySelection(ids: selection.compactMap(\.itemIdentifier), limit: limit)
    }

    var body: some View {
        VStack(spacing: 0) {
            if let assets, assets.count > 0 {
                DragSelectScrollView(
                    isEnabled: true,
                    isSelected: { index in
                        guard index < assets.count else { return false }
                        return current.contains(assets.object(at: index).localIdentifier)
                    },
                    begin: { dragBase = current },
                    apply: { indices, isOn in
                        guard let base = dragBase else { return }
                        var next = base
                        next.apply(indices.filter { $0 < assets.count }.map { assets.object(at: $0).localIdentifier }, selected: isOn)
                        write(next)
                    },
                    end: { dragBase = nil }
                ) {
                    LazyVGrid(columns: columns, spacing: 2) {
                        ForEach(0..<assets.count, id: \.self) { index in
                            tile(assets.object(at: index), index: index).dragSelectTile(index: index)
                        }
                    }
                }
                .accessibilityIdentifier("gallery-grid")
            } else if assets != nil {
                ContentUnavailableView("No \(kindsNoun) in Photos", systemImage: "photo.on.rectangle")
                    .frame(maxHeight: .infinity)
            } else {
                ProgressView().frame(maxHeight: .infinity)
            }
            footer
        }
        .sensoryFeedback(.selection, trigger: selection.count)
        .sensoryFeedback(.warning, trigger: tapToken)
        .task { load() }
    }

    private var kindsNoun: String {
        kinds == .videos ? "videos" : kinds == .images ? "photos" : "photos or videos"
    }

    private var footer: some View {
        GalleryFooter(text: limitMessage ?? "\(selection.count) of \(limit) selected · slide across to select", identifier: "gallery-count")
    }

    private func load() {
        let options = PHFetchOptions()
        options.sortDescriptors = [NSSortDescriptor(key: "creationDate", ascending: false)]
        let types = kinds.mediaTypes
        options.predicate = NSCompoundPredicate(orPredicateWithSubpredicates: types.map { NSPredicate(format: "mediaType == %d", $0.rawValue) })
        assets = PHAsset.fetchAssets(with: options)
    }

    private func write(_ next: LibraryGallerySelection) {
        guard next.ids != selection.compactMap(\.itemIdentifier) else { return }
        let existing = Dictionary(selection.compactMap { item in item.itemIdentifier.map { ($0, item) } }, uniquingKeysWith: { first, _ in first })
        selection = next.ids.map { existing[$0] ?? PhotosPickerItem(itemIdentifier: $0) }
        limitMessage = nil
    }

    private func tap(_ id: String) {
        var next = current
        if next.toggle(id) == .refusedAtLimit {
            limitMessage = "You can choose up to \(limit) here."
            tapToken += 1
            return
        }
        write(next)
    }

    private func tile(_ asset: PHAsset, index: Int) -> some View {
        let id = asset.localIdentifier
        let number = current.number(of: id)
        return Button { tap(id) } label: {
            Color.clear
                .aspectRatio(1, contentMode: .fit)
                .overlay { GalleryThumbnail(asset: asset, loader: loader) }
                .clipped()
                .modifier(GalleryTileChrome(number: number, duration: asset.mediaType == .video ? asset.duration : nil))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(asset.mediaType == .video ? "Video, \(Self.duration(asset.duration))" : "Photo")
        .accessibilityValue(number.map { "Selected, \($0)" } ?? "Not selected")
        .accessibilityAddTraits(number != nil ? .isSelected : [])
        .accessibilityIdentifier("gallery-tile-\(index)")
    }

    static func duration(_ seconds: TimeInterval) -> String { GalleryTileChrome.duration(seconds) }
}

/// One thumbnail: opportunistic request (fast low-res first), network access on so iCloud-only assets still
/// show a poster. Cancelled when the tile scrolls away.
struct GalleryThumbnail: View {
    let asset: PHAsset
    let loader: GalleryThumbnailLoader
    @State private var image: UIImage?

    var body: some View {
        ZStack {
            KriaColor.zinc.opacity(0.14)
            if let image { Image(uiImage: image).resizable().scaledToFill() }
        }
        .clipped()
        .task(id: asset.localIdentifier) {
            let stream = loader.images(for: asset)
            for await next in stream { image = next }
        }
        .accessibilityHidden(true)
    }
}

final class GalleryThumbnailLoader: @unchecked Sendable {
    private let manager = PHCachingImageManager()
    private let size = CGSize(width: 240, height: 240)

    func images(for asset: PHAsset) -> AsyncStream<UIImage> {
        AsyncStream { continuation in
            let options = PHImageRequestOptions()
            options.deliveryMode = .opportunistic
            options.resizeMode = .fast
            options.isNetworkAccessAllowed = true
            let request = manager.requestImage(for: asset, targetSize: size, contentMode: .aspectFill, options: options) { image, info in
                if let image { continuation.yield(image) }
                let degraded = (info?[PHImageResultIsDegradedKey] as? Bool) ?? false
                if !degraded || (info?[PHImageCancelledKey] as? Bool) == true || info?[PHImageErrorKey] != nil { continuation.finish() }
            }
            continuation.onTermination = { [manager] _ in manager.cancelImageRequest(request) }
        }
    }
}
