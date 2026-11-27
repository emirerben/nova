import AVFoundation
import KriaMediaEngine
import SwiftUI
import UIKit

/// KRI-1582: one design option drawn on this iPhone: the server's text layers over a frame of the creator's own
/// clip, through the media engine's text pass (`TextLayerStill`), so the still shows what the export will draw.
enum DesignPreviewRenderer {
    /// What the still was drawn over, reported to VoiceOver and to UI tests.
    enum Backdrop: String, Sendable {
        case footage, poster, plain

        var spoken: String {
            switch self {
            case .footage: String(localized: "Preview drawn over your clip")
            case .poster: String(localized: "Preview drawn over your clip's poster")
            case .plain: String(localized: "Preview drawn without your clip")
            }
        }
    }

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

/// Where the strip's tiles sit: 96x136 stills in a 6pt wrap, so three fit a card down to a small iPhone and a fourth
/// scrolls in at full size.
enum DesignStripLayout {
    static let gap: CGFloat = 2
    static let wrap: CGFloat = 6
    static let still = CGSize(width: 96, height: 136)
    static let tileOuterWidth = still.width + 2 * wrap

    /// `stripWidth` is the card's inner width plus the wrap it bleeds into the card padding (0 until measured).
    static func tileWidth(stripWidth: CGFloat, count: Int) -> CGFloat {
        guard (1...3).contains(count), stripWidth > 0 else { return tileOuterWidth }
        return min(tileOuterWidth, floor((stripWidth - gap * CGFloat(count - 1)) / CGFloat(count)))
    }
}

/// The creator's pick in a strip: which style is selected, which one is out for sending, and which one a refused
/// send leaves behind for Try again. A tile only selects; the primary sends.
struct DesignPick: Equatable {
    private(set) var selected: String
    private(set) var sent: String?
    private(set) var failed: String?

    init(_ question: ChoiceQuestion) {
        selected = (question.options.first(where: \.recommended) ?? question.options[0]).key
    }

    var isSubmitting: Bool { sent != nil }

    mutating func select(_ key: String) {
        guard sent == nil else { return }
        selected = key
        failed = nil
    }

    mutating func send() -> String {
        sent = selected
        failed = nil
        return selected
    }

    mutating func sendEnded() {
        guard let key = sent else { return }
        sent = nil
        failed = key
    }

    mutating func retry() -> String? {
        guard let key = failed else { return nil }
        failed = nil
        sent = key
        return key
    }

    func opacity(of key: String) -> Double { sent != nil && key != sent ? 0.4 : 1 }

    func primaryTitle(in question: ChoiceQuestion) -> String {
        let name = question.option(key: sent ?? selected)?.label ?? ""
        return sent == nil ? String(localized: "Use \(name)") : String(localized: "Applying \(name)")
    }
}

extension ChoiceQuestion {
    /// The style the receipt draws: the option the creator's answer names, when it has a preview. A typed answer or
    /// "Let Kria decide" names none, so its receipt is the plain capsule.
    func previewedOption(answered summary: String) -> Option? {
        options.first { $0.label == summary && $0.preview != nil }
    }
}

/// The design options as a strip of stills in the question card: each tile is the creator's own clip in that design with its
/// name under it. A tile selects, the primary "Use <style>" sends it, and "Type my own" stays under the strip.
struct DesignPreviewStrip: View {
    let question: ChoiceQuestion
    let projectID: UUID?
    let isSending: Bool
    var isLocked = false
    var typeMyOwn: (() -> Void)?
    let submit: (ChoiceQuestion.Option, String) -> Void

    @State private var pick: DesignPick
    @State private var stripWidth: CGFloat = 0

    init(question: ChoiceQuestion, projectID: UUID?, isSending: Bool, isLocked: Bool = false,
         typeMyOwn: (() -> Void)? = nil, submit: @escaping (ChoiceQuestion.Option, String) -> Void) {
        self.question = question
        self.projectID = projectID
        self.isSending = isSending
        self.isLocked = isLocked
        self.typeMyOwn = typeMyOwn
        self.submit = submit
        _pick = State(initialValue: DesignPick(question))
    }

    var body: some View {
        KriaQuestionCard {
            strip
            VStack(spacing: 8) {
                if pick.failed != nil {
                    KriaInlineFailure(text: failureText, announces: false).accessibilityIdentifier("answer-send-failed")
                    Button("Try again") { if let key = pick.retry() { send(key) } }
                        .buttonStyle(KriaButtonStyle(role: .ink, fullWidth: true))
                        .disabled(isSending || isLocked)
                        .accessibilityIdentifier("answer-try-again")
                } else {
                    Button(pick.primaryTitle(in: question)) { send(pick.send()) }
                        .buttonStyle(KriaButtonStyle(role: .primary, fullWidth: true, isLoading: pick.isSubmitting))
                        .disabled(isSending || isLocked)
                        .accessibilityIdentifier("choice-use-style")
                }
                if let typeMyOwn {
                    Button("Type my own", action: typeMyOwn)
                        .buttonStyle(KriaButtonStyle(role: .tertiary, size: .medium, fullWidth: true))
                        .disabled(isSending || isLocked)
                        .accessibilityIdentifier("answer-type-my-own")
                }
            }
        }
        .onChange(of: isSending) { wasSending, nowSending in
            guard wasSending, !nowSending, pick.isSubmitting else { return }
            pick.sendEnded()
            KriaAnnouncements.post(failureText, highPriority: true, repeatsAfter: 0)
        }
    }

