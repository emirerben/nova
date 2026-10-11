import KriaMediaEngine
import SwiftUI
import UIKit

/// The slide Crop mode (KRI-44): the stage becomes the shared `KriaCropEditor` for the selected photo, with a
/// Vertical | Landscape choice on TikTok posts (Instagram keeps one shape for every slide).
struct SlidePostCropWorkspace: View {
    @ObservedObject var session: SlidePostSession
    let slide: SlidePostSlide
    let profile: String
    /// The signed preview URL of the slide's photo and its refresher (a stale signature is re-signed once).
    let previewURL: URL?
    let onExpired: () async -> Void
    let onDone: () -> Void
    var cache: SlidePostImageCache = .shared

    private var framing: SlidePostFraming? { slide.edits?.framing }
    private var orientation: SlidePostFraming.Orientation { framing?.orientation ?? .portrait }
    private var canvas: CGSize { SlidePostFraming.canvas(for: slide, profile: profile) }
    private var allowsLandscape: Bool { SlidePostFraming.allowsLandscape(profile: profile) }

    var body: some View {
        VStack(spacing: 10) {
            HStack {
                Text("Crop").font(KriaFont.body(18).weight(.semibold))
                Spacer()
                Button(action: onDone) {
                    Text("Done").font(KriaFont.body(14)).frame(minWidth: 64, minHeight: 44)
                        .background(KriaColor.ink.opacity(0.06), in: Capsule())
                }
                .accessibilityIdentifier("slidepost-crop-done")
            }
            if allowsLandscape { shapeControl } else {
                Text("Instagram keeps one shape for every slide").font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("slidepost-crop-shape-note")
            }
            KriaCropFrameHost(
                load: loadImage,
                outputAspect: canvas.width / canvas.height,
                crop: Binding(
                    get: { framing?.sourceCrop },
                    set: { session.setFraming(slideID: slide.id, SlidePostFraming(orientation: orientation, sourceCrop: $0)) }
                ),
                reloadKey: previewURL?.absoluteString ?? ""
            )
            .id(slide.id)
        }
        .padding(.horizontal, 16).padding(.top, 8).padding(.bottom, 8)
        .background(KriaColor.paper)
        .accessibilityIdentifier("slidepost-crop")
    }

    private var shapeControl: some View {
        HStack(spacing: 8) {
            shapeButton(.portrait, "Vertical", symbol: "rectangle.portrait")
            shapeButton(.landscape, "Landscape", symbol: "rectangle")
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("slidepost-crop-shape")
    }

    private func shapeButton(_ value: SlidePostFraming.Orientation, _ title: String, symbol: String) -> some View {
        let on = orientation == value
        return Button {
            // The old rectangle has the wrong shape for the other canvas, so a new shape starts uncropped.
            guard !on else { return }
            session.setFraming(slideID: slide.id, SlidePostFraming(orientation: value, sourceCrop: nil))
        } label: {
            Label(title, systemImage: symbol).font(KriaFont.body(14).weight(on ? .semibold : .regular))
                .foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(on ? KriaColor.butter : KriaColor.fill, in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(on ? .isSelected : [])
        .accessibilityIdentifier("slidepost-crop-\(value.rawValue)")
    }

    private func loadImage() async -> UIImage? {
        if let hit = cache.lookup(assetID: slide.assetID, variant: .preview) { return hit.image }
        guard let url = previewURL else { return nil }
        do { return try await cache.image(assetID: slide.assetID, variant: .preview, url: url) }
        catch SlidePostImageCache.LoadError.expired {
            await onExpired()
            return nil
        } catch { return nil }
    }
}
