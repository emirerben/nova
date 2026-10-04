import SwiftUI

// KRI-306: the creator picks the finished video's shape -- Vertical 9:16 or
// Landscape 16:9 -- and, when Vertical, how sideways clips sit in the frame
// (black bars or crop). The server decides what is on offer (`render_shape`
// on the creation thread, or the `orientation` / `landscape_fit` editor
// capabilities); this file only models, validates and draws that offer.

/// One concrete choice. `orientation` is "portrait" | "landscape" and
/// `landscapeFit` is "fit" (black bars) | "fill" (crop) -- the server's wire values.
struct RenderShapeChoice: Equatable, Sendable {
    var orientation: String
    var landscapeFit: String

    static let portraitFit = RenderShapeChoice(orientation: RenderShapeOffer.portrait, landscapeFit: RenderShapeOffer.fit)
}

/// `CreationThread.render_shape`: what the server will let the creator choose
/// before the render starts. Decoding is deliberately tolerant -- an unknown
/// value is dropped and an offer with nothing usable fails to decode, which the
/// thread turns into `nil` (picker hidden, nothing sent). That is also exactly
/// what an older server produces, so old and new backends behave the same.
struct RenderShapeOffer: Codable, Equatable, Sendable {
    static let portrait = "portrait", landscape = "landscape"
    static let fit = "fit", fill = "fill"

    let orientations: [String]
    let fitChoices: [String]
    let defaultChoice: RenderShapeChoice

    private enum CodingKeys: String, CodingKey { case orientations, fitChoices = "fit_choices", defaultChoice = "default" }
    private struct DefaultWire: Codable {
        let outputOrientation: String?
        let landscapeFit: String?
        enum CodingKeys: String, CodingKey { case outputOrientation = "output_orientation", landscapeFit = "landscape_fit" }
    }

    init(orientations: [String], fitChoices: [String], defaultChoice: RenderShapeChoice? = nil) throws {
        let knownOrientations = Self.unique(orientations.filter { [Self.portrait, Self.landscape].contains($0) })
        guard let firstOrientation = knownOrientations.first else { throw RenderShapeOfferError.nothingOffered }
        let knownFits = Self.unique(fitChoices.filter { [Self.fit, Self.fill].contains($0) })
        self.orientations = knownOrientations
        self.fitChoices = knownFits
        let orientation = defaultChoice.map(\.orientation).flatMap { knownOrientations.contains($0) ? $0 : nil } ?? firstOrientation
        let fit = defaultChoice.map(\.landscapeFit).flatMap { knownFits.contains($0) ? $0 : nil }
            ?? (knownFits.contains(Self.fit) ? Self.fit : knownFits.first ?? Self.fit)
        self.defaultChoice = RenderShapeChoice(orientation: orientation, landscapeFit: fit)
    }

    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        let wire = try? values.decodeIfPresent(DefaultWire.self, forKey: .defaultChoice)
        try self.init(
            orientations: (try? values.decodeIfPresent([String].self, forKey: .orientations)) ?? [],
            fitChoices: (try? values.decodeIfPresent([String].self, forKey: .fitChoices)) ?? [],
            defaultChoice: wire.map { RenderShapeChoice(orientation: $0.outputOrientation ?? "", landscapeFit: $0.landscapeFit ?? "") }
        )
    }

    /// `CreationThread` is Codable, so the offer re-encodes in the wire shape it decodes.
    func encode(to encoder: Encoder) throws {
        var values = encoder.container(keyedBy: CodingKeys.self)
        try values.encode(orientations, forKey: .orientations)
        try values.encode(fitChoices, forKey: .fitChoices)
        try values.encode(DefaultWire(outputOrientation: defaultChoice.orientation, landscapeFit: defaultChoice.landscapeFit), forKey: .defaultChoice)
    }

    private static func unique(_ values: [String]) -> [String] {
        var seen = Set<String>()
        return values.filter { seen.insert($0).inserted }
    }

    var offersOrientation: Bool { orientations.count > 1 }
    var offersFit: Bool { fitChoices.count > 1 }
    /// Nothing for the creator to decide: the picker stays hidden.
    var isChoosable: Bool { offersOrientation || offersFit }

    /// The fit row only applies to a vertical frame; landscape output always crops.
    func showsFitRow(for choice: RenderShapeChoice) -> Bool { offersFit && choice.orientation != Self.landscape }

    /// `choice` coerced to what is on offer, so a stale override can never be sent.
    func normalized(_ choice: RenderShapeChoice?) -> RenderShapeChoice {
        guard let choice else { return defaultChoice }
        return RenderShapeChoice(
            orientation: orientations.contains(choice.orientation) ? choice.orientation : defaultChoice.orientation,
            landscapeFit: fitChoices.contains(choice.landscapeFit) ? choice.landscapeFit : defaultChoice.landscapeFit
        )
    }

    /// The request keys for `choice`. A key is present only for a dimension the
    /// server offered, so nothing the backend did not ask for is ever sent.
    func wire(for choice: RenderShapeChoice) -> (orientation: String?, landscapeFit: String?) {
        let value = normalized(choice)
        return (
            offersOrientation ? value.orientation : nil,
            showsFitRow(for: value) ? value.landscapeFit : nil
        )
    }

    /// The same keys as v1 thread-action payload entries.
    func payload(for choice: RenderShapeChoice) -> [String: JSONValue] {
        let wire = wire(for: choice)
        var result: [String: JSONValue] = [:]
        if let orientation = wire.orientation { result["output_orientation"] = .string(orientation) }
        if let fit = wire.landscapeFit { result["landscape_fit"] = .string(fit) }
        return result
    }
}

