import XCTest
import CoreLocation
import ImageIO
import UniformTypeIdentifiers
import UIKit
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

    func testISO6709ParsesQuickTimeLocationTags() {
        let istanbul = ClipCaptureReader.parseISO6709("+41.0082+028.9784+012.000/")
        XCTAssertEqual(istanbul?.latitude ?? 0, 41.0082, accuracy: 0.0001)
        XCTAssertEqual(istanbul?.longitude ?? 0, 28.9784, accuracy: 0.0001)
        let south = ClipCaptureReader.parseISO6709("-33.8688+151.2093/")
        XCTAssertEqual(south?.latitude ?? 0, -33.8688, accuracy: 0.0001)
        XCTAssertNil(ClipCaptureReader.parseISO6709("garbage"))
        XCTAssertNil(ClipCaptureReader.parseISO6709("+95.0+028.0/"), "out-of-range latitude is dropped")
    }

    func testFileMetadataFillsOnlyWhatPhotosLeftEmpty() {
        let coordinate = CLLocationCoordinate2D(latitude: arnavutkoy.lat, longitude: arnavutkoy.lon)
        let file = ClipCaptureReader.capture(creationDate: shot, coordinate: coordinate)
        XCTAssertEqual(ClipCaptureReader.merged(photos: nil, file: file)?.captureTime, shot, "no Photos access: the file answers")
        XCTAssertEqual(ClipCaptureReader.merged(photos: nil, file: file)?.latitude, 41.19)
        let photosDateOnly = ClipCaptureReader.capture(creationDate: shot.addingTimeInterval(-60), coordinate: nil)
        let both = ClipCaptureReader.merged(photos: photosDateOnly, file: file)
        XCTAssertEqual(both?.captureTime, shot.addingTimeInterval(-60), "Photos wins where it has a value")
        XCTAssertEqual(both?.latitude, 41.19, "the file fills the missing location")
        XCTAssertNil(ClipCaptureReader.merged(photos: nil, file: nil))
    }

    func testTurningTheSettingOffForgetsRememberedCapture() {
        let store = ClipCaptureStore(defaults: defaults, key: "test-store")
        let id = UUID()
        store.set(ClipCaptureRaw(captureTime: shot, latitude: 41.19, longitude: 28.74), for: id)
        ClipCaptureSetting.settingChanged(to: true, store: store)
        XCTAssertNotNil(store.capture(for: id), "turning it on keeps everything")
        ClipCaptureSetting.settingChanged(to: false, store: store)
        XCTAssertNil(store.capture(for: id))
        XCTAssertNil(ClipCaptureStore(defaults: defaults, key: "test-store").capture(for: id), "and it is gone after relaunch")
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

    // MARK: KRI-300 slide-post photos

    private func writeJPEG(exif: [CFString: Any]?, gps: [CFString: Any]?) throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString).jpg")
        let image = UIGraphicsImageRenderer(size: CGSize(width: 4, height: 4)).image { ctx in
            UIColor.red.setFill(); ctx.fill(CGRect(x: 0, y: 0, width: 4, height: 4))
        }.cgImage!
        let dest = try XCTUnwrap(CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
        var props: [CFString: Any] = [:]
        if let exif { props[kCGImagePropertyExifDictionary] = exif }
        if let gps { props[kCGImagePropertyGPSDictionary] = gps }
        CGImageDestinationAddImage(dest, image, props as CFDictionary)
        XCTAssertTrue(CGImageDestinationFinalize(dest))
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    func testImageFileReadsExifDateAndSignedGPS() async throws {
        let url = try writeJPEG(
            exif: [kCGImagePropertyExifDateTimeOriginal: "2026:09:20 10:31:02", "OffsetTimeOriginal" as CFString: "+03:00"],
            gps: [kCGImagePropertyGPSLatitude: 33.8688123, kCGImagePropertyGPSLatitudeRef: "S", kCGImagePropertyGPSLongitude: 151.2093, kCGImagePropertyGPSLongitudeRef: "E"]
        )
        let read = await ClipCaptureReader.readFile(url)
        let raw = try XCTUnwrap(read)
        XCTAssertEqual(raw.captureTime, shot, "10:31:02 at +03:00 is 07:31:02Z")
        XCTAssertEqual(raw.latitude ?? 0, -33.87, accuracy: 0.0001, "a southern latitude is negative and rounded")
        XCTAssertEqual(raw.longitude ?? 0, 151.21, accuracy: 0.0001)
    }

    func testExifWithoutOffsetPrefersGPSUTCOverDeviceZone() async throws {
        // Taken at 10:31:02 local in Istanbul (+03:00) = 07:31:02Z; no OffsetTimeOriginal, GPS stamp is UTC
        // a few seconds off the shutter (a GPS fix is not the shutter instant).
        let url = try writeJPEG(
            exif: [kCGImagePropertyExifDateTimeOriginal: "2026:09:20 10:31:02"],
            gps: [kCGImagePropertyGPSLatitude: 41.19, kCGImagePropertyGPSLatitudeRef: "N", kCGImagePropertyGPSLongitude: 28.74, kCGImagePropertyGPSLongitudeRef: "E",
                  kCGImagePropertyGPSDateStamp: "2026:09:20", kCGImagePropertyGPSTimeStamp: "07:30:55"]
        )
        let read = await ClipCaptureReader.readFile(url)
        let raw = try XCTUnwrap(read)
        XCTAssertEqual(raw.captureTime, shot, "the GPS UTC stamp fixes the zone, whatever zone the device is in")
    }

    func testExifOffsetWinsOverGPSAndDeviceZoneIsTheLastResort() {
        let gps = Date(timeIntervalSince1970: shot.timeIntervalSince1970 - 7)
        XCTAssertEqual(ClipCaptureReader.parseExifDate("2026:09:20 10:31:02", offset: "+03:00", gpsUTC: nil), shot)
        XCTAssertEqual(ClipCaptureReader.parseExifDate("2026:09:20 10:31:02", offset: "+03:00", gpsUTC: gps), shot)
        XCTAssertEqual(ClipCaptureReader.parseExifDate("2026:09:20 10:31:02", offset: nil, gpsUTC: gps), shot)
        let f = DateFormatter(); f.locale = Locale(identifier: "en_US_POSIX"); f.timeZone = .current; f.dateFormat = "yyyy:MM:dd HH:mm:ss"
        XCTAssertEqual(ClipCaptureReader.parseExifDate("2026:09:20 10:31:02", offset: nil, gpsUTC: nil), f.date(from: "2026:09:20 10:31:02"))
        // A stamp more than 14 h from the naive time is bad data and is ignored.
        let bad = Date(timeIntervalSince1970: shot.timeIntervalSince1970 + 20 * 3600)
        XCTAssertEqual(ClipCaptureReader.parseExifDate("2026:09:20 10:31:02", offset: nil, gpsUTC: bad), f.date(from: "2026:09:20 10:31:02"))
    }

    func testImageWithNoImageExtensionStillReadsExif() async throws {
        let jpg = try writeJPEG(exif: [kCGImagePropertyExifDateTimeOriginal: "2026:09:20 10:31:02", "OffsetTimeOriginal" as CFString: "+03:00"], gps: nil)
        let bare = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.copyItem(at: jpg, to: bare)
        addTeardownBlock { try? FileManager.default.removeItem(at: bare) }
        let raw = await ClipCaptureReader.readFile(bare)
        XCTAssertEqual(raw?.captureTime, shot)
    }

    func testImageFileWithoutMetadataYieldsNothing() async throws {
        let url = try writeJPEG(exif: nil, gps: nil)
        let raw = await ClipCaptureReader.readFile(url)
        XCTAssertNil(raw)
    }

    func testVisualReadHonoursTheSettingForImages() async throws {
        let url = try writeJPEG(exif: [kCGImagePropertyExifDateTimeOriginal: "2026:09:20 10:31:02"], gps: nil)
        ClipCaptureSetting.setEnabled(false, defaults: defaults)
        let off = await ClipCaptureReader.read(assetIdentifier: "none", fileURL: url, defaults: defaults)
        XCTAssertNil(off, "the setting off reads nothing from the photo")
        ClipCaptureSetting.setEnabled(true, defaults: defaults)
        let on = await ClipCaptureReader.read(assetIdentifier: "none", fileURL: url, defaults: defaults)
        XCTAssertNotNil(on?.captureTime)
    }

    func testRegisterBodyFieldsOmitAbsentPartsAndCarryPresentOnes() {
        XCTAssertTrue(ClipCaptureWire().jsonFields.isEmpty)
        let wire = ClipCaptureWire.make(from: ClipCaptureRaw(captureTime: shot, latitude: arnavutkoy.lat, longitude: arnavutkoy.lon), place: place)
        let fields = wire?.jsonFields ?? [:]
        XCTAssertEqual(fields["capture_time"]?.stringValue, "2026-09-20T07:31:02Z")
        XCTAssertEqual(fields["coarse_location"]?.objectValue?["lat"]?.numberValue, 41.19)
        XCTAssertNotNil(fields["place"]?.objectValue)
    }

    func testSlidePostAssetDecodesCaptureAndToleratesItsAbsence() throws {
        let with = #"{"id":"a","kind":"image","status":"ready","capture":{"capture_time":"2026-09-20T07:31:02Z","coarse_location":{"lat":41.19,"lon":28.74},"place":{"locality":"Istanbul","country":"Türkiye"}}}"#
        let asset = try JSONDecoder().decode(SlidePostAsset.self, from: Data(with.utf8))
        XCTAssertEqual(asset.capture?.date, shot)
        XCTAssertEqual(asset.capture?.place?.locality, "Istanbul")
        XCTAssertEqual(asset.capture?.coarseLocation?.lon, 28.74)
        let without = try JSONDecoder().decode(SlidePostAsset.self, from: Data(#"{"id":"a","kind":"image","status":"ready"}"#.utf8))
        XCTAssertNil(without.capture)
    }
}
