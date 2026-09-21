import Foundation

/// AVFoundation picks its reader from the file extension, and the verified
/// cache stores downloads as `<sha256>-<bytes>`. Link a verified file under a
/// playable name without copying it: a hard link keeps the verified inode.
/// Shared by `VisualVideoFile` and `PlayableAudioFile`, which differ only in
/// how they sniff the destination extension from the source bytes.
enum PlayableMediaFile {
    static func prepare(source: URL, fingerprint: RenderFingerprint, directory: URL, extensionOf: (URL) throws -> String) throws -> URL {
        let destination = directory.appendingPathComponent("\(fingerprint.sha256)-\(fingerprint.byteCount).\(try extensionOf(source))")
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

    static func sameFile(_ a: URL, _ b: URL) throws -> Bool {
        let fm = FileManager.default
        let first = try fm.attributesOfItem(atPath: a.path)[.systemFileNumber] as? NSNumber
        let second = try fm.attributesOfItem(atPath: b.resolvingSymlinksInPath().path)[.systemFileNumber] as? NSNumber
        return first != nil && first == second
    }

    static func header(of source: URL, count: Int) throws -> Data {
        let handle = try FileHandle(forReadingFrom: source)
        defer { try? handle.close() }
        return try handle.read(upToCount: count) ?? Data()
    }
}

/// The Visuals pool accepts MP4 and QuickTime only; both are ISO-BMFF boxes.
public enum VisualVideoFile {
    public static func prepare(source: URL, fingerprint: RenderFingerprint, directory: URL) throws -> URL {
        try PlayableMediaFile.prepare(source: source, fingerprint: fingerprint, directory: directory, extensionOf: fileExtension)
    }

    static func fileExtension(of source: URL) throws -> String {
        let header = try PlayableMediaFile.header(of: source, count: 12)
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

/// Sniffs a downloaded audio asset's container from its bytes -- the verified
/// cache has no extension, and AVFoundation refuses to open one (-11828
/// "Cannot Open"), same class of bug `VisualVideoFile` fixes for videos
/// (KRI-132). Library `music`/`sound_effect` assets and a recorded voiceover
/// all land here. The upload-time allowlist (`_SLOT_UPLOAD_AUDIO_EXT` in
/// `app/routes/music_jobs.py`) accepts mp3/m4a/wav/webm/ogg/aac, but
/// AVFoundation has no built-in Vorbis/Opus-in-Ogg or Opus-in-WebM decoder --
/// those two fail closed with `unsupportedCapability` instead of a crash.
enum PlayableAudioFile {
    static func prepare(source: URL, fingerprint: RenderFingerprint, directory: URL) throws -> URL {
        try PlayableMediaFile.prepare(source: source, fingerprint: fingerprint, directory: directory, extensionOf: fileExtension)
    }

    static func fileExtension(of source: URL) throws -> String {
        let header = try PlayableMediaFile.header(of: source, count: 12)
        guard header.count >= 4 else { throw MediaEngineError.missingAsset(source.lastPathComponent) }
        if header.count >= 8, String(data: header[4..<8], encoding: .ascii) == "ftyp" { return "m4a" } // AAC-in-MP4
        if header.starts(with: [0x52, 0x49, 0x46, 0x46]) { return "wav" } // "RIFF"..."WAVE"
        if header.starts(with: [0x49, 0x44, 0x33]) { return "mp3" } // "ID3"
        if header.starts(with: [0x4F, 0x67, 0x67, 0x53]) { throw MediaEngineError.unsupportedCapability } // "OggS"
        if header.starts(with: [0x1A, 0x45, 0xDF, 0xA3]) { throw MediaEngineError.unsupportedCapability } // EBML (WebM/MKV)
        if header[0] == 0xFF, (header[1] & 0xE0) == 0xE0 {
            // MPEG frame sync; the layer field distinguishes raw ADTS AAC (00) from MP3 (01/10/11).
            return (header[1] >> 1) & 0x3 == 0 ? "aac" : "mp3"
        }
        throw MediaEngineError.missingAsset(source.lastPathComponent)
    }
}
