#if canImport(AVFoundation)
import CoreGraphics
import CoreText
import Foundation

/// Resolves edited text locally using the exact font file in the source manifest.
/// The resulting positioned runs feed the existing preview/export compositor.
enum NativeFontIdentity {
    static func font(_ graphics: CGFont, size: Double, variations: [String: Double]) -> CTFont {
        guard !variations.isEmpty else { return CTFontCreateWithGraphicsFont(graphics, size, nil, nil) }
        let axes = Dictionary(uniqueKeysWithValues: variations.map { name, value in
            (NSNumber(value: name.utf8.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }), NSNumber(value: value))
        })
        let descriptor = CTFontDescriptorCreateWithAttributes([kCTFontVariationAttribute: axes] as CFDictionary)
        return CTFontCreateWithGraphicsFont(graphics, size, nil, descriptor)
    }
    /// Color emoji are supplied by the OS, while ordinary text must still use
    /// the exact bundled face. CoreText draws the color glyphs through CTLine.
    static func isColorEmoji(_ resolved: CTFont) -> Bool {
        CTFontCopyPostScriptName(resolved) as String == "AppleColorEmoji"
            && CTFontGetSymbolicTraits(resolved).contains(.traitColorGlyphs)
    }
    static func matches(_ resolved: CTFont, graphics: CGFont) -> Bool {
        let actual = CTFontCopyGraphicsFont(resolved, nil)
        if CFEqual(actual, graphics) { return true }
        // CoreText instantiates variable fonts for the requested size, so the
        // graphics objects need not compare equal. Require the same named face
        // and source mapping/metrics tables to distinguish a real fallback.
        guard actual.numberOfGlyphs == graphics.numberOfGlyphs else { return false }
        return [UInt32(0x68656164), UInt32(0x636D6170), UInt32(0x6D617870), UInt32(0x6E616D65)].allSatisfy { tag in
            guard let expected = graphics.table(for: tag), let value = actual.table(for: tag) else { return false }
            return CFEqual(expected, value)
        }
    }
}

public enum AuthoredTextLayout {
    public enum Alignment: String, Sendable { case left, center, right }
    public struct Style: Sendable {
        public var wrapsLines: Bool
        public var lineSpacing: Double
        public var letterSpacing: Double
        public var fontVariations: [String: Double]
        public var fontAssetID: String
        public var size: Double
        public var widthFraction: Double
        public var xFraction: Double
        public var yFraction: Double
        public var rotation: Double
        /// KRI-646: -100...100, positive bows the lines up like a rainbow. Dropped by the compiler
        /// for effects that need a straight baseline (`PortableTextLayer.bendSupported`).
        public var bend: Double
        public var alignment: Alignment
        public var color: TextInk
        public var stroke: TextInk
        public var strokeWidth: Double
        public var shadows: [TextBlurLayer]
        public var bottomAligned: Bool
        public var background: TextInk?
        public init(fontAssetID: String, size: Double = 72, widthFraction: Double = 0.9,
                    xFraction: Double = 0.5, yFraction: Double = 0.5, rotation: Double = 0,
                    alignment: Alignment = .center, color: TextInk,
                    stroke: TextInk = .init(red: 0, green: 0, blue: 0, alpha: 1),
                    strokeWidth: Double = 0, shadows: [TextBlurLayer] = [], background: TextInk? = nil, bottomAligned: Bool = false, fontVariations: [String: Double] = [:], letterSpacing: Double = 0, lineSpacing: Double = 1.15, wrapsLines: Bool = true, bend: Double = 0) {
            self.bend = bend
            self.wrapsLines = wrapsLines
            self.lineSpacing = lineSpacing
            self.letterSpacing = letterSpacing
            self.fontVariations = fontVariations
            self.fontAssetID = fontAssetID; self.size = size; self.widthFraction = widthFraction
            self.xFraction = xFraction; self.yFraction = yFraction; self.rotation = rotation
            self.alignment = alignment; self.color = color; self.stroke = stroke
            self.strokeWidth = strokeWidth; self.shadows = shadows; self.background = background; self.bottomAligned = bottomAligned
        }
    }

