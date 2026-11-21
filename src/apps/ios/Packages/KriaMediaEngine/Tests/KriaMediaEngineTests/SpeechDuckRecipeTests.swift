import XCTest
@testable import KriaMediaEngine

/// Wire contract for music under speech (KRI-1562). A key the device failed to decode would leave
/// full-level music over the creator's voice with no error, so the snake_case names are pinned here.
final class SpeechDuckRecipeTests: XCTestCase {
    private func recipe(role: TimelineTrackRole?, windows: [SpeechWindow], gain: Double? = nil) -> EditRecipe {
        EditRecipe(assets: [MediaAsset(id: "a", relativePath: "a")], tracks: [
            TimelineTrack(id: "v", kind: .video, clips: [TimelineClip(id: "c", sourceAssetID: "a", sourceDuration: 4)]),
            TimelineTrack(id: "m", kind: .audio, clips: [TimelineClip(id: "m0", sourceAssetID: "a", sourceDuration: 4)], role: role),
        ], audio: AudioMixRecipe(speechWindows: windows, speechDuckGain: gain))
    }

    func testServerKeysDecodeAndAnOlderRecipeEncodesWithoutThem() throws {
        let sent = recipe(role: .music, windows: [SpeechWindow(start: 0.5, end: 1.5)], gain: 0.25)
        let wire = try XCTUnwrap(String(data: RecipeJSON.encode(sent), encoding: .utf8))
        for key in ["\"speech_windows\"", "\"speech_duck_gain\"", "\"role\":\"music\""] { XCTAssertTrue(wire.contains(key), key) }
        XCTAssertEqual(try RecipeJSON.decode(Data(wire.utf8)), sent)

        let plain = try XCTUnwrap(String(data: RecipeJSON.encode(recipe(role: nil, windows: [])), encoding: .utf8))
        for key in ["speech_windows", "speech_duck_gain", "\"role\""] { XCTAssertFalse(plain.contains(key), "\(key) must not appear on a recipe without speech ducking") }
        XCTAssertEqual(try RecipeJSON.decode(Data(plain.utf8)).audio.speechWindows, [])
    }

    func testWindowsNeedAMusicTrackAndAnOldBuildRefusesThem() {
        let ducking = recipe(role: .music, windows: [SpeechWindow(start: 0.5, end: 1.5)])
        XCTAssertTrue(ducking.effectiveCapabilities.contains(.speechDuck))
        XCTAssertFalse(recipe(role: nil, windows: [SpeechWindow(start: 0.5, end: 1.5)]).effectiveCapabilities.contains(.speechDuck))
        XCTAssertFalse(recipe(role: .music, windows: []).effectiveCapabilities.contains(.speechDuck))
        let old = DefaultRendererCapabilities(capabilities: ducking.effectiveCapabilities.subtracting([.speechDuck]))
        XCTAssertEqual(CapabilityNegotiator(provider: old).decide(for: ducking).missingCapabilities, [.speechDuck])
    }
}
