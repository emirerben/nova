import Foundation
import CoreLocation
import Photos
import AVFoundation

// KRI-189: when and where a clip was filmed.
//
// The phone reads the Photos asset's creation date and location when a clip is chosen, rounds the
// location to two decimal places (about 1 km), reverse-geocodes it to a place name on the device, and
// sends all three with the attach call. Everything here is best effort and non-fatal: a missing date, a
// denied location, an offline geocoder or a failed lookup sends nothing for that part and never blocks
// or fails an upload. The whole feature is one setting, "Use when and where clips were filmed"
// (default ON); with it off nothing below reads or sends anything.
//
// Privacy: a precise coordinate is never stored, sent or persisted. `ClipCaptureRaw` only ever holds a
// coordinate that has already been rounded, and the server rounds again.

// MARK: Setting

enum ClipCaptureSetting {
    static let defaultsKey = "kria.settings.share-clip-capture-context"

    /// ON unless the user turned it off. Read every time (never cached) so flipping the switch takes
    /// effect for clips that are already uploading.
    static func isEnabled(_ defaults: UserDefaults = .standard) -> Bool {
        defaults.object(forKey: defaultsKey) as? Bool ?? true
    }

    static func setEnabled(_ enabled: Bool, defaults: UserDefaults = .standard) {
        defaults.set(enabled, forKey: defaultsKey)
    }

    /// The Account switch changed. Turning it off also forgets any filming context already remembered for
    /// clips that have not attached yet, so "off" means nothing is kept, not just nothing is sent.
    static func settingChanged(to enabled: Bool, store: ClipCaptureStore = .shared) {
        if !enabled { store.removeAll() }
    }
}

// MARK: Coarse coordinates

enum CoarseCoordinate {
    /// Two decimal places, about 1 km. Mirrors `COARSE_LOCATION_DECIMALS` in the API.
    static func round(_ value: Double) -> Double { (value * 100).rounded() / 100 }

    /// Cache key for a rounded coordinate, so nearby clips share one geocode lookup.
    static func key(latitude: Double, longitude: Double) -> String {
        String(format: "%.2f,%.2f", round(latitude), round(longitude))
    }

    static func isValid(latitude: Double, longitude: Double) -> Bool {
        latitude.isFinite && longitude.isFinite && abs(latitude) <= 90 && abs(longitude) <= 180
    }
}

// MARK: What we remember between choosing a clip and attaching it

/// The filming context read at choose time. A coordinate here is already rounded.
struct ClipCaptureRaw: Codable, Sendable, Equatable {
    var captureTime: Date?
    var latitude: Double?
    var longitude: Double?
    var savedAt: Date

    init(captureTime: Date?, latitude: Double?, longitude: Double?, savedAt: Date = Date()) {
        self.captureTime = captureTime
        if let latitude, let longitude, CoarseCoordinate.isValid(latitude: latitude, longitude: longitude) {
            self.latitude = CoarseCoordinate.round(latitude)
            self.longitude = CoarseCoordinate.round(longitude)
        } else {
            self.latitude = nil
            self.longitude = nil
        }
        self.savedAt = savedAt
    }

    var hasLocation: Bool { latitude != nil && longitude != nil }
    var isEmpty: Bool { captureTime == nil && !hasLocation }
}

/// What the Photos asset told us, before rounding. `ClipCaptureRaw.init` is what rounds.
enum ClipCaptureReader {
    /// Reads `PHAsset.creationDate` / `location` for a picker item. Returns nil when the feature is off,
    /// the asset cannot be resolved (no Photos access, deleted) or it carries neither value.
    static func read(assetIdentifier: String, defaults: UserDefaults = .standard) -> ClipCaptureRaw? {
        guard ClipCaptureSetting.isEnabled(defaults) else { return nil }
        guard let asset = PHAsset.fetchAssets(withLocalIdentifiers: [assetIdentifier], options: nil).firstObject else { return nil }
        return capture(creationDate: asset.creationDate, coordinate: asset.location?.coordinate)
    }

    /// `read`, plus a fallback for what Photos could not tell us. `PHAsset.fetchAssets` returns nothing
    /// without Photos read access (the picker only asks for it on the library-backed path), so any part
    /// still missing is filled from the exported file's own QuickTime metadata (creation date and the
    /// `com.apple.quicktime.location.ISO6709` tag iPhone video carries). Photos wins where both exist.
    static func read(assetIdentifier: String, fileURL: URL, defaults: UserDefaults = .standard) async -> ClipCaptureRaw? {
        guard ClipCaptureSetting.isEnabled(defaults) else { return nil }
        let photos = read(assetIdentifier: assetIdentifier, defaults: defaults)
        if let photos, photos.captureTime != nil, photos.hasLocation { return photos }
        let file = await readFile(fileURL)
        return merged(photos: photos, file: file)
    }

    /// Photos parts win; the file fills only what Photos left empty.
    static func merged(photos: ClipCaptureRaw?, file: ClipCaptureRaw?, now: Date = Date()) -> ClipCaptureRaw? {
        let raw = ClipCaptureRaw(
            captureTime: photos?.captureTime ?? file?.captureTime,
            latitude: photos?.hasLocation == true ? photos?.latitude : file?.latitude,
            longitude: photos?.hasLocation == true ? photos?.longitude : file?.longitude,
            savedAt: now
        )
        return raw.isEmpty ? nil : raw
    }

