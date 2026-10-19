import CryptoKit
import Foundation

/// SHA-256 fingerprints remembered by file identity (path, size, modification time), so a file that has not changed is
/// not read again. Hashing reads the whole file and grows with its size; a cached proxy or download is opened many times.
///
/// A file whose size or modification time differs from the record is hashed afresh and the record replaced, so the
/// worst case is the cost of not having a cache. The records live in `directory`, which the caller owns (it should be
/// somewhere the system may reclaim).
public struct FileFingerprintCache: Sendable {
    private struct Record: Codable {
        let hex: String
        let byteCount: Int64
        let modified: TimeInterval
    }

    private let directory: URL
    private let fingerprinter = SHA256Fingerprinter()

    public init(directory: URL) { self.directory = directory }

    public func fingerprint(file: URL) throws -> AssetFingerprint {
        let attributes = try FileManager.default.attributesOfItem(atPath: file.path)
        let size = (attributes[.size] as? NSNumber)?.int64Value
        let modified = (attributes[.modificationDate] as? Date)?.timeIntervalSince1970
        let record = recordURL(for: file)
        if let size, let modified,
           let data = try? Data(contentsOf: record), let saved = try? JSONDecoder().decode(Record.self, from: data),
           saved.byteCount == size, saved.modified == modified {
            return AssetFingerprint(hex: saved.hex, byteCount: saved.byteCount)
        }
        let fingerprint = try fingerprinter.fingerprint(file: file)
        if let modified {
            try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let fresh = Record(hex: fingerprint.hex, byteCount: fingerprint.byteCount, modified: modified)
            if let data = try? JSONEncoder().encode(fresh) { try? data.write(to: record, options: .atomic) }
        }
        return fingerprint
    }

    private func recordURL(for file: URL) -> URL {
        let name = SHA256.hash(data: Data(file.standardizedFileURL.path.utf8)).map { String(format: "%02x", $0) }.joined()
        return directory.appendingPathComponent(name + ".json")
    }
}
