import XCTest
@testable import Kria

/// Failures: provenance lost on Save/reopen, changed text/timing shown as current,
/// unsafe links opened, a moved/replaced clip retaining a misleading current citation.
final class NativeResearchSourcesTests: XCTestCase {
    func testSourcesSurviveDocumentSaveAndFlagChangedCopy() throws {
        var document = EditorDocument.decode(snapshot: NativeEditorUITestFixtures.twoText.serverSnapshot)
        document.textElements[0].raw["source_params"] = .object(["research": .object([
            "timeline_binding": .array(document.clips.map { clip in .object([
                "clip_index": .number(Double(clip.clipIndex)), "in_s": .number(clip.inS),
                "duration_s": .number(clip.durationS ?? 0),
                "transition_after": .string(clip.transitionAfter),
                "transition_duration_s": .number(clip.transitionDurationS ?? 0)
            ]) }),
            "original_text": .string(document.textElements[0].text),
            "start_s": .number(document.textElements[0].startS),
            "end_s": .number(document.textElements[0].endS),
            "media_ids": .array([.string("gallery")]),
            "sources": .array([.object(["title": .string("Museum"), "url": .string("https://museum.example.org")])]),
            "search_entry_html": .string("<div>Search</div>"),
            "retrieved_at": .string("2026-10-19T00:00:00Z")
        ])])
        document.clips[0].raw["media_id"] = .string("gallery")
        let restored = EditorDocument.decode(snapshot: document.encodeSnapshot())
        let citation = try XCTUnwrap(ResearchCitation.from(restored.textElements[0]))
        XCTAssertEqual(citation.sources.first?.title, "Museum")
        XCTAssertEqual(citation.originalText, document.textElements[0].text)
        XCTAssertTrue(citation.matches(restored.textElements[0], clips: restored.clips))
        var edited = restored.textElements[0]
        edited.text = "An invented date"
        XCTAssertFalse(citation.matches(edited, clips: restored.clips))
        edited = restored.textElements[0]
        edited.startS += 1
        XCTAssertFalse(citation.matches(edited, clips: restored.clips))
    }

    func testUnsafeSourceSchemesAndPrivateHostsCannotOpen() {
        for value in ["file:///etc/passwd", "javascript:alert(1)", "https://localhost", "https://127.0.0.1/a", "https://169.254.169.254/", "https://phone.local"] {
            XCTAssertNil(ResearchCitation.publicURL(value), value)
        }
        XCTAssertNotNil(ResearchCitation.publicURL("https://museum.example.org/collection"))
    }
}

#if canImport(AVFoundation)
import AVFoundation
import KriaMediaEngine

extension NativeResearchSourcesTests {
    /// Prepared by the real API compiler; optional on-device media is copied into this QA app only.
    @MainActor func testPreparedResearchExportOnDevice() async throws {
        guard ProcessInfo.processInfo.environment["KRIA_RESEARCH_DEVICE_EXPORT"] == "1" else {
            throw XCTSkip("Set KRIA_RESEARCH_DEVICE_EXPORT=1 with a prepared Documents/researchqa bundle")
        }
        let root = try XCTUnwrap(FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first)
            .appendingPathComponent("researchqa")
        let recipe = try RecipeJSON.decode(Data(contentsOf: root.appendingPathComponent("recipe.json")))
        let paths = try JSONDecoder().decode([String: String].self, from: Data(contentsOf: root.appendingPathComponent("assets.json")))
        let urls = paths.mapValues { root.appendingPathComponent($0) }
        let output = root.appendingPathComponent("iphone-export.mp4")
        _ = try await AVFoundationLocalExporter(
            stateStore: FileExportStateStore(directory: root.appendingPathComponent("state")), branding: .none
        ).export(recipe: recipe, assetURLs: urls, outputURL: output)
        let movie = AVURLAsset(url: output)
        let duration = try await movie.load(.duration).seconds
        XCTAssertGreaterThan(duration, 3)
        let video = try await movie.loadTracks(withMediaType: .video)
        let audio = try await movie.loadTracks(withMediaType: .audio)
        XCTAssertEqual(video.count, 1)
        XCTAssertFalse(audio.isEmpty, "The soundtrack/camera audio must survive native export")
    }
}
#endif
