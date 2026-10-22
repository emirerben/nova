import CoreGraphics
import CoreText
import Foundation

/// KRI-785: the phone mirror of `app/pipeline/font_coverage.py`.
///
/// Several bundled faces lack Turkish letters (Alte Haas Grotesk, Permanent
/// Marker, Satisfy: ğ Ğ ş Ş İ; Big Curls also ı; Rascal all twelve) and
/// ZT Bros Oskon 90s draws its dotless ı with a dot. CoreText would patch each
/// missing letter from a system font, so "Güneş" in Satisfy rendered with a
/// lone San Francisco "ş"; the cloud burned tofu. Both now apply one rule:
/// when the chosen face can't draw a needed character of the text (blank
/// outlines count as missing: Big Curls' ç ö ü), the WHOLE run uses its `glyph-fallbacks.json` partner (when that
/// partner covers the text). Otherwise the chosen face is kept unchanged.
///
/// The cloud also walks same-category registry faces for fonts the JSON does
/// not list; every face that needs a fallback today is listed, so the two
/// platforms pick the same face.
final class NativeGlyphFallback: @unchecked Sendable {
    private let fallbacks: [String: String]
    private let fontURL: (String) -> URL?
    private let lock = NSLock()
    private var fonts: [String: CTFont] = [:]

    init(fontDirectory: URL, fontURL: @escaping (String) -> URL?) {
        let data = try? Data(contentsOf: fontDirectory.appendingPathComponent("glyph-fallbacks.json"))
        let raw = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        fallbacks = raw.compactMapValues { $0 as? String }.filter { !$0.key.hasPrefix("_") }
        self.fontURL = fontURL
    }

    /// `family` when it covers `text`, else its listed fallback when that covers it.
    func family(_ family: String, for text: String) -> String {
        guard covers(family, text) == false, let fallback = fallbacks[family], covers(fallback, text) == true else {
            return family
        }
        return fallback
    }

    /// nil when the face can't be loaded: never swap a face we can't inspect.
    private func covers(_ family: String, _ text: String) -> Bool? {
        guard let font = font(family) else { return nil }
        let dottedDotless = Self.dotlessIHasDot(font)
        for scalar in Self.neededScalars(text) {
            if scalar.value == 0x0131 && dottedDotless { return false }
            let units = Array(String(scalar).utf16)
            var glyphs = [CGGlyph](repeating: 0, count: units.count)
            if !CTFontGetGlyphsForCharacters(font, units, &glyphs, units.count) || glyphs.contains(0) { return false }
            // Big Curls maps ç ö ü to EMPTY glyphs; a blank outline is missing too.
            if glyphs.contains(where: { Self.isBlank(font, $0) }) { return false }
        }
        return true
    }

    private func font(_ family: String) -> CTFont? {
        lock.lock(); defer { lock.unlock() }
        if let cached = fonts[family] { return cached }
        guard let url = fontURL(family), let provider = CGDataProvider(url: url as CFURL),
              let graphics = CGFont(provider) else { return nil }
        let font = CTFontCreateWithGraphicsFont(graphics, 100, nil, nil)
        fonts[family] = font
        return font
    }

    /// Like `font_coverage.needed_chars`: NFC, then letters, digits, punctuation,
    /// currency and math symbols (₺ ° ×), non-emoji other symbols and leftover
    /// combining marks (the U+0307 of a lower-cased İ). Variation selectors and
    /// emoji are not font glyphs.
    static func neededScalars(_ text: String) -> [Unicode.Scalar] {
        var seen = Set<UInt32>()
        return text.precomposedStringWithCanonicalMapping.unicodeScalars.filter { scalar in
            if (0xFE00...0xFE0F).contains(scalar.value) { return false }
            switch scalar.properties.generalCategory {
            case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter,
                 .decimalNumber, .letterNumber, .otherNumber,
                 .connectorPunctuation, .dashPunctuation, .openPunctuation, .closePunctuation,
                 .initialPunctuation, .finalPunctuation, .otherPunctuation,
                 .currencySymbol, .mathSymbol, .nonspacingMark:
                return seen.insert(scalar.value).inserted
            case .otherSymbol:
                return !scalar.properties.isEmoji && seen.insert(scalar.value).inserted
            default:
                return false
            }
        }
    }

    private static func isBlank(_ font: CTFont, _ glyph: CGGlyph) -> Bool {
        guard let path = CTFontCreatePathForGlyph(font, glyph, nil) else { return true }
        return path.isEmpty || path.boundingBoxOfPath.isNull
    }

    /// A dotless ı drawn with as many contours as a dotted i still has its dot.
    private static func dotlessIHasDot(_ font: CTFont) -> Bool {
        var units: [UniChar] = [0x69, 0x0131]
        var glyphs = [CGGlyph](repeating: 0, count: 2)
        guard CTFontGetGlyphsForCharacters(font, &units, &glyphs, 2), !glyphs.contains(0) else { return false }
        let dotted = contours(font, glyphs[0])
        return dotted >= 2 && contours(font, glyphs[1]) >= dotted
    }

    private static func contours(_ font: CTFont, _ glyph: CGGlyph) -> Int {
        guard let path = CTFontCreatePathForGlyph(font, glyph, nil) else { return 0 }
        var count = 0
        path.applyWithBlock { element in
            if element.pointee.type == .moveToPoint { count += 1 }
        }
        return count
    }
}
