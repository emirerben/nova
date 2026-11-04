import XCTest
import KriaMediaEngine
@testable import Kria

/// KRI-1192: spoken captions in pt, es, el, de, ko, it, fr, ar, da and sr compile on the phone in ONE bundled face
/// that covers every letter, the same face the server picks; Greek all caps drops its accents; English and Turkish
/// stay as they were. Rendering (ink, right-to-left Arabic) is proven in KriaMediaEngine's
/// `CaptionScriptRenderTests` with the faces asserted here.
///
/// Failure modes (docs/reviews/kri-1192-caption-languages.md): F5 Serbian mixes Cyrillic and Latin, F6 missing
/// glyphs, F7 Arabic left to right, F8 Greek caps keep accents, F11 iPhone and cloud pick different faces,
/// F12 the editor breaks right-to-left or Serbian editing, F15 English or Turkish change.
@MainActor final class CaptionLanguageTests: XCTestCase {
    private static let repo: URL = {
        let path = #filePath
        let root = path.range(of: "/src/apps/ios/")!
        return URL(fileURLWithPath: String(path[..<root.lowerBound]))
    }()

    private var fonts: URL { get throws { try XCTUnwrap(Bundle.main.url(forResource: "fonts", withExtension: nil)) } }

    /// `tests/fixtures/caption-glyph-parity/script_fallback_faces.json`: the face the server's
    /// `font_coverage.glyph_safe_family` picks for each (family, text).
    private struct Parity: Decodable {
        struct Text: Decodable { let id: String; let language: String; let text: String }
        let families: [String]
        let texts: [Text]
        let faces: [String: [String: String]]
        func text(_ id: String) -> String { texts.first { $0.id == id }!.text }
        func face(_ family: String, _ id: String) -> String { faces[id]?[family] ?? family }
    }

    private func parity() throws -> Parity {
        try JSONDecoder().decode(Parity.self, from: Data(contentsOf: Self.repo
            .appendingPathComponent("tests/fixtures/caption-glyph-parity/script_fallback_faces.json")))
    }

    private func registry() throws -> [String: NativeFontRegistry.Entry] {
        try XCTUnwrap(NativeFontRegistry.entries(from: Data(contentsOf: fonts.appendingPathComponent("font-registry.json"))))
    }

    // MARK: - F6 / F11 / F15: the server's face for every language and family

    func testGlyphFallbackPicksTheServersFaceForEveryLanguageAndFamily() throws {
        let parity = try parity()
        let directory = try fonts
        // The compiler's lookup order (`NativeFontCatalog.fontURL` documents it as the same).
        let fallback = NativeGlyphFallback(fontDirectory: directory, fontURL: NativeFontCatalog(directory: directory).fontURL(for:))
        XCTAssertGreaterThanOrEqual(parity.texts.count, 40)
        var mismatches: [String] = []
        for text in parity.texts {
            for family in parity.families {
                let actual = fallback.family(family, for: text.text)
                if actual != parity.face(family, text.id) {
                    mismatches.append("\(text.id) \(family): phone \(actual), server \(parity.face(family, text.id))")
                }
            }
        }
        XCTAssertEqual(mismatches, [])
        // The script faces draw but are never offered in a picker.
        for name in ["Noto Sans KR", "Noto Sans KR Bold", "Noto Sans Arabic", "Noto Sans Arabic Bold"] {
            XCTAssertFalse(NativeFontCatalog.shared.pickerFonts.contains(name), name)
            XCTAssertNotNil(NativeFontCatalog.shared.fontURL(for: name), name)
        }
    }

    func testOnlyLettersOutsideTheLatinScriptTakeTheScriptStep() {
        XCTAssertFalse(NativeGlyphFallback.needsNonLatinLetter("Güneş ışığı, 2024! Ærø ŠĐČĆŽ"))
        XCTAssertFalse(NativeGlyphFallback.needsNonLatinLetter("— “quoted” 15€ ☀️"))
        for text in ["Καλημέρα", "Добар дан", "다리", "مرحبا", "Visit Αθήνα"] {
            XCTAssertTrue(NativeGlyphFallback.needsNonLatinLetter(text), text)
        }
    }

    // MARK: - Caption cues compile in that face (preview and the phone's own render)

    private func compiler() throws -> NativeEditorRenderCompiler { try NativeEditorRenderCompiler(fontDirectory: fonts) }

