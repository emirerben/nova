import OSLog
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
    /// Exposes `slidepost-preview-image` with value `<assetID>|full` or `<assetID>|blur` so UI tests can see
    /// whether the full picture or the blurred placeholder is on screen.
    var reportsState = false
    /// Source-only grade. Thumbnails remain ungraded in the shared cache.
    var lookPreset = "none"
    var lookCanvas = CGSize(width: 1080, height: 1350)

    @State private var tick = 0
    @State private var failed = false
    @State private var expiredRetries = 0
    @State private var lookedImage: UIImage?
    @State private var lookedIdentity = ""
    @State private var lookFailed = false

    private var taskID: String { "\(SlidePostImageCache.key(assetID, variant))|\(url == nil)|\(tick)" }

    var body: some View {
        let full = resolved()
        let lookIdentity = "\(assetID)|\(lookPreset)|\(lookCanvas.width)x\(lookCanvas.height)|\(full.map { ObjectIdentifier($0).debugDescription } ?? "missing")"
        ZStack {
            if let full {
                Image(uiImage: lookedIdentity == lookIdentity ? (lookedImage ?? full) : full).resizable().scaledToFill()
                if lookFailed, lookPreset != "none" {
                    Text("Look preview unavailable").font(KriaFont.body(12))
                        .padding(8).background(KriaColor.paper, in: Capsule())
                }
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
        .task(id: lookIdentity) {
            lookedImage = nil; lookedIdentity = lookIdentity; lookFailed = false
            guard lookPreset != "none", let full else { return }
            do {
                let canvas = lookCanvas, preset = lookPreset
                let image = try await Task.detached(priority: .userInitiated) {
                    try SlidePostOnDeviceRender.lookedImage(full, pixels: canvas, preset: preset)
                }.value
                guard !Task.isCancelled else { return }
                lookedImage = image
            } catch { if !Task.isCancelled { lookFailed = true } }
        }
        .modifier(PreviewStateReport(enabled: reportsState,
                                    value: "\(assetID)|\(full != nil ? "full" : "blur")" + (lookedImage != nil && lookedIdentity == lookIdentity ? "|look:\(lookPreset)" : ""),
                                    blurred: full == nil && variant == .preview && cache.cachedImage(assetID: assetID, variant: .thumb) != nil))
    }

    private static let log = Logger(subsystem: "com.kria.app", category: "slidepost-images")

    /// Memory, then the pinned working set, then (previews only) a synchronous disk read: the selected slide
    /// must never fall back to the blurred thumbnail when its decoded copy is merely a few ms away.
    private func resolved() -> UIImage? {
        guard variant == .preview else { return cache.cachedImage(assetID: assetID, variant: variant) }
        guard let hit = cache.lookup(assetID: assetID, variant: variant) else { return nil }
        if hit.tier != .memory { Self.log.info("select \(assetID, privacy: .public) hit=\(hit.tier.rawValue, privacy: .public)") }
        return hit.image
    }

    private func load() async {
        guard let url else { return }
        if resolved() != nil { return }
        Self.log.info("select \(assetID, privacy: .public) hit=miss (async load)")
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

private struct PreviewStateReport: ViewModifier {
    let enabled: Bool
    let value: String
    let blurred: Bool
    func body(content: Content) -> some View {
        if enabled {
            content.overlay {
                ZStack {
                    Color.clear.accessibilityElement().accessibilityIdentifier("slidepost-preview-image").accessibilityValue(value)
                    if blurred { Color.clear.accessibilityElement().accessibilityIdentifier("slidepost-preview-blurred") }
                }.allowsHitTesting(false)
            }
        } else { content }
    }
}

private struct OptionalIdentifier: ViewModifier {
    let id: String?
    func body(content: Content) -> some View {
        if let id { content.accessibilityIdentifier(id) } else { content }
    }
}