    public static func compile(id: String, text: String, start: Double, end: Double,
                               style: Style, fontURL: URL, canvas: Canvas,
                               phases: TextAnimationPhases? = nil, legacyEffect: PortableTextLayer.Effect = .none,
                               motion: TextMotionParameters? = nil, revealSchedule: [Double]? = nil) throws -> PortableTextLayer {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, text.count <= 500,
              style.size.isFinite, style.size >= 8,
              style.widthFraction.isFinite, style.widthFraction >= 0.2,
              style.xFraction.isFinite, style.yFraction.isFinite, style.letterSpacing.isFinite, (-0.05...0.5).contains(style.letterSpacing),
              let provider = CGDataProvider(url: fontURL as CFURL), let graphics = CGFont(provider) else {
            throw RecipeError.invalidTimeline
        }
        try phases?.validate()
        let normalized = text.replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
        // KRI-826: the creator's own lines (`wrapsLines == false`) keep their breaks and size, but stay
        // inside the frame: the same rule as cloud `text_overlay_skia._authored_lines` (see `AuthoredTextFit`).
        let size = style.wrapsLines ? style.size : AuthoredTextFit.fittedSize(text: normalized, size: style.size,
            safeWidth: AuthoredTextFit.safeWidth(canvasWidth: Double(canvas.width))) { points in
                let font = NativeFontIdentity.font(graphics, size: points, variations: style.fontVariations)
                return [NSAttributedString.Key(kCTFontAttributeName as String): font, NSAttributedString.Key(kCTKernAttributeName as String): style.letterSpacing * points]
            }
        let font = NativeFontIdentity.font(graphics, size: size, variations: style.fontVariations)
        let attributes: [NSAttributedString.Key: Any] = [NSAttributedString.Key(kCTFontAttributeName as String): font, NSAttributedString.Key(kCTKernAttributeName as String): style.letterSpacing * size]
        let wrapWidth = style.wrapsLines ? Double(canvas.width) * style.widthFraction : AuthoredTextFit.safeWidth(canvasWidth: Double(canvas.width))
        var values: [String] = []
        for paragraph in normalized.components(separatedBy: "\n") {
            if paragraph.isEmpty || (!style.wrapsLines && AuthoredTextFit.width(paragraph, attributes: attributes) <= wrapWidth) {
                values.append(paragraph)
                continue
            }
            if !style.wrapsLines {
                values.append(contentsOf: AuthoredTextFit.wordRows(paragraph, attributes: attributes, width: wrapWidth))
                continue
            }
            let source = paragraph as NSString
            let typesetter = CTTypesetterCreateWithAttributedString(NSAttributedString(string: paragraph, attributes: attributes))
            var offset = 0
            while offset < source.length {
                let count = min(CTTypesetterSuggestLineBreak(typesetter, offset, wrapWidth), source.length - offset)
                guard count > 0 else { throw RecipeError.invalidTimeline }
                values.append(source.substring(with: NSRange(location: offset, length: count)).trimmingCharacters(in: .whitespaces))
                offset += count
            }
        }
        var lines: [(String, Double)] = []
        for value in values {
            // Blank authored lines still occupy one baseline; a space is a valid
            // drawable run without introducing visible ink.
            let value = value.isEmpty ? " " : value
            let line = CTLineCreateWithAttributedString(NSAttributedString(string: value, attributes: attributes))
            for run in CTLineGetGlyphRuns(line) as! [CTRun] {
                let resolved = (CTRunGetAttributes(run) as NSDictionary)[kCTFontAttributeName] as! CTFont
                guard NativeFontIdentity.matches(resolved, graphics: graphics) || NativeFontIdentity.isColorEmoji(resolved) else {
                    throw NativePreviewFeatureError("font-fallback:" + (graphics.postScriptName as String? ?? "unknown") + ":" + (CTFontCopyPostScriptName(resolved) as String))
                }
            }
            lines.append((value, CTLineGetTypographicBounds(line, nil, nil, nil)))
        }
        guard !lines.isEmpty, lines.count <= 100 else { throw RecipeError.invalidTimeline }
        let ascent = CTFontGetAscent(font), descent = CTFontGetDescent(font)
        let lineStep = (ascent + descent) * min(3, max(0.5, style.lineSpacing))
        let blockHeight = ascent + descent + Double(lines.count - 1) * lineStep
        let anchorX = Double(canvas.width) * style.xFraction
        let anchorY = Double(canvas.height) * style.yFraction
        let top = anchorY - blockHeight / (style.bottomAligned ? 1 : 2)
        let runs = lines.enumerated().map { index, line in
            let x: Double = switch style.alignment {
            case .left: anchorX
            case .center: anchorX - line.1 / 2
            case .right: anchorX - line.1
            }
            return PositionedTextRun(text: line.0, fontAssetID: style.fontAssetID, fontSize: size,
                x: x, baselineY: top + ascent + Double(index) * lineStep, letterSpacing: style.letterSpacing * size,
                shaped: true, fill: style.color, stroke: style.stroke, strokeWidth: 2 * style.strokeWidth,
                blurLayers: style.shadows, fontVariations: style.fontVariations)
        }
        let typewriter = phases?.entrance == .typewriter || phases?.exit == .typewriter || (phases == nil && [.typewriter, .streamIn].contains(legacyEffect))
        let reveal: DiscreteRevealContent?
        if typewriter {
            let revealLines = runs.enumerated().map { index, run in
                let cursorText: String = switch motion?.cursorStyle {
                case .block: " █"
                case .underscore: " _"
                default: " |"
                }
                let cursor = PositionedTextRun(text: cursorText, fontAssetID: run.fontAssetID, fontSize: run.fontSize,
                    x: run.x, baselineY: run.baselineY, letterSpacing: 0, shaped: true,
                    fill: run.fill, stroke: run.stroke, strokeWidth: 0, fontVariations: style.fontVariations)
                let scalars = Array(run.text.unicodeScalars)
                let offsets = (0...scalars.count).map { count in
                    let prefix = String(String.UnicodeScalarView(scalars.prefix(count)))
                    let line = CTLineCreateWithAttributedString(NSAttributedString(string: prefix, attributes: attributes))
                    return CTLineGetTypographicBounds(line, nil, nil, nil)
                }
                return DiscreteRevealLine(text: run.text, runIndex: index,
                    cursorOffsets: offsets, cursorRun: cursor)
            }
            reveal = DiscreteRevealContent(text: lines.map(\.0).joined(separator: " "), schedule: revealSchedule, lines: revealLines)
        } else { reveal = nil }
        func bleed(_ x: Bool, _ positive: Bool) -> Double {
            max(style.strokeWidth + 2, style.shadows.map { shadow in
                let offset = x ? shadow.dx : shadow.dy
                return ceil(3 * shadow.sigma + max(0, positive ? offset : -offset)) + (shadow.sigma == 20 && shadow.dx == 0 && shadow.dy == 0 ? 2 : 0)
            }.max() ?? 0)
        }
        let smooth: SmoothRevealContent? = legacyEffect == .smoothType && phases == nil ? SmoothRevealContent(text: text,
            lines: runs.enumerated().map { index, run in
                let line = CTLineCreateWithAttributedString(NSAttributedString(string: run.text, attributes: attributes))
                let firstRun = (CTLineGetGlyphRuns(line) as! [CTRun]).first
                return SmoothRevealLine(text: run.text, runIndex: index,
                    bounds: TextRevealBounds(left: run.x - bleed(true, false), top: top + Double(index) * lineStep - bleed(false, false),
                        right: run.x + lines[index].1 + bleed(true, true), bottom: top + Double(index + 1) * lineStep + bleed(false, true)),
                    firstStrongRTL: firstRun.map { CTRunGetStatus($0).contains(.rightToLeft) } ?? false)
            }) : nil
        let inkBounds: TextRevealBounds?
        if legacyEffect == .inkReveal && phases == nil {
            inkBounds = TextRevealBounds(left: (runs.map(\.x).min() ?? anchorX) - bleed(true, false),
                top: top - bleed(false, false),
                right: (zip(runs, lines).map { $0.0.x + $0.1.1 }.max() ?? anchorX) + bleed(true, true),
                bottom: top + blockHeight + bleed(false, true))
        } else { inkBounds = nil }
        let background = style.background.map { color in
            let left = runs.map(\.x).min() ?? anchorX
            let right = zip(runs, lines).map { $0.0.x + $0.1.1 }.max() ?? anchorX
            return TextBackground(color: color, left: left - 8, top: top - 4,
                                  width: right - left + 16, height: blockHeight + 8, radius: 4)
        }
        let layerEffect: PortableTextLayer.Effect = phases == nil ? legacyEffect : (typewriter ? .typewriter : .none)
        let bend: Double? = !TextBend.isStraight(style.bend) && PortableTextLayer.bendSupported(effect: layerEffect, phasesTypewriter: typewriter)
            ? min(100, max(-100, style.bend)) : nil
        return PortableTextLayer(id: id, start: start, end: end, anchorX: anchorX, anchorY: anchorY,
            rotationDegrees: style.rotation, runs: runs, effect: layerEffect, motion: motion, revealBounds: inkBounds,
            discreteReveal: reveal, smoothReveal: smooth, animationPhases: phases, background: background, bend: bend)
    }
    /// One-word-at-a-time caption for a cue (`KaraokeContent.singleWord`; see its wire contract). Every word is laid
    /// out STANDALONE with `compile`, so it is positioned exactly as a one-word caption layer of this style would
    /// be, and the layer carries all of them as runs. `starts` are layer-local, one per word. A cue of more than 100
    /// words (the karaoke cap) comes back as consecutive layers. Returns nil if a word cannot stay on one line,
    /// in which case the caller shows the cue as a full-line karaoke layer.
    public static func compileSingleWords(id: String, text: String, start: Double, end: Double, starts: [Double],
                                          highlight: TextInk, style: Style, fontURL: URL, canvas: Canvas) throws -> [PortableTextLayer]? {
        let words = text.split(whereSeparator: \.isWhitespace).map(String.init)
        guard !words.isEmpty, words.count == starts.count, end > start else { throw RecipeError.invalidTimeline }
        var runs: [PositionedTextRun] = []
        var anchor: PortableTextLayer?
        for word in words {
            let layout = try compile(id: id, text: word, start: start, end: end, style: style, fontURL: fontURL, canvas: canvas)
            guard layout.runs.count == 1, let run = layout.runs.first else { return nil }
            runs.append(run)
            anchor = anchor ?? layout
        }
        guard let anchor else { throw RecipeError.invalidTimeline }
        let limit = 100
        return stride(from: 0, to: words.count, by: limit).enumerated().map { part, lower in
            let upper = min(lower + limit, words.count)
            let layerStart = start + starts[lower]
            let layerEnd = upper < words.count ? start + starts[upper] : end
            return PortableTextLayer(id: part == 0 ? id : "\(id)-part-\(part)", start: layerStart, end: layerEnd,
                anchorX: anchor.anchorX, anchorY: anchor.anchorY, rotationDegrees: anchor.rotationDegrees,
                runs: Array(runs[lower..<upper]), effect: .karaokeLine,
                karaoke: KaraokeContent(starts: starts[lower..<upper].map { $0 - starts[lower] }, highlight: highlight, activeOnly: true, singleWord: true))
        }
    }

