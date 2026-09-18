import Foundation

/// AVFoundation picks its reader from the file extension, and the verified
/// cache stores downloads as `<sha256>-<bytes>`. Give a Visuals-pool video a
/// playable name without copying it: a hard link keeps the verified inode.
public enum VisualVideoFile {
    public static func prepare(source: URL, fingerprint: RenderFingerprint, directory: URL) throws -> URL {
        let destination = directory.appendingPathComponent("\(fingerprint.sha256)-\(fingerprint.byteCount).\(try fileExtension(of: source))")
        let fm = FileManager.default
        // Reuse a name only while it is still the verified file: a repaired cache
        // entry or an interrupted copy must not keep serving other bytes.
        if fm.fileExists(atPath: destination.path) {
            if try sameFile(destination, source) { return destination }
            try fm.removeItem(at: destination)
        }
        try fm.createDirectory(at: directory, withIntermediateDirectories: true)
        let staging = directory.appendingPathComponent(UUID().uuidString + ".partial")
        defer { try? fm.removeItem(at: staging) }
        do { try fm.linkItem(at: source, to: staging) }
        catch { try fm.copyItem(at: source, to: staging) } // Other volumes can't link.
        do { try fm.moveItem(at: staging, to: destination) }
        catch {
            // A concurrent render may have prepared it first.
            guard fm.fileExists(atPath: destination.path) else { throw error }
        }
        return destination
    }

    private static func sameFile(_ a: URL, _ b: URL) throws -> Bool {
        let fm = FileManager.default
        let first = try fm.attributesOfItem(atPath: a.path)[.systemFileNumber] as? NSNumber
        let second = try fm.attributesOfItem(atPath: b.resolvingSymlinksInPath().path)[.systemFileNumber] as? NSNumber
        return first != nil && first == second
    }

    /// The pool accepts MP4 and QuickTime only; both are ISO-BMFF boxes.
    static func fileExtension(of source: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: source)
        defer { try? handle.close() }
        let header = try handle.read(upToCount: 12) ?? Data()
        guard header.count >= 8, let box = String(data: header[4..<8], encoding: .ascii) else { throw MediaEngineError.missingAsset(source.lastPathComponent) }
        if box == "ftyp" {
            let brand = header.count >= 12 ? String(data: header[8..<12], encoding: .ascii) : nil
            return brand == "qt  " ? "mov" : "mp4"
        }
        // Classic QuickTime files open with a movie box instead of a file type.
        guard ["moov", "mdat", "wide", "free", "skip", "pnot"].contains(box) else { throw MediaEngineError.missingAsset(source.lastPathComponent) }
        return "mov"
    }
}