    /// The exported file's embedded creation date and location. Never throws; nil when it has neither.
    static func readFile(_ url: URL) async -> ClipCaptureRaw? {
        let asset = AVURLAsset(url: url)
        var date: Date?
        if let item = try? await asset.load(.creationDate) { date = try? await item.load(.dateValue) }
        var coordinate: CLLocationCoordinate2D?
        if let items = try? await asset.load(.metadata) {
            for item in items where item.identifier == .quickTimeMetadataLocationISO6709 {
                if let text = try? await item.load(.stringValue), let parsed = parseISO6709(text) { coordinate = parsed; break }
            }
        }
        return capture(creationDate: date, coordinate: coordinate)
    }

    /// ISO 6709 as QuickTime writes it, e.g. `+41.0082+028.9784+012.000/`. Nil when malformed.
    static func parseISO6709(_ text: String) -> CLLocationCoordinate2D? {
        let body = text.trimmingCharacters(in: CharacterSet(charactersIn: "/ \n"))
        var numbers: [Double] = []
        var current = ""
        for ch in body {
            if (ch == "+" || ch == "-"), !current.isEmpty {
                guard let value = Double(current) else { return nil }
                numbers.append(value); current = ""
            }
            current.append(ch)
        }
        if !current.isEmpty { guard let value = Double(current) else { return nil }; numbers.append(value) }
        guard numbers.count >= 2, CoarseCoordinate.isValid(latitude: numbers[0], longitude: numbers[1]) else { return nil }
        return CLLocationCoordinate2D(latitude: numbers[0], longitude: numbers[1])
    }

    /// Pure core of `read`, separated so it is testable without a Photos library.
    static func capture(creationDate: Date?, coordinate: CLLocationCoordinate2D?, now: Date = Date()) -> ClipCaptureRaw? {
        let raw = ClipCaptureRaw(
            captureTime: creationDate,
            latitude: coordinate?.latitude,
            longitude: coordinate?.longitude,
            savedAt: now
        )
        return raw.isEmpty ? nil : raw
    }
}

/// Filming context keyed by upload record id. The record id is stable from choosing a clip through
/// staging, retries and relaunch, so this survives every path without threading a new field through
/// them. Entries are tiny; anything older than `maxAge` is dropped on load.
final class ClipCaptureStore: @unchecked Sendable {
    static let shared = ClipCaptureStore()
    static let maxAge: TimeInterval = 14 * 24 * 60 * 60

    private let defaults: UserDefaults
    private let key: String
    private let lock = NSLock()
    private var entries: [String: ClipCaptureRaw]

    init(defaults: UserDefaults = .standard, key: String = "kria.clip-capture-context.v1", now: Date = Date()) {
        self.defaults = defaults
        self.key = key
        var loaded: [String: ClipCaptureRaw] = [:]
        if let data = defaults.data(forKey: key), let decoded = try? JSONDecoder().decode([String: ClipCaptureRaw].self, from: data) {
            loaded = decoded.filter { now.timeIntervalSince($0.value.savedAt) < Self.maxAge }
        }
        self.entries = loaded
    }

    func set(_ capture: ClipCaptureRaw, for recordID: UUID) {
        lock.lock(); defer { lock.unlock() }
        entries[recordID.uuidString] = capture
        persistLocked()
    }

    func capture(for recordID: UUID) -> ClipCaptureRaw? {
        lock.lock(); defer { lock.unlock() }
        return entries[recordID.uuidString]
    }

    func remove(_ recordID: UUID) {
        lock.lock(); defer { lock.unlock() }
        guard entries.removeValue(forKey: recordID.uuidString) != nil else { return }
        persistLocked()
    }

    /// Forget everything: what the setting being turned off means for what is already remembered.
    func removeAll() {
        lock.lock(); defer { lock.unlock() }
        guard !entries.isEmpty else { return }
        entries = [:]
        persistLocked()
    }

    private func persistLocked() {
        guard let data = try? JSONEncoder().encode(entries) else { return }
        defaults.set(data, forKey: key)
    }
}

// MARK: Wire format

struct ClipPlaceWire: Encodable, Sendable, Equatable {
    var subLocality: String?
    var locality: String?
    var country: String?

    enum CodingKeys: String, CodingKey { case subLocality = "sub_locality", locality, country }

    var isEmpty: Bool { subLocality == nil && locality == nil && country == nil }
}

struct CoarseLocationWire: Encodable, Sendable, Equatable {
    var lat: Double
    var lon: Double
}

/// The three optional attach fields. Absent parts are omitted from the JSON entirely.
struct ClipCaptureWire: Sendable, Equatable {
    var captureTime: String?
    var coarseLocation: CoarseLocationWire?
    var place: ClipPlaceWire?

    var isEmpty: Bool { captureTime == nil && coarseLocation == nil && place == nil }

