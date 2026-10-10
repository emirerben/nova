import AVFoundation
import Foundation

/// The creator's song as a LOCAL file: one download feeds both the timeline waveform and the audition player.
struct SongAudioFile: Equatable, Sendable {
    let url: URL
    let durationS: Double?
}

/// Downloads the song behind `GET creation-threads/{id}/song-audio` into Caches (KRI-561).
/// Keyed by thread and song generation, so a replaced song never plays from the old file. Every failure
/// returns nil: the timeline still works, silently.
enum SongAudioCache {
    static func key(threadID: UUID, generation: Int?) -> String {
        "\(threadID.uuidString)-\(generation.map(String.init) ?? "0").audio"
    }

    static var directory: URL {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0].appendingPathComponent("SongAudio", isDirectory: true)
    }

    /// `fetchLink` asks the server for the signed URL; `download` saves it (injectable for tests).
    @MainActor static func load(
        threadID: UUID, generation: Int?,
        fetchLink: () async throws -> SongAudioLink,
        download: (URL) async throws -> (file: URL, contentType: String?) = Self.download
    ) async -> SongAudioFile? {
        let key = key(threadID: threadID, generation: generation)
        // Without a generation there is no way to tell a replaced song apart, so never reuse.
        if generation != nil, let cached = cachedFile(key: key) {
            return SongAudioFile(url: cached, durationS: await duration(of: cached))
        }
        do {
            let link = try await fetchLink()
            let (temp, contentType) = try await download(link.url)
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let destination = directory.appendingPathComponent("\(key).\(fileExtension(for: link.url, contentType: contentType))")
            for stale in matches(key: key) { try? FileManager.default.removeItem(at: stale) }
            try FileManager.default.moveItem(at: temp, to: destination)
            let measured = await duration(of: destination)
            return SongAudioFile(url: destination, durationS: link.durationS ?? measured)
        } catch {
            return nil
        }
    }

    /// An existing download for `key` (any extension), if it has content.
    static func cachedFile(key: String) -> URL? {
        matches(key: key).first { ((try? $0.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) > 0 }
    }

    private static func matches(key: String) -> [URL] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []
        return names.filter { $0.hasPrefix(key + ".") }.map { directory.appendingPathComponent($0) }
    }

    /// Players decide the format from the extension, so keep a real one beside the `.audio` key.
    static func fileExtension(for url: URL, contentType: String?) -> String {
        let known: Set<String> = ["m4a", "mp3", "wav", "aac", "caf", "aif", "aiff", "mp4"]
        let fromPath = url.pathExtension.lowercased()
        if known.contains(fromPath) { return fromPath }
        switch contentType?.lowercased().split(separator: ";").first.map(String.init) {
        case "audio/mpeg", "audio/mp3": return "mp3"
        case "audio/wav", "audio/x-wav", "audio/wave": return "wav"
        case "audio/aac": return "aac"
        default: return "m4a"
        }
    }

    /// Saves `url` to a temporary file. A file URL (UI-test fixture) is copied; anything else must answer 200.
    static func download(_ url: URL) async throws -> (file: URL, contentType: String?) {
        let target = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        if url.isFileURL {
            try FileManager.default.copyItem(at: url, to: target)
            return (target, nil)
        }
        let (temp, response) = try await URLSession.shared.download(from: url)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            try? FileManager.default.removeItem(at: temp)
            throw URLError(.badServerResponse)
        }
        try FileManager.default.moveItem(at: temp, to: target)
        return (target, http.value(forHTTPHeaderField: "Content-Type"))
    }

    static func duration(of file: URL) async -> Double? {
        guard let seconds = try? await AVURLAsset(url: file).load(.duration).seconds, seconds.isFinite, seconds > 0 else { return nil }
        return seconds
    }
}
