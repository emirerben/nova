import SwiftUI
import UIKit

/// A slide photo backed by `SlidePostImageCache`. Identity is the asset id, so a new signed URL for the
/// same photo never reloads it, and switching slides shows the new image in the same frame when it is
/// cached. While a miss loads, the already-cached thumbnail is shown blurred (blur-up) instead of a
/// spinner; the spinner (`loadingIdentifier`) appears only when there is nothing at all to show.
struct SlidePostCachedImage: View {
    let assetID: String
    let variant: SlidePostImageCache.Variant
    let url: URL?
    /// Called once when the signed URL turned out to be expired; refresh the asset URLs there.
    var onExpired: (() async -> Void)? = nil
    var loadingIdentifier: String? = nil
    var showsSpinner = true
    var showsRetry = false
    var cache: SlidePostImageCache = .shared

    @State private var tick = 0
    @State private var failed = false
    @State private var expiredRetries = 0

    private var taskID: String { "\(SlidePostImageCache.key(assetID, variant))|\(url == nil)|\(tick)" }

    var body: some View {
        let full = cache.cachedImage(assetID: assetID, variant: variant)
        ZStack {
            if let full {
                Image(uiImage: full).resizable().scaledToFill()
            } else if variant == .preview, let thumb = cache.cachedImage(assetID: assetID, variant: .thumb) {
                Image(uiImage: thumb).resizable().scaledToFill().blur(radius: 10)
            } else if failed {
                VStack(spacing: 8) {
                    Image(systemName: "exclamationmark.triangle").foregroundStyle(KriaColor.zinc)
                    if showsRetry {
                        Text("Preview unavailable").font(KriaFont.body(12))
                        Button("Retry preview") { Task { await onExpired?(); expiredRetries = 0; tick += 1 } }
                            .buttonStyle(KriaSecondaryButtonStyle())
                    }
                }
            } else if showsSpinner {
                ProgressView().modifier(OptionalIdentifier(id: loadingIdentifier))
            }
        }
        .task(id: taskID) { await load() }
    }

    private func load() async {
        guard let url else { return }
        if cache.cachedImage(assetID: assetID, variant: variant) != nil { return }
        failed = false
        do {
            _ = try await cache.image(assetID: assetID, variant: variant, url: url)
            expiredRetries = 0
            tick += 1
        } catch SlidePostImageCache.LoadError.expired {
            guard expiredRetries < 1, let onExpired else { failed = true; return }
            expiredRetries += 1
            await onExpired()
            tick += 1
        } catch is CancellationError {
        } catch {
            failed = true
        }
    }
}

private struct OptionalIdentifier: ViewModifier {
    let id: String?
    func body(content: Content) -> some View {
        if let id { content.accessibilityIdentifier(id) } else { content }
    }
}