    /// Keeps full-line shaping and positions while assigning an independently
    /// timed color to each word. No line is reflowed when the highlight changes.
    public static func compileHighlightedWords(id: String, text: String, start: Double, end: Double,
                                               starts: [Double], highlight: TextInk,
                                               style: Style, fontURL: URL, canvas: Canvas) throws -> PortableTextLayer {
        let layout = try compile(id: id, text: text, start: start, end: end, style: style, fontURL: fontURL, canvas: canvas)
        guard let provider = CGDataProvider(url: fontURL as CFURL), let graphics = CGFont(provider) else {
            throw RecipeError.invalidTimeline
        }
        // KRI-826: authored lines may have been fitted smaller than `style.size`.
        let size = layout.runs.first?.fontSize ?? style.size
        let font = NativeFontIdentity.font(graphics, size: size, variations: style.fontVariations)
        let regex = try NSRegularExpression(pattern: "\\S+")
        var result: [PositionedTextRun] = []
        for lineRun in layout.runs {
            let string = lineRun.text as NSString
            let line = CTLineCreateWithAttributedString(NSAttributedString(string: lineRun.text,
                attributes: [NSAttributedString.Key(kCTFontAttributeName as String): font]))
            for match in regex.matches(in: lineRun.text, range: NSRange(location: 0, length: string.length)) {
                var glyphs: [PositionedGlyph] = []
                var containsEmoji = false
                for run in CTLineGetGlyphRuns(line) as! [CTRun] {
                    let count = CTRunGetGlyphCount(run)
                    var ids = [CGGlyph](repeating: 0, count: count)
                    var positions = [CGPoint](repeating: .zero, count: count)
                    var indices = [CFIndex](repeating: 0, count: count)
                    CTRunGetGlyphs(run, CFRange(location: 0, length: 0), &ids)
                    CTRunGetPositions(run, CFRange(location: 0, length: 0), &positions)
                    CTRunGetStringIndices(run, CFRange(location: 0, length: 0), &indices)
                    for index in 0..<count where NSLocationInRange(indices[index], match.range) {
                        let resolved = (CTRunGetAttributes(run) as NSDictionary)[kCTFontAttributeName] as! CTFont
                        containsEmoji = containsEmoji || NativeFontIdentity.isColorEmoji(resolved)
                        glyphs.append(PositionedGlyph(glyphID: Int(ids[index]), x: positions[index].x, y: -positions[index].y))
                    }
                }
                guard !glyphs.isEmpty else { throw NativePreviewFeatureError("AuthoredTextLayout-206") }
                result.append(PositionedTextRun(text: string.substring(with: match.range), fontAssetID: style.fontAssetID,
                    fontSize: size, x: lineRun.x + (containsEmoji ? (glyphs.map(\.x).min() ?? 0) : 0), baselineY: lineRun.baselineY, letterSpacing: 0, shaped: containsEmoji,
                    fill: style.color, stroke: style.stroke, strokeWidth: style.strokeWidth, blurLayers: style.shadows, glyphs: containsEmoji ? nil : glyphs, fontVariations: style.fontVariations))
            }
        }
        guard result.map(\.text) == text.split(whereSeparator: \.isWhitespace).map(String.init), result.count == starts.count else {
            throw NativePreviewFeatureError("AuthoredTextLayout-213")
        }
        return PortableTextLayer(id: id, start: start, end: end, anchorX: layout.anchorX, anchorY: layout.anchorY,
            rotationDegrees: 0, runs: result, effect: .karaokeLine,
            karaoke: KaraokeContent(starts: starts, highlight: highlight, activeOnly: true))
    }

}
/// KRI-826: one fitting rule for the creator's own lines (`wrap_lines == false`), shared by the video
/// compiler (`AuthoredTextLayout`) and the slide canvas, mirroring cloud `text_overlay_skia._authored_lines`:
/// an explicit line break is always a hard break; a line that fits the frame's safe width (0.9 of the canvas)
/// stays one line at the creator's size; a wider line soft-wraps at word boundaries at that width; only a single
/// word still wider than it shrinks the whole text, never below `min(size, 24)`. Nothing is ever cut.
public enum AuthoredTextFit {
    public static let safeWidthFraction = 0.9
    public static let minimumSize = 24.0

