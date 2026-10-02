import AVFoundation
import Foundation
import XCTest
import KriaMediaEngine
@testable import Kria

/// Opt-in KRI-277 replay of one already-approved narration recipe.  The Python
/// preparer provides local analysis proxies for originals and exact bytes for
/// the pinned (possibly cleaned) voiceover.  This test uses the production
/// resolver and exporter; only the HTTP grant/storage transport is fixture-backed.
@MainActor final class DeviceNarrationDeliveryReplayTests: XCTestCase {
    override func tearDown() {
        NativeEditorURLProtocol.handler = nil
        super.tearDown()
    }

    func testPersistedNarrationDeliveryReplaysOnSimulator() async throws {
        let input = try directory()
        let replay = try dictionary(input.appendingPathComponent("replay.json"))
        let status = try JSONDecoder().decode(DeviceRenderStatusResponse.self, from: Data(contentsOf: input.appendingPathComponent("status.json")))
        let recipe = status.request.recipe
        let project = ProjectDirectory(root: FileManager.default.temporaryDirectory.appendingPathComponent("kri-277-\(UUID().uuidString)"))
        defer { try? FileManager.default.removeItem(at: project.root) }
        try project.createIfNeeded()
        let originals = try XCTUnwrap(replay["originals"] as? [String: String])
        let store = SourceAssetStore(project: project)
        for (mediaID, relative) in originals {
            let name = URL(fileURLWithPath: relative).lastPathComponent
            let destination = project.originals.appendingPathComponent(name)
            try FileManager.default.copyItem(at: input.appendingPathComponent(relative), to: destination)
            try store.bind(mediaID: mediaID, original: MediaAsset(id: mediaID, relativePath: "originals/\(name)", fingerprint: try SHA256Fingerprinter().fingerprint(file: destination)))
        }
        let grants = try XCTUnwrap(replay["grants"] as? [String: String])
        let bytes = try Dictionary(uniqueKeysWithValues: grants.map { (id, path) in (id, try Data(contentsOf: input.appendingPathComponent(path))) })
        let grantResponses = try XCTUnwrap(replay["grant_responses"] as? [String: Any])
        var requested: [String] = []
        NativeEditorURLProtocol.handler = { request in
            if request.url?.host == "storage.replay.test" {
                let id = request.url!.lastPathComponent
                return (200, bytes[id] ?? Data())
            }
            let body = try JSONSerialization.jsonObject(with: NativeEditorTestSupport.bodyData(request)) as? [String: Any]
            let id = body?["asset_id"] as? String ?? ""
            requested.append(id)
            guard let response = grantResponses[id] else { return (404, Data()) }
            return (200, try JSONSerialization.data(withJSONObject: response))
        }
        let resolver = AuthorizedDeviceSourceResolver(api: NativeEditorTestSupport.api(), request: status.request, originals: store, library: RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory)), downloadSession: NativeEditorTestSupport.session())
        let urls = try await resolver.resolve(for: recipe)
        let voiceover = try XCTUnwrap(replay["voiceover_asset_id"] as? String)
        XCTAssertTrue(requested.contains(voiceover), "the exact pinned narration must use the grant path")
        XCTAssertNotNil(urls[voiceover])
        let movie = input.appendingPathComponent("export.mp4")
        try? FileManager.default.removeItem(at: movie)
        let checkpoint = try await AVFoundationLocalExporter(stateStore: FileExportStateStore(directory: project.root.appendingPathComponent("state")), branding: .none).export(recipe: recipe, assetURLs: urls, outputURL: movie)
        XCTAssertEqual(checkpoint.status, .completed)
        let asset = AVURLAsset(url: movie)
        let exportedDuration = try await asset.load(.duration).seconds
        let audioTracks = try await asset.loadTracks(withMediaType: .audio)
        XCTAssertEqual(exportedDuration, try XCTUnwrap(replay["duration_s"] as? Double), accuracy: 0.12)
        XCTAssertFalse(audioTracks.isEmpty, "narration delivery must export AAC audio")
        try JSONSerialization.data(withJSONObject: ["duration_s": exportedDuration, "voiceover_granted": requested.contains(voiceover)], options: [.prettyPrinted]).write(to: input.appendingPathComponent("simulator-report.json"))
    }

    private func directory() throws -> URL {
        let environment = ProcessInfo.processInfo.environment
        guard let path = environment["KRIA_NARRATION_REPLAY_DIR"] ?? environment["TEST_RUNNER_KRIA_NARRATION_REPLAY_DIR"] else {
            throw XCTSkip("Set KRIA_NARRATION_REPLAY_DIR")
        }
        return URL(fileURLWithPath: path)
    }

    private func dictionary(_ url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }
}
