import CoreText
import Foundation
import SwiftUI
import UIKit

/// The type groups the font picker's sideways tabs show (KRI-1312). Derived from the
/// registry's `vibe` (and `category` for mono) so a new font joins a tab by data alone.
enum NativeFontGroup: String, CaseIterable, Identifiable {
    case editorial, headlines, clean, handwriting, script, mono

    var id: String { rawValue }

    var label: String {
        switch self {
        case .editorial: String(localized: "Editorial", comment: "Font group")
        case .headlines: String(localized: "Headlines", comment: "Font group")
        case .clean: String(localized: "Clean", comment: "Font group: plain, simple fonts")
        case .handwriting: String(localized: "Handwriting", comment: "Font group")
        case .script: String(localized: "Script", comment: "Font group: cursive fonts")
        case .mono: String(localized: "Mono", comment: "Font group: monospaced fonts")
        }
    }

    init?(vibe: String?, category: String?) {
        if category == "mono" { self = .mono; return }
        switch vibe {
        case "editorial": self = .editorial
        case "viral_headlines": self = .headlines
        case "clean_captions": self = .clean
        case "handwritten": self = .handwriting
        case "script": self = .script
        default: return nil
        }
    }
}

/// The fonts the editor offers, read from the same bundled `fonts/` folder the
/// render compiler uses (`font-registry.json` + the TTFs). The picker list is
/// every non-deprecated registry key, i.e. the web's `INTRO_FONTS`, so both
/// platforms offer the same choices and the backend accepts every one.
///
/// `previewFont` builds a font straight from the file (CGFont → CTFont) rather
/// than registering it globally: the same route the renderer takes, so a picker
/// row looks like the rendered text, and nothing collides with the
/// `UIAppFonts` copies of Inter/Fraunces.
final class NativeFontCatalog: @unchecked Sendable {
    static let shared = NativeFontCatalog(directory: Bundle.main.url(forResource: "fonts", withExtension: nil))
    /// Used only when the bundled folder can't be read.
    static let fallbackFonts = ["Inter", "Fraunces", "Space Grotesk"]

    let pickerFonts: [String]
    /// Picker font -> its sideways tab. A font without a recognised vibe is only in "All".
    let groups: [String: NativeFontGroup]
    private let directory: URL?
    private let files: [String: String]
    private let instances: [String: [String: Double]]
    private let lock = NSLock()
    private var graphicsCache: [String: CGFont] = [:]

    init(directory: URL?) {
        self.directory = directory
        var files: [String: String] = [:]
        var live: [String] = []
        var groups: [String: NativeFontGroup] = [:]
        if let directory,
           let data = try? Data(contentsOf: directory.appendingPathComponent("font-registry.json")),
           let registry = NativeFontRegistry.entries(from: data) {
            for (name, entry) in registry {
                files[name] = entry.file
                if entry.deprecated != true {
                    live.append(name)
                    if let group = NativeFontGroup(vibe: entry.vibe, category: entry.category) { groups[name] = group }
                }
            }
        }
        assert(!live.isEmpty || directory == nil, "font-registry.json missing from the bundled fonts folder")
        self.files = files
        self.groups = groups
        self.pickerFonts = live.isEmpty
            ? Self.fallbackFonts
            : live.sorted { $0.lowercased() < $1.lowercased() }
        if let directory,
           let data = try? Data(contentsOf: directory.appendingPathComponent("native-font-instances.json")),
           let decoded = try? JSONDecoder().decode([String: [String: Double]].self, from: data) {
            instances = decoded
        } else {
            instances = [:]
        }
    }

    /// The picker fonts in one tab, in picker (alphabetical) order.
    func fonts(in group: NativeFontGroup) -> [String] { pickerFonts.filter { groups[$0] == group } }

    /// The tabs that have at least one font, in display order.
    var populatedGroups: [NativeFontGroup] { NativeFontGroup.allCases.filter { !fonts(in: $0).isEmpty } }

    /// Same lookup order as `NativeEditorRenderCompiler.resolveFont`.
    func fontURL(for name: String) -> URL? {
        guard let directory else { return nil }
        for file in [files[name], name + ".ttf", name + "-Regular.ttf", name + ".otf"].compactMap({ $0 }) {
            let url = directory.appendingPathComponent(file)
            if FileManager.default.fileExists(atPath: url.path) { return url }
        }
        return nil
    }

    func ctFont(_ name: String, size: CGFloat) -> CTFont? {
        guard let graphics = graphicsFont(name) else { return nil }
        guard let axes = instances[name], !axes.isEmpty else { return CTFontCreateWithGraphicsFont(graphics, size, nil, nil) }
        // Variation axis tags are four-character codes packed big-endian.
        let variation = Dictionary(uniqueKeysWithValues: axes.map { tag, value in
            (NSNumber(value: tag.utf8.reduce(UInt32(0)) { ($0 << 8) | UInt32($1) }), NSNumber(value: value))
        })
        let descriptor = CTFontDescriptorCreateWithAttributes([kCTFontVariationAttribute: variation] as CFDictionary)
        return CTFontCreateWithGraphicsFont(graphics, size, nil, descriptor)
    }

