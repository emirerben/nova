import Foundation
import AVFoundation
import XCTest
import KriaMediaEngine
@testable import Kria

final class NativeEditorSourcesTests: XCTestCase {
    func testDownloadedMP4ImportsWithPlayableExtensionAndUnchangedBytes() async throws {
        let original = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let download = directory.appendingPathComponent("URLSession-download.tmp")
        try FileManager.default.copyItem(at: original, to: download)
        do {
            _ = try await AVURLAsset(url: download).loadTracks(withMediaType: .video)
            XCTFail("The temporary extension must reproduce the reported AVFoundation failure")
        } catch { XCTAssertEqual((error as NSError).domain, AVFoundationErrorDomain) }
        let remote = URL(string: "https://storage.example/base.mp4")!
        let response = HTTPURLResponse(url: remote, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "video/mp4"])!
        let project = ProjectDirectory(root: directory.appendingPathComponent("project"))
        let asset = try await NativeDownloadedMedia.importAsset(from: download, sourceURL: remote, response: response, project: project)
        let local = project.root.appendingPathComponent(asset.relativePath)
        XCTAssertEqual(local.pathExtension, "mp4")
        XCTAssertEqual(asset.fingerprint, try SHA256Fingerprinter().fingerprint(file: original))
        XCTAssertFalse(NativeDownloadedMedia.isStillImage(local))
        let tracks = try await AVURLAsset(url: local).loadTracks(withMediaType: .video)
        XCTAssertFalse(tracks.isEmpty)
    }

    func testPreviewDownloadsDeduplicateAndSurviveResolverRecreation() async throws {
        let original = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let project = ProjectDirectory(root: directory.appendingPathComponent("project"))
        let cacheDirectory = directory.appendingPathComponent("cache")
        let cache = NativePreviewAssetCache(project: project, directory: cacheDirectory)
        let remote = URL(string: "https://storage.example/original.mp4")!
        let response = URLResponse(url: remote, mimeType: "video/mp4", expectedContentLength: -1, textEncodingName: nil)
        var assets: [MediaAsset] = []
        for index in 0..<2 {
            let download = directory.appendingPathComponent("download-\(index).tmp")
            try FileManager.default.copyItem(at: original, to: download)
            assets.append(try await NativeDownloadedMedia.importAsset(from: download, sourceURL: remote,
                response: response, project: cache.project))
        }
        XCTAssertEqual(assets[0].relativePath, assets[1].relativePath)
        XCTAssertEqual(try FileManager.default.contentsOfDirectory(atPath: cache.project.originals.path).count, 1)
        var asset = assets[0]
        asset.duration = 3
        try cache.store(asset, for: "audio:g:music")
        let recreated = NativeEditorSourceResolver(project: project, cacheDirectory: cacheDirectory)
        // The deliberately unreachable URL proves this resolves from disk.
        let resolved = try await recreated.resolveAudio(id: "music", url: remote, generation: "g")
        XCTAssertEqual(resolved.asset.duration, 3)
        XCTAssertEqual(resolved.asset.fingerprint, asset.fingerprint)
        XCTAssertNil(cache.load("audio:new-generation:music"))
        let otherProject = NativePreviewAssetCache(project: ProjectDirectory(root: directory.appendingPathComponent("other")), directory: cacheDirectory)
        XCTAssertNil(otherProject.load("audio:g:music"))
        try Data("corrupt".utf8).write(to: resolved.url)
        XCTAssertNil(cache.load("audio:g:music"))
        try FileManager.default.removeItem(at: resolved.url)
        XCTAssertNil(cache.load("audio:g:music"))
    }

    func testLibraryCacheUsesJobIdentityAcrossLocalSessionDirectories() async throws {
        let original = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let jobID = UUID()
        let cacheDirectory = directory.appendingPathComponent("cache")
        let firstProject = ProjectDirectory(root: directory.appendingPathComponent("session-one"))
        let secondProject = ProjectDirectory(root: directory.appendingPathComponent("session-two"))
        let cache = NativePreviewAssetCache(project: firstProject, directory: cacheDirectory, jobID: jobID)
        let download = directory.appendingPathComponent("download.tmp")
        try FileManager.default.copyItem(at: original, to: download)
        let remote = URL(string: "https://storage.example/original.mp4")!
        var asset = try await NativeDownloadedMedia.importAsset(from: download, sourceURL: remote,
            response: URLResponse(url: remote, mimeType: "video/mp4", expectedContentLength: -1, textEncodingName: nil), project: cache.project)
        asset.duration = 3
        try cache.store(asset, for: "audio:g:music")
        let reopened = NativeEditorSourceResolver(project: secondProject, cacheDirectory: cacheDirectory, jobID: jobID)
        let resolved = try await reopened.resolveAudio(id: "music", url: remote, generation: "g")
        XCTAssertEqual(resolved.asset.fingerprint, asset.fingerprint)
        let otherJob = NativePreviewAssetCache(project: secondProject, directory: cacheDirectory, jobID: UUID())
        XCTAssertNil(otherJob.load("audio:g:music"))
        XCTAssertNil(cache.load("audio:changed-generation:music"))
    }

    func testDownloadedAssetExtensionUsesMediaTypeWithoutTrustingTemporaryNames() {
        let remote = URL(string: "https://storage.example/download")!
        for (mime, ext) in [("audio/mpeg", "mp3"), ("audio/mp4", "m4a"), ("image/png", "png")] {
            let response = URLResponse(url: remote, mimeType: mime, expectedContentLength: 1, textEncodingName: nil)
            XCTAssertEqual(NativeDownloadedMedia.fileExtension(sourceURL: remote, response: response), ext)
        }
        let response = URLResponse(url: remote, mimeType: "application/octet-stream", expectedContentLength: 1, textEncodingName: nil)
        XCTAssertNil(NativeDownloadedMedia.fileExtension(sourceURL: remote, response: response))
    }

    func testSourcePoolUsesConcreteTransportThroughProtocol() async throws {
        defer { NativeEditorURLProtocol.handler = nil }
        let job = UUID()
        NativeEditorURLProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/generative-jobs/\(job.uuidString)/variants/original_text/timeline")
            return (200, Data(#"{"clips":[{"clip_index":0,"signed_url":"https://storage.googleapis.com/bucket/source.mov"}]}"#.utf8))
        }
        let api: any KriaAPIClient = NativeEditorTestSupport.api()
        let pool = try await api.editorSourcePool(jobID: job, variantID: "original_text")
        XCTAssertEqual(pool.clips.first?.nativeSource?.mediaID, "clip-0")
    }

    func testCompositeSourceCannotUseFinishedOutputOrOverrideEditableCuts() throws {
        var variant: [String: JSONValue] = ["resolved_archetype": .string("talking_head"),
            "base_video_path": .string("base.mp4"), "base_video_url": .string("https://example.com/base.mp4"),
            "output_url": .string("https://example.com/finished.mp4")]
        var document = EditorDocument(capabilities: ["timeline": .init(editable: false)], revision: .init(baseGeneration: "g"))
        let base = try XCTUnwrap(NativeEditorBaseSource(variant: variant, document: document))
        var edited = document
        edited.textElements = [.init(id: "later", text: "Preserve this")]
        XCTAssertEqual(try base.hydrate(edited, duration: 5).textElements, edited.textElements)
        edited.revision.baseGeneration = "new-generation"
        XCTAssertThrowsError(try base.hydrate(edited, duration: 5))
        document.capabilities["timeline"] = .init(editable: true)
        XCTAssertNil(NativeEditorBaseSource(variant: variant, document: document))
        document.capabilities["timeline"] = .init(editable: false)
        variant["base_video_url"] = variant["output_url"]
        XCTAssertNil(NativeEditorBaseSource(variant: variant, document: document))
        variant["base_video_url"] = nil
        XCTAssertNil(NativeEditorBaseSource(variant: variant, document: document))
    }

    func testProductionTimelineVideoURLsDecodeAsOriginalSources() throws {
        let data = Data(#"{"clips":[{"clip_index":0,"signed_url":"https://storage.googleapis.com/bucket/original.mov","used":true},{"clip_index":1,"media_id":"approved-video","kind":"video","signed_url":"https://storage.googleapis.com/bucket/other.mp4"}]}"#.utf8)
        let pool = try JSONDecoder().decode(NativeEditorSourcePool.self, from: data)
        XCTAssertNil(pool.baseGeneration)
        XCTAssertEqual(pool.clips[0].nativeSource?.mediaID, "clip-0")
        XCTAssertEqual(pool.clips[0].nativeSource?.sourceURL?.lastPathComponent, "original.mov")
        XCTAssertEqual(pool.clips[1].nativeSource?.mediaID, "approved-video")
        XCTAssertEqual(pool.clips[0].nativeSource?.localRequired, false)
    }

    func testLegacyCompatibilityCannotBypassUnavailableOriginalOrUseDerivatives() throws {
        for row in [
            #"{"native_source":null,"signed_url":"https://storage.googleapis.com/bucket/original.mov"}"#,
            #"{"kind":"image","signed_url":"https://storage.googleapis.com/bucket/preview.jpg"}"#,
            #"{"signed_url":"https://storage.googleapis.com/bucket/analysis-proxy-source.mp4"}"#,
            #"{"signed_url":"http://storage.googleapis.com/bucket/original.mov"}"#,
        ] {
            let object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(row.utf8)) as? [String: Any])
            let data = try JSONSerialization.data(withJSONObject: object.merging(["clip_index": 0]) { left, _ in left })
            let clip = try JSONDecoder().decode(NativeEditorSourcePool.Clip.self, from: data)
            XCTAssertNil(clip.nativeSource, row)
        }
    }

    @MainActor func testPreviewFailureMessagesDoNotExposeSignedURLs() {
        let url = URL(string: "https://example.com/video.mp4?secret=token")!
        let error = NSError(domain: NSURLErrorDomain, code: -1, userInfo: [NSURLErrorFailingURLErrorKey: url])
        XCTAssertFalse(NativeEditorSession.sourcePreviewMessage(for: error).contains("token"))
        XCTAssertTrue(NativeEditorSession.sourcePreviewMessage(for: APIError.conflict).contains("reopen"))
    }

    func testUnusedUnavailableSourcesDoNotBlockPreparation() async throws {
        let pool = NativeEditorSourcePool(clips: [.init(clipIndex: 7, nativeSource: nil)], baseGeneration: "generation")
        let resolver = NativeEditorSourceResolver(project: ProjectDirectory(root: FileManager.default.temporaryDirectory))
        let result = try await resolver.resolve(pool, generation: "generation", requiredIndices: [])
        XCTAssertTrue(result.isEmpty)
        do {
            _ = try await resolver.resolve(pool, generation: "generation", requiredIndices: [7])
            XCTFail("Adding an unavailable source must require preparation")
        } catch MediaEngineError.missingAsset("clip-7") { }
        do {
            _ = try await resolver.resolve(pool, generation: "generation", requiredIndices: [8])
            XCTFail("A missing source index must not silently disappear")
        } catch MediaEngineError.missingAsset("source-pool") { }
    }

    func testLocalOriginalMustMatchApprovedFingerprint() async throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let input = directory.appendingPathComponent("input.mp4")
        let fixture = try XCTUnwrap(Bundle.main.url(forResource: "montage", withExtension: "mp4"))
        try FileManager.default.copyItem(at: fixture, to: input)
        let project = ProjectDirectory(root: directory.appendingPathComponent("project"))
        let asset = try await AssetImportCoordinator(project: project).importAsset(from: input)
        try SourceAssetStore(project: project).bind(mediaID: "source", original: asset)
        let descriptor = OriginalMediaDescriptor(sha256: asset.fingerprint!.hex, byteCount: asset.fingerprint!.byteCount,
                                                durationS: 1, width: 1080, height: 1920, orientationDegrees: 0, hasAudio: true)
        let pool = NativeEditorSourcePool(clips: [.init(clipIndex: 0, nativeSource: .init(mediaID: "source", sourceURL: nil,
                    original: descriptor, localRequired: true))], baseGeneration: "generation")
        let resolver = NativeEditorSourceResolver(project: project)
        let resolved = try await resolver.resolve(pool, generation: "generation")
        XCTAssertEqual(resolved[0]?.asset.fingerprint, asset.fingerprint)
        XCTAssertGreaterThan(try XCTUnwrap(resolved[0]?.asset.duration), 0)
        do {
            _ = try await resolver.resolve(pool, generation: "stale")
            XCTFail("A stale source pool must not be accepted")
        } catch APIError.conflict { }
        try Data("changed bytes".utf8).write(to: project.root.appendingPathComponent(asset.relativePath))
        let freshResolver = NativeEditorSourceResolver(project: project)
        do {
            _ = try await freshResolver.resolve(pool, generation: "generation")
            XCTFail("A changed local original must not be accepted")
        } catch SourceAssetError.changedOriginal("source") { }
    }
}