    private func captionProgram(_ compiler: NativeEditorRenderCompiler, cues texts: [String], meta: [String: JSONValue],
                                raws: [[String: JSONValue]] = []) throws -> NativeEditorRenderProgram {
        let duration = Double(texts.count) * 4
        let source = ResolvedEditorSource(clipIndex: 0, mediaID: "original",
            asset: MediaAsset(id: "local", relativePath: "original.mp4",
                              fingerprint: AssetFingerprint(hex: String(repeating: "a", count: 64), byteCount: 100), duration: duration),
            url: URL(fileURLWithPath: "/fixture/original.mp4"))
        let clip = EditorClip(id: UUID(), assetID: UUID(), sourceClipIndex: 0, start: 0, end: duration,
                              trimIn: 0, trimOut: duration, sourceDuration: duration, slotID: "slot")
        let cues = texts.enumerated().map { index, text in
            EditorCaptionCue(id: "cue-\(index)", startS: Double(index) * 4, endS: Double(index + 1) * 4, text: text,
                             raw: index < raws.count ? raws[index] : [:])
        }
        let items = cues.map { NativeEditorTimelineItem(selection: .init(kind: .captionCue, id: $0.id), start: $0.startS, end: $0.endS) }
        let document = EditorDocument(editFormat: "subtitled", clips: [.init(id: "slot", clipIndex: 0, inS: 0, durationS: duration)],
                                      captionMeta: meta, captionCues: cues)
        return try compiler.compile(document: document, clips: [clip], items: items, sources: [0: source], deviceCaptions: true)
    }

    /// Font asset ids per cue, in cue order.
    private func cueFaces(_ program: NativeEditorRenderProgram, cues: Int) -> [Set<String>] {
        (0..<cues).map { index in
            Set(program.recipe.textLayers.filter { $0.id.hasPrefix("caption-cue-\(index)") }.flatMap { $0.runs.map(\.fontAssetID) })
        }
    }

    private static let languages = ["pt", "es", "el", "de", "ko", "it", "fr", "ar", "da", "sr", "sr-latn", "en", "tr"]

    func testSpokenCaptionsCompileInTheServersFaceForEveryLanguage() throws {
        let parity = try parity(), registry = try registry(), compiler = try compiler()
        let displays: [(String, [String: JSONValue])] = [
            ("sentence", [:]),
            ("karaoke", ["style": .string("word")]),
            ("one word at a time", ["style": .string("word"), "appearance": .object(["highlight_spoken_word": .bool(true)])]),
        ]
        for language in Self.languages {
            let ids = (0..<3).map { "\(language)-\($0)" }
            let texts = ids.map(parity.text)
            for family in ["TikTok Sans", "Montserrat", "Poppins Bold"] {
                let faces = Set(ids.map { parity.face(family, $0) })
                XCTAssertEqual(faces.count, 1, "\(language) \(family): the server picks one face for the language")
                let face = try XCTUnwrap(faces.first)
                let asset = "font-" + (try XCTUnwrap(registry[face]?.file, face))
                for (name, display) in displays {
                    var meta = display
                    if family != "TikTok Sans" { meta["font"] = .string(family) }
                    let program: NativeEditorRenderProgram
                    do { program = try captionProgram(compiler, cues: texts, meta: meta) } catch {
                        XCTFail("\(language) \(family) \(name): \(error)"); continue
                    }
                    XCTAssertEqual(cueFaces(program, cues: 3), Array(repeating: [asset], count: 3), "\(language) \(family) \(name)")
                }
            }
        }
    }

    /// Like the server's `glyph_safe_overlays`: the face covers the UNION of a family's cues, so it never flips
    /// cue by cue, and a cue with its own family is decided on its own.
    func testACaptionTrackDrawsEveryCueOfAFamilyInOneFace() throws {
        let compiler = try compiler()
        let korean = try captionProgram(compiler, cues: ["Hello there", "다리 밑 수직 간격", "See you"], meta: [:])
        XCTAssertEqual(cueFaces(korean, cues: 3), Array(repeating: ["font-NotoSansKR-Regular.ttf"], count: 3))
        // F16: a recipe drawn in a script face needs a build that bundles it (protocol 35, `scriptFonts`).
        XCTAssertTrue(korean.recipe.effectiveCapabilities.contains(.scriptFonts))
        XCTAssertGreaterThanOrEqual(KriaClientProtocolContract.version, 35, "35 = bundles the script faces")
        let greek = try captionProgram(compiler, cues: ["Hello", "Καλημέρα Ελλάδα", "Bye"], meta: ["font": .string("Montserrat")],
                                       raws: [[:], [:], ["font_family": .string("Pacifico")]])
        XCTAssertEqual(cueFaces(greek, cues: 3), [["font-TikTokSans-Bold.ttf"], ["font-TikTokSans-Bold.ttf"], ["font-Pacifico-Regular.ttf"]])
        // KRI-785's Turkish partner now reaches caption cues too: Satisfy has no ş, so the whole track is Pacifico.
        let turkish = try captionProgram(compiler, cues: ["Merhaba", "Güneş ışığı"], meta: ["font": .string("Satisfy")])
        XCTAssertEqual(cueFaces(turkish, cues: 2), Array(repeating: ["font-Pacifico-Regular.ttf"], count: 2))
        // English and Turkish in the default face are untouched (F15).
        let plain = try captionProgram(compiler, cues: ["Hello there", "Güneş ışığı"], meta: [:])
        XCTAssertEqual(cueFaces(plain, cues: 2), Array(repeating: ["font-TikTokSans-Regular.ttf"], count: 2))
        XCTAssertFalse(plain.recipe.effectiveCapabilities.contains(.scriptFonts))
    }

