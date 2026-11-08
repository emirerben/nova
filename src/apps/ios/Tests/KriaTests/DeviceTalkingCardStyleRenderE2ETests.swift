import Foundation
import XCTest
import AVFoundation
@testable import Kria
import KriaMediaEngine

#if DEBUG
/// KRI-1264 (Talking card zoom and animation), opt-in; skipped unless `KRIA_CARD_STYLE_E2E_DIR` is set. Driven by
/// `scripts/ios/card-style-e2e.py`:
///
/// - `testWritesTheAppSaveForATwoTimesPoppedCard` loads the card the server projected (`editor.json`) into a real
///   editor session, sets Zoom 2 and a Pop entrance through the session API the panel calls, and writes the
///   `media_overlays` its Save sends (`save.json`), read from the same document sections `commitRequest` reads.
/// - `testRendersEveryPreparedCardStyleCase` renders every `cases/<name>/recipe.json` the server's real Save pinned
///   through the production exporter, to `cases/<name>/phone.mp4`.
@MainActor final class DeviceTalkingCardStyleRenderE2ETests: XCTestCase {
    private func root() throws -> URL {
        guard let path = ProcessInfo.processInfo.environment["KRIA_CARD_STYLE_E2E_DIR"] else { throw XCTSkip("Set KRIA_CARD_STYLE_E2E_DIR to run") }
        return URL(fileURLWithPath: path)
    }

    func testWritesTheAppSaveForATwoTimesPoppedCard() throws {
        let root = try root()
        let editor = try JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: root.appendingPathComponent("editor.json"))).objectValue
        let card = try XCTUnwrap(editor?["media_overlays"]?.arrayValue?.first?.objectValue)
        let id = try XCTUnwrap(card["id"]?.stringValue)
        var draft = NativeEditorUITestFixtures.captionVisuals
        var document = EditorDocument(snapshot: draft.serverSnapshot)
        document.mediaOverlays = [.init(id: id, startS: try XCTUnwrap(card["start_s"]?.numberValue), endS: try XCTUnwrap(card["end_s"]?.numberValue),
                                        kind: card["kind"]?.stringValue ?? "image", raw: card)]
        draft.serverSnapshot = document.encodeSnapshot()
        draft.serverSnapshot["editor_capabilities"] = try XCTUnwrap(editor?["editor_capabilities"])
        let session = NativeEditorSession(draft: draft)
        let selection = EditorSelection(kind: .mediaOverlay, id: id)
        XCTAssertTrue(session.canStyleVisual(selection), "the server offered Zoom and Animation")

        session.setVisualEditorStyle(selection, key: "zoom", value: .number(2))
        var animation = NativeVisualAuthoring.style(for: session.visualRaw(selection) ?? [:])["animation"]?.objectValue ?? [:]
        animation["entrance"] = .string("pop")
        session.setVisualEditorStyle(selection, key: "animation", value: .object(animation), animationPhase: "entrance")

        let sections = session.document.encodeSnapshot()["editor_payload"]?.objectValue?["sections"]?.objectValue
        let overlays = try XCTUnwrap(sections?["media_overlays"])
        let style = overlays.arrayValue?.first?.objectValue?["editor_style"]?.objectValue
        XCTAssertEqual(style?["zoom"], .number(2))
        XCTAssertEqual(style?["animation"]?.objectValue?["entrance"], .string("pop"))
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(JSONValue.object(["media_overlays": overlays])).write(to: root.appendingPathComponent("save.json"))
    }

    func testRendersEveryPreparedCardStyleCase() async throws {
        let cases = try root().appendingPathComponent("cases")
        let names = try FileManager.default.contentsOfDirectory(atPath: cases.path).sorted().filter { !$0.hasPrefix(".") }
        XCTAssertEqual(names, ["plain", "zoom2_pop"], "run the prepare and save steps first")
        var tail = 0.0
        if let outro = KriaBranding.outroURL() { tail = try await AVURLAsset(url: outro).load(.duration).seconds }
        for name in names {
            let directory = cases.appendingPathComponent(name)
            let recipe = try RecipeJSON.decode(Data(contentsOf: directory.appendingPathComponent("recipe.json")))
            let card = recipe.tracks.first { $0.id == "subtitled-overlays" }?.clips.first
            XCTAssertEqual(card?.visualPlacement?.editorStyle?.zoom, name == "plain" ? nil : 2, name)
            let paths = try JSONDecoder().decode([String: String].self, from: Data(contentsOf: directory.appendingPathComponent("assets.json")))
            let decision = CapabilityNegotiator(provider: DefaultRendererCapabilities(capabilities: recipe.effectiveCapabilities)).decide(for: recipe)
            XCTAssertEqual(decision.route, .local, "\(name): \(decision.reason ?? "")")
            let output = directory.appendingPathComponent("phone.mp4")
            try? FileManager.default.removeItem(at: output)
            let state = directory.appendingPathComponent("state")
            try? FileManager.default.removeItem(at: state)
            let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: state))
                .export(recipe: recipe, assetURLs: paths.mapValues { URL(fileURLWithPath: $0) }, outputURL: output)
            XCTAssertEqual(checkpoint.status, .completed, name)
            let duration = try await AVURLAsset(url: output).load(.duration).seconds
            XCTAssertEqual(duration, TimelineMath.totalDuration(of: recipe) + tail, accuracy: 0.1, name)
        }
    }
}
#endif
