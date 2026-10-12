import SwiftUI
import UIKit

/// The reading surface is kept independent from camera capture. Timeline ticks only this small
/// subtree; moving or resizing it never changes the recording session.
struct TeleprompterScriptWidget: View {
    @ObservedObject var model: TeleprompterScript
    let preference: TeleprompterWindowPreference
    let canvas: CGRect
    let setPreference: (TeleprompterWindowPreference) -> Void
    let persist: () -> Void
    let openSettings: () -> Void
    @Environment(\.accessibilityReduceTransparency) private var reduceTransparency
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var layout: [TeleprompterVisualLine] = []
    @State private var travelDistance: CGFloat = 0
    @State private var dragStartScroll: CGFloat?
    @State private var moveStartPreference: TeleprompterWindowPreference?
    @State private var resizeStartPreference: TeleprompterWindowPreference?

    private let inset: CGFloat = 16
    private let headerHeight: CGFloat = 44
    private let trailingGutter: CGFloat = 44

    static func minimumHeight(fontSize: CGFloat, dynamicTypeSize: DynamicTypeSize) -> CGFloat {
        let font = TeleprompterTextLayout.scaledInterFont(size: fontSize, dynamicTypeSize: dynamicTypeSize)
        return max(TeleprompterWindowGeometry.minimumSize.height, 44 + 32 + font.lineHeight)
    }

    private var requiredHeight: CGFloat { Self.minimumHeight(fontSize: model.fontSize, dynamicTypeSize: dynamicTypeSize) }

    var body: some View {
        SwiftUI.TimelineView(.animation(minimumInterval: 1.0 / 30.0, paused: !model.isPlaying)) { context in
            GeometryReader { proxy in
                let font = effectiveFont
                let marker = inset + font.lineHeight / 2
                ZStack(alignment: .topLeading) {
                    teleprompterBackground
                    ZStack(alignment: .topLeading) {
                        ForEach(layout) { line in
                            scriptLine(line, font: font, width: proxy.size.width, marker: marker)
                        }
                        // The marker stays beside the reading line rather than drawing
                        // through its words, which also keeps it quiet in camera preview.
                        RoundedRectangle(cornerRadius: 2).fill(KriaColor.sky.opacity(0.9))
                            .frame(width: 4, height: font.lineHeight)
                            .offset(x: inset - 8, y: marker - font.lineHeight / 2)
                    }
                    .frame(width: proxy.size.width, height: max(0, proxy.size.height - headerHeight), alignment: .topLeading)
                    .clipped()
                    .contentShape(Rectangle())
                    .accessibilityIdentifier("teleprompter-window-text")
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
                    .offset(y: headerHeight)

                    moveGrip
                    resizeGrip.offset(x: max(0, proxy.size.width - 44), y: max(0, proxy.size.height - 44))
                }
                .clipShape(RoundedRectangle(cornerRadius: 22, style: .continuous))
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Script")
        .accessibilityValue("\(preference.appearance.rawValue.capitalized), \(accessibilityValue)")
        .accessibilityIdentifier("teleprompter-window")
        .accessibilityAction(named: "Previous line") { model.backLine() }
        .accessibilityAction(named: "Next line") { model.nextLine() }
        .accessibilityAdjustableAction { direction in
            switch direction { case .increment: model.nextLine(); case .decrement: model.backLine(); @unknown default: break }
        }
    }

    private func scriptLine(_ line: TeleprompterVisualLine, font: UIFont, width: CGFloat, marker: CGFloat) -> some View {
        let textWidth = max(1, width - inset * 2 - trailingGutter)
        return Text(line.text)
            .font(.custom(font.fontName, fixedSize: font.pointSize))
            .foregroundStyle(preference.appearance == .transparent ? KriaColor.paper : KriaColor.ink)
            .shadow(color: KriaColor.ink.opacity(preference.appearance == .transparent ? 0.9 : 0.16), radius: 1.2, x: 0, y: 1)
            .lineLimit(1)
            .frame(width: textWidth, alignment: .leading)
            .position(x: inset + textWidth / 2, y: marker + line.anchor - model.scrollPosition)
    }

    @ViewBuilder private var teleprompterBackground: some View {
        if preference.appearance == .transparent {
            Color.clear
        } else if preference.appearance == .solid || KriaTransparency.isReduced(reduceTransparency) {
            KriaColor.paper
        } else {
            Color.clear.kriaFloatingSurface(RoundedRectangle(cornerRadius: 22, style: .continuous))
        }
    }

    private var moveGrip: some View {
        KriaFloatingIconButton(kind: .move, label: "Move text window", identifier: "teleprompter-window-move", action: openSettings)
            .highPriorityGesture(DragGesture(minimumDistance: 3, coordinateSpace: .global)
                .onChanged { value in
                    if moveStartPreference == nil { moveStartPreference = preference }
                    guard let start = moveStartPreference else { return }
                    setPreference(TeleprompterWindowGeometry.moving(start, in: canvas, by: value.translation, minimumHeight: requiredHeight))
                }
                .onEnded { _ in moveStartPreference = nil; persist() })
    }

    private var resizeGrip: some View {
        KriaFloatingIconButton(kind: .resize, label: "Resize text window", identifier: "teleprompter-window-resize", action: openSettings)
            .highPriorityGesture(DragGesture(minimumDistance: 3, coordinateSpace: .global)
                .onChanged { value in
                    if resizeStartPreference == nil { resizeStartPreference = preference }
                    guard let start = resizeStartPreference else { return }
                    setPreference(TeleprompterWindowGeometry.resizing(start, in: canvas, by: value.translation, minimumHeight: requiredHeight))
                }
                .onEnded { _ in resizeStartPreference = nil; persist() })
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
        let result = TeleprompterTextLayout.lines(text: model.text, width: max(1, width - inset * 2 - trailingGutter), font: font)
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