    private var failureText: String { String(localized: "Couldn’t apply that style.") }

    private var strip: some View {
        let width = DesignStripLayout.tileWidth(stripWidth: stripWidth, count: question.options.count)
        return ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: DesignStripLayout.gap) {
                ForEach(question.options) { option in
                    DesignPreviewTile(option: option, projectID: projectID, width: width,
                                      isSelected: pick.selected == option.key, opacity: isLocked ? 0.4 : pick.opacity(of: option.key)) {
                        pick.select(option.key)
                    }
                    .disabled(isSending || isLocked || pick.isSubmitting)
                }
            }
        }
        .padding(.horizontal, -DesignStripLayout.wrap)
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { stripWidth = $0 }
        .accessibilityIdentifier("design-preview-strip")
    }

    private func send(_ key: String) {
        guard let option = question.option(key: key) else { return }
        submit(option, question.message(for: option))
    }
}

/// The answered receipt of a style question: the style she chose as a small still inside the "You chose" bubble,
/// then its name. Falls back to the plain capsule when the still cannot be drawn.
struct DesignYouChose: View {
    let option: ChoiceQuestion.Option
    let projectID: UUID?
    @State private var rendered: DesignPreviewRenderer.Rendered?
    @State private var finished = false

    var body: some View {
        if finished, rendered == nil {
            KriaYouChose(text: option.label)
        } else {
            VStack(alignment: .trailing, spacing: 8) {
                Text("You chose").kriaType(.caption).foregroundStyle(KriaColor.mutedInk)
                VStack(spacing: 6) {
                    Group {
                        if let rendered {
                            Image(decorative: rendered.image, scale: 1).resizable().scaledToFill()
                        } else {
                            KriaColor.mutedInk
                        }
                    }
                    .frame(width: 84, height: 112)
                    .clipShape(RoundedRectangle(cornerRadius: 14, style: .continuous))
                    Text(option.label).kriaType(.label).foregroundStyle(KriaColor.paper)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 84)
                        .padding(.bottom, 4)
                }
                .padding(8)
                .background(KriaColor.ink, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("You chose: \(option.label)")
            .accessibilityValue(rendered?.backdrop.spoken ?? "")
            .task {
                guard let preview = option.preview else { finished = true; return }
                rendered = await DesignPreviewRenderer.render(preview, projectID: projectID)
                finished = true
            }
        }
    }
}

/// Plain, without the system's disabled dimming: the strip sets each tile's own opacity, so the chosen tile stays at
/// full strength while its siblings fall to 40 percent.
private struct DesignTileButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label.opacity(configuration.isPressed ? 0.85 : 1)
    }
}

private struct DesignPreviewTile: View {
    let option: ChoiceQuestion.Option
    let projectID: UUID?
    let width: CGFloat
    let isSelected: Bool
    let opacity: Double
    let select: () -> Void
    @State private var rendered: DesignPreviewRenderer.Rendered?
    @State private var finished = false

    private var stillSize: CGSize {
        let w = width - 2 * DesignStripLayout.wrap
        return CGSize(width: w, height: (w * DesignStripLayout.still.height / DesignStripLayout.still.width).rounded())
    }

    var body: some View {
        Button(action: select) {
            VStack(alignment: .leading, spacing: 6) {
                still
                    .frame(width: stillSize.width, height: stillSize.height)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                    .overlay(alignment: .topTrailing) {
                        if isSelected {
                            Image(systemName: "checkmark").font(.system(size: 11, weight: .bold)).foregroundStyle(KriaColor.paper)
                                .frame(width: 22, height: 22).background(KriaColor.ink, in: Circle())
                                .padding(6)
                        }
                    }
                Text(option.label).kriaType(.label).fontWeight(isSelected ? .semibold : .medium)
                    .foregroundStyle(KriaColor.ink)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 2)
            }
            .padding(DesignStripLayout.wrap)
            .frame(width: width, alignment: .topLeading)
            .background(isSelected ? KriaColor.selectionSoft : Color.clear, in: RoundedRectangle(cornerRadius: 18, style: .continuous))
            .opacity(opacity)
            .contentShape(Rectangle())
        }
        .buttonStyle(DesignTileButtonStyle())
        .accessibilityLabel([option.label, option.detail, option.recommended ? String(localized: "recommended") : nil]
            .compactMap { $0 }.joined(separator: ", "))
        .accessibilityValue(previewState)
        .accessibilityAddTraits(isSelected ? .isSelected : [])
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
        rendered?.backdrop.spoken ?? (finished ? String(localized: "No preview") : String(localized: "Drawing preview"))
    }
}