    public static func safeWidth(canvasWidth: Double) -> Double { canvasWidth * safeWidthFraction }

    static func width(_ text: String, attributes: [NSAttributedString.Key: Any]) -> Double {
        CTLineGetTypographicBounds(CTLineCreateWithAttributedString(NSAttributedString(string: text, attributes: attributes)), nil, nil, nil)
    }

    /// Greedy word wrap, the same as cloud `_wrap_text_to_lines`: a word wider than `width` stays whole on its row.
    static func wordRows(_ paragraph: String, attributes: [NSAttributedString.Key: Any], width limit: Double) -> [String] {
        var rows: [String] = []
        var current: [String] = []
        for word in paragraph.split(whereSeparator: \.isWhitespace).map(String.init) {
            let candidate = (current + [word]).joined(separator: " ")
            if current.isEmpty || width(candidate, attributes: attributes) <= limit {
                current.append(word)
            } else {
                rows.append(current.joined(separator: " "))
                current = [word]
            }
        }
        if !current.isEmpty { rows.append(current.joined(separator: " ")) }
        return rows.isEmpty ? [""] : rows
    }

    /// The size the text draws at: `size` unless one word is wider than `safeWidth` at it.
    public static func fittedSize(text: String, size: Double, safeWidth: Double,
                                  attributes: (Double) -> [NSAttributedString.Key: Any]) -> Double {
        let words = text.split(whereSeparator: \.isWhitespace).map(String.init)
        func widest(_ points: Double) -> Double {
            let attrs = attributes(points)
            return words.map { width($0, attributes: attrs) }.max() ?? 0
        }
        let floor = Swift.min(size, minimumSize)
        var fitted = size
        while fitted > floor && widest(fitted) > safeWidth {
            fitted = Swift.max(floor, Swift.min(fitted - 1, (fitted * 0.9).rounded(.down)))
        }
        return fitted
    }
}
#endif
