import Foundation
import XCTest
@testable import KriaMediaEngine

/// KRI-125: importing an asset may share the source's bytes (hardlink) instead of copying them,
/// but only when the caller opts in, and a failed link must degrade to a copy — never an error.
final class AssetLinkImportTests: XCTestCase {
    private var directories: [URL] = []

    override func tearDown() {
        directories.forEach { try? FileManager.default.removeItem(at: $0) }
        directories = []
        super.tearDown()
    }

    private func workspace() throws -> (source: URL, destination: URL) {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("link-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        directories.append(root)
        let source = root.appendingPathComponent("source.mov")
        try Data("footage bytes".utf8).write(to: source)
        return (source, root.appendingPathComponent("out/asset.mov"))
    }

    private func identity(_ url: URL) -> UInt64? {
        ((try? FileManager.default.attributesOfItem(atPath: url.path))?[.systemFileNumber] as? NSNumber)?.uint64Value
    }

    func testLinkAssetSharesTheSourcesBytes() throws {
        let (source, destination) = try workspace()

        try CoordinatedFileCopier().linkAsset(from: source, to: destination)

        XCTAssertEqual(identity(destination), identity(source))
        try FileManager.default.removeItem(at: source)
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "footage bytes",
                       "removing one name must not disturb the other")
    }

    func testLinkAssetFallsBackToARealCopyWhenLinkingFails() throws {
        let (source, destination) = try workspace()
        let copier = CoordinatedFileCopier(linker: { _, _ in throw CocoaError(.fileWriteVolumeReadOnly) })

        XCTAssertNoThrow(try copier.linkAsset(from: source, to: destination), "a failed link must never fail the import")

        XCTAssertNotEqual(identity(destination), identity(source))
        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "footage bytes")
        XCTAssertTrue(FileManager.default.fileExists(atPath: source.path))
    }

    func testCopyAssetIsStillAnIndependentCopy() throws {
        let (source, destination) = try workspace()

        try CoordinatedFileCopier().copyAsset(from: source, to: destination)

        XCTAssertNotEqual(identity(destination), identity(source), "copyAsset must keep its snapshot semantics")
    }

    func testLinkAssetReplacesAnExistingDestination() throws {
        let (source, destination) = try workspace()
        try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("stale".utf8).write(to: destination)

        try CoordinatedFileCopier().linkAsset(from: source, to: destination)

        XCTAssertEqual(try String(contentsOf: destination, encoding: .utf8), "footage bytes")
    }

    func testConformersThatCannotLinkFallBackToCopyByDefault() throws {
        struct CopyOnly: AssetFileCoordinator {
            let copies: CopyLog
            func copyAsset(from source: URL, to destination: URL) throws {
                copies.record(source)
                try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
                try FileManager.default.copyItem(at: source, to: destination)
            }
        }
        let (source, destination) = try workspace()
        let log = CopyLog()

        try CopyOnly(copies: log).linkAsset(from: source, to: destination)

        XCTAssertEqual(log.sources, [source], "the protocol default routes linkAsset through copyAsset")
    }

    func testImportDefaultsToCopyAndPreferLinkSharesBytes() async throws {
        let (source, _) = try workspace()
        let projectRoot = FileManager.default.temporaryDirectory.appendingPathComponent("project-\(UUID().uuidString)", isDirectory: true)
        directories.append(projectRoot)
        let importer = AssetImportCoordinator(project: ProjectDirectory(root: projectRoot))

        let copied = try await importer.importAsset(from: source)
        let linked = try await importer.importAsset(from: source, preferLink: true)

        let copiedURL = projectRoot.appendingPathComponent(copied.relativePath)
        let linkedURL = projectRoot.appendingPathComponent(linked.relativePath)
        XCTAssertNotEqual(identity(copiedURL), identity(source), "opt-in only: the default import stays a snapshot")
        XCTAssertEqual(identity(linkedURL), identity(source))
        XCTAssertEqual(copied.fingerprint, linked.fingerprint, "linking must not change the recorded fingerprint")
    }
}

private final class CopyLog: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [URL] = []
    var sources: [URL] { lock.withLock { recorded } }
    func record(_ url: URL) { lock.withLock { recorded.append(url) } }
}
