import KriaMediaEngine
import SwiftUI
import UIKit

/// The slide Crop mode (KRI-44): the stage becomes the shared `KriaCropEditor` for the selected photo, with a
/// Vertical | Landscape choice on TikTok posts (Instagram keeps one shape for every slide), and (KRI-606) a
/// Fill | Fit choice that shows the whole cropped photo with bars instead of trimming its edges.
struct SlidePostCropWorkspace: View {
    @ObservedObject var session: SlidePostSession
    let slide: SlidePostSlide
    let profile: String
    /// The post-wide size (`SlidePostDraft.aspect`, nil = the platform default). Passed explicitly, never defaulted.
    let aspect: String?
    /// True when the server's `slide_post_sizes` is on: shows the Fill | Fit row and the post-size note.
    let showsFit: Bool
    /// The signed preview URL of the slide's photo and its refresher (a stale signature is re-signed once).
    let previewURL: URL?
    let onExpired: () async -> Void
    let onDone: () -> Void
    var cache: SlidePostImageCache = .shared

    private var framing: SlidePostFraming? { slide.edits?.framing }
    private var orientation: SlidePostFraming.Orientation { framing?.orientation ?? .portrait }
    private var canvas: CGSize { SlidePostFraming.canvas(for: slide, profile: profile, aspect: aspect) }
    private var fit: SlidePostFit? { slide.edits?.fit }
    private var postSize: SlidePostSize { SlidePostSize.resolved(aspect, profile: profile) }
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
                Text(showsFit ? "Every slide uses the post size (\(postSize.rawValue)). Change it in Size."
                     : "Instagram keeps one shape for every slide")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc)
                    .accessibilityIdentifier("slidepost-crop-shape-note")
            }
            if showsFit { fitControl }
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

    /// Fill trims the photo's edges to cover the frame; Fit shows exactly the cropped region, with bars around it.
    private var fitControl: some View {
        VStack(spacing: 8) {
            HStack(spacing: 8) {
                fitModeButton("Fill", on: fit == nil, id: "slidepost-crop-fit-fill") { session.setFit(slideID: slide.id, nil) }
                fitModeButton("Fit", on: fit != nil, id: "slidepost-crop-fit-fit") { session.setFit(slideID: slide.id, fit ?? .blur) }
            }
            if let fit {
                HStack(spacing: 8) {
                    ForEach(SlidePostFit.allCases, id: \.self) { value in
                        let on = fit == value
                        Button { session.setFit(slideID: slide.id, value) } label: {
                            Text(value.label).font(KriaFont.body(13).weight(on ? .semibold : .regular)).foregroundStyle(KriaColor.ink)
                                .frame(maxWidth: .infinity, minHeight: 44)
                                .background(on ? KriaColor.butter : KriaColor.fill, in: Capsule())
                        }
                        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
                        .accessibilityIdentifier("slidepost-crop-bars-\(value.rawValue)")
                    }
                }
                Text("Fit keeps the whole cropped photo in frame, with bars around it. Use Free to crop to any shape.")
                    .font(KriaFont.body(12)).foregroundStyle(KriaColor.zinc).frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("slidepost-crop-fit-note")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("slidepost-crop-fit")
    }

    private func fitModeButton(_ title: String, on: Bool, id: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(KriaFont.body(14).weight(on ? .semibold : .regular)).foregroundStyle(KriaColor.ink)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(on ? KriaColor.butter : KriaColor.fill, in: Capsule())
        }
        .buttonStyle(.plain).accessibilityAddTraits(on ? .isSelected : [])
        .accessibilityIdentifier(id)
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
