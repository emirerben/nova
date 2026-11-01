import KriaMediaEngine
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
    /// KRI-44: the slide's crop, in the upright frame. The view this fills has the slide's canvas aspect, so what
    /// shows here is what exports. nil = plain centred cover.
    var crop: NormalizedSourceRect? = nil
    /// KRI-606: show the whole (cropped) photo with these bars instead of covering the frame. nil = fill.
    var fit: SlidePostFit? = nil
    /// KRI-1138: the media turned about the canvas centre over a backdrop. nil = upright.
    var tilt: SlidePostTilt? = nil

    @State private var tick = 0
    @State private var failed = false
    @State private var expiredRetries = 0
    @State private var lookedImage: UIImage?
    @State private var lookedIdentity = ""
    @State private var lookFailed = false

    private var taskID: String { "\(SlidePostImageCache.key(assetID, variant))|\(url == nil)|\(tick)" }

    var body: some View {
        let full = resolved()
        let lookIdentity = "\(assetID)|\(lookPreset)|\(fit?.rawValue ?? "fill")|\(tilt.map { "\($0.rotationDeg)|\($0.backdrop)" } ?? "-")|\(lookCanvas.width)x\(lookCanvas.height)|\(crop.map { "\($0.x),\($0.y),\($0.width),\($0.height)" } ?? "-")|\(full.map { ObjectIdentifier($0).debugDescription } ?? "missing")"
        ZStack {
            if let full {
                if lookedIdentity == lookIdentity, let lookedImage {
                    // Already framed to the canvas by the look pass.
                    Image(uiImage: lookedImage).resizable().scaledToFill()
                } else {
                    framed(full)
                }
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
            guard lookPreset != "none" || fit != nil || tilt != nil, let full else { return }
            do {
                let canvas = lookCanvas, preset = lookPreset, framing = crop, bars = fit, turn = tilt
                let image = try await Task.detached(priority: .userInitiated) {
                    try SlidePostOnDeviceRender.lookedImage(full, pixels: canvas, preset: preset, crop: framing, fit: bars, tilt: turn)
                }.value
                guard !Task.isCancelled else { return }
                lookedImage = image
            } catch { if !Task.isCancelled { lookFailed = true } }
        }
        .modifier(PreviewStateReport(enabled: reportsState,
                                    value: "\(assetID)|\(full != nil ? "full" : "blur")" + (lookedImage != nil && lookedIdentity == lookIdentity ? "|look:\(lookPreset)" : "")
                                        + (tilt.map { "|tilt:\(Int($0.rotationDeg.rounded()))" } ?? ""),
                                    blurred: full == nil && variant == .preview && cache.cachedImage(assetID: assetID, variant: .thumb) != nil))
    }

    @ViewBuilder private func framed(_ image: UIImage) -> some View {
        if let tilt, !tilt.isUpright {
            tiltedFrame(image, tilt: tilt)
        } else if crop != nil || fit != nil {
            GeometryReader { proxy in
                let rect = SlidePostFraming.placement(imageSize: image.size, crop: crop, canvas: proxy.size, fit: fit != nil)
                // With bars, only the cropped region is the photo; the rest of the source never shows in the bars.
                let region: CGRect? = fit != nil ? crop.map {
                    CGRect(x: rect.minX + $0.x * rect.width, y: rect.minY + $0.y * rect.height, width: $0.width * rect.width, height: $0.height * rect.height)
                } : nil
                Image(uiImage: image).resizable()
                    .frame(width: rect.width, height: rect.height)
                    .offset(x: rect.minX, y: rect.minY)
                    .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
                    .clipShape(SlidePostRegionClip(region: region))
                    .background(fit == .white ? Color.white : Color.black)
            }
            .clipped()
        } else {
            Image(uiImage: image).resizable().scaledToFill()
        }
    }

    /// KRI-1138: instant SwiftUI stand-in for `SlidePostOnDeviceRender.tiltedBase` while the exact Core Image pass computes, so
    /// a dial drag never shows the upright photo. Same geometry (the region scaled like the slide frames it, turned
    /// clockwise about the canvas centre); the blur backdrop is approximate.
    private func tiltedFrame(_ image: UIImage, tilt: SlidePostTilt) -> some View {
        GeometryReader { proxy in
            let whole = SlidePostFraming.placement(imageSize: image.size, crop: crop, canvas: proxy.size, fit: fit != nil)
            let cover = SlidePostFraming.placement(imageSize: image.size, crop: crop, canvas: proxy.size, fit: false)
            let region: CGRect? = crop.map {
                CGRect(x: whole.minX + $0.x * whole.width, y: whole.minY + $0.y * whole.height, width: $0.width * whole.width, height: $0.height * whole.height)
            }
            ZStack {
                SlidePostTiltBackdrop(backdrop: tilt.backdrop, image: image, cover: cover, canvas: proxy.size)
                Image(uiImage: image).resizable()
                    .frame(width: whole.width, height: whole.height)
                    .offset(x: whole.minX, y: whole.minY)
                    .frame(width: proxy.size.width, height: proxy.size.height, alignment: .topLeading)
                    .clipShape(SlidePostRegionClip(region: region, clipsToView: false))
                    .rotationEffect(.degrees(tilt.rotationDeg))
            }
            .frame(width: proxy.size.width, height: proxy.size.height)
            .clipped()
        }
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

/// Clips to `region` (top-left, in the view's coordinates) when there is one, else leaves the frame whole.
private struct SlidePostRegionClip: Shape {
    let region: CGRect?
    /// False for a turned photo: the region may extend past the view and must keep its overflow so the turn can reveal it.
    var clipsToView = true
    func path(in rect: CGRect) -> Path { Path(region.map { clipsToView ? $0.intersection(rect) : $0 } ?? rect) }
}

/// What shows behind a turned slide (KRI-1138): black, a colour, or a blurred cover-fit copy of the same photo.
struct SlidePostTiltBackdrop: View {
    let backdrop: ClipBackdrop
    var image: UIImage? = nil
    var cover: CGRect = .zero
    let canvas: CGSize

    var body: some View {
        switch backdrop {
        case .blank, .media:
            Color.black
        case .color(let hex):
            Color(slideHex: hex)
        case .blur:
            if let image {
                Image(uiImage: image).resizable()
                    .frame(width: cover.width, height: cover.height)
                    .offset(x: cover.minX, y: cover.minY)
                    .frame(width: canvas.width, height: canvas.height, alignment: .topLeading)
                    .blur(radius: SlidePostFit.blurSigmaPx * canvas.width / 1080)
                    .clipped()
            } else { Color.black }
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