enum RenderShapeOfferError: Error { case nothingOffered }

/// The creator's pending pick on the confirm screen. `scope` is the approval
/// (v2) or thread (v1) the pick belongs to; a new scope drops the override so the
/// picker re-seeds from the server's `default` instead of carrying a stale choice
/// onto a different approval.
struct RenderShapePickerState: Equatable {
    private(set) var scope: String?
    private(set) var override: RenderShapeChoice?

    mutating func select(_ choice: RenderShapeChoice, scope newScope: String?) {
        scope = newScope
        override = choice
    }

    mutating func reset(scope newScope: String?) {
        guard newScope != scope else { return }
        scope = newScope
        override = nil
    }

    func choice(for offer: RenderShapeOffer, scope current: String?) -> RenderShapeChoice {
        offer.normalized(current == scope ? override : nil)
    }
}

/// Words for the picker, shared by the confirm card and the editor.
enum VideoShapeCopy {
    static func orientationTitle(_ orientation: String) -> String {
        orientation == RenderShapeOffer.landscape ? "Landscape 16:9" : "Vertical 9:16"
    }
    static func fitTitle(_ fit: String) -> String { fit == RenderShapeOffer.fill ? "Crop" : "Black bars" }
    static func editorOrientationNote(_ orientation: String) -> String {
        orientation == RenderShapeOffer.landscape ? "Wide frame for YouTube and the web." : "Tall frame for TikTok, Reels and Shorts."
    }
}

/// A two-option choice drawn as a pill pair. Each segment is a 44pt-minimum touch
/// target, announces its selected state, and carries a stable identifier.
private struct VideoShapeSegments: View {
    let title: String
    let options: [String]
    let selection: String
    let label: (String) -> String
    var glyph: ((String) -> AnyView)? = nil
    let isEnabled: Bool
    let identifierPrefix: String
    let select: (String) -> Void
    @Environment(\.dynamicTypeSize) private var typeSize

