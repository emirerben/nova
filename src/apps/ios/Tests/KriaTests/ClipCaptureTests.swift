import XCTest
import CoreLocation
@testable import Kria

/// KRI-189: when and where a clip was filmed. Everything here is best effort and privacy-first: a
/// coordinate is never kept precise, the whole feature is one setting, and no failure reaches the upload.
final class ClipCaptureTests: XCTestCase {
    private var suite: String!
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        suite = "kria.clip-capture-tests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suite)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suite)
        super.tearDown()
    }

    private let arnavutkoy = (lat: 41.192345, lon: 28.741199)
    private let shot = Date(timeIntervalSince1970: 1_789_889_462) // 2026-09-20T07:31:02Z

    // MARK: setting

    func testSettingDefaultsToOnAndCanBeTurnedOff() {
        XCTAssertTrue(ClipCaptureSetting.isEnabled(defaults), "default is ON")
        ClipCaptureSetting.setEnabled(false, defaults: defaults)
        XCTAssertFalse(ClipCaptureSetting.isEnabled(defaults))
        ClipCaptureSetting.setEnabled(true, defaults: defaults)
        XCTAssertTrue(ClipCaptureSetting.isEnabled(defaults))
    }

    // MARK: coarse coordinates

    func testCoordinatesAreRoundedToTwoDecimals() {
        XCTAssertEqual(CoarseCoordinate.round(41.192345), 41.19)
        XCTAssertEqual(CoarseCoordinate.round(28.746), 28.75)
        XCTAssertEqual(CoarseCoordinate.round(-0.004), 0.0, accuracy: 0.0001)
        XCTAssertEqual(CoarseCoordinate.key(latitude: 41.191, longitude: 28.744), CoarseCoordinate.key(latitude: 41.194, longitude: 28.7441),
                       "clips shot in one spot share a lookup")
    }

    func testRawCaptureNeverHoldsAPreciseCoordinate() {
        let raw = ClipCaptureRaw(captureTime: shot, latitude: arnavutkoy.lat, longitude: arnavutkoy.lon)
        XCTAssertEqual(raw.latitude, 41.19)
        XCTAssertEqual(raw.longitude, 28.74)
        let encoded = String(data: try! JSONEncoder().encode(raw), encoding: .utf8)!
        XCTAssertFalse(encoded.contains("41.192345"), "the precise fix must never be persisted")
    }

    func testInvalidCoordinateIsDroppedButTheDateSurvives() {
        let raw = ClipCaptureRaw(captureTime: shot, latitude: 999, longitude: 28.7)
        XCTAssertNil(raw.latitude)
        XCTAssertNil(raw.longitude)
        XCTAssertEqual(raw.captureTime, shot)
        XCTAssertFalse(ClipCaptureRaw(captureTime: nil, latitude: .nan, longitude: 1).hasLocation)
    }

    // MARK: reading Photos metadata

    func testReaderReturnsNilWhenPhotosHasNothing() {
        XCTAssertNil(ClipCaptureReader.capture(creationDate: nil, coordinate: nil))
    }

    func testReaderKeepsWhateverPartsExist() {
        let dateOnly = ClipCaptureReader.capture(creationDate: shot, coordinate: nil)
        XCTAssertEqual(dateOnly?.captureTime, shot)
        XCTAssertEqual(dateOnly?.hasLocation, false)
        let locationOnly = ClipCaptureReader.capture(creationDate: nil, coordinate: CLLocationCoordinate2D(latitude: arnavutkoy.lat, longitude: arnavutkoy.lon))
        XCTAssertNil(locationOnly?.captureTime)
        XCTAssertEqual(locationOnly?.latitude, 41.19)
    }

    func testReaderReadsNothingWhenTheSettingIsOff() {
        ClipCaptureSetting.setEnabled(false, defaults: defaults)
        XCTAssertNil(ClipCaptureReader.read(assetIdentifier: "any-asset", defaults: defaults))
    }

    // MARK: store

    func testStoreRoundTripsAcrossInstancesAndForgets() {
        let id = UUID()
        let store = ClipCaptureStore(defaults: defaults, key: "k")
        store.set(ClipCaptureRaw(captureTime: shot, latitude: 41.19, longitude: 28.74), for: id)
        let reloaded = ClipCaptureStore(defaults: defaults, key: "k")
        XCTAssertEqual(reloaded.capture(for: id)?.captureTime, shot)
        reloaded.remove(id)
        XCTAssertNil(ClipCaptureStore(defaults: defaults, key: "k").capture(for: id))
    }

    func testStoreDropsStaleEntriesOnLoad() {
        let old = UUID(), fresh = UUID()
        let now = Date()
        let store = ClipCaptureStore(defaults: defaults, key: "k")
        store.set(ClipCaptureRaw(captureTime: shot, latitude: nil, longitude: nil, savedAt: now.addingTimeInterval(-ClipCaptureStore.maxAge - 60)), for: old)
        store.set(ClipCaptureRaw(captureTime: shot, latitude: nil, longitude: nil, savedAt: now), for: fresh)
        let reloaded = ClipCaptureStore(defaults: defaults, key: "k", now: now)
        XCTAssertNil(reloaded.capture(for: old))
        XCTAssertNotNil(reloaded.capture(for: fresh))
    }

    // MARK: wire format

    func testTimestampIsIso8601UtcWithZ() {
        XCTAssertEqual(ClipCaptureWire.isoString(shot), "2026-09-20T07:31:02Z")
    }

    func testWireOmitsWhatIsAbsentAndIsNilWhenEmpty() {
        XCTAssertNil(ClipCaptureWire.make(from: ClipCaptureRaw(captureTime: nil, latitude: nil, longitude: nil), place: nil))
        let dateOnly = ClipCaptureWire.make(from: ClipCaptureRaw(captureTime: shot, latitude: nil, longitude: nil), place: ClipPlaceWire())
        XCTAssertEqual(dateOnly?.captureTime, "2026-09-20T07:31:02Z")
        XCTAssertNil(dateOnly?.coarseLocation)
        XCTAssertNil(dateOnly?.place, "an empty place is not sent")
    }

    private func json(_ media: ProjectMediaInput) throws -> [String: Any] {
        let data = try JSONEncoder().encode(media)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testAttachBodyWithoutCaptureEncodesExactlyAsBefore() throws {
        let body = try json(ProjectMediaInput(mediaID: "m1", gcsPath: "p", kind: "video", filename: "a.mov", contentType: "video/quicktime"))
        XCTAssertEqual(Set(body.keys), ["media_id", "gcs_path", "kind", "filename", "content_type"])
    }

    func testAttachBodyCarriesTimeCoarseLocationAndPlace() throws {
        let raw = ClipCaptureRaw(captureTime: shot, latitude: arnavutkoy.lat, longitude: arnavutkoy.lon)
        let place = ClipPlaceWire(subLocality: "Arnavutköy", locality: "İstanbul", country: "Türkiye")
        let capture = try XCTUnwrap(ClipCaptureWire.make(from: raw, place: place))
        let body = try json(ProjectMediaInput(mediaID: "m1", gcsPath: "p", kind: "video", filename: "a.mov", contentType: "video/quicktime", capture: capture))
        XCTAssertEqual(body["capture_time"] as? String, "2026-09-20T07:31:02Z")
        let location = try XCTUnwrap(body["coarse_location"] as? [String: Double])
        XCTAssertEqual(location, ["lat": 41.19, "lon": 28.74])
        let sentPlace = try XCTUnwrap(body["place"] as? [String: String])
        XCTAssertEqual(sentPlace, ["sub_locality": "Arnavutköy", "locality": "İstanbul", "country": "Türkiye"])
    }

    // MARK: place resolver

    private final class FakeGeocoder: PlaceReverseGeocoding, @unchecked Sendable {
        private let lock = NSLock()
        private var _calls: [String] = []
        private var _active = 0
        private(set) var maxActive = 0
        var answer: ClipPlaceWire?
        var delay: Duration = .zero
        init(answer: ClipPlaceWire?) { self.answer = answer }
        var calls: [String] { lock.lock(); defer { lock.unlock() }; return _calls }

        // Locking lives in synchronous helpers: NSLock is unavailable from async contexts.
        private func begin(_ key: String) {
            lock.lock(); defer { lock.unlock() }
            _calls.append(key); _active += 1; maxActive = max(maxActive, _active)
        }

        private func end() {
            lock.lock(); defer { lock.unlock() }
            _active -= 1
        }

        func place(latitude: Double, longitude: Double) async -> ClipPlaceWire? {
            begin(CoarseCoordinate.key(latitude: latitude, longitude: longitude))
            if delay > .zero { try? await Task.sleep(for: delay) }
            end()
            return answer
        }
    }

    private let place = ClipPlaceWire(subLocality: "Arnavutköy", locality: "İstanbul", country: "Türkiye")

    func testResolverCachesASuccessfulLookupByRoundedCoordinate() async {
        let geocoder = FakeGeocoder(answer: place)
        let resolver = ClipPlaceResolver(geocoder: geocoder)
        let first = await resolver.place(latitude: 41.191, longitude: 28.744)
        let second = await resolver.place(latitude: 41.194, longitude: 28.7441)
        XCTAssertEqual(first, place)
        XCTAssertEqual(second, place)
        XCTAssertEqual(geocoder.calls.count, 1, "the second clip in the same spot is a cache hit")
    }

    func testResolverDoesNotCacheAFailure() async {
        let geocoder = FakeGeocoder(answer: nil)
        let resolver = ClipPlaceResolver(geocoder: geocoder)
        let miss = await resolver.place(latitude: 41.19, longitude: 28.74)
        XCTAssertNil(miss)
        geocoder.answer = place
        let hit = await resolver.place(latitude: 41.19, longitude: 28.74)
        XCTAssertEqual(hit, place, "a transient failure must not poison the spot")
        XCTAssertEqual(geocoder.calls.count, 2)
    }

    func testResolverNeverLooksUpAnInvalidCoordinate() async {
        let geocoder = FakeGeocoder(answer: place)
        let resolver = ClipPlaceResolver(geocoder: geocoder)
        let miss = await resolver.place(latitude: 500, longitude: 0)
        XCTAssertNil(miss)
        XCTAssertTrue(geocoder.calls.isEmpty)
    }

    func testResolverSerializesConcurrentLookups() async {
        let geocoder = FakeGeocoder(answer: place)
        geocoder.delay = .milliseconds(30)
        let resolver = ClipPlaceResolver(geocoder: geocoder)
        await withTaskGroup(of: Void.self) { group in
            for index in 0..<4 {
                group.addTask { _ = await resolver.place(latitude: 40 + Double(index), longitude: 28) }
            }
        }
        XCTAssertEqual(geocoder.maxActive, 1, "CLGeocoder allows one request at a time")
        XCTAssertEqual(geocoder.calls.count, 4)
    }

    func testTimeoutReturnsNilWithoutWaitingAndTheLookupStillWarmsTheCache() async {
        let geocoder = FakeGeocoder(answer: place)
        geocoder.delay = .milliseconds(250)
        let resolver = ClipPlaceResolver(geocoder: geocoder)
        let started = Date()
        let timedOut = await resolver.placeWithTimeout(latitude: 41.19, longitude: 28.74, timeout: .milliseconds(20))
        XCTAssertNil(timedOut)
        XCTAssertLessThan(Date().timeIntervalSince(started), 0.2, "an attach must never wait on a slow geocoder")
        try? await Task.sleep(for: .milliseconds(400))
        let warmed = await resolver.place(latitude: 41.19, longitude: 28.74)
        XCTAssertEqual(warmed, place)
        XCTAssertEqual(geocoder.calls.count, 1, "the timed-out lookup finished in the background and filled the cache")
    }

    // MARK: attach-time assembly

    func testForAttachSendsNothingWhenTheSettingIsOff() async {
        let id = UUID()
        let store = ClipCaptureStore(defaults: defaults, key: "k")
        store.set(ClipCaptureRaw(captureTime: shot, latitude: 41.19, longitude: 28.74), for: id)
        ClipCaptureSetting.setEnabled(false, defaults: defaults)
        let geocoder = FakeGeocoder(answer: place)
        let wire = await ClipCaptureWire.forAttach(recordID: id, store: store, resolver: ClipPlaceResolver(geocoder: geocoder), defaults: defaults)
        XCTAssertNil(wire, "turning the switch off after choosing a clip still sends nothing")
        XCTAssertTrue(geocoder.calls.isEmpty)
    }

    func testForAttachSendsNothingWithoutStoredCapture() async {
        let wire = await ClipCaptureWire.forAttach(recordID: UUID(), store: ClipCaptureStore(defaults: defaults, key: "k"), resolver: ClipPlaceResolver(geocoder: FakeGeocoder(answer: place)), defaults: defaults)
        XCTAssertNil(wire)
    }

    func testForAttachStillSendsTimeAndLocationWhenTheGeocoderFails() async {
        let id = UUID()
        let store = ClipCaptureStore(defaults: defaults, key: "k")
        store.set(ClipCaptureRaw(captureTime: shot, latitude: arnavutkoy.lat, longitude: arnavutkoy.lon), for: id)
        let wire = await ClipCaptureWire.forAttach(recordID: id, store: store, resolver: ClipPlaceResolver(geocoder: FakeGeocoder(answer: nil)), defaults: defaults)
        XCTAssertEqual(wire?.captureTime, "2026-09-20T07:31:02Z")
        XCTAssertEqual(wire?.coarseLocation, CoarseLocationWire(lat: 41.19, lon: 28.74))
        XCTAssertNil(wire?.place)
    }

    func testForAttachAddsThePlaceWhenTheLookupSucceeds() async {
        let id = UUID()
        let store = ClipCaptureStore(defaults: defaults, key: "k")
        store.set(ClipCaptureRaw(captureTime: shot, latitude: arnavutkoy.lat, longitude: arnavutkoy.lon), for: id)
        let wire = await ClipCaptureWire.forAttach(recordID: id, store: store, resolver: ClipPlaceResolver(geocoder: FakeGeocoder(answer: place)), defaults: defaults)
        XCTAssertEqual(wire?.place, place)
    }

    func testForAttachWithADateButNoLocationNeverGeocodes() async {
        let id = UUID()
        let store = ClipCaptureStore(defaults: defaults, key: "k")
        store.set(ClipCaptureRaw(captureTime: shot, latitude: nil, longitude: nil), for: id)
        let geocoder = FakeGeocoder(answer: place)
        let wire = await ClipCaptureWire.forAttach(recordID: id, store: store, resolver: ClipPlaceResolver(geocoder: geocoder), defaults: defaults)
        XCTAssertEqual(wire?.captureTime, "2026-09-20T07:31:02Z")
        XCTAssertNil(wire?.coarseLocation)
        XCTAssertTrue(geocoder.calls.isEmpty)
    }
}
