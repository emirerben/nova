import Foundation
import XCTest
import CoreImage
import KriaMediaEngine
@testable import Kria

private final class GrantLog: @unchecked Sendable { var paths: [String] = [] }

/// KRI-121: Visuals photos download through one grant each on the phone.
@MainActor final class DeviceVisualGrantTests: XCTestCase {
    override func tearDown() { NativeEditorURLProtocol.handler = nil; super.tearDown() }

    func testVisualPhotoGrantWaitsOutTheRateLimitAndVerifiesTheBytes() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let photo = root.appendingPathComponent("photo.png")
        try CIContext().writePNGRepresentation(of: CIImage(color: .red).cropped(to: CGRect(x: 0, y: 0, width: 300, height: 200)),
            to: photo, format: .RGBA8, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!)
        let bytes = try Data(contentsOf: photo)
        let fingerprint = try SHA256Fingerprinter().fingerprint(file: photo)
        let recipe = KriaMediaEngine.EditRecipe(schemaVersion: 2, rendererVersion: "kria-ios-2", canvas: .vertical1080,
            assets: [MediaAsset(id: "visual-1", relativePath: "visual-1", fingerprint: fingerprint)],
            tracks: [TimelineTrack(id: "story", kind: .video, clips: [TimelineClip(id: "photo", sourceAssetID: "visual-1", sourceDuration: 2)])],
            assetManifest: RenderAssetManifest(assets: [RenderAssetReference(id: "visual-1", fingerprint: try RenderFingerprint(fingerprint),
                                                                             source: .visual(visualID: "5f0c3a52-0d6e-4c1a-9b51-2e7f2f7c1a11", generation: "7"))]))
        let job = UUID()
        let request = DeviceRenderRequest(identity: DeviceRenderIdentity(jobID: job, variantID: "guided_story", recipeRevision: 1,
                                                                         recipeDigest: String(repeating: "a", count: 64)), recipe: recipe)
        let log = GrantLog()
        NativeEditorURLProtocol.handler = { request in
            log.paths.append(request.url?.path ?? "")
            if request.url?.host == "storage.test" { return (200, bytes) }
            // The first grant hits the per-minute limit; the retry succeeds.
            if log.paths.count == 1 { return (429, Data(#"{"detail":"Rate limit exceeded"}"#.utf8)) }
            return (200, Data(#"{"asset_id":"visual-1","download_url":"https://storage.test/photo","expires_at":"2099-01-01T00:00:00Z"}"#.utf8))
        }
        let project = ProjectDirectory(root: root.appendingPathComponent("project"))
        try project.createIfNeeded()
        var resolver = AuthorizedDeviceSourceResolver(api: NativeEditorTestSupport.api(), request: request,
            originals: SourceAssetStore(project: project),
            library: RenderLibraryCache(root: project.root.appending(path: "library", directoryHint: .isDirectory)),
            downloadSession: NativeEditorTestSupport.session())
        resolver.rateLimitBackoff = .milliseconds(1)

        let urls = try await resolver.resolve(for: recipe)

        let grant = "/me/jobs/\(job.uuidString)/device-render/assets"
        XCTAssertEqual(log.paths, [grant, grant, "/photo"])
        let resolved = try XCTUnwrap(urls["visual-1"])
        // Already smaller than the canvas: the verified bytes render as-is.
        XCTAssertEqual(try SHA256Fingerprinter().fingerprint(file: resolved), fingerprint)
    }
}
