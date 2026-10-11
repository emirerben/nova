import SwiftUI
import UIKit

/// Fixed 144pt, lens-adjacent reading surface. Timeline ticks only this small
/// subtree; capture and the creation workspace are not invalidated per frame.
struct TeleprompterScriptWidget: View {
    @ObservedObject var model: TeleprompterScript
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var layout: [TeleprompterVisualLine] = []
    @State private var travelDistance: CGFloat = 0
    @State private var dragStartScroll: CGFloat?

    private let height: CGFloat = 144
    private let inset: CGFloat = 16

    var body: some View {
        SwiftUI.TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: !model.isPlaying)) { context in
            GeometryReader { proxy in
                let font = effectiveFont
                let marker = inset + font.lineHeight / 2
                ZStack(alignment: .topLeading) {
                    teleprompterBackground
                    ForEach(layout) { line in
                        scriptLine(line, font: font, width: proxy.size.width, marker: marker)
                    }
                    // The marker stays beside the reading line rather than drawing
                    // through its words, which also keeps it quiet in camera preview.
                    RoundedRectangle(cornerRadius: 2).fill(KriaColor.sky.opacity(0.9))
                        .frame(width: 4, height: font.lineHeight)
                        .offset(x: inset - 8, y: marker - font.lineHeight / 2)
                }
                .clipped()
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 1).onChanged { value in
                    if dragStartScroll == nil { dragStartScroll = model.scrollPosition }
                    model.setScrollPosition((dragStartScroll ?? model.scrollPosition) - value.translation.height)
                }.onEnded { _ in dragStartScroll = nil })
                .onAppear { updateLayout(width: proxy.size.width) }
                .onChange(of: proxy.size.width) { _, width in updateLayout(width: width) }
                .onChange(of: model.text) { _, _ in updateLayout(width: proxy.size.width) }
                .onChange(of: model.fontSize) { _, _ in updateLayout(width: proxy.size.width) }
                .onChange(of: dynamicTypeSize) { _, _ in updateLayout(width: proxy.size.width) }
                .onChange(of: context.date) { _, _ in model.tick() }
            }
        }
        .frame(height: height)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Script")
        .accessibilityValue(accessibilityValue)
        .accessibilityAction(named: "Previous line") { model.backLine() }
        .accessibilityAction(named: "Next line") { model.nextLine() }
        .accessibilityAdjustableAction { direction in
            switch direction { case .increment: model.nextLine(); case .decrement: model.backLine(); @unknown default: break }
        }
    }

    private func scriptLine(_ line: TeleprompterVisualLine, font: UIFont, width: CGFloat, marker: CGFloat) -> some View {
        Text(line.text)
            .font(.custom(font.fontName, fixedSize: font.pointSize))
            .foregroundStyle(.white)
            .lineLimit(1)
            .frame(width: max(0, width - inset * 2), alignment: .leading)
            .position(x: width / 2, y: marker + line.anchor - model.scrollPosition)
    }

    @ViewBuilder private var teleprompterBackground: some View {
        if KriaTransparency.isReduced(reduceTransparency) {
            Color.black.opacity(0.92)
        } else {
            Rectangle().fill(.ultraThinMaterial).overlay(Color.black.opacity(0.54))
        }
    }

    private var effectiveFont: UIFont {
        TeleprompterTextLayout.scaledInterFont(size: model.fontSize, dynamicTypeSize: dynamicTypeSize)
    }
    private var accessibilityValue: String {
        guard let index = TeleprompterVisualLine.index(containing: model.anchorUTF16, in: layout) else { return "No script" }
        return "\(layout[index].text). Line \(index + 1) of \(layout.count)"
    }

    private func updateLayout(width: CGFloat) {
        let font = effectiveFont
        let result = TeleprompterTextLayout.lines(text: model.text, width: max(1, width - inset * 2), font: font)
        layout = result
        travelDistance = max(0, CGFloat(max(0, result.count - 1)) * font.lineHeight)
        model.setLayout(lines: result, travelDistance: travelDistance)
    }
}

enum TeleprompterTextLayout {
    static func scaledInterFont(size: CGFloat, dynamicTypeSize: DynamicTypeSize) -> UIFont {
        let base = UIFont(name: "Inter-Regular", size: size) ?? UIFont(name: "Inter", size: size) ?? .systemFont(ofSize: size)
        let traits = UITraitCollection(preferredContentSizeCategory: contentSizeCategory(for: dynamicTypeSize))
        return UIFontMetrics(forTextStyle: .body).scaledFont(for: base, compatibleWith: traits)
    }

    static func lines(text: String, width: CGFloat, font: UIFont) -> [TeleprompterVisualLine] {
        guard !text.isEmpty else { return [] }
        let storage = NSTextStorage(string: text, attributes: [.font: font])
        let layout = NSLayoutManager()
        let container = NSTextContainer(size: CGSize(width: width, height: .greatestFiniteMagnitude))
        container.lineFragmentPadding = 0
        layout.addTextContainer(container)
        storage.addLayoutManager(layout)
        let glyphRange = layout.glyphRange(for: container)
        var result: [TeleprompterVisualLine] = []
        layout.enumerateLineFragments(forGlyphRange: glyphRange) { _, _, _, glyphRange, _ in
            let characterRange = layout.characterRange(forGlyphRange: glyphRange, actualGlyphRange: nil)
            let ns = text as NSString
            let lineText = ns.substring(with: characterRange).trimmingCharacters(in: .newlines)
            result.append(.init(range: characterRange.location..<(characterRange.location + characterRange.length), anchor: CGFloat(result.count) * font.lineHeight, text: lineText))
        }
        return result
    }

    private static func contentSizeCategory(for size: DynamicTypeSize) -> UIContentSizeCategory {
        switch size {
        case .xSmall: return .extraSmall
        case .small: return .small
        case .medium: return .medium
        case .large: return .large
        case .xLarge: return .extraLarge
        case .xxLarge: return .extraExtraLarge
        case .xxxLarge: return .extraExtraExtraLarge
        case .accessibility1: return .accessibilityMedium
        case .accessibility2: return .accessibilityLarge
        case .accessibility3: return .accessibilityExtraLarge
        case .accessibility4: return .accessibilityExtraExtraLarge
        case .accessibility5: return .accessibilityExtraExtraExtraLarge
        @unknown default: return .large
        }
    }
}
