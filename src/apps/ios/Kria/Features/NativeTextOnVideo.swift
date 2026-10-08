import SwiftUI
import UIKit

// KRI-508 (plan 027): edit text and titles right on the video. One tap on a
// text selects it and shows its actions next to it; a second tap (or "Edit
// text") types on the video itself, with fonts, colours and a size slider
// around the preview. Everything layout-related that can be pure lives in the
// enums below so it is unit tested; the views only draw what they compute.

/// The actions offered next to a selected text on the preview.
struct NativePreviewTextActions {
    var onEdit: () -> Void
    var onStyle: () -> Void
    var onDelete: () -> Void
    /// Why Delete is unavailable (shown as its hint); nil when it works.
    var deleteBlockedReason: String?
}

/// A notice over the bottom of the preview. The token tells two notices of the same kind apart.
struct NativeEditorPreviewNoticeState: Equatable {
    enum Kind: Equatable {
        /// Typing a text empty removed it; Undo works while that removal is the newest undo step.
        case textRemoved(undoVersion: Int)
        /// A tap on a title the server keeps read-only (KRI-508 D12).
        case lockedTitle
    }
    let kind: Kind
    let token = UUID()
}

/// Where the action pill sits relative to the selected text (plan 027 D3):
/// above the text, or below it when the text is near the top, never outside the canvas.
enum NativeTextPillPlacement {
    static let gapAbove: CGFloat = 14
    static let gapBelow: CGFloat = 18
    static let edgeInset: CGFloat = 8

    /// The pill's centre in canvas points.
    static func center(selection: CGRect, pill: CGSize, canvas: CGSize) -> CGPoint {
        let halfWidth = pill.width / 2
        let x = canvas.width >= pill.width + edgeInset * 2
            ? min(max(selection.midX, edgeInset + halfWidth), canvas.width - edgeInset - halfWidth)
            : canvas.width / 2
        let above = selection.minY - gapAbove - pill.height / 2
        let below = selection.maxY + gapBelow + pill.height / 2
        let y: CGFloat
        if above - pill.height / 2 >= edgeInset {
            y = above
        } else if below + pill.height / 2 <= canvas.height - edgeInset {
            y = below
        } else {
            // A text that fills the frame: the pill sits on its top edge.
            y = edgeInset + pill.height / 2
        }
        return CGPoint(x: x, y: y)
    }

    /// The pill only appears when the preview is wide enough to hold it; a
    /// narrower preview keeps the island's text actions instead.
    static func fits(canvasWidth: CGFloat, pillWidth: CGFloat = estimatedWidth) -> Bool {
        canvasWidth >= pillWidth + edgeInset * 2
    }

    /// Edit text + Style + Delete at the default text size.
    static let estimatedWidth: CGFloat = 232
    static let height: CGFloat = 52
}

/// Text sizes a gesture or the size slider may set (plan 027 D11). A stored size
/// outside the range is kept until the creator changes it.
enum NativeTextSizeRange {
    static let minimum = 24.0
    static let maximum = 320.0

    /// Clamps a gesture's scale so the result stays in range, without jumping a
    /// text that already sits outside it.
    static func clampedScale(_ scale: Double, baseSize: Double) -> Double {
        guard baseSize > 0, scale.isFinite else { return 1 }
        let low = min(1, minimum / baseSize)
        let high = max(1, maximum / baseSize)
        return min(max(scale, low), high)
    }

    static func clamped(_ size: Double) -> Double { min(max(size, minimum), maximum) }
}

/// How the text being typed is drawn over the preview.
enum NativeTextInlineLayout {
    /// The smallest on-screen size for the typing field, so a small title stays readable while typing.
    static let minimumDisplaySize: CGFloat = 15

    /// Canvas width in output pixels for an aspect ratio (the render compiler's canvases).
    static func canvasWidth(aspectRatio: CGFloat) -> CGFloat { aspectRatio > 1.01 ? 1920 : 1080 }

    /// Point size of the typing field: the text's own size scaled to the preview, but readable and never taller than a third of it.
    static func displaySize(sizePx: Double, previewSize: CGSize, aspectRatio: CGFloat) -> CGFloat {
        let scale = previewSize.width / canvasWidth(aspectRatio: aspectRatio)
        let scaled = CGFloat(sizePx) * scale
        let ceiling = max(minimumDisplaySize, previewSize.height / 3)
        return min(max(scaled, minimumDisplaySize), ceiling)
    }

