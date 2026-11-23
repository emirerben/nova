import AVFoundation
import KriaMediaEngine
import SwiftUI
import UIKit

/// KRI-1582: one design option drawn on this iPhone: the server's text layers over a frame of the creator's own
/// clip, through the media engine's text pass (`TextLayerStill`), so the still shows what the export will draw.
enum DesignPreviewRenderer {
    /// What the still was drawn over, reported to VoiceOver and to UI tests.
    enum Backdrop: String, Sendable { case footage, poster, plain }

    struct Rendered: Sendable {
        let image: CGImage
        let backdrop: Backdrop
    }

    static let pixelWidth = 540

    @MainActor
    static func render(_ preview: ChoiceQuestion.Preview, projectID: UUID?) async -> Rendered? {
        guard let fonts = Bundle.main.url(forResource: "fonts", withExtension: nil) else { return nil }
        let fontURLs = preview.fonts.mapValues { fonts.appendingPathComponent($0) }
        var backdrop = Backdrop.plain
        var picture: CGImage?
        if let original = ClipPreviewSource.localURL(mediaID: preview.mediaID, projectID: projectID),
           let frame = await frame(of: original, at: preview.atSeconds, isPhoto: preview.isPhoto) {
            picture = frame
            backdrop = .footage
        } else if let poster = await CreationMediaPreview.image(mediaID: preview.mediaID, projectID: projectID)?.cgImage {
            picture = poster
            backdrop = .poster
        }
        let layers = preview.layers, time = preview.timeSeconds, drawnOver = picture
        let image = await Task.detached(priority: .userInitiated) {
            try? TextLayerStill.render(layers: layers, fontURLs: fontURLs, over: drawnOver, canvas: .vertical1080,
                                       time: time, width: pixelWidth)
        }.value
        return image.map { Rendered(image: $0, backdrop: backdrop) }
    }

    private static func frame(of url: URL, at seconds: Double, isPhoto: Bool) async -> CGImage? {
        if isPhoto {
            guard let image = UIImage(contentsOfFile: url.path), image.size.width > 0, image.size.height > 0 else { return nil }
            // Drawn once so the photo's EXIF orientation is applied, as an upright picture no larger than the canvas.
            let scale = min(1, 1920 / max(image.size.width, image.size.height))
            let size = CGSize(width: (image.size.width * scale).rounded(), height: (image.size.height * scale).rounded())
            let format = UIGraphicsImageRendererFormat.default()
            format.scale = 1
            return UIGraphicsImageRenderer(size: size, format: format).image { _ in
                image.draw(in: CGRect(origin: .zero, size: size))
            }.cgImage
        }
        let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
        generator.appliesPreferredTrackTransform = true
        generator.maximumSize = CGSize(width: 1080, height: 1920)
        generator.requestedTimeToleranceBefore = CMTime(seconds: 0.25, preferredTimescale: 600)
        generator.requestedTimeToleranceAfter = CMTime(seconds: 0.25, preferredTimescale: 600)
        return try? await generator.image(at: CMTime(seconds: seconds, preferredTimescale: 600)).image
    }
}

/// The design options as a strip of 9:16 stills, the format picker's card shape (`FormatStage`): each tile is the
/// creator's own clip in that design, its name under it, the agent's default marked Recommended. Tapping a tile
/// answers the question; "Let Kria decide" and "Type my own" stay under the strip.
struct DesignPreviewStrip: View {
    let question: ChoiceQuestion
    let projectID: UUID?
    let isSending: Bool
    var isLocked = false
    var typeMyOwn: (() -> Void)?
    var allowsDecide = true
    let submit: (ChoiceQuestion.Option) -> Void

    @State private var tappedKey: String?

    static let tile = CGSize(width: 144, height: 256)

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(alignment: .top, spacing: 10) {
                    ForEach(question.options) { option in
                        DesignPreviewTile(option: option, projectID: projectID,
                                          isSending: isSending && tappedKey == option.key) {
                            tappedKey = option.key
                            submit(option)
                        }
                        .disabled(isSending || isLocked)
                    }
                }
            }
            .accessibilityIdentifier("design-preview-strip")
            KriaAnswerStack(options: [], isSending: isSending, isLocked: isLocked, typeMyOwn: typeMyOwn,
                            decide: decideOption.map { option in { tappedKey = option.key; submit(option) } }) { _ in }
        }
    }

    private var decideOption: ChoiceQuestion.Option? {
        allowsDecide ? question.options.first(where: \.recommended) : nil
    }
}

private struct DesignPreviewTile: View {
    let option: ChoiceQuestion.Option
    let projectID: UUID?
    let isSending: Bool
    let select: () -> Void
    @State private var rendered: DesignPreviewRenderer.Rendered?
    @State private var finished = false

    var body: some View {
        Button(action: select) {
            VStack(alignment: .leading, spacing: 8) {
                ZStack(alignment: .topLeading) {
                    still
                        .frame(width: DesignPreviewStrip.tile.width, height: DesignPreviewStrip.tile.height)
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    if option.recommended {
                        Text("Recommended").kriaType(.caption).fontWeight(.semibold).fixedSize()
                            .foregroundStyle(KriaColor.ink)
                            .padding(.horizontal, 8).padding(.vertical, 2)
                            .background(KriaColor.butter, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .padding(8)
                    }
                    if isSending {
                        KriaSpinner(size: 18, tint: KriaColor.paper)
                            .frame(width: DesignPreviewStrip.tile.width, height: DesignPreviewStrip.tile.height)
                    }
                }
                Text(option.label).font(KriaFont.headline(16)).foregroundStyle(KriaColor.ink)
                    .lineLimit(2).fixedSize(horizontal: false, vertical: true)
            }
            .frame(width: DesignPreviewStrip.tile.width, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel([option.label, option.detail, option.recommended ? String(localized: "recommended") : nil]
            .compactMap { $0 }.joined(separator: ", "))
        .accessibilityValue(previewState)
        .accessibilityIdentifier("choice-option-\(option.key)")
        .task(id: option.key) {
            guard let preview = option.preview else { finished = true; return }
            rendered = await DesignPreviewRenderer.render(preview, projectID: projectID)
            finished = true
        }
    }

    @ViewBuilder private var still: some View {
        if let rendered {
            Image(decorative: rendered.image, scale: 1).resizable().scaledToFill()
        } else {
            ZStack {
                KriaColor.ink
                if !finished { KriaSpinner(size: 18, tint: KriaColor.paper) }
            }
        }
    }

    /// "Preview drawn over your clip" (or its poster, or no picture), or that none could be drawn.
    private var previewState: String {
        switch rendered?.backdrop {
        case .footage: String(localized: "Preview drawn over your clip")
        case .poster: String(localized: "Preview drawn over your clip's poster")
        case .plain: String(localized: "Preview drawn without your clip")
        case nil: finished ? String(localized: "No preview") : String(localized: "Drawing preview")
        }
    }
}