    /// A SwiftUI font in the named face; `KriaFont.body` when the file is missing.
    func previewFont(_ name: String?, size: CGFloat) -> Font {
        let scaled = UIFontMetrics(forTextStyle: .body).scaledValue(for: size)
        guard let name, let font = ctFont(name, size: scaled) else { return KriaFont.body(size) }
        return Font(font)
    }

    private func graphicsFont(_ name: String) -> CGFont? {
        lock.lock(); defer { lock.unlock() }
        if let cached = graphicsCache[name] { return cached }
        guard let url = fontURL(for: name), let provider = CGDataProvider(url: url as CFURL),
              let graphics = CGFont(provider) else { return nil }
        graphicsCache[name] = graphics
        return graphics
    }
}

/// Reads `font-registry.json`'s `fonts` map keeping the LAST entry for a
/// repeated key, which is what Python's `json` and JS `JSON.parse` do. The file
/// repeats "Outfit" (a stale deprecated entry before the live `Outfit-VF.ttf`
/// one); Foundation's decoders keep the first, which would drop Outfit from the
/// picker and point the renderer at a file that isn't bundled.
enum NativeFontRegistry {
    struct Entry: Decodable {
        let file: String
        let deprecated: Bool?
        /// Read by `NativeGlyphFallback`'s script step (KRI-1192); a value that is not a number reads as absent
        /// rather than dropping the face.
        let weight: Double?
        /// The picker tab this font belongs to (KRI-1312); absent reads as "All" only.
        let vibe: String?
        let category: String?

        private enum CodingKeys: String, CodingKey { case file, deprecated, weight, vibe, category }
        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            file = try container.decode(String.self, forKey: .file)
            deprecated = try container.decodeIfPresent(Bool.self, forKey: .deprecated)
            weight = try? container.decodeIfPresent(Double.self, forKey: .weight)
            vibe = try? container.decodeIfPresent(String.self, forKey: .vibe)
            category = try? container.decodeIfPresent(String.self, forKey: .category)
        }
    }

    static func entries(from data: Data) -> [String: Entry]? {
        let bytes = [UInt8](data)
        var i = 0
        func skipSpace() { while i < bytes.count, [0x20, 0x0A, 0x0D, 0x09].contains(bytes[i]) { i += 1 } }
        /// Advances past one JSON value and returns its byte range.
        func skipValue() -> Range<Int>? {
            skipSpace()
            let start = i
            guard i < bytes.count else { return nil }
            var depth = 0
            var inString = false
            while i < bytes.count {
                let byte = bytes[i]
                if inString {
                    if byte == 0x5C { i += 1 } else if byte == 0x22 { inString = false; if depth == 0 { i += 1; return start..<i } }
                } else if byte == 0x22 {
                    inString = true
                } else if byte == 0x7B || byte == 0x5B {
                    depth += 1
                } else if byte == 0x7D || byte == 0x5D {
                    if depth == 0 { return start < i ? start..<i : nil }
                    depth -= 1
                    if depth == 0 { i += 1; return start..<i }
                } else if depth == 0, byte == 0x2C {
                    return start..<i
                }
                i += 1
            }
            return depth == 0 && start < i ? start..<i : nil
        }
        func string(_ range: Range<Int>) -> String? {
            (try? JSONSerialization.jsonObject(with: Data(bytes[range]), options: .fragmentsAllowed)) as? String
        }
        /// Calls `body(key, valueRange)` for each member of the object at `i`.
        func members(_ body: (String, Range<Int>) -> Void) -> Bool {
            skipSpace()
            guard i < bytes.count, bytes[i] == 0x7B else { return false }
            i += 1
            while true {
                skipSpace()
                if i < bytes.count, bytes[i] == 0x7D { i += 1; return true }
                guard let keyRange = skipValue(), let key = string(keyRange) else { return false }
                skipSpace()
                guard i < bytes.count, bytes[i] == 0x3A else { return false }
                i += 1
                guard let value = skipValue() else { return false }
                body(key, value)
                skipSpace()
                if i < bytes.count, bytes[i] == 0x2C { i += 1 }
            }
        }

        var result: [String: Entry]?
        let parsed = members { key, value in
            guard key == "fonts" else { return }
            let saved = i
            i = value.lowerBound
            var fonts: [String: Entry] = [:]
            if members({ name, entryRange in
                if let entry = try? JSONDecoder().decode(Entry.self, from: Data(bytes[entryRange])) { fonts[name] = entry }
            }) { result = fonts }
            i = saved
        }
        return parsed ? result : nil
    }
}