    /// A typing font size whose longest line fits `available` points (never below 12pt).
    static func fittedSize(_ size: CGFloat, longestLineAt size1: CGFloat, available: CGFloat) -> CGFloat {
        guard size1 > available, size1 > 0 else { return size }
        return max(12, floor(size * available / size1))
    }

    /// The field's frame: explicit lines only (titles never wrap), placed on the
    /// text's anchor by alignment and kept inside the preview.
    static func fieldFrame(lineWidths: [CGFloat], lineHeight: CGFloat, anchor: CGPoint, alignment: String,
                           centerY: CGFloat?, canvas: CGSize) -> CGRect {
        let padding: CGFloat = 12
        let width = min(max(48, (lineWidths.max() ?? 0) + padding * 2), max(48, canvas.width - 8))
        let height = CGFloat(max(1, lineWidths.count)) * lineHeight + 12
        let x = anchor.x * canvas.width
        let minX: CGFloat = switch alignment {
        case "left": x - padding
        case "right": x - width + padding
        default: x - width / 2
        }
        let midY = centerY ?? anchor.y * canvas.height
        let clampedX = min(max(4, minX), max(4, canvas.width - width - 4))
        let clampedY = min(max(4, midY - height / 2), max(4, canvas.height - height - 4))
        return CGRect(x: clampedX, y: clampedY, width: width, height: height)
    }
}

/// The pill of text actions drawn next to a selected text on the preview.
/// It carries the island strip's identifiers: only one of the two is shown.
struct NativeTextActionPill: View {
    let actions: NativePreviewTextActions

    var body: some View {
        HStack(spacing: 2) {
            Button(action: actions.onEdit) {
                Label("Edit text", systemImage: "character.cursor.ibeam")
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(KriaColor.plum)
                    .padding(.horizontal, 14)
                    .frame(minHeight: 44)
                    .background(KriaColor.lilac, in: Capsule())
            }
            .accessibilityHint("Type on the video")
            .accessibilityIdentifier("native-editor-text-edit-action")
            Button(action: actions.onStyle) {
                Text("Style")
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(KriaColor.ink)
                    .padding(.horizontal, 14)
                    .frame(minHeight: 44)
                    .contentShape(Capsule())
            }
            .accessibilityHint("Font, outline, shadow, position, animation and timing")
            .accessibilityIdentifier("native-editor-text-style-action")
            Button(action: actions.onDelete) {
                Text("Delete")
                    .font(KriaFont.body(13).weight(.semibold))
                    .foregroundStyle(KriaColor.failureText)
                    .padding(.horizontal, 14)
                    .frame(minHeight: 44)
                    .contentShape(Capsule())
            }
            .disabled(actions.deleteBlockedReason != nil)
            .accessibilityLabel("Delete text")
            .accessibilityHint(actions.deleteBlockedReason ?? "Removes this text. Undo brings it back.")
            .accessibilityIdentifier("native-editor-text-delete")
        }
        .buttonStyle(.plain)
        .padding(4)
        .frame(height: NativeTextPillPlacement.height)
        .background(.white.opacity(0.95), in: Capsule())
        .overlay(Capsule().stroke(.white.opacity(0.8), lineWidth: 1))
        .shadow(color: .black.opacity(0.28), radius: 14, y: 8)
        .fixedSize()
        .accessibilityElement(children: .contain)
        .background(
            Color.clear
                .accessibilityElement()
                .accessibilityLabel("Text actions")
                .accessibilityAddTraits(.isHeader)
                .accessibilityIdentifier("native-editor-text-context")
        )
    }
}

