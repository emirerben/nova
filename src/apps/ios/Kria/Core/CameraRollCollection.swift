import Foundation
import CoreLocation

/// Metadata cache for an explicit selection, not a second media library. Never sent to the API.
/// Original/source identity and render availability stay with MediaLibraryOriginals and receipts.
struct CameraRollMetadata: Codable, Sendable, Equatable, Identifiable {
    let id: String
    let kind: String
    let duration: Double
    let width: Int
    let height: Int
    let modifiedAt: Date?
    var capture: ClipCaptureRaw?
}

struct CameraRollCollection: Codable, Sendable {
    // Measured on iPhone 13 Pro: 53 mixed assets, 33.72 ms, 72.1 MiB process peak.
    // 50 also fits the existing Main Creator media bound. No file data is loaded.
    static let maximumCount = 50
    static let batchSize = 32
    let id: UUID
    var name: String
    private(set) var entries: [Entry]

    struct Entry: Codable, Sendable, Identifiable {
        let id: String
        var metadata: CameraRollMetadata?
        var correctedGroup: String?
    }

    enum SelectionError: Error { case tooManyAssets, invalidSnapshot, erasedAccount }

    init(id: UUID = UUID(), name: String, identifiers: [String]) throws {
        self.id = id
        self.name = name
        entries = []
        try select(identifiers)
    }

    var pendingIdentifiers: [String] { entries.filter { $0.metadata == nil }.map(\.id) }
    var completedCount: Int { entries.count - pendingIdentifiers.count }

    mutating func select(_ identifiers: [String]) throws {
        var seen = Set<String>()
        let unique = identifiers.filter { !$0.isEmpty && seen.insert($0).inserted }
        guard unique.count <= Self.maximumCount else { throw SelectionError.tooManyAssets }
        let previous = Dictionary(uniqueKeysWithValues: entries.map { ($0.id, $0) })
        entries = unique.map { previous[$0] ?? Entry(id: $0) }
    }

    mutating func record(_ metadata: [CameraRollMetadata], sharesCaptureContext: Bool) {
        let selected = Set(entries.map(\.id))
        var incoming: [String: CameraRollMetadata] = [:]
        for var record in metadata where selected.contains(record.id) {
            if !sharesCaptureContext { record.capture = nil }
            incoming[record.id] = record
        }
        for index in entries.indices {
            if let record = incoming[entries[index].id] { entries[index].metadata = record }
        }
        if !sharesCaptureContext { clearCaptureContext() }
    }

    mutating func reconcileAccessibleIdentifiers(_ identifiers: Set<String>) {
        // Revoked/deleted material ceases to be a candidate; post bindings are untouched.
        entries.removeAll { !identifiers.contains($0.id) }
    }

    mutating func clearCaptureContext() {
        for index in entries.indices { entries[index].metadata?.capture = nil }
    }

    mutating func setGroup(_ value: String, for identifier: String) {
        guard let index = entries.firstIndex(where: { $0.id == identifier }) else { return }
        let cleaned = String(value.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        entries[index].correctedGroup = cleaned.isEmpty ? nil : cleaned
    }

    func group(for identifier: String) -> String {
        if let corrected = entries.first(where: { $0.id == identifier })?.correctedGroup { return corrected }
        let dated = entries.compactMap { entry -> (String, ClipCaptureRaw)? in
            guard let capture = entry.metadata?.capture, capture.captureTime != nil else { return nil }
            return (entry.id, capture)
        }.sorted {
            if $0.1.captureTime == $1.1.captureTime { return $0.0 < $1.0 }
            return $0.1.captureTime! < $1.1.captureTime!
        }
        var last: ClipCaptureRaw?
        var event = 0
        for (id, capture) in dated {
            let gap = last?.captureTime.map { capture.captureTime!.timeIntervalSince($0) } ?? .infinity
            var farApart = false
            if let lat = capture.latitude, let lon = capture.longitude,
               let lastLat = last?.latitude, let lastLon = last?.longitude {
                farApart = CLLocation(latitude: lat, longitude: lon).distance(from: CLLocation(latitude: lastLat, longitude: lastLon)) > 25_000
            }
            if gap > 4 * 3600 || farApart { event += 1 }
            if id == identifier { return "Event \(event)" }
            last = capture
        }
        return name
    }

    /// Saved below the existing account-scoped originals root, so account erasure removes it too.
    func save(in originals: MediaLibraryOriginals) throws {
        guard !FileManager.default.fileExists(atPath: originals.erasureMarker.path) else { throw SelectionError.erasedAccount }
        let directory = originals.root.appendingPathComponent("collection-indexes", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let path = directory.appendingPathComponent("\(id).json")
        try JSONEncoder().encode(self).write(to: path, options: [.atomic, .completeFileProtection])
        // Account erasure may have raced the directory creation / atomic write.
        if FileManager.default.fileExists(atPath: originals.erasureMarker.path) {
            try? FileManager.default.removeItem(at: path)
            throw SelectionError.erasedAccount
        }
    }

    static func load(id: UUID, in originals: MediaLibraryOriginals, sharesCaptureContext: Bool) throws -> Self {
        guard !FileManager.default.fileExists(atPath: originals.erasureMarker.path) else { throw SelectionError.erasedAccount }
        let path = originals.root.appendingPathComponent("collection-indexes/\(id).json")
        var value = try JSONDecoder().decode(Self.self, from: Data(contentsOf: path))
        guard value.id == id, value.entries.count <= maximumCount,
              Set(value.entries.map(\.id)).count == value.entries.count else { throw SelectionError.invalidSnapshot }
        if !sharesCaptureContext { value.clearCaptureContext() }
        return value
    }
}