    /// ISO8601 UTC with a `Z`, which the server parses as an aware datetime.
    static func isoString(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        return formatter.string(from: date)
    }

    static func make(from raw: ClipCaptureRaw, place: ClipPlaceWire?) -> ClipCaptureWire? {
        var wire = ClipCaptureWire()
        wire.captureTime = raw.captureTime.map(isoString)
        if let latitude = raw.latitude, let longitude = raw.longitude {
            wire.coarseLocation = CoarseLocationWire(lat: latitude, lon: longitude)
        }
        if let place, !place.isEmpty { wire.place = place }
        return wire.isEmpty ? nil : wire
    }

    /// Everything the attach call should send for one upload record: nothing when the setting is off, and
    /// only the parts that could be read or looked up. Never throws.
    static func forAttach(
        recordID: UUID,
        store: ClipCaptureStore = .shared,
        resolver: ClipPlaceResolver = .shared,
        defaults: UserDefaults = .standard,
        placeTimeout: Duration = .seconds(6)
    ) async -> ClipCaptureWire? {
        guard ClipCaptureSetting.isEnabled(defaults), let raw = store.capture(for: recordID) else { return nil }
        var place: ClipPlaceWire?
        if let latitude = raw.latitude, let longitude = raw.longitude {
            place = await resolver.placeWithTimeout(latitude: latitude, longitude: longitude, timeout: placeTimeout)
        }
        return make(from: raw, place: place)
    }
}

// MARK: Reverse geocoding

protocol PlaceReverseGeocoding: Sendable {
    /// nil on any failure (offline, throttled, no result). Never throws.
    func place(latitude: Double, longitude: Double) async -> ClipPlaceWire?
}

/// CLGeocoder, on the main actor: it delivers on the main queue and is not `Sendable`.
@MainActor final class CLGeocoderPlaceReverseGeocoder: PlaceReverseGeocoding {
    nonisolated init() {}

    func place(latitude: Double, longitude: Double) async -> ClipPlaceWire? {
        let geocoder = CLGeocoder()
        do {
            let marks = try await geocoder.reverseGeocodeLocation(CLLocation(latitude: latitude, longitude: longitude))
            guard let mark = marks.first else { return nil }
            let place = ClipPlaceWire(subLocality: mark.subLocality, locality: mark.locality, country: mark.country)
            return place.isEmpty ? nil : place
        } catch {
            return nil
        }
    }
}

/// Serializes lookups (CLGeocoder allows one request at a time and throttles bursts) and caches a
/// successful answer by rounded coordinate, so a clip series shot in one spot costs one lookup.
/// Failures are not cached: they are usually transient.
actor ClipPlaceResolver {
    static let shared = ClipPlaceResolver(geocoder: CLGeocoderPlaceReverseGeocoder())

    private let geocoder: any PlaceReverseGeocoding
    private var cache: [String: ClipPlaceWire] = [:]
    private var busy = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    init(geocoder: any PlaceReverseGeocoding) { self.geocoder = geocoder }

    func place(latitude: Double, longitude: Double) async -> ClipPlaceWire? {
        guard CoarseCoordinate.isValid(latitude: latitude, longitude: longitude) else { return nil }
        let key = CoarseCoordinate.key(latitude: latitude, longitude: longitude)
        if let hit = cache[key] { return hit }
        await acquire()
        defer { release() }
        // Another lookup for this spot may have finished while this one waited its turn.
        if let hit = cache[key] { return hit }
        let result = await geocoder.place(latitude: CoarseCoordinate.round(latitude), longitude: CoarseCoordinate.round(longitude))
        if let result { cache[key] = result }
        return result
    }

    /// `place`, but gives up after `timeout` so an attach never waits on a slow geocoder. The lookup keeps
    /// running in the background and still warms the cache for the next clip.
    nonisolated func placeWithTimeout(latitude: Double, longitude: Double, timeout: Duration) async -> ClipPlaceWire? {
        await firstResult(within: timeout) { await self.place(latitude: latitude, longitude: longitude) }
    }

    private func acquire() async {
        if !busy { busy = true; return }
        await withCheckedContinuation { waiters.append($0) }
    }

    private func release() {
        if waiters.isEmpty { busy = false } else { waiters.removeFirst().resume() }
    }
}

/// Returns `work`'s result, or nil once `timeout` passes. Unlike a task group this does not wait for
/// `work` to finish after the deadline.
func firstResult<T: Sendable>(within timeout: Duration, _ work: @escaping @Sendable () async -> T?) async -> T? {
    await withCheckedContinuation { (continuation: CheckedContinuation<T?, Never>) in
        let once = OneShot()
        let worker = Task {
            let value = await work()
            if once.claim() { continuation.resume(returning: value) }
        }
        Task {
            try? await Task.sleep(for: timeout)
            if once.claim() {
                worker.cancel()
                continuation.resume(returning: nil)
            }
        }
    }
}

private final class OneShot: @unchecked Sendable {
    private let lock = NSLock()
    private var claimed = false
    func claim() -> Bool {
        lock.lock(); defer { lock.unlock() }
        if claimed { return false }
        claimed = true
        return true
    }
}