    /// At accessibility sizes a half-width pill cannot hold its label: stack the options and let
    /// the text wrap, keeping every target at least 44pt tall.
    private var stacked: Bool { typeSize.isAccessibilitySize }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(KriaFont.body(12).weight(.semibold))
                .foregroundStyle(KriaColor.zinc)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityHidden(true)
            Group {
                if stacked {
                    VStack(spacing: 2) { segments }
                } else {
                    HStack(spacing: 0) { segments }
                }
            }
            .padding(3)
            .background(KriaColor.softZinc, in: stacked ? AnyShape(RoundedRectangle(cornerRadius: 22, style: .continuous)) : AnyShape(Capsule()))
            .opacity(isEnabled ? 1 : 0.55)
            .disabled(!isEnabled)
        }
    }

    @ViewBuilder private var segments: some View {
        ForEach(options, id: \.self) { option in
            let selected = option == selection
            Button { select(option) } label: {
                HStack(spacing: 6) {
                    if let glyph { glyph(option) }
                    Text(label(option))
                        .font(KriaFont.body(14).weight(selected ? .bold : .regular))
                        .lineLimit(stacked ? nil : 2)
                        .minimumScaleFactor(stacked ? 1 : 0.85)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .foregroundStyle(selected ? KriaColor.ink : KriaColor.zinc)
                .padding(.vertical, 6)
                .padding(.horizontal, 8)
                .frame(maxWidth: .infinity, minHeight: 44)
                .background(selected ? KriaColor.paper : .clear, in: stacked ? AnyShape(RoundedRectangle(cornerRadius: 19, style: .continuous)) : AnyShape(Capsule()))
                .shadow(color: .black.opacity(selected ? 0.10 : 0), radius: 4, y: 1)
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(title): \(label(option))")
            .accessibilityAddTraits(selected ? .isSelected : [])
            .accessibilityIdentifier("\(identifierPrefix)-\(option)")
        }
    }
}

/// Vertical 9:16 / Landscape 16:9, plus "Sideways clips: Black bars / Crop" when
/// the frame is Vertical and the server offers the fit choice. Used on the confirm
/// screen (before generating) and in the editor (re-render).
struct VideoShapePicker: View {
    let offer: RenderShapeOffer
    @Binding var choice: RenderShapeChoice
    var isEnabled = true
    /// The fit row's own gate; the editor's `orientation` and `landscape_fit`
    /// capabilities are independent. Defaults to `isEnabled`.
    var fitEnabled: Bool? = nil
    /// Why the picker is closed, shown under it (the editor passes the server's reason).
    var lockedReason: String? = nil
    var identifierPrefix = "video-shape"
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var motionReduced: Bool {
        reduceMotion || ProcessInfo.processInfo.environment["UI_TEST_REDUCE_MOTION"] == "1"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if offer.offersOrientation {
                VideoShapeSegments(
                    title: "Video shape",
                    options: offer.orientations,
                    selection: choice.orientation,
                    label: VideoShapeCopy.orientationTitle,
                    glyph: { orientation in AnyView(FrameGlyph(landscape: orientation == RenderShapeOffer.landscape)) },
                    isEnabled: isEnabled,
                    identifierPrefix: "\(identifierPrefix)-orientation"
                ) { orientation in update { $0.orientation = orientation } }
            }
            if offer.showsFitRow(for: choice) {
                VideoShapeSegments(
                    title: "Sideways clips",
                    options: offer.fitChoices,
                    selection: choice.landscapeFit,
                    label: VideoShapeCopy.fitTitle,
                    isEnabled: fitEnabled ?? isEnabled,
                    identifierPrefix: "\(identifierPrefix)-fit"
                ) { fit in update { $0.landscapeFit = fit } }
                .transition(motionReduced ? .identity : .opacity)
            }
            if !(isEnabled && (fitEnabled ?? true)), let lockedReason, !lockedReason.isEmpty {
                Label(lockedReason, systemImage: "lock")
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("\(identifierPrefix)-locked-reason")
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(identifierPrefix)
    }

    private func update(_ change: (inout RenderShapeChoice) -> Void) {
        var next = choice
        change(&next)
        guard next != choice else { return }
        if motionReduced { choice = next } else { withAnimation(.easeOut(duration: 0.15)) { choice = next } }
    }
}

/// A tiny outline of the frame: tall for Vertical, wide for Landscape.
private struct FrameGlyph: View {
    let landscape: Bool
    var body: some View {
        RoundedRectangle(cornerRadius: 2, style: .continuous)
            .stroke(lineWidth: 1.5)
            .frame(width: landscape ? 16 : 9, height: landscape ? 9 : 16)
            .accessibilityHidden(true)
    }
}

/// The picker as a card on the confirm screens, with a one-line reminder of what
/// the choice does. The card only exists for an offer the creator can act on.
struct VideoShapeCard: View {
    let offer: RenderShapeOffer
    @Binding var choice: RenderShapeChoice
    var isEnabled = true

    var body: some View {
        if offer.isChoosable {
            VStack(alignment: .leading, spacing: 10) {
                VideoShapePicker(offer: offer, choice: $choice, isEnabled: isEnabled)
                Text(choice.orientation == RenderShapeOffer.landscape
                     ? "Sideways clips fill the wide frame."
                     : "Pick how sideways clips sit in the tall frame.")
                    .font(KriaFont.body(12))
                    .foregroundStyle(KriaColor.zinc)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(KriaColor.softZinc.opacity(0.6))
            .overlay { RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(KriaColor.line, lineWidth: 1) }
            .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("video-shape-card")
        }
    }
}