/// The 28pt resize/rotate corner of a selected text (a 44pt grab zone around it).
struct NativeTextResizeHandle: View {
    var body: some View {
        Image(systemName: "arrow.up.left.and.arrow.down.right")
            .font(.system(size: 12, weight: .bold))
            .foregroundStyle(KriaColor.ink)
            .frame(width: 28, height: 28)
            .background(KriaColor.sky, in: Circle())
            .overlay(Circle().stroke(.white, lineWidth: 2))
            .shadow(color: .black.opacity(0.3), radius: 4, y: 2)
            .frame(width: 44, height: 44)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

/// The live "Size N" readout above a text being resized (plan 027 D11).
struct NativeTextSizeReadout: View {
    let size: Int
    var body: some View {
        Text("Size \(size)")
            .font(KriaFont.body(13).weight(.semibold))
            .monospacedDigit()
            .foregroundStyle(.white)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(KriaColor.ink.opacity(0.9), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .fixedSize()
            .allowsHitTesting(false)
            .accessibilityIdentifier("native-editor-text-size-readout")
    }
}

/// Centre lines that light up while a text is moved onto them, plus the area
/// that platform buttons cover on a vertical video (plan 027 D10).
struct NativeTextMoveGuides: View {
    let showsVerticalCenter: Bool
    let showsHorizontalCenter: Bool
    let showsPlatformZones: Bool

    var body: some View {
        GeometryReader { proxy in
            let size = proxy.size
            ZStack(alignment: .topLeading) {
                if showsPlatformZones {
                    // Right column and bottom band where TikTok/Reels/Shorts draw their buttons and caption.
                    Rectangle().fill(KriaColor.ink.opacity(0.28))
                        .frame(width: size.width * 0.17, height: size.height * 0.92)
                        .offset(x: size.width * 0.83, y: size.height * 0.08)
                    Rectangle().fill(KriaColor.ink.opacity(0.28))
                        .frame(width: size.width * 0.83, height: size.height * 0.23)
                        .overlay(alignment: .bottom) {
                            Text("App buttons cover this")
                                .font(KriaFont.body(10).weight(.semibold))
                                .foregroundStyle(.white)
                                .padding(.bottom, 6)
                        }
                        .offset(y: size.height * 0.77)
                }
                if showsVerticalCenter {
                    Rectangle().fill(KriaColor.sky).frame(width: 1.5, height: size.height)
                        .shadow(color: KriaColor.ink.opacity(0.35), radius: 0.5)
                        .offset(x: size.width / 2 - 0.75)
                }
                if showsHorizontalCenter {
                    Rectangle().fill(KriaColor.sky).frame(width: size.width, height: 1.5)
                        .shadow(color: KriaColor.ink.opacity(0.35), radius: 0.5)
                        .offset(y: size.height / 2 - 0.75)
                }
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// The typing field drawn on the video at the text's own position and look.
struct NativeTextInlineField: UIViewRepresentable {
    @Binding var text: String
    let font: UIFont
    let color: UIColor
    let background: UIColor?
    let alignment: NSTextAlignment

    func makeUIView(context: Context) -> ExplicitLineTextView {
        let view = ExplicitLineTextView()
        view.delegate = context.coordinator
        view.backgroundColor = .clear
        view.keyboardAppearance = .dark
        view.returnKeyType = .default
        view.autocapitalizationType = .sentences
        view.isScrollEnabled = false
        view.textContainerInset = UIEdgeInsets(top: 6, left: 4, bottom: 6, right: 4)
        view.textContainer.widthTracksTextView = true
        view.textContainer.heightTracksTextView = false
        view.wrapsLines = true
        view.tintColor = UIColor(KriaColor.sky)
        view.layer.shadowColor = UIColor.black.cgColor
        view.layer.shadowOpacity = 0.45
        view.layer.shadowRadius = 4
        view.layer.shadowOffset = CGSize(width: 0, height: 1)
        view.accessibilityLabel = "Text"
        view.accessibilityIdentifier = "native-editor-inline-text-field"
        // The frame SwiftUI proposes wins over the text's own width (a long title must not overflow the video).
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.setContentCompressionResistancePriority(.defaultLow, for: .vertical)
        view.wantsKeyboardFocus = true
        apply(to: view)
        view.text = text
        let end = (text as NSString).length
        view.selectedRange = NSRange(location: end, length: 0)
        return view
    }

    func updateUIView(_ view: ExplicitLineTextView, context: Context) {
        context.coordinator.parent = self
        apply(to: view)
        if view.text != text { view.text = text }
        if view.window != nil, !view.isFirstResponder, view.wantsKeyboardFocus { view.becomeFirstResponder() }
    }

    private func apply(to view: ExplicitLineTextView) {
        if view.font != font { view.font = font }
        view.textColor = color
        view.textAlignment = alignment
        view.layer.cornerRadius = font.pointSize * 0.18
        view.layer.backgroundColor = background?.cgColor ?? UIColor.clear.cgColor
        // A highlight already reads on any footage; the soft glow is for bare text.
        view.layer.shadowOpacity = background == nil ? 0.45 : 0
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: ExplicitLineTextView, context: Context) -> CGSize? {
        CGSize(width: proposal.width ?? 120, height: proposal.height ?? font.lineHeight + 12)
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: NativeTextInlineField
        init(_ parent: NativeTextInlineField) { self.parent = parent }
        func textViewDidChange(_ textView: UITextView) { parent.text = textView.text }
    }

    /// Widths of each explicit line in `font` (the field's layout input).
    static func lineWidths(_ text: String, font: UIFont) -> [CGFloat] {
        let lines = text.components(separatedBy: .newlines)
        return (lines.isEmpty ? [""] : lines).map { line in
            ceil((line.isEmpty ? " " : line as NSString).size(withAttributes: [.font: font]).width) + 10
        }
    }
}

/// The bar that rides the keyboard while typing on the video (plan 027 D5):
/// fonts and Done on top, colours, background preset and alignment below.
struct NativeTextInlineBar: View {
    static let topPadding: CGFloat = 12
    static let rowSpacing: CGFloat = 8
    static let bottomPadding: CGFloat = 10
    static func height(rowHeight: CGFloat) -> CGFloat { topPadding + rowHeight * 2 + rowSpacing + bottomPadding }

    let id: String
    @ObservedObject var session: NativeEditorSession
    let rowHeight: CGFloat
    let onDone: () -> Void

    private let palette: [(hex: String, name: String)] = [
        ("#FFFFFF", "White"), ("#30352C", "Ink"), ("#FFF0A6", "Butter"), ("#9BCAFF", "Sky"), ("#E7DDF5", "Lilac")
    ]
    private let presets = ["Simple", "Bold", "Highlight"]
    private let alignments = ["center", "left", "right"]

    private var item: EditorTextElement? { session.textElement(id: id) }
    private func string(_ key: String, _ fallback: String) -> String { item?.raw[key]?.stringValue ?? fallback }
    private var currentFont: String { item?.fontFamily ?? EditorTextElement.defaultFontFamily }

    var body: some View {
        VStack(spacing: Self.rowSpacing) {
            HStack(spacing: 8) {
                ScrollViewReader { reader in
                    ScrollView(.horizontal, showsIndicators: false) {
                        LazyHStack(spacing: 6) {
                            ForEach(NativeFontCatalog.shared.pickerFonts, id: \.self) { name in
                                fontChip(name)
                            }
                        }
                        .padding(.vertical, 2)
                    }
                    .onAppear { reader.scrollTo(currentFont, anchor: .leading) }
                }
                .accessibilityElement(children: .contain)
                .accessibilityLabel("Fonts")
                Button(action: onDone) {
                    Text("Done")
                        .font(KriaFont.body(16).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                        .padding(.horizontal, 20)
                        .frame(minHeight: 44)
                        .background(KriaColor.butter, in: Capsule())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("native-editor-inline-text-done")
            }
            .frame(height: rowHeight)
            HStack(spacing: 4) {
                ForEach(palette, id: \.hex) { swatch in
                    let selected = string("color", "#FFFFFF").uppercased() == swatch.hex
                    Button { session.setTextColor(id: id, color: swatch.hex) } label: {
                        Circle().fill(nativeEditorColor(swatch.hex))
                            .overlay(Circle().stroke(KriaColor.ink.opacity(0.25), lineWidth: 1))
                            .frame(width: 28, height: 28)
                            .padding(3)
                            .overlay(Circle().stroke(selected ? KriaColor.ink : .clear, lineWidth: 2))
                            .frame(width: 36, height: 44)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Text colour \(swatch.name)")
                    .accessibilityAddTraits(selected ? .isSelected : [])
                }
                Rectangle().fill(KriaColor.line).frame(width: 1, height: 24).padding(.horizontal, 6)
                    .accessibilityHidden(true)
                Button {
                    let current = presets.firstIndex(of: string("editor_preset", "Simple")) ?? 0
                    session.applyTextPreset(id: id, preset: presets[(current + 1) % presets.count])
                } label: {
                    Text(string("editor_preset", "Simple"))
                        .font(KriaFont.body(14).weight(.semibold))
                        .foregroundStyle(KriaColor.ink)
                        .frame(minWidth: 84, minHeight: 44)
                        .padding(.horizontal, 4)
                        .background(KriaColor.softZinc, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Text background")
                .accessibilityValue(string("editor_preset", "Simple"))
                .accessibilityHint("Switches between Simple, Bold and Highlight")
                .accessibilityIdentifier("native-editor-inline-text-preset")
                Button {
                    let current = alignments.firstIndex(of: string("alignment", "center")) ?? 0
                    session.setTextAlignment(id: id, alignment: alignments[(current + 1) % alignments.count])
                } label: {
                    Image(systemName: "text.align\(string("alignment", "center"))")
                        .font(.system(size: 17, weight: .semibold))
                        .foregroundStyle(KriaColor.ink)
                        .frame(width: 44, height: 44)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Text alignment")
                .accessibilityValue(string("alignment", "center"))
                .accessibilityIdentifier("native-editor-inline-text-align")
                Spacer(minLength: 0)
            }
            .frame(height: rowHeight)
        }
        .padding(.top, Self.topPadding)
        .padding(.bottom, Self.bottomPadding)
        .padding(.horizontal, 16)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("native-editor-inline-text-bar")
    }

    private func fontChip(_ name: String) -> some View {
        let selected = name == currentFont
        return Button { session.setTextStyle(id: id, style: name) } label: {
            Text(name)
                .font(NativeFontCatalog.shared.previewFont(name, size: 15))
                .lineLimit(1)
                .foregroundStyle(selected ? Color.white : KriaColor.ink)
                .padding(.horizontal, 12)
                .frame(minHeight: 40)
                .background(selected ? KriaColor.ink : KriaColor.softZinc, in: Capsule())
        }
        .buttonStyle(.plain)
        .id(name)
        .accessibilityLabel("Font \(name)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// A vertical size slider beside the preview while typing (plan 027 D5).
/// Up is bigger. VoiceOver adjusts it in steps of 4.
struct NativeTextSizeSlider: View {
    let size: Double
    let onChange: (Double) -> Void
    var trackHeight: CGFloat = 200

    private var fraction: CGFloat {
        CGFloat((NativeTextSizeRange.clamped(size) - NativeTextSizeRange.minimum)
                / (NativeTextSizeRange.maximum - NativeTextSizeRange.minimum))
    }

    var body: some View {
        VStack(spacing: 6) {
            Text("A").font(KriaFont.display(22)).accessibilityHidden(true)
            ZStack(alignment: .bottom) {
                Capsule().fill(KriaColor.line.opacity(0.6)).frame(width: 4)
                Capsule().fill(KriaColor.ink).frame(width: 4, height: trackHeight * fraction)
                Circle().fill(.white)
                    .overlay(Circle().stroke(KriaColor.ink.opacity(0.15), lineWidth: 1))
                    .shadow(color: KriaColor.ink.opacity(0.3), radius: 4, y: 2)
                    .frame(width: 28, height: 28)
                    .offset(y: -(trackHeight * fraction) + 14)
            }
            .frame(width: 44, height: trackHeight)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                let clampedY = min(max(0, value.location.y), trackHeight)
                let next = NativeTextSizeRange.minimum
                    + Double(1 - clampedY / trackHeight) * (NativeTextSizeRange.maximum - NativeTextSizeRange.minimum)
                onChange((next / 2).rounded() * 2)
            })
            Text("A").font(KriaFont.display(12)).accessibilityHidden(true)
            Text("\(Int(size.rounded()))")
                .font(KriaFont.body(12).weight(.semibold))
                .monospacedDigit()
                .foregroundStyle(KriaColor.mutedInk)
                .accessibilityHidden(true)
        }
        .foregroundStyle(KriaColor.ink)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Text size")
        .accessibilityValue("\(Int(size.rounded()))")
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: onChange(NativeTextSizeRange.clamped(size + 4))
            case .decrement: onChange(NativeTextSizeRange.clamped(size - 4))
            @unknown default: break
            }
        }
        .accessibilityIdentifier("native-editor-inline-text-size")
    }
}

/// A short notice with an optional action, over the bottom of the preview
/// ("Text removed · Undo", "This title can't be edited here yet. · Ask Kria").
struct NativeEditorPreviewNotice: View {
    let message: String
    let actionTitle: String?
    let action: (() -> Void)?
    let identifier: String

    var body: some View {
        HStack(spacing: 8) {
            Text(message)
                .font(KriaFont.body(14))
                .foregroundStyle(.white)
                .fixedSize(horizontal: false, vertical: true)
            if let actionTitle, let action {
                Button(actionTitle, action: action)
                    .font(KriaFont.body(14).weight(.semibold))
                    .foregroundStyle(KriaColor.sky)
                    .frame(minHeight: 44)
                    .padding(.horizontal, 4)
                    .accessibilityIdentifier(identifier + "-action")
            }
        }
        .padding(.leading, 16).padding(.trailing, actionTitle == nil ? 16 : 8)
        .frame(minHeight: 44)
        .background(KriaColor.ink.opacity(0.94), in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .shadow(color: .black.opacity(0.2), radius: 12, y: 6)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier(identifier)
    }
}
