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
///
/// KRI-1192: text with a letter outside the Latin script (Greek, Cyrillic, Hangul,
/// Arabic) takes the same whole-run route one step further, exactly like
/// `glyph_safe_family`: the partner when it covers the text, else the covering
/// `_script_fallbacks` face whose registry weight is closest to the chosen face's
/// (ties keep the list order), else the chosen face. The script faces (Noto Sans
/// KR / Arabic) are deprecated registry entries: never offered in a picker, but
/// they resolve for drawing. Latin-only text keeps the KRI-785 rule above, so
/// English and Turkish pick exactly the faces they did.
final class NativeGlyphFallback: @unchecked Sendable {
    /// Legacy file-root names that still name registry faces (`app/pipeline/font_aliases.py`).
    static let legacyAliases = [
        "PlayfairDisplay-Bold": "Playfair Display",
        "PlayfairDisplay-Regular": "Playfair Display Regular",
        "Inter-Bold": "Inter",
        "Inter-Regular": "Inter Regular",
    ]

    private let fallbacks: [String: String]
    private let scriptFallbacks: [String]
    /// Registry face -> weight (the server reads a missing weight as 400).
    private let weights: [String: Int]
    private let fontURL: (String) -> URL?
    private let lock = NSLock()
    private var fonts: [String: CTFont] = [:]

    init(fontDirectory: URL, fontURL: @escaping (String) -> URL?) {
        let data = try? Data(contentsOf: fontDirectory.appendingPathComponent("glyph-fallbacks.json"))
        let raw = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        fallbacks = raw.compactMapValues { $0 as? String }.filter { !$0.key.hasPrefix("_") }
        scriptFallbacks = (raw["_script_fallbacks"] as? [Any])?.compactMap { $0 as? String } ?? []
        let registry = (try? Data(contentsOf: fontDirectory.appendingPathComponent("font-registry.json")))
            .flatMap(NativeFontRegistry.entries) ?? [:]
        weights = registry.mapValues { Int($0.weight ?? 400) }
        self.fontURL = fontURL
    }

    /// `family` when it covers `text`, else the whole-run fallback described above.
    func family(_ family: String, for text: String) -> String {
        guard covers(family, text) == false else { return family }
        if Self.needsNonLatinLetter(text) { return scriptFamily(family, for: text) ?? family }
        guard let fallback = fallbacks[family], covers(fallback, text) == true else {
            return family
        }
        return fallback
    }

    /// KRI-1192 (`glyph_safe_family`'s non-Latin branch): the partner, else the closest-weight covering
    /// `_script_fallbacks` face. Nil keeps the chosen face: not a registry face, or nothing listed covers the text.
    private func scriptFamily(_ family: String, for text: String) -> String? {
        let name = Self.legacyAliases[family] ?? family
        guard let weight = weights[name] else { return nil }
        if let partner = fallbacks[name], weights[partner] != nil, covers(partner, text) == true { return partner }
        let ranked = scriptFallbacks.enumerated().compactMap { index, face in
            weights[face].flatMap { face == name ? nil : (distance: abs($0 - weight), index: index, face: face) }
        }.sorted { ($0.distance, $0.index) < ($1.distance, $1.index) }
        return ranked.first { covers($0.face, text) == true }?.face
    }

    /// Like `font_coverage.needs_non_latin_letter`: a letter (NFC) whose Unicode name is not LATIN... (Greek,
    /// Cyrillic, Hangul, Arabic). Digits, punctuation and symbols belong to no script.
    static func needsNonLatinLetter(_ text: String) -> Bool {
        text.precomposedStringWithCanonicalMapping.unicodeScalars.contains { scalar in
            switch scalar.properties.generalCategory {
            case .uppercaseLetter, .lowercaseLetter, .titlecaseLetter, .modifierLetter, .otherLetter:
                return !(scalar.properties.name ?? "").hasPrefix("LATIN")
            default:
                return false
            }
        }
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
