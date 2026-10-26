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
    // Catalogue capacity is independent of the smaller selected-story render bound.
    // Metadata stays batched and originals load one at a time.
    static let maximumCount = 500
    static let batchSize = 32
    let id: UUID
    /// Ideas the server holds for each event, kept so the surface opens with them before the network answers.
    /// A file written before ideas were per event stored one `savedIdeas` object; that key is no longer read, so
    /// such a file still loads and its ideas come back from the server.
    var ideasByGroup: [String: SavedCameraRollGroupIdeas] = [:]
    /// What the creator calls an event. The server's group key never changes when they rename, so ideas stay put.
    var eventTitles: [String: String] = [:]
    var serverRevision: Int?
    var name: String
    private(set) var entries: [Entry]

    enum CodingKeys: String, CodingKey { case id, ideasByGroup, eventTitles, serverRevision, name, entries }

    struct Entry: Codable, Sendable, Identifiable {
        let id: String
        var metadata: CameraRollMetadata?
        var correctedGroup: String?
        var savedAsset: SavedMediaAsset?
    }

    enum SelectionError: Error { case tooManyAssets, invalidSnapshot, erasedAccount }

    /// Files from before ideas were per event lack the newer keys, so each is optional on read.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(UUID.self, forKey: .id)
        name = try c.decode(String.self, forKey: .name)
        entries = try c.decode([Entry].self, forKey: .entries)
        serverRevision = try c.decodeIfPresent(Int.self, forKey: .serverRevision)
        ideasByGroup = try c.decodeIfPresent([String: SavedCameraRollGroupIdeas].self, forKey: .ideasByGroup) ?? [:]
        eventTitles = try c.decodeIfPresent([String: String].self, forKey: .eventTitles) ?? [:]
    }

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
            if let record = incoming[entries[index].id] {
                if let previous = entries[index].metadata, previous.modifiedAt != record.modifiedAt {
                    entries[index].savedAsset = nil
                }
                entries[index].metadata = record
            }
        }
        if !sharesCaptureContext { clearCaptureContext() }
        pinAutomaticGroups()
    }

    mutating func reconcileAccessibleIdentifiers(_ identifiers: Set<String>) {
        // Revoked/deleted material ceases to be a candidate; post bindings are untouched.
        entries.removeAll { !identifiers.contains($0.id) }
    }

    mutating func bind(_ asset: SavedMediaAsset, to identifier: String) {
        guard let index = entries.firstIndex(where: { $0.id == identifier }) else { return }
        if entries.contains(where: { $0.id != identifier && $0.savedAsset?.id == asset.id }) {
            entries.remove(at: index)
        } else { entries[index].savedAsset = asset }
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
        groupMap()[identifier] ?? name
    }

    /// Every entry's event in one pass. A creator's correction (or an event pinned earlier) wins; otherwise a gap of
    /// more than four hours or 25 km between capture times starts a new event, and an entry with no capture time
    /// joins the collection's name.
    func groupMap() -> [String: String] {
        let automatic = automaticKeys()
        var map: [String: String] = [:]
        for entry in entries { map[entry.id] = entry.correctedGroup ?? automatic[entry.id] ?? name }
        return map
    }

    /// Writes each automatic event into the entries that have no key yet. The server, the ideas and the creator's
    /// renames all point at an event by its key, so a key must not move when a neighbouring event is deleted or
    /// when later metadata reorders the capture times.
    mutating func pinAutomaticGroups() {
        for (id, key) in automaticKeys() {
            if let index = entries.firstIndex(where: { $0.id == id }) { entries[index].correctedGroup = key }
        }
    }

    /// Dated entries that still have no key, grouped by capture-time clusters. A cluster that already holds a
    /// keyed entry keeps that key; a new cluster takes the next free "Event N", never a number that is in use.
    private func automaticKeys() -> [String: String] {
        let dated = entries.compactMap { entry -> (String, ClipCaptureRaw)? in
            guard let capture = entry.metadata?.capture, capture.captureTime != nil else { return nil }
            return (entry.id, capture)
        }.sorted {
            if $0.1.captureTime == $1.1.captureTime { return $0.0 < $1.0 }
            return $0.1.captureTime! < $1.1.captureTime!
        }
        var clusters: [[String]] = []
        var last: ClipCaptureRaw?
        for (id, capture) in dated {
            let gap = last?.captureTime.map { capture.captureTime!.timeIntervalSince($0) } ?? .infinity
            var farApart = false
            if let lat = capture.latitude, let lon = capture.longitude,
               let lastLat = last?.latitude, let lastLon = last?.longitude {
                farApart = CLLocation(latitude: lat, longitude: lon).distance(from: CLLocation(latitude: lastLat, longitude: lastLon)) > 25_000
            }
            if gap > 4 * 3600 || farApart { clusters.append([]) }
            clusters[clusters.count - 1].append(id)
            last = capture
        }
        var keyed: [String: String] = [:]
        for entry in entries { if let key = entry.correctedGroup { keyed[entry.id] = key } }
        var highest = keyed.values.compactMap(Self.eventNumber).max() ?? 0
        var result: [String: String] = [:]
        for cluster in clusters {
            let counts = Dictionary(grouping: cluster.compactMap { keyed[$0] }, by: { $0 }).mapValues(\.count)
            let key = counts.max { ($0.value, $1.key) < ($1.value, $0.key) }?.key ?? {
                highest += 1
                return "Event \(highest)"
            }()
            for id in cluster where keyed[id] == nil { result[id] = key }
        }
        return result
    }

    private static func eventNumber(_ key: String) -> Int? {
        guard key.hasPrefix("Event "), let number = Int(key.dropFirst(6)), number > 0 else { return nil }
        return number
    }

    /// Entries are taken out of the collection whole so Undo can put the very same bindings back.
    func title(forGroup group: String) -> String { eventTitles[group] ?? group }

    mutating func rename(group: String, to title: String) {
        let cleaned = String(title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(80))
        eventTitles[group] = cleaned.isEmpty || cleaned == group ? nil : cleaned
    }

    mutating func remove(inGroup group: String) -> [Entry] {
        let map = groupMap()
        let leaving = entries.filter { map[$0.id] == group }
        let gone = Set(leaving.map(\.id))
        entries.removeAll { gone.contains($0.id) }
        return leaving
    }

    mutating func restore(_ restored: [Entry]) {
        let present = Set(entries.map(\.id))
        entries.append(contentsOf: restored.filter { !present.contains($0.id) })
    }

    func localCopy() throws -> Self {
        var copy = try Self(name: String(name.prefix(140)) + " (local changes)", identifiers: entries.map(\.id))
        copy.entries = entries
        return copy
    }

    /// Saved below the existing account-scoped originals root, so account erasure removes it too.
    func save(in originals: MediaLibraryOriginals) throws {
        guard !FileManager.default.fileExists(atPath: originals.erasureMarker.path) else { throw SelectionError.erasedAccount }
        let directory = originals.root.appendingPathComponent("collection-indexes", isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        var root = originals.root
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try root.setResourceValues(values)
        let path = directory.appendingPathComponent("\(id).json")
        let data = try JSONEncoder().encode(self)
        guard data.count <= 2 * 1024 * 1024 else { throw SelectionError.invalidSnapshot }
        try data.write(to: path, options: [.atomic, .completeFileProtection])
        // Account erasure may have raced the directory creation / atomic write.
        if FileManager.default.fileExists(atPath: originals.erasureMarker.path) {
            try? FileManager.default.removeItem(at: path)
            throw SelectionError.erasedAccount
        }
    }

    static func load(id: UUID, in originals: MediaLibraryOriginals, sharesCaptureContext: Bool) throws -> Self {
        guard !FileManager.default.fileExists(atPath: originals.erasureMarker.path) else { throw SelectionError.erasedAccount }
        let path = originals.root.appendingPathComponent("collection-indexes/\(id).json")
        let size = try path.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? Int.max
        guard size <= 2 * 1024 * 1024 else { throw SelectionError.invalidSnapshot }
        var value = try JSONDecoder().decode(Self.self, from: Data(contentsOf: path))
        guard value.id == id, value.entries.count <= maximumCount,
              Set(value.entries.map(\.id)).count == value.entries.count else { throw SelectionError.invalidSnapshot }
        if !sharesCaptureContext { value.clearCaptureContext() }
        value.pinAutomaticGroups()
        return value
    }
}