    // MARK: - F7: Arabic highlighted words read right to left

    func testArabicHighlightedCaptionRunsRightToLeft() throws {
        let parity = try parity(), compiler = try compiler()
        let words = Array(parity.text("ar-0").split(separator: " ").prefix(4).map(String.init))
        let karaoke = try captionProgram(compiler, cues: [words.joined(separator: " ")], meta: ["style": .string("word")])
        let layer = try XCTUnwrap(karaoke.recipe.textLayers.first)
        XCTAssertNotNil(layer.karaoke)
        XCTAssertEqual(layer.runs.map(\.text), words, "runs keep reading order; the highlight follows it")
        func span(_ run: PositionedTextRun) throws -> ClosedRange<Double> {
            let xs = try XCTUnwrap(run.glyphs).map { run.x + $0.x }
            return try XCTUnwrap(xs.min())...XCTUnwrap(xs.max())
        }
        let spans = try layer.runs.map(span)
        for index in spans.indices.dropLast() {
            XCTAssertGreaterThan(spans[index].lowerBound, spans[index + 1].upperBound, "word \(index) is right of word \(index + 1)")
        }
        let single = try captionProgram(compiler, cues: [words.joined(separator: " ")],
            meta: ["style": .string("word"), "appearance": .object(["highlight_spoken_word": .bool(true)])])
        XCTAssertEqual(try XCTUnwrap(single.recipe.textLayers.first).runs.map(\.text), words)
    }

    // MARK: - F8: Greek all caps (shared text_case parity fixture)

    func testTextCaseMatchesTheSharedParityFixture() throws {
        struct Fixture: Decodable {
            struct Case: Decodable {
                struct Element: Decodable { let text: String; let text_case: String? }
                struct Expected: Decodable { let text: String }
                let name: String; let language: String?; let element: Element; let expected: Expected
            }
            let cases: [Case]
        }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: Self.repo
            .appendingPathComponent("tests/fixtures/text-element-parity/text_case.json")))
        XCTAssertGreaterThanOrEqual(fixture.cases.count, 24)
        for item in fixture.cases {
            XCTAssertEqual(KriaTextCase.apply(item.element.text, item.element.text_case, language: item.language),
                           item.expected.text, item.name)
        }
        // Python's str.lower and JS toLowerCase end a Greek word with ς.
        XCTAssertEqual(KriaTextCase.apply("ΟΔΟΣ ΚΑΙ ΣΑΣ", "lower"), "οδος και σας")
        XCTAssertEqual(KriaTextCase.apply("ΟΔΟΣ", "title"), "Οδος")
    }

    // MARK: - F5 / F12: the caption editor's keyboard

    func testCaptionKeyboardFollowsTheLinesScriptForSerbian() {
        let pick = LineEditorConfiguration.keyboardLanguage
        XCTAssertEqual(pick(["en-US", "sr", "sr-Latn"], "sr", "Dobar dan, kako ste?"), "sr-Latn")
        XCTAssertEqual(pick(["en-US", "sr-Latn", "sr"], "sr", "Добар дан"), "sr")
        XCTAssertEqual(pick(["sr-Cyrl", "sr-Latn"], "sr", "Dobar dan"), "sr-Latn")
        XCTAssertEqual(pick(["en-US", "sr"], "sr", "Dobar dan"), "sr", "the only Serbian keyboard is still the caption's")
        // Everything else keeps the first installed keyboard of the language.
        XCTAssertEqual(pick(["en-US", "en-GB"], "en", "Hello"), "en-US")
        XCTAssertEqual(pick(["en-US", "tr-TR"], "tr", "Merhaba"), "tr-TR")
        XCTAssertEqual(pick(["en-US", "ar"], "ar", "مرحبا"), "ar")
        XCTAssertNil(pick(["en-US"], "ko", "다리"))
        XCTAssertEqual(LineEditorConfiguration.captionLine(language: "ar", isLast: false, installedLanguages: ["en-US"]).spellCheckingType, .no)
    }
}
